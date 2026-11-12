"""Match named movies and numbered TV episodes to the viewer's cloud inventory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath

from omakase.plus.debrid_anime import DebridTitleSource
from omakase.plus.debrid_library import DebridLibraryError, _video, account_binding, account_token
from omakase.plus.household_media import personal_legacy_visible
from omakase.plus.language_source import digest

_TOKEN = re.compile(r"[^\W_]+")
_EPISODE = re.compile(r"s([0-9]{1,3})e([0-9]{1,4})")
_SEASON = re.compile(r"s([0-9]{1,3})")
_TV_CACHE_TTL = 120
_TV_CACHE_LIMIT = 256
_TV_FETCH_LIMIT = 4
_TV_JOB_LIMIT = 48


def _tokens(name):
    return (
        tuple(_TOKEN.findall(unicodedata.normalize("NFKC", name).casefold()))
        if isinstance(name, str)
        else ()
    )


def _leaf(path):
    return PurePosixPath(path.replace("\\", "/")).name if isinstance(path, str) else ""


@dataclass(frozen=True)
class CatalogTitle:
    domain: str
    key: str
    title: str
    year: int | None
    season: int = 0
    episode: int = 0


def catalog_title(db, scope, domain, key, season=0, episode=0):
    if (
        scope.kind != "personal"
        or scope.viewer_id != scope.operational_owner_id
        or not personal_legacy_visible(db, scope, domain=domain, canonical_id=key)
    ):
        return None
    if db.execute(
        """SELECT 1 FROM library_items WHERE library_id=? AND domain=?
           AND canonical_id=? AND state='removed'""",
        (scope.library_id, domain, key),
    ).fetchone():
        return None
    if domain == "movie":
        row = db.execute(
            """SELECT c.title,c.year FROM movie_catalog c LEFT JOIN movie_requests q
               USING(user_id,imdb_id)
               WHERE c.user_id=? AND c.imdb_id=? AND c.identity_verified=1
               AND COALESCE(q.code,'')<>'copy_removed'""",
            (scope.viewer_id, key),
        ).fetchone()
        return CatalogTitle(domain, key, row["title"], row["year"]) if row and row["year"] else None
    row = db.execute(
        """SELECT c.metadata_json FROM tv_catalog c
           WHERE c.user_id=? AND c.tmdb_id=? AND json_extract(c.metadata_json,'$.in_collection')=1
           AND (?=0 OR EXISTS (
               SELECT 1 FROM tv_episodes e WHERE e.user_id=c.user_id AND e.tmdb_id=c.tmdb_id
                 AND e.season_number=? AND e.episode_number=?
           ))""",
        (scope.viewer_id, key, episode, season, episode),
    ).fetchone()
    metadata = json.loads(row[0]) if row else {}
    name = metadata.get("title")
    return CatalogTitle(domain, key, name, metadata.get("year"), season, episode) if name else None


def _tail(name, title):
    parts = _tokens(name)
    prefix = _tokens(title.title)
    if not prefix or parts[: len(prefix)] != prefix:
        return ()
    return parts[len(prefix) :]


def _movie_matches(name, title):
    tail = _tail(name, title)
    return bool(tail and tail[0] == str(title.year) and not {"sample", "trailer"} & set(tail))


def _tv_marker(name, title, *, from_pack=False):
    parts = _tail(name, title)
    if parts and title.year and parts[0] == str(title.year):
        parts = parts[1:]
    if not parts and from_pack:
        parts = _tokens(name)
    if not parts or {"sample", "trailer"} & set(parts):
        return None
    if match := _EPISODE.fullmatch(parts[0]):
        if len(parts) > 1 and re.fullmatch(r"e[0-9]{1,4}|[0-9]{1,4}", parts[1]):
            return None
        return int(match[1]), int(match[2])
    if match := _SEASON.fullmatch(parts[0]):
        return int(match[1]), 0
    if len(parts) > 1 and parts[0] == "season" and re.fullmatch(r"[0-9]{1,3}", parts[1]):
        return int(parts[1]), 0
    return None


def _matches(name, title):
    if title.domain == "movie":
        return _movie_matches(name, title)
    marker = _tv_marker(name, title)
    return bool(
        marker
        and marker[0] == title.season
        and (not title.episode or marker[1] in {0, title.episode})
    )


def _records(inventory, title):
    torrents, _ = inventory
    for key, data in torrents.items():
        if data.get("status") == "downloaded" and _matches(data.get("filename"), title):
            yield key, data


async def movie_ids(db, scope, library):
    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or not token:
        return set()
    inventory = await library.inventory(token)
    ids = set()
    for row in db.execute(
        "SELECT imdb_id FROM movie_catalog WHERE user_id=? AND identity_verified=1",
        (scope.viewer_id,),
    ):
        title = catalog_title(db, scope, "movie", row[0])
        if title and next(_records(inventory, title), None):
            ids.add(title.key)
    return ids


def _video_files(files, links):
    for number, file in files.items():
        name = _leaf(file.get("path"))
        if number in links and _video(name):
            yield number, name


def _inventory_signature(inventory):
    torrents, downloads = inventory
    parts = [f"{key}:{data.get('status')}:{data.get('hash')}" for key, data in torrents.items()]
    parts.extend(f"{key}:{data.get('streamable')}" for key, data in downloads.items())
    return hashlib.sha256("|".join(sorted(parts)).encode()).hexdigest()


async def tv_episode_ids(db, scope, key, library):
    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or not token:
        return set()
    binding = account_binding(token)
    inventory = await library.inventory(token)
    signature = _inventory_signature(inventory)
    cache = getattr(library, "tv_episode_cache", None)
    if cache is None:
        cache = {}
        library.tv_episode_cache = cache
    cache_key = (binding, scope.viewer_id, str(key))
    now = time.monotonic()
    cached = cache.get(cache_key)
    if cached is not None and cached[0] > now and cached[1] == signature:
        return set(cached[2])
    seasons = db.execute(
        "SELECT DISTINCT season_number FROM tv_episodes WHERE user_id=? AND tmdb_id=? ORDER BY season_number LIMIT 30",
        (scope.viewer_id, key),
    ).fetchall()
    jobs = {}
    for (season,) in seasons:
        title = catalog_title(db, scope, "tv", key, season)
        if title is None:
            continue
        for index, (record, data) in enumerate(_records(inventory, title)):
            if index >= 12:
                break
            jobs[(season, record)] = (season, title, record, data)
            if len(jobs) >= _TV_JOB_LIMIT:
                break
        if len(jobs) >= _TV_JOB_LIMIT:
            break
    slots = asyncio.Semaphore(_TV_FETCH_LIMIT)

    async def fetch(job):
        season, title, record, data = job
        async with slots:
            try:
                return job, await library._torrent(token, record)
            except DebridLibraryError as error:
                return job, error

    result = set()
    for (season, title, _record, data), info in await asyncio.gather(
        *(fetch(job) for job in jobs.values())
    ):
        if isinstance(info, DebridLibraryError):
            if info.status != 404:
                raise info
            continue
        files, links, content_hash = info
        if content_hash.upper() != str(data.get("hash") or "").upper():
            continue
        for _, name in _video_files(files, links):
            marker = _tv_marker(name, title, from_pack=True)
            if marker and marker[0] == season and marker[1]:
                result.add(marker)
    if len(cache) >= _TV_CACHE_LIMIT:
        for stale in [entry for entry, (expiry, _, _) in cache.items() if expiry <= now]:
            cache.pop(stale, None)
        while len(cache) >= _TV_CACHE_LIMIT:
            cache.pop(next(iter(cache)))
    cache[cache_key] = (now + _TV_CACHE_TTL, signature, frozenset(result))
    return result


async def resolve_catalog(db, scope, domain, key, season, episode, library):
    title = catalog_title(db, scope, domain, key, season, episode)
    token = account_token(db, scope.viewer_id)
    if not title or not token:
        return None
    inventory = await library.inventory(token)
    failure = None
    for index, (record, data) in enumerate(_records(inventory, title)):
        if index >= 8:
            break
        try:
            info = await library._torrent(token, record)
            files, links, content_hash = info
            if content_hash.upper() != str(data.get("hash") or "").upper():
                continue
            matches = [
                number
                for number, name in _video_files(files, links)
                if (
                    _movie_matches(name, title)
                    if domain == "movie"
                    else _tv_marker(name, title, from_pack=True) == (season, episode)
                )
            ]
            if len(matches) != 1:
                continue
            cloud = await library.torrent_source(token, record, matches[0], inventory=info)
        except DebridLibraryError as error:
            if error.status in {403, 429, 451}:
                raise
            failure = error
            continue
        source = DebridTitleSource(
            scope,
            domain,
            key,
            season,
            episode,
            cloud,
            digest(["realdebrid", domain, key, season, episode, cloud.media_version]),
            title,
        )
        revalidate_catalog(db, source)
        return source, title.title
    if failure:
        raise failure
    return None


def revalidate_catalog(db, source):
    scope = source._scope
    if (
        catalog_title(
            db,
            scope,
            source.domain,
            source.canonical_id,
            source.season_number,
            source.episode_number,
        )
        != source.locator
        or account_binding(account_token(db, scope.viewer_id)) != source.cloud.account
    ):
        raise DebridLibraryError("The Real-Debrid copy is no longer available.", 404)
