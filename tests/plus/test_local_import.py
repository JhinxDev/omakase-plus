"""Hash-bound local import planning and apply tests."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.local_import import (
    LocalImportError,
    LocalImportRequest,
    apply_local_import_plan,
    plan_local_import,
)
from omakase.plus.manifest import build_library_manifest
from omakase.plus.media_probe import ExternalSubtitle, MediaProbeResult
from omakase.plus.media_store import upsert_monitor
from omakase.plus.providers.local import LocalImportProvider
from omakase.plus.storage import configure_root, file_receipt


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def user_and_monitor(db):
    user_id = db.execute(
        "INSERT INTO users (email, password_hash) VALUES ('local@example.com', 'hash')"
    ).lastrowid
    monitor_id = upsert_monitor(
        db,
        user_id=int(user_id),
        anilist_id=777,
        title="Fixture Show",
        search_titles=("Fixture Show",),
        genres=("Action",),
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=2,
        aired_episodes=2,
    )
    db.commit()
    return int(user_id), monitor_id


def _usable_probe(_root, _locator, *, inventory_fingerprint, **_kwargs):
    return MediaProbeResult(
        outcome="usable",
        detail="",
        inventory_fingerprint=inventory_fingerprint,
        byte_range_receipt={"file_bytes": 5, "range_bytes": 5},
        video_streams=({"stream_index": 0, "codec": "h264"},),
        audio_streams=({"stream_index": 1, "language": "jpn", "codec": "aac"},),
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
        ),
    )


def test_plan_is_read_only_and_apply_persists_verified_local_asset(db, user_and_monitor, tmp_path):
    user_id, monitor_id = user_and_monitor
    media = tmp_path / "Fixture Show S01E01.mkv"
    media.write_bytes(b"media")
    root = configure_root(tmp_path, owner_id=user_id)
    before = db.total_changes

    plan = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=(LocalImportRequest(monitor_id, media.name),),
        created_at="2026-07-26T07:00:00Z",
    )

    assert db.total_changes == before
    assert plan["files"][0]["episode_coverage"] == [1]
    assert plan["files"][0]["collision"] == "none"
    receipt = apply_local_import_plan(
        db,
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        root=root,
        applied_at="2026-07-26T07:01:00Z",
        probe=_usable_probe,
    )

    assert receipt["usable"] == 1
    asset = db.execute("SELECT * FROM media_assets").fetchone()
    provider = db.execute(
        "SELECT kind, config_json FROM media_providers WHERE id = ?",
        (asset["provider_id"],),
    ).fetchone()
    tracks = db.execute(
        """SELECT language, sync_policy
           FROM media_subtitle_tracks ORDER BY stream_index"""
    ).fetchall()
    assert asset["provider_asset_id"] == media.name
    assert asset["rd_torrent_id"] == ""
    assert asset["storage_backend"] == "local"
    assert asset["status"] == "downloaded"
    assert asset["usable_coverage_json"] == "[1]"
    assert provider["kind"] == "local_import"
    assert str(tmp_path) not in provider["config_json"]
    assert [tuple(row) for row in tracks] == [
        ("eng", "exact_release"),
        ("fra", "exact_release"),
    ]
    manifest_asset = build_library_manifest(
        db,
        generated_at="2026-07-26T07:02:00Z",
    )["titles"][0]["assets"][0]
    assert len(manifest_asset["torrent_hash"]) == 64
    assert manifest_asset["storage_backend"] == "local"
    assert manifest_asset["storage_locator"] == media.name


def test_apply_refuses_content_changed_after_plan(db, user_and_monitor, tmp_path):
    user_id, monitor_id = user_and_monitor
    media = tmp_path / "Fixture Show S01E01.mkv"
    media.write_bytes(b"first")
    root = configure_root(tmp_path, owner_id=user_id)
    plan = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=(LocalImportRequest(monitor_id, media.name),),
        created_at="2026-07-26T07:00:00Z",
    )
    media.write_bytes(b"changed")

    with pytest.raises(LocalImportError, match="inventory changed"):
        apply_local_import_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            root=root,
            applied_at="2026-07-26T07:01:00Z",
            probe=_usable_probe,
        )
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0


def test_apply_is_idempotent_after_exact_success(db, user_and_monitor, tmp_path):
    user_id, monitor_id = user_and_monitor
    media = tmp_path / "Fixture Show S01E01.mkv"
    media.write_bytes(b"media")
    root = configure_root(tmp_path, owner_id=user_id)
    plan = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=(LocalImportRequest(monitor_id, media.name),),
        created_at="2026-07-26T07:00:00Z",
    )
    first = apply_local_import_plan(
        db,
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        root=root,
        applied_at="2026-07-26T07:01:00Z",
        probe=_usable_probe,
    )
    second = apply_local_import_plan(
        db,
        plan=plan,
        expected_plan_hash=plan["plan_hash"],
        root=root,
        applied_at="2026-07-26T07:02:00Z",
        probe=_usable_probe,
    )

    assert first["imported"] == 1
    assert second["imported"] == 0
    assert second["already_present"] == 1
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 1


def test_plan_refuses_root_owned_by_another_user(db, user_and_monitor, tmp_path):
    user_id, monitor_id = user_and_monitor
    media = tmp_path / "Fixture Show S01E01.mkv"
    media.write_bytes(b"media")
    root = configure_root(tmp_path, owner_id=user_id + 1)

    with pytest.raises(LocalImportError, match="different user"):
        plan_local_import(
            db,
            user_id=user_id,
            root=root,
            requests=(LocalImportRequest(monitor_id, media.name),),
            created_at="2026-07-26T07:00:00Z",
        )


def test_plan_reports_cross_monitor_content_collision(db, user_and_monitor, tmp_path):
    user_id, first_monitor = user_and_monitor
    first = tmp_path / "Fixture Show S01E01.mkv"
    first.write_bytes(b"identical")
    root = configure_root(tmp_path, owner_id=user_id)
    first_plan = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=(LocalImportRequest(first_monitor, first.name),),
        created_at="2026-07-26T07:00:00Z",
    )
    apply_local_import_plan(
        db,
        plan=first_plan,
        expected_plan_hash=first_plan["plan_hash"],
        root=root,
        applied_at="2026-07-26T07:01:00Z",
        probe=_usable_probe,
    )
    second_monitor = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=778,
        title="Other Show",
        search_titles=("Other Show",),
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=1,
        aired_episodes=1,
    )
    second = tmp_path / "Other Show S01E01.mkv"
    second.write_bytes(b"identical")

    collision = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=(LocalImportRequest(second_monitor, second.name),),
        created_at="2026-07-26T07:02:00Z",
    )

    assert collision["files"][0]["collision"] == "collision"
    with pytest.raises(LocalImportError, match="content collision"):
        apply_local_import_plan(
            db,
            plan=collision,
            expected_plan_hash=collision["plan_hash"],
            root=root,
            applied_at="2026-07-26T07:03:00Z",
            probe=_usable_probe,
        )


def test_external_subtitle_is_content_bound_in_plan(db, user_and_monitor, tmp_path):
    user_id, monitor_id = user_and_monitor
    media = tmp_path / "Fixture Show S01E01.mkv"
    subtitle = tmp_path / "Fixture Show S01E01.en.srt"
    media.write_bytes(b"media")
    subtitle.write_text("first", encoding="utf-8")
    root = configure_root(tmp_path, owner_id=user_id)
    fingerprint = file_receipt(root, media.name).sha256
    plan = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=(
            LocalImportRequest(
                monitor_id,
                media.name,
                external_subtitles=(
                    ExternalSubtitle(
                        locator=subtitle.name,
                        language="eng",
                        release_fingerprint=fingerprint,
                        sync_state="verified",
                    ),
                ),
            ),
        ),
        created_at="2026-07-26T07:00:00Z",
    )
    subtitle.write_text("changed timing", encoding="utf-8")

    with pytest.raises(LocalImportError, match="inventory changed"):
        apply_local_import_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            root=root,
            applied_at="2026-07-26T07:01:00Z",
            probe=_usable_probe,
        )


def test_local_provider_inventory_is_read_only_and_bounded(tmp_path):
    first = tmp_path / "Fixture Show S01E01.mkv"
    second = tmp_path / "Fixture Show S01E02.mkv"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    root = configure_root(tmp_path, owner_id=1)
    provider = LocalImportProvider(root, max_inventory_files=1)

    items = asyncio.run(provider.list_items())

    assert len(items) == 1
    assert items[0].storage_locator == first.name
    assert asyncio.run(provider.delete_item(first.name)) is False
    assert first.exists() and second.exists()
