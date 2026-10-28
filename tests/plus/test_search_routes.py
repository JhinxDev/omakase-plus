"""Direct Search is private, authenticated, cache-safe and metadata-only."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db


@pytest.fixture
def browser(tmp_path, monkeypatch):
    assert importlib.util.find_spec("omakase.plus.search_routes") is not None, (
        "Search router is missing"
    )
    module = importlib.import_module("omakase.plus.search_routes")
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    db = sqlite3.connect(tmp_path / "search.db", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'search@example.invalid','h')")
    db.commit()
    calls = []
    controls = {
        "delay": 0,
        "active": 0,
        "peak": 0,
        "fail": {},
        "attempts": {"anime": 0, "movies": 0, "tv": 0},
    }

    async def provider(kind):
        controls["attempts"][kind] += 1
        controls["active"] += 1
        controls["peak"] = max(controls["peak"], controls["active"])
        try:
            await asyncio.sleep(controls["delay"])
            if kind in controls["fail"]:
                raise controls["fail"][kind]
        finally:
            controls["active"] -= 1

    class Templates:
        def TemplateResponse(self, request, name, context, **kwargs):
            return JSONResponse(
                {
                    "template": name,
                    "query": context.get("query"),
                    "kind": context.get("kind"),
                    "cards": context.get("cards"),
                    "card": context.get("card"),
                    "search_error": context.get("search_error"),
                    "search_warnings": context.get("search_warnings"),
                },
                **kwargs,
            )

    class Anime:
        async def search(self, q):
            await provider("anime")
            if q == "limited":
                from omakase.plus.catalog_search import CatalogSearchError

                raise CatalogSearchError("rate_limited", retry_after=60)
            calls.append(("anime", q))
            if "batches" in controls:
                return controls["batches"]["anime"]
            return [
                {
                    "id": 21,
                    "anilist_id": 21,
                    "title": "One Piece",
                    "poster_url": "/plus/media/21/poster",
                    "url": "/plus/catalog/anime/21",
                    "watch_status": "unknown",
                }
            ]

        async def detail(self, media_id):
            if media_id == 429:
                from omakase.plus.catalog_search import CatalogSearchError

                raise CatalogSearchError("rate_limited", retry_after=60)
            calls.append(("detail", media_id))
            return {
                "id": media_id,
                "anilist_id": media_id,
                "title": "One Piece",
                "description": "A story.",
                "genres": ["Adventure"],
            }

    class Movies:
        def __init__(self):
            self.db = db

        async def search(self, user_id, q):
            await provider("movies")
            calls.append(("movies", q))
            if "batches" in controls:
                return controls["batches"]["movies"]
            return [
                {
                    "imdb_id": "tt0084787",
                    "tmdb_id": 1091,
                    "title": "The Thing",
                    "poster_url": "https://image.tmdb.org/t/p/w342/poster.jpg",
                    "backdrop_url": "https://image.tmdb.org/t/p/w1280/backdrop.jpg",
                }
            ]

    class Television:
        async def search(self, user_id, q):
            await provider("tv")
            calls.append(("tv", q))
            if "batches" in controls:
                return controls["batches"]["tv"]
            return [
                {
                    "tmdb_id": 1396 if q.casefold() == "breaking bad" else 420,
                    "title": "Breaking Bad" if q.casefold() == "breaking bad" else "A series",
                    "in_collection": True,
                    "status": "watching",
                    "status_source": "manual",
                    "watched_episodes": 2,
                    "rating": 8,
                    "poster_url": "/plus/tv/420/artwork",
                }
            ]

    app = FastAPI()
    app.include_router(
        module.create_search_router(
            Templates(),
            anime_factory=lambda conn, user: Anime(),
            movie_factory=lambda conn, user: Movies(),
            tv_factory=lambda conn, user: Television(),
        ),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        client.search_controls = controls
        yield client, db, calls
    db.close()


def login(client, db):
    client.cookies.set(
        "omakase_session", create_session(db, 1), domain="testserver.local", path="/plus"
    )


def test_all_search_routes_require_authentication_and_private_cache_headers(browser):
    client, db, calls = browser
    for path in ("/plus/search?q=one", "/plus/search/suggestions?q=one", "/plus/catalog/anime/21"):
        response = client.get(path)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"
    assert calls == []


def test_private_guard_blocks_search_even_with_a_valid_session(browser, monkeypatch):
    client, db, calls = browser
    login(client, db)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/search?q=one").status_code == 404
    assert calls == []


def test_suggestions_are_direct_catalog_cards_without_acquisition(browser):
    client, db, calls = browser
    login(client, db)
    response = client.get("/plus/search/suggestions?q=one&kind=anime")
    assert response.status_code == 200 and response.json()["results"][0]["id"] == 21
    assert response.headers["cache-control"] == "private, no-store"
    assert calls == [("anime", "one")]
    assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 0


def test_empty_short_and_invalid_kind_never_call_catalogs(browser):
    client, db, calls = browser
    login(client, db)
    assert client.get("/plus/search/suggestions?q=a").json()["results"] == []
    assert client.get("/plus/search").json()["cards"] == []
    assert client.get("/plus/search/suggestions?q=one&kind=other").status_code == 422
    assert client.get("/plus/search/suggestions", params={"q": "x" * 121}).status_code == 422
    assert calls == []


def test_html_and_detail_context_match_ui_contract(browser):
    client, db, calls = browser
    login(client, db)
    page = client.get("/plus/search?q=one&kind=anime").json()
    assert page["template"] == "search.html" and page["cards"][0]["url"] == "/plus/catalog/anime/21"
    detail = client.get("/plus/catalog/anime/21").json()
    assert detail["template"] == "catalog_anime.html" and detail["card"]["id"] == 21


def test_movie_search_reuses_exact_ids_history_and_same_origin_art(browser):
    client, db, calls = browser
    login(client, db)
    card = client.get("/plus/search/suggestions?q=thing&kind=movies").json()["results"][0]
    assert card["imdb_id"] == "tt0084787" and card["tmdb_id"] == 1091
    assert card["url"] == "/plus/movies/tt0084787"
    assert card["poster_url"] == "/plus/movies/tt0084787/artwork"
    assert not card.get("backdrop_url") or card["backdrop_url"].startswith("/plus/")
    assert calls == [("movies", "thing")]


def test_retry_after_is_preserved_for_json_and_html_catalog_responses(browser):
    client, db, calls = browser
    login(client, db)
    for path in (
        "/plus/search?q=limited&kind=anime",
        "/plus/search/suggestions?q=limited&kind=anime",
        "/plus/catalog/anime/429",
    ):
        response = client.get(path)
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.headers["Cache-Control"] == "private, no-store"


def test_tv_suggestions_keep_exact_identity_and_saved_progress(browser):
    client, db, calls = browser
    login(client, db)
    response = client.get("/plus/search/suggestions?kind=tv&q=series")
    assert response.status_code == 200
    card = response.json()["results"][0]
    assert card["url"] == "/plus/tv/420"
    assert card["progress"] == 2
    assert card["watch_status"] == "watching"
    assert card["rating"] == 8
    assert calls == [("tv", "series")]


@pytest.mark.parametrize(
    "query,title,media_type,url",
    [
        ("breaking bad", "Breaking Bad", "tv", "/plus/tv/1396"),
        ("the thing", "The Thing", "movie", "/plus/movies/tt0084787"),
        ("one piece", "One Piece", "anime", "/plus/catalog/anime/21"),
    ],
)
def test_global_default_search_finds_each_domain_with_exact_identity(
    browser, query, title, media_type, url
):
    client, db, calls = browser
    login(client, db)
    response = client.get("/plus/search/suggestions", params={"q": query})
    assert response.status_code == 200
    cards = response.json()["results"]
    assert cards[0]["title"] == title
    assert cards[0]["media_type"] == media_type and cards[0]["url"] == url
    assert {kind for kind, _ in calls} == {"anime", "movies", "tv"}
    assert len(cards) <= 8
    for table in ("media_monitors", "watch_events", "movie_history", "tv_history"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_mixed_domains_run_concurrently_with_a_fixed_fanout(browser):
    client, db, _ = browser
    login(client, db)
    client.search_controls["delay"] = 0.02
    response = client.get("/plus/search/suggestions?q=mixed&kind=all")
    assert response.status_code == 200
    assert client.search_controls["peak"] == 3
    assert client.search_controls["active"] == 0
    assert client.search_controls["attempts"] == {"anime": 1, "movies": 1, "tv": 1}


def test_partial_failure_keeps_tv_result_and_never_reports_a_complete_empty_search(browser):
    from omakase.plus.catalog_search import CatalogSearchError

    client, db, _ = browser
    login(client, db)
    client.search_controls["fail"]["anime"] = CatalogSearchError("transport_error")
    response = client.get("/plus/search/suggestions?q=breaking%20bad")
    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["title"] == "Breaking Bad"
    assert payload["warnings"][0]["kind"] == "anime"
    page = client.get("/plus/search?q=breaking%20bad").json()
    assert page["search_warnings"] and not page["search_error"]


def test_provider_retry_after_blocks_only_that_domain_and_owner(browser):
    client, db, _ = browser
    login(client, db)
    first = client.get("/plus/search/suggestions?q=limited")
    assert first.status_code == 200 and first.headers["Retry-After"] == "60"
    before = dict(client.search_controls["attempts"])
    second = client.get("/plus/search/suggestions?q=one%20piece")
    assert second.status_code == 200
    assert client.search_controls["attempts"]["anime"] == before["anime"]
    assert client.search_controls["attempts"]["tv"] == before["tv"] + 1
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(2,'other@example.invalid','h')")
    db.commit()
    client.cookies.set(
        "omakase_session", create_session(db, 2), domain="testserver.local", path="/plus"
    )
    assert client.get("/plus/search/suggestions?q=one%20piece").status_code == 200
    assert client.search_controls["attempts"]["anime"] == before["anime"] + 1


def test_global_query_rate_limit_stops_fanout_before_provider_calls(browser, monkeypatch):
    module = importlib.import_module("omakase.plus.search_routes")
    monkeypatch.setattr(module, "_SEARCH_RATE_LIMIT", 1)
    client, db, _ = browser
    login(client, db)
    assert client.get("/plus/search/suggestions?q=one%20piece").status_code == 200
    attempts = dict(client.search_controls["attempts"])
    response = client.get("/plus/search/suggestions?q=breaking%20bad")
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
    assert client.search_controls["attempts"] == attempts


def test_slow_providers_are_bounded_and_do_not_leave_inflight_searches(browser, monkeypatch):
    module = importlib.import_module("omakase.plus.search_routes")
    monkeypatch.setattr(module, "_SEARCH_TIMEOUT", 0.001)
    client, db, _ = browser
    login(client, db)
    client.search_controls["delay"] = 0.05
    response = client.get("/plus/search/suggestions?q=mixed")
    assert response.status_code == 503
    assert len(response.json()["warnings"]) == 3 and not response.json()["results"]
    assert client.search_controls["active"] == 0
    client.search_controls["delay"] = 0
    assert client.get("/plus/search/suggestions?q=mixed").status_code == 200


@pytest.mark.parametrize("global_cap,expected_peak", [(8, 6), (1, 3)])
def test_inflight_admission_caps_provider_work_and_recovers_after_completion(
    browser, monkeypatch, global_cap, expected_peak
):
    module = importlib.import_module("omakase.plus.search_routes")
    monkeypatch.setattr(module, "_SEARCH_GLOBAL_INFLIGHT", global_cap)
    client, db, _ = browser
    session = create_session(db, 1)
    client.search_controls["delay"] = 0.02

    async def requests():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="https://testserver",
            headers={"Cookie": "omakase_session=" + session},
        ) as request:
            return await asyncio.gather(
                *(request.get("/plus/search/suggestions?q=mixed") for _ in range(3))
            )

    responses = asyncio.run(requests())
    assert sorted(response.status_code for response in responses) == (
        [200, 200, 429] if global_cap == 8 else [200, 429, 429]
    )
    assert client.search_controls["peak"] == expected_peak
    assert client.search_controls["active"] == 0
    login(client, db)
    assert client.get("/plus/search/suggestions?q=mixed").status_code == 200


def test_mixed_cap_keeps_every_domain_and_deduplicates_typed_identities(browser):
    client, db, _ = browser
    login(client, db)
    client.search_controls["batches"] = {
        "anime": [
            {"id": n, "title": f"Anime {n}", "url": f"/plus/catalog/anime/{n}"}
            for n in range(1, 13)
        ],
        "movies": [
            {"imdb_id": f"tt{n:07d}", "tmdb_id": n, "title": f"Movie {n}"} for n in range(1, 13)
        ],
        "tv": [{"tmdb_id": n, "title": f"TV {n}", "status_source": "none"} for n in range(1, 13)],
    }
    payload = client.get("/plus/search/suggestions?q=mixed").json()
    cards = payload["results"]
    assert len(cards) == 8
    assert {card["media_type"] for card in cards} == {"anime", "movie", "tv"}
    assert len({(card["media_type"], card["id"]) for card in cards}) == 8
    assert [card["media_type"] for card in cards[:3]] == ["anime", "movie", "tv"]


def test_total_provider_failure_is_reported_by_domain_without_a_fake_empty_success(browser):
    client, db, _ = browser
    login(client, db)
    client.search_controls["fail"] = {
        kind: RuntimeError("private diagnostic must not escape")
        for kind in ("anime", "movies", "tv")
    }
    response = client.get("/plus/search/suggestions?q=mixed")
    assert response.status_code == 503
    assert len(response.json()["warnings"]) == 3
    assert {w["label"] for w in response.json()["warnings"]} == {"Anime", "Movies", "TV"}
    assert "private diagnostic" not in response.text


def test_long_provider_cooldown_never_resumes_before_its_deadline(browser, monkeypatch):
    from omakase.plus.catalog_search import CatalogSearchError

    module = importlib.import_module("omakase.plus.search_routes")
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: clock[0])
    client, db, _ = browser
    login(client, db)
    client.search_controls["fail"]["anime"] = CatalogSearchError("rate_limited", retry_after=7200)
    first = client.get("/plus/search/suggestions?q=one%20piece")
    assert first.headers["Retry-After"] == "7200"
    assert first.json()["warnings"][0]["retry_after"] == 7200
    client.search_controls["fail"].clear()
    for elapsed, remaining in [(3601, 3599), (7199, 1)]:
        clock[0] = elapsed * 1_000_000_000
        response = client.get("/plus/search/suggestions?q=one%20piece")
        assert response.headers["Retry-After"] == str(remaining)
        assert client.search_controls["attempts"]["anime"] == 1
        assert response.json()["warnings"][0]["retry_after"] == remaining
    clock[0] = 7200 * 1_000_000_000
    response = client.get("/plus/search/suggestions?q=one%20piece")
    assert response.status_code == 200 and not response.json()["warnings"]
    assert client.search_controls["attempts"]["anime"] == 2


@pytest.mark.parametrize("retry", [10**400, "7200", float("inf"), float("nan"), "invalid", None])
def test_provider_cooldown_handles_large_and_malformed_delays(browser, retry):
    from omakase.plus.catalog_search import CatalogSearchError

    client, db, _ = browser
    login(client, db)
    client.search_controls["fail"]["anime"] = CatalogSearchError("rate_limited", retry_after=retry)
    first = client.get("/plus/search/suggestions?q=one%20piece")
    expected = retry if isinstance(retry, int) else 7200 if retry == "7200" else 60
    assert first.status_code == 200
    assert first.headers["Retry-After"] == str(expected)
    second = client.get("/plus/search/suggestions?q=one%20piece")
    assert second.status_code == 200
    assert client.search_controls["attempts"]["anime"] == 1
    assert int(second.headers["Retry-After"]) > 0
