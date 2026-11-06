"""Private-mode self-registration is closed after the first account exists.

Public BYOK/Lite deployments keep the historical contract (signup only when
``OMAKASE_PLUS_INVITE`` is set). Private Plus deployments keep the documented
first-run bootstrap, then require the operator to set ``OMAKASE_PLUS_INVITE``
instead of accepting registrations from anyone who can reach the origin.
"""

from __future__ import annotations

from tests.plus.test_auth import client as client


def _signup(http_client, email: str):
    return http_client.post(
        "/plus/signup",
        data={
            "email": email,
            "password": "secret123",
            "confirm_password": "secret123",
        },
        follow_redirects=False,
    )


def test_private_mode_allows_first_run_bootstrap(client, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.delenv("OMAKASE_PLUS_INVITE", raising=False)

    page = client.get("/plus/signup", follow_redirects=False)
    assert page.status_code == 200
    assert "Create" in page.text or "<form" in page.text

    created = _signup(client, "first-owner@example.com")
    assert created.status_code == 302
    assert created.headers["location"] == "/plus/onboarding"


def test_private_mode_closes_self_registration_after_first_account(client, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.delenv("OMAKASE_PLUS_INVITE", raising=False)
    assert _signup(client, "owner@example.com").status_code == 302

    page = client.get("/plus/signup", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/plus/login"

    stranger = _signup(client, "stranger@example.com")
    assert stranger.status_code == 302
    assert stranger.headers["location"] == "/plus/login"


def test_private_mode_invite_keeps_registration_available(client, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_INVITE", "launch-invite")

    first = _signup(client, "owner@example.com")
    assert first.status_code == 302
    invited = _signup(client, "invited@example.com")
    assert invited.status_code == 302
    assert invited.headers["location"] == "/plus/onboarding"


def test_public_mode_registration_gate_is_unchanged(client, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    monkeypatch.delenv("OMAKASE_PLUS_INVITE", raising=False)

    page = client.get("/plus/signup", follow_redirects=False)
    assert page.status_code == 302
    assert page.headers["location"] == "/plus/login"
    rejected = _signup(client, "nobody@example.com")
    assert rejected.status_code == 302
    assert rejected.headers["location"] == "/plus/login"
