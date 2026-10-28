"""Persistent login renews active sessions without reviving revoked ones."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from omakase.plus import auth


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id INTEGER, expires_at TEXT)")
    yield conn
    conn.close()


def test_new_session_lasts_ninety_days(db, monkeypatch):
    monkeypatch.delenv("OMAKASE_SESSION_DAYS", raising=False)
    token = auth.create_session(db, 1)
    expires = datetime.fromisoformat(db.execute("SELECT expires_at FROM sessions").fetchone()[0])
    assert timedelta(days=89) < expires - datetime.now(timezone.utc) <= timedelta(days=90)
    assert auth.authenticate_session(db, token) == (1, False)


def test_near_expiry_renews_once(db, monkeypatch):
    monkeypatch.delenv("OMAKASE_SESSION_DAYS", raising=False)
    token = auth.create_session(db, 2)
    old = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    db.execute("UPDATE sessions SET expires_at=?", (old,))
    db.commit()
    assert auth.authenticate_session(db, token) == (2, True)
    assert auth.authenticate_session(db, token) == (2, False)
    assert db.execute("SELECT expires_at FROM sessions").fetchone()[0] != old


@pytest.mark.parametrize("expiry", ["2000-01-01T00:00:00+00:00", "bad", "2099-01-01T00:00:00"])
def test_expired_or_malformed_never_renews(db, expiry):
    db.execute("INSERT INTO sessions VALUES ('token', 1, ?)", (expiry,))
    db.commit()
    assert auth.authenticate_session(db, "token") == (None, False)
    assert auth.authenticate_session(db, "missing") == (None, False)


@pytest.mark.parametrize(
    "value,expected", [("7", 7), ("365", 365), ("0", 90), ("bad", 90), ("99999", 90)]
)
def test_configured_session_duration_is_bounded(monkeypatch, value, expected):
    monkeypatch.setenv("OMAKASE_SESSION_DAYS", value)
    assert auth.session_max_age() == expected * 86400
