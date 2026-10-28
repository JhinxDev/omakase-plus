"""Exact-ID private movies. Browsing is metadata-only; verification is read-only.

Only operator-injected service origins receive credentials. No movie operation
writes AniList, watch history, provider settings, blacklists or Plex selections.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

import httpx

from omakase.plus import movies_store as store
from omakase.plus.outbound import OutboundError, Resolver, pin_destination

_OPERATION_TIMEOUT = 20.0
_MAX_METADATA = 2 * 1024 * 1024
_RANGE_BYTES = 65_536
_IMDB = re.compile(r"tt[0-9]{7,10}\Z")
_BACKEND_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
_DETAILS = {
    "disabled": "Movies integration is disabled.",
    "unsafe_destination": "This service destination is blocked by the deployment network policy.",
    "invalid_input": "Movie input is invalid.",
    "identity_mismatch": "IMDb and TMDB movie identities do not agree.",
    "invalid_metadata": "The metadata service returned an incompatible response.",
    "invalid_riven_metadata": "Riven returned an incompatible movie response; no submission was sent.",
    "inventory_incomplete": "Riven inventory exceeds the bounded check; no submission was sent.",
    "rate_limited": "The service asked for a later check; Retry-After has been preserved.",
    "provider_restricted": "The service reports a restriction. This file or request will not be retried automatically.",
    "connection_rejected": "The service rejected the configured connection.",
    "backend_unavailable": "The service is temporarily unavailable.",
    "transport_timeout": "The service timed out; the result is unknown and no duplicate submission was sent.",
    "transport_error": "The service could not be reached; the result is unknown.",
    "redirect_rejected": "The service returned a redirect; it was not followed.",
    "acknowledgement_unconfirmed": "Riven acknowledged the request, but persistence was not observed. No duplicate was sent.",
    "backend_item_missing": "The requested movie is not present in the bounded Riven inventory.",
    "submission_pending": "A submission is already being checked; no duplicate was sent.",
    "investigation_exhausted": "The bounded request checks are exhausted. A new eligible release or operator review is needed.",
    "unknown_backend_state": "Riven returned an unrecognised state; delivery is unknown.",
    "requested": "Requested; delivery has not been verified.",
    "searching": "Riven is searching for an eligible movie release.",
    "acquiring": "Riven is acquiring a movie file; Plex delivery has not been verified.",
    "verifying": "The movie needs current Plex identity, English audio and byte verification.",
    "no_eligible_results": "The backend has no eligible result. No automatic resubmission was sent.",
    "unreleased": "The movie is not released; a later eligible release may be checked.",
    "paused": "The backend has paused this movie.",
    "plex_not_found": "An exact IMDb match is not present in the Movies library.",
    "plex_identity_mismatch": "Plex did not return the exact requested movie identity.",
    "plex_library_mismatch": "Plex returned the movie from a different library.",
    "anime_movie": "This identity belongs to Anime Movies and is managed separately.",
    "invalid_plex_metadata": "Plex file metadata is incomplete or unsafe; no media bytes were requested.",
    "english_audio_not_selected": "An English audio track is not selected in Plex.",
    "edition_mismatch": "The Plex edition does not match the requested cut.",
    "resolution_unknown": "The actual movie resolution is unknown or below the supported HD fallback.",
    "video_codec_unknown": "The movie video codec has not been identified as compatible.",
    "range_not_supported": "Plex did not return partial content for the bounded byte request.",
    "file_unavailable": "Plex reports that the current movie file is unavailable.",
    "incomplete_range": "Plex did not return exactly 65,536 requested bytes.",
    "invalid_content_range": "Plex returned a different byte range than requested.",
    "ready": "Exact movie identity, selected English audio and 65,536 accessible bytes were verified.",
    "verification_expired": "The previous playback check has expired; verify this file again.",
    "file_changed": "The Plex file or selected streams changed; verify the current file.",
}


class MovieError(ValueError):
    """Credential-free diagnostics for authenticated clients."""

    def __init__(
        self,
        code: str,
        *,
        http_status: int | None = None,
        retry_after: str | None = None,
        retry_at: str | None = None,
    ) -> None:
        self.code = code if code in _DETAILS else "invalid_input"
        self.detail = _DETAILS[self.code]
        self.http_status, self.retry_after, self.retry_at = http_status, retry_after, retry_at
        super().__init__(self.detail)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "detail": self.detail,
            "http_status": self.http_status,
            "retry_after": self.retry_after,
            "retry_at": self.retry_at,
        }


def _imdb(value: Any) -> str:
    if not isinstance(value, str) or not _IMDB.fullmatch(value):
        raise MovieError("invalid_input")
    return value


def _positive(value: Any, *, native_string: bool = False) -> int:
    if native_string and isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,9}", value):
        value = int(value)
    if type(value) is not int or not 0 < value <= 2_147_483_647:
        raise MovieError("invalid_input")
    return value


def _base(value: str) -> str:
    if not isinstance(value, str) or len(value) > 1000 or any(ord(c) < 33 for c in value):
        raise MovieError("invalid_input")
    try:
        parsed = urlsplit(value)
        valid_port = parsed.port is None or 0 < parsed.port <= 65535
    except ValueError:
        raise MovieError("invalid_input") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or "%" in parsed.path
        or "//" in parsed.path
        or any(p in {".", ".."} for p in parsed.path.split("/"))
    ):
        raise MovieError("invalid_input")
    return str(httpx.URL(value.rstrip("/").removesuffix("/api/v1")))


def _secret(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise MovieError("invalid_input")


@dataclass(frozen=True, slots=True)
class MovieConnections:
    enabled: bool = False
    riven_url: str = field(default="", repr=False)
    riven_api_key: str = field(default="", repr=False)
    seerr_url: str = field(default="", repr=False)
    seerr_api_key: str = field(default="", repr=False)
    plex_url: str = field(default="", repr=False)
    plex_token: str = field(default="", repr=False)
    plex_movies_section_id: int | None = None
    plex_machine_id: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise MovieError("invalid_input")
        if not self.enabled:
            return
        for value in (self.riven_url, self.seerr_url, self.plex_url):
            _base(value)
        for value in (self.riven_api_key, self.seerr_api_key, self.plex_token):
            _secret(value)
        if self.plex_movies_section_id is not None:
            _positive(self.plex_movies_section_id)
        if self.plex_machine_id and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", self.plex_machine_id):
            raise MovieError("invalid_input")


def _text(value: Any, maximum: int, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or any(ord(c) < 32 and c not in "\n\t\r" for c in value)
    ):
        raise MovieError("invalid_metadata")
    value = re.sub(r"https?://\S+", "[link omitted]", value).strip()
    if not value:
        if required:
            raise MovieError("invalid_metadata")
        return None
    return value


def _art(value: Any, size: str) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 200
        or not re.fullmatch(r"/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp)", value)
    ):
        return None
    return f"https://image.tmdb.org/t/p/{size}{value}"


def _genre_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 50:
        raise MovieError("invalid_metadata")
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict):
            raise MovieError("invalid_metadata")
        genre_id = _positive(row.get("id"))
        if genre_id in seen:
            raise MovieError("invalid_metadata")
        seen.add(genre_id)
        result.append({"id": genre_id, "name": _text(row.get("name"), 80, required=True)})
    return result


def _year(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        try:
            value = datetime.strptime(value, "%Y-%m-%d").year
        except ValueError:
            raise MovieError("invalid_metadata") from None
    if type(value) is not int or not 1870 <= value <= 2200:
        raise MovieError("invalid_metadata")
    return value


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _time(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, AttributeError):
        return None


def _outcome(code: str) -> str:
    if code in {
        "provider_restricted",
        "no_eligible_results",
        "investigation_exhausted",
        "anime_movie",
    }:
        return "unavailable"
    if code in {"requested", "searching", "acquiring", "verifying", "ready"}:
        return code
    if code in {
        "plex_not_found",
        "plex_identity_mismatch",
        "plex_library_mismatch",
        "invalid_plex_metadata",
        "english_audio_not_selected",
        "edition_mismatch",
        "resolution_unknown",
        "video_codec_unknown",
        "verification_expired",
        "file_changed",
    }:
        return "verifying"
    return "deferred"


def _riven_state(value: Any) -> tuple[str, str]:
    name = re.sub(r"[ _.-]", "", value).lower() if isinstance(value, str) else ""
    code = {
        "requested": "requested",
        "indexed": "searching",
        "unknown": "searching",
        "scraping": "searching",
        "scraped": "searching",
        "searching": "searching",
        "downloading": "acquiring",
        "downloaded": "acquiring",
        "acquiring": "acquiring",
        "symlinked": "verifying",
        "completed": "verifying",
        "partiallycompleted": "verifying",
        "failed": "no_eligible_results",
        "unavailable": "no_eligible_results",
        "notfound": "no_eligible_results",
        "restricted": "provider_restricted",
        "blacklisted": "provider_restricted",
        "unreleased": "unreleased",
        "paused": "paused",
    }.get(name, "unknown_backend_state")
    return _outcome(code), code


@dataclass(frozen=True, slots=True)
class _PlexFile:
    fingerprint: str
    plex_key: str
    part_key: str = field(repr=False)
    resolution: str | None = None
    selected_audio: str | None = None
    edition: str | None = None
    eligibility_code: str | None = None


class MovieService:
    """Injected movie boundary. Only verify reads bytes, and never changes audio."""

    def __init__(
        self,
        db: sqlite3.Connection,
        connections: MovieConnections,
        *,
        transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
        now: Callable[[], datetime] | None = None,
        verification_ttl_seconds: int = 900,
        max_investigations: int = 5,
        resolver: Resolver | None = None,
    ) -> None:
        if type(verification_ttl_seconds) is not int or not 1 <= verification_ttl_seconds <= 86400:
            raise MovieError("invalid_input")
        if type(max_investigations) is not int or not 1 <= max_investigations <= 20:
            raise MovieError("invalid_input")
        self.db, self.connections, self._transport = db, connections, transport
        self._resolver = resolver
        self._machine = connections.plex_machine_id
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl, self._max_investigations = verification_ttl_seconds, max_investigations
        self._scope = (
            hashlib.sha256(_base(connections.riven_url).encode()).hexdigest()
            if connections.enabled
            else ""
        )

    def _clock(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise MovieError("invalid_input")
        return value.astimezone(timezone.utc)

    def _guard(self, user_id: int) -> None:
        if not self.connections.enabled:
            raise MovieError("disabled")
        store.require_user(self.db, user_id)

    def _connection(self, service: str) -> tuple[str, dict[str, str]]:
        if service == "plex":
            return _base(self.connections.plex_url), {
                "Accept": "application/xml",
                "X-Plex-Token": self.connections.plex_token,
                "X-Plex-Product": "Omakase Plus",
                "X-Plex-Client-Identifier": "omakase-plus-movies",
            }
        return _base(getattr(self.connections, service + "_url")), {
            "Accept": "application/json",
            "X-Api-Key": getattr(self.connections, service + "_api_key"),
        }

    def _retry(self, value: str | None) -> tuple[str | None, str | None]:
        if not isinstance(value, str) or len(value) > 80:
            return None, None
        if re.fullmatch(r"[0-9]{1,10}", value):
            seconds = int(value)
            return (
                (value, _iso(self._clock() + timedelta(seconds=seconds)))
                if seconds <= 2_147_483_647
                else (None, None)
            )
        try:
            date = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None, None
        if date.tzinfo is not None and self._clock() <= date:
            return value, _iso(date)
        return None, None

    def _http_error(self, status: int, headers: httpx.Headers) -> MovieError | None:
        if 200 <= status < 300:
            return None
        if status == 429:
            after, at = self._retry(headers.get("Retry-After"))
            return MovieError("rate_limited", http_status=status, retry_after=after, retry_at=at)
        code = (
            "provider_restricted"
            if status == 451
            else (
                "connection_rejected"
                if status in {401, 403}
                else ("redirect_rejected" if 300 <= status < 400 else "backend_unavailable")
            )
        )
        return MovieError(code, http_status=status)

    async def _bytes(self, service: str, method: str, path: str, *, params=None) -> bytes:
        try:
            return await asyncio.wait_for(
                self._read_bytes(service, method, path, params=params), timeout=_OPERATION_TIMEOUT
            )
        except asyncio.TimeoutError:
            raise MovieError("transport_timeout") from None

    async def _read_bytes(self, service: str, method: str, path: str, *, params=None) -> bytes:
        base, headers = self._connection(service)
        try:
            destination = await pin_destination(
                base, transport=self._transport, resolver=self._resolver
            )
        except OutboundError as error:
            raise MovieError(error.code) from None
        headers["Host"] = destination.host_header
        url = destination.base_url + path
        if service == "seerr" and params:
            # Seerr's OpenAPI validator rejects '+' for a space in query values.
            # Encode once on the wire using %20, preserving literal '+' as %2B.
            url += "?" + urlencode(params, doseq=True, quote_via=quote)
            params = None
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(10.0, connect=5.0),
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    extensions={"sni_hostname": destination.sni_hostname},
                ) as response:
                    error = self._http_error(response.status_code, response.headers)
                    if error is not None:
                        raise error
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > _MAX_METADATA:
                            raise MovieError("invalid_metadata")
                        body.extend(chunk)
                    return bytes(body)
        except httpx.TimeoutException:
            raise MovieError("transport_timeout") from None
        except httpx.HTTPError:
            raise MovieError("transport_error") from None

    async def _json(self, service: str, path: str, *, params=None) -> dict[str, Any]:
        raw = await self._bytes(service, "GET", path, params=params)
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise MovieError("invalid_metadata") from None
        if not isinstance(data, dict):
            raise MovieError("invalid_metadata")
        return data

    async def _xml(self, path: str, *, params=None) -> ElementTree.Element:
        raw = await self._bytes("plex", "GET", path, params=params)
        if b"\x00" in raw or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise MovieError("invalid_plex_metadata")
        try:
            root = ElementTree.fromstring(raw)
        except (ElementTree.ParseError, ValueError):
            raise MovieError("invalid_plex_metadata") from None
        if root.tag != "MediaContainer":
            raise MovieError("invalid_plex_metadata")
        return root

    async def _metadata(self, tmdb_id: int, *, imdb_id: str | None = None) -> dict[str, Any]:
        data = await self._json("seerr", f"/api/v1/movie/{tmdb_id}")
        if data.get("id") != tmdb_id or type(data.get("id")) is not int:
            raise MovieError("identity_mismatch")
        external = data.get("externalIds", {})
        if not isinstance(external, dict):
            raise MovieError("invalid_metadata")
        values = [
            value
            for value in (data.get("imdbId"), external.get("imdbId"))
            if value not in (None, "")
        ]
        if not values or any(
            not isinstance(value, str) or not _IMDB.fullmatch(value) for value in values
        ):
            raise MovieError("identity_mismatch")
        if len(set(values)) != 1 or (imdb_id is not None and values[0] != imdb_id):
            raise MovieError("identity_mismatch")
        metadata = {
            "imdb_id": values[0],
            "tmdb_id": tmdb_id,
            "title": _text(data.get("title"), 300, required=True),
            "year": _year(data.get("releaseDate")),
            "poster_url": _art(data.get("posterPath"), "w342"),
            "backdrop_url": _art(data.get("backdropPath"), "w1280"),
            "overview": _text(data.get("overview"), 5000),
        }
        if "genres" in data:
            metadata["genres"] = _genre_rows(data["genres"])
        return metadata

    def _card(self, user_id: int, metadata: dict[str, Any]) -> dict[str, Any]:
        request = store.get_request(self.db, user_id, metadata["imdb_id"])
        code = request["code"] if request else "verifying"
        return {
            "imdb_id": metadata["imdb_id"],
            "tmdb_id": metadata["tmdb_id"],
            "title": metadata["title"],
            "year": metadata["year"],
            "state": request["state"] if request else "verifying",
            "code": code,
            "detail": _DETAILS.get(code, _DETAILS["verifying"]),
            "resolution": None,
            "plex_key": None,
            "plex_url": None,
            "selected_audio": None,
            "verified_at": None,
            "http_status": None,
            "bytes_read": 0,
            "poster_url": metadata.get("poster_url"),
            "backdrop_url": metadata.get("backdrop_url"),
            "overview": metadata.get("overview"),
            "genres": store.list_movie_genres(self.db, user_id, metadata["imdb_id"]),
            "edition": request["requested_edition"] if request else None,
            "requested": request is not None,
            "retry_after": request["retry_after"] if request else None,
            "retry_at": request["retry_at"] if request else None,
        }

    def _failure(self, card: dict[str, Any], error: MovieError) -> dict[str, Any]:
        return {**card, "state": _outcome(error.code), **error.as_dict(), "plex_url": None}

    def _save_status(
        self,
        user_id: int,
        imdb_id: str,
        code: str,
        *,
        backend_id=None,
        retry_after=None,
        retry_at=None,
        investigation=False,
    ) -> None:
        store.update_request(
            self.db,
            user_id,
            imdb_id,
            now=_iso(self._clock()),
            investigation=investigation,
            state=_outcome(code),
            code=code,
            detail=_DETAILS[code],
            backend_id=backend_id,
            retry_after=retry_after,
            retry_at=retry_at,
        )

    async def search(self, user_id: int, query: str) -> list[dict[str, Any]]:
        self._guard(user_id)
        if (
            not isinstance(query, str)
            or not query.strip()
            or len(query) > 200
            or any(ord(c) < 32 for c in query)
        ):
            raise MovieError("invalid_input")
        query = query.strip()
        exact_imdb = query if _IMDB.fullmatch(query) else None
        data = await self._json(
            "seerr",
            "/api/v1/search",
            params={"query": "imdb:" + exact_imdb if exact_imdb else query, "page": 1},
        )
        results = data.get("results")
        if not isinstance(results, list) or len(results) > 100:
            raise MovieError("invalid_metadata")
        seen, cards = set(), []
        for row in results:
            if not isinstance(row, dict):
                raise MovieError("invalid_metadata")
            if row.get("mediaType") != "movie":
                continue
            tmdb_id = _positive(row.get("id"))
            if tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            if len(cards) >= 20:
                break
            metadata = await self._metadata(tmdb_id)
            if exact_imdb and metadata["imdb_id"] != exact_imdb:
                raise MovieError("identity_mismatch")
            store.upsert_movie(self.db, user_id, metadata, now=_iso(self._clock()))
            cards.append(self._card(user_id, metadata))
        return cards

    async def _identity(self, user_id: int, imdb_id: str) -> dict[str, Any]:
        known = store.get_movie(self.db, user_id, imdb_id)
        if known and known["identity_verified"]:
            return known
        data = await self._json(
            "seerr", "/api/v1/search", params={"query": "imdb:" + imdb_id, "page": 1}
        )
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) > 100:
            raise MovieError("invalid_metadata")
        matched, seen = [], set()
        for row in rows[:20]:
            if not isinstance(row, dict):
                raise MovieError("invalid_metadata")
            if row.get("mediaType") != "movie":
                continue
            tmdb_id = _positive(row.get("id"))
            if tmdb_id in seen:
                continue
            seen.add(tmdb_id)
            metadata = await self._metadata(tmdb_id)
            if metadata["imdb_id"] == imdb_id:
                matched.append(metadata)
        if len(matched) != 1:
            raise MovieError("identity_mismatch")
        return store.upsert_movie(self.db, user_id, matched[0], now=_iso(self._clock()))

    async def detail(self, user_id: int, imdb_id: str) -> dict[str, Any]:
        """Read exact cached/catalog metadata without acquiring or probing media."""
        self._guard(user_id)
        return self._card(user_id, await self._identity(user_id, _imdb(imdb_id)))

    def _riven_item(self, row: Any) -> dict[str, Any]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("type"), str)
            or row["type"].casefold() != "movie"
        ):
            raise MovieError("invalid_riven_metadata")
        try:
            imdb_id = _imdb(row.get("imdb_id"))
            tmdb_id = (
                _positive(row["tmdb_id"], native_string=True)
                if row.get("tmdb_id") is not None
                else None
            )
            if type(row.get("id")) not in {str, int}:
                raise MovieError("invalid_input")
            backend_id = str(row["id"])
            if not _BACKEND_ID.fullmatch(backend_id):
                raise MovieError("invalid_input")
            title, year = _text(row.get("title"), 300, required=True), _year(row.get("year"))
        except MovieError:
            raise MovieError("invalid_riven_metadata") from None
        state, code = _riven_state(row.get("state"))
        return {
            "imdb_id": imdb_id,
            "tmdb_id": tmdb_id,
            "backend_id": backend_id,
            "title": title,
            "year": year,
            "state": state,
            "code": code,
        }

    async def _inventory(self) -> list[dict[str, Any]]:
        data = await self._json("riven", "/api/v1/items", params={"limit": 100, "type": "movie"})
        rows = data.get("items")
        if not isinstance(rows, list) or len(rows) > 100:
            raise MovieError("invalid_riven_metadata")
        for name in ("total", "total_count", "totalCount", "total_items"):
            total = data.get(name)
            if total is not None and (type(total) is not int or total != len(rows)):
                raise MovieError("inventory_incomplete")
        for name in ("page", "total_pages"):
            value = data.get(name)
            if value is not None and (type(value) is not int or value < 0 or value > 1):
                raise MovieError("inventory_incomplete")
        if len(rows) == 100:
            raise MovieError("inventory_incomplete")
        result, seen = [], set()
        for raw in rows:
            row = self._riven_item(raw)
            if row["imdb_id"] in seen:
                raise MovieError("invalid_riven_metadata")
            seen.add(row["imdb_id"])
            result.append(row)
        return result

    async def _backend(self, imdb_id: str, tmdb_id: int) -> dict[str, Any] | None:
        item = next((row for row in await self._inventory() if row["imdb_id"] == imdb_id), None)
        if item is None:
            return None
        direct = self._riven_item(await self._json("riven", "/api/v1/items/" + item["backend_id"]))
        if (
            direct["imdb_id"] != imdb_id
            or direct["backend_id"] != item["backend_id"]
            or direct["tmdb_id"] not in {None, tmdb_id}
            or item["tmdb_id"] not in {None, tmdb_id}
        ):
            raise MovieError("identity_mismatch")
        return direct

    async def request(
        self,
        user_id: int,
        *,
        imdb_id: str,
        tmdb_id: int,
        edition: str | None = None,
        _authorize_submission: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Explicit intake only. Auth repair never reclaims an acknowledged write."""
        self._guard(user_id)
        imdb_id, tmdb_id = _imdb(imdb_id), _positive(tmdb_id)
        if edition is not None:
            edition = _text(edition, 80, required=True)
        metadata = await self._metadata(tmdb_id, imdb_id=imdb_id)
        store.upsert_movie(self.db, user_id, metadata, now=_iso(self._clock()))
        store.ensure_request(self.db, user_id, imdb_id, now=_iso(self._clock()), edition=edition)
        card = self._card(user_id, metadata)
        prior = store.get_submission(self.db, self._scope, imdb_id)
        if prior and prior["tmdb_id"] != tmdb_id:
            raise MovieError("identity_mismatch")
        if prior and (prior["code"] == "provider_restricted" or self._waiting(prior)):
            self._save_status(
                user_id,
                imdb_id,
                prior["code"],
                backend_id=prior["backend_id"],
                retry_after=prior["retry_after"],
                retry_at=prior["retry_at"],
            )
            return self._card(user_id, metadata)
        claimed = False
        try:
            # A successful current authenticated read is required before a
            # definitive POST401/403 may be retried by this explicit action.
            existing = await self._backend(imdb_id, tmdb_id)
            if existing:
                self._save_status(
                    user_id, imdb_id, existing["code"], backend_id=existing["backend_id"]
                )
                if prior is not None:
                    store.update_submission(
                        self.db,
                        self._scope,
                        imdb_id,
                        state=existing["state"],
                        code=existing["code"],
                        detail=_DETAILS[existing["code"]],
                        backend_id=existing["backend_id"],
                    )
                return self._card(user_id, metadata)
            if _authorize_submission is not None:
                _authorize_submission()
            claimed = store.claim_submission(
                self.db,
                self._scope,
                imdb_id,
                tmdb_id,
                now=_iso(self._clock()),
                retry_rejected=prior is not None and prior["code"] == "connection_rejected",
            )
            if not claimed:
                prior = store.get_submission(self.db, self._scope, imdb_id)
                assert prior is not None
                self._save_status(
                    user_id,
                    imdb_id,
                    prior["code"],
                    backend_id=prior["backend_id"],
                    retry_after=prior["retry_after"],
                    retry_at=prior["retry_at"],
                )
                return self._card(user_id, metadata)
            await self._bytes("riven", "POST", "/api/v1/items/add", params={"imdb_ids": imdb_id})
            # Record acknowledgement BEFORE the follow-up read: a read-side
            # auth error cannot turn an accepted write into an auth retry.
            store.update_submission(
                self.db,
                self._scope,
                imdb_id,
                state="deferred",
                code="acknowledgement_unconfirmed",
                detail=_DETAILS["acknowledgement_unconfirmed"],
            )
            persisted = await self._backend(imdb_id, tmdb_id)
            code = persisted["code"] if persisted else "acknowledgement_unconfirmed"
            backend_id = persisted["backend_id"] if persisted else None
            store.update_submission(
                self.db,
                self._scope,
                imdb_id,
                state=_outcome(code),
                code=code,
                detail=_DETAILS[code],
                backend_id=backend_id,
            )
            self._save_status(user_id, imdb_id, code, backend_id=backend_id)
            return self._card(user_id, metadata)
        except MovieError as error:
            current = store.get_submission(self.db, self._scope, imdb_id)
            if claimed and current and current["code"] == "submission_pending":
                store.update_submission(
                    self.db,
                    self._scope,
                    imdb_id,
                    state=_outcome(error.code),
                    code=error.code,
                    detail=error.detail,
                    retry_after=error.retry_after,
                    retry_at=error.retry_at,
                )
            elif (
                claimed
                and current
                and current["code"] == "acknowledgement_unconfirmed"
                and error.code == "rate_limited"
            ):
                # Preserve the read-side cooldown without erasing the accepted
                # write marker or making it eligible for another submission.
                store.update_submission(
                    self.db,
                    self._scope,
                    imdb_id,
                    retry_after=error.retry_after,
                    retry_at=error.retry_at,
                )
            self._save_status(
                user_id, imdb_id, error.code, retry_after=error.retry_after, retry_at=error.retry_at
            )
            return self._failure(card, error)

    def _waiting(self, record: dict[str, Any]) -> bool:
        until = _time(record.get("retry_at"))
        return (record.get("code") == "rate_limited" and until is None) or (
            until is not None and self._clock() < until
        )

    async def refresh(self, user_id: int, imdb_id: str) -> dict[str, Any]:
        self._guard(user_id)
        imdb_id = _imdb(imdb_id)
        metadata, request = (
            store.get_movie(self.db, user_id, imdb_id),
            store.get_request(self.db, user_id, imdb_id),
        )
        if metadata is None or request is None:
            raise MovieError("invalid_input")
        if request["code"] in {
            "provider_restricted",
            "no_eligible_results",
            "investigation_exhausted",
        } or self._waiting(request):
            return self._card(user_id, metadata)
        if request["investigations"] >= self._max_investigations:
            self._save_status(user_id, imdb_id, "investigation_exhausted")
            return self._card(user_id, metadata)
        try:
            existing = await self._backend(imdb_id, metadata["tmdb_id"])
            code = existing["code"] if existing else "backend_item_missing"
            self._save_status(
                user_id,
                imdb_id,
                code,
                backend_id=existing["backend_id"] if existing else None,
                investigation=existing is None or code == "unknown_backend_state",
            )
        except MovieError as error:
            self._save_status(
                user_id,
                imdb_id,
                error.code,
                investigation=True,
                retry_after=error.retry_after,
                retry_at=error.retry_at,
            )
        return self._card(user_id, metadata)

    async def _sections(self) -> tuple[int, int | None]:
        root = await self._xml("/library/sections")
        if not self._machine:
            identity = await self._xml("/identity")
            machine = identity.get("machineIdentifier", "")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", machine):
                raise MovieError("invalid_plex_metadata")
            self._machine = machine
        movies, anime = [], []
        for row in root.findall("Directory"):
            if row.get("title") not in {"Movies", "Anime Movies"}:
                continue
            try:
                key = _positive(row.get("key"), native_string=True)
            except MovieError:
                raise MovieError("invalid_plex_metadata") from None
            if row.get("type") != "movie":
                raise MovieError("plex_library_mismatch")
            (movies if row.get("title") == "Movies" else anime).append(key)
        configured = self.connections.plex_movies_section_id
        if (
            len(movies) != 1
            or (configured is not None and movies[0] != configured)
            or len(anime) > 1
        ):
            raise MovieError("plex_library_mismatch")
        return movies[0], anime[0] if anime else None

    async def _videos(self, section: int) -> list[ElementTree.Element]:
        videos = []
        for offset in range(0, 1000, 100):
            params = {
                "type": "1",
                "includeGuids": "1",
                "includeDetails": "1",
                "X-Plex-Container-Start": str(offset),
                "X-Plex-Container-Size": "100",
            }
            root = await self._xml(f"/library/sections/{section}/all", params=params)
            rows = root.findall("Video")
            if len(rows) > 100:
                raise MovieError("invalid_plex_metadata")
            for row in rows:
                for key in ("librarySectionID", "librarySectionTitle"):
                    if row.get(key) is None and root.get(key) is not None:
                        row.set(key, root.get(key))
            videos.extend(rows)
            try:
                total = int(root.get("totalSize", str(len(videos))))
            except ValueError:
                raise MovieError("invalid_plex_metadata") from None
            if total < 0 or total > 1000:
                raise MovieError("invalid_plex_metadata")
            if len(rows) < 100 or len(videos) >= total:
                return videos
        raise MovieError("invalid_plex_metadata")

    @staticmethod
    def _guids(video: ElementTree.Element, namespace: str) -> set[str]:
        prefix = namespace + "://"
        values = [row.get("id", "") for row in video.findall("Guid")]
        if video.get("guid", "").startswith(prefix):
            values.append(video.get("guid"))
        return {value[len(prefix) :] for value in values if value.startswith(prefix)}

    async def _rich_videos(self, videos: list[ElementTree.Element]) -> list[ElementTree.Element]:
        """Complete abbreviated library rows with bounded, batched metadata only."""
        if len(videos) > 100:
            raise MovieError("invalid_plex_metadata")
        pending = {}
        for video in videos:
            if not video.findall("./Media/Part/Stream"):
                key = video.get("ratingKey", "")
                if not re.fullmatch(r"[1-9][0-9]{0,19}", key):
                    raise MovieError("invalid_plex_metadata")
                pending[key] = video
        if not pending:
            return videos
        complete, semaphore = {}, asyncio.Semaphore(3)

        async def batch(keys):
            async with semaphore:
                root = await self._xml(
                    "/library/metadata/" + ",".join(keys), params={"includeGuids": "1"}
                )
                rows, found = root.findall("Video"), set()
                if len(rows) != len(keys):
                    raise MovieError("invalid_plex_metadata")
                for row in rows:
                    key = row.get("ratingKey", "")
                    if key not in keys or key in found:
                        raise MovieError("plex_identity_mismatch")
                    found.add(key)
                    for field in ("librarySectionID", "librarySectionTitle"):
                        if row.get(field) is None:
                            value = root.get(field)
                            if value is None:
                                value = pending[key].get(field)
                            if value is not None:
                                row.set(field, value)
                    complete[key] = row

        keys = list(pending)
        tasks = [
            asyncio.create_task(batch(keys[start : start + 20]))
            for start in range(0, len(keys), 20)
        ]
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=_OPERATION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise MovieError("transport_timeout") from None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return [complete.get(video.get("ratingKey"), video) for video in videos]

    async def _plex_movie(self, metadata: dict[str, Any]) -> tuple[ElementTree.Element, int]:
        section, anime_section = await self._sections()
        # Modern Plex agents have a primary plex:// GUID. The guid query filters
        # that primary value, so external IMDb matching uses bounded inventory.
        videos = await self._videos(section)
        matches = [video for video in videos if metadata["imdb_id"] in self._guids(video, "imdb")]
        if len(matches) != 1:
            if not matches and anime_section is not None:
                anime = await self._videos(anime_section)
                if any(metadata["imdb_id"] in self._guids(video, "imdb") for video in anime):
                    raise MovieError("anime_movie")
            raise MovieError("plex_identity_mismatch" if matches else "plex_not_found")
        key = matches[0].get("ratingKey", "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", key):
            raise MovieError("invalid_plex_metadata")
        root = await self._xml("/library/metadata/" + key, params={"includeGuids": "1"})
        direct = root.findall("Video")
        if len(direct) != 1 or direct[0].get("ratingKey") != key:
            raise MovieError("plex_identity_mismatch")
        for name in ("librarySectionID", "librarySectionTitle"):
            if direct[0].get(name) is None and root.get(name) is not None:
                direct[0].set(name, root.get(name))
        return direct[0], section

    def _file(
        self,
        video: ElementTree.Element,
        section: int,
        metadata: dict[str, Any],
        requested_edition: str | None,
    ) -> _PlexFile:
        if video.get("type") != "movie" or self._guids(video, "imdb") != {metadata["imdb_id"]}:
            raise MovieError("plex_identity_mismatch")
        tmdb_guids = self._guids(video, "tmdb")
        if (
            metadata["tmdb_id"] is not None
            and tmdb_guids
            and tmdb_guids != {str(metadata["tmdb_id"])}
        ):
            raise MovieError("plex_identity_mismatch")
        if (
            video.get("librarySectionID") != str(section)
            or video.get("librarySectionTitle") != "Movies"
        ):
            raise MovieError("plex_library_mismatch")
        # Festival and regional release years can differ by one year. Require
        # both exact catalog IDs before accepting that narrowly bounded variation.
        plex_year = video.get("year", "")
        nearby_release_year = (
            bool(re.fullmatch(r"[0-9]{4}", plex_year))
            and metadata.get("year") is not None
            and abs(int(plex_year) - metadata["year"]) <= 1
        )
        if (
            metadata.get("year")
            and plex_year != str(metadata["year"])
            and not (tmdb_guids == {str(metadata["tmdb_id"])} and nearby_release_year)
        ):
            raise MovieError("plex_identity_mismatch")
        key = video.get("ratingKey", "")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", key):
            raise MovieError("invalid_plex_metadata")
        media = video.findall("Media")
        if not media or len(media) > 20:
            raise MovieError("invalid_plex_metadata")
        try:
            media.sort(
                key=lambda row: (int(row.get("width", "0")), int(row.get("height", "0"))),
                reverse=True,
            )
        except ValueError:
            raise MovieError("invalid_plex_metadata") from None
        selected = media[0]
        parts = selected.findall("Part")
        if len(parts) != 1:
            raise MovieError("invalid_plex_metadata")
        part = parts[0]
        part_key, part_id = part.get("key", ""), part.get("id", "")
        if (
            not re.fullmatch(r"[1-9][0-9]{0,19}", part_id)
            or not part_key.startswith(f"/library/parts/{part_id}/")
            or not re.fullmatch(
                r"/library/parts/[0-9]+/(?:[A-Za-z0-9_~.-]+/){0,2}[A-Za-z0-9_~.-]+", part_key
            )
            or any(piece in {".", ".."} for piece in part_key.split("/"))
            or len(part_key) > 300
        ):
            raise MovieError("invalid_plex_metadata")
        file_path = part.get("file", "")
        try:
            size = int(part.get("size", "0"))
            width, height = int(selected.get("width", "0")), int(selected.get("height", "0"))
        except ValueError:
            raise MovieError("invalid_plex_metadata") from None
        if (
            not file_path
            or len(file_path) > 4096
            or size <= 0
            or size > 100 * 1024**4
            or not 0 <= width <= 16384
            or not 0 <= height <= 16384
        ):
            raise MovieError("invalid_plex_metadata")
        resolution = (
            "2160p"
            if width >= 3800 or height >= 2000
            else "1080p"
            if width >= 1900 or height >= 1000
            else "720p"
            if width >= 1200 or height >= 700
            else None
        )
        audio = [
            row
            for row in part.findall("Stream")
            if row.get("streamType") == "2" and row.get("selected") == "1"
        ]
        language = audio[0].get("languageCode", "").lower() if len(audio) == 1 else ""
        selected_audio = {
            "en": "English",
            "eng": "English",
            "spa": "Spanish",
            "es": "Spanish",
            "fra": "French",
            "fre": "French",
            "fr": "French",
            "jpn": "Japanese",
            "ja": "Japanese",
        }.get(language, "Unknown" if audio else None)
        edition = _text(video.get("editionTitle"), 80)
        codec = selected.get("videoCodec", "").lower()
        eligibility = (
            "english_audio_not_selected"
            if language not in {"en", "eng"}
            else (
                "edition_mismatch"
                if requested_edition
                and (not edition or edition.casefold() != requested_edition.casefold())
                else (
                    "resolution_unknown"
                    if resolution is None
                    else (
                        "video_codec_unknown"
                        if codec
                        not in {"h264", "hevc", "h265", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"}
                        else None
                    )
                )
            )
        )
        connection = _base(self.connections.plex_url) + "\0" + self.connections.plex_token
        identity = {
            "connection": hashlib.sha256(connection.encode()).hexdigest(),
            "section": section,
            "key": key,
            "imdb": sorted(self._guids(video, "imdb")),
            "tmdb": sorted(tmdb_guids),
            "updated": video.get("updatedAt"),
            "edition": edition,
            "media": {
                name: selected.get(name)
                for name in ("id", "width", "height", "videoResolution", "videoCodec", "container")
            },
            "part": {
                name: part.get(name)
                for name in ("id", "key", "file", "size", "duration", "exists", "accessible")
            },
            "audio": [
                {
                    name: row.get(name)
                    for name in ("id", "languageCode", "codec", "channels", "selected", "default")
                }
                for row in part.findall("Stream")
                if row.get("streamType") == "2"
            ],
        }
        fingerprint = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return _PlexFile(
            fingerprint, key, part_key, resolution, selected_audio, edition, eligibility
        )

    def _observed(self, card: dict[str, Any], observed: _PlexFile) -> dict[str, Any]:
        return {
            **card,
            "plex_key": observed.plex_key,
            "resolution": observed.resolution,
            "selected_audio": observed.selected_audio,
            "edition": observed.edition or card["edition"],
        }

    def _cached(
        self, user_id: int, card: dict[str, Any], observed: _PlexFile, *, force: bool = False
    ) -> dict[str, Any] | None:
        cached = store.get_verification(self.db, user_id, card["imdb_id"], observed.fingerprint)
        if cached is None:
            return None
        # Policy-specific receipts need their real viewer/request applicability
        # checked; an English legacy read cannot reuse a schema2 ready flag.
        if cached.get("proof_schema", 1) != 1:
            return None
        if cached["fingerprint"] != observed.fingerprint or observed.eligibility_code is not None:
            store.invalidate_verification(self.db, user_id, card["imdb_id"])
            return None
        expiry = _time(cached["expires_at"])
        protected = cached["code"] == "provider_restricted" or self._waiting(cached)
        if not protected and (force or expiry is None or self._clock() >= expiry):
            return None
        result = self._observed(card, observed)
        for name in (
            "state",
            "code",
            "detail",
            "http_status",
            "bytes_read",
            "verified_at",
            "retry_after",
            "retry_at",
        ):
            result[name] = cached[name]
        if result["state"] == "ready":
            result["plex_url"] = self._plex_link(observed.plex_key)
        return result

    def _plex_link(self, key: str) -> str | None:
        machine = self._machine
        if not machine:
            return None
        return f"https://app.plex.tv/desktop/#!/server/{machine}/details?key={quote('/library/metadata/' + key, safe='')}"

    async def _probe(self, part_key: str) -> dict[str, Any]:
        try:
            return await asyncio.wait_for(self._read_probe(part_key), timeout=_OPERATION_TIMEOUT)
        except asyncio.TimeoutError:
            return {**MovieError("transport_timeout").as_dict(), "bytes_read": 0}

    async def _read_probe(self, part_key: str) -> dict[str, Any]:
        base, headers = self._connection("plex")
        try:
            destination = await pin_destination(
                base, transport=self._transport, resolver=self._resolver
            )
        except OutboundError as error:
            return {**MovieError(error.code).as_dict(), "bytes_read": 0}
        headers["Host"] = destination.host_header
        headers["Range"] = "bytes=0-65535"
        headers["Accept-Encoding"] = "identity"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(15.0, connect=5.0),
                trust_env=False,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                async with client.stream(
                    "GET",
                    destination.base_url + part_key,
                    headers=headers,
                    extensions={"sni_hostname": destination.sni_hostname},
                ) as response:
                    status = response.status_code
                    error = self._http_error(status, response.headers)
                    if error is not None:
                        if status == 404:
                            error = MovieError("file_unavailable", http_status=status)
                        return {**error.as_dict(), "bytes_read": 0}
                    if status != 206:
                        return {
                            **MovieError("range_not_supported", http_status=status).as_dict(),
                            "bytes_read": 0,
                        }
                    if response.headers.get("Content-Encoding", "identity").lower() not in {
                        "identity",
                        "",
                    }:
                        return {
                            **MovieError("invalid_content_range", http_status=status).as_dict(),
                            "bytes_read": 0,
                        }
                    match = re.fullmatch(
                        r"bytes 0-65535/([0-9]+)", response.headers.get("Content-Range", "")
                    )
                    if match is None or int(match[1]) < _RANGE_BYTES:
                        return {
                            **MovieError("invalid_content_range", http_status=status).as_dict(),
                            "bytes_read": 0,
                        }
                    count = 0
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        count += len(chunk)
                        if count > _RANGE_BYTES:
                            break
                    code = "ready" if count == _RANGE_BYTES else "incomplete_range"
                    return {
                        "code": code,
                        "detail": _DETAILS[code],
                        "http_status": status,
                        "bytes_read": count,
                        "retry_after": None,
                        "retry_at": None,
                    }
        except httpx.TimeoutException:
            return {**MovieError("transport_timeout").as_dict(), "bytes_read": 0}
        except httpx.HTTPError:
            return {**MovieError("transport_error").as_dict(), "bytes_read": 0}

    async def verify(self, user_id: int, imdb_id: str, *, force: bool = False) -> dict[str, Any]:
        self._guard(user_id)
        imdb_id = _imdb(imdb_id)
        if type(force) is not bool:
            raise MovieError("invalid_input")
        metadata = await self._identity(user_id, imdb_id)
        card = self._card(user_id, metadata)
        try:
            video, section = await self._plex_movie(metadata)
            observed = self._file(video, section, metadata, card["edition"])
        except MovieError as error:
            store.invalidate_verification(self.db, user_id, imdb_id)
            return self._failure(card, error)
        card = self._observed(card, observed)
        cached = self._cached(user_id, card, observed, force=force)
        if cached is not None:
            return cached
        if observed.eligibility_code is not None:
            return self._failure(card, MovieError(observed.eligibility_code))
        proof = await self._probe(observed.part_key)
        verified = self._clock()
        ttl = self._ttl if proof["code"] == "ready" else min(60, self._ttl)
        receipt = {
            **proof,
            "state": _outcome(proof["code"]),
            "fingerprint": observed.fingerprint,
            "plex_key": observed.plex_key,
            "resolution": observed.resolution,
            "selected_audio": observed.selected_audio,
            "edition": observed.edition,
            "verified_at": _iso(verified),
            "expires_at": _iso(verified + timedelta(seconds=ttl)),
        }
        store.save_verification(self.db, user_id, imdb_id, receipt)
        result = self._cached(user_id, card, observed)
        assert result is not None
        return result

    async def request_language(self, actor_user_id: int, *, request_id: str) -> dict[str, Any]:
        from omakase.plus.movie_languages import request_language

        return await request_language(self, actor_user_id, request_id=request_id)

    def _language_card(self, actor_user_id: int, request_id: str):
        from omakase.plus.households import resolve_scope
        from omakase.plus.language_store import load_request

        store.require_user(self.db, actor_user_id)
        snapshot, _, _ = load_request(self.db, actor_user_id=actor_user_id, request_id=request_id)
        if snapshot.domain != "movie":
            raise MovieError("invalid_input")
        scope = resolve_scope(self.db, viewer_id=actor_user_id, library_id=snapshot.library_id)
        imdb_id = _imdb(snapshot.media_identity.removeprefix("imdb:"))
        metadata = store.get_movie(self.db, scope.operational_owner_id, imdb_id)
        if metadata is None or not metadata["identity_verified"]:
            raise MovieError("identity_mismatch")
        return self._card(actor_user_id, metadata)

    @staticmethod
    def _language_unavailable():
        return {
            "state": "verifying",
            "code": "language_proof_required",
            "detail": "This account needs a current movie language and playback check.",
            "plex_url": None,
        }

    def language_status(self, actor_user_id: int, *, request_id: str) -> dict[str, Any]:
        from omakase.plus.movie_languages import language_status

        return language_status(self, actor_user_id, request_id=request_id)

    async def verify_language(
        self, actor_user_id: int, *, request_id: str, rating_key: str
    ) -> dict[str, Any]:
        from omakase.plus.movie_languages import verify_language

        return await verify_language(
            self, actor_user_id, request_id=request_id, rating_key=rating_key
        )

    async def _hydrate(
        self, user_id: int, *, excluded: set[str], records: list[dict[str, Any]]
    ) -> None:
        """Hydrate at most 12 native identities, three metadata reads at a time.

        Positive mappings persist. Failed lookups keep their native card and are
        checked no more than hourly, respecting any longer Retry-After value.
        """
        candidates = []
        for row in records:
            checked = _time(row.get("metadata_checked_at"))
            retry = _time(row.get("metadata_retry_at"))
            if (
                row["imdb_id"] in excluded
                or row["identity_verified"]
                or row["tmdb_id"] is None
                or (retry is not None and self._clock() < retry)
                or (row.get("metadata_error_code") == "rate_limited" and retry is None)
                or (checked is not None and self._clock() < checked + timedelta(hours=1))
            ):
                continue
            candidates.append(row)
            if len(candidates) == 12:
                break
        semaphore = asyncio.Semaphore(3)
        limited: MovieError | None = None

        async def one(row: dict[str, Any]) -> None:
            nonlocal limited
            async with semaphore:
                try:
                    if limited is not None:
                        raise limited
                    metadata = await self._metadata(row["tmdb_id"], imdb_id=row["imdb_id"])
                    store.upsert_movie(self.db, user_id, metadata, now=_iso(self._clock()))
                except ValueError as error:
                    failure = (
                        error if isinstance(error, MovieError) else MovieError("identity_mismatch")
                    )
                    if failure.code == "rate_limited":
                        limited = failure
                    store.mark_metadata_check(
                        self.db,
                        user_id,
                        row["imdb_id"],
                        now=_iso(self._clock()),
                        error_code=failure.code,
                        retry_after=failure.retry_after,
                        retry_at=failure.retry_at,
                    )

        await asyncio.gather(*(one(row) for row in candidates))

    async def list(self, user_id: int, *, view: str = "all") -> list[dict[str, Any]]:
        """List at most 100 cards, with current metadata and no media byte reads."""
        self._guard(user_id)
        if view not in {"all", "requests", "ready", "discover"}:
            raise MovieError("invalid_input")
        inventory_error, plex_error = None, None
        inventory, videos, anime_ids, section = [], [], set(), None
        try:
            inventory = await self._inventory()
        except MovieError as error:
            inventory_error = error
        try:
            section, anime_section = await self._sections()
            videos = await self._videos(section)
            if anime_section is not None:
                for video in await self._videos(anime_section):
                    anime_ids.update(self._guids(video, "imdb"))
        except MovieError as error:
            plex_error = error
        by_imdb = {row["imdb_id"]: row for row in inventory}
        native_ids = set(by_imdb) - anime_ids
        for row in inventory:
            if row["imdb_id"] not in anime_ids:
                store.upsert_movie(
                    self.db, user_id, row, now=_iso(self._clock()), identity_verified=False
                )
        for video in videos:
            imdbs = self._guids(video, "imdb")
            if len(imdbs) != 1:
                continue
            imdb_id = next(iter(imdbs))
            if not _IMDB.fullmatch(imdb_id) or imdb_id in anime_ids:
                continue
            tmdbs = self._guids(video, "tmdb")
            tmdb_id = None
            if len(tmdbs) == 1:
                try:
                    tmdb_id = _positive(next(iter(tmdbs)), native_string=True)
                except MovieError:
                    continue
            try:
                metadata = {
                    "imdb_id": imdb_id,
                    "tmdb_id": tmdb_id,
                    "title": _text(video.get("title"), 300, required=True),
                    "year": _year(int(video.get("year")))
                    if video.get("year", "").isdigit()
                    else None,
                }
                store.upsert_movie(
                    self.db, user_id, metadata, now=_iso(self._clock()), identity_verified=False
                )
                native_ids.add(imdb_id)
            except (ValueError, TypeError):
                continue

        def visible_records():
            if view == "discover":
                return store.list_movies(self.db, user_id)
            return store.collection_movies(
                self.db, user_id, native_ids=native_ids, requested_only=view == "requests"
            )

        await self._hydrate(user_id, excluded=anime_ids, records=visible_records())
        visible = visible_records()
        if plex_error is None:
            wanted = {row["imdb_id"] for row in visible}
            try:
                videos = await self._rich_videos(
                    [video for video in videos if self._guids(video, "imdb") & wanted]
                )
            except MovieError as error:
                plex_error, videos = error, []
        cards = []
        for metadata in visible:
            imdb_id = metadata["imdb_id"]
            request = store.get_request(self.db, user_id, imdb_id)
            if imdb_id in anime_ids and request is None:
                continue
            if request is not None and view == "discover":
                continue
            card = self._card(user_id, metadata)
            if imdb_id in anime_ids:
                card = self._failure(card, MovieError("anime_movie"))
            elif imdb_id in by_imdb and (
                request is None
                or (
                    request["investigations"] < self._max_investigations
                    and request["code"]
                    not in {"provider_restricted", "no_eligible_results", "investigation_exhausted"}
                )
            ):
                native = by_imdb[imdb_id]
                card.update(
                    state=native["state"], code=native["code"], detail=_DETAILS[native["code"]]
                )
            elif inventory_error is not None:
                card = self._failure(card, inventory_error)
            matches = [video for video in videos if imdb_id in self._guids(video, "imdb")]
            if len(matches) == 1 and section is not None and plex_error is None:
                try:
                    observed = self._file(matches[0], section, metadata, card["edition"])
                    card = self._observed(card, observed)
                    cached = self._cached(user_id, card, observed)
                    if cached is not None:
                        card = cached
                    else:
                        previous = store.get_verification(
                            self.db, user_id, imdb_id, observed.fingerprint
                        )
                        expired = (
                            observed.eligibility_code is None
                            and previous is not None
                            and previous["code"] == "ready"
                        )
                        card = self._failure(
                            card,
                            MovieError(
                                "verification_expired"
                                if expired
                                else observed.eligibility_code or "verifying"
                            ),
                        )
                        if expired:
                            # Historical proof never grants current Ready status or a play link.
                            card["last_checked_at"] = previous["verified_at"]
                except MovieError as error:
                    store.invalidate_verification(self.db, user_id, imdb_id)
                    card = self._failure(card, error)
            else:
                store.invalidate_verification(self.db, user_id, imdb_id)
                if plex_error is not None:
                    card = self._failure(card, plex_error)
            if view != "ready" or card["state"] == "ready":
                cards.append(card)
        return cards
