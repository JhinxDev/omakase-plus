"""Per-user TV catalog and explicit Plex observations using the existing safe transport.

TV catalog data is not playback proof. This module never submits acquisition,
changes Plex history or writes another media domain.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from html.parser import HTMLParser

from omakase.plus.movies import MovieError, MovieService, _art, _imdb, _iso, _positive, _text
from omakase.plus.movies_store import require_user

UNSET = object()


class TVError(ValueError):
    def __init__(self, detail, code="invalid_input"):
        self.detail, self.code = detail, code
        super().__init__(detail)

    def as_dict(self):
        return {"code": self.code, "detail": self.detail}


class _Plain(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"br", "p", "div"}:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(value, maximum=5000):
    value = _text(value, maximum)
    parser = _Plain()
    parser.feed(value or "")
    return " ".join("".join(parser.parts).split())


def number(value, maximum, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        raise TVError("Invalid TV number.")
    return value


def air_date(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise TVError("Invalid catalog air date.", "invalid_metadata")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise TVError("Invalid catalog air date.", "invalid_metadata") from None


class TVService(MovieService):
    """Reuse operator origins, encrypted credentials, pinned outbound policy and limits."""

    def _owner(self, user_id):
        try:
            require_user(self.db, user_id)
        except ValueError:
            raise TVError("Invalid TV owner.") from None

    def _online(self, user_id):
        self._owner(user_id)
        self._guard(user_id)

    def _raw(self, user_id, tmdb_id):
        self._owner(user_id)
        _positive(tmdb_id)
        row = self.db.execute(
            "SELECT metadata_json FROM tv_catalog WHERE user_id=? AND tmdb_id=?", (user_id, tmdb_id)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, user_id, metadata, *, collection=False):
        old = self._raw(user_id, metadata["tmdb_id"])
        if old and old.get("imdb_id") and metadata.get("imdb_id") != old["imdb_id"]:
            raise TVError("Catalog identity changed; import stopped.", "identity_mismatch")
        metadata = dict(metadata)
        metadata["in_collection"] = collection or bool(old and old.get("in_collection"))
        if old:
            loaded = {
                row["season_number"] for row in old.get("seasons", []) if row.get("episodes_loaded")
            }
            metadata["seasons"] = [
                {
                    **row,
                    "episodes_loaded": row.get("episodes_loaded", row["season_number"] in loaded),
                }
                for row in metadata.get("seasons", [])
            ]
        self.db.execute(
            "INSERT INTO tv_catalog VALUES(?,?,?,?) ON CONFLICT(user_id,tmdb_id) DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
            (user_id, metadata["tmdb_id"], json.dumps(metadata), _iso(self._clock())),
        )
        self.db.commit()

    def _plex_scope(self):
        return hashlib.sha256(
            (self.connections.plex_url + "\0" + self.connections.plex_token).encode()
        ).hexdigest()

    def _released(self, value):
        aired = air_date(value)
        return bool(aired and aired <= self._clock().date().isoformat())

    def card(self, user_id, metadata):
        card = dict(metadata)
        tmdb_id = card["tmdb_id"]
        history = self.db.execute(
            "SELECT * FROM tv_history WHERE user_id=? AND tmdb_id=?", (user_id, tmdb_id)
        ).fetchone()
        card.update(
            dict(history)
            if history
            else {
                "status": "unwatched",
                "rating": None,
                "status_source": "none",
                "rating_source": "none",
            }
        )
        card.pop("user_id", None)
        episodes = self.db.execute(
            "SELECT * FROM tv_episodes WHERE user_id=? AND tmdb_id=? ORDER BY season_number,episode_number",
            (user_id, tmdb_id),
        ).fetchall()
        snapshot = self.db.execute(
            "SELECT * FROM tv_plex_snapshots WHERE user_id=? AND tmdb_id=?", (user_id, tmdb_id)
        ).fetchone()
        current_scope = bool(
            snapshot and self.connections.enabled and snapshot["source_scope"] == self._plex_scope()
        )
        by_season = {}
        for row in episodes:
            episode = json.loads(row["metadata_json"])
            episode.update(
                released=self._released(episode.get("air_date")),
                watched=bool(row["watched"]),
                watch_source=row["watch_source"],
                in_plex=bool(current_scope and row["plex_present"]),
                available=None,
            )
            by_season.setdefault(row["season_number"], []).append(episode)
        card["seasons"] = [
            {
                **s,
                "released": self._released(s.get("air_date")),
                "episodes": by_season.get(s["season_number"], []),
                "episodes_loaded": s.get("episodes_loaded", False),
            }
            for s in card.get("seasons", [])
        ]
        observed_seasons = sorted({r["season_number"] for r in episodes if r["plex_present"]})
        current_seasons = observed_seasons if current_scope else []
        season_coverage = []
        relevant = [
            s for s in card["seasons"] if s["released"] or s["season_number"] in current_seasons
        ]
        for season in relevant:
            rows = season["episodes"]
            # Inventory pagination completeness is not expected-season completeness.
            # Only a loaded full catalog with known air dates can define released sets.
            reconciled = bool(
                current_scope
                and snapshot["complete"]
                and season["episodes_loaded"]
                and len(rows) == season.get("episode_count")
                and rows
                and all(e.get("air_date") for e in rows)
            )
            if not reconciled:
                continue
            released = [e for e in rows if e["released"]]
            if not released:
                continue
            present = sum(e["in_plex"] for e in released)
            season_coverage.append(
                {
                    "season_number": season["season_number"],
                    "released_total": len(released),
                    "in_plex": present,
                    "state": "complete"
                    if present == len(released)
                    else "partial"
                    if present
                    else "missing",
                }
            )
        coverage_known = bool(
            relevant
            and len(season_coverage) == len(relevant)
            and set(current_seasons) <= {s["season_number"] for s in relevant}
        )
        if current_scope and current_seasons:
            if coverage_known:
                summary = " · ".join(
                    f"{'Specials' if s['season_number'] == 0 else 'S' + str(s['season_number'])} {s['state']}"
                    for s in season_coverage
                )
            else:
                count = sum(number > 0 for number in current_seasons)
                has_specials = 0 in current_seasons
                if count:
                    summary = f"{count} {'season' if count == 1 else 'seasons'}"
                    if has_specials:
                        summary += " + specials"
                    summary += " in Plex"
                else:
                    summary = "Specials in Plex"
        elif snapshot and observed_seasons and not current_scope:
            summary = "Last known in Plex"
        else:
            summary = "Availability unknown"
        card.update(
            plex_summary=summary,
            plex_snapshot_current=current_scope,
            plex_season_ids=current_seasons,
            plex_season_coverage=season_coverage,
            season_coverage_known=coverage_known,
            plex_indexed_episodes=sum(r["plex_present"] for r in episodes) if current_scope else 0,
            watch_status_label=(
                card["status"].replace("_", " ").title()
                if card.get("status_source") not in (None, "none", "")
                else "Not tracked"
            ),
            watched_episodes=sum(r["watched"] for r in episodes),
            in_plex_episodes=sum(r["plex_present"] for r in episodes) if current_scope else 0,
            available_episodes=0,
            coverage_complete=bool(current_scope and snapshot["complete"]),
            plex_url=self._plex_link(snapshot["plex_key"]) if current_scope else None,
            plex_observed_at=snapshot["observed_at"] if snapshot else None,
            request_supported=False,
            request_unavailable_reason="TV season requests need manual backend configuration; no documented native per-request season subset is available.",
        )
        card["detail"] = (
            "Plex inventory observed; media bytes have not been verified."
            if current_scope
            else "Catalog metadata only; playback availability has not been checked."
        )
        for field, variant in (("poster_url", "poster"), ("backdrop_url", "backdrop")):
            if card.get(field):
                card[field] = f"/plus/tv/{tmdb_id}/artwork?variant={variant}"
        return card

    async def _metadata_tv(self, tmdb_id):
        _positive(tmdb_id)
        data = await self._json("seerr", f"/api/v1/tv/{tmdb_id}")
        if type(data.get("id")) is not int or data["id"] != tmdb_id:
            raise TVError("TV catalog identity does not agree.", "identity_mismatch")
        external = data.get("externalIds") or {}
        if not isinstance(external, dict):
            raise TVError("Invalid TV catalog identity.", "invalid_metadata")
        imdb = external.get("imdbId") or data.get("imdbId")
        if imdb:
            _imdb(imdb)
        if data.get("imdbId") and external.get("imdbId") and data["imdbId"] != external["imdbId"]:
            raise TVError("Conflicting TV catalog identities.", "identity_mismatch")
        seasons, seen = [], set()
        rows = data.get("seasons", [])
        if not isinstance(rows, list) or len(rows) > 201:
            raise TVError("TV season catalog is too large.", "invalid_metadata")
        for row in rows:
            if not isinstance(row, dict):
                raise TVError("Invalid TV season metadata.", "invalid_metadata")
            n = number(row.get("seasonNumber"), 200, zero=True)
            if n in seen:
                raise TVError("Duplicate TV season.", "invalid_metadata")
            seen.add(n)
            aired = air_date(row.get("airDate"))
            seasons.append(
                {
                    "season_number": n,
                    "name": plain(row.get("name"), 300) or f"Season {n}",
                    "air_date": aired,
                    "released": bool(aired and aired <= self._clock().date().isoformat()),
                    "episode_count": number(row.get("episodeCount", 0), 1000, zero=True),
                }
            )
        first = air_date(data.get("firstAirDate"))
        return {
            "tmdb_id": tmdb_id,
            "imdb_id": imdb,
            "title": plain(_text(data.get("name"), 300, required=True), 300),
            "year": int(first[:4]) if first else None,
            "overview": plain(data.get("overview")),
            "poster_url": _art(data.get("posterPath"), "w342"),
            "backdrop_url": _art(data.get("backdropPath"), "w1280"),
            "seasons": sorted(seasons, key=lambda s: s["season_number"]),
        }

    async def collection_page(self, user_id, *, view="all", offset=0):
        self._owner(user_id)
        if view not in {"all", "watching", "completed"}:
            raise TVError("Invalid TV collection view.")
        number(offset, 10000, zero=True)
        page_size = 100
        where = "c.user_id=? AND json_extract(c.metadata_json,'$.in_collection')=1"
        values = [user_id]
        if view != "all":
            where += " AND h.status=?"
            values.append(view)
        source = (
            "tv_catalog c LEFT JOIN tv_history h ON h.user_id=c.user_id AND h.tmdb_id=c.tmdb_id"
        )
        total = self.db.execute(f"SELECT count(*) FROM {source} WHERE {where}", values).fetchone()[
            0
        ]
        rows = self.db.execute(
            f"SELECT c.metadata_json FROM {source} WHERE {where} ORDER BY c.updated_at DESC,c.tmdb_id ASC LIMIT ? OFFSET ?",
            (*values, page_size, offset),
        ).fetchall()
        cards = [self.card(user_id, json.loads(row[0])) for row in rows]
        next_offset = offset + len(cards)
        return {
            "cards": cards,
            "total": total,
            "offset": offset,
            "page_size": page_size,
            "next_offset": next_offset if next_offset < total else None,
            "previous_offset": max(0, offset - page_size) if offset else None,
            "has_more": next_offset < total,
        }

    async def list(self, user_id, *, view="all", offset=0):
        return (await self.collection_page(user_id, view=view, offset=offset))["cards"]

    async def search(self, user_id, query):
        self._online(user_id)
        if (
            not isinstance(query, str)
            or not 1 <= len(query.strip()) <= 200
            or any(ord(c) < 32 for c in query)
        ):
            raise TVError("Enter a TV search of 1–200 characters.")
        if re.fullmatch(r"[1-9][0-9]{0,9}", query.strip()):
            return [await self.detail(user_id, _positive(int(query.strip())))]
        data = await self._json(
            "seerr", "/api/v1/search", params={"query": query.strip(), "page": 1}
        )
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) > 100:
            raise TVError("Invalid TV search response.", "invalid_metadata")
        cards, seen = [], set()
        for row in rows:
            if not isinstance(row, dict) or row.get("mediaType") != "tv":
                continue
            tmdb = _positive(row.get("id"))
            if tmdb in seen:
                continue
            seen.add(tmdb)
            first = air_date(row.get("firstAirDate"))
            metadata = {
                "tmdb_id": tmdb,
                "imdb_id": None,
                "title": plain(_text(row.get("name"), 300, required=True), 300),
                "year": int(first[:4]) if first else None,
                "overview": plain(row.get("overview")),
                "poster_url": _art(row.get("posterPath"), "w342"),
                "backdrop_url": _art(row.get("backdropPath"), "w1280"),
                "seasons": [],
                "summary_only": True,
            }
            existing = self._raw(user_id, tmdb)
            if not existing:
                self._save(user_id, metadata)
            cards.append(self.card(user_id, existing or metadata))
            if len(cards) >= 20:
                break
        return cards

    async def detail(self, user_id, tmdb_id):
        existing = self._raw(user_id, tmdb_id)
        if not existing or existing.get("summary_only"):
            self._online(user_id)
            existing = await self._metadata_tv(tmdb_id)
            self._save(user_id, existing)
        return self.card(user_id, existing)

    async def refresh(self, user_id, tmdb_id):
        """Explicit catalog refresh keeps saved membership and personal state."""
        self._online(user_id)
        metadata = await self._metadata_tv(tmdb_id)
        for row in metadata["seasons"]:
            row["episodes_loaded"] = False
        self._save(user_id, metadata)
        return self.card(user_id, self._raw(user_id, tmdb_id))

    async def season(self, user_id, tmdb_id, season_number):
        self._online(user_id)
        await self.detail(user_id, tmdb_id)
        metadata = self._raw(user_id, tmdb_id)
        number(season_number, 200, zero=True)
        season = next((s for s in metadata["seasons"] if s["season_number"] == season_number), None)
        if season is None:
            raise TVError("Season does not belong to this TV series.")
        data = await self._json("seerr", f"/api/v1/tv/{tmdb_id}/season/{season_number}")
        if type(data.get("seasonNumber")) is not int or data["seasonNumber"] != season_number:
            raise TVError("TV season identity does not agree.", "identity_mismatch")
        rows = data.get("episodes")
        if not isinstance(rows, list) or len(rows) > 1000:
            raise TVError("Invalid TV episode catalog.", "invalid_metadata")
        parsed, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise TVError("Invalid TV episode metadata.", "invalid_metadata")
            if (
                type(row.get("seasonNumber", season_number)) is not int
                or row.get("seasonNumber", season_number) != season_number
            ):
                raise TVError("Episode belongs to another season.", "identity_mismatch")
            n = number(row.get("episodeNumber"), 1000)
            if n in seen:
                raise TVError("Duplicate TV episode.", "invalid_metadata")
            seen.add(n)
            aired = air_date(row.get("airDate"))
            parsed.append(
                {
                    "episode_number": n,
                    "title": plain(row.get("name"), 300) or f"Episode {n}",
                    "air_date": aired,
                    "released": bool(aired and aired <= self._clock().date().isoformat()),
                    "overview": plain(row.get("overview")),
                }
            )
        now = _iso(self._clock())
        with self.db:
            for episode in parsed:
                self.db.execute(
                    "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,tmdb_id,season_number,episode_number) DO UPDATE SET metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",
                    (
                        user_id,
                        tmdb_id,
                        season_number,
                        episode["episode_number"],
                        json.dumps(episode),
                        now,
                    ),
                )
        season["episodes_loaded"] = True
        season["episode_count"] = len(parsed)
        self._save(user_id, metadata)
        return self.card(user_id, metadata)

    async def save_history(self, user_id, tmdb_id, *, status=UNSET, rating=UNSET):
        await self.detail(user_id, tmdb_id)
        if status is not UNSET and status not in {"unwatched", "watching", "completed", "dropped"}:
            raise TVError("Invalid TV watch status.")
        if (
            rating is not UNSET
            and rating is not None
            and (
                type(rating) not in {int, float}
                or not math.isfinite(rating)
                or not 1 <= rating <= 10
                or rating * 2 != int(rating * 2)
            )
        ):
            raise TVError("Choose a TV rating from 1–10 in half steps.")
        now = _iso(self._clock())
        with self.db:
            self.db.execute(
                "INSERT INTO tv_history(user_id,tmdb_id,updated_at) VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (user_id, tmdb_id, now),
            )
            if status is not UNSET:
                self.db.execute(
                    "UPDATE tv_history SET status=?,status_source='manual',updated_at=? WHERE user_id=? AND tmdb_id=?",
                    (status, now, user_id, tmdb_id),
                )
            if rating is not UNSET:
                self.db.execute(
                    "UPDATE tv_history SET rating=?,rating_source='manual',updated_at=? WHERE user_id=? AND tmdb_id=?",
                    (rating, now, user_id, tmdb_id),
                )
        self._save(user_id, self._raw(user_id, tmdb_id), collection=True)
        return self.card(user_id, self._raw(user_id, tmdb_id))

    async def save_episode(self, user_id, tmdb_id, season_number, episode_number, watched):
        self._owner(user_id)
        _positive(tmdb_id)
        number(season_number, 200, zero=True)
        number(episode_number, 1000)
        if type(watched) is not bool:
            raise TVError("Invalid episode watch state.")
        row = self.db.execute(
            "SELECT metadata_json FROM tv_episodes WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
            (user_id, tmdb_id, season_number, episode_number),
        ).fetchone()
        if not row or (watched and not self._released(json.loads(row[0]).get("air_date"))):
            raise TVError("Load this released episode before recording watched state.")
        with self.db:
            self.db.execute(
                "UPDATE tv_episodes SET watched=?,watch_source='manual',updated_at=? WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
                (
                    int(watched),
                    _iso(self._clock()),
                    user_id,
                    tmdb_id,
                    season_number,
                    episode_number,
                ),
            )
        self._save(user_id, self._raw(user_id, tmdb_id), collection=True)
        return self.card(user_id, self._raw(user_id, tmdb_id))

    async def _tv_section(self):
        root = await self._xml("/library/sections")
        matches = [r for r in root.findall("Directory") if r.get("title") == "TV Shows"]
        if len(matches) != 1 or matches[0].get("type") != "show":
            raise TVError("A unique TV Shows library is required.", "plex_library_mismatch")
        return _positive(matches[0].get("key"), native_string=True)

    def _plex_identity(self, row):
        ids = self._guids(row, "tmdb")
        if len(ids) != 1:
            raise TVError("Plex series needs one exact TMDB identity.", "identity_mismatch")
        return _positive(next(iter(ids)), native_string=True)

    async def _show_page(self, section, offset):
        root = await self._xml(
            f"/library/sections/{section}/all",
            params={
                "type": "2",
                "includeGuids": "1",
                "X-Plex-Container-Start": str(offset),
                "X-Plex-Container-Size": "25",
            },
        )
        total = (
            _positive(root.get("totalSize", "0"), native_string=True)
            if root.get("totalSize", "0") != "0"
            else 0
        )
        rows = root.findall("Directory")
        if (
            root.get("totalSize") is None
            or len(rows) > 25
            or total > 10000
            or root.get("offset", str(offset)) != str(offset)
            or len(rows) != min(25, max(0, total - offset))
        ):
            raise TVError("Plex TV paging is incomplete.", "inventory_incomplete")
        for row in rows:
            if row.get("type") != "show" or row.get(
                "librarySectionID", root.get("librarySectionID", str(section))
            ) != str(section):
                raise TVError("Plex series belongs to another library.", "plex_library_mismatch")
        return rows, total

    async def sync(self, user_id, *, offset=0):
        self._online(user_id)
        number(offset, 10000, zero=True)
        section = await self._tv_section()
        rows, total = await self._show_page(section, offset)
        imported = skipped = 0
        for row in rows:
            try:
                tmdb = self._plex_identity(row)
            except (TVError, MovieError):
                skipped += 1
                continue
            metadata = await self._metadata_tv(tmdb)
            imdbs = self._guids(row, "imdb")
            if imdbs and (len(imdbs) != 1 or metadata.get("imdb_id") not in imdbs):
                skipped += 1
                continue
            self._save(user_id, metadata, collection=True)
            imported += 1
        next_offset = offset + len(rows)
        return {
            "imported": imported,
            "skipped": skipped,
            "remaining": max(0, total - next_offset),
            "next_offset": next_offset,
            "total": total,
            "coverage_complete": next_offset >= total,
        }

    async def check(self, user_id, tmdb_id):
        """Explicit metadata/history import. No file presence becomes playable proof."""
        self._online(user_id)
        await self.detail(user_id, tmdb_id)
        metadata = self._raw(user_id, tmdb_id)
        section = await self._tv_section()
        matches = []
        expected_total = None
        for offset in range(0, 1000, 25):
            rows, total = await self._show_page(section, offset)
            if expected_total is not None and total != expected_total:
                raise TVError("TV inventory changed during the check.", "inventory_incomplete")
            expected_total = total
            if total > 1000:
                raise TVError("TV inventory exceeds the bounded check.", "inventory_incomplete")
            for row in rows:
                try:
                    if self._plex_identity(row) == tmdb_id:
                        matches.append(row)
                except (TVError, MovieError):
                    continue
            if offset + len(rows) >= total:
                break
        if len(matches) != 1:
            raise TVError("One exact TV series match was not found in Plex.", "identity_mismatch")
        show = matches[0]
        imdbs = self._guids(show, "imdb")
        if imdbs and (len(imdbs) != 1 or metadata.get("imdb_id") not in imdbs):
            raise TVError("Plex external TV identities conflict.", "identity_mismatch")
        key = str(_positive(show.get("ratingKey"), native_string=True))
        parsed, seen = [], set()
        expected_total = None
        for offset in range(0, 1000, 100):
            root = await self._xml(
                f"/library/metadata/{key}/allLeaves",
                params={
                    "includeGuids": "1",
                    "X-Plex-Container-Start": str(offset),
                    "X-Plex-Container-Size": "100",
                },
            )
            rows = root.findall("Video")
            total_text = root.get("totalSize", "")
            if (
                not re.fullmatch(r"[0-9]{1,4}", total_text)
                or int(total_text) > 1000
                or len(rows) > 100
                or root.get("offset", str(offset)) != str(offset)
            ):
                raise TVError(
                    "TV episode coverage exceeds the bounded check.", "inventory_incomplete"
                )
            total = int(total_text)
            if expected_total is not None and total != expected_total:
                raise TVError("TV episodes changed during the check.", "inventory_incomplete")
            expected_total = total
            if len(rows) != min(100, max(0, total - offset)):
                raise TVError("TV episode paging stalled.", "inventory_incomplete")
            for row in rows:
                if (
                    row.get("type") != "episode"
                    or row.get("grandparentRatingKey") != key
                    or row.get("librarySectionID", root.get("librarySectionID", str(section)))
                    != str(section)
                ):
                    raise TVError(
                        "Plex episode series identity does not agree.", "identity_mismatch"
                    )
                if not re.fullmatch(r"[0-9]{1,3}", row.get("parentIndex", "")) or not re.fullmatch(
                    r"[1-9][0-9]{0,3}", row.get("index", "")
                ):
                    raise TVError("Invalid Plex episode numbering.", "invalid_metadata")
                s = number(int(row.get("parentIndex")), 200, zero=True)
                e = number(int(row.get("index")), 1000)
                if s not in {season["season_number"] for season in metadata.get("seasons", [])}:
                    raise TVError("Plex season is absent from the TV catalog.", "identity_mismatch")
                if (s, e) in seen:
                    raise TVError("Plex episode numbering is ambiguous.", "identity_mismatch")
                seen.add((s, e))
                aired = air_date(row.get("originallyAvailableAt"))
                count = row.get("viewCount", "0")
                if not re.fullmatch(r"[0-9]{1,9}", count):
                    raise TVError("Invalid Plex episode watch count.", "invalid_metadata")
                parsed.append(
                    (
                        s,
                        e,
                        {
                            "episode_number": e,
                            "title": plain(row.get("title"), 300) or f"Episode {e}",
                            "air_date": aired,
                            "released": bool(aired and aired <= self._clock().date().isoformat()),
                            "overview": plain(row.get("summary")),
                        },
                        int(count) > 0,
                    )
                )
            if offset + len(rows) >= total:
                break
        now = _iso(self._clock())
        with self.db:
            self.db.execute(
                "UPDATE tv_episodes SET plex_present=0 WHERE user_id=? AND tmdb_id=?",
                (user_id, tmdb_id),
            )
            for s, e, episode, watched in parsed:
                self.db.execute(
                    "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(?,?,?,?,?,?,'plex',1,?) ON CONFLICT(user_id,tmdb_id,season_number,episode_number) DO UPDATE SET plex_present=1,watched=CASE WHEN watch_source='manual' THEN watched ELSE excluded.watched END,watch_source=CASE WHEN watch_source='manual' THEN watch_source ELSE 'plex' END,updated_at=excluded.updated_at",
                    (user_id, tmdb_id, s, e, json.dumps(episode), int(watched), now),
                )
            scope = self._plex_scope()
            self.db.execute(
                "INSERT INTO tv_plex_snapshots VALUES(?,?,?,?,1,?) ON CONFLICT(user_id,tmdb_id) DO UPDATE SET source_scope=excluded.source_scope,plex_key=excluded.plex_key,complete=1,observed_at=excluded.observed_at",
                (user_id, tmdb_id, scope, key, now),
            )
        self._save(user_id, metadata, collection=True)
        return self.card(user_id, metadata)
