"""Owner-scoped, default-off completion cleanup policy without transaction commits."""

from __future__ import annotations

import sqlite3
from typing import Any

from omakase.plus.title_removal import TitleRemovalError, request_title_removal


def get_completion_cleanup_preference(db: sqlite3.Connection, *, user_id: int) -> bool:
    row = db.execute(
        "SELECT remove_from_plex FROM user_completion_cleanup_preferences WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    return bool(row[0]) if row else False


def set_completion_cleanup_preference(
    db: sqlite3.Connection, *, user_id: int, remove_from_plex: bool
) -> None:
    if not isinstance(remove_from_plex, bool):
        raise ValueError("remove_from_plex must be a boolean")
    db.execute(
        """INSERT INTO user_completion_cleanup_preferences (user_id, remove_from_plex)
           VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE
           SET remove_from_plex = excluded.remove_from_plex""",
        (user_id, int(remove_from_plex)),
    )


def set_keep_completed_in_plex(
    db: sqlite3.Connection, *, user_id: int, monitor_id: int, keep: bool
) -> None:
    if not isinstance(keep, bool):
        raise ValueError("keep must be a boolean")
    cursor = db.execute(
        """UPDATE media_monitors SET keep_completed_in_plex = ?
           WHERE id = ? AND user_id = ?""",
        (int(keep), monitor_id, user_id),
    )
    if cursor.rowcount != 1:
        raise TitleRemovalError("Media title not found")


def queue_completion_cleanup(
    db: sqlite3.Connection, *, user_id: int, monitor_id: int, requested_at: str
) -> dict[str, Any]:
    """Called only for a newly accepted completion, inside the watch transaction."""
    monitor = db.execute(
        """SELECT title, keep_completed_in_plex FROM media_monitors
           WHERE id = ? AND user_id = ?""",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        raise TitleRemovalError("Media title not found")
    if not get_completion_cleanup_preference(db, user_id=user_id):
        return {"status": "disabled"}
    if monitor["keep_completed_in_plex"]:
        return {"status": "kept", "detail": "Kept in Plex for this title"}
    try:
        result = request_title_removal(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            confirmation=str(monitor["title"]),
            requested_at=requested_at,
            scope="plex_only",
            commit=False,
        )
    except TitleRemovalError as exc:
        detail = str(exc)[:500]
        db.execute(
            """INSERT INTO completion_plex_cleanup_blocks
               (monitor_id, user_id, requested_at, detail) VALUES (?, ?, ?, ?)
               ON CONFLICT(monitor_id) DO UPDATE SET detail = excluded.detail""",
            (monitor_id, user_id, requested_at, detail),
        )
        db.execute(
            """UPDATE media_monitors SET active = 0, next_check_at = NULL,
               last_result = 'COMPLETION_CLEANUP_BLOCKED' WHERE id = ? AND user_id = ?""",
            (monitor_id, user_id),
        )
        return {"status": "blocked", "detail": detail}
    db.execute("DELETE FROM completion_plex_cleanup_blocks WHERE monitor_id = ?", (monitor_id,))
    return {**result, "detail": "Waiting for native Plex absence proof; source media retained"}


def retry_completion_cleanup(
    db: sqlite3.Connection, *, user_id: int, monitor_id: int, requested_at: str
) -> dict[str, Any]:
    """Retry only an existing blocked, still-completed owner title; never sweep history."""
    row = db.execute(
        """SELECT 1 FROM completion_plex_cleanup_blocks block
           JOIN watch_entries entry ON entry.monitor_id = block.monitor_id
             AND entry.user_id = block.user_id
           WHERE block.monitor_id = ? AND block.user_id = ? AND entry.status = 'completed'""",
        (monitor_id, user_id),
    ).fetchone()
    if row is None:
        raise TitleRemovalError("No blocked completed cleanup exists for this title")
    return queue_completion_cleanup(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        requested_at=requested_at,
    )
