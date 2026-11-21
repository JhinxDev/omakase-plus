"""Private, local TV collection events and review intent receipts."""

from __future__ import annotations

LIMIT = 60


def record_addition(db, *, user_id: int, tmdb_id: int, title: str, source: str, added_at: str):
    """Record an authenticated personal action in the caller's write transaction."""
    db.execute(
        """INSERT INTO tv_collection_additions(user_id,tmdb_id,title,source,added_at)
           VALUES(?,?,?,?,?) ON CONFLICT(user_id,tmdb_id) DO NOTHING""",
        (user_id, tmdb_id, title.strip()[:300] or f"TV show {tmdb_id}", source, added_at),
    )


def history(db, *, user_id: int) -> dict[str, list[dict]]:
    """Read only positively evidenced local actions for this authenticated account."""
    additions = db.execute(
        """SELECT tmdb_id,title,added_at,source FROM tv_collection_additions
           WHERE user_id=? ORDER BY added_at DESC,tmdb_id DESC LIMIT ?""",
        (user_id, LIMIT),
    )
    review_intents = db.execute(
        """SELECT title_id AS tmdb_id,title,created_at FROM for_you_requests
           WHERE user_id=? AND kind='tv' ORDER BY created_at DESC,title_id DESC LIMIT ?""",
        (user_id, LIMIT),
    )
    return {
        "tv_collection_additions": [dict(row) for row in additions],
        "tv_review_intents": [dict(row) for row in review_intents],
    }
