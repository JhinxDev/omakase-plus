"""Viewing cards are owner-scoped observations, separate from operations logs."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3

import pytest

from omakase.plus.db import run_migrations


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    conn.executemany(
        "INSERT INTO users(id,email,password_hash) VALUES (?,?,?)",
        [(1, "history-one@example.invalid", "h"), (2, "history-two@example.invalid", "h")],
    )
    yield conn
    conn.close()


def feed(db, **kwargs):
    assert importlib.util.find_spec("omakase.plus.viewing_history") is not None, (
        "Viewing feed is missing"
    )
    return importlib.import_module("omakase.plus.viewing_history").viewing_history(db, 1, **kwargs)


def entry(db, user_id=1, anilist_id=21, title="One Piece"):
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,expected_episodes,season_number) VALUES (?,?,?,?,1)",
        (user_id, anilist_id, title, 12),
    ).lastrowid
    watch = db.execute(
        "INSERT INTO watch_entries(user_id,monitor_id,anilist_id,status,progress_episodes,episode_total,season_number) VALUES (?,?,?,'watching',3,12,1)",
        (user_id, monitor, anilist_id),
    ).lastrowid
    return monitor, watch


def event(
    db,
    watch,
    *,
    user_id=1,
    episode=1,
    source="plex",
    stamp="2026-10-27T18:00:00Z",
    kind="progress",
    disposition="accepted",
    key=None,
    reason="state_updated",
    payload=None,
):
    key = key or f"{source}:{episode}:{stamp}:{kind}:{disposition}"
    return db.execute(
        "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,episode,confidence,force,disposition,reason,observed_at,payload_json) VALUES (?,?,?,?,?,?,'exact',?,?,?,?,?)",
        (
            watch,
            user_id,
            source,
            key,
            kind,
            episode,
            int(kind in {"correction", "reopened"}),
            disposition,
            reason,
            stamp,
            json.dumps(payload or {}),
        ),
    ).lastrowid


def test_feed_is_user_scoped_deduplicated_and_read_only(db):
    monitor, watch = entry(db)
    event(db, watch, episode=1, source="manual")
    event(db, watch, episode=2, stamp="2026-10-27T19:00:00Z")
    event(db, watch, episode=2, stamp="2026-10-27T20:00:00Z")
    _, other = entry(db, 2, 99, "Private other title")
    event(db, other, user_id=2, episode=3, stamp="2026-10-27T21:00:00Z")
    changes = db.total_changes
    result = feed(db)
    assert len(result["items"]) == 2
    assert result["items"][0]["episode"] == 2
    assert result["items"][0]["watched_at"] == "2026-10-27T20:00:00Z"
    assert result["items"][0]["monitor_id"] == monitor
    assert result["items"][1]["time_label"] == "Marked watched"
    assert result["items"][1]["watched_at"] is None
    assert "Private other title" not in json.dumps(result)
    assert db.total_changes == changes


def test_feed_excludes_operational_and_unaccepted_events(db):
    _, watch = entry(db)
    event(db, watch, episode=1)
    event(db, watch, episode=2, disposition="ignored")
    event(db, watch, episode=3, disposition="confirmation_required")
    event(db, watch, episode=4, source="manual", kind="correction")
    event(db, watch, episode=5, source="manual", kind="reopened")
    event(db, watch, episode=6, source="migration")
    event(
        db, watch, episode=7, source="manual", kind="completed", reason="completion_score_updated"
    )
    event(db, watch, episode=8, source="manual", key="manual:bulk:migration:8")
    assert [item["episode"] for item in feed(db)["items"]] == [1]


def test_anilist_checkpoint_is_recorded_once_without_fabricated_episode_dates(db):
    _, watch = entry(db)
    event(db, watch, episode=10, source="anilist")
    item = feed(db)["items"][0]
    assert item["episode"] == 10
    assert item["watched_at"] is None
    assert item["recorded_at"] == "2026-10-27T18:00:00Z"
    assert item["time_label"] == "Recorded" and item["source_label"] == "AniList"
    assert item["episode_title"] is None
    assert item["artwork_url"] == "/plus/media/21/poster"


def test_plex_snapshot_dates_are_season_checkpoints_not_episode_watch_dates(db):
    _, watch = entry(db)
    event(db, watch, episode=10, key="plex:snapshot:99:s1:e10:2026-10-20T10:00:00Z")
    event(db, watch, episode=11, key="plex:snapshot:99:s1:e11:None", stamp="2026-10-27T19:00:00Z")
    items = feed(db)["items"]
    known = next(item for item in items if item["episode"] == 10)
    unknown = next(item for item in items if item["episode"] == 11)
    assert known["watched_at"] is None and known["time_label"] == "Recorded"
    assert unknown["watched_at"] is None and unknown["time_label"] == "Recorded"


def test_lower_episode_rewatch_does_not_change_higher_episode_watch_date(db):
    _, watch = entry(db)
    # The producer independently takes highest watched index and latest season
    # timestamp: episode10 on Oct20, episode1 rewatched Oct27 becomes e10:Oct27.
    event(db, watch, episode=10, key="plex:snapshot:99:s1:e10:2026-10-27T10:00:00Z")
    item = feed(db)["items"][0]
    assert item["episode"] == 10
    assert item["watched_at"] is None
    assert item["time_label"] == "Recorded"


def test_episode_title_only_uses_persisted_metadata_and_payload_secrets_are_omitted(db):
    _, watch = entry(db)
    event(
        db,
        watch,
        payload={"episode_title": "Departure", "api_key": "never-render", "path": "/private/file"},
    )
    item = feed(db)["items"][0]
    assert item["episode_title"] == "Departure"
    assert "never-render" not in str(item) and "/private/file" not in str(item)


def test_page_bounds_and_has_more_are_honest(db):
    _, watch = entry(db)
    for episode in range(1, 6):
        event(db, watch, episode=episode, stamp=f"2026-10-27T18:00:0{episode}Z")
    first = feed(db, limit=2, page=1)
    second = feed(db, limit=2, page=2)
    third = feed(db, limit=2, page=3)
    assert first["has_more"] and second["has_more"] and not third["has_more"]
    assert [item["episode"] for item in first["items"]] == [5, 4]
    assert [item["episode"] for item in second["items"]] == [3, 2]
    assert [item["episode"] for item in third["items"]] == [1]


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": 101}, {"page": 0}, {"page": True}])
def test_invalid_pagination_is_rejected(db, kwargs):
    with pytest.raises(ValueError):
        feed(db, **kwargs)


def test_manually_completed_movie_has_no_invented_watch_timestamp(db):
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,updated_at) VALUES (1,'tt0084787',1091,'The Thing','2026-10-27T17:00:00Z')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,status_updated_at,created_at,updated_at) VALUES (1,'tt0084787',1091,'completed','manual','2026-10-27T17:00:00Z','2026-10-27T17:00:00Z','2026-10-27T17:00:00Z')"
    )
    item = feed(db)["items"][0]
    assert item["media_type"] == "movie" and item["title"] == "The Thing"
    assert item["watched_at"] is None and item["time_label"] == "Marked watched"
    assert item["detail_url"] == "/plus/movies/tt0084787"


@pytest.mark.parametrize("rating", [None, 8.0])
def test_movie_completion_and_rating_are_independent_history_facts(db, rating):
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,updated_at) VALUES (1,'tt0084787',1091,'The Thing','2026-10-27T17:00:00Z')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,status_updated_at,rating,rating_source,created_at,updated_at) VALUES (1,'tt0084787',1091,'completed','manual','2026-10-27T17:00:00Z',?,'manual','2026-10-27T17:00:00Z','2026-10-27T17:00:00Z')",
        (rating,),
    )
    item = feed(db)["items"][0]
    assert item["status_label"] == "Completed"
    assert item["rating"] == rating
    assert item["source_detail"] == "Added manually"
    assert item["watched_at"] is None
    assert item["watch_time_known"] is False


def test_movie_history_uses_current_saved_rating_without_changing_completion(db):
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,updated_at) "
        "VALUES (1,'tt0084787',1091,'The Thing','2026-10-27T17:00:00Z')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,"
        "status_updated_at,rating,rating_source,created_at,updated_at) "
        "VALUES (1,'tt0084787',1091,'completed','manual','2026-10-27T17:00:00Z',"
        "7,'manual','2026-10-27T17:00:00Z','2026-10-27T17:00:00Z')"
    )
    assert feed(db)["items"][0]["rating"] == 7
    db.execute("UPDATE movie_history SET rating=8 WHERE user_id=1")
    changes = db.total_changes
    item = feed(db)["items"][0]
    assert item["rating"] == 8
    assert item["status_label"] == "Completed"
    assert item["watched_at"] is None
    assert item["recorded_at"] == "2026-10-27T17:00:00Z"
    assert db.total_changes == changes


def test_tv_history_is_owner_scoped_and_uses_observation_dates(db):
    from omakase.plus.viewing_history import viewing_history

    for user in (1, 2):
        db.execute(
            "INSERT INTO tv_catalog VALUES (?,?,?,?)",
            (user, 42, json.dumps({"title": "A series"}), "2026-10-27T20:00:00Z"),
        )
        db.execute(
            "INSERT INTO tv_episodes VALUES (?,?,?,?,?,1,'manual',1,?)",
            (user, 42, 2, 3, json.dumps({"title": "A chapter"}), "2026-10-27T20:00:00Z"),
        )
        db.execute(
            "INSERT INTO tv_history VALUES (?,42,'completed',8,'manual','manual',?)",
            (user, "2026-10-27T20:00:00Z"),
        )
    cards = [c for c in viewing_history(db, user_id=1)["items"] if c["media_type"] == "tv"]
    assert len(cards) == 2
    episode = next(c for c in cards if c["episode"])
    assert episode["season_number"] == 2 and episode["episode"] == 3
    assert episode["watched_at"] is None and not episode["watch_time_known"]
    assert episode["source_detail"] == "Added manually"
    complete = next(c for c in cards if not c["episode"])
    assert complete["status_label"] == "Completed" and complete["rating"] == 8
