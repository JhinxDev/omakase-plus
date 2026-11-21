"""Owner-scoped collection observations and legacy TV watch receipts."""

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
    return {"tv_collection_additions": [dict(row) for row in additions]}


def inventory_history(db, *, user_id: int) -> list[dict]:
    rows = db.execute(
        """SELECT q.title, '/plus/requests/tv/' || q.title_id AS href,
                  p.checked_at AS observed_at,
                  json_array_length(p.snapshot_json,'$.library.episodes') || ' episodes confirmed in ' ||
                    json_extract(p.snapshot_json,'$.library.provider') AS detail
           FROM tv_provider_requests p JOIN for_you_requests q USING(user_id,kind,title_id)
           WHERE p.user_id=? AND p.checked_at IS NOT NULL
             AND json_array_length(p.snapshot_json,'$.library.episodes')>0
           UNION ALL
           SELECT title, '/plus/library/' || monitor_id, plex_removed_at, 'Removed from Plex'
           FROM media_title_removals WHERE user_id=? AND plex_removed_at IS NOT NULL
           UNION ALL
           SELECT c.title, '/plus/movies/' || q.imdb_id, q.updated_at, 'Library copy removed'
           FROM movie_requests q JOIN movie_catalog c USING(user_id,imdb_id)
           WHERE q.user_id=? AND q.code='copy_removed'
           ORDER BY observed_at DESC, title LIMIT ?""",
        (user_id, user_id, user_id, LIMIT),
    )
    return [dict(row) for row in rows]
