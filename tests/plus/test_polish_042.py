"""0.4.2 polish: branded 404, logout destination, password policy and session bounds."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.auth import (
    create_session,
    hash_password,
    validate_session,
)
from omakase.plus.db import _db, run_migrations
from omakase.plus.deps import get_db as _deps_get_db


def _make_conn(tmp: str) -> sqlite3.Connection:
    path = os.path.join(tmp, "omakase-plus.db")
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    run_migrations(conn)
    return conn


def _cleanup():
    for conn in _db.values():
        try:
            conn.close()
        except Exception:
            pass
    _db.clear()


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    from omakase.plus.routes import _login_attempts

    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def client():
    tmp = tempfile.mkdtemp(prefix="omakase-polish-042-")
    conn = _make_conn(tmp)
    conn.close()

    def _override():
        return _make_conn(tmp)

    app = FastAPI()
    from omakase.plus.routes import router

    app.include_router(router)
    app.dependency_overrides[_deps_get_db] = _override
    test_client = TestClient(app)
    test_client.db_path = os.path.join(tmp, "omakase-plus.db")  # type: ignore[attr-defined]
    yield test_client
    _cleanup()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _signup(client, email: str = "owner@example.com", password: str = "long-enough"):
    os.environ["OMAKASE_PLUS_INVITE"] = "test123"
    try:
        return client.post(
            "/plus/signup",
            data={"email": email, "password": password, "confirm_password": password},
            follow_redirects=False,
        )
    finally:
        os.environ.pop("OMAKASE_PLUS_INVITE", None)


def _csrf(client) -> str:
    page = client.get("/plus/dashboard")
    match = re.search(r'name="csrf-token" content="([0-9a-f]{64})"', page.text)
    if match:
        return match.group(1)
    page = client.get("/plus/settings")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
    assert match is not None
    return match.group(1)


class TestBrandedErrors:
    def test_unknown_plus_page_returns_branded_html(self, client):
        response = client.get("/plus/this-page-does-not-exist", headers={"Accept": "text/html"})
        assert response.status_code == 404
        assert "Page not found" in response.text
        assert "404" in response.text
        assert response.headers.get("cache-control") == "private, no-store"

    def test_unknown_plus_page_stays_json_for_api_clients(self, client):
        response = client.get(
            "/plus/this-page-does-not-exist", headers={"Accept": "application/json"}
        )
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"detail": "Not Found"}

    def test_signed_out_notice_after_logout(self, client):
        assert _signup(client).status_code == 302
        response = client.post("/plus/logout", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/plus/login?signed_out=1"
        page = client.get("/plus/login?signed_out=1")
        assert page.status_code == 200
        assert "You have been signed out." in page.text


class TestSignupPolicy:
    def test_short_password_rejected(self, client):
        os.environ["OMAKASE_PLUS_INVITE"] = "test123"
        try:
            response = client.post(
                "/plus/signup",
                data={
                    "email": "short@example.com",
                    "password": "short",
                    "confirm_password": "short",
                },
            )
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)
        assert response.status_code == 200
        assert "at least 8 characters" in response.text
        conn = sqlite3.connect(client.db_path)
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM users WHERE email = 'short@example.com'"
                ).fetchone()[0]
                == 0
            )
        finally:
            conn.close()

    def test_long_password_accepted(self, client):
        assert _signup(client, email="long@example.com", password="long-enough").status_code == 302


class TestRateLimiterBounds:
    def test_prune_removes_stale_entries(self):
        from omakase.plus.routes import _login_attempts, _prune_login_attempts

        now = 10_000.0
        _login_attempts["stale.example"] = [1.0]
        _login_attempts["fresh.example"] = [now - 5]
        _prune_login_attempts(now)
        assert "stale.example" not in _login_attempts
        assert "fresh.example" in _login_attempts

    def test_prune_caps_tracked_buckets(self):
        from omakase.plus.routes import (
            _LOGIN_TRACKED_MAX_IPS,
            _login_attempts,
            _prune_login_attempts,
        )

        now = 10_000.0
        for index in range(_LOGIN_TRACKED_MAX_IPS * 2):
            _login_attempts[f"ip-{index}"] = [now - 1]
        _prune_login_attempts(now)
        assert len(_login_attempts) <= _LOGIN_TRACKED_MAX_IPS


class TestAbsoluteSessionCap:
    def test_old_session_is_rejected_and_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("old@example.com", hash_password("long-enough")),
            ).lastrowid
            conn.commit()
            session_id = create_session(conn, user_id)
            old = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
            conn.execute("UPDATE sessions SET created_at = ? WHERE id = ?", (old, session_id))
            conn.commit()
            assert validate_session(conn, session_id) is None
            assert (
                conn.execute("SELECT 1 FROM sessions WHERE id = ?", (session_id,)).fetchone()
                is None
            )
            conn.close()

    def test_fresh_session_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("fresh@example.com", hash_password("long-enough")),
            ).lastrowid
            conn.commit()
            session_id = create_session(conn, user_id)
            assert validate_session(conn, session_id) == user_id
            conn.close()
