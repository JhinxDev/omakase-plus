"""TV route security and explicit mutation contracts."""

import sqlite3

import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from jinja2 import DictLoader

from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.tv_routes import create_tv_router
from omakase.plus.web_security import csrf_token


@pytest.fixture
def browser(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    db = sqlite3.connect(tmp_path / "tv.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'one@test.invalid','x')")
    db.commit()

    class Service:
        def __init__(self):
            self.calls = []

        async def list(self, user_id, **kwargs):
            self.calls.append(("list", user_id, kwargs))
            return [{"title": "TV Fixture", "tmdb_id": 42}]

        async def search(self, user_id, query):
            self.calls.append(("search", user_id, query))
            return [{"title": "TV Fixture", "tmdb_id": 42}]

        async def detail(self, user_id, tmdb_id):
            self.calls.append(("detail", user_id, tmdb_id))
            return {
                "title": "TV Fixture",
                "tmdb_id": tmdb_id,
                "seasons": [{"season_number": 1, "released": True}],
            }

        async def sync(self, user_id, **kwargs):
            self.calls.append(("sync", user_id, kwargs))
            return {"imported": 1, "skipped": 0, "remaining": 0, "next_offset": 1}

        async def save_history(self, user_id, **kwargs):
            self.calls.append(("save_history", user_id, kwargs))
            return {"tmdb_id": kwargs["tmdb_id"]}

        async def save_episode(self, user_id, **kwargs):
            self.calls.append(("save_episode", user_id, kwargs))
            return {"tmdb_id": kwargs["tmdb_id"]}

        async def refresh(self, user_id, **kwargs):
            self.calls.append(("refresh", user_id, kwargs))
            return {"tmdb_id": kwargs["tmdb_id"]}

        async def check(self, user_id, **kwargs):
            self.calls.append(("check", user_id, kwargs))
            return {"tmdb_id": kwargs["tmdb_id"]}

        async def season(self, user_id, **kwargs):
            self.calls.append(("season", user_id, kwargs))
            return {"tmdb_id": kwargs["tmdb_id"]}

    svc = Service()
    app = FastAPI()
    templates = Jinja2Templates(directory=str(tmp_path))
    templates.env.loader = DictLoader(
        {
            "tv.html": "{% for card in cards %}{{card.title}}{% endfor %}{{tv_error}} {{sync_result.imported if sync_result else ''}}",
            "tv_detail.html": "{{card.title}} season={{selected_season}}",
        }
    )
    app.include_router(
        create_tv_router(templates, service_factory=lambda db, user: svc), prefix="/plus"
    )
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        session = create_session(db, 1)
        client.cookies.set("omakase_session", session, domain="testserver.local", path="/plus")
        token = csrf_token(
            Request(
                {"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]}
            )
        )
        yield client, svc, token
    db.close()


def test_navigation_is_metadata_only_and_normalizes_season(browser):
    client, svc, _ = browser
    assert client.get("/plus/tv").status_code == 200
    assert client.get("/plus/tv?q=Fixture").status_code == 200
    response = client.get("/plus/tv/42?season=99")
    assert response.status_code == 200 and "season=1" in response.text
    assert [c[0] for c in svc.calls] == ["list", "search", "detail"]
    assert all(c[1] == 1 for c in svc.calls)


@pytest.mark.parametrize(
    "path,data",
    [
        ("/tv/sync", {}),
        ("/tv/history", {"tmdb_id": 42, "status": "watching"}),
        (
            "/tv/episode",
            {"tmdb_id": 42, "season_number": 1, "episode_number": 1, "watched": "true"},
        ),
        ("/tv/42/check", {}),
        ("/tv/42/refresh", {}),
        ("/tv/42/season/1", {}),
    ],
)
def test_mutations_require_csrf_and_use_session_owner(browser, path, data):
    client, svc, token = browser
    assert client.post("/plus" + path, data=data).status_code == 403
    assert not svc.calls
    response = client.post(
        "/plus" + path,
        data={**data, "csrf_token": token, "user_id": 999},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert svc.calls[0][1] == 1


def test_route_auth_private_and_body_boundaries(browser, monkeypatch):
    client, svc, token = browser
    assert (
        client.post(
            "/plus/tv/sync",
            content="x" * 17000,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    assert client.get("/plus/tv?q=" + "x" * 201).status_code == 422
    assert client.get("/plus/tv/0").status_code == 422
    assert client.get("/plus/tv/2147483648").status_code == 422
    assert (
        client.post(
            "/plus/tv/episode",
            data={
                "csrf_token": token,
                "tmdb_id": 42,
                "season_number": 1,
                "episode_number": 1,
                "watched": "yes",
            },
        ).status_code
        == 422
    )
    assert not svc.calls
    client.cookies.clear()
    assert client.get("/plus/tv").status_code == 401
    assert client.get("/plus/tv/42").status_code == 401
    assert client.get("/plus/tv/42/artwork").status_code == 401
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/tv").status_code == 404
    assert client.post("/plus/tv/sync", data={"csrf_token": token}).status_code == 404
    assert not svc.calls


def test_history_missing_status_preserves_other_fields(browser):
    client, svc, token = browser
    response = client.post(
        "/plus/tv/history", data={"tmdb_id": 42, "rating": "8.5", "csrf_token": token}
    )
    assert response.status_code == 303 and response.headers["location"] == "/plus/tv/42"
    assert svc.calls[0][2]["rating"] == 8.5
    from omakase.plus.tv_service import UNSET

    assert svc.calls[0][2]["status"] is UNSET
