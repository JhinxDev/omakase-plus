"""Offline release contract, transport privacy, cache and authenticated form checks."""

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

from omakase.plus import update_service as updates
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.update_routes import create_updates_router
from omakase.plus.web_security import csrf_token


def metadata(**changes):
    return {
        "schema_version": 1,
        "product": "omakase-plus",
        "channel": "stable",
        "version": "2026.10.28.1",
        "released_at": "2026-10-28T12:00:00Z",
        "notes_url": updates.NOTES_URL,
        "summary": "A polished new release.",
        **changes,
    }


def transport(monkeypatch, handler):
    real_client = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(updates.httpx, "AsyncClient", client)
    return options


@pytest.fixture(autouse=True)
def fresh_checker(monkeypatch):
    monkeypatch.setenv("OMAKASE_UPDATE_CHECKS", "true")
    monkeypatch.setattr(updates, "checker", updates.UpdateChecker())


@pytest.mark.parametrize(
    "version,newer",
    [
        ("2026.10.28.1", True),
        (updates.PRODUCT_VERSION, False),
        ("2026.10.26.9", False),
        (
            ".".join(
                [
                    *updates.PRODUCT_VERSION.split(".")[:-1],
                    str(int(updates.PRODUCT_VERSION.split(".")[-1]) + 1),
                ]
            ),
            True,
        ),
    ],
)
def test_valid_release_comparison(version, newer):
    assert updates.parse_release(json.dumps(metadata(version=version)).encode()).newer is newer


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "v2026.10.28.1"},
        {"version": "2026.02.30.1"},
        {"version": 42},
        {"schema_version": True},
        {"schema_version": 2},
        {"product": "other"},
        {"channel": "beta"},
        {"released_at": "2026-02-30T00:00:00Z"},
        {"released_at": "2026-10-28T12:00:00+00:00"},
        {"summary": ""},
        {"summary": "x" * 1001},
        {"summary": "bad\ntext"},
        {"notes_url": "https://jhinx.dev.evil.test/work/omakase-plus"},
        {"notes_url": "https://user@jhinx.dev/work/omakase-plus"},
        {"notes_url": "http://jhinx.dev/work/omakase-plus"},
        {"notes_url": "https://app.example.com/plus"},
        {"extra": "unknown"},
    ],
)
def test_malformed_metadata_rejected(changes):
    with pytest.raises(ValueError):
        updates.parse_release(json.dumps(metadata(**changes)).encode())


def test_duplicate_fields_rejected():
    with pytest.raises(ValueError):
        updates.parse_release(b'{"schema_version":1,"schema_version":1}')


def test_fixed_public_get_privacy_and_cache(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=metadata())

    options = transport(monkeypatch, handler)
    first, cooldown = asyncio.run(updates.checker.check())
    second, remaining = asyncio.run(updates.checker.check())
    assert first.release.newer and not first.error and cooldown == 0
    assert second == first and remaining >= 899 and len(seen) == 1
    req = seen[0]
    assert str(req.url) == updates.FEED_URL and req.method == "GET" and not req.content
    assert not req.url.query and "cookie" not in req.headers and "authorization" not in req.headers
    assert updates.PRODUCT_VERSION not in str(req.headers)
    assert options[0]["follow_redirects"] is False and options[0]["trust_env"] is False
    assert options[0]["timeout"] == 5.0


@pytest.mark.parametrize(
    "kind", ["redirect", "oversize", "offline", "malformed", "encoding", "html"]
)
def test_failures_sanitized_and_cached(monkeypatch, kind):
    seen = []

    def handler(request):
        seen.append(request)
        if kind == "offline":
            raise httpx.ConnectError("SECRET transport failure", request=request)
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/SECRET"})
        if kind == "oversize":
            return httpx.Response(
                200,
                content=b"SECRET" + b"x" * updates.MAX_BYTES,
                headers={"content-type": "application/json"},
            )
        if kind == "encoding":
            return httpx.Response(200, json=metadata(), headers={"content-encoding": "br"})
        if kind == "html":
            return httpx.Response(200, text="SECRET html")
        return httpx.Response(200, content=b"SECRET", headers={"content-type": "application/json"})

    transport(monkeypatch, handler)
    result, _ = asyncio.run(updates.checker.check())
    assert result.release is None and "unavailable" in result.error and "SECRET" not in result.error
    assert result.checked_at and asyncio.run(updates.checker.check())[1] >= 899
    assert len(seen) == 1


def test_retry_after_and_last_success_retained(monkeypatch):
    responses = [
        httpx.Response(200, json=metadata()),
        httpx.Response(503, headers={"retry-after": "3600"}),
    ]
    transport(monkeypatch, lambda request: responses.pop(0))
    success, _ = asyncio.run(updates.checker.check())
    updates.checker.next_check = 0
    failed, _ = asyncio.run(updates.checker.check())
    assert failed.release == success.release and failed.error
    assert 3599 <= updates.checker.retry_after() <= 3600
    assert updates.retry_seconds("-1") == updates.MIN_INTERVAL
    assert updates.retry_seconds("999999999999") == updates.MAX_RETRY_AFTER
    assert updates.retry_seconds("invalid") == updates.MIN_INTERVAL
    assert updates.retry_seconds("Wed, 28 Oct 2099 12:00:00 GMT") == updates.MAX_RETRY_AFTER


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    conn = sqlite3.connect(tmp_path / "updates.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute(
        "INSERT INTO users(id,email,password_hash) VALUES (1,'fixture@example.invalid','synthetic')"
    )
    conn.commit()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(updates.__file__).parent / "templates"))
    templates.env.globals["asset_version"] = "test"
    app.include_router(create_updates_router(templates), prefix="/plus")
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


def test_auth_private_boundary_and_cold_get(browser, monkeypatch):
    client, db = browser
    assert client.get("/plus/settings/updates").status_code == 401
    assert (
        client.post("/plus/settings/updates/check", data={"csrf_token": "fake"}).status_code == 401
    )
    login(client, db)

    def forbidden(**kwargs):
        pytest.fail("GET must never perform an update check")

    monkeypatch.setattr(updates.httpx, "AsyncClient", forbidden)
    response = client.get("/plus/settings/updates")
    assert response.status_code == 200 and updates.PRODUCT_VERSION in response.text
    assert (
        "No check has been made" in response.text
        and response.headers["cache-control"] == "private, no-store"
    )
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/settings/updates").status_code == 404
    assert client.post("/plus/settings/updates/check").status_code == 404


def test_csrf_bounded_and_unknown_forms_do_not_call_network(browser, monkeypatch):
    client, db = browser
    token = login(client, db)
    monkeypatch.setattr(
        updates.httpx, "AsyncClient", lambda **kwargs: pytest.fail("Invalid form caused network")
    )
    assert (
        client.post("/plus/settings/updates/check", data={"csrf_token": "wrong"}).status_code == 403
    )
    assert (
        client.post(
            "/plus/settings/updates/check",
            data={"csrf_token": token, "url": "https://evil.invalid"},
        ).status_code
        == 422
    )
    assert client.post("/plus/settings/updates/check", content=b"x" * 5000).status_code == 413
    assert client.post("/plus/settings/updates/check", json={}).status_code == 415


def test_explicit_check_rendering_cache_and_disabled(browser, monkeypatch):
    client, db = browser
    token = login(client, db)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=metadata(summary="<script>alert(1)</script>"))

    transport(monkeypatch, handler)
    result = client.post("/plus/settings/updates/check", data={"csrf_token": token})
    assert result.status_code == 200 and "Update available" in result.text
    assert "&lt;script&gt;" in result.text and "<script>alert(1)</script>" not in result.text
    assert updates.NOTES_URL in result.text
    cached = client.post("/plus/settings/updates/check", data={"csrf_token": token})
    assert cached.status_code == 429 and int(cached.headers["retry-after"]) >= 899
    assert len(seen) == 1
    monkeypatch.setenv("OMAKASE_UPDATE_CHECKS", "false")
    disabled = client.post("/plus/settings/updates/check", data={"csrf_token": token})
    assert disabled.status_code == 200 and "disabled by this host" in disabled.text
    assert len(seen) == 1


def test_stream_limit_stops_consuming_and_closes(monkeypatch):
    consumed = []
    closed = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(100):
                consumed.append(index)
                yield b"x" * 4096

        async def aclose(self):
            closed.append(True)

    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, stream=Stream(), headers={"content-type": "application/json"}
        ),
    )
    result, _ = asyncio.run(updates.checker.check())
    assert result.error and len(consumed) == 9 and closed


def test_concurrent_requests_share_one_network_attempt(monkeypatch):
    seen = []

    async def handler(request):
        seen.append(request)
        await asyncio.sleep(0)
        return httpx.Response(200, json=metadata())

    transport(monkeypatch, handler)

    async def run():
        return await asyncio.gather(updates.checker.check(), updates.checker.check())

    results = asyncio.run(run())
    assert len(seen) == 1 and results[0][1] == 0 and results[1][1] >= 899


@pytest.mark.parametrize("codepoint", [0xD800, 0xDFFF])
def test_lone_surrogate_feed_rejected_without_poisoning_page(browser, monkeypatch, codepoint):
    client, db = browser
    token = login(client, db)
    body = json.dumps(metadata(summary="Bad scalar " + chr(codepoint))).encode("ascii")
    with pytest.raises(ValueError):
        updates.parse_release(body)
    transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        ),
    )
    checked = client.post("/plus/settings/updates/check", data={"csrf_token": token})
    assert checked.status_code == 200
    assert "Update check unavailable" in checked.text
    assert "Bad scalar" not in checked.text
    assert updates.checker.status.release is None
    subsequent = client.get("/plus/settings/updates")
    assert subsequent.status_code == 200
    assert "Update check unavailable" in subsequent.text
    assert "Bad scalar" not in subsequent.text


def test_valid_unicode_scalar_summary_remains_supported():
    summary = "New release " + chr(0x1F363)
    body = json.dumps(metadata(summary=summary)).encode("ascii")
    assert updates.parse_release(body).summary == summary
