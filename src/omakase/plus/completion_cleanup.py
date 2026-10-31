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
    db: sqlite3.Connection, *, user_id: int, monitor_id: int, requested_at: str, now=None
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
    if has_active_playback_hold(db, user_id=user_id, monitor_id=monitor_id, now=now):
        db.execute(
            "INSERT INTO deferred_completion_cleanup(monitor_id,user_id,requested_at) VALUES(?,?,?) ON CONFLICT(monitor_id) DO NOTHING",
            (monitor_id, user_id, requested_at),
        )
        return {"status": "deferred", "detail": "Waiting for active web playback to finish"}
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


def has_active_playback_hold(db, *, user_id, monitor_id, now=None):
    """Conservative media hold, never a playback authorization grant."""
    import hashlib
    import time
    from datetime import datetime, timezone

    from omakase.plus import connections, households
    from omakase.plus.web_playback_store import active_media_holds

    monitor = db.execute(
        "SELECT anilist_id,season_number FROM media_monitors WHERE id=? AND user_id=?",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        return False
    # A worker may still read the original while its browser disconnects. The
    # durable RUNNING lease survives until process absence is actually proved.
    # This holds deletion only; it never authorizes a media request.
    if db.execute(
        "SELECT 1 FROM web_playback_remux j JOIN web_playback_resumes r "
        "ON r.media_version=j.media_version AND r.user_id=j.actor_user_id "
        "JOIN libraries l ON l.id=r.library_id WHERE j.state='RUNNING' "
        "AND l.owner_user_id=? AND r.canonical_id=? AND r.season_number=? LIMIT 1",
        (user_id, str(monitor["anilist_id"]), monitor["season_number"]),
    ).fetchone():
        return True
    for hold in active_media_holds(db, now=now):
        try:
            scope = households.resolve_scope(
                db, viewer_id=hold["user_id"], library_id=hold["library_id"]
            )
        except households.HouseholdError:
            # A revoked viewer has no valid session capable of holding media.
            continue
        clock = time.time() if now is None else now
        token = connections._read(db, hold["user_id"], "plex_token")
        if not token:
            continue
        credential_generation = hashlib.sha256(token.encode()).hexdigest()
        # Auth IDs remain local opaque values. Inspect only a bounded newest set,
        # without invoking authentication (which may renew or delete sessions).
        bindings = set()
        utc_now = datetime.fromtimestamp(clock, timezone.utc)
        for auth in db.execute(
            "SELECT id,expires_at FROM sessions WHERE user_id=? ORDER BY rowid DESC LIMIT 4096",
            (hold["user_id"],),
        ):
            try:
                expires = datetime.fromisoformat(auth["expires_at"])
                if expires.tzinfo is not None and expires > utc_now:
                    bindings.add(hashlib.sha256(auth["id"].encode()).hexdigest())
            except (TypeError, ValueError, OverflowError):
                continue
        if not bindings:
            continue
        valid = db.execute(
            "SELECT s.binding_digest FROM web_playback_sessions s JOIN web_playback_resumes r USING(target_key) "
            "WHERE r.user_id=? AND r.library_id=? AND r.media_version=? "
            "AND r.canonical_id=? AND r.season_number=? AND r.episode_number=? "
            "AND s.closed=0 AND s.expires_at>? AND s.last_seen>? AND s.revision=r.revision "
            "AND json_extract(s.snapshot_json,'$.membership.library_id')=? "
            "AND json_extract(s.snapshot_json,'$.membership.viewer_id')=? "
            "AND json_extract(s.snapshot_json,'$.membership.revision')=? "
            "AND json_extract(s.snapshot_json,'$.policy.revision')=? "
            "AND json_extract(s.snapshot_json,'$.source.credential_generation')=? LIMIT 4096",
            (
                hold["user_id"],
                hold["library_id"],
                hold["media_version"],
                hold["canonical_id"],
                hold["season_number"],
                hold["episode_number"],
                clock,
                clock - 1200,
                scope.library_id,
                scope.viewer_id,
                scope.membership_revision,
                scope.policy_revision,
                credential_generation,
            ),
        ).fetchall()
        if not any(row[0] in bindings for row in valid):
            continue
        owner = db.execute(
            "SELECT owner_user_id FROM libraries WHERE id=?", (hold["library_id"],)
        ).fetchone()
        if (
            owner
            and owner[0] == user_id
            and hold["domain"] == "anime"
            and hold["canonical_id"] == str(monitor["anilist_id"])
            and hold["season_number"] == monitor["season_number"]
        ):
            return True
    return False


def process_deferred_completion_cleanup(db, *, now=None, limit=20):
    """Retry bounded deferred completions after the final writer closes/expires."""
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Choose a cleanup limit from 1 to 20")
    handled = []
    rows = db.execute(
        "SELECT * FROM deferred_completion_cleanup ORDER BY requested_at,monitor_id LIMIT ?",
        (limit,),
    ).fetchall()
    for row in rows:
        if has_active_playback_hold(
            db, user_id=row["user_id"], monitor_id=row["monitor_id"], now=now
        ):
            continue
        completed = db.execute(
            "SELECT 1 FROM watch_entries WHERE user_id=? AND monitor_id=? AND status='completed'",
            (row["user_id"], row["monitor_id"]),
        ).fetchone()
        if completed:
            result = queue_completion_cleanup(
                db,
                user_id=row["user_id"],
                monitor_id=row["monitor_id"],
                requested_at=row["requested_at"],
                now=now,
            )
            if result["status"] == "deferred":
                continue
        db.execute(
            "DELETE FROM deferred_completion_cleanup WHERE monitor_id=? AND user_id=?",
            (row["monitor_id"], row["user_id"]),
        )
        handled.append(row["monitor_id"])
    return handled
