"""FastAPI dependency helpers for Omakase Plus.

Provides a ``get_db`` dependency that yields a per-request SQLite connection.
Each request gets its own connection to avoid SQLite threading issues with
uvicorn's worker threads.
"""

from __future__ import annotations

import sqlite3

from omakase.plus.db import _connect_runtime


def get_db() -> sqlite3.Connection:
    """FastAPI dependency — yields a per-request database connection.

    Opens a fresh connection to the migrated database, checks schema compatibility,
    and closes it when the request completes.
    """
    conn = _connect_runtime()
    try:
        yield conn
    finally:
        conn.close()
