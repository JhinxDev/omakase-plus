"""SQLite persistence boundaries for watch progress."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from omakase.plus.watch.models import TrackerOutboxItem, WatchDecision, WatchEntry, WatchEvent


class WatchRepositoryError(ValueError):
    """Watch persistence input is invalid or outside owner scope."""


def _positive_id(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise WatchRepositoryError(f"{name} must be a positive integer")
    return value


def _entry(row: sqlite3.Row) -> WatchEntry:
    return WatchEntry(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        monitor_id=int(row["monitor_id"]) if row["monitor_id"] is not None else None,
        anilist_id=int(row["anilist_id"]),
        season_number=int(row["season_number"]),
        status=row["status"],
        progress_episodes=int(row["progress_episodes"]),
        episode_total=int(row["episode_total"]) if row["episode_total"] is not None else None,
        last_source=row["last_source"],
        confidence=row["confidence"],
        last_observed_at=row["last_observed_at"],
        completed_at=row["completed_at"],
        revision=int(row["revision"]),
    )


def get_watch_entry(db: sqlite3.Connection, *, user_id: int, monitor_id: int) -> WatchEntry | None:
    """Return one owner-scoped projection or None."""
    _positive_id(user_id, name="user_id")
    _positive_id(monitor_id, name="monitor_id")
    row = db.execute(
        "SELECT * FROM watch_entries WHERE user_id = ? AND monitor_id = ?",
        (user_id, monitor_id),
    ).fetchone()
    return _entry(row) if row is not None else None


def get_watch_entry_by_id(
    db: sqlite3.Connection, *, user_id: int, entry_id: int
) -> WatchEntry | None:
    """Return one projection by its owner-scoped identifier."""
    _positive_id(user_id, name="user_id")
    _positive_id(entry_id, name="entry_id")
    row = db.execute(
        "SELECT * FROM watch_entries WHERE user_id = ? AND id = ?",
        (user_id, entry_id),
    ).fetchone()
    return _entry(row) if row is not None else None


def ensure_watch_entry(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
    created_at: str,
) -> WatchEntry:
    """Load or create a projection from one owner-scoped media monitor."""
    existing = get_watch_entry(db, user_id=user_id, monitor_id=monitor_id)
    if existing is not None:
        return existing
    monitor = db.execute(
        """SELECT monitor.*, planning.status AS planning_status
           FROM media_monitors AS monitor
           LEFT JOIN anilist_plannings AS planning
             ON planning.id = monitor.anilist_planning_id
           WHERE monitor.id = ? AND monitor.user_id = ?""",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        raise WatchRepositoryError("monitor does not exist for this user")
    completed = str(monitor["planning_status"] or "").upper() == "COMPLETED"
    if not completed:
        completed = (
            db.execute(
                """SELECT 1 FROM tracker_media_actions
                   WHERE user_id = ? AND media_id = ? AND action = 'completed'
                   LIMIT 1""",
                (user_id, int(monitor["anilist_id"])),
            ).fetchone()
            is not None
        )
    expected = int(monitor["expected_episodes"] or 0)
    episode_total = expected if expected > 0 else None
    progress = expected if completed and expected > 0 else 0
    cursor = db.execute(
        """INSERT INTO watch_entries
           (user_id, monitor_id, anilist_id, season_number, status,
            progress_episodes, episode_total, last_source, confidence,
            completed_at, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'migration', 'manual', ?, ?, ?)""",
        (
            user_id,
            monitor_id,
            int(monitor["anilist_id"]),
            int(monitor["season_number"] or 0),
            "completed" if completed else "unwatched",
            progress,
            episode_total,
            created_at if completed else None,
            created_at,
            created_at,
        ),
    )
    row = db.execute("SELECT * FROM watch_entries WHERE id = ?", (cursor.lastrowid,)).fetchone()
    if row is None:
        raise RuntimeError("watch entry insert did not produce a row")
    return _entry(row)


def find_watch_event(
    db: sqlite3.Connection, *, user_id: int, source: str, event_key: str
) -> dict[str, object] | None:
    """Find a prior owner/source event receipt for replay protection."""
    row = db.execute(
        """SELECT * FROM watch_events
           WHERE user_id = ? AND source = ? AND event_key = ?""",
        (user_id, source, event_key),
    ).fetchone()
    return dict(row) if row is not None else None


def list_watch_events(
    db: sqlite3.Connection, *, user_id: int, entry_id: int, limit: int = 50
) -> list[dict[str, object]]:
    """Return newest-first owner-scoped event receipts."""
    _positive_id(user_id, name="user_id")
    _positive_id(entry_id, name="entry_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise WatchRepositoryError("limit must be between 1 and 200")
    rows = db.execute(
        """SELECT event.* FROM watch_events AS event
           JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
           WHERE event.user_id = ? AND entry.user_id = ? AND entry.id = ?
           ORDER BY event.created_at DESC, event.id DESC LIMIT ?""",
        (user_id, user_id, entry_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def list_pending_confirmations(
    db: sqlite3.Connection, *, user_id: int, limit: int = 20
) -> list[dict[str, object]]:
    """Return unresolved owner-scoped confirmations."""
    _positive_id(user_id, name="user_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WatchRepositoryError("limit must be between 1 and 100")
    rows = db.execute(
        """SELECT confirmation.*, entry.anilist_id, entry.season_number,
                  event.source, event.event_key
           FROM watch_confirmations AS confirmation
           JOIN watch_entries AS entry ON entry.id = confirmation.watch_entry_id
           JOIN watch_events AS event ON event.id = confirmation.watch_event_id
           WHERE confirmation.user_id = ? AND entry.user_id = ?
             AND confirmation.status = 'pending'
           ORDER BY confirmation.created_at DESC, confirmation.id DESC LIMIT ?""",
        (user_id, user_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_pending_confirmation(
    db: sqlite3.Connection, *, user_id: int, confirmation_id: int
) -> dict[str, object] | None:
    """Load one unresolved confirmation through its owner-scoped watch entry."""
    _positive_id(user_id, name="user_id")
    _positive_id(confirmation_id, name="confirmation_id")
    row = db.execute(
        """SELECT confirmation.*, entry.monitor_id, entry.progress_episodes,
                  event.source, event.event_key
           FROM watch_confirmations AS confirmation
           JOIN watch_entries AS entry ON entry.id = confirmation.watch_entry_id
           JOIN watch_events AS event ON event.id = confirmation.watch_event_id
           WHERE confirmation.id = ? AND confirmation.user_id = ?
             AND entry.user_id = ? AND confirmation.status = 'pending'""",
        (confirmation_id, user_id, user_id),
    ).fetchone()
    return dict(row) if row is not None else None


def mark_watch_confirmation_resolved(
    db: sqlite3.Connection,
    *,
    user_id: int,
    confirmation_id: int,
    status: str,
    resolved_at: str,
) -> None:
    """Resolve exactly one still-pending owner confirmation."""
    if status not in {"accepted", "rejected"}:
        raise WatchRepositoryError("confirmation status is invalid")
    cursor = db.execute(
        """UPDATE watch_confirmations
           SET status = ?, resolved_at = ?, updated_at = ?
           WHERE id = ? AND user_id = ? AND status = 'pending'""",
        (status, resolved_at, resolved_at, confirmation_id, user_id),
    )
    if cursor.rowcount != 1:
        raise WatchRepositoryError("watch confirmation is no longer pending")


def insert_watch_event(
    db: sqlite3.Connection,
    *,
    user_id: int,
    entry_id: int,
    event: WatchEvent,
    decision: WatchDecision,
    observed_at: str,
    score_ten: int | None = None,
) -> int:
    cursor = db.execute(
        """INSERT INTO watch_events
           (watch_entry_id, user_id, source, event_key, kind, episode,
            playback_fraction, confidence, force, disposition, reason, observed_at, score_ten)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            entry_id,
            user_id,
            event.source,
            event.event_key,
            event.kind,
            event.episode,
            event.playback_fraction,
            event.confidence,
            int(event.force),
            decision.disposition,
            decision.reason,
            observed_at,
            score_ten,
        ),
    )
    return int(cursor.lastrowid)


def update_watch_entry(
    db: sqlite3.Connection, *, previous: WatchEntry, updated: WatchEntry
) -> None:
    if previous.id is None or previous.user_id is None:
        raise WatchRepositoryError("persisted watch entry identity is missing")
    cursor = db.execute(
        """UPDATE watch_entries
           SET status = ?, progress_episodes = ?, episode_total = ?, last_source = ?,
               confidence = ?, last_observed_at = ?, completed_at = ?, revision = ?,
               updated_at = ?
           WHERE id = ? AND user_id = ? AND revision = ?""",
        (
            updated.status,
            updated.progress_episodes,
            updated.episode_total,
            updated.last_source,
            updated.confidence,
            updated.last_observed_at,
            updated.completed_at,
            updated.revision,
            updated.last_observed_at,
            previous.id,
            previous.user_id,
            previous.revision,
        ),
    )
    if cursor.rowcount != 1:
        raise WatchRepositoryError("watch entry changed concurrently")


def insert_watch_confirmation(
    db: sqlite3.Connection,
    *,
    user_id: int,
    entry_id: int,
    event_id: int,
    proposed_episode: int | None,
    prompt: str,
) -> int:
    cursor = db.execute(
        """INSERT INTO watch_confirmations
           (watch_entry_id, watch_event_id, user_id, proposed_episode, prompt)
           VALUES (?, ?, ?, ?, ?)""",
        (entry_id, event_id, user_id, proposed_episode, prompt),
    )
    return int(cursor.lastrowid)


def insert_tracker_outbox(
    db: sqlite3.Connection,
    *,
    user_id: int,
    entry_id: int,
    event_id: int,
    action: str,
    progress_episodes: int,
    score_ten: int | None = None,
) -> int:
    cursor = db.execute(
        """INSERT INTO tracker_sync_outbox
           (watch_entry_id, watch_event_id, user_id, tracker, action,
            progress_episodes, score_ten)
           VALUES (?, ?, ?, 'anilist', ?, ?, ?)""",
        (entry_id, event_id, user_id, action, progress_episodes, score_ten),
    )
    return int(cursor.lastrowid)


def _outbox_item(row: sqlite3.Row) -> TrackerOutboxItem:
    return TrackerOutboxItem(
        id=int(row["id"]),
        watch_entry_id=int(row["watch_entry_id"]),
        watch_event_id=int(row["watch_event_id"]),
        user_id=int(row["user_id"]),
        tracker=row["tracker"],
        action=row["action"],
        progress_episodes=int(row["progress_episodes"]),
        sync_state=row["sync_state"],
        attempt_count=int(row["attempt_count"]),
        lease_token=row["lease_token"],
        score_ten=row["score_ten"],
    )


def claim_tracker_outbox(
    db: sqlite3.Connection,
    *,
    limit: int,
    now: str,
    outbox_id: int | None = None,
    attempted_only: bool = False,
) -> list[TrackerOutboxItem]:
    """Atomically lease eligible tracker work in creation order."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise WatchRepositoryError("limit must be between 1 and 100")
    if outbox_id is not None and (
        isinstance(outbox_id, bool) or not isinstance(outbox_id, int) or outbox_id <= 0
    ):
        raise WatchRepositoryError("outbox_id must be a positive integer")
    if not isinstance(attempted_only, bool):
        raise WatchRepositoryError("attempted_only must be a boolean")
    if db.in_transaction:
        raise WatchRepositoryError("outbox claim requires a clean transaction boundary")
    expired_before = (
        datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        - timedelta(minutes=10)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("BEGIN IMMEDIATE")
    try:
        ids = [
            int(row["id"])
            for row in db.execute(
                """SELECT id FROM tracker_sync_outbox
                   WHERE ((sync_state IN ('pending', 'failed')
                           AND (not_before IS NULL OR not_before <= ?))
                          OR (sync_state = 'leased' AND leased_at < ?))
                     AND (? IS NULL OR id = ?)
                     AND (? = 0 OR attempt_count > 0)
                   ORDER BY created_at, id LIMIT ?""",
                (now, expired_before, outbox_id, outbox_id, int(attempted_only), limit),
            ).fetchall()
        ]
        claimed: list[TrackerOutboxItem] = []
        for item_id in ids:
            lease_token = secrets.token_urlsafe(24)
            cursor = db.execute(
                """UPDATE tracker_sync_outbox
                   SET sync_state = 'leased', lease_token = ?, leased_at = ?,
                       attempt_count = attempt_count + 1, last_attempt_at = ?,
                       updated_at = ?
                   WHERE id = ? AND (sync_state IN ('pending', 'failed')
                       OR (sync_state = 'leased' AND leased_at < ?))""",
                (lease_token, now, now, now, item_id, expired_before),
            )
            if cursor.rowcount != 1:
                continue
            row = db.execute(
                "SELECT * FROM tracker_sync_outbox WHERE id = ?", (item_id,)
            ).fetchone()
            if row is not None:
                claimed.append(_outbox_item(row))
        db.commit()
        return claimed
    except Exception:
        db.rollback()
        raise


def get_tracker_outbox_target(
    db: sqlite3.Connection, *, item: TrackerOutboxItem
) -> dict[str, object]:
    """Return the exact leased owner/media target or fail closed."""
    row = db.execute(
        """SELECT outbox.id, entry.anilist_id, entry.progress_episodes, entry.status,
                  EXISTS (
                      SELECT 1 FROM tracker_sync_outbox AS newer
                      WHERE newer.watch_entry_id = outbox.watch_entry_id
                        AND newer.user_id = outbox.user_id
                        AND newer.id > outbox.id AND newer.action = 'complete'
                  ) AS newer_completion_exists
           FROM tracker_sync_outbox AS outbox
           JOIN watch_entries AS entry ON entry.id = outbox.watch_entry_id
           WHERE outbox.id = ? AND outbox.user_id = ?
             AND outbox.lease_token = ? AND outbox.sync_state = 'leased'
             AND entry.user_id = outbox.user_id""",
        (item.id, item.user_id, item.lease_token),
    ).fetchone()
    if row is None:
        raise WatchRepositoryError("tracker outbox lease is no longer valid")
    return dict(row)


def mark_tracker_outbox_synced(
    db: sqlite3.Connection,
    *,
    item: TrackerOutboxItem,
    remote_entry_id: int,
    synced_at: str,
) -> None:
    """Finalize a lease only after an exact remote receipt."""
    _positive_id(remote_entry_id, name="remote_entry_id")
    cursor = db.execute(
        """UPDATE tracker_sync_outbox
           SET sync_state = 'synced', remote_entry_id = ?, last_error = '',
               not_before = NULL, lease_token = NULL, leased_at = NULL,
               synced_at = ?, updated_at = ?
           WHERE id = ? AND user_id = ? AND lease_token = ?
             AND sync_state = 'leased'""",
        (
            remote_entry_id,
            synced_at,
            synced_at,
            item.id,
            item.user_id,
            item.lease_token,
        ),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise WatchRepositoryError("tracker outbox lease is no longer valid")
    db.commit()


def mark_tracker_outbox_failed(
    db: sqlite3.Connection,
    *,
    item: TrackerOutboxItem,
    detail: str,
    updated_at: str,
    not_before: str | None,
    conflict: bool = False,
) -> None:
    """Release a lease into bounded retry or terminal conflict state."""
    cursor = db.execute(
        """UPDATE tracker_sync_outbox
           SET sync_state = ?, last_error = ?, not_before = ?,
               lease_token = NULL, leased_at = NULL, updated_at = ?
           WHERE id = ? AND user_id = ? AND lease_token = ?
             AND sync_state = 'leased'""",
        (
            "conflict" if conflict else "failed",
            detail,
            not_before,
            updated_at,
            item.id,
            item.user_id,
            item.lease_token,
        ),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise WatchRepositoryError("tracker outbox lease is no longer valid")
    db.commit()
