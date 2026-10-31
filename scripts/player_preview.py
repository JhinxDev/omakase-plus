"""Loopback-only synthetic playback review; never connects to saved services.

Run with the locked interpreter and --media pointing to a synthetic H264/AAC
MP4. The database and demo sign-in exist only in the task-owned temporary folder.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from omakase.plus import households
from omakase.plus.auth import create_session
from omakase.plus.db import _connect
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.web_playback_delivery import parse_range
from omakase.plus.web_playback_routes import create_player_router
from omakase.plus.web_security import csrf_token


def preview(media: Path, data_dir: str):
    media = media.resolve(strict=True)
    if media.suffix.lower() != ".mp4" or not 1024 < media.stat().st_size <= 16 * 1024 * 1024:
        raise ValueError("Supply a small synthetic MP4 fixture.")
    version = hashlib.sha256(media.read_bytes()).hexdigest()
    size = media.stat().st_size
    os.environ["OMAKASE_PLUS_PRIVATE"] = "true"
    os.environ["OMAKASE_PLUS_WEB_PLAYER_ENABLED"] = "true"
    os.environ["OMAKASE_DATA_DIR"] = data_dir
    db = _connect(data_dir)
    db.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'review@example.invalid','no-password-login')"
    )
    library_id = households.ensure_personal_library(db, user_id=1)
    db.execute(
        "INSERT INTO media_monitors(id,user_id,anilist_id,title,season_number) VALUES(1,1,100,'Synthetic playback study',1)"
    )
    db.commit()
    login = create_session(db, 1)
    db.close()

    async def resolver(db, **values):
        assert values["actor_user_id"] == 1 and values["anilist_id"] == 100
        scope = households.resolve_scope(db, viewer_id=1, library_id=values["library_id"])
        return SimpleNamespace(
            actor_user_id=1,
            library_id=library_id,
            anilist_id=100,
            season_number=1,
            episode_number=values["episode_number"],
            server_id="synthetic-playback-fixture",
            account_id="1234567",
            metadata_key="1",
            part_key="/library/parts/1/1/fixture.mp4",
            part_size=size,
            duration_ms=30000,
            container="mp4",
            video_codec="h264",
            audio_codec="aac",
            streams=(),
            media_version=version,
            source_identity=version,
            base_url="https://fixture.invalid",
            token="dummy-web",
            _scope=scope,
            _snapshot=(),
        )

    def guard(db, source):
        households.revalidate_scope(db, source._scope)
        if media.stat().st_size != size:
            raise ValueError("Fixture changed")

    class FileBody(httpx.AsyncByteStream):
        def __init__(self, left, length):
            self.left, self.length = left, length

        async def __aiter__(self):
            with media.open("rb") as stream:
                stream.seek(self.left)
                remaining = self.length
                while remaining:
                    data = stream.read(min(65536, remaining))
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

    def transport(request):
        assert request.url.host == "fixture.invalid"
        assert request.url.path == "/library/parts/1/1/fixture.mp4"
        selected = request.headers.get("range")
        left, right = parse_range(selected, size) if selected else (0, size - 1)
        headers = {"content-length": str(right - left + 1), "content-type": "video/mp4"}
        if selected:
            headers["content-range"] = f"bytes {left}-{right}/{size}"
        return httpx.Response(
            206 if selected else 200, headers=headers, stream=FileBody(left, right - left + 1)
        )

    root = Path(__file__).resolve().parents[1] / "src/omakase/plus"
    templates = Jinja2Templates(directory=str(root / "templates"))
    templates.env.globals["asset_version"] = "player-review"
    app = FastAPI()

    class IsolatedAssets(StaticFiles):
        async def get_response(self, path, scope):
            response = await super().get_response(path, scope)
            if path.replace("\\", "/") == "ass/worker.js":
                response.headers["Cross-Origin-Embedder-Policy"] = "require-corp"
            return response

    app.mount("/plus/static", IsolatedAssets(directory=root / "static"), name="static")
    app.include_router(
        create_player_router(
            templates,
            lambda request, user, **kw: {
                "email": user.email,
                "active_page": "library",
                "csrf_token": csrf_token(request),
            },
            resolver=resolver,
            source_guard=guard,
            remote_guard=lambda *a, **kw: None,
            transport=httpx.MockTransport(transport),
        ),
        prefix="/plus",
    )

    def connection():
        conn = _connect(data_dir)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = connection

    @app.get("/", response_class=HTMLResponse)
    async def entry():
        return '<!doctype html><html lang="en"><meta name="viewport" content="width=device-width"><title>Omakase player review</title><body style="background:#080d1a;color:#edf0f8;font:20px system-ui;padding:3rem;max-width:40rem"><h1>Omakase player review</h1><p>This loopback preview uses one synthetic 30-second silent H264/AAC video and temporary viewing records. It cannot contact saved Plex or media-provider accounts.</p><p><a style="color:#c0b1ff" href="/review/sign-in">Enter the synthetic review account</a></p></body></html>'

    @app.get("/plus/api/notifications")
    async def empty_notifications():
        return {"items": [], "unread_count": 0}

    @app.get("/review/evidence")
    async def evidence(user=Depends(require_user), conn=Depends(get_db)):
        return {
            "synthetic": True,
            "completed": conn.execute(
                "SELECT COUNT(*) FROM web_playback_completions WHERE user_id=?", (user.id,)
            ).fetchone()[0],
            "watch_events": conn.execute(
                "SELECT COUNT(*) FROM watch_events WHERE user_id=? AND source='web_player'",
                (user.id,),
            ).fetchone()[0],
            "coverage": [
                dict(r)
                for r in conn.execute(
                    "SELECT covered_seconds,duration_seconds,last_played_at FROM web_playback_watch WHERE user_id=?",
                    (user.id,),
                )
            ],
        }

    @app.get("/review/sign-in")
    async def sign_in(request: Request):
        response = RedirectResponse("/plus/watch/1/1", status_code=303)
        response.set_cookie("omakase_session", login, httponly=True, samesite="strict")
        return response

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media", required=True, type=Path)
    parser.add_argument("--port", type=int, default=8126)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="omakase-player-review-") as data_dir:
        uvicorn.run(
            preview(args.media, data_dir),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
            log_level="warning",
        )
