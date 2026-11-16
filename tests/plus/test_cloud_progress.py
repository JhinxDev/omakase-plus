"""Cloud playback progress is owner-scoped and bounded."""

import sqlite3
from types import SimpleNamespace

import pytest

from omakase.plus import cloud_progress
from omakase.plus.db import run_migrations
from omakase.plus.home_shell import movie_signals
from omakase.plus.households import ensure_personal_library
from omakase.plus.web_playback_navigation import catalog_resume_progress


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    run_migrations(connection)
    for user_id in (1, 2):
        connection.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user_id, f"{user_id}@example.invalid", "fixture"),
        )
    connection.commit()
    yield connection
    connection.close()


def test_cloud_progress_round_trip_and_finished_clear(db):
    saved = cloud_progress.save_position(
        db,
        user_id=1,
        file_key="t-torrent1-1",
        title="Film.mkv",
        position=120,
        duration=5400,
        now=1000,
    )
    assert saved["position_seconds"] == 120 and saved["label"] == "2:00"
    assert cloud_progress.get_position(db, 1, "t-torrent1-1")["label"] == "2:00"
    assert cloud_progress.positions(db, 1, ["t-torrent1-1"])["t-torrent1-1"]["label"] == "2:00"
    assert cloud_progress.get_position(db, 2, "t-torrent1-1") is None
    assert cloud_progress.positions(db, 2, ["t-torrent1-1"]) == {}

    cloud_progress.save_position(
        db,
        user_id=1,
        file_key="t-torrent1-1",
        title="Film.mkv",
        position=5390,
        duration=5400,
        now=1100,
    )
    assert cloud_progress.get_position(db, 1, "t-torrent1-1") is None
    assert cloud_progress.positions(db, 1, ["t-torrent1-1"]) == {}


def test_cloud_progress_rejects_unknown_keys_and_bad_positions(db):
    for key in ("", "not-a-file", "t-", "d-", "t-torrent1-0", "named:anime:x:1"):
        with pytest.raises(ValueError):
            cloud_progress.save_position(
                db, user_id=1, file_key=key, title="x", position=5, duration=600
            )
    with pytest.raises(ValueError):
        cloud_progress.save_position(
            db, user_id=1, file_key="d-download1", title="x", position=700, duration=600
        )
    with pytest.raises(ValueError):
        cloud_progress.save_position(
            db, user_id=1, file_key="d-download1", title="x", position="bad", duration=600
        )
    assert cloud_progress.valid_key("named:tv:123:1:2")
    assert not cloud_progress.valid_key("named:tv:123")


def test_raw_cloud_playback_never_projects_as_a_movie(db):
    ensure_personal_library(db, user_id=1)
    library_id = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[
        0
    ]
    for domain, identity in (("anime-film", "851491141156437"), ("movie", "tt1234567")):
        db.execute(
            "INSERT INTO web_playback_resumes(target_key,user_id,library_id,domain,canonical_id,"
            "season_number,episode_number,media_version,position_seconds,duration_seconds,updated_at)"
            " VALUES(?,1,?,?,?,0,0,?,120,600,1000)",
            (domain, library_id, domain, identity, "a" * 64),
        )
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,title,year,updated_at) "
        "VALUES(1,'tt1234567','Fixture film',2026,'2026-01-01T00:00:00Z')"
    )
    cards = movie_signals(db, user_id=1, library_id=library_id)
    assert [card.title for card in cards] == ["Fixture film"]

    cloud_progress.save_position(
        db,
        user_id=1,
        file_key="named:movie:tt1234567:0:0",
        title="Fixture film",
        position=300,
        duration=600,
        now=1000,
    )
    assert catalog_resume_progress(db, SimpleNamespace(viewer_id=1), "movie", cloud=True) == {
        "tt1234567": 50
    }
    assert catalog_resume_progress(db, SimpleNamespace(viewer_id=2), "movie", cloud=True) == {}


def test_cloud_progress_never_reads_another_viewers_rows(db):
    cloud_progress.save_position(
        db,
        user_id=1,
        file_key="d-download1",
        title="Mine.mp4",
        position=300,
        duration=900,
        now=1000,
    )
    cloud_progress.save_position(
        db,
        user_id=2,
        file_key="d-download1",
        title="Theirs.mp4",
        position=600,
        duration=900,
        now=1000,
    )
    assert cloud_progress.get_position(db, 1, "d-download1")["position_seconds"] == 300
    assert cloud_progress.get_position(db, 2, "d-download1")["position_seconds"] == 600
