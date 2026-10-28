from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_store import acquire_sync_lease, ensure_media_provider, upsert_monitor
from omakase.plus.media_sync import (
    SchedulerMonitorResult,
    failure_backoff_hours,
    run_media_sync,
    schedule_monitor,
)
from omakase.plus.media_types import MediaSyncResult, MediaTarget
from omakase.plus.providers.contracts import (
    ProviderCapabilities,
    ProviderHealth,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def user_id(db: sqlite3.Connection) -> int:
    row_id = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("scheduler@example.com", "hash"),
    ).lastrowid
    assert row_id is not None
    return int(row_id)


def _monitor(db: sqlite3.Connection, user_id: int, anilist_id: int) -> int:
    return upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=anilist_id,
        title=f"Show {anilist_id}",
        search_titles=(f"Show {anilist_id}",),
        release_status="RELEASING",
        season_number=1,
        expected_episodes=2,
        aired_episodes=1,
        next_check_at=None,
    )


def _target(anilist_id: int, *, status: str = "RELEASING") -> MediaTarget:
    return MediaTarget(
        anilist_id=anilist_id,
        title=f"Show {anilist_id}",
        search_titles=(f"Show {anilist_id}",),
        format="TV",
        status=status,
        episodes=2,
        aired_episodes=1 if status == "RELEASING" else 2,
        season_number=1,
        season="SUMMER",
        season_year=2026,
        genres=("Action",),
        next_airing_at=1783900000 if status == "RELEASING" else None,
    )


def _sync_result(monitor_id: int, *, outcome: str = "complete") -> MediaSyncResult:
    return MediaSyncResult(
        monitor_id=monitor_id,
        run_id=monitor_id,
        outcome=outcome,
        required=frozenset({1}),
        covered=frozenset({1}) if outcome == "complete" else frozenset(),
        accepted_assets=(),
        detail=outcome,
    )


def test_live_lease_is_non_mutating_and_expired_lease_recovers_with_hash(db):
    first = acquire_sync_lease(
        db,
        owner="owner-a-secret",
        entrypoint="timer",
        acquired_at="2026-07-15T08:00:00Z",
        expires_at="2026-07-15T08:30:00Z",
    )
    db.commit()
    contender = acquire_sync_lease(
        db,
        owner="owner-b-secret",
        entrypoint="cli",
        acquired_at="2026-07-15T08:10:00Z",
        expires_at="2026-07-15T08:40:00Z",
    )
    row_during_live_lease = tuple(
        db.execute("SELECT owner, entrypoint, expires_at FROM media_sync_lease").fetchone()
    )
    recovered = acquire_sync_lease(
        db,
        owner="owner-b-secret",
        entrypoint="cli",
        acquired_at="2026-07-15T08:31:00Z",
        expires_at="2026-07-15T09:01:00Z",
    )

    assert first.acquired is True
    assert contender.acquired is False
    assert contender.outcome == "already_running"
    assert row_during_live_lease == ("owner-a-secret", "timer", "2026-07-15T08:30:00Z")
    assert recovered.acquired is True
    assert recovered.recovered_owner_hash == hashlib.sha256(b"owner-a-secret").hexdigest()
    recovered_row = db.execute(
        "SELECT owner, prior_owner_hash, prior_expires_at FROM media_sync_lease"
    ).fetchone()
    assert tuple(recovered_row) == (
        "owner-b-secret",
        hashlib.sha256(b"owner-a-secret").hexdigest(),
        "2026-07-15T08:30:00Z",
    )


def test_live_contender_returns_already_running_without_sync(db, user_id, monkeypatch):
    monitor_id = _monitor(db, user_id, 1)
    acquire_sync_lease(
        db,
        owner="active-owner",
        entrypoint="timer",
        acquired_at="2026-07-15T08:00:00Z",
        expires_at="2026-07-15T08:30:00Z",
    )
    db.commit()
    reconcile = AsyncMock()
    monkeypatch.setattr("omakase.plus.media_sync.reconcile_monitor", reconcile)

    receipt = asyncio.run(
        run_media_sync(
            db,
            entrypoint="direct_request",
            monitor_ids=(monitor_id,),
            now="2026-07-15T08:10:00Z",
            lease_owner="contender",
            identity_resolver=lambda *_: _target(1),
            rd_factory=lambda _user_id: object(),
        )
    )

    assert receipt.outcome == "already_running"
    assert receipt.monitor_results == ()
    reconcile.assert_not_awaited()


def test_monitor_failure_isolated_and_backoff_persisted(db, user_id, monkeypatch):
    first_id = _monitor(db, user_id, 1)
    second_id = _monitor(db, user_id, 2)
    reconcile = AsyncMock(side_effect=[RuntimeError("provider failed"), _sync_result(second_id)])
    monkeypatch.setattr("omakase.plus.media_sync.reconcile_monitor", reconcile)

    receipt = asyncio.run(
        run_media_sync(
            db,
            entrypoint="timer",
            now="2026-07-15T08:00:00Z",
            lease_owner="scheduler-owner",
            identity_resolver=lambda value, _season: _target(int(value)),
            rd_factory=lambda _user_id: object(),
        )
    )

    assert receipt.outcome == "degraded"
    assert receipt.monitor_results == (
        SchedulerMonitorResult(first_id, "degraded", "SYNC_FAILED"),
        SchedulerMonitorResult(second_id, "complete", "complete"),
    )
    first = db.execute(
        "SELECT consecutive_failures, next_check_at, last_result FROM media_monitors WHERE id = ?",
        (first_id,),
    ).fetchone()
    second = db.execute(
        "SELECT consecutive_failures, next_check_at FROM media_monitors WHERE id = ?",
        (second_id,),
    ).fetchone()
    assert tuple(first) == (1, "2026-07-15T12:00:00Z", "SYNC_FAILED")
    assert tuple(second) == (0, "2026-07-15T12:00:00Z")


def test_timer_refreshes_provider_health_even_when_no_title_is_due(db, user_id):
    capabilities = ProviderCapabilities(
        kind="realdebrid",
        acquisition=True,
        adoption=True,
        instant_check=True,
        file_selection=True,
        deletion=True,
        local_storage=False,
    )
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="realdebrid",
        label="Real-Debrid",
        config={},
        capabilities={},
    )
    provider = type(
        "HealthyProvider",
        (),
        {
            "capabilities": capabilities,
            "health": AsyncMock(
                return_value=ProviderHealth(
                    status="healthy",
                    reason_code="READY",
                    detail="Real-Debrid inventory is reachable",
                    checked_at="2026-07-15T08:00:00Z",
                    capabilities=capabilities,
                )
            ),
        },
    )()

    receipt = asyncio.run(
        run_media_sync(
            db,
            entrypoint="timer",
            now="2026-07-15T08:00:00Z",
            lease_owner="health-owner",
            identity_resolver=lambda *_: None,
            provider_factory=lambda _user_id: provider,
        )
    )

    assert receipt.monitor_results == ()
    row = db.execute(
        """SELECT health_status, health_reason_code, last_health_at
           FROM media_providers WHERE id = ?""",
        (provider_id,),
    ).fetchone()
    assert tuple(row) == ("healthy", "READY", "2026-07-15T08:00:00Z")


@pytest.mark.parametrize(
    ("failures", "hours"),
    [(1, 4), (2, 8), (3, 16), (4, 32), (5, 48), (99, 48)],
)
def test_failure_backoff_is_bounded(failures, hours):
    assert failure_backoff_hours(failures) == hours


def test_finished_incomplete_monitor_checks_in_12_hours(db, user_id):
    monitor_id = _monitor(db, user_id, 3)
    db.execute(
        "UPDATE media_monitors SET release_status = 'FINISHED', aired_episodes = 2 WHERE id = ?",
        (monitor_id,),
    )

    schedule_monitor(
        db,
        monitor_id=monitor_id,
        now="2026-07-15T08:00:00Z",
        sync_result=MediaSyncResult(
            monitor_id=monitor_id,
            run_id=1,
            outcome="degraded",
            required=frozenset({1, 2}),
            covered=frozenset({1}),
            accepted_assets=(),
            detail="incomplete",
        ),
    )

    row = db.execute(
        "SELECT active, consecutive_failures, next_check_at FROM media_monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(row) == (1, 0, "2026-07-15T20:00:00Z")


def test_finished_complete_monitor_closes(db, user_id):
    monitor_id = _monitor(db, user_id, 4)
    db.execute(
        "UPDATE media_monitors SET release_status = 'FINISHED', aired_episodes = 2 WHERE id = ?",
        (monitor_id,),
    )

    schedule_monitor(
        db,
        monitor_id=monitor_id,
        now="2026-07-15T08:00:00Z",
        sync_result=MediaSyncResult(
            monitor_id=monitor_id,
            run_id=1,
            outcome="complete",
            required=frozenset({1, 2}),
            covered=frozenset({1, 2}),
            accepted_assets=(),
            detail="complete",
        ),
    )

    row = db.execute(
        "SELECT active, consecutive_failures, next_check_at FROM media_monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(row) == (0, 0, None)


@pytest.mark.parametrize(
    ("unit_name", "required_lines"),
    [
        (
            "omakase-media-sync.service",
            (
                "Type=oneshot",
                "NoNewPrivileges=true",
                "RuntimeDirectory=omakase-media-sync",
                "ReadWritePaths=/run/omakase-media-sync",
                "flock -n /run/omakase-media-sync/sync.lock",
                "docker exec omakase-plus omakase plus sync-media",
            ),
        ),
        (
            "omakase-media-sync.timer",
            (
                "Persistent=true",
                "OnBootSec=5m",
                "OnUnitActiveSec=15m",
                "RandomizedDelaySec=2m",
            ),
        ),
    ],
)
def test_media_sync_systemd_units_are_hardened(unit_name, required_lines):
    text = (REPO_ROOT / "deploy" / "systemd" / unit_name).read_text(encoding="utf-8")

    for required_line in required_lines:
        assert required_line in text
    assert "REALDEBRID_API_KEY=" not in text
