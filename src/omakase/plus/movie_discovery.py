"""Bounded public movie metadata with private, local rating/genre re-ranking.

API shape: Seerr v3.4.0 /genres/movie, /discover/movies and /movie/{TMDB}.
History, ratings and personalized ordering never leave the local database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from omakase.plus import movies_store as store
from omakase.plus.movie_history import WATCH_STATUSES, MovieHistoryService
from omakase.plus.movies import (
    MovieError,
    MovieService,
    _art,
    _base,
    _genre_rows,
    _iso,
    _positive,
    _text,
    _time,
    _year,
)

_SORTS = {
    "personalized": "popularity.desc",
    "popular": "popularity.desc",
    "top_rated": "vote_average.desc",
    "newest": "primary_release_date.desc",
}
_LABELS = {
    "personalized": "For your taste",
    "popular": "Popular",
    "top_rated": "Top rated",
    "newest": "Newest",
}


def _number(value: Any, maximum: float) -> float:
    if value is None:
        return 0.0
    if type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= maximum:
        raise MovieError("invalid_metadata")
    return float(value)


class MovieDiscoveryService:
    """Public candidates are cached; effective history is applied on every call."""

    def __init__(self, movies: MovieService, *, cache_seconds: int = 900) -> None:
        if type(cache_seconds) is not int or not 1 <= cache_seconds <= 86400:
            raise MovieError("invalid_input")
        self.movies, self.db = movies, movies.db
        self.history, self._ttl = MovieHistoryService(movies), cache_seconds
        self._scope = (
            hashlib.sha256(_base(movies.connections.seerr_url).encode()).hexdigest()
            if movies.connections.seerr_url
            else ""
        )

    @staticmethod
    def _key(kind: str, identity: Any) -> str:
        return hashlib.sha256(
            json.dumps(
                {"kind": kind, "identity": identity}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    def _row(self, key: str):
        return self.db.execute(
            "SELECT * FROM movie_discovery_cache WHERE source_scope=? AND cache_key=?",
            (self._scope, key),
        ).fetchone()

    def _put(
        self,
        kind: str,
        key: str,
        payload: Any,
        *,
        error: MovieError | None = None,
        fetched_at: str | None = None,
    ) -> None:
        if self._row(key) is None:
            count = self.db.execute(
                "SELECT count(*) FROM movie_discovery_cache WHERE source_scope=?", (self._scope,)
            ).fetchone()[0]
            if count >= 128:
                old = self.db.execute(
                    "SELECT cache_key FROM movie_discovery_cache WHERE source_scope=? "
                    "AND (error_code IS NULL OR (error_code NOT IN ('rate_limited','provider_restricted')) "
                    "OR (error_code='rate_limited' AND expires_at<=?)) ORDER BY fetched_at LIMIT 1",
                    (self._scope, _iso(self.movies._clock())),
                ).fetchone()
                if old is None:
                    raise MovieError("inventory_incomplete")
                self.db.execute(
                    "DELETE FROM movie_discovery_cache WHERE source_scope=? AND cache_key=?",
                    (self._scope, old["cache_key"]),
                )
        now = self.movies._clock()
        expires = now + timedelta(seconds=self._ttl if error is None else 60)
        if error is not None and error.retry_at is not None:
            retry = _time(error.retry_at)
            if retry is not None:
                expires = max(expires, retry)
        if error is not None and error.code == "connection_rejected":
            expires = now
        self.db.execute(
            "INSERT INTO movie_discovery_cache (source_scope,cache_key,kind,payload_json,fetched_at,expires_at,error_code,retry_after,retry_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(source_scope,cache_key) DO UPDATE SET "
            "payload_json=excluded.payload_json,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at,"
            "error_code=excluded.error_code,retry_after=excluded.retry_after,retry_at=excluded.retry_at",
            (
                self._scope,
                key,
                kind,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                fetched_at or _iso(now),
                _iso(expires),
                error.code if error else None,
                error.retry_after if error else None,
                error.retry_at if error else None,
            ),
        )
        self.db.commit()

    def _active_error(self, row) -> MovieError | None:
        if row is None or row["error_code"] is None:
            return None
        expiry = _time(row["expires_at"])
        if row["error_code"] == "provider_restricted" or (
            expiry is not None and self.movies._clock() < expiry
        ):
            return MovieError(
                row["error_code"], retry_after=row["retry_after"], retry_at=row["retry_at"]
            )
        return None

    async def _cache(
        self, kind: str, identity: Any, loader: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, bool]:
        key = self._key(kind, identity)
        row = self._row(key)
        previous = json.loads(row["payload_json"]) if row is not None else None
        active = self._active_error(row)
        expiry = _time(row["expires_at"]) if row is not None else None
        if active is not None:
            if previous is not None:
                return previous, True
            raise active
        if (
            row is not None
            and row["error_code"] is None
            and expiry is not None
            and self.movies._clock() < expiry
        ):
            return previous, False
        cooldown = self._active_error(self._row(self._key("cooldown", "seerr")))
        if cooldown is not None:
            if previous is not None:
                return previous, True
            raise cooldown
        try:
            result = await loader()
        except MovieError as error:
            self._put(
                kind, key, previous, error=error, fetched_at=row["fetched_at"] if row else None
            )
            if error.code == "rate_limited":
                self._put("cooldown", self._key("cooldown", "seerr"), None, error=error)
            if previous is not None:
                return previous, True
            raise
        self._put(kind, key, result)
        return result, False

    async def options(self, user_id: int) -> dict[str, Any]:
        self.movies._guard(user_id)

        async def load():
            raw = await self.movies._bytes(
                "seerr", "GET", "/api/v1/genres/movie", params={"language": "en"}
            )
            try:
                data = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError):
                raise MovieError("invalid_metadata") from None
            return _genre_rows(data)

        genres, stale = await self._cache("genres", "en", load)
        return {
            "genres": genres,
            "sorts": [{"value": value, "label": _LABELS[value]} for value in _SORTS],
            "watch_statuses": list(WATCH_STATUSES),
            "exclude_completed_default": True,
            "year_min": 1870,
            "year_max": 2200,
            "page_min": 1,
            "page_max": 3,
            "rating_min": 1,
            "rating_max": 10,
            "rating_step": 0.5,
            "metadata_stale": stale,
        }

    def _public_rows(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) > 100:
            raise MovieError("invalid_metadata")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise MovieError("invalid_metadata")
            if row.get("mediaType") != "movie" or row.get("adult") is True:
                continue
            tmdb_id = _positive(row.get("id"))
            if tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            ids = row.get("genreIds", [])
            if not isinstance(ids, list) or len(ids) > 50:
                raise MovieError("invalid_metadata")
            ids = list(dict.fromkeys(_positive(value) for value in ids))
            result.append(
                {
                    "tmdb_id": tmdb_id,
                    "title": _text(row.get("title"), 300, required=True),
                    "year": _year(row.get("releaseDate")),
                    "genre_ids": ids,
                    "poster_url": _art(row.get("posterPath"), "w342"),
                    "backdrop_url": _art(row.get("backdropPath"), "w1280"),
                    "overview": _text(row.get("overview"), 5000),
                    "popularity": _number(row.get("popularity"), 1_000_000_000),
                    "vote_average": _number(row.get("voteAverage"), 10),
                    "vote_count": _number(row.get("voteCount"), 1_000_000_000),
                }
            )
            if len(result) == 20:
                break
        return result

    def _preferences(self, user_id: int) -> tuple[dict[int, float], dict[int, str], int]:
        rows = self.db.execute(
            "SELECT g.genre_id,g.name,h.rating FROM movie_history h JOIN movie_genres g "
            "ON h.user_id=g.user_id AND h.imdb_id=g.imdb_id WHERE h.user_id=? AND h.rating IS NOT NULL",
            (user_id,),
        ).fetchall()
        ratings = self.db.execute(
            "SELECT count(*) FROM movie_history WHERE user_id=? AND rating IS NOT NULL", (user_id,)
        ).fetchone()[0]
        values, names = {}, {}
        for row in rows:
            values.setdefault(row["genre_id"], []).append(float(row["rating"]) - 5.5)
            names[row["genre_id"]] = row["name"]
        return (
            {genre: sum(ratings) / len(ratings) for genre, ratings in values.items()},
            names,
            ratings,
        )

    async def discover(
        self,
        user_id: int,
        *,
        genre_id: int | None = None,
        sort: str = "personalized",
        year_from: int | None = None,
        year_to: int | None = None,
        include_completed: bool = False,
        page: int = 1,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.movies._guard(user_id)
        if not isinstance(sort, str) or sort not in _SORTS or type(include_completed) is not bool:
            raise MovieError("invalid_input")
        if (
            type(page) is not int
            or not 1 <= page <= 3
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise MovieError("invalid_input")
        if genre_id is not None:
            genre_id = _positive(genre_id)
        for year in (year_from, year_to):
            if year is not None and (type(year) is not int or not 1870 <= year <= 2200):
                raise MovieError("invalid_input")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise MovieError("invalid_input")
        options = await self.options(user_id)
        genres = {row["id"]: row for row in options["genres"]}
        if genre_id is not None and genre_id not in genres:
            raise MovieError("invalid_input")
        params = {"page": str(page), "sortBy": _SORTS[sort]}
        if genre_id is not None:
            params["genre"] = str(genre_id)
        if year_from is not None:
            params["primaryReleaseDateGte"] = f"{year_from}-01-01"
        if year_to is not None:
            params["primaryReleaseDateLte"] = f"{year_to}-12-31"

        async def load_candidates():
            return self._public_rows(
                await self.movies._json("seerr", "/api/v1/discover/movies", params=params)
            )

        public, stale_candidates = await self._cache("candidates", params, load_candidates)
        candidates = []
        for row in public:
            known = self.db.execute(
                "SELECT status FROM movie_history WHERE user_id=? AND tmdb_id=?",
                (user_id, row["tmdb_id"]),
            ).fetchone()
            if not include_completed and known is not None and known["status"] == "completed":
                continue
            candidates.append(row)
        semaphore = asyncio.Semaphore(3)

        async def card_for(row: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                try:

                    async def detail():
                        return await self.movies._metadata(row["tmdb_id"])

                    metadata, stale_detail = await self._cache("detail", row["tmdb_id"], detail)
                    metadata = dict(metadata)
                    if "genres" not in metadata:
                        metadata["genres"] = [
                            genres[genre] for genre in row["genre_ids"] if genre in genres
                        ]
                    store.upsert_movie(self.db, user_id, metadata, now=_iso(self.movies._clock()))
                    card = self.history.augment(user_id, self.movies._card(user_id, metadata))
                    card.update(
                        requestable=True,
                        metadata_stale=bool(
                            stale_detail or stale_candidates or options["metadata_stale"]
                        ),
                    )
                except ValueError as error:
                    failure = (
                        error if isinstance(error, MovieError) else MovieError("identity_mismatch")
                    )
                    metadata = {
                        "imdb_id": None,
                        **{
                            key: row.get(key)
                            for key in (
                                "tmdb_id",
                                "title",
                                "year",
                                "poster_url",
                                "backdrop_url",
                                "overview",
                            )
                        },
                    }
                    card = self.movies._failure(self.movies._card(user_id, metadata), failure)
                    card.update(
                        genres=[genres[genre] for genre in row["genre_ids"] if genre in genres],
                        history=None,
                        watch_status=None,
                        user_rating=None,
                        history_recorded=False,
                        requestable=False,
                        metadata_stale=True,
                    )
                card.update(
                    popularity=row["popularity"],
                    vote_average=row["vote_average"],
                    vote_count=row["vote_count"],
                )
                return card

        cards = await asyncio.gather(*(card_for(row) for row in candidates))
        filtered = []
        for card in cards:
            ids = {genre["id"] for genre in card["genres"]}
            if genre_id is not None and genre_id not in ids:
                continue
            year = card["year"]
            if year_from is not None and (year is None or year < year_from):
                continue
            if year_to is not None and (year is None or year > year_to):
                continue
            if not include_completed and card.get("watch_status") == "completed":
                continue
            filtered.append(card)
        weights, names, _ = self._preferences(user_id)
        personal = sort == "personalized" and any(value != 0 for value in weights.values())

        def affinity(card: dict[str, Any]) -> float:
            ids = [genre["id"] for genre in card["genres"]]
            return sum(weights.get(genre, 0.0) for genre in ids) / max(1, len(ids))

        if personal:
            filtered.sort(key=lambda card: (-affinity(card), -card["popularity"], card["tmdb_id"]))
        elif sort == "top_rated":
            filtered.sort(
                key=lambda card: (
                    -card["vote_average"],
                    -card["vote_count"],
                    -card["popularity"],
                    card["tmdb_id"],
                )
            )
        elif sort == "newest":
            filtered.sort(
                key=lambda card: (-(card["year"] or 0), -card["popularity"], card["tmdb_id"])
            )
        else:
            filtered.sort(key=lambda card: (-card["popularity"], card["tmdb_id"]))
        for index, card in enumerate(filtered[:limit], 1):
            positive = [
                names[genre["id"]] for genre in card["genres"] if weights.get(genre["id"], 0) > 0
            ]
            negative = [
                names[genre["id"]] for genre in card["genres"] if weights.get(genre["id"], 0) < 0
            ]
            basis = (
                "saved_ratings"
                if personal
                else {"top_rated": "public_rating", "newest": "release_date"}.get(
                    sort, "public_popularity"
                )
            )
            if not card["requestable"]:
                reason = "Exact movie identity is unavailable; requesting is disabled."
            elif personal and positive:
                reason = "Based on your saved ratings for " + ", ".join(positive) + "."
            elif personal and negative:
                reason = "You rated these genres lower: " + ", ".join(negative) + "."
            elif personal:
                reason = "Ordered using your saved genre ratings."
            elif sort == "top_rated":
                reason = "Ordered by public TMDB ratings."
            elif sort == "newest":
                reason = "Ordered by release year."
            else:
                reason = "Popular on TMDB; save ratings to personalize this list."
            card.update(rank=index, ranking_basis=basis, recommendation_reason=reason)
        return filtered[:limit]
