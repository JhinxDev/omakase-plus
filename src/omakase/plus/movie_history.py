"""Per-user movie history, manual field ownership and read-only Plex snapshots."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree

from omakase.plus import movies_store as store
from omakase.plus.movies import MovieError, MovieService, _base, _imdb, _iso, _positive

UNSET = object()
WATCH_STATUSES = ("unwatched", "watching", "completed", "dropped")


def _rating(value: Any) -> float | None:
    if value is None:
        return None
    if (
        type(value) not in {int, float}
        or not math.isfinite(value)
        or not 1 <= value <= 10
        or not float(value * 2).is_integer()
    ):
        raise MovieError("invalid_input")
    return float(value)


class MovieHistoryService:
    """Effective local history. Manual status and rating overrides are independent.

    Only sync_plex reads native playback fields. Core acquired/Ready states never
    enter this module, and saving a status never marks the item watched in Plex.
    """

    def __init__(self, movies: MovieService) -> None:
        self.movies, self.db = movies, movies.db

    def _guard(self, user_id: int) -> None:
        store.require_user(self.db, user_id)

    def _scope(self) -> str:
        config = self.movies.connections
        return hashlib.sha256(
            (_base(config.plex_url) + "\0" + config.plex_token).encode()
        ).hexdigest()

    def get(self, user_id: int, imdb_id: str) -> dict[str, Any] | None:
        self._guard(user_id)
        imdb_id = _imdb(imdb_id)
        row = self.db.execute(
            "SELECT h.*, c.title, c.year FROM movie_history h JOIN movie_catalog c "
            "ON c.user_id=h.user_id AND c.imdb_id=h.imdb_id WHERE h.user_id=? AND h.imdb_id=?",
            (user_id, imdb_id),
        ).fetchone()
        if row is None:
            return None
        result = {
            key: row[key]
            for key in (
                "imdb_id",
                "tmdb_id",
                "title",
                "year",
                "status",
                "rating",
                "status_source",
                "rating_source",
                "status_updated_at",
                "rating_updated_at",
                "last_viewed_at",
                "created_at",
                "updated_at",
            )
        }
        result["genres"] = store.list_movie_genres(self.db, user_id, imdb_id)
        return result

    def list(
        self, user_id: int, *, status: str | None = None, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        self._guard(user_id)
        if status is not None and status not in WATCH_STATUSES:
            raise MovieError("invalid_input")
        if (
            type(limit) is not int
            or not 1 <= limit <= 500
            or type(offset) is not int
            or not 0 <= offset <= 10000
        ):
            raise MovieError("invalid_input")
        query, params = "SELECT imdb_id FROM movie_history WHERE user_id=?", [user_id]
        if status is not None:
            query += " AND status=?"
            params.append(status)
        query += " ORDER BY updated_at DESC, imdb_id LIMIT ? OFFSET ?"
        params += [limit, offset]
        return [
            self.get(user_id, row["imdb_id"]) for row in self.db.execute(query, params).fetchall()
        ]

    def _ensure(self, user_id: int, imdb_id: str, tmdb_id: int) -> None:
        now = _iso(self.movies._clock())
        self.db.execute(
            "INSERT OR IGNORE INTO movie_history (user_id, imdb_id, tmdb_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, imdb_id, tmdb_id, now, now),
        )
        row = self.db.execute(
            "SELECT tmdb_id FROM movie_history WHERE user_id=? AND imdb_id=?", (user_id, imdb_id)
        ).fetchone()
        if row is None or row["tmdb_id"] != tmdb_id:
            raise MovieError("identity_mismatch")

    def _change(
        self, user_id: int, imdb_id: str, field: str, value: Any, source: str, action: str
    ) -> bool:
        if field not in {"status", "rating"} or source not in {"manual", "plex", "none"}:
            raise MovieError("invalid_input")
        row = self.db.execute(
            "SELECT * FROM movie_history WHERE user_id=? AND imdb_id=?", (user_id, imdb_id)
        ).fetchone()
        previous = {"value": row[field], "source": row[field + "_source"]}
        current = {"value": value, "source": source}
        if previous == current:
            return False
        now = _iso(self.movies._clock())
        self.db.execute(
            f"UPDATE movie_history SET {field}=?, {field}_source=?, {field}_updated_at=?, updated_at=? WHERE user_id=? AND imdb_id=?",
            (value, source, now, now, user_id, imdb_id),
        )
        self.db.execute(
            "INSERT INTO movie_history_events (user_id, imdb_id, field_name, action, previous_json, current_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                user_id,
                imdb_id,
                field,
                action,
                json.dumps(previous, sort_keys=True),
                json.dumps(current, sort_keys=True),
                now,
            ),
        )
        return True

    async def _identity(self, user_id: int, imdb_id: str, tmdb_id: int) -> dict[str, Any]:
        metadata = store.get_movie(self.db, user_id, imdb_id)
        if metadata and metadata["identity_verified"] and metadata["tmdb_id"] == tmdb_id:
            return metadata
        self.movies._guard(user_id)
        metadata = await self.movies._metadata(tmdb_id, imdb_id=imdb_id)
        return store.upsert_movie(self.db, user_id, metadata, now=_iso(self.movies._clock()))

    async def save(
        self, user_id: int, *, imdb_id: str, tmdb_id: int, status: Any = UNSET, rating: Any = UNSET
    ) -> dict[str, Any]:
        self._guard(user_id)
        imdb_id, tmdb_id = _imdb(imdb_id), _positive(tmdb_id)
        if status is UNSET and rating is UNSET:
            raise MovieError("invalid_input")
        if status is not UNSET and status not in WATCH_STATUSES:
            raise MovieError("invalid_input")
        if rating is not UNSET:
            rating = _rating(rating)
        await self._identity(user_id, imdb_id, tmdb_id)
        with self.db:
            self._ensure(user_id, imdb_id, tmdb_id)
            if status is not UNSET:
                self._change(user_id, imdb_id, "status", status, "manual", "manual")
            if rating is not UNSET:
                self._change(user_id, imdb_id, "rating", rating, "manual", "manual")
        result = self.get(user_id, imdb_id)
        assert result is not None
        return result

    @staticmethod
    def _counter(value: str | None, maximum: int) -> int | None:
        if value is None:
            return None
        if not re.fullmatch(r"[0-9]{1,12}", value) or int(value) > maximum:
            raise MovieError("invalid_plex_metadata")
        return int(value)

    def _snapshot(
        self, video: ElementTree.Element, metadata: dict[str, Any], section: int
    ) -> dict[str, Any]:
        if (
            video.get("type") != "movie"
            or video.get("librarySectionID") != str(section)
            or video.get("librarySectionTitle") != "Movies"
            or self.movies._guids(video, "imdb") != {metadata["imdb_id"]}
        ):
            raise MovieError("plex_identity_mismatch")
        guids = self.movies._guids(video, "tmdb")
        if guids and guids != {str(metadata["tmdb_id"])}:
            raise MovieError("plex_identity_mismatch")
        key = video.get("ratingKey", "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", key):
            raise MovieError("invalid_plex_metadata")
        view_count = self._counter(video.get("viewCount"), 1_000_000)
        view_offset = self._counter(video.get("viewOffset"), 1_000_000_000)
        last_viewed = self._counter(
            video.get("lastViewedAt"), int(self.movies._clock().timestamp()) + 300
        )
        rating = video.get("userRating")
        try:
            rating = _rating(float(rating)) if rating not in (None, "", "0", "0.0") else None
        except (ValueError, TypeError, OverflowError):
            raise MovieError("invalid_plex_metadata") from None
        data = {
            "imdb_id": metadata["imdb_id"],
            "tmdb_id": metadata["tmdb_id"],
            "plex_key": key,
            "view_count": view_count,
            "view_offset_ms": view_offset,
            "last_viewed_at": _iso(datetime.fromtimestamp(last_viewed, timezone.utc))
            if last_viewed
            else None,
            "user_rating": rating,
        }
        data["fingerprint"] = hashlib.sha256(
            json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return data

    def _apply_snapshot(self, user_id: int, snapshot: dict[str, Any]) -> bool:
        imdb_id, scope = snapshot["imdb_id"], self._scope()
        previous = self.db.execute(
            "SELECT fingerprint FROM movie_plex_history WHERE user_id=? AND source_scope=? AND imdb_id=?",
            (user_id, scope, imdb_id),
        ).fetchone()
        if previous and previous["fingerprint"] == snapshot["fingerprint"]:
            return False
        fields = (
            "imdb_id",
            "tmdb_id",
            "plex_key",
            "view_count",
            "view_offset_ms",
            "last_viewed_at",
            "user_rating",
            "fingerprint",
        )
        updates = ",".join(f"{name}=excluded.{name}" for name in fields if name != "imdb_id")
        now = _iso(self.movies._clock())
        with self.db:
            self.db.execute(
                f"INSERT INTO movie_plex_history (user_id, source_scope, {','.join(fields)}, observed_at) "
                f"VALUES ({','.join('?' for _ in range(len(fields) + 3))}) "
                f"ON CONFLICT(user_id, source_scope, imdb_id) DO UPDATE SET {updates}, observed_at=excluded.observed_at",
                (user_id, scope, *(snapshot[name] for name in fields), now),
            )
            status = (
                "completed"
                if (snapshot["view_count"] or 0) > 0
                else ("watching" if (snapshot["view_offset_ms"] or 0) > 0 else None)
            )
            row = self.get(user_id, imdb_id)
            if (
                row is None
                and status is None
                and snapshot["user_rating"] is None
                and snapshot["last_viewed_at"] is None
            ):
                return False
            self._ensure(user_id, imdb_id, snapshot["tmdb_id"])
            row = self.get(user_id, imdb_id)
            changed = False
            if status is not None and row["status_source"] != "manual":
                order = {"unwatched": 0, "watching": 1, "completed": 2, "dropped": 3}
                if order[status] >= order[row["status"]]:
                    changed |= self._change(user_id, imdb_id, "status", status, "plex", "plex")
            if snapshot["user_rating"] is not None and row["rating_source"] != "manual":
                changed |= self._change(
                    user_id, imdb_id, "rating", snapshot["user_rating"], "plex", "plex"
                )
            viewed = snapshot["last_viewed_at"]
            if viewed is not None and (
                row["last_viewed_at"] is None or viewed > row["last_viewed_at"]
            ):
                self.db.execute(
                    "UPDATE movie_history SET last_viewed_at=?, updated_at=? WHERE user_id=? AND imdb_id=?",
                    (viewed, now, user_id, imdb_id),
                )
                changed = True
            return changed

    async def sync_plex(
        self, user_id: int, *, limit: int = 100, offset: int | None = None
    ) -> dict[str, Any]:
        """Explicit, read-only sync of at most100 Movies and12 new mappings."""
        self.movies._guard(user_id)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MovieError("invalid_input")
        if offset is not None and (type(offset) is not int or not 0 <= offset <= 1000):
            raise MovieError("invalid_input")
        scope = self._scope()
        cursor = self.db.execute(
            "SELECT next_offset FROM movie_sync_cursors WHERE user_id=? AND source_scope=?",
            (user_id, scope),
        ).fetchone()
        expected_offset = cursor["next_offset"] if cursor else 0
        start = offset if offset is not None else cursor["next_offset"] if cursor else 0
        section, _ = await self.movies._sections()
        videos = await self.movies._videos(section)
        videos.sort(key=lambda row: (row.get("ratingKey", "").zfill(32), row.get("key", "")))
        if start >= len(videos):
            start = 0
        next_offset = min(start + limit, len(videos))
        result = {
            "observed": 0,
            "imported": 0,
            "unchanged": 0,
            "skipped": 0,
            "remaining": max(0, len(videos) - next_offset),
            "checked_at": _iso(self.movies._clock()),
            "errors": [],
        }
        mappings, seen = 0, set()
        for position, video in enumerate(videos[start : start + limit], start=start):
            imdb_id = None
            try:
                if (
                    video.get("type") != "movie"
                    or video.get("librarySectionID") != str(section)
                    or video.get("librarySectionTitle") != "Movies"
                ):
                    raise MovieError("plex_library_mismatch")
                imdbs, tmdbs = self.movies._guids(video, "imdb"), self.movies._guids(video, "tmdb")
                if len(imdbs) != 1 or len(tmdbs) > 1:
                    raise MovieError("plex_identity_mismatch")
                imdb_id = _imdb(next(iter(imdbs)))
                if imdb_id in seen:
                    raise MovieError("plex_identity_mismatch")
                seen.add(imdb_id)
                known = store.get_movie(self.db, user_id, imdb_id)
                tmdb_id = (
                    _positive(next(iter(tmdbs)), native_string=True)
                    if tmdbs
                    else (known["tmdb_id"] if known else None)
                )
                if tmdb_id is None:
                    if mappings >= 12:
                        next_offset = position
                        break
                    mappings += 1
                    metadata = await self.movies._identity(user_id, imdb_id)
                else:
                    if not known or not known["identity_verified"] or known["tmdb_id"] != tmdb_id:
                        if mappings >= 12:
                            next_offset = position
                            break
                        mappings += 1
                    metadata = await self._identity(user_id, imdb_id, tmdb_id)
                snapshot = self._snapshot(video, metadata, section)
                changed = self._apply_snapshot(user_id, snapshot)
                result["imported" if changed else "unchanged"] += 1
            except ValueError as error:
                failure = (
                    error if isinstance(error, MovieError) else MovieError("identity_mismatch")
                )
                result["skipped"] += 1
                if len(result["errors"]) < 20:
                    result["errors"].append({"imdb_id": imdb_id, **failure.as_dict()})
            result["observed"] += 1
        result.update(
            remaining=max(0, len(videos) - next_offset),
            next_offset=next_offset if next_offset < len(videos) else None,
            total=len(videos),
        )
        if offset is None:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO movie_sync_cursors(user_id,source_scope,next_offset,updated_at) VALUES(?,?,?,?)",
                    (user_id, scope, expected_offset, result["checked_at"]),
                )
                # An older concurrent batch cannot rewind a newer batch's cursor.
                self.db.execute(
                    "UPDATE movie_sync_cursors SET next_offset=?,updated_at=? WHERE user_id=? AND source_scope=? AND next_offset=?",
                    (
                        result["next_offset"] or 0,
                        result["checked_at"],
                        user_id,
                        scope,
                        expected_offset,
                    ),
                )
        return result

    def use_plex(
        self, user_id: int, imdb_id: str, *, fields: tuple[str, ...] = ("status", "rating")
    ) -> dict[str, Any]:
        """Explicitly release selected local overrides to the latest saved snapshot."""
        self._guard(user_id)
        imdb_id = _imdb(imdb_id)
        if (
            not isinstance(fields, (tuple, list))
            or not fields
            or len(fields) > 2
            or len(set(fields)) != len(fields)
            or not set(fields) <= {"status", "rating"}
        ):
            raise MovieError("invalid_input")
        snapshot = self.db.execute(
            "SELECT * FROM movie_plex_history WHERE user_id=? AND source_scope=? AND imdb_id=?",
            (user_id, self._scope(), imdb_id),
        ).fetchone()
        if snapshot is None:
            raise MovieError("invalid_input")
        status = (
            "completed"
            if (snapshot["view_count"] or 0) > 0
            else ("watching" if (snapshot["view_offset_ms"] or 0) > 0 else "unwatched")
        )
        with self.db:
            self._ensure(user_id, imdb_id, snapshot["tmdb_id"])
            if "status" in fields:
                self._change(user_id, imdb_id, "status", status, "plex", "follow_plex")
            if "rating" in fields:
                self._change(
                    user_id, imdb_id, "rating", snapshot["user_rating"], "plex", "follow_plex"
                )
        result = self.get(user_id, imdb_id)
        assert result is not None
        return result

    def augment(self, user_id: int, card: dict[str, Any]) -> dict[str, Any]:
        row = self.get(user_id, card["imdb_id"])
        if row is not None and row["tmdb_id"] != card.get("tmdb_id"):
            raise MovieError("identity_mismatch")
        return {
            **card,
            "history": row,
            "watch_status": row["status"] if row else None,
            "user_rating": row["rating"] if row else None,
            "history_recorded": row is not None,
        }

    def export_history(self, user_id: int) -> dict[str, Any]:
        """Portable owner JSON, without service URLs, credentials or shared data."""
        self._guard(user_id)
        rows = self.db.execute(
            "SELECT imdb_id FROM movie_history WHERE user_id=? ORDER BY imdb_id LIMIT 10001",
            (user_id,),
        ).fetchall()
        if len(rows) > 10000:
            raise MovieError("inventory_incomplete")
        return {
            "schema_version": 1,
            "exported_at": _iso(self.movies._clock()),
            "history": [self.get(user_id, row["imdb_id"]) for row in rows],
        }
