"""Match named movies and numbered TV episodes to the viewer's cloud inventory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass, field, replace
from itertools import islice, zip_longest
from pathlib import PurePosixPath

from omakase.plus.btih import normalize_btih
from omakase.plus.catalog import quality_from_title
from omakase.plus.debrid_anime import DebridTitleSource
from omakase.plus.debrid_library import (
    DebridLibraryError,
    _size,
    _video,
    account_binding,
    account_token,
)
from omakase.plus.episode_quality import QUALITY_RANK
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


def published_name(name):
    """A provider label or leaf filename, never a path or an endpoint."""
    if (
        isinstance(name, str)
        and 0 < len(name) <= 255
        and name not in {".", ".."}
        and _leaf(name) == name
        and not any(ord(char) < 32 for char in name)
    ):
        return name
    return ""


@dataclass(frozen=True)
class PublishedEpisode:
    infohash: str
    folder: str
    filename: str

    def matches(self, torrent):
        return (
            torrent.get("status") == "downloaded"
            and torrent.get("filename") == self.folder
            and normalize_btih(torrent.get("hash")) == self.infohash
        )

    def file_id(self, files, links, title):
        marker = _tv_marker(self.filename, title, from_pack=True)
        if marker and marker != (title.season, title.episode):
            return None
        selected = [number for number, name in _video_files(files, links) if name == self.filename]
        if len(selected) == 1 and _size(files[selected[0]].get("bytes")):
            return selected[0]
        return None


def _published_episodes(db, scope, key, binding):
    if db is None or scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id:
        return {}
    row = db.execute(
        "SELECT snapshot_json FROM tv_provider_requests WHERE user_id=? AND title_id=?",
        (scope.viewer_id, key),
    ).fetchone()
    publication = (json.loads(row[0]).get("published_files") or {}) if row and row[0] else {}
    if not binding or publication.get("binding") != binding:
        return {}
    return {
        (season, episode): PublishedEpisode(infohash, folder, filename)
        for season, episode, infohash, folder, filename in publication.get("episodes", ())
    }


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
           WHERE c.user_id=? AND c.tmdb_id=? AND (
               json_extract(c.metadata_json,'$.in_collection')=1 OR EXISTS (
                   SELECT 1 FROM for_you_requests q WHERE q.user_id=c.user_id
                     AND q.title_id=c.tmdb_id AND q.kind='tv'
               )
           )
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


def _records(inventory, title, published=()):
    torrents, _ = inventory
    for key, data in torrents.items():
        if data.get("status") == "downloaded" and (
            _matches(data.get("filename"), title) or any(hint.matches(data) for hint in published)
        ):
            yield key, data


async def movie_ids(db, scope, library):
    from omakase.plus import households as core

    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id or not token:
        return set()
    core.revalidate_scope(db, scope)
    inventory = await library.inventory(token)
    rows = db.execute(
        """SELECT c.imdb_id, c.title, c.year
           FROM movie_catalog c
           LEFT JOIN movie_requests q USING(user_id, imdb_id)
           WHERE c.user_id=? AND c.identity_verified=1
             AND COALESCE(q.code, '')<>'copy_removed'
             AND NOT EXISTS (
                 SELECT 1 FROM library_items li
                 WHERE li.library_id=? AND li.domain='movie'
                   AND li.canonical_id=c.imdb_id AND li.state='removed'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM library_resources r
                 JOIN library_items i ON i.resource_id=r.id
                 WHERE r.owner_user_id=? AND r.domain='movie'
                   AND r.canonical_id=c.imdb_id AND r.season_number=0
                   AND i.state='active' AND i.library_id!=?
                   AND NOT EXISTS (
                       SELECT 1 FROM library_items own
                       WHERE own.library_id=? AND own.state='active' AND own.resource_id=r.id
                   )
             )""",
        (
            scope.viewer_id,
            scope.library_id,
            scope.operational_owner_id,
            scope.library_id,
            scope.library_id,
        ),
    ).fetchall()
    ids = set()
    for row in rows:
        if row["year"]:
            title = CatalogTitle("movie", row["imdb_id"], row["title"], row["year"])
            if next(_records(inventory, title), None):
                ids.add(title.key)
    return ids


async def tv_ids(db, scope, library):
    from omakase.plus import households as core

    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id or not token:
        return set()
    core.revalidate_scope(db, scope)
    inventory = await library.inventory(token)
    rows = db.execute(
        """SELECT c.tmdb_id, c.metadata_json
           FROM tv_catalog c
           WHERE c.user_id=? AND (
               json_extract(c.metadata_json, '$.in_collection')=1 OR EXISTS (
                   SELECT 1 FROM for_you_requests q WHERE q.user_id=c.user_id
                     AND q.title_id=c.tmdb_id AND q.kind='tv'
               )
           )
           AND NOT EXISTS (
               SELECT 1 FROM library_items li
               WHERE li.library_id=? AND li.domain='tv'
                 AND li.canonical_id=CAST(c.tmdb_id AS TEXT) AND li.state='removed'
           )
           AND NOT EXISTS (
               SELECT 1 FROM library_resources r
               JOIN library_items i ON i.resource_id=r.id
               WHERE r.owner_user_id=? AND r.domain='tv'
                 AND r.canonical_id=CAST(c.tmdb_id AS TEXT) AND r.season_number=0
                 AND i.state='active' AND i.library_id!=?
                 AND NOT EXISTS (
                     SELECT 1 FROM library_items own
                     WHERE own.library_id=? AND own.state='active' AND own.resource_id=r.id
                 )
           )""",
        (
            scope.viewer_id,
            scope.library_id,
            scope.operational_owner_id,
            scope.library_id,
            scope.library_id,
        ),
    ).fetchall()
    ids = set()
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        name = metadata.get("title")
        if not name:
            continue
        year = metadata.get("year")
        for season in metadata.get("seasons", []):
            title = CatalogTitle("tv", str(row["tmdb_id"]), name, year, season["season_number"])
            if next(_records(inventory, title), None):
                ids.add(row["tmdb_id"])
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
    published: dict[int, PublishedEpisode]
    # Insertion order rotates retryable rows to the end of the pending queue.
    pending: dict
    matched: dict[str, frozenset[tuple[int, int]]] = field(default_factory=dict)


def _season_scan(cache, key, title, inventory, signature, now, published):
    previous = cache.get(key)
    if (
        previous is not None
        and previous.expires > now
        and previous.signature == signature
        and previous.title == title
        and previous.published == published
    ):
        return previous
    cache.pop(key, None)
    while len(cache) >= _TV_CACHE_LIMIT:
        cache.pop(next(iter(cache)))
    scan = _SeasonScan(
        now + _TV_CACHE_TTL,
        signature,
        title,
        published,
        dict(_records(inventory, title, published.values())),
    )
    cache[key] = scan
    return scan


async def tv_episode_ids(
    db, scope, key, library, *, seasons, timeout=_TV_SCAN_SECONDS, published=None
):
    """Return confirmed episodes and unfinished seasons within a caller's time budget."""
    token = account_token(db, scope.viewer_id)
    if scope.kind != "personal" or not token or not seasons:
        return EpisodeAvailability()
    binding = account_binding(token)
    if published is None:
        published = _published_episodes(db, scope, key, binding)
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        async with asyncio.timeout_at(deadline):
            inventory = await library.inventory(token)
    except TimeoutError:
        return EpisodeAvailability(partial_seasons=tuple(sorted(set(seasons))))
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
            if error.status in {404, 451}:
                scan.pending.pop(record)
            elif error.status in {401, 403}:
                return error
            else:
                scan.pending[record] = scan.pending.pop(record)
            return error if error.status == 429 else None
        if content_hash.upper() != str(data.get("hash") or "").upper():
            scan.pending[record] = scan.pending.pop(record)
            return None
        markers = set()
        if _matches(data.get("filename"), scan.title):
            markers.update(
                marker
                for _, name in _video_files(files, links)
                if (marker := _tv_marker(name, scan.title, from_pack=True))
                and marker[0] == season
                and marker[1]
            )
        for episode, hint in scan.published.items():
            if hint.matches(data) and hint.file_id(
                files, links, replace(scan.title, episode=episode)
            ):
                markers.add((season, episode))
        if markers:
            scan.matched[record] = frozenset(markers)
        scan.pending.pop(record)
        return None

    try:
        async with asyncio.timeout_at(deadline), library.tv_episode_scan:
            now = time.monotonic()
            for season in seasons:
                title = catalog_title(db, scope, "tv", key, season)
                if title is None:
                    partial.discard(season)
                    continue
                cache_key = (binding, scope.viewer_id, str(key), season)
                season_files = {ep: hint for (s, ep), hint in published.items() if s == season}
                scans[season] = _season_scan(
                    cache, cache_key, title, inventory, signature, now, season_files
                )
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


async def resolve_catalog(
    db, scope, domain, key, season, episode, library, *, need_transcode: bool = True
):
    title = catalog_title(db, scope, domain, key, season, episode)
    token = account_token(db, scope.viewer_id)
    if not title or not token:
        return None
    inventory = await library.inventory(token)
    failure = None
    hint = (
        _published_episodes(db, scope, key, account_binding(token)).get((season, episode))
        if domain == "tv"
        else None
    )
    records = _records(inventory, title, (hint,) if hint else ())
    if domain == "movie":
        # Release labels order verification. The selected copy still needs to resolve.
        records = sorted(
            records,
            key=lambda pair: QUALITY_RANK.get(
                quality_from_title(" ".join(_tail(pair[1].get("filename"), title))), 0
            ),
            reverse=True,
        )
    elif domain == "tv":
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
                not (hint and hint.matches(pair[1])),
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
            matches = (
                [
                    number
                    for number, name in _video_files(files, links)
                    if (
                        _movie_matches(name, title)
                        if domain == "movie"
                        else _tv_marker(name, title, from_pack=True) == (season, episode)
                    )
                ]
                if _matches(data.get("filename"), title)
                else []
            )
            locator = title
            if len(matches) == 1:
                file_id = matches[0]
            elif hint and hint.matches(data) and (file_id := hint.file_id(files, links, title)):
                locator = (title, hint)
            else:
                continue
            source_kwargs: dict[str, object] = {"inventory": info}
            if not need_transcode:
                source_kwargs["need_transcode"] = False
            cloud = await library.torrent_source(token, record, file_id, **source_kwargs)
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
            locator,
        )
        revalidate_catalog(db, source)
        return source, title.title
    if failure:
        raise failure
    return None


def revalidate_catalog(db, source):
    scope = source._scope
    title = catalog_title(
        db, scope, source.domain, source.canonical_id, source.season_number, source.episode_number
    )
    locator = source.locator
    binding = account_binding(account_token(db, scope.viewer_id))
    if isinstance(locator, tuple):
        published = _published_episodes(db, scope, source.canonical_id, binding)
        hint = published.get((source.season_number, source.episode_number))
        locator = locator[0] if hint == locator[1] else None
    if title is None or title != locator or binding != source.cloud.account:
        raise DebridLibraryError("The Real-Debrid copy is no longer available.", 404)
