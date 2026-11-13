from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

from click.testing import CliRunner

from omakase.cli import cli
from omakase.plus import db as plus_db
from omakase.plus.media_cli import _create_backup
from omakase.plus.media_sync import SchedulerReceipt


def test_sync_media_cli_exits_zero_for_degraded_monitor_receipts():
    receipt = SchedulerReceipt(
        outcome="degraded",
        monitor_results=(),
        recovered_owner_hash="",
    )
    with patch("omakase.plus.media_cli.sync_media_once", return_value=receipt):
        result = CliRunner().invoke(cli, ["plus", "sync-media"])

    assert result.exit_code == 0
    assert "degraded" in result.output


def test_sync_media_cli_exits_nonzero_for_scheduler_failure():
    with patch(
        "omakase.plus.media_cli.sync_media_once",
        side_effect=RuntimeError("lease database failed"),
    ):
        result = CliRunner().invoke(cli, ["plus", "sync-media"])

    assert result.exit_code != 0
    assert "scheduler failed" in result.output.lower()


def test_plan_existing_movies_cli_writes_only_the_hash_bound_plan(tmp_path):
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "existing-movie-adoption-request-v1",
                "requests": [{"anilist_id": 42, "torrent_hash": "A" * 40, "quality": "1080p"}],
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "plan.json"
    expected = {"plan_hash": "b" * 64, "movies": [{}]}
    with patch("omakase.plus.media_cli.plan_existing_movies", return_value=expected):
        result = CliRunner().invoke(
            cli,
            [
                "plus",
                "plan-existing-movies",
                "--user-id",
                "1",
                "--request",
                str(request),
                "--plan",
                str(plan_path),
            ],
        )

    assert result.exit_code == 0
    assert "b" * 64 in result.output


def test_apply_existing_movies_cli_requires_out_of_band_plan_hash(tmp_path):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"plan_hash": "a" * 64}), encoding="utf-8")
    backup = tmp_path / "omakase-plus.db.backup"

    result = CliRunner().invoke(
        cli,
        [
            "plus",
            "apply-existing-movies",
            "--plan",
            str(plan),
            "--backup",
            str(backup),
            "--expected-plan-hash",
            "b" * 64,
        ],
    )

    assert result.exit_code != 0
    assert "plan hash" in result.output.lower()
    assert not backup.exists()


def test_plan_existing_series_cli_reports_hash_bound_plan(tmp_path):
    request = tmp_path / "series-request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": "existing-series-adoption-request-v1",
                "requests": [{"monitor_id": 104, "torrent_hash": "A" * 40, "quality": "1080p"}],
            }
        ),
        encoding="utf-8",
    )
    plan_path = tmp_path / "series-plan.json"
    expected = {"plan_hash": "c" * 64, "series": [{}]}
    with patch("omakase.plus.media_cli.plan_existing_series", return_value=expected):
        result = CliRunner().invoke(
            cli,
            [
                "plus",
                "plan-existing-series",
                "--user-id",
                "1",
                "--request",
                str(request),
                "--plan",
                str(plan_path),
            ],
        )

    assert result.exit_code == 0
    assert "c" * 64 in result.output


def test_apply_existing_series_cli_requires_out_of_band_plan_hash(tmp_path):
    plan = tmp_path / "series-plan.json"
    plan.write_text(json.dumps({"plan_hash": "a" * 64}), encoding="utf-8")
    backup = tmp_path / "omakase-plus.db.backup"

    result = CliRunner().invoke(
        cli,
        [
            "plus",
            "apply-existing-series",
            "--plan",
            str(plan),
            "--backup",
            str(backup),
            "--expected-plan-hash",
            "b" * 64,
        ],
    )

    assert result.exit_code != 0
    assert "plan hash" in result.output.lower()
    assert not backup.exists()


def test_adoption_apply_creates_a_validated_plan_bound_sqlite_backup(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    plus_db.run_migrations(db)
    db.execute("INSERT INTO users (email, password_hash) VALUES ('backup@example.com', 'hash')")
    db.commit()
    backup = tmp_path / "omakase-plus.db.backup"

    receipt = _create_backup(db, backup_path=backup, user_id=1, plan_hash="a" * 64)

    assert receipt["schema_version"] == "existing-movie-adoption-backup-v1"
    assert receipt["plan_hash"] == "a" * 64
    assert receipt["quick_check"] == "ok"
    assert len(receipt["backup_sha256"]) == 64
    assert backup.exists()
    assert backup.with_name(f"{backup.name}.receipt.json").exists()
    restored = sqlite3.connect(backup)
    try:
        assert restored.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert restored.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        restored.close()
        db.close()


def test_local_import_cli_exposes_hash_bound_plan(tmp_path):
    request = tmp_path / "local-request.json"
    request.write_text("{}", encoding="utf-8")
    root = tmp_path / "import"
    root.mkdir()
    plan = tmp_path / "local-plan.json"
    expected = {"plan_hash": "d" * 64, "files": [{}, {}]}

    with patch("omakase.plus.media_cli.plan_local_media", return_value=expected):
        result = CliRunner().invoke(
            cli,
            [
                "plus",
                "plan-local-import",
                "--user-id",
                "1",
                "--root",
                str(root),
                "--request",
                str(request),
                "--plan",
                str(plan),
            ],
        )

    assert result.exit_code == 0
    assert "d" * 64 in result.output
    assert "no files or database rows changed" in result.output


def test_plex_setup_cli_never_accepts_token_as_an_argument(tmp_path):
    request = tmp_path / "plex-request.json"
    request.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(
        cli,
        [
            "plus",
            "plan-plex-setup",
            "--plex-url",
            "http://plex.local:32400",
            "--request",
            str(request),
            "--plan",
            str(tmp_path / "plan.json"),
            "--plex-token",
            "should-not-be-accepted",
        ],
    )

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_plus_help_lists_professional_setup_commands():
    result = CliRunner().invoke(cli, ["plus", "--help"])

    assert result.exit_code == 0
    for command in (
        "doctor",
        "plan-local-import",
        "apply-local-import",
        "plan-plex-setup",
        "apply-plex-setup",
        "support-bundle",
    ):
        assert command in result.output
