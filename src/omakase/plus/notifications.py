"""Bounded notifications derived exclusively from this account's saved domain state.

No provider, metadata, history import, image request or media mutation occurs here.
Completion versions keep a later rewatch distinct from a dismissed old reminder.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

_LIMIT = 20
_COUNT_CAP = 100

_ANIME = """
WITH anime AS (
 SELECT e.id AS entry_id,m.id AS monitor_id,m.title,e.completed_at AS created_at,
        COALESCE((SELECT v.score_ten FROM watch_events v
                  WHERE v.watch_entry_id=e.id AND v.user_id=e.user_id
                    AND v.disposition='accepted' AND v.score_ten IS NOT NULL
                  ORDER BY v.id DESC LIMIT 1),
                 (SELECT t.score_ten FROM tracker_media_actions t
                  WHERE t.user_id=e.user_id AND t.tracker IN ('anilist','myanimelist')
                    AND t.media_id=e.anilist_id
                    AND t.action='completed' AND t.score_ten IS NOT NULL
                  ORDER BY t.id DESC LIMIT 1)) AS rating
 FROM watch_entries e JOIN media_monitors m
 ON m.id=e.monitor_id AND m.user_id=e.user_id AND m.anilist_id=e.anilist_id
    AND COALESCE(m.season_number,0)=e.season_number
 WHERE e.user_id=:user_id AND e.status='completed' AND e.completed_at IS NOT NULL
)
"""

# Each source is independently indexed/ordered/limited after account UI-state filters.
_SOURCES = {
    "anime_rating": """
SELECT 'completed_unrated:anime:'||entry_id||':'||created_at AS id,
        'completed_unrated' AS kind,'anime' AS media_type,title,rating,created_at,
        '/plus/library/'||monitor_id||'#watch-tracking' AS href
 FROM anime WHERE rating IS NULL
""",
    "movie_rating": """
SELECT 'completed_unrated:movie:'||h.imdb_id||':'||COALESCE(h.status_updated_at,h.updated_at),
        'completed_unrated','movie',c.title,h.rating,
        COALESCE(h.status_updated_at,h.updated_at),
        '/plus/movies/'||h.imdb_id||'#movie-rating'
 FROM movie_history h JOIN movie_catalog c ON c.user_id=h.user_id AND c.imdb_id=h.imdb_id
 WHERE h.user_id=:user_id AND h.status='completed' AND h.rating IS NULL
       AND h.status_source IN ('manual','plex')
""",
    "tv_rating": """
SELECT 'completed_unrated:tv:'||h.tmdb_id||':'||h.updated_at,
        'completed_unrated','tv',COALESCE(json_extract(c.metadata_json,'$.title'),'TV show'),
        h.rating,h.updated_at,'/plus/tv/'||h.tmdb_id||'#tv-history'
 FROM tv_history h JOIN tv_catalog c ON c.user_id=h.user_id AND c.tmdb_id=h.tmdb_id
 WHERE h.user_id=:user_id AND h.status='completed' AND h.rating IS NULL
       AND h.status_source IN ('manual','plex')
""",
    "anime_media": """
SELECT 'removal_reminder:anime:'||n.monitor_id||':'||n.created_at,
        'removal_reminder','anime',n.title,n.rating,n.created_at,
        '/plus/library/'||n.monitor_id||'/remove'
 FROM anime n
 WHERE NOT EXISTS (SELECT 1 FROM media_title_removals r
                   WHERE r.user_id=:user_id AND r.monitor_id=n.monitor_id
                     AND (r.status!='complete' OR r.completed_at>=n.created_at))
 AND EXISTS (
  SELECT 1 FROM media_assets a JOIN media_providers p ON p.id=a.provider_id
  WHERE a.monitor_id=n.monitor_id AND p.user_id=:user_id AND p.kind='realdebrid'
    AND a.provider_asset_id!='' AND a.ownership='monitor_owned'
    AND a.provenance='omakase_created' AND a.created_by_run_id IS NOT NULL
    AND a.shared_reference_count=0 AND a.downloaded_once=1 AND a.status='downloaded'
    AND a.inventory_verified_at IS NOT NULL AND a.usable_coverage_json!='[]'
    AND NOT EXISTS (SELECT 1 FROM media_assets other
                    WHERE other.monitor_id!=a.monitor_id AND other.status!='removed'
                      AND other.torrent_hash=a.torrent_hash)
    AND NOT EXISTS (SELECT 1 FROM media_assets other
                    WHERE other.monitor_id!=a.monitor_id AND other.status!='removed'
                      AND other.provider_asset_id=a.provider_asset_id)
    AND NOT EXISTS (
      SELECT 1 FROM media_monitors own JOIN media_monitors other
      ON other.id!=own.id AND other.anilist_id=own.anilist_id
         AND other.season_number IS own.season_number
      JOIN media_assets shared ON shared.monitor_id=other.id
      WHERE own.id=a.monitor_id AND shared.status='downloaded'
        AND shared.inventory_verified_at IS NOT NULL AND shared.usable_coverage_json!='[]'
        AND NOT EXISTS (SELECT 1 FROM media_title_removals r WHERE r.monitor_id=other.id)
    )
 )
""",
}


def _window_sql(source: str) -> str:
    return (
        _ANIME
        + ",candidate(id,kind,media_type,title,rating,created_at,href) AS ("
        + _SOURCES[source]
        + """
        ) SELECT c.*,s.read_at FROM candidate c LEFT JOIN notification_state s
        ON s.user_id=:user_id AND s.notification_id=c.id
        WHERE (:include_dismissed OR s.dismissed_at IS NULL)
          AND (NOT :unread_only OR s.read_at IS NULL)
          AND (:target_id IS NULL OR c.id=:target_id)
        ORDER BY c.created_at DESC LIMIT :cap
        """
    )


def _params(user_id: int) -> dict:
    if type(user_id) is not int or user_id < 1:
        raise ValueError("Choose a valid account")
    return {"user_id": user_id}


def _item(row: sqlite3.Row) -> dict:
    review = row["kind"] == "removal_reminder"
    title = "".join(c for c in str(row["title"]) if ord(c) >= 32).strip()[:300]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "media_type": row["media_type"],
        "title": title or "Untitled",
        "message": (
            "Saved records show completed, Omakase-managed Real-Debrid media. Keeping or removing it is your choice."
            if review
            else "You marked this title watched. Add your rating when you are ready."
        ),
        "href": row["href"],
        "action_label": "Review media" if review else "Rate title",
        "rating": row["rating"],
        "created_at": row["created_at"],
        "read": row["read_at"] is not None,
        "artwork_url": None,
    }


def notification_feed(db: sqlite3.Connection, *, user_id: int) -> dict:
    """Read indexed per-domain windows (at most 101 each); return the newest 20."""
    params = {
        **_params(user_id),
        "cap": _COUNT_CAP + 1,
        "unread_only": False,
        "include_dismissed": False,
        "target_id": None,
    }
    rows = []
    unread_total = 0
    for source in _SOURCES:
        rows.extend(db.execute(_window_sql(source), params).fetchall())
        unread_total += len(
            db.execute(_window_sql(source), {**params, "unread_only": True}).fetchall()
        )
    rows.sort(key=lambda row: (row["created_at"].replace(" ", "T"), row["id"]), reverse=True)

    return {
        "items": [_item(row) for row in rows[:_LIMIT]],
        "count": min(len(rows), _COUNT_CAP),
        "unread_count": min(unread_total, _COUNT_CAP),
        "count_capped": len(rows) > _COUNT_CAP,
        "unread_count_capped": unread_total > _COUNT_CAP,
        "limit": _LIMIT,
    }


def set_notification_state(
    db: sqlite3.Connection, *, user_id: int, notification_id: str, action: str
) -> bool:
    """Idempotently read/dismiss a currently eligible signal for this account only."""
    params = _params(user_id)
    if action not in {"read", "dismiss"}:
        raise ValueError("Choose read or dismiss")
    if not isinstance(notification_id, str) or not re.fullmatch(
        r"[A-Za-z0-9_: .+\-]{1,240}", notification_id
    ):
        return False
    parts = notification_id.split(":", 2)
    source = {
        ("completed_unrated", "anime"): "anime_rating",
        ("completed_unrated", "movie"): "movie_rating",
        ("completed_unrated", "tv"): "tv_rating",
        ("removal_reminder", "anime"): "anime_media",
    }.get(tuple(parts[:2]))
    if source is None:
        return False
    params.update(cap=1, unread_only=False, include_dismissed=True, target_id=notification_id)
    if db.execute(_window_sql(source), params).fetchone() is None:
        return False
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db:
        db.execute(
            """INSERT INTO notification_state(user_id,notification_id,read_at,dismissed_at)
               VALUES(?,?,?,?) ON CONFLICT(user_id,notification_id) DO UPDATE SET
               read_at=COALESCE(notification_state.read_at,excluded.read_at),
               dismissed_at=COALESCE(notification_state.dismissed_at,excluded.dismissed_at)""",
            (user_id, notification_id, now, now if action == "dismiss" else None),
        )
    return True
