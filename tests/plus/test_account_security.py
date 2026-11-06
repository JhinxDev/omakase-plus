"""Account security tests: password change, session revocation and admin recovery."""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from urllib.parse import unquote

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus import admin as admin_module
from omakase.plus.auth import (
    change_password,
    create_session,
    hash_password,
    revoke_user_sessions,
    session_count,
    validate_new_password,
    verify_password,
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
def _clear_login_rate_limiter():
    from omakase.plus.routes import _login_attempts

    _login_attempts.clear()
    yield
    _login_attempts.clear()


@pytest.fixture
def client():
    tmp = tempfile.mkdtemp(prefix="omakase-account-security-")
    conn = _make_conn(tmp)
    user_id = conn.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("owner@example.com", hash_password("old-password")),
    ).lastrowid
    conn.commit()
    conn.close()

    def _override():
        return _make_conn(tmp)

    app = FastAPI()
    from omakase.plus.routes import router

    app.include_router(router)
    app.dependency_overrides[_deps_get_db] = _override
    test_client = TestClient(app)
    test_client.db_path = os.path.join(tmp, "omakase-plus.db")  # type: ignore[attr-defined]
    test_client.user_id = user_id  # type: ignore[attr-defined]
    yield test_client
    _cleanup()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def _db_conn(client) -> sqlite3.Connection:
    conn = sqlite3.connect(client.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _login(client, password: str = "old-password"):
    return client.post(
        "/plus/login",
        data={"email": "owner@example.com", "password": password},
        follow_redirects=False,
    )


def _csrf(client) -> str:
    page = client.get("/plus/settings/security")
    assert page.status_code == 200
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page.text)
    assert match is not None
    return match.group(1)


def _add_other_session(client):
    conn = _db_conn(client)
    try:
        return create_session(conn, client.user_id)
    finally:
        conn.close()


class TestPasswordHelpers:
    def test_validate_new_password(self):
        assert validate_new_password("short") is not None
        assert validate_new_password("") is not None
        assert validate_new_password("long-enough-password") is None

    def test_change_password_rejects_wrong_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("unit@example.com", hash_password("old-password")),
            ).lastrowid
            conn.commit()
            assert change_password(conn, user_id, "wrong", "new-password") is False
            assert change_password(conn, user_id, "old-password", "new-password") is True
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            assert verify_password("new-password", row["password_hash"])
            assert not verify_password("old-password", row["password_hash"])
            conn.close()

    def test_revoke_user_sessions_can_keep_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = _make_conn(tmp)
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("sessions@example.com", hash_password("pw")),
            ).lastrowid
            conn.commit()
            current = create_session(conn, user_id)
            other = create_session(conn, user_id)
            assert session_count(conn, user_id, except_session_id=current) == 1
            assert revoke_user_sessions(conn, user_id, except_session_id=current) == 1
            assert session_count(conn, user_id) == 1
            assert conn.execute("SELECT id FROM sessions WHERE id = ?", (other,)).fetchone() is None
            assert (
                conn.execute("SELECT id FROM sessions WHERE id = ?", (current,)).fetchone()
                is not None
            )
            conn.close()


class TestSecurityPage:
    def test_security_page_requires_login(self, client):
        response = client.get("/plus/settings/security", follow_redirects=False)
        assert response.status_code in (302, 401)

    def test_security_page_shows_account_and_forms(self, client):
        assert _login(client).status_code == 302
        _add_other_session(client)
        page = client.get("/plus/settings/security")
        assert page.status_code == 200
        assert "Password &amp; sessions" in page.text or "Password & sessions" in page.text
        assert "owner@example.com" in page.text
        assert "1 other signed-in session" in page.text
        assert 'action="/plus/settings/security/password"' in page.text

    def test_password_change_requires_csrf(self, client):
        assert _login(client).status_code == 302
        response = client.post(
            "/plus/settings/security/password",
            data={
                "current_password": "old-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            follow_redirects=False,
        )
        assert response.status_code == 403

    def test_password_change_rejects_wrong_current(self, client):
        assert _login(client).status_code == 302
        token = _csrf(client)
        response = client.post(
            "/plus/settings/security/password",
            data={
                "csrf_token": token,
                "current_password": "wrong-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "Current password is incorrect." in unquote(response.headers["location"])
        conn = _db_conn(client)
        try:
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (client.user_id,)
            ).fetchone()
            assert verify_password("old-password", row["password_hash"])
        finally:
            conn.close()

    def test_password_change_revokes_other_sessions_and_keeps_current(self, client):
        assert _login(client).status_code == 302
        other = _add_other_session(client)
        token = _csrf(client)
        response = client.post(
            "/plus/settings/security/password",
            data={
                "csrf_token": token,
                "current_password": "old-password",
                "new_password": "new-password",
                "confirm_password": "new-password",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "Password updated" in unquote(response.headers["location"])
        conn = _db_conn(client)
        try:
            assert conn.execute("SELECT id FROM sessions WHERE id = ?", (other,)).fetchone() is None
            assert session_count(conn, client.user_id) == 1
            row = conn.execute(
                "SELECT password_hash FROM users WHERE id = ?", (client.user_id,)
            ).fetchone()
            assert verify_password("new-password", row["password_hash"])
        finally:
            conn.close()
        # Current session still works.
        assert client.get("/plus/settings/security").status_code == 200
        # Old password is rejected; new password is accepted.
        assert _login(client, "old-password").status_code == 200
        assert _login(client, "new-password").status_code == 302

    def test_sign_out_other_sessions_keeps_current(self, client):
        assert _login(client).status_code == 302
        other = _add_other_session(client)
        token = _csrf(client)
        response = client.post(
            "/plus/settings/security/sessions",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "Signed out 1 other session" in unquote(response.headers["location"])
        conn = _db_conn(client)
        try:
            assert conn.execute("SELECT id FROM sessions WHERE id = ?", (other,)).fetchone() is None
            assert session_count(conn, client.user_id) == 1
        finally:
            conn.close()
        assert client.get("/plus/settings/security").status_code == 200


class TestAdminRecovery:
    def test_set_password_updates_hash_and_revokes_sessions(self, client, monkeypatch, tmp_path):
        # Add extra sessions directly, then update through the CLI with its own connection.
        conn = _db_conn(client)
        try:
            create_session(conn, client.user_id)
        finally:
            conn.close()

        cli_conn = _db_conn(client)
        monkeypatch.setattr(admin_module, "get_db", lambda: cli_conn)
        result = CliRunner().invoke(
            admin_module.admin,
            ["set-password", "--email", "owner@example.com"],
            env={"OMAKASE_ADMIN_NEW_PASSWORD": "recovered-password"},
        )
        assert result.exit_code == 0, result.output
        assert "all sessions were revoked" in result.output
        row = cli_conn.execute(
            "SELECT password_hash FROM users WHERE email = 'owner@example.com'"
        ).fetchone()
        assert verify_password("recovered-password", row["password_hash"])
        assert cli_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        cli_conn.close()

    def test_set_password_rejects_short_password(self, monkeypatch, tmp_path):
        cli_conn = _make_conn(str(tmp_path))
        cli_conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("short@example.com", hash_password("old-password")),
        )
        cli_conn.commit()
        monkeypatch.setattr(admin_module, "get_db", lambda: cli_conn)
        result = CliRunner().invoke(
            admin_module.admin,
            ["set-password", "--email", "short@example.com", "--password", "short"],
        )
        assert result.exit_code == 1
        assert "at least 8 characters" in result.output
        cli_conn.close()

    def test_revoke_sessions_command(self, monkeypatch, tmp_path):
        cli_conn = _make_conn(str(tmp_path))
        user_id = cli_conn.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("revoke@example.com", hash_password("old-password")),
        ).lastrowid
        cli_conn.commit()
        create_session(cli_conn, user_id)
        create_session(cli_conn, user_id)
        monkeypatch.setattr(admin_module, "get_db", lambda: cli_conn)
        result = CliRunner().invoke(
            admin_module.admin, ["revoke-sessions", "--email", "revoke@example.com"]
        )
        assert result.exit_code == 0, result.output
        assert "Revoked 2 session(s)" in result.output
        assert cli_conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        cli_conn.close()
