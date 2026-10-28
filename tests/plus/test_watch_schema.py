"""Schema contracts for durable owner-scoped watch progress."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.db import run_migrations


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_watch_schema_is_owner_scoped_and_restart_safe(db: sqlite3.Connection) -> None:
    tables = {
        row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {
        "watch_entries",
        "watch_events",
        "watch_confirmations",
        "tracker_sync_outbox",
        "watch_integrations",
    } <= tables

    run_migrations(db)

    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def _insert_owner_monitor(
    db: sqlite3.Connection,
    *,
    email: str,
    anilist_id: int,
    status: str = "PLANNING",
    season_number: int = 1,
    expected_episodes: int = 12,
) -> tuple[int, int]:
    user_id = int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, 'hash')",
            (email,),
        ).lastrowid
    )
    planning_id = int(
        db.execute(
            """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
               VALUES (?, ?, ?, ?)""",
            (user_id, anilist_id, f"Title {anilist_id}", status),
        ).lastrowid
    )
    monitor_id = int(
        db.execute(
            """INSERT INTO media_monitors
               (user_id, anilist_planning_id, anilist_id, title, season_number,
                expected_episodes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                planning_id,
                anilist_id,
                f"Title {anilist_id}",
                season_number,
                expected_episodes,
            ),
        ).lastrowid
    )
    db.commit()
    return user_id, monitor_id


def test_watch_backfill_preserves_completed_planning_without_external_actions(
    tmp_path, monkeypatch
) -> None:
    migrations_before_watch = tmp_path / "migrations-before-watch"
    migrations_before_watch.mkdir()
    canonical_migrations = plus_db._MIGRATIONS_DIR
    for path in canonical_migrations.glob("*.sql"):
        if int(path.name.split("-", 1)[0]) < 14:
            shutil.copy2(path, migrations_before_watch / path.name)

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", migrations_before_watch)
    run_migrations(connection)
    _insert_owner_monitor(
        connection,
        email="completed@example.com",
        anilist_id=151807,
        status="COMPLETED",
        expected_episodes=12,
    )

    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", canonical_migrations)
    run_migrations(connection)

    row = connection.execute(
        """SELECT status, progress_episodes, episode_total
           FROM watch_entries WHERE anilist_id = ?""",
        (151807,),
    ).fetchone()
    assert dict(row) == {
        "status": "completed",
        "progress_episodes": 12,
        "episode_total": 12,
    }
    assert connection.execute("SELECT count(*) FROM tracker_sync_outbox").fetchone()[0] == 0
    assert connection.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0
    connection.close()


def test_watch_entry_rejects_cross_owner_monitor_binding(db: sqlite3.Connection) -> None:
    _, first_monitor_id = _insert_owner_monitor(db, email="one@example.com", anilist_id=100)
    second_user_id, _ = _insert_owner_monitor(db, email="two@example.com", anilist_id=200)

    with pytest.raises(sqlite3.IntegrityError, match="watch entry owner"):
        db.execute(
            """INSERT INTO watch_entries
               (user_id, monitor_id, anilist_id, season_number)
               VALUES (?, ?, ?, ?)""",
            (second_user_id, first_monitor_id, 999, 1),
        )


def test_watch_entry_identity_and_progress_constraints(db: sqlite3.Connection) -> None:
    user_id, monitor_id = _insert_owner_monitor(db, email="constraints@example.com", anilist_id=300)
    db.execute(
        """INSERT INTO watch_entries
           (user_id, monitor_id, anilist_id, season_number, episode_total)
           VALUES (?, ?, ?, 1, 12)""",
        (user_id, monitor_id, 300),
    )

    invalid_rows = (
        (user_id, None, 300, 1, "unwatched", 0, 12, "manual", None),
        (user_id, None, 301, 1, "watching", -1, 12, "manual", None),
        (user_id, None, 302, 1, "watching", 13, 12, "manual", None),
        (user_id, None, 303, 1, "completed", 12, 12, "manual", None),
        (user_id, None, 304, 1, "watching", 1, 12, "crunchyroll", None),
    )
    for row in invalid_rows:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO watch_entries
                   (user_id, monitor_id, anilist_id, season_number, status,
                    progress_episodes, episode_total, last_source, completed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                row,
            )


def test_watch_event_owner_and_event_key_are_constrained(db: sqlite3.Connection) -> None:
    first_user_id, first_monitor_id = _insert_owner_monitor(
        db, email="events-one@example.com", anilist_id=400
    )
    second_user_id, _ = _insert_owner_monitor(db, email="events-two@example.com", anilist_id=500)
    entry_id = int(
        db.execute(
            """INSERT INTO watch_entries
               (user_id, monitor_id, anilist_id, season_number)
               VALUES (?, ?, ?, 1)""",
            (first_user_id, first_monitor_id, 400),
        ).lastrowid
    )

    with pytest.raises(sqlite3.IntegrityError, match="watch event owner"):
        db.execute(
            """INSERT INTO watch_events
               (watch_entry_id, user_id, source, event_key, kind, disposition, observed_at)
               VALUES (?, ?, 'plex', 'session:e1', 'progress', 'accepted', ?)""",
            (entry_id, second_user_id, "2026-08-27T12:00:00Z"),
        )

    db.execute(
        """INSERT INTO watch_events
           (watch_entry_id, user_id, source, event_key, kind, disposition, observed_at)
           VALUES (?, ?, 'plex', 'session:e1', 'progress', 'accepted', ?)""",
        (entry_id, first_user_id, "2026-08-27T12:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO watch_events
               (watch_entry_id, user_id, source, event_key, kind, disposition, observed_at)
               VALUES (?, ?, 'plex', 'session:e1', 'progress', 'ignored', ?)""",
            (entry_id, first_user_id, "2026-08-27T12:01:00Z"),
        )
