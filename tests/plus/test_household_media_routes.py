"""Authenticated selected-library collection/detail/art and personal history journeys."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

from omakase.plus import households as hh
from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.household_routes import create_household_router
from omakase.plus.movies_routes import create_movies_router
from omakase.plus.search_routes import create_search_router
from omakase.plus.tv_routes import create_tv_router
from omakase.plus.web_security import csrf_token

ROOT = Path(__file__).resolve().parents[2] / "src/omakase/plus"


def fixture_app(*, enabled=True, requests=False):
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for user in (1, 2, 3):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"person{user}@example.invalid", "synthetic"),
        )
        hh.ensure_personal_library(db, user_id=user)
    db.commit()
    lib = hh.create_household(db, user_id=1, name="Shared Home", acquisition_requests=requests)
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    token = hh.create_invite(db, owner, now=100)
    hh.accept_invite(db, viewer_id=2, token=token["token"], now=101)
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(1,100,'Code Geass: Akito the Exiled — A very long subtitle with four lines of meaningful title',1)"
    ).lastrowid
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,identity_verified,updated_at) VALUES(1,'tt1234567',42,'Shared film',1,'now')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,rating,created_at,updated_at) VALUES(1,'tt1234567',42,'completed',9,'now','now')"
    )
    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(1,55,?,'now')",
        (
            json.dumps(
                {
                    "title": "Shared television",
                    "poster_url": "https://image.tmdb.org/t/p/w500/safe.jpg",
                    "seasons": [],
                    "in_collection": True,
                }
            ),
        ),
    )
    db.execute(
        "INSERT INTO tv_history(user_id,tmdb_id,status,rating,updated_at) VALUES(1,55,'completed',10,'now')"
    )
    db.execute(
        "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(1,55,1,1,?,1,'plex',1,'now')",
        (json.dumps({"name": "A cached released episode", "air_date": "2000-01-01"}),),
    )
    db.commit()
    items = {}
    for domain, identity, title, legacy, season in [
        (
            "anime",
            "100",
            "Code Geass: Akito the Exiled — A very long subtitle with four lines of meaningful title",
            str(monitor),
            1,
        ),
        ("movie", "tt1234567", "Shared film", "tt1234567", 0),
        ("tv", "55", "Shared television", "55", 0),
    ]:
        items[domain] = hh.LibraryRepository(db, owner).add(
            domain=domain, canonical_id=identity, title=title, legacy_id=legacy, season=season
        )
    app = FastAPI()
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.globals["asset_version"] = "synthetic-household"

    def context(request, user, **kwargs):
        return {"email": user.email, "active_page": "library", "synthetic_review": True}

    app.include_router(create_household_router(templates, context, enabled=enabled), prefix="/plus")

    # Fails immediately if scoped GETs fall through to an owner/member service.
    def no_service(db, user):
        raise AssertionError("Scoped reads must not invoke operational service")

    app.include_router(
        create_movies_router(
            templates, context, service_factory=no_service, households_enabled=enabled
        ),
        prefix="/plus",
    )
    app.include_router(
        create_tv_router(
            templates, context, service_factory=no_service, households_enabled=enabled
        ),
        prefix="/plus",
    )
    app.include_router(
        create_search_router(
            templates,
            context,
            anime_factory=no_service,
            movie_factory=no_service,
            tv_factory=no_service,
            households_enabled=enabled,
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    sessions = {user: create_session(db, user) for user in (1, 2, 3)}
    return app, db, sessions, lib, items


def client(app, session):
    c = TestClient(app, base_url="https://testserver", follow_redirects=False)
    c.cookies.set("omakase_session", session)
    request = Request(
        {"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]}
    )
    return c, csrf_token(request)


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "send",
        AsyncMock(side_effect=AssertionError("No provider in household read tests")),
    )
    data = fixture_app()
    yield data
    data[1].close()


def test_actual_three_account_scope_detail_collection_and_get_without_dml(state):
    app, db, sessions, lib, items = state
    member, token = client(app, sessions[2])
    before = db.total_changes
    for path in (
        f"/plus/libraries/{lib}",
        f"/plus/libraries/{lib}/items/{items['movie']}",
        "/plus/movies",
        "/plus/tv",
    ):
        response = member.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "Not tracked" in response.text
        assert "Your rating 9" not in response.text and "Your rating 10" not in response.text
    assert db.total_changes == before
    for domain, path in [("movie", "/plus/movies/tt1234567"), ("tv", "/plus/tv/55")]:
        response = member.get(path)
        assert (
            response.status_code == 307
            and response.headers["location"] == f"/plus/libraries/{lib}/items/{items[domain]}"
        )
    outsider, _ = client(app, sessions[3])
    assert outsider.get(f"/plus/libraries/{lib}").status_code == 404
    assert outsider.get(f"/plus/libraries/{lib}/items/{items['anime']}").status_code == 404
    assert member.get("/plus/movies/tt7654321").status_code == 409
    assert member.get("/plus/tv/999").status_code == 409
    assert member.get("/plus/movies/not-an-imdb").status_code == 422


def test_actual_local_movie_tv_save_independent_and_csrf_blocked(state):
    app, db, sessions, lib, items = state
    member, token = client(app, sessions[2])
    for domain, rating in [("movie", "6.5"), ("tv", "7")]:
        path = f"/plus/libraries/{lib}/items/{items[domain]}/history"
        assert (
            member.post(
                path, data={"status": "completed", "rating_action": "save", "rating": rating}
            ).status_code
            == 403
        )
        response = member.post(
            path,
            data={
                "csrf_token": token,
                "status": "watching",
                "rating_action": "save",
                "rating": rating,
            },
        )
        assert response.status_code == 303
        table, key = (
            ("movie_history", "imdb_id") if domain == "movie" else ("tv_history", "tmdb_id")
        )
        identity = "tt1234567" if domain == "movie" else 55
        row = db.execute(
            f"SELECT status,rating FROM {table} WHERE user_id=2 AND {key}=?", (identity,)
        ).fetchone()
        assert tuple(row) == ("watching", float(rating))
        owner = db.execute(
            f"SELECT status,rating FROM {table} WHERE user_id=1 AND {key}=?", (identity,)
        ).fetchone()
        assert tuple(owner) == ("completed", 9 if domain == "movie" else 10)
        assert (
            member.post(
                path,
                data={
                    "csrf_token": token,
                    "status": "completed",
                    "rating_action": "keep",
                    "rating": "",
                },
            ).status_code
            == 303
        )
        assert db.execute(
            f"SELECT rating FROM {table} WHERE user_id=2 AND {key}=?", (identity,)
        ).fetchone()[0] == float(rating)
        assert (
            member.post(
                path, data={"csrf_token": token, "status": "completed", "rating_action": "clear"}
            ).status_code
            == 303
        )
        assert (
            db.execute(
                f"SELECT rating FROM {table} WHERE user_id=2 AND {key}=?", (identity,)
            ).fetchone()[0]
            is None
        )
    assert db.execute("SELECT COUNT(*) FROM movie_requests").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM movie_submissions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM media_title_removals").fetchone()[0] == 0


def test_actual_art_auth_before_fetch_and_revoked_no_cache(state, monkeypatch):
    from omakase.plus import artwork_delivery, movie_artwork, posters

    app, db, sessions, lib, items = state
    calls = []

    async def fake(*args, **kwargs):
        calls.append(1)
        return b"synthetic-image", "image/webp"

    monkeypatch.setattr(posters, "poster_bytes", fake)
    monkeypatch.setattr(movie_artwork, "movie_artwork", fake)
    monkeypatch.setattr(
        artwork_delivery, "optimized_artwork", lambda payload, **kwargs: (payload, "image/webp")
    )
    member, token = client(app, sessions[2])
    outsider, _ = client(app, sessions[3])
    path = f"/plus/libraries/{lib}/items/{items['anime']}/artwork"
    assert outsider.get(path).status_code == 404 and calls == []
    assert (
        TestClient(app, base_url="https://testserver").get(path).status_code == 401 and calls == []
    )
    result = member.get(path)
    assert result.status_code == 200 and result.content == b"synthetic-image"
    assert result.headers["cache-control"] == "private, no-store"
    assert len(calls) == 1
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    assert member.get(path).status_code == 404 and len(calls) == 1
    assert (
        member.post(
            f"/plus/libraries/{lib}/items/{items['anime']}/track", data={"csrf_token": token}
        ).status_code
        == 404
    )


def test_disabled_router_default_does_not_expose_media(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    app, db, sessions, lib, items = fixture_app(enabled=False)
    try:
        member, _ = client(app, sessions[2])
        assert member.get(f"/plus/libraries/{lib}").status_code == 404
        assert member.get(f"/plus/libraries/{lib}/items/{items['anime']}").status_code == 404
    finally:
        db.close()


def test_global_search_saved_household_results_survive_provider_outage_without_hidden_writes(state):
    app, db, sessions, lib, items = state
    member, _ = client(app, sessions[2])
    before = db.total_changes
    result = member.get("/plus/search/suggestions?q=Shared&kind=all")
    assert result.status_code == 200
    payload = result.json()
    assert {card["media_type"] for card in payload["results"]} == {"movie", "tv"}
    assert len(payload["warnings"]) == 3
    assert all(
        card["watch_status"] == "unknown" and card["rating"] is None for card in payload["results"]
    )
    assert all(
        card["poster_url"].startswith(f"/plus/libraries/{lib}/items/")
        for card in payload["results"]
    )
    assert db.total_changes == before
    assert member.get("/plus/search/suggestions?q=Private&kind=all").status_code == 503
    page = member.get("/plus/catalog/anime/100")
    assert page.status_code == 307 and page.headers["location"].endswith(items["anime"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-to", required=True)
    parser.add_argument("--requests-enabled", action="store_true")
    args = parser.parse_args()
    os.environ["OMAKASE_PLUS_PRIVATE"] = "true"
    app, db, sessions, lib, items = fixture_app(requests=args.requests_enabled)
    member, _ = client(app, sessions[2])
    target = Path(args.render_to)
    target.mkdir(parents=True, exist_ok=True)
    paths = {
        "collection": f"/plus/libraries/{lib}",
        **{domain: f"/plus/libraries/{lib}/items/{item}" for domain, item in items.items()},
    }
    for name, path in paths.items():
        result = member.get(path)
        assert result.status_code == 200
        (target / (name + ".html")).write_text(result.text, encoding="utf-8")
    db.close()


@pytest.mark.parametrize(("path", "domain"), [("/plus/movies", "movie"), ("/plus/tv", "tv")])
def test_selected_library_collection_page_is_bounded_and_advances(state, path, domain):
    app, db, sessions, lib, items = state
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    for number in range(1, 62):
        identity = f"tt{3000000 + number}" if domain == "movie" else str(3000000 + number)
        hh.LibraryRepository(db, owner).add(
            domain=domain, canonical_id=identity, title=f"Page title {number:03}"
        )
    member, _ = client(app, sessions[2])
    first = member.get(path + "?q=Page")
    second = member.get(path + "?q=Page&offset=60")
    assert first.status_code == second.status_code == 200
    assert "Page title 001" in first.text and "Page title 061" not in first.text
    assert "Page title 061" in second.text and "Page title 001" not in second.text
    assert member.get(path + "?offset=10001").status_code == 422


def test_actual_tv_episode_form_records_viewer_only_and_keeps_series_explicit(state):
    app, db, sessions, lib, items = state
    member, token = client(app, sessions[2])
    path = f"/plus/libraries/{lib}/items/{items['tv']}/episode"
    assert (
        member.post(path, data={"season": "1", "episode": "1", "watched": "1"}).status_code == 403
    )
    result = member.post(
        path, data={"csrf_token": token, "season": "1", "episode": "1", "watched": "1"}
    )
    assert result.status_code == 303
    assert (
        db.execute(
            "SELECT watched FROM tv_episodes WHERE user_id=2 AND tmdb_id=55 AND season_number=1 AND episode_number=1"
        ).fetchone()[0]
        == 1
    )
    assert tuple(
        db.execute("SELECT status,rating FROM tv_history WHERE user_id=2 AND tmdb_id=55").fetchone()
    ) == ("unwatched", None)
    assert tuple(
        db.execute("SELECT status,rating FROM tv_history WHERE user_id=1 AND tmdb_id=55").fetchone()
    ) == ("completed", 10)
    assert (
        member.post(
            path, data={"csrf_token": token, "season": "1", "episode": "999", "watched": "1"}
        ).status_code
        == 422
    )


def test_shared_movie_request_copy_truthfully_separates_backend_filters_and_personal_history(
    monkeypatch,
):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "send",
        AsyncMock(side_effect=AssertionError("No provider in disclosure test")),
    )
    app, db, sessions, lib, items = fixture_app(requests=True)
    try:
        member, _ = client(app, sessions[2])
        page = member.get(f"/plus/libraries/{lib}/items/{items['movie']}")
        assert page.status_code == 200
        assert "Request shared movie media" in page.text
        assert "Riven chooses its configured release filters" in page.text
        assert "recorded for verification after delivery" in page.text
        assert "does not change backend filters" in page.text
        assert "Your rating 9/10" not in page.text
        assert db.execute("SELECT COUNT(*) FROM movie_history WHERE user_id=2").fetchone()[0] == 0
    finally:
        db.close()
