"""Owned episode stills with bounded, token-safe, lazy metadata/image reads."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from xml.etree import ElementTree

import httpx

from omakase.plus.artwork_delivery import optimized_artwork
from omakase.plus.connections import (
    _read,
    _saved_health,
    _spec,
    _values,
    private_connections_enabled,
)
from omakase.plus.outbound import OutboundError, blocked_address, pin_destination
from omakase.plus.posters import PosterUnavailable

_METADATA_LIMIT = 512 * 1024
_IMAGE_LIMIT = 6 * 1024 * 1024
_CACHE_BYTES = 16 * 1024 * 1024
_CACHE = OrderedDict()
_INFLIGHT = {}
_LOCK = threading.Lock()
_STILL = re.compile(r"/[A-Za-z0-9_-]{1,160}\.(?:jpg|png)")


class EpisodeArtworkUnavailable(ValueError):
    """Constant public diagnostic; source locators and credentials stay private."""

    def __init__(self):
        super().__init__("Exact episode image unavailable")


def _require(condition):
    if not condition:
        raise EpisodeArtworkUnavailable()


def _json(value):
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError, RecursionError):
        raise EpisodeArtworkUnavailable() from None
    _require(isinstance(parsed, dict))
    return parsed


@dataclass(frozen=True)
class EpisodeIdentity:
    user_id: int
    media_type: str
    media_id: int
    season: int
    episode: int
    event_id: int | None = None
    show_key: str | None = None
    canonical_title: str | None = field(default=None, repr=False)
    saved_still: str | None = field(default=None, repr=False)


def anime_identity(db, user_id, event_id):
    row = db.execute(
        "SELECT e.*,w.anilist_id,w.season_number,m.title FROM watch_events e "
        "JOIN watch_entries w ON w.id=e.watch_entry_id AND w.user_id=e.user_id "
        "JOIN media_monitors m ON m.id=w.monitor_id AND m.user_id=w.user_id "
        "AND m.anilist_id=w.anilist_id AND COALESCE(m.season_number,0)=w.season_number "
        "WHERE e.id=? AND e.user_id=? AND e.disposition='accepted' "
        "AND e.kind IN ('progress','completed') AND e.source IN ('plex','apple_tv','anilist','manual') "
        "AND e.confidence IN ('exact','manual') AND e.reason!='completion_score_updated' "
        "AND lower(e.event_key) NOT LIKE '%bulk%' AND lower(e.event_key) NOT LIKE '%migration%'",
        (event_id, user_id),
    ).fetchone()
    _require(row is not None)
    season, episode = row["season_number"], row["episode"]
    _require(
        type(season) is int and 1 <= season <= 99 and type(episode) is int and 1 <= episode <= 5000
    )
    # No alias/numbering guess. Only an unambiguous canonical path binding from
    # the latest trusted curator snapshot can connect AniList to a Plex show.
    _require(
        not db.execute(
            "SELECT 1 FROM media_monitors WHERE title=? AND season_number=? AND anilist_id!=? LIMIT 1",
            (row["title"], season, row["anilist_id"]),
        ).fetchone()
    )
    candidates = db.execute(
        "SELECT * FROM plex_library_items WHERE snapshot_id=(SELECT id FROM plex_library_snapshots "
        "ORDER BY generated_at DESC,id DESC LIMIT 1) AND media_type='show' "
        "AND canonical_title=? AND season_number=? AND path_identity_complete=1",
        (row["title"], season),
    ).fetchall()
    _require(len(candidates) == 1)
    candidate = candidates[0]
    try:
        refs = json.loads(candidate["managed_paths_json"])
    except (ValueError, TypeError, RecursionError):
        raise EpisodeArtworkUnavailable() from None
    _require(refs == [{"canonical_title": row["title"], "season_number": season}])
    _require(bool(re.fullmatch(r"[1-9][0-9]{0,31}", candidate["rating_key"])))
    return EpisodeIdentity(
        user_id,
        "anime",
        row["anilist_id"],
        season,
        episode,
        event_id,
        candidate["rating_key"],
        row["title"],
    )


def tv_identity(db, user_id, media_id, season, episode):
    _require(
        type(media_id) is int
        and 0 < media_id <= 2147483647
        and type(season) is int
        and 0 <= season <= 200
        and type(episode) is int
        and 1 <= episode <= 1000
    )
    row = db.execute(
        "SELECT e.metadata_json FROM tv_episodes e JOIN tv_catalog c "
        "ON c.user_id=e.user_id AND c.tmdb_id=e.tmdb_id WHERE e.user_id=? AND e.tmdb_id=? "
        "AND e.season_number=? AND e.episode_number=? AND e.watched=1 AND e.watch_source IN ('manual','plex')",
        (user_id, media_id, season, episode),
    ).fetchone()
    _require(row is not None)
    metadata = _json(row["metadata_json"])
    _require(
        metadata.get("episode_number", episode) == episode
        and metadata.get("season_number", season) == season
    )
    still = metadata.get("still_url")
    if not isinstance(still, str) or not re.fullmatch(
        r"https://image\.tmdb\.org/t/p/(?:w[0-9]{2,4}|original)/[A-Za-z0-9_-]{1,160}\.(?:jpg|png)",
        still,
    ):
        still = None
    return EpisodeIdentity(user_id, "tv", media_id, season, episode, saved_still=still)


@dataclass(frozen=True)
class _Source:
    origin: str = field(repr=False)
    key: str = field(repr=False)
    service: str
    machine: str = field(default="", repr=False)


def _source(db, identity):
    service = "plex" if identity.media_type == "anime" else "seerr"
    _require(private_connections_enabled())
    if identity.media_type == "tv" and identity.saved_still:
        return _Source("https://image.tmdb.org", "", "tmdb")
    spec = _spec(service)
    url, key = _values(db, identity.user_id, spec)
    _require(
        bool(url and key)
        and _saved_health(db, identity.user_id, spec, url, key)["state"] == "connected"
    )
    machine = _read(db, identity.user_id, "plex_machine_id") if service == "plex" else ""
    if service == "plex":
        _require(bool(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", machine)))
    return _Source(url, key, service, machine)


class _Reader:
    def __init__(self, source, *, transport=None, resolver=None):
        self.source, self.transport, self.resolver = source, transport, resolver
        self.requests = 0

    async def read(self, path, *, image=False, public=False):
        self.requests += 1
        _require(
            self.requests <= 6
            and isinstance(path, str)
            and path.startswith("/")
            and not path.startswith("//")
            and not any(c in path for c in ("?", "#", "\\"))
            and not any(p in {".", ".."} for p in path.split("/"))
        )
        origin = "https://image.tmdb.org" if public else self.source.origin
        destination = await pin_destination(
            origin, transport=self.transport, resolver=self.resolver
        )
        if public and not (
            isinstance(self.transport, httpx.MockTransport) and self.resolver is None
        ):
            address = ipaddress.ip_address(httpx.URL(destination.base_url).host)
            _require(not blocked_address(address, allow_private=False))
        headers = {
            "Host": destination.host_header,
            "Accept": "image/*"
            if image
            else "application/json"
            if self.source.service == "seerr"
            else "application/xml",
        }
        if not public:
            headers["X-Plex-Token" if self.source.service == "plex" else "X-Api-Key"] = (
                self.source.key
            )
        limit = _IMAGE_LIMIT if image else _METADATA_LIMIT
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=httpx.Timeout(8, connect=3),
            follow_redirects=False,
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET",
                destination.base_url + path,
                headers=headers,
                extensions={"sni_hostname": destination.sni_hostname},
            ) as response:
                _require(response.status_code == 200)
                declared = response.headers.get("content-length", "")
                _require(not declared.isdigit() or int(declared) <= limit)
                if image:
                    _require(
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                        in {"image/jpeg", "image/png", "image/webp"}
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    _require(len(chunks) + len(chunk) <= limit)
                    chunks.extend(chunk)
        return bytes(chunks)

    async def xml(self, path):
        data = await self.read(path)
        _require(b"<!DOCTYPE" not in data.upper() and b"<!ENTITY" not in data.upper())
        try:
            return ElementTree.fromstring(data)
        except ElementTree.ParseError:
            raise EpisodeArtworkUnavailable() from None


def _complete(root, rows):
    total = root.get("totalSize", root.get("size", ""))
    _require(
        bool(re.fullmatch(r"[0-9]{1,4}", total))
        and int(total) == len(rows)
        and len(rows) <= 1000
        and root.get("offset", "0") == "0"
    )


def _managed_episode(path, identity):
    if not isinstance(path, str) or "\\" in path:
        return False
    if not PurePosixPath(path).is_absolute():
        return False
    parts = PurePosixPath(path).parts
    if (
        len(parts) < 4
        or ".." in parts
        or parts[-4] != "Anime"
        or parts[-3] != identity.canonical_title
        or parts[-2] != f"Season {identity.season:02d}"
    ):
        return False
    match = re.search(r"\bS([0-9]{2})E([0-9]{2,4})(?:-E([0-9]{2,4}))?\b", parts[-1])
    return bool(
        match
        and int(match[1]) == identity.season
        and int(match[2]) <= identity.episode <= int(match[3] or match[2])
    )


async def _plex_still(reader, identity):
    machine = await reader.xml("/identity")
    _require(machine.get("machineIdentifier") == reader.source.machine)
    root = await reader.xml(f"/library/metadata/{identity.show_key}/children")
    seasons = root.findall("Directory")
    _complete(root, seasons)
    matched = [
        r
        for r in seasons
        if r.get("type") == "season"
        and r.get("parentRatingKey") == identity.show_key
        and r.get("index") == str(identity.season)
    ]
    _require(len(matched) == 1)
    season_key = matched[0].get("ratingKey", "")
    _require(bool(re.fullmatch(r"[1-9][0-9]{0,31}", season_key)))
    root = await reader.xml(f"/library/metadata/{season_key}/children")
    episodes = root.findall("Video")
    _complete(root, episodes)
    matched = [
        r
        for r in episodes
        if r.get("type") == "episode"
        and r.get("grandparentRatingKey") == identity.show_key
        and r.get("parentRatingKey") == season_key
        and r.get("parentIndex") == str(identity.season)
        and r.get("index") == str(identity.episode)
    ]
    _require(len(matched) == 1)
    row = matched[0]
    key = row.get("ratingKey", "")
    thumb = row.get("thumb", "")
    _require(
        bool(re.fullmatch(r"[1-9][0-9]{0,31}", key))
        and bool(
            re.fullmatch(r"/library/metadata/" + re.escape(key) + r"/thumb/[0-9]{1,20}", thumb)
        )
    )
    parts = row.findall("./Media/Part")
    _require(bool(parts) and all(_managed_episode(part.get("file"), identity) for part in parts))
    return await reader.read(thumb, image=True)


async def _tv_still(reader, identity):
    if identity.saved_still:
        return await reader.read(
            identity.saved_still.removeprefix("https://image.tmdb.org"), image=True, public=True
        )
    show = _json(await reader.read(f"/api/v1/tv/{identity.media_id}"))
    _require(type(show.get("id")) is int and show["id"] == identity.media_id)
    season = _json(await reader.read(f"/api/v1/tv/{identity.media_id}/season/{identity.season}"))
    _require(type(season.get("seasonNumber")) is int and season["seasonNumber"] == identity.season)
    rows = season.get("episodes")
    _require(isinstance(rows, list) and len(rows) <= 1000)
    matches = [
        r
        for r in rows
        if isinstance(r, dict)
        and type(r.get("episodeNumber")) is int
        and r["episodeNumber"] == identity.episode
    ]
    _require(len(matches) == 1)
    row = matches[0]
    _require(type(row.get("seasonNumber")) is int and row["seasonNumber"] == identity.season)
    path = row.get("stillPath")
    _require(isinstance(path, str) and bool(_STILL.fullmatch(path)))
    return await reader.read("/t/p/w780" + path, image=True, public=True)


async def episode_artwork(db, identity, *, transport=None, resolver=None):
    """Only call after a fresh owned-row lookup, including before cache hits."""
    source = _source(db, identity)
    fingerprint = hashlib.sha256(
        repr(
            (
                source.origin,
                source.key,
                source.machine,
                identity,
                identity.canonical_title,
                identity.saved_still,
            )
        ).encode()
    ).hexdigest()
    cache_key = (identity.user_id, fingerprint)
    now = time.monotonic()
    loop = asyncio.get_running_loop()
    inflight_key = (loop, cache_key)
    with _LOCK:
        cached = _CACHE.get(cache_key)
        if cached and cached[0] > now:
            _CACHE.move_to_end(cache_key)
            _require(cached[1] is not None)
            return cached[1], "image/webp"
        task = _INFLIGHT.get(inflight_key)
        if task is None:
            _require(len(_INFLIGHT) < 4)
            task = loop.create_task(_resolve(source, identity, cache_key, transport, resolver))
            _INFLIGHT[inflight_key] = task
            task.add_done_callback(lambda done: _forget(inflight_key))
    return await asyncio.shield(task)


def _forget(key):
    with _LOCK:
        _INFLIGHT.pop(key, None)


async def _resolve(source, identity, cache_key, transport, resolver):
    payload = None
    try:
        async with asyncio.timeout(10):
            reader = _Reader(source, transport=transport, resolver=resolver)
            raw = await (
                _plex_still(reader, identity)
                if identity.media_type == "anime"
                else _tv_still(reader, identity)
            )
            payload, kind = await asyncio.to_thread(optimized_artwork, raw, kind="banner")
    except (
        EpisodeArtworkUnavailable,
        PosterUnavailable,
        OutboundError,
        httpx.HTTPError,
        TimeoutError,
        ValueError,
        TypeError,
        KeyError,
        OverflowError,
    ):
        raise EpisodeArtworkUnavailable() from None
    finally:
        with _LOCK:
            _CACHE[cache_key] = (time.monotonic() + (86400 if payload else 300), payload)
            _CACHE.move_to_end(cache_key)
            while len(_CACHE) > 64 or sum(len(v[1] or b"") for v in _CACHE.values()) > _CACHE_BYTES:
                _CACHE.popitem(last=False)
    return payload, kind
