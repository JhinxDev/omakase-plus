"""Real authenticated household routes for owner, accepted member and outsider."""

from __future__ import annotations

import argparse
import sqlite3
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus import households as core
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.household_routes import create_household_router
from omakase.plus.web_security import csrf_token

ROOT = Path(__file__).resolve().parents[2] / "src/omakase/plus"


class InputValues(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.values = {}
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "input" and "id" in values:
            self.values[values["id"]] = values.get("value", "")


def fixture_app():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for user in (1, 2, 3):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"person{user}@example.invalid", "synthetic"),
        )
        core.ensure_personal_library(db, user_id=user)
    db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(1,100,'A very long real inventory title with a season and subtitle that must wrap safely on phones',1)"
    )
    db.commit()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.globals["asset_version"] = "household-fixture"
    app.include_router(
        create_household_router(
            templates,
            lambda request, user, **context: {
                "email": user.email,
                "active_page": "settings",
                "synthetic_review": True,
            },
            enabled=True,
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    sessions = {user: create_session(db, user) for user in (1, 2, 3)}
    return app, db, sessions


def client_for(app, session):
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    client.cookies.set("omakase_session", session)
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]}
    )
    return client, csrf_token(request)


@pytest.fixture
def fixture(monkeypatch):
    from unittest.mock import AsyncMock

    import httpx

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "send",
        AsyncMock(side_effect=AssertionError("Household core must not call providers")),
    )
    app, db, sessions = fixture_app()
    yield app, db, sessions
    db.close()


def create(client, token, *, promote=False, digest=""):
    return client.post(
        "/plus/settings/household/create",
        data={
            "csrf_token": token,
            "name": "Shared Home",
            "starting_library": "promote" if promote else "empty",
            "preview_digest": digest,
            "members_can_add": "1",
        },
    )


def test_viewer_can_request_exact_title_and_only_owner_can_decide(fixture, monkeypatch):
    from omakase.plus.notifications_routes import create_notifications_router

    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    app, db, sessions = fixture
    app.include_router(create_notifications_router(), prefix="/plus")
    owner, owner_token = client_for(app, sessions[1])
    member, member_token = client_for(app, sessions[2])
    outsider, outsider_token = client_for(app, sessions[3])
    library = core.create_household(db, user_id=1, name="Cedar House", members_can_add=False)

    def owner_scope():
        return core.resolve_scope(db, viewer_id=1, library_id=library)

    code = core.create_invite(db, owner_scope())["token"]
    assert (
        member.post(
            "/plus/households/join",
            data={
                "csrf_token": member_token,
                "invite_token": code,
                "preview_library_id": library,
                "display_name": "Alex",
            },
        ).status_code
        == 303
    )
    db.executemany(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(2,?,?,CURRENT_TIMESTAMP)",
        [(42, '{"tmdb_id":42,"title":"Night Train"}'), (43, '{"tmdb_id":43,"title":"Day Train"}')],
    )
    db.commit()
    settings = f"/plus/settings/household/{library}"
    assert "Alex" in owner.get("/plus/settings/household").text
    form = {"csrf_token": member_token, "domain": "tv", "canonical_id": "42"}
    catalog = f"/plus/libraries/{library}/catalog"
    assert member.post(f"{catalog}/add", data=form).status_code == 403
    assert member.post(f"{catalog}/request", data={**form, "canonical_id": "44"}).status_code == 409
    assert member.post(f"{catalog}/request", data=form).status_code == 303
    assert member.post(f"{catalog}/request", data=form).status_code == 303
    assert not core.LibraryRepository(db, owner_scope()).items()
    assert db.execute("SELECT count(*) FROM library_request_events").fetchone()[0] == 0
    feed = owner.get("/plus/api/notifications").json()
    assert len(feed["items"]) == 1
    notice = feed["items"][0]
    assert notice["kind"] == "household_request" and notice["title"] == "Night Train"
    assert "Alex" in notice["message"]
    assert member.get("/plus/api/notifications").json()["items"] == []
    assert outsider.get("/plus/api/notifications").json()["items"] == []
    assert (
        outsider.post(
            f"/plus/api/notifications/{notice['id']}/read", headers={"X-CSRF-Token": outsider_token}
        ).status_code
        == 404
    )
    marked = owner.post(
        f"/plus/api/notifications/{notice['id']}/read", headers={"X-CSRF-Token": owner_token}
    )
    assert marked.status_code == 200 and marked.json()["items"][0]["read"]
    request_id = notice["id"].rsplit(":", 1)[1]
    decision = f"{settings}/requests/{request_id}/decision"
    assert (
        member.post(decision, data={"csrf_token": member_token, "decision": "approve"}).status_code
        == 403
    )
    assert (
        outsider.post(
            decision, data={"csrf_token": outsider_token, "decision": "approve"}
        ).status_code
        == 404
    )
    assert (
        owner.post(decision, data={"csrf_token": owner_token, "decision": "approve"}).status_code
        == 303
    )
    assert (
        owner.post(decision, data={"csrf_token": owner_token, "decision": "approve"}).status_code
        == 303
    )
    assert [item["title"] for item in core.LibraryRepository(db, owner_scope()).items()] == [
        "Night Train"
    ]
    assert owner.get("/plus/api/notifications").json()["items"] == []
    member_page = member.get("/plus/settings/household").text
    assert "Night Train" in member_page and "Approved" in member_page

    assert member.post(f"{catalog}/request", data={**form, "canonical_id": "43"}).status_code == 303
    next_notice = owner.get("/plus/api/notifications").json()["items"][0]
    next_id = next_notice["id"].rsplit(":", 1)[1]
    assert (
        owner.post(
            f"{settings}/requests/{next_id}/decision",
            data={"csrf_token": owner_token, "decision": "decline"},
        ).status_code
        == 303
    )
    assert "Declined" in member.get("/plus/settings/household").text
    assert [item["title"] for item in core.LibraryRepository(db, owner_scope()).items()] == [
        "Night Train"
    ]

    permissions = f"{settings}/members/2/permissions"
    invite = core.create_invite(db, owner_scope())["token"]
    core.accept_invite(db, viewer_id=3, token=invite, display_name="Morgan")
    assert (
        member.post(permissions, data={"csrf_token": member_token, "can_add": "1"}).status_code
        == 403
    )
    assert (
        owner.post(permissions, data={"csrf_token": owner_token, "can_add": "1"}).status_code == 303
    )
    assert member.post(f"{catalog}/request", data={**form, "canonical_id": "43"}).status_code == 403
    other_scope = core.resolve_scope(db, viewer_id=3, library_id=library)
    assert other_scope.can_request_titles and not other_scope.members_can_add
    assert member.post(f"{catalog}/add", data={**form, "canonical_id": "43"}).status_code == 303
    assert (
        owner.post(f"{settings}/members/2/revoke", data={"csrf_token": owner_token}).status_code
        == 303
    )
    assert member.post(f"{catalog}/add", data=form).status_code == 404

    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(3,45,?,CURRENT_TIMESTAMP)",
        ('{"tmdb_id":45,"title":"Coast Road"}',),
    )
    db.commit()
    assert (
        outsider.post(
            f"{catalog}/request",
            data={"csrf_token": outsider_token, "domain": "tv", "canonical_id": "45"},
        ).status_code
        == 303
    )
    pending = owner.get("/plus/api/notifications").json()["items"][0]["id"].rsplit(":", 1)[1]
    assert (
        owner.post(f"{settings}/members/3/revoke", data={"csrf_token": owner_token}).status_code
        == 303
    )
    assert owner.get("/plus/api/notifications").json()["items"] == []
    assert (
        owner.post(
            f"{settings}/requests/{pending}/decision",
            data={"csrf_token": owner_token, "decision": "approve"},
        ).status_code
        == 409
    )
    assert "Coast Road" not in [
        item["title"] for item in core.LibraryRepository(db, owner_scope()).items()
    ]


def test_real_three_account_invite_join_private_status_and_immediate_revocation(fixture):
    app, db, sessions = fixture
    owner, token = client_for(app, sessions[1])
    member, member_csrf = client_for(app, sessions[2])
    outsider, other_csrf = client_for(app, sessions[3])
    before_sessions = [tuple(r) for r in db.execute("SELECT * FROM sessions ORDER BY id")]
    assert create(owner, token).status_code == 303
    library = core.overview(db, viewer_id=1)["household"]["id"]
    invited = owner.post(f"/plus/settings/household/{library}/invite", data={"csrf_token": token})
    assert invited.status_code == 200
    assert (
        "private, no-store" in invited.headers["cache-control"]
        and invited.headers["referrer-policy"] == "no-referrer"
    )
    code = InputValues(invited.text).values["created-invite-code"]
    assert len(code) == 43
    assert code not in owner.get("/plus/settings/household").text and code not in "\n".join(
        db.iterdump()
    )
    assert (
        outsider.post(
            f"/plus/settings/household/{library}/invite", data={"csrf_token": other_csrf}
        ).status_code
        == 404
    )
    assert "Shared Home" not in outsider.get("/plus/settings/household").text
    assert (
        member.post(
            "/plus/households/join",
            data={"csrf_token": member_csrf, "invite_token": code, "preview_library_id": library},
        ).status_code
        == 303
    )
    assert core.selected_scope(db, viewer_id=2).library_id == library
    page = member.get("/plus/settings/household")
    assert page.status_code == 200
    assert "Shared Home" in page.text and "Create invitation" not in page.text
    assert "person1@example.invalid" not in page.text and "person3@example.invalid" not in page.text
    assert (
        member.post(
            f"/plus/settings/household/{library}/policy",
            data={"csrf_token": member_csrf, "members_can_add": "1"},
        ).status_code
        == 403
    )
    db.execute("INSERT INTO watch_entries(user_id,anilist_id,season_number) VALUES(2,100,1)")
    db.commit()
    assert (
        owner.post(
            f"/plus/settings/household/{library}/members/2/revoke", data={"csrf_token": token}
        ).status_code
        == 303
    )
    assert core.overview(db, viewer_id=2) == {"household": None}
    assert core.selected_scope(db, viewer_id=2).kind == "personal"
    assert len(core.personal_anime_history(db, viewer_id=2)) == 1
    assert (
        member.post(
            f"/plus/settings/household/{library}/invite", data={"csrf_token": member_csrf}
        ).status_code
        == 404
    )
    assert before_sessions == [tuple(r) for r in db.execute("SELECT * FROM sessions ORDER BY id")]


def test_real_auth_csrf_body_bounds_and_no_get_mutation(fixture):
    app, db, sessions = fixture
    owner, token = client_for(app, sessions[1])
    anonymous = TestClient(app)
    assert anonymous.get("/plus/settings/household").status_code == 401
    changes = db.total_changes
    preview = owner.get("/plus/settings/household/create")
    assert preview.status_code == 200 and "A very long real inventory title" in preview.text
    assert db.total_changes == changes
    missing = create(owner, "")
    assert missing.status_code == 403 and "no-store" in missing.headers["cache-control"]
    assert (
        owner.post(
            "/plus/settings/household/create", data={"csrf_token": token, "owner_user_id": "3"}
        ).status_code
        == 422
    )
    assert (
        owner.post(
            "/plus/settings/household/create",
            content="x" * 8193,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    assert core.overview(db, viewer_id=1) == {"household": None}


def test_promotion_selects_shared_scope_without_modifying_personal_records(fixture):
    app, db, sessions = fixture
    owner, token = client_for(app, sessions[1])
    db.execute(
        "INSERT INTO watch_entries(user_id,anilist_id,season_number,status,progress_episodes) VALUES(1,100,1,'watching',3)"
    )
    db.commit()
    before = [tuple(r) for r in db.execute("SELECT * FROM watch_entries")]
    preview = core.promotion_preview(db, user_id=1)
    assert create(owner, token, promote=True, digest=preview["digest"]).status_code == 303
    selected = core.selected_scope(db, viewer_id=1)
    assert selected.kind == "household"
    assert before == [tuple(r) for r in db.execute("SELECT * FROM watch_entries")]
    assert len(core.LibraryRepository(db, selected).items()) == 1
    assert (
        owner.post(
            f"/plus/settings/household/{selected.library_id}/leave", data={"csrf_token": token}
        ).status_code
        == 409
    )


def render_route_fixtures(output):
    import os

    os.environ["OMAKASE_PLUS_PRIVATE"] = "true"
    app, db, sessions = fixture_app()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    owner, token = client_for(app, sessions[1])
    member, member_csrf = client_for(app, sessions[2])
    outsider, _ = client_for(app, sessions[3])
    (output / "empty.html").write_text(
        outsider.get("/plus/settings/household").text, encoding="utf-8"
    )
    (output / "create.html").write_text(
        owner.get("/plus/settings/household/create").text, encoding="utf-8"
    )
    (output / "join.html").write_text(member.get("/plus/households/join").text, encoding="utf-8")
    (output / "error.html").write_text(
        create(owner, token, promote=True, digest="stale").text, encoding="utf-8"
    )
    create(owner, token)
    library = core.overview(db, viewer_id=1)["household"]["id"]
    invited = owner.post(f"/plus/settings/household/{library}/invite", data={"csrf_token": token})
    (output / "owner.html").write_text(invited.text, encoding="utf-8")
    code = InputValues(invited.text).values["created-invite-code"]
    (output / "join-preview.html").write_text(
        member.post(
            "/plus/households/invitation/preview",
            data={"csrf_token": member_csrf, "invite_token": code},
        ).text,
        encoding="utf-8",
    )
    member.post(
        "/plus/households/join",
        data={
            "csrf_token": member_csrf,
            "invite_token": code,
            "preview_library_id": library,
            "display_name": "Alex",
        },
    )
    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(2,42,?,CURRENT_TIMESTAMP)",
        ('{"tmdb_id":42,"title":"The Last Train Through the Cedar Mountains"}',),
    )
    db.commit()
    receipt = member.post(
        f"/plus/libraries/{library}/catalog/request",
        data={"csrf_token": member_csrf, "domain": "tv", "canonical_id": "42"},
    )
    assert receipt.status_code == 303
    (output / "owner-requests.html").write_text(
        owner.get("/plus/settings/household").text, encoding="utf-8"
    )
    (output / "member.html").write_text(
        member.get("/plus/settings/household").text, encoding="utf-8"
    )
    for client in (owner, member, outsider):
        client.close()
    db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-to", required=True)
    args = parser.parse_args()
    render_route_fixtures(args.render_to)


def test_router_defaults_off_and_invite_preview_does_not_consume(fixture):
    app, db, sessions = fixture
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    disabled = FastAPI()
    disabled.include_router(create_household_router(templates), prefix="/plus")
    disabled.dependency_overrides[get_db] = lambda: db
    client, _ = client_for(disabled, sessions[1])
    assert client.get("/plus/settings/household").status_code == 404
    owner, token = client_for(app, sessions[1])
    member, member_csrf = client_for(app, sessions[2])
    create(owner, token)
    library = core.overview(db, viewer_id=1)["household"]["id"]
    invite = core.create_invite(db, core.resolve_scope(db, viewer_id=1, library_id=library))
    before = db.total_changes
    preview = member.post(
        "/plus/households/invitation/preview",
        data={"csrf_token": member_csrf, "invite_token": invite["token"]},
    )
    assert preview.status_code == 200 and "Join Shared Home?" in preview.text
    assert db.total_changes == before and core.overview(db, viewer_id=2) == {"household": None}
    wrong = member.post(
        "/plus/households/join",
        data={
            "csrf_token": member_csrf,
            "invite_token": invite["token"],
            "preview_library_id": "f" * 32,
        },
    )
    assert wrong.status_code == 422 and core.overview(db, viewer_id=2) == {"household": None}
