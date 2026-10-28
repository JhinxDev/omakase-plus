"""Plex connection checks bind the exact server, never a stale check result."""

import asyncio
import ipaddress
import json
import sqlite3

import httpx
import pytest

from omakase.plus import connections
from omakase.plus.db import run_migrations
from omakase.plus.secrets import read_secret, store_secret


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'identity@fixture.invalid','x')")
    db.commit()

    async def ips(*args):
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr(connections, "_resolved_ips", ips)
    connections.save_connection(
        db, 1, "plex", url="https://plex.fixture.invalid", api_key="fixture-a"
    )
    yield db
    db.close()


def check(db, callback=None, identity='<MediaContainer machineIdentifier="current-machine"/>'):
    calls = []

    def native(request):
        calls.append(request.url.path)
        assert request.headers["X-Plex-Token"] == "fixture-a"
        if callback:
            callback(request)
        return httpx.Response(
            200, text=identity if request.url.path == "/identity" else '<MediaContainer size="0"/>'
        )

    result = asyncio.run(
        connections.test_connection(db, 1, "plex", transport=httpx.MockTransport(native))
    )
    return result, calls


def test_check_captures_current_server_and_keeps_identity_out_of_public_receipt(db):
    store_secret(db, 1, "plex_machine_id", "stale-machine")
    result, calls = check(db)
    assert read_secret(db, 1, "plex_machine_id") == "current-machine"
    assert result["state"] == "connected"
    assert set(calls) == {"/identity", "/library/sections"}
    assert "current-machine" not in json.dumps(result)
    assert (
        "current-machine"
        not in db.execute("SELECT health_json FROM user_connection_health").fetchone()[0]
    )


@pytest.mark.parametrize(
    "identity",
    [
        "<MediaContainer/>",
        '<MediaContainer machineIdentifier="bad/id"/>',
        '<wrong machineIdentifier="current-machine"/>',
    ],
)
def test_missing_or_invalid_identity_does_not_certify_connection(db, identity):
    result, _ = check(db, identity=identity)
    assert result["state"] == "error"
    assert read_secret(db, 1, "plex_machine_id") is None


def test_check_cannot_certify_replaced_credentials_after_await(db):
    changed = False

    def replace(request):
        nonlocal changed
        if not changed:
            changed = True
            connections.save_connection(
                db, 1, "plex", url="https://new.fixture.invalid", api_key="fixture-new"
            )

    result, _ = check(db, callback=replace)
    assert result["state"] != "connected"
    assert read_secret(db, 1, "plex_machine_id") is None
    assert db.execute("SELECT COUNT(*) FROM user_connection_health").fetchone()[0] == 0
