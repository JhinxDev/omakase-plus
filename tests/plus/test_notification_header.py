"""Authenticated header fixtures and actual static routes; no account/provider data."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus import routes


def render_notification_header(display_name="Sample viewer"):
    return routes.templates.get_template("base.html").render(
        active_page="home",
        email="viewer@example.invalid",
        display_name=display_name,
        avatar_available=False,
        movies_enabled=True,
        csrf_token="notification-fixture-csrf",
        asset_version="notification-fixture",
        synthetic_review=True,
    )


def test_header_contains_one_named_bell_and_nonmodal_reminder_region():
    html = render_notification_header()
    assert html.count("data-notifications>") == 1
    assert 'aria-controls="notification-panel"' in html
    assert 'role="dialog" aria-modal="false" aria-labelledby="notification-heading"' in html
    assert 'data-notification-badge aria-hidden="true" hidden' in html
    assert 'content="notification-fixture-csrf"' in html
    assert 'class="header-utilities"' in html
    assert "data-account-menu" in html
    assert "/plus/static/notifications.js?v=notification-fixture" in html


@pytest.mark.parametrize(
    "name,mime", [("notifications.css", "text/css"), ("notifications.js", "application/javascript")]
)
def test_notification_assets_are_served_by_the_real_allowlisted_route(name, mime):
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.get(f"/plus/static/{name}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(mime)
    assert response.content == (Path(routes.__file__).parent / "static" / name).read_bytes()


def test_authenticated_home_route_renders_bell_with_real_session_and_no_provider_reads(monkeypatch):
    import sqlite3
    from unittest.mock import AsyncMock

    import httpx

    from omakase.plus.auth import create_session
    from omakase.plus.db import run_migrations
    from omakase.plus.deps import get_db

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "send",
        AsyncMock(side_effect=AssertionError("Header rendering cannot contact providers")),
    )
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'viewer@example.invalid','fixture')"
    )
    db.commit()
    from omakase.plus import secrets as account_secrets

    monkeypatch.setenv("OMAKASE_PLUS_MASTER_KEY", "synthetic-header-key")
    monkeypatch.setattr(account_secrets, "_KEY", None)
    account_secrets.store_secret(db, 1, "display_name", "Jhinx")
    session = create_session(db, 1)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    try:
        with TestClient(app) as client:
            client.cookies.set("omakase_session", session)
            response = client.get("/plus/home")
            assert response.status_code == 200
            assert response.text.count("data-notifications>") == 1
            assert 'class="account-label" title="Jhinx">Jhinx</span>' in response.text
            assert 'aria-label="Notifications"' in response.text
            assert 'name="csrf-token" content="' in response.text
            assert "/plus/static/notifications.js?v=" in response.text
            assert client.get("/plus/api/notifications").json()["items"] == []
    finally:
        db.close()


@pytest.mark.parametrize(
    "name,label",
    [
        ("Jhinx", "Jhinx"),
        ("  ", "Account"),
        (None, "Account"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
    ],
)
def test_saved_header_name_is_escaped_trimmed_and_falls_back(name, label):
    html = render_notification_header(name)
    assert f">{label}</span>" in html
    assert "<script>alert(1)</script>" not in html
    if name and name.strip():
        assert f"for {label}" in html
