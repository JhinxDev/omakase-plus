from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus.auth import create_session
from omakase.plus.db import get_db as database
from omakase.plus.deps import get_db
from omakase.plus.mal_routes import create_mal_router
from omakase.plus.web_security import csrf_token


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_MASTER_KEY", "synthetic-route-master")
    monkeypatch.setenv("MAL_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("MAL_CLIENT_SECRET", "synthetic-secret")
    monkeypatch.setenv("OMAKASE_PLUS_URL", "https://example.invalid")
    from omakase.plus import secrets

    monkeypatch.setattr(secrets, "_KEY", None)
    db = database(tmp_path)
    for n in range(1, 4):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (n, f"member{n}@example.test", "hash"),
        )
    db.commit()
    sessions = [create_session(db, n) for n in range(1, 4)]
    calls = []
    remote = {"mal": None}

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/token"):
            return httpx.Response(
                200,
                json={
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "access_token": "synthetic-personal-access",
                    "refresh_token": "synthetic-personal-refresh",
                },
            )
        if request.url.path == "/v2/users/@me":
            return httpx.Response(200, json={"id": 202, "name": "Synthetic member"})
        if request.url.host == "graphql.anilist.co":
            import json

            query = json.loads(request.content)["query"]
            row = {
                "id": 1,
                "mediaId": 11,
                "status": "CURRENT",
                "score": 70.0,
                "progress": 3,
                "repeat": 0,
                "startedAt": {"year": None, "month": None, "day": None},
                "completedAt": {"year": None, "month": None, "day": None},
                "updatedAt": 1000,
                "media": {
                    "id": 11,
                    "idMal": 22,
                    "type": "ANIME",
                    "title": {"userPreferred": "Synthetic anime"},
                },
            }
            if "Viewer" in query:
                return httpx.Response(
                    200, json={"data": {"Viewer": {"id": 101, "name": "Synthetic AniList member"}}}
                )
            if "Page(" in query:
                return httpx.Response(
                    200,
                    json={
                        "data": {"Page": {"pageInfo": {"hasNextPage": False}, "mediaList": [row]}}
                    },
                )
            if "MediaList(" in query:
                return httpx.Response(200, json={"data": {"MediaList": row}})
            if "Media(idMal" in query:
                return httpx.Response(
                    200, json={"data": {"Media": {"id": 11, "idMal": 22, "type": "ANIME"}}}
                )
        if request.url.path == "/v2/users/@me/animelist":
            return httpx.Response(
                200,
                json={
                    "data": []
                    if remote["mal"] is None
                    else [
                        {
                            "node": {"id": 22, "title": "Synthetic anime"},
                            "list_status": remote["mal"],
                        }
                    ],
                    "paging": {},
                },
            )
        if request.method == "PATCH" and request.url.path == "/v2/anime/22/my_list_status":
            from urllib.parse import parse_qs

            form = parse_qs(request.content.decode())
            assert set(form) <= {
                "status",
                "score",
                "num_watched_episodes",
                "num_times_rewatched",
                "is_rewatching",
            }
            remote["mal"] = {
                "status": form.get("status", ["watching"])[0],
                "score": int(form.get("score", ["0"])[0]),
                "num_episodes_watched": int(form.get("num_watched_episodes", ["0"])[0]),
                "num_times_rewatched": int(form.get("num_times_rewatched", ["0"])[0]),
                "is_rewatching": form.get("is_rewatching", ["false"])[0] == "true",
                "start_date": None,
                "finish_date": None,
                "updated_at": "2026-10-28T12:00:00Z",
            }
            return httpx.Response(200, json=remote["mal"])
        if request.url.path == "/v2/anime/22":
            return httpx.Response(200, json={"id": 22, "my_list_status": remote["mal"]})
        pytest.fail("Unexpected provider request")

    app = FastAPI()
    templates = Jinja2Templates(
        directory=str(Path(__file__).parents[2] / "src/omakase/plus/templates")
    )
    app.include_router(
        create_mal_router(
            templates, http_factory=lambda: httpx.Client(transport=httpx.MockTransport(handler))
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)
    client.cookies.set("omakase_session", sessions[0])

    def csrf(session):
        from starlette.requests import Request

        return csrf_token(
            Request(
                {"type": "http", "headers": [(b"cookie", ("omakase_session=" + session).encode())]}
            )
        )

    return client, db, sessions, csrf, calls


def test_get_is_local_only_missing_client_setup_is_truthful(web, monkeypatch):
    client, db, sessions, csrf, calls = web
    monkeypatch.delenv("MAL_CLIENT_ID")
    page = client.get("/plus/settings/myanimelist")
    assert page.status_code == 200 and "administrator must configure" in page.text
    assert page.headers["cache-control"] == "private, no-store"
    assert not calls
    error = client.post(
        "/plus/integrations/myanimelist/connect", data={"csrf_token": csrf(sessions[0])}
    )
    assert error.status_code == 400 and not calls


def test_connection_callback_is_session_bound_and_token_never_rendered(web):
    client, db, sessions, csrf, calls = web
    response = client.post(
        "/plus/integrations/myanimelist/connect",
        data={"csrf_token": csrf(sessions[0])},
        follow_redirects=False,
    )
    from urllib.parse import parse_qs, urlsplit

    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["plain"]
    state = query["state"][0]
    client.cookies.set("omakase_session", sessions[1])
    assert (
        client.get(
            "/plus/integrations/myanimelist/callback",
            params={"code": "synthetic-code", "state": state},
        ).status_code
        == 400
    )
    assert not calls
    client.cookies.set("omakase_session", sessions[0])
    callback = client.get(
        "/plus/integrations/myanimelist/callback",
        params={"code": "synthetic-code", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303 and len(calls) == 2
    page = client.get(callback.headers["location"])
    assert "Verified as Synthetic member" in page.text
    assert (
        "synthetic-personal-access" not in page.text
        and "synthetic-personal-refresh" not in page.text
    )
    assert (
        client.get(
            "/plus/integrations/myanimelist/callback",
            params={"code": "synthetic-code", "state": state},
        ).status_code
        == 400
    )
    assert len(calls) == 2


def test_csrf_and_three_account_saved_status_are_isolated(web):
    client, db, sessions, csrf, calls = web
    assert (
        client.post(
            "/plus/integrations/myanimelist/connect", data={"csrf_token": "wrong"}
        ).status_code
        == 403
    )
    for session in sessions:
        client.cookies.set("omakase_session", session)
        result = client.get("/plus/api/tracker-bridge/status")
        assert (
            result.status_code == 200
            and result.json()["connections"]["myanimelist"]["state"] == "disconnected"
        )
        assert result.json()["enabled"] is False
    assert not calls


def test_private_route_off_and_large_body_are_rejected_before_provider(web, monkeypatch):
    client, db, sessions, csrf, calls = web
    assert (
        client.post(
            "/plus/tracker-bridge/preview",
            content=b"x" * 16385,
            headers={"content-type": "application/x-www-form-urlencoded"},
        ).status_code
        == 413
    )
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/settings/myanimelist").status_code == 404
    assert not calls


def test_actual_connect_verify_preview_apply_http_journey_and_other_accounts(web, monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from omakase.plus.secrets import store_secret

    client, db, sessions, csrf, calls = web
    monkeypatch.setenv("OMAKASE_TRACKER_BRIDGE_WRITERS_READY", "true")
    form = {"csrf_token": csrf(sessions[0])}
    start = client.post("/plus/integrations/myanimelist/connect", data=form, follow_redirects=False)
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    assert (
        client.get(
            "/plus/integrations/myanimelist/callback",
            params={"code": "synthetic-code", "state": state},
            follow_redirects=False,
        ).status_code
        == 303
    )
    store_secret(db, 1, "anilist_oauth_token", "synthetic-anilist-access")
    assert client.post("/plus/tracker-bridge/verify-anilist", data=form).status_code == 200
    preview = client.post("/plus/tracker-bridge/preview", data=form)
    assert (
        preview.status_code == 200
        and "Synthetic anime" in preview.text
        and "Proposed: 7/10" in preview.text
    )
    assert not any(method == "PATCH" for method, path in calls)
    row = db.execute(
        "SELECT id,plan_hash FROM tracker_bridge_plans WHERE user_id=1 ORDER BY created_at DESC"
    ).fetchone()
    apply = client.post(
        "/plus/tracker-bridge/plans/" + row["id"] + "/apply",
        data={**form, "plan_hash": row["plan_hash"], "approved": "0"},
    )
    assert apply.status_code == 200 and "Confirmed: 1." in apply.text
    assert sum(method == "PATCH" for method, path in calls) == 1
    count = len(calls)
    for session in sessions[1:]:
        client.cookies.set("omakase_session", session)
        assert (
            client.get("/plus/api/tracker-bridge/status").json()["connections"]["myanimelist"][
                "state"
            ]
            == "disconnected"
        )
        private = client.get("/plus/tracker-bridge/plans/" + row["id"])
        assert private.status_code == 400 and "Synthetic anime" not in private.text
    assert len(calls) == count
