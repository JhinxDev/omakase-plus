"""A resurfaced completed title continues only through an explicit guarded action."""

import json

import pytest

from omakase.plus import media_store
from omakase.plus.watch import service
from omakase.plus.watch.models import WatchEvent
from tests.plus.test_episode_quality_recheck import asset
from tests.plus.test_viewing_history import db as db


def setup(db):
    monitor = media_store.upsert_monitor(
        db,
        user_id=1,
        anilist_id=21,
        title="One Piece",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=2,
        aired_episodes=2,
    )
    completed = service.ingest_watch_event(
        db,
        user_id=1,
        monitor_id=monitor,
        event=WatchEvent.complete(source="manual", event_key="prior:completion"),
        score_ten=9,
        observed_at="2026-10-28T20:00:00Z",
    )
    db.execute(
        "UPDATE media_monitors SET release_status='RELEASING',expected_episodes=12,aired_episodes=3 WHERE id=?",
        (monitor,),
    )
    asset(db, monitor, "A", (1, 2, 3), "1080p")
    db.commit()
    return monitor, completed.entry


def continue_episode(db, monitor, entry, **kwargs):
    return service.continue_new_episode(
        db,
        user_id=kwargs.pop("user_id", 1),
        monitor_id=monitor,
        episode=kwargs.pop("episode", 3),
        expected_revision=kwargs.pop("revision", entry.revision),
        observed_at="2026-10-29T15:00:00Z",
        **kwargs,
    )


def test_explicit_continuation_extends_current_total_and_preserves_prior_completion_rating(db):
    monitor, prior = setup(db)
    old_events = [tuple(row) for row in db.execute("SELECT * FROM watch_events ORDER BY id")]
    result = continue_episode(db, monitor, prior)
    assert result.disposition == "accepted"
    assert result.entry.status == "watching" and result.entry.progress_episodes == 3
    assert result.entry.episode_total == 12 and result.entry.completed_at is None
    assert [
        tuple(row)
        for row in db.execute(
            "SELECT * FROM watch_events WHERE id<=? ORDER BY id", (len(old_events),)
        )
    ] == old_events
    reopened = db.execute("SELECT payload_json FROM watch_events WHERE kind='reopened'").fetchone()
    assert json.loads(reopened[0])["previous_completed_at"] == "2026-10-28T20:00:00Z"
    assert json.loads(reopened[0])["previous_episode_total"] == 2
    assert (
        db.execute(
            "SELECT score_ten FROM watch_events WHERE event_key=?", ("prior:completion",)
        ).fetchone()[0]
        == 9
    )
    changes = db.total_changes
    replay = continue_episode(db, monitor, prior)
    assert replay.reason == "duplicate_event" and replay.entry.progress_episodes == 3
    assert db.total_changes == changes


@pytest.mark.parametrize(
    "invalid", ["owner", "revision", "skip", "not_usable", "not_aired", "excluded"]
)
def test_continuation_rejects_changed_scope_before_any_watch_write(db, invalid):
    monitor, prior = setup(db)
    kwargs = {}
    if invalid == "owner":
        kwargs["user_id"] = 2
    elif invalid == "revision":
        kwargs["revision"] = prior.revision + 1
    elif invalid == "skip":
        kwargs["episode"] = 4
    elif invalid == "not_usable":
        db.execute(
            "UPDATE media_assets SET usable_coverage_json='[1,2]' WHERE monitor_id=?", (monitor,)
        )
    elif invalid == "not_aired":
        db.execute("UPDATE media_monitors SET aired_episodes=2 WHERE id=?", (monitor,))
    elif invalid == "excluded":
        db.execute(
            "INSERT INTO completion_plex_cleanup_blocks(monitor_id,user_id,requested_at,detail) VALUES(?,1,'2026-10-29T14:00:00Z','Fixture exclusion')",
            (monitor,),
        )
    db.commit()
    changes = db.total_changes
    with pytest.raises(service.WatchServiceError):
        continue_episode(db, monitor, prior, **kwargs)
    assert db.total_changes == changes


def test_unknown_ongoing_total_does_not_auto_complete_the_newly_aired_episode(db):
    monitor, prior = setup(db)
    db.execute("UPDATE media_monitors SET expected_episodes=NULL WHERE id=?", (monitor,))
    db.commit()
    result = continue_episode(db, monitor, prior)
    assert result.entry.status == "watching" and result.entry.episode_total is None


def test_continuation_rolls_back_total_reopen_and_events_on_outbox_failure(db, monkeypatch):
    from omakase.plus.watch import repository

    monitor, prior = setup(db)
    before = [tuple(row) for row in db.execute("SELECT * FROM watch_entries")]
    events = [tuple(row) for row in db.execute("SELECT * FROM watch_events")]

    def fail(*args, **kwargs):
        raise RuntimeError("fixture outbox failure")

    monkeypatch.setattr(repository, "insert_tracker_outbox", fail)
    with pytest.raises(RuntimeError, match="fixture outbox"):
        continue_episode(db, monitor, prior)
    assert [tuple(row) for row in db.execute("SELECT * FROM watch_entries")] == before
    assert [tuple(row) for row in db.execute("SELECT * FROM watch_events")] == events
