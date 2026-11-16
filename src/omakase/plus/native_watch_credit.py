"""Durable anime completion intents for the Apple TV client.

The audit reproduced a completion that was neither saved nor retryable: the anime
ingest failed after the resume checkpoint had committed, the endpoint answered
200, and the client's exact retry returned early because the checkpoint was
already stored. The watch credit was simply gone.

Two properties make that impossible now:

* **The intent is durable and atomic with the checkpoint.** It is written inside
  the same transaction that saves the resume position, so either both exist or
  neither does. A completed playback always leaves a claim that it happened.
* **The credit is independently retryable and idempotent.** The intent carries the
  watch event key the ingest already deduplicates on, so replaying it cannot
  double-credit. A retry is attempted on the duplicate checkpoint path, on close
  and on later authenticated playback boundaries, even after the pass is gone.

Only an anime completion needs this. Movie and television history is plain SQL that
joins the resume transaction, so those were already all-or-nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

STATUS_PENDING = "pending"
STATUS_CREDITED = "credited"

# A completion is worth crediting at the same fraction the projection uses.
_CREDIT_FRACTION = 0.85

# Allow the immediate duplicate and close paths through once. Thereafter defer
# retries up to one hour so a long outage preserves its intents without retrying
# on every checkpoint. There is no terminal attempt count for a pending intent.
_RETRY_DELAY = "CASE WHEN attempts < 2 THEN 0 ELSE MIN(3600, 1 << MIN(attempts, 12)) END"


@dataclass(frozen=True, slots=True)
class Intent:
    id: int
    user_id: int
    domain: str
    canonical_id: str
    season_number: int
    episode_number: int
    monitor_id: int | None
    event_key: str
    playback_fraction: float
    observed_at: str
    status: str
    attempts: int
    next_attempt_at: int


def completion_fraction(duration_seconds, position) -> float | None:
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        return None
    try:
        fraction = min(1.0, max(0.0, float(position) / float(duration_seconds)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return fraction if fraction >= _CREDIT_FRACTION else None


def record(db, grant, *, fraction, observed_at: str, now: int) -> int | None:
    """Remember that this anime episode was watched, inside the caller's transaction.

    Returns the intent id, or ``None`` when this completion needs no durable intent
    (a lower fraction, or a domain whose history already joins the transaction).
    Re-recording the same episode updates one row: the unique key is the identity
    the watch event itself uses.
    """
    target = getattr(grant, "target", None)
    monitor_id = getattr(grant, "anime_monitor_id", None)
    if target is None or getattr(target, "domain", "") != "anime" or monitor_id is None:
        return None
    event_key = (
        f"native-tv:{target.user_id}:{target.canonical_id}:"
        f"{target.season_number}:{target.episode_number}"
    )
    db.execute(
        """INSERT INTO native_watch_credits
               (user_id, domain, canonical_id, season_number, episode_number,
                monitor_id, event_key, playback_fraction, observed_at, status,
                attempts, detail, outbox_id, created_at, updated_at)
           VALUES (?, 'anime', ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', NULL, ?, ?)
           ON CONFLICT (user_id, domain, canonical_id, season_number, episode_number)
           DO UPDATE SET
               monitor_id = COALESCE(excluded.monitor_id, native_watch_credits.monitor_id),
               event_key = excluded.event_key,
               playback_fraction = MAX(
                   native_watch_credits.playback_fraction, excluded.playback_fraction
               ),
               observed_at = MAX(native_watch_credits.observed_at, excluded.observed_at)""",
        (
            target.user_id,
            target.canonical_id,
            target.season_number,
            target.episode_number,
            monitor_id,
            event_key,
            float(fraction),
            observed_at,
            now,
            now,
        ),
    )
    row = db.execute(
        """SELECT id FROM native_watch_credits
           WHERE user_id=? AND domain='anime' AND canonical_id=?
             AND season_number=? AND episode_number=?""",
        (
            target.user_id,
            target.canonical_id,
            target.season_number,
            target.episode_number,
        ),
    ).fetchone()
    return int(row["id"]) if row is not None else None


def _intent(row) -> Intent:
    return Intent(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        domain=str(row["domain"]),
        canonical_id=str(row["canonical_id"]),
        season_number=int(row["season_number"]),
        episode_number=int(row["episode_number"]),
        monitor_id=int(row["monitor_id"]) if row["monitor_id"] is not None else None,
        event_key=str(row["event_key"]),
        playback_fraction=float(row["playback_fraction"]),
        observed_at=str(row["observed_at"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        next_attempt_at=int(row["next_attempt_at"]),
    )


def load(db, intent_id: int) -> Intent | None:
    if not isinstance(intent_id, int) or intent_id <= 0:
        return None
    try:
        row = db.execute(
            f"SELECT *, updated_at + ({_RETRY_DELAY}) AS next_attempt_at "
            "FROM native_watch_credits WHERE id=?",
            (intent_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _intent(row) if row is not None else None


def for_target(db, target) -> Intent | None:
    """Find the intent for one resumed episode, if a completion was recorded."""
    if target is None or target.domain != "anime":
        return None
    try:
        row = db.execute(
            f"""SELECT *, updated_at + ({_RETRY_DELAY}) AS next_attempt_at
                FROM native_watch_credits
                WHERE user_id=? AND domain='anime' AND canonical_id=?
                 AND season_number=? AND episode_number=?""",
            (
                target.user_id,
                target.canonical_id,
                target.season_number,
                target.episode_number,
            ),
        ).fetchone()
    except sqlite3.Error:
        return None
    return _intent(row) if row is not None else None


def retry_due(intent: Intent, *, now: int) -> bool:
    return intent.status == STATUS_PENDING and intent.next_attempt_at <= now


def outstanding(db, user_id: int, *, now: int, limit: int = 4) -> tuple[Intent, ...]:
    """Oldest due completions for one account, with bounded work per request."""
    rows = db.execute(
        f"""SELECT *, updated_at + ({_RETRY_DELAY}) AS next_attempt_at
            FROM native_watch_credits
            WHERE user_id=? AND status='pending' AND updated_at + ({_RETRY_DELAY}) <= ?
            ORDER BY observed_at, id LIMIT ?""",
        (int(user_id), now, max(1, min(int(limit), 16))),
    ).fetchall()
    return tuple(_intent(row) for row in rows)


def settle(db, intent_id: int, *, outbox_id=None, now: int) -> None:
    """Mark a completion credited. Safe to call twice for the same intent."""
    db.execute(
        """UPDATE native_watch_credits
           SET status='credited', outbox_id=?, detail='', attempts=attempts+1, updated_at=?
           WHERE id=? AND status<>'credited'""",
        (outbox_id if isinstance(outbox_id, int) else None, now, int(intent_id)),
    )
    db.commit()


def note_failure(db, intent_id: int, *, detail: str, now: int) -> int:
    """Record a failed attempt and return the total attempt count."""
    text = "".join(char for char in str(detail) if char.isprintable())[:200]
    db.execute(
        """UPDATE native_watch_credits
           SET attempts=attempts+1, detail=?, updated_at=?
           WHERE id=? AND status='pending'""",
        (text, now, int(intent_id)),
    )
    db.commit()
    row = db.execute(
        "SELECT attempts FROM native_watch_credits WHERE id=?", (int(intent_id),)
    ).fetchone()
    return int(row["attempts"]) if row is not None else 0
