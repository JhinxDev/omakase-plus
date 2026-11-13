"""Local recommendation feedback persistence for Omakase Plus."""

from __future__ import annotations

import sqlite3

from omakase.types import RecommendationFeedbackSignal

VALID_FEEDBACK = {"interested", "not_for_me", "wrong_sequel", "already_watched"}


def save_feedback(
    db: sqlite3.Connection,
    user_id: int,
    source: str,
    media_id: int | None,
    title: str,
    feedback_type: str,
    run_id: int | None,
) -> None:
    if feedback_type not in VALID_FEEDBACK:
        raise ValueError(f"Unsupported feedback type: {feedback_type}")
    db.execute(
        """INSERT INTO recommendation_feedback
           (user_id, source, media_id, title, feedback_type, run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, source, media_id, title, feedback_type, run_id),
    )
    db.commit()


def feedback_for_prompt(
    db: sqlite3.Connection,
    user_id: int,
    limit: int = 30,
) -> list[RecommendationFeedbackSignal]:
    rows = db.execute(
        """SELECT media_id, title, feedback_type
           FROM recommendation_feedback
           WHERE user_id = ?
           ORDER BY id DESC
           LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return [
        RecommendationFeedbackSignal(
            media_id=row["media_id"],
            title=row["title"],
            feedback_type=row["feedback_type"],
        )
        for row in rows
    ]
