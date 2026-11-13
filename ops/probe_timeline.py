"""Timeline probe for native HLS playback: declared boundaries versus real content.

The defect this measures: the on-demand seek engine declares a segment boundary
and the generated chunk starts earlier than that boundary and runs longer than
the declared duration. The player's clock then steps backwards at every chunk
edge, the picture replays the tail of the previous chunk, and subtitles slide
against the picture. The durable fix is a prepared store where one ffmpeg HLS
pass writes the segments and the playlist together, so a declared duration
cannot disagree with the file it describes. This probe is the machine check for
both sides of that change: it reports what the live build serves, and it can
recognise a correct timeline without touching the provider at all.

Run against the live build from the workstation, the script fed over stdin:

    ssh omakase-ct "docker exec \
        -e OMAKASE_ACCEPT_WATCH='/plus/watch/title/tv/247718/1/1' \
        -i omakase-plus python -" < ops/probe_timeline.py

Another watch path is the same command with a different environment value:

    ssh omakase-ct "docker exec \
        -e OMAKASE_ACCEPT_WATCH='/plus/watch/title/movie/1234' \
        -i omakase-plus python -" < ops/probe_timeline.py

Analyse a local HLS directory instead of the app, which is how the synthetic
control proves the probe recognises a correct timeline:

    ssh omakase-ct "docker exec \
        -e OMAKASE_PROBE_CONTROL=/home/omakase/data/timeline-control \
        -e OMAKASE_PROBE_SEGMENTS='0,1,2,last' \
        -i omakase-plus python -" < ops/probe_timeline.py

Environment:
    OMAKASE_ACCEPT_WATCH    watch path for live mode; required there
    OMAKASE_PROBE_CONTROL   directory holding stream.m3u8 and its segments
    OMAKASE_PROBE_SEGMENTS  comma list of segment indexes, "last" allowed;
                            default 0,1,2,20,last
    OMAKASE_PROBE_FRAMES    frames compared at each boundary; default 6
    OMAKASE_PROBE_ROOT      live segment storage; default <data>/timeline-probe

Design notes that matter:

- index.m3u8 is requested before stream.m3u8. The seek plan only activates on
  the index request, so the other order measures the provider path and reports
  a timeline we do not serve.
- Segment bytes are written under the app data directory, never /tmp (a 128 MB
  tmpfs on this host) and never /dev/shm (64 MB).
- The declared timeline comes from the playlist alone. The player accumulates
  EXTINF values into its clock, so the cumulative sum is the boundary being
  checked, not an internal prediction of ours.
- Each boundary is checked twice: ffprobe gives the first and last video PTS of
  the chunk, and ffmpeg framemd5 gives decoded frame hashes, so "the same
  picture twice" is a hash match rather than an opinion.
- A constant container start offset (the mpegts muxer preload) is normal and
  harmless; the probe reports it separately from the per-segment error that
  makes a boundary replay.
- Exit code is zero whenever the probe produced numbers, because a timeline
  defect is a reported fact and not a probe failure. Only an unrunnable probe,
  or one that measured no segment at all, exits non-zero (2).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
import traceback
from collections import Counter
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

_VERSION = 1
_DEFAULT_INDEXES = "0,1,2,20,last"
_DEFAULT_FRAMES = 6
_PROBE_ROOT = "timeline-probe"
_PROBE_TIMEOUT = 180
# One frame of 23.976 fps is 0.0417 s. The tolerance covers a frame of boundary
# rounding plus playlist EXTINF rounding, and is far below the ~1.1 s defect.
_TOLERANCE = 0.15
_MAX_EXTRA_SEGMENTS = 4
_FMP4_INIT = "init.mp4"
_SEGMENT_SUFFIX = (".ts", ".m4s")
_EXTINF = re.compile(r"#EXTINF:([0-9]+(?:\.[0-9]+)?)")
_LANGUAGE = re.compile(r'LANGUAGE="([^"]*)"')
_NAME = re.compile(r'NAME="([^"]*)"')
_RUN_NAME = re.compile(r"[0-9]{8}T[0-9]{6}")
_ZERO_HASH = "0" * 32


class ProbeError(RuntimeError):
    """The probe cannot produce numbers."""


def _tool(name):
    path = shutil.which(name)
    if not path:
        raise ProbeError(f"{name} is not installed in this container")
    return path


def _execute(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise ProbeError(f"{Path(args[0]).name} failed: {error}") from None


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def _max_abs(values):
    magnitudes = [abs(value) for value in values]
    return round(max(magnitudes), 3) if magnitudes else None


def _positive_int(value, default):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _parse_indexes(text):
    chosen = []
    for token in str(text).split(","):
        token = token.strip().lower()
        if not token:
            continue
        if token == "last":
            selector = "last"
        else:
            try:
                selector = int(token)
            except ValueError:
                raise ProbeError(
                    f"segment selector {token!r} is neither an index nor 'last'"
                ) from None
            if selector < 0:
                raise ProbeError(f"segment selector {selector} is negative")
        if selector not in chosen:
            chosen.append(selector)
    return tuple(chosen) or (0,)


class Config:
    """Probe behaviour, read from the environment only."""

    def __init__(self, environ):
        self.control = (environ.get("OMAKASE_PROBE_CONTROL") or "").strip()
        self.watch_path = (environ.get("OMAKASE_ACCEPT_WATCH") or "").strip()
        self.indexes = _parse_indexes(environ.get("OMAKASE_PROBE_SEGMENTS") or _DEFAULT_INDEXES)
        self.frames = _positive_int(environ.get("OMAKASE_PROBE_FRAMES"), _DEFAULT_FRAMES)
        self.root = (environ.get("OMAKASE_PROBE_ROOT") or "").strip()


class _Timeline:
    """Declared timeline read from one media playlist."""

    def __init__(self, text):
        self.segments = []
        self.target_duration = None
        self.playlist_type = None
        pending = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                match = _EXTINF.match(line)
                if not match:
                    raise ProbeError(f"playlist has an EXTINF without a duration: {line!r}")
                pending = float(match[1])
                continue
            if line.startswith("#EXT-X-TARGETDURATION:"):
                with suppress(ValueError):
                    self.target_duration = int(float(line.split(":", 1)[1]))
                continue
            if line.startswith("#EXT-X-PLAYLIST-TYPE:"):
                self.playlist_type = line.split(":", 1)[1].strip() or None
                continue
            if line.startswith("#"):
                continue
            if pending is None:
                raise ProbeError(f"playlist names a segment without an EXTINF: {line!r}")
            self.segments.append({"name": line, "duration": pending})
            pending = None
        if not self.segments:
            raise ProbeError("the stream playlist has no segments")
        declared = 0.0
        for segment in self.segments:
            segment["start"] = round(declared, 6)
            declared += segment["duration"]
            segment["end"] = round(declared, 6)


def _master(text):
    """What a master playlist offers; empty when the body is a media playlist."""
    lines = [line.strip() for line in text.splitlines()]
    variants = []
    for position, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        for following in lines[position + 1 :]:
            if following and not following.startswith("#"):
                variants.append(following)
                break
    subtitles = [
        line for line in lines if line.startswith("#EXT-X-MEDIA:") and "TYPE=SUBTITLES" in line
    ]
    languages = [match.group(1) for line in subtitles if (match := _LANGUAGE.search(line))]
    names = [match.group(1) for line in subtitles if (match := _NAME.search(line))]
    return {
        "is_master": bool(variants),
        "variants": variants,
        "subtitle_renditions": len(subtitles),
        "subtitle_languages": languages,
        "subtitle_names": names,
    }


def _select(selectors, count):
    """Resolve requested selectors against the playlist, keeping the order."""
    chosen = []
    skipped = []
    for selector in selectors:
        number = count - 1 if selector == "last" else selector
        if not 0 <= number < count:
            skipped.append({"index": selector, "reason": f"outside 0..{count - 1}"})
            continue
        if number not in chosen:
            chosen.append(number)
    return chosen, skipped


def _expand(chosen, count):
    """Add one neighbour to requested samples that would otherwise have no pair.

    A boundary check needs both sides of the edge, so sampling segment 20 is only
    useful together with 21 (or 19). Requested samples that already sit next to
    another requested sample keep that natural pair and ask for nothing extra.
    """
    measured = set(chosen)
    extra = []
    for number in chosen:
        if number - 1 in measured or number + 1 in measured:
            continue
        candidate = number + 1 if number + 1 < count else number - 1
        if 0 <= candidate < count and candidate not in measured and candidate not in extra:
            extra.append(candidate)
        if len(extra) >= _MAX_EXTRA_SEGMENTS:
            break
    return sorted(chosen + extra), sorted(extra)


def _measure(path):
    """First and last video packet PTS, and the frame interval, from ffprobe."""
    result = _execute(
        [
            _tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "csv=p=0",
            str(path),
        ]
    )
    values = []
    for line in result.stdout.splitlines():
        try:
            values.append(float(line.strip().split(",")[0]))
        except (IndexError, ValueError):
            continue
    if not values:
        raise ProbeError("ffprobe found no video packets")
    values.sort()
    gaps = sorted(second - first for first, second in zip(values, values[1:]) if second > first)
    frame = round(_median(gaps), 4) if gaps else None
    return round(values[0], 3), round(values[-1], 3), frame, len(values)


def _frame_md5(path):
    """Decoded frame hashes for one segment, in presentation order."""
    result = _execute(
        [
            _tool("ffmpeg"),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-an",
            "-sn",
            "-f",
            "framemd5",
            "-",
        ]
    )
    hashes = []
    for line in result.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 6:
            continue
        digest = fields[5]
        # A zero hash means ffmpeg could not decode that frame. It is not
        # evidence that two different moments look the same, so drop it.
        if not digest or digest == _ZERO_HASH:
            continue
        hashes.append(digest)
    if not hashes:
        raise ProbeError("ffmpeg produced no frame hashes")
    return hashes


def _shared_frames(tail, head):
    """Multiset intersection of the tail hashes and the head hashes."""
    available = Counter(tail)
    shared = []
    for digest in head:
        if available[digest] > 0:
            available[digest] -= 1
            shared.append(digest)
    return shared


def _positions_from_end(hashes, matched):
    """Frame offsets from the end of a segment for each matched digest."""
    positions = []
    for digest in matched:
        for offset, value in enumerate(reversed(hashes)):
            if value == digest:
                positions.append(offset)
                break
    return sorted(positions)


class _HttpSource:
    """Segment bytes from the in-process app, exactly as the player asks for them."""

    def __init__(self, client, base):
        self.client = client
        self.base = base
        self.asset = "stream.m3u8"
        self.status = None
        self.seconds = None
        self.text = ""
        self.index_status = None
        self.index_seconds = None
        self.index_text = ""

    async def fetch(self, name):
        response = await self.client.get(self.base + "/" + name)
        if response.status_code != 200:
            raise ProbeError(f"{name} answered HTTP {response.status_code}")
        return response.content

    async def init_segment(self):
        response = await self.client.get(self.base + "/" + _FMP4_INIT)
        if response.status_code != 200:
            raise ProbeError(f"{_FMP4_INIT} answered HTTP {response.status_code}")
        return response.content

    def local(self, name):
        return None


class _FileSource:
    """Segment bytes read straight off disk, for the synthetic control."""

    def __init__(self, directory):
        self.directory = directory
        self.asset = "stream.m3u8"
        self.status = None
        self.seconds = None
        self.index_status = None
        self.index_seconds = None
        index_path = directory / "index.m3u8"
        self.index_text = ""
        if index_path.is_file():
            self.index_text = index_path.read_text(encoding="utf-8", errors="replace")
        stream_path = directory / "stream.m3u8"
        if not stream_path.is_file():
            raise ProbeError(f"{stream_path} is missing")
        self.text = stream_path.read_text(encoding="utf-8", errors="replace")

    async def fetch(self, name):
        return await asyncio.to_thread(self._read, name)

    def _read(self, name):
        try:
            return (self.directory / name).read_bytes()
        except OSError as error:
            raise ProbeError(f"{name} is unreadable: {error}") from None

    async def init_segment(self):
        return await self.fetch(_FMP4_INIT)

    def local(self, name):
        return self.directory / name


def _run_directory(config, data):
    root = Path(config.root).expanduser().resolve() if config.root else data / _PROBE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 2 * 86400
    for path in root.iterdir():
        if not _RUN_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                shutil.rmtree(path)
        except OSError:
            continue
    run_dir = root / time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_dir.mkdir(exist_ok=True)
    return run_dir


def _store_segment(run_dir, number, name, data, init):
    suffix = Path(name).suffix.lower()
    if suffix not in _SEGMENT_SUFFIX:
        raise ProbeError(f"segment {name!r} is neither MPEG-TS nor fMP4")
    if suffix == ".m4s":
        if not init:
            raise ProbeError("fMP4 segments need init.mp4")
        path = run_dir / f"s{number:05d}.mp4"
        path.write_bytes(init + data)
        return path
    path = run_dir / f"s{number:05d}.ts"
    path.write_bytes(data)
    return path


def _index_report(source):
    report = {
        "asset": "index.m3u8",
        "present": bool(source.index_text),
        "http_status": source.index_status,
        "bytes": len(source.index_text.encode()),
        "seconds": source.index_seconds,
        **_master(source.index_text),
    }
    if source.index_text and "#EXTINF" in source.index_text and not report["is_master"]:
        report["note"] = "index itself is the media playlist (no subtitle group)"
    return report


def _verdict(summary):
    if (
        summary["shared_frames_total"]
        or summary["replayed_frames_total"]
        or (summary["min_abut_gap_s"] is not None and summary["min_abut_gap_s"] < -_TOLERANCE)
    ):
        return "OVERLAP_REPEAT"
    if summary["max_abut_gap_s"] is not None and summary["max_abut_gap_s"] > _TOLERANCE:
        return "GAP_SKIP"
    if summary["max_abs_span_error_s"] is not None and summary["max_abs_span_error_s"] > _TOLERANCE:
        return "SPAN_MISMATCH"
    return "CONSISTENT"


def _summarise(samples, pairs):
    measured = [sample for sample in samples if "start_error_s" in sample]
    gaps = [pair["abut_gap_s"] for pair in pairs if "abut_gap_s" in pair]
    shared = [pair["shared_frames"] for pair in pairs if "shared_frames" in pair]
    replayed = [pair["replayed_frames"] for pair in pairs if "replayed_frames" in pair]
    summary = {
        "segments_requested": sum(1 for sample in samples if sample["requested"]),
        "segments_measured": len(measured),
        "segments_failed": len(samples) - len(measured),
        "pairs_compared": len(pairs),
        "max_abs_start_error_s": _max_abs(sample["start_error_s"] for sample in measured),
        "max_abs_start_error_relative_s": _max_abs(
            sample["start_error_relative_s"]
            for sample in measured
            if "start_error_relative_s" in sample
        ),
        "max_abs_span_error_s": _max_abs(sample["span_error_s"] for sample in measured),
        "max_abs_covered_span_error_s": _max_abs(
            sample["covered_span_error_s"]
            for sample in measured
            if "covered_span_error_s" in sample
        ),
        "min_abut_gap_s": round(min(gaps), 3) if gaps else None,
        "max_abut_gap_s": round(max(gaps), 3) if gaps else None,
        "max_content_overlap_s": round(max(0.0, -min(gaps)), 3) if gaps else None,
        "shared_frames_total": sum(shared) if shared else 0,
        # Frames that the next chunk replays from somewhere in the previous
        # chunk's tail, found through the measured content overlap. The strict
        # last-6-against-first-6 check above misses this when the overlap spans
        # more than six frames, which is the shape of the live defect.
        "replayed_frames_total": sum(replayed) if replayed else 0,
    }
    summary["verdict"] = _verdict(summary)
    return summary


def _control_result(summary, samples, pairs):
    measured = [sample for sample in samples if "start_error_s" in sample]
    relative = [
        abs(sample["start_error_relative_s"])
        for sample in measured
        if "start_error_relative_s" in sample
    ]
    spans = [
        abs(sample["covered_span_error_s"])
        for sample in measured
        if "covered_span_error_s" in sample
    ]
    gaps = [abs(pair["abut_gap_s"]) for pair in pairs if "abut_gap_s" in pair]
    shared = [pair["shared_frames"] for pair in pairs if "shared_frames" in pair]
    replayed = [pair.get("replayed_frames", 0) for pair in pairs]

    def check(name, ok, detail):
        return {"check": name, "ok": bool(ok), "detail": detail}

    checks = [
        check(
            "start error within 0.15 s once the common container offset is removed",
            bool(relative) and max(relative) <= _TOLERANCE,
            {"per_segment_s": [round(value, 3) for value in relative]},
        ),
        check(
            "declared span matches the real span within 0.15 s",
            bool(spans) and max(spans) <= _TOLERANCE,
            {"per_segment_s": [round(value, 3) for value in spans]},
        ),
        check(
            "neighbour segments abut within 0.15 s",
            bool(gaps) and max(gaps) <= _TOLERANCE,
            {"per_pair_s": [round(value, 3) for value in gaps]},
        ),
        check(
            "no frames are shared or replayed between neighbours",
            bool(shared) and sum(shared) == 0 and sum(replayed) == 0,
            {"shared_per_pair": shared, "replayed_per_pair": replayed},
        ),
    ]
    return {
        "expectation": (
            "one ffmpeg HLS pass: declared durations equal file durations, "
            "neighbours abut, no repeated frames"
        ),
        "checks": checks,
        "result": "PASS" if all(item["ok"] for item in checks) else "FAIL",
        "verdict": summary["verdict"],
    }


async def _sample(source, config, run_dir):
    """Fetch, store, probe and compare the selected segments."""
    timeline = _Timeline(source.text)
    count = len(timeline.segments)
    playlist = {
        "asset": source.asset,
        "http_status": source.status,
        "bytes": len(source.text.encode()),
        "seconds": source.seconds,
        "segment_count": count,
        "declared_total_s": round(timeline.segments[-1]["end"], 3),
        "target_duration_s": timeline.target_duration,
        "playlist_type": timeline.playlist_type,
        "declared_durations": [round(segment["duration"], 3) for segment in timeline.segments],
        "first_segment": timeline.segments[0]["name"],
        "last_segment": timeline.segments[-1]["name"],
        "segment_uris_relative": all(
            not re.match(r"[a-z][a-z0-9+.-]*://", segment["name"], re.IGNORECASE)
            for segment in timeline.segments
        ),
    }
    chosen, skipped = _select(config.indexes, count)
    measured, extra = _expand(chosen, count)
    samples = []
    hashes = {}
    init = None
    for number in measured:
        segment = timeline.segments[number]
        sample = {
            "index": number,
            "name": segment["name"],
            "requested": number in chosen,
            "declared_start_s": segment["start"],
            "declared_end_s": segment["end"],
            "declared_span_s": round(segment["duration"], 3),
        }
        try:
            local = source.local(segment["name"])
            if local is not None:
                if not local.is_file():
                    raise ProbeError(f"{local} is missing")
                path = local
                sample["bytes"] = local.stat().st_size
                sample["stored"] = str(local)
            else:
                if Path(segment["name"]).suffix.lower() == ".m4s" and init is None:
                    init = await source.init_segment()
                data = await source.fetch(segment["name"])
                sample["bytes"] = len(data)
                path = _store_segment(run_dir, number, segment["name"], data, init)
                sample["stored"] = str(path)
            first, last, frame, frames = _measure(path)
            span = last - first
            covered = span + (frame or 0.0)
            sample.update(
                {
                    "first_pts_s": first,
                    "last_pts_s": last,
                    "actual_span_s": round(span, 3),
                    "covered_span_s": round(covered, 3),
                    "frame_duration_s": frame,
                    "frames": frames,
                    "start_error_s": round(first - segment["start"], 3),
                    "span_error_s": round(span - segment["duration"], 3),
                    "covered_span_error_s": round(covered - segment["duration"], 3),
                }
            )
            hashes[number] = await asyncio.to_thread(_frame_md5, path)
        except (ProbeError, OSError) as error:
            sample["error"] = str(error)
        samples.append(sample)

    measured_samples = [sample for sample in samples if "start_error_s" in sample]
    if measured_samples:
        anchor = min(measured_samples, key=lambda sample: sample["index"])
        for sample in measured_samples:
            sample["start_error_relative_s"] = round(
                sample["start_error_s"] - anchor["start_error_s"], 3
            )

    by_index = {sample["index"]: sample for sample in samples}
    pairs = []
    for left_number in sorted(by_index):
        right_number = left_number + 1
        if right_number not in by_index:
            continue
        left = by_index[left_number]
        right = by_index[right_number]
        if not left["requested"] and not right["requested"]:
            continue
        pair = {
            "left": left_number,
            "right": right_number,
            "left_name": left["name"],
            "right_name": right["name"],
        }
        if "last_pts_s" in left and "first_pts_s" in right:
            end = left["last_pts_s"] + (left.get("frame_duration_s") or 0.0)
            pair["abut_gap_s"] = round(right["first_pts_s"] - end, 3)
        left_all = hashes.get(left_number, [])
        right_all = hashes.get(right_number, [])
        if left_all and right_all:
            pair["shared_anywhere_frames"] = len(_shared_frames(left_all, right_all))
        tail = left_all[-config.frames :]
        head = right_all[: config.frames]
        if tail and head:
            shared = _shared_frames(tail, head)
            pair.update(
                {
                    "tail_hashes": tail,
                    "head_hashes": head,
                    "shared_frames": len(shared),
                    "shared_hashes": shared,
                }
            )
        frame = left.get("frame_duration_s")
        if head and left_all and frame and "abut_gap_s" in pair:
            # The declared timeline says the next chunk starts where this one
            # ended. When the measured overlap is positive, the next chunk's
            # first frames are content this chunk already showed, but not at its
            # last frame: they sit ``overlap`` seconds before the end. Widening
            # the tail band by the measured overlap turns that replay into a
            # hash count instead of an inference.
            overlap = max(0.0, -pair["abut_gap_s"])
            window = config.frames + math.ceil(overlap / frame)
            replayed = _shared_frames(left_all[-window:], head)
            pair.update(
                {
                    "overlap_s": round(overlap, 3),
                    "replay_window_frames": window,
                    "replayed_frames": len(replayed),
                    "replayed_frames_from_end": _positions_from_end(left_all, replayed),
                }
            )
        pairs.append(pair)

    return {
        "index": _index_report(source),
        "playlist": playlist,
        "samples": samples,
        "pairs": pairs,
        "summary": _summarise(samples, pairs),
        "skipped_indexes": skipped,
        "neighbour_indexes": extra,
    }


async def _live(config):
    """The live path: the same in-process wiring the Apple TV acceptance check uses."""
    import httpx
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    from omakase.plus import native_tv_test
    from omakase.plus.auth import create_session
    from omakase.plus.db import _resolved_data_dir
    from omakase.plus.debrid_library import PLAYBACK_PROVIDER_KEY
    from omakase.plus.deps import get_db
    from omakase.plus.native_tv_test import COOKIE
    from omakase.plus.secrets import store_secret
    from omakase.plus.web_security import csrf_token

    data = Path(_resolved_data_dir()).resolve()
    run_dir = _run_directory(config, data)
    library = "/plus/api/native-tv-test/v1/library"
    clock = {"now": 1000.0}
    connection = sqlite3.connect(f"file:{data / 'omakase-plus.db'}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    actor = connection.execute("SELECT id AS user_id FROM users ORDER BY id LIMIT 1").fetchone()[
        "user_id"
    ]
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    connection.backup(db)
    connection.close()
    store_secret(db, actor, PLAYBACK_PROVIDER_KEY, "realdebrid")
    templates = Jinja2Templates(directory=str(Path(native_tv_test.__file__).parent / "templates"))
    app = FastAPI()
    app.include_router(
        native_tv_test.create_native_tv_test_router(
            templates,
            lambda request, user, **kw: {
                "csrf_token": csrf_token(request),
                "email": user.email,
                **kw,
            },
            clock=lambda: clock["now"],
            hls_database=lambda: None,
            subtitle_root=str(data / "subtitles"),
            seek_root=str(data / "seek"),
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    origin = native_tv_test._origin()
    cookie = create_session(db, actor)
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"omakase_session={cookie}".encode())]}
    )
    transport = httpx.ASGITransport(app=app)
    report = {"mode": "live", "storage": {"data_dir": str(data), "run_dir": str(run_dir)}}
    async with httpx.AsyncClient(transport=transport, base_url=origin, timeout=500) as client:
        client.cookies.set("omakase_session", cookie)
        issued = await client.post(
            "/plus/native-tv-library",
            follow_redirects=True,
            data={"csrf_token": csrf_token(request), "action": "issue"},
        )
        code = re.search(r"#library-([A-F0-9]{20})", issued.text)
        if not code:
            raise ProbeError(f"library issue returned no code (HTTP {issued.status_code})")
        redeemed = await client.post(library + "/redeem", json={"code": code[1]})
        if redeemed.status_code != 200:
            raise ProbeError(f"library redeem answered HTTP {redeemed.status_code}")
        client.headers["Authorization"] = "Bearer " + redeemed.json()["token"]
        clock["now"] += 61
        started = time.monotonic()
        play = await client.post(library + "/play", json={"watch_path": config.watch_path})
        play_seconds = round(time.monotonic() - started, 2)
        if play.status_code != 200:
            raise ProbeError(f"play answered HTTP {play.status_code}")
        media = play.json()
        client.cookies.set(COOKIE, media["media_token"])
        base = "/plus/api/native-tv-test/v1/media/" + media["media_id"]
        report["media"] = {"media_id": media["media_id"], "play_s": play_seconds}
        source = _HttpSource(client, base)
        try:
            # The seek plan only activates on the index request. Asking for the
            # stream playlist first measures the provider path instead.
            started = time.monotonic()
            index = await client.get(base + "/index.m3u8")
            source.index_status = index.status_code
            source.index_seconds = round(time.monotonic() - started, 2)
            source.index_text = index.text
            if index.status_code != 200:
                raise ProbeError(f"index.m3u8 answered HTTP {index.status_code}")
            master = _master(index.text)
            asset = master["variants"][0] if master["variants"] else "stream.m3u8"
            started = time.monotonic()
            stream = await client.get(base + "/" + asset)
            source.asset = asset
            source.status = stream.status_code
            source.seconds = round(time.monotonic() - started, 2)
            source.text = stream.text
            if stream.status_code != 200:
                raise ProbeError(f"{asset} answered HTTP {stream.status_code}")
            if "#EXTINF" not in source.text and "#EXTINF" in index.text:
                # No subtitle group: the index body was already the media playlist.
                source.asset = "index.m3u8"
                source.status = index.status_code
                source.seconds = source.index_seconds
                source.text = index.text
            report.update(await _sample(source, config, run_dir))
        finally:
            with suppress(Exception):
                await client.post(library + "/media/" + media["media_id"] + "/close")
    return report


async def _control(config):
    """The provider-free path: a directory written by one HLS pass."""
    directory = Path(config.control).expanduser().resolve()
    if not directory.is_dir():
        raise ProbeError(f"control directory {directory} does not exist")
    source = _FileSource(directory)
    report = {"mode": "control", "control_dir": str(directory)}
    report.update(await _sample(source, config, None))
    report["control"] = _control_result(report["summary"], report["samples"], report["pairs"])
    report["result"] = report["control"]["result"]
    return report


async def _run(config):
    report = {
        "probe": "timeline",
        "version": _VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "watch_path": config.watch_path or None,
        "requested_indexes": [str(selector) for selector in config.indexes],
        "frames_compared": config.frames,
    }
    if config.control:
        report.update(await _control(config))
    else:
        report.update(await _live(config))
    report.setdefault("result", "MEASURED")
    return report


def _error_report(error, debug):
    detail = f"{type(error).__name__}: {error}"
    if debug:
        detail = traceback.format_exc()
    return {"probe": "timeline", "result": "ERROR", "error": detail}


def main(environ=None):
    environ = os.environ if environ is None else environ
    debug = bool(environ.get("OMAKASE_PROBE_DEBUG"))
    began = time.monotonic()
    try:
        config = Config(environ)
        if not config.control and not config.watch_path:
            raise ProbeError(
                "set OMAKASE_ACCEPT_WATCH to a watch path, "
                "or OMAKASE_PROBE_CONTROL to a local HLS directory"
            )
        _tool("ffmpeg")
        _tool("ffprobe")
        report = asyncio.run(_run(config))
    except ProbeError as error:
        print(json.dumps(_error_report(error, debug), indent=1))
        return 2
    except Exception as error:  # noqa: BLE001 - the report is the output
        print(json.dumps(_error_report(error, debug), indent=1))
        return 2
    report["wall_s"] = round(time.monotonic() - began, 1)
    print(json.dumps(report, indent=1))
    if report.get("summary", {}).get("segments_measured", 0) <= 0:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
