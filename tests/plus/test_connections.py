"""Per-user connection storage, safe native checks and secret-free receipts."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from omakase.plus import connections
from omakase.plus.db import run_migrations
from omakase.plus.secrets import read_secret, store_secret

NOW = datetime(2026, 10, 27, 18, tzinfo=timezone.utc)
KEY = "synthetic-key-never-render"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "false")
    conn = sqlite3.connect(tmp_path / "connections.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    for user_id in (1, 2):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES (?,?,?)",
            (user_id, f"connection{user_id}@example.invalid", "synthetic"),
        )
    conn.commit()

    async def public_ips(hostname, port):
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr(connections, "_resolved_ips", public_ips)
    yield conn
    conn.close()


def save(db, integration="riven", **kwargs):
    return connections.save_connection(
        db, 1, integration, url="https://service.example.invalid", api_key=KEY, **kwargs
    )


def check(db, integration, handler, *, now=NOW):
    return asyncio.run(
        connections.test_connection(
            db, 1, integration, transport=httpx.MockTransport(handler), now=now
        )
    )


def test_saved_credentials_are_encrypted_user_scoped_and_never_in_cards(db):
    save(db)
    card = connections.connection_cards(db, 1)[2]
    assert card["id"] == "riven"
    assert card["key_saved"] is True
    assert card["url_saved"] is True
    assert KEY not in json.dumps(card)
    assert "service.example.invalid" not in json.dumps(card)
    assert read_secret(db, 1, "riven_api_key") == KEY
    assert connections.connection_cards(db, 2)[2]["key_saved"] is False
    assert KEY not in db.execute("SELECT encrypted_value FROM user_secrets").fetchone()[0]


def test_blank_key_retains_saved_value_but_new_origin_requires_replacement(db):
    save(db)
    connections.save_connection(db, 1, "riven", url="https://service.example.invalid/proxy")
    assert read_secret(db, 1, "riven_api_key") == KEY
    with pytest.raises(connections.ConnectionInputError, match="replacement"):
        connections.save_connection(db, 1, "riven", url="https://other.example.invalid")
    assert read_secret(db, 1, "riven_url") == "https://service.example.invalid/proxy"
    connections.save_connection(
        db, 1, "riven", url="https://other.example.invalid", api_key="replacement"
    )
    assert read_secret(db, 1, "riven_api_key") == "replacement"


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/test",
        "http://user:pass@example.invalid",
        "http://example.invalid?key=abc",
        "http://example.invalid/#fragment",
        "http://169.254.169.254",
        "http://metadata.google.internal",
        "http://[fe80::1]",
        "http://[::ffff:169.254.169.254]",
        "http://example.invalid/a/../b",
        "http://example.invalid/%2e%2e",
        "http://127.0.0.1",
        "http://192.168.1.5",
        "http://example.invalid:0",
        "http://example.invalid:abc",
        "http://example.invalid\\x",
    ],
)
def test_unsafe_service_base_urls_are_rejected_before_storage(db, url):
    with pytest.raises(connections.ConnectionInputError):
        connections.save_connection(db, 1, "plex", url=url, api_key=KEY)
    assert read_secret(db, 1, "plex_token") is None


def test_private_network_requires_existing_explicit_policy_and_metadata_always_blocked(
    db, monkeypatch
):
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")
    connections.save_connection(db, 1, "plex", url="http://192.168.1.5:32400", api_key=KEY)
    assert read_secret(db, 1, "plex_url") == "http://192.168.1.5:32400"
    with pytest.raises(connections.ConnectionInputError):
        connections.save_connection(db, 1, "plex", url="http://169.254.169.254", api_key=KEY)


def test_private_deployment_guard_applies_to_storage_and_test(db, monkeypatch):
    save(db)
    monkeypatch.delenv("OMAKASE_PLUS_PRIVATE")
    with pytest.raises(connections.ConnectionInputError):
        save(db)
    with pytest.raises(connections.ConnectionInputError):
        check(db, "riven", lambda request: pytest.fail("must not connect"))


def test_seerr_uses_existing_overseerr_pair_without_splitting_origins(db):
    store_secret(db, 1, "overseerr_url", "https://old.example.invalid")
    store_secret(db, 1, "overseerr_api_key", KEY)
    connections.save_connection(db, 1, "seerr", api_key="replacement")
    assert read_secret(db, 1, "overseerr_api_key") == "replacement"
    assert read_secret(db, 1, "seerr_api_key") is None
    assert connections.connection_cards(db, 1)[3]["key_saved"]
    connections.disconnect_connection(db, 1, "seerr")
    assert read_secret(db, 1, "overseerr_url") is None


def test_disconnect_only_removes_owned_integration_and_its_health(db):
    save(db)
    save(db, "prowlarr")
    store_secret(db, 1, "prowlarr_categories", "5070,5000")
    store_secret(db, 2, "riven_api_key", "another-user")
    check(db, "riven", lambda request: httpx.Response(200, json={"items": []}))
    connections.disconnect_connection(db, 1, "riven")
    assert read_secret(db, 1, "riven_api_key") is None
    assert read_secret(db, 1, "prowlarr_categories") == "5070,5000"
    assert read_secret(db, 2, "riven_api_key") == "another-user"
    assert connections.connection_cards(db, 1)[2]["health"]["state"] == "disconnected"


@pytest.mark.parametrize(
    ("integration", "path", "response"),
    [
        ("realdebrid", "/rest/1.0/user", {"id": 123, "type": "premium", "email": "private"}),
        ("seerr", "/api/v1/auth/me", {"id": 1, "plexToken": "never-save-this"}),
        ("riven", "/api/v1/items", {"items": []}),
    ],
)
def test_checks_are_read_only_native_authenticated_and_pinned_to_validated_ip(
    db, integration, path, response
):
    if integration == "realdebrid":
        connections.save_connection(db, 1, integration, api_key=KEY)
    else:
        save(db, integration)
    requests = []

    def native(request):
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == path
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] in {"service.example.invalid", "api.real-debrid.com"}
        assert KEY not in str(request.url)
        return httpx.Response(200, json=response)

    result = check(db, integration, native)
    assert result["state"] == "connected"
    assert result["checked_at"] == "2026-10-27T18:00:00Z"
    assert len(requests) == 1
    saved = db.execute("SELECT health_json FROM user_connection_health").fetchone()[0]
    for forbidden in (KEY, "never-save-this", "private", "plexToken"):
        assert forbidden not in saved


def test_plex_check_uses_token_header_and_validates_native_container(db):
    save(db, "plex")

    def native(request):
        assert request.url.path in {"/library/sections", "/identity"}
        assert request.headers["X-Plex-Token"] == KEY
        if request.url.path == "/identity":
            return httpx.Response(200, text='<MediaContainer machineIdentifier="fixture-machine"/>')
        return httpx.Response(
            200, text='<MediaContainer size="1"><Directory key="1" type="movie"/></MediaContainer>'
        )

    assert check(db, "plex", native)["state"] == "connected"


def test_prowlarr_connection_health_does_not_hide_individual_indexer_cooldowns(db):
    save(db, "prowlarr")
    store_secret(db, 1, "prowlarr_categories", "5070,5000")
    seen = []

    def native(request):
        seen.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["X-Api-Key"] == KEY
        payload = {
            "/api/v1/system/status": {"version": "synthetic"},
            "/api/v1/indexer": [
                {"id": 1, "name": "SubsPlease", "enable": True, "fields": [{"value": KEY}]},
                {"id": 2, "name": "Tokyo Toshokan", "enable": True},
            ],
            "/api/v1/indexerstatus": [{"indexerId": 2, "disabledTill": "2026-10-27T19:54:00Z"}],
        }[request.url.path]
        return httpx.Response(200, json=payload)

    result = check(db, "prowlarr", native)
    assert result["state"] == "degraded"
    assert result["code"] == "indexer_cooldowns"
    assert result["connected"] is True
    assert result["indexers"] == [
        {"id": 1, "name": "SubsPlease", "state": "available", "disabled_till": None},
        {
            "id": 2,
            "name": "Tokyo Toshokan",
            "state": "cooldown",
            "disabled_till": "2026-10-27T19:54:00Z",
        },
    ]
    assert len(seen) == 3
    assert KEY not in json.dumps(result)
    assert read_secret(db, 1, "prowlarr_categories") == "5070,5000"


def test_provider_retry_after_is_persisted_and_blocks_repeat_network_test(db):
    save(db)
    calls = []

    def native(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "120"}, json={"error": KEY})

    first = check(db, "riven", native)
    second = check(db, "riven", native, now=NOW + timedelta(seconds=30))
    assert first == second
    assert first["state"] == "rate_limited"
    assert first["retry_at"] == "2026-10-27T18:02:00Z"
    assert first["retry_after"] == 120
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (302, {}, "redirect_rejected"),
        (401, {}, "authentication_failed"),
        (503, {}, "service_unavailable"),
        (200, {"api_key": KEY}, "invalid_response"),
    ],
)
def test_failures_are_sanitized_and_redirects_are_not_followed(db, status, body, code):
    save(db)
    seen = []

    def native(request):
        seen.append(request)
        return httpx.Response(status, json=body, headers={"Location": "http://169.254.169.254"})

    result = check(db, "riven", native)
    assert result["code"] == code
    assert result["state"] == "error"
    assert len(seen) == 1
    assert KEY not in json.dumps(result)


def test_dns_private_or_metadata_addresses_are_rejected_before_network(db, monkeypatch):
    save(db)

    async def metadata_ips(hostname, port):
        return (ipaddress.ip_address("169.254.169.254"),)

    monkeypatch.setattr(connections, "_resolved_ips", metadata_ips)
    result = check(db, "riven", lambda request: pytest.fail("unsafe DNS must not connect"))
    assert result["code"] == "unsafe_destination"


def test_response_read_is_bounded(db):
    save(db)
    result = check(db, "riven", lambda request: httpx.Response(200, content=b"x" * (262144 + 1)))
    assert result["code"] == "response_too_large"


@pytest.mark.parametrize("url", ["http://100.100.100.200", "http://[fd00:ec2::254]"])
def test_cloud_metadata_is_blocked_even_when_lan_is_explicitly_allowed(db, monkeypatch, url):
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")
    with pytest.raises(connections.ConnectionInputError):
        connections.save_connection(db, 1, "riven", url=url, api_key=KEY)


def test_long_retry_after_is_never_shortened(db):
    save(db)
    result = check(
        db, "riven", lambda request: httpx.Response(429, headers={"Retry-After": "1209600"})
    )
    assert result["retry_after"] == 1209600
    assert result["retry_at"] == "2026-11-10T18:00:00Z"


def test_retry_after_never_resumes_early_when_clock_has_subsecond_precision(db):
    save(db)
    now = NOW + timedelta(microseconds=900000)
    result = check(
        db, "riven", lambda request: httpx.Response(429, headers={"Retry-After": "1"}), now=now
    )
    allowed = datetime.fromisoformat(result["retry_at"].replace("Z", "+00:00"))
    assert allowed >= now + timedelta(seconds=1)


def test_imported_seerr_base_with_api_suffix_can_enable_after_passing_checks(db):
    for integration in ("riven", "plex", "seerr"):
        save(db, integration)
    store_secret(db, 1, "seerr_url", "https://service.example.invalid/api/v1")
    for integration in ("riven", "plex", "seerr"):
        check(
            db,
            integration,
            lambda request: (
                httpx.Response(200, text='<MediaContainer size="0"/>')
                if request.url.path == "/library/sections"
                else httpx.Response(
                    200, text='<MediaContainer machineIdentifier="fixture-machine"/>'
                )
                if request.url.path == "/identity"
                else httpx.Response(200, json={"items": [], "id": 1})
            ),
        )
    assert connections.movie_connections(db, 1).enabled is True


def test_invalid_indexer_cooldown_is_unknown_instead_of_available(db):
    save(db, "prowlarr")

    def native(request):
        payload = {
            "/api/v1/system/status": {"version": "synthetic"},
            "/api/v1/indexer": [{"id": 1, "name": "Tokyo Toshokan", "enable": True}],
            "/api/v1/indexerstatus": [{"indexerId": 1, "disabledTill": "malformed"}],
        }[request.url.path]
        return httpx.Response(200, json=payload)

    result = check(db, "prowlarr", native)
    assert result["state"] == "degraded"
    assert result["connected"] is True
    assert result["code"] == "indexer_status_unavailable"
    assert result["indexers"] == []


def test_optional_plex_connection_identity_is_retained_for_movie_backend(db):
    store_secret(db, 1, "plex_movies_section_id", "7")
    store_secret(db, 1, "plex_machine_id", "synthetic-machine")
    configured = connections.movie_connections(db, 1)
    assert configured.plex_movies_section_id == 7
    assert configured.plex_machine_id == "synthetic-machine"


@pytest.mark.parametrize("action", ["replace_origin", "disconnect"])
def test_plex_identity_cannot_survive_a_different_server_or_disconnect(db, action):
    save(db, "plex")
    store_secret(db, 1, "plex_movies_section_id", "7")
    store_secret(db, 1, "plex_machine_id", "old-machine")
    if action == "replace_origin":
        connections.save_connection(
            db, 1, "plex", url="https://new.example.invalid", api_key="replacement"
        )
    else:
        connections.disconnect_connection(db, 1, "plex")
    assert read_secret(db, 1, "plex_movies_section_id") is None
    assert read_secret(db, 1, "plex_machine_id") is None


def test_movie_connections_require_own_tested_pairs_and_invalidate_after_edit(db):
    assert connections.movie_connections(db, 1).enabled is False
    for integration in ("riven", "seerr", "plex"):
        save(db, integration)
        response = {"items": []} if integration == "riven" else {"id": 1}
        check(
            db,
            integration,
            lambda request: (
                httpx.Response(200, text='<MediaContainer size="0"/>')
                if request.url.path == "/library/sections"
                else httpx.Response(
                    200, text='<MediaContainer machineIdentifier="fixture-machine"/>'
                )
                if request.url.path == "/identity"
                else httpx.Response(200, json=response)
            ),
        )
    configured = connections.movie_connections(db, 1)
    assert configured.enabled is True
    assert configured.riven_api_key == KEY
    assert configured.seerr_url == "https://service.example.invalid"
    assert KEY not in repr(configured)
    assert connections.movie_connections(db, 2).enabled is False
    connections.save_connection(db, 1, "riven", api_key="replacement")
    assert connections.movie_connections(db, 1).enabled is False


def test_loopback_requires_its_separate_trusted_permission(db, monkeypatch):
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")
    monkeypatch.delenv("OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS", raising=False)
    with pytest.raises(connections.ConnectionInputError):
        connections.save_connection(db, 1, "riven", url="http://127.0.0.1:8080", api_key=KEY)
    monkeypatch.setenv("OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS", "true")
    connections.save_connection(db, 1, "riven", url="http://127.0.0.1:8080", api_key=KEY)

    def native(request):
        assert request.url.host == "127.0.0.1"
        assert request.headers["Host"] == "127.0.0.1:8080"
        return httpx.Response(200, json={"items": []})

    assert check(db, "riven", native)["state"] == "connected"


def test_shared_runtime_policy_keeps_metadata_blocked_under_all_private_permissions(
    db, monkeypatch
):
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")
    monkeypatch.setenv("OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS", "true")
    save(db)

    async def rebound(host, port):
        return (ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("169.254.169.254"))

    monkeypatch.setattr(connections, "_resolved_ips", rebound)
    result = check(db, "riven", lambda request: pytest.fail("mixed metadata DNS must not connect"))
    assert result["code"] == "unsafe_destination"


def test_plex_health_rejects_utf16_entity_declarations(db):
    save(db, "plex")
    xml = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY item "hidden">]><MediaContainer title="&item;"/>'.encode(
        "utf-16"
    )
    result = check(db, "plex", lambda request: httpx.Response(200, content=xml))
    assert result["code"] == "invalid_response"
