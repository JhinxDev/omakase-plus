"""Migration035 is additive and keeps pre-existing rows/schemas on old-code rollback."""

import sqlite3
from pathlib import Path

import pytest

from omakase.plus import db as module


def test035_preserves_all_existing_tables_rows_and_old_migrator_compatibility(
    tmp_path, monkeypatch
):
    source = Path(module.__file__).parent / "migrations"
    old = tmp_path / "old"
    old.mkdir()
    for file in source.glob("*.sql"):
        if not file.name.startswith("035-"):
            (old / file.name).write_bytes(file.read_bytes())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(module, "_MIGRATIONS_DIR", old)
    module.run_migrations(conn)
    for n in range(1, 4):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (n, f"schema{n}@example.test", "hash"),
        )
    conn.commit()
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('_migrations','sqlite_sequence')"
        )
    ]
    before = {
        name: [tuple(r) for r in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
        for name in names
    }
    schemas = {
        r[0]: r[1] for r in conn.execute("SELECT name,sql FROM sqlite_master WHERE type='table'")
    }
    monkeypatch.setattr(module, "_MIGRATIONS_DIR", source)
    module.run_migrations(conn)
    for name in names:
        assert [
            tuple(r) for r in conn.execute('SELECT * FROM "' + name + '" ORDER BY rowid')
        ] == before[name]
        assert (
            conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0]
            == schemas[name]
        )
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    # Prior-code migrator ignores already-applied additive035; no down migration/DB restore.
    monkeypatch.setattr(module, "_MIGRATIONS_DIR", old)
    module.run_migrations(conn)
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 3
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='tracker_bridge_outbox'").fetchone()
    conn.execute(
        "INSERT INTO tracker_bridge_plans VALUES(?,?,?,?,?,?,?)",
        ("a" * 32, 1, "b" * 64, "synthetic-ciphertext", 1, 9999, "preview"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO tracker_bridge_outbox(plan_id,user_id,item_key,encrypted_patch,state,updated_at) VALUES(?,?,?,?,?,?)",
            ("a" * 32, 2, "0", "synthetic-ciphertext", "pending", 1),
        )
