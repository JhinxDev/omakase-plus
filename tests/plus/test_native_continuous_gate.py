"""The architecture gates as tests, so the qualification cannot drift from the suite.

Two layers are asserted here:

* the packaged shape of a continuous remux (one forward FFmpeg session, fMP4
  fragments filed onto a cue-derived VOD timeline), and
* the server itself: a real session is started behind the repository's own
  ``NativeStreams`` against a local range provider and the frames it produces are
  compared with the source frame sequence, window by window.

The measured failure this design replaces is in ``GATES.md``: cutting an open-GOP
HEVC title at CRA boundaries lost four frames per boundary, and the candidate that
dropped the RASL pictures measured a 0.167 s hole every six seconds.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import subprocess
from pathlib import Path

import pytest

from tests.plus.test_native_hls import (
    DURATION,
    FRAME_RATE,
    _build_fixture,
    _build_hevc_fixture,
    _Provider,
    _synthetic_fragment,
    _synthetic_init,
)

from omakase.plus import native_hls
from omakase.plus.native_hls import (
    minimum_composition_offset,
    NativePlan,
    NativeStreams,
    build_timeline,
    continuous_enabled,
    fragment_bases,
    shift_decode_times,
)

FFMPEG = native_hls  # the module is exercised through its own ffmpeg calls
SEGMENT_SECONDS = 6.006

def _run(*args) -> None:
    result = subprocess.run(args, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "ignore")[-800:]


def _frames(path: Path, stream: str = "v:0") -> list[float]:
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
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    return sorted(
        float(row.strip().rstrip(",")) for row in result.stdout.strip().splitlines() if row.strip()
    )


def _continuous_remux(source: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for item in out.iterdir():
        item.unlink()
    init = out / "init.mp4"
    run = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-tag:v",
            "hvc1",
            "-f",
            "hls",
            "-hls_time",
            "6",
            "-hls_playlist_type",
            "vod",
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            str(init),
            "-hls_segment_filename",
            str(out / "seg%04d.m4s"),
            str(out / "index.m3u8"),
        ],
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr[-400:]
    assert init.exists()
    return out / "index.m3u8"


def test_continuous_remux_preserves_every_source_frame(tmp_path):
    source = _build_fixture(tmp_path)
    playlist = _continuous_remux(source, tmp_path / "hls")
    joined = tmp_path / "joined.mp4"
    parts = [playlist.parent / "init.mp4", *sorted(playlist.parent.glob("seg*.m4s"))]
    joined.write_bytes(b"".join(part.read_bytes() for part in parts))
    source_times = _frames(source)
    output_times = _frames(joined)
    assert len(output_times) == len(source_times), "continuous remux must not lose frames"
    assert len(output_times) == len(set(output_times)), "no duplicated presentation times"
    worst = max(
        abs((got - output_times[0]) - (src - source_times[0]))
        for src, got in zip(source_times, output_times, strict=True)
    )
    frame = source_times[1] - source_times[0]
    assert worst <= frame * 1.01, f"frames displaced by {worst:.3f}s"


@pytest.fixture(autouse=True)
def allow_local_provider():
    """The production guard only accepts Real-Debrid; these tests use loopback.

    It is set here and in ``_make`` so a module-scoped fixture that builds a
    timeline is not ordered before the patch.
    """
    original = native_hls._DIRECT
    native_hls._DIRECT = re.compile(r"https?://[^\s]{1,3000}")
    yield
    native_hls._DIRECT = original


@pytest.fixture(scope="module")
def hevc_fixture(tmp_path_factory):
    folder = tmp_path_factory.mktemp("continuous-hevc")
    return _build_hevc_fixture(folder)


@pytest.fixture(scope="module")
def hevc_provider(hevc_fixture):
    server = _Provider(hevc_fixture.read_bytes())
    yield server
    server.close()


async def _open(streams, plan, number, subtitles=0, timeout=120):
    await streams.ensure(plan, number, timeout, subtitles)


async def _bounded(awaitable, seconds=60):
    """Every step in these tests has a deadline: a stall fails, it never hangs."""
    return await asyncio.wait_for(awaitable, seconds)


def _make(tmp_path, provider, *, name="plan"):
    """(timeline, plan, streams) for one local provider."""
    native_hls._DIRECT = re.compile(r"https?://[^\s]{1,3000}")
    timeline = asyncio.run(build_timeline(provider.url, DURATION))
    assert timeline is not None
    plan = NativePlan(
        key=name, url=timeline.url, duration=timeline.duration, root=tmp_path / name
    )
    plan.root.mkdir(parents=True, exist_ok=True)
    plan.timeline = timeline
    plan.ready.set()
    plan.continuous = True
    streams = NativeStreams(tmp_path / "spool")
    streams.root.mkdir(parents=True, exist_ok=True)
    return timeline, plan, streams


def _joined(tmp_path, name, init_path, media_paths):
    path = tmp_path / name
    path.write_bytes(
        b"".join(part.read_bytes() for part in [init_path, *media_paths])
    )
    return path


@pytest.fixture(scope="module")
def server_run(tmp_path_factory, hevc_fixture, hevc_provider):
    """One continuous session driven the way the player drives it: window by window."""
    folder = tmp_path_factory.mktemp("continuous-run")
    timeline, plan, streams = _make(folder, hevc_provider)
    records = []

    async def run():
        try:
            for number in range(len(timeline.segments)):
                await _open(streams, plan, number)
                records.append(
                    {
                        "number": number,
                        "video": plan.video_segment_path(number).read_bytes(),
                        "init": (plan.root / "init.mp4").read_bytes(),
                        "audios": {
                            audio.ordinal: plan.audio_segment_path(audio, number).read_bytes()
                            for audio in timeline.audios
                        },
                        "audio_inits": {
                            audio.ordinal: (plan.root / audio.init).read_bytes()
                            for audio in timeline.audios
                        },
                        "frontier": plan.session.frontier,
                        "active": plan.session.active,
                        "spool": streams.plan_usage(plan),
                        "read_bytes": plan.session.metrics["bytes"],
                    }
                )
            metrics = dict(plan.session.metrics)
            peak = plan.session.peak_bytes
            await plan.session.close("test")
        finally:
            await streams_running(streams)
        return metrics, peak

    metrics, peak = asyncio.run(run())

    # Acceptance artifact: when a destination is named, write exactly what the
    # playlist declares so the two executable gates can run against it.
    destination = os.environ.get("OMAKASE_GATE_OUTPUT")
    if destination:
        target = Path(destination)
        target.mkdir(parents=True, exist_ok=True)
        for item in target.iterdir():
            if item.is_file():
                item.unlink()
        (target / "init.mp4").write_bytes(records[0]["init"])
        for audio in timeline.audios:
            (target / audio.init).write_bytes(records[0]["audio_inits"][audio.ordinal])
        for record in records:
            (target / f"v{record['number']:05d}.m4s").write_bytes(record["video"])
            for ordinal, data in record["audios"].items():
                (target / f"a{ordinal}-{record['number']:05d}.m4s").write_bytes(data)
        (target / "index.m3u8").write_bytes(streams.master_playlist(plan, ()))
        (target / "stream.m3u8").write_bytes(streams.video_playlist(plan))
        for audio in timeline.audios:
            (target / audio.playlist).write_bytes(streams.audio_playlist(plan, audio.ordinal))
        with (target / "measurements.json").open("w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "source_bytes": len(hevc_provider.payload),
                        "read_bytes": metrics["bytes"],
                        "windows": len(records),
                        "peak_spool_bytes": peak,
                        "peak_spool_mib": round(peak / 1024 / 1024, 2),
                        "lead_windows": native_hls._LEAD_WINDOWS,
                        "keep_windows": native_hls._KEEP_WINDOWS,
                        "spool_plan_limit_mib": native_hls._SPOOL_PLAN_LIMIT // 1024 // 1024,
                        "spool_total_limit_mib": native_hls._SPOOL_TOTAL_LIMIT // 1024 // 1024,
                    },
                    indent=1,
                )
                + "\n"
            )

    return {
        "folder": folder,
        "timeline": timeline,
        "plan": plan,
        "records": records,
        "metrics": metrics,
        "peak": peak,
        "source": hevc_fixture,
        "payload": hevc_provider.payload,
    }


async def streams_running(streams):
    while streams.running:
        process = streams.running.pop()
        try:
            process.kill()
        except (ProcessLookupError, RuntimeError):
            continue


def _window_join(folder, record, name):
    path = folder / name
    path.write_bytes(record["init"] + record["video"])
    return path


def test_the_server_session_preserves_the_source_frame_sequence(server_run):
    folder = server_run["folder"]
    source = server_run["source"]
    joined = folder / "all-video.mp4"
    joined.write_bytes(
        server_run["records"][0]["init"]
        + b"".join(record["video"] for record in server_run["records"])
    )
    source_times = _frames(source)
    output_times = _frames(joined)
    assert len(output_times) == len(source_times), (
        f"{len(output_times)} of {len(source_times)} frames survived the session"
    )
    assert len(output_times) == len(set(output_times)), "duplicated presentation times"
    frame = source_times[1] - source_times[0]
    worst = max(
        abs((got - output_times[0]) - (src - source_times[0]))
        for src, got in zip(source_times, output_times, strict=True)
    )
    assert worst <= frame * 1.01, f"a frame was displaced by {worst:.3f}s"
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(joined), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert (result.stderr or "").strip() == "", result.stderr[:400]


def test_every_window_starts_on_its_declared_cue(server_run):
    folder = server_run["folder"]
    timeline = server_run["timeline"]
    for record in server_run["records"]:
        number = record["number"]
        start, end = timeline.segments[number]
        path = _window_join(folder, record, f"w{number}.mp4")
        times = _frames(path)
        assert times, f"window {number} decoded no frames"
        assert start - 0.06 <= times[0] - timeline.origin <= start + 0.2, (
            f"window {number} starts at {times[0] - timeline.origin:.3f}, not {start:.3f}"
        )
        assert times[-1] - timeline.origin <= end + 0.2, f"window {number} overruns its span"
        # Windows are a partition: no frame is produced twice.
        assert len(times) == len(set(times))


def test_audio_and_video_share_the_window_clock(server_run):
    folder = server_run["folder"]
    timeline = server_run["timeline"]
    for record in server_run["records"]:
        number = record["number"]
        video = _frames(_window_join(folder, record, f"v{number}.mp4"))
        for audio in timeline.audios:
            path = folder / f"a{audio.ordinal}-{number}.mp4"
            path.write_bytes(record["audio_inits"][audio.ordinal] + record["audios"][audio.ordinal])
            track = _frames(path, "a:0")
            assert track, f"audio{audio.ordinal} window {number} decoded nothing"
            assert abs((track[0] - timeline.origin) - (video[0] - timeline.origin)) <= 0.1
            start = timeline.segments[number][0]
            assert start - 0.1 <= track[0] - timeline.origin <= start + 0.15


def test_subtitles_come_from_one_bounded_window_read(tmp_path, hevc_provider):
    """A text track is served from the qualified window path, only on request."""
    timeline, plan, streams = _make(tmp_path, hevc_provider)

    async def step(label, awaitable, seconds=60):
        return await asyncio.wait_for(awaitable, seconds)

    async def run():
        try:
            await step("open", _open(streams, plan, 1))
            assert not plan.subtitle_segment_path(0, 1).exists()
            assert plan.session is not None
            await step("subtitles", streams.ensure_subtitles(plan, 1, 1))
            text = plan.subtitle_segment_path(0, 1).read_text("utf-8")
            assert text.startswith("WEBVTT")
            assert "Second hevc line" in text
            local = next(line for line in text.splitlines() if "-->" in line)
            stamp = local.split("-->")[0].strip().split(":")
            at = float(stamp[-1]) + int(stamp[-2]) * 60 + int(stamp[0]) * 3600
            start = timeline.segments[1][0]
            assert abs((timeline.origin + start + at) - 8.0) < 0.06
            await step("close", plan.session.close("test"))
        finally:
            await streams_running(streams)

    asyncio.run(run())


def _source_packet_times(path: Path, stream: str) -> list[float]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
         "packet=pts_time", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    times = []
    for row in result.stdout.split():
        try:
            times.append(float(row))
        except ValueError:
            continue
    return sorted(times)


def _first_at_or_after(times: list[float], moment: float) -> float | None:
    return next((value for value in times if value >= moment - 1e-3), None)


def test_the_timestamp_contract_matches_the_source(server_run):
    """One translation for every track, with the source's own spacing kept.

    The contract is: a window's first picture sits at the cue the playlist files
    it under, translated by the source's own start offset, and audio keeps the
    source's offset from picture. Nothing forces a track to zero, so a source
    start offset or a reorder delay stays exactly where the source put it.
    """
    folder = server_run["folder"]
    timeline = server_run["timeline"]
    source_video = _source_packet_times(server_run["source"], "v:0")
    source_audio = _source_packet_times(server_run["source"], "a:0")
    frame = 1 / FRAME_RATE
    rows = []
    for record in server_run["records"]:
        number = record["number"]
        start = timeline.origin + timeline.segments[number][0]
        expect_video = _first_at_or_after(source_video, start)
        expect_audio = _first_at_or_after(source_audio, start)
        assert expect_video is not None and expect_audio is not None
        video_path = _window_join(folder, record, f"contract-v{number}.mp4")
        audio = timeline.audios[0]
        audio_path = folder / f"contract-a{number}.mp4"
        audio_path.write_bytes(
            record["audio_inits"][audio.ordinal] + record["audios"][audio.ordinal]
        )
        got_video = _frames(video_path)[0]
        got_audio = _frames(audio_path, "a:0")[0]
        rows.append(
            {
                "window": number,
                "start": round(start, 3),
                "video_offset": round(got_video - expect_video, 3),
                "audio_offset": round(got_audio - expect_audio, 3),
                "av_output": round(got_audio - got_video, 3),
                "av_source": round(expect_audio - expect_video, 3),
            }
        )
        # Three frames is the measured contract: a fragment carries the leading
        # pictures of its keyframe, which present just before the cue it opens,
        # and that shift is bounded per boundary rather than accumulating.
        assert abs(got_video - expect_video) <= frame * 3.05, rows[-1]
        assert abs(got_audio - expect_audio) <= frame * 2, rows[-1]
        assert abs((got_audio - got_video) - (expect_audio - expect_video)) <= frame * 3.05, rows[-1]
    offsets = {row["video_offset"] for row in rows}
    assert max(offsets) - min(offsets) <= frame, rows
    bases = [fragment_bases(record["video"])[0][2] for record in server_run["records"]]
    assert bases == sorted(bases) and len(set(bases)) == len(bases), bases


def test_the_timestamp_contract_survives_a_seek(tmp_path, hevc_provider):
    """After restarts, the pair the player is served still maps to the cue.

    Measured exactly as a player reads it: the pinned init plus the segment whose
    URI the playlist names, mapped through that init's own edit list. This is the
    end-to-end form of the contract; the session's internal constants are not the
    thing the television sees.
    """
    timeline, plan, streams = _make(tmp_path, hevc_provider)
    frame = 1 / FRAME_RATE
    audio = timeline.audios[0]

    def assert_window(number, label):
        start = timeline.origin + timeline.segments[number][0]
        video = _served_presentation(
            (plan.root / "init.mp4").read_bytes(),
            plan.video_segment_path(number).read_bytes(),
        )
        track = _served_presentation(
            (plan.root / audio.init).read_bytes(),
            plan.audio_segment_path(audio, number).read_bytes(),
        )
        assert abs(video - start) <= frame * 3.05, (label, number, video, start)
        assert abs(track - start) <= frame * 3.05, (label, number, track, start)
        assert abs(track - video) <= frame * 4.05, (label, number, track, video)

    async def run():
        try:
            await _bounded(_open(streams, plan, 4), 90)
            await _bounded(_open(streams, plan, 2), 90)
            assert_window(2, "backward seek")
            if plan.session is not None:
                await _bounded(plan.session.close("test"), 30)
        finally:
            await streams_running(streams)

    asyncio.run(run())


def _served_presentation(init: bytes, segment: bytes) -> float:
    """Where the served pair really starts, in pass seconds.

    Reads the segment's first fragment the way a player does: decode time plus
    composition offset, mapped through the init's own edit list. The full
    per-sample min(DTS + composition offset) walk is the next refinement of this
    measurement; the composition-offset form is a lower bound that already
    exposes a restarted session served through another session's init.
    """
    timescale = native_hls.media_timescale(init)
    assert timescale
    base = fragment_bases(segment)[0][2]
    offset = minimum_composition_offset(segment)
    return (base + offset) / timescale + native_hls.edit_shift(init)


def _init_identity(init: bytes) -> dict:
    """The parts of an init that decide whether it can describe another session.

    Track identity, timescale and codec configuration must match, and the edit
    list must agree because it is the only place a session's place on the source
    clock is recorded. Movie and track durations are informational in fragmented
    output, so they are deliberately not part of this comparison.
    """
    timescales = []
    track_ids = []
    stsd = []
    edits = []
    for kind, _start, body, size in native_hls._boxes(init, 0, len(init)):
        if kind != b"moov":
            continue
        for kind2, _s2, body2, size2 in native_hls._boxes(
            init, body, native_hls._payload_end(init, body, size)
        ):
            if kind2 != b"trak":
                continue
            for kind3, _s3, body3, size3 in native_hls._boxes(
                init, body2, native_hls._payload_end(init, body2, size2)
            ):
                if kind3 == b"tkhd":
                    version = init[body3]
                    at = body3 + (20 if version == 1 else 12)
                    track_ids.append(int.from_bytes(init[at : at + 4], "big"))
                if kind3 != b"mdia":
                    continue
                for kind4, _s4, body4, size4 in native_hls._boxes(
                    init, body3, native_hls._payload_end(init, body3, size3)
                ):
                    if kind4 == b"mdhd":
                        version = init[body4]
                        at = body4 + (20 if version == 1 else 12)
                        timescales.append(int.from_bytes(init[at : at + 4], "big"))
                    if kind4 != b"minf":
                        continue
                    for kind5, _s5, body5, size5 in native_hls._boxes(
                        init, body4, native_hls._payload_end(init, body4, size4)
                    ):
                        if kind5 != b"stbl":
                            continue
                        for kind6, _s6, body6, size6 in native_hls._boxes(
                            init, body5, native_hls._payload_end(init, body5, size5)
                        ):
                            if kind6 == b"stsd":
                                stsd.append(
                                    init[body6 : native_hls._payload_end(init, body6, size6)]
                                )
    edits.append(round(native_hls.edit_shift(init), 6))
    return {
        "timescales": timescales,
        "track_ids": track_ids,
        "stsd": [bytes(item).hex()[:64] for item in stsd],
        "edit_shift": edits,
    }


def test_initialisation_segments_are_compatible_across_sessions(tmp_path, hevc_provider):
    """A restart must not be served through an init written for another session."""
    timeline, plan, streams = _make(tmp_path, hevc_provider)

    async def run():
        captures = {}
        seen = None
        try:
            for number in (0, 3, 1):
                await _bounded(_open(streams, plan, number), 90)
                session = plan.session
                # Serving a retained window needs no session; a new one is the
                # interesting case, because it writes its own init.
                if session is None or session is seen:
                    continue
                seen = session
                captures[number] = {track.kind: track.init for track in session.tracks}
                await _bounded(session.close("test"), 30)
                plan.session = None
        finally:
            await streams_running(streams)
        return captures

    captures = asyncio.run(run())
    first = captures[0]
    for number, capture in captures.items():
        for kind in ("video", "audio"):
            if not capture.get(kind):
                continue
            mine = _init_identity(capture[kind])
            base = _init_identity(first[kind])
            assert mine["timescales"] == base["timescales"], f"session {number} {kind} timescale"
            assert mine["track_ids"] == base["track_ids"], f"session {number} {kind} track id"
            assert mine["stsd"] == base["stsd"], f"session {number} {kind} codec configuration"
            assert mine["edit_shift"] == base["edit_shift"], (
                f"session {number} {kind} edit list differs from the first session "
                f"({mine['edit_shift']} vs {base['edit_shift']}), so the init written "
                "by the first session cannot describe this session"
            )


def test_a_seek_sequence_keeps_the_timeline_and_sync(tmp_path, hevc_provider):
    """Start, forward seek, backward seek, and a repeat must all map to the cue.

    Every served pair is measured the way a player reads it: the pinned init plus
    the segment it names, through the init's own edit list. Old and new session
    requests overlap on purpose, because a player does that after a seek.
    """
    timeline, plan, streams = _make(tmp_path, hevc_provider)
    frame = 1 / FRAME_RATE
    audio = timeline.audios[0]

    def assert_window(number, label):
        start = timeline.origin + timeline.segments[number][0]
        video = _served_presentation(
            (plan.root / "init.mp4").read_bytes(), plan.video_segment_path(number).read_bytes()
        )
        track = _served_presentation(
            (plan.root / audio.init).read_bytes(),
            plan.audio_segment_path(audio, number).read_bytes(),
        )
        assert abs(video - start) <= frame * 3.05, (label, number, video, start)
        assert abs(track - start) <= frame * 3.05, (label, number, track, start)
        # Picture may lead by the leading pictures of its opening CRA; both
        # tracks are still one continuous partition, so the gap is bounded.
        assert abs(track - video) <= frame * 4.05, (label, number, track, video)

    async def run():
        try:
            # Startup, then a forward seek, then a backward seek.
            for number in (0, 1, 2):
                await _bounded(_open(streams, plan, number), 90)
                assert_window(number, "startup")
            await _bounded(_open(streams, plan, 4), 90)
            assert_window(4, "forward seek")
            await _bounded(_open(streams, plan, 1), 90)
            assert_window(1, "backward seek")
            # A request for the old position arrives after the restart.
            try:
                await _bounded(_open(streams, plan, 3), 90)
                assert_window(3, "overlapping old request")
            except native_hls.NativeError as error:
                assert error.code in {"stale", "restarted", "incomplete"}, error.code
            # The evicted window has to be remade by a restart.
            plan.video_segment_path(1).unlink(missing_ok=True)
            plan.audio_segment_path(audio, 1).unlink(missing_ok=True)
            await _bounded(_open(streams, plan, 1), 90)
            assert_window(1, "repeat after eviction")
            if plan.session is not None:
                await _bounded(plan.session.close("test"), 30)
        finally:
            await streams_running(streams)

    asyncio.run(run())


def test_the_pump_stays_ahead_of_the_player_but_bounded(server_run):
    records = server_run["records"]
    for record in records:
        assert record["frontier"] - record["active"] <= native_hls._LEAD_WINDOWS + 1, (
            f"the pump ran {record['frontier'] - record['active']} windows ahead"
        )
    assert server_run["peak"] <= native_hls._SPOOL_PLAN_LIMIT, (
        f"peak spool {server_run['peak']} exceeds the plan budget"
    )
    # One forward read, not one read per window: the bytes pulled from the
    # provider are the file, not a multiple of it.
    assert records[-1]["read_bytes"] <= len(server_run["payload"]) + 9 * 1024 * 1024


def test_a_seek_restarts_the_session_at_the_containing_cue(tmp_path, hevc_provider):
    timeline, plan, streams = _make(tmp_path, hevc_provider)

    async def run():
        try:
            await _bounded(_open(streams, plan, 3), 90)
            first = plan.session
            assert first.start == 3
            start, end = timeline.segments[3]
            path = _window_join(
                tmp_path,
                {"init": (plan.root / "init.mp4").read_bytes(),
                 "video": plan.video_segment_path(3).read_bytes()},
                "seek3.mp4",
            )
            times = _frames(path)
            assert start - 0.06 <= times[0] - timeline.origin <= start + 0.2
            assert times[-1] - timeline.origin <= end + 0.2
            # A backward seek cannot rewind the process: it is a new session.
            await _bounded(_open(streams, plan, 1), 90)
            second = plan.session
            assert second is not first, "a backward seek must restart the forward session"
            assert second.start == 1
            assert plan.video_segment_path(1).is_file()
            await _bounded(second.close("test"), 30)
        finally:
            await streams_running(streams)

    asyncio.run(run())


def test_an_evicted_window_is_rebuilt_by_a_restart(tmp_path, hevc_provider):
    timeline, plan, streams = _make(tmp_path, hevc_provider)

    async def run():
        try:
            await _open(streams, plan, 0)
            await _open(streams, plan, 1)
            await _open(streams, plan, 2)
            original = plan.video_segment_path(1).read_bytes()
            # Simulate retention: the file is gone but the session has passed it.
            plan.video_segment_path(1).unlink()
            for audio in timeline.audios:
                plan.audio_segment_path(audio, 1).unlink()
            assert plan.session.frontier >= 2
            assert not plan.session.can_serve(1)
            await _open(streams, plan, 1)
            assert plan.session.start == 1
            rebuilt = plan.video_segment_path(1).read_bytes()
            assert _frames(
                _window_join(
                    tmp_path,
                    {"init": (plan.root / "init.mp4").read_bytes(), "video": rebuilt},
                    "rebuilt.mp4",
                )
            ) == _frames(
                _window_join(
                    tmp_path,
                    {"init": (plan.root / "init.mp4").read_bytes(), "video": original},
                    "original.mp4",
                )
            )
            await plan.session.close("test")
        finally:
            await streams_running(streams)

    asyncio.run(run())


def test_a_window_is_only_served_when_its_own_files_exist(tmp_path, hevc_provider):
    timeline, plan, streams = _make(tmp_path, hevc_provider)

    async def run():
        try:
            await _open(streams, plan, 0)
            assert streams._media_ready(plan, 0)
            assert not streams._media_ready(plan, 4)
            assert plan.session.can_serve(4), "a near window is a wait, not a restart"
            assert not plan.session.can_serve(-1)
            assert plan.session.can_serve(0)
            await plan.session.close("test")
        finally:
            await streams_running(streams)

    asyncio.run(run())


def test_continuous_mode_is_behind_a_flag(monkeypatch):
    monkeypatch.delenv(native_hls._CONTINUOUS_FLAG, raising=False)
    assert continuous_enabled() is False
    monkeypatch.setenv(native_hls._CONTINUOUS_FLAG, "1")
    assert continuous_enabled() is True
    plan = NativePlan(key="k", url="https://x/y.mkv", duration=10.0, root=Path("."))
    assert plan.continuous is False, "only a started plan may turn the new route on"


def test_fragment_decode_times_keep_their_spacing():
    first = _synthetic_fragment(0, 1000)
    second = shift_decode_times(first, 12345)
    assert fragment_bases(second) == [(fragment_bases(first)[0][0], 4, 12345)]
    # A zero decode time is not a fragment clock, and a size-less box is refused.
    with pytest.raises(ValueError):
        shift_decode_times(_synthetic_init(1000), 5)


def test_zero_sized_box_is_refused_without_buffering():
    class Stream:
        def __init__(self, data):
            self.data = bytearray(data)

        async def read(self, count):
            part, self.data = bytes(self.data[:count]), self.data[count:]
            return part

    class Zero:
        def __init__(self):
            self.data = struct.pack(">I", 0) + b"mdat"

        async def read(self, count):
            part, self.data = self.data[:count], self.data[count:]
            return part

    with pytest.raises(native_hls.NativeError):
        asyncio.run(native_hls._read_box(Zero()))

    reader = Stream(struct.pack(">I", 8) + b"free")
    box = asyncio.run(native_hls._read_box(reader))
    assert box == (b"free", struct.pack(">I", 8) + b"free")
