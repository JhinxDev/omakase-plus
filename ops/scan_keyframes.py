"""Pre-compute the demuxer keyframe list for one title, between sessions.

Stream copy can only cut at the keyframes the demuxer itself recognises, and the
Matroska cue table does not always agree with it. Reading the whole file once and
caching the demuxer's list lets every later session build its timeline from
boundaries that actually exist, instead of repeating or skipping about a second at
each boundary.

The scan reads the whole file, so it must not run while someone is watching. Run
it between sessions; it writes one JSON file next to the seek cache and the server
picks it up the next time it builds a plan for that media.

    docker exec -i omakase-plus python - < ops/scan_keyframes.py \
        "TV Shows/MobLand/Season 1/Episode 1.mkv"

Exit codes: 0 cached, 2 no seekable direct source, 3 the probe produced no frames.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path


async def resolve(watch_path: str):
    import httpx
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from starlette.requests import Request

    from omakase.plus import debrid_stream, native_tv_test
    from omakase.plus.auth import create_session
    from omakase.plus.db import _resolved_data_dir
    from omakase.plus.debrid_library import PLAYBACK_PROVIDER_KEY
    from omakase.plus.deps import get_db
    from omakase.plus.secrets import store_secret
    from omakase.plus.web_security import csrf_token

    clock = {"now": 1000.0}
    captured = {}
    original = debrid_stream.DebridStreams.prepare

    async def capture(self, source, **kwargs):
        media = await original(self, source, **kwargs)
        captured.setdefault("source", source)
        return media

    debrid_stream.DebridStreams.prepare = capture
    debrid_stream.DebridStreams._start_seek = lambda self, source: None

    path = Path(_resolved_data_dir()).resolve() / "omakase-plus.db"
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
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
            lambda request, user, **kwargs: {
                "csrf_token": csrf_token(request),
                "email": user.email,
                **kwargs,
            },
            clock=lambda: clock["now"],
            hls_database=lambda: None,
            subtitle_root=None,
            seek_root=None,
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
    async with httpx.AsyncClient(transport=transport, base_url=origin, timeout=300) as client:
        client.cookies.set("omakase_session", cookie)
        issued = await client.post(
            "/plus/native-tv-library",
            follow_redirects=True,
            data={"csrf_token": csrf_token(request), "action": "issue"},
        )
        code = re.search(r"#library-([A-F0-9]{20})", issued.text)[1]
        redeemed = await client.post(
            "/plus/api/native-tv-test/v1/library/redeem", json={"code": code}
        )
        client.headers["Authorization"] = "Bearer " + redeemed.json()["token"]
        clock["now"] += 61
        response = await client.post(
            "/plus/api/native-tv-test/v1/library/play", json={"watch_path": watch_path}
        )
        if response.status_code != 200:
            return None, None
        media = response.json()
        await client.post(
            "/plus/api/native-tv-test/v1/library/media/" + media["media_id"] + "/close"
        )
        duration = float(media.get("duration") or 0)
    source = captured.get("source")
    direct = getattr(source, "direct", "")
    return (direct or None), duration


def frame_hashes(path, limit=None):
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:v:0"]
    if limit:
        args += ["-frames:v", str(limit)]
    args += ["-f", "framemd5", "-"]
    result = subprocess.run(args, capture_output=True, text=True, timeout=1800)
    out = []
    for line in result.stdout.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 6:
            out.append(parts[5])
    return out


def measure_offset(direct, keyframes):
    """Measure how far past its boundary the picture actually starts.

    The demuxer can only begin at a keyframe, so a generated chunk can start a
    little after the time the playlist declares. That constant is what makes
    subtitles run early. One small local sample plus one generated chunk measure
    it exactly; when the measurement is not possible this returns 0.0 and leaves
    behaviour unchanged.
    """
    work = Path("/home/omakase/data/seeklab")
    try:
        work.mkdir(parents=True, exist_ok=True)
        boundary = next((value for value in keyframes if value >= 60.0), None)
        if boundary is None:
            return 0.0
        following = [value for value in keyframes if value > boundary + 0.001]
        if not following:
            return 0.0
        target = following[0]
        sample_start = max(0.0, boundary - 10.0)
        sample = work / "offset-sample.mkv"
        built = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                f"{sample_start:.3f}",
                "-i",
                direct,
                "-t",
                "25.000",
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-y",
                str(sample),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if built.returncode != 0 or not sample.is_file():
            return 0.0
        reference = frame_hashes(sample)
        index_of = {}
        for number, value in enumerate(reference):
            index_of.setdefault(value, number)
        rate = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=r_frame_rate",
                "-of",
                "csv=p=0",
                direct,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.strip()
        try:
            numerator, denominator = rate.split("/")
            fps = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            fps = 24000 / 1001
        chunk = work / "offset-chunk.ts"
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                f"{target:.3f}",
                "-i",
                direct,
                "-t",
                "2.000",
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-f",
                "mpegts",
                "-y",
                str(chunk),
            ],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        head = frame_hashes(chunk, limit=1)
        position = index_of.get(head[0]) if head else None
        if position is None:
            return 0.0
        content_start = sample_start + position / fps
        return round(content_start - boundary, 3)
    except (OSError, subprocess.SubprocessError):
        return 0.0
    finally:
        for leftover in ("offset-sample.mkv", "offset-chunk.ts"):
            try:
                (work / leftover).unlink(missing_ok=True)
            except OSError:
                pass


def measure_timeline(direct, boundaries, limit=0):
    """Record where a copy actually starts for each planned boundary.

    Predicting this has failed repeatedly: a copy asked to start at a keyframe
    begins on the keyframe before it, and the offset varies along the file. Rather
    than model ffmpeg, ask it. One small probe per boundary, reading only until the
    first frame, records the real start. The playlist is then built from these
    measured starts, so it cannot disagree with what generation produces.

    Nothing is written to disk. Returns a list of [seek_target, declared_start,
    duration] triples.
    """
    measured = []
    probes = boundaries if not limit else boundaries[:limit]
    for boundary in probes:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-ss",
                f"{boundary:.3f}",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "csv=p=0",
                "-read_intervals",
                "%+#1",
                direct,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        value = None
        for line in result.stdout.splitlines():
            token = line.strip().split(",")[0]
            if not token:
                continue
            try:
                value = round(float(token), 3)
            except ValueError:
                continue
            break
        if value is None or value < 0:
            continue
        measured.append([round(boundary, 3), value])
    rows = []
    for index, (seek_target, content_start) in enumerate(measured[:-1]):
        next_start = measured[index + 1][1]
        duration = round(next_start - content_start, 3)
        if duration < 1.0:
            continue
        rows.append([seek_target, content_start, duration])
    return rows


def main(argv):
    # The app container has a read-only root, so this also runs from stdin:
    #   docker exec -e OMAKASE_SCAN_WATCH_PATH=... -i omakase-plus python - < ops/scan_keyframes.py
    watch_path = argv[0] if argv else os.environ.get("OMAKASE_SCAN_WATCH_PATH", "")
    if not watch_path:
        print(__doc__)
        return 1
    direct, duration = asyncio.run(resolve(watch_path))
    if not direct:
        print(json.dumps({"error": "no direct source", "watch_path": watch_path}))
        return 2
    print(f"scanning keyframes for {duration:.1f}s of media", flush=True)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            direct,
        ],
        capture_output=True,
        text=True,
        timeout=3600,
    )
    values = []
    for line in result.stdout.splitlines():
        try:
            value = round(float(line.strip().split(",")[0]), 3)
        except ValueError:
            continue
        if value >= 0:
            values.append(value)
    values = sorted(set(values))
    if len(values) < 2:
        print(
            json.dumps(
                {"error": "probe produced no keyframes", "lines": len(result.stdout.splitlines())}
            )
        )
        return 3
    offset = measure_offset(direct, values)
    from omakase.plus.debrid_seek import keyframes_cache_path

    root = Path("/home/omakase/data/seek")
    root.mkdir(parents=True, exist_ok=True)
    target = keyframes_cache_path(root, direct)
    planned = []
    start = 0.0
    while values and start < values[-1]:
        ahead = [value for value in values if value >= start + 6.0]
        if not ahead:
            break
        planned.append(round(start, 3))
        start = ahead[0]
    limit = int(os.environ.get("OMAKASE_SCAN_LIMIT", "0") or 0)
    timeline = measure_timeline(direct, planned, limit=limit)
    target.write_text(
        json.dumps({"keyframes": values, "offset": offset, "timeline": timeline}),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "cached": str(target),
                "keyframes": len(values),
                "offset": offset,
                "timeline_segments": len(timeline),
                "first": values[:4],
                "last": values[-2:],
                "duration": duration,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
