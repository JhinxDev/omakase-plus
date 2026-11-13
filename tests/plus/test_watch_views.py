"""Viewer lifecycle composition for Library and title detail."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_store import ensure_media_provider, upsert_asset, upsert_monitor
from omakase.plus.product_views import library_titles, title_detail
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import ingest_watch_event
from tests.plus.test_product_views import _seed_ready_title


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


def _user(db: sqlite3.Connection) -> int:
    return int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES ('viewer@example.com', 'hash')"
        ).lastrowid
    )


def _monitor(
    db: sqlite3.Connection,
    *,
    user_id: int,
    anilist_id: int,
    title: str,
    episodes: int = 12,
) -> int:
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=anilist_id,
        title=title,
        search_titles=(title,),
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=episodes,
        aired_episodes=episodes,
    )
    db.commit()
    return monitor_id


def _progress(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
    episode: int,
    key: str,
) -> None:
    ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(source="plex", episode=episode, event_key=key),
        observed_at="2026-08-27T12:00:00Z",
    )


def test_library_defaults_to_active_and_hides_completed(db: sqlite3.Connection) -> None:
    user_id = _user(db)
    frieren_id = _monitor(db, user_id=user_id, anilist_id=154587, title="Frieren")
    solo_id = _monitor(db, user_id=user_id, anilist_id=151807, title="Solo Leveling")
    _progress(db, user_id=user_id, monitor_id=frieren_id, episode=3, key="plex:frieren:e3")
    ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=solo_id,
        event=WatchEvent.complete(source="manual", event_key="manual:solo:complete"),
        observed_at="2026-08-27T12:05:00Z",
    )

    active = library_titles(db, user_id=user_id)
    completed = library_titles(db, user_id=user_id, lifecycle="completed")

    assert [item["title"] for item in active] == ["Frieren"]
    assert [item["title"] for item in completed] == ["Solo Leveling"]
    assert active[0]["progress_episodes"] == 3
    assert active[0]["next_episode"] == 4
    assert active[0]["progress_percent"] == 25


def test_library_never_resurrects_a_title_after_removal_completed(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    monitor_id = _monitor(
        db,
        user_id=user_id,
        anilist_id=151807,
        title="Solo Leveling",
    )
    db.execute(
        """INSERT INTO media_title_removals
           (user_id, monitor_id, anilist_id, season_number, title, status,
            curator_run_id, detail, requested_at, plex_removed_at, completed_at)
           VALUES (?, ?, 151807, 1, 'Solo Leveling', 'complete',
                   'curator-test-run', 'Removed from Plex and provider',
                   '2026-08-27T12:00:00Z', '2026-08-27T12:01:00Z',
                   '2026-08-27T12:01:00Z')""",
        (user_id, monitor_id),
    )
    db.commit()

    assert library_titles(db, user_id=user_id) == []
    assert library_titles(db, user_id=user_id, lifecycle="all") == []


def test_lifecycle_filters_cover_in_progress_ready_waiting_completed_and_all(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    ready_id, _ = _seed_ready_title(db, user_id)
    frieren_id = _monitor(db, user_id=user_id, anilist_id=154587, title="Frieren")
    _monitor(db, user_id=user_id, anilist_id=170000, title="Pluto")
    solo_id = _monitor(db, user_id=user_id, anilist_id=151807, title="Solo Leveling")
    _progress(db, user_id=user_id, monitor_id=frieren_id, episode=3, key="plex:frieren:e3")
    ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=solo_id,
        event=WatchEvent.complete(source="manual", event_key="manual:solo:complete"),
        observed_at="2026-08-27T12:05:00Z",
    )

    assert [
        item["title"] for item in library_titles(db, user_id=user_id, lifecycle="in_progress")
    ] == ["Frieren"]
    assert [
        item["id"] for item in library_titles(db, user_id=user_id, lifecycle="ready_to_start")
    ] == [ready_id]
    assert [item["title"] for item in library_titles(db, user_id=user_id, lifecycle="waiting")] == [
        "Pluto"
    ]
    assert [
        item["title"] for item in library_titles(db, user_id=user_id, lifecycle="completed")
    ] == ["Solo Leveling"]
    assert len(library_titles(db, user_id=user_id, lifecycle="all")) == 4


def test_verified_first_episode_is_found_without_inferring_exact_plex_membership(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    monitor_id, _ = _seed_ready_title(db, user_id)
    db.execute(
        "UPDATE media_monitors SET expected_episodes = 12, aired_episodes = 12 WHERE id = ?",
        (monitor_id,),
    )
    db.commit()

    item = next(
        item
        for item in library_titles(db, user_id=user_id, lifecycle="all")
        if item["id"] == monitor_id
    )

    assert item["new_episode_ready"] is False
    assert item["new_episode_available"] is True
    assert item["lifecycle"] == "ready_to_start"
    assert item["lifecycle_label"] == "Episode found"
    assert item["status_label"] == "Episode 1 found · Plex availability unconfirmed"


def test_waiting_titles_explain_no_source_versus_no_aired_episodes(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    _monitor(db, user_id=user_id, anilist_id=170000, title="No Source")
    not_aired_id = _monitor(
        db,
        user_id=user_id,
        anilist_id=170001,
        title="Not Aired",
    )
    db.execute(
        """UPDATE media_monitors
           SET release_status = 'NOT_YET_RELEASED', aired_episodes = 0
           WHERE id = ?""",
        (not_aired_id,),
    )
    db.commit()

    items = {item["title"]: item for item in library_titles(db, user_id=user_id, lifecycle="all")}

    no_source = items["No Source"]
    assert no_source["lifecycle"] == "waiting"
    assert no_source["lifecycle_label"] == "No source"
    assert no_source["status_label"] == "No media source found · 12 episodes missing"
    assert no_source["provider_summary"] == "No provider media found"

    not_aired = items["Not Aired"]
    assert not_aired["lifecycle"] == "waiting"
    assert not_aired["lifecycle_label"] == "Waiting to air"
    assert not_aired["status_label"] == "No episodes have aired yet"
    assert not_aired["provider_summary"] == "No provider media found"


def test_waiting_title_with_provider_reports_missing_episodes_not_no_source(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    monitor_id = _monitor(
        db,
        user_id=user_id,
        anilist_id=170002,
        title="Provider Found",
    )
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="realdebrid",
        label="Real-Debrid",
        config={},
        capabilities={"remote_cache": True},
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="B" * 40,
        source_title="Provider Found S01",
        provider_id=provider_id,
        provider_asset_id="provider-found-s01",
        planned_coverage=range(1, 13),
        usable_coverage=(),
        status="error",
        last_error="No usable files matched",
    )
    db.commit()

    item = next(
        item
        for item in library_titles(db, user_id=user_id, lifecycle="all")
        if item["id"] == monitor_id
    )

    assert item["lifecycle_label"] == "Episodes missing"
    assert item["status_label"] == "12 episodes missing"
    assert item["provider_summary"] == "Real-Debrid"


def test_watch_projections_are_attached_in_one_query(db: sqlite3.Connection) -> None:
    user_id = _user(db)
    for index in range(4):
        _monitor(
            db,
            user_id=user_id,
            anilist_id=1000 + index,
            title=f"Title {index}",
        )
    statements: list[str] = []
    db.set_trace_callback(statements.append)

    library_titles(db, user_id=user_id, lifecycle="all")

    watch_queries = [
        statement
        for statement in statements
        if "FROM watch_entries" in statement and statement.lstrip().upper().startswith("SELECT")
    ]
    assert len(watch_queries) == 1


def test_title_detail_leads_with_progress_history_and_pending_confirmation(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    monitor_id = _monitor(db, user_id=user_id, anilist_id=154587, title="Frieren")
    _progress(db, user_id=user_id, monitor_id=monitor_id, episode=3, key="plex:frieren:e3")
    ingest_watch_event(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
        event=WatchEvent.progress(
            source="apple_tv",
            episode=4,
            confidence="inferred",
            event_key="apple:frieren:e4",
        ),
        observed_at="2026-08-27T12:05:00Z",
    )

    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)

    assert detail is not None
    assert detail["watch_status"] == "watching"
    assert detail["progress_episodes"] == 3
    assert detail["has_pending_confirmation"] is True
    assert detail["pending_confirmations"][0]["proposed_episode"] == 4
    assert [item["source_label"] for item in detail["watch_timeline"]] == [
        "Apple TV",
        "Plex",
    ]
