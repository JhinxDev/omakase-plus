"""Direct read-only AniList search with owner-scoped watch enrichment."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import OrderedDict
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import httpx
from cryptography.exceptions import InvalidTag

from omakase.plus.movies_store import require_user
from omakase.plus.outbound import OutboundError, pin_destination
from omakase.plus.secrets import read_secret

_ENDPOINT = "https://graphql.anilist.co"
_MAX_BYTES = 524288
_TIMEOUT = 12.0
_PUBLIC_CACHE: OrderedDict = OrderedDict()
_MESSAGES = {
    "invalid_query": "Search with 2 to 120 characters.",
    "invalid_identity": "Choose a valid AniList anime identity.",
    "not_found": "This anime could not be found in AniList.",
    "invalid_response": "AniList returned an incompatible response.",
    "rate_limited": "AniList requested a pause. Try again after the displayed delay.",
    "unavailable": "AniList is temporarily unavailable.",
    "transport_error": "AniList could not be reached.",
    "timeout": "The AniList search timed out.",
    "authentication_failed": "AniList could not verify this connection. Reconnect it in Settings.",
}
_FIELDS = """id type title { english romaji native } seasonYear startDate { year }
format episodes status(version:2) description(asHtml:false) genres
studios(isMain:true) { nodes { name } }"""
_PRIVATE = "mediaListEntry { mediaId userId status progress score(format:POINT_10_DECIMAL) }"


class CatalogSearchError(ValueError):
    def __init__(self, code: str, *, retry_after: int | None = None) -> None:
        self.code = code if code in _MESSAGES else "unavailable"
        self.detail, self.retry_after = _MESSAGES[self.code], retry_after
        self.status_code = {
            "invalid_query": 422,
            "invalid_identity": 422,
            "not_found": 404,
            "rate_limited": 429,
        }.get(self.code, 503)
        super().__init__(self.detail)


class _Plaintext(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts, self.suppressed = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object", "template"}:
            self.suppressed += 1
        elif tag in {"br", "p", "div", "li"} and not self.suppressed:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object", "template"} and self.suppressed:
            self.suppressed -= 1
        elif tag in {"p", "div", "li"} and not self.suppressed:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def _plain(value: Any, maximum: int = 20000) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > maximum:
        raise CatalogSearchError("invalid_response")
    parser = _Plaintext()
    parser.feed(value)
    parser.close()
    result = re.sub(r"<[^>]*>", "", "".join(parser.parts)).replace("<", "").replace(">", "")
    result = "\n".join(" ".join(line.split()) for line in result.splitlines()).strip()
    return re.sub(r"https?://\S+", "[link omitted]", result, flags=re.I)


def _id(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 2147483647:
        raise CatalogSearchError("invalid_identity")
    return value


def _retry(value: str | None) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value) and int(value) <= 2147483647:
        return int(value)
    try:
        parsed = parsedate_to_datetime(value or "")
        if parsed.tzinfo is not None:
            return max(0, math.ceil((parsed - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        pass
    return 60


class AnimeCatalogSearch:
    def __init__(self, db, user_id: int, transport=None) -> None:
        require_user(db, user_id)
        self.db, self.user_id, self._transport = db, user_id, transport
        # Injected fixtures have an isolated public cache; production shares at
        # most64 short-lived public records. Private entries are never cached.
        self._public = _PUBLIC_CACHE if transport is None else OrderedDict()

    def _token(self) -> str | None:
        try:
            token = read_secret(self.db, self.user_id, "anilist_oauth_token")
        except (InvalidTag, ValueError):
            return None
        if (
            token
            and isinstance(token, str)
            and len(token) <= 4096
            and all(33 <= ord(char) <= 126 for char in token)
        ):
            return token
        return None

    async def _request(self, query: str, variables: dict, token: str | None) -> dict:
        async def fetch():
            try:
                destination = await pin_destination(_ENDPOINT, transport=self._transport)
            except OutboundError:
                raise CatalogSearchError("transport_error") from None
            headers = {"Accept": "application/json", "Host": destination.host_header}
            if token:
                headers["Authorization"] = "Bearer " + token
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(8, connect=3),
                    trust_env=False,
                    follow_redirects=False,
                    transport=self._transport,
                ) as client:
                    async with client.stream(
                        "POST",
                        destination.base_url,
                        headers=headers,
                        json={"query": query, "variables": variables},
                        extensions={"sni_hostname": destination.sni_hostname},
                    ) as response:
                        if response.status_code == 429:
                            raise CatalogSearchError(
                                "rate_limited",
                                retry_after=_retry(response.headers.get("Retry-After")),
                            )
                        if response.status_code in {401, 403}:
                            raise CatalogSearchError(
                                "authentication_failed" if token else "unavailable"
                            )
                        if response.status_code != 200:
                            raise CatalogSearchError("unavailable")
                        body = bytearray()
                        async for chunk in response.aiter_bytes(chunk_size=16384):
                            if len(body) + len(chunk) > _MAX_BYTES:
                                raise CatalogSearchError("invalid_response")
                            body.extend(chunk)
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise CatalogSearchError("invalid_response") from None
                if not isinstance(payload, dict):
                    raise CatalogSearchError("invalid_response")
                errors = payload.get("errors") or []
                if not isinstance(errors, list) or len(errors) > 20:
                    raise CatalogSearchError("invalid_response")
                codes = {
                    item.get("status")
                    for item in errors
                    if isinstance(item, dict) and type(item.get("status")) is int
                }
                if 429 in codes:
                    raise CatalogSearchError(
                        "rate_limited", retry_after=_retry(response.headers.get("Retry-After"))
                    )
                if not isinstance(payload.get("data"), dict):
                    if token and codes & {401, 403}:
                        raise CatalogSearchError("authentication_failed")
                    raise CatalogSearchError("invalid_response")
                data = dict(payload["data"])
                if errors:
                    # Partial metadata is useful, but failed private fields must
                    # never be promoted to authenticated viewing evidence.
                    data["Viewer"] = None
                return data
            except httpx.TimeoutException:
                raise CatalogSearchError("timeout") from None
            except httpx.HTTPError:
                raise CatalogSearchError("transport_error") from None

        try:
            return await asyncio.wait_for(fetch(), timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            raise CatalogSearchError("timeout") from None

    def _metadata(self, raw: Any, expected: int | None = None) -> dict:
        if not isinstance(raw, dict):
            raise CatalogSearchError("not_found" if raw is None else "invalid_response")
        media_id = _id(raw.get("id"))
        if (expected is not None and media_id != expected) or raw.get("type", "ANIME") != "ANIME":
            raise CatalogSearchError("invalid_response")
        title = raw.get("title")
        if not isinstance(title, dict):
            raise CatalogSearchError("invalid_response")
        name = next(
            (
                _plain(title.get(key), 300)
                for key in ("english", "romaji", "native")
                if title.get(key)
            ),
            "",
        )
        if not name:
            raise CatalogSearchError("invalid_response")
        start_date = raw.get("startDate")
        if start_date is not None and not isinstance(start_date, dict):
            raise CatalogSearchError("invalid_response")
        year = raw.get("seasonYear") or (start_date or {}).get("year")
        if year is not None and (type(year) is not int or not 1870 <= year <= 2200):
            raise CatalogSearchError("invalid_response")
        episodes = raw.get("episodes")
        if episodes is not None and (type(episodes) is not int or not 0 < episodes <= 100000):
            raise CatalogSearchError("invalid_response")
        genres = raw.get("genres") or []
        if not isinstance(genres, list) or len(genres) > 40:
            raise CatalogSearchError("invalid_response")
        genres = [_plain(value, 100) for value in genres]
        studio = None
        studios = raw.get("studios") or {}
        if (
            not isinstance(studios, dict)
            or not isinstance(studios.get("nodes", []), list)
            or len(studios.get("nodes", [])) > 20
        ):
            raise CatalogSearchError("invalid_response")
        for row in studios.get("nodes", []):
            if isinstance(row, dict) and row.get("name"):
                studio = _plain(row["name"], 200)
                break
        return {
            "id": media_id,
            "anilist_id": media_id,
            "title": name,
            "year": year,
            "format": _plain(raw.get("format"), 30) or None,
            "episode_total": episodes,
            "description": _plain(raw.get("description")),
            "genres": genres,
            "status": _plain(raw.get("status"), 40) or None,
            "studio": studio,
            "poster_url": f"/plus/media/{media_id}/poster",
            "url": f"/plus/catalog/anime/{media_id}",
        }

    def _enrich(self, metadata: dict, entry: Any = None, viewer: Any = None) -> dict:
        card = deepcopy(metadata)
        card.update(
            watch_status="unknown",
            progress=None,
            rating=None,
            history_source=None,
            rating_source=None,
        )
        if (
            isinstance(entry, dict)
            and isinstance(viewer, dict)
            and type(viewer.get("id")) is int
            and type(entry.get("userId")) is int
            and type(entry.get("mediaId")) is int
            and entry.get("userId") == viewer["id"]
            and entry.get("mediaId") == card["id"]
        ):
            status = {
                "CURRENT": "watching",
                "COMPLETED": "completed",
                "PLANNING": "planned",
                "DROPPED": "dropped",
                "PAUSED": "paused",
                "REPEATING": "rewatching",
            }.get(entry.get("status"))
            if status:
                card.update(watch_status=status, history_source="anilist")
            progress = entry.get("progress")
            if type(progress) is int and 0 <= progress <= 100000:
                card["progress"] = progress
            score = entry.get("score")
            if type(score) in {int, float} and math.isfinite(score) and 0 < score <= 10:
                card.update(rating=float(score), rating_source="anilist")
        monitor = self.db.execute(
            "SELECT id,active FROM media_monitors WHERE user_id=? AND anilist_id=?",
            (self.user_id, card["id"]),
        ).fetchone()
        removal = (
            self.db.execute(
                "SELECT status FROM media_title_removals WHERE monitor_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
                (monitor["id"], self.user_id),
            ).fetchone()
            if monitor
            else None
        )
        library_status = ("tracking" if monitor["active"] else "paused") if monitor else None
        if removal:
            library_status = "removed" if removal["status"] == "complete" else "removing"
        card.update(
            library_id=monitor["id"] if monitor else None,
            library_status=library_status,
            library_present=bool(monitor and not removal),
        )
        local = self.db.execute(
            "SELECT * FROM watch_entries WHERE user_id=? AND anilist_id=? ORDER BY updated_at DESC,id DESC LIMIT 1",
            (self.user_id, card["id"]),
        ).fetchone()
        if local and (
            local["last_source"] != "migration"
            or local["progress_episodes"] > 0
            or local["status"] == "completed"
        ):
            card.update(
                watch_status=local["status"],
                progress=local["progress_episodes"],
                history_source=local["last_source"],
            )
            if local["episode_total"]:
                card["episode_total"] = local["episode_total"]
        score = self.db.execute(
            "SELECT event.score_ten FROM watch_events event JOIN watch_entries entry ON entry.id=event.watch_entry_id "
            "WHERE event.user_id=? AND entry.user_id=? AND entry.anilist_id=? AND event.disposition='accepted' "
            "AND event.score_ten IS NOT NULL ORDER BY event.observed_at DESC,event.id DESC LIMIT 1",
            (self.user_id, self.user_id, card["id"]),
        ).fetchone()
        if score:
            card.update(rating=score["score_ten"], rating_source="manual")
        return card

    async def _lookup(self, *, query: str | None = None, media_id: int | None = None):
        token = self._token()
        key = ("search", query.casefold()) if query is not None else ("detail", media_id)
        cached = self._public.get(key)
        if token is None and cached is not None and time.monotonic() < cached[0]:
            return [self._enrich(row) for row in cached[1]]
        private = _PRIVATE if token else ""
        viewer = "Viewer { id }" if token else ""
        if query is not None:
            document = (
                "query($search:String!){ "
                + viewer
                + " Page(page:1,perPage:8){media(search:$search,type:ANIME,isAdult:false){"
                + _FIELDS
                + private
                + "}}}"
            )
            data = await self._request(document, {"search": query}, token)
            page = data.get("Page")
            rows = page.get("media") if isinstance(page, dict) else None
            if not isinstance(rows, list) or len(rows) > 8:
                raise CatalogSearchError("invalid_response")
        else:
            document = (
                "query($id:Int!){ "
                + viewer
                + " Media(id:$id,type:ANIME){"
                + _FIELDS
                + private
                + "}}"
            )
            data = await self._request(document, {"id": media_id}, token)
            rows = [data.get("Media")]
        public, cards, seen = [], [], set()
        for raw in rows:
            metadata = self._metadata(raw, media_id)
            if metadata["id"] in seen:
                continue
            seen.add(metadata["id"])
            from omakase.plus.anime_synopsis import store_synopsis

            store_synopsis(self.db, metadata["id"], metadata["description"])
            public.append(metadata)
            cards.append(
                self._enrich(
                    metadata,
                    raw.get("mediaListEntry") if token else None,
                    data.get("Viewer") if token else None,
                )
            )
        if token is None:
            self._public[key] = (time.monotonic() + 60, deepcopy(public))
            self._public.move_to_end(key)
            while len(self._public) > 64:
                self._public.popitem(last=False)
        return cards

    async def search(self, q: str) -> list[dict]:
        if (
            not isinstance(q, str)
            or not 2 <= len(q.strip()) <= 120
            or any(ord(char) < 32 for char in q)
        ):
            raise CatalogSearchError("invalid_query")
        return await self._lookup(query=q.strip())

    async def detail(self, media_id: int) -> dict:
        return (await self._lookup(media_id=_id(media_id)))[0]
