"""SQLite connection manager and migration runner for omakase Plus."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from contextlib import contextmanager
from functools import cache
from pathlib import Path

_PACKAGED_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATIONS_DIR = _PACKAGED_MIGRATIONS_DIR

# Per-path connection cache so the same database file reuses one connection.
_db: dict[str, sqlite3.Connection] = {}

_MIGRATIONS_TABLE = (
    "CREATE TABLE IF NOT EXISTS _migrations ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "name TEXT NOT NULL UNIQUE, "
    "applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
    ")"
)

_MIGRATION_PATTERN = re.compile(r"^(\d+)-.+\.sql$")


def _resolved_data_dir(data_dir: str | os.PathLike[str] | None = None) -> str:
    if data_dir is not None:
        return os.fspath(data_dir)
    return os.getenv("OMAKASE_DATA_DIR", "data")


class IncompatibleSchemaError(RuntimeError):
    """The deployed application and SQLite schema are not compatible."""


def _database_path(data_dir):
    return Path(_resolved_data_dir(data_dir)).resolve() / "omakase-plus.db"


def _applied_names(conn):
    try:
        return frozenset(row[0] for row in conn.execute("SELECT name FROM _migrations"))
    except sqlite3.OperationalError:
        return frozenset()


def _migration_files(directory):
    files = []
    for path in directory.glob("*.sql"):
        if match := _MIGRATION_PATTERN.fullmatch(path.name):
            files.append((int(match[1]), path.name, path))
    return tuple(sorted(files))


@cache
def _packaged_migration_files():
    return _migration_files(_PACKAGED_MIGRATIONS_DIR)


def _migrations():
    if _MIGRATIONS_DIR == _PACKAGED_MIGRATIONS_DIR:
        return _packaged_migration_files()
    return _migration_files(_MIGRATIONS_DIR)


@cache
def _packaged_migration_names():
    return frozenset(name for _, name, _ in _packaged_migration_files())


def _expected_names():
    if _MIGRATIONS_DIR == _PACKAGED_MIGRATIONS_DIR:
        return _packaged_migration_names()
    return frozenset(name for _, name, _ in _migrations())


def _check_schema(conn):
    expected = _expected_names()
    if not expected or _applied_names(conn) != expected:
        raise IncompatibleSchemaError("Omakase Plus database schema is incompatible.")


def _configure_connection(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")


def _connect_runtime_file(path: Path) -> sqlite3.Connection:
    """Open an existing, fully migrated database file without migration locks."""
    uri = path.resolve().as_uri() + "?mode=rw"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
    except sqlite3.OperationalError as error:
        raise IncompatibleSchemaError("Omakase Plus database is unavailable.") from error
    try:
        _configure_connection(conn)
        _check_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _connect_runtime(data_dir: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Open an existing, fully migrated database without migration writer locks."""
    return _connect_runtime_file(_database_path(data_dir))


@contextmanager
def worker_connection(db: sqlite3.Connection):
    """Yield an independent connection for threadpool workers when file-backed.

    If a client disconnects while an endpoint offloads work to a thread, FastAPI's
    request dependency tears down the request connection; opening a fresh handle
    for file-backed databases avoids ``ProgrammingError`` while keeping
    in-memory/transactional test fixtures working on the caller connection.
    """
    file_path = ""
    if not db.in_transaction:
        try:
            row = db.execute("PRAGMA database_list").fetchone()
            file_path = row[2] if row and len(row) >= 3 else ""
        except sqlite3.Error:
            file_path = ""
    if file_path and file_path != ":memory:":
        worker = _connect_runtime_file(Path(file_path))
        try:
            yield worker
        finally:
            worker.close()
    else:
        yield db


def _enable_wal(conn):
    deadline = time.monotonic() + 5
    while True:
        try:
            if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
                raise IncompatibleSchemaError("Omakase Plus database could not enable WAL.")
            return
        except sqlite3.OperationalError as error:
            # Concurrent startup can race the journal transition before SQLite's
            # busy handler is active. Retry only this explicit lock failure.
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _connect(data_dir: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Initialize or migrate a database at application startup or an admin entry point."""
    path = _database_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    try:
        _configure_connection(conn)
        expected = _expected_names()
        applied = _applied_names(conn)
        if not expected or applied - expected:
            raise IncompatibleSchemaError("Omakase Plus database schema is incompatible.")
        _enable_wal(conn)
        if applied != expected:
            run_migrations(conn)
        _check_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def get_db(data_dir: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Return a cached SQLite connection (for CLI / admin / tests use only).

    Not safe for multi-threaded FastAPI workers — use ``_connect_runtime()`` +
    per-request close in ``deps.py`` for web routes.
    """
    resolved_data_dir = _resolved_data_dir(data_dir)
    db_path = os.path.join(resolved_data_dir, "omakase-plus.db")
    abs_path = str(Path(db_path).resolve())

    if abs_path in _db:
        return _db[abs_path]

    conn = _connect(resolved_data_dir)
    _db[abs_path] = conn
    return conn


def _migration_statements(sql: str) -> tuple[str, ...]:
    """Use SQLite's parser boundary check, including complete trigger bodies."""
    statements = []
    start = 0
    # Appending a delimiter permits a final SQL statement without a semicolon.
    script = sql + "\n;"
    for index, character in enumerate(script):
        if character == ";" and sqlite3.complete_statement(script[start : index + 1]):
            statement = script[start : index + 1]
            # Migration files cannot commit, detach, or change connection policy.
            token = re.match(
                r"(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*([A-Za-z]+)",
                statement,
                flags=re.DOTALL,
            )
            if token and token[1].upper() in {
                "BEGIN",
                "COMMIT",
                "END",
                "ROLLBACK",
                "SAVEPOINT",
                "RELEASE",
                "PRAGMA",
                "VACUUM",
                "ATTACH",
                "DETACH",
            }:
                raise ValueError(
                    "Migration SQL cannot control the owned transaction or connection."
                )
            statements.append(statement)
            start = index + 1
    if script[start:].strip():
        raise ValueError("Migration SQL contains an incomplete statement.")
    return tuple(statements)


def _historical_rd_alias(conn: sqlite3.Connection, name: str, sql: str) -> bool:
    """Recognize only the repository's proven redundant 002 column migration.

    The earlier download-tracking migration already created all three columns.
    An unrecorded or differently shaped partial schema is never inferred complete.
    """
    if name != "002-plus-rd-torrent-id.sql" or sql.strip() != (
        "ALTER TABLE anilist_plannings ADD COLUMN rd_torrent_id TEXT DEFAULT '';"
    ):
        return False
    if (
        conn.execute("SELECT 1 FROM _migrations WHERE name='002-download-tracking.sql'").fetchone()
        is None
    ):
        return False
    columns = {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in conn.execute("PRAGMA table_info(anilist_plannings)")
    }
    return all(
        columns.get(column) == ("TEXT", 1, "''", 0)
        for column in ("download_status", "download_info", "rd_torrent_id")
    )


def _rollback_migration(conn: sqlite3.Connection) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply trusted repository SQL with one atomic transaction per migration.

    Schema changes, data changes, and the applied marker commit together. Each
    writer obtains BEGIN IMMEDIATE and rechecks the marker under that lock, so
    concurrent startup is idempotent. Every BaseException rolls back the current
    migration; previously completed migrations remain committed. SQL boundaries
    use sqlite3.complete_statement, preserving trigger BEGIN/END bodies.

    Call only on a connection without an active caller transaction (default
    sqlite3 transaction mode or autocommit=True). A caller transaction is rejected
    without committing or rolling it back. Migration SQL cannot control its own
    transaction/connection. Unexpected existing schema fails closed: repairing a
    historically partial database requires separately reviewed recovery, not an
    applied marker or restoring a stale database.
    """
    if conn.in_transaction:
        raise RuntimeError("Migrations require a connection without an active caller transaction.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_MIGRATIONS_TABLE)
        conn.execute("COMMIT")
    except BaseException:
        _rollback_migration(conn)
        raise

    for _, name, path in _migrations():
        conn.execute("BEGIN IMMEDIATE")
        try:
            if conn.execute("SELECT 1 FROM _migrations WHERE name=?", (name,)).fetchone():
                conn.execute("COMMIT")
                continue
            sql = path.read_text(encoding="utf-8")
            statements = _migration_statements(sql)
            if not _historical_rd_alias(conn, name, sql):
                for statement in statements:
                    conn.execute(statement)
            conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
            conn.execute("COMMIT")
        except BaseException:
            _rollback_migration(conn)
            raise
