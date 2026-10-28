"""Authenticated portable connection forms, CSRF and secret-free rendering."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus import connections
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.secrets import read_secret, store_secret
from omakase.plus.web_security import csrf_token


@pytest.fixture
def browser(tmp_path, monkeypatch):
    from omakase.plus.connections_routes import create_connections_router

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    conn = sqlite3.connect(tmp_path / "routes.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    conn.execute(
        "INSERT INTO users(id,email,password_hash) VALUES (1,'fixture@example.invalid','synthetic')"
    )
    conn.commit()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(connections.__file__).parent / "templates"))
    templates.env.globals["asset_version"] = "test"
    app.include_router(create_connections_router(templates), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client, conn
    conn.close()


def login(client, db):
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    return csrf_token(
        Request({"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]})
    )


def test_chunked_connection_form_stops_reading_at_limit_without_auth(browser):
    client, _ = browser
    consumed = []

    async def chunks():
        for index in range(20):
            consumed.append(index)
            yield b"x" * 1024

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app), base_url="https://testserver"
        ) as request:
            return await request.post(
                "/plus/settings/connections/plex/save",
                content=chunks(),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    response = asyncio.run(run())
    assert response.status_code == 413
    assert len(consumed) == 5


def test_connection_pages_and_mutations_require_a_session(browser):
    client, _ = browser
    assert client.get("/plus/settings/connections").status_code == 401
    html = client.get("/plus/settings/connections", headers={"Accept": "text/html"})
    assert html.status_code == 302
    assert html.headers["location"] == "/plus/login"
    for action in ("save", "test", "disconnect"):
        assert client.post(f"/plus/settings/connections/riven/{action}").status_code == 401


@pytest.mark.parametrize("action", ["save", "test", "disconnect"])
def test_every_mutation_requires_csrf_even_without_browser_headers(browser, action):
    client, db = browser
    login(client, db)
    response = client.post(
        f"/plus/settings/connections/riven/{action}",
        data={"url": "https://riven.example.invalid", "api_key": "synthetic"},
    )
    assert response.status_code == 403
    assert read_secret(db, 1, "riven_api_key") is None


def test_forms_save_and_disconnect_only_the_named_service(browser):
    client, db = browser
    token = login(client, db)
    store_secret(db, 1, "prowlarr_categories", "5070,5000")
    response = client.post(
        "/plus/settings/connections/riven/save",
        data={
            "csrf_token": token,
            "url": "https://riven.example.invalid",
            "api_key": "synthetic-secret",
        },
    )
    assert response.status_code == 303
    assert (
        response.headers["location"] == "/plus/settings/connections?notice=saved#connection-riven"
    )
    assert read_secret(db, 1, "riven_api_key") == "synthetic-secret"
    page = client.get("/plus/settings/connections")
    assert page.status_code == 200
    assert "private, no-store" in page.headers["cache-control"]
    assert "synthetic-secret" not in page.text
    assert "riven.example.invalid" not in page.text
    assert 'id="connection-riven"' in page.text
    assert 'name="api_key"' in page.text
    assert "Key saved" in page.text
    assert "/plus/settings/tracking" in page.text
    disconnected = client.post(
        "/plus/settings/connections/riven/disconnect", data={"csrf_token": token}
    )
    assert disconnected.status_code == 303
    assert read_secret(db, 1, "riven_api_key") is None
    assert read_secret(db, 1, "prowlarr_categories") == "5070,5000"


def test_changed_origin_error_does_not_echo_submission_or_saved_key(browser):
    client, db = browser
    token = login(client, db)
    connections.save_connection(
        db, 1, "riven", url="https://first.example.invalid", api_key="test-key"
    )
    response = client.post(
        "/plus/settings/connections/riven/save",
        data={"csrf_token": token, "url": "https://second.example.invalid"},
    )
    assert response.status_code == 422
    assert "replacement key" in response.text
    assert "first.example.invalid" not in response.text
    assert "second.example.invalid" not in response.text
    assert "test-key" not in response.text


def test_native_test_route_calls_only_the_saved_service_and_redirects(browser, monkeypatch):
    client, db = browser
    token = login(client, db)
    seen = []

    async def native(db_arg, user_id, integration):
        seen.append((user_id, integration))
        return {"state": "connected"}

    monkeypatch.setattr(connections, "test_connection", native)
    response = client.post(
        "/plus/settings/connections/plex/test",
        data={"csrf_token": token, "url": "http://169.254.169.254", "api_key": "never-forward"},
    )
    assert response.status_code == 303
    assert seen == [(1, "plex")]
    assert "never-forward" not in response.text


def test_connection_router_is_closed_when_private_deployment_is_disabled(browser, monkeypatch):
    client, db = browser
    token = login(client, db)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/settings/connections").status_code == 404
    assert (
        client.post("/plus/settings/connections/riven/save", data={"csrf_token": token}).status_code
        == 404
    )


def test_unknown_connection_and_oversized_form_are_rejected_without_echo(browser):
    client, db = browser
    token = login(client, db)
    unknown = client.post("/plus/settings/connections/unknown/save", data={"csrf_token": token})
    assert unknown.status_code == 404
    oversized = client.post(
        "/plus/settings/connections/riven/save", data={"csrf_token": token, "api_key": "s" * 8192}
    )
    assert oversized.status_code == 413


def test_cooldown_names_and_times_are_accessible_text(browser):
    client, db = browser
    login(client, db)
    connections.save_connection(
        db, 1, "prowlarr", url="https://prowlarr.example.invalid", api_key="synthetic"
    )
    url, key = connections._values(db, 1, connections._spec("prowlarr"))
    receipt = connections._health(
        "degraded",
        "indexer_cooldowns",
        connected=True,
        checked_at="2026-10-27T18:00:00Z",
        indexers=[
            {
                "id": 1,
                "name": "Tokyo Toshokan",
                "state": "cooldown",
                "disabled_till": "2026-10-27T19:54:51Z",
            }
        ],
    )
    db.execute(
        "INSERT INTO user_connection_health VALUES (?,?,?,?,?)",
        (
            1,
            "prowlarr",
            connections._fingerprint(url, key),
            json.dumps(receipt),
            receipt["checked_at"],
        ),
    )
    db.commit()
    page = client.get("/plus/settings/connections")
    assert "Tokyo Toshokan" in page.text
    assert "Cooldown until" in page.text
    assert 'datetime="2026-10-27T19:54:51Z"' in page.text
    assert "Prowlarr is connected" in page.text

    attention = page.text.split('class="connection-attention"', 1)[1].split("</div>", 1)[0]
    assert "Tokyo Toshokan" in attention
    assert "is paused by Prowlarr until" in attention
    assert "Other available indexers can still be used" in attention
