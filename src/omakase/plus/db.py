"""SQLite connection manager and migration runner for omakase Plus."""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

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


def _connect(data_dir: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Create a new SQLite connection (per-request safe for FastAPI/uvicorn).

    Each call returns a fresh connection. Migrations are run on first connect;
    subsequent connects skip already-applied migrations.
    """
    resolved_data_dir = _resolved_data_dir(data_dir)
    db_path = os.path.join(resolved_data_dir, "omakase-plus.db")
    Path(resolved_data_dir).mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)
    return conn


def get_db(data_dir: str | os.PathLike[str] | None = None) -> sqlite3.Connection:
    """Return a cached SQLite connection (for CLI / admin / tests use only).

    Not safe for multi-threaded FastAPI workers — use ``_connect()`` +
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


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any SQL migration files that have not yet been applied.

    Migration files live under ``migrations/`` in the project root and
    follow the naming convention ``NN-slug.sql`` where ``NN`` is a
    zero-padded integer prefix that determines apply order.  Already-applied
    migrations are tracked in a ``_migrations`` table.
    """
    conn.execute(_MIGRATIONS_TABLE)
    conn.commit()

    applied = {
        row["name"] for row in conn.execute("SELECT name FROM _migrations ORDER BY id").fetchall()
    }

    if not _MIGRATIONS_DIR.is_dir():
        return

    pending: list[tuple[int, str, Path]] = []
    for f in sorted(_MIGRATIONS_DIR.iterdir()):
        m = _MIGRATION_PATTERN.match(f.name)
        if m and f.name not in applied:
            pending.append((int(m.group(1)), f.name, f))

    pending.sort(key=lambda x: x[0])
    for _, name, path in pending:
        sql = path.read_text(encoding="utf-8")
        try:
            conn.executescript(sql)
        except sqlite3.OperationalError as e:
            # Gracefully skip if columns/tables already exist (idempotent)
            if "duplicate column" not in str(e).lower() and "already exists" not in str(e).lower():
                raise
        conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
        conn.commit()
