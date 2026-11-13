"""Loopback-only native-form fixture. No real services, accounts or media writes."""

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.responses import FileResponse, RedirectResponse
from jinja2 import FileSystemLoader

from tests.plus.test_movies_routes import browser

ROOT = Path(__file__).resolve().parents[2]
IMDB = "tt15239678"
MODES = {"stale", "updated", "unchanged", "saved", "failed", "ready", "unknown", "no_result"}


def create_app():
    directory = tempfile.TemporaryDirectory(prefix="omakase-movie-feedback-")
    patcher = pytest.MonkeyPatch()
    fixture = browser.__wrapped__(Path(directory.name), patcher)
    client, svc, _ = next(fixture)
    app = client.app
    app.state.fixture_owners = (directory, patcher, fixture, client)
    client.templates.env.loader = FileSystemLoader(ROOT / "src/omakase/plus/templates")
    svc.mode, svc.current, svc.checks = "stale", "searching", 0
    svc.observed = False

    def stamp(delta=timedelta()):
        return (datetime.now(timezone.utc) + delta).isoformat().replace("+00:00", "Z")

    async def cards(user_id, **kwargs):
        svc.calls.append("list")
        code = svc.current
        state = {"searching": "searching", "acquiring": "acquiring", "ready": "ready"}.get(
            code, "deferred"
        )
        return [
            {
                "imdb_id": IMDB,
                "tmdb_id": 693134,
                "title": "Dune: Part Two",
                "year": 2024,
                "overview": "Paul Atreides joins the Fremen on a path of revenge and faces a choice between love and the fate of the universe. This is a synthetic review account; checks use a local fixture.",
                "genres": ["Adventure", "Science Fiction", "Drama"],
                "poster_url": "fixture-poster",
                "backdrop_url": "fixture-backdrop",
                "state": state,
                "code": code,
                "detail": "Synthetic saved request report. No live service was contacted.",
                "requested": True,
                "plex_url": "/qa/never-play" if code == "ready" else None,
                "verified_at": stamp() if code == "ready" else None,
                "request_report": {"code": "searching", "updated_at": stamp(timedelta(hours=-3))},
                "request_observation": {"state": state, "code": code, "observed_at": stamp()}
                if svc.observed
                else None,
            }
        ]

    async def refresh(user_id, imdb_id):
        assert user_id == 1 and imdb_id == IMDB
        svc.checks += 1
        await asyncio.wait_for(svc.gate.wait(), timeout=20)
        svc.current = {
            "updated": "acquiring",
            "failed": "backend_unavailable",
            "saved": "provider_restricted",
        }.get(svc.mode, "searching")
        svc.observed = svc.mode in {"updated", "unchanged"}
        return {
            "imdb_id": IMDB,
            "request_check": {"outcome": svc.mode, "performed": svc.mode != "saved"},
        }

    svc.list, svc.refresh = cards, refresh

    @app.get("/qa/reset/{mode}")
    async def reset(mode: str):
        assert mode in MODES
        svc.mode, svc.checks, svc.calls = mode, 0, []
        svc.gate = asyncio.Event()
        svc.current = {
            "saved": "provider_restricted",
            "ready": "ready",
            "unknown": "backend_state_unknown",
            "no_result": "no_eligible_results",
        }.get(mode, "searching")
        svc.observed = mode == "ready"
        response = RedirectResponse(f"/plus/movies/{IMDB}", status_code=303)
        response.set_cookie(
            "omakase_session",
            client.cookies.get("omakase_session"),
            path="/plus",
            httponly=True,
            samesite="lax",
        )
        return response

    @app.get("/qa/receipt")
    async def receipt():
        return {
            "checks": svc.checks,
            "calls": svc.calls,
            "history_rows": svc.db.execute("SELECT COUNT(*) FROM movie_history").fetchone()[0],
            "acquisitions": 0,
            "media_probes": 0,
        }

    @app.get("/qa/complete")
    async def complete():
        svc.gate.set()
        return {"released": True}

    @app.get("/plus/static/{asset:path}")
    async def static(asset: str):
        directory = (ROOT / "src/omakase/plus/static").resolve()
        candidate = (directory / asset).resolve()
        assert candidate.is_relative_to(directory) and candidate.is_file()
        return FileResponse(candidate)

    # Register before the real artwork route. Existing cached bytes only.
    async def artwork(imdb_id: str, variant: str = "poster"):
        directory = Path(os.environ["OMAKASE_MOVIE_QA_ART_DIR"]).resolve()
        assert imdb_id == IMDB and variant in {"poster", "backdrop"}
        candidate = (
            directory / f"movies-693134.{'banner' if variant == 'backdrop' else 'poster'}.jpg"
        )
        assert candidate.is_file()
        return FileResponse(candidate)

    from fastapi.routing import APIRoute

    app.router.routes.insert(
        0, APIRoute("/plus/movies/{imdb_id}/artwork", artwork, methods=["GET"])
    )
    return app
