"""Periodic player cleanup on synthetic DB/cache fixtures, with no live jobs."""

import asyncio
import sqlite3
import time
from dataclasses import replace

import pytest

from omakase.plus import media_sync
from omakase.plus import web_playback_remux as remux
from omakase.plus import web_playback_store as store
from omakase.plus.db import run_migrations


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'cache@example.invalid','fixture')"
    )
    conn.commit()
    yield conn
    conn.close()


def session(db, target, now):
    return store.create_session(
        db,
        target=target,
        binding_digest="b" * 64,
        snapshot={"membership": {}, "policy": {}, "source": {}},
        now=now,
    )


def cache_job(db, cache, job, version, now, state="READY"):
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires) VALUES(?,?,1,?,?,?)",
        (job, version, state, now - 100, now - 1),
    )
    folder = cache / job
    folder.mkdir()
    (folder / ".omakase-remux").write_text(job)
    (folder / "chunk-1-00001.m4s").write_bytes(b"synthetic")
    db.commit()
    return folder


def test_periodic_cleanup_retires_only_known_unheld_cache_and_old_sessions(
    db, tmp_path, monkeypatch
):
    cache = tmp_path / "configured-cache"
    cache.mkdir()
    monkeypatch.setenv("OMAKASE_PLAYBACK_CACHE_ROOT", str(cache))
    now = time.time()
    library = db.execute(
        "SELECT id FROM libraries WHERE owner_user_id=1 AND kind='personal'"
    ).fetchone()[0]
    active_target = store.ResolvedTarget(1, library, "123", 1, 1, "a" * 64, 100)
    active = session(db, active_target, now)
    expired_target = replace(active_target, episode_number=2, media_version="e" * 64)
    expired = session(db, expired_target, now - 8 * 86400)
    active_folder = cache_job(db, cache, "1" * 32, "a" * 64, now)
    running_folder = cache_job(db, cache, "2" * 32, "c" * 64, now, "RUNNING")
    running_held_ready = cache_job(db, cache, "3" * 32, "c" * 64, now)
    retired_folder = cache_job(db, cache, "4" * 32, "f" * 64, now)
    unknown = cache / "unknown-owner"
    unknown.mkdir()
    (unknown / "source.mkv").write_bytes(b"preserve")
    outside = tmp_path / ("4" * 32)
    outside.mkdir()
    (outside / ".omakase-remux").write_text("4" * 32)

    media_sync._maintain_web_playback(db)

    assert (
        db.execute(
            "SELECT 1 FROM web_playback_sessions WHERE session_id=?", (expired["session_id"],)
        ).fetchone()
        is None
    )
    assert db.execute(
        "SELECT 1 FROM web_playback_sessions WHERE session_id=?", (active["session_id"],)
    ).fetchone()
    assert store.get_resume(db, target=expired_target) is not None
    assert active_folder.is_dir() and running_folder.is_dir() and running_held_ready.is_dir()
    assert not retired_folder.exists()
    assert (unknown / "source.mkv").read_bytes() == b"preserve"
    assert outside.is_dir()
    assert db.execute("SELECT COUNT(*) FROM web_playback_remux").fetchone()[0] == 3


@pytest.mark.parametrize("cache_kind", ["absent", "relative", "symlink"])
def test_unknown_or_unconfigured_cache_is_not_created_or_cleaned(
    db, tmp_path, monkeypatch, cache_kind
):
    target = tmp_path / "target"
    target.mkdir()
    cache = tmp_path / "absent"
    if cache_kind == "relative":
        cache = "relative-cache"
    elif cache_kind == "symlink":
        try:
            cache.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("Host cannot create test symlinks")
    monkeypatch.setenv("OMAKASE_PLAYBACK_CACHE_ROOT", str(cache))
    calls = []
    monkeypatch.setattr(remux, "cleanup", lambda *args, **kwargs: calls.append(args))
    media_sync._maintain_web_playback(db)
    assert calls == []
    if cache_kind == "absent":
        assert not cache.exists()


def test_disabled_player_does_not_run_housekeeping(db, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "false")
    monkeypatch.setenv("OMAKASE_PLAYBACK_CACHE_ROOT", str(tmp_path))

    def forbidden(*args, **kwargs):
        pytest.fail("Disabled player must not run maintenance")

    monkeypatch.setattr(store, "cleanup_sessions", forbidden)
    monkeypatch.setattr(remux, "cleanup", forbidden)
    media_sync._maintain_web_playback(db)


@pytest.mark.parametrize("operation", ["sessions", "cache"])
def test_unavailable_maintenance_does_not_block_sync_or_retain_lease(
    db, tmp_path, monkeypatch, operation
):
    monkeypatch.setenv("OMAKASE_PLAYBACK_CACHE_ROOT", str(tmp_path))

    def unavailable(*args, **kwargs):
        if operation == "sessions":
            raise sqlite3.OperationalError("readonly fixture")
        raise PermissionError("readonly fixture")

    monkeypatch.setattr(
        store if operation == "sessions" else remux,
        "cleanup_sessions" if operation == "sessions" else "cleanup",
        unavailable,
    )
    for owner in ("first-pass", "next-pass"):
        receipt = asyncio.run(
            media_sync.run_media_sync(
                db,
                entrypoint="timer",
                monitor_ids=(),
                lease_owner=owner,
                provider_factory=lambda _: object(),
            )
        )
        assert receipt.outcome == "complete"


def test_periodic_hook_runs_only_after_sync_lease_is_acquired(db, monkeypatch):
    from omakase.plus.media_store import acquire_sync_lease

    acquire_sync_lease(
        db,
        owner="existing",
        entrypoint="timer",
        acquired_at="2026-10-31T10:00:00Z",
        expires_at="2026-10-31T11:00:00Z",
    )
    db.commit()
    calls = []
    monkeypatch.setattr(media_sync, "_maintain_web_playback", lambda db: calls.append(True))
    receipt = asyncio.run(
        media_sync.run_media_sync(
            db,
            entrypoint="timer",
            now="2026-10-31T10:30:00Z",
            monitor_ids=(),
            provider_factory=lambda _: object(),
        )
    )
    assert receipt.outcome == "already_running"
    assert calls == []
