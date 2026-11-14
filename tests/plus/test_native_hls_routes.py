"""Route wiring for the native Apple TV player engine.

The native module has its own end-to-end suite with a real ffmpeg window. These
tests prove the delivery contract the app sees: the pass asks for `native`, comes
back as a fixed VOD playlist, and every asset the player can request is answered
from that one pass with its own authorization.
"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from starlette.requests import Request

from omakase.plus import native_hls, native_tv_test
from omakase.plus.debrid_library import (
    CloudSource,
    DebridLibrary,
    DebridLibraryError,
    account_binding,
)
from omakase.plus.debrid_stream import DebridStreams
from omakase.plus.native_hls import NativeAudio, NativeTimeline
from tests.plus.test_native_tv_test import API, LIBRARY_API, _anime_pass
from tests.plus.test_native_tv_test import system as system

TIMELINE_ARGS = dict(
    url="https://download.real-debrid.com/x.mkv",
    duration=24.0,
    origin=0.0,
    segments=((0.0, 6.0), (6.0, 12.0), (12.0, 18.0), (18.0, 24.0)),
    clusters=(0, 1000, 2000, 3000),
    total=4000,
    header=b"",
    info=b"",
    tracks=b"",
    video_codec="h264",
    width=1920,
    height=1080,
    frame_rate=23.976,
    audios=(NativeAudio(ordinal=0, codec="eac3", language="eng"),),
    bandwidth=8_000_000,
)


@pytest.fixture
def native_plan(tmp_path, monkeypatch):
    """Install a synthetic timeline instead of downloading and remuxing."""
    timeline = NativeTimeline(**TIMELINE_ARGS)
    starts = []

    def fake_start(self, key, url, duration, *, subtitle_key="", warm=None):
        starts.append((key, url, duration, subtitle_key))
        plan = native_hls.NativePlan(key, url, duration, tmp_path / key, subtitle_key, warm=warm)
        plan.root.mkdir(parents=True, exist_ok=True)
        plan.timeline = timeline
        plan.ready.set()
        self.plans[key] = plan
        return plan

    async def fake_ensure(self, plan, number, timeout=30.0, subtitles=0):
        self.ensured.append(number)
        (plan.root / "init.mp4").write_bytes(b"\x00\x00\x00\x08init")
        plan.video_segment_path(number).write_bytes(b"\x00\x00\x00\x08v" + bytes([number]))
        for audio in plan.timeline.audios:
            (plan.root / audio.init).write_bytes(b"\x00\x00\x00\x08inita")
            plan.audio_segment_path(audio, number).write_bytes(
                b"\x00\x00\x00\x08a" + bytes([number])
            )
        for track in range(subtitles):
            plan.subtitle_segment_path(track, number).write_text(
                "WEBVTT\n\n00:01.000 --> 00:02.000\nHi\n", encoding="utf-8"
            )

    monkeypatch.setattr(native_hls.NativeStreams, "start", fake_start)
    monkeypatch.setattr(native_hls.NativeStreams, "ensure", fake_ensure)
    monkeypatch.setattr(native_hls.NativeStreams, "ensured", [], raising=False)
    return starts


def _open_native(system, monkeypatch, engine="native"):
    native, db, options, _, _ = _anime_pass(system, monkeypatch)
    account = SimpleNamespace(token=uuid4().hex)

    monkeypatch.setattr(
        native_tv_test, "account_token", lambda _, actor: account.token if actor == 1 else ""
    )

    async def resolve(self, token, file_id):
        assert token == account.token and file_id == "d-fixture"
        return CloudSource(
            "Fixture.mkv",
            "a" * 64,
            "b" * 64,
            1800,
            account_binding(token),
            "",
            direct="https://download.real-debrid.com/synthetic",
            size=4,
        )

    async def no_preparation(*args, **kwargs):
        raise AssertionError("a native pass must not prepare a title")

    monkeypatch.setattr(DebridLibrary, "source", resolve)
    monkeypatch.setattr(DebridStreams, "prepare", no_preparation)

    def no_provider(request):
        raise AssertionError("a native pass reads no provider playlist")

    options.debrid = no_provider
    body = {"watch_path": "/plus/watch/cloud/d-fixture", "engine": engine}
    return native, native.post(LIBRARY_API + "/play", json=body)


def test_native_engine_returns_a_fixed_vod_pass(system, monkeypatch, native_plan):
    native, opened = _open_native(system, monkeypatch)
    assert opened.status_code == 200, opened.text
    data = opened.json()
    assert data["media_format"] == "hls"
    assert data["media_playlist"] == "vod"
    assert "media_url" not in data and "media_range_url" not in data
    assert data["duration_seconds"] == 1800
    assert "real-debrid.com" not in opened.text
    assert native_plan and native_plan[0][1].startswith("https://download.real-debrid.com/")

    cookie = {"omakase_tv_test_media": data["media_token"]}
    media = f"{API}/media/{data['media_id']}"
    master = native.get(media + "/index.m3u8", cookies=cookie)
    assert master.status_code == 200, master.text
    # The playlist is only answered after a small buffer exists, so the player's
    # first segment request cannot race an empty spool.
    assert len(set(native_hls.NativeStreams.ensured)) >= 2
    assert master.headers["cache-control"] == "private, no-store"
    text = master.text
    assert "#EXT-X-STREAM-INF" in text and "stream.m3u8" in text
    assert '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud"' in text
    assert 'AUDIO="aud"' in text
    assert "synthetic" not in text and "real-debrid" not in text

    video = native.get(media + "/stream.m3u8", cookies=cookie)
    assert video.status_code == 200
    assert video.text.count("#EXTINF:") == 4
    assert '#EXT-X-MAP:URI="init.mp4"' in video.text

    audio = native.get(media + "/audio0.m3u8", cookies=cookie)
    assert audio.status_code == 200 and "a0-00000.m4s" in audio.text

    init = native.get(media + "/init.mp4", cookies=cookie)
    assert init.status_code == 200 and init.content

    far = native.get(media + "/v00003.m4s", cookies=cookie)
    assert far.status_code == 200 and far.content.endswith(b"\x03")
    missing = native.get(media + "/v00009.m4s", cookies=cookie)
    assert missing.status_code == 404

    unauthorized = native.get(media + "/v00000.m4s")
    assert unauthorized.status_code == 404


def test_disconnected_caption_requests_release_the_producer(tmp_path, monkeypatch):
    streams = object.__new__(DebridStreams)
    streams.native = native_hls.NativeStreams(tmp_path, free_bytes=lambda: 16 * 1024**3)
    streams.subtitles = SimpleNamespace(get=lambda _: SimpleNamespace(tracks=(object(),)))
    plan = native_hls.NativePlan("caption", TIMELINE_ARGS["url"], 24.0, tmp_path)
    plan.timeline = NativeTimeline(**TIMELINE_ARGS)
    plan.continuous = True
    entry = SimpleNamespace(subtitle_key="caption")

    async def run():
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def produce(*_args, **_kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        monkeypatch.setattr(streams.native, "_produce", produce)

        def open_request(disconnect):
            async def receive():
                await disconnect.wait()
                return {"type": "http.disconnect"}

            request = Request({"type": "http", "method": "GET"}, receive)
            return asyncio.create_task(
                streams._native_response(entry, plan, "s0-00000.vtt", request)
            )

        first_disconnect, second_disconnect = asyncio.Event(), asyncio.Event()
        first = open_request(first_disconnect)
        await asyncio.wait_for(started.wait(), 2)
        second = open_request(second_disconnect)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        first_disconnect.set()
        with pytest.raises(DebridLibraryError) as first_error:
            await asyncio.wait_for(first, 2)
        assert first_error.value.status == 499
        assert not cancelled.is_set()

        second_disconnect.set()
        with pytest.raises(DebridLibraryError) as second_error:
            await asyncio.wait_for(second, 2)
        assert second_error.value.status == 499
        assert cancelled.is_set()
        assert plan.reserved_bytes == 0
        assert list(tmp_path.iterdir()) == []

    asyncio.run(run())


def test_native_pass_refuses_a_copy_the_player_cannot_remux(system, monkeypatch, tmp_path):
    def unsupported(self, key, url, duration, *, subtitle_key="", warm=None):
        self.plans[key] = native_hls.NativePlan(key, url, duration, tmp_path / key, subtitle_key)
        self.plans[key].error = "unsupported"
        self.plans[key].ready.set()
        return self.plans[key]

    monkeypatch.setattr(native_hls.NativeStreams, "start", unsupported)
    _native, opened = _open_native(system, monkeypatch)
    # A refusal is retryable, so the app never falls back to the direct player.\n    assert opened.status_code == 503
    assert "Apple TV player" in opened.json()["detail"]


def test_direct_engine_still_asks_for_the_original_file(system, monkeypatch, native_plan):
    _native, opened = _open_native(system, monkeypatch, engine="direct")
    # A direct pass fails on the synthetic provider here, but it must never touch
    # the native plan: the two routes stay separate.
    assert native_plan == []
    assert opened.status_code in {200, 415, 502, 504}


@pytest.mark.parametrize("kind", ["cloud", "anime"])
def test_replacing_and_closing_native_pass_reclaims_its_segments(
    system, monkeypatch, native_plan, tmp_path, kind
):
    from omakase.plus.debrid_anime import DebridTitleSource

    native, opened = _open_native(system, monkeypatch)
    assert opened.status_code == 200, opened.text
    path = "/plus/watch/cloud/d-fixture"
    if kind == "anime":
        monkeypatch.setattr(native_tv_test, "playback_provider", lambda *_: "realdebrid")
        monkeypatch.setattr(native_tv_test, "revalidate_anime", lambda *_: None)

        async def resolve(connection, scope, monitor, episode, library):
            cloud = await library.source(native_tv_test.account_token(connection, 1), "d-fixture")
            return DebridTitleSource(
                scope, "anime", "100", 1, episode, cloud, "b" * 64, ()
            ), "Fixture"

        monkeypatch.setattr(native_tv_test, "resolve_debrid_anime", resolve)
        path = "/plus/watch/1/1"
        opened = native.post(LIBRARY_API + "/play", json={"watch_path": path, "engine": "native"})
        assert opened.status_code == 200, opened.text

    def request_master(response):
        data = response.json()
        media = f"{API}/media/{data['media_id']}"
        cookie = {"omakase_tv_test_media": data["media_token"]}
        master = native.get(media + "/index.m3u8", cookies=cookie)
        assert master.status_code == 200, master.text
        return f"{LIBRARY_API}/media/{data['media_id']}/close"

    old_close = request_master(opened)
    old_root = tmp_path / native_plan[-1][0]
    assert (old_root / "init.mp4").exists()

    replacement = native.post(LIBRARY_API + "/play", json={"watch_path": path, "engine": "native"})
    assert replacement.status_code == 200, replacement.text
    assert not old_root.exists()
    current_close = request_master(replacement)
    current_root = tmp_path / native_plan[-1][0]
    assert current_root != old_root and (current_root / "init.mp4").exists()
    assert native.post(old_close).status_code == 200
    assert current_root.exists()
    assert native.post(current_close).status_code == 200
    assert not current_root.exists()
