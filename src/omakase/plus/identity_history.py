"""Evidence-bound recovery for provider titles renamed after selection."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from omakase.plus.identity import identity_rejection_reason


def successful_exact_hash_identity_title(
    db: sqlite3.Connection,
    *,
    user_id: int,
    anilist_id: int,
    torrent_hash: str,
    target_title: str,
    target_aliases: Sequence[str],
    season: int,
    media_format: str,
) -> str | None:
    """Return a previously selected title only when its exact hash and identity agree."""
    rows = db.execute(
        """SELECT attempt.torrent_title
           FROM download_attempts AS attempt
           JOIN anilist_plannings AS planning
             ON planning.id = attempt.anilist_planning_id
           WHERE attempt.user_id = ?
             AND planning.user_id = ?
             AND planning.anilist_id = ?
             AND UPPER(attempt.torrent_hash) = ?
             AND attempt.status = 'selected'
             AND TRIM(attempt.torrent_title) <> ''
           ORDER BY attempt.created_at DESC, attempt.id DESC""",
        (user_id, user_id, anilist_id, torrent_hash.upper()),
    ).fetchall()
    for row in rows:
        historical_title = str(row["torrent_title"])
        if not identity_rejection_reason(
            historical_title,
            target_title=target_title,
            target_aliases=target_aliases,
            season=season,
            anilist_id=anilist_id,
            media_format=media_format,
        ):
            return historical_title
    return None
