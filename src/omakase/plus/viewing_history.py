"""Read-only viewing cards from accepted user history, never acquisition logs."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from omakase.plus.households import feature_enabled
from omakase.plus.movies_store import require_user

_MAX_RECENT = 5000
_ANIME_VIEWING_EVENTS = (
    "e.disposition='accepted' AND e.kind IN ('progress','completed') "
    "AND e.source IN ('plex','apple_tv','anilist','manual') "
    "AND e.confidence IN ('exact','manual') AND e.reason!='completion_score_updated' "
    "AND lower(e.event_key) NOT LIKE '%bulk%' AND lower(e.event_key) NOT LIKE '%migration%'"
)
_PLEX_SEASON_ACTIVITY = re.compile(
    r"plex:snapshot:[1-9][0-9]{0,31}:s(?P<season>[0-9]+):e(?P<episode>[0-9]+):"
    r"(?P<stamp>[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z)"
)


def _stamp(value):
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _title(value, maximum=300):
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(char) < 32 for char in value)
        or "://" in value
    ):
        return None
    return re.sub(r"<[^>]*>", "", value).strip() or None


def _anime_event_times(row):
    recorded = _stamp(row["observed_at"])
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (ValueError, TypeError, RecursionError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    watched = None
    if row["source"] in {"plex", "apple_tv"}:
        # Season snapshots combine highest watched episode with latest activity
        # across the season. Their timestamp cannot date that specific episode.
        if not (row["source"] == "plex" and row["event_key"].startswith("plex:snapshot:")):
            watched = _stamp(payload.get("watched_at")) or recorded
    return payload, recorded, watched


def anime_viewing_recency(db, user_id: int) -> dict[tuple[int, int], str]:
    """Title/season activity for Home, not a fabricated episode watch date.

    A Plex snapshot's observation time is an import time. Its validated season
    last-viewed value can rank the title, while History keeps its honest label.
    Read only this viewer's accepted events; callers retain library visibility.
    """
    require_user(db, user_id)
    rows = db.execute(
        "SELECT e.source,e.event_key,e.episode,e.observed_at,e.payload_json,"
        "w.anilist_id,w.season_number FROM watch_events e JOIN watch_entries w "
        "ON w.id=e.watch_entry_id AND w.user_id=e.user_id "
        f"WHERE e.user_id=? AND w.user_id=? AND w.status='watching' AND {_ANIME_VIEWING_EVENTS}",
        (user_id, user_id),
    )
    recency = {}
    for row in rows:
        _, recorded, watched = _anime_event_times(row)
        if recorded is None:
            continue
        activity = watched or recorded
        if row["source"] == "plex" and row["event_key"].startswith("plex:snapshot:"):
            snapshot = _PLEX_SEASON_ACTIVITY.fullmatch(row["event_key"])
            if (
                snapshot is None
                or int(snapshot["season"]) != row["season_number"]
                or int(snapshot["episode"]) != row["episode"]
            ):
                continue
            activity = _stamp(snapshot["stamp"])
            if activity is None:
                continue
        identity = (int(row["anilist_id"]), int(row["season_number"]))
        recency[identity] = max(recency.get(identity, ""), activity)
    return recency


def _anime(row, *, personal_navigation=True):
    payload, recorded, watched = _anime_event_times(row)
    if recorded is None:
        return None
    episode = row["episode"] if row["episode"] and row["episode"] > 0 else None
    episode_title = _title(payload.get("episode_title") or payload.get("episode_name"), 300)
    if payload.get("episode") is not None and payload["episode"] != episode:
        episode_title = None
    source = {"plex": "Plex", "apple_tv": "Apple TV", "anilist": "AniList", "manual": "Manual"}[
        row["source"]
    ]
    label = (
        "Watched" if watched else ("Marked watched" if row["source"] == "manual" else "Recorded")
    )
    total = row["episode_total"] or row["expected_episodes"] or None
    percent = (
        min(100, round(100 * episode / total))
        if episode and total
        else (100 if row["kind"] == "completed" else 0)
    )
    media_id = row["anilist_id"]
    return {
        "id": f"anime-{row['id']}",
        "media_type": "anime",
        "anilist_id": media_id,
        "monitor_id": row["owned_monitor_id"],
        "title": _title(row["title"]) or _title(payload.get("title")) or f"AniList #{media_id}",
        "episode": episode,
        "episode_title": episode_title,
        "season_number": row["season_number"],
        "episode_total": total,
        "progress_percent": percent,
        "watched_at": watched,
        "watch_time_known": bool(watched and _stamp(payload.get("watched_at"))),
        "recorded_at": recorded,
        "source_label": source,
        "source_detail": "Added manually" if row["source"] == "manual" else f"From {source}",
        "status_label": "Completed"
        if row["kind"] == "completed"
        else ("Watched" if watched else "Progress saved"),
        "rating": row["current_rating"],
        "time_label": label,
        "artwork_url": f"/plus/history/artwork/anime/{row['id']}"
        if episode and row["season_number"] and row["owned_monitor_id"]
        else None,
        "episode_artwork": True,
        "series_artwork_url": f"/plus/media/{media_id}/poster",
        "event_label": "Series completed"
        if row["kind"] == "completed" and not episode
        else "Progress updated"
        if not episode
        else None,
        "detail_url": f"/plus/library/{row['owned_monitor_id']}#overview"
        if row["owned_monitor_id"] and personal_navigation
        else f"/plus/catalog/anime/{media_id}"
        if row["owned_monitor_id"] or not feature_enabled()
        else f"/plus/history/anime/{row['personal_entry_id']}",
    }


def _movies(db, user_id):
    rows = db.execute(
        "SELECT h.*,c.title,c.year FROM movie_history h JOIN movie_catalog c "
        "ON c.user_id=h.user_id AND c.imdb_id=h.imdb_id AND c.tmdb_id=h.tmdb_id "
        "WHERE h.user_id=? AND h.status='completed' AND h.status_source IN ('manual','plex') "
        "ORDER BY h.status_updated_at DESC,h.imdb_id LIMIT ?",
        (user_id, _MAX_RECENT),
    ).fetchall()
    result = []
    for row in rows:
        imdb_id = row["imdb_id"]
        if not re.fullmatch(r"tt[0-9]{7,10}", imdb_id):
            continue
        recorded = _stamp(row["status_updated_at"])
        if recorded is None:
            continue
        watched = _stamp(row["last_viewed_at"]) if row["status_source"] == "plex" else None
        result.append(
            {
                "id": f"movie-{imdb_id}",
                "media_type": "movie",
                "anilist_id": None,
                "imdb_id": imdb_id,
                "tmdb_id": row["tmdb_id"],
                "monitor_id": None,
                "title": _title(row["title"]) or f"TMDB #{row['tmdb_id']}",
                "episode": None,
                "episode_title": None,
                "season_number": None,
                "episode_total": None,
                "progress_percent": 100,
                "watched_at": watched,
                "watch_time_known": bool(watched),
                "recorded_at": recorded,
                "source_label": "Plex" if row["status_source"] == "plex" else "Manual",
                "source_detail": "From Plex"
                if row["status_source"] == "plex"
                else "Added manually",
                "status_label": "Completed",
                "rating": row["rating"],
                "time_label": "Watched"
                if watched
                else ("Marked watched" if row["status_source"] == "manual" else "Recorded"),
                "artwork_url": f"/plus/movies/{imdb_id}/artwork",
                "detail_url": f"/plus/movies/{imdb_id}",
            }
        )
    return result


def _television(db, user_id):
    """Current explicit TV records; observation dates are never watch dates."""
    rows = db.execute(
        "SELECT e.*,c.metadata_json AS catalog_json,h.rating FROM tv_episodes e "
        "JOIN tv_catalog c ON c.user_id=e.user_id AND c.tmdb_id=e.tmdb_id "
        "LEFT JOIN tv_history h ON h.user_id=e.user_id AND h.tmdb_id=e.tmdb_id "
        "WHERE e.user_id=? AND e.watched=1 AND e.watch_source IN ('manual','plex') "
        "ORDER BY e.updated_at DESC,e.tmdb_id,e.season_number,e.episode_number LIMIT ?",
        (user_id, _MAX_RECENT),
    ).fetchall()
    result = []
    for row in rows:
        metadata = json.loads(row["catalog_json"])
        episode = json.loads(row["metadata_json"])
        stamp = _stamp(row["updated_at"])
        if not stamp:
            continue
        tmdb = row["tmdb_id"]
        result.append(
            {
                "id": f"tv-{tmdb}-{row['season_number']}-{row['episode_number']}",
                "media_type": "tv",
                "title": _title(metadata.get("title")) or f"TV #{tmdb}",
                "episode": row["episode_number"],
                "episode_title": _title(episode.get("title")),
                "season_number": row["season_number"],
                "status_label": "Watched",
                "source_detail": "Added manually"
                if row["watch_source"] == "manual"
                else "From Plex",
                "rating": row["rating"],
                "watched_at": None,
                "watch_time_known": False,
                "recorded_at": stamp,
                "artwork_url": f"/plus/history/artwork/tv/{tmdb}/{row['season_number']}/{row['episode_number']}",
                "episode_artwork": True,
                "series_artwork_url": f"/plus/tv/{tmdb}/artwork",
                "detail_url": f"/plus/tv/{tmdb}?season={row['season_number']}",
            }
        )
    completed = db.execute(
        "SELECT h.*,c.metadata_json FROM tv_history h JOIN tv_catalog c "
        "ON c.user_id=h.user_id AND c.tmdb_id=h.tmdb_id WHERE h.user_id=? "
        "AND h.status='completed' AND h.status_source IN ('manual','plex') "
        "ORDER BY h.updated_at DESC LIMIT ?",
        (user_id, _MAX_RECENT),
    ).fetchall()
    for row in completed:
        stamp = _stamp(row["updated_at"])
        if not stamp:
            continue
        metadata = json.loads(row["metadata_json"])
        tmdb = row["tmdb_id"]
        result.append(
            {
                "id": f"tv-{tmdb}-completed",
                "media_type": "tv",
                "title": _title(metadata.get("title")) or f"TV #{tmdb}",
                "episode": None,
                "episode_title": None,
                "status_label": "Completed",
                "rating": row["rating"],
                "watched_at": None,
                "watch_time_known": False,
                "recorded_at": stamp,
                "source_detail": "Added manually"
                if row["status_source"] == "manual"
                else "From Plex",
                "artwork_url": None,
                "episode_artwork": True,
                "series_artwork_url": f"/plus/tv/{tmdb}/artwork",
                "detail_url": f"/plus/tv/{tmdb}",
            }
        )
    return result


def viewing_history(db, user_id: int, limit: int = 24, page: int = 1) -> dict:
    """Return a bounded recent feed. No metadata request or state write occurs.

    AniList checkpoints stay one Recorded card; missing episode names stay None.
    Plex snapshot import time is never substituted for its last-viewed time.
    """
    require_user(db, user_id)
    if (
        type(limit) is not int
        or not 1 <= limit <= 100
        or type(page) is not int
        or page < 1
        or (page - 1) * limit >= _MAX_RECENT
    ):
        raise ValueError("Choose a valid viewing-history page and limit")

    retained_title = (
        "(SELECT i.title FROM watch_library_links link JOIN library_items i ON i.id=link.library_item_id WHERE link.user_id=w.user_id AND link.watch_entry_id=w.id ORDER BY i.created_at DESC,i.id LIMIT 1)"
        if feature_enabled()
        else "NULL"
    )
    rows = db.execute(
        "WITH ranked AS (SELECT e.*,ROW_NUMBER() OVER (PARTITION BY e.watch_entry_id,e.episode "
        "ORDER BY e.observed_at DESC,e.id DESC) AS occurrence FROM watch_events e "
        "JOIN watch_entries w ON w.id=e.watch_entry_id AND w.user_id=e.user_id "
        f"WHERE e.user_id=? AND w.user_id=? AND {_ANIME_VIEWING_EVENTS}) "
        f"SELECT r.*,w.id AS personal_entry_id,w.anilist_id,w.season_number,w.episode_total,m.id AS owned_monitor_id,COALESCE(m.title,{retained_title}) AS title,m.expected_episodes,(SELECT score_ten FROM watch_events score WHERE score.user_id=w.user_id AND score.watch_entry_id=w.id AND score.disposition='accepted' AND score.score_ten IS NOT NULL ORDER BY score.id DESC LIMIT 1) AS current_rating "
        "FROM ranked r JOIN watch_entries w ON w.id=r.watch_entry_id AND w.user_id=? "
        "LEFT JOIN media_monitors m ON m.id=w.monitor_id AND m.user_id=w.user_id AND m.anilist_id=w.anilist_id AND m.season_number=w.season_number "
        "WHERE r.occurrence=1 ORDER BY r.observed_at DESC,r.id DESC LIMIT ?",
        (user_id, user_id, user_id, _MAX_RECENT),
    ).fetchall()
    personal_navigation = True
    if feature_enabled():
        from omakase.plus.households import HouseholdError, selected_scope

        try:
            personal_navigation = selected_scope(db, viewer_id=user_id).kind == "personal"
        except HouseholdError:
            # An unknown selection is never permission to bypass catalog resolution.
            personal_navigation = False
    items = [
        card
        for row in rows
        if (card := _anime(row, personal_navigation=personal_navigation)) is not None
    ]
    items.extend(_movies(db, user_id))
    items.extend(_television(db, user_id))
    items.sort(
        key=lambda card: (
            card["watched_at"] or card["recorded_at"],
            card["recorded_at"],
            card["id"],
        ),
        reverse=True,
    )
    items = items[:_MAX_RECENT]
    start = (page - 1) * limit
    return {
        "items": items[start : start + limit],
        "page": page,
        "has_more": len(items) > start + limit,
    }
