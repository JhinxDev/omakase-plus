import asyncio
import sqlite3
from functools import wraps
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.requests import Request

from omakase.plus import households
from omakase.plus.auth import create_session, delete_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_playback_routes import create_player_router
from omakase.plus.web_playback_source import PlexStream, ResolvedAnimeSource
from omakase.plus.web_security import csrf_token


def run_async(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@pytest.fixture
def setup(monkeypatch, tmp_path, request):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")
    if getattr(request, "param", False):
        monkeypatch.setenv("OMAKASE_PLAYBACK_STREAMING", "true")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for uid in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (uid, f"{uid}@example.invalid", "fixture"),
        )
        households.ensure_personal_library(db, user_id=uid)
    db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number) VALUES(1,1,100,'Synthetic episode',1)"
    )
    db.commit()
    calls, valid = [], [True]

    async def resolver(db, **values):
        calls.append(values)
        if getattr(app.state, "resolver_wait", None) is not None:
            await app.state.resolver_wait.wait()
        scope = households.resolve_scope(
            db, viewer_id=values["actor_user_id"], library_id=values["library_id"]
        )
        resolved = ResolvedAnimeSource(
            actor_user_id=1,
            library_id=scope.library_id,
            anilist_id=100,
            season_number=1,
            episode_number=values["episode_number"],
            server_id="synthetic-server",
            account_id="12345",
            metadata_key="11",
            part_key="/library/parts/12/1234/file.mp4",
            part_size=100,
            duration_ms=30000,
            container=getattr(app.state, "source_container", "mp4"),
            video_codec="h264",
            audio_codec="aac",
            streams=(
                PlexStream("3", "video", "h264", "und", True, False, True, 0),
                PlexStream("4", "audio", "aac", "eng", True, False, True, 1),
                PlexStream("5", "subtitle", "ass", "eng", False, False, True, 2),
            )
            if getattr(app.state, "source_container", "mp4") == "mkv"
            else (),
            media_version="a" * 64,
            source_identity="b" * 64,
            base_url="https://plex.invalid",
            token="dummy-only",
            _scope=scope,
            _snapshot=(),
            episode_name=getattr(app.state, "episode_name", ""),
            episode_summary=getattr(app.state, "episode_summary", ""),
        )
        if getattr(app.state, "actual_source_guard", False):
            from dataclasses import replace

            from omakase.plus import web_playback_source as source_module

            resolved = replace(
                resolved,
                _snapshot=source_module._snapshot(
                    db, scope, resolved.anilist_id, resolved.season_number, resolved.episode_number
                ),
            )
        return resolved

    def guard(db, source):
        if not valid[0]:
            raise ValueError("Access revoked")
        households.revalidate_scope(db, source._scope)
        if getattr(app.state, "actual_source_guard", False):
            from omakase.plus.web_playback_source import revalidate_source

            revalidate_source(db, source)

    async def serve(req):
        if fixture := getattr(app.state, "catalog_plex_fixture", None):
            return fixture(req)
        if getattr(app.state, "upstream_status", 200) == 451:
            return httpx.Response(451)
        assert req.headers["x-plex-token"] == "dummy-only"
        bounds = req.headers.get("range", "bytes=0-99").removeprefix("bytes=").split("-")
        left, right = map(int, bounds)
        data = bytes(range(100))[left : right + 1]

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                if getattr(app.state, "read_wait", None) is not None:
                    app.state.reads_opened += 1
                    await app.state.read_wait.wait()
                yield data

        return httpx.Response(
            206 if "range" in req.headers else 200,
            stream=Stream(),
            headers={
                "content-length": str(right - left + 1),
                **(
                    {"content-range": f"bytes {left}-{right}/100"} if "range" in req.headers else {}
                ),
            },
        )

    app = FastAPI()
    resource = tmp_path / "manifest.mpd"
    resource.write_bytes(b"<MPD>synthetic immutable derivative</MPD>")

    async def prepare(db, *, authorize, **kwargs):
        app.state.prepare_calls = getattr(app.state, "prepare_calls", 0) + 1
        await authorize()
        if getattr(app.state, "prepare_wait", None) is not None:
            app.state.prepare_entered.set()
            await app.state.prepare_wait.wait()
        return {
            "job_id": "c" * 32,
            "media_version": "a" * 64,
            "resources": getattr(app.state, "remux_resources", ["manifest.mpd"]),
            "track_map": [],
            "subtitles": [],
            "fonts": [],
        }

    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    app.include_router(
        create_player_router(
            templates,
            lambda request, user, **kw: {"email": user.email},
            resolver=resolver,
            source_guard=guard,
            remote_guard=lambda *a, **kw: None,
            transport=httpx.MockTransport(serve),
            remux_prepare=prepare,
            resource_getter=lambda *a, **kw: getattr(app.state, "resource_override", resource),
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    sessions = {uid: create_session(db, uid) for uid in (1, 2)}
    db.commit()
    yield app, db, sessions, calls, valid
    db.close()


def client(app, session):
    result = TestClient(app, base_url="https://testserver", follow_redirects=False)
    result.cookies.set("omakase_session", session)
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]}
    )
    result.headers["x-csrf-token"] = csrf_token(request)
    result.headers["origin"] = "https://testserver"
    return result


@pytest.mark.parametrize("play", [False, True])
def test_page_does_not_resolve_media_or_create_history(setup, play):
    app, db, sessions, calls, _ = setup
    response = client(app, sessions[1]).get("/plus/watch/1/1", params={"play": int(play)})
    assert response.status_code == 200
    assert ("is-opening" in response.text) is play
    assert "Play episode" in response.text
    assert calls == []
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


def test_realdebrid_only_routes_web_watch_links_without_starting_plex(setup, monkeypatch):
    from omakase.plus import web_playback_routes

    app, db, sessions, calls, _ = setup
    monkeypatch.setattr(web_playback_routes, "playback_provider", lambda *_: "realdebrid")
    viewer = client(app, sessions[1])
    for source, target in (
        ("/plus/watch/1/1", "/plus/cloud/title/anime/1/0/1"),
        ("/plus/watch/title/movie/tt1234567", "/plus/cloud/title/movie/tt1234567/0/0"),
        ("/plus/watch/title/tv/100/1/2", "/plus/cloud/title/tv/100/1/2"),
    ):
        reply = viewer.get(source, params={"play": 1})
        assert reply.status_code == 307 and reply.headers["location"] == target
    assert calls == []
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 0


def test_watch_shows_next_without_starting_a_session(setup, monkeypatch):
    from omakase.plus import web_playback_navigation

    app, db, sessions, calls, _ = setup
    monkeypatch.setattr(web_playback_navigation, "_candidates", lambda *_: {2: None})
    response = client(app, sessions[1]).get("/plus/watch/1/1")
    library_id = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[
        0
    ]
    assert (
        f'data-player-sidebar-next href="/plus/watch/1/2?library_id={library_id}&amp;play=1"'
        in response.text
    )
    assert "Previous episode" not in response.text
    assert calls == []
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 0


def test_actual_source_guard_start_and_resource_routes_keep_atomic_authorization(
    setup, monkeypatch
):
    from omakase.plus import web_playback_source as source_module

    app, db, sessions, _, _ = setup
    app.state.actual_source_guard = True
    monkeypatch.setenv("OMAKASE_PLUS_PLEX_MANAGED_ROOT", "/synthetic")
    monkeypatch.setattr(source_module, "_publication", lambda *a: ("synthetic.mp4", 100, "b" * 64))
    monkeypatch.setattr(
        source_module.connections,
        "_values",
        lambda *a: ("https://plex.invalid", "dummy-only"),
    )
    monkeypatch.setattr(
        source_module.connections,
        "_read",
        lambda db, uid, key: "synthetic-server" if key == "plex_machine_id" else "dummy-only",
    )
    owner = client(app, sessions[1])
    created = owner.post("/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1})
    assert created.status_code == 201, created.text
    assert owner.head(created.json()["media_url"]).status_code == 200
    assert owner.get(created.json()["media_url"], headers={"range": "bytes=0-9"}).content == bytes(
        range(10)
    )
    assert (
        db.execute("SELECT COUNT(*) FROM web_playback_sessions WHERE closed=0").fetchone()[0] == 1
    )
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


def test_owned_exact_session_range_events_and_logout(setup):
    app, db, sessions, calls, valid = setup
    owner = client(app, sessions[1])
    response = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert "dummy-only" not in response.text
    assert "plex.invalid" not in response.text
    assert calls[0]["anilist_id"] == 100
    url = data["media_url"]
    response = owner.get(url, headers={"range": "bytes=-10"})
    assert response.status_code == 206 and response.content == bytes(range(90, 100))
    assert client(app, sessions[2]).get(url).status_code == 404
    assert (
        owner.post(
            f"/plus/api/playback/sessions/{data['session_id']}/events",
            json={
                "seq": 1,
                "position_seconds": 20,
                "playing": False,
                "event": "seek",
                "playback_rate": 1,
            },
        ).status_code
        == 200
    )
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0
    valid[0] = False
    assert owner.get(url).status_code == 409
    valid[0] = True
    delete_session(db, sessions[1])
    assert owner.get(url).status_code == 401


@pytest.mark.parametrize("container", ["mp4", "mkv"])
def test_prepared_response_reuses_exact_episode_text_without_another_resolution(setup, container):
    app, _, sessions, calls, _ = setup
    app.state.source_container = container
    app.state.episode_name = "<b>Night crossing</b>"
    app.state.episode_summary = "<p>The keeper follows this signal.</p>"
    response = client(app, sessions[1]).post(
        "/plus/api/playback/sessions",
        json={
            "monitor_id": 1,
            "episode_number": 1,
            "ass_supported": True,
            "codecs": ["h264", "aac"],
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["episode_name"] == "Night crossing"
    assert response.json()["episode_summary"] == "The keeper follows this signal."
    assert len(calls) == 1
    assert "dummy-only" not in response.text and "plex.invalid" not in response.text


def test_no_foreign_monitor_or_arbitrary_source_or_csrf_grant(setup):
    app, _, sessions, calls, _ = setup
    stranger = client(app, sessions[2])
    assert (
        stranger.post(
            "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
        ).status_code
        == 404
    )
    owner = client(app, sessions[1])
    assert (
        owner.post(
            "/plus/api/playback/sessions",
            json={"monitor_id": 1, "episode_number": 1, "url": "http://localhost/secret"},
        ).status_code
        == 422
    )
    del owner.headers["x-csrf-token"]
    assert (
        owner.post(
            "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
        ).status_code
        == 403
    )
    assert calls == []


def test_bounded_playback_body_preserves_connected_client():
    from fastapi import APIRouter

    app = FastAPI()
    router = APIRouter(route_class=bounded_form_route(4096, "Too large", forward_disconnect=True))

    @router.post("/prepare")
    async def prepare(request: Request, body: dict):
        return {"disconnected": await request.is_disconnected()}

    app.include_router(router)
    assert TestClient(app).post("/prepare", json={"allowed": True}).json() == {
        "disconnected": False
    }


@run_async
async def test_start_reserves_before_resolution_and_recovers_on_cancel(setup):
    app, db, sessions, calls, _ = setup
    owner = client(app, sessions[1])
    app.state.resolver_wait = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=dict(owner.headers),
        cookies={"omakase_session": sessions[1]},
    ) as http:
        tasks = [
            asyncio.create_task(
                http.post(
                    "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
                )
            )
            for _ in range(20)
        ]
        for _ in range(100):
            if sum(t.done() for t in tasks) == 19:
                break
            await asyncio.sleep(0.005)
        assert len(calls) == 1
        assert sum(t.done() for t in tasks) == 19
        assert all(t.result().status_code == 429 for t in tasks if t.done())
        pending = next(t for t in tasks if not t.done())
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 0
        app.state.resolver_wait.set()
        assert (
            await http.post(
                "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
            )
        ).status_code == 201


@run_async
async def test_stalled_media_reads_are_bounded_and_release_on_completion(setup):
    app, _, sessions, _, _ = setup
    owner = client(app, sessions[1])
    created = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    ).json()
    app.state.read_wait = asyncio.Event()
    app.state.reads_opened = 0
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        cookies={"omakase_session": sessions[1]},
    ) as http:
        tasks = [asyncio.create_task(http.get(created["media_url"])) for _ in range(20)]
        for _ in range(100):
            if sum(t.done() for t in tasks) == 12:
                break
            await asyncio.sleep(0.005)
        assert app.state.reads_opened == 8
        assert sum(t.done() for t in tasks) == 12
        assert all(t.result().status_code == 429 for t in tasks if t.done())
        app.state.read_wait.set()
        results = await asyncio.gather(*tasks)
        assert sum(r.status_code == 200 for r in results) == 8
        assert (
            await http.get(created["media_url"], headers={"range": "bytes=0-9"})
        ).status_code == 206


@run_async
async def test_ass_read_collision_retries_after_one_slot_releases_without_raising_cap(
    setup, tmp_path
):
    app, _, sessions, _, _ = setup
    app.state.source_container = "mkv"
    app.state.remux_resources = ["manifest.mpd", "subtitle-5.ass"]
    owner = client(app, sessions[1])
    created = owner.post(
        "/plus/api/playback/sessions",
        json={
            "monitor_id": 1,
            "episode_number": 1,
            "codecs": ["h264", "aac"],
            "ass_supported": True,
        },
    ).json()
    resource = created["media_url"].replace("manifest.mpd", "subtitle-5.ass")
    app.state.resource_override = tmp_path / "subtitle-5.ass"
    app.state.resource_override.write_bytes(b"[Script Info]\n[Events]\n")
    entered, release_body = asyncio.Event(), asyncio.Event()
    calls = []

    async def stalled_app(scope, receive, send):
        accepted = False

        async def stalled_send(message):
            nonlocal accepted
            if message["type"] == "http.response.start":
                accepted = message["status"] == 200
            if accepted and message["type"] == "http.response.body" and message.get("body"):
                calls.append(1)
                if len(calls) <= 8:
                    if len(calls) == 8:
                        entered.set()
                    await release_body.wait()
            await send(message)

        await app(scope, receive, stalled_send)

    # Actual authenticated local bodies remain open at the ASGI send boundary.
    # Cancellation must release one permit before a ninth ASS GET can enter.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=stalled_app),
        base_url="https://testserver",
        cookies={"omakase_session": sessions[1]},
    ) as http:
        held = [asyncio.create_task(http.get(resource)) for _ in range(8)]
        await asyncio.wait_for(entered.wait(), 2)
        rejected = await http.get(resource)
        assert rejected.status_code == 429 and rejected.headers["retry-after"] == "1"
        assert len(calls) == 8
        held[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await held[0]
        await asyncio.sleep(int(rejected.headers["retry-after"]))
        retry = asyncio.create_task(http.get(resource))
        for _ in range(50):
            if len(calls) == 9:
                break
            await asyncio.sleep(0.005)
        assert len(calls) == 9
        release_body.set()
        results = await asyncio.gather(*held[1:], retry)
        assert all(response.status_code == 200 for response in results)
        assert all(
            response.headers["content-type"].startswith("text/x-ssa") for response in results
        )


def test_authenticated_copy_inventory_range_head_and_current_revocation(setup):
    app, db, sessions, _, valid = setup
    app.state.source_container = "mkv"
    owner = client(app, sessions[1])
    response = owner.post(
        "/plus/api/playback/sessions",
        json={
            "monitor_id": 1,
            "episode_number": 1,
            "codecs": ["h264", "aac"],
            "ass_supported": True,
        },
    )
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["source"]["mode"] == "copy-remux"
    assert data["media_type"] == "application/dash+xml"
    url = data["media_url"]
    assert owner.head(url).status_code == 200
    assert owner.get(url, headers={"range": "bytes=0-3"}).content == b"<MPD"
    assert owner.get(url, headers={"range": "bytes=99999-"}).status_code == 416
    assert owner.get(url.replace("manifest.mpd", "unknown.ass")).status_code == 404
    assert client(app, sessions[2]).get(url).status_code == 404
    valid[0] = False
    assert owner.get(url).status_code == 409
    valid[0] = True
    assert (
        owner.post("/plus/api/playback/sessions/" + data["session_id"] + "/close").status_code
        == 204
    )
    assert owner.get(url).status_code == 404
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


def test_region_unavailable_is_finite_and_releases_media_admission(setup):
    app, _, sessions, _, _ = setup
    owner = client(app, sessions[1])
    data = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    ).json()
    app.state.upstream_status = 451
    for _ in range(5):
        assert owner.get(data["media_url"]).status_code == 451
    app.state.upstream_status = 200
    assert owner.get(data["media_url"]).status_code == 200


@run_async
async def test_cancelled_copy_preparation_closes_only_its_session_and_recovers(setup):
    app, db, sessions, _, _ = setup
    app.state.source_container = "mkv"
    app.state.prepare_wait = asyncio.Event()
    app.state.prepare_entered = asyncio.Event()
    owner = client(app, sessions[1])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=dict(owner.headers),
        cookies={"omakase_session": sessions[1]},
    ) as http:
        body = {
            "monitor_id": 1,
            "episode_number": 1,
            "codecs": ["h264", "aac"],
            "ass_supported": True,
        }
        task = asyncio.create_task(http.post("/plus/api/playback/sessions", json=body))
        await asyncio.wait_for(app.state.prepare_entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert db.execute("SELECT closed FROM web_playback_sessions").fetchone()[0] == 1
        app.state.prepare_wait.set()
        assert (await http.post("/plus/api/playback/sessions", json=body)).status_code == 201


def test_deferred_copy_returns_exact_fallback_before_any_worker_and_prepares_after_csrf(setup):
    app, db, sessions, _, _ = setup
    app.state.source_container = "mkv"
    owner = client(app, sessions[1])
    body = {
        "monitor_id": 1,
        "episode_number": 1,
        "codecs": ["h264", "aac"],
        "ass_supported": True,
        "defer_prepare": True,
    }
    response = owner.post("/plus/api/playback/sessions", json=body)
    assert response.status_code == 202, response.text
    pending = response.json()
    assert pending["preparation_required"] and pending["media_url"] is None
    assert pending["source"]["plex_url"].startswith("https://app.plex.tv/")
    assert getattr(app.state, "prepare_calls", 0) == 0
    url = pending["prepare_url"]
    assert client(app, sessions[2]).post(url, json={}).status_code == 404
    assert owner.post(url, json={"url": "http://arbitrary.invalid"}).status_code == 422
    csrf = owner.headers.pop("x-csrf-token")
    assert owner.post(url, json={}).status_code == 403
    owner.headers["x-csrf-token"] = csrf
    response = owner.post(url, json={})
    assert response.status_code == 201, response.text
    ready = response.json()
    assert not ready["preparation_required"] and ready["prepare_url"] is None
    assert owner.get(ready["media_url"]).status_code == 200
    assert app.state.prepare_calls == 1
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


@run_async
async def test_closing_pending_preparation_prevents_late_ready_response(setup):
    app, db, sessions, _, _ = setup
    app.state.source_container = "mkv"
    app.state.prepare_wait = asyncio.Event()
    app.state.prepare_entered = asyncio.Event()
    owner = client(app, sessions[1])
    pending = owner.post(
        "/plus/api/playback/sessions",
        json={
            "monitor_id": 1,
            "episode_number": 1,
            "codecs": ["h264", "aac"],
            "ass_supported": True,
            "defer_prepare": True,
        },
    ).json()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=dict(owner.headers),
        cookies={"omakase_session": sessions[1]},
    ) as http:
        task = asyncio.create_task(http.post(pending["prepare_url"], json={}))
        await asyncio.wait_for(app.state.prepare_entered.wait(), 2)
        assert (
            await http.post(
                "/plus/api/playback/sessions/" + pending["session_id"] + "/close", json={}
            )
        ).status_code == 204
        app.state.prepare_wait.set()
        assert (await task).status_code == 404
        assert (
            db.execute(
                "SELECT closed FROM web_playback_sessions WHERE session_id=?",
                (pending["session_id"],),
            ).fetchone()[0]
            == 1
        )


@run_async
async def test_terminal_close_persists_latest_seek_before_immediate_new_start(setup):
    app, db, sessions, _, _ = setup
    owner = client(app, sessions[1])
    created = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    ).json()
    prefix = "/plus/api/playback/sessions/" + created["session_id"]
    old = {"seq": 1, "position_seconds": 27, "playing": False, "event": "seek", "playback_rate": 1}
    assert owner.post(prefix + "/events", json=old).status_code == 200
    final = {**old, "seq": 3, "position_seconds": 3}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=dict(owner.headers),
        cookies={"omakase_session": sessions[1]},
    ) as http:
        closing = asyncio.create_task(http.post(prefix + "/close", json={"final_event": final}))
        await asyncio.sleep(0.01)
        starting = asyncio.create_task(
            http.post("/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1})
        )
        closed, started = await asyncio.gather(closing, starting)
        assert closed.status_code == 204, closed.text
        assert started.status_code == 201 and started.json()["position_seconds"] == 3
        assert (await http.post(prefix + "/close", json={"final_event": final})).status_code == 204
        assert (await http.post(prefix + "/events", json={**old, "seq": 2})).status_code == 404
        assert (
            await http.post(
                prefix + "/close", json={"final_event": {**final, "position_seconds": 9}}
            )
        ).status_code == 409
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


@pytest.mark.parametrize("boundary", ["foreign", "revoked", "takeover", "playing", "logout"])
def test_terminal_close_rejects_unqualified_state_without_progress(setup, boundary):
    app, db, sessions, _, valid = setup
    owner = client(app, sessions[1])
    created = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    ).json()
    final = {
        "seq": 1,
        "position_seconds": 27,
        "playing": False,
        "event": "seek",
        "playback_rate": 1,
    }
    expected = 409
    sender = owner
    if boundary == "foreign":
        sender, expected = client(app, sessions[2]), 404
    elif boundary == "revoked":
        valid[0] = False
    elif boundary == "takeover":
        assert (
            owner.post(
                "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
            ).status_code
            == 201
        )
    elif boundary == "playing":
        final["playing"], expected = True, 422
    else:
        delete_session(db, sessions[1])
        expected = 401
    response = sender.post(
        f"/plus/api/playback/sessions/{created['session_id']}/close", json={"final_event": final}
    )
    assert response.status_code == expected, response.text
    assert db.execute("SELECT COUNT(*) FROM web_playback_events").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0
    assert db.execute("SELECT MAX(position_seconds) FROM web_playback_resumes").fetchone()[0] == 0


@run_async
async def test_overlapping_exact_terminal_replay_joins_the_same_bounded_save(setup):
    app, db, sessions, _, _ = setup
    owner = client(app, sessions[1])
    created = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    ).json()
    prefix = f"/plus/api/playback/sessions/{created['session_id']}"
    event = {
        "seq": 1,
        "position_seconds": 27,
        "playing": False,
        "event": "seek",
        "playback_rate": 1,
    }
    assert owner.post(prefix + "/events", json=event).status_code == 200
    final = {**event, "seq": 2, "position_seconds": 3}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=dict(owner.headers),
        cookies={"omakase_session": sessions[1]},
    ) as http:
        first = asyncio.create_task(http.post(prefix + "/close", json={"final_event": final}))
        await asyncio.sleep(0.01)
        replay = asyncio.create_task(http.post(prefix + "/close", json={"final_event": final}))
        await asyncio.sleep(0.01)
        conflicting = await http.post(
            prefix + "/close", json={"final_event": {**final, "position_seconds": 9}}
        )
        assert conflicting.status_code == 409
        closed, replayed = await asyncio.gather(first, replay)
        assert closed.status_code == replayed.status_code == 204
        started = await http.post(
            "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
        )
        assert started.status_code == 201 and started.json()["position_seconds"] == 3
    assert db.execute("SELECT COUNT(*) FROM web_playback_events").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


@run_async
async def test_terminal_close_cancellation_releases_admission_and_has_no_detached_save(setup):
    app, db, sessions, _, _ = setup
    owner = client(app, sessions[1])
    created = owner.post(
        "/plus/api/playback/sessions", json={"monitor_id": 1, "episode_number": 1}
    ).json()
    prefix = f"/plus/api/playback/sessions/{created['session_id']}"
    event = {
        "seq": 1,
        "position_seconds": 27,
        "playing": False,
        "event": "seek",
        "playback_rate": 1,
    }
    assert owner.post(prefix + "/events", json=event).status_code == 200
    final = {**event, "seq": 2, "position_seconds": 3}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
        headers=dict(owner.headers),
        cookies={"omakase_session": sessions[1]},
    ) as http:
        closing = asyncio.create_task(http.post(prefix + "/close", json={"final_event": final}))
        await asyncio.sleep(0.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert db.execute("SELECT last_seq,closed FROM web_playback_sessions").fetchone()[:] == (
            1,
            0,
        )
        # A real second close must acquire the same finite hold and perform the
        # pending save itself; no detached request task touches the connection.
        assert (await http.post(prefix + "/close", json={"final_event": final})).status_code == 204
    assert db.execute("SELECT position_seconds FROM web_playback_resumes").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM web_playback_events").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


def test_player_return_keeps_explicit_personal_scope_without_changing_selection(setup, monkeypatch):
    from omakase.plus import routes

    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    app, db, sessions, _, _ = setup
    personal = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[0]
    shared = households.create_household(db, user_id=1, name="Other selected scope")
    households.select_library(db, households.resolve_scope(db, viewer_id=1, library_id=shared))

    async def synopsis(*args, **kwargs):
        return {}

    monkeypatch.setattr(routes, "ensure_anime_synopsis", synopsis)
    app.include_router(routes.router)
    owner = client(app, sessions[1])
    response = owner.get("/plus/watch/1/1?library_id=" + personal)
    assert response.status_code == 200
    expected = "/plus/library/1?library_id=" + personal
    assert 'href="' + expected + '"' in response.text
    assert owner.get(expected).status_code == 200
    assert households.selected_scope(db, viewer_id=1).library_id == shared
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


def test_watch_page_uses_only_scoped_cached_context_and_omits_missing_scores(setup):
    app, db, sessions, calls, _ = setup
    db.execute(
        "INSERT INTO anime_synopses(anilist_id,description,retry_at) VALUES(100,?,0)",
        ("A cached story. <script>unsafe</script>",),
    )
    db.commit()
    before = db.total_changes
    response = client(app, sessions[1]).get("/plus/watch/1/1")
    assert response.status_code == 200
    assert "A cached story." in response.text
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in response.text
    assert "Your viewing progress" not in response.text
    assert "data-player-timeline" in response.text
    assert "data-player-skip hidden" in response.text
    assert "See episodes" in response.text
    assert "Public score" not in response.text
    assert calls == []
    assert db.total_changes == before


def test_skip_marker_contract_requires_matching_identity_and_finite_nonoverlap():
    from omakase.plus.web_playback_context import normalize_skip_segments

    source = type(
        "Source", (), {"media_version": "a" * 64, "episode_number": 3, "duration_ms": 100000}
    )()
    valid = {
        "media_version": source.media_version,
        "episode_number": 3,
        "segments": [
            {"kind": "intro", "start_seconds": 2, "end_seconds": 12},
            {"kind": "credits", "start_seconds": 80, "end_seconds": 90},
        ],
    }
    assert len(normalize_skip_segments(source, valid)["segments"]) == 2
    for bad in [
        None,
        {**valid, "episode_number": 2},
        {**valid, "media_version": "b" * 64},
        {**valid, "segments": [{"kind": "credits", "start_seconds": 90, "end_seconds": 101}]},
        {
            **valid,
            "segments": [{"kind": "intro", "start_seconds": float("nan"), "end_seconds": 10}],
        },
        {**valid, "segments": [{"kind": "intro", "start_seconds": False, "end_seconds": 10}]},
        {**valid, "segments": list(reversed(valid["segments"]))},
    ]:
        assert normalize_skip_segments(source, bad)["segments"] == []


@pytest.mark.parametrize("setup", [False, True], indirect=True, ids=["direct", "compatible"])
@pytest.mark.parametrize("domain", ["movie", "tv"])
def test_catalog_plays_exact_personal_copy_and_saves_resume(setup, monkeypatch, domain):
    import hashlib
    import json
    import os
    import secrets

    from omakase.plus.web_playback_catalog import connections

    app, db, sessions, calls, _ = setup
    compatible = os.getenv("OMAKASE_PLAYBACK_STREAMING") == "true"
    base_url = "https://plex.invalid"
    fixture_auth = secrets.token_urlsafe(24)
    monkeypatch.setattr(connections, "_values", lambda *args: (base_url, fixture_auth))
    monkeypatch.setattr(connections, "_read", lambda *args: "synthetic-server")
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,year,identity_verified,updated_at) "
        "VALUES(1,'tt1234567',100,'Synthetic film',2025,1,'now')"
    )
    db.execute(
        "INSERT INTO tv_catalog VALUES(1,100,?,'now')",
        (json.dumps({"title": "Synthetic show", "tmdb_id": 100, "imdb_id": "tt7654321"}),),
    )
    db.execute(
        "INSERT INTO tv_plex_snapshots VALUES(1,100,?,'11',1,'now')",
        (hashlib.sha256((base_url + "\0" + fixture_auth).encode()).hexdigest(),),
    )
    for ep in (1, 2):
        db.execute(
            "INSERT INTO tv_episodes VALUES(1,100,1,?,? ,0,'none',1,'now')",
            (ep, json.dumps({"overview": f"Episode {ep} description"})),
        )
    db.commit()
    if domain == "movie":
        target = {"domain": domain, "imdb_id": "tt1234567"}
        url = "/plus/watch/title/movie/tt1234567?play=1"
        video_attrs = 'type="movie" year="2025" librarySectionTitle="Movies"'
        ids = '<Guid id="imdb://tt1234567"/><Guid id="tmdb://100"/>'
        section = "2"
    else:
        target = {"domain": domain, "tmdb_id": 100, "season_number": 1, "episode_number": 1}
        url = "/plus/watch/title/tv/100/1/1?play=1"
        video_attrs = 'type="episode" grandparentRatingKey="11" parentIndex="1" index="1"'
        ids = ""
        section = "3"
    moved = [False]
    provider_reads = []
    stopped = []

    class Bytes(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            yield self.data

    def plex(request):
        provider_reads.append(request.url.path)
        assert request.headers["x-plex-token"] == fixture_auth
        path = request.url.path
        filename = "other" if moved[0] else "synthetic"
        container, audio = ("mkv", "dca") if compatible else ("mp4", "aac")
        video = (
            f'<Video ratingKey="12" librarySectionID="{section}" {video_attrs} duration="30000">'
            + ids
            + f'<Media id="10" container="{container}" videoCodec="h264" audioCodec="{audio}" width="1920" height="1080">'
            + f'<Part id="1" size="100000" key="/library/parts/1/123/file.mp4" file="/media/{filename}.mp4"/>'
            + "</Media></Video>"
        )
        if path == "/api/v2/user":
            return httpx.Response(200, json={"id": 123})
        if path == "/identity":
            return httpx.Response(
                200, text='<MediaContainer machineIdentifier="synthetic-server"/>'
            )
        if path == "/library/sections":
            return httpx.Response(
                200,
                text='<MediaContainer><Directory key="2" title="Movies" type="movie"/>'
                '<Directory key="3" title="TV Shows" type="show"/></MediaContainer>',
            )
        if path == "/library/metadata/11":
            return httpx.Response(
                200,
                text='<MediaContainer librarySectionID="3"><Directory ratingKey="11" type="show">'
                '<Guid id="tmdb://100"/><Guid id="imdb://tt7654321"/></Directory></MediaContainer>',
            )
        if path in {
            "/library/sections/2/all",
            "/library/metadata/12",
            "/library/metadata/11/allLeaves",
        }:
            return httpx.Response(
                200,
                text=f'<MediaContainer librarySectionID="{section}" totalSize="1">{video}</MediaContainer>',
            )
        prefix = "/video/:/transcode/universal/"
        if path.startswith(prefix) and compatible:
            session_id = request.headers["x-plex-session-identifier"]
            if path == prefix + "decision":
                assert request.url.params["path"] == "/library/metadata/12"
                data = (
                    '<MediaContainer><Video ratingKey="12"><Media><Part id="1">'
                    '<Stream streamType="1" decision="copy" codec="h264"/>'
                    '<Stream streamType="2" decision="transcode" codec="aac"/>'
                    "</Part></Media></Video></MediaContainer>"
                )
            elif path == prefix + "start.mpd":
                resource = f"session/{session_id}/$RepresentationID$/"
                template = (
                    '<SegmentTemplate timescale="1" duration="5" startNumber="0" '
                    f'initialization="{resource}header" media="{resource}$Number$.m4s"/>'
                )
                data = (
                    '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" mediaPresentationDuration="PT30S">'
                    '<Period start="PT0S" duration="PT30S">'
                    f'<AdaptationSet>{template}<Representation id="0" mimeType="video/mp4" '
                    'codecs="avc1.640029" width="1920" height="1080" bandwidth="28000000"/></AdaptationSet>'
                    f'<AdaptationSet>{template}<Representation id="1" mimeType="audio/mp4" '
                    'codecs="mp4a.40.2" audioSamplingRate="48000" bandwidth="256000"/></AdaptationSet>'
                    "</Period></MPD>"
                )
            elif path == prefix + "stop":
                assert request.url.params["session"] == session_id
                stopped.append(session_id)
                return httpx.Response(200)
            else:
                assert path == prefix + f"session/{session_id}/0/header"
                data = "synthetic init"
            return httpx.Response(200, stream=Bytes(data.encode()))
        assert path == "/library/parts/1/123/file.mp4"
        assert request.headers["range"] == "bytes=0-9"

        return httpx.Response(
            206,
            stream=Bytes(b"0123456789"),
            headers={"Content-Range": "bytes 0-9/100000", "Content-Length": "10"},
        )

    app.state.catalog_plex_fixture = plex
    viewer, stranger = (client(app, sessions[uid]) for uid in (1, 2))
    page = viewer.get(url)
    assert page.status_code == 200
    assert ('aria-label="Movie player"' in page.text) is (domain == "movie")
    if domain == "tv":
        assert "/plus/watch/title/tv/100/1/2?play=1" in page.text
    assert stranger.get(url).status_code == 404
    assert not provider_reads

    start = "/plus/api/playback/catalog-sessions"
    assert viewer.post(start, json={"target": target | {"source_url": base_url}}).status_code == 422
    assert stranger.post(start, json={"target": target}).status_code == 409
    assert not provider_reads
    payload = {"target": target, "codecs": ["h264", "aac"]}
    response = viewer.post(start, json=payload)
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["domain"] == domain
    assert session["source"]["anilist_id"] is None
    assert fixture_auth not in response.text and "/media/" not in response.text
    assert calls == []

    media_url = session["media_url"]
    assert stranger.get(media_url, headers={"range": "bytes=0-9"}).status_code == 404
    if compatible:
        import re

        manifest = viewer.get(media_url)
        assert manifest.status_code == 200
        resource = re.search(r'initialization="([^"]+)"', manifest.text)[1]
        resource = media_url.rsplit("/", 1)[0] + "/" + resource.replace("$RepresentationID$", "0")
        assert stranger.get(resource).status_code == 404
        assert viewer.get(resource).content == b"synthetic init"
        moved[0] = True
        assert viewer.post(session["seek_url"], json={"position_seconds": 20}).status_code == 409
        assert not stopped
        moved[0] = False
        assert viewer.post(session["seek_url"], json={"position_seconds": 20}).status_code == 200
        assert viewer.get(resource).status_code == 404
        assert len(stopped) == 1
    else:
        assert viewer.get(media_url, headers={"range": "bytes=0-9"}).content == b"0123456789"
        moved[0] = True
        assert viewer.get(media_url, headers={"range": "bytes=0-9"}).status_code == 409
        moved[0] = False

    close = viewer.post(
        f"/plus/api/playback/sessions/{session['session_id']}/close",
        json={
            "final_event": {
                "seq": 1,
                "position_seconds": 7.0,
                "playing": False,
                "event": "seek",
                "playback_rate": 1.0,
            }
        },
    )
    assert close.status_code == 204, close.text
    if compatible:
        assert len(stopped) == 2
    resumed = viewer.post(start, json=payload)
    assert resumed.status_code == 201, resumed.text
    assert resumed.json()["position_seconds"] == 7
    assert db.execute("SELECT COUNT(*) FROM web_playback_watch").fetchone()[0] == 0


def test_cloud_player_session_events_and_close_mirror_cloud_progress(setup):
    from omakase.plus import cloud_progress
    from omakase.plus import web_playback_store as store

    app, db, sessions, _, _ = setup
    library_id = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[
        0
    ]
    target = store.ResolvedTarget(
        user_id=1,
        library_id=library_id,
        canonical_id="123456789",
        season_number=0,
        episode_number=0,
        media_version="c" * 64,
        duration_seconds=600,
        domain="anime-film",
    )
    snapshot = {
        "membership": {"library_id": library_id, "viewer_id": 1},
        "policy": {"kind": "realdebrid-cloud"},
        "source": {
            "kind": "debrid-cloud",
            "identity": "b" * 64,
            "media_version": "c" * 64,
            "file_key": "d-download1",
            "title": "Existing.mp4",
        },
    }
    binding = __import__("hashlib").sha256(sessions[1].encode()).hexdigest()
    state = store.create_session(db, target=target, binding_digest=binding, snapshot=snapshot)
    owner = client(app, sessions[1])
    event = owner.post(
        f"/plus/api/playback/sessions/{state['session_id']}/events",
        json={
            "seq": 1,
            "position_seconds": 42,
            "playing": True,
            "event": "progress",
            "playback_rate": 1,
        },
    )
    assert event.status_code == 200, event.text
    assert cloud_progress.get_position(db, 1, "d-download1")["position_seconds"] == 42
    import time as _time

    _time.sleep(0.3)
    closed = owner.post(
        f"/plus/api/playback/sessions/{state['session_id']}/close",
        json={
            "final_event": {
                "seq": 2,
                "position_seconds": 47,
                "playing": False,
                "event": "pause",
                "playback_rate": 1,
            }
        },
    )
    assert closed.status_code == 204, closed.text
    assert cloud_progress.get_position(db, 1, "d-download1")["position_seconds"] == 47
    row = db.execute(
        "SELECT closed FROM web_playback_sessions WHERE session_id=?",
        (state["session_id"],),
    ).fetchone()
    assert row[0] == 1
