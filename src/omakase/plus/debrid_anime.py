"""Owner-scoped playback of the exact Real-Debrid file for an anime episode."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from omakase.plus import households
from omakase.plus.debrid_library import (
    CloudSource,
    DebridLibraryError,
    _video,
    account_binding,
    account_token,
)
from omakase.plus.household_media import personal_legacy_visible
from omakase.plus.language_source import digest
from omakase.plus.media_store import verified_asset_coverage
from omakase.plus.media_sync import _inventory_fingerprint
from omakase.plus.providers.contracts import ProviderFile, ProviderItem
from omakase.plus.publication_episodes import approved_file_episodes

_TORRENT_ID = re.compile(r"[A-Za-z0-9]{3,64}")
_HASH = re.compile(r"(?:[A-F0-9]{40}|[A-F0-9]{64})")


@dataclass(frozen=True)
class AnimeAsset:
    id: int
    external_id: str
    torrent_hash: str
    inventory: str
    coverage: frozenset[int]
    numbering_override: str | None


@dataclass(frozen=True, repr=False)
class DebridTitleSource:
    _scope: object
    domain: str
    canonical_id: str
    season_number: int
    episode_number: int
    cloud: CloudSource
    source_identity: str
    locator: object = field(repr=False)

    @property
    def duration_ms(self):
        return round(self.cloud.duration * 1000)

    @property
    def media_version(self):
        return digest([self.source_identity, self.cloud.media_version])


def _anime_assets(db, scope, monitor_id):
    if scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id:
        return None, ()
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE user_id=? AND id=?", (scope.viewer_id, monitor_id)
    ).fetchone()
    if monitor is None:
        return None, ()
    season = monitor["season_number"] or 0
    identity = str(monitor["anilist_id"])
    if not personal_legacy_visible(db, scope, domain="anime", canonical_id=identity, season=season):
        return None, ()
    if db.execute(
        """SELECT 1 FROM library_items WHERE library_id=? AND domain='anime'
           AND canonical_id=? AND season_number=? AND state='removed'""",
        (scope.library_id, identity, season),
    ).fetchone():
        return None, ()
    removal = db.execute(
        "SELECT status FROM media_title_removals WHERE monitor_id=? ORDER BY id DESC LIMIT 1",
        (monitor_id,),
    ).fetchone()
    if removal and removal[0] in {"awaiting_plex", "plex_removed", "provider_failed", "complete"}:
        return None, ()
    assets = []
    rows = db.execute(
        """SELECT a.* FROM media_assets a LEFT JOIN media_providers p ON p.id=a.provider_id
           WHERE a.monitor_id=? AND a.status='downloaded' AND a.inventory_verified_at IS NOT NULL
             AND ((p.kind='realdebrid' AND p.user_id=? AND p.enabled=1)
                  OR (a.provider_id IS NULL AND a.rd_torrent_id<>''))
           ORDER BY CASE a.quality WHEN '2160p' THEN 0 WHEN '1080p' THEN 1 WHEN '720p' THEN 2 ELSE 3 END,
             a.inventory_verified_at DESC, a.id DESC LIMIT 128""",
        (monitor_id, scope.viewer_id),
    )
    for row in rows:
        external_id = row["provider_asset_id"] or row["rd_torrent_id"]
        torrent_hash = row["torrent_hash"].upper()
        if (
            not _TORRENT_ID.fullmatch(external_id)
            or not _HASH.fullmatch(torrent_hash)
            or not re.fullmatch(r"[a-f0-9]{64}", row["inventory_fingerprint"])
        ):
            continue
        try:
            coverage = frozenset(verified_asset_coverage(row))
        except ValueError:
            continue
        if coverage:
            assets.append(
                AnimeAsset(
                    row["id"],
                    external_id,
                    torrent_hash,
                    row["inventory_fingerprint"],
                    coverage,
                    row["numbering_override"],
                )
            )
    return monitor, tuple(assets)


def anime_episodes(db, scope, monitor_id, *, torrents=None):
    monitor, assets = _anime_assets(db, scope, monitor_id)
    if monitor is None:
        return set()
    if torrents is not None:
        assets = tuple(
            asset
            for asset in assets
            if (entry := torrents.get(asset.external_id))
            and entry.get("status") == "downloaded"
            and str(entry.get("hash", "")).upper() == asset.torrent_hash
        )
    if monitor["format"] == "MOVIE":
        return {0} if any(asset.coverage == {1} for asset in assets) else set()
    return {episode for asset in assets for episode in asset.coverage}


def _match_file(monitor, asset, inventory, episode):
    files, links, content_hash = inventory
    if content_hash.upper() != asset.torrent_hash or not links:
        raise DebridLibraryError("The Real-Debrid copy changed.", 409)
    selected = tuple(
        ProviderFile(str(number), data["path"], data["bytes"], True)
        for number, data in files.items()
        if number in links and isinstance(data.get("path"), str) and type(data.get("bytes")) is int
    )
    item = ProviderItem(
        asset.external_id, "Anime file", "downloaded", 100, 0, 0, selected, content_hash
    )
    if _inventory_fingerprint(item) != asset.inventory:
        raise DebridLibraryError("The selected Real-Debrid files changed.", 409)
    videos = [file for file in selected if file.bytes > 0 and _video(file.path)]
    matches = []
    for file in videos:
        name = PurePosixPath(file.path.replace("\\", "/")).name
        try:
            coverage = approved_file_episodes(
                name,
                media_format=monitor["format"],
                season=monitor["season_number"],
                expected=monitor["expected_episodes"],
                anilist_id=monitor["anilist_id"],
                numbering_override=asset.numbering_override,
                approved_coverage=asset.coverage,
                sole_file=len(videos) == 1,
            )
        except ValueError:
            continue
        if coverage == {episode or 1}:
            matches.append(int(file.id))
    if len(matches) != 1:
        raise DebridLibraryError("This episode needs one matching Real-Debrid file.", 409)
    return matches[0]


async def resolve_anime(db, scope, monitor_id, episode, library):
    token = account_token(db, scope.viewer_id)
    if not token:
        return None
    monitor, assets = _anime_assets(db, scope, monitor_id)
    if monitor is None or (episode == 0) != (monitor["format"] == "MOVIE"):
        return None
    candidates = [asset for asset in assets if (episode or 1) in asset.coverage]
    failure = None
    for asset in candidates[:3]:
        try:
            inventory = await library._torrent(token, asset.external_id)
            number = _match_file(monitor, asset, inventory, episode)
            cloud = await library.torrent_source(
                token, asset.external_id, number, inventory=inventory
            )
        except DebridLibraryError as error:
            if error.status in {403, 429, 451}:
                raise
            failure = error
            continue
        source = DebridTitleSource(
            scope,
            "anime" if episode else "anime-film",
            str(monitor["anilist_id"]),
            monitor["season_number"] or 0,
            episode,
            cloud,
            digest(["realdebrid", asset.id, asset.torrent_hash, asset.inventory, number]),
            (int(monitor_id), asset),
        )
        revalidate_anime(db, source)
        return source, monitor["title"]
    if failure:
        raise failure
    return None


def revalidate_anime(db, source):
    scope = source._scope
    households.revalidate_scope(db, scope)
    monitor_id, asset = source.locator
    monitor, assets = _anime_assets(db, scope, monitor_id)
    if (
        monitor is None
        or str(monitor["anilist_id"]) != source.canonical_id
        or (monitor["season_number"] or 0) != source.season_number
        or (monitor["format"] == "MOVIE") != (source.domain == "anime-film")
        or asset not in assets
        or account_binding(account_token(db, scope.viewer_id)) != source.cloud.account
    ):
        raise DebridLibraryError("The Real-Debrid copy is no longer available.", 404)
