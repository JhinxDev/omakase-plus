"""Authenticated language forms with synthetic libraries; no provider access."""

from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlencode

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus import households as core
from omakase.plus.auth import create_session
from omakase.plus.db import get_db as database
from omakase.plus.deps import get_db
from omakase.plus.language_policy import DomainLanguagePolicies, LanguageRule
from omakase.plus.language_preferences import (
    effective_language,
    member_preference,
    set_member_preference,
)
from omakase.plus.language_routes import create_language_router
from omakase.plus.language_store import library_defaults, set_library_defaults
from omakase.plus.web_security import csrf_token

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED", "true")
    monkeypatch.delenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", raising=False)
    blocked = Mock(side_effect=AssertionError("Language settings cannot call providers"))
    monkeypatch.setattr(httpx.Client, "send", blocked)
    db = database(tmp_path)
    for user in (1, 2, 3):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"member{user}@example.test", "synthetic"),
        )
    db.commit()
    library = core.create_household(
        db, user_id=1, name="Synthetic household with a deliberately long name for phone wrapping"
    )
    scope = core.resolve_scope(db, viewer_id=1, library_id=library)
    invitation = core.create_invite(db, scope)
    core.accept_invite(db, viewer_id=2, token=invitation["token"])
    for user in (1, 2):
        core.select_library(db, core.resolve_scope(db, viewer_id=user, library_id=library))
    app = FastAPI()
    templates = Jinja2Templates(directory=str(ROOT / "src/omakase/plus/templates"))
    app.include_router(create_language_router(templates), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: db
    sessions = {user: create_session(db, user) for user in (1, 2, 3)}
    # Starlette TestClient uses its own transport, not the blocked provider client.
    yield app, db, sessions, blocked
    db.close()


def client(web, user=1):
    app, _, sessions, _ = web
    c = TestClient(app, base_url="https://testserver")
    c.cookies.set("omakase_session", sessions[user])
    req = Request(
        {"type": "http", "headers": [(b"cookie", f"omakase_session={sessions[user]}".encode())]}
    )
    return c, csrf_token(req)


def payload(web, user=1, **changes):
    _, db, _, _ = web
    scope = core.selected_scope(db, viewer_id=user)
    data = dict(
        library_id=scope.library_id,
        membership_revision=str(scope.membership_revision),
        policy_revision=str(scope.policy_revision),
        domain="movie",
        target="personal",
        action="save",
        audio_required="fr",
        audio_requirement="any_of",
        subtitle_requirement="any_of",
        subtitles_mode="off",
        subtitle_kind="selectable_required",
    )
    data.update(changes)
    return data


def post(c, token, data):
    return c.post(
        "/plus/settings/languages",
        content=urlencode({"csrf_token": token, **data}, doseq=True),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_get_local_only_independent_household_gate(web):
    c, _ = client(web)
    response = c.get("/plus/settings/languages")
    assert response.status_code == 200
    assert (
        "Language delivery is awaiting" in response.text
        and 'name="audio_required"' in response.text
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert not web[3].called
    assert web[1].execute("SELECT count(*) FROM member_language_preferences").fetchone()[0] == 0


def test_default_disabled_and_private_guard(web, monkeypatch):
    c, token = client(web)
    monkeypatch.delenv("OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED")
    assert c.get("/plus/settings/languages").status_code == 404
    assert post(c, token, payload(web)).status_code == 404
    monkeypatch.setenv("OMAKASE_PLUS_LANGUAGE_SETTINGS_ENABLED", "true")
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert c.get("/plus/settings/languages").status_code == 404


def test_member_owner_isolation_preview_reset_and_no_evidence(web):
    c, token = client(web, 2)
    data = payload(web, 2)
    assert post(c, token, {**data, "action": "preview"}).status_code == 200
    db = web[1]
    assert db.execute("SELECT count(*) FROM member_language_preferences").fetchone()[0] == 0
    assert post(c, token, data).status_code == 200
    member = core.selected_scope(db, viewer_id=2)
    owner = core.selected_scope(db, viewer_id=1)
    assert member_preference(db, member, "movie").audio_required == ("fr",)
    assert member_preference(db, owner, "movie") is None
    assert library_defaults(db, owner)[0].movie.audio_required == ("en",)
    assert effective_language(db, member, "movie").source == "member_preference"
    assert post(c, token, payload(web, 2, action="reset")).status_code == 200
    assert member_preference(db, member, "movie") is None
    for table in (
        "language_request_policies",
        "asset_language_observations",
        "language_evaluations",
        "publication_language_contracts",
        "viewer_selection_observations",
    ):
        assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    assert not web[3].called


def test_owner_defaults_separate_domains_and_member_permissions(web):
    c, token = client(web)
    data = payload(web, target="library", allow_member_preferences="1", allow_request_overrides="1")
    response = post(c, token, data)
    assert response.status_code == 200
    scope = core.selected_scope(web[1], viewer_id=1)
    policies, _, _ = library_defaults(web[1], scope)
    assert policies.movie.audio_required == ("fr",)
    assert policies.anime == DomainLanguagePolicies.legacy_defaults().anime
    assert policies.tv == DomainLanguagePolicies.legacy_defaults().tv
    member, mtoken = client(web, 2)
    assert post(member, mtoken, payload(web, 2, target="library")).status_code == 403
    assert member.get("/plus/settings/languages?target=library").status_code == 403
    assert post(c, token, payload(web, target="library")).status_code == 200
    assert post(member, mtoken, payload(web, 2)).status_code == 403
    assert "owner has disabled" in member.get("/plus/settings/languages").text


@pytest.mark.parametrize(
    "changes",
    [
        {"audio_required": "bogus"},
        {"audio_required": ["en"] * 9},
        {"audio_fallback": "en", "audio_required": "en"},
        {"subtitles_mode": "required"},
        {"subtitles_mode": "off", "subtitle_preferred": "en"},
        {"physical_verified": "1"},
        {"require_audio_stream": "0"},
        {"allow_sdh": "yes"},
        {"allow_member_preferences": "1"},
    ],
)
def test_strict_invalid_forms_never_save(web, changes):
    c, token = client(web, 2)
    assert post(c, token, payload(web, 2, **changes)).status_code == 422
    assert web[1].execute("SELECT count(*) FROM member_language_preferences").fetchone()[0] == 0


def test_csrf_scope_switch_stale_permissions_and_oversize(web):
    c, token = client(web, 2)
    data = payload(web, 2)
    assert post(c, "wrong", data).status_code == 403
    assert (
        post(
            c, token, {**data, "library_id": core.selected_scope(web[1], viewer_id=3).library_id}
        ).status_code
        == 409
    )
    scope = core.selected_scope(web[1], viewer_id=1)
    set_library_defaults(web[1], scope, DomainLanguagePolicies.legacy_defaults())
    assert post(c, token, data).status_code == 409
    assert post(c, token, payload(web, 2, audio_required="x" * 17000)).status_code == 413


def test_saved_preference_rechecks_membership_and_owner_policy(web):
    db = web[1]
    scope = core.selected_scope(db, viewer_id=2)
    set_member_preference(db, scope, "anime", LanguageRule(audio_required=("ja",)))
    owner = core.selected_scope(db, viewer_id=1)
    set_library_defaults(
        db, owner, DomainLanguagePolicies.legacy_defaults(), allow_member_preferences=False
    )
    fresh = core.selected_scope(db, viewer_id=2)
    assert effective_language(db, fresh, "anime").source == "library_default"
    with pytest.raises(core.HouseholdError):
        set_member_preference(db, scope, "anime", LanguageRule())
    with pytest.raises(ValueError):
        set_member_preference(db, fresh, "anime", LanguageRule())


def test_tv_truth_and_unauthenticated_access(web):
    c, _ = client(web)
    response = c.get("/plus/settings/languages?domain=tv")
    assert "catalog and tracking only" in response.text
    assert "automatic TV downloads" in response.text
    guest = TestClient(web[0], base_url="https://testserver", follow_redirects=False)
    assert guest.get("/plus/settings/languages").status_code in (302, 303, 401, 403)


def test_compatible_multi_track_choices_and_reset_only_one_domain(web):
    c, token = client(web)
    data = payload(
        web,
        target="library",
        audio_required=["fr", "en"],
        audio_requirement="all_of",
        allow_member_preferences="1",
        allow_request_overrides="1",
    )
    assert post(c, token, data).status_code == 200
    scope = core.selected_scope(web[1], viewer_id=1)
    assert library_defaults(web[1], scope)[0].movie.audio_required == ("fr", "en")
    assert (
        post(
            c,
            token,
            payload(
                web,
                target="library",
                action="reset",
                allow_member_preferences="1",
                allow_request_overrides="1",
            ),
        ).status_code
        == 200
    )
    scope = core.selected_scope(web[1], viewer_id=1)
    assert (
        library_defaults(web[1], scope)[0].movie == DomainLanguagePolicies.legacy_defaults().movie
    )
    assert not web[3].called


def test_corrupted_preferences_fail_closed_and_variant_names_roundtrip(web):
    db = web[1]
    scope = core.selected_scope(db, viewer_id=2)
    set_member_preference(db, scope, "movie", LanguageRule(audio_required=("fre", "fr-CA")))
    assert member_preference(db, scope, "movie").audio_required == ("fr", "fr-CA")
    db.execute("UPDATE member_language_preferences SET revision='bad' WHERE user_id=2")
    db.commit()
    with pytest.raises(ValueError, match="revision validation"):
        effective_language(db, scope, "movie")
