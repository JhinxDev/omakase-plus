"""Public app-origin surface: docs redirect, no API console, CSP fonts, mode gates.

Deployment modes are exercised by reloading ``omakase.web.server`` with
``OMAKASE_PLUS_PRIVATE`` set or cleared, matching the shipped compose files:

* public BYOK/Lite mode keeps ``/`` and ``/api/*`` exactly as before;
* Plus private mode redirects ``/`` into the Plus UI and hides ``/api/*``.

Neither mode serves an API console or an OpenAPI document on the app origin.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from fastapi.testclient import TestClient

DOCS_SITE = "https://omakase-plus.jhinx.dev/docs"


def _load_app(monkeypatch, *, private: bool):
    """Load a fresh server module for one deployment mode."""
    if private:
        monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    else:
        monkeypatch.delenv("OMAKASE_PLUS_PRIVATE", raising=False)
    monkeypatch.delenv("OMAKASE_PLUS_INVITE", raising=False)
    monkeypatch.delenv("OMAKASE_ALLOWED_HOSTS", raising=False)
    sys.modules.pop("omakase.web.server", None)
    server = importlib.import_module("omakase.web.server")
    return server.app


@pytest.fixture
def public_app(monkeypatch):
    """BYOK/Lite deployment: ``omakase web`` without private Plus mode."""
    return _load_app(monkeypatch, private=False)


@pytest.fixture
def private_app(monkeypatch):
    """Plus deployment: the shipped compose files set OMAKASE_PLUS_PRIVATE=true."""
    return _load_app(monkeypatch, private=True)


# ── Item: no public API console or OpenAPI route discovery ──────────────────


@pytest.mark.parametrize("fixture_name", ["public_app", "private_app"])
def test_docs_redirects_to_documentation_site(request, fixture_name):
    app = request.getfixturevalue(fixture_name)
    response = TestClient(app, follow_redirects=False).get("/docs")

    assert response.status_code == 307
    assert response.headers["location"] == DOCS_SITE


@pytest.mark.parametrize("fixture_name", ["public_app", "private_app"])
def test_app_origin_publishes_no_api_console_or_schema(request, fixture_name):
    app = request.getfixturevalue(fixture_name)

    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None
    client = TestClient(app, follow_redirects=False)
    for path in ("/openapi.json", "/redoc"):
        response = client.get(path)
        assert response.status_code == 404, f"{path} must not be public"
        assert "openapi" not in response.text.lower()


# ── Item: the Plus CSP allows the self-hosted Geist font ────────────────────


def test_plus_csp_explicitly_allows_self_hosted_font(private_app):
    client = TestClient(private_app)
    response = client.get("/plus/login")

    assert response.status_code == 200
    policy = response.headers["content-security-policy"]
    assert "font-src 'self'" in policy
    assert "fonts.gstatic.com" not in policy
    assert "default-src 'self'" in policy


def test_self_hosted_geist_font_is_served_from_the_same_origin(private_app):
    client = TestClient(private_app)
    response = client.get("/plus/static/geist-variable.woff2")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("font/woff2")
    assert len(response.content) > 1024
    # Assets are served under /plus, so the page policy must cover the font.
    policy = client.get("/plus/login").headers["content-security-policy"]
    assert "font-src 'self'" in policy


# ── Item: legacy product is gated on Plus origins, unchanged on BYOK ───────


def test_private_origin_root_sends_visitors_to_plus_ui(private_app):
    response = TestClient(private_app, follow_redirects=False).get("/")

    assert response.status_code == 302
    assert response.headers["location"] == "/plus/home"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/profile"),
        ("GET", "/api/sources"),
        ("GET", "/api/backends"),
        ("GET", "/api/models"),
        ("POST", "/api/recommend"),
    ],
)
def test_private_origin_hides_legacy_api(private_app, method, path):
    client = TestClient(private_app, follow_redirects=False)
    response = client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code == 404, f"{method} {path} must not be public in Plus mode"
    assert response.json() == {"detail": "Not Found"}
    assert response.headers["cache-control"] == "private, no-store"


def test_private_origin_keeps_supported_public_front_door(private_app):
    client = TestClient(private_app, follow_redirects=False)

    assert client.get("/plus/login").status_code == 200
    health = client.get("/plus/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert client.get("/favicon.ico").status_code == 200


def test_public_origin_keeps_legacy_byok_product(public_app):
    client = TestClient(public_app)

    home = client.get("/")
    assert home.status_code == 200
    assert "Try it with your API key" in home.text
    assert client.get("/api/sources").status_code == 200
    assert client.get("/api/backends").status_code == 200
