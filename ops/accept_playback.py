"""Acceptance smoke test for Apple TV playback, run against a live container.

This is the check that replaces ad-hoc probing after a deploy. It exercises the
same endpoints the Apple TV app calls, in the same order, and answers one
question: would playback work right now?

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

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path


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
        stored = sorted(Path(data / "seek").glob("keyframes-*.json"))
        offset = None
        if stored:
            try:
                payload = json.loads(stored[0].read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    offset = payload.get("offset")
            except (OSError, ValueError):
                offset = None
        checks.append(_result("subtitle calibration offset", True, {"offset": offset}))

        hashes = []
        sizes = []
        for number in (1, 2, 20):
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
    watch_path = os.environ.get("OMAKASE_ACCEPT_WATCH", "")
    if not watch_path:
        print(json.dumps({"error": "set OMAKASE_ACCEPT_WATCH to a watch path"}))
        return 2
    began = time.monotonic()
    try:
        checks = asyncio.run(_run(watch_path))
    except Exception as error:  # noqa: BLE001 - the report is the output
        print(json.dumps({"error": f"{type(error).__name__}: {error}"}))
        return 2
    failed = [item for item in checks if not item["ok"]]
    report = {
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
