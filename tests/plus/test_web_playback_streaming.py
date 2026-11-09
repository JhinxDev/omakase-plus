"""HLS direct-stream playback sessions, independent of the prepared remux path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient
from starlette.requests import Request

from omakase.plus import households
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.web_playback_compatible import CompatibleStream
from omakase.plus.web_playback_delivery import DeliveryError
from omakase.plus.web_playback_remux import RemuxError
from omakase.plus.web_playback_routes import create_player_router
from omakase.plus.web_playback_source import PlexStream, ResolvedAnimeSource
from omakase.plus.web_security import csrf_token


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")
    monkeypatch.setenv("OMAKASE_PLAYBACK_STREAMING", "true")
    monkeypatch.setenv("OMAKASE_PLAYBACK_STREAM_CACHE_ROOT", str(tmp_path / "playback-streams"))
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
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number) "
        "VALUES(1,1,100,'Synthetic stream',1)"
    )
    db.commit()

    async def resolver(db, **values):
        scope = households.resolve_scope(
            db, viewer_id=values["actor_user_id"], library_id=values["library_id"]
        )
        return ResolvedAnimeSource(
            actor_user_id=1,
            library_id=scope.library_id,
            anilist_id=100,
            season_number=1,
            episode_number=values["episode_number"],
            server_id="synthetic-server",
            account_id="12345",
            metadata_key="11",
            part_key="/library/parts/12/1234/file.mkv",
            part_size=1_500_000_000,
            duration_ms=1_436_250,
            container="mkv",
            video_codec="h264",
            audio_codec="aac",
            streams=(
                PlexStream("3", "video", "h264", "und", True, False, True, 0),
                PlexStream("4", "audio", "aac", "jpn", True, False, True, 1),
                PlexStream("5", "subtitle", "ass", "eng", False, False, True, 2),
            ),
            media_version="a" * 64,
            source_identity="b" * 64,
            base_url="https://plex.invalid",
            token="dummy-only",
            _scope=scope,
            _snapshot=(),
            _single_plex_part=getattr(app.state, "single_plex_part", False),
        )

    launches = []
    launch_errors = []

    async def fake_launcher(source, sid, item):
        if launch_errors:
            raise RemuxError(launch_errors.pop(0))
        folder = tmp_path / "playback-streams" / sid
        folder.mkdir(parents=True)
        playlist = folder / "index.m3u8"
        playlist.write_text(
            "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:4\n"
            "#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:4.004,\n"
            "segments/seg-00000.ts\n#EXT-X-ENDLIST\n",
            encoding="utf-8",
        )
        (folder / "seg-00000.ts").write_bytes(b"synthetic-ts")
        subtitle = folder / "subtitle.ass"
        subtitle.write_text("[Script Info]\nTitle: synthetic\n", encoding="utf-8")
        item.update(
            {
                "streaming": True,
                "stream_dir": folder,
                "stream_playlist": playlist,
                "stream_subtitle": subtitle,
                "stream_process": None,
                "subtitle_process": None,
            }
        )
        launches.append(sid)

    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "src/omakase/plus/templates")
    )
    app = FastAPI()
    app.state.launch_errors = launch_errors
    app.include_router(
        create_player_router(
            templates,
            lambda request, user, **kw: {"email": user.email},
            resolver=resolver,
            source_guard=lambda db, source: None,
            remote_guard=lambda *args, **kwargs: True,
            stream_launcher=fake_launcher,
            cache_root=tmp_path / "remux",
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    sessions = {uid: create_session(db, uid) for uid in (1, 2)}
    db.commit()
    yield app, db, sessions, launches, tmp_path / "playback-streams"
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


def start_stream(owner, *, ass_supported=True, **options):
    return owner.post(
        "/plus/api/playback/sessions",
        json={
            "monitor_id": 1,
            "episode_number": 10,
            "codecs": ["h264", "aac"],
            "ass_supported": ass_supported,
            **options,
        },
    )


def test_stream_starts_without_the_prepared_size_limit(setup):
    app, _, sessions, launches, _ = setup
    owner = client(app, sessions[1])
    response = start_stream(owner)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["streaming"] is True
    assert payload["preparation_required"] is False
    assert payload["media_type"] == "application/vnd.apple.mpegurl"
    assert payload["media_url"].endswith("/playlist.m3u8")
    assert payload["subtitle_url"].endswith("/subtitle.ass")
    assert payload["source"]["mode"] == "stream"
    assert len(launches) == 1
    assert "dummy-only" not in response.text
    assert "plex.invalid" not in response.text


@pytest.mark.parametrize("available,prefer_on_demand", [(True, True), (False, True), (True, False)])
def test_exact_anime_resume_uses_on_demand_stream_with_hls_fallback(
    setup, monkeypatch, available, prefer_on_demand
):
    app, _, sessions, launches, _ = setup
    owner = client(app, sessions[1])
    old = start_stream(owner).json()
    route = f"/plus/api/playback/sessions/{old['session_id']}"
    assert (
        owner.post(
            route + "/events",
            json={
                "seq": 1,
                "event": "seek",
                "position_seconds": 300,
                "playing": False,
                "playback_rate": 1,
            },
        ).status_code
        == 200
    )
    assert owner.post(route + "/close", json={}).status_code == 204

    offsets = []

    async def open_at_position(self, position, authorize):
        await authorize()
        offsets.append(position)
        if not available:
            raise DeliveryError("Unavailable", 502)

    monkeypatch.setattr(CompatibleStream, "open", open_at_position)
    app.state.single_plex_part = True
    response = start_stream(owner, ass_supported=False, prefer_on_demand=prefer_on_demand)
    assert response.status_code == 201, response.text
    payload = response.json()
    assert offsets == ([300] if prefer_on_demand else [])
    assert payload["position_seconds"] == 300
    fast = available and prefer_on_demand
    assert payload["media_url"].endswith("/compatible.mpd" if fast else "/playlist.m3u8")
    assert len(launches) == (1 if fast else 2)
    assert (
        owner.post(
            f"/plus/api/playback/sessions/{payload['session_id']}/close", json={}
        ).status_code
        == 204
    )


def test_stream_playlist_segments_and_subtitles_are_authenticated(setup):
    app, _, sessions, _, cache_root = setup
    owner = client(app, sessions[1])
    payload = start_stream(owner).json()
    sid = payload["session_id"]
    playlist = owner.get(payload["media_url"])
    assert playlist.status_code == 200
    assert playlist.text.startswith("#EXTM3U")
    assert playlist.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    segment = owner.get(f"/plus/api/playback/sessions/{sid}/segments/seg-00000.ts")
    assert segment.status_code == 200
    assert segment.content == b"synthetic-ts"
    subtitle = owner.get(payload["subtitle_url"])
    assert subtitle.status_code == 200
    assert "Script Info" in subtitle.text
    # Another signed-in user cannot use the session.
    stranger = client(app, sessions[2])
    assert stranger.get(payload["media_url"]).status_code == 404
    assert (
        stranger.get(f"/plus/api/playback/sessions/{sid}/segments/seg-00000.ts").status_code == 404
    )
    assert (cache_root / sid).is_dir()


def test_stream_close_removes_the_session_cache(setup):
    app, _, sessions, _, cache_root = setup
    owner = client(app, sessions[1])
    payload = start_stream(owner).json()
    sid = payload["session_id"]
    response = owner.post(
        f"/plus/api/playback/sessions/{sid}/close",
        json={},
    )
    assert response.status_code == 204, response.text
    assert not (cache_root / sid).exists()
    assert owner.get(payload["media_url"]).status_code == 404


def test_stream_start_requires_csrf(setup):
    app, _, sessions, _, _ = setup
    owner = client(app, sessions[1])
    del owner.headers["x-csrf-token"]
    response = start_stream(owner)
    assert response.status_code == 403


def test_stream_rejects_invalid_csrf_token(setup):
    app, _, sessions, _, _ = setup
    owner = client(app, sessions[1])
    owner.headers["x-csrf-token"] = "0" * 64
    response = start_stream(owner)
    assert response.status_code == 403


def test_stream_subtitle_waits_for_nonempty_extraction(setup):
    app, _, sessions, _, cache_root = setup
    owner = client(app, sessions[1])
    payload = start_stream(owner).json()
    sid = payload["session_id"]
    subtitle_path = cache_root / sid / "subtitle.ass"
    subtitle_path.write_bytes(b"")
    assert owner.get(payload["subtitle_url"]).status_code == 404
    subtitle_path.write_text("[Script Info]\n", encoding="utf-8")
    response = owner.get(payload["subtitle_url"])
    assert response.status_code == 200
    assert "Script Info" in response.text


def test_stream_start_retries_one_timeout_without_a_second_session(setup):
    app, db, sessions, launches, _ = setup
    app.state.launch_errors.extend(["stream_start_timeout"])
    assert start_stream(client(app, sessions[1])).status_code == 201
    assert len(launches) == 1
    assert db.execute("SELECT count(*) FROM web_playback_sessions").fetchone()[0] == 1


def test_stream_start_transient_and_codec_errors_are_distinct(setup):
    app, db, sessions, _, _ = setup
    owner = client(app, sessions[1])
    app.state.launch_errors.extend(["stream_start_timeout", "stream_start_timeout"])
    failure = start_stream(owner)
    assert failure.status_code == 503
    assert failure.headers["retry-after"] == "2"
    assert failure.json()["code"] == "stream_start_timeout"
    assert db.execute("SELECT closed FROM web_playback_sessions").fetchone()[0] == 1
    codec = owner.post(
        "/plus/api/playback/sessions",
        json={"monitor_id": 1, "episode_number": 10, "codecs": ["aac"]},
    )
    assert codec.status_code == 415
    assert codec.json()["code"] == "unsupported_browser"


def test_publication_rechecks_after_concurrent_sqlite_commit(tmp_path, monkeypatch):
    from omakase.plus import web_playback_source as source

    with (
        sqlite3.connect(tmp_path / "wal.sqlite") as reader,
        sqlite3.connect(tmp_path / "wal.sqlite") as writer,
    ):
        reader.execute("PRAGMA journal_mode = WAL")
        reader.execute("CREATE TABLE unrelated (value INTEGER)")
        observations = []

        def publication(db, *_):
            count = db.execute("SELECT count(*) FROM unrelated").fetchone()[0]
            observations.append(count)
            if not count:
                writer.execute("INSERT INTO unrelated VALUES (1)")
                writer.commit()
            return "relative.mkv", 10, "same-source"

        monkeypatch.setattr(source, "_publication", publication)
        result = source._current_publication(reader, None, 1, 1, 1, {})
        assert result == ("relative.mkv", 10, "same-source")
        assert observations == [0, 1]
        assert not reader.in_transaction
