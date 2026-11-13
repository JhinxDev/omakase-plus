"""Transactional ingestion tests for normalized watch events."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus.db import run_migrations
from omakase.plus.watch import repository
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import (
    WatchServiceError,
    ingest_watch_event,
    resolve_watch_confirmation,
)


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


@pytest.mark.parametrize("score", [0, 11, True, 7.5, "8"])
def test_completion_score_rejected_before_any_watch_mutation(db, score):
    owner_id, monitor_id = _owner_monitor(db, email="score@example.com", anilist_id=151807)
    with pytest.raises(WatchServiceError, match="score"):
        ingest_watch_event(
            db,
            user_id=owner_id,
            monitor_id=monitor_id,
            event=WatchEvent.complete(source="manual", event_key="rated"),
            score_ten=score,
        )
    for table in ("watch_entries", "watch_events", "tracker_sync_outbox"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_completion_score_is_durable_and_replay_does_not_replace_it(db):
    owner_id, monitor_id = _owner_monitor(db, email="score@example.com", anilist_id=151807)
    event = WatchEvent.complete(source="manual", event_key="rated")
    for score in (8, 3):
        ingest_watch_event(
            db, user_id=owner_id, monitor_id=monitor_id, event=event, score_ten=score
        )
    assert [row[0] for row in db.execute("SELECT score_ten FROM watch_events")] == [8]
    assert [row[0] for row in db.execute("SELECT score_ten FROM tracker_sync_outbox")] == [8]


def test_completion_score_rolls_back_with_outbox_failure(db, monkeypatch):
    owner_id, monitor_id = _owner_monitor(db, email="score@example.com", anilist_id=151807)

    def fail(*args, **kwargs):
        raise RuntimeError("outbox failure")

    monkeypatch.setattr(repository, "insert_tracker_outbox", fail)
    with pytest.raises(RuntimeError, match="outbox failure"):
        ingest_watch_event(
            db,
            user_id=owner_id,
            monitor_id=monitor_id,
            event=WatchEvent.complete(source="manual", event_key="rated"),
            score_ten=8,
        )
    for table in ("watch_entries", "watch_events", "tracker_sync_outbox"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_score_can_be_added_after_completion_but_unscored_repeat_is_noop(db):
    owner_id, monitor_id = _owner_monitor(db, email="score@example.com", anilist_id=151807)
    first = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="complete"),
    )
    rated = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="rate"),
        score_ten=8,
    )
    repeat = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="repeat"),
    )
    assert rated.outbox_id is not None
    assert rated.entry.completed_at == first.entry.completed_at
    assert repeat.outbox_id is None
    assert db.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 2


@pytest.mark.parametrize(
    "event",
    [
        WatchEvent.complete(source="plex", event_key="plex"),
        WatchEvent.progress(source="manual", episode=2, event_key="manual"),
    ],
)
def test_score_requires_manual_completion(db, event):
    owner_id, monitor_id = _owner_monitor(db, email="score@example.com", anilist_id=151807)
    with pytest.raises(WatchServiceError, match="manual completion"):
        ingest_watch_event(db, user_id=owner_id, monitor_id=monitor_id, event=event, score_ten=8)
    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0


def test_ingest_event_is_atomic_and_idempotent(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="atomic@example.com", anilist_id=151807)
    event = WatchEvent.progress(
        source="plex", episode=4, confidence="exact", event_key="plex:session-7:e4"
    )

    first = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=event,
        observed_at="2026-08-27T12:00:00Z",
    )
    second = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=event,
        observed_at="2026-08-27T12:00:00Z",
    )

    assert first.disposition == "accepted"
    assert first.entry.progress_episodes == 4
    assert second.disposition == "ignored"
    assert second.reason == "duplicate_event"
    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 1


def test_ingest_rejects_monitor_owned_by_another_user(db: sqlite3.Connection) -> None:
    _, monitor_id = _owner_monitor(db, email="owner@example.com", anilist_id=1)
    other_id, _ = _owner_monitor(db, email="other@example.com", anilist_id=2)

    with pytest.raises(WatchServiceError, match="monitor"):
        ingest_watch_event(
            db,
            user_id=other_id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(source="plex", episode=1, event_key="plex:e1"),
            observed_at="2026-08-27T12:00:00Z",
        )

    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0


def test_inferred_event_creates_confirmation_without_progress(
    db: sqlite3.Connection,
) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="confirm@example.com", anilist_id=151807)

    result = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(
            source="apple_tv",
            episode=7,
            confidence="inferred",
            event_key="apple:living-room:e7",
        ),
        observed_at="2026-08-27T12:00:00Z",
    )

    assert result.disposition == "confirmation_required"
    assert result.entry.progress_episodes == 0
    assert result.confirmation_id is not None
    assert db.execute("SELECT count(*) FROM watch_confirmations").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 0


def test_manual_reopen_is_persisted_and_enqueued(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="reopen@example.com", anilist_id=151807)
    ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="manual:complete", force=True),
        observed_at="2026-08-27T12:00:00Z",
    )

    result = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.reopen(episode=3, event_key="manual:reopen", force=True),
        observed_at="2026-08-27T12:05:00Z",
    )

    assert result.entry.status == "watching"
    assert result.entry.progress_episodes == 3
    assert result.entry.completed_at is None
    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 2


def test_anilist_import_does_not_enqueue_an_echo(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="anilist-import@example.com", anilist_id=151807)

    result = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(source="anilist", episode=6, event_key="anilist:151807:6"),
        observed_at="2026-08-27T12:00:00Z",
    )

    assert result.entry.progress_episodes == 6
    assert result.outbox_id is None
    assert db.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 0


def test_ingest_rolls_back_projection_event_and_outbox_on_error(
    db: sqlite3.Connection, monkeypatch
) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="rollback@example.com", anilist_id=151807)

    def fail_outbox(*args, **kwargs):
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr(repository, "insert_tracker_outbox", fail_outbox)

    with pytest.raises(RuntimeError, match="synthetic"):
        ingest_watch_event(
            db,
            user_id=owner_id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(source="plex", episode=4, event_key="plex:e4"),
            observed_at="2026-08-27T12:00:00Z",
        )

    assert db.execute("SELECT count(*) FROM watch_entries").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 0


def test_confirmation_accept_advances_and_resolves_atomically(db: sqlite3.Connection) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="accept@example.com", anilist_id=151807)
    inferred = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(
            source="apple_tv",
            episode=4,
            confidence="inferred",
            event_key="apple:accept:e4",
        ),
        observed_at="2026-08-27T12:00:00Z",
    )
    assert inferred.confirmation_id is not None

    resolved = resolve_watch_confirmation(
        db,
        user_id=owner_id,
        confirmation_id=inferred.confirmation_id,
        accepted=True,
        observed_at="2026-08-27T12:01:00Z",
    )

    assert resolved.status == "accepted"
    assert resolved.ingest_result is not None
    assert resolved.ingest_result.entry.progress_episodes == 4
    assert (
        db.execute(
            "SELECT status FROM watch_confirmations WHERE id = ?", (inferred.confirmation_id,)
        ).fetchone()[0]
        == "accepted"
    )


def test_confirmation_dismiss_is_owner_scoped_and_does_not_advance(
    db: sqlite3.Connection,
) -> None:
    owner_id, monitor_id = _owner_monitor(db, email="dismiss@example.com", anilist_id=151807)
    other_id, _ = _owner_monitor(db, email="dismiss-other@example.com", anilist_id=2)
    inferred = ingest_watch_event(
        db,
        user_id=owner_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(
            source="apple_tv",
            episode=4,
            confidence="inferred",
            event_key="apple:dismiss:e4",
        ),
        observed_at="2026-08-27T12:00:00Z",
    )
    assert inferred.confirmation_id is not None

    with pytest.raises(WatchServiceError, match="confirmation"):
        resolve_watch_confirmation(
            db,
            user_id=other_id,
            confirmation_id=inferred.confirmation_id,
            accepted=False,
            observed_at="2026-08-27T12:01:00Z",
        )
    resolved = resolve_watch_confirmation(
        db,
        user_id=owner_id,
        confirmation_id=inferred.confirmation_id,
        accepted=False,
        observed_at="2026-08-27T12:01:00Z",
    )

    assert resolved.status == "rejected"
    assert resolved.ingest_result is None
    assert db.execute("SELECT progress_episodes FROM watch_entries").fetchone()[0] == 0


# Completion cleanup intentionally lives in the same watch-event transaction.
def _enable_cleanup(db, owner_id):
    from omakase.plus.completion_cleanup import set_completion_cleanup_preference

    set_completion_cleanup_preference(db, user_id=owner_id, remove_from_plex=True)
    db.commit()


def test_completion_cleanup_default_off_and_owner_isolated(db):
    from omakase.plus.completion_cleanup import get_completion_cleanup_preference

    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    other, other_monitor = _owner_monitor(db, email="other@example.com", anilist_id=2)
    assert not get_completion_cleanup_preference(db, user_id=owner)
    _enable_cleanup(db, owner)
    assert not get_completion_cleanup_preference(db, user_id=other)
    result = ingest_watch_event(
        db,
        user_id=other,
        monitor_id=other_monitor,
        event=WatchEvent.complete(source="manual", event_key="other-complete"),
    )
    assert result.cleanup == {"status": "disabled"}
    assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 0
    result = ingest_watch_event(
        db,
        user_id=owner,
        monitor_id=monitor,
        event=WatchEvent.complete(source="manual", event_key="owner-complete"),
        score_ten=9,
    )
    assert result.cleanup["status"] == "awaiting_plex"
    removal = db.execute("SELECT * FROM media_title_removals").fetchone()
    assert removal["monitor_id"] == monitor and removal["scope"] == "plex_only"
    assert db.execute("SELECT active FROM media_monitors WHERE id=?", (monitor,)).fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT score_ten FROM tracker_sync_outbox WHERE user_id=?", (owner,)
        ).fetchone()[0]
        == 9
    )


@pytest.mark.parametrize("source", ["manual", "plex", "apple_tv", "anilist"])
def test_completion_cleanup_is_only_new_accepted_transition(db, source):
    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    _enable_cleanup(db, owner)
    event = WatchEvent.complete(source=source, event_key="complete")
    first = ingest_watch_event(db, user_id=owner, monitor_id=monitor, event=event)
    assert first.cleanup["status"] == "awaiting_plex"
    second = ingest_watch_event(db, user_id=owner, monitor_id=monitor, event=event)
    assert second.cleanup is None and second.reason == "duplicate_event"
    third = ingest_watch_event(
        db,
        user_id=owner,
        monitor_id=monitor,
        event=WatchEvent.complete(source="manual", event_key="score"),
        score_ten=7,
    )
    assert third.reason == "completion_score_updated" and third.cleanup is None
    assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["migration", "backfill", "inferred", "below_threshold", "keep"])
def test_completion_cleanup_no_backfill_uncertainty_or_optout(db, mode):
    from omakase.plus.completion_cleanup import set_keep_completed_in_plex

    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    _enable_cleanup(db, owner)
    if mode == "backfill":
        db.execute("UPDATE anilist_plannings SET status='COMPLETED' WHERE user_id=?", (owner,))
        db.commit()
    if mode == "keep":
        set_keep_completed_in_plex(db, user_id=owner, monitor_id=monitor, keep=True)
        db.commit()
    event = WatchEvent(
        source="migration" if mode == "migration" else "manual",
        kind="completed",
        event_key="event",
        confidence="inferred" if mode == "inferred" else "exact",
        playback_fraction=0.1 if mode == "below_threshold" else None,
    )
    result = ingest_watch_event(db, user_id=owner, monitor_id=monitor, event=event)
    assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 0
    if mode == "keep":
        assert result.cleanup["status"] == "kept"


def test_completion_cleanup_savepoint_preserves_atomic_outer_rollback(db):
    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    _enable_cleanup(db, owner)
    db.execute("BEGIN")
    result = ingest_watch_event(
        db,
        user_id=owner,
        monitor_id=monitor,
        event=WatchEvent.complete(source="manual", event_key="complete"),
    )
    assert result.cleanup["status"] == "awaiting_plex" and db.in_transaction
    db.rollback()
    for table in ("watch_entries", "watch_events", "tracker_sync_outbox", "media_title_removals"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert db.execute("SELECT active FROM media_monitors WHERE id=?", (monitor,)).fetchone()[0] == 1


def test_completion_cleanup_failure_rolls_back_completion_and_score(db, monkeypatch):
    import omakase.plus.title_removal as removal

    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    _enable_cleanup(db, owner)

    def fail(*args, **kwargs):
        raise RuntimeError("sync insert failed")

    monkeypatch.setattr(removal, "record_sync_run", fail)
    with pytest.raises(RuntimeError, match="sync insert failed"):
        ingest_watch_event(
            db,
            user_id=owner,
            monitor_id=monitor,
            event=WatchEvent.complete(source="manual", event_key="complete"),
            score_ten=8,
        )
    for table in ("watch_entries", "watch_events", "tracker_sync_outbox", "media_title_removals"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_completion_cleanup_policy_boolean_constraints_and_override_ownership(db):
    from omakase.plus.completion_cleanup import (
        set_completion_cleanup_preference,
        set_keep_completed_in_plex,
    )
    from omakase.plus.title_removal import TitleRemovalError

    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    other, _ = _owner_monitor(db, email="other@example.com", anilist_id=2)
    with pytest.raises(ValueError, match="boolean"):
        set_completion_cleanup_preference(db, user_id=owner, remove_from_plex=1)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO user_completion_cleanup_preferences VALUES (?, 2)", (owner,))
    with pytest.raises(TitleRemovalError, match="not found"):
        set_keep_completed_in_plex(db, user_id=other, monitor_id=monitor, keep=True)


def test_completion_cleanup_blocked_intent_can_retry_without_losing_completion(db, monkeypatch):
    import omakase.plus.completion_cleanup as cleanup
    from omakase.plus.title_removal import TitleRemovalError

    owner, monitor = _owner_monitor(db, email="cleanup@example.com", anilist_id=1)
    _enable_cleanup(db, owner)
    original = cleanup.request_title_removal

    def blocked(*args, **kwargs):
        raise TitleRemovalError("A removal with a different scope is already open")

    monkeypatch.setattr(cleanup, "request_title_removal", blocked)
    result = ingest_watch_event(
        db,
        user_id=owner,
        monitor_id=monitor,
        event=WatchEvent.complete(source="manual", event_key="complete"),
        score_ten=8,
    )
    assert result.entry.status == "completed" and result.cleanup["status"] == "blocked"
    block = db.execute("SELECT * FROM completion_plex_cleanup_blocks").fetchone()
    assert block["user_id"] == owner and block["monitor_id"] == monitor
    assert "different scope" in block["detail"]
    assert db.execute("SELECT score_ten FROM tracker_sync_outbox").fetchone()[0] == 8
    monkeypatch.setattr(cleanup, "request_title_removal", original)
    retried = cleanup.retry_completion_cleanup(
        db, user_id=owner, monitor_id=monitor, requested_at="2026-10-11T23:00:00Z"
    )
    assert retried["status"] == "awaiting_plex"
    assert db.execute("SELECT count(*) FROM completion_plex_cleanup_blocks").fetchone()[0] == 0
    with pytest.raises(TitleRemovalError, match="No blocked"):
        cleanup.retry_completion_cleanup(
            db, user_id=owner, monitor_id=monitor, requested_at="2026-10-11T23:01:00Z"
        )
