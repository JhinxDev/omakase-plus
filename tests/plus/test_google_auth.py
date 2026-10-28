"""Synthetic Google protocol and browser account-boundary regressions."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus import google_auth as ga
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.web_security import csrf_token

CONFIG = ga.GoogleConfig(
    "synthetic-client", "synthetic-secret", "https://testserver/plus/auth/google/callback"
)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    for user_id in (1, 2):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES (?,?,?)",
            (user_id, f"synthetic{user_id}@example.invalid", "synthetic-hash"),
        )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(ga, "google_config", lambda: CONFIG)
    ga._starts.clear()
    app = FastAPI()
    from omakase.web.server import plus_security_headers

    app.middleware("http")(plus_security_headers)
    templates = Jinja2Templates(directory=str(Path(ga.__file__).parent / "templates"))
    templates.env.globals["asset_version"] = "test"

    def session_cookie(response, value):
        response.set_cookie(
            "omakase_session", value, secure=True, httponly=True, samesite="lax", path="/plus"
        )

    app.include_router(ga.create_google_router(templates, session_cookie), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as result:
        yield result


@pytest.fixture(scope="module")
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def signed(key, **overrides):
    now = int(time.time())
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CONFIG.client_id,
        "sub": "synthetic-sub",
        "nonce": "synthetic-nonce",
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    header = ga._b64(json.dumps({"alg": "RS256", "kid": "synthetic-key"}).encode())
    payload = ga._b64(json.dumps(claims).encode())
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    numbers = key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kid": "synthetic-key",
                "kty": "RSA",
                "alg": "RS256",
                "n": ga._b64(numbers.n.to_bytes(256, "big")),
                "e": ga._b64(numbers.e.to_bytes(3, "big")),
            }
        ]
    }
    return f"{header}.{payload}.{ga._b64(signature)}", jwks


def csrf(session):
    return csrf_token(
        Request({"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]})
    )


def start(client):
    response = client.get("/plus/auth/google/login")
    assert response.status_code == 303
    return parse_qs(urlsplit(response.headers["location"]).query)


async def identity(*args):
    return "synthetic-sub"


@pytest.mark.parametrize("linked", [False, True])
def test_signed_in_google_login_returns_home_without_starting_flow(client, db, monkeypatch, linked):
    if linked:
        ga.link_identity(db, 1, "synthetic-sub")
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")

    def unexpected_oauth_configuration():
        pytest.fail("An existing session must not start a new Google flow")

    monkeypatch.setattr(ga, "google_config", unexpected_oauth_configuration)
    response = client.get("/plus/auth/google/login?next=https://example.invalid/")

    assert response.status_code == 303
    assert response.headers["location"] == "/plus/home"
    assert "set-cookie" not in response.headers
    assert db.execute("SELECT count(*) FROM google_login_flows").fetchone()[0] == 0


def test_signed_identity_and_no_email_dependency(key):
    token, jwks = signed(key, email="someone@example.invalid", email_verified=False)
    assert ga.verify_id_token(token, jwks, CONFIG, "synthetic-nonce") == "synthetic-sub"


def test_production_csp_allows_only_fixed_google_form_redirect_paths(client, db):
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    account = client.get("/plus/auth/google/account")
    expected = "form-action 'self' https://accounts.google.com;"
    assert expected in account.headers["content-security-policy"]
    assert "script-src 'self';" in account.headers["content-security-policy"]
    linked = client.post("/plus/auth/google/link", data={"csrf_token": csrf(session)})
    assert linked.status_code == 303
    assert linked.headers["location"].startswith(ga.AUTH_URL + "?")
    assert expected in linked.headers["content-security-policy"]
    for path in (
        "/plus/auth/google/login",
        "/plus/auth/google/callback",
        "/plus/discover",
        "/plus/auth/google/account/",
        "/plus/auth/google/link-extra",
    ):
        response = client.get(path)
        assert "form-action 'self';" in response.headers["content-security-policy"]
        assert "https://accounts.google.com" not in response.headers["content-security-policy"]


def test_production_csp_disabled_configuration_stays_self_only(client, db, monkeypatch):
    monkeypatch.setattr(ga, "google_config", lambda: None)
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    for response in (
        client.get("/plus/auth/google/account"),
        client.post("/plus/auth/google/link", data={"csrf_token": csrf(session)}),
    ):
        assert "form-action 'self';" in response.headers["content-security-policy"]
        assert "https://accounts.google.com" not in response.headers["content-security-policy"]


def test_google_form_origin_not_derived_from_callback_or_forwarded_host(client, db, monkeypatch):
    monkeypatch.setattr(
        ga,
        "google_config",
        lambda: ga.GoogleConfig(
            CONFIG.client_id,
            CONFIG.client_secret,
            "https://another.example.invalid/plus/auth/google/callback",
        ),
    )
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    response = client.get(
        "/plus/auth/google/account", headers={"x-forwarded-host": "evil.example.invalid"}
    )
    policy = response.headers["content-security-policy"]
    assert "form-action 'self' https://accounts.google.com;" in policy
    assert "another.example.invalid" not in policy and "evil.example.invalid" not in policy


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-client"},
        {"azp": "another-client"},
        {"aud": [CONFIG.client_id, "another-client"]},
        {"iss": "https://evil.invalid"},
        {"nonce": "another-nonce"},
        {"exp": 1},
        {"exp": True},
        {"iat": int(time.time()) + 3600},
        {"iat": 1},
        {"sub": ""},
    ],
)
def test_rejects_invalid_binding_claims(key, claims):
    token, jwks = signed(key, **claims)
    with pytest.raises(ga.GoogleAuthError):
        ga.verify_id_token(token, jwks, CONFIG, "synthetic-nonce")


def test_rejects_signature_unknown_key_and_malformed_token(key):
    token, jwks = signed(key)
    for bad in ("invalid", token[:-8] + "ABCDEFGH"):
        with pytest.raises(ga.GoogleAuthError):
            ga.verify_id_token(bad, jwks, CONFIG, "synthetic-nonce")
    jwks["keys"][0]["kid"] = "another-key"
    with pytest.raises(ga.GoogleAuthError):
        ga.verify_id_token(token, jwks, CONFIG, "synthetic-nonce")


def test_flow_browser_binding_expiry_replay_and_pkce(db):
    state, nonce, challenge = ga.create_flow(
        db, CONFIG, purpose="login", browser="browser", now=100
    )
    raw = db.execute("SELECT * FROM google_login_flows").fetchone()
    assert raw["state_hash"] != state and raw["browser_hash"] != "browser"
    assert challenge == ga._b64(ga.hashlib.sha256(raw["code_verifier"].encode()).digest())
    assert ga.consume_flow(db, CONFIG, state=state, browser="wrong", now=101) is None
    assert ga.consume_flow(db, CONFIG, state=state, browser="browser", now=101)["nonce"] == nonce
    assert ga.consume_flow(db, CONFIG, state=state, browser="browser", now=101) is None
    state, _, _ = ga.create_flow(db, CONFIG, purpose="login", browser="browser", now=100)
    assert ga.consume_flow(db, CONFIG, state=state, browser="browser", now=400) is None
    assert db.execute("SELECT count(*) FROM google_login_flows").fetchone()[0] == 0


def test_link_flow_session_owner_and_config_binding(db):
    for user, session in ((2, "owner-session"), (1, "other-session")):
        state, _, _ = ga.create_flow(
            db, CONFIG, purpose="link", browser="browser", user_id=1, session="owner-session"
        )
        assert (
            ga.consume_flow(
                db, CONFIG, state=state, browser="browser", user_id=user, session=session
            )
            is None
        )
    state, _, _ = ga.create_flow(db, CONFIG, purpose="login", browser="browser")
    changed = ga.GoogleConfig("changed", CONFIG.client_secret, CONFIG.redirect_uri)
    assert ga.consume_flow(db, changed, state=state, browser="browser") is None


def test_no_identity_reassignment(db):
    ga.link_identity(db, 1, "subject-a")
    ga.link_identity(db, 1, "subject-a")
    for user, subject in ((2, "subject-a"), (1, "subject-b")):
        with pytest.raises(ga.GoogleAuthError):
            ga.link_identity(db, user, subject)
    assert db.execute("SELECT count(*) FROM google_identities").fetchone()[0] == 1


def test_unlinked_google_never_creates_or_email_merges_account(client, db, monkeypatch):
    monkeypatch.setattr(ga, "exchange_identity", identity)
    query = start(client)
    response = client.get(
        "/plus/auth/google/callback", params={"state": query["state"][0], "code": "synthetic-code"}
    )
    assert "Log+in+with+your+password+once" in response.headers["location"]
    assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_link_then_login_and_replay(client, db, monkeypatch):
    monkeypatch.setattr(ga, "exchange_identity", identity)
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    response = client.post("/plus/auth/google/link", data={"csrf_token": csrf(session)})
    query = parse_qs(urlsplit(response.headers["location"]).query)
    response = client.get(
        "/plus/auth/google/callback", params={"state": query["state"][0], "code": "synthetic-code"}
    )
    assert response.headers["location"].endswith("result=linked")
    client.cookies.clear()
    query = start(client)
    params = {"state": query["state"][0], "code": "synthetic-code"}
    response = client.get("/plus/auth/google/callback", params=params)
    assert response.headers["location"] == "/plus/home"
    assert (
        "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    )
    assert response.headers["cache-control"] == "no-store"
    before = db.execute("SELECT count(*) FROM sessions").fetchone()[0]
    response = client.get("/plus/auth/google/callback", params=params)
    assert "could+not+be+verified" in response.headers["location"]
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == before


def test_link_requires_authenticated_session_and_explicit_csrf(client, db):
    assert client.post("/plus/auth/google/link", data={"csrf_token": "invalid"}).status_code == 401
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    assert client.post("/plus/auth/google/link", data={"csrf_token": "invalid"}).status_code == 403
    assert (
        client.post(
            "/plus/auth/google/link",
            content="csrf_token=" + "a" * 5000,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )


def test_unlink_only_owner_and_cancels_pending_link(client, db):
    session = create_session(db, 1)
    ga.link_identity(db, 1, "subject-a")
    ga.link_identity(db, 2, "subject-b")
    ga.create_flow(db, CONFIG, purpose="link", browser="browser", user_id=1, session=session)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    response = client.post("/plus/auth/google/unlink", data={"csrf_token": csrf(session)})
    assert response.status_code == 303
    assert [r[0] for r in db.execute("SELECT user_id FROM google_identities")] == [2]
    assert db.execute("SELECT count(*) FROM google_login_flows").fetchone()[0] == 0
    assert (
        db.execute("SELECT password_hash FROM users WHERE id=1").fetchone()[0] == "synthetic-hash"
    )


def test_callback_provider_failure_is_safe_and_consumes_flow(client, db, monkeypatch):
    async def fail(*args):
        raise ga.GoogleAuthError("synthetic-secret must never be shown")

    monkeypatch.setattr(ga, "exchange_identity", fail)
    query = start(client)
    response = client.get(
        "/plus/auth/google/callback", params={"state": query["state"][0], "code": "synthetic-code"}
    )
    assert "synthetic-secret" not in str(response.headers) + response.text
    assert db.execute("SELECT count(*) FROM google_login_flows").fetchone()[0] == 0


def test_callback_rejects_duplicate_parameters(client):
    assert client.get("/plus/auth/google/callback?state=a&state=b&code=c").status_code == 400


def test_rate_limit_and_configured_callback_ignores_host(client):
    for _ in range(5):
        response = client.get(
            "/plus/auth/google/login",
            headers={"host": "evil.invalid", "x-forwarded-host": "evil.invalid"},
        )
        query = parse_qs(urlsplit(response.headers["location"]).query)
        assert query["redirect_uri"] == [CONFIG.redirect_uri]
    assert client.get("/plus/auth/google/login").status_code == 429


@pytest.mark.parametrize(
    "uri",
    [
        "http://testserver/plus/auth/google/callback",
        "https://user:pass@testserver/plus/auth/google/callback",
        "https://testserver/plus/auth/google/callback?next=evil",
        "https://[broken",
    ],
)
def test_configuration_requires_exact_https_callback(monkeypatch, uri):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", uri)
    assert ga.google_config() is None


def test_bounded_provider_response():
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * (ga.MAX_PROVIDER_BYTES + 1))
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ga.GoogleAuthError, match="too large"):
                await ga._provider_json(client, "GET", ga.JWKS_URL)

    asyncio.run(run())


def test_concurrent_callback_consumes_once(db):
    state, _, _ = ga.create_flow(db, CONFIG, purpose="login", browser="browser")
    path = db.execute("PRAGMA database_list").fetchone()[2]
    barrier = threading.Barrier(2)

    def consume():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        try:
            barrier.wait()
            return ga.consume_flow(connection, CONFIG, state=state, browser="browser") is not None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))
    assert sorted(results) == [False, True]


def test_logout_during_google_exchange_cannot_link(client, db, monkeypatch):
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    response = client.post("/plus/auth/google/link", data={"csrf_token": csrf(session)})
    query = parse_qs(urlsplit(response.headers["location"]).query)

    async def logged_out(*args):
        ga.delete_session(db, session)
        return "synthetic-sub"

    monkeypatch.setattr(ga, "exchange_identity", logged_out)
    response = client.get(
        "/plus/auth/google/callback", params={"state": query["state"][0], "code": "synthetic-code"}
    )
    assert response.headers["location"].endswith("result=failed")
    assert db.execute("SELECT count(*) FROM google_identities").fetchone()[0] == 0


def test_callback_timeout_consumes_flow_and_hides_details(client, db, monkeypatch):
    async def timeout(*args):
        raise asyncio.TimeoutError("synthetic-secret")

    monkeypatch.setattr(ga, "exchange_identity", timeout)
    query = start(client)
    response = client.get(
        "/plus/auth/google/callback", params={"state": query["state"][0], "code": "synthetic-code"}
    )
    assert "could+not+be+verified" in response.headers["location"]
    assert "synthetic-secret" not in str(response.headers) + response.text
    assert db.execute("SELECT count(*) FROM google_login_flows").fetchone()[0] == 0


def test_disabled_provider_keeps_password_route_and_account_page(client, db, monkeypatch):
    monkeypatch.setattr(ga, "google_config", lambda: None)
    assert "Use+your+password" in client.get("/plus/auth/google/login").headers["location"]
    session = create_session(db, 1)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    response = client.get("/plus/auth/google/account")
    assert response.status_code == 200 and "not configured" in response.text
    assert "Link Google account</button>" not in response.text


def test_expiry_during_exchange_cannot_create_session(client, db, monkeypatch):
    ga.link_identity(db, 1, "synthetic-sub")
    query = start(client)

    async def late(*args):
        # This represents an exchange finishing after the original five-minute deadline.
        monkeypatch.setattr(ga.time, "time", lambda: args[2]["expires_at"])
        return "synthetic-sub"

    monkeypatch.setattr(ga, "exchange_identity", late)
    response = client.get(
        "/plus/auth/google/callback", params={"state": query["state"][0], "code": "synthetic-code"}
    )
    assert "could+not+be+verified" in response.headers["location"]
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
