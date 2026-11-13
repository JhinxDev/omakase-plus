"""Durable anime-list action outbox tests."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.tracker import (
    TrackerActionError,
    enqueue_completed,
    get_action,
    mark_attempt,
    mark_failed,
    mark_synced,
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


def test_completion_action_is_idempotent_retryable_and_score_aware(db):
    user_id = _user(db, "owner@example.com")
    first = enqueue_completed(
        db,
        user_id=user_id,
        tracker="anilist",
        media_id=180560,
        title="Hell's Paradise Season 2",
        score_ten=8.5,
    )
    mark_attempt(
        db,
        user_id=user_id,
        action_id=first.id,
        attempted_at="2026-07-26T12:00:00Z",
    )
    mark_synced(
        db,
        user_id=user_id,
        action_id=first.id,
        remote_entry_id=77,
        synced_at="2026-07-26T12:00:01Z",
    )

    unchanged = enqueue_completed(
        db,
        user_id=user_id,
        tracker="anilist",
        media_id=180560,
        title="Hell's Paradise Season 2",
        score_ten=8.5,
    )
    changed = enqueue_completed(
        db,
        user_id=user_id,
        tracker="anilist",
        media_id=180560,
        title="Hell's Paradise Season 2",
        score_ten=9,
    )

    assert unchanged.sync_state == "synced"
    assert changed.id == first.id
    assert changed.sync_state == "pending"
    assert changed.score_ten == 9
    assert changed.attempt_count == 1


def test_failed_detail_is_bounded_redacted_and_owner_scoped(db):
    owner_id = _user(db, "owner@example.com")
    stranger_id = _user(db, "stranger@example.com")
    action = enqueue_completed(
        db,
        user_id=owner_id,
        tracker="anilist",
        media_id=1,
        title="Cowboy Bebop",
        score_ten=None,
    )
    mark_failed(
        db,
        user_id=owner_id,
        action_id=action.id,
        detail="Bearer abc.def API_KEY=sk-private failed",
    )
    row = db.execute(
        "SELECT sync_state, last_error FROM tracker_media_actions WHERE id = ?",
        (action.id,),
    ).fetchone()

    assert row["sync_state"] == "failed"
    assert "abc.def" not in row["last_error"]
    assert "sk-private" not in row["last_error"]
    assert get_action(db, user_id=stranger_id, tracker="anilist", media_id=1) is None
    with pytest.raises(TrackerActionError, match="does not exist"):
        mark_attempt(db, user_id=stranger_id, action_id=action.id)


@pytest.mark.parametrize("score", [0, 10.1, True, "8"])
def test_completion_action_rejects_invalid_scores(db, score):
    user_id = _user(db, "owner@example.com")
    with pytest.raises(TrackerActionError, match="score"):
        enqueue_completed(
            db,
            user_id=user_id,
            tracker="anilist",
            media_id=1,
            title="Cowboy Bebop",
            score_ten=score,
        )
