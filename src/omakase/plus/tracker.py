"""Durable, user-scoped outbox for anime-list mutations."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

_TRACKERS = frozenset({"anilist", "myanimelist"})
_TOKEN_RE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:token|key|secret|password)[a-z0-9_-]*)"
    r"\s*[:=]\s*[^\s,;&]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]+=*")


class TrackerActionError(ValueError):
    """A tracker action is invalid or outside the user's ownership."""


@dataclass(frozen=True)
class TrackerAction:
    id: int
    user_id: int
    tracker: str
    media_id: int
    title: str
    action: str
    score_ten: float | None
    sync_state: str
    attempt_count: int


def enqueue_completed(
    db: sqlite3.Connection,
    *,
    user_id: int,
    tracker: str,
    media_id: int,
    title: str,
    score_ten: float | None,
) -> TrackerAction:
    """Upsert a completion intent before attempting a remote mutation."""
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise TrackerActionError("user_id must be a positive integer")
    if tracker not in _TRACKERS:
        raise TrackerActionError("tracker is unsupported")
    if isinstance(media_id, bool) or not isinstance(media_id, int) or media_id <= 0:
        raise TrackerActionError("media_id must be a positive integer")
    normalized_title = " ".join(str(title).split()).strip()
    if not 1 <= len(normalized_title) <= 300:
        raise TrackerActionError("title must be between 1 and 300 characters")
    normalized_score = _score(score_ten)
    db.execute(
        """INSERT INTO tracker_media_actions
           (user_id, tracker, media_id, title, action, score_ten, sync_state)
           VALUES (?, ?, ?, ?, 'completed', ?, 'pending')
           ON CONFLICT(user_id, tracker, media_id, action) DO UPDATE SET
               title = excluded.title,
               score_ten = excluded.score_ten,
               sync_state = CASE
                   WHEN tracker_media_actions.sync_state = 'synced'
                        AND tracker_media_actions.score_ten IS excluded.score_ten
                   THEN 'synced'
                   ELSE 'pending'
               END,
               last_error = '',
               updated_at = datetime('now')""",
        (user_id, tracker, media_id, normalized_title, normalized_score),
    )
    row = db.execute(
        """SELECT * FROM tracker_media_actions
           WHERE user_id = ? AND tracker = ? AND media_id = ? AND action = 'completed'""",
        (user_id, tracker, media_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("tracker action upsert did not produce a row")
    db.commit()
    return _action(row)


def mark_attempt(
    db: sqlite3.Connection,
    *,
    user_id: int,
    action_id: int,
    attempted_at: str | None = None,
) -> None:
    cursor = db.execute(
        """UPDATE tracker_media_actions
           SET attempt_count = attempt_count + 1,
               last_attempt_at = ?,
               updated_at = datetime('now')
           WHERE id = ? AND user_id = ?""",
        (attempted_at or _utc_now(), action_id, user_id),
    )
    if cursor.rowcount != 1:
        raise TrackerActionError("tracker action does not exist for this user")
    db.commit()


def mark_synced(
    db: sqlite3.Connection,
    *,
    user_id: int,
    action_id: int,
    remote_entry_id: int,
    synced_at: str | None = None,
) -> None:
    if (
        isinstance(remote_entry_id, bool)
        or not isinstance(remote_entry_id, int)
        or remote_entry_id <= 0
    ):
        raise TrackerActionError("remote entry id is invalid")
    timestamp = synced_at or _utc_now()
    cursor = db.execute(
        """UPDATE tracker_media_actions
           SET sync_state = 'synced', remote_entry_id = ?, last_error = '',
               synced_at = ?, updated_at = datetime('now')
           WHERE id = ? AND user_id = ?""",
        (remote_entry_id, timestamp, action_id, user_id),
    )
    if cursor.rowcount != 1:
        raise TrackerActionError("tracker action does not exist for this user")
    db.commit()


def mark_failed(
    db: sqlite3.Connection,
    *,
    user_id: int,
    action_id: int,
    detail: str,
    unsupported: bool = False,
) -> None:
    safe_detail = safe_tracker_detail(detail)
    cursor = db.execute(
        """UPDATE tracker_media_actions
           SET sync_state = ?, last_error = ?, updated_at = datetime('now')
           WHERE id = ? AND user_id = ?""",
        ("unsupported" if unsupported else "failed", safe_detail, action_id, user_id),
    )
    if cursor.rowcount != 1:
        raise TrackerActionError("tracker action does not exist for this user")
    db.commit()


def get_action(
    db: sqlite3.Connection,
    *,
    user_id: int,
    tracker: str,
    media_id: int,
) -> TrackerAction | None:
    row = db.execute(
        """SELECT * FROM tracker_media_actions
           WHERE user_id = ? AND tracker = ? AND media_id = ?
             AND action = 'completed'""",
        (user_id, tracker, media_id),
    ).fetchone()
    return _action(row) if row is not None else None


def get_action_by_id(
    db: sqlite3.Connection,
    *,
    user_id: int,
    action_id: int,
) -> TrackerAction | None:
    row = db.execute(
        "SELECT * FROM tracker_media_actions WHERE id = ? AND user_id = ?",
        (action_id, user_id),
    ).fetchone()
    return _action(row) if row is not None else None


def _action(row) -> TrackerAction:
    return TrackerAction(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        tracker=str(row["tracker"]),
        media_id=int(row["media_id"]),
        title=str(row["title"]),
        action=str(row["action"]),
        score_ten=float(row["score_ten"]) if row["score_ten"] is not None else None,
        sync_state=str(row["sync_state"]),
        attempt_count=int(row["attempt_count"]),
    )


def _score(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TrackerActionError("score must be a number from 1 to 10")
    normalized = round(float(value), 1)
    if not 1 <= normalized <= 10:
        raise TrackerActionError("score must be a number from 1 to 10")
    return normalized


def safe_tracker_detail(value: object) -> str:
    """Bound and redact tracker failures before durable storage."""
    detail = " ".join(str(value or "").split()).strip()
    detail = _BEARER_RE.sub("Bearer <redacted>", detail)
    detail = _TOKEN_RE.sub(lambda match: f"{match.group(1)}=<redacted>", detail)
    return detail[:240]


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
