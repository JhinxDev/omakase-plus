"""Exact managed-anime Parts, resolved with the signed-in viewer's Plex identity.

No submitted URLs, filesystem paths or Plex keys are accepted. This module only
reads metadata; the authenticated gateway owns bytes and capability lifetimes.
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import quote

from omakase.plus import (
    connections,
    households,
    language_publication,
    manifest,
    quality_publication,
)
from omakase.plus.households import resolve_scope, revalidate_scope
from omakase.plus.language_source import digest
from omakase.plus.playback_proof import PlaybackProofError, _account, _get, _xml


class PlaybackSourceError(ValueError):
    """Safe constant diagnostics; never includes credentials or provider bodies."""

    def __init__(self, message, *, code="source_unavailable"):
        super().__init__(message)
        self.code = code


_fresh_write_connection = ContextVar("playback_fresh_write_connection", default=None)


@contextmanager
def fresh_source_transaction(db):
    """Own a fresh atomic source/binding write; callers must not await or commit.

    Only this synchronous scope permits a source check inside a transaction.
    Existing read/write transactions cannot be promoted into trusted evidence.
    """
    _require(not db.in_transaction, "Playback requires a fresh database snapshot.")
    with households._transaction(db):
        marker = _fresh_write_connection.set(db)
        try:
            yield
        finally:
            _fresh_write_connection.reset(marker)


def _require(
    value,
    message="The exact episode source is unavailable or changed.",
    *,
    code="source_unavailable",
):
    if not value:
        raise PlaybackSourceError(message, code=code)


def _numeric(value):
    _require(isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value))
    return value


def _container_index(value):
    if value is None:
        return None
    _require(
        isinstance(value, str) and re.fullmatch(r"[0-9]{1,3}", value) and 0 <= int(value) <= 127,
        "Plex container stream index is invalid.",
    )
    return int(value)


@dataclass(frozen=True, slots=True)
class PlexStream:
    stream_id: str
    kind: str
    codec: str
    language: str
    default: bool
    forced: bool
    selected: bool
    container_index: int | None = None


@dataclass(frozen=True, slots=True)
class ResolvedAnimeSource:
    actor_user_id: int
    library_id: str
    anilist_id: int | None
    season_number: int
    episode_number: int
    server_id: str
    account_id: str
    metadata_key: str
    part_key: str = field(repr=False)
    part_size: int
    duration_ms: int
    container: str
    video_codec: str
    audio_codec: str
    streams: tuple[PlexStream, ...]
    media_version: str
    source_identity: str
    base_url: str = field(repr=False)
    token: str = field(repr=False)
    _snapshot: tuple = field(repr=False)
    _scope: object = field(repr=False)
    _section_id: str = field(default="", repr=False)
    _publication_cache: dict = field(default_factory=dict, repr=False, compare=False)
    _skip_evidence: dict = field(default_factory=dict, repr=False, compare=False)
    domain: str = "anime"
    catalog_id: str = ""
    _file_path: str = field(default="", repr=False)
    _series_key: str = field(default="", repr=False)
    _single_plex_part: bool = field(default=False, repr=False)
    frame_rate: float | None = None

    @property
    def canonical_id(self):
        return self.catalog_id or str(self.anilist_id)


def _physical_version(identity, server, section, metadata, media, part):
    def physical_attributes(element):
        return {k: v for k, v in element.attrib.items() if k not in {"selected", "default"}}

    return digest(
        [
            identity,
            server,
            section,
            metadata,
            physical_attributes(media),
            physical_attributes(part),
            [physical_attributes(stream) for stream in part.findall("Stream")],
        ]
    )


def _trusted_skip_evidence(source, video):
    """Optional episode markers only when its sole physical cut is this source.

    Plex's legacy XML offsets are milliseconds. No marker analysis is requested;
    missing/unsupported/ambiguous data cannot change playback authorization.
    """
    from omakase.plus.web_playback_context import normalize_skip_segments

    medias = video.findall("Media")
    if (
        len(medias) != 1
        or video.get("type") != "episode"
        or video.get("ratingKey") != source.metadata_key
        or video.get("parentIndex") != str(source.season_number)
        or video.get("index") != str(source.episode_number)
        or video.get("duration") != str(source.duration_ms)
    ):
        return {}
    media = medias[0]
    parts = media.findall("Part")
    if len(parts) != 1 or parts[0].get("key") != source.part_key:
        return {}
    if (
        _physical_version(
            source.source_identity,
            source.server_id,
            source._section_id,
            source.metadata_key,
            media,
            parts[0],
        )
        != source.media_version
    ):
        return {}
    markers = video.findall("Marker")
    if len(markers) > 32:
        return {}
    rows = []
    for marker in markers:
        kind = marker.get("type")
        if kind not in {"intro", "credits"}:
            continue
        offsets = [marker.get(key, "") for key in ("startTimeOffset", "endTimeOffset")]
        if not all(re.fullmatch(r"[0-9]{1,9}", value) for value in offsets):
            return {}
        rows.append(
            {
                "kind": kind,
                "start_seconds": int(offsets[0]) / 1000,
                "end_seconds": int(offsets[1]) / 1000,
            }
        )
    evidence = {
        key: getattr(source, key)
        for key in (
            "source_identity",
            "metadata_key",
            "season_number",
            "episode_number",
            "media_version",
        )
    }
    evidence["segments"] = rows
    return evidence | {"segments": normalize_skip_segments(source, evidence)["segments"]}


def qualified_publication(db):
    row = db.execute("SELECT * FROM media_curator_receipt_latest WHERE id=1").fetchone()
    _require(row is not None)
    _require(len(row["receipt_json"]) <= 2 * 1024 * 1024)
    receipt = json.loads(row["receipt_json"])
    _require(receipt.get("schema_version") == "curator-receipt-v4")
    _require(
        receipt.get("mode") == "live"
        and receipt.get("topology_converged") is True
        and receipt.get("selection_applied") is True
        and receipt.get("outcome") in {"success", "degraded"}
    )
    manifest = quality_publication.validate_current_selection(db, receipt)
    return receipt, manifest


def publication_identity(item, asset_id, file, policy):
    return digest([item["id"], item["resource_id"], item["legacy_id"], asset_id, file, policy])


def _publication(db, scope, anilist_id, season_number, episode_number):
    film = season_number == 0 and episode_number == 0
    published_season, published_episode = (1, 1) if film else (season_number, episode_number)
    item = db.execute(
        "SELECT i.id,i.resource_id,r.legacy_id FROM library_items i JOIN library_resources r ON r.id=i.resource_id JOIN media_monitors m ON m.id=r.legacy_id WHERE i.library_id=? AND i.domain='anime' AND i.canonical_id=? AND i.season_number=? AND i.state='active' AND r.owner_user_id=? AND r.domain=i.domain AND r.canonical_id=i.canonical_id AND r.season_number=i.season_number AND m.user_id=r.owner_user_id AND m.anilist_id=CAST(r.canonical_id AS INTEGER) AND COALESCE(m.season_number,0)=r.season_number",
        (scope.library_id, str(anilist_id), season_number, scope.operational_owner_id),
    ).fetchone()
    _require(item is not None)
    try:
        receipt, manifest = qualified_publication(db)
        titles = [t for t in manifest["titles"] if t["anilist_id"] == anilist_id]
        _require(len(titles) == 1)
        _require((titles[0].get("format") == "MOVIE") == film)
        title = titles[0]["canonical_title"]
        _require(
            isinstance(title, str)
            and title not in {"", ".", ".."}
            and "/" not in title
            and "\\" not in title
            and not any(ord(c) < 32 for c in title)
        )
        files = [
            f
            for f in receipt["publication_files"]
            if f["selected"]
            and f["anilist_id"] == anilist_id
            and f["season_number"] == published_season
            and published_episode in f["episodes"]
        ]
        _require(len(files) == 1, "The episode requires one exact published source.")
        file = files[0]
        published_assets = [
            a for a in titles[0]["assets"] if a["torrent_hash"] == file["torrent_hash"]
        ]
        _require(len(published_assets) == 1)
        publication_policy = published_assets[0].get("language_contract")
        # Combined files need a qualified per-episode timeline before progress can
        # be safely mapped to an individual numbered episode.
        _require(
            file["episodes"] == [published_episode], "Combined episode files are not qualified."
        )
        assets = db.execute(
            "SELECT a.* FROM media_assets a JOIN media_monitors m ON m.id=a.monitor_id WHERE m.user_id=? AND m.anilist_id=? AND COALESCE(m.season_number,0)=? AND upper(a.torrent_hash)=? AND m.id=? AND a.status='downloaded' AND a.inventory_verified_at IS NOT NULL",
            (
                scope.operational_owner_id,
                anilist_id,
                season_number,
                file["torrent_hash"],
                item["legacy_id"],
            ),
        ).fetchall()
        _require(len(assets) == 1)
        asset = assets[0]
        _require(
            asset["inventory_fingerprint"] == file["inventory_generation"]
            and quality_publication.source_generation(db, asset["id"]) == file["source_generation"]
        )
        suffix = PurePosixPath(file["source"]).suffix.lower()
        _require(suffix in {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts"})
        if film:
            quality = published_assets[0]["quality"]
            torrent = file["torrent_hash"]
            _require(re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", quality))
            _require(re.fullmatch(r"(?:[A-F0-9]{40}|[A-F0-9]{64})", torrent))
            relative = f"Anime Movies/{title}/{title} - {quality} [{torrent[:8]}]{suffix}"
        else:
            relative = f"Anime/{title}/Season {season_number:02d}/{title} - S{season_number:02d}E{episode_number:02d}{suffix}"
        identity = publication_identity(item, asset["id"], file, publication_policy)
        return relative, file["stat"]["size"], identity
    except (KeyError, TypeError, ValueError, RecursionError):
        raise PlaybackSourceError(
            "The exact current publication could not be confirmed.", code="publication_unavailable"
        ) from None


def _publication_boundary(db):
    # Connection-local counters must never be compared across connections. Own
    # rolled-back writes still increase total_changes. Caller-held transactions
    # could retain an old WAL read snapshot, so they are never evidence of fresh
    # authorization (nor candidates for memoization).
    _require(not db.in_transaction, "Playback requires a fresh database snapshot.")
    return (
        db.total_changes,
        db.execute("PRAGMA main.data_version").fetchone()[0],
        db.execute("PRAGMA main.schema_version").fetchone()[0],
        db.execute("PRAGMA temp.schema_version").fetchone()[0],
        digest(sorted(os.environ.items())),
        quality_publication.QUALITY_PUBLICATION_QUALIFIED,
        quality_publication.CAPABILITY,
        quality_publication.SELECTION,
        language_publication.LANGUAGE_PUBLICATION_QUALIFIED,
        language_publication.CAPABILITY,
        quality_publication.validate_current_selection,
        quality_publication.source_generation,
        manifest.build_library_manifest,
        language_publication.export_contract,
        connections._values,
        connections._read,
    )


def _current_publication(db, scope, anilist_id, season_number, episode_number, cache):
    """One source-local immutable result; no clock-based authorization cache."""
    if db.in_transaction:
        cache.clear()
        _require(
            _fresh_write_connection.get() is db,
            "Playback requires a fresh database snapshot.",
        )
        # The wrapper acquired BEGIN IMMEDIATE from an idle connection. Rebuild
        # under that lock; never nest BEGIN, roll back its writes or publish a memo.
        return _publication(db, scope, anilist_id, season_number, episode_number)
    for _ in range(2):
        boundary = _publication_boundary(db)
        key = (scope, anilist_id, season_number, episode_number, boundary)
        if cache.get("db") is db and cache.get("key") == key:
            result = cache["result"]
        else:
            cache.clear()
            # The full library validation must see one coherent SQLite snapshot.
            # We own this read transaction and never hold it across an await.
            db.execute("BEGIN")
            try:
                result = _publication(db, scope, anilist_id, season_number, episode_number)
            finally:
                db.rollback()
        if _publication_boundary(db) == boundary:
            cache.update(db=db, key=key, result=result)
            return result
        # An unrelated WAL commit can move data_version during a read. Rebuild
        # from a fresh snapshot once, then fail closed under sustained churn.
        cache.clear()
    raise PlaybackSourceError(
        "Publication changed during validation.", code="publication_read_conflict"
    )


def _snapshot(db, scope, anilist_id, season_number, episode_number, publication_cache=None):
    revalidate_scope(db, scope)
    relative, size, identity = (
        _publication(db, scope, anilist_id, season_number, episode_number)
        if publication_cache is None
        else _current_publication(
            db, scope, anilist_id, season_number, episode_number, publication_cache
        )
    )
    base, owner_token = connections._values(
        db, scope.operational_owner_id, connections._spec("plex")
    )
    token = connections._read(db, scope.viewer_id, "plex_token")
    server = connections._read(db, scope.operational_owner_id, "plex_machine_id")
    root = os.getenv("OMAKASE_PLUS_PLEX_MANAGED_ROOT", "").rstrip("/")
    _require(
        root.startswith("/")
        and "\\" not in root
        and all(p not in {".", ".."} for p in root.split("/"))
        and not any(ord(c) < 32 for c in root),
        "The managed Plex root is not configured.",
    )
    _require(
        token
        and owner_token
        and (scope.viewer_id == scope.operational_owner_id or token != owner_token),
        "A separately authenticated personal Plex connection is required.",
    )
    _require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", server or ""))
    return (relative, size, identity, base, digest(token), digest(owner_token), server, root)


def revalidate_source(db, source):
    """Synchronous revocation/current-publication check for every gateway request."""
    try:
        _require(
            _snapshot(
                db,
                source._scope,
                source.anilist_id,
                source.season_number,
                source.episode_number,
                source._publication_cache,
            )
            == source._snapshot,
            code="source_snapshot_mismatch",
        )
        _require(
            db.execute(
                "SELECT 1 FROM viewer_playback_bindings WHERE external_account_id=? AND server_id=? AND actor_user_id!=?",
                (source.account_id, source.server_id, source.actor_user_id),
            ).fetchone()
            is None,
            "This Plex identity belongs to another viewer.",
            code="source_account_mismatch",
        )
    except PlaybackSourceError:
        raise
    except (ValueError, households.HouseholdError):
        raise PlaybackSourceError(
            "Playback access changed. Resolve the episode again.", code="source_scope_changed"
        ) from None
    return source


async def _section_pages(xml, section, media_type):
    """Bounded complete pages of the selected Plex media type."""
    videos = []
    total = None
    seen = set()
    while total is None or len(videos) < total:
        offset = len(videos)
        page = await xml(
            "/library/sections/"
            + section
            + f"/all?type={media_type}"
            + f"&X-Plex-Container-Start={offset}&X-Plex-Container-Size=40"
        )
        children = page.findall("Video")
        envelope_section = page.get("librarySectionID")
        _require(envelope_section in {None, section})
        _require(
            all(child.get("librarySectionID", envelope_section) == section for child in children),
            "Plex episode section identity could not be confirmed.",
        )
        size = page.get("size", "")
        count = page.get("totalSize", "")
        start = page.get("offset", "0")
        _require(size.isdigit() and count.isdigit() and start.isdigit())
        _require(
            int(start) == offset
            and int(size) == len(children)
            and len(children) <= 40
            and 0 <= int(count) <= 1000
        )
        if total is None:
            total = int(count)
        _require(total == int(count) and offset + len(children) <= total)
        _require(bool(children) or total == 0, "The bounded Plex listing is incomplete.")
        for child in children:
            key = _numeric(child.get("ratingKey", ""))
            _require(key not in seen, "The bounded Plex listing is contradictory.")
            seen.add(key)
        videos.extend(children)
    return videos


def _resolved_part(
    video,
    media,
    part,
    *,
    scope,
    anilist_id,
    season_number,
    episode_number,
    account,
    section,
    base,
    token,
    server,
    identity,
    snapshot,
    domain="anime",
    catalog_id="",
    series_key="",
):
    size_text = part.get("size", "")
    _require(re.fullmatch(r"[1-9][0-9]{0,12}", size_text))
    size = int(size_text)
    file_path = part.get("file", "")
    _require(0 < len(file_path) <= 4096 and not any(ord(char) < 32 for char in file_path))
    metadata = _numeric(video.get("ratingKey", ""))
    part_id = _numeric(part.get("id", ""))
    _numeric(media.get("id", ""))
    part_key = part.get("key", "")
    _require(
        re.fullmatch(
            "/library/parts/" + part_id + r"/(?:[A-Za-z0-9_~.-]+/){0,2}[A-Za-z0-9_~.-]+",
            part_key,
        )
        and all(p not in {".", ".."} for p in part_key.split("/"))
    )
    duration = part.get("duration") or video.get("duration", "")
    _require(duration.isdigit() and 0 < int(duration) <= 86400000)
    raw_streams = part.findall("Stream")
    _require(len(raw_streams) <= 128)
    streams = tuple(
        PlexStream(
            _numeric(s.get("id", "")),
            {"1": "video", "2": "audio", "3": "subtitle"}.get(s.get("streamType"), "other"),
            s.get("codec", ""),
            s.get("languageCode", "und"),
            s.get("default") == "1",
            s.get("forced") == "1",
            s.get("selected") == "1",
            _container_index(s.get("index")),
        )
        for s in raw_streams
    )
    _require(len({s.stream_id for s in streams}) == len(streams))
    indices = [s.container_index for s in streams if s.container_index is not None]
    _require(len(indices) == len(set(indices)), "Plex container stream indices are ambiguous.")
    version = _physical_version(identity, server, section, metadata, media, part)
    video_stream = next((s for s in raw_streams if s.get("streamType") == "1"), None)
    try:
        frame_rate = float(video_stream.get("frameRate", "0")) if video_stream is not None else 0
    except ValueError:
        frame_rate = 0
    return ResolvedAnimeSource(
        scope.viewer_id,
        scope.library_id,
        anilist_id,
        season_number,
        episode_number,
        server,
        account,
        metadata,
        part_key,
        size,
        int(duration),
        media.get("container", ""),
        media.get("videoCodec", ""),
        media.get("audioCodec", ""),
        streams,
        version,
        identity,
        base,
        token,
        snapshot,
        scope,
        section,
        domain=domain,
        catalog_id=catalog_id,
        _file_path=file_path,
        _series_key=series_key,
        _single_plex_part=len(video.findall("Media")) == 1 and len(media.findall("Part")) == 1,
        frame_rate=frame_rate if 0 < frame_rate <= 240 else None,
    )


def _anime_item_matches(video, season, episode, film):
    if film:
        return video.get("type") == "movie"
    return (
        video.get("type") == "episode"
        and video.get("parentIndex") == str(season)
        and video.get("index") == str(episode)
    )


async def resolve_anime_source(
    db,
    *,
    actor_user_id,
    library_id,
    anilist_id,
    season_number,
    episode_number,
    media_version=None,
    transport=None,
):
    """Resolve one immutable Part. Explicit versions are opaque SHA-256 values."""
    film = (
        type(season_number) is int
        and type(episode_number) is int
        and season_number == episode_number == 0
    )
    for value, maximum in (
        (actor_user_id, 2**63 - 1),
        (anilist_id, 2147483647),
    ):
        _require(type(value) is int and 0 < value <= maximum)
    _require(
        film
        or (
            type(season_number) is int
            and 0 < season_number <= 99
            and type(episode_number) is int
            and 0 < episode_number <= 5000
        )
    )
    _require(
        media_version is None
        or (isinstance(media_version, str) and re.fullmatch("[0-9a-f]{64}", media_version))
    )
    try:
        scope = resolve_scope(db, viewer_id=actor_user_id, library_id=library_id)
        initial = _snapshot(db, scope, anilist_id, season_number, episode_number)
        relative, size, identity, base, _, _, server, root_path = initial
        token = connections._read(db, actor_user_id, "plex_token")

        def current():
            _require(_snapshot(db, scope, anilist_id, season_number, episode_number) == initial)

        async def xml(path):
            result = _xml(await _get(base, path, token, transport=transport))
            current()
            return result

        account = await _account(token, transport)
        current()
        if actor_user_id != scope.operational_owner_id:
            owner_account = await _account(
                connections._read(db, scope.operational_owner_id, "plex_token"), transport
            )
            current()
            _require(
                account != owner_account,
                "The owner's Plex identity cannot represent another viewer.",
            )
        _require(
            db.execute(
                "SELECT 1 FROM viewer_playback_bindings WHERE external_account_id=? AND server_id=? AND actor_user_id!=?",
                (account, server, actor_user_id),
            ).fetchone()
            is None
        )
        _require((await xml("/identity")).get("machineIdentifier") == server)
        sections = await xml("/library/sections")
        name = "Anime Movies" if film else "Anime"
        matching = [
            s
            for s in sections.findall("Directory")
            if s.get("type") == ("movie" if film else "show")
            and s.get("title") == name
            and any(loc.get("path") == root_path + "/" + name for loc in s.findall("Location"))
        ]
        _require(
            len(matching) == 1,
            "This personal Plex connection cannot access the managed anime section.",
        )
        section = _numeric(matching[0].get("key", ""))
        leaves = await _section_pages(xml, section, 1 if film else 4)
        candidates = []
        for video in leaves:
            if not (
                _anime_item_matches(video, season_number, episode_number, film)
                and video.get("librarySectionID", section) == section
            ):
                continue
            for media in video.findall("Media"):
                parts = media.findall("Part")
                for part in parts:
                    if part.get("file") != root_path + "/" + relative:
                        continue
                    _require(len(parts) == 1 and part.get("size") == str(size))
                    candidates.append((video, media, part))
        _require(
            0 < len(candidates) <= 16, "The exact managed episode is not available to this viewer."
        )
        hydrated = []
        for listed_video, listed_media, listed_part in candidates:
            metadata = _numeric(listed_video.get("ratingKey", ""))
            detail = await xml("/library/metadata/" + metadata + "?includeMarkers=1")
            videos = detail.findall("Video")
            _require(len(videos) == 1 and detail.get("librarySectionID") in {None, section})
            video = videos[0]
            _require(video.get("librarySectionID", detail.get("librarySectionID")) == section)
            _require(
                all(
                    video.get(key) == listed_video.get(key)
                    for key in (
                        "ratingKey",
                        "type",
                        "parentIndex",
                        "index",
                        "duration",
                        "grandparentRatingKey",
                    )
                )
            )
            medias = [m for m in video.findall("Media") if m.get("id") == listed_media.get("id")]
            _require(len(medias) == 1)
            media = medias[0]
            parts = media.findall("Part")
            _require(len(parts) == 1)
            part = parts[0]
            _require(
                all(part.get(key) == listed_part.get(key) for key in ("id", "key", "file", "size")),
                "Plex replaced the exact Part during source resolution.",
            )
            hydrated.append((video, media, part))
        resolved = []
        for video, media, part in hydrated:
            resolved.append(
                _resolved_part(
                    video,
                    media,
                    part,
                    scope=scope,
                    anilist_id=anilist_id,
                    season_number=season_number,
                    episode_number=episode_number,
                    account=account,
                    section=section,
                    base=base,
                    token=token,
                    server=server,
                    identity=identity,
                    snapshot=initial,
                    domain="anime-film" if film else "anime",
                )
            )
            resolved[-1]._skip_evidence.update(_trusted_skip_evidence(resolved[-1], video))
        choices = [r for r in resolved if media_version is None or r.media_version == media_version]
        _require(len(choices) == 1, "Choose one exact qualified media version.")
        result = choices[0]
        return revalidate_source(db, result)
    except PlaybackSourceError:
        raise
    except (PlaybackProofError, ValueError, KeyError, TypeError):
        raise PlaybackSourceError(
            "Personal playback access or exact source could not be confirmed."
        ) from None


async def revalidate_remote_source(db, source, transport=None):
    """Confirm current personal Plex entitlement before exposing cached bytes.

    Exactly two bounded read-only LAN metadata requests; no plex.tv account
    lookup, watch updates, preference changes or media jobs are performed.
    """
    source._skip_evidence.clear()
    try:
        revalidate_source(db, source)
        section = _numeric(source._section_id)
        metadata = _numeric(source.metadata_key)

        async def xml(path):
            revalidate_source(db, source)
            body = await _get(source.base_url, path, source.token, transport=transport)
            revalidate_source(db, source)
            return _xml(body)

        identity = await xml("/identity")
        _require(identity.get("machineIdentifier") == source.server_id)
        detail = await xml("/library/metadata/" + metadata + "?includeMarkers=1")
        videos = detail.findall("Video")
        _require(len(videos) == 1 and detail.get("librarySectionID") in {None, section})
        video = videos[0]
        _require(
            video.get("librarySectionID", detail.get("librarySectionID")) == section
            and video.get("ratingKey") == metadata
            and _anime_item_matches(
                video,
                source.season_number,
                source.episode_number,
                getattr(source, "domain", "anime") == "anime-film",
            )
        )
        expected_path = source._snapshot[7] + "/" + source._snapshot[0]
        matches = []
        for media in video.findall("Media"):
            parts = media.findall("Part")
            for part in parts:
                if part.get("key") != source.part_key:
                    continue
                _require(
                    len(parts) == 1
                    and part.get("file") == expected_path
                    and part.get("size") == str(source.part_size)
                    and (part.get("duration") or video.get("duration")) == str(source.duration_ms)
                )
                matches.append(
                    _physical_version(
                        source.source_identity, source.server_id, section, metadata, media, part
                    )
                )
        _require(
            matches == [source.media_version],
            "Personal Plex access or the exact physical source changed.",
        )
        revalidate_source(db, source)
        source._skip_evidence.update(_trusted_skip_evidence(source, video))
        return source
    except (PlaybackProofError, ValueError, KeyError, TypeError, IndexError):
        raise PlaybackSourceError(
            "Current personal Plex playback access could not be confirmed."
        ) from None


def public_projection(source):
    """Safe UI metadata only; never serialize the server-only dataclass."""
    return {
        "anilist_id": source.anilist_id,
        "domain": getattr(source, "domain", "anime"),
        "canonical_id": getattr(source, "canonical_id", str(source.anilist_id)),
        "season_number": source.season_number,
        "episode_number": source.episode_number,
        "media_version": source.media_version,
        "duration_ms": source.duration_ms,
        "container": source.container,
        "video_codec": source.video_codec,
        "frame_rate": getattr(source, "frame_rate", None),
        "audio_codec": source.audio_codec,
        "streams": [
            {
                "id": s.stream_id,
                "kind": s.kind,
                "codec": s.codec,
                "language": s.language,
                "default": s.default,
                "forced": s.forced,
            }
            for s in source.streams
        ],
        "plex_url": "https://app.plex.tv/desktop/#!/server/"
        + quote(source.server_id, safe="")
        + "/details?key="
        + quote("/library/metadata/" + source.metadata_key, safe=""),
    }
