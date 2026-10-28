"""Ephemeral ASGI browser fixture over stdin. No listener or real credentials."""

from __future__ import annotations

import base64
import json
import sqlite3
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus import google_auth as ga
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.web.server import plus_security_headers


def main():
    database = sqlite3.connect(":memory:", check_same_thread=False)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys=ON")
    run_migrations(database)
    database.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'synthetic@example.invalid','synthetic-hash')"
    )
    database.commit()
    session = create_session(database, 1)
    config = ga.GoogleConfig(
        "synthetic-client",
        "synthetic-secret",
        "https://omakase.example.invalid/plus/auth/google/callback",
    )
    ga.google_config = lambda: config
    ga._starts.clear()
    templates = Jinja2Templates(directory=str(Path(ga.__file__).parent / "templates"))
    templates.env.globals["asset_version"] = "synthetic-csp-redirect"
    app = FastAPI()
    app.middleware("http")(plus_security_headers)
    app.include_router(
        ga.create_google_router(templates, lambda response, value: None), prefix="/plus"
    )
    app.mount("/plus/static", StaticFiles(directory=str(Path(ga.__file__).parent / "static")))
    app.dependency_overrides[get_db] = lambda: database
    with TestClient(
        app, base_url="https://omakase.example.invalid", follow_redirects=False
    ) as client:
        print(json.dumps({"kind": "ready", "session": session}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("stop"):
                break
            response = client.request(
                request["method"],
                request["path"],
                headers=request.get("headers", {}),
                content=base64.b64decode(request.get("body", "")),
            )
            print(
                json.dumps(
                    {
                        "id": request["id"],
                        "status": response.status_code,
                        "headers": dict(response.headers),
                        "body": base64.b64encode(response.content).decode(),
                    }
                ),
                flush=True,
            )
    database.close()


if __name__ == "__main__":
    main()
