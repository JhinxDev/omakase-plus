"""Focused contracts for the media-orchestrator persistence helpers."""

from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Iterator

import pytest

from omakase.plus import db as plus_db
from omakase.plus.media_store import (
    covered_episodes,
    exclude_asset_episodes,
    list_due_monitors,
    record_asset_observation,
    record_asset_removal,
    record_sync_run,
    upsert_asset,
    upsert_monitor,
)


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def user_id(db: sqlite3.Connection) -> int:
    cursor = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("store@example.com", "hash"),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


@pytest.fixture
def monitor_id(db: sqlite3.Connection, user_id: int) -> int:
    return upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=178789,
        title="Mushoku Tensei Season 3",
        search_titles=("Mushoku Tensei III", "無職転生Ⅲ"),
        genres=("Adventure", "Fantasy"),
        release_status="RELEASING",
        season_number=3,
        expected_episodes=14,
        aired_episodes=3,
    )


def _completed_run(
    db: sqlite3.Connection,
    monitor_id: int,
    completed_at: str = "2026-07-14T06:00:00Z",
) -> int:
    return record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="completed",
        completed_at=completed_at,
    )


def test_asset_episode_exclusions_are_persistent_and_idempotent(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="health-asset",
        source_title="Health asset",
        usable_coverage=(1, 2, 3, 4),
    )

    first = exclude_asset_episodes(
        db,
        asset_id=asset_id,
        episodes=(2, 3),
        reason="PLAYBACK_IO_ERROR",
    )
    second = exclude_asset_episodes(
        db,
        asset_id=asset_id,
        episodes=(3,),
        reason="PLAYBACK_IO_ERROR",
    )

    assert first["usable_episodes"] == (1, 4)
    assert second["excluded_episodes"] == (2, 3)
    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["usable_coverage_json"] == "[1,4]"
    assert row["unusable_coverage_json"] == "[2,3]"
    assert row["last_error"] == "PLAYBACK_IO_ERROR"


def test_upsert_monitor_is_idempotent_and_stores_mutable_metadata(
    db: sqlite3.Connection, user_id: int
) -> None:
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=178789,
        title="Old title",
        search_titles=("無職転生Ⅲ", "Mushoku Tensei III"),
        genres=("Fantasy", "Adventure"),
        release_status="RELEASING",
        next_check_at="2026-07-15T04:00:00Z",
        last_result="initial",
    )
    same_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=178789,
        title="Mushoku Tensei: Jobless Reincarnation Season 3",
        search_titles=("Mushoku Tensei III", "無職転生Ⅲ"),
        genres=("Adventure", "Drama", "Fantasy"),
        format="TV",
        release_status="FINISHED",
        season_number=3,
        expected_episodes=14,
        aired_episodes=14,
        active=False,
        last_checked_at="2026-07-15T08:00:00Z",
        last_success_at="2026-07-15T08:00:00Z",
        next_check_at=None,
        consecutive_failures=2,
        last_result="é" * 501,
    )

    row = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    assert row is not None
    assert same_id == monitor_id
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 1
    assert row["title"] == "Mushoku Tensei: Jobless Reincarnation Season 3"
    assert row["search_titles_json"] == '["Mushoku Tensei III","無職転生Ⅲ"]'
    assert row["genres_json"] == '["Adventure","Drama","Fantasy"]'
    assert row["release_status"] == "FINISHED"
    assert row["aired_episodes"] == 14
    assert row["active"] == 0
    assert row["next_check_at"] is None
    assert row["consecutive_failures"] == 2
    assert row["last_result"] == "é" * 500


def test_list_due_monitors_filters_orders_and_limits(db: sqlite3.Connection, user_id: int) -> None:
    common = {"db": db, "user_id": user_id, "title": "Due"}
    no_schedule = upsert_monitor(**common, anilist_id=1, next_check_at=None)
    past_first = upsert_monitor(**common, anilist_id=2, next_check_at="2026-07-15T03:59:59Z")
    past_second = upsert_monitor(**common, anilist_id=3, next_check_at="2026-07-15T03:59:59Z")
    boundary = upsert_monitor(**common, anilist_id=4, next_check_at="2026-07-15T04:00:00Z")
    upsert_monitor(**common, anilist_id=5, next_check_at="2026-07-15T04:00:01Z")
    upsert_monitor(**common, anilist_id=6, active=False, next_check_at=None)

    rows = list_due_monitors(db, now="2026-07-15T04:00:00Z", limit=3)
    assert [row["id"] for row in rows] == [no_schedule, past_first, past_second]
    assert [row["id"] for row in list_due_monitors(db, now="2026-07-15T04:00:00Z")] == [
        no_schedule,
        past_first,
        past_second,
        boundary,
    ]


def test_list_due_monitors_excludes_malformed_legacy_timestamps_before_limit(
    db: sqlite3.Connection, user_id: int
) -> None:
    malformed_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=7,
        title="Malformed legacy schedule",
        next_check_at="2026-07-15T03:00:00Z",
    )
    db.execute(
        "UPDATE media_monitors SET next_check_at = ? WHERE id = ?",
        ("0000-malformed", malformed_id),
    )
    valid_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=8,
        title="Valid due schedule",
        next_check_at="2026-07-15T03:30:00Z",
    )

    rows = list_due_monitors(db, now="2026-07-15T04:00:00Z", limit=1)

    assert [row["id"] for row in rows] == [valid_id]


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_list_due_monitors_rejects_non_positive_limit(db: sqlite3.Connection, limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be a positive integer"):
        list_due_monitors(db, now="2026-07-15T04:00:00Z", limit=limit)


@pytest.mark.parametrize("limit", [True, 1.5, "1"])
def test_list_due_monitors_rejects_non_integer_limit(db: sqlite3.Connection, limit: object) -> None:
    with pytest.raises(TypeError, match="limit must be a positive integer"):
        list_due_monitors(db, now="2026-07-15T04:00:00Z", limit=limit)  # type: ignore[arg-type]


def test_store_leaves_transaction_ownership_to_caller(db: sqlite3.Connection, user_id: int) -> None:
    db.commit()
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=100,
        title="Rollback",
    )
    run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="abc123",
        source_title="Rollback S01E01",
        usable_coverage=(1,),
    )
    observation_id = record_asset_observation(
        db,
        asset_id=asset_id,
        sync_run_id=run_id,
        observed_at="2026-07-14T05:59:00Z",
        rd_status="downloading",
        bytes_done=100,
        progress=10,
    )

    assert monitor_id > 0 and run_id > 0 and asset_id > 0 and observation_id > 0
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM media_sync_runs").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM media_asset_observations").fetchone()[0] == 0


def test_sql_values_are_bound_as_data(db: sqlite3.Connection, user_id: int) -> None:
    title = "Robert'); DROP TABLE media_monitors;--"
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=101,
        title=title,
        last_result="' OR 1=1 --",
    )

    row = db.execute(
        "SELECT title, last_result FROM media_monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    assert row is not None
    assert tuple(row) == (title, "' OR 1=1 --")
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("user_id", 0),
        ("anilist_id", -1),
        ("title", ""),
        ("season_number", -1),
        ("expected_episodes", -1),
        ("aired_episodes", -1),
        ("consecutive_failures", -1),
    ],
)
def test_upsert_monitor_rejects_invalid_inputs(
    db: sqlite3.Connection, user_id: int, field: str, value: object
) -> None:
    values: dict[str, object] = {
        "user_id": user_id,
        "anilist_id": 102,
        "title": "Invalid",
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        upsert_monitor(db, **values)  # type: ignore[arg-type]


def test_upsert_asset_uppercases_hash_normalizes_coverage_and_is_idempotent(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="abcdef012345",
        rd_torrent_id="rd-safe",
        source_title="[Group] Mushoku S03 01-03",
        source_folder="Mushoku S03",
        quality="1080p",
        seeders=12,
        planned_coverage=(3, 1, 2, 2),
        usable_coverage=(3, 1, 3),
        inventory_fingerprint="sha256:old",
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
        progress=100,
        last_error="é" * 501,
    )
    same_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="ABCDEF012345",
        rd_torrent_id="rd-safe",
        source_title="[Group] Mushoku S03 01-03 v2",
        source_folder="Mushoku S03",
        quality="1080p",
        seeders=13,
        planned_coverage=(1, 2, 3),
        usable_coverage=(1, 3),
        inventory_fingerprint="sha256:new",
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
        downloaded_once=True,
        progress=100,
        last_error="fixed",
    )

    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    assert row is not None
    assert same_id == asset_id
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 1
    assert row["torrent_hash"] == "ABCDEF012345"
    assert row["source_title"] == "[Group] Mushoku S03 01-03 v2"
    assert row["seeders"] == 13
    assert row["planned_coverage_json"] == "[1,2,3]"
    assert row["usable_coverage_json"] == "[1,3]"
    assert row["downloaded_once"] == 1
    assert row["last_error"] == "fixed"


def test_downloaded_asset_sets_or_requires_downloaded_once(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="downloaded-auto",
        source_title="Downloaded",
        status="downloaded",
    )
    assert (
        db.execute("SELECT downloaded_once FROM media_assets WHERE id = ?", (asset_id,)).fetchone()[
            0
        ]
        == 1
    )

    with pytest.raises(ValueError, match="downloaded status requires downloaded_once"):
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="downloaded-invalid",
            source_title="Invalid",
            status="downloaded",
            downloaded_once=False,
        )


def test_upsert_asset_preserves_downloaded_once_monotonicity(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="monotonic",
        source_title="Downloaded once",
        status="downloaded",
    )
    same_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="monotonic",
        source_title="Currently checking",
        status="checking",
        downloaded_once=False,
    )

    row = db.execute(
        "SELECT downloaded_once, status FROM media_assets WHERE id = ?", (asset_id,)
    ).fetchone()
    assert row is not None
    assert same_id == asset_id
    assert tuple(row) == (1, "checking")


@pytest.mark.parametrize("coverage", [(0,), (-1,), (1, 1.5), (True,), ("1",)])
def test_upsert_asset_rejects_invalid_coverage(
    db: sqlite3.Connection, monitor_id: int, coverage: tuple[object, ...]
) -> None:
    with pytest.raises((TypeError, ValueError), match="coverage"):
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash=f"invalid-{coverage!r}",
            source_title="Invalid coverage",
            planned_coverage=coverage,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("monitor_id", 0),
        ("torrent_hash", ""),
        ("source_title", ""),
        ("seeders", -1),
        ("shared_reference_count", -1),
        ("progress", -0.1),
        ("progress", 100.1),
        ("stagnant_checks", -1),
        ("ownership", "borrowed"),
        ("provenance", "unknown"),
    ],
)
def test_upsert_asset_rejects_invalid_inputs(
    db: sqlite3.Connection, monitor_id: int, field: str, value: object
) -> None:
    values: dict[str, object] = {
        "monitor_id": monitor_id,
        "torrent_hash": "valid-hash",
        "source_title": "Valid title",
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        upsert_asset(db, **values)  # type: ignore[arg-type]


def test_omakase_created_asset_requires_creation_run(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    with pytest.raises(ValueError, match="created_by_run_id"):
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="missing-create-run",
            source_title="Invalid provenance",
            ownership="monitor_owned",
            provenance="omakase_created",
        )


def test_record_sync_run_returns_id_validates_counts_and_caps_detail(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    run_id = record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="completed",
        required_count=14,
        covered_count=3,
        accepted_count=1,
        detail="d" * 501,
        completed_at="2026-07-14T06:00:00Z",
    )
    row = db.execute("SELECT * FROM media_sync_runs WHERE id = ?", (run_id,)).fetchone()
    assert row is not None
    assert row["required_count"] == 14
    assert row["covered_count"] == 3
    assert row["accepted_count"] == 1
    assert row["detail"] == "d" * 500


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("monitor_id", 0),
        ("outcome", ""),
        ("required_count", -1),
        ("covered_count", 1.5),
        ("accepted_count", True),
    ],
)
def test_record_sync_run_rejects_invalid_inputs(
    db: sqlite3.Connection, monitor_id: int, field: str, value: object
) -> None:
    values: dict[str, object] = {"monitor_id": monitor_id, "outcome": "completed"}
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        record_sync_run(db, **values)  # type: ignore[arg-type]


def test_record_asset_observation_returns_id_and_uses_schema_guards(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="observed",
        source_title="Observed",
    )
    observation_id = record_asset_observation(
        db,
        asset_id=asset_id,
        sync_run_id=run_id,
        observed_at="2026-07-14T05:59:00Z",
        rd_status="downloading",
        bytes_done=123,
        progress=12.5,
    )

    assert observation_id > 0
    row = db.execute(
        "SELECT rd_status, bytes_done, progress FROM media_asset_observations WHERE id = ?",
        (observation_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("downloading", 123, 12.5)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        record_asset_observation(
            db,
            asset_id=asset_id,
            sync_run_id=run_id,
            observed_at="2026-07-14T05:59:30Z",
            rd_status="downloading",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asset_id", 0),
        ("sync_run_id", -1),
        ("observed_at", ""),
        ("rd_status", "selected"),
        ("bytes_done", -1),
        ("bytes_done", 1.5),
        ("progress", -0.1),
        ("progress", 100.1),
        ("progress", "half"),
    ],
)
def test_record_asset_observation_rejects_invalid_inputs(
    db: sqlite3.Connection,
    monitor_id: int,
    field: str,
    value: object,
) -> None:
    run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash=f"observation-{field}",
        source_title="Observed",
    )
    values: dict[str, object] = {
        "asset_id": asset_id,
        "sync_run_id": run_id,
        "observed_at": "2026-07-14T05:59:00Z",
        "rd_status": "downloading",
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        record_asset_observation(db, **values)  # type: ignore[arg-type]


def _removal_evidence(db: sqlite3.Connection, monitor_id: int) -> dict[str, int]:
    oldest_run_id = _completed_run(db, monitor_id, "2026-07-14T00:05:00Z")
    newest_run_id = _completed_run(db, monitor_id, "2026-07-14T04:05:00Z")
    attempt_run_id = _completed_run(db, monitor_id, "2026-07-14T05:05:00Z")
    decision_run_id = _completed_run(db, monitor_id, "2026-07-14T06:00:00Z")
    source_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="stalled-source",
        source_title="Stalled source",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=oldest_run_id,
        status="downloading",
    )
    replacement_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="usable-replacement",
        source_title="Usable replacement",
        status="downloaded",
        usable_coverage=(1,),
        inventory_verified_at="2026-07-14T05:00:00Z",
        replaces_asset_id=source_id,
    )
    attempt_cursor = db.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
            decision, terminal_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            attempt_run_id,
            replacement_id,
            "Mushoku Tensei",
            "USABLE-REPLACEMENT",
            "ACCEPTED",
            "2026-07-14T05:00:00Z",
        ),
    )
    assert attempt_cursor.lastrowid is not None
    oldest_id = record_asset_observation(
        db,
        asset_id=source_id,
        sync_run_id=oldest_run_id,
        observed_at="2026-07-14T00:00:00Z",
        rd_status="downloading",
        bytes_done=100,
        progress=10,
    )
    newest_id = record_asset_observation(
        db,
        asset_id=source_id,
        sync_run_id=newest_run_id,
        observed_at="2026-07-14T04:00:00Z",
        rd_status="downloading",
        bytes_done=100,
        progress=10,
    )
    return {
        "source_id": source_id,
        "replacement_id": replacement_id,
        "attempt_id": attempt_cursor.lastrowid,
        "decision_run_id": decision_run_id,
        "oldest_id": oldest_id,
        "newest_id": newest_id,
    }


def test_record_asset_removal_returns_id_and_trigger_generates_proof(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    evidence = _removal_evidence(db, monitor_id)
    removal_id = record_asset_removal(
        db,
        operation_key="remove-stalled-source",
        asset_id=evidence["source_id"],
        replacement_asset_id=evidence["replacement_id"],
        candidate_attempt_id=evidence["attempt_id"],
        sync_run_id=evidence["decision_run_id"],
        support_observation_oldest_id=evidence["oldest_id"],
        support_observation_newest_id=evidence["newest_id"],
        requested_at="2026-07-14T06:05:00Z",
        outcome="pending",
        detail="d" * 501,
    )

    row = db.execute(
        "SELECT detail, proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()
    assert row is not None
    proof = json.loads(row["proof_json"])
    assert removal_id > 0
    assert row["detail"] == "d" * 500
    assert proof["source"]["id"] == evidence["source_id"]
    assert proof["replacement"]["id"] == evidence["replacement_id"]
    assert proof["attempt"]["id"] == evidence["attempt_id"]
    assert proof["support"]["oldest_id"] == evidence["oldest_id"]
    assert proof["support"]["newest_id"] == evidence["newest_id"]
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source_id"],)
        ).fetchone()[0]
        == 1
    )


def test_record_asset_removal_does_not_accept_caller_proof() -> None:
    assert "proof_json" not in inspect.signature(record_asset_removal).parameters


def test_record_asset_removal_leaves_commit_and_rollback_to_caller(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    evidence = _removal_evidence(db, monitor_id)
    db.commit()
    removal_id = record_asset_removal(
        db,
        operation_key="rollback-removal",
        asset_id=evidence["source_id"],
        replacement_asset_id=evidence["replacement_id"],
        candidate_attempt_id=evidence["attempt_id"],
        sync_run_id=evidence["decision_run_id"],
        support_observation_oldest_id=evidence["oldest_id"],
        support_observation_newest_id=evidence["newest_id"],
        requested_at="2026-07-14T06:05:00Z",
    )

    assert removal_id > 0
    assert db.in_transaction
    db.rollback()
    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_asset_removals WHERE id = ?", (removal_id,)
        ).fetchone()[0]
        == 0
    )
    assert (
        db.execute(
            "SELECT removable FROM media_assets WHERE id = ?", (evidence["source_id"],)
        ).fetchone()[0]
        == 0
    )


def test_record_refused_removal_uses_empty_database_default_proof(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    decision_run_id = _completed_run(db, monitor_id)
    source_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="refused-source",
        source_title="Refused source",
    )
    removal_id = record_asset_removal(
        db,
        operation_key="refused-removal",
        asset_id=source_id,
        sync_run_id=decision_run_id,
        requested_at="2026-07-14T06:00:00Z",
        completed_at="2026-07-14T06:01:00Z",
        outcome="refused",
        detail="ownership guard failed",
    )

    row = db.execute(
        "SELECT outcome, proof_json FROM media_asset_removals WHERE id = ?", (removal_id,)
    ).fetchone()
    assert row is not None
    assert tuple(row) == ("refused", "{}")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("operation_key", ""),
        ("asset_id", 0),
        ("sync_run_id", 0),
        ("requested_at", ""),
        ("outcome", "invalid"),
    ],
)
def test_record_asset_removal_rejects_invalid_inputs(
    db: sqlite3.Connection,
    monitor_id: int,
    field: str,
    value: object,
) -> None:
    decision_run_id = _completed_run(db, monitor_id)
    source_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash=f"invalid-removal-{field}",
        source_title="Invalid removal",
    )
    values: dict[str, object] = {
        "operation_key": f"invalid-{field}",
        "asset_id": source_id,
        "sync_run_id": decision_run_id,
        "requested_at": "2026-07-14T06:00:00Z",
        "completed_at": "2026-07-14T06:01:00Z",
        "outcome": "refused",
    }
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        record_asset_removal(db, **values)  # type: ignore[arg-type]


def test_covered_episodes_unions_only_verified_downloaded_usable_coverage(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="usable-a",
        source_title="Usable A",
        planned_coverage=(1, 2, 3, 4),
        usable_coverage=(1, 2),
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="selected-only",
        source_title="Selected only",
        planned_coverage=(3,),
        usable_coverage=(3,),
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="selected",
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="unverified",
        source_title="Unverified",
        planned_coverage=(4,),
        usable_coverage=(4,),
        status="downloaded",
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="usable-b",
        source_title="Usable B",
        planned_coverage=(2, 5, 6),
        usable_coverage=(2, 5),
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
    )

    assert covered_episodes(db, monitor_id) == {1, 2, 5}


@pytest.mark.parametrize("monitor_id", [0, -1, True, 1.5])
def test_covered_episodes_rejects_invalid_monitor_id(
    db: sqlite3.Connection, monitor_id: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        covered_episodes(db, monitor_id)  # type: ignore[arg-type]


def test_monitor_retry_preserves_every_omitted_optional_field(
    db: sqlite3.Connection, user_id: int
) -> None:
    planning_cursor = db.execute(
        "INSERT INTO anilist_plannings (user_id, anilist_id, title) VALUES (?, ?, ?)",
        (user_id, 200, "Preserved planning"),
    )
    assert planning_cursor.lastrowid is not None
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=200,
        title="Original title",
        anilist_planning_id=planning_cursor.lastrowid,
        search_titles=("Original", "オリジナル"),
        genres=("Action", "Drama"),
        format="ONA",
        release_status="RELEASING",
        season_number=2,
        expected_episodes=12,
        aired_episodes=4,
        active=False,
        last_checked_at="2026-07-14T01:00:00Z",
        last_success_at="2026-07-14T00:59:00Z",
        next_check_at="2026-07-14T05:00:00Z",
        consecutive_failures=3,
        last_result="preserve me",
    )

    same_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=200,
        title="Refreshed title",
    )

    row = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    assert row is not None
    assert same_id == monitor_id
    assert row["title"] == "Refreshed title"
    assert row["anilist_planning_id"] == planning_cursor.lastrowid
    assert row["search_titles_json"] == '["Original","オリジナル"]'
    assert row["genres_json"] == '["Action","Drama"]'
    assert row["format"] == "ONA"
    assert row["release_status"] == "RELEASING"
    assert row["season_number"] == 2
    assert row["expected_episodes"] == 12
    assert row["aired_episodes"] == 4
    assert row["active"] == 0
    assert row["last_checked_at"] == "2026-07-14T01:00:00Z"
    assert row["last_success_at"] == "2026-07-14T00:59:00Z"
    assert row["next_check_at"] == "2026-07-14T05:00:00Z"
    assert row["consecutive_failures"] == 3
    assert row["last_result"] == "preserve me"


def test_monitor_explicit_values_can_clear_and_mutate_preserved_fields(
    db: sqlite3.Connection, user_id: int
) -> None:
    planning_cursor = db.execute(
        "INSERT INTO anilist_plannings (user_id, anilist_id, title) VALUES (?, ?, ?)",
        (user_id, 201, "Mutable planning"),
    )
    assert planning_cursor.lastrowid is not None
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=201,
        title="Mutable monitor",
        anilist_planning_id=planning_cursor.lastrowid,
        search_titles=("Alias",),
        genres=("Drama",),
        format="ONA",
        release_status="RELEASING",
        season_number=2,
        expected_episodes=12,
        active=False,
        last_checked_at="2026-07-14T01:00:00Z",
        last_success_at="2026-07-14T00:59:00Z",
        next_check_at="2026-07-14T05:00:00Z",
        consecutive_failures=3,
        last_result="old",
    )

    upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=201,
        title="Mutable monitor",
        anilist_planning_id=None,
        search_titles=(),
        genres=(),
        format="TV",
        release_status="FINISHED",
        season_number=None,
        expected_episodes=None,
        aired_episodes=12,
        active=True,
        last_checked_at=None,
        last_success_at=None,
        next_check_at=None,
        consecutive_failures=0,
        last_result="",
    )

    row = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    assert row is not None
    assert row["anilist_planning_id"] is None
    assert row["search_titles_json"] == "[]"
    assert row["genres_json"] == "[]"
    assert row["format"] == "TV"
    assert row["release_status"] == "FINISHED"
    assert row["season_number"] is None
    assert row["expected_episodes"] is None
    assert row["aired_episodes"] == 12
    assert row["active"] == 1
    assert row["last_checked_at"] is None
    assert row["last_success_at"] is None
    assert row["next_check_at"] is None
    assert row["consecutive_failures"] == 0
    assert row["last_result"] == ""


def test_monitor_omitted_fields_use_documented_defaults_on_insert(
    db: sqlite3.Connection, user_id: int
) -> None:
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=202,
        title="Defaults",
    )

    row = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    assert row is not None
    assert row["anilist_planning_id"] is None
    assert row["search_titles_json"] == "[]"
    assert row["genres_json"] == "[]"
    assert row["format"] == "TV"
    assert row["release_status"] == ""
    assert row["aired_episodes"] == 0
    assert row["active"] == 1
    assert row["consecutive_failures"] == 0
    assert row["last_result"] == ""


def test_asset_retry_preserves_every_omitted_optional_field(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    creation_run_id = _completed_run(db, monitor_id)
    replaced_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="older-asset",
        source_title="Older asset",
    )
    replacement_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="newer-asset",
        source_title="Newer asset",
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="stateful-asset",
        source_title="Original source",
        rd_torrent_id="rd-preserved",
        source_folder="Preserved folder",
        quality="1080p",
        seeders=42,
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=creation_run_id,
        shared_reference_count=2,
        replaces_asset_id=replaced_id,
        replaced_by_asset_id=replacement_id,
        planned_coverage=(1, 2, 3),
        usable_coverage=(1, 2),
        inventory_fingerprint="sha256:preserved",
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
        progress=100,
        stagnant_checks=4,
        last_error="preserved error",
    )

    same_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="stateful-asset",
        source_title="Refreshed source",
    )

    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    assert row is not None
    assert same_id == asset_id
    assert row["source_title"] == "Refreshed source"
    assert row["rd_torrent_id"] == "rd-preserved"
    assert row["source_folder"] == "Preserved folder"
    assert row["quality"] == "1080p"
    assert row["seeders"] == 42
    assert row["ownership"] == "monitor_owned"
    assert row["provenance"] == "omakase_created"
    assert row["created_by_run_id"] == creation_run_id
    assert row["shared_reference_count"] == 2
    assert row["downloaded_once"] == 1
    assert row["replaces_asset_id"] == replaced_id
    assert row["replaced_by_asset_id"] == replacement_id
    assert row["planned_coverage_json"] == "[1,2,3]"
    assert row["usable_coverage_json"] == "[1,2]"
    assert row["inventory_fingerprint"] == "sha256:preserved"
    assert row["inventory_verified_at"] == "2026-07-14T05:00:00Z"
    assert row["status"] == "downloaded"
    assert row["progress"] == 100
    assert row["stagnant_checks"] == 4
    assert row["last_error"] == "preserved error"


def test_asset_explicit_values_can_clear_and_mutate_preserved_fields(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    creation_run_id = _completed_run(db, monitor_id)
    replaced_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="explicit-old",
        source_title="Old",
    )
    replacement_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="explicit-new",
        source_title="New",
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="explicit-state",
        source_title="Explicit state",
        rd_torrent_id="rd-old",
        source_folder="Old folder",
        quality="1080p",
        seeders=20,
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=creation_run_id,
        shared_reference_count=2,
        replaces_asset_id=replaced_id,
        replaced_by_asset_id=replacement_id,
        planned_coverage=(1, 2),
        usable_coverage=(1,),
        inventory_fingerprint="old",
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
        progress=100,
        stagnant_checks=2,
        last_error="old",
    )

    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="explicit-state",
        source_title="Explicit state",
        rd_torrent_id="",
        source_folder="",
        quality="",
        seeders=0,
        ownership="external",
        provenance="adopted",
        created_by_run_id=None,
        shared_reference_count=0,
        downloaded_once=False,
        replaces_asset_id=None,
        replaced_by_asset_id=None,
        planned_coverage=(),
        usable_coverage=(),
        inventory_fingerprint="",
        inventory_verified_at=None,
        status="candidate",
        progress=0,
        stagnant_checks=0,
        last_error="",
    )

    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    assert row is not None
    assert row["rd_torrent_id"] == ""
    assert row["source_folder"] == ""
    assert row["quality"] == ""
    assert row["seeders"] == 0
    assert row["ownership"] == "external"
    assert row["provenance"] == "adopted"
    assert row["created_by_run_id"] is None
    assert row["shared_reference_count"] == 0
    assert row["downloaded_once"] == 1
    assert row["replaces_asset_id"] is None
    assert row["replaced_by_asset_id"] is None
    assert row["planned_coverage_json"] == "[]"
    assert row["usable_coverage_json"] == "[]"
    assert row["inventory_fingerprint"] == ""
    assert row["inventory_verified_at"] is None
    assert row["status"] == "candidate"
    assert row["progress"] == 0
    assert row["stagnant_checks"] == 0
    assert row["last_error"] == ""


def test_asset_partial_provenance_update_preserves_existing_creation_run(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    creation_run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="partial-provenance",
        source_title="Partial provenance",
        provenance="adopted",
        created_by_run_id=creation_run_id,
    )

    same_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="partial-provenance",
        source_title="Partial provenance",
        provenance="omakase_created",
    )

    row = db.execute(
        "SELECT provenance, created_by_run_id FROM media_assets WHERE id = ?", (asset_id,)
    ).fetchone()
    assert row is not None
    assert same_id == asset_id
    assert tuple(row) == ("omakase_created", creation_run_id)


def test_asset_omitted_fields_use_documented_defaults_on_insert(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="asset-defaults",
        source_title="Defaults",
    )

    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    assert row is not None
    assert row["rd_torrent_id"] == ""
    assert row["source_folder"] == ""
    assert row["quality"] == ""
    assert row["seeders"] == 0
    assert row["ownership"] == "external"
    assert row["provenance"] == "pre_existing"
    assert row["created_by_run_id"] is None
    assert row["shared_reference_count"] == 0
    assert row["downloaded_once"] == 0
    assert row["planned_coverage_json"] == "[]"
    assert row["usable_coverage_json"] == "[]"
    assert row["status"] == "candidate"


def test_torrent_hash_whitespace_is_normalized_before_upsert(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="  abcdef012345  ",
        source_title="Whitespace hash",
    )
    same_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="ABCDEF012345",
        source_title="Whitespace hash retry",
    )

    assert same_id == asset_id
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 1
    assert (
        db.execute("SELECT torrent_hash FROM media_assets WHERE id = ?", (asset_id,)).fetchone()[0]
        == "ABCDEF012345"
    )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-14 01:00:00",
        "2026-07-14T01:00:00+00:00",
        "2026-07-14T01:00:00.123Z",
        "2026-02-30T01:00:00Z",
        "not-a-timestamp",
        "",
    ],
)
@pytest.mark.parametrize("field", ["last_checked_at", "last_success_at", "next_check_at"])
def test_monitor_rejects_noncanonical_timestamps(
    db: sqlite3.Connection,
    user_id: int,
    timestamp: str,
    field: str,
) -> None:
    values = {
        "user_id": user_id,
        "anilist_id": 203,
        "title": "Invalid timestamp",
        field: timestamp,
    }
    with pytest.raises(ValueError, match=field):
        upsert_monitor(db, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-14 01:00:00",
        "2026-07-14T01:00:00+00:00",
        "2026-07-14T01:00:00.123Z",
        "2026-02-30T01:00:00Z",
        "not-a-timestamp",
        "",
    ],
)
def test_list_due_monitors_rejects_noncanonical_now(db: sqlite3.Connection, timestamp: str) -> None:
    with pytest.raises(ValueError, match="now"):
        list_due_monitors(db, now=timestamp)


def test_monitor_timestamps_are_normalized_and_local_chronology_is_checked(
    db: sqlite3.Connection, user_id: int
) -> None:
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=204,
        title="Canonical timestamps",
        last_checked_at=" 2026-07-14T01:00:00Z ",
        last_success_at=" 2026-07-14T00:59:00Z ",
        next_check_at=" 2026-07-14T05:00:00Z ",
    )
    row = db.execute(
        "SELECT last_checked_at, last_success_at, next_check_at FROM media_monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert row is not None
    assert tuple(row) == (
        "2026-07-14T01:00:00Z",
        "2026-07-14T00:59:00Z",
        "2026-07-14T05:00:00Z",
    )

    with pytest.raises(ValueError, match="last_success_at must not be after last_checked_at"):
        upsert_monitor(
            db,
            user_id=user_id,
            anilist_id=205,
            title="Invalid chronology",
            last_checked_at="2026-07-14T01:00:00Z",
            last_success_at="2026-07-14T01:00:01Z",
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-14 05:00:00",
        "2026-07-14T05:00:00+00:00",
        "2026-07-14T05:00:00.001Z",
        "2026-02-30T05:00:00Z",
    ],
)
def test_asset_rejects_noncanonical_inventory_timestamp(
    db: sqlite3.Connection, monitor_id: int, timestamp: str
) -> None:
    with pytest.raises(ValueError, match="inventory_verified_at"):
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash=f"invalid-inventory-{timestamp}",
            source_title="Invalid inventory timestamp",
            inventory_verified_at=timestamp,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-14 06:00:00",
        "2026-07-14T06:00:00+00:00",
        "2026-07-14T06:00:00.001Z",
        "2026-02-30T06:00:00Z",
    ],
)
def test_sync_run_rejects_noncanonical_completed_timestamp(
    db: sqlite3.Connection, monitor_id: int, timestamp: str
) -> None:
    with pytest.raises(ValueError, match="completed_at"):
        record_sync_run(
            db,
            monitor_id=monitor_id,
            outcome="completed",
            completed_at=timestamp,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-14 05:59:00",
        "2026-07-14T05:59:00+00:00",
        "2026-07-14T05:59:00.001Z",
        "2026-02-30T05:59:00Z",
    ],
)
def test_observation_rejects_noncanonical_observed_timestamp(
    db: sqlite3.Connection, monitor_id: int, timestamp: str
) -> None:
    run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash=f"invalid-observed-{timestamp}",
        source_title="Invalid observation timestamp",
    )
    with pytest.raises(ValueError, match="observed_at"):
        record_asset_observation(
            db,
            asset_id=asset_id,
            sync_run_id=run_id,
            observed_at=timestamp,
            rd_status="downloading",
        )


@pytest.mark.parametrize("field", ["requested_at", "completed_at"])
@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-07-14 06:00:00",
        "2026-07-14T06:00:00+00:00",
        "2026-07-14T06:00:00.001Z",
        "2026-02-30T06:00:00Z",
    ],
)
def test_removal_rejects_noncanonical_timestamps(
    db: sqlite3.Connection,
    monitor_id: int,
    field: str,
    timestamp: str,
) -> None:
    run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash=f"invalid-removal-time-{field}-{timestamp}",
        source_title="Invalid removal timestamp",
    )
    values = {
        "operation_key": f"invalid-removal-time-{field}-{timestamp}",
        "asset_id": asset_id,
        "sync_run_id": run_id,
        "requested_at": "2026-07-14T06:00:00Z",
        "completed_at": "2026-07-14T06:01:00Z",
        "outcome": "refused",
        field: timestamp,
    }
    with pytest.raises(ValueError, match=field):
        record_asset_removal(db, **values)  # type: ignore[arg-type]


def test_removal_rejects_completion_before_request(db: sqlite3.Connection, monitor_id: int) -> None:
    run_id = _completed_run(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="invalid-removal-chronology",
        source_title="Invalid removal chronology",
    )
    with pytest.raises(ValueError, match="completed_at must not be before requested_at"):
        record_asset_removal(
            db,
            operation_key="invalid-removal-chronology",
            asset_id=asset_id,
            sync_run_id=run_id,
            requested_at="2026-07-14T06:01:00Z",
            completed_at="2026-07-14T06:00:00Z",
            outcome="refused",
        )


def test_timestamp_whitespace_is_normalized_before_persistence(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    run_id = record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="completed",
        completed_at=" 2026-07-14T06:00:00Z ",
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="normalized-time",
        source_title="Normalized time",
        inventory_verified_at=" 2026-07-14T05:00:00Z ",
    )
    observation_id = record_asset_observation(
        db,
        asset_id=asset_id,
        sync_run_id=run_id,
        observed_at=" 2026-07-14T05:59:00Z ",
        rd_status="downloading",
    )
    removal_id = record_asset_removal(
        db,
        operation_key="normalized-refusal-time",
        asset_id=asset_id,
        sync_run_id=run_id,
        requested_at=" 2026-07-14T06:00:00Z ",
        completed_at=" 2026-07-14T06:01:00Z ",
        outcome="refused",
    )

    assert (
        db.execute("SELECT completed_at FROM media_sync_runs WHERE id = ?", (run_id,)).fetchone()[0]
        == "2026-07-14T06:00:00Z"
    )
    assert (
        db.execute(
            "SELECT inventory_verified_at FROM media_assets WHERE id = ?", (asset_id,)
        ).fetchone()[0]
        == "2026-07-14T05:00:00Z"
    )
    assert (
        db.execute(
            "SELECT observed_at FROM media_asset_observations WHERE id = ?", (observation_id,)
        ).fetchone()[0]
        == "2026-07-14T05:59:00Z"
    )
    removal_row = db.execute(
        "SELECT requested_at, completed_at FROM media_asset_removals WHERE id = ?",
        (removal_id,),
    ).fetchone()
    assert removal_row is not None
    assert tuple(removal_row) == ("2026-07-14T06:00:00Z", "2026-07-14T06:01:00Z")


def test_covered_episodes_ignores_malformed_legacy_inventory_timestamp(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="legacy-inventory-time",
        source_title="Legacy inventory timestamp",
        usable_coverage=(1, 2),
        inventory_verified_at="2026-07-14T05:00:00Z",
        status="downloaded",
    )
    db.execute(
        "UPDATE media_assets SET inventory_verified_at = ? WHERE id = ?",
        ("legacy-not-a-timestamp", asset_id),
    )

    assert covered_episodes(db, monitor_id) == set()


def test_record_asset_removal_retry_is_idempotent_without_reinserting(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    evidence = _removal_evidence(db, monitor_id)
    values = {
        "operation_key": "idempotent-removal",
        "asset_id": evidence["source_id"],
        "replacement_asset_id": evidence["replacement_id"],
        "candidate_attempt_id": evidence["attempt_id"],
        "sync_run_id": evidence["decision_run_id"],
        "support_observation_oldest_id": evidence["oldest_id"],
        "support_observation_newest_id": evidence["newest_id"],
        "requested_at": "2026-07-14T06:05:00Z",
        "outcome": "pending",
        "detail": "initial detail",
    }
    removal_id = record_asset_removal(db, **values)  # type: ignore[arg-type]
    statements: list[str] = []
    db.set_trace_callback(statements.append)
    try:
        same_id = record_asset_removal(db, **values)  # type: ignore[arg-type]
    finally:
        db.set_trace_callback(None)

    assert same_id == removal_id
    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_asset_removals WHERE operation_key = ?",
            ("idempotent-removal",),
        ).fetchone()[0]
        == 1
    )
    assert not any(
        statement.lstrip().upper().startswith("INSERT INTO MEDIA_ASSET_REMOVALS")
        for statement in statements
    )


def test_record_asset_removal_retry_matches_immutable_payload_after_terminal_update(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    evidence = _removal_evidence(db, monitor_id)
    values = {
        "operation_key": "terminal-retry-removal",
        "asset_id": evidence["source_id"],
        "replacement_asset_id": evidence["replacement_id"],
        "candidate_attempt_id": evidence["attempt_id"],
        "sync_run_id": evidence["decision_run_id"],
        "support_observation_oldest_id": evidence["oldest_id"],
        "support_observation_newest_id": evidence["newest_id"],
        "requested_at": "2026-07-14T06:05:00Z",
        "outcome": "pending",
        "detail": "initial detail",
    }
    removal_id = record_asset_removal(db, **values)  # type: ignore[arg-type]
    db.execute(
        """UPDATE media_asset_removals
           SET outcome = ?, completed_at = ?, detail = ?
           WHERE id = ?""",
        ("failed", "2026-07-14T06:06:00Z", "terminal detail", removal_id),
    )

    assert record_asset_removal(db, **values) == removal_id  # type: ignore[arg-type]


def test_record_asset_removal_rejects_operation_key_payload_mismatch(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    evidence = _removal_evidence(db, monitor_id)
    values = {
        "operation_key": "mismatched-removal",
        "asset_id": evidence["source_id"],
        "replacement_asset_id": evidence["replacement_id"],
        "candidate_attempt_id": evidence["attempt_id"],
        "sync_run_id": evidence["decision_run_id"],
        "support_observation_oldest_id": evidence["oldest_id"],
        "support_observation_newest_id": evidence["newest_id"],
        "requested_at": "2026-07-14T06:05:00Z",
    }
    removal_id = record_asset_removal(db, **values)  # type: ignore[arg-type]

    with pytest.raises(
        ValueError, match="operation_key already exists with a different immutable payload"
    ):
        record_asset_removal(
            db,
            **{**values, "requested_at": "2026-07-14T06:06:00Z"},
        )  # type: ignore[arg-type]

    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_asset_removals WHERE operation_key = ?",
            ("mismatched-removal",),
        ).fetchone()[0]
        == 1
    )
    assert (
        db.execute(
            "SELECT id FROM media_asset_removals WHERE operation_key = ?",
            ("mismatched-removal",),
        ).fetchone()[0]
        == removal_id
    )
