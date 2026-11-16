"""Match named movies and numbered TV episodes to the viewer's cloud inventory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from itertools import islice, zip_longest
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
_TV_SCAN_SECONDS = 20


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


async def tv_ids(db, scope, library):
    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or not token:
        return set()
    inventory = await library.inventory(token)
    ids = set()
    for row in db.execute(
        "SELECT tmdb_id,metadata_json FROM tv_catalog WHERE user_id=? "
        "AND json_extract(metadata_json,'$.in_collection')=1",
        (scope.viewer_id,),
    ):
        metadata = json.loads(row[1])
        for season in metadata.get("seasons", []):
            title = catalog_title(db, scope, "tv", str(row[0]), season["season_number"])
            if title and next(_records(inventory, title), None):
                ids.add(row[0])
                break
    return ids


def _video_files(files, links):
    for number, file in files.items():
        name = _leaf(file.get("path"))
        if number in links and _video(name):
            yield number, name


def _inventory_signature(inventory):
    torrents, downloads = inventory
    parts = [
        (key, data.get("status"), data.get("hash"), data.get("filename"))
        for key, data in torrents.items()
    ]
    parts.extend((key, data.get("streamable")) for key, data in downloads.items())
    return hashlib.sha256(json.dumps(sorted(parts, key=repr)).encode()).hexdigest()


@dataclass(frozen=True)
class EpisodeAvailability:
    episodes: frozenset[tuple[int, int]] = frozenset()
    partial_seasons: tuple[int, ...] = ()


@dataclass
class _SeasonScan:
    expires: float
    signature: str
    title: CatalogTitle
    # Insertion order rotates retryable rows to the end of the pending queue.
    pending: dict
    matched: dict[str, frozenset[tuple[int, int]]] = field(default_factory=dict)


def _season_scan(cache, key, title, inventory, signature, now):
    previous = cache.get(key)
    if (
        previous is not None
        and previous.expires > now
        and previous.signature == signature
        and previous.title == title
    ):
        return previous
    cache.pop(key, None)
    while len(cache) >= _TV_CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    scan = _SeasonScan(now + _TV_CACHE_TTL, signature, title, dict(_records(inventory, title)))
    cache[key] = scan
    return scan


async def tv_episode_ids(db, scope, key, library, *, seasons):
    """Advance availability for the catalog page's seasons with a shared provider budget."""
    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or not token or not seasons:
        return EpisodeAvailability()
    binding = account_binding(token)
    inventory = await library.inventory(token)
    signature = _inventory_signature(inventory)
    if not hasattr(library, "tv_episode_cache"):
        library.tv_episode_cache = {}
        library.tv_episode_scan = asyncio.Lock()
    cache = library.tv_episode_cache
    result = set()
    partial = set(seasons)
    scans = {}

    async def fetch(season, scan, record, data):
        try:
            files, links, content_hash = await library._torrent(token, record)
        except DebridLibraryError as error:
            if error.status == 404:
                scan.pending.pop(record)
            elif error.status in {401, 403, 451}:
                return error
            else:
                scan.pending[record] = scan.pending.pop(record)
            return error if error.status == 429 else None
        if content_hash.upper() != str(data.get("hash") or "").upper():
            scan.pending[record] = scan.pending.pop(record)
            return None
        markers = frozenset(
            marker
            for _, name in _video_files(files, links)
            if (marker := _tv_marker(name, scan.title, from_pack=True))
            and marker[0] == season
            and marker[1]
        )
        if markers:
            scan.matched[record] = markers
        scan.pending.pop(record)
        return None

    try:
        async with asyncio.timeout(_TV_SCAN_SECONDS), library.tv_episode_scan:
            now = time.monotonic()
            for season in seasons:
                title = catalog_title(db, scope, "tv", key, season)
                if title is None:
                    partial.discard(season)
                    continue
                cache_key = (binding, scope.viewer_id, str(key), season)
                scans[season] = _season_scan(cache, cache_key, title, inventory, signature, now)
            # Round-robin across seasons so a busy season cannot starve the other
            # seasons on this page. Snapshot the jobs before mutating queues.
            pending = [
                [
                    (season, scan, record, data)
                    for record, data in islice(scan.pending.items(), _TV_JOB_LIMIT)
                ]
                for season, scan in scans.items()
            ]
            jobs = list(
                islice(
                    (job for group in zip_longest(*pending) for job in group if job is not None),
                    _TV_JOB_LIMIT,
                )
            )
            for index in range(0, len(jobs), _TV_FETCH_LIMIT):
                batch = jobs[index : index + _TV_FETCH_LIMIT]
                errors = await asyncio.gather(*(fetch(*job) for job in batch))
                if error := next((error for error in errors if error is not None), None):
                    if error.status != 429:
                        raise error
                    break
    except TimeoutError:
        pass
    for season, scan in scans.items():
        result.update(marker for markers in scan.matched.values() for marker in markers)
        if not scan.pending:
            partial.discard(season)
    return EpisodeAvailability(frozenset(result), tuple(sorted(partial)))


async def resolve_catalog(db, scope, domain, key, season, episode, library):
    title = catalog_title(db, scope, domain, key, season, episode)
    token = account_token(db, scope.viewer_id)
    if not title or not token:
        return None
    inventory = await library.inventory(token)
    failure = None
    records = _records(inventory, title)
    if domain == "tv":
        cache_key = (account_binding(token), scope.viewer_id, str(key), season)
        scan = getattr(library, "tv_episode_cache", {}).get(cache_key)
        verified = (
            {record for record, markers in scan.matched.items() if (season, episode) in markers}
            if scan is not None
            and scan.expires > time.monotonic()
            and scan.signature == _inventory_signature(inventory)
            else set()
        )
        records = sorted(
            records,
            key=lambda pair: (
                pair[0] not in verified,
                _tv_marker(pair[1].get("filename"), title) != (season, episode),
            ),
        )
    for index, (record, data) in enumerate(records):
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
