"""Machine manifest capability negotiation never turns client claims into qualification."""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus import language_publication, routes
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "fixture-machine-token")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    routes._manifest_rate_limiter.clear()
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        yield client
    db.close()


def test_actual_qualified_peer_receives_v3_without_exposing_a_query_override(client, monkeypatch):
    monkeypatch.setattr(language_publication, "LANGUAGE_PUBLICATION_QUALIFIED", True)
    headers = {
        "Authorization": "Bearer fixture-machine-token",
        "X-Omakase-Curator-Capabilities": "language-policy-v1",
    }
    response = client.get("/plus/api/library-manifest", headers=headers)
    assert response.status_code == 200 and response.json()["schema_version"] == "manifest-v3"
    assert response.headers["cache-control"] == "private, no-store"
    monkeypatch.setattr(language_publication, "LANGUAGE_PUBLICATION_QUALIFIED", False)
    response = client.get("/plus/api/library-manifest?qualification=true", headers=headers)
    assert response.status_code == 200 and response.json()["schema_version"] == "manifest-v2"


@pytest.mark.parametrize(
    "value",
    [
        "unknown",
        "language-policy-v1,unknown",
        "language-policy-v1,",
        "language-policy-v1,language-policy-v1",
        "x" * 257,
    ],
)
def test_malformed_or_unknown_capability_is_rejected_without_enabling_anything(client, value):
    headers = {
        "Authorization": "Bearer fixture-machine-token",
        "X-Omakase-Curator-Capabilities": value,
    }
    response = client.get("/plus/api/library-manifest", headers=headers)
    assert response.status_code == 400
    assert response.json() == {"detail": "Unsupported curator capabilities"}


def test_machine_auth_precedes_capability_negotiation(client):
    response = client.get(
        "/plus/api/library-manifest", headers={"X-Omakase-Curator-Capabilities": "unknown"}
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "language,quality,capabilities,expected",
    [
        (True, True, "language-policy-v1,quality-fallback-v1", 200),
        (True, False, "language-policy-v1,quality-fallback-v1", 500),
        (False, True, "language-policy-v1,quality-fallback-v1", 500),
        (True, True, "quality-fallback-v1", 500),
    ],
)
def test_quality_peer_cannot_supply_its_own_operator_qualification(
    client, monkeypatch, language, quality, capabilities, expected
):
    from omakase.plus import quality_publication

    monkeypatch.setattr(language_publication, "LANGUAGE_PUBLICATION_QUALIFIED", language)
    monkeypatch.setattr(quality_publication, "QUALITY_PUBLICATION_QUALIFIED", quality)
    response = client.get(
        "/plus/api/library-manifest?quality_qualification=true",
        headers={
            "Authorization": "Bearer fixture-machine-token",
            "X-Omakase-Curator-Capabilities": capabilities,
        },
    )
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["schema_version"] == "manifest-v4"
    assert response.headers["cache-control"] == "private, no-store"
