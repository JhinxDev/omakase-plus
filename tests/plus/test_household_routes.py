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
        data={"csrf_token": member_csrf, "invite_token": code, "preview_library_id": library},
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
