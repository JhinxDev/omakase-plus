"""Materialize one title into the prepared HLS store, between sessions.

The on-demand seek path predicts a segment timeline and then asks ffmpeg for one
chunk at a time. The prediction and the produced chunks disagree, so the player
repeats about a second at every boundary and the subtitle clock slides. The
validated fix is to stop predicting: one ffmpeg pass writes the segments and the
playlist together, and the muxer that cut the chunks also wrote their declared
durations, so the two cannot disagree. See ``omakase.plus.prepared_hls`` for the
read side of the store.

Run it inside the app container, with the store mounted and
``OMAKASE_PREPARED_ROOT`` pointing at it:

    docker exec -i omakase-plus python - < ops/prepare_title.py \
        "/plus/watch/title/tv/247718/1/1"

    docker exec -i omakase-plus python - < ops/prepare_title.py \
        "/plus/watch/4091/1" --force

    docker exec -i omakase-plus python - < ops/prepare_title.py \
        --verify <key>

The preparation is written into ``<root>/<key>.partial`` and renamed into place
only after the playlist and every segment verify, so a run that is still going,
or one that failed, can never be served.

The pass deliberately converts audio to AAC. ``-c:a copy`` with a TrueHD track
produces a playlist with no segments at all, measured on the real media. Text
subtitle tracks are extracted in the same pass, because a second pass would read
the whole provider file again and preparation is provider-read bound.

Exit codes: 0 prepared and verified, 2 could not resolve the title, 3 the ffmpeg
pass failed, 4 the produced timeline failed verification.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

# Text subtitle codecs ffmpeg can convert to WebVTT without a bitmap decoder.
# PGS and DVD bitmap tracks are excluded on purpose: the app already serves its
# own OCR rendition for those, and a bitmap track cannot become text here.
_TEXT_SUBTITLE_CODECS = {
    "subrip",
    "srt",
    "ass",
    "ssa",
    "mov_text",
    "webvtt",
    "text",
    "sami",
    "microdvd",
    "subviewer",
    "subviewer1",
    "realtext",
    "jacosub",
    "mpl2",
    "pjs",
    "stl",
    "vplayer",
}
_SEGMENT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.(?:ts|m4s)")
_INIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.mp4")
_MAP = re.compile(r'#EXT-X-MAP:URI="([^"]{1,64})"')
_EXTINF = re.compile(r"#EXTINF:([0-9]+(?:\.[0-9]+)?),?")


class _Captured(Exception):
    """Raised to stop the play request as soon as the media source is captured."""


def _prepared_key(url):
    """The store key for a media link.

    The app module is authoritative and is used whenever it is importable. A
    preparation run may happen against an image built before that module landed,
    so the same computation is repeated here as a fallback, and a disagreement
    would be visible as a title that never becomes servable.
    """
    try:
        from omakase.plus.prepared_hls import prepared_key

        return prepared_key(url)
    except ImportError:
        if not isinstance(url, str) or not url:
            return ""
        path = urllib.parse.urlsplit(url).path
        return hashlib.sha256(path.encode("utf-8")).hexdigest() if path else ""


def _prepared_root():
    configured = os.getenv("OMAKASE_PREPARED_ROOT", "").strip()
    if configured:
        return Path(configured)
    data = os.getenv("OMAKASE_DATA_DIR", "data").strip() or "data"
    return Path(data) / "prepared"


def _store():
    """The app's read side when the image has it, otherwise nothing."""
    try:
        from omakase.plus.prepared_hls import PreparedStore

        return PreparedStore(_prepared_root())
    except ImportError:
        return None


async def _resolve(watch_path):
    """Resolve a watch path to its provider source without opening a player."""
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
        captured.setdefault("source", source)
        raise _Captured()

    debrid_stream.DebridStreams.prepare = capture
    debrid_stream.DebridStreams._start_seek = lambda self, source: None
    try:
        data = Path(_resolved_data_dir()).resolve()
        connection = sqlite3.connect(
            f"file:{data / 'omakase-plus.db'}?mode=ro", uri=True, timeout=5
        )
        connection.row_factory = sqlite3.Row
        actor = connection.execute(
            "SELECT id AS user_id FROM users ORDER BY id LIMIT 1"
        ).fetchone()["user_id"]
        db = sqlite3.connect(":memory:", check_same_thread=False)
        db.row_factory = sqlite3.Row
        connection.backup(db)
        connection.close()
        store_secret(db, actor, PLAYBACK_PROVIDER_KEY, "realdebrid")
        templates = Jinja2Templates(
            directory=str(Path(native_tv_test.__file__).parent / "templates")
        )
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
            code = re.search(r"#library-([A-F0-9]{20})", issued.text)
            if not code:
                return None, "the library page did not issue a pairing code"
            redeemed = await client.post(
                "/plus/api/native-tv-test/v1/library/redeem", json={"code": code[1]}
            )
            if redeemed.status_code != 200:
                return None, f"redeem returned {redeemed.status_code}"
            client.headers["Authorization"] = "Bearer " + redeemed.json()["token"]
            clock["now"] += 61
            try:
                response = await client.post(
                    "/plus/api/native-tv-test/v1/library/play", json={"watch_path": watch_path}
                )
                return None, f"play returned {response.status_code} before a source was captured"
            except _Captured:
                pass
    finally:
        debrid_stream.DebridStreams.prepare = original
    source = captured.get("source")
    if source is None:
        return None, "no provider source was resolved for that watch path"
    direct = getattr(source, "direct", "")
    if not direct:
        return None, "the resolved source has no direct file link"
    return source, None


def _probe_streams(direct):
    """Read the container's stream list without decoding the file."""
    command = [
        "ffprobe",
        "-hide_banner",
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name:stream_tags=language,title",
        "-of",
        "json",
        direct,
    ]
    done = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if done.returncode != 0:
        return None, done.stderr.strip()[:400]
    try:
        payload = json.loads(done.stdout)
    except ValueError as error:
        return None, str(error)
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return None, "ffprobe returned no stream list"
    return streams, None


def _audio_index(streams):
    """First audio stream, which is the one the validated recipe converts."""
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return stream.get("index")
    return None


def _recipe(streams):
    """Container for the segments: fMP4 for HEVC, MPEG-TS otherwise.

    Apple players do not play HEVC from MPEG-TS segments. They do play it from
    fMP4 (the HLS authoring specification requires it), so the recipe follows the
    codec instead of being fixed. MPEG-TS stays for H.264 because that is the
    form already measured on the device.
    """
    for stream in streams:
        if stream.get("codec_type") == "video" and stream.get("codec_name") == "hevc":
            return "fmp4"
    return "mpegts"


def _text_tracks(streams):
    tracks = []
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").lower()
        if codec not in _TEXT_SUBTITLE_CODECS:
            continue
        tags = stream.get("tags") or {}
        tracks.append(
            {
                "index": stream.get("index"),
                "name": str(tags.get("title") or "").strip()[:40],
                "language": str(tags.get("language") or "").strip().lower()[:8],
                "codec": codec,
            }
        )
    return tracks


def _verify(directory, expected_duration=None):
    """Check the produced store the same way the server will read it."""
    playlist_path = directory / "stream.m3u8"
    errors = []
    try:
        data = playlist_path.read_bytes()
    except OSError as error:
        return {"ok": False, "errors": [f"playlist unreadable: {error}"]}
    try:
        lines = [line.strip() for line in data.decode("ascii").splitlines()]
    except UnicodeDecodeError:
        return {"ok": False, "errors": ["playlist is not ascii"]}
    if not lines or lines[0] != "#EXTM3U":
        errors.append("playlist does not start with #EXTM3U")
    if "#EXT-X-ENDLIST" not in lines:
        errors.append("playlist has no #EXT-X-ENDLIST, so the pass did not finish")
    names, durations = [], []
    init = None
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith("#"):
            if match := _EXTINF.fullmatch(line):
                durations.append(float(match[1]))
            elif match := _MAP.fullmatch(line):
                init = match[1]
            continue
        names.append(line)
    if not names:
        errors.append("playlist names no segments")
    if len(names) != len(durations):
        errors.append(f"{len(names)} segments but {len(durations)} EXTINF lines")
    for name in names:
        if not _SEGMENT_NAME.fullmatch(name):
            errors.append(f"unexpected segment name {name!r}")
            continue
        path = directory / name
        try:
            size = path.stat().st_size
        except OSError:
            errors.append(f"segment {name} is missing")
            continue
        if size <= 0:
            errors.append(f"segment {name} is empty")
    if init is not None:
        # fMP4 needs its initialization segment before the first media segment.
        if not _INIT_NAME.fullmatch(init):
            errors.append(f"unexpected initialization segment name {init!r}")
        else:
            try:
                if (directory / init).stat().st_size <= 0:
                    errors.append(f"initialization segment {init} is empty")
            except OSError:
                errors.append(f"initialization segment {init} is missing")
    if durations:
        if min(durations) <= 0.5:
            errors.append(f"shortest declared duration {min(durations):.3f} s is not usable")
        if max(durations) >= 30:
            errors.append(f"longest declared duration {max(durations):.3f} s is not usable")
    total = sum(durations)
    if expected_duration and total > 0:
        if abs(total - expected_duration) > max(20.0, expected_duration * 0.03):
            errors.append(
                f"declared total {total:.1f} s disagrees with the source duration "
                f"{expected_duration:.1f} s"
            )
    return {
        "ok": not errors,
        "errors": errors,
        "segments": len(names),
        "declared_total_s": round(total, 3),
        "min_s": round(min(durations), 3) if durations else None,
        "max_s": round(max(durations), 3) if durations else None,
    }


def _materialize(direct, key, partial, duration, tracks, log_path, limit=None, recipe="mpegts"):
    """One ffmpeg pass: the segments, the playlist and the text sidecars."""
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-loglevel",
        "info",
    ]
    if limit:
        # An input limit keeps a smoke run cheap: ffmpeg stops reading the
        # remote file instead of writing a whole title.
        command += ["-t", str(limit)]
    command += [
        "-i",
        direct,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
        "-sn",
    ]
    if recipe == "fmp4":
        # Apple requires the hvc1 tag for HEVC; the default hev1 is refused.
        command += ["-tag:v", "hvc1"]
    command += [
        "-f",
        "hls",
        "-hls_time",
        "6",
        "-hls_playlist_type",
        "vod",
        "-hls_flags",
        "independent_segments",
    ]
    if recipe == "fmp4":
        command += [
            "-hls_segment_type",
            "fmp4",
            "-hls_fmp4_init_filename",
            "init.mp4",
            "-hls_segment_filename",
            str(partial / "%05d.m4s"),
        ]
    else:
        command += ["-hls_segment_filename", str(partial / "%05d.ts")]
    command += [str(partial / "stream.m3u8")]
    sidecars = []
    for number, track in enumerate(tracks):
        name = f"subtitles-{number}.vtt"
        sidecars.append({"file": name, **track})
        command += [
            "-map",
            f"0:{track['index']}",
            "-c:s",
            "webvtt",
            str(partial / name),
        ]
    started = time.monotonic()
    with log_path.open("wb") as log:
        process = subprocess.run(command, stdout=log, stderr=log, timeout=10800)
    return {
        "exit": process.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "sidecars": sidecars,
        "command": [
            "ffmpeg",
            "-i",
            "<direct link>",
            *command[command.index("-map") :],
        ],
    }


def _write_meta(directory, payload):
    path = directory / "meta.json"
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")


async def main(argv):
    if not argv:
        print("usage: prepare_title.py <watch-path> [--force] | --verify <key>")
        return 2
    root = _prepared_root()
    store = _store()
    if argv[0] == "--verify":
        key = argv[1] if len(argv) > 1 else ""
        opened = store.open(key) if store is not None else None
        if opened is None:
            print(json.dumps({"key": key, "ok": False, "error": "not a complete prepared title"}))
            return 4
        result = _verify(root / key)
        print(json.dumps({"key": key, **result}, indent=1))
        return 0 if result["ok"] else 4

    watch_path = argv[0]
    force = "--force" in argv[1:]
    smoke = next((item for item in argv[1:] if item.startswith("--smoke")), "")
    probe_only = "--probe" in argv[1:]
    smoke_limit = 30
    if smoke and "=" in smoke:
        smoke_limit = max(10, min(180, int(smoke.split("=", 1)[1])))
    if smoke:
        # A smoke run writes under the data volume so it never touches the real
        # store, and it is deleted after verifying.
        root = Path(os.getenv("OMAKASE_DATA_DIR", "data")).resolve() / "prepared-smoke"
    source, error = await _resolve(watch_path)
    if source is None:
        print(json.dumps({"watch_path": watch_path, "ok": False, "error": error}))
        return 2
    direct = source.direct
    key = _prepared_key(direct)
    duration = float(getattr(source, "duration", 0) or 0)
    target = root / key
    report = {
        "watch_path": watch_path,
        "key": key,
        "root": str(root),
        "duration_s": duration,
        "video_codec": getattr(source, "video_codec", ""),
        "audio_codec": getattr(source, "audio_codec", ""),
        "container": getattr(source, "container", ""),
    }
    if not force and store is not None and store.open(key) is not None:
        result = _verify(target, duration)
        report.update({"ok": True, "skipped": "already prepared and verified", **result})
        print(json.dumps(report, indent=1))
        return 0
    root.mkdir(parents=True, exist_ok=True)
    streams, probe_error = _probe_streams(direct)
    if streams is None:
        report.update({"ok": False, "error": f"ffprobe failed: {probe_error}"})
        print(json.dumps(report, indent=1))
        return 2
    if probe_only:
        # Report what the container would cut before spending provider read on a
        # whole title. HEVC has to go into fMP4 segments for Apple hardware;
        # MPEG-TS carries it but AVPlayer will not play it from there.
        report.update(
            {
                "ok": True,
                "probe_only": True,
                "ffmpeg_recipe": _recipe(streams),
                "video": [
                    {"codec": stream.get("codec_name"), "index": stream.get("index")}
                    for stream in streams
                    if stream.get("codec_type") == "video"
                ],
                "audio": [
                    {"codec": stream.get("codec_name"), "index": stream.get("index")}
                    for stream in streams
                    if stream.get("codec_type") == "audio"
                ],
                "subtitles": [
                    {"codec": stream.get("codec_name"), "index": stream.get("index")}
                    for stream in streams
                    if stream.get("codec_type") == "subtitle"
                ],
            }
        )
        print(json.dumps(report, indent=1))
        return 0
    audio_index = _audio_index(streams)
    tracks = _text_tracks(streams)
    recipe = _recipe(streams)
    report.update(
        {
            "audio_index": audio_index,
            "text_tracks": len(tracks),
            "recipe": recipe,
            "subtitle_tracks": [
                {
                    "codec": stream.get("codec_name"),
                    "language": (stream.get("tags") or {}).get("language", ""),
                }
                for stream in streams
                if stream.get("codec_type") == "subtitle"
            ],
        }
    )
    if duration and duration > 3600 * 4:
        report.update({"ok": False, "error": "duration is implausible, refusing to prepare"})
        print(json.dumps(report, indent=1))
        return 3
    # Written into a partial directory and renamed only after verification, so
    # an unfinished run can never be served.
    partial = root / f"{key}.partial"
    if partial.exists():
        shutil.rmtree(partial, ignore_errors=True)
    partial.mkdir(parents=True)
    run = _materialize(
        direct,
        key,
        partial,
        duration,
        tracks,
        partial / "ffmpeg.log",
        limit=smoke_limit if smoke else None,
        recipe=recipe,
    )
    report["ffmpeg"] = {name: run[name] for name in ("exit", "seconds", "sidecars")}
    if run["exit"] != 0:
        report.update(
            {
                "ok": False,
                "error": f"ffmpeg exited {run['exit']}; log kept at {partial / 'ffmpeg.log'}",
            }
        )
        print(json.dumps(report, indent=1))
        return 3
    # Only sidecars that are non-empty become renditions; an empty extraction
    # would show the player an empty subtitle track.
    sidecars = []
    for track in run["sidecars"]:
        path = partial / track["file"]
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > 0:
            sidecars.append({**track, "bytes": size})
        else:
            path.unlink(missing_ok=True)
    if sidecars:
        (partial / "tracks.json").write_text(
            json.dumps({"tracks": sidecars}, indent=1) + "\n", encoding="utf-8"
        )
    verified = _verify(partial, duration if not smoke else float(smoke_limit))
    report.update(verified)
    if not verified["ok"]:
        report["partial"] = str(partial)
        print(json.dumps(report, indent=1))
        return 4
    if smoke:
        # A smoke run proves the recipe and the extractor on the real media and
        # then removes its own output, so the store never sees a partial title.
        shutil.rmtree(partial, ignore_errors=True)
        report.update({"ok": True, "smoke_seconds": smoke_limit})
        print(json.dumps(report, indent=1))
        return 0
    (partial / "ffmpeg.log").unlink(missing_ok=True)
    completed = root / key
    if completed.exists():
        shutil.rmtree(completed)
    partial.rename(completed)
    _write_meta(
        completed,
        {
            "watch_path": watch_path,
            "key": key,
            "duration_s": duration,
            "video_codec": report["video_codec"],
            "audio_codec": report["audio_codec"],
            "container": report["container"],
            "segments": verified["segments"],
            "declared_total_s": verified["declared_total_s"],
            "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ffmpeg_seconds": run["seconds"],
            "recipe": recipe,
            "sidecars": [
                {"name": item["name"], "language": item["language"], "file": item["file"]}
                for item in sidecars
            ],
        },
    )
    report["ok"] = True
    report["directory"] = str(completed)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
