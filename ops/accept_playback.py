"""Apple TV playback smoke and offline acceptance receipt verifier.

Run the legacy live smoke only in an authorized environment. Use independent
reference and candidate receipts for the strict gate:

    python ops/accept_playback.py --verify reference.json observed.json

The strict gate requires measured timing, media continuity, cue mapping, seek
landing and database completion readback for the same target and generation.
For the live smoke, timings and calibration are informational checks.

Run inside the container, which has the application and its data:

    docker exec -e OMAKASE_ACCEPT_WATCH="/plus/watch/title/tv/247718/1/1" \
        -i omakase-plus python - < ops/accept_playback.py

Exit codes: 0 every check passed, 1 at least one check failed, 2 setup failed.

Design notes that matter:

- The index is requested before the stream playlist. The seek engine only takes
  over after the player asks for the index; skipping it measures the provider
  path instead and reports nonsense.
- Segment distinctness is checked by hash, because the failure mode being caught
  is "every request returns the same stored segment", which still returns 200.
- Timings are reported but never asserted, because they depend on the provider
  link rather than on our code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_./:-]{0,159}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_ASSET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\.m3u8")
_DEADLINES = {"open": 5.0, "playlist": 8.0, "seek": 10.0}
_TIMELINE_TOLERANCE = 0.15
_MAX_RECEIPT_SIZE = 8 * 1024 * 1024


def _number(value):
    return type(value) in {int, float} and -math.inf < value < math.inf


def _close(actual, expected, tolerance):
    return _number(actual) and _number(expected) and abs(actual - expected) <= tolerance


def _dict(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) and all(isinstance(row, dict) for row in value) else []


def _identifier(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _same_identity(reference, record):
    return all(record.get(key) == reference.get(key) for key in ("target", "generation"))


def _timeline(expected, actual):
    expected, actual = _rows(expected), _rows(actual)
    if not 2 <= len(expected) == len(actual) <= 10000:
        return False
    previous = None
    for index, (reference, segment) in enumerate(zip(expected, actual, strict=True)):
        start, end = segment.get("start"), segment.get("end")
        if not (
            type(segment.get("index")) is int
            and segment["index"] == reference.get("index") == index
            and _close(start, reference.get("start"), _TIMELINE_TOLERANCE)
            and _close(end, reference.get("end"), _TIMELINE_TOLERANCE)
            and start < end
            and all(
                _identifier(segment.get(key), _DIGEST) and segment[key] == reference.get(key)
                for key in ("first_frame", "last_frame", "content")
            )
        ):
            return False
        if previous and not _close(start, previous["end"], _TIMELINE_TOLERANCE):
            return False
        previous = segment
    return True


def _cues(expected, actual):
    expected, actual = _rows(expected), _rows(actual)
    if not 1 <= len(expected) == len(actual) <= 10000:
        return False
    return all(
        _identifier(reference.get("id"), _ID)
        and cue.get("id") == reference["id"]
        and _close(cue.get("start"), reference.get("start"), _TIMELINE_TOLERANCE)
        and _close(cue.get("end"), reference.get("end"), _TIMELINE_TOLERANCE)
        and 0 <= cue["start"] < cue["end"]
        for reference, cue in zip(expected, actual, strict=True)
    )


def _seeks(expected, actual):
    actual = _rows(actual)
    return (
        isinstance(expected, list)
        and 3 <= len(expected) == len(actual) <= 1000
        and all(
            _number(target)
            and target >= 0
            and _close(seek.get("target"), target, 0.001)
            and _close(seek.get("landed"), target, 0.5)
            for target, seek in zip(expected, actual, strict=True)
        )
        and len(set(expected)) == len(expected)
    )


def _completion(reference, actual):
    position = reference.get("completion_position")
    credits = tuple(
        actual.get(key) for key in ("credits_before", "credits_after", "credits_after_retry")
    )
    return (
        _same_identity(reference, actual)
        and _number(position)
        and position >= 0
        and _close(actual.get("position"), position, 0.5)
        and actual["position"] >= 0
        and _close(actual.get("readback_position"), position, 0.5)
        and actual["readback_position"] >= 0
        and all(type(count) is int for count in credits)
        and credits[0] >= 0
        and credits[1] == credits[0] + 1 == credits[2]
    )


def verify_acceptance(reference, observed):
    """Validate independently measured evidence without contacting a provider."""
    reference, observed = _dict(reference), _dict(observed)
    calibration = _dict(observed.get("calibration"))
    timings = _dict(observed.get("timings"))
    checks = {
        "identity": (
            _identifier(reference.get("target"), _ID)
            and _identifier(reference.get("generation"), _DIGEST)
            and _same_identity(reference, observed)
        ),
        "master": all(
            _identifier(reference.get(key), _ASSET) and observed.get(key) == reference[key]
            for key in ("master_variant", "subtitle_variant")
        ),
        "calibration": (
            _same_identity(reference, calibration)
            and calibration.get("status") == "hit"
            and _close(calibration.get("offset"), reference.get("calibration_offset"), 0.05)
        ),
        "deadlines": all(
            _number(elapsed := timings.get(stage)) and 0 <= elapsed <= deadline
            for stage, deadline in _DEADLINES.items()
        ),
        "timeline": _timeline(reference.get("segments"), observed.get("segments")),
        "cues": _cues(reference.get("cues"), observed.get("cues")),
        "seek": _seeks(reference.get("seek_targets"), observed.get("seeks")),
        "completion": _completion(reference, _dict(observed.get("completion"))),
    }
    failed = [name for name, ok in checks.items() if not ok]
    return {
        "kind": "playback_acceptance",
        "result": "FAIL" if failed else "PASS",
        "failed_checks": failed,
    }


def _read_receipt(path):
    def reject_duplicate_fields(pairs):
        receipt = dict(pairs)
        if len(receipt) != len(pairs):
            raise ValueError("Duplicate receipt key")
        return receipt

    with path.open("rb") as source:
        raw = source.read(_MAX_RECEIPT_SIZE + 1)
    if len(raw) > _MAX_RECEIPT_SIZE:
        raise ValueError("Oversized receipt")
    return json.loads(raw, object_pairs_hook=reject_duplicate_fields)


def _result(name, ok, detail):
    return {"check": name, "ok": bool(ok), "detail": detail}


async def _run(watch_path):
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

    library = "/plus/api/native-tv-test/v1/library"
    clock = {"now": 1000.0}
    checks = []
    data = Path(_resolved_data_dir()).resolve()
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
    async with httpx.AsyncClient(transport=transport, base_url=origin, timeout=500) as client:
        client.cookies.set("omakase_session", cookie)
        issued = await client.post(
            "/plus/native-tv-library",
            follow_redirects=True,
            data={"csrf_token": csrf_token(request), "action": "issue"},
        )
        code = re.search(r"#library-([A-F0-9]{20})", issued.text)
        checks.append(_result("library issue", bool(code), issued.status_code))
        if not code:
            return checks
        redeemed = await client.post(library + "/redeem", json={"code": code[1]})
        checks.append(_result("library redeem", redeemed.status_code == 200, redeemed.status_code))
        client.headers["Authorization"] = "Bearer " + redeemed.json()["token"]
        clock["now"] += 61
        started = time.monotonic()
        play = await client.post(library + "/play", json={"watch_path": watch_path})
        play_seconds = round(time.monotonic() - started, 2)
        checks.append(_result("play opens the media", play.status_code == 200, play.status_code))
        if play.status_code != 200:
            return checks
        media = play.json()
        client.cookies.set(COOKIE, media["media_token"])
        base = "/plus/api/native-tv-test/v1/media/" + media["media_id"]

        index = await client.get(base + "/index.m3u8")
        is_master = "#EXT-X-STREAM-INF" in index.text
        languages = re.findall(r'LANGUAGE="([^"]+)"', index.text)
        names = re.findall(r'NAME="([^"]+)"', index.text)
        checks.append(_result("index is a master playlist", is_master, index.status_code))
        checks.append(
            _result(
                "subtitle renditions offered",
                len(languages) >= 1,
                {"count": len(languages), "languages": languages, "names": names},
            )
        )
        checks.append(_result("play open time", True, {"seconds": play_seconds}))

        started = time.monotonic()
        playlist = await client.get(base + "/stream.m3u8")
        playlist_seconds = round(time.monotonic() - started, 2)
        segments = [
            line.strip()
            for line in playlist.text.splitlines()
            if line.strip() and not line.startswith("#")
        ]
        durations = [float(value) for value in re.findall(r"#EXTINF:([0-9.]+)", playlist.text)]
        checks.append(
            _result(
                "seek playlist has segments",
                len(segments) > 50,
                {"count": len(segments), "seconds": playlist_seconds},
            )
        )
        checks.append(
            _result(
                "declared durations are sane",
                bool(durations) and min(durations) > 0.5 and max(durations) < 30,
                {
                    "min": min(durations) if durations else None,
                    "max": max(durations) if durations else None,
                },
            )
        )
        checks.append(_result("subtitle calibration (see strict gate)", True, {"measured": False}))

        hashes = []
        sizes = []
        for number in (1, 2, 20):
            if number >= len(segments):
                checks.append(_result(f"segment index {number}", False, {"missing": True}))
                continue
            started = time.monotonic()
            reply = await client.get(base + "/" + segments[number])
            seconds = round(time.monotonic() - started, 2)
            hashes.append(hashlib.md5(reply.content).hexdigest())
            sizes.append(len(reply.content))
            checks.append(
                _result(
                    f"segment {segments[number]}",
                    reply.status_code == 200 and len(reply.content) > 100_000,
                    {"status": reply.status_code, "bytes": len(reply.content), "seconds": seconds},
                )
            )
        checks.append(
            _result(
                "segments are not the same bytes",
                len(set(hashes)) == len(hashes),
                {"distinct": len(set(hashes))},
            )
        )

        started = time.monotonic()
        subtitles = await client.get(base + "/subtitles0.vtt")
        cues = subtitles.text.count("-->")
        checks.append(
            _result(
                "subtitles serve cues",
                subtitles.status_code == 200 and cues > 50,
                {
                    "status": subtitles.status_code,
                    "cues": cues,
                    "bytes": len(subtitles.content),
                    "seconds": round(time.monotonic() - started, 2),
                },
            )
        )
        await client.post(library + "/media/" + media["media_id"] + "/close")
    return checks


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", nargs=2, type=Path, metavar=("REFERENCE", "OBSERVED"))
    args = parser.parse_args(argv)
    if args.verify:
        try:
            result = verify_acceptance(*(_read_receipt(path) for path in args.verify))
        except (OSError, ValueError, RecursionError):
            print(json.dumps({"kind": "playback_acceptance", "error": "invalid_receipt"}))
            return 2
        print(json.dumps(result, indent=1))
        return 0 if result["result"] == "PASS" else 1
    watch_path = os.environ.get("OMAKASE_ACCEPT_WATCH", "")
    if not watch_path:
        print(json.dumps({"error": "set OMAKASE_ACCEPT_WATCH to a watch path"}))
        return 2
    began = time.monotonic()
    try:
        checks = asyncio.run(_run(watch_path))
    except Exception as error:  # noqa: BLE001 - the report is the output
        print(json.dumps({"kind": "playback_smoke", "error": type(error).__name__}))
        return 2
    failed = [item for item in checks if not item["ok"]]
    report = {
        "kind": "playback_smoke",
        "watch_path": watch_path,
        "checks": checks,
        "failed": len(failed),
        "passed": len(checks) - len(failed),
        "wall_s": round(time.monotonic() - began, 1),
        "result": "PASS" if checks and not failed else "FAIL",
    }
    print(json.dumps(report, indent=1))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
