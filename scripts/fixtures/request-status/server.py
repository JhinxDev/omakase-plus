"""Labelled synthetic fixture server for the web Requests dashboard proof.

Not production code and not part of the shipped app. It seeds synthetic request
rows shaped from the recorded live evidence (Chouchou 2003, IMDb tt0328955: Riven
received it, rejected two candidates, then reported an acquisition failure whose
exact provider reason was never supplied) plus a resolved request, a confirmed
removal, a live movie request and a manual TV request. A read-only movie service
stand-in replaces the provider boundary so no acquisition provider is contacted.

Run: .venv/Scripts/python.exe scripts/fixtures/request-status/server.py --port 8792
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from pathlib import Path

CHOUCHOU = "tt0328955"
CHOUCHOU_TMDB = 56780
RESOLVED = "tt0084787"
RESOLVED_TMDB = 1091
REMOVED = "tt0450385"
REMOVED_TMDB = 3021
LIVE = "tt0111161"
LIVE_TMDB = 278

T0 = "2026-11-10T18:00:00Z"
T5 = "2026-11-10T18:12:00Z"
FAILED_DETAIL = "Riven reported that this request failed. No automatic resubmission was sent."


class ReadOnlyMovieService:
    """Synthetic stand-in: returns provider/library reads and never submits."""

    def __init__(self, inventory):
        self.inventory = inventory

    async def indexed_requests(self, user_id, imdb_ids):
        return {key: self.inventory[key] for key in imdb_ids if key in self.inventory}

    async def refresh(self, user_id, imdb_id):
        return {"imdb_id": imdb_id, "code": "searching", "request_check": {"performed": True}}

    async def verify(self, user_id, imdb_id):
        return {"imdb_id": imdb_id, "state": "ready"}


def _seed(db, user_id, request_status):
    from omakase.plus import movies_store

    def movie(imdb_id, tmdb_id, title, year, code, state, detail, *, updated_at=T5):
        movies_store.upsert_movie(
            db,
            user_id,
            {"imdb_id": imdb_id, "tmdb_id": tmdb_id, "title": title, "year": year},
            now=T0,
        )
        movies_store.ensure_request(db, user_id, imdb_id, now=T0, edition=None)
        movies_store.update_request(
            db, user_id, imdb_id, now=updated_at, code=code, state=state, detail=detail
        )

    # Synthetic fixture shaped from the recorded Chouchou evidence.
    movie(
        CHOUCHOU,
        CHOUCHOU_TMDB,
        "Chouchou",
        2003,
        "backend_failed",
        "unavailable",
        FAILED_DETAIL,
    )
    for code, at in [
        ("requested", "2026-11-10T18:00:05Z"),
        ("searching", "2026-11-10T18:02:00Z"),
        ("search_complete", "2026-11-10T18:05:00Z"),
        ("candidate_rejected", "2026-11-10T18:06:00Z"),
        ("candidate_rejected", "2026-11-10T18:08:00Z"),
        ("backend_failed", T5),
    ]:
        request_status.record_observation(
            user_id, "movie", CHOUCHOU, lifecycle=T0, code=code, occurred_at=at
        )
    # A stale pending request that the verified library match resolves.
    movie(
        RESOLVED,
        RESOLVED_TMDB,
        "The Thing",
        1982,
        "requested",
        "requested",
        "Requested; delivery has not been verified.",
        updated_at=T0,
    )
    # A confirmed removal belongs in history.
    movie(
        REMOVED,
        REMOVED_TMDB,
        "1408",
        2007,
        "copy_removed",
        "unavailable",
        movies_store.COPY_REMOVED_DETAIL,
        updated_at="2026-11-09T12:00:00Z",
    )
    # A live request that is still active.
    movie(
        LIVE,
        LIVE_TMDB,
        "The Shawshank Redemption",
        1994,
        "searching",
        "searching",
        "Riven last reported a search in progress; delivery is unverified.",
        updated_at="2026-11-11T09:30:00Z",
    )
    db.execute(
        "INSERT INTO for_you_requests (user_id,kind,title_id,title) VALUES (?,'tv',?,?)",
        (user_id, 123, "A manual show"),
    )
    db.commit()


def main():
    parser = argparse.ArgumentParser(description="Requests dashboard fixture server")
    parser.add_argument("--port", type=int, default=8792)
    args = parser.parse_args()
    data_dir = Path(tempfile.mkdtemp(prefix="omakase-requests-fixture-"))
    os.environ["OMAKASE_DATA_DIR"] = str(data_dir)
    os.environ["OMAKASE_PLUS_PRIVATE"] = "true"
    os.environ.pop("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", None)

    from fastapi import FastAPI
    from fastapi.responses import JSONResponse, RedirectResponse

    from omakase.plus import households, request_status
    from omakase.plus.auth import create_session
    from omakase.plus.db import run_migrations
    from omakase.plus.deps import get_db
    from omakase.plus.routes import router

    db_path = data_dir / "omakase-plus.db"
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    db.execute("INSERT INTO users(email,password_hash) VALUES('fixture@example.test','fixture')")
    db.commit()
    user_id = db.execute("SELECT id FROM users WHERE email='fixture@example.test'").fetchone()[0]
    households.ensure_personal_library(db, user_id=user_id)
    _seed(db, user_id, request_status)
    inventory = {RESOLVED: ("f" * 64, "2026-11-11T08:00:00Z")}
    service = ReadOnlyMovieService(inventory)
    import omakase.plus.for_you as for_you

    for_you.MovieService = lambda _db, _connections: service
    session = create_session(db, user_id)

    app = FastAPI(title="Omakase Requests fixture")

    def fixture_db():
        connection = sqlite3.connect(db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    app.dependency_overrides[get_db] = fixture_db
    app.include_router(router)

    @app.get("/fixture/enter")
    async def fixture_enter():
        response = RedirectResponse("/plus/requests", status_code=303)
        response.set_cookie("omakase_session", session, path="/plus", httponly=True)
        return response

    @app.get("/fixture/state")
    async def fixture_state():
        connection = fixture_db()
        try:
            rows = {
                row["imdb_id"]: row["code"]
                for row in connection.execute("SELECT imdb_id,code FROM movie_requests")
            }
            return JSONResponse(
                {
                    "fixture": True,
                    "movie_requests": len(rows),
                    "movie_submissions": connection.execute(
                        "SELECT count(*) FROM movie_submissions"
                    ).fetchone()[0],
                    "for_you_requests": connection.execute(
                        "SELECT count(*) FROM for_you_requests"
                    ).fetchone()[0],
                    "codes": rows,
                }
            )
        finally:
            connection.close()

    import uvicorn

    print(f"fixture ready on http://127.0.0.1:{args.port} data_dir={data_dir}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
