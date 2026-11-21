"""Shared completion projection for verified web and Apple TV playback."""

from datetime import UTC, datetime
from types import SimpleNamespace


def credit_web_completion(db, state):
    if (
        state["domain"] not in {"movie", "tv"}
        or state.get("credited_seconds", 0) <= 0
        or state.get("watch_covered_seconds", 0) < 0.85 * state["duration_seconds"]
    ):
        return
    personal = db.execute(
        "SELECT 1 FROM personal_libraries WHERE user_id=? AND library_id=?",
        (state["user_id"], state["library_id"]),
    ).fetchone()
    if personal is None:
        return
    target = SimpleNamespace(
        user_id=state["user_id"],
        canonical_id=state["canonical_id"],
        season_number=state["season_number"],
        episode_number=state["episode_number"],
    )
    stamp = datetime.fromtimestamp(state["accepted_at"], UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if state["domain"] == "movie":
        credit_movie_completion(db, target, stamp, source="web_player")
    else:
        credit_tv_completion(db, target, stamp, source="web_player")


def credit_movie_completion(db, target, stamp, *, source):
    catalog = db.execute(
        "SELECT tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if catalog is None:
        return None
    history = db.execute(
        "SELECT status_source FROM movie_history WHERE user_id=? AND imdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if history is not None and history["status_source"] == "manual":
        return True
    if history is None:
        db.execute(
            "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,"
            "status_source_detail,status_updated_at,last_viewed_at,created_at,updated_at) "
            "VALUES(?,?,?,'completed','plex',?,?,?,?,?)",
            (
                target.user_id,
                target.canonical_id,
                catalog["tmdb_id"],
                source,
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
    else:
        db.execute(
            "UPDATE movie_history SET status='completed',status_source='plex',"
            "status_source_detail=?,status_updated_at=?,last_viewed_at=?,"
            "updated_at=? WHERE user_id=? AND imdb_id=?",
            (source, stamp, stamp, stamp, target.user_id, target.canonical_id),
        )
    return True


def credit_tv_completion(db, target, stamp, *, source):
    catalog = db.execute(
        "SELECT 1 FROM tv_catalog WHERE user_id=? AND tmdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if catalog is None:
        return None
    episode = db.execute(
        "SELECT watched,watch_source FROM tv_episodes "
        "WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
        (
            target.user_id,
            target.canonical_id,
            target.season_number,
            target.episode_number,
        ),
    ).fetchone()
    if episode is not None and episode["watch_source"] == "manual":
        return True
    if episode is None:
        db.execute(
            "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,"
            "metadata_json,watched,watch_source,watch_source_detail,plex_present,updated_at) "
            "VALUES(?,?,?,?,'{}',1,'plex',?,0,?)",
            (
                target.user_id,
                target.canonical_id,
                target.season_number,
                target.episode_number,
                source,
                stamp,
            ),
        )
    else:
        db.execute(
            "UPDATE tv_episodes SET watched=1,watch_source='plex',"
            "watch_source_detail=?,updated_at=? "
            "WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
            (
                source,
                stamp,
                target.user_id,
                target.canonical_id,
                target.season_number,
                target.episode_number,
            ),
        )
    history = db.execute(
        "SELECT status,status_source FROM tv_history WHERE user_id=? AND tmdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if history is None:
        db.execute(
            "INSERT INTO tv_history(user_id,tmdb_id,status,status_source,"
            "status_source_detail,updated_at) VALUES(?,?,'watching','plex',?,?)",
            (target.user_id, target.canonical_id, source, stamp),
        )
    elif history["status_source"] != "manual":
        status = "completed" if history["status"] == "completed" else "watching"
        db.execute(
            "UPDATE tv_history SET status=?,status_source='plex',"
            "status_source_detail=?,updated_at=? WHERE user_id=? AND tmdb_id=?",
            (status, source, stamp, target.user_id, target.canonical_id),
        )
    return True
