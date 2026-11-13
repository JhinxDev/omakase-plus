from __future__ import annotations

import importlib
import sys

from fastapi.testclient import TestClient


def _load_public_app(monkeypatch):
    monkeypatch.delenv("OMAKASE_PLUS_PRIVATE", raising=False)
    sys.modules.pop("omakase.web.server", None)
    server = importlib.import_module("omakase.web.server")
    return server.app


def test_public_mode_promotes_byok_and_keeps_plus_private(monkeypatch):
    app = _load_public_app(monkeypatch)
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "Try it with your API key" in response.text
    assert "Nothing stored server-side" in response.text
    assert "Plus accounts are private and not currently open." in response.text
    assert "waitlist" not in response.text.lower()


def test_public_mode_does_not_mount_plus_routes(monkeypatch):
    app = _load_public_app(monkeypatch)
    client = TestClient(app)

    for path in (
        "/plus",
        "/plus/login",
        "/plus/dashboard",
        "/plus/api/library-manifest",
        "/plus/api/curator-receipts",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404
