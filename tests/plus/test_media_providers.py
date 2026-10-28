"""Provider-neutral contract and Real-Debrid adapter tests."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_store import (
    ensure_media_provider,
    record_media_file_probe,
    replace_subtitle_tracks,
    update_provider_health,
    upsert_asset,
    upsert_monitor,
    upsert_plex_target,
)
from omakase.plus.media_sync import reconcile_candidates
from omakase.plus.media_types import MediaCandidate
from omakase.plus.providers.contracts import (
    AcquisitionProvider,
    ProviderBlock,
    ProviderCapabilities,
    ProviderFile,
    ProviderHealth,
    ProviderItem,
)
from omakase.plus.providers.realdebrid import RealDebridProvider
from omakase.plus.realdebrid import RDFile, RDTorrent, RealDebridProviderBlock


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def user_id(db):
    return db.execute(
        "INSERT INTO users (email, password_hash) VALUES ('provider@example.com', 'hash')"
    ).lastrowid


def test_provider_contract_values_are_immutable_and_normalized():
    file = ProviderFile(id="7", path="/Show/Episode 01.mkv", bytes=1024, selected=True)
    item = ProviderItem(
        external_id="asset-1",
        name="Show",
        status="downloaded",
        progress=100,
        bytes_total=1024,
        bytes_done=1024,
        files=(file,),
        content_hash="abcdef",
        storage_locator="Show",
    )

    assert item.content_hash == "ABCDEF"
    assert item.files == (file,)


def test_provider_contract_refuses_invalid_progress():
    with pytest.raises(ValueError, match="progress"):
        ProviderItem(
            external_id="asset-1",
            name="Show",
            status="downloading",
            progress=101,
            bytes_total=1,
            bytes_done=0,
        )


def test_provider_store_round_trip_is_secret_free(db, user_id):
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="local_import",
        label="Local library",
        config={"import_roots": ["/imports"], "secret_name": "local_import_token"},
        capabilities={"adoption": True, "local_storage": True},
    )
    same_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="local_import",
        label="Local library",
        enabled=False,
        config={"import_roots": ["/imports"]},
        capabilities={"adoption": True, "local_storage": True},
    )
    update_provider_health(
        db,
        provider_id=provider_id,
        status="healthy",
        reason_code="READY",
        detail="Import root is readable",
        checked_at="2026-07-26T05:00:00Z",
    )

    row = db.execute("SELECT * FROM media_providers WHERE id = ?", (provider_id,)).fetchone()
    assert same_id == provider_id
    assert row["kind"] == "local_import"
    assert row["enabled"] == 0
    assert row["config_json"] == '{"import_roots":["/imports"]}'
    assert row["health_status"] == "healthy"


def test_generic_asset_probe_subtitles_and_plex_target(db, user_id):
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=1,
        title="Fixture",
        season_number=1,
    )
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="local_import",
        label="Local library",
        config={"import_roots": ["/imports"]},
        capabilities={"adoption": True, "local_storage": True},
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="fixture-hash",
        source_title="Fixture S01E01",
        provider_id=provider_id,
        provider_asset_id="fixture/episode-01",
        storage_backend="local",
        storage_locator="Fixture/Season 01/Episode 01.mkv",
        usable_coverage=(1,),
        status="downloaded",
    )
    probe_id = record_media_file_probe(
        db,
        asset_id=asset_id,
        file_locator="Fixture/Season 01/Episode 01.mkv",
        inventory_fingerprint="a" * 64,
        byte_range_receipt={"first": 206, "last": 206},
        video_streams=[{"codec": "hevc"}],
        audio_streams=[{"language": "jpn", "codec": "aac"}],
        outcome="usable",
        detail="",
        verified_at="2026-07-26T05:00:00Z",
    )
    replace_subtitle_tracks(
        db,
        probe_id=probe_id,
        tracks=[
            {
                "stream_index": 2,
                "language": "eng",
                "codec": "ass",
                "switchable": True,
                "default_track": True,
                "forced": False,
                "source_kind": "embedded",
                "sync_policy": "exact_release",
            },
            {
                "stream_index": 3,
                "language": "fra",
                "codec": "ass",
                "switchable": True,
                "default_track": False,
                "forced": False,
                "source_kind": "embedded",
                "sync_policy": "exact_release",
            },
        ],
    )
    target_id = upsert_plex_target(
        db,
        user_id=user_id,
        label="Living room",
        base_url="http://plex:32400",
        token_secret_name="plex_token_living_room",
        library_map={"anime": "Anime", "anime_movies": "Anime Movies"},
        publication_policy="managed",
    )

    asset = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    tracks = db.execute(
        "SELECT language, sync_policy FROM media_subtitle_tracks WHERE probe_id = ? "
        "ORDER BY stream_index",
        (probe_id,),
    ).fetchall()
    assert asset["provider_id"] == provider_id
    assert asset["provider_asset_id"] == "fixture/episode-01"
    assert asset["storage_backend"] == "local"
    assert [tuple(row) for row in tracks] == [
        ("eng", "exact_release"),
        ("fra", "exact_release"),
    ]
    assert target_id > 0


def test_provider_owner_isolation_blocks_cross_user_asset(db, user_id):
    other_user = db.execute(
        "INSERT INTO users (email, password_hash) VALUES ('other@example.com', 'hash')"
    ).lastrowid
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=2,
        title="Owned monitor",
    )
    other_provider = ensure_media_provider(
        db,
        user_id=other_user,
        kind="local_import",
        label="Other local",
        config={},
        capabilities={},
    )

    with pytest.raises(sqlite3.IntegrityError, match="provider owner mismatch"):
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="cross-user",
            source_title="Cross user",
            provider_id=other_provider,
            provider_asset_id="item",
            storage_backend="local",
            storage_locator="item.mkv",
        )


def test_realdebrid_adapter_converts_inventory(monkeypatch):
    provider = RealDebridProvider("secret")
    torrent = RDTorrent(
        id="rd-1",
        filename="Show",
        status="downloaded",
        progress=100,
        bytes_total=2048,
        bytes_done=2048,
        links=(),
        files=(RDFile(id=2, path="/Show/Episode 01.mkv", bytes=2048, selected=True),),
        torrent_hash="abcdef",
    )
    monkeypatch.setattr(provider._client, "get_torrent", AsyncMock(return_value=torrent))

    item = asyncio.run(provider.get_item("rd-1"))

    assert isinstance(provider, AcquisitionProvider)
    assert item == ProviderItem(
        external_id="rd-1",
        name="Show",
        status="downloaded",
        progress=100,
        bytes_total=2048,
        bytes_done=2048,
        files=(
            ProviderFile(
                id="2",
                path="/Show/Episode 01.mkv",
                bytes=2048,
                selected=True,
            ),
        ),
        content_hash="ABCDEF",
    )


def test_realdebrid_adapter_translates_provider_block(monkeypatch):
    provider = RealDebridProvider("secret")
    monkeypatch.setattr(
        provider._client,
        "add_magnet",
        AsyncMock(
            side_effect=RealDebridProviderBlock(
                http_status=451,
                error_code="provider_blocked",
                detail="Provider rejected this hash",
            )
        ),
    )

    with pytest.raises(ProviderBlock) as raised:
        asyncio.run(provider.acquire("magnet:?xt=urn:btih:ABC"))

    assert raised.value.http_status == 451
    assert raised.value.error_code == "provider_blocked"


def test_realdebrid_health_is_secret_free(monkeypatch):
    provider = RealDebridProvider("super-secret")
    monkeypatch.setattr(provider._client, "list_torrents", AsyncMock(return_value=[]))

    health = asyncio.run(provider.health(checked_at="2026-07-26T05:00:00Z"))

    assert health == ProviderHealth(
        status="healthy",
        reason_code="READY",
        detail="Real-Debrid inventory is reachable",
        checked_at="2026-07-26T05:00:00Z",
        capabilities=ProviderCapabilities(
            kind="realdebrid",
            acquisition=True,
            adoption=True,
            instant_check=True,
            file_selection=True,
            deletion=True,
            local_storage=False,
        ),
    )
    assert "super-secret" not in repr(health)


def test_candidate_lifecycle_uses_generic_provider_contract(db, user_id):
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=3,
        title="Contract Show",
        search_titles=("Contract Show",),
        release_status="FINISHED",
        season_number=1,
        expected_episodes=1,
        aired_episodes=1,
    )
    monitor = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    content_hash = "B" * 40
    provider = type(
        "GenericProvider",
        (),
        {
            "capabilities": ProviderCapabilities(
                kind="qbittorrent",
                acquisition=True,
                adoption=True,
                instant_check=False,
                file_selection=False,
                deletion=False,
                local_storage=True,
            ),
            "acquire": AsyncMock(return_value="job-1"),
            "get_item": AsyncMock(
                return_value=ProviderItem(
                    external_id="job-1",
                    name="Contract Show Season 1",
                    status="downloaded",
                    progress=100,
                    bytes_total=2048,
                    bytes_done=2048,
                    files=(
                        ProviderFile(
                            id="episode-1",
                            path="Contract Show S01E01.mkv",
                            bytes=2048,
                            selected=True,
                        ),
                    ),
                    content_hash=content_hash,
                    storage_locator="downloads/Contract Show Season 1",
                )
            ),
        },
    )()
    candidate = MediaCandidate(
        torrent_hash=content_hash,
        source_title="Contract Show S01E01 [1080p][Multi-Subs]",
        magnet=f"magnet:?xt=urn:btih:{content_hash}",
        seeders=10,
        size_bytes=2048,
        quality="1080p",
        score=100,
        coverage=frozenset({1}),
        search_alias="Contract Show",
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            monitor,
            (candidate,),
            rd=provider,
            searched_aliases=("Contract Show",),
            now="2026-07-26T06:00:00Z",
        )
    )

    assert result.outcome == "complete"
    asset = db.execute("SELECT * FROM media_assets").fetchone()
    configured = db.execute(
        "SELECT kind, capabilities_json FROM media_providers WHERE id = ?",
        (asset["provider_id"],),
    ).fetchone()
    assert asset["provider_asset_id"] == "job-1"
    assert asset["rd_torrent_id"] == ""
    assert asset["storage_backend"] == "local"
    assert asset["storage_locator"] == "downloads/Contract Show Season 1"
    assert configured["kind"] == "qbittorrent"
    assert '"deletion":false' in configured["capabilities_json"]
