"""The served playlist and the seek/restart path, on real produced media.

The route's HTTP layer already has its own test (authorization, asset names,
no-store, 404s). What these tests add is the part that was never checked: that
the playlist the server generates names exactly the media it produced, that the
init it maps is the pinned one, that the returned bytes decode in order and
agree with their own EXTINF, and that a burst of requests for the position the
viewer abandoned cannot tear down the position the viewer chose.

The pipeline here is the production one: a local range provider, `build_timeline`,
`NativeStreams`, `_ContinuousSession`, and the same playlist functions the route
serves. Driving the FastAPI app itself needs the router's native spool root wired
into the test app, which it is not today; that gap is recorded rather than hidden.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from omakase.plus import native_hls
from omakase.plus.native_hls import NativePlan, NativeStreams, build_timeline
from tests.plus.test_native_hls import (
    DURATION,
    FRAME_RATE,
    _build_hevc_fixture,
    _Provider,
)


def _ffprobe_times(path: Path, stream: str = "v:0") -> list[float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    values = []
    for row in result.stdout.splitlines():
        row = row.strip().rstrip(",")
        if row:
            try:
                values.append(float(row))
            except ValueError:
                continue
    return values


def _lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


@pytest.fixture
def produced_pass(tmp_path, monkeypatch):
    """One real continuous pass, driven the way the player drives it."""
    folder = tmp_path
    fixture = _build_hevc_fixture(folder)
    provider = _Provider(fixture.read_bytes())
    monkeypatch.setattr(native_hls, "_DIRECT", re.compile(r"https?://[^\s]{1,3000}"))
    timeline = asyncio.run(build_timeline(provider.url, DURATION))
    assert timeline is not None and len(timeline.segments) >= 4
    plan = NativePlan(
        key="served", url=timeline.url, duration=timeline.duration, root=folder / "plan"
    )
    plan.root.mkdir(parents=True, exist_ok=True)
    plan.timeline = timeline
    plan.ready.set()
    plan.continuous = True
    streams = NativeStreams(folder / "spool")
    streams.root.mkdir(parents=True, exist_ok=True)
    restarts: list[int] = []
    served: dict[str, bytes] = {}

    original_open = native_hls._ContinuousSession.open

    async def counting_open(self):
        restarts.append(self.start)
        return await original_open(self)

    monkeypatch.setattr(native_hls._ContinuousSession, "open", counting_open)

    async def run():
        try:
            for number in range(len(timeline.segments)):
                await asyncio.wait_for(streams.ensure(plan, number, 90), 240)
                served[f"v{number:05d}.m4s"] = plan.video_segment_path(number).read_bytes()
                for audio in timeline.audios:
                    served[audio.segment(number)] = plan.audio_segment_path(
                        audio, number
                    ).read_bytes()
            served["init.mp4"] = (plan.root / "init.mp4").read_bytes()
            for audio in timeline.audios:
                served[audio.init] = (plan.root / audio.init).read_bytes()
            yield_state = {
                "video_playlist": streams.video_playlist(plan),
                "master_playlist": streams.master_playlist(plan, ()),
                "audio_playlist": streams.audio_playlist(plan, timeline.audios[0].ordinal),
            }
        finally:
            if plan.session is not None:
                await plan.session.close("fixture")
                plan.session = None
        return yield_state

    try:
        playlists = asyncio.run(run())
        yield {
            "folder": folder,
            "timeline": timeline,
            "plan": plan,
            "streams": streams,
            "served": served,
            "playlists": playlists,
            "restarts": restarts,
        }
    finally:
        provider.close()


def test_the_served_playlist_is_the_media_it_names(produced_pass):
    """EXT-X-MAP, EXTINF, URIs and the returned bytes agree, in order."""
    timeline = produced_pass["timeline"]
    served = produced_pass["served"]
    video = produced_pass["playlists"]["video_playlist"].decode()

    lines = _lines(video)
    assert [line for line in lines if line.startswith("#EXT-X-MAP:")] == [
        '#EXT-X-MAP:URI="init.mp4"'
    ]
    durations = [
        float(line[len("#EXTINF:") :].rstrip(",")) for line in lines if line.startswith("#EXTINF:")
    ]
    uris = [line for line in lines if not line.startswith("#")]
    assert len(durations) == len(uris) == len(timeline.segments)
    assert uris == [f"v{number:05d}.m4s" for number in range(len(timeline.segments))]
    assert "EXT-X-ENDLIST" in video
    assert "#EXT-X-DISCONTINUITY" not in video
    assert "#EXT-X-INDEPENDENT-SEGMENTS" not in video
    declared = sum(durations)
    span = timeline.segments[-1][1] - timeline.segments[0][0]
    assert abs(declared - span) <= DURATION * 0.02, (declared, span)

    # Every URI the playlist names is a file the server holds, and the init it
    # maps is the pinned init.
    for uri in uris:
        assert uri in served and served[uri], uri
    assert served["init.mp4"]

    joined = produced_pass["folder"] / "served-video.mp4"
    joined.write_bytes(served["init.mp4"] + b"".join(served[uri] for uri in uris))
    times = _ffprobe_times(joined)
    assert times, "the served bytes do not decode"
    assert all(later > earlier for earlier, later in zip(times, times[1:])), (
        "the served video is not monotonic in presentation order"
    )
    frame = 1 / FRAME_RATE
    inside_first = len([value for value in times if value < durations[0]])
    assert abs(inside_first * frame - durations[0]) <= frame * 3, (
        f"first EXTINF {durations[0]:.3f}s disagrees with its own media "
        f"({inside_first * frame:.3f}s)"
    )

    audio = produced_pass["playlists"]["audio_playlist"].decode()
    assert '#EXT-X-MAP:URI="init-a0.mp4"' in audio
    audio_uris = [line for line in _lines(audio) if not line.startswith("#")]
    assert audio_uris == [f"a0-{number:05d}.m4s" for number in range(len(timeline.segments))]
    for uri in audio_uris:
        assert uri in served and served[uri], uri
    joined_audio = produced_pass["folder"] / "served-audio.mp4"
    joined_audio.write_bytes(served["init-a0.mp4"] + b"".join(served[uri] for uri in audio_uris))
    audio_times = _ffprobe_times(joined_audio, "a:0")
    assert audio_times and all(
        later > earlier for earlier, later in zip(audio_times, audio_times[1:])
    ), "the served audio is not monotonic"

    master = produced_pass["playlists"]["master_playlist"].decode()
    assert "stream.m3u8" in master and 'AUDIO="aud"' in master
    assert "real-debrid" not in master and "http" not in master


@pytest.mark.parametrize("session_start", [0, 16], ids=["evicted", "before-start"])
def test_backward_scrub_opens_only_the_last_distinct_window(tmp_path, monkeypatch, session_start):
    timeline = native_hls.NativeTimeline(
        "https://mock.invalid/episode",
        180,
        0,
        tuple((6 * number, 6 * (number + 1)) for number in range(30)),
        tuple(100 * number for number in range(30)),
        3100,
        b"",
        b"",
        b"",
        "hevc",
        audios=(native_hls.NativeAudio(0, "aac"),),
    )
    plan = NativePlan("scrub", timeline.url, timeline.duration, tmp_path)
    plan.timeline = timeline
    plan.continuous = True
    streams = NativeStreams(tmp_path)
    opened = []
    audio = timeline.audios[0]
    payload = b"test segment bytes "

    def cache(number):
        plan.video_segment_path(number).write_bytes(payload + bytes([number]))
        plan.audio_segment_path(audio, number).write_bytes(payload + bytes([number]))

    (tmp_path / "init.mp4").write_bytes(payload)
    (tmp_path / audio.init).write_bytes(payload)
    cache(15)

    class Session(native_hls._ContinuousSession):
        async def open(self):
            opened.append(self.start)
            cache(self.start)

        async def close(self, _reason):
            self.closed = True

    monkeypatch.setattr(native_hls, "_ContinuousSession", Session)
    plan.session = Session(streams, plan, session_start)
    plan.session.active = 22
    for kind in ("video", "audio"):
        track = native_hls._ContinuousTrack(kind)
        track.frontier = 25
        plan.session.tracks.append(track)
    assert plan.session.frontier == 25 and not plan.session.can_serve(14)

    async def drive():
        async def request(offset, number, kind):
            await asyncio.sleep(offset)
            try:
                await streams.ensure(plan, number, 2, subtitles=1)
            except native_hls.NativeError as error:
                return error.code
            path = (
                plan.video_segment_path(number)
                if kind == "video"
                else plan.audio_segment_path(audio, number)
            )
            return path.read_bytes()

        # Relative arrival times from the physical backward scrub. The paired
        # renditions follow the same target. A late duplicate of window 13
        # and an already cached earlier read must not move the pending target.
        requests = (
            (0.000, 14, "audio"),
            (0.010, 14, "video"),
            (0.225, 13, "video"),
            (0.301, 13, "audio"),
            (0.494, 12, "video"),
            (0.503, 12, "audio"),
            (0.552, 13, "video"),
            (0.580, 15, "video"),
            (0.636, 11, "audio"),
            (0.638, 11, "video"),
            (0.799, 10, "video"),
            (0.801, 10, "audio"),
        )
        outcomes = await asyncio.gather(*(request(*args) for args in requests))
        assert opened == [10] and plan.session.start == 10

        async def unexpected_backward_settle(*_args):
            pytest.fail("a distant forward request should start immediately")

        monkeypatch.setattr(streams, "_settle_backward", unexpected_backward_settle)
        assert await request(0, 29, "video") == payload + bytes([29])
        return outcomes

    outcomes = asyncio.run(drive())
    assert opened == [10, 29]
    assert outcomes[7] == payload + bytes([15])
    assert outcomes[-2:] == [payload + bytes([10]), payload + bytes([10])]
    assert all(outcome == "stale" for outcome in outcomes[:7] + outcomes[8:-2])


def test_a_stale_request_cannot_restart_the_chosen_position(produced_pass):
    """Forward, back, then a burst for the abandoned position, concurrently.

    The burst must cost at most the one restart the cooldown allows, must not
    replace the pinned init, and must leave the chosen window serviceable. The
    restart count and the served bytes are the measurements.
    """
    timeline = produced_pass["timeline"]
    plan = produced_pass["plan"]
    streams = produced_pass["streams"]
    restarts = produced_pass["restarts"]
    last = len(timeline.segments) - 1
    pinned = (plan.root / "init.mp4").read_bytes()
    audio = timeline.audios[0]

    async def drive():
        async def request(number, subtitles=0):
            await asyncio.wait_for(streams.ensure(plan, number, 90), 240)
            return (
                plan.video_segment_path(number).read_bytes(),
                plan.audio_segment_path(audio, number).read_bytes(),
            )

        try:
            forward = await request(last)
            backward = await request(1)
            before = len(restarts)
            stale = await asyncio.gather(*[request(last) for _ in range(5)], return_exceptions=True)
            restarted = len(restarts) - before
            current = await request(1)
            return forward, backward, stale, restarted, current
        finally:
            if plan.session is not None:
                await plan.session.close("test")

    forward, backward, stale, restarted, current = asyncio.run(drive())
    assert forward and backward and current, "a window was not served"
    assert restarted <= 1, f"{restarted} restarts from a burst of stale requests"
    for outcome in stale:
        assert not isinstance(outcome, AssertionError)
        if isinstance(outcome, BaseException):
            assert isinstance(outcome, native_hls.NativeError), repr(outcome)
            assert outcome.code in {
                "stale",
                "restarted",
                "incomplete",
                "deadline",
            }, outcome.code
    assert (plan.root / "init.mp4").read_bytes() == pinned, (
        "a stale request rewrote the pinned init"
    )
    # The chosen window is still the same media after the burst, not a mix of two
    # generations: its first presented sample must stay inside its own window.
    path = produced_pass["folder"] / "stale-check.mp4"
    path.write_bytes(pinned + current[0])
    times = _ffprobe_times(path)
    start = timeline.segments[1][0]
    assert times and abs(times[0] - start) <= 1 / FRAME_RATE * 6, (times[:3], start)
