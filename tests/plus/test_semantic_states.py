"""Real route errors and explicit tracking state variants, using disposable data only."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus import routes
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db


@pytest.fixture
def semantic_client(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "send",
        AsyncMock(side_effect=AssertionError("No provider calls in semantic route tests")),
    )
    routes._login_attempts.clear()
    db_path = tmp_path / "omakase-plus.db"

    def database():
        db = sqlite3.connect(db_path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        run_migrations(db)
        try:
            yield db
        finally:
            db.close()

    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        yield client, app, db_path
    routes._login_attempts.clear()


def assert_error(response, detail):
    assert response.status_code == 200
    assert (
        'class="product-notice notice-danger semantic-notice--error" role="alert"' in response.text
    )
    assert detail in response.text
    assert "/plus/static/semantic-states.css" in response.text


def test_failed_login_real_route_is_an_error_not_success(semantic_client):
    client, _, _ = semantic_client
    response = client.post(
        "/plus/login", data={"email": "absent@example.invalid", "password": "invalid-fixture"}
    )
    assert_error(response, "Invalid email or password.")
    assert 'action="/plus/login"' in response.text
    assert 'name="password"' in response.text


def test_signup_validation_real_route_preserves_form_and_does_not_create_user(semantic_client):
    client, _, db_path = semantic_client
    response = client.post(
        "/plus/signup",
        data={
            "email": "fixture@example.invalid",
            "password": "fixture-password",
            "confirm_password": "different-password",
        },
    )
    assert_error(response, "Passwords do not match.")
    assert 'action="/plus/signup"' in response.text
    assert 'name="confirm_password"' in response.text
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 0


def test_real_tracking_route_marks_disconnected_states_neutral(semantic_client):
    client, app, _ = semantic_client
    app.dependency_overrides[routes.require_user] = lambda: SimpleNamespace(
        id=1, email="fixture@example.invalid"
    )
    response = client.get("/plus/settings/tracking")
    assert response.status_code == 200
    assert "privacy-stamp semantic-state--disconnected" in response.text
    assert response.text.count('class="state-tag semantic-state--disconnected">Disconnected') == 2
    assert "semantic-state--connected" not in response.text
    assert 'name="csrf_token"' in response.text


@pytest.mark.parametrize("connected", [False, True])
def test_tracking_fragment_uses_explicit_connection_variants(connected):
    rendered = routes.templates.get_template("_settings_tracking.html").render(
        anilist_connected=connected,
        apple_tv_integration=SimpleNamespace(enabled=connected, key_id="fixture-key"),
        apple_tv_watch_enabled=False,
    )
    variant = "connected" if connected else "disconnected"
    assert f"privacy-stamp semantic-state--{variant}" in rendered
    assert rendered.count(f"semantic-state--{variant}") == 3
    assert ('formaction="/plus/integrations/anilist/disconnect"' in rendered) == connected


def test_semantic_styles_define_independent_error_and_connection_colors():
    root = Path(routes.__file__).parent
    css = (root / "static/semantic-states.css").read_text(encoding="utf-8")
    assert ".product-notice.notice-danger" in css
    assert "background: #321e27" in css
    assert "color: #f7c3c8" in css
    assert "color: #c2cad8" in css
    assert "color: #a6e8c6" in css
