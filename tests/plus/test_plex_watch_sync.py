"""Real Plex inventory and progress ingestion for Omakase Plus."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_store import upsert_monitor
from omakase.plus.watch.plex_sync import (
    PlexSnapshotError,
    latest_plex_library_summary,
    store_plex_library_snapshot,
)


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
            "INSERT INTO users (email, password_hash) VALUES ('plex@example.com', 'hash')"
        ).lastrowid
    )


def _snapshot(*, watched_through: int = 9) -> dict:
    return {
        "schema_version": "plex-library-snapshot-v1",
        "generated_at": "2026-08-27T15:00:00Z",
        "items": [
            {
                "rating_key": "42",
                "section": "Anime",
                "media_type": "show",
                "title": "Hell's Paradise",
                "season_number": 2,
                "episode_total": 12,
                "watched_through": watched_through,
                "last_viewed_at": "2026-08-27T14:55:00Z",
            },
            {
                "rating_key": "77",
                "section": "Anime Movies",
                "media_type": "movie",
                "title": "Ninja Scroll",
                "season_number": 0,
                "episode_total": 1,
                "watched_through": 0,
                "last_viewed_at": None,
            },
        ],
    }


def test_snapshot_persists_full_inventory_and_advances_matched_monitor_progress(
    db: sqlite3.Connection,
) -> None:
    user_id = _user(db)
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=166613,
        title="Hell's Paradise Season 2",
        search_titles=("Jigokuraku 2nd Season", "Hell's Paradise Season 2"),
        format="TV",
        release_status="FINISHED",
        season_number=2,
        expected_episodes=12,
        aired_episodes=12,
    )
    db.commit()

    result = store_plex_library_snapshot(
        db,
        _snapshot(),
        received_at="2026-08-27T15:00:02Z",
    )

    assert result == {
        "stored": True,
        "item_count": 2,
        "title_count": 2,
        "matched_monitors": 1,
        "progress_updates": 1,
    }
    entry = db.execute("SELECT * FROM watch_entries WHERE monitor_id = ?", (monitor_id,)).fetchone()
    assert entry is not None
    assert entry["status"] == "watching"
    assert entry["progress_episodes"] == 9
    assert entry["episode_total"] == 12
    assert entry["last_source"] == "plex"
    assert db.execute("SELECT COUNT(*) FROM tracker_sync_outbox").fetchone()[0] == 1
    assert latest_plex_library_summary(db) == {
        "connected": True,
        "generated_at": "2026-08-27T15:00:00Z",
        "title_count": 2,
        "item_count": 2,
    }


def test_snapshot_is_idempotent_and_never_regresses_progress(db: sqlite3.Connection) -> None:
    user_id = _user(db)
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=166613,
        title="Hell's Paradise Season 2",
        format="TV",
        release_status="FINISHED",
        season_number=2,
        expected_episodes=12,
        aired_episodes=12,
    )
    db.commit()
    store_plex_library_snapshot(db, _snapshot(), received_at="2026-08-27T15:00:02Z")

    duplicate = store_plex_library_snapshot(
        db,
        _snapshot(),
        received_at="2026-08-27T15:01:02Z",
    )
    older_progress = _snapshot(watched_through=7)
    older_progress["generated_at"] = "2026-08-27T15:02:00Z"
    store_plex_library_snapshot(
        db,
        older_progress,
        received_at="2026-08-27T15:02:02Z",
    )

    assert duplicate["stored"] is False
    entry = db.execute(
        "SELECT progress_episodes FROM watch_entries WHERE monitor_id = ?", (monitor_id,)
    ).fetchone()
    assert entry["progress_episodes"] == 9


def test_managed_canonical_title_recovers_plex_progress_after_a_display_rename(db):
    user_id = _user(db)
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=185874,
        title="BLEACH: Thousand-Year Blood War - The Calamity",
        format="TV",
        release_status="RELEASING",
        season_number=2,
        expected_episodes=10,
        aired_episodes=8,
    )
    db.commit()
    payload = _snapshot(watched_through=3)
    payload["items"] = [
        dict(
            payload["items"][0],
            title="Bleach",
            episode_total=8,
            canonical_title="BLEACH: Thousand-Year Blood War - The Calamity",
        )
    ]
    result = store_plex_library_snapshot(db, payload, received_at="2026-08-27T15:00:02Z")
    assert result["progress_updates"] == 1
    row = db.execute("SELECT * FROM watch_entries WHERE monitor_id=?", (monitor_id,)).fetchone()
    assert (row["progress_episodes"], row["last_source"]) == (3, "plex")
    assert (
        db.execute("SELECT canonical_title FROM plex_library_items").fetchone()[0]
        == payload["items"][0]["canonical_title"]
    )


def test_managed_identity_never_falls_back_to_an_unrelated_display_title(db):
    user_id = _user(db)
    upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=166613,
        title="Hell's Paradise Season 2",
        format="TV",
        season_number=2,
        expected_episodes=12,
        aired_episodes=12,
    )
    db.commit()
    payload = _snapshot()
    payload["items"][0]["canonical_title"] = "Different canonical anime"
    result = store_plex_library_snapshot(db, payload, received_at="2026-08-27T15:00:02Z")
    assert result["matched_monitors"] == result["progress_updates"] == 0


def test_path_aware_ambiguous_items_do_not_advance_progress_via_display_alias(db):
    user_id = _user(db)
    upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=166613,
        title="Hell's Paradise Season 2",
        format="TV",
        season_number=2,
        expected_episodes=12,
        aired_episodes=12,
    )
    db.commit()
    payload = _snapshot()
    payload["items"][0].update(
        managed_paths=[{"canonical_title": "Hell's Paradise Season 2", "season_number": 2}],
        path_identity_complete=False,
    )
    result = store_plex_library_snapshot(db, payload, received_at="2026-08-27T15:00:02Z")
    assert result["matched_monitors"] == result["progress_updates"] == 0


def test_canonical_watch_claim_cannot_disagree_with_its_managed_path_reference(db):
    payload = _snapshot()
    payload["items"][0].update(
        canonical_title="Hell's Paradise Season 2",
        managed_paths=[{"canonical_title": "Different anime", "season_number": 2}],
        path_identity_complete=True,
    )
    with pytest.raises(PlexSnapshotError):
        store_plex_library_snapshot(db, payload, received_at="2026-08-27T15:00:02Z")


@pytest.mark.parametrize(
    "change",
    [
        lambda payload: payload.update(schema_version="unknown"),
        lambda payload: payload["items"][0].update(rating_key="not-a-number"),
        lambda payload: payload["items"][0].update(watched_through=13),
        lambda payload: payload["items"][0].update(extra="unknown"),
    ],
)
def test_snapshot_contract_rejects_unknown_or_unsafe_payloads(change, db) -> None:
    payload = _snapshot()
    change(payload)

    with pytest.raises(PlexSnapshotError):
        store_plex_library_snapshot(
            db,
            payload,
            received_at="2026-08-27T15:00:02Z",
        )
