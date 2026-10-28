"""Session-protected profile upload, replacement, removal and private serving."""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from PIL import Image

from omakase.plus import avatar, avatar_routes
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.secrets import read_secret
from omakase.plus.web_security import csrf_token


def picture(color="red"):
    output = io.BytesIO()
    with Image.new("RGB", (50, 50), color) as image:
        image.save(output, format="PNG")
    return output.getvalue()


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    conn = sqlite3.connect(tmp_path / "avatar-routes.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    for user_id in (1, 2):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES (?,?,?)",
            (user_id, f"avatar{user_id}@example.invalid", "synthetic"),
        )
    conn.commit()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(Path(avatar.__file__).parent / "templates"))
    templates.env.globals["asset_version"] = "test"
    app.include_router(avatar_routes.create_avatar_router(templates), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: conn
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        yield client, conn
    conn.close()


def login(client, db, user_id=1):
    session = create_session(db, user_id)
    client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
    return csrf_token(
        Request({"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]})
    )


def test_profile_and_avatar_require_authentication(browser):
    client, _ = browser
    for path in ("/plus/settings/profile", "/plus/account/avatar"):
        assert client.get(path).status_code == 401
    html = client.get("/plus/settings/profile", headers={"Accept": "text/html"})
    assert html.status_code == 302
    assert html.headers["location"] == "/plus/login"
    assert (
        client.post(
            "/plus/settings/profile", files={"avatar": ("face.png", picture(), "image/png")}
        ).status_code
        == 401
    )


def test_upload_actual_image_is_saved_privately_and_never_exposes_filename(browser):
    client, db = browser
    token = login(client, db)
    original = picture()
    response = client.post(
        "/plus/settings/profile",
        data={"csrf_token": token},
        files={"avatar": ("../../private-location.png", original, "image/png")},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/plus/settings/profile?notice=saved"
    image = client.get("/plus/account/avatar")
    assert image.status_code == 200
    assert image.content != original
    assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "private, no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert image.headers["vary"] == "Cookie"
    page = client.get("/plus/settings/profile")
    assert page.status_code == 200
    assert 'action="/plus/settings/profile"' in page.text
    assert 'enctype="multipart/form-data"' in page.text
    assert "/plus/account/avatar" in page.text
    assert "private-location" not in page.text
    assert "Remove picture" in page.text


@pytest.mark.parametrize("action", ["upload", "remove"])
def test_picture_mutations_always_require_csrf(browser, action):
    client, db = browser
    login(client, db)
    if action == "upload":
        response = client.post(
            "/plus/settings/profile", files={"avatar": ("face.png", picture(), "image/png")}
        )
    else:
        response = client.post("/plus/settings/profile/remove", data={"anything": "value"})
    assert response.status_code == 403
    assert avatar.get_avatar(db, 1) is None


def test_another_account_cannot_read_or_remove_the_owners_picture(browser):
    client, db = browser
    avatar.save_avatar(db, 1, picture())
    token = login(client, db, 2)
    assert client.get("/plus/account/avatar?user_id=1").status_code == 404
    response = client.post(
        "/plus/settings/profile/remove", data={"csrf_token": token, "user_id": "1"}
    )
    assert response.status_code == 303
    assert avatar.get_avatar(db, 1) is not None


def test_replace_and_remove_picture_are_available_as_real_forms(browser):
    client, db = browser
    token = login(client, db)
    for color in ("red", "blue"):
        result = client.post(
            "/plus/settings/profile",
            data={"csrf_token": token},
            files={"avatar": ("face.png", picture(color), "image/png")},
        )
        assert result.status_code == 303
    current = avatar.get_avatar(db, 1).revision
    assert current
    removed = client.post("/plus/settings/profile/remove", data={"csrf_token": token})
    assert removed.status_code == 303
    assert removed.headers["location"] == "/plus/settings/profile?notice=removed"
    assert client.get("/plus/account/avatar").status_code == 404
    assert avatar.get_avatar(db, 1) is None


def test_mime_and_filename_cannot_disguise_svg_or_external_url(browser):
    client, db = browser
    token = login(client, db)
    for body in (
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b"https://external.example.invalid/face.png",
    ):
        response = client.post(
            "/plus/settings/profile",
            data={"csrf_token": token},
            files={"avatar": ("face.png", body, "image/png")},
        )
        assert response.status_code == 422
        assert "external.example.invalid" not in response.text
        assert avatar.get_avatar(db, 1) is None


def test_oversized_upload_is_rejected_before_image_processing(browser):
    client, db = browser
    token = login(client, db)
    response = client.post(
        "/plus/settings/profile",
        data={"csrf_token": token},
        files={"avatar": ("face.png", b"x" * (avatar.MAX_INPUT_BYTES + 1), "image/png")},
    )
    assert response.status_code == 413
    assert avatar.get_avatar(db, 1) is None


def test_chunked_body_is_bounded_before_multipart_or_auth_parsing(browser):
    client, db = browser
    login(client, db)

    def chunks():
        for _ in range(avatar.MAX_INPUT_BYTES // 65536 + 3):
            yield b"x" * 65536

    response = client.post(
        "/plus/settings/profile",
        content=chunks(),
        headers={"Content-Type": "multipart/form-data; boundary=synthetic"},
    )
    assert response.status_code == 413


def test_malformed_multipart_is_a_controlled_bad_request(browser):
    client, db = browser
    login(client, db)
    response = client.post(
        "/plus/settings/profile",
        content=b"invalid multipart",
        headers={"Content-Type": "multipart/form-data; boundary=synthetic"},
    )
    assert response.status_code == 400


def test_picture_routes_are_closed_outside_private_plus(browser, monkeypatch):
    client, db = browser
    token = login(client, db)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/settings/profile").status_code == 404
    assert client.get("/plus/account/avatar").status_code == 404
    assert (
        client.post("/plus/settings/profile/remove", data={"csrf_token": token}).status_code == 404
    )


def test_name_only_save_is_private_encrypted_scoped_and_preserves_picture(browser):
    client, db = browser
    token = login(client, db)
    avatar.save_avatar(db, 1, picture())
    before = avatar.get_avatar(db, 1).revision
    response = client.post(
        "/plus/account/profile", data={"csrf_token": token, "display_name": "  Alex Morgan  "}
    )
    assert response.status_code == 303
    assert read_secret(db, 1, "display_name") == "Alex Morgan"
    assert read_secret(db, 2, "display_name") is None
    stored = db.execute(
        "SELECT encrypted_value FROM user_secrets WHERE user_id=1 AND key_name='display_name'"
    ).fetchone()[0]
    assert "Alex Morgan" not in stored
    assert avatar.get_avatar(db, 1).revision == before
    page = client.get("/plus/settings/profile")
    assert 'value="Alex Morgan"' in page.text
    assert '<strong class="account-email">Alex Morgan</strong>' in page.text
    assert "avatar1@example.invalid" in page.text
    assert (
        client.post(
            "/plus/account/profile", data={"csrf_token": token, "display_name": ""}
        ).status_code
        == 303
    )
    assert avatar.avatar_context(db, 1)["display_name"] == ""


@pytest.mark.parametrize(
    "name", ["x" * 81, "<script>", "Alex\x00Morgan", "Alex\nMorgan", "Alex\u202eMorgan"]
)
def test_invalid_display_names_are_not_saved(browser, name):
    client, db = browser
    token = login(client, db)
    assert (
        client.post(
            "/plus/account/profile", data={"csrf_token": token, "display_name": name}
        ).status_code
        == 422
    )
    assert read_secret(db, 1, "display_name") is None


def test_profile_name_form_is_authenticated_csrf_protected_bounded_and_private(
    browser, monkeypatch
):
    client, db = browser
    assert client.post("/plus/account/profile", data={"display_name": "Alex"}).status_code == 401
    token = login(client, db)
    assert client.post("/plus/account/profile", data={"display_name": "Alex"}).status_code == 403
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    assert (
        client.post(
            "/plus/account/profile",
            content=f"csrf_token={token}&display_name=Alex&display_name=Other",
            headers=headers,
        ).status_code
        == 422
    )
    assert (
        client.post("/plus/account/profile", content="x" * 4097, headers=headers).status_code == 413
    )
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert (
        client.post(
            "/plus/account/profile", data={"csrf_token": token, "display_name": "Alex"}
        ).status_code
        == 404
    )
