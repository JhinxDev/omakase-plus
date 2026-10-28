"""Tests for omakase.plus.secrets — AES-256-GCM encryption + CRUD."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from omakase.plus.secrets import (
    decrypt,
    delete_secret,
    encrypt,
    read_secret,
    store_secret,
)


@pytest.fixture
def db() -> sqlite3.Connection:
    """Create a temp database with the user_secrets table."""
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """CREATE TABLE user_secrets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key_name TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, key_name)
        )"""
    )
    conn.commit()
    yield conn
    conn.close()


class TestEncryption:
    def test_roundtrip(self):
        original = "sk-ant-api03-abcdef123456"
        encrypted = encrypt(original)
        assert encrypted != original
        assert decrypt(encrypted) == original

    def test_encrypt_produces_different_ciphertexts(self):
        """Same plaintext encrypted twice yields different blobs (random nonce)."""
        a = encrypt("hello")
        b = encrypt("hello")
        assert a != b

    def test_decrypt_tampered_fails(self):
        encrypted = encrypt("secret")
        # Flip the last character
        tampered = encrypted[:-1] + ("A" if encrypted[-1] != "A" else "B")
        with pytest.raises(Exception):
            decrypt(tampered)

    def test_unicode_roundtrip(self):
        original = "こんにちは 🔑 sekret"
        assert decrypt(encrypt(original)) == original

    def test_empty_string(self):
        assert decrypt(encrypt("")) == ""


class TestSecretsCRUD:
    def test_store_and_read(self, db):
        store_secret(db, 1, "llm_api_key", "sk-test-123")
        assert read_secret(db, 1, "llm_api_key") == "sk-test-123"

    def test_read_missing_returns_none(self, db):
        assert read_secret(db, 1, "nonexistent") is None

    def test_store_upserts(self, db):
        store_secret(db, 1, "key_a", "first")
        store_secret(db, 1, "key_a", "second")
        assert read_secret(db, 1, "key_a") == "second"
        # Should still be only one row
        count = db.execute(
            "SELECT COUNT(*) FROM user_secrets WHERE user_id = 1 AND key_name = 'key_a'"
        ).fetchone()[0]
        assert count == 1

    def test_delete(self, db):
        store_secret(db, 1, "key_a", "value")
        delete_secret(db, 1, "key_a")
        assert read_secret(db, 1, "key_a") is None

    def test_delete_nonexistent_does_not_error(self, db):
        delete_secret(db, 1, "nonexistent")  # should not raise

    def test_isolation_by_user(self, db):
        store_secret(db, 1, "api_key", "user1-key")
        store_secret(db, 2, "api_key", "user2-key")
        assert read_secret(db, 1, "api_key") == "user1-key"
        assert read_secret(db, 2, "api_key") == "user2-key"

    def test_stored_value_is_not_plaintext(self, db):
        store_secret(db, 1, "api_key", "secret-plaintext")
        row = db.execute(
            "SELECT encrypted_value FROM user_secrets WHERE user_id = 1 AND key_name = 'api_key'"
        ).fetchone()
        assert row is not None
        assert "secret-plaintext" not in row["encrypted_value"]
