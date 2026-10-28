"""Reliable AniList processing for leased watch-progress outbox work."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import httpx
import pytest

from omakase.plus.db import run_migrations
from omakase.plus.watch.anilist_sync import process_tracker_outbox
from omakase.plus.watch.models import WatchEvent
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


def _owner_monitor(db: sqlite3.Connection) -> tuple[int, int]:
    user_id = int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES ('sync@example.com', 'hash')"
        ).lastrowid
    )
    planning_id = int(
        db.execute(
            """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
               VALUES (?, 151807, 'Solo Leveling', 'PLANNING')""",
            (user_id,),
        ).lastrowid
    )
    monitor_id = int(
        db.execute(
            """INSERT INTO media_monitors
               (user_id, anilist_planning_id, anilist_id, title, season_number,
                expected_episodes)
               VALUES (?, ?, 151807, 'Solo Leveling', 1, 12)""",
            (user_id, planning_id),
        ).lastrowid
    )
    db.commit()
    return user_id, monitor_id


def _enqueue_progress(
    db: sqlite3.Connection, *, episode: int = 5, complete: bool = False
) -> tuple[int, int]:
    user_id, monitor_id = _owner_monitor(db)
    event = (
        WatchEvent.complete(source="manual", event_key="manual:complete", force=True)
        if complete
        else WatchEvent.progress(source="plex", episode=episode, event_key=f"plex:e{episode}")
    )
    ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=event,
        observed_at="2026-08-27T11:00:00Z",
    )
    return user_id, monitor_id


def test_progress_sync_marks_item_synced_only_after_exact_receipt(
    db: sqlite3.Connection,
) -> None:
    _enqueue_progress(db)
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "mediaId": 151807, "status": "CURRENT", "progress": 3},
        ),
        patch(
            "omakase.plus.watch.anilist_sync.save_progress_entry",
            return_value={"id": 81, "mediaId": 151807, "status": "CURRENT", "progress": 5},
        ) as save,
    ):
        result = process_tracker_outbox(db, now="2026-08-27T12:00:00Z")

    assert result == {"claimed": 1, "synced": 1, "failed": 0, "conflict": 0}
    save.assert_called_once_with("oauth-token", 151807, progress=5)
    row = db.execute(
        """SELECT sync_state, remote_entry_id, last_error, lease_token
           FROM tracker_sync_outbox"""
    ).fetchone()
    assert dict(row) == {
        "sync_state": "synced",
        "remote_entry_id": 81,
        "last_error": "",
        "lease_token": None,
    }


def test_matching_remote_progress_is_idempotently_synced_without_write(
    db: sqlite3.Connection,
) -> None:
    _enqueue_progress(db)
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "mediaId": 151807, "status": "CURRENT", "progress": 5},
        ),
        patch("omakase.plus.watch.anilist_sync.save_progress_entry") as save,
    ):
        result = process_tracker_outbox(db, now="2026-08-27T12:00:00Z")

    assert result["synced"] == 1
    save.assert_not_called()


@pytest.mark.parametrize(
    "remote",
    [
        {"id": 81, "mediaId": 151807, "status": "CURRENT", "progress": 7},
        {"id": 81, "mediaId": 151807, "status": "COMPLETED", "progress": 12},
    ],
)
def test_remote_state_ahead_becomes_conflict_without_local_or_remote_rollback(
    db: sqlite3.Connection, remote
) -> None:
    _enqueue_progress(db)
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value=remote,
        ),
        patch("omakase.plus.watch.anilist_sync.save_progress_entry") as save,
    ):
        result = process_tracker_outbox(db, now="2026-08-27T12:00:00Z")

    assert result["conflict"] == 1
    save.assert_not_called()
    assert db.execute("SELECT sync_state FROM tracker_sync_outbox").fetchone()[0] == "conflict"
    assert db.execute("SELECT progress_episodes FROM watch_entries").fetchone()[0] == 5


def test_missing_oauth_token_schedules_bounded_retry(db: sqlite3.Connection) -> None:
    _enqueue_progress(db)
    with patch("omakase.plus.watch.anilist_sync.read_secret", return_value=None):
        result = process_tracker_outbox(db, now="2026-08-27T12:00:00Z")

    assert result["failed"] == 1
    row = db.execute(
        """SELECT sync_state, attempt_count, not_before, last_error, lease_token
           FROM tracker_sync_outbox"""
    ).fetchone()
    assert dict(row) == {
        "sync_state": "failed",
        "attempt_count": 1,
        "not_before": "2026-08-27T12:00:30Z",
        "last_error": "AniList connection is missing or expired",
        "lease_token": None,
    }


def test_transport_failure_is_redacted_and_retryable(db: sqlite3.Connection) -> None:
    _enqueue_progress(db)
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            side_effect=httpx.ConnectError("Bearer private.token API_KEY=sk-private failed"),
        ),
    ):
        result = process_tracker_outbox(db, now="2026-08-27T12:00:00Z")

    assert result["failed"] == 1
    detail = db.execute("SELECT last_error FROM tracker_sync_outbox").fetchone()[0]
    assert "private.token" not in detail
    assert "sk-private" not in detail
    assert len(detail) <= 240


@pytest.mark.parametrize("remote_status", ["CURRENT", "COMPLETED"])
def test_scored_completion_syncs_rating_even_when_remote_already_complete(db, remote_status):
    user_id, monitor_id = _owner_monitor(db)
    ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="rated"),
        score_ten=9,
    )
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "status": remote_status, "score": 6},
        ),
        patch(
            "omakase.plus.watch.anilist_sync.save_completed_entry",
            return_value={"id": 81, "status": "COMPLETED", "score": 9},
        ) as save,
    ):
        result = process_tracker_outbox(db)
    assert result["synced"] == 1
    save.assert_called_once_with("oauth-token", 151807, score_ten=9)


def test_exact_outbox_processing_and_scheduled_retries_preserve_dormant_work(db):
    user_id, monitor_id = _enqueue_progress(db, episode=3)
    recent = ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="rated"),
        score_ten=7,
    )
    with patch("omakase.plus.watch.anilist_sync.read_secret", return_value=None):
        assert process_tracker_outbox(db, attempted_only=True)["claimed"] == 0
        assert (
            process_tracker_outbox(db, outbox_id=recent.outbox_id, now="2026-09-19T10:00:00Z")[
                "failed"
            ]
            == 1
        )
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "status": "COMPLETED"},
        ),
        patch(
            "omakase.plus.watch.anilist_sync.save_completed_entry",
            return_value={"id": 81, "status": "COMPLETED"},
        ) as save,
    ):
        assert (
            process_tracker_outbox(db, attempted_only=True, now="2026-09-19T10:01:00Z")["synced"]
            == 1
        )
    save.assert_called_once_with("oauth-token", 151807, score_ten=7)
    rows = db.execute(
        "SELECT sync_state, attempt_count FROM tracker_sync_outbox ORDER BY id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("pending", 0), ("synced", 2)]


def test_old_failed_score_cannot_overwrite_newer_synced_completion(db):
    user_id, monitor_id = _owner_monitor(db)
    old = ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="old-score"),
        score_ten=3,
    )
    with patch("omakase.plus.watch.anilist_sync.read_secret", return_value=None):
        process_tracker_outbox(db, outbox_id=old.outbox_id, now="2026-09-19T10:00:00Z")
    new = ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="new-score"),
        score_ten=9,
    )
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "status": "COMPLETED"},
        ),
        patch(
            "omakase.plus.watch.anilist_sync.save_completed_entry",
            return_value={"id": 81, "status": "COMPLETED"},
        ) as save,
    ):
        process_tracker_outbox(db, outbox_id=new.outbox_id, now="2026-09-19T10:00:10Z")
        result = process_tracker_outbox(db, attempted_only=True, now="2026-09-19T10:01:00Z")
    save.assert_called_once_with("oauth-token", 151807, score_ten=9)
    assert result["conflict"] == 1
    old_row = db.execute(
        "SELECT sync_state, last_error FROM tracker_sync_outbox WHERE id = ?", (old.outbox_id,)
    ).fetchone()
    assert old_row["sync_state"] == "conflict"
    assert "Superseded" in old_row["last_error"]


def test_unscored_completion_leaves_existing_remote_score_untouched(db):
    _enqueue_progress(db, complete=True)
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "status": "COMPLETED", "score": 9},
        ),
        patch("omakase.plus.watch.anilist_sync.save_completed_entry") as save,
    ):
        assert process_tracker_outbox(db)["synced"] == 1
    save.assert_not_called()


def test_completion_uses_existing_exact_completion_mutation(db: sqlite3.Connection) -> None:
    _enqueue_progress(db, complete=True)
    with (
        patch("omakase.plus.watch.anilist_sync.read_secret", return_value="oauth-token"),
        patch(
            "omakase.plus.watch.anilist_sync.get_authenticated_media_entry",
            return_value={"id": 81, "mediaId": 151807, "status": "CURRENT", "progress": 5},
        ),
        patch(
            "omakase.plus.watch.anilist_sync.save_completed_entry",
            return_value={"id": 81, "mediaId": 151807, "status": "COMPLETED"},
        ) as save,
    ):
        result = process_tracker_outbox(db, now="2026-08-27T12:00:00Z")

    assert result["synced"] == 1
    save.assert_called_once_with("oauth-token", 151807)
