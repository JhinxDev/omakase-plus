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
from types import SimpleNamespace

import pytest

from omakase.plus import native_hls
from omakase.plus.native_hls import (
    NativePlan,
    NativeStreams,
    build_timeline,
    continuous_enabled,
    fragment_bases,
    shift_decode_times,
)
from tests.plus.test_native_hls import (
    DURATION,
    FRAME_RATE,
    _build_fixture,
    _build_hevc_fixture,
    _Provider,
    _synthetic_fragment,
    _synthetic_init,
)

FFMPEG = native_hls  # the module is exercised through its own ffmpeg calls
SEGMENT_SECONDS = 6.006

# Every external process in this file runs under a finite deadline. A test that
# waits forever reports nothing and hides the defect it exists to catch, so a
# timeout fails loudly with the command, the deadline, and whatever the child
# managed to write before it was killed.
PROCESS_TIMEOUT_SECONDS = 120


def _tail(stream, limit: int = 800) -> str:
    if stream is None:
        return "<none>"
    text = stream.decode("utf-8", "ignore") if isinstance(stream, bytes) else str(stream)
    return text[-limit:] or "<empty>"


def _bounded_run(cmd, *, timeout: float = PROCESS_TIMEOUT_SECONDS, **kwargs):
    try:
        return subprocess.run(cmd, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as expired:
        raise AssertionError(
            f"external process exceeded its {timeout:g}s deadline: {cmd!r}\n"
            f"captured stdout: {_tail(expired.stdout)}\n"
            f"captured stderr: {_tail(expired.stderr)}"
        ) from expired


def _run(*args) -> None:
    result = _bounded_run(args, capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "ignore")[-800:]


def _frames(path: Path, stream: str = "v:0") -> list[float]:
    result = _bounded_run(
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
    # Without this check a failed probe returned an empty list, which the caller
    # then reported as a missing or shortened sequence: a probe failure must
    # never be able to masquerade as a continuity defect.
    assert result.returncode == 0, f"ffprobe failed for {path}: {_tail(result.stderr)}"
    return sorted(
        float(row.strip().rstrip(",")) for row in result.stdout.strip().splitlines() if row.strip()
    )


def _continuous_remux(source: Path, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    for item in out.iterdir():
        item.unlink()
    init = out / "init.mp4"
    run = _bounded_run(
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
    plan = NativePlan(key=name, url=timeline.url, duration=timeline.duration, root=tmp_path / name)
    plan.root.mkdir(parents=True, exist_ok=True)
    plan.timeline = timeline
    plan.ready.set()
    plan.continuous = True
    streams = NativeStreams(tmp_path / "spool")
    streams.root.mkdir(parents=True, exist_ok=True)
    return timeline, plan, streams


def _joined(tmp_path, name, init_path, media_paths):
    path = tmp_path / name
    path.write_bytes(b"".join(part.read_bytes() for part in [init_path, *media_paths]))
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
    result = _bounded_run(
        ["ffmpeg", "-v", "error", "-i", str(joined), "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    assert (result.stderr or "").strip() == "", result.stderr[:400]


def test_dense_keyframes_share_one_playlist_window(tmp_path):
    """A playlist window contains several muxer fragments on real HEVC sources."""
    source = _build_hevc_fixture(tmp_path, keyframe_frames=24)
    provider = _Provider(source.read_bytes())
    try:
        timeline, plan, streams = _make(tmp_path, provider)
        parts = []

        async def run():
            try:
                for number in range(len(timeline.segments)):
                    await _bounded(_open(streams, plan, number), 20)
                    parts.append(plan.video_segment_path(number).read_bytes())
                # Exercise a distant restart after deliberately evicting a window.
                number = 2
                previous = plan.session
                plan.video_segment_path(number).unlink()
                await _bounded(_open(streams, plan, number), 20)
                assert plan.session is not previous
                output = tmp_path / "seek.mp4"
                output.write_bytes(
                    (plan.root / "init.mp4").read_bytes()
                    + plan.video_segment_path(number).read_bytes()
                )
                times = _frames(output)
                assert times
                assert abs(times[0] - timeline.segments[number][0]) < 0.25
            finally:
                if plan.session is not None:
                    await plan.session.close("test")
                if streams.client is not None:
                    await streams.client.aclose()

        asyncio.run(run())
        output = tmp_path / "dense.mp4"
        output.write_bytes((plan.root / "init.mp4").read_bytes() + b"".join(parts))
        assert len(fragment_bases(parts[0])) > 1
        assert _picture_hashes(output) == _picture_hashes(source)
    finally:
        provider.close()


def _picture_hashes(path):
    decoded = _bounded_run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-fps_mode",
            "passthrough",
            "-f",
            "framemd5",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=25,
        check=True,
    )
    assert not decoded.stderr
    return [
        line.rsplit(",", 1)[-1].strip()
        for line in decoded.stdout.splitlines()
        if line and not line.startswith("#")
    ]


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


def _pass_times(server_run, kind: str) -> list[float]:
    """Decoded presentation times of the served pass, in playlist order."""
    folder = server_run["folder"]
    records = server_run["records"]
    audio = server_run["timeline"].audios[0]
    if kind == "video":
        parts = [records[0]["init"], *[record["video"] for record in records]]
    else:
        parts = [
            records[0]["audio_inits"][audio.ordinal],
            *[record["audios"][audio.ordinal] for record in records],
        ]
    path = folder / f"pass-{kind}.mp4"
    path.write_bytes(b"".join(parts))
    return _frames(path, "v:0" if kind == "video" else "a:0")


def test_the_opening_window_presents_every_source_frame(server_run):
    """The pass is the source picture sequence, translated by one common shift.

    This is the assertion that used to skip the opening window. Every source
    picture is matched to its own served picture by position in the decoded
    sequence: a missing picture changes the count, and a picture landing anywhere
    but the common offset widens the spread. Measured on this fixture: 720 of 720
    pictures present, spread inside one frame, common shift 0.166 s. The shift is
    the session origin the muxer has to use because a picture whose decode time
    would be negative cannot be written; it is common to every picture, so it is a
    translation and not a loss.
    """
    source = _frames(server_run["source"], "v:0")
    served = _pass_times(server_run, "video")
    frame = 1 / FRAME_RATE
    assert len(served) == len(source), (
        f"{len(served)} served pictures against {len(source)} source pictures"
    )
    deltas = [got - want for want, got in zip(source, served)]
    spread = max(deltas) - min(deltas)
    shift = sorted(deltas)[len(deltas) // 2]
    assert spread <= frame, f"pictures not translated by one constant: spread {spread:.4f}s"
    assert 0.0 <= shift <= 0.25, f"measured common shift {shift:.4f}s"
    # The same bytes must also decode to the same pictures, not just the same
    # count: the executable continuity gate compares decoded hashes.
    assert len({round(got, 4) for got in served}) == len(served), "duplicate presentation time"


def test_audio_and_video_share_the_window_clock(server_run):
    """Both renditions retain every frame and preserve relative source timing."""
    offsets = {}
    for kind, stream in (("video", "v:0"), ("audio", "a:0")):
        source = _frames(server_run["source"], stream)
        served = _pass_times(server_run, kind)
        assert len(served) == len(source), (kind, len(served), len(source))
        deltas = [got - want for want, got in zip(source, served, strict=True)]
        shift = sorted(deltas)[len(deltas) // 2]
        assert max(abs(delta - shift) for delta in deltas) <= 0.003, kind
        offsets[kind] = shift
    assert abs(offsets["audio"] - offsets["video"]) <= 0.003, offsets


def test_subtitles_come_from_one_bounded_window_read(tmp_path, hevc_provider):
    """A text track is served from the qualified window path, only on request."""
    timeline, plan, streams = _make(tmp_path, hevc_provider)

    async def step(label, awaitable, seconds=60):
        return await asyncio.wait_for(awaitable, seconds)

    async def run():
        try:
            await step("open", _open(streams, plan, 1, subtitles=1))
            assert not plan.subtitle_segment_path(0, 1).exists()
            assert plan.session is not None
            provider_requests = len(hevc_provider.requests)
            await step("subtitles", streams.ensure_subtitles(plan, 1, 1))
            assert len(hevc_provider.requests) == provider_requests
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


def test_webvtt_timestamp_map_matches_served_media_after_a_backward_seek(tmp_path, hevc_fixture):
    shifted = tmp_path / "shifted.mkv"
    _run(
        "ffmpeg",
        "-v",
        "error",
        "-copyts",
        "-i",
        str(hevc_fixture),
        "-map",
        "0",
        "-c",
        "copy",
        "-output_ts_offset",
        "0.250",
        str(shifted),
    )
    provider = _Provider(shifted.read_bytes())
    try:
        timeline, plan, streams = _make(tmp_path, provider)
        assert timeline.origin >= 0.250
        audio = timeline.audios[0]

        async def run():
            try:
                await _bounded(_open(streams, plan, 4, subtitles=1))
                first = plan.session
                await _bounded(_open(streams, plan, 1, subtitles=1))
                assert plan.session is not first and plan.session.start == 1
                await _bounded(streams.ensure_subtitles(plan, 1, 1))

                caption = plan.subtitle_segment_path(0, 1).read_text(encoding="utf-8")
                match = re.search(
                    r"^X-TIMESTAMP-MAP=LOCAL:00:00:00[.]000,MPEGTS:(\d+)$",
                    caption,
                    re.MULTILINE,
                )
                assert match, caption
                mapped_start = int(match[1]) / 90_000
                assert mapped_start == pytest.approx(timeline.segments[1][0] + timeline.origin)
                cue = next(
                    line.partition(" --> ")[0] for line in caption.splitlines() if " --> " in line
                )
                caption_pts = native_hls._vtt_seconds(cue) + mapped_start
                assert caption_pts == pytest.approx(
                    _source_packet_times(shifted, "s:0")[1], abs=0.001
                )

                # A segment can include reordered picture or earlier audio.
                # Compare packets by content around the cue, not the first PTS.
                for stream, init, media in (
                    ("v:0", plan.root / "init.mp4", plan.video_segment_path(1)),
                    ("a:0", plan.root / audio.init, plan.audio_segment_path(audio, 1)),
                ):
                    joined = tmp_path / f"seek-{stream[0]}.mp4"
                    joined.write_bytes(init.read_bytes() + media.read_bytes())
                    source = _packet_clock(shifted, stream)
                    served = _packet_clock(joined, stream)
                    near_cue = min(
                        (key for key in source if key in served),
                        key=lambda key: abs(source[key] - caption_pts),
                    )
                    assert served[near_cue] == pytest.approx(caption_pts, abs=3 / FRAME_RATE)
            finally:
                if plan.session is not None:
                    await _bounded(plan.session.close("test"), 30)
                await streams_running(streams)

        asyncio.run(run())
    finally:
        provider.close()


def _packet_clock(path: Path, stream: str) -> dict[str, float]:
    result = _bounded_run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_packets",
            "-show_data_hash",
            "sha256",
            "-show_entries",
            "packet=pts_time,data_hash",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        row["data_hash"]: float(row["pts_time"])
        for row in json.loads(result.stdout)["packets"]
        if "pts_time" in row and "data_hash" in row
    }


def _source_packet_times(path: Path, stream: str) -> list[float]:
    result = _bounded_run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            stream,
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
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


def _constant_offset(served: list[float], source: list[float], frame: float) -> float:
    """The translation from source time to served time, proven constant.

    Every served frame is matched to its own source frame. A correct pass is one
    constant translation: a served frame that is not on a source frame, or an
    offset that varies by more than a frame, fails here rather than being absorbed
    by a tolerance.
    """
    offsets = []
    unmatched = 0
    tail = 0
    limit = source[-1] + frame / 2
    for value in served:
        if value > limit:
            # The muxer flushes the pictures still in flight when the feed ends.
            # They lie past the source's declared last frame, so the playlist's
            # last EXTINF never addresses them; they are counted, not ignored.
            tail += 1
            continue
        nearest = min(source, key=lambda item: abs(item - value))
        delta = value - nearest
        if abs(delta) > frame:
            unmatched += 1
        offsets.append(delta)
    assert not unmatched, f"{unmatched} served frames do not sit on a source frame"
    # The muxer flushes the pictures still in flight when the feed ends. They lie
    # past the source's declared last frame, so no EXTINF addresses them. At most
    # one reorder depth can be in flight, which is the same four frames this
    # material measures at the head; more than that would mean the tail is running
    # on after the source ended.
    assert tail <= 4, f"{tail} served frames lie past the source's declared end"
    ordered = sorted(offsets)
    spread = ordered[-1] - ordered[0]
    assert spread <= frame, f"served offset varies by {spread:.4f}s, more than one frame"
    return ordered[len(ordered) // 2]


def test_the_timestamp_contract_matches_the_source(server_run):
    """Each window is a piece of one constant translation of the source.

    The pass-level check measures the opening; this one measures every window on
    its own, so a window that drifts relative to the pass is caught even when the
    pass as a whole looks right. No window is skipped and no tolerance is widened:
    a served frame that is not on a source frame fails, and the per-window offsets
    must agree to within one frame.
    """
    folder = server_run["folder"]
    source_video = _source_packet_times(server_run["source"], "v:0")
    frame = 1 / FRAME_RATE
    offsets = []
    for record in server_run["records"]:
        number = record["number"]
        path = _window_join(folder, record, f"contract-v{number}.mp4")
        offset = _constant_offset(_frames(path), source_video, frame)
        offsets.append((number, round(offset, 4)))
    values = [offset for _number, offset in offsets]
    assert max(values) - min(values) <= frame, offsets


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

    Reads the segment the way a player does: the earliest *presented* sample,
    which is min(DTS_i + CTS_i) over every sample of the fragment, mapped through
    the init's own edit list. The fragment's tfdt is already part of each sample's
    decode time, so no separate base term is added here. Using the smallest
    composition offset instead was a lower bound that hid the two-frame reorder
    lead-in of this material.
    """
    timescale = native_hls.media_timescale(init)
    assert timescale
    return native_hls.earliest_presentation(segment) / timescale + native_hls.edit_shift(init)


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
                {
                    "init": (plan.root / "init.mp4").read_bytes(),
                    "video": plan.video_segment_path(3).read_bytes(),
                },
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


def _pipe_fragment(decode_time, composition_offset=0):
    def box(kind, body):
        return struct.pack(">I4s", len(body) + 8, kind) + body

    tfdt = box(b"tfdt", struct.pack(">B3xI", 0, decode_time))
    trun = box(b"trun", struct.pack(">IIII", 0x900, 1, 23, composition_offset))
    return box(b"moof", box(b"traf", tfdt + trun)) + box(b"mdat", b"packet")


def test_audio_pipe_keeps_draining_before_video_clock(tmp_path):
    streams = NativeStreams(tmp_path, free_bytes=lambda: 16 * 1024**3)
    plan = NativePlan("pipe", "https://mock.invalid/source", 12, tmp_path)
    audio = native_hls.NativeAudio(0, "aac", number=2)
    plan.timeline = native_hls.NativeTimeline(
        plan.url,
        12,
        0,
        ((0, 6), (6, 12)),
        (0, 100),
        200,
        b"",
        b"",
        b"",
        "hevc",
        video_number=1,
        audios=(audio,),
    )
    session = native_hls._ContinuousSession(streams, plan, 0)
    session.feed_first = {1: 0, 2: 60}
    video_track = native_hls._ContinuousTrack("video")
    audio_track = native_hls._ContinuousTrack("audio", audio=audio)
    session.tracks = [video_track, audio_track]
    audio_bytes = _synthetic_init(1000) + _pipe_fragment(0) + _pipe_fragment(23)

    async def run():
        audio_drained = asyncio.Event()

        class AudioOutput(asyncio.StreamReader):
            delivered = 0

            async def read(self, count):
                data = await super().read(count)
                self.delivered += len(data)
                if self.delivered == len(audio_bytes):
                    audio_drained.set()
                return data

        audio_output = AudioOutput()
        video_output = asyncio.StreamReader()

        class AudioInput:
            def write(self, data):
                if data == b"first":
                    audio_output.feed_data(audio_bytes)

            async def drain(self):
                # A full stdout pipe prevents the child accepting the input
                # that would let the other muxer emit its first video fragment.
                await audio_drained.wait()

        class VideoInput:
            def write(self, data):
                if data == b"next":
                    video_output.feed_data(_synthetic_init(1000) + _pipe_fragment(0, 83))

            async def drain(self):
                pass

        audio_track.process = SimpleNamespace(stdin=AudioInput(), stdout=audio_output, returncode=0)
        video_track.process = SimpleNamespace(stdin=VideoInput(), stdout=video_output, returncode=0)
        readers = [asyncio.create_task(session._read_fragments(track)) for track in session.tracks]

        async def broadcast():
            await session._broadcast(b"first")
            await session._broadcast(b"next")
            # A quiet track may have no next stdout fragment. Its buffered
            # packets must flush as soon as the video clock arrives.
            while audio_track.fragments < 2:
                await asyncio.sleep(0.005)

        try:
            try:
                await asyncio.wait_for(broadcast(), 5.75)
            except TimeoutError:
                pytest.fail(f"shared input could not reach video: {session.error}")
            for output in (audio_output, video_output):
                output.feed_eof()
            await asyncio.wait_for(asyncio.gather(*readers), 1)
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)

        assert session.error is None
        assert session.clock_delay == pytest.approx(0.083)
        audio_result = plan.audio_segment_path(audio, 0).read_bytes()
        assert [base for _, _, base in fragment_bases(audio_result)] == [143, 166]
        assert audio_result.count(b"packet") == 2

    asyncio.run(run())


def test_audio_clock_buffer_is_shared_and_released_on_cancel(tmp_path, monkeypatch):
    streams = NativeStreams(tmp_path)
    plan = NativePlan("pipe", "https://mock.invalid/source", 12, tmp_path)
    audios = (native_hls.NativeAudio(0, "aac"), native_hls.NativeAudio(1, "aac"))
    plan.timeline = native_hls.NativeTimeline(
        plan.url,
        12,
        0,
        ((0, 6), (6, 12)),
        (0, 100),
        200,
        b"",
        b"",
        b"",
        "hevc",
        audios=audios,
    )
    session = native_hls._ContinuousSession(streams, plan, 0)
    fragment = _pipe_fragment(0)
    monkeypatch.setattr(native_hls, "_AUDIO_CLOCK_BUFFER_BYTES", len(fragment))

    async def run():
        for audio in audios:
            track = native_hls._ContinuousTrack("audio", audio.ordinal, audio)
            output = asyncio.StreamReader()
            output.feed_data(_synthetic_init(1000) + fragment)
            output.feed_eof()
            track.process = SimpleNamespace(stdout=output, returncode=0)
            session.tracks.append(track)
        first, second = session.tracks

        async def wait_for_pending_fragment():
            while session.audio_clock_bytes < len(fragment):
                await asyncio.sleep(0.005)

        pending = asyncio.create_task(session._read_fragments(first))
        try:
            await asyncio.wait_for(wait_for_pending_fragment(), 1)
            await asyncio.wait_for(session._read_fragments(second), 1)
            assert session.error == "audio_clock_buffer"
            assert session.audio_clock_bytes == len(fragment)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

        assert session.audio_clock_bytes == 0
        assert not list(tmp_path.glob("*.m4s"))
        assert not list(tmp_path.glob("*.part"))

    asyncio.run(run())


def test_overlapping_subtitle_requests_share_only_the_finished_window(tmp_path, monkeypatch):
    streams = NativeStreams(tmp_path, free_bytes=lambda: 16 * 1024**3)
    plan = NativePlan("caption", "https://mock.invalid/episode", 12, tmp_path)
    plan.timeline = native_hls.NativeTimeline(
        plan.url, 12, 0, ((0, 6), (6, 12)), (100, 200), 300, b"", b"", b"", "hevc"
    )
    plan.continuous = True
    requests = 0
    raw = "WEBVTT\n\n00:00:05.500 --> 00:00:06.200\nCrossing cue\n"

    async def run():
        producing = asyncio.Event()
        release = asyncio.Event()

        async def produce(
            plan, number, _args, _video_part, _audio_parts, subtitles, *, subtitles_only=False
        ):
            nonlocal requests
            assert subtitles_only
            requests += 1
            for _, path in subtitles:
                path.write_text(raw, encoding="utf-8")
            producing.set()
            await release.wait()
            streams._commit_subtitles(plan, number, subtitles)

        monkeypatch.setattr(streams, "_produce", produce)
        first = asyncio.create_task(streams.ensure_subtitles(plan, 1, 1))
        await producing.wait()
        second = asyncio.create_task(streams.ensure_subtitles(plan, 1, 1))
        await asyncio.sleep(0)
        published_too_early = plan.subtitle_segment_path(0, 1).exists() or second.done()
        release.set()
        await asyncio.gather(first, second)

        assert not published_too_early
        assert requests == 1
        assert "00:00:00.000 --> 00:00:00.200" in plan.subtitle_segment_path(0, 1).read_text()

    asyncio.run(run())


def test_subtitle_clipping_keeps_crossing_and_preceding_cues(tmp_path):
    """A cue that crosses a window edge, and one that began before it.

    Both are clipped to this window and moved onto its clock, and a window with
    no cues at all must still be a valid (empty) WebVTT track, because the player
    requests that URI for every segment the subtitle playlist declares.
    """
    cues = (
        "WEBVTT\n\n"
        "00:00:05.500 --> 00:00:06.200\nbefore the edge\n\n"
        "00:00:08.000 --> 00:00:08.500\ninside the window\n\n"
        "00:00:11.900 --> 00:00:12.400\ncrossing the far edge\n"
    )
    path = tmp_path / "crossing.vtt"
    path.write_text(cues, encoding="utf-8")
    native_hls._clip_webvtt(path, 6.006, 12.012, media_start=6.006)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("WEBVTT")
    # The cue that started before the window keeps only its visible part and is
    # rebased to the window's own clock.
    assert "00:00:00.000 --> 00:00:00.194" in text, text
    assert "before the edge" in text
    # The cue inside the window moves by the window start.
    assert "00:00:01.994 --> 00:00:02.494" in text, text
    # The cue that runs past the far edge is cut at it.
    assert "00:00:05.894 --> 00:00:06.006" in text, text

    empty = tmp_path / "empty.vtt"
    empty.write_text(cues, encoding="utf-8")
    native_hls._clip_webvtt(empty, 18.018, 24.024, media_start=18.018)
    assert empty.read_text(encoding="utf-8") == (
        "WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:1621620\n\n"
    )


def test_a_window_with_no_cues_still_serves_a_track(tmp_path, hevc_provider):
    """A window with no cue in it must still answer its subtitle URI.

    The fixture's cues sit at 2.0-4.0 s and 8.0-10.0 s, so the last window
    (24.024 s onward) has none. The playlist still names that segment, so an empty
    but valid WebVTT file is the correct answer; a missing file would break the
    rendition at that point.
    """
    timeline, plan, streams = _make(tmp_path, hevc_provider)
    last = len(timeline.segments) - 1

    async def run():
        try:
            await _bounded(_open(streams, plan, last), 90)
            await _bounded(streams.ensure_subtitles(plan, last, 1), 90)
            text = plan.subtitle_segment_path(0, last).read_text(encoding="utf-8")
            assert text.startswith("WEBVTT")
            assert "-->" not in text, text
        finally:
            await streams_running(streams)

    asyncio.run(run())


def test_a_cue_is_still_served_after_a_restart(tmp_path, hevc_provider):
    """Subtitles for an earlier window survive a session restarted elsewhere.

    The subtitle path owns no media segment, so it must not evict or rewrite the
    picture and sound of the window it is attached to.
    """
    timeline, plan, streams = _make(tmp_path, hevc_provider)
    audio = timeline.audios[0]

    async def run():
        try:
            await _bounded(_open(streams, plan, 3), 90)
            await _bounded(streams.ensure_subtitles(plan, 3, 1), 90)
            # A jump back to the window that has the cue restarts the media
            # session; the subtitle must still come out on that window's clock and
            # must not have disturbed the media already produced.
            await _bounded(_open(streams, plan, 0), 90)
            video_before = plan.video_segment_path(0).read_bytes()
            audio_before = plan.audio_segment_path(audio, 0).read_bytes()
            await _bounded(streams.ensure_subtitles(plan, 0, 1), 90)
            assert plan.video_segment_path(0).read_bytes() == video_before
            assert plan.audio_segment_path(audio, 0).read_bytes() == audio_before
            local = plan.subtitle_segment_path(0, 0).read_text(encoding="utf-8")
            assert "Hevc line" in local, local
            start = timeline.segments[0][0]
            stamp = (
                next(line for line in local.splitlines() if "-->" in line).split("-->")[0].strip()
            )
            parts = stamp.split(":")
            at = float(parts[2]) + int(parts[1]) * 60 + int(parts[0]) * 3600
            assert abs((timeline.origin + start + at) - 2.0) < 0.06, (at, start)
        finally:
            await streams_running(streams)

    asyncio.run(run())
