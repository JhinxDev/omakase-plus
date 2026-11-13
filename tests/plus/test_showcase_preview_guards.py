"""The showcase never adopts owner configuration or uses remote assets/network."""

import json
import os
import runpy
import socket
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest


def script():
    return runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/showcase_preview.py"))


def test_inherited_configuration_is_rejected_before_app_import(tmp_path):
    module = script()
    with patch.dict(os.environ, {"OMAKASE_DATA_DIR": "owner-data-must-not-open"}, clear=True):
        with pytest.raises(SystemExit, match="refuses inherited"):
            module["prepare_environment"](tmp_path)


def test_only_empty_temporary_data_and_finite_environment_are_accepted(tmp_path):
    module = script()
    with patch.dict(os.environ, {}, clear=True):
        environment = module["prepare_environment"](tmp_path)
        assert environment["OMAKASE_DATA_DIR"] == str(tmp_path)
        assert {k for k in environment if k.startswith("OMAKASE_")} == module["ENV_NAMES"]
        assert environment["PYTHON_DOTENV_DISABLED"] == "1"
    (tmp_path / "unrelated.db").write_text("preserve")
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(SystemExit, match="newly created empty"):
            module["prepare_environment"](tmp_path)


def test_optional_ui_is_present_without_enabling_tracker_writers(tmp_path):
    with patch.dict(os.environ, {}, clear=True):
        environment = script()["prepare_environment"](tmp_path)
    assert environment["OMAKASE_PLUS_HOUSEHOLDS_ENABLED"] == "true"
    assert environment["OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED"] == "true"
    assert "OMAKASE_TRACKER_BRIDGE_WORKER_ENABLED" not in environment
    assert "OMAKASE_TRACKER_BRIDGE_WRITERS_READY" not in environment


@pytest.mark.parametrize("state", ["personal", "multiple", "shared", "unavailable"])
def test_library_fixtures_use_native_scope_without_adopting_personal_titles(state):
    from omakase.plus import households
    from omakase.plus.db import run_migrations

    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(1,'alex@example.com','sample')"
        )
        db.commit()
        script()["seed_library_fixture"](db, user_id=1, state=state)
        navigation = households.navigation_context(db, viewer_id=1)
        assert len(navigation["libraries"]) == (2 if state in {"multiple", "shared"} else 1)
        assert navigation["selected_kind"] == ("household" if state == "shared" else "personal")
        assert db.execute("SELECT count(*) FROM library_items").fetchone()[0] == 0
        changes = db.total_changes
        with pytest.raises(ValueError, match="Unknown library fixture"):
            script()["seed_library_fixture"](db, user_id=1, state="owner-data")
        assert db.total_changes == changes


@pytest.mark.parametrize("relative", ["../outside.jpg", "https://example.invalid/poster.jpg"])
def test_manifest_cannot_escape_the_explicit_cached_art_root(tmp_path, relative):
    art = tmp_path / "art"
    art.mkdir()
    (tmp_path / "outside.jpg").write_bytes(b"fixture")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"anime": [{"anilist_id": 1, "title": "Sample", "poster": relative}]})
    )
    with pytest.raises(ValueError, match="Artwork"):
        script()["load_manifest"](manifest, art)


def test_outbound_guard_rejects_direct_dns_and_helper_connections(monkeypatch):
    module = script()
    # Register originals with pytest so the global guard is restored after this test.
    for owner, name in [
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
    ]:
        monkeypatch.setattr(owner, name, getattr(owner, name))
    module["prohibit_outbound"]()
    with socket.socket() as connection:
        with pytest.raises(OSError, match="External networking"):
            connection.connect(("203.0.113.1", 443))
        with pytest.raises(OSError, match="External networking"):
            connection.connect_ex(("203.0.113.1", 443))
    with pytest.raises(OSError, match="External networking"):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(OSError, match="External networking"):
        socket.getaddrinfo("example.invalid", 443)


def test_populated_title_fixture_is_pure_and_separates_watch_history_from_media():
    catalog = {"anime": {21: {"anilist_id": 21, "title": "A cached public title", "episodes": 10}}}
    title = script()["title_presentation_card"](catalog)
    assert title["title"] == "A cached public title" and title["anilist_id"] == 21
    assert title["episode_total"] == 10
    assert title["progress_episodes"] == 2 and 2 not in title["usable_episodes"]
    assert 4 in title["usable_episodes"] and 4 > title["progress_episodes"]
    assert title["usable"] == len(title["usable_episodes"]) == title["plex_visible"]
    assert title["current_assets"][0]["usable_coverage"] == title["usable_episodes"]
    assert title["timeline"] and title["current_assets"][0]["subtitles"]
    assert "http" not in title["current_assets"][0]["source"]
    assert "SAMPLE RELEASE" in title["current_assets"][0]["source"]
