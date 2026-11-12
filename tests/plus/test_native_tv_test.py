import asyncio
import hashlib
import json
import re
import secrets
import shutil
import sqlite3
import subprocess
from contextlib import nullcontext
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.requests import Request
from starlette.testclient import TestClient

from omakase.plus import (
    connections,
    debrid_routes,
    households,
    native_tv_devices,
    native_tv_hls,
    native_tv_library,
    native_tv_test,
    web_playback_catalog,
    web_playback_navigation,
)
from omakase.plus import web_playback_store as store
from omakase.plus.auth import create_session, delete_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.media_store import upsert_asset
from omakase.plus.media_sync import _inventory_fingerprint
from omakase.plus.native_tv_test import COOKIE, create_native_tv_test_router
from omakase.plus.providers.contracts import ProviderFile, ProviderItem
from omakase.plus.web_playback_catalog import personal_scope
from omakase.plus.web_playback_delivery import DeliveryError
from omakase.plus.web_playback_source import PlexStream
from omakase.plus.web_security import csrf_token
from tests.plus.test_debrid_library import Provider as DebridProvider
from tests.plus.test_native_tv_matroska import fixture as indexed_caption_fixture

ORIGIN = "https://testserver"
PAGE = "/plus/native-tv-test"
API = "/plus/api/native-tv-test/v1"
DEVICES_PAGE = "/plus/settings/devices"
LIBRARY_PAGE = "/plus/native-tv-library"
LIBRARY_API = API + "/library"


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
        now=1000.0,
        available=True,
        duration_ms=1800000,
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        transcode=None,
        debrid=None,
        fixture_credential=secrets.token_hex(12),
        part_bytes=None,
        streams=(
            PlexStream("1", "video", "h264", "", False, False, False, 0),
            PlexStream("2", "audio", "aac", "en", True, False, True, 1),
        ),
    )

    async def resolve(
        db, *, actor_user_id, library_id, anilist_id, season_number, episode_number, **_
    ):
        return SimpleNamespace(
            actor_user_id=actor_user_id,
            library_id=library_id,
            _scope=households.resolve_scope(db, viewer_id=actor_user_id, library_id=library_id),
            anilist_id=anilist_id,
            season_number=season_number,
            episode_number=episode_number,
            episode_name="Arrival",
            episode_summary="The team returns to the station.",
            media_version="a" * 64,
            source_identity="b" * 64,
            duration_ms=options.duration_ms,
            container=options.container,
            video_codec=options.video_codec,
            audio_codec=options.audio_codec,
            part_key="/library/parts/12/1234/file.mp4",
            metadata_key="8",
            _single_plex_part=True,
            part_size=len(options.part_bytes) if options.part_bytes is not None else 100,
            streams=options.streams,
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
            yield options.part_bytes if options.part_bytes is not None else b"abcd"

    def upstream(request):
        if options.debrid is not None and request.url.host.endswith(".real-debrid.com"):
            return options.debrid(request)
        assert request.headers["x-plex-token"] == options.fixture_credential
        if options.transcode is not None:
            return options.transcode(request)
        assert request.url.path == "/library/parts/12/1234/file.mp4"
        if options.part_bytes is not None:
            return httpx.Response(
                200, stream=Raw(), headers={"content-length": str(len(options.part_bytes))}
            )
        assert request.headers["range"] == "bytes=1-4"
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
            lambda req, user, **kw: {
                "csrf_token": csrf_token(req),
                "email": user.email,
                "households_enabled": households.feature_enabled(),
                **kw,
            },
            resolve_anime=resolve,
            local_anime=local_guard,
            remote_anime=remote_guard,
            transport=httpx.MockTransport(upstream),
            clock=lambda: options.now,
            hls_database=lambda: nullcontext(db),
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


def test_native_cloud_browsing_and_playback_revoke_with_device(system, monkeypatch):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    token = "owner-token"
    monkeypatch.setattr(
        native_tv_test, "account_token", lambda _, user_id: token if user_id == 1 else ""
    )
    options.debrid = DebridProvider()
    owner, csrf = browser(app, sessions[1])
    device = TestClient(app, base_url=ORIGIN)
    assert device.get(LIBRARY_API + "/catalog?kind=cloud").status_code == 401
    issued = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
    link = re.search(r"https://testserver#library-([A-F0-9]{20})", issued.text)[1]
    paired = device.post(LIBRARY_API + "/redeem", json={"code": link})
    device.headers["Authorization"] = "Bearer " + paired.json()["token"]
    catalog = device.get(LIBRARY_API + "/catalog?kind=cloud")
    assert {item["id"] for item in catalog.json()["items"]} == {"t-torrent1", "d-download1"}
    files = device.get(LIBRARY_API + "/episodes?kind=cloud&title_id=t-torrent1").json()["items"]
    path = next(item["watch_path"] for item in files if item["watch_path"])
    play = device.post(LIBRARY_API + "/play", json={"watch_path": path})
    assert play.status_code == 200, play.text
    data = play.json()
    assert data["identity"]["domain"] == "cloud"
    assert token not in play.text and "real-debrid.com" not in play.text
    media = API + "/media/" + data["media_id"]
    assert device.get(media + "/index.m3u8").status_code == 404
    device.cookies.set(COOKIE, data["media_token"])
    manifest = device.get(media + "/index.m3u8")
    assert manifest.status_code == 200 and "00000.ts" in manifest.text
    assert "provider" not in manifest.text
    assert device.get(media + "/00000.ts").content == b"synthetic segment"
    assert device.get(LIBRARY_API + "/media/" + data["media_id"] + "/access").json() == {
        "allowed": True
    }
    assert device.post(LIBRARY_API + "/unlink").status_code == 200
    assert device.get(media + "/index.m3u8").status_code == 404
    assert db.execute("SELECT count(*) FROM web_playback_resumes").fetchone()[0] == 0


def test_realdebrid_only_plays_named_titles_and_rechecks_removed_files(system, monkeypatch):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    preferences = {"realdebrid_api_key": "owner-token", "preferred_playback_provider": "realdebrid"}
    monkeypatch.setattr(
        connections, "_read", lambda _, actor, key: preferences.get(key, "") if actor == 1 else ""
    )
    names = {
        "anime01": "Fixture Episode S01E01.mkv",
        "movie01": "Fixture Movie 2025.mkv",
        "series01": "Fixture Series S01E02.mkv",
    }
    content_hash = "a" * 40
    provider = DebridProvider()
    removed = set()
    registered = []
    preparing = {"downloadanime01"}

    def upstream(request):
        path = request.url.path.removeprefix("/rest/1.0/")
        if path == "torrents":
            return httpx.Response(
                200,
                json=[
                    {"id": key, "filename": name, "hash": content_hash, "status": "downloaded"}
                    for key, name in names.items()
                    if key not in removed
                ],
            )
        if path == "downloads":
            return httpx.Response(
                200, json=list(registered), headers={"X-Total-Count": str(len(registered))}
            )
        if path.startswith("torrents/info/"):
            key = path.rsplit("/", 1)[-1]
            if key in removed:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "id": key,
                    "hash": content_hash,
                    "status": "downloaded",
                    "files": [{"id": 1, "path": "/" + names[key], "bytes": 60, "selected": 1}],
                    "links": ["https://real-debrid.com/d/" + key],
                },
            )
        if path == "unrestrict/link":
            key = next(key for key in names if key.encode() in request.content)
            registered.append(
                {
                    "id": "download" + key,
                    "filename": names[key],
                    "filesize": 60,
                    "streamable": 1,
                    "link": "https://real-debrid.com/d/" + key,
                }
            )
            return httpx.Response(
                200,
                json={
                    "id": "generated" + key,
                    "filename": names[key],
                    "filesize": 60,
                    "streamable": 1,
                },
            )
        if path.startswith("streaming/mediaInfos/"):
            key = path.rsplit("/", 1)[-1]
            if key in preparing:
                preparing.remove(key)
                return httpx.Response(503)
        return provider(request)

    def unexpected_plex(*args, **kwargs):
        pytest.fail("Real-Debrid only must never resolve against Plex")

    options.debrid = upstream
    options.transcode = unexpected_plex
    monkeypatch.setattr(native_tv_test, "personal_plex_movies", unexpected_plex)
    db.execute("UPDATE media_monitors SET expected_episodes=1 WHERE id=1")
    item = ProviderItem(
        "anime01",
        "Fixture Episode",
        "downloaded",
        100,
        0,
        0,
        (ProviderFile("1", "/" + names["anime01"], 60, True),),
        content_hash,
    )
    upsert_asset(
        db,
        monitor_id=1,
        torrent_hash=content_hash,
        source_title="Fixture Episode",
        rd_torrent_id="anime01",
        status="downloaded",
        usable_coverage=[1],
        inventory_fingerprint=_inventory_fingerprint(item),
        inventory_verified_at="2026-01-01T00:00:00Z",
    )
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,title,year,identity_verified,updated_at) "
        "VALUES(1,'tt0000001','Fixture Movie',2025,1,'fixture')"
    )
    db.execute(
        "INSERT INTO tv_catalog VALUES(1,1,?,'fixture')",
        (json.dumps({"title": "Fixture Series", "in_collection": True}),),
    )
    db.execute("INSERT INTO tv_episodes VALUES(1,1,1,2,'{}',0,'none',0,'fixture')")
    db.commit()
    owner, csrf = browser(app, sessions[1])
    device = TestClient(app, base_url=ORIGIN)
    issue_page = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
    link = re.search(r"https://testserver#library-([A-F0-9]{20})", issue_page.text)[1]
    paired = device.post(LIBRARY_API + "/redeem", json={"code": link})
    device.headers["Authorization"] = "Bearer " + paired.json()["token"]
    paths = {
        "anime": "/plus/watch/1/1",
        "movie": "/plus/watch/title/movie/tt0000001",
        "tv": "/plus/watch/title/tv/1/1/2",
    }
    assert (
        device.get(LIBRARY_API + "/catalog?kind=movie").json()["items"][0]["watch_path"]
        == paths["movie"]
    )
    assert (
        device.get(LIBRARY_API + "/episodes?kind=tv&title_id=1").json()["items"][0]["watch_path"]
        == paths["tv"]
    )
    for domain, path in paths.items():
        result = device.post(LIBRARY_API + "/play", json={"watch_path": path})
        assert result.status_code == 200, result.text
        media = result.json()
        assert media["identity"]["domain"] == domain
        device.cookies.set(COOKIE, media["media_token"])
        prefix = API + "/media/" + media["media_id"]
        assert "00000.ts" in device.get(prefix + "/index.m3u8").text
        assert device.get(prefix + "/00000.ts").content == b"synthetic segment"
    assert not preparing
    removed.update(names)
    for path in paths.values():
        assert device.post(LIBRARY_API + "/play", json={"watch_path": path}).status_code == 404
    options.now += 26
    assert not device.get(LIBRARY_API + "/catalog?kind=movie").json()["items"]
    assert (
        device.get(LIBRARY_API + "/episodes?kind=tv&title_id=1").json()["items"][0]["watch_path"]
        is None
    )


def test_web_cloud_player_keeps_provider_and_other_accounts_out(system, monkeypatch):
    app, _, sessions, _ = system
    token = "owner-token"
    monkeypatch.setattr(
        debrid_routes, "account_token", lambda _, user_id: token if user_id == 1 else ""
    )
    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    app.include_router(
        debrid_routes.create_debrid_router(
            templates,
            lambda request, user, **context: {"email": user.email, **context},
            transport=httpx.MockTransport(DebridProvider()),
        ),
        prefix="/plus",
    )
    owner, csrf = browser(app, sessions[1])
    other, _ = browser(app, sessions[2])
    assert "Connect Real-Debrid" in other.get("/plus/cloud").text
    assert "Existing.mp4" in owner.get("/plus/cloud").text
    assert "Test.mkv" in owner.get("/plus/cloud?folder=t-torrent1").text
    url = "/plus/watch/cloud/d-download1"
    assert owner.post(url).status_code == 403
    page = owner.get(url + "?play=1&title=Existing.mp4")
    assert page.status_code == 200
    assert "data-web-player" in page.text and "web-player.js" in page.text
    assert '"cloud_file": "d-download1"' in page.text
    assert '"start_immediately": true' in page.text
    assert token not in page.text and "real-debrid.com" not in page.text
    redirect = owner.post(url, data={"csrf_token": csrf}, follow_redirects=False)
    assert redirect.status_code == 303
    assert "?play=1" in redirect.headers["location"]
    csrf_header = csrf_token(
        Request(
            {"type": "http", "headers": [(b"cookie", f"omakase_session={sessions[1]}".encode())]}
        )
    )
    session = owner.post(
        "/plus/api/playback/cloud-sessions",
        json={"file_key": "d-download1"},
        headers={"X-CSRF-Token": csrf_header},
    )
    assert session.status_code == 201, session.text
    opened = session.json()
    assert opened["media_type"] == "application/vnd.apple.mpegurl"
    assert opened["media_url"].startswith("/plus/cloud/media/")
    assert opened["subtitles"] == [] and opened["streaming"] is True
    media = opened["media_url"]
    assert owner.get(media).status_code == 200
    assert other.get(media).status_code == 404
    assert owner.get(media.removesuffix("index.m3u8") + "00000.ts").content == b"synthetic segment"


def test_cloud_main_player_session_restores_saved_cloud_position(system, monkeypatch):
    app, db, sessions, _ = system
    token = "owner-token"
    monkeypatch.setattr(
        debrid_routes, "account_token", lambda _, user_id: token if user_id == 1 else ""
    )
    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    app.include_router(
        debrid_routes.create_debrid_router(
            templates,
            lambda request, user, **context: {"email": user.email, **context},
            transport=httpx.MockTransport(DebridProvider()),
        ),
        prefix="/plus",
    )
    owner, _ = browser(app, sessions[1])
    saved = owner.post(
        "/plus/api/cloud/progress",
        data={
            "csrf_token": csrf_token(
                Request(
                    {
                        "type": "http",
                        "headers": [(b"cookie", f"omakase_session={sessions[1]}".encode())],
                    }
                )
            ),
            "file_key": "d-download1",
            "title": "Existing.mp4",
            "position": 42,
            "duration": 600,
        },
    )
    assert saved.status_code == 200
    csrf = csrf_token(
        Request(
            {"type": "http", "headers": [(b"cookie", f"omakase_session={sessions[1]}".encode())]}
        )
    )
    session = owner.post(
        "/plus/api/playback/cloud-sessions",
        json={"file_key": "d-download1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert session.status_code == 201, session.text
    opened = session.json()
    assert opened["position_seconds"] == 42
    assert opened["media_url"].startswith("/plus/cloud/media/")
    row = db.execute(
        "SELECT position_seconds FROM web_playback_resumes WHERE target_key IN ("
        "SELECT target_key FROM web_playback_sessions WHERE session_id=?)",
        (opened["session_id"],),
    ).fetchone()
    assert row is not None and row[0] == 42
    unauthenticated = owner.post(
        "/plus/api/playback/cloud-sessions", json={"file_key": "d-download1"}
    )
    assert unauthenticated.status_code == 403


def test_web_cloud_player_saves_and_resumes_progress(system, monkeypatch):
    app, _, sessions, _ = system
    token = "owner-token"
    monkeypatch.setattr(
        debrid_routes, "account_token", lambda _, user_id: token if user_id == 1 else ""
    )
    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    app.include_router(
        debrid_routes.create_debrid_router(
            templates,
            lambda request, user, **context: {"email": user.email, **context},
            transport=httpx.MockTransport(DebridProvider()),
        ),
        prefix="/plus",
    )
    owner, csrf = browser(app, sessions[1])
    progress = owner.post(
        "/plus/api/cloud/progress",
        data={
            "csrf_token": csrf,
            "file_key": "d-download1",
            "title": "Existing.mp4",
            "position": 42,
            "duration": 600,
        },
    )
    assert progress.status_code == 200, progress.text
    assert progress.json()["position_seconds"] == 42
    listing = owner.get("/plus/cloud").text
    assert "Resume 0:42" in listing
    assert 'aria-label="Resume Existing.mp4"' in listing
    play = owner.post(
        "/plus/watch/cloud/d-download1", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert play.status_code == 303
    assert "?play=1" in play.headers["location"]
    player = owner.get(play.headers["location"])
    assert player.status_code == 200
    assert '"cloud_file": "d-download1"' in player.text
    assert '"start_immediately": true' in player.text
    bad = owner.post(
        "/plus/api/cloud/progress",
        data={
            "csrf_token": csrf,
            "file_key": "not-a-file",
            "title": "x",
            "position": 10,
            "duration": 100,
        },
    )
    assert bad.status_code == 422
    missing_csrf = owner.post(
        "/plus/api/cloud/progress",
        data={"file_key": "d-download1", "position": 10, "duration": 100},
    )
    assert missing_csrf.status_code == 403
    finished = owner.post(
        "/plus/api/cloud/progress",
        data={
            "csrf_token": csrf,
            "file_key": "d-download1",
            "title": "Existing.mp4",
            "position": 595,
            "duration": 600,
        },
    )
    assert finished.status_code == 200 and finished.json()["position_seconds"] == 0
    assert "Resume 0:42" not in owner.get("/plus/cloud").text


def test_live_test_pass_is_scoped_and_revocable_without_watch_writes(system):
    app, db, sessions, options = system
    owner, csrf = browser(app, sessions[1])
    other, other_csrf = browser(app, sessions[2])
    native = TestClient(app, base_url=ORIGIN)
    assert native.post(PAGE).status_code == 401
    assert issue(owner, "invalid").status_code == 403
    assert "exact copy could not be opened" in issue(other, other_csrf).text
    issued = issue(owner, csrf)
    assert issued.headers["cache-control"] == "private, no-store"
    assert "Use a one-time link" in issued.text
    assert 'data-copy-device-link="apple-tv-test-link"' in issued.text
    link = code(issued)
    redeem = native.post(API + "/redeem", json={"code": link})
    assert redeem.status_code == 200
    assert redeem.headers["cache-control"] == "private, no-store"
    assert native.post(API + "/redeem", json={"code": link}).status_code == 404
    data = redeem.json()
    assert data["title"] == "Fixture episode"
    assert "access_check" not in data
    assert data["identity"]["media_version"] == "a" * 64
    assert options.fixture_credential not in redeem.text and sessions[1] not in redeem.text
    media = API + "/media/" + data["media_id"]
    assert native.get(media).status_code == 404
    native.cookies.set(COOKIE, data["media_token"])
    stream = native.get(media, headers={"Range": "bytes=1-4"})
    assert (stream.status_code, stream.content) == (206, b"abcd")
    revoked = owner.post(PAGE + "/revoke", data={"csrf_token": csrf})
    assert revoked.status_code == 200
    assert "Link revoked" in revoked.text and link not in revoked.text
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


def test_library_link_scopes_catalog_and_media_to_the_browser_owner(system, monkeypatch):
    app, db, sessions, options = system
    owner, csrf = browser(app, sessions[1])
    native = TestClient(app, base_url=ORIGIN)
    assert owner.get(LIBRARY_PAGE).status_code == 404
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    assert native.get(LIBRARY_PAGE).status_code == 401
    assert owner.post(LIBRARY_PAGE, data={"action": "issue"}).status_code == 403
    assert native.get(LIBRARY_API + "/catalog?kind=anime").status_code == 401
    episode_image = LIBRARY_API + "/episode-artwork/tv/42/1/1"
    assert native.get(episode_image).status_code == 401
    for actor, title in ((1, "Owner"), (2, "Other")):
        db.execute(
            "INSERT INTO movie_catalog(user_id,imdb_id,title,updated_at) VALUES(?,?,?,'fixture')",
            (actor, "tt0000001", f"{title} movie"),
        )
        db.execute(
            """INSERT INTO movie_requests(user_id,imdb_id,detail,created_at,updated_at)
               VALUES(?,'tt0000001','fixture','fixture','fixture')""",
            (actor,),
        )
        db.execute(
            """INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at)
               VALUES(?,42,json_object('title',?,'in_collection',1),'fixture')""",
            (actor, f"{title} series"),
        )
        db.execute(
            """INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,
                       metadata_json,plex_present,updated_at)
               VALUES(?,42,1,1,json_object('title',?),1,'fixture')""",
            (actor, f"{title} episode"),
        )
    db.execute(
        """INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number)
           VALUES(2,2,100,'Other anime',1)"""
    )
    db.execute(
        "UPDATE tv_episodes SET watched=1,watch_source='manual' WHERE user_id=1 AND tmdb_id=42"
    )
    synopsis = "A keeper follows the light.\nThe lantern reveals a hidden harbor."
    db.execute("INSERT INTO anime_synopses VALUES (100,?,0)", (synopsis,))
    db.commit()

    async def fixture_image(_db, identity):
        assert (identity.user_id, identity.media_id, identity.season, identity.episode) == (
            1,
            42,
            1,
            1,
        )
        return b"owned episode still", "image/webp"

    monkeypatch.setattr(native_tv_test, "episode_artwork", fixture_image)

    response = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
    link = re.search(r"https://testserver#library-([A-F0-9]{20})", response.text)[1]
    response = native.post(LIBRARY_API + "/redeem", json={"code": link})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert native.post(LIBRARY_API + "/redeem", json={"code": link}).status_code == 404
    native.headers["Authorization"] = "Bearer " + response.json()["token"]
    for kind, title in (
        ("anime", "Fixture episode"),
        ("movie", "Owner movie"),
        ("tv", "Owner series"),
    ):
        catalog = native.get(LIBRARY_API + "/catalog", params={"kind": kind})
        assert [entry["title"] for entry in catalog.json()["items"]] == [title]
        assert "Other" not in catalog.text
    detail = native.get(LIBRARY_API + "/episodes", params={"kind": "tv", "title_id": "42"})
    episode = detail.json()["items"][0]
    assert episode["title"] == "Owner episode" and episode["watched"] is True
    assert episode["episode_artwork"] == "episode-artwork/tv/42/1/1"
    anime_episodes = LIBRARY_API + "/episodes?kind=anime&title_id="
    assert native.get(anime_episodes + "1").json()["title_summary"] == synopsis
    assert native.get(anime_episodes + "2").status_code == 404
    assert native.get(episode_image).content == b"owned episode still"
    assert native.get(LIBRARY_API + "/episode-artwork/tv/42/2/1").status_code == 404
    assert (
        native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/2/1"}).status_code
        == 404
    )
    options.container = "mkv"
    assert (
        native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"}).status_code
        == 415
    )
    options.container = "mp4"
    response = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"})
    assert response.status_code == 200
    assert options.fixture_credential not in response.text and sessions[1] not in response.text
    playback = response.json()
    assert playback["access_check"] is True
    native.cookies.set(COOKIE, playback["media_token"])
    media = API + "/media/" + playback["media_id"]
    access = f"{LIBRARY_API}/media/{playback['media_id']}/access"
    allowed = native.get(access)
    assert allowed.json() == {"allowed": True}
    assert allowed.headers["cache-control"] == "private, no-store"
    assert TestClient(app, base_url=ORIGIN).get(access).status_code == 401
    peer, peer_csrf = browser(app, sessions[2])
    issued = peer.post(LIBRARY_PAGE, data={"csrf_token": peer_csrf, "action": "issue"})
    peer_link = re.search(r"https://testserver#library-([A-F0-9]{20})", issued.text)[1]
    peer_device = TestClient(app, base_url=ORIGIN)
    paired = peer_device.post(LIBRARY_API + "/redeem", json={"code": peer_link})
    peer_device.headers["Authorization"] = "Bearer " + paired.json()["token"]
    assert peer_device.get(access).status_code == 404
    assert native.get(media, headers={"Range": "bytes=1-4"}).content == b"abcd"
    assert (
        owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "revoke"}).status_code == 200
    )
    assert native.get(LIBRARY_API + "/catalog?kind=anime").status_code == 401
    assert native.head(media).status_code == 404
    assert native.get(access).status_code == 401
    resume = f"{LIBRARY_API}/media/{playback['media_id']}/resume"
    assert native.post(resume, json={"seq": 1, "position_seconds": 10}).status_code == 401
    assert db.execute("SELECT count(*) FROM web_playback_resumes").fetchone()[0] == 0


def test_personal_movie_catalog_keeps_removed_history_without_offering_play(system):
    _, db, _, _ = system
    for imdb_id, tmdb_id, title, request_code, status in (
        ("tt0000001", 1, "A removed movie", "copy_removed", "watching"),
        ("tt0000002", 2, "B library movie", "requested", "unwatched"),
    ):
        db.execute(
            """INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,backdrop_url,updated_at)
               VALUES(1,?,?,?,'https://image.tmdb.org/t/p/w780/test.jpg','fixture')""",
            (imdb_id, tmdb_id, title),
        )
        db.execute(
            """INSERT INTO movie_requests(user_id,imdb_id,code,detail,created_at,updated_at)
               VALUES(1,?,?,'fixture','fixture','fixture')""",
            (imdb_id, request_code),
        )
        db.execute(
            """INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,
                       created_at,updated_at) VALUES(1,?,?,?,'manual','fixture','fixture')""",
            (imdb_id, tmdb_id, status),
        )
    db.commit()
    scope = personal_scope(db, 1)

    catalog = native_tv_library.collection(db, scope, "movie", 0)
    assert catalog["items"][0]["watch_path"] is None
    assert catalog["featured"]["id"] == "tt0000002"
    assert catalog["continue_watching"] == []
    assert native_tv_library.collection(db, scope, "movie", 0, "removed")["items"] == [
        catalog["items"][0]
    ]
    history = db.execute(
        "SELECT status FROM movie_history WHERE user_id=1 AND imdb_id='tt0000001'"
    ).fetchone()
    assert history[0] == "watching"

    db.execute(
        """INSERT INTO movie_verifications(user_id,imdb_id,fingerprint,plex_key,selected_audio,
                   state,code,detail,http_status,bytes_read,verified_at,expires_at)
           VALUES(1,'tt0000001',?,'82','English','ready','ready','fixture',206,65536,
                  datetime('now'),datetime('now','+1 hour'))""",
        ("a" * 64,),
    )
    catalog = native_tv_library.collection(db, scope, "movie", 0)
    assert catalog["items"][0]["watch_path"] == "/plus/watch/title/movie/tt0000001"
    assert catalog["continue_watching"][0]["id"] == "tt0000001"
    db.execute("UPDATE movie_verifications SET expires_at=datetime('now','-1 hour')")
    assert native_tv_library.collection(db, scope, "movie", 0)["items"][0]["watch_path"] is None


def test_linked_movie_catalog_includes_current_verified_plex_movies(system, monkeypatch):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    connections = native_tv_library.connections
    monkeypatch.setattr(
        connections,
        "movie_connections",
        lambda _, user_id: native_tv_library.MovieConnections(
            plex_url="https://plex.invalid" if user_id == 1 else "",
            plex_token=options.fixture_credential,
            plex_machine_id="fixture-server",
            plex_movies_section_id=1,
        ),
    )
    db.executemany(
        """INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,identity_verified,updated_at)
           VALUES(?,?,?,?,?,'fixture')""",
        [
            (1, "tt0000001", 1, "Plex Movie", 1),
            (1, "tt0000002", 2, "Cached only", 1),
            (1, "tt0000003", 3, "Unverified", 0),
            (2, "tt0000004", 4, "Other owner", 1),
            (1, "tt0000005", 5, "Duplicate", 1),
            (1, "tt0000006", 6, "Wrong TMDB", 1),
        ],
    )
    db.commit()
    inventory = [
        (1, 1, 11),
        (3, 3, 13),
        (4, 4, 14),
        (5, 5, 15),
        (5, 5, 16),
        (6, 7, 17),
    ]
    upstream_available = True

    def upstream(request):
        assert request.method == "GET"
        path = request.url.path
        if path == "/identity":
            return httpx.Response(200, text='<MediaContainer machineIdentifier="fixture-server"/>')
        if path == "/library/sections":
            return httpx.Response(
                200,
                text='<MediaContainer><Directory key="1" title="Movies" type="movie"/></MediaContainer>',
            )
        assert path == "/library/sections/1/all"
        if not upstream_available:
            return httpx.Response(503)
        videos = "".join(
            f'<Video type="movie" ratingKey="{key}"><Guid id="imdb://tt{imdb:07d}"/>'
            f'<Guid id="tmdb://{tmdb}"/></Video>'
            for imdb, tmdb, key in inventory
        )
        return httpx.Response(
            200,
            text=f'<MediaContainer totalSize="{len(inventory)}" librarySectionID="1" '
            f'librarySectionTitle="Movies">{videos}</MediaContainer>',
        )

    options.transcode = upstream
    device = TestClient(app, base_url=ORIGIN)
    owner, csrf = browser(app, sessions[1])
    issued = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
    link = re.search(r"https://testserver#library-([A-F0-9]{20})", issued.text)[1]
    paired = device.post(LIBRARY_API + "/redeem", json={"code": link})
    device.headers["Authorization"] = "Bearer " + paired.json()["token"]
    path = LIBRARY_API + "/catalog?kind=movie"
    result = device.get(path)
    items = result.json()["items"]
    assert [item["id"] for item in items] == ["tt0000001"]
    assert items[0]["watch_path"] == "/plus/watch/title/movie/tt0000001"
    assert options.fixture_credential not in result.text
    assert device.get(path + "&q=missing").json()["items"] == []
    upstream_available = False
    assert device.get(path).json()["items"] == []
    assert db.execute("SELECT count(*) FROM movie_history").fetchone()[0] == 0


def test_household_library_scope_is_explicit_and_revokes_open_streams(system, monkeypatch):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    shared = households.create_household(db, user_id=1, name="Shared Cinema")

    def owner_scope():
        return households.resolve_scope(db, viewer_id=1, library_id=shared)

    with db:
        item = households.LibraryRepository(db, owner_scope()).add(
            domain="anime", canonical_id="100", season=1, title="Shared Anime", legacy_id="1"
        )
        db.execute(
            """INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number)
               VALUES(2,1,101,'Private Anime',1)"""
        )
    invite = households.create_invite(db, owner_scope())
    households.accept_invite(db, viewer_id=2, token=invite["token"], display_name="Member")
    monkeypatch.setattr(
        web_playback_navigation, "_candidates", lambda *_: {1: "b" * 64, 2: "b" * 64}
    )

    native = TestClient(app, base_url=ORIGIN)
    member, csrf = browser(app, sessions[2])
    pairing = native.post(LIBRARY_API + "/device").json()
    approval = {"action": "approve", "device_code": pairing["user_code"], "csrf_token": csrf}
    assert "Apple TV connected" in member.post(DEVICES_PAGE, data=approval).text
    native.headers["Authorization"] = "Bearer " + pairing["device_token"]

    personal = personal_scope(db, 2).library_id
    choices = native.get(LIBRARY_API + "/libraries")
    assert choices.status_code == 200
    assert [(row["id"], row["kind"]) for row in choices.json()["items"]] == [
        (personal, "personal"),
        (shared, "household"),
    ]
    catalog = LIBRARY_API + "/catalog?kind=anime"
    query = {"library_id": shared}
    assert native.get(catalog).json()["items"] == []
    assert [
        row["id"] for row in native.get(catalog, params={"kind": "anime", **query}).json()["items"]
    ] == [item]
    assert (
        native.get(
            catalog, params={"kind": "anime", "library_id": personal_scope(db, 1).library_id}
        ).status_code
        == 404
    )
    detail = native.get(
        LIBRARY_API + "/episodes", params=query | {"kind": "anime", "title_id": item}
    )
    assert detail.json()["items"][0]["watch_path"] == "/plus/watch/1/1"
    play = LIBRARY_API + "/play"
    assert (
        native.post(play, params=query, json={"watch_path": "/plus/watch/2/1"}).status_code == 404
    )
    response = native.post(play, params=query, json={"watch_path": "/plus/watch/1/1"})
    assert response.status_code == 200
    grant = response.json()
    assert grant["identity"]["library_id"] == shared
    assert re.fullmatch(r"[a-f0-9]{64}", grant["resume_scope"])
    native.cookies.set(COOKIE, grant["media_token"])
    media_id = grant["media_id"]
    media = API + "/media/" + media_id
    resume = f"{LIBRARY_API}/media/{media_id}/resume"
    access = f"{LIBRARY_API}/media/{media_id}/access"
    assert native.get(media, headers={"Range": "bytes=1-4"}).content == b"abcd"
    assert native.post(resume, json={"seq": 1, "position_seconds": 37}).status_code == 200
    assert tuple(
        db.execute(
            "SELECT user_id,library_id,position_seconds FROM web_playback_resumes"
        ).fetchone()
    ) == (2, shared, 37)
    successor = native.post(f"{LIBRARY_API}/media/{media_id}/next")
    assert successor.status_code == 200
    assert successor.json()["identity"]["library_id"] == shared
    assert successor.json()["identity"]["episode_number"] == 2
    assert successor.json()["resume_scope"] == grant["resume_scope"]
    assert native.get(access).json() == {"allowed": True}

    households.leave_or_revoke(db, owner_scope(), member_id=2)
    assert native.get(catalog, params={"kind": "anime", **query}).status_code == 404
    assert native.get(catalog).status_code == 200
    assert native.head(media).status_code == 404
    assert native.get(access).status_code == 404
    assert native.post(resume, json={"seq": 2, "position_seconds": 40}).status_code == 404
    invite = households.create_invite(db, owner_scope())
    households.accept_invite(db, viewer_id=2, token=invite["token"], display_name="Member")
    assert native.head(media).status_code == 404
    assert native.get(access).status_code == 404


@pytest.mark.parametrize("kind", ["movie", "tv"])
def test_shared_catalog_source_requires_the_viewers_own_plex_access(system, monkeypatch, kind):
    _, db, _, _ = system
    base, owner_token, member_token = "https://plex.invalid", "synthetic-owner", "synthetic-member"
    credentials = {1: owner_token, 2: member_token}
    accounts = {owner_token: 10, member_token: 20}
    connections = web_playback_catalog.connections
    monkeypatch.setattr(connections, "_values", lambda _db, user, _spec: (base, credentials[user]))
    monkeypatch.setattr(
        connections,
        "_read",
        lambda _db, user, key: credentials[user] if key == "plex_token" else "fixture-server",
    )
    shared = households.create_household(db, user_id=1, name="Shared Cinema")
    owner = households.resolve_scope(db, viewer_id=1, library_id=shared)
    canonical, season, episode = ("tt1234567", 0, 0) if kind == "movie" else ("42", 1, 1)
    with db:
        db.execute(
            """INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,year,identity_verified,updated_at)
               VALUES(1,'tt1234567',100,'Shared film',2025,1,'fixture')"""
        )
        db.execute(
            """INSERT INTO movie_verifications(user_id,imdb_id,fingerprint,plex_key,selected_audio,
                        state,code,detail,http_status,bytes_read,verified_at,expires_at)
               VALUES(1,'tt1234567',?,'12','English','ready','ready','fixture',206,65536,'fixture','fixture')""",
            ("c" * 64,),
        )
        db.execute(
            """INSERT INTO tv_catalog VALUES
               (1,42,'{"title":"Shared series","tmdb_id":42,"imdb_id":"tt7654321"}','fixture')"""
        )
        db.execute("INSERT INTO tv_episodes VALUES(1,42,1,1,'{}',1,'manual',1,'fixture')")
        db.execute(
            "INSERT INTO tv_plex_snapshots VALUES(1,42,?,'11',1,'fixture')",
            (hashlib.sha256((base + "\0" + owner_token).encode()).hexdigest(),),
        )
        item = households.LibraryRepository(db, owner).add(
            domain=kind, canonical_id=canonical, title="Shared title", legacy_id=canonical
        )
    invite = households.create_invite(db, owner)
    households.accept_invite(db, viewer_id=2, token=invite["token"], display_name="Member")
    scope = households.resolve_scope(db, viewer_id=2, library_id=shared)
    if kind == "tv":
        detail = native_tv_test.episodes(db, scope, "tv", canonical, 0)["items"][0]
        assert not detail["watched"] and detail["watch_path"] == "/plus/watch/title/tv/42/1/1"
    section, attributes, ids = (
        (
            "2",
            'type="movie" year="2025" librarySectionTitle="Movies"',
            '<Guid id="imdb://tt1234567"/><Guid id="tmdb://100"/>',
        )
        if kind == "movie"
        else ("3", 'type="episode" grandparentRatingKey="11" parentIndex="1" index="1"', "")
    )
    video = (
        f'<Video ratingKey="12" librarySectionID="{section}" {attributes} duration="30000">{ids}'
        '<Media id="10" container="mp4" videoCodec="h264" audioCodec="aac" width="1920" height="1080">'
        '<Part id="1" size="100000" key="/library/parts/1/123/file.mp4" file="/media/fixture.mp4"/>'
        "</Media></Video>"
    )
    reads = []

    def provider(request):
        path = request.url.path
        token = request.headers["x-plex-token"]
        reads.append((path, token))
        if path == "/api/v2/user":
            return httpx.Response(200, json={"id": accounts[token]})
        assert token == member_token
        if path == "/identity":
            return httpx.Response(200, text='<MediaContainer machineIdentifier="fixture-server"/>')
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
                '<Guid id="tmdb://42"/><Guid id="imdb://tt7654321"/></Directory></MediaContainer>',
            )
        assert path in {
            "/library/sections/2/all",
            "/library/metadata/12",
            "/library/metadata/11/allLeaves",
        }
        return httpx.Response(
            200,
            text=f'<MediaContainer librarySectionID="{section}" totalSize="1">{video}</MediaContainer>',
        )

    def resolve():
        return asyncio.run(
            web_playback_catalog.resolve_catalog_source(
                db,
                scope=scope,
                domain=kind,
                canonical_id=canonical,
                season=season,
                episode=episode,
                media_version=None,
                transport=httpx.MockTransport(provider),
            )
        )

    source = resolve()
    assert (
        source.actor_user_id == 2 and source.library_id == shared and source.token == member_token
    )
    assert [path for path, token in reads if token == owner_token] == ["/api/v2/user"]
    reads.clear()
    credentials[2] = owner_token
    with pytest.raises(web_playback_catalog.PlaybackSourceError):
        resolve()
    assert reads == []
    credentials[2] = member_token
    accounts[member_token] = accounts[owner_token]
    with pytest.raises(web_playback_catalog.PlaybackSourceError):
        resolve()
    assert reads == [("/api/v2/user", member_token), ("/api/v2/user", owner_token)]
    with db:
        db.execute("UPDATE library_items SET state='removed' WHERE id=?", (item,))
    with pytest.raises(web_playback_catalog.PlaybackSourceError):
        web_playback_catalog.revalidate_catalog_source(db, source)


def test_native_checkpoints_share_exact_resume_and_yield_to_newer_web_progress(system, monkeypatch):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    owner, csrf = browser(app, sessions[1])

    def linked(viewer, viewer_csrf):
        native = TestClient(app, base_url=ORIGIN)
        pairing = native.post(LIBRARY_API + "/device").json()
        reply = viewer.post(
            DEVICES_PAGE,
            data={
                "csrf_token": viewer_csrf,
                "action": "approve",
                "device_code": pairing["user_code"],
            },
        )
        assert "Apple TV connected" in reply.text
        native.headers["Authorization"] = "Bearer " + pairing["device_token"]
        return native

    native = linked(owner, csrf)
    other = linked(*browser(app, sessions[2]))
    target = store.ResolvedTarget(1, personal_scope(db, 1).library_id, "100", 1, 1, "a" * 64, 1800)
    snapshot = {
        "membership": {},
        "policy": {},
        "source": {"identity": "b" * 64, "media_version": "a" * 64},
    }
    web = store.create_session(db, target=target, binding_digest="c" * 64, snapshot=snapshot)
    web_saved = store.record_event(
        db,
        session_id=web["session_id"],
        user_id=1,
        binding_digest="c" * 64,
        seq=1,
        position_seconds=40,
        playing=False,
        event="seek",
    )
    playback = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"}).json()
    assert playback["position_seconds"] == 40
    url = f"{LIBRARY_API}/media/{playback['media_id']}/resume"
    sample = {"seq": 1, "position_seconds": 94.25}
    assert other.post(url, json=sample).status_code == 404
    assert native.post(url, json=sample | {"position_seconds": 1801}).status_code == 422
    response = native.post(url, json=sample)
    assert response.status_code == 200
    assert (
        playback["resume_sync"] is True and playback["resume_updated_at"] == web_saved["updated_at"]
    )
    assert response.headers["cache-control"] == "private, no-store"
    result = store.get_resume(db, target=target)
    assert result["position_seconds"] == 94.25 and result["covered_seconds"] == 0
    assert db.execute("SELECT count(*) FROM web_playback_watch").fetchone()[0] == 0
    assert native.post(url, json=sample).json() == response.json()
    assert native.post(url, json=sample | {"position_seconds": 42}).status_code == 409
    assert store.get_resume(db, target=target) == result
    web = store.create_session(db, target=target, binding_digest="c" * 64, snapshot=snapshot)
    assert web["position_seconds"] == 94.25
    store.record_event(
        db,
        session_id=web["session_id"],
        user_id=1,
        binding_digest="c" * 64,
        seq=1,
        position_seconds=111,
        playing=False,
        event="seek",
    )
    options.now += 6
    assert native.post(url, json={"seq": 2, "position_seconds": 100}).status_code == 409
    assert store.get_resume(db, target=target)["position_seconds"] == 111
    closed = native.post(f"{LIBRARY_API}/media/{playback['media_id']}/close")
    assert closed.status_code == 200
    assert native.post(url, json=sample).status_code == 404
    reopened = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"})
    assert reopened.status_code == 200
    assert reopened.json()["media_id"] != playback["media_id"]
    assert reopened.json()["identity"] == playback["identity"]
    assert reopened.json()["position_seconds"] == 111
    assert native.post(LIBRARY_API + "/unlink").status_code == 200
    assert native.post(url, json=sample).status_code == 401


def test_native_playback_credits_anime_watch_and_continue_watching(system, monkeypatch):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    owner, csrf = browser(app, sessions[1])
    native = TestClient(app, base_url=ORIGIN)
    pairing = native.post(LIBRARY_API + "/device").json()
    reply = owner.post(
        DEVICES_PAGE,
        data={
            "csrf_token": csrf,
            "action": "approve",
            "device_code": pairing["user_code"],
        },
    )
    assert "Apple TV connected" in reply.text
    native.headers["Authorization"] = "Bearer " + pairing["device_token"]
    playback = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"})
    assert playback.status_code == 200, playback.text
    media = playback.json()
    resume_url = f"{LIBRARY_API}/media/{media['media_id']}/resume"
    early = native.post(resume_url, json={"seq": 1, "position_seconds": 700})
    assert early.status_code == 200, early.text
    assert db.execute("SELECT count(*) FROM watch_entries").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0
    options.now += 1
    saved = native.post(resume_url, json={"seq": 2, "position_seconds": 1740})
    assert saved.status_code == 200, saved.text
    entry = db.execute(
        "SELECT status,progress_episodes,last_source FROM watch_entries WHERE user_id=1"
    ).fetchone()
    assert entry is not None
    assert (entry["status"], entry["progress_episodes"], entry["last_source"]) == (
        "watching",
        1,
        "apple_tv",
    )
    event = db.execute("SELECT source,episode,kind FROM watch_events WHERE user_id=1").fetchone()
    assert event is not None and (event["source"], event["episode"], event["kind"]) == (
        "apple_tv",
        1,
        "progress",
    )
    outbox = db.execute(
        "SELECT action,progress_episodes,sync_state FROM tracker_sync_outbox WHERE user_id=1"
    ).fetchone()
    assert outbox is not None
    assert (outbox["action"], outbox["progress_episodes"]) == ("progress", 1)
    from omakase.plus.product_views import home_view
    from omakase.plus.viewing_history import viewing_history

    cards = home_view(db, user_id=1)["continue_watching"]
    assert cards and cards[0]["id"] == 1
    history = viewing_history(db, user_id=1, limit=10)["items"]
    assert history and history[0]["source_label"] == "Apple TV"
    assert history[0]["episode"] == 1 and history[0]["watched_at"] is not None


def test_native_next_preserves_current_pass_when_successor_preparation_fails(system, monkeypatch):
    app, db, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_NATIVE_TV_HLS", "1")
    candidates = {1: None, 2: None, 4: None}
    monkeypatch.setattr(web_playback_navigation, "_candidates", lambda *_: candidates)

    class UnavailableStream:
        closed = False

        def __init__(self, *_args, **_kwargs):
            pass

        async def ready(self):
            raise DeliveryError("Fixture failure", 503)

        async def close(self):
            UnavailableStream.closed = True

    monkeypatch.setattr(native_tv_test, "NativeHLS", UnavailableStream)

    def linked_client(actor):
        owner, csrf = browser(app, sessions[actor])
        client = TestClient(app, base_url=ORIGIN)
        pairing = client.post(LIBRARY_API + "/device").json()
        response = owner.post(
            DEVICES_PAGE,
            data={"action": "approve", "device_code": pairing["user_code"], "csrf_token": csrf},
        )
        assert "Apple TV connected" in response.text
        client.headers["Authorization"] = "Bearer " + pairing["device_token"]
        return client

    def read(play):
        return native.get(
            API + "/media/" + play["media_id"],
            headers={"Range": "bytes=1-4", "Cookie": COOKIE + "=" + play["media_token"]},
        )

    native, other = linked_client(1), linked_client(2)
    first = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"}).json()
    assert first["next_watch_path"] == "/plus/watch/1/2"
    next_url = f"{LIBRARY_API}/media/{first['media_id']}/next"
    assert other.post(next_url).status_code == 404

    options.container = "mkv"
    assert native.post(next_url).status_code == 503
    assert UnavailableStream.closed
    assert read(first).content == b"abcd"

    options.container = "mp4"
    abandoned = native.post(next_url).json()
    assert read(abandoned).content == b"abcd"
    second = native.post(next_url).json()
    assert read(abandoned).status_code == 404
    assert second["identity"]["episode_number"] == 2
    assert "next_watch_path" not in second
    assert native.post(f"{LIBRARY_API}/media/{second['media_id']}/next").status_code == 404
    assert read(first).content == b"abcd"
    assert native.post(f"{LIBRARY_API}/media/{first['media_id']}/close").status_code == 200
    assert read(first).status_code == 404
    assert read(second).content == b"abcd"
    assert db.execute("SELECT count(*) FROM web_playback_resumes").fetchone()[0] == 0


def test_library_anime_episode_names_use_exact_metadata_without_hiding_fallbacks(
    system, monkeypatch
):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    owner, csrf = browser(app, sessions[1])
    native = TestClient(app, base_url=ORIGIN)
    link = re.search(
        r"https://testserver#library-([A-F0-9]{20})",
        owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"}).text,
    )[1]
    native.headers["Authorization"] = (
        "Bearer " + native.post(LIBRARY_API + "/redeem", json={"code": link}).json()["token"]
    )

    def fixture_episodes(_db, scope, kind, key, offset):
        assert (scope.viewer_id, kind, key, offset) == (1, "anime", "1", 0)
        return {
            "items": [
                {
                    "id": str(n),
                    "title": f"Episode {n}",
                    "episode_artwork": f"episode-artwork/anime/1/1/{n}",
                }
                for n in (1, 2)
            ],
            "next_offset": None,
        }

    def fixture_identity(_db, scope, kind, monitor, season, episode):
        assert (scope.viewer_id, kind, monitor, season, episode) == (1, "anime", 1, 1, 1)
        return "exact season"

    async def fixture_metadata(_db, identity):
        assert identity == "exact season"
        return {
            1: {
                "title": "Arrival",
                "summary": "A keeper follows a signal.",
                "duration_seconds": 1441,
            },
            3: {"title": "Outside this page"},
        }

    monkeypatch.setattr(native_tv_test, "episodes", fixture_episodes)
    monkeypatch.setattr(native_tv_test, "episode_image_identity", fixture_identity)
    monkeypatch.setattr(native_tv_test, "anime_series_metadata", fixture_metadata)
    url = LIBRARY_API + "/episodes?kind=anime&title_id=1"
    response = native.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert [item["title"] for item in response.json()["items"]] == ["Arrival", "Episode 2"]
    assert response.json()["items"][0]["summary"] == "A keeper follows a signal."
    assert response.json()["items"][0]["duration_seconds"] == 1441
    assert "duration_seconds" not in response.json()["items"][1]

    async def unavailable(_db, _identity):
        raise native_tv_test.EpisodeArtworkUnavailable()

    monkeypatch.setattr(native_tv_test, "anime_series_metadata", unavailable)
    assert [item["title"] for item in native.get(url).json()["items"]] == ["Episode 1", "Episode 2"]


def test_native_tv_next_requires_a_visible_exact_same_season_episode(system):
    _, db, _, _ = system
    for actor in (1, 2):
        db.execute("INSERT INTO tv_catalog VALUES (?,42,'{}','fixture')", (actor,))
        db.execute(
            "INSERT INTO tv_plex_snapshots VALUES (?,42,'fixture','12',1,'fixture')", (actor,)
        )
    db.executemany(
        """INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,
                   metadata_json,plex_present,updated_at) VALUES (?,42,?,?,'{}',?,'fixture')""",
        [(1, 1, 1, 1), (1, 1, 2, 0), (1, 1, 3, 1), (1, 2, 1, 1), (2, 1, 2, 1)],
    )
    source = SimpleNamespace(
        _scope=personal_scope(db, 1),
        domain="tv",
        canonical_id="42",
        season_number=1,
        episode_number=1,
    )
    assert native_tv_test.next_watch_path(db, source, anime_monitor_id=None) is None
    db.execute("UPDATE tv_episodes SET plex_present=1 WHERE user_id=1 AND episode_number=2")
    assert (
        native_tv_test.next_watch_path(db, source, anime_monitor_id=None)
        == "/plus/watch/title/tv/42/1/2"
    )
    source.episode_number = 3
    assert native_tv_test.next_watch_path(db, source, anime_monitor_id=None) is None


def test_tv_code_approval_is_single_use_and_browser_logout_revokes_device(system, monkeypatch):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    owner, csrf = browser(app, sessions[1])
    other, other_csrf = browser(app, sessions[2])
    native = TestClient(app, base_url=ORIGIN)
    device_url = LIBRARY_API + "/device"
    pairing = native.post(device_url).json()
    token, device_code = pairing["device_token"], pairing["user_code"]
    native.headers["Authorization"] = "Bearer " + token
    assert native.get(device_url).status_code == 202
    catalog_url = LIBRARY_API + "/catalog?kind=anime"
    assert native.get(catalog_url).status_code == 401
    approval = {"action": "approve", "device_code": device_code}
    assert device_code in owner.get(LIBRARY_PAGE, params={"code": device_code}).text
    assert owner.post(LIBRARY_PAGE, data=approval).status_code == 403
    assert (
        "Apple TV connected" in owner.post(LIBRARY_PAGE, data=approval | {"csrf_token": csrf}).text
    )
    assert (
        "Check the code"
        in other.post(LIBRARY_PAGE, data=approval | {"csrf_token": other_csrf}).text
    )
    grant = native.get(device_url)
    assert grant.status_code == 200
    assert 2_591_990 <= grant.json()["expires_in"] <= 2_592_000
    stored = db.execute("SELECT token_digest,code_digest FROM native_tv_devices").fetchone()
    assert stored["token_digest"] != token and stored["code_digest"] is None
    assert native.get(catalog_url + "&q=fixture").json()["items"][0]["title"] == "Fixture episode"
    assert not native.get(catalog_url + "&q=missing").json()["items"]
    assert (
        other.post(LIBRARY_PAGE, data={"action": "revoke", "csrf_token": other_csrf}).status_code
        == 200
    )
    assert native.get(catalog_url).status_code == 200
    assert native.get(LIBRARY_API + "/artwork/2/poster").status_code == 404
    delete_session(db, sessions[1])
    assert native.get(catalog_url).status_code == 401
    assert native.get(device_url).status_code == 401


def test_devices_page_lists_and_revokes_only_the_selected_device(system, monkeypatch):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    household = households.create_household(db, user_id=1, name="The Home Cinema")
    with db:
        db.execute(
            "UPDATE household_memberships SET display_name=? WHERE library_id=? AND user_id=1",
            ("Living room profile", household),
        )
    owner, csrf = browser(app, sessions[1])
    other, other_csrf = browser(app, sessions[2])
    native = TestClient(app, base_url=ORIGIN, client=("93.184.216.34", 10000))
    device_url = LIBRARY_API + "/device"
    anonymous = native.get(
        DEVICES_PAGE,
        params={"code": "ABCDEFGH"},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )
    assert anonymous.status_code == 303 and anonymous.headers["location"] == "/plus/login"
    assert "omakase_device_pairing=ABCDEFGH" in anonymous.headers["set-cookie"]

    invalid_kind = native.post(device_url, params={"kind": "browser"})
    assert invalid_kind.status_code == 422
    assert db.execute("SELECT count(*) FROM native_tv_devices").fetchone()[0] == 0

    def link(name, *, kind=None):
        params = {"kind": kind} if kind is not None else {}
        pairing = native.post(device_url, params=params).json()
        response = owner.post(
            DEVICES_PAGE,
            data={
                "action": "approve",
                "device_code": pairing["user_code"],
                "device_name": name,
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303 and response.headers["location"] == DEVICES_PAGE
        return pairing["device_token"]

    first = link("Living room")
    tv_page = owner.get(DEVICES_PAGE)
    assert "Apple TV connected" in tv_page.text
    second = link("", kind="iphone")
    devices_page = owner.get(DEVICES_PAGE)
    assert "2 linked" in devices_page.text
    assert "iPhone connected" in devices_page.text
    assert first not in devices_page.text and second not in devices_page.text
    assert "Awaiting first connection" in devices_page.text
    assert "<dt>Masked IP</dt><dd>Unavailable</dd>" in devices_page.text
    assert "93.184.216.0/24" not in devices_page.text
    spoofed = {"CF-Connecting-IP": "1.1.1.1", "X-Forwarded-For": "8.8.8.8"}
    assert native.get(device_url, headers=spoofed).status_code == 401
    assert (
        native.get(
            LIBRARY_API + "/catalog?kind=anime",
            headers={"Authorization": "Bearer " + first, **spoofed},
        ).status_code
        == 200
    )
    seen = owner.get(DEVICES_PAGE).text
    first_row = next(row for row in seen.split("</li>") if "<strong>Living room</strong>" in row)
    second_row = next(row for row in seen.split("</li>") if "<strong>iPhone</strong>" in row)
    assert "Apple TV · Linked by you" in first_row
    assert "iPhone · Linked by you" in second_row
    assert "Living room profile · The Home Cinema" in first_row
    assert "<dt>Last seen</dt><dd><time" in first_row
    assert "<dt>Masked IP</dt><dd>93.184.216.0/24</dd>" in first_row
    assert "Awaiting first connection" in second_row
    assert all(host not in seen for host in ("93.184.216.34", "1.1.1.1", "8.8.8.8"))
    first_reference = re.search(r'name="device_reference" value="([a-f0-9]{32})"', first_row)[1]
    assert "iPhone connected" not in seen
    other_devices = other.get(DEVICES_PAGE).text
    assert all(
        label not in other_devices
        for label in ("<strong>Living room</strong>", "The Home Cinema", "93.184.216.0/24")
    )
    revoke = {"action": "disconnect", "device_reference": first_reference}
    assert other.post(DEVICES_PAGE, data=revoke | {"csrf_token": other_csrf}).status_code == 200
    assert native.get(device_url, headers={"Authorization": "Bearer " + first}).status_code == 200
    assert owner.post(DEVICES_PAGE, data=revoke).status_code == 403
    result = owner.post(DEVICES_PAGE, data=revoke | {"csrf_token": csrf})
    assert "Device disconnected." in result.text and "<strong>iPhone</strong>" in result.text
    assert "<strong>Living room</strong>" not in result.text
    assert native.get(device_url, headers={"Authorization": "Bearer " + first}).status_code == 401
    assert native.get(device_url, headers={"Authorization": "Bearer " + second}).status_code == 200

    unlink_url = LIBRARY_API + "/unlink"
    assert native.post(unlink_url).status_code == 401
    assert native.post(unlink_url, headers={"Authorization": "Bearer " + first}).json() == {
        "unlinked": True
    }
    assert native.get(device_url, headers={"Authorization": "Bearer " + second}).status_code == 200
    for _ in range(2):
        response = native.post(unlink_url, headers={"Authorization": "Bearer " + second})
        assert response.status_code == 200 and response.json() == {"unlinked": True}
    assert "No devices linked" in owner.get(DEVICES_PAGE).text

    response = owner.post(
        DEVICES_PAGE, data={"action": "issue", "csrf_token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"] == DEVICES_PAGE
    manual = owner.get(DEVICES_PAGE)
    manual_code = re.search(r"https://testserver#library-([A-F0-9]{20})", manual.text)[1]
    assert manual_code in owner.get(DEVICES_PAGE).text
    assert manual_code not in other.get(DEVICES_PAGE).text
    assert native.post(LIBRARY_API + "/redeem", json={"code": manual_code}).status_code == 200
    assert manual_code not in owner.get(DEVICES_PAGE).text
    assert "Temporary access" in owner.get(DEVICES_PAGE).text


def test_household_device_sharing_identifies_the_member_without_granting_access(
    system, monkeypatch
):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    household = households.create_household(db, user_id=1, name="Home cinema")

    def scope():
        return households.resolve_scope(db, viewer_id=1, library_id=household)

    def invite(actor, name):
        invitation = households.create_invite(db, scope())
        households.accept_invite(db, viewer_id=actor, token=invitation["token"], display_name=name)

    invite(2, "Morgan")
    with db:
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(3,'peer@example.invalid','fixture')"
        )
    invite(3, "Morgan")
    owner, owner_csrf = browser(app, sessions[1])
    member, member_csrf = browser(app, sessions[2])
    peer, peer_csrf = browser(app, create_session(db, 3))
    native = TestClient(app, base_url=ORIGIN, client=("93.184.216.34", 10000))
    device_url = LIBRARY_API + "/device"

    def link(client, csrf, name, *, kind="apple-tv"):
        pairing = native.post(device_url, params={"kind": kind}).json()
        credential = pairing["device_token"]
        linked = client.post(
            DEVICES_PAGE,
            data={
                "action": "approve",
                "device_code": pairing["user_code"],
                "device_name": name,
                "csrf_token": csrf,
            },
        )
        label = "iPhone" if kind == "iphone" else "Apple TV"
        assert f"{label} connected" in linked.text
        assert (
            native.get(device_url, headers={"Authorization": "Bearer " + credential}).status_code
            == 200
        )
        return credential, native_tv_devices.device_reference(native_tv_devices.digest(credential))

    token, reference = link(member, member_csrf, "Den screen")
    headers = {"Authorization": "Bearer " + token}
    assert "Den screen" not in owner.get(DEVICES_PAGE).text

    def change(client, csrf, action, ref=reference):
        return client.post(
            DEVICES_PAGE,
            data={"action": action, "device_reference": ref, "csrf_token": csrf},
        )

    assert "Device is visible to the household owner." in change(member, member_csrf, "share").text
    _, phone = link(member, member_csrf, "Morgan’s phone", kind="iphone")
    _, office = link(peer, peer_csrf, "Office TV")
    change(member, member_csrf, "share", phone)
    change(peer, peer_csrf, "share", office)
    shared = owner.get(DEVICES_PAGE)
    assert "Household devices" in shared.text
    assert "3 shared" in shared.text
    groups = re.findall(r'<section class="device-member-group".*?</section>', shared.text, re.S)
    assert len(groups) == 2 and all(
        "Morgan <span>· Home cinema</span>" in group for group in groups
    )
    assert all(
        name in groups[0] for name in ("Den screen", "Morgan’s phone", "<span>iPhone</span>")
    )
    assert "Office TV" in groups[1] and "Office TV" not in groups[0]
    assert "<dt>Masked IP</dt><dd>93.184.216.0/24</dd>" in shared.text
    assert "Disconnect Den screen" not in shared.text
    assert "Rename Den screen" not in shared.text
    assert all(value not in shared.text for value in (token, reference, "93.184.216.34"))
    assert shared.headers["cache-control"] == "private, no-store"
    assert "Den screen" not in peer.get(DEVICES_PAGE).text
    assert "already disconnected" in change(owner, owner_csrf, "disconnect").text
    assert "Device sharing is unavailable" in change(peer, peer_csrf, "unshare").text
    rename = {"action": "rename", "device_reference": reference, "device_name": "Cabin TV"}
    assert member.post(DEVICES_PAGE, data=rename).status_code == 403
    assert (
        "already disconnected"
        in owner.post(DEVICES_PAGE, data=rename | {"csrf_token": owner_csrf}).text
    )
    renamed = member.post(DEVICES_PAGE, data=rename | {"csrf_token": member_csrf})
    assert "Device name updated." in renamed.text and "Shared with owner" in renamed.text
    assert "<strong>Cabin TV</strong>" in owner.get(DEVICES_PAGE).text
    assert native.get(device_url, headers=headers).status_code == 200

    assert (
        "Device is hidden from the household owner." in change(member, member_csrf, "unshare").text
    )
    assert "Cabin TV" not in owner.get(DEVICES_PAGE).text
    change(member, member_csrf, "share")
    households.leave_or_revoke(db, scope(), member_id=2)
    assert "Cabin TV" not in owner.get(DEVICES_PAGE).text
    invite(2, "Morgan again")
    assert "Cabin TV" not in owner.get(DEVICES_PAGE).text
    assert "Cabin TV" in member.get(DEVICES_PAGE).text


def test_tv_profile_and_avatar_belong_to_the_linked_account(system, monkeypatch):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    picture = b"normalized synthetic png"
    db.execute(
        "INSERT INTO user_avatars(user_id,image_bytes,revision) VALUES (?,?,?)",
        (1, picture, "a" * 64),
    )
    db.commit()
    native = TestClient(app, base_url=ORIGIN)
    profile = LIBRARY_API + "/profile"
    assert native.get(profile).status_code == 401
    assert native.get(profile + "/avatar").status_code == 401
    for actor in (1, 2):
        owner, csrf = browser(app, sessions[actor])
        pairing = native.post(LIBRARY_API + "/device").json()
        assert (
            owner.post(
                DEVICES_PAGE,
                data={"action": "approve", "device_code": pairing["user_code"], "csrf_token": csrf},
                follow_redirects=False,
            ).status_code
            == 303
        )
        auth = {"Authorization": "Bearer " + pairing["device_token"]}
        response = native.get(profile, headers=auth)
        assert response.status_code == 200
        assert response.json() == {
            "id": str(actor),
            "name": "Profile",
            "has_avatar": actor == 1,
        }
        avatar_response = native.get(profile + "/avatar", headers=auth)
        if actor == 1:
            assert avatar_response.status_code == 200 and avatar_response.content == picture
            assert avatar_response.headers["content-type"] == "image/png"
        else:
            assert avatar_response.status_code == 404
        assert native.post(LIBRARY_API + "/unlink", headers=auth).status_code == 200
        assert native.get(profile, headers=auth).status_code == 401


@pytest.mark.parametrize(
    ("codec", "caption_mode", "duration_seconds"),
    [
        ("h264", "disabled", 1800),
        ("h264", "indexed", 1800),
        ("h264", "unusable", 1800),
        ("hevc", "disabled", 7200),
        ("vc1", "disabled", 7200),
    ],
    ids=["no-captions", "indexed-captions", "unusable-captions", "long-hevc", "long-vc1"],
)
def test_tv_plex_hls_scopes_segments_and_releases_transcode_on_unlink(
    system, monkeypatch, codec, caption_mode, duration_seconds
):
    app, _, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_NATIVE_TV_PLEX_HLS", "1")
    options.container = "mkv"
    options.video_codec = codec
    options.audio_codec = "dca"
    options.duration_ms = duration_seconds * 1000
    long_session = duration_seconds > 3600
    last_segment = "00721.ts" if long_session else "00060.ts"
    expected_offsets = ["0.0", "290.0", "3595.0"] if long_session else ["0.0", "290.0"]
    segment_data = b"transport segment"
    if caption_mode != "disabled":
        monkeypatch.setenv("OMAKASE_NATIVE_TV_INDEXED_CAPTIONS", "1")
        options.part_bytes = (
            indexed_caption_fixture(media_tracks=True)
            if caption_mode == "indexed"
            else b"part without a usable caption index"
        )
        options.streams += (PlexStream("7", "subtitle", "srt", "en", False, False, False, 2),)
        pts = 900_000
        encoded_pts = bytes(
            (
                0x21 | ((pts >> 29) & 0x0E),
                (pts >> 22) & 0xFF,
                1 | ((pts >> 14) & 0xFE),
                (pts >> 7) & 0xFF,
                1 | ((pts << 1) & 0xFE),
            )
        )
        segment_data = (
            b"\x47\x41\x00\x10\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + encoded_pts
        ).ljust(188, b"\xff")
    openings = []
    stops = []
    unlinked = Event()

    class Bytes(httpx.AsyncByteStream):
        def __init__(self, data):
            self.data = data

        async def __aiter__(self):
            yield self.data

    def transcode(request):
        if request.url.path == "/library/parts/12/1234/file.mp4":
            assert caption_mode != "disabled"
            start, end = map(int, request.headers["range"].removeprefix("bytes=").split("-"))
            part = options.part_bytes[start : end + 1]
            return httpx.Response(
                206,
                stream=Bytes(part),
                headers={
                    "content-range": f"bytes {start}-{end}/{len(options.part_bytes)}",
                    "content-length": str(len(part)),
                },
            )
        prefix = "/video/:/transcode/universal/"
        assert request.url.path.startswith(prefix)
        path = request.url.path.removeprefix(prefix)
        session_id = request.headers["x-plex-session-identifier"]
        base = f"session/{session_id}/base/"
        if path == "decision":
            assert request.url.params["path"] == "/library/metadata/8"
            assert request.url.params["protocol"] == "hls"
            assert request.url.params["directStream"] == "0"
            assert request.url.params["videoResolution"] == "1920x1080"
            assert request.url.params["maxVideoBitrate"] == "12000"
            assert request.url.params["subtitles"] == "none"
            return httpx.Response(
                200,
                stream=Bytes(
                    b'<MediaContainer><Video ratingKey="8"><Media><Part id="12">'
                    b'<Stream streamType="1" decision="transcode" codec="h264"/>'
                    b'<Stream streamType="2" decision="transcode" codec="aac"/>'
                    b"</Part></Media></Video></MediaContainer>"
                ),
            )
        if path == "start.m3u8":
            openings.append((session_id, request.url.params["offset"]))
            return httpx.Response(
                200,
                stream=Bytes(
                    f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n{base}index.m3u8\n".encode()
                ),
            )
        if path == base + "index.m3u8":
            _, offset = openings[-1]
            start = f"#EXT-X-START:TIME-OFFSET={float(offset):.6f}\n" if float(offset) else ""
            header = f"#EXTM3U\n#EXT-X-TARGETDURATION:5\n{start}#EXT-X-ALLOW-CACHE:NO\n#EXT-X-MEDIA-SEQUENCE:0\n"
            segments = "".join(
                f"#EXTINF:5, nodesc\n{number:05d}.ts\n" for number in range(duration_seconds // 5)
            )
            return httpx.Response(
                200, stream=Bytes((header + segments + "#EXT-X-ENDLIST\n").encode())
            )
        if path == "stop":
            assert request.url.params["session"] == session_id
            stops.append(session_id)
            if len(stops) == len(expected_offsets):
                unlinked.set()
            return httpx.Response(200)
        assert path in {base + "00000.ts", base + "00060.ts", base + last_segment}
        assert session_id == openings[-1][0]
        return httpx.Response(200, stream=Bytes(segment_data))

    options.transcode = transcode
    with TestClient(app, base_url=ORIGIN) as native:
        owner, csrf = browser(app, sessions[1])
        pairing = native.post(LIBRARY_API + "/device").json()
        approval = owner.post(
            LIBRARY_PAGE,
            data={"action": "approve", "device_code": pairing["user_code"], "csrf_token": csrf},
        )
        assert "Apple TV connected" in approval.text
        native.headers["Authorization"] = "Bearer " + pairing["device_token"]
        play = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"})
        assert play.status_code == 200, play.text
        grant = play.json()
        assert grant["media_format"] == "hls"
        assert grant["media_playlist"] == "vod"
        assert grant["expires_in"] == duration_seconds + 600
        assert grant["episode_name"] == "Arrival"
        assert grant["episode_summary"] == "The team returns to the station."
        assert "audio_languages" not in grant
        assert grant.get("subtitle_languages", []) == (
            ["English"] if caption_mode == "indexed" else []
        )
        path = API + "/media/" + grant["media_id"]
        assert native.get(path + "/index.m3u8").status_code == 404
        assert native.get(path + "/captions-0-00001.vtt").status_code == 404
        native.cookies.set(COOKIE, grant["media_token"])
        playlist = native.get(path + "/index.m3u8")
        assert playlist.status_code == 200
        if caption_mode == "indexed":
            assert "TYPE=SUBTITLES" in playlist.text and 'URI="captions-0.m3u8"' in playlist.text
            assert "captions-0-00001.vtt\n" in native.get(path + "/captions-0.m3u8").text
            fragment = native.get(path + "/captions-0-00001.vtt")
            assert fragment.status_code == 200
            assert "00:00:01.500 --> 00:00:05.500" in fragment.text
            assert "MPEGTS:900000" in fragment.text
            playlist = native.get(path + "/video.m3u8")
        else:
            assert "TYPE=SUBTITLES" not in playlist.text
        assert "00060.ts\n" in playlist.text
        assert (
            openings[0][0] not in playlist.text and options.fixture_credential not in playlist.text
        )
        assert native.get(path + f"/{duration_seconds // 5:05d}.ts").status_code == 404
        first = native.get(path + "/00000.ts")
        assert first.headers["content-type"] == "video/mp2t"
        assert first.content == segment_data
        assert native.get(path + "/00060.ts").content == segment_data
        if long_session:
            options.now += 3601
            media_session = f"{LIBRARY_API}/media/{grant['media_id']}"
            assert native.get(media_session + "/access").status_code == 200
            assert native.get(path + "/" + last_segment).content == segment_data
            checkpoint = native.post(
                media_session + "/resume", json={"seq": 1, "position_seconds": 3605}
            )
            assert checkpoint.status_code == 200
        assert [offset for _, offset in openings] == expected_offsets
        assert stops == [session_id for session_id, _ in openings[:-1]]
        assert native.post(LIBRARY_API + "/unlink").status_code == 200
        assert unlinked.wait(2)
        assert stops == [session_id for session_id, _ in openings]
        assert native.get(path + "/" + last_segment).status_code == 404
        assert native.get(path + "/captions-0-00001.vtt").status_code == 404


def test_native_hls_rejects_partial_master_even_after_the_first_segment(tmp_path):
    stream = object.__new__(native_tv_hls.NativeHLS)
    stream.folder = tmp_path
    stream.renditions = True
    stream.video_playlist = "media-video.m3u8"
    stream.first_segment = "part-video-00000.m4s"
    (tmp_path / stream.first_segment).touch()
    index = tmp_path / "index.m3u8"
    partial = '#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,URI="media-video_vtt.m3u8"\n'

    index.write_text(partial, encoding="ascii")
    assert not stream._index_ready()

    variant = "#EXT-X-STREAM-INF:BANDWIDTH=100000\n"
    index.write_text(partial + variant, encoding="ascii")
    assert not stream._index_ready()

    index.write_text(partial + variant + "media-video.m3u8\n", encoding="ascii")
    assert stream._index_ready()


@pytest.mark.parametrize(
    "rename_during_scan,renditions",
    [(False, False), (True, False), (False, True)],
    ids=["normal", "atomic-rename", "audio-and-subtitles"],
)
def test_tv_hls_serves_private_segments_for_a_real_synthetic_matroska(
    system, monkeypatch, tmp_path, rename_during_scan, renditions
):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is required for synthetic native HLS")
    app, _, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_NATIVE_TV_HLS", "1")
    monkeypatch.setenv("OMAKASE_DATA_DIR", str(tmp_path))
    renamed = False
    if rename_during_scan:
        original_iterdir = Path.iterdir

        def iterdir(folder):
            nonlocal renamed
            if not renamed and folder.parent == tmp_path / ".native-tv-hls":
                temporary = folder / "rename-probe.tmp"
                temporary.write_bytes(b"fragment")
                yield temporary
                temporary.replace(folder / "rename-probe.m4s")
                renamed = True
            yield from original_iterdir(folder)

        monkeypatch.setattr(Path, "iterdir", iterdir)
        # Exercise the storage scan with a short synthetic clip.
        monkeypatch.setattr(native_tv_hls, "_MIB", 256)
    sample = tmp_path / "synthetic.mkv"
    inputs = []
    mapping = []
    if renditions:
        captions = tmp_path / "fixture.srt"
        captions.write_text(
            "1\n00:00:07,750 --> 00:00:08,250\nQualification subtitle.\n", encoding="utf-8"
        )
        french = tmp_path / "fixture-fr.srt"
        french.write_text(
            "1\n00:00:09,000 --> 00:00:09,500\nSous-titre de qualification.\n", encoding="utf-8"
        )
        inputs = ["-f", "lavfi", "-i", "sine=frequency=880", "-i", str(captions), "-i", str(french)]
        mapping = [
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-map",
            "3:s",
            "-map",
            "4:s",
            "-c:s",
            "srt",
        ]
        options.streams += (
            PlexStream("3", "audio", "aac", "ja", False, False, False, 2),
            PlexStream("99", "subtitle", "srt", "fr", True, False, True),
            PlexStream("4", "subtitle", "srt", "en", False, False, False, 3),
            PlexStream("5", "subtitle", "srt", "fr", False, False, False, 4, role="sdh"),
        )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "color=c=navy:s=320x180:r=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            *inputs,
            *mapping,
            "-t",
            "16" if renditions else "2",
            "-c:v",
            "libx264",
            "-g",
            "24",
            "-c:a",
            "aac",
            str(sample),
        ],
        check=True,
        timeout=30,
    )
    options.container = "mkv"
    options.part_bytes = sample.read_bytes()
    owner, csrf = browser(app, sessions[1])
    with TestClient(app, base_url=ORIGIN) as native:
        pairing = native.post(LIBRARY_API + "/device").json()
        approval = owner.post(
            LIBRARY_PAGE,
            data={"action": "approve", "device_code": pairing["user_code"], "csrf_token": csrf},
        )
        assert "Apple TV connected" in approval.text
        native.headers["Authorization"] = "Bearer " + pairing["device_token"]
        play = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"})
        assert renamed == rename_during_scan
        assert play.status_code == 200, play.text
        grant = play.json()
        assert grant["media_format"] == "hls"
        assert len(list((tmp_path / ".native-tv-hls").glob("omakase-tv-hls-*/index.m3u8"))) == 1
        assert options.fixture_credential not in play.text
        path = API + "/media/" + grant["media_id"]
        assert native.get(path + "/index.m3u8").status_code == 404
        native.cookies.set(COOKIE, grant["media_token"])
        playlist = native.get(path + "/index.m3u8")
        assert playlist.status_code == 200
        assert playlist.headers["content-type"] == "application/vnd.apple.mpegurl"
        assert "https://" not in playlist.text
        if renditions:
            assert grant["audio_languages"] == ["English", "Japanese"]
            assert grant["subtitle_languages"] == ["English", "French"]
            assert playlist.text.count("TYPE=AUDIO") == 2
            assert 'LANGUAGE="en"' in playlist.text and 'LANGUAGE="ja"' in playlist.text
            tracks = [line for line in playlist.text.splitlines() if "TYPE=SUBTITLES" in line]
            assert len(tracks) == 2
            english = next(line for line in tracks if 'LANGUAGE="en"' in line)
            french = next(line for line in tracks if 'LANGUAGE="fr"' in line)
            assert 'NAME="French (SDH)"' in french
            assert "public.accessibility.describes-music-and-sound" in french
            for name in ("media-a0.m3u8", "media-a1.m3u8", "media-video.m3u8"):
                assert name in playlist.text and native.get(f"{path}/{name}").status_code == 200
            video_index = native.get(path + "/media-video.m3u8")
            assert "#EXT-X-ENDLIST" in video_index.text
            video_windows = re.findall(r"(?m)^#EXTINF:([^,]+),", video_index.text)
            assert len(video_windows) == 4
            header = "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:0\n\n"
            by_language = {}
            for language, tag in (("en", english), ("fr", french)):
                uri = re.search(r'URI="([^"]+)"', tag)[1]
                index = native.get(f"{path}/{uri}")
                assert index.status_code == 200 and "#EXT-X-ENDLIST" in index.text
                assert re.findall(r"(?m)^#EXTINF:([^,]+),", index.text) == video_windows
                names = [line for line in index.text.splitlines() if line.endswith(".vtt")]
                assert len(names) == 4
                with TestClient(app, base_url=ORIGIN) as anonymous:
                    assert anonymous.get(f"{path}/{names[2]}").status_code == 404
                fragments = [native.get(f"{path}/{name}") for name in names]
                for fragment in fragments:
                    assert fragment.status_code == 200
                    assert fragment.headers["content-type"].startswith("text/vtt")
                by_language[language] = [fragment.text for fragment in fragments]
            en, fr = by_language["en"], by_language["fr"]
            assert "Qualification subtitle." in en[1]
            assert en[1] == en[2], "seeking after the cut must include the same active cue"
            assert en[0] == en[3] == fr[0] == fr[1] == fr[3] == header
            assert all(fragment.startswith(header) for fragment in en + fr)
            assert "Sous-titre de qualification." in fr[2]
            assert "Qualification subtitle." not in fr[2]
            assert "Sous-titre de qualification." not in en[2]
            assert native.get(path + "/captions-1-99999.vtt").status_code == 404
            segment_name = "part-video-00000.m4s"
        else:
            assert '#EXT-X-MAP:URI="init.mp4"' in playlist.text
            assert "part-00000.m4s" in playlist.text
            segment_name = "part-00000.m4s"
        segment = native.get(f"{path}/{segment_name}", headers={"Range": "bytes=0-9"})
        assert segment.status_code == 206 and len(segment.content) == 10
        assert native.get(path + "/source.mkv").status_code == 404
        closed = native.post(LIBRARY_API + "/media/" + grant["media_id"] + "/close")
        assert closed.status_code == 200
        assert native.get(path + "/index.m3u8").status_code == 404


@pytest.mark.parametrize(
    ("transfer", "expected_range", "alternate"),
    [
        pytest.param("smpte2084", "PQ", True, id="hdr10-alternate-audio"),
        pytest.param("smpte2084", "PQ", False, id="hdr10-one-audio"),
        pytest.param("arib-std-b67", "HLG", False, id="hlg-one-audio"),
        pytest.param("bt709", None, False, id="sdr-one-audio"),
    ],
)
def test_tv_hls_keeps_hevc_and_surround_through_the_authenticated_route(
    system, monkeypatch, tmp_path, transfer, expected_range, alternate
):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg and ffprobe are required for the native HDR stream")
    encoders = subprocess.check_output([ffmpeg, "-hide_banner", "-encoders"], timeout=5)
    if b"libx265" not in encoders or b" eac3 " not in encoders:
        pytest.skip("the synthetic HDR stream needs libx265 and E-AC-3 encoding")

    sample = tmp_path / "hevc.mkv"
    tones = "|".join(f"0.08*sin(2*PI*{tone}*t)" for tone in (440, 550, 660, 80, 770, 880))
    primaries, matrix = ("bt2020", "bt2020nc") if expected_range else ("bt709", "bt709")
    encoder = f"repeat-headers=1:colorprim={primaries}:transfer={transfer}:colormatrix={matrix}:"
    if expected_range == "PQ":
        encoder += (
            "hdr10=1:master-display=G(13250,34500)B(7500,3000)R(34000,16000)"
            "WP(15635,16450)L(10000000,50):max-cll=1000,400:"
        )
    encoder += "pools=1:frame-threads=1:log-level=error"
    stereo = ["-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000"] if alternate else []
    stereo_map = (
        ["-map", "2:a", "-c:a:1", "aac", "-b:a:1", "128k", "-ac:a:1", "2"] if alternate else []
    )
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=24",
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc={tones}:s=48000:c=5.1",
            *stereo,
            "-map",
            "0:v",
            "-map",
            "1:a",
            *stereo_map,
            "-t",
            "6",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-g",
            "24",
            "-pix_fmt",
            "yuv420p10le",
            "-x265-params",
            encoder,
            "-color_primaries",
            primaries,
            "-color_trc",
            transfer,
            "-colorspace",
            matrix,
            "-c:a:0",
            "eac3",
            "-b:a:0",
            "448k",
            str(sample),
        ],
        check=True,
        timeout=30,
    )
    app, _, sessions, options = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setenv("OMAKASE_NATIVE_TV_HLS", "1")
    monkeypatch.setenv("OMAKASE_DATA_DIR", str(tmp_path))
    options.container, options.video_codec, options.audio_codec = "mkv", "hevc", "eac3"
    options.duration_ms = 6000
    options.part_bytes = sample.read_bytes()
    options.streams = (
        PlexStream("1", "video", "hevc", "", False, False, False, 0),
        PlexStream("2", "audio", "eac3", "en", True, False, True, 1),
    )
    if alternate:
        options.streams += (PlexStream("3", "audio", "aac", "ja", False, False, False, 2),)
    owner, csrf = browser(app, sessions[1])
    with TestClient(app, base_url=ORIGIN) as native:
        pairing = native.post(LIBRARY_API + "/device").json()
        approval = owner.post(
            LIBRARY_PAGE,
            data={"action": "approve", "device_code": pairing["user_code"], "csrf_token": csrf},
        )
        assert "Apple TV connected" in approval.text
        native.headers["Authorization"] = "Bearer " + pairing["device_token"]
        play = native.post(LIBRARY_API + "/play", json={"watch_path": "/plus/watch/1/1"})
        assert play.status_code == 200, play.text
        grant = play.json()
        path = f"{API}/media/{grant['media_id']}"
        assert native.get(path + "/index.m3u8").status_code == 404
        native.cookies.set(COOKIE, grant["media_token"])
        master = native.get(path + "/index.m3u8")
        assert master.status_code == 200
        variant = "media-video.m3u8" if alternate else "media-muxed.m3u8"
        assert variant in master.text
        assert "hvc1." in master.text and "ec-3" in master.text
        if expected_range:
            assert master.text.count(f"VIDEO-RANGE={expected_range}") == 1
        else:
            assert "VIDEO-RANGE=" not in master.text
        if alternate:
            assert 'CHANNELS="6"' in master.text and 'CHANNELS="2"' in master.text
            assert "mp4a.40.2" in master.text
        else:
            assert "#EXT-X-MEDIA:TYPE=AUDIO" not in master.text
        media = native.get(f"{path}/{variant}")
        assert media.status_code == 200 and "#EXTINF:" in media.text
        streams = {}
        artifacts = {}
        for track in ("-video", "-a0", "-a1") if alternate else ("",):
            init = native.get(f"{path}/init{track}.mp4")
            segment = native.get(f"{path}/part{track}-00000.m4s")
            assert init.status_code == segment.status_code == 200
            artifact = tmp_path / f"track-{track}.mp4"
            artifact.write_bytes(init.content + segment.content)
            artifacts[track] = artifact
            result = subprocess.check_output(
                [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(artifact)],
                timeout=10,
            )
            streams[track] = json.loads(result)["streams"]
        video_track, audio_track = ("-video", "-a0") if alternate else ("", "")
        video = next(stream for stream in streams[video_track] if stream["codec_type"] == "video")
        surround = next(
            stream for stream in streams[audio_track] if stream["codec_type"] == "audio"
        )
        assert (video["codec_name"], video["codec_tag_string"], video["profile"]) == (
            "hevc",
            "hvc1",
            "Main 10",
        )
        assert (video["color_primaries"], video["color_transfer"], video["pix_fmt"]) == (
            primaries,
            transfer,
            "yuv420p10le",
        )
        assert (surround["codec_name"], surround["channels"]) == ("eac3", 6)
        if alternate:
            (stereo,) = streams["-a1"]
            assert (stereo["codec_name"], stereo["channels"]) == ("aac", 2)
        frames = subprocess.check_output(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-read_intervals",
                "%+#1",
                "-show_frames",
                "-of",
                "json",
                str(artifacts[video_track]),
            ],
            timeout=10,
        )
        side_data = {
            data["side_data_type"]: data
            for data in json.loads(frames)["frames"][0].get("side_data_list", [])
        }
        if expected_range == "PQ":
            assert "Mastering display metadata" in side_data
            light = side_data["Content light level metadata"]
            assert (light["max_content"], light["max_average"]) == (1000, 400)

        def first_audio_packet_hashes(file):
            data = subprocess.check_output(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-read_intervals",
                    "%+#8",
                    "-show_packets",
                    "-show_data_hash",
                    "sha256",
                    "-of",
                    "json",
                    str(file),
                ],
                timeout=10,
            )
            return [packet["data_hash"] for packet in json.loads(data)["packets"]]

        surround_packets = first_audio_packet_hashes(artifacts[audio_track])
        assert len(surround_packets) == 8
        assert surround_packets == first_audio_packet_hashes(sample)
        assert native.post(f"{LIBRARY_API}/media/{grant['media_id']}/close").status_code == 200
        assert native.get(path + "/index.m3u8").status_code == 404
