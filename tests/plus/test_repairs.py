"""Safe, owner-scoped repair workflow tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_probe import MediaProbeResult
from omakase.plus.media_store import (
    ensure_media_provider,
    upsert_asset,
    upsert_monitor,
    upsert_plex_target,
)
from omakase.plus.repairs import (
    RepairError,
    quarantine_asset,
    rematch_plan,
    reprobe_asset,
    rescan_title,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


def _user(db, email: str) -> int:
    return int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, 'hash')",
            (email,),
        ).lastrowid
    )


def _asset(db, user_id: int) -> tuple[int, int]:
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=180560,
        title="Hell's Paradise Season 2",
        search_titles=("Jigokuraku 2nd Season",),
        format="TV",
        release_status="RELEASING",
        season_number=2,
        expected_episodes=12,
        aired_episodes=4,
    )
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="local_import",
        label="Local library",
        config={},
        capabilities={"adoption": True},
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Jigokuraku S02E01",
        provider_id=provider_id,
        provider_asset_id="jigokuraku-s02-e01",
        storage_backend="local",
        storage_locator="Jigokuraku/Season 02/Episode 01.mkv",
        ownership="monitor_owned",
        provenance="adopted",
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="c" * 64,
        inventory_verified_at="2026-07-26T12:00:00Z",
        status="downloaded",
        progress=100,
    )
    db.commit()
    return monitor_id, asset_id


def test_quarantine_is_idempotent_and_never_deletes_the_asset(db):
    user_id = _user(db, "owner@example.com")
    monitor_id, asset_id = _asset(db, user_id)

    first = quarantine_asset(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        asset_id=asset_id,
        reason="Subtitle timing is not trusted",
    )
    second = quarantine_asset(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        asset_id=asset_id,
        reason="Subtitle timing is not trusted",
    )
    row = db.execute(
        "SELECT status, last_error FROM media_assets WHERE id = ?", (asset_id,)
    ).fetchone()

    assert first["changed"] is True
    assert second["changed"] is False
    assert dict(row) == {
        "status": "quarantined",
        "last_error": "Subtitle timing is not trusted",
    }


def test_repairs_refuse_cross_user_assets_and_titles(db):
    owner_id = _user(db, "owner@example.com")
    stranger_id = _user(db, "stranger@example.com")
    monitor_id, asset_id = _asset(db, owner_id)

    with pytest.raises(RepairError, match="does not exist"):
        quarantine_asset(
            db,
            user_id=stranger_id,
            monitor_id=monitor_id,
            asset_id=asset_id,
            reason="Not mine",
        )
    with pytest.raises(RepairError, match="does not exist"):
        rematch_plan(db, user_id=stranger_id, monitor_id=monitor_id)


def test_rematch_plan_is_stable_and_explicitly_read_only(db):
    user_id = _user(db, "owner@example.com")
    monitor_id, _ = _asset(db, user_id)

    first = rematch_plan(db, user_id=user_id, monitor_id=monitor_id)
    second = rematch_plan(db, user_id=user_id, monitor_id=monitor_id)

    assert first == second
    assert first["mutation"] is False
    assert first["identity"]["search_titles"] == ["Jigokuraku 2nd Season"]
    assert len(first["plan_hash"]) == 64


def test_reprobe_uses_one_exact_owned_locator_and_persists_result(db, tmp_path):
    user_id = _user(db, "owner@example.com")
    monitor_id, asset_id = _asset(db, user_id)
    root = MagicMock()
    result = MediaProbeResult(
        outcome="usable",
        detail="",
        inventory_fingerprint="c" * 64,
        byte_range_receipt={},
        video_streams=({"codec": "hevc"},),
        audio_streams=({"codec": "aac", "language": "jpn"},),
        subtitle_tracks=(
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
        ),
    )

    with (
        patch("omakase.plus.repairs.configure_root", return_value=root),
        patch("omakase.plus.repairs.probe_media_file", return_value=result) as probe,
    ):
        receipt = reprobe_asset(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            asset_id=asset_id,
            storage_roots={"local": Path(tmp_path)},
            verified_at="2026-07-26T12:01:00Z",
        )

    probe.assert_called_once_with(
        root,
        "Jigokuraku/Season 02/Episode 01.mkv",
        inventory_fingerprint="c" * 64,
    )
    assert receipt == {"asset_id": asset_id, "outcome": "usable", "subtitle_tracks": 1}
    assert db.execute("SELECT COUNT(*) FROM media_subtitle_tracks").fetchone()[0] == 1


def test_plex_repair_scans_only_the_mapped_section_and_path(db):
    user_id = _user(db, "owner@example.com")
    monitor_id, _ = _asset(db, user_id)
    upsert_plex_target(
        db,
        user_id=user_id,
        label="Primary Plex",
        base_url="http://plex:32400",
        token_secret_name="plex_primary_token",
        library_map={
            "show": {"section_key": 7, "path": "/media/anime"},
            "movie": {"section_key": 8, "path": "/media/anime-movies"},
        },
        publication_policy="managed",
    )
    db.commit()
    client = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = client
    context.__exit__.return_value = False

    with patch("omakase.plus.repairs.PlexClient", return_value=context) as plex:
        receipt = rescan_title(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            token_reader=lambda *_: "token-from-secret-store",
        )

    plex.assert_called_once_with("http://plex:32400", "token-from-secret-store")
    client.scan_section.assert_called_once_with(7, path="/media/anime")
    assert receipt["scan"] == "requested"
