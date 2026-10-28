"""Owner-scope and leasing tests for the watch repository."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus.db import run_migrations
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.repository import (
    claim_tracker_outbox,
    get_watch_entry,
    list_pending_confirmations,
    list_watch_events,
)
from omakase.plus.watch.service import ingest_watch_event


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


def _owner_monitor(db: sqlite3.Connection, *, email: str, anilist_id: int) -> tuple[int, int]:
    user_id = int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, 'hash')", (email,)
        ).lastrowid
    )
    planning_id = int(
        db.execute(
            """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
               VALUES (?, ?, ?, 'PLANNING')""",
            (user_id, anilist_id, f"Title {anilist_id}"),
        ).lastrowid
    )
    monitor_id = int(
        db.execute(
            """INSERT INTO media_monitors
               (user_id, anilist_planning_id, anilist_id, title, season_number,
                expected_episodes)
               VALUES (?, ?, ?, ?, 1, 12)""",
            (user_id, planning_id, anilist_id, f"Title {anilist_id}"),
        ).lastrowid
    )
    db.commit()
    return user_id, monitor_id


def test_get_watch_entry_is_owner_scoped(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="owner@example.com", anilist_id=151807)
    other_id, _ = _owner_monitor(db, email="other@example.com", anilist_id=151808)
    ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(source="plex", episode=2, event_key="plex:e2"),
        observed_at="2026-08-27T12:00:00Z",
    )

    entry = get_watch_entry(db, user_id=owner_id, monitor_id=monitor_id)

    assert entry is not None
    assert entry.progress_episodes == 2
    assert get_watch_entry(db, user_id=other_id, monitor_id=monitor_id) is None


def test_history_and_confirmations_are_owner_scoped(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="history@example.com", anilist_id=151807)
    other_id, _ = _owner_monitor(db, email="history-other@example.com", anilist_id=2)
    result = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(
            source="apple_tv",
            episode=5,
            confidence="inferred",
            event_key="apple:ambiguous:e5",
        ),
        observed_at="2026-08-27T12:00:00Z",
    )

    assert result.entry.id is not None
    history = list_watch_events(db, user_id=owner_id, entry_id=result.entry.id)
    confirmations = list_pending_confirmations(db, user_id=owner_id)

    assert [item["event_key"] for item in history] == ["apple:ambiguous:e5"]
    assert confirmations[0]["watch_event_id"] == history[0]["id"]
    assert list_watch_events(db, user_id=other_id, entry_id=result.entry.id) == []
    assert list_pending_confirmations(db, user_id=other_id) == []


def test_claim_tracker_outbox_leases_each_item_once(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="outbox@example.com", anilist_id=151807)
    for episode in (1, 2):
        ingest_watch_event(
            db,
            user_id=owner_id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(source="plex", episode=episode, event_key=f"plex:e{episode}"),
            observed_at=f"2026-08-27T12:0{episode}:00Z",
        )

    first = claim_tracker_outbox(db, limit=1, now="2026-08-27T13:00:00Z")
    second = claim_tracker_outbox(db, limit=5, now="2026-08-27T13:00:00Z")
    third = claim_tracker_outbox(db, limit=5, now="2026-08-27T13:00:00Z")

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].id != second[0].id
    assert first[0].lease_token
    assert second[0].lease_token
    assert third == []


def test_expired_lease_recovery_is_bounded_and_respects_exact_target(db):
    owner_id, monitor_id = _owner_monitor(db, email="lease@example.com", anilist_id=151807)
    for episode in (1, 2):
        ingest_watch_event(
            db,
            user_id=owner_id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(source="manual", episode=episode, event_key=f"e{episode}"),
        )
    leased = claim_tracker_outbox(db, limit=2, now="2026-09-19T12:00:00Z")
    assert claim_tracker_outbox(db, limit=2, now="2026-09-19T12:09:59Z", attempted_only=True) == []
    recovered = claim_tracker_outbox(
        db, limit=1, now="2026-09-19T12:10:01Z", attempted_only=True, outbox_id=leased[1].id
    )
    assert len(recovered) == 1
    assert recovered[0].id == leased[1].id
    assert recovered[0].lease_token != leased[1].lease_token
    assert recovered[0].attempt_count == 2
    assert (
        db.execute(
            "SELECT lease_token FROM tracker_sync_outbox WHERE id = ?", (leased[0].id,)
        ).fetchone()[0]
        == leased[0].lease_token
    )
