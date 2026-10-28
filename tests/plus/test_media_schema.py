"""Contract tests for the media-orchestrator persistence schema."""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from omakase.plus import db as plus_db

MEDIA_TABLE_COLUMNS = {
    "media_monitors": {
        "id",
        "user_id",
        "anilist_planning_id",
        "anilist_id",
        "title",
        "search_titles_json",
        "genres_json",
        "format",
        "release_status",
        "season_number",
        "expected_episodes",
        "aired_episodes",
        "next_airing_at",
        "active",
        "keep_completed_in_plex",
        "last_checked_at",
        "last_success_at",
        "next_check_at",
        "consecutive_failures",
        "last_result",
        "created_at",
        "updated_at",
    },
    "media_sync_runs": {
        "id",
        "monitor_id",
        "outcome",
        "required_count",
        "covered_count",
        "accepted_count",
        "detail",
        "created_at",
        "completed_at",
    },
    "media_assets": {
        "id",
        "monitor_id",
        "torrent_hash",
        "rd_torrent_id",
        "provider_id",
        "provider_asset_id",
        "storage_backend",
        "storage_locator",
        "source_title",
        "source_folder",
        "quality",
        "seeders",
        "ownership",
        "provenance",
        "created_by_run_id",
        "shared_reference_count",
        "downloaded_once",
        "removable",
        "replaces_asset_id",
        "replaced_by_asset_id",
        "planned_coverage_json",
        "usable_coverage_json",
        "unusable_coverage_json",
        "numbering_override",
        "inventory_fingerprint",
        "inventory_verified_at",
        "requires_strict_inventory",
        "status",
        "progress",
        "stagnant_checks",
        "last_error",
        "created_at",
        "updated_at",
    },
    "media_candidate_attempts": {
        "id",
        "monitor_id",
        "sync_run_id",
        "asset_id",
        "search_alias",
        "source_title",
        "torrent_hash",
        "candidate_rank",
        "quality",
        "seeders",
        "proposed_coverage_json",
        "decision",
        "reason",
        "rd_outcome",
        "provider_outcome",
        "provider_kind",
        "catalog_kind",
        "instant_checked",
        "instant_available",
        "terminal_at",
        "created_at",
    },
    "media_alias_search_receipts": {
        "id",
        "monitor_id",
        "sync_run_id",
        "query_version",
        "search_alias",
        "started_at",
        "completed_at",
        "candidate_count",
        "eligible_1080p_count",
        "attempted_1080p_count",
        "cursor",
        "provider_complete",
        "exhaustion_outcome",
    },
    "media_asset_observations": {
        "id",
        "asset_id",
        "sync_run_id",
        "observed_at",
        "rd_status",
        "provider_status",
        "bytes_done",
        "progress",
    },
    "media_asset_removals": {
        "id",
        "operation_key",
        "asset_id",
        "replacement_asset_id",
        "candidate_attempt_id",
        "sync_run_id",
        "support_observation_oldest_id",
        "support_observation_newest_id",
        "requested_at",
        "completed_at",
        "outcome",
        "detail",
        "proof_json",
    },
    "media_sync_lease": {
        "id",
        "owner",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
        "entrypoint",
        "prior_owner_hash",
        "prior_expires_at",
    },
    "media_curator_receipt_latest": {
        "id",
        "receipt_json",
        "receipt_hash",
        "curator_run_id",
        "outcome",
        "received_at",
        "completed_at",
    },
    "media_providers": {
        "id",
        "user_id",
        "kind",
        "label",
        "enabled",
        "config_json",
        "capabilities_json",
        "health_status",
        "health_reason_code",
        "health_detail",
        "last_health_at",
        "created_at",
        "updated_at",
    },
    "media_file_probes": {
        "id",
        "asset_id",
        "file_locator",
        "inventory_fingerprint",
        "byte_range_receipt_json",
        "video_streams_json",
        "audio_streams_json",
        "outcome",
        "detail",
        "verified_at",
        "created_at",
    },
    "media_subtitle_tracks": {
        "id",
        "probe_id",
        "stream_index",
        "language",
        "codec",
        "switchable",
        "default_track",
        "forced",
        "source_kind",
        "sync_policy",
        "created_at",
    },
    "plex_targets": {
        "id",
        "user_id",
        "label",
        "base_url",
        "token_secret_name",
        "library_map_json",
        "publication_policy",
        "health_status",
        "health_reason_code",
        "health_detail",
        "last_health_at",
        "created_at",
        "updated_at",
    },
}


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def user_id(db):
    return db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("media@example.com", "hash"),
    ).lastrowid


@pytest.fixture
def monitor_id(db, user_id):
    return db.execute(
        """INSERT INTO media_monitors
           (user_id, anilist_id, title, release_status, season_number)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, 178789, "Mushoku Tensei Season 3", "RELEASING", 3),
    ).lastrowid


@pytest.fixture
def sync_run_id(db, monitor_id):
    return db.execute(
        """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
           VALUES (?, ?, ?)""",
        (monitor_id, "completed", "2026-07-14T11:05:00Z"),
    ).lastrowid


@pytest.fixture
def asset_id(db, monitor_id, sync_run_id):
    return db.execute(
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance, created_by_run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            "0123456789ABCDEF",
            "[Group] Mushoku Tensei S03",
            "monitor_owned",
            "omakase_created",
            sync_run_id,
        ),
    ).lastrowid


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _foreign_key_parents(db: sqlite3.Connection, table: str) -> dict[str, str]:
    return {row["from"]: row["table"] for row in db.execute(f"PRAGMA foreign_key_list({table})")}


def _assert_integrity_error(db: sqlite3.Connection, sql: str, values: tuple[object, ...]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, values)


def _latest_support_observation_ids(db: sqlite3.Connection, asset_id: int) -> tuple[int, int]:
    newest, oldest = db.execute(
        """SELECT id
           FROM media_asset_observations
           WHERE asset_id = ?
           ORDER BY strftime('%s', observed_at) DESC, id DESC
           LIMIT 2""",
        (asset_id,),
    ).fetchall()
    return oldest["id"], newest["id"]


def test_migration_upgrades_current_schema_and_is_idempotent(monkeypatch, tmp_path):
    current_dir = tmp_path / "current-migrations"
    current_dir.mkdir()
    migrations_dir = plus_db._MIGRATIONS_DIR
    for path in migrations_dir.glob("*.sql"):
        if not path.name.startswith(
            ("005-", "006-", "007-", "008-", "011-", "014-", "016-", "020-", "021-")
        ):
            shutil.copy2(path, current_dir / path.name)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", current_dir)
    plus_db.run_migrations(conn)
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("upgrade@example.com", "hash"),
    ).lastrowid

    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", migrations_dir)
    plus_db.run_migrations(conn)
    plus_db.run_migrations(conn)

    assert conn.execute("SELECT email FROM users WHERE id = ?", (user_id,)).fetchone()[0] == (
        "upgrade@example.com"
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE name = '005-media-orchestrator.sql'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE name = '006-acquisition-reliability.sql'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE name = '008-provider-neutral-media.sql'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE name = '014-watch-state.sql'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE name = '020-strict-metadata-inventory.sql'"
        ).fetchone()[0]
        == 1
    )
    assert "status" in _columns(conn, "watch_entries")
    assert _columns(conn, "media_asset_removals") == MEDIA_TABLE_COLUMNS["media_asset_removals"]
    conn.close()


def test_strict_inventory_migration_preserves_legacy_assets_and_flags_on_repeat(
    monkeypatch, tmp_path
):
    legacy_dir = tmp_path / "before-strict-inventory"
    legacy_dir.mkdir()
    migrations_dir = plus_db._MIGRATIONS_DIR
    for path in migrations_dir.glob("*.sql"):
        if not path.name.startswith("020-"):
            shutil.copy2(path, legacy_dir / path.name)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", legacy_dir)
    plus_db.run_migrations(conn)
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES ('metadata-upgrade@example.com', 'hash')"
    ).lastrowid
    monitor_id = conn.execute(
        "INSERT INTO media_monitors (user_id, anilist_id, title) VALUES (?, 999, 'Legacy Show')",
        (user_id,),
    ).lastrowid
    for torrent_hash, status, progress in (("A" * 40, "downloaded", 100), ("B" * 40, "queued", 0)):
        conn.execute(
            """INSERT INTO media_assets
               (monitor_id, torrent_hash, source_title, status, progress, downloaded_once,
                planned_coverage_json, usable_coverage_json)
               VALUES (?, ?, 'Legacy Show S01E01', ?, ?, ?, '[1]', ?)""",
            (
                monitor_id,
                torrent_hash,
                status,
                progress,
                int(status == "downloaded"),
                "[1]" if status == "downloaded" else "[]",
            ),
        )
    before = [dict(row) for row in conn.execute("SELECT * FROM media_assets ORDER BY id")]
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", migrations_dir)
    plus_db.run_migrations(conn)
    after = [dict(row) for row in conn.execute("SELECT * FROM media_assets ORDER BY id")]
    assert [row.pop("requires_strict_inventory") for row in after] == [0, 0]
    assert after == before
    conn.execute(
        "UPDATE media_assets SET requires_strict_inventory = 1 WHERE torrent_hash = ?", ("B" * 40,)
    )
    plus_db.run_migrations(conn)
    assert [
        row[0]
        for row in conn.execute("SELECT requires_strict_inventory FROM media_assets ORDER BY id")
    ] == [0, 1]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM _migrations WHERE name = '020-strict-metadata-inventory.sql'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_provider_migration_backfills_existing_realdebrid_evidence(monkeypatch, tmp_path):
    legacy_dir = tmp_path / "legacy-migrations"
    legacy_dir.mkdir()
    migrations_dir = plus_db._MIGRATIONS_DIR
    for path in migrations_dir.glob("*.sql"):
        if not path.name.startswith("008-"):
            shutil.copy2(path, legacy_dir / path.name)

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", legacy_dir)
    plus_db.run_migrations(conn)
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES ('legacy@example.com', 'hash')"
    ).lastrowid
    monitor_id = conn.execute(
        """INSERT INTO media_monitors
           (user_id, anilist_id, title, season_number)
           VALUES (?, 100, 'Legacy Show', 1)""",
        (user_id,),
    ).lastrowid
    run_id = conn.execute(
        """INSERT INTO media_sync_runs
           (monitor_id, outcome, completed_at)
           VALUES (?, 'complete', '2026-07-26T04:00:00Z')""",
        (monitor_id,),
    ).lastrowid
    asset_id = conn.execute(
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, rd_torrent_id, source_title, source_folder,
            status, downloaded_once)
           VALUES (?, 'ABCDEF', 'rd-legacy', 'Legacy release', 'Legacy folder',
                   'downloaded', 1)""",
        (monitor_id,),
    ).lastrowid
    attempt_id = conn.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
            decision, rd_outcome, terminal_at)
           VALUES (?, ?, ?, 'Legacy', 'ABCDEF', 'ACCEPTED', 'downloaded',
                   '2026-07-26T04:00:00Z')""",
        (monitor_id, run_id, asset_id),
    ).lastrowid
    observation_id = conn.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status)
           VALUES (?, ?, '2026-07-26T04:00:00Z', 'downloaded')""",
        (asset_id, run_id),
    ).lastrowid
    conn.commit()

    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", migrations_dir)
    plus_db.run_migrations(conn)

    provider = conn.execute("SELECT * FROM media_providers").fetchone()
    asset = conn.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    attempt = conn.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (attempt_id,)
    ).fetchone()
    observation = conn.execute(
        "SELECT * FROM media_asset_observations WHERE id = ?", (observation_id,)
    ).fetchone()
    assert provider["user_id"] == user_id
    assert provider["kind"] == "realdebrid"
    assert asset["provider_id"] == provider["id"]
    assert asset["provider_asset_id"] == "rd-legacy"
    assert asset["storage_backend"] == "zurg"
    assert asset["storage_locator"] == "Legacy folder"
    assert attempt["provider_outcome"] == "downloaded"
    assert observation["provider_status"] == "downloaded"
    conn.close()


def test_media_tables_have_contract_columns(db):
    assert {table: _columns(db, table) for table in MEDIA_TABLE_COLUMNS} == MEDIA_TABLE_COLUMNS


def test_media_foreign_keys_point_to_expected_parents(db):
    expected = {
        "media_monitors": {"user_id": "users", "anilist_planning_id": "anilist_plannings"},
        "media_sync_runs": {"monitor_id": "media_monitors"},
        "media_assets": {
            "monitor_id": "media_monitors",
            "created_by_run_id": "media_sync_runs",
            "replaces_asset_id": "media_assets",
            "replaced_by_asset_id": "media_assets",
            "provider_id": "media_providers",
        },
        "media_candidate_attempts": {
            "monitor_id": "media_monitors",
            "sync_run_id": "media_sync_runs",
            "asset_id": "media_assets",
        },
        "media_alias_search_receipts": {
            "monitor_id": "media_monitors",
            "sync_run_id": "media_sync_runs",
        },
        "media_asset_observations": {
            "asset_id": "media_assets",
            "sync_run_id": "media_sync_runs",
        },
        "media_asset_removals": {
            "asset_id": "media_assets",
            "replacement_asset_id": "media_assets",
            "candidate_attempt_id": "media_candidate_attempts",
            "sync_run_id": "media_sync_runs",
            "support_observation_oldest_id": "media_asset_observations",
            "support_observation_newest_id": "media_asset_observations",
        },
        "media_providers": {"user_id": "users"},
        "media_file_probes": {"asset_id": "media_assets"},
        "media_subtitle_tracks": {"probe_id": "media_file_probes"},
        "plex_targets": {"user_id": "users"},
    }
    for table, parents in expected.items():
        assert _foreign_key_parents(db, table) == parents


def test_evidence_foreign_keys_use_deferred_no_action(db):
    expected_actions = {
        "media_assets": {"created_by_run_id": "NO ACTION"},
        "media_candidate_attempts": {"sync_run_id": "NO ACTION"},
        "media_alias_search_receipts": {"sync_run_id": "NO ACTION"},
        "media_asset_observations": {"sync_run_id": "NO ACTION"},
        "media_asset_removals": {
            "asset_id": "CASCADE",
            "replacement_asset_id": "NO ACTION",
            "candidate_attempt_id": "NO ACTION",
            "sync_run_id": "NO ACTION",
            "support_observation_oldest_id": "NO ACTION",
            "support_observation_newest_id": "NO ACTION",
        },
    }
    for table, actions in expected_actions.items():
        foreign_keys = {
            row["from"]: row["on_delete"] for row in db.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert {column: foreign_keys[column] for column in actions} == actions

        create_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()[0]
        for column, action in actions.items():
            if action == "NO ACTION":
                declaration = create_sql.split(column, 1)[1].split(",", 1)[0]
                assert "DEFERRABLE INITIALLY DEFERRED" in declaration


def test_monitor_is_unique_per_user_and_anilist_id(db, user_id):
    values = (user_id, 178789, "Mushoku Tensei Season 3", "RELEASING", 3)
    sql = """INSERT INTO media_monitors
             (user_id, anilist_id, title, release_status, season_number)
             VALUES (?, ?, ?, ?, ?)"""
    db.execute(sql, values)
    _assert_integrity_error(db, sql, values)


@pytest.mark.parametrize("column", ["required_count", "covered_count", "accepted_count"])
def test_sync_run_counts_cannot_be_negative(db, monitor_id, column):
    sql = f"INSERT INTO media_sync_runs (monitor_id, outcome, {column}) VALUES (?, ?, ?)"
    _assert_integrity_error(db, sql, (monitor_id, "invalid", -1))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("active", 2),
        ("aired_episodes", -1),
        ("expected_episodes", -1),
        ("consecutive_failures", -1),
    ],
)
def test_monitor_boolean_and_numeric_bounds(db, user_id, column, value):
    sql = f"""INSERT INTO media_monitors
              (user_id, anilist_id, title, {column}) VALUES (?, ?, ?, ?)"""
    _assert_integrity_error(db, sql, (user_id, 200000 + abs(value), "Invalid", value))


@pytest.mark.parametrize(
    "column",
    ["season_number", "expected_episodes", "aired_episodes", "active", "consecutive_failures"],
)
@pytest.mark.parametrize("value", ["not-an-integer", 1.5])
def test_monitor_integer_affinity(db, user_id, column, value):
    sql = f"""INSERT INTO media_monitors
              (user_id, anilist_id, title, {column}) VALUES (?, ?, ?, ?)"""
    _assert_integrity_error(db, sql, (user_id, 300000, "Invalid affinity", value))


@pytest.mark.parametrize("column", ["required_count", "covered_count", "accepted_count"])
@pytest.mark.parametrize("value", ["not-an-integer", 1.5])
def test_sync_run_integer_affinity(db, monitor_id, column, value):
    sql = f"INSERT INTO media_sync_runs (monitor_id, outcome, {column}) VALUES (?, ?, ?)"
    _assert_integrity_error(db, sql, (monitor_id, "invalid-affinity", value))


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("ownership", "borrowed"),
        ("provenance", "unknown"),
        ("shared_reference_count", -1),
        ("downloaded_once", 2),
        ("removable", -1),
        ("seeders", -1),
        ("progress", -0.01),
        ("progress", 100.01),
        ("stagnant_checks", -1),
    ],
)
def test_asset_closed_domains_and_bounds(db, monitor_id, column, value):
    sql = f"""INSERT INTO media_assets
              (monitor_id, torrent_hash, source_title, {column})
              VALUES (?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (monitor_id, f"INVALID-{column}-{value}", "Invalid", value),
    )


@pytest.mark.parametrize(
    "column",
    ["seeders", "shared_reference_count", "downloaded_once", "removable", "stagnant_checks"],
)
@pytest.mark.parametrize("value", ["not-an-integer", 1.5])
def test_asset_integer_affinity(db, monitor_id, column, value):
    sql = f"""INSERT INTO media_assets
              (monitor_id, torrent_hash, source_title, {column}) VALUES (?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (monitor_id, f"INVALID-AFFINITY-{column}-{value}", "Invalid affinity", value),
    )


def test_asset_progress_rejects_nonnumeric_text(db, monitor_id):
    _assert_integrity_error(
        db,
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, progress) VALUES (?, ?, ?, ?)""",
        (monitor_id, "INVALID-PROGRESS", "Invalid affinity", "not-a-number"),
    )


def test_omakase_created_asset_requires_creation_run(db, monitor_id):
    _assert_integrity_error(
        db,
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance)
           VALUES (?, ?, ?, ?, ?)""",
        (monitor_id, "NO-RUN", "Invalid", "monitor_owned", "omakase_created"),
    )


def test_downloaded_asset_requires_downloaded_once(db, monitor_id):
    _assert_integrity_error(
        db,
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (monitor_id, "NOT-DOWNLOADED", "Invalid", "external", "pre_existing", "downloaded"),
    )


def test_asset_is_unique_per_monitor_and_torrent_hash(db, monitor_id):
    sql = """INSERT INTO media_assets
             (monitor_id, torrent_hash, source_title, ownership, provenance)
             VALUES (?, ?, ?, ?, ?)"""
    values = (monitor_id, "SAME-HASH", "Candidate", "external", "pre_existing")
    db.execute(sql, values)
    _assert_integrity_error(db, sql, values)


def test_downloaded_once_is_monotonic(db, asset_id):
    db.execute("UPDATE media_assets SET downloaded_once = 1 WHERE id = ?", (asset_id,))
    with pytest.raises(sqlite3.IntegrityError, match="^media asset downloaded_once regression$"):
        db.execute("UPDATE media_assets SET downloaded_once = 0 WHERE id = ?", (asset_id,))


def test_direct_removable_insert_is_rejected(db, monitor_id):
    with pytest.raises(sqlite3.IntegrityError, match="^media asset removable assertion invalid$"):
        db.execute(
            """INSERT INTO media_assets
               (monitor_id, torrent_hash, source_title, removable) VALUES (?, ?, ?, ?)""",
            (monitor_id, "DIRECT-REMOVABLE", "Direct removable", 1),
        )


def test_direct_removable_update_without_pending_receipt_is_rejected(db, asset_id):
    with pytest.raises(sqlite3.IntegrityError, match="^media asset removable assertion invalid$"):
        db.execute("UPDATE media_assets SET removable = 1 WHERE id = ?", (asset_id,))


@pytest.mark.parametrize("column", ["instant_checked", "instant_available"])
def test_candidate_attempt_booleans_are_closed(db, monitor_id, sync_run_id, column):
    sql = f"""INSERT INTO media_candidate_attempts
              (monitor_id, sync_run_id, search_alias, torrent_hash, decision, {column})
              VALUES (?, ?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (monitor_id, sync_run_id, "Mushoku", "HASH", "rejected", 2),
    )


def test_candidate_attempt_deterministic_key_is_unique(db, monitor_id, sync_run_id):
    sql = """INSERT INTO media_candidate_attempts
             (monitor_id, sync_run_id, search_alias, torrent_hash, decision)
             VALUES (?, ?, ?, ?, ?)"""
    values = (monitor_id, sync_run_id, "Mushoku", "HASH", "rejected")
    db.execute(sql, values)
    _assert_integrity_error(db, sql, values)


@pytest.mark.parametrize(("column", "value"), [("candidate_rank", -1), ("seeders", -1)])
def test_candidate_attempt_numeric_bounds(db, monitor_id, sync_run_id, column, value):
    sql = f"""INSERT INTO media_candidate_attempts
              (monitor_id, sync_run_id, search_alias, decision, {column})
              VALUES (?, ?, ?, ?, ?)"""
    _assert_integrity_error(db, sql, (monitor_id, sync_run_id, "Mushoku", "rejected", value))


@pytest.mark.parametrize(
    "column", ["candidate_rank", "seeders", "instant_checked", "instant_available"]
)
@pytest.mark.parametrize("value", ["not-an-integer", 1.5])
def test_candidate_attempt_integer_affinity(db, monitor_id, sync_run_id, column, value):
    sql = f"""INSERT INTO media_candidate_attempts
              (monitor_id, sync_run_id, search_alias, decision, {column})
              VALUES (?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (monitor_id, sync_run_id, f"Invalid-{column}-{value}", "rejected", value),
    )


def test_alias_search_receipt_is_unique_per_run_and_alias(db, monitor_id, sync_run_id):
    sql = """INSERT INTO media_alias_search_receipts
             (monitor_id, sync_run_id, query_version, search_alias, started_at)
             VALUES (?, ?, ?, ?, ?)"""
    values = (monitor_id, sync_run_id, "v1", "Mushoku", "2026-07-15T12:00:00Z")
    db.execute(sql, values)
    _assert_integrity_error(db, sql, values)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("provider_complete", 2),
        ("candidate_count", -1),
        ("eligible_1080p_count", -1),
        ("attempted_1080p_count", -1),
    ],
)
def test_alias_search_receipt_bounds(db, monitor_id, sync_run_id, column, value):
    sql = f"""INSERT INTO media_alias_search_receipts
              (monitor_id, sync_run_id, query_version, search_alias, started_at, {column})
              VALUES (?, ?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (monitor_id, sync_run_id, "v1", "Mushoku", "2026-07-15T12:00:00Z", value),
    )


@pytest.mark.parametrize(
    "column",
    ["candidate_count", "eligible_1080p_count", "attempted_1080p_count", "provider_complete"],
)
@pytest.mark.parametrize("value", ["not-an-integer", 1.5])
def test_alias_search_receipt_integer_affinity(db, monitor_id, sync_run_id, column, value):
    sql = f"""INSERT INTO media_alias_search_receipts
              (monitor_id, sync_run_id, query_version, search_alias, started_at, {column})
              VALUES (?, ?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (
            monitor_id,
            sync_run_id,
            "v1",
            f"Invalid-{column}-{value}",
            "2026-07-15T12:00:00Z",
            value,
        ),
    )


def test_asset_observation_status_domain_is_closed(db, asset_id, sync_run_id):
    _assert_integrity_error(
        db,
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "missing"),
    )


@pytest.mark.parametrize(
    ("column", "value"), [("bytes_done", -1), ("progress", -0.01), ("progress", 100.01)]
)
def test_asset_observation_numeric_bounds(db, asset_id, sync_run_id, column, value):
    sql = f"""INSERT INTO media_asset_observations
              (asset_id, sync_run_id, observed_at, rd_status, {column})
              VALUES (?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued", value),
    )


@pytest.mark.parametrize("value", ["not-an-integer", 1.5])
def test_asset_observation_bytes_done_integer_affinity(db, asset_id, sync_run_id, value):
    _assert_integrity_error(
        db,
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, bytes_done)
           VALUES (?, ?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued", value),
    )


def test_asset_observation_progress_rejects_nonnumeric_text(db, asset_id, sync_run_id):
    _assert_integrity_error(
        db,
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, progress)
           VALUES (?, ?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued", "not-a-number"),
    )


def test_asset_observation_is_unique_per_asset_and_run(db, asset_id, sync_run_id):
    sql = """INSERT INTO media_asset_observations
             (asset_id, sync_run_id, observed_at, rd_status)
             VALUES (?, ?, ?, ?)"""
    values = (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued")
    db.execute(sql, values)
    _assert_integrity_error(db, sql, values)


def test_observation_requires_completed_sync_run(db, monitor_id, asset_id):
    incomplete_run_id = db.execute(
        "INSERT INTO media_sync_runs (monitor_id, outcome) VALUES (?, ?)",
        (monitor_id, "running"),
    ).lastrowid
    sql = """INSERT INTO media_asset_observations
             (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)"""
    values = (asset_id, incomplete_run_id, "2026-07-14T05:00:00Z", "queued")
    with pytest.raises(sqlite3.IntegrityError, match="^media observation run incomplete$"):
        db.execute(sql, values)

    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        ("2026-07-14T05:01:00Z", incomplete_run_id),
    )
    db.execute(sql, values)


def test_observation_update_is_rejected(db, asset_id, sync_run_id):
    observation_id = db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued"),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="^media observation immutable$"):
        db.execute(
            "UPDATE media_asset_observations SET observed_at = ? WHERE id = ?",
            ("2026-07-14T05:30:00Z", observation_id),
        )


def test_observation_delete_is_rejected(db, asset_id, sync_run_id):
    observation_id = db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued"),
    ).lastrowid
    with pytest.raises(sqlite3.IntegrityError, match="^media observation immutable$"):
        db.execute("DELETE FROM media_asset_observations WHERE id = ?", (observation_id,))


@pytest.mark.parametrize(
    "observed_at",
    [
        "not-a-time",
        "2026-07-14T05:00:00+00:00",
        "2026-07-14T05:00:00.000Z",
        "2026-07-14T24:00:00Z",
        "2026-02-30T05:00:00Z",
        "2999-01-01T00:00:00Z",
        "2026-07-14T11:05:01Z",
    ],
)
def test_observation_rejects_noncanonical_future_or_misordered_time(
    db, asset_id, sync_run_id, observed_at
):
    with pytest.raises(sqlite3.IntegrityError, match="^media observation timestamp invalid$"):
        db.execute(
            """INSERT INTO media_asset_observations
               (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)""",
            (asset_id, sync_run_id, observed_at, "queued"),
        )


@pytest.mark.parametrize(
    "run_completed_at",
    [
        "not-a-time",
        "2026-07-14T06:00:00+00:00",
        "2026-07-14T06:00:00.000Z",
        "2026-07-14T24:00:00Z",
        "2026-02-30T06:00:00Z",
        "2999-01-01T00:00:00Z",
    ],
)
def test_observation_rejects_noncanonical_or_future_run_completion(
    db, asset_id, sync_run_id, run_completed_at
):
    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        (run_completed_at, sync_run_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="^media observation timestamp invalid$"):
        db.execute(
            """INSERT INTO media_asset_observations
               (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)""",
            (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "queued"),
        )


@pytest.fixture
def replacement_and_attempt_ids(db, monitor_id, asset_id, sync_run_id):
    observation_run_id = db.execute(
        """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
           VALUES (?, ?, ?)""",
        (monitor_id, "completed", "2026-07-14T11:00:00Z"),
    ).lastrowid
    db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T06:00:00Z", "downloading", 4096, 25),
    )
    db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asset_id, observation_run_id, "2026-07-14T11:00:00Z", "queued", 4096, 25),
    )
    replacement_id = db.execute(
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance, downloaded_once,
            replaces_asset_id, usable_coverage_json, inventory_verified_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            "REPLACEMENT",
            "Replacement",
            "external",
            "pre_existing",
            1,
            asset_id,
            "[1,2]",
            "2026-07-14T11:01:00Z",
            "downloaded",
        ),
    ).lastrowid
    attempt_id = db.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash, decision, terminal_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            sync_run_id,
            replacement_id,
            "Mushoku",
            "REPLACEMENT",
            "ACCEPTED",
            "2026-07-14T11:02:00Z",
        ),
    ).lastrowid
    return replacement_id, attempt_id


def _insert_linked_removal(
    db: sqlite3.Connection,
    *,
    operation_key: str,
    asset_id: int,
    replacement_id: int,
    attempt_id: int,
    sync_run_id: int,
    outcome: str,
    detail: str = "",
) -> None:
    completed_at = None if outcome == "pending" else "2026-07-14T12:01:00Z"
    oldest_id, newest_id = _latest_support_observation_ids(db, asset_id)
    db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
            sync_run_id, support_observation_oldest_id, support_observation_newest_id,
            requested_at, completed_at, outcome, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            operation_key,
            asset_id,
            replacement_id,
            attempt_id,
            sync_run_id,
            oldest_id,
            newest_id,
            "2026-07-14T12:00:00Z",
            completed_at,
            outcome,
            detail,
        ),
    )


@pytest.fixture
def removal_evidence_factory(db, user_id, monitor_id):
    def create(
        invalid: str | None = None,
        *,
        attempt_terminal_at: str | None = None,
    ) -> dict[str, object]:
        three_observations = invalid in {
            "latest_pair_changed_bytes",
            "latest_pair_invalid_state",
            "older_pair_changed",
        }
        observation_times = (
            ("2026-07-14T01:00:00Z", "2026-07-14T06:00:00Z", "2026-07-14T11:00:00Z")
            if three_observations
            else ("2026-07-14T06:00:00Z", "2026-07-14T11:00:00Z")
        )
        observation_runs = [
            db.execute(
                """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
                   VALUES (?, ?, ?)""",
                (monitor_id, "completed", completed_at),
            ).lastrowid
            for completed_at in observation_times
        ]

        decision_monitor_id = monitor_id
        if invalid == "monitor_match":
            decision_monitor_id = db.execute(
                """INSERT INTO media_monitors (user_id, anilist_id, title)
                   VALUES (?, ?, ?)""",
                (user_id, 999001, "Other monitor"),
            ).lastrowid
        attempt_run_id = None
        if invalid in {"cross_run_attempt", "cross_run_attempt_time"}:
            attempt_run_id = db.execute(
                """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
                   VALUES (?, ?, ?)""",
                (monitor_id, "completed", "2026-07-14T11:03:00Z"),
            ).lastrowid
        decision_run_id = db.execute(
            """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
               VALUES (?, ?, ?)""",
            (
                decision_monitor_id,
                "completed" if invalid != "decision_run_completion" else "running",
                None if invalid == "decision_run_completion" else "2026-07-14T11:05:00Z",
            ),
        ).lastrowid

        source_id = db.execute(
            """INSERT INTO media_assets
               (monitor_id, torrent_hash, source_title, ownership, provenance,
                created_by_run_id, downloaded_once, shared_reference_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor_id,
                "LIFECYCLE-SOURCE",
                "Lifecycle source",
                "external" if invalid == "ownership" else "monitor_owned",
                "adopted" if invalid == "provenance" else "omakase_created",
                observation_runs[0],
                1 if invalid == "downloaded_once" else 0,
                1 if invalid == "shared_reference" else 0,
            ),
        ).lastrowid

        observation_count = 1 if invalid == "one_observation" else len(observation_times)
        statuses = ["downloading", "queued", "queued"]
        bytes_done = [4096, 4096, 4096]
        progress = [25, 25, 25]
        if invalid == "observation_state":
            statuses[1] = "error"
        elif invalid == "changed_bytes":
            bytes_done[1] = 4097
        elif invalid == "changed_progress":
            progress[1] = 26
        elif invalid == "latest_pair_changed_bytes":
            bytes_done[2] = 4097
        elif invalid == "latest_pair_invalid_state":
            statuses[2] = "error"
        elif invalid == "older_pair_changed":
            bytes_done[0] = 4095
        second_observed_at = {
            "observation_interval_3h59m59s": "2026-07-14T09:59:59Z",
            "observation_interval_exact": "2026-07-14T10:00:00Z",
        }.get(invalid, "2026-07-14T11:00:00Z")
        observed_at = list(observation_times)
        if not three_observations:
            observed_at[1] = second_observed_at
        observation_ids = []
        for index in range(observation_count):
            observation_ids.append(
                db.execute(
                    """INSERT INTO media_asset_observations
                   (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        source_id,
                        observation_runs[index],
                        observed_at[index],
                        statuses[index],
                        bytes_done[index],
                        progress[index],
                    ),
                ).lastrowid
            )
        if invalid == "incomplete_observation_run":
            db.execute(
                "UPDATE media_sync_runs SET completed_at = NULL WHERE id = ?",
                (observation_runs[1],),
            )

        replacement_status = "candidate" if invalid == "replacement_status" else "downloaded"
        replacement_downloaded = 0 if invalid == "replacement_downloaded_once" else 1
        if invalid == "replacement_downloaded_once":
            db.execute("PRAGMA ignore_check_constraints = ON")
        replacement_id = db.execute(
            """INSERT INTO media_assets
               (monitor_id, torrent_hash, source_title, ownership, provenance, downloaded_once,
                replaces_asset_id, usable_coverage_json, inventory_verified_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor_id,
                "LIFECYCLE-REPLACEMENT",
                "Lifecycle replacement",
                "external",
                "pre_existing",
                replacement_downloaded,
                None if invalid == "replacement_link" else source_id,
                (
                    "not-json"
                    if invalid == "replacement_coverage_invalid"
                    else "[]"
                    if invalid == "replacement_coverage_empty"
                    else "[1,2]"
                ),
                None if invalid == "replacement_inventory" else "2026-07-14T11:01:00Z",
                replacement_status,
            ),
        ).lastrowid
        if invalid == "replacement_downloaded_once":
            db.execute("PRAGMA ignore_check_constraints = OFF")

        attempt_id = db.execute(
            """INSERT INTO media_candidate_attempts
               (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash, decision,
                terminal_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                monitor_id,
                (
                    observation_runs[0]
                    if decision_monitor_id != monitor_id
                    else attempt_run_id or decision_run_id
                ),
                source_id if invalid == "attempt_asset" else replacement_id,
                "Lifecycle",
                "LIFECYCLE-REPLACEMENT",
                "REJECTED" if invalid == "attempt_decision" else "ACCEPTED",
                (
                    None
                    if invalid == "attempt_terminal"
                    else attempt_terminal_at or "2026-07-14T11:02:00Z"
                ),
            ),
        ).lastrowid
        return {
            "source": source_id,
            "replacement": replacement_id,
            "attempt": attempt_id,
            "decision_run": decision_run_id,
            "attempt_run": attempt_run_id or decision_run_id,
            "observation_runs": observation_runs,
            "observations": observation_ids,
        }

    return create


def _insert_removal_receipt(
    db: sqlite3.Connection,
    evidence: dict[str, object],
    *,
    operation_key: str = "lifecycle-removal",
    outcome: str = "pending",
    requested_at: str = "2026-07-14T11:06:00Z",
    completed_at: str | None = None,
) -> int:
    terminal_completed_at = (
        completed_at
        if completed_at is not None
        else None
        if outcome == "pending"
        else "2026-07-14T11:07:00Z"
    )
    oldest_observation_id = (
        evidence["observations"][-2]
        if len(evidence["observations"]) >= 2
        else evidence["observations"][-1]
    )
    return db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
            sync_run_id, support_observation_oldest_id, support_observation_newest_id,
            requested_at, completed_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            operation_key,
            evidence["source"],
            evidence["replacement"],
            evidence["attempt"],
            evidence["decision_run"],
            oldest_observation_id,
            evidence["observations"][-1],
            requested_at,
            terminal_completed_at,
            outcome,
        ),
    ).lastrowid


def _insert_later_observation(
    db: sqlite3.Connection,
    evidence: dict[str, object],
    *,
    rd_status: str = "queued",
    bytes_done: int = 4096,
    progress: int = 25,
) -> int:
    source_monitor_id = db.execute(
        "SELECT monitor_id FROM media_assets WHERE id = ?", (evidence["source"],)
    ).fetchone()[0]
    run_id = db.execute(
        """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
           VALUES (?, 'completed', '2026-07-14T12:00:00Z')""",
        (source_monitor_id,),
    ).lastrowid
    return db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
           VALUES (?, ?, '2026-07-14T12:00:00Z', ?, ?, ?)""",
        (evidence["source"], run_id, rd_status, bytes_done, progress),
    ).lastrowid


def _create_tenant_graph(db: sqlite3.Connection, label: str) -> dict[str, int]:
    ordinal = ord(label)
    user_id = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (f"tenant-{label.lower()}@example.com", "hash"),
    ).lastrowid
    planning_id = db.execute(
        """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
           VALUES (?, ?, ?, ?)""",
        (user_id, 500000 + ordinal, f"Planning {label}", "PLANNING"),
    ).lastrowid
    monitor_id = db.execute(
        """INSERT INTO media_monitors
           (user_id, anilist_planning_id, anilist_id, title)
           VALUES (?, ?, ?, ?)""",
        (user_id, planning_id, 600000 + ordinal, f"Monitor {label}"),
    ).lastrowid
    sync_run_id = db.execute(
        """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
           VALUES (?, ?, ?)""",
        (monitor_id, "completed", "2026-07-14T11:05:00Z"),
    ).lastrowid
    observation_run_id = db.execute(
        """INSERT INTO media_sync_runs (monitor_id, outcome, completed_at)
           VALUES (?, ?, ?)""",
        (monitor_id, "completed", "2026-07-14T11:00:00Z"),
    ).lastrowid
    asset_id = db.execute(
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance, created_by_run_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            f"SOURCE-{label}",
            f"Source {label}",
            "monitor_owned",
            "omakase_created",
            sync_run_id,
        ),
    ).lastrowid
    replacement_id = db.execute(
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance, downloaded_once,
            replaces_asset_id, usable_coverage_json, inventory_verified_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            f"REPLACEMENT-{label}",
            f"Replacement {label}",
            "external",
            "pre_existing",
            1,
            asset_id,
            "[1,2]",
            "2026-07-14T11:01:00Z",
            "downloaded",
        ),
    ).lastrowid
    attempt_id = db.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash, decision, terminal_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            sync_run_id,
            replacement_id,
            f"Attempt {label}",
            f"REPLACEMENT-{label}",
            "ACCEPTED",
            "2026-07-14T11:02:00Z",
        ),
    ).lastrowid
    alias_receipt_id = db.execute(
        """INSERT INTO media_alias_search_receipts
           (monitor_id, sync_run_id, query_version, search_alias, started_at)
           VALUES (?, ?, ?, ?, ?)""",
        (monitor_id, sync_run_id, "v1", f"Receipt {label}", "2026-07-14T12:00:00Z"),
    ).lastrowid
    observation_id = db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T06:00:00Z", "downloading", 4096, 25),
    ).lastrowid
    newest_observation_id = db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (asset_id, observation_run_id, "2026-07-14T11:00:00Z", "queued", 4096, 25),
    ).lastrowid
    _insert_linked_removal(
        db,
        operation_key=f"tenant-removal-{label}",
        asset_id=asset_id,
        replacement_id=replacement_id,
        attempt_id=attempt_id,
        sync_run_id=sync_run_id,
        outcome="pending",
    )
    removal_id = db.execute(
        "SELECT id FROM media_asset_removals WHERE operation_key = ?",
        (f"tenant-removal-{label}",),
    ).fetchone()[0]
    return {
        "user": user_id,
        "planning": planning_id,
        "monitor": monitor_id,
        "run": sync_run_id,
        "asset": asset_id,
        "replacement": replacement_id,
        "attempt": attempt_id,
        "alias_receipt": alias_receipt_id,
        "observation": observation_id,
        "oldest_observation": observation_id,
        "newest_observation": newest_observation_id,
        "removal": removal_id,
    }


@pytest.fixture
def two_tenants(db):
    return {label: _create_tenant_graph(db, label) for label in ("A", "B")}


def _assert_tenant_violation(
    db: sqlite3.Connection,
    message: str,
    sql: str,
    values: tuple[object, ...],
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match=f"^{message}$"):
        db.execute(sql, values)


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_monitor_planning_rejects_cross_user_relationship(db, two_tenants, operation):
    tenant_a = two_tenants["A"]
    tenant_b = two_tenants["B"]
    if operation == "insert":
        sql = """INSERT INTO media_monitors
                 (user_id, anilist_planning_id, anilist_id, title) VALUES (?, ?, ?, ?)"""
        values = (tenant_a["user"], tenant_b["planning"], 700001, "Cross-user monitor")
    else:
        sql = "UPDATE media_monitors SET anilist_planning_id = ? WHERE id = ?"
        values = (tenant_b["planning"], tenant_a["monitor"])
    _assert_tenant_violation(db, "media monitor planning owner mismatch", sql, values)


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize(
    ("column", "other_key"),
    [
        ("created_by_run_id", "run"),
        ("replaces_asset_id", "replacement"),
        ("replaced_by_asset_id", "replacement"),
    ],
)
def test_asset_rejects_cross_monitor_relationship(db, two_tenants, operation, column, other_key):
    tenant_a = two_tenants["A"]
    cross_id = two_tenants["B"][other_key]
    if operation == "insert":
        sql = f"""INSERT INTO media_assets
                  (monitor_id, torrent_hash, source_title, {column}) VALUES (?, ?, ?, ?)"""
        values = (
            tenant_a["monitor"],
            f"CROSS-ASSET-{column}",
            "Cross-monitor asset",
            cross_id,
        )
    else:
        sql = f"UPDATE media_assets SET {column} = ? WHERE id = ?"
        values = (cross_id, tenant_a["asset"])
    _assert_tenant_violation(db, "media asset monitor mismatch", sql, values)


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize(("column", "other_key"), [("sync_run_id", "run"), ("asset_id", "asset")])
def test_candidate_attempt_rejects_cross_monitor_relationship(
    db, two_tenants, operation, column, other_key
):
    tenant_a = two_tenants["A"]
    tenant_b = two_tenants["B"]
    if operation == "insert":
        run_id = tenant_b["run"] if column == "sync_run_id" else tenant_a["run"]
        asset_id = tenant_b["asset"] if column == "asset_id" else tenant_a["asset"]
        sql = """INSERT INTO media_candidate_attempts
                 (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash, decision)
                 VALUES (?, ?, ?, ?, ?, ?)"""
        values = (
            tenant_a["monitor"],
            run_id,
            asset_id,
            f"Cross attempt {column}",
            f"CROSS-ATTEMPT-{column}",
            "rejected",
        )
    else:
        sql = f"UPDATE media_candidate_attempts SET {column} = ? WHERE id = ?"
        values = (tenant_b[other_key], tenant_a["attempt"])
    _assert_tenant_violation(db, "media candidate attempt monitor mismatch", sql, values)


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_alias_receipt_rejects_cross_monitor_run(db, two_tenants, operation):
    tenant_a = two_tenants["A"]
    tenant_b = two_tenants["B"]
    if operation == "insert":
        sql = """INSERT INTO media_alias_search_receipts
                 (monitor_id, sync_run_id, query_version, search_alias, started_at)
                 VALUES (?, ?, ?, ?, ?)"""
        values = (
            tenant_a["monitor"],
            tenant_b["run"],
            "v1",
            "Cross alias receipt",
            "now",
        )
    else:
        sql = "UPDATE media_alias_search_receipts SET sync_run_id = ? WHERE id = ?"
        values = (tenant_b["run"], tenant_a["alias_receipt"])
    _assert_tenant_violation(db, "media alias receipt monitor mismatch", sql, values)


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("column", ["asset_id", "sync_run_id"])
def test_observation_rejects_cross_monitor_asset_and_run(db, two_tenants, operation, column):
    tenant_a = two_tenants["A"]
    tenant_b = two_tenants["B"]
    if operation == "insert":
        asset_id = tenant_b["asset"] if column == "asset_id" else tenant_a["asset"]
        run_id = tenant_b["run"] if column == "sync_run_id" else tenant_a["run"]
        sql = """INSERT INTO media_asset_observations
                 (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)"""
        values = (asset_id, run_id, f"cross-{column}", "queued")
    else:
        sql = f"UPDATE media_asset_observations SET {column} = ? WHERE id = ?"
        values = (
            tenant_b["asset" if column == "asset_id" else "run"],
            tenant_a["observation"],
        )
    _assert_tenant_violation(db, "media asset observation monitor mismatch", sql, values)


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize(
    ("column", "other_key"),
    [
        ("asset_id", "asset"),
        ("replacement_asset_id", "replacement"),
        ("candidate_attempt_id", "attempt"),
        ("sync_run_id", "run"),
        ("support_observation_oldest_id", "oldest_observation"),
        ("support_observation_newest_id", "newest_observation"),
    ],
)
def test_removal_rejects_cross_monitor_evidence(db, two_tenants, operation, column, other_key):
    tenant_a = two_tenants["A"]
    tenant_b = two_tenants["B"]
    if operation == "insert":
        values_by_column = {
            "asset_id": tenant_a["asset"],
            "replacement_asset_id": tenant_a["replacement"],
            "candidate_attempt_id": tenant_a["attempt"],
            "sync_run_id": tenant_a["run"],
            "support_observation_oldest_id": tenant_a["oldest_observation"],
            "support_observation_newest_id": tenant_a["newest_observation"],
        }
        values_by_column[column] = tenant_b[other_key]
        sql = """INSERT INTO media_asset_removals
                 (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
                  sync_run_id, support_observation_oldest_id,
                  support_observation_newest_id, requested_at, outcome)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        values = (
            f"cross-removal-{column}",
            values_by_column["asset_id"],
            values_by_column["replacement_asset_id"],
            values_by_column["candidate_attempt_id"],
            values_by_column["sync_run_id"],
            values_by_column["support_observation_oldest_id"],
            values_by_column["support_observation_newest_id"],
            "now",
            "pending",
        )
    else:
        sql = f"UPDATE media_asset_removals SET {column} = ? WHERE id = ?"
        values = (tenant_b[other_key], tenant_a["removal"])
    expected = (
        "media removal preflight invalid"
        if operation == "insert"
        else "media asset removal monitor mismatch"
    )
    _assert_tenant_violation(db, expected, sql, values)


@pytest.mark.parametrize(
    ("sql", "key", "message"),
    [
        (
            "UPDATE anilist_plannings SET user_id = ? WHERE id = ?",
            "planning",
            "anilist planning owner update would violate media monitor",
        ),
        (
            "UPDATE media_sync_runs SET monitor_id = ? WHERE id = ?",
            "run",
            "media run monitor update would violate ownership",
        ),
        (
            "UPDATE media_assets SET monitor_id = ? WHERE id = ?",
            "asset",
            "media asset monitor update would violate ownership",
        ),
    ],
)
def test_parent_updates_cannot_break_tenant_relationships(db, two_tenants, sql, key, message):
    tenant_a = two_tenants["A"]
    tenant_b = two_tenants["B"]
    new_parent_id = tenant_b["user"] if key == "planning" else tenant_b["monitor"]
    _assert_tenant_violation(db, message, sql, (new_parent_id, tenant_a[key]))


@pytest.mark.parametrize("order", [("A", "B"), ("B", "A")])
def test_deleting_either_user_cannot_be_blocked_by_other_tenant(db, two_tenants, order):
    db.commit()
    first, second = order
    db.execute("DELETE FROM users WHERE id = ?", (two_tenants[first]["user"],))
    db.commit()

    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_monitors WHERE id = ?", (two_tenants[first]["monitor"],)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_monitors WHERE id = ?", (two_tenants[second]["monitor"],)
        ).fetchone()[0]
        == 1
    )

    db.execute("DELETE FROM users WHERE id = ?", (two_tenants[second]["user"],))
    db.commit()
    for media_table in (
        "media_monitors",
        "media_sync_runs",
        "media_assets",
        "media_candidate_attempts",
        "media_alias_search_receipts",
        "media_asset_observations",
        "media_asset_removals",
    ):
        assert db.execute(f"SELECT COUNT(*) FROM {media_table}").fetchone()[0] == 0


def test_valid_pending_removal_derives_removable(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    _insert_removal_receipt(db, evidence)

    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("column", "value_key"),
    [
        ("decision", "decision"),
        ("reason", "reason"),
        ("torrent_hash", "torrent_hash"),
        ("search_alias", "search_alias"),
        ("sync_run_id", "sync_run"),
        ("asset_id", "asset"),
        ("terminal_at", "terminal_at"),
    ],
)
def test_candidate_attempt_receipts_reject_direct_updates(
    db, removal_evidence_factory, column, value_key
):
    evidence = removal_evidence_factory()
    before = tuple(
        db.execute(
            "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
        ).fetchone()
    )
    values = {
        "decision": "REJECTED",
        "reason": "mutated reason",
        "torrent_hash": "MUTATED-HASH",
        "search_alias": "Mutated alias",
        "sync_run": evidence["observation_runs"][0],
        "asset": evidence["source"],
        "terminal_at": "2026-07-14T11:03:00Z",
    }

    with pytest.raises(sqlite3.IntegrityError, match="^media candidate attempt immutable$"):
        db.execute(
            f"UPDATE media_candidate_attempts SET {column} = ? WHERE id = ?",
            (values[value_key], evidence["attempt"]),
        )

    after = tuple(
        db.execute(
            "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
        ).fetchone()
    )
    assert after == before


def test_candidate_attempt_receipt_rejects_direct_delete(db, removal_evidence_factory):
    evidence = removal_evidence_factory()

    with pytest.raises(sqlite3.IntegrityError, match="^media candidate attempt immutable$"):
        db.execute("DELETE FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],))

    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_candidate_attempts WHERE id = ?",
            (evidence["attempt"],),
        ).fetchone()[0]
        == 1
    )


def test_candidate_attempt_run_rejects_direct_delete(db, removal_evidence_factory):
    evidence = removal_evidence_factory("cross_run_attempt")
    db.commit()

    with pytest.raises(
        sqlite3.IntegrityError, match="^media candidate attempt run delete blocked$"
    ):
        db.execute("DELETE FROM media_sync_runs WHERE id = ?", (evidence["attempt_run"],))

    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_candidate_attempts WHERE id = ?",
            (evidence["attempt"],),
        ).fetchone()[0]
        == 1
    )


def test_candidate_attempt_composite_replace_cannot_rewrite_history(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    before = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    db.execute("PRAGMA recursive_triggers = OFF")

    with pytest.raises(sqlite3.IntegrityError, match="^media candidate attempt collision$"):
        db.execute(
            """INSERT OR REPLACE INTO media_candidate_attempts
               (id, monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
                decision, reason, terminal_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                evidence["attempt"] + 1000,
                before["monitor_id"],
                before["sync_run_id"],
                before["asset_id"],
                before["search_alias"],
                before["torrent_hash"],
                before["decision"],
                "replacement reason",
                before["terminal_at"],
            ),
        )

    after = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert db.execute("SELECT COUNT(*) FROM media_candidate_attempts").fetchone()[0] == 1


def test_candidate_attempt_explicit_pk_replace_cannot_rewrite_history(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    before = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    assert before["id"] > 0
    db.execute("PRAGMA recursive_triggers = OFF")

    with pytest.raises(sqlite3.IntegrityError, match="^media candidate attempt collision$"):
        db.execute(
            """INSERT OR REPLACE INTO media_candidate_attempts
               (id, monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
                decision, reason, terminal_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                before["id"],
                before["monitor_id"],
                before["sync_run_id"],
                before["asset_id"],
                "Rewritten alias",
                "REWRITTEN-HASH",
                "REJECTED",
                "rewritten reason",
                before["terminal_at"],
            ),
        )

    after = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert db.execute("SELECT COUNT(*) FROM media_candidate_attempts").fetchone()[0] == 1


@pytest.mark.parametrize("insert_clause", ["INSERT", "INSERT OR REPLACE"])
@pytest.mark.parametrize("explicit_id", [0, -1, -2])
def test_candidate_attempt_rejects_nonpositive_explicit_ids(
    db, removal_evidence_factory, insert_clause, explicit_id
):
    evidence = removal_evidence_factory()
    original = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    before_count = db.execute("SELECT COUNT(*) FROM media_candidate_attempts").fetchone()[0]
    db.execute("PRAGMA recursive_triggers = OFF")

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            f"""{insert_clause} INTO media_candidate_attempts
                (id, monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
                 decision, reason, terminal_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                explicit_id,
                original["monitor_id"],
                original["sync_run_id"],
                original["asset_id"],
                f"Explicit ID {explicit_id} via {insert_clause}",
                f"EXPLICIT-ID-{explicit_id}-{insert_clause}",
                "ACCEPTED",
                "nonpositive explicit id",
                original["terminal_at"],
            ),
        )

    assert db.execute("SELECT COUNT(*) FROM media_candidate_attempts").fetchone()[0] == before_count
    after = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    assert tuple(after) == tuple(original)


def test_candidate_attempt_upsert_cannot_rewrite_history(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    before = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    db.execute("PRAGMA recursive_triggers = OFF")

    with pytest.raises(sqlite3.IntegrityError, match="^media candidate attempt collision$"):
        db.execute(
            """INSERT INTO media_candidate_attempts
               (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
                decision, reason, terminal_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(monitor_id, sync_run_id, search_alias, torrent_hash, decision)
               DO UPDATE SET reason = excluded.reason""",
            (
                before["monitor_id"],
                before["sync_run_id"],
                before["asset_id"],
                before["search_alias"],
                before["torrent_hash"],
                before["decision"],
                "upserted reason",
                before["terminal_at"],
            ),
        )

    after = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_candidate_attempt_fresh_insert_still_passes_with_recursive_triggers_off(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory()
    before = db.execute(
        "SELECT * FROM media_candidate_attempts WHERE id = ?", (evidence["attempt"],)
    ).fetchone()
    db.execute("PRAGMA recursive_triggers = OFF")

    retry_id = db.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
            decision, reason, terminal_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            before["monitor_id"],
            before["sync_run_id"],
            before["asset_id"],
            "Retry alias",
            "RETRY-HASH",
            "ACCEPTED",
            "new retry receipt",
            before["terminal_at"],
        ),
    ).lastrowid

    assert evidence["attempt"] > 0
    assert retry_id > 0
    assert retry_id != evidence["attempt"]
    assert db.execute("SELECT COUNT(*) FROM media_candidate_attempts").fetchone()[0] == 2


def test_removal_snapshot_binds_exact_support_and_proof_values(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    db.execute(
        "UPDATE media_assets SET inventory_fingerprint = 'fingerprint-v1' WHERE id = ?",
        (evidence["replacement"],),
    )
    removal_id = _insert_removal_receipt(db, evidence)
    row = db.execute(
        """SELECT support_observation_oldest_id, support_observation_newest_id,
                  proof_json, json_valid(proof_json) AS proof_valid
           FROM media_asset_removals WHERE id = ?""",
        (removal_id,),
    ).fetchone()

    assert row["support_observation_oldest_id"] == evidence["observations"][-2]
    assert row["support_observation_newest_id"] == evidence["observations"][-1]
    assert row["proof_json"] != "{}"
    assert row["proof_valid"] == 1
    assert len(row["proof_json"]) <= 65_536
    proof = db.execute("SELECT json(?)", (row["proof_json"],)).fetchone()[0]
    assert f'"id":{evidence["source"]}' in proof
    assert f'"id":{evidence["replacement"]}' in proof
    assert '"usable_coverage":[1,2]' in proof
    assert '"inventory_fingerprint":"fingerprint-v1"' in proof
    assert f'"oldest_id":{evidence["observations"][-2]}' in proof
    assert f'"newest_id":{evidence["observations"][-1]}' in proof


def test_removal_accepts_attempt_from_prior_completed_run_and_snapshots_both_runs(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory("cross_run_attempt")
    assert evidence["attempt_run"] != evidence["decision_run"]

    removal_id = _insert_removal_receipt(db, evidence)
    proof = json.loads(
        db.execute(
            "SELECT proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
        ).fetchone()[0]
    )

    assert proof["attempt"]["sync_run_id"] == evidence["attempt_run"]
    assert proof["attempt"]["run_completed_at"] == "2026-07-14T11:03:00Z"
    assert proof["decision_run"]["id"] == evidence["decision_run"]
    assert proof["decision_run"]["completed_at"] == "2026-07-14T11:05:00Z"


def test_removal_rejects_decision_that_predates_supporting_evidence(db, removal_evidence_factory):
    evidence = removal_evidence_factory("cross_run_attempt")
    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        ("2026-07-14T10:59:00Z", evidence["decision_run"]),
    )

    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        _insert_removal_receipt(db, evidence)

    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


def test_removal_accepts_decision_and_request_at_latest_evidence_boundary(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory("cross_run_attempt")
    db.execute(
        "UPDATE media_assets SET inventory_verified_at = ? WHERE id = ?",
        ("2026-07-14T11:05:00Z", evidence["replacement"]),
    )

    removal_id = _insert_removal_receipt(db, evidence, requested_at="2026-07-14T11:05:00Z")
    proof = json.loads(
        db.execute(
            "SELECT proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
        ).fetchone()[0]
    )

    assert proof["replacement"]["inventory_verified_at"] == "2026-07-14T11:05:00Z"
    assert proof["decision_run"]["completed_at"] == "2026-07-14T11:05:00Z"
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


def test_caller_cannot_supply_removal_proof(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    with pytest.raises(sqlite3.IntegrityError, match="^media removal proof supplied$"):
        db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
                sync_run_id, support_observation_oldest_id,
                support_observation_newest_id, requested_at, outcome, proof_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "forged-proof",
                evidence["source"],
                evidence["replacement"],
                evidence["attempt"],
                evidence["decision_run"],
                evidence["observations"][-2],
                evidence["observations"][-1],
                "2026-07-14T11:06:00Z",
                "pending",
                '{"forged":true}',
            ),
        )


def test_removal_proof_column_enforces_valid_bounded_json(db):
    create_sql = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media_asset_removals'"
    ).fetchone()[0]
    declaration = create_sql.split("proof_json", 1)[1].split(",", 1)[0]

    assert "json_valid(proof_json)" in declaration
    assert "length(proof_json) <= 65536" in declaration


def test_nonrefused_removal_preflight_has_no_relationship_enabling_gate(db):
    trigger_sql = db.execute(
        """SELECT sql FROM sqlite_master
           WHERE type = 'trigger' AND name = 'trg_media_asset_removals_preflight_insert'"""
    ).fetchone()[0]
    before_full_validation = trigger_sql.split("WHEN", 1)[1].split("AND NOT EXISTS", 1)[0]

    assert "EXISTS" not in before_full_validation


def test_refused_removal_has_no_support_snapshot(db, asset_id, sync_run_id):
    removal_id = db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, sync_run_id, requested_at, completed_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "refused-without-support",
            asset_id,
            sync_run_id,
            "2026-07-14T11:05:00Z",
            "2026-07-14T11:06:00Z",
            "refused",
        ),
    ).lastrowid

    row = db.execute(
        """SELECT support_observation_oldest_id, support_observation_newest_id, proof_json
           FROM media_asset_removals WHERE id = ?""",
        (removal_id,),
    ).fetchone()
    assert tuple(row) == (None, None, "{}")


def test_refused_removal_rejects_support_observations(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, sync_run_id, support_observation_oldest_id,
                support_observation_newest_id, requested_at, completed_at, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "refused-with-support",
                evidence["source"],
                evidence["decision_run"],
                evidence["observations"][-2],
                evidence["observations"][-1],
                "2026-07-14T11:06:00Z",
                "2026-07-14T11:07:00Z",
                "refused",
            ),
        )


@pytest.mark.parametrize("shape", ["missing_oldest", "missing_newest", "same_support"])
def test_non_refused_removal_requires_distinct_support_observations(
    db, removal_evidence_factory, shape
):
    evidence = removal_evidence_factory()
    oldest_id = None if shape == "missing_oldest" else evidence["observations"][-2]
    newest_id = None if shape == "missing_newest" else evidence["observations"][-1]
    if shape == "same_support":
        oldest_id = newest_id
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
                sync_run_id, support_observation_oldest_id,
                support_observation_newest_id, requested_at, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"invalid-support-{shape}",
                evidence["source"],
                evidence["replacement"],
                evidence["attempt"],
                evidence["decision_run"],
                oldest_id,
                newest_id,
                "2026-07-14T11:06:00Z",
                "pending",
            ),
        )


def test_removal_rejects_support_pair_that_is_not_latest(db, removal_evidence_factory):
    evidence = removal_evidence_factory("older_pair_changed")
    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
                sync_run_id, support_observation_oldest_id,
                support_observation_newest_id, requested_at, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "stale-support-pair",
                evidence["source"],
                evidence["replacement"],
                evidence["attempt"],
                evidence["decision_run"],
                evidence["observations"][0],
                evidence["observations"][1],
                "2026-07-14T11:06:00Z",
                "pending",
            ),
        )


def test_removal_preflight_fails_closed_for_same_monitor_foreign_support(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory()
    monitor_id = db.execute(
        "SELECT monitor_id FROM media_assets WHERE id = ?", (evidence["source"],)
    ).fetchone()[0]
    foreign_source_id = db.execute(
        """INSERT INTO media_assets
           (monitor_id, torrent_hash, source_title, ownership, provenance)
           VALUES (?, ?, ?, ?, ?)""",
        (monitor_id, "FOREIGN-SUPPORT", "Foreign support", "external", "pre_existing"),
    ).lastrowid
    foreign_support_ids = [
        db.execute(
            """INSERT INTO media_asset_observations
               (asset_id, sync_run_id, observed_at, rd_status, bytes_done, progress)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                foreign_source_id,
                evidence["observation_runs"][index],
                observed_at,
                "queued",
                4096,
                25,
            ),
        ).lastrowid
        for index, observed_at in enumerate(("2026-07-14T06:00:00Z", "2026-07-14T11:00:00Z"))
    ]
    db.execute(
        """UPDATE media_assets
           SET ownership = 'external', provenance = 'adopted', shared_reference_count = 1
           WHERE id = ?""",
        (evidence["source"],),
    )

    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
                sync_run_id, support_observation_oldest_id,
                support_observation_newest_id, requested_at, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "foreign-same-monitor-support",
                evidence["source"],
                evidence["replacement"],
                evidence["attempt"],
                evidence["decision_run"],
                foreign_support_ids[0],
                foreign_support_ids[1],
                "2026-07-14T11:06:00Z",
                "pending",
            ),
        )

    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    ("column", "value_key"),
    [
        ("support_observation_oldest_id", "newest"),
        ("support_observation_newest_id", "oldest"),
        ("proof_json", "proof"),
    ],
)
def test_removal_support_and_snapshot_are_immutable(
    db, removal_evidence_factory, column, value_key
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    values = {
        "oldest": evidence["observations"][-2],
        "newest": evidence["observations"][-1],
        "proof": '{"mutated":true}',
    }
    with pytest.raises(sqlite3.IntegrityError, match="^media removal immutable$"):
        db.execute(
            f"UPDATE media_asset_removals SET {column} = ? WHERE id = ?",
            (values[value_key], removal_id),
        )


def test_exact_four_hour_observation_interval_is_accepted(db, removal_evidence_factory):
    evidence = removal_evidence_factory("observation_interval_exact")
    _insert_removal_receipt(db, evidence)

    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "invalid",
    [
        "ownership",
        "provenance",
        "downloaded_once",
        "shared_reference",
        "one_observation",
        "incomplete_observation_run",
        "observation_state",
        "changed_bytes",
        "changed_progress",
        "observation_interval_3h59m59s",
        "replacement_status",
        "replacement_downloaded_once",
        "replacement_inventory",
        "replacement_coverage_empty",
        "replacement_coverage_invalid",
        "replacement_link",
        "attempt_asset",
        "attempt_decision",
        "attempt_terminal",
        "decision_run_completion",
        "monitor_match",
    ],
)
@pytest.mark.parametrize("outcome", ["pending", "deleted", "failed"])
def test_removal_rejects_each_invalid_preflight(db, removal_evidence_factory, invalid, outcome):
    evidence = removal_evidence_factory(invalid)
    expected = (
        "media removal timestamp invalid"
        if invalid == "decision_run_completion"
        else "media removal preflight invalid"
    )
    with pytest.raises(sqlite3.IntegrityError, match=f"^{expected}$"):
        _insert_removal_receipt(db, evidence, outcome=outcome)

    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 0
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "coverage_json",
    ["[null]", "[{}]", '[""]', "[0]", "[-1]", "[1.5]", "[1,1]", "not-json", '{"1":1}'],
)
def test_removal_rejects_noncanonical_usable_coverage_ids(
    db, removal_evidence_factory, coverage_json
):
    evidence = removal_evidence_factory()
    db.execute(
        "UPDATE media_assets SET usable_coverage_json = ? WHERE id = ?",
        (coverage_json, evidence["replacement"]),
    )

    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        _insert_removal_receipt(db, evidence)


def test_removal_accepts_unique_positive_integer_coverage_ids(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    db.execute(
        "UPDATE media_assets SET usable_coverage_json = '[1,2]' WHERE id = ?",
        (evidence["replacement"],),
    )
    _insert_removal_receipt(db, evidence)


@pytest.mark.parametrize(
    ("proof_time", "value"),
    [
        ("inventory", "not-a-time"),
        ("inventory", "2026-07-14T11:01:00+00:00"),
        ("inventory", "2026-07-14T11:01:00.000Z"),
        ("inventory", "2026-07-14T24:00:00Z"),
        ("inventory", "2999-01-01T00:00:00Z"),
        ("inventory", "2026-07-14T11:06:01Z"),
        ("attempt_terminal", "not-a-time"),
        ("attempt_terminal", "2026-07-14T11:02:00+00:00"),
        ("attempt_terminal", "2026-07-14T11:02:00.000Z"),
        ("attempt_terminal", "2026-07-14T24:00:00Z"),
        ("attempt_terminal", "2999-01-01T00:00:00Z"),
        ("attempt_terminal", "2026-07-14T11:05:01Z"),
    ],
)
def test_removal_rejects_noncanonical_future_or_misordered_proof_times(
    db, removal_evidence_factory, proof_time, value
):
    evidence = removal_evidence_factory(
        attempt_terminal_at=value if proof_time == "attempt_terminal" else None
    )
    if proof_time == "inventory":
        db.execute(
            "UPDATE media_assets SET inventory_verified_at = ? WHERE id = ?",
            (value, evidence["replacement"]),
        )

    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        _insert_removal_receipt(db, evidence)


@pytest.mark.parametrize(
    "completed_at",
    [None, "not-a-time", "2026-07-14T24:00:00Z", "2999-01-01T00:00:00Z"],
)
def test_removal_rejects_invalid_attempt_run_completion(db, removal_evidence_factory, completed_at):
    evidence = removal_evidence_factory("cross_run_attempt_time")
    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        (completed_at, evidence["attempt_run"]),
    )

    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        _insert_removal_receipt(db, evidence)


@pytest.mark.parametrize("invalid", ["latest_pair_changed_bytes", "latest_pair_invalid_state"])
def test_latest_two_observations_cannot_be_masked_by_older_valid_evidence(
    db, removal_evidence_factory, invalid
):
    evidence = removal_evidence_factory(invalid)
    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        _insert_removal_receipt(db, evidence)


def test_older_changed_observation_does_not_block_latest_valid_pair(db, removal_evidence_factory):
    evidence = removal_evidence_factory("older_pair_changed")
    _insert_removal_receipt(db, evidence)

    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "supporting_run_completed_at",
    [
        "not-a-time",
        "2999-01-01T00:00:00Z",
        "2026-07-14T10:59:59Z",
        "2026-07-14T24:00:00Z",
    ],
)
def test_removal_rejects_invalid_latest_observation_run_proof(
    db, removal_evidence_factory, supporting_run_completed_at
):
    evidence = removal_evidence_factory()
    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        (supporting_run_completed_at, evidence["observation_runs"][-1]),
    )

    with pytest.raises(sqlite3.IntegrityError, match="^media removal preflight invalid$"):
        _insert_removal_receipt(db, evidence)


def test_pending_supporting_run_drift_terminalizes_receipt(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        ("2026-07-14T11:30:00Z", evidence["observation_runs"][-1]),
    )

    receipt = db.execute(
        "SELECT outcome, detail FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()
    assert tuple(receipt) == ("failed", "preflight guard changed")
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


def test_pending_attempt_run_completion_drift_terminalizes_fixed_receipt(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory("cross_run_attempt")
    removal_id = _insert_removal_receipt(db, evidence)
    proof_before = db.execute(
        "SELECT proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()[0]

    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        ("2026-07-14T11:04:00Z", evidence["attempt_run"]),
    )

    receipt = db.execute(
        "SELECT outcome, detail, proof_json FROM media_asset_removals WHERE id = ?",
        (removal_id,),
    ).fetchone()
    assert tuple(receipt) == ("failed", "preflight guard changed", proof_before)
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


def test_candidate_attempt_update_cannot_terminalize_pending_receipt(db, removal_evidence_factory):
    evidence = removal_evidence_factory("cross_run_attempt")
    removal_id = _insert_removal_receipt(db, evidence)
    before = db.execute(
        "SELECT outcome, detail, proof_json FROM media_asset_removals WHERE id = ?",
        (removal_id,),
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError, match="^media candidate attempt immutable$"):
        db.execute(
            "UPDATE media_candidate_attempts SET sync_run_id = ? WHERE id = ?",
            (evidence["decision_run"], evidence["attempt"]),
        )

    after = db.execute(
        "SELECT outcome, detail, proof_json FROM media_asset_removals WHERE id = ?",
        (removal_id,),
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert after["outcome"] == "pending"
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    ("rd_status", "bytes_done", "progress"),
    [("queued", 4096, 25), ("error", 8192, 50)],
)
def test_any_later_observation_terminalizes_pending_with_fixed_snapshot(
    db, removal_evidence_factory, rd_status, bytes_done, progress
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    before = db.execute(
        """SELECT support_observation_oldest_id, support_observation_newest_id, proof_json
           FROM media_asset_removals WHERE id = ?""",
        (removal_id,),
    ).fetchone()

    _insert_later_observation(
        db,
        evidence,
        rd_status=rd_status,
        bytes_done=bytes_done,
        progress=progress,
    )

    receipt = db.execute(
        """SELECT outcome, detail, support_observation_oldest_id,
                  support_observation_newest_id, proof_json
           FROM media_asset_removals WHERE id = ?""",
        (removal_id,),
    ).fetchone()
    assert receipt["outcome"] == "failed"
    assert receipt["detail"] == "support observation superseded"
    assert tuple(receipt)[2:] == tuple(before)
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("outcome", ["deleted", "failed"])
def test_later_observation_does_not_rebind_terminal_snapshot(db, removal_evidence_factory, outcome):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence, outcome=outcome)
    before = db.execute(
        """SELECT support_observation_oldest_id, support_observation_newest_id, proof_json
           FROM media_asset_removals WHERE id = ?""",
        (removal_id,),
    ).fetchone()

    _insert_later_observation(db, evidence)

    after = db.execute(
        """SELECT outcome, support_observation_oldest_id,
                  support_observation_newest_id, proof_json
           FROM media_asset_removals WHERE id = ?""",
        (removal_id,),
    ).fetchone()
    assert after["outcome"] == outcome
    assert tuple(after)[1:] == tuple(before)


@pytest.mark.parametrize("outcome", ["deleted", "failed"])
def test_direct_terminal_removal_insert_leaves_source_not_removable(
    db, removal_evidence_factory, outcome
):
    evidence = removal_evidence_factory()
    _insert_removal_receipt(db, evidence, outcome=outcome)

    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize(
    "guard",
    [
        "source_ownership",
        "source_provenance",
        "source_downloaded_once",
        "source_shared_reference",
        "replacement_status",
        "replacement_inventory",
        "replacement_coverage",
        "replacement_link",
        "decision_run_completion",
    ],
)
def test_allowed_guard_drift_terminalizes_pending_and_clears_removable(
    db, removal_evidence_factory, guard
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    mutations = {
        "source_ownership": (
            "UPDATE media_assets SET ownership = 'external' WHERE id = ?",
            evidence["source"],
        ),
        "source_provenance": (
            "UPDATE media_assets SET provenance = 'adopted' WHERE id = ?",
            evidence["source"],
        ),
        "source_downloaded_once": (
            "UPDATE media_assets SET downloaded_once = 1 WHERE id = ?",
            evidence["source"],
        ),
        "source_shared_reference": (
            "UPDATE media_assets SET shared_reference_count = 1 WHERE id = ?",
            evidence["source"],
        ),
        "replacement_status": (
            "UPDATE media_assets SET status = 'candidate' WHERE id = ?",
            evidence["replacement"],
        ),
        "replacement_inventory": (
            "UPDATE media_assets SET inventory_verified_at = NULL WHERE id = ?",
            evidence["replacement"],
        ),
        "replacement_coverage": (
            "UPDATE media_assets SET usable_coverage_json = '[]' WHERE id = ?",
            evidence["replacement"],
        ),
        "replacement_link": (
            "UPDATE media_assets SET replaces_asset_id = NULL WHERE id = ?",
            evidence["replacement"],
        ),
        "decision_run_completion": (
            "UPDATE media_sync_runs SET completed_at = NULL WHERE id = ?",
            evidence["decision_run"],
        ),
    }
    sql, values = mutations[guard]
    values = values if isinstance(values, tuple) else (values,)
    db.execute(sql, values)

    receipt = db.execute(
        "SELECT outcome, completed_at, detail FROM media_asset_removals WHERE id = ?",
        (removal_id,),
    ).fetchone()
    assert receipt["outcome"] == "failed"
    assert receipt["detail"] == "preflight guard changed"
    assert (
        db.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', ?) = ?",
            (receipt["completed_at"], receipt["completed_at"]),
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


def test_failed_receipt_allows_live_asset_state_changes_without_changing_snapshot(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    db.execute(
        "UPDATE media_assets SET ownership = 'external' WHERE id = ?",
        (evidence["source"],),
    )
    before = db.execute(
        "SELECT proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()[0]

    db.execute(
        """UPDATE media_assets
           SET downloaded_once = 1, status = 'downloaded', shared_reference_count = 1
           WHERE id = ?""",
        (evidence["source"],),
    )
    db.execute(
        """UPDATE media_assets
           SET status = 'candidate', inventory_verified_at = NULL,
               usable_coverage_json = '[]', replaces_asset_id = NULL
           WHERE id = ?""",
        (evidence["replacement"],),
    )

    receipt = db.execute(
        "SELECT outcome, proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()
    assert tuple(receipt) == ("failed", before)


@pytest.mark.parametrize("proof_parent", ["candidate", "run", "attempt_run"])
def test_failed_receipt_keeps_candidate_and_run_proof_blocked(
    db, removal_evidence_factory, proof_parent
):
    evidence = removal_evidence_factory(
        "cross_run_attempt" if proof_parent == "attempt_run" else None
    )
    removal_id = _insert_removal_receipt(db, evidence)
    db.execute(
        "UPDATE media_assets SET ownership = 'external' WHERE id = ?",
        (evidence["source"],),
    )
    before = db.execute(
        "SELECT proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()[0]
    mutations = {
        "candidate": (
            "UPDATE media_candidate_attempts SET decision = 'REJECTED' WHERE id = ?",
            evidence["attempt"],
        ),
        "run": (
            "UPDATE media_sync_runs SET completed_at = NULL WHERE id = ?",
            evidence["decision_run"],
        ),
        "attempt_run": (
            "UPDATE media_sync_runs SET completed_at = NULL WHERE id = ?",
            evidence["attempt_run"],
        ),
    }
    sql, parent_id = mutations[proof_parent]

    expected = (
        "media candidate attempt immutable"
        if proof_parent == "candidate"
        else "media removal proof immutable"
    )
    with pytest.raises(sqlite3.IntegrityError, match=f"^{expected}$"):
        db.execute(sql, (parent_id,))

    receipt = db.execute(
        "SELECT outcome, proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()
    assert tuple(receipt) == ("failed", before)


def test_multi_column_guard_drift_cannot_restore_stale_removable(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    db.execute(
        """UPDATE media_assets
           SET shared_reference_count = 1, removable = 1 WHERE id = ?""",
        (evidence["source"],),
    )

    assert (
        db.execute(
            "SELECT outcome FROM media_asset_removals WHERE id = ?", (removal_id,)
        ).fetchone()[0]
        == "failed"
    )
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


def test_blocked_replacement_downloaded_once_regression_keeps_pending_valid(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)

    with pytest.raises(sqlite3.IntegrityError, match="^media asset downloaded_once regression$"):
        db.execute(
            "UPDATE media_assets SET downloaded_once = 0 WHERE id = ?",
            (evidence["replacement"],),
        )

    assert (
        db.execute(
            "SELECT outcome FROM media_asset_removals WHERE id = ?", (removal_id,)
        ).fetchone()[0]
        == "pending"
    )
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


def test_blocked_decision_run_monitor_drift_keeps_pending_valid(
    db, user_id, removal_evidence_factory
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    other_monitor_id = db.execute(
        "INSERT INTO media_monitors (user_id, anilist_id, title) VALUES (?, ?, ?)",
        (user_id, 999002, "Blocked monitor drift"),
    ).lastrowid

    with pytest.raises(
        sqlite3.IntegrityError, match="^media run monitor update would violate ownership$"
    ):
        db.execute(
            "UPDATE media_sync_runs SET monitor_id = ? WHERE id = ?",
            (other_monitor_id, evidence["decision_run"]),
        )

    assert (
        db.execute(
            "SELECT outcome FROM media_asset_removals WHERE id = ?", (removal_id,)
        ).fetchone()[0]
        == "pending"
    )
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


@pytest.mark.parametrize(
    "proof_field",
    [
        "source_ownership",
        "source_provenance",
        "source_downloaded_once",
        "source_shared_reference",
        "replacement_status",
        "replacement_downloaded_once",
        "replacement_inventory",
        "replacement_coverage",
        "replacement_link",
        "attempt_asset",
        "attempt_decision",
        "attempt_terminal",
        "decision_run_completion",
        "supporting_run_completion",
    ],
)
def test_terminal_removal_proof_fields_are_immutable(db, removal_evidence_factory, proof_field):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence, outcome="deleted")
    proof_before = db.execute(
        "SELECT proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()[0]
    mutations = {
        "source_ownership": (
            "UPDATE media_assets SET ownership = 'external' WHERE id = ?",
            (evidence["source"],),
        ),
        "source_provenance": (
            "UPDATE media_assets SET provenance = 'adopted' WHERE id = ?",
            (evidence["source"],),
        ),
        "source_downloaded_once": (
            "UPDATE media_assets SET downloaded_once = 1 WHERE id = ?",
            (evidence["source"],),
        ),
        "source_shared_reference": (
            "UPDATE media_assets SET shared_reference_count = 1 WHERE id = ?",
            (evidence["source"],),
        ),
        "replacement_status": (
            "UPDATE media_assets SET status = 'candidate' WHERE id = ?",
            (evidence["replacement"],),
        ),
        "replacement_downloaded_once": (
            "UPDATE media_assets SET downloaded_once = 0 WHERE id = ?",
            (evidence["replacement"],),
        ),
        "replacement_inventory": (
            "UPDATE media_assets SET inventory_verified_at = NULL WHERE id = ?",
            (evidence["replacement"],),
        ),
        "replacement_coverage": (
            "UPDATE media_assets SET usable_coverage_json = '[1,2,3]' WHERE id = ?",
            (evidence["replacement"],),
        ),
        "replacement_link": (
            "UPDATE media_assets SET replaces_asset_id = NULL WHERE id = ?",
            (evidence["replacement"],),
        ),
        "attempt_asset": (
            "UPDATE media_candidate_attempts SET asset_id = ? WHERE id = ?",
            (evidence["source"], evidence["attempt"]),
        ),
        "attempt_decision": (
            "UPDATE media_candidate_attempts SET decision = 'REJECTED' WHERE id = ?",
            (evidence["attempt"],),
        ),
        "attempt_terminal": (
            "UPDATE media_candidate_attempts SET terminal_at = NULL WHERE id = ?",
            (evidence["attempt"],),
        ),
        "decision_run_completion": (
            "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
            ("2026-07-14T11:04:00Z", evidence["decision_run"]),
        ),
        "supporting_run_completion": (
            "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
            ("2026-07-14T11:30:00Z", evidence["observation_runs"][-1]),
        ),
    }
    sql, values = mutations[proof_field]

    expected = (
        "media candidate attempt immutable"
        if proof_field.startswith("attempt_")
        else "media removal proof immutable"
    )
    with pytest.raises(sqlite3.IntegrityError, match=f"^{expected}$"):
        db.execute(sql, values)

    receipt = db.execute(
        "SELECT outcome, proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()
    assert tuple(receipt) == ("deleted", proof_before)


@pytest.mark.parametrize(
    "requested_at",
    [
        "not-a-time",
        "2026-07-14T11:06:00+00:00",
        "2026-07-14T11:06:00.000Z",
        "2026-07-14T24:00:00Z",
        "2026-02-30T11:06:00Z",
        "2999-01-01T00:00:00Z",
        "2026-07-14T11:04:59Z",
    ],
)
def test_removal_rejects_noncanonical_future_or_misordered_request_time(
    db, removal_evidence_factory, requested_at
):
    evidence = removal_evidence_factory()
    with pytest.raises(sqlite3.IntegrityError, match="^media removal timestamp invalid$"):
        _insert_removal_receipt(db, evidence, requested_at=requested_at)


@pytest.mark.parametrize(
    "completed_at",
    [
        "not-a-time",
        "2026-07-14T11:07:00+00:00",
        "2026-07-14T11:07:00.000Z",
        "2026-07-14T24:00:00Z",
        "2026-02-30T11:07:00Z",
        "2999-01-01T00:00:00Z",
        "2026-07-14T11:05:59Z",
    ],
)
def test_removal_rejects_noncanonical_future_or_misordered_terminal_time(
    db, removal_evidence_factory, completed_at
):
    evidence = removal_evidence_factory()
    with pytest.raises(sqlite3.IntegrityError, match="^media removal timestamp invalid$"):
        _insert_removal_receipt(db, evidence, outcome="deleted", completed_at=completed_at)


@pytest.mark.parametrize(
    "decision_completed_at",
    [
        "not-a-time",
        "2026-07-14T11:05:00+00:00",
        "2026-07-14T11:05:00.000Z",
        "2026-07-14T24:00:00Z",
        "2026-02-30T11:05:00Z",
        "2999-01-01T00:00:00Z",
    ],
)
def test_removal_rejects_noncanonical_or_future_decision_completion(
    db, removal_evidence_factory, decision_completed_at
):
    evidence = removal_evidence_factory()
    db.execute(
        "UPDATE media_sync_runs SET completed_at = ? WHERE id = ?",
        (decision_completed_at, evidence["decision_run"]),
    )
    with pytest.raises(sqlite3.IntegrityError, match="^media removal timestamp invalid$"):
        _insert_removal_receipt(db, evidence)


@pytest.mark.parametrize(
    ("requested_at", "completed_at"),
    [
        ("garbage-request", "garbage-completion"),
        ("2026-07-14T11:06:00Z", "2026-07-14T11:07:00Z"),
    ],
)
def test_refused_removal_requires_completed_canonical_decision_run(
    db, removal_evidence_factory, requested_at, completed_at
):
    evidence = removal_evidence_factory("decision_run_completion")
    with pytest.raises(sqlite3.IntegrityError, match="^media removal timestamp invalid$"):
        db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, sync_run_id, requested_at, completed_at, outcome)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                f"refused-incomplete-{requested_at}",
                evidence["source"],
                evidence["decision_run"],
                requested_at,
                completed_at,
                "refused",
            ),
        )


def test_direct_source_delete_with_audit_evidence_is_rejected(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    _insert_removal_receipt(db, evidence)

    with pytest.raises(sqlite3.IntegrityError, match="^media source evidence delete blocked$"):
        db.execute("DELETE FROM media_assets WHERE id = ?", (evidence["source"],))

    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_asset_observations WHERE asset_id = ?",
            (evidence["source"],),
        ).fetchone()[0]
        == 2
    )
    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_asset_removals WHERE asset_id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 1
    )


def test_only_one_pending_removal_is_allowed_per_source(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    _insert_removal_receipt(db, evidence, operation_key="pending-one")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_removal_receipt(db, evidence, operation_key="pending-two")

    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_asset_removals WHERE asset_id = ? AND outcome = 'pending'",
            (evidence["source"],),
        ).fetchone()[0]
        == 1
    )


def test_two_connections_cannot_commit_two_pending_receipts(db, removal_evidence_factory, tmp_path):
    evidence = removal_evidence_factory()
    db.commit()
    db_path = tmp_path / "pending-race.db"
    seed = sqlite3.connect(db_path)
    db.backup(seed)
    seed.close()

    first = sqlite3.connect(db_path, timeout=0)
    second = sqlite3.connect(db_path, timeout=0)
    first.execute("PRAGMA foreign_keys = ON")
    second.execute("PRAGMA foreign_keys = ON")
    try:
        first.execute("BEGIN IMMEDIATE")
        _insert_removal_receipt(first, evidence, operation_key="race-first")

        # SQLite serializes writers: the contender first observes the write lock.
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _insert_removal_receipt(second, evidence, operation_key="race-second-locked")

        first.commit()
        # Once the lock clears, the partial unique index is the durable arbiter.
        with pytest.raises(sqlite3.IntegrityError):
            _insert_removal_receipt(second, evidence, operation_key="race-second-unique")
        second.rollback()

        assert (
            second.execute(
                """SELECT COUNT(*) FROM media_asset_removals
               WHERE asset_id = ? AND outcome = 'pending'""",
                (evidence["source"],),
            ).fetchone()[0]
            == 1
        )
    finally:
        first.close()
        second.close()


def test_refused_removal_may_be_nullable_and_does_not_mark_source_removable(
    db, removal_evidence_factory
):
    evidence = removal_evidence_factory()
    db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, sync_run_id, requested_at, completed_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "refused-nullable",
            evidence["source"],
            evidence["decision_run"],
            "2026-07-14T11:06:00Z",
            "2026-07-14T11:07:00Z",
            "refused",
        ),
    )
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )


@pytest.mark.parametrize("outcome", ["deleted", "failed"])
def test_pending_removal_can_transition_once_and_clears_removable(
    db, removal_evidence_factory, outcome
):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    db.execute(
        """UPDATE media_asset_removals
           SET outcome = ?, completed_at = ?, detail = ? WHERE id = ?""",
        (outcome, "2026-07-14T12:00:00Z", "terminal receipt", removal_id),
    )

    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source"],)
        ).fetchone()[0]
        == 0
    )
    with pytest.raises(sqlite3.IntegrityError, match="^media removal immutable$"):
        db.execute(
            "UPDATE media_asset_removals SET detail = ? WHERE id = ?",
            ("second update", removal_id),
        )


@pytest.mark.parametrize(
    "column",
    [
        "id",
        "operation_key",
        "asset_id",
        "replacement_asset_id",
        "candidate_attempt_id",
        "sync_run_id",
        "requested_at",
    ],
)
def test_pending_terminal_transition_cannot_mutate_identity(db, removal_evidence_factory, column):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    values = {
        "id": removal_id + 1000,
        "operation_key": "mutated-operation",
        "asset_id": evidence["replacement"],
        "replacement_asset_id": evidence["source"],
        "candidate_attempt_id": None,
        "sync_run_id": evidence["observation_runs"][0],
        "requested_at": "2026-07-14T00:00:00Z",
    }
    with pytest.raises(sqlite3.IntegrityError, match="^media removal immutable$"):
        db.execute(
            f"""UPDATE media_asset_removals
                SET outcome = 'deleted', completed_at = ?, {column} = ? WHERE id = ?""",
            ("2026-07-14T12:00:00Z", values[column], removal_id),
        )


def test_pending_removal_cannot_transition_to_refused(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    with pytest.raises(sqlite3.IntegrityError, match="^media removal immutable$"):
        db.execute(
            """UPDATE media_asset_removals SET outcome = 'refused', completed_at = ?
               WHERE id = ?""",
            ("2026-07-14T12:00:00Z", removal_id),
        )


def test_terminal_removal_completion_cannot_be_cleared(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    db.execute(
        """UPDATE media_asset_removals SET outcome = 'deleted', completed_at = ?
           WHERE id = ?""",
        ("2026-07-14T12:00:00Z", removal_id),
    )
    with pytest.raises(sqlite3.IntegrityError, match="^media removal immutable$"):
        db.execute(
            "UPDATE media_asset_removals SET completed_at = NULL WHERE id = ?", (removal_id,)
        )


def test_removal_delete_is_rejected(db, removal_evidence_factory):
    evidence = removal_evidence_factory()
    removal_id = _insert_removal_receipt(db, evidence)
    with pytest.raises(sqlite3.IntegrityError, match="^media removal immutable$"):
        db.execute("DELETE FROM media_asset_removals WHERE id = ?", (removal_id,))


def test_pending_removal_requires_null_completion(
    db, asset_id, sync_run_id, replacement_and_attempt_ids
):
    replacement_id, attempt_id = replacement_and_attempt_ids
    oldest_id, newest_id = _latest_support_observation_ids(db, asset_id)
    sql = """INSERT INTO media_asset_removals
             (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
              sync_run_id, support_observation_oldest_id, support_observation_newest_id,
              requested_at, completed_at, outcome)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (
            "pending-complete",
            asset_id,
            replacement_id,
            attempt_id,
            sync_run_id,
            oldest_id,
            newest_id,
            "2026-07-14T12:00:00Z",
            "2026-07-14T12:01:00Z",
            "pending",
        ),
    )


@pytest.mark.parametrize("outcome", ["deleted", "failed", "refused"])
def test_terminal_removals_require_completion(db, asset_id, sync_run_id, outcome):
    sql = """INSERT INTO media_asset_removals
             (operation_key, asset_id, sync_run_id, requested_at, completed_at, outcome)
             VALUES (?, ?, ?, ?, ?, ?)"""
    _assert_integrity_error(
        db,
        sql,
        (f"{outcome}-incomplete", asset_id, sync_run_id, "2026-07-14T12:00:00Z", None, outcome),
    )


@pytest.mark.parametrize("outcome", ["pending", "deleted", "failed"])
@pytest.mark.parametrize("missing_link", ["replacement", "attempt"])
def test_non_refused_removals_require_replacement_and_attempt_links(
    db,
    asset_id,
    sync_run_id,
    replacement_and_attempt_ids,
    outcome,
    missing_link,
):
    replacement_id, attempt_id = replacement_and_attempt_ids
    oldest_id, newest_id = _latest_support_observation_ids(db, asset_id)
    if missing_link == "replacement":
        replacement_id = None
    else:
        attempt_id = None
    completed_at = None if outcome == "pending" else "2026-07-14T12:01:00Z"
    _assert_integrity_error(
        db,
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
            sync_run_id, support_observation_oldest_id, support_observation_newest_id,
            requested_at, completed_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"{outcome}-missing-{missing_link}",
            asset_id,
            replacement_id,
            attempt_id,
            sync_run_id,
            oldest_id,
            newest_id,
            "2026-07-14T12:00:00Z",
            completed_at,
            outcome,
        ),
    )


@pytest.mark.parametrize("outcome", ["pending", "deleted", "failed"])
def test_linked_non_refused_removal_shapes_are_accepted(
    db, asset_id, sync_run_id, replacement_and_attempt_ids, outcome
):
    replacement_id, attempt_id = replacement_and_attempt_ids
    oldest_id, newest_id = _latest_support_observation_ids(db, asset_id)
    completed_at = None if outcome == "pending" else "2026-07-14T12:01:00Z"
    db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
            sync_run_id, support_observation_oldest_id, support_observation_newest_id,
            requested_at, completed_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"{outcome}-complete",
            asset_id,
            replacement_id,
            attempt_id,
            sync_run_id,
            oldest_id,
            newest_id,
            "2026-07-14T12:00:00Z",
            completed_at,
            outcome,
        ),
    )


def test_refused_removal_may_omit_replacement_and_attempt_links(db, asset_id, sync_run_id):
    db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, sync_run_id, requested_at, completed_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "refused-unlinked",
            asset_id,
            sync_run_id,
            "2026-07-14T12:00:00Z",
            "2026-07-14T12:01:00Z",
            "refused",
        ),
    )


def test_removal_operation_key_is_unique(db, asset_id, sync_run_id, replacement_and_attempt_ids):
    replacement_id, attempt_id = replacement_and_attempt_ids
    oldest_id, newest_id = _latest_support_observation_ids(db, asset_id)
    db.execute(
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
            sync_run_id, support_observation_oldest_id, support_observation_newest_id,
            requested_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "same-operation",
            asset_id,
            replacement_id,
            attempt_id,
            sync_run_id,
            oldest_id,
            newest_id,
            "2026-07-14T12:00:00Z",
            "pending",
        ),
    )
    _assert_integrity_error(
        db,
        """INSERT INTO media_asset_removals
           (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
            sync_run_id, support_observation_oldest_id, support_observation_newest_id,
            requested_at, outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "same-operation",
            asset_id,
            replacement_id,
            attempt_id,
            sync_run_id,
            oldest_id,
            newest_id,
            "2026-07-14T12:00:00Z",
            "pending",
        ),
    )


@pytest.mark.parametrize("target", ["replacement", "attempt", "run"])
def test_linked_removal_blocks_individual_evidence_deletion(
    db,
    asset_id,
    sync_run_id,
    replacement_and_attempt_ids,
    target,
):
    replacement_id, attempt_id = replacement_and_attempt_ids
    _insert_linked_removal(
        db,
        operation_key=f"preserve-{target}",
        asset_id=asset_id,
        replacement_id=replacement_id,
        attempt_id=attempt_id,
        sync_run_id=sync_run_id,
        outcome="pending",
    )
    db.commit()

    deletes = {
        "replacement": ("DELETE FROM media_assets WHERE id = ?", replacement_id),
        "attempt": ("DELETE FROM media_candidate_attempts WHERE id = ?", attempt_id),
        "run": ("DELETE FROM media_sync_runs WHERE id = ?", sync_run_id),
    }
    sql, row_id = deletes[target]
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, (row_id,))
        db.commit()
    db.rollback()

    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 1


@pytest.mark.parametrize("delete_target", ["monitor", "user"])
def test_monitor_and_user_cascades_remove_linked_media_evidence(
    db,
    user_id,
    monitor_id,
    asset_id,
    sync_run_id,
    replacement_and_attempt_ids,
    delete_target,
):
    replacement_id, attempt_id = replacement_and_attempt_ids
    for outcome in ("pending", "failed"):
        _insert_linked_removal(
            db,
            operation_key=f"cascade-{delete_target}-{outcome}",
            asset_id=asset_id,
            replacement_id=replacement_id,
            attempt_id=attempt_id,
            sync_run_id=sync_run_id,
            outcome=outcome,
        )
    db.execute(
        """INSERT INTO media_alias_search_receipts
           (monitor_id, sync_run_id, query_version, search_alias, started_at)
           VALUES (?, ?, ?, ?, ?)""",
        (monitor_id, sync_run_id, "v1", "Cascade alias", "2026-07-15T12:00:00Z"),
    )
    db.commit()

    snapshots = db.execute(
        """SELECT support_observation_oldest_id, support_observation_newest_id, proof_json
           FROM media_asset_removals"""
    ).fetchall()
    assert len(snapshots) == 2
    assert all(row[0] is not None and row[1] is not None for row in snapshots)
    assert all(
        row[2] != "{}" and db.execute("SELECT json_valid(?)", (row[2],)).fetchone()[0]
        for row in snapshots
    )

    table, row_id = (
        ("media_monitors", monitor_id) if delete_target == "monitor" else ("users", user_id)
    )
    db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
    db.commit()

    for media_table in (
        "media_monitors",
        "media_sync_runs",
        "media_assets",
        "media_candidate_attempts",
        "media_alias_search_receipts",
        "media_asset_observations",
        "media_asset_removals",
    ):
        assert db.execute(f"SELECT COUNT(*) FROM {media_table}").fetchone()[0] == 0


def _insert_safe_summary(
    db: sqlite3.Connection,
    field: str,
    value: str,
    *,
    user_id: int,
    monitor_id: int,
    asset_id: int,
    sync_run_id: int,
    replacement_id: int,
    attempt_id: int,
) -> None:
    suffix = len(value)
    if field == "monitor.last_result":
        db.execute(
            """INSERT INTO media_monitors (user_id, anilist_id, title, last_result)
               VALUES (?, ?, ?, ?)""",
            (user_id, 400000 + suffix, f"Summary {suffix}", value),
        )
    elif field == "sync_run.detail":
        db.execute(
            "INSERT INTO media_sync_runs (monitor_id, outcome, detail) VALUES (?, ?, ?)",
            (monitor_id, f"summary-{suffix}", value),
        )
    elif field == "asset.last_error":
        db.execute(
            """INSERT INTO media_assets
               (monitor_id, torrent_hash, source_title, last_error) VALUES (?, ?, ?, ?)""",
            (monitor_id, f"SUMMARY-ASSET-{suffix}", f"Summary {suffix}", value),
        )
    elif field in {"candidate.reason", "candidate.rd_outcome"}:
        column = field.split(".", 1)[1]
        db.execute(
            f"""INSERT INTO media_candidate_attempts
                (monitor_id, sync_run_id, search_alias, torrent_hash, decision, {column})
                VALUES (?, ?, ?, ?, ?, ?)""",
            (monitor_id, sync_run_id, field, f"SUMMARY-{field}-{suffix}", "rejected", value),
        )
    elif field == "alias.exhaustion_outcome":
        db.execute(
            """INSERT INTO media_alias_search_receipts
               (monitor_id, sync_run_id, query_version, search_alias, started_at,
                exhaustion_outcome) VALUES (?, ?, ?, ?, ?, ?)""",
            (monitor_id, sync_run_id, "v1", f"Summary-{suffix}", "now", value),
        )
    elif field == "removal.detail":
        _insert_linked_removal(
            db,
            operation_key=f"summary-removal-{suffix}",
            asset_id=asset_id,
            replacement_id=replacement_id,
            attempt_id=attempt_id,
            sync_run_id=sync_run_id,
            outcome="pending",
            detail=value,
        )
    else:  # pragma: no cover - parametrization is closed below
        raise AssertionError(f"Unhandled safe summary field: {field}")


@pytest.mark.parametrize(
    "field",
    [
        "monitor.last_result",
        "sync_run.detail",
        "asset.last_error",
        "candidate.reason",
        "candidate.rd_outcome",
        "alias.exhaustion_outcome",
        "removal.detail",
    ],
)
def test_safe_summaries_enforce_500_character_boundary(
    db,
    user_id,
    monitor_id,
    asset_id,
    sync_run_id,
    replacement_and_attempt_ids,
    field,
):
    replacement_id, attempt_id = replacement_and_attempt_ids
    common = {
        "user_id": user_id,
        "monitor_id": monitor_id,
        "asset_id": asset_id,
        "sync_run_id": sync_run_id,
        "replacement_id": replacement_id,
        "attempt_id": attempt_id,
    }
    _insert_safe_summary(db, field, "x" * 500, **common)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_safe_summary(db, field, "x" * 501, **common)


def test_sync_lease_only_accepts_id_one(db):
    _assert_integrity_error(
        db,
        """INSERT INTO media_sync_lease
           (id, owner, acquired_at, heartbeat_at, expires_at, entrypoint)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (2, "worker", "now", "now", "later", "scheduled"),
    )


def test_curator_receipt_latest_only_accepts_id_one(db):
    _assert_integrity_error(
        db,
        """INSERT INTO media_curator_receipt_latest
           (id, receipt_json, receipt_hash, curator_run_id, outcome, received_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (2, "{}", "hash", "curator-1", "completed", "now", "now"),
    )


def test_foreign_keys_and_monitor_cascade(db, user_id, monitor_id, asset_id, sync_run_id):
    _assert_integrity_error(
        db,
        "INSERT INTO media_sync_runs (monitor_id, outcome) VALUES (?, ?)",
        (999999, "missing-monitor"),
    )
    db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status) VALUES (?, ?, ?, ?)""",
        (asset_id, sync_run_id, "2026-07-14T05:00:00Z", "downloading"),
    )
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    for table in (
        "media_monitors",
        "media_sync_runs",
        "media_assets",
        "media_asset_observations",
    ):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_media_query_indexes_cover_contract(db):
    expected = {
        "media_monitors": {"idx_media_monitors_due"},
        "media_assets": {
            "idx_media_assets_monitor_status",
            "idx_media_assets_torrent_hash",
        },
        "media_alias_search_receipts": {"idx_media_alias_receipts_monitor_run"},
        "media_asset_observations": {"idx_media_asset_observations_asset_time"},
        "media_asset_removals": {
            "idx_media_asset_removals_outcome_requested",
            "idx_media_asset_removals_one_pending",
        },
    }
    for table, names in expected.items():
        actual = {row["name"] for row in db.execute(f"PRAGMA index_list({table})")}
        assert names <= actual

    attempt_indexes = {
        row["name"] for row in db.execute("PRAGMA index_list(media_candidate_attempts)")
    }
    assert "idx_media_candidate_attempts_monitor_run" not in attempt_indexes


def test_completion_cleanup_migration_retains_legacy_removal_scope(monkeypatch, tmp_path):
    earlier_dir = tmp_path / "earlier-migrations"
    earlier_dir.mkdir()
    migrations_dir = plus_db._MIGRATIONS_DIR
    for path in migrations_dir.glob("*.sql"):
        if not path.name.startswith("021-"):
            shutil.copy2(path, earlier_dir / path.name)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", earlier_dir)
    plus_db.run_migrations(conn)
    owner = conn.execute(
        "INSERT INTO users(email,password_hash) VALUES('legacy@example.com','hash')"
    ).lastrowid
    monitor = conn.execute(
        """INSERT INTO media_monitors(user_id,anilist_id,title,season_number)
        VALUES (?, 1, 'Legacy', 1)""",
        (owner,),
    ).lastrowid
    conn.execute(
        """INSERT INTO media_title_removals(user_id,monitor_id,anilist_id,season_number,title,requested_at)
        VALUES (?, ?, 1, 1, 'Legacy', '2026-10-11T01:00:00Z')""",
        (owner, monitor),
    )
    before = dict(conn.execute("SELECT * FROM media_title_removals").fetchone())
    conn.commit()
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", migrations_dir)
    plus_db.run_migrations(conn)
    plus_db.run_migrations(conn)
    after = dict(conn.execute("SELECT * FROM media_title_removals").fetchone())
    assert after.pop("scope") == "plex_and_provider"
    assert after == before
    assert (
        conn.execute("SELECT count(*) FROM user_completion_cleanup_preferences").fetchone()[0] == 0
    )
    assert conn.execute("SELECT keep_completed_in_plex FROM media_monitors").fetchone()[0] == 0
    conn.close()
