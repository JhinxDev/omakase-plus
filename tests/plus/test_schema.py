"""Tests for the omakase Plus schema and database layer."""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from omakase.plus import db as plus_db
from omakase.plus.db import _db, get_db, run_migrations


@pytest.fixture(autouse=True)
def _fresh_db():
    """Provide a fresh database connection in a temp directory per test.

    Clears the module-level connection cache on teardown so subsequent
    tests start with a clean slate.
    """
    with tempfile.TemporaryDirectory() as tmp:
        conn = get_db(tmp)
        yield conn
        conn.close()
        _db.clear()


# ---------------------------------------------------------------------------
# Migration tests
# ---------------------------------------------------------------------------


def test_migration_001_applies_cleanly():
    """Applying migration 001 twice should be a no-op (no error)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "omakase-plus.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        conn.close()

        # Second connection to the same file — run migrations again
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        run_migrations(conn)
        conn.close()


def test_default_database_honors_configured_data_directory(tmp_path, monkeypatch):
    configured = tmp_path / "persistent-plus-data"
    monkeypatch.setenv("OMAKASE_DATA_DIR", str(configured))

    conn = plus_db._connect()
    try:
        assert configured.joinpath("omakase-plus.db").is_file()
        assert conn.execute("SELECT COUNT(*) FROM _migrations").fetchone()[0] >= 1
    finally:
        conn.close()


def test_all_tables_exist(_fresh_db):
    """Verify all expected tables are present after migration."""
    conn = _fresh_db
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = {row["name"] for row in cursor.fetchall()}

    expected = {
        "users",
        "discovery_preferences",
        "codex_workers",
        "codex_discovery_jobs",
        "google_login_flows",
        "google_identities",
        "user_completion_cleanup_preferences",
        "completion_plex_cleanup_blocks",
        "sessions",
        "user_secrets",
        "taste_profiles",
        "run_history",
        "anilist_plannings",
        "overseerr_requests",
        "recommendation_feedback",
        "tracker_media_actions",
        "tracker_sync_outbox",
        "download_attempts",
        "media_alias_search_receipts",
        "media_asset_observations",
        "media_asset_removals",
        "media_assets",
        "media_candidate_attempts",
        "media_catalog_health",
        "media_curator_receipt_latest",
        "media_file_probes",
        "media_monitors",
        "media_providers",
        "media_subtitle_tracks",
        "media_sync_lease",
        "media_sync_runs",
        "media_title_removal_assets",
        "media_title_removals",
        "oauth_flow_states",
        "plex_targets",
        "plex_library_items",
        "plex_library_snapshots",
        "watch_confirmations",
        "watch_entries",
        "watch_events",
        "watch_integrations",
        "movie_catalog",
        "movie_requests",
        "movie_submissions",
        "movie_verifications",
        "movie_genres",
        "movie_history",
        "movie_plex_history",
        "movie_history_events",
        "movie_discovery_cache",
        "movie_sync_cursors",
        "user_connection_health",
        "user_avatars",
        "tv_catalog",
        "tv_history",
        "tv_episodes",
        "tv_plex_snapshots",
        "anime_synopses",
        "agent_audit",
        "_migrations",  # internal tracking table
    }
    assert tables == expected, (
        f"Mismatch — missing: {expected - tables}, extra: {tables - expected}"
    )
    assert len(tables) == len(expected)


def test_recommendation_feedback_migration_runs_before_existing_lane_error(monkeypatch, tmp_path):
    """Feedback table should exist even if run_history.lane was added before migration 003."""
    first_pass_dir = tmp_path / "migrations-without-003"
    first_pass_dir.mkdir()
    migrations_dir = plus_db._MIGRATIONS_DIR

    for path in migrations_dir.iterdir():
        if path.name == "003-recommendation-intelligence.sql":
            continue
        if path.suffix == ".sql":
            (first_pass_dir / path.name).write_text(path.read_text(encoding="utf-8"))

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", first_pass_dir)
    plus_db.run_migrations(conn)

    conn.execute("ALTER TABLE run_history ADD COLUMN lane TEXT NOT NULL DEFAULT 'best_match'")
    conn.commit()

    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", migrations_dir)
    plus_db.run_migrations(conn)

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("recommendation_feedback",),
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------


def test_user_roundtrip(_fresh_db):
    """Insert a user and read it back, verifying email."""
    conn = _fresh_db
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("alice@example.com", "hashed_pw_123"),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM users WHERE email = ?", ("alice@example.com",)).fetchone()
    assert row is not None
    assert row["email"] == "alice@example.com"
    assert row["password_hash"] == "hashed_pw_123"
    assert row["id"] == 1
    assert row["created_at"] is not None


def test_session_roundtrip(_fresh_db):
    """Insert a session with a FK to an existing user and read it back."""
    conn = _fresh_db
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("bob@example.com", "hashed_pw_456"),
    )
    conn.commit()
    (user_id,) = conn.execute(
        "SELECT id FROM users WHERE email = ?", ("bob@example.com",)
    ).fetchone()

    session_id = "sess_abc123"
    conn.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (session_id, user_id, "2026-06-01T00:00:00"),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    assert row is not None
    assert row["id"] == session_id
    assert row["user_id"] == user_id
    assert row["expires_at"] == "2026-06-01T00:00:00"
    assert row["created_at"] is not None


def test_user_secret_unique_constraint(_fresh_db):
    """Inserting two secrets with the same (user_id, key_name) must fail."""
    conn = _fresh_db
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("carol@example.com", "hashed_pw_789"),
    )
    conn.commit()
    (user_id,) = conn.execute(
        "SELECT id FROM users WHERE email = ?", ("carol@example.com",)
    ).fetchone()

    conn.execute(
        "INSERT INTO user_secrets (user_id, key_name, encrypted_value) VALUES (?, ?, ?)",
        (user_id, "anilist_token", "encrypted_value_1"),
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO user_secrets (user_id, key_name, encrypted_value) VALUES (?, ?, ?)",
            (user_id, "anilist_token", "encrypted_value_2"),
        )
        conn.commit()


def test_run_history_roundtrip(_fresh_db):
    """Insert a run_history record and read it back."""
    conn = _fresh_db
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("dave@example.com", "hashed_pw_000"),
    )
    conn.commit()
    (user_id,) = conn.execute(
        "SELECT id FROM users WHERE email = ?", ("dave@example.com",)
    ).fetchone()

    conn.execute(
        "INSERT INTO run_history (user_id, source, model, picks) VALUES (?, ?, ?, ?)",
        (user_id, "anilist", "gpt-4o-mini", '["Anime A", "Anime B"]'),
    )
    conn.commit()

    rows = conn.execute("SELECT * FROM run_history WHERE user_id = ?", (user_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "anilist"
    assert rows[0]["model"] == "gpt-4o-mini"
    assert rows[0]["picks"] == '["Anime A", "Anime B"]'


def test_recommendation_feedback_roundtrip(_fresh_db):
    conn = _fresh_db
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("feedback@example.com", "hash"),
    ).lastrowid
    conn.execute(
        "INSERT INTO run_history (id, user_id, source, model, picks) VALUES (?, ?, ?, ?, ?)",
        (7, user_id, "anilist", "gpt-4o-mini", "[]"),
    )
    conn.execute(
        """INSERT INTO recommendation_feedback
           (user_id, source, media_id, title, feedback_type, run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, "anilist", 123, "Base 2", "wrong_sequel", 7),
    )
    row = conn.execute(
        "SELECT * FROM recommendation_feedback WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    assert row["feedback_type"] == "wrong_sequel"


# ---------------------------------------------------------------------------
# Constraint / cascade tests
# ---------------------------------------------------------------------------


def test_cascade_delete(_fresh_db):
    """Deleting a user should cascade-delete their sessions."""
    conn = _fresh_db
    conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("eve@example.com", "hashed_pw_cascade"),
    )
    conn.commit()
    (user_id,) = conn.execute(
        "SELECT id FROM users WHERE email = ?", ("eve@example.com",)
    ).fetchone()

    conn.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        ("sess_cascade", user_id, "2026-07-01T00:00:00"),
    )
    conn.commit()

    # Verify session exists before delete
    (before,) = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
    assert before == 1

    # Delete the user — cascade should remove the session
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()

    (after,) = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)
    ).fetchone()
    assert after == 0


def test_download_attempts_cascade_with_planning(_fresh_db):
    """Deleting a planning row should remove its RD attempt telemetry."""
    conn = _fresh_db
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("attempts@example.com", "hash"),
    ).lastrowid
    planning_id = conn.execute(
        """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
           VALUES (?, ?, ?, ?)""",
        (user_id, 170732, "BLEACH: Thousand-Year Blood War - The Conflict", "PLANNING"),
    ).lastrowid
    conn.execute(
        """INSERT INTO download_attempts
           (user_id, anilist_planning_id, request_id, candidate_rank, total_candidates,
            torrent_title, torrent_hash, seeders, size_display, is_batch, status,
            http_status, error_code, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user_id,
            planning_id,
            "req-test",
            1,
            1,
            "[Group] Bleach 451 [1080p]",
            "ABCDEF1234567890",
            44,
            "1.4 GiB",
            0,
            "provider_block",
            451,
            "infringing_file",
            "Provider blocked this file",
        ),
    )
    conn.commit()

    conn.execute("DELETE FROM anilist_plannings WHERE id = ?", (planning_id,))
    conn.commit()

    (remaining,) = conn.execute(
        "SELECT COUNT(*) FROM download_attempts WHERE anilist_planning_id = ?",
        (planning_id,),
    ).fetchone()
    assert remaining == 0
