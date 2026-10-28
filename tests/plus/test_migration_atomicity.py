"""Atomic trusted migrations with fresh synthetic SQLite data only."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omakase.plus import db as manager


def connection(path=":memory:", **kwargs):
    db = sqlite3.connect(path, **kwargs)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def scripts(tmp_path, monkeypatch, **files):
    directory = tmp_path / "migrations"
    directory.mkdir()
    for name, sql in files.items():
        (directory / name).write_text(sql)
    monkeypatch.setattr(manager, "_MIGRATIONS_DIR", directory)
    return directory


def seed(db):
    db.executescript(
        "CREATE TABLE sessions(id TEXT PRIMARY KEY,value TEXT); INSERT INTO sessions VALUES('session','unchanged'); CREATE TABLE history(id INTEGER PRIMARY KEY,score INTEGER,watched TEXT); INSERT INTO history VALUES(1,9,'2026-01-02');"
    )


def rebuild(failure):
    return (
        "CREATE TABLE history_next(id INTEGER PRIMARY KEY,score INTEGER,watched TEXT); INSERT INTO history_next SELECT * FROM history; "
        + ("SELECT missing_function(); " if failure == "before_drop" else "")
        + "DROP TABLE history; "
        + ("SELECT missing_function(); " if failure == "after_drop" else "")
        + "ALTER TABLE history_next RENAME TO history;"
    )


@pytest.mark.parametrize("stage", ["before_drop", "after_drop"])
def test_failure_rolls_back_rebuild_and_completion_then_retry_succeeds(
    tmp_path, monkeypatch, stage
):
    directory = scripts(tmp_path, monkeypatch, **{"001-rebuild.sql": rebuild(stage)})
    db = connection()
    seed(db)
    with pytest.raises(sqlite3.OperationalError):
        manager.run_migrations(db)
    assert not db.in_transaction
    assert [tuple(r) for r in db.execute("SELECT * FROM history")] == [(1, 9, "2026-01-02")]
    assert [tuple(r) for r in db.execute("SELECT * FROM sessions")] == [("session", "unchanged")]
    assert db.execute("SELECT name FROM sqlite_master WHERE name='history_next'").fetchall() == []
    assert db.execute("SELECT name FROM _migrations").fetchall() == []
    (directory / "001-rebuild.sql").write_text(rebuild(None))
    manager.run_migrations(db)
    manager.run_migrations(db)
    assert [tuple(r) for r in db.execute("SELECT * FROM history")] == [(1, 9, "2026-01-02")]
    assert db.execute("SELECT count(*) FROM _migrations").fetchone()[0] == 1


def test_keyboard_interrupt_after_drop_rolls_back(tmp_path, monkeypatch):
    class InterruptingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            result = super().execute(sql, *args, **kwargs)
            if sql.strip().upper() == "DROP TABLE HISTORY;":
                raise KeyboardInterrupt("synthetic interruption")
            return result

    scripts(tmp_path, monkeypatch, **{"001-rebuild.sql": rebuild(None)})
    db = connection(factory=InterruptingConnection)
    seed(db)
    with pytest.raises(KeyboardInterrupt):
        manager.run_migrations(db)
    assert not db.in_transaction
    assert db.execute("SELECT score FROM history").fetchone()[0] == 9
    assert db.execute("SELECT count(*) FROM _migrations").fetchone()[0] == 0


def test_applied_marker_failure_rolls_back_schema_and_data(tmp_path, monkeypatch):
    scripts(
        tmp_path,
        monkeypatch,
        **{
            "001-new.sql": "CREATE TABLE new_table(value TEXT); INSERT INTO history VALUES(2,5,'later');"
        },
    )
    db = connection()
    seed(db)
    db.execute(manager._MIGRATIONS_TABLE)
    db.execute(
        "CREATE TRIGGER refuse_marker BEFORE INSERT ON _migrations BEGIN SELECT RAISE(ABORT,'synthetic failure'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError):
        manager.run_migrations(db)
    assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1
    assert db.execute("SELECT name FROM sqlite_master WHERE name='new_table'").fetchall() == []


def test_trigger_bodies_comments_and_quoted_semicolons_are_not_split(tmp_path, monkeypatch):
    scripts(
        tmp_path,
        monkeypatch,
        **{
            "001-trigger.sql": "-- a ; comment\nCREATE TABLE input(value TEXT); CREATE TABLE output(value TEXT); CREATE TRIGGER copy_value AFTER INSERT ON input BEGIN INSERT INTO output VALUES('first;part'); INSERT INTO output VALUES(NEW.value); END; INSERT INTO input VALUES('second;part'); -- trailing comment"
        },
    )
    db = connection()
    manager.run_migrations(db)
    assert [r[0] for r in db.execute("SELECT * FROM output")] == ["first;part", "second;part"]


def test_caller_transaction_is_rejected_without_commit_or_rollback(tmp_path, monkeypatch):
    scripts(tmp_path, monkeypatch, **{"001-new.sql": "CREATE TABLE forbidden(value TEXT);"})
    db = connection()
    seed(db)
    db.execute("INSERT INTO history VALUES(2,4,'pending')")
    with pytest.raises(RuntimeError, match="transaction"):
        manager.run_migrations(db)
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT count(*) FROM history").fetchone()[0] == 1
    assert db.execute("SELECT name FROM sqlite_master WHERE name='_migrations'").fetchall() == []


@pytest.mark.parametrize(
    "sql", ["CREATE TABLE history(id INTEGER);", "ALTER TABLE history ADD COLUMN score INTEGER;"]
)
def test_unrecorded_partial_legacy_schema_fails_closed(tmp_path, monkeypatch, sql):
    scripts(tmp_path, monkeypatch, **{"001-partial.sql": sql})
    db = connection()
    seed(db)
    with pytest.raises(sqlite3.OperationalError):
        manager.run_migrations(db)
    assert db.execute("SELECT count(*) FROM _migrations").fetchone()[0] == 0
    assert db.execute("SELECT score FROM history").fetchone()[0] == 9


def test_concurrent_startup_rechecks_applied_under_writer_lock(tmp_path, monkeypatch):
    scripts(
        tmp_path,
        monkeypatch,
        **{
            "001-once.sql": "CREATE TABLE once_only(value TEXT); INSERT INTO once_only VALUES('once');"
        },
    )
    path = tmp_path / "synthetic.db"
    barrier = Barrier(2)

    def start():
        db = connection(path, timeout=5)
        barrier.wait(timeout=5)
        manager.run_migrations(db)
        db.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(start) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    db = connection(path)
    assert db.execute("SELECT count(*) FROM once_only").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM _migrations").fetchone()[0] == 1


def test_current_repository_migrations_fresh_and_repeat_are_valid():
    db = connection()
    manager.run_migrations(db)
    before = [tuple(r) for r in db.execute("SELECT * FROM _migrations")]
    manager.run_migrations(db)
    assert [tuple(r) for r in db.execute("SELECT * FROM _migrations")] == before
    assert len(before) >= 33
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "sql",
    ["COMMIT;", "-- comment\nEND TRANSACTION;", "PRAGMA foreign_keys=OFF;", "SAVEPOINT escape;"],
)
def test_migration_cannot_escape_owned_transaction(tmp_path, monkeypatch, sql):
    scripts(
        tmp_path, monkeypatch, **{"001-control.sql": "CREATE TABLE transient(value TEXT); " + sql}
    )
    db = connection()
    with pytest.raises(ValueError, match="transaction"):
        manager.run_migrations(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE name='transient'").fetchall() == []


@pytest.mark.parametrize(
    "shape,recorded,modified",
    [("TEXT", False, False), ("INTEGER", True, False), ("TEXT", True, True)],
)
def test_historical_alias_requires_exact_proven_predecessor(
    tmp_path, monkeypatch, shape, recorded, modified
):
    sql = "ALTER TABLE anilist_plannings ADD COLUMN rd_torrent_id TEXT DEFAULT '';"
    scripts(
        tmp_path,
        monkeypatch,
        **{"002-plus-rd-torrent-id.sql": sql + (" SELECT 1;" if modified else "")},
    )
    db = connection()
    db.execute(
        "CREATE TABLE anilist_plannings(download_status TEXT NOT NULL DEFAULT '', download_info TEXT NOT NULL DEFAULT '', rd_torrent_id "
        + shape
        + " NOT NULL DEFAULT '')"
    )
    db.execute(manager._MIGRATIONS_TABLE)
    if recorded:
        db.execute("INSERT INTO _migrations(name) VALUES('002-download-tracking.sql')")
        db.commit()
    with pytest.raises(sqlite3.OperationalError):
        manager.run_migrations(db)
    assert (
        db.execute(
            "SELECT name FROM _migrations WHERE name='002-plus-rd-torrent-id.sql'"
        ).fetchall()
        == []
    )


def test_autocommit_mode_has_same_atomic_rollback(tmp_path, monkeypatch):
    scripts(tmp_path, monkeypatch, **{"001-rebuild.sql": rebuild("after_drop")})
    db = connection(autocommit=True)
    seed(db)
    with pytest.raises(sqlite3.OperationalError):
        manager.run_migrations(db)
    assert not db.in_transaction
    assert db.execute("SELECT score FROM history").fetchone()[0] == 9


@pytest.mark.parametrize("when", ["before", "after", "after_rename"])
def test_actual_036_rebuild_interruption_preserves_receipts_sessions_and_history(
    tmp_path, monkeypatch, when
):
    import shutil

    class InterruptedRebuild(sqlite3.Connection):
        armed = False

        def execute(self, sql, *args, **kwargs):
            target = self.armed and sql.strip().upper() == (
                "ALTER TABLE MOVIE_VERIFICATIONS_NEXT RENAME TO MOVIE_VERIFICATIONS;"
                if when == "after_rename"
                else "DROP TABLE MOVIE_VERIFICATIONS;"
            )
            if target and when == "before":
                raise KeyboardInterrupt("synthetic pre-drop interrupt")
            result = super().execute(sql, *args, **kwargs)
            if target:
                raise KeyboardInterrupt("synthetic post-drop interrupt")
            return result

    original = manager._MIGRATIONS_DIR
    directory = scripts(tmp_path, monkeypatch)
    for source in original.glob("*.sql"):
        if int(source.name.split("-", 1)[0]) < 36:
            shutil.copyfile(source, directory / source.name)
    db = connection(factory=InterruptedRebuild)
    manager.run_migrations(db)
    db.executescript(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'synthetic@fixture.invalid','x'); INSERT INTO sessions(id,user_id,expires_at) VALUES('synthetic-session',1,'2099-01-01'); INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,updated_at) VALUES(1,'tt0000001',1,'Synthetic','2026-01-01'); INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,rating,status_source,rating_source,created_at,updated_at) VALUES(1,'tt0000001',1,'completed',9,'manual','manual','2026-01-01','2026-01-01');"
    )
    db.execute(
        "INSERT INTO movie_verifications(user_id,imdb_id,fingerprint,plex_key,selected_audio,state,code,detail,http_status,bytes_read,verified_at,expires_at) VALUES(1,'tt0000001',?,'synthetic-key','English','ready','ready','synthetic',206,65536,'2026-01-01','2099-01-01')",
        ("a" * 64,),
    )
    db.commit()
    before = {
        name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '"')]
        for name in ("sessions", "movie_history", "movie_verifications")
    }
    old_columns = [r[1] for r in db.execute("PRAGMA table_info(movie_verifications)")]
    shutil.copyfile(
        original / "036-movie-language-proof.sql", directory / "036-movie-language-proof.sql"
    )
    db.armed = True
    with pytest.raises(KeyboardInterrupt):
        manager.run_migrations(db)
    assert not db.in_transaction
    assert before == {
        name: [tuple(r) for r in db.execute('SELECT * FROM "' + name + '"')] for name in before
    }
    assert (
        db.execute(
            "SELECT name FROM _migrations WHERE name='036-movie-language-proof.sql'"
        ).fetchall()
        == []
    )
    assert (
        db.execute(
            "SELECT name FROM sqlite_master WHERE name='movie_verifications_next'"
        ).fetchall()
        == []
    )
    db.armed = False
    manager.run_migrations(db)
    columns = ",".join(old_columns)
    assert [
        tuple(r) for r in db.execute("SELECT " + columns + " FROM movie_verifications")
    ] == before["movie_verifications"]
    assert db.execute("SELECT proof_schema FROM movie_verifications").fetchone()[0] == 1
    assert [tuple(r) for r in db.execute("SELECT * FROM sessions")] == before["sessions"]
    assert [tuple(r) for r in db.execute("SELECT * FROM movie_history")] == before["movie_history"]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
