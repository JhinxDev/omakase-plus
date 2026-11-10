import re
import secrets
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.testclient import TestClient

from omakase.plus import households
from omakase.plus.auth import create_session, delete_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.native_tv_test import COOKIE, create_native_tv_test_router
from omakase.plus.web_security import csrf_token

ORIGIN = "https://testserver"
PAGE = "/plus/native-tv-test"
API = "/plus/api/native-tv-test/v1"


@pytest.fixture
def system(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")
    monkeypatch.setenv("OMAKASE_NATIVE_TV_TEST", "1")
    monkeypatch.setenv("OMAKASE_NATIVE_TV_ORIGIN", ORIGIN)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for actor in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (actor, f"{actor}@example.invalid", "fixture"),
        )
    db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number) VALUES(1,1,100,'Fixture episode',1)"
    )
    sessions = {actor: create_session(db, actor) for actor in (1, 2)}
    options = SimpleNamespace(
        now=1000.0, available=True, container="mp4", fixture_credential=secrets.token_hex(12)
    )

    async def resolve(
        db, *, actor_user_id, library_id, anilist_id, season_number, episode_number, **_
    ):
        return SimpleNamespace(
            actor_user_id=actor_user_id,
            _scope=households.resolve_scope(db, viewer_id=actor_user_id, library_id=library_id),
            anilist_id=anilist_id,
            season_number=season_number,
            episode_number=episode_number,
            media_version="a" * 64,
            duration_ms=1800000,
            container=options.container,
            video_codec="h264",
            audio_codec="aac",
            part_key="/library/parts/12/1234/file.mp4",
            part_size=100,
            base_url="https://plex.invalid",
            token=options.fixture_credential,
        )

    def local_guard(_, source):
        if not options.available:
            raise ValueError("Changed")
        return source

    async def remote_guard(db, source, **_):
        return local_guard(db, source)

    class Raw(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"abcd"

    def upstream(request):
        assert request.url.path == "/library/parts/12/1234/file.mp4"
        assert request.headers["range"] == "bytes=1-4"
        assert request.headers["x-plex-token"] == options.fixture_credential
        return httpx.Response(
            206, stream=Raw(), headers={"content-range": "bytes 1-4/100", "content-length": "4"}
        )

    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    app = FastAPI()
    app.include_router(
        create_native_tv_test_router(
            templates,
            lambda req, user, **kw: {"csrf_token": csrf_token(req), "email": user.email, **kw},
            resolve_anime=resolve,
            local_anime=local_guard,
            remote_anime=remote_guard,
            transport=httpx.MockTransport(upstream),
            clock=lambda: options.now,
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    yield app, db, sessions, options
    db.close()


def browser(app, session):
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set("omakase_session", session)
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]}
    )
    return client, csrf_token(request)


def issue(owner, csrf):
    return owner.post(PAGE, data={"csrf_token": csrf, "watch_path": "/plus/watch/1/1"})


def code(response):
    match = re.search(r"https://testserver#([A-F0-9]{20})", response.text)
    assert match
    return match[1]


def test_live_test_pass_is_scoped_and_revocable_without_watch_writes(system):
    app, db, sessions, options = system
    owner, csrf = browser(app, sessions[1])
    other, other_csrf = browser(app, sessions[2])
    native = TestClient(app, base_url=ORIGIN)
    assert native.post(PAGE).status_code == 401
    assert issue(owner, "invalid").status_code == 403
    assert "exact copy could not be opened" in issue(other, other_csrf).text
    link = code(issue(owner, csrf))
    redeem = native.post(API + "/redeem", json={"code": link})
    assert redeem.status_code == 200
    assert redeem.headers["cache-control"] == "private, no-store"
    assert native.post(API + "/redeem", json={"code": link}).status_code == 404
    data = redeem.json()
    assert data["title"] == "Fixture episode"
    assert data["identity"]["media_version"] == "a" * 64
    assert options.fixture_credential not in redeem.text and sessions[1] not in redeem.text
    media = API + "/media/" + data["media_id"]
    assert native.get(media).status_code == 404
    native.cookies.set(COOKIE, data["media_token"])
    stream = native.get(media, headers={"Range": "bytes=1-4"})
    assert (stream.status_code, stream.content) == (206, b"abcd")
    assert owner.post(PAGE + "/revoke", data={"csrf_token": csrf}).status_code == 200
    assert native.get(media).status_code == 404
    assert db.execute("SELECT count(*) FROM web_playback_resumes").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0


def test_pass_respects_feature_origin_compatibility_and_session_expiry(system, monkeypatch):
    app, db, sessions, options = system
    owner, csrf = browser(app, sessions[1])
    native = TestClient(app, base_url=ORIGIN)
    monkeypatch.delenv("OMAKASE_NATIVE_TV_TEST")
    assert issue(owner, csrf).status_code == 404
    monkeypatch.setenv("OMAKASE_NATIVE_TV_TEST", "1")
    wrong_origin = TestClient(app, base_url="http://testserver")
    assert (
        wrong_origin.post(API + "/redeem", headers={"X-Forwarded-Proto": "https"}).status_code
        == 404
    )
    options.container = "mkv"
    assert "H.264/AAC MP4" in issue(owner, csrf).text
    options.container = "mp4"
    old = code(issue(owner, csrf))
    options.now += 301
    assert native.post(API + "/redeem", json={"code": old}).status_code == 404
    data = native.post(API + "/redeem", json={"code": code(issue(owner, csrf))}).json()
    native.cookies.set(COOKIE, data["media_token"])
    media = API + "/media/" + data["media_id"]
    assert native.head(media).status_code == 200
    options.available = False
    assert native.head(media).status_code == 404
    options.available = True
    delete_session(db, sessions[1])
    assert native.head(media).status_code == 404
