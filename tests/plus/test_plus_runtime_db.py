"""Runtime SQLite requests can read but never acquire migration writer locks."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from omakase.plus import db as manager
from omakase.plus.deps import get_db


def test_request_connections_work_during_a_writer_transaction(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_DATA_DIR", str(tmp_path))
    with closing(manager._connect()) as writer:
        writer.execute("BEGIN IMMEDIATE")
        # An accidental migration lock fails immediately rather than waiting on the writer.
        connect = sqlite3.connect
        statements = []

        def traced(*args, **kwargs):
            db = connect(*args, **kwargs, timeout=0)
            db.set_trace_callback(statements.append)
            return db

        monkeypatch.setattr(sqlite3, "connect", traced)
        for _ in range(3):
            with closing(get_db()) as dependency:
                db = next(dependency)
                assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0
        assert all("BEGIN" not in sql and "journal_mode =" not in sql for sql in statements)


def test_runtime_rejects_missing_incomplete_and_newer_schema(tmp_path):
    with pytest.raises(manager.IncompatibleSchemaError):
        manager._connect_runtime(tmp_path)
    assert not (tmp_path / "omakase-plus.db").exists()
    with closing(manager._connect(tmp_path)) as installed:
        row = installed.execute("SELECT * FROM _migrations ORDER BY id DESC LIMIT 1").fetchone()
        installed.execute("DELETE FROM _migrations WHERE id=?", (row["id"],))
        installed.commit()
        with pytest.raises(manager.IncompatibleSchemaError):
            manager._connect_runtime(tmp_path)
        installed.execute("INSERT INTO _migrations(name) VALUES(?)", (row["name"],))
        installed.execute("INSERT INTO _migrations(name) VALUES('999-newer.sql')")
        installed.commit()
        with pytest.raises(manager.IncompatibleSchemaError):
            manager._connect_runtime(tmp_path)


def test_parallel_startup_then_runtime_read(tmp_path):
    def start():
        with closing(manager._connect(tmp_path)):
            pass
        with closing(manager._connect_runtime(tmp_path)) as db:
            return db.execute("PRAGMA quick_check").fetchone()[0]

    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda _: start(), range(2))) == ["ok", "ok"]


def test_failed_startup_leaves_runtime_incompatible(tmp_path, monkeypatch):
    scripts = tmp_path / "migrations"
    scripts.mkdir()
    (scripts / "001-first.sql").write_text("CREATE TABLE first(x);")
    (scripts / "002-fail.sql").write_text("CREATE TABLE second(x); SELECT missing_function();")
    monkeypatch.setattr(manager, "_MIGRATIONS_DIR", scripts)
    with pytest.raises(sqlite3.OperationalError):
        manager._connect(tmp_path)
    with pytest.raises(manager.IncompatibleSchemaError):
        manager._connect_runtime(tmp_path)
