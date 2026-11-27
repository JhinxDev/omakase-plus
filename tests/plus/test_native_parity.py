"""Tests for v0.8.0 native iOS/tvOS parity endpoints and projections."""

from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock

from starlette.testclient import TestClient

from omakase.plus import connections, for_you, households, native_tv_test
from omakase.plus.movie_history import MovieHistoryService
from omakase.plus.tv_service import UNSET as TV_UNSET
from omakase.plus.tv_service import TVService
from tests.plus.test_native_tv_test import (
    LIBRARY_API,
    LIBRARY_PAGE,
    ORIGIN,
    browser,
)
from tests.plus.test_native_tv_test import (
    system as system,  # pytest fixture
)


def _pair_personal(app, sessions, *, monkeypatch):
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    owner, csrf = browser(app, sessions[1])
    issued = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
    link = re.search(r"https://testserver#library-([A-F0-9]{20})", issued.text)[1]
    device = TestClient(app, base_url=ORIGIN)
    paired = device.post(LIBRARY_API + "/redeem", json={"code": link})
    device.headers["Authorization"] = "Bearer " + paired.json()["token"]
    return device


def test_native_track_endpoint_supports_anime_movie_tv_and_scope_checks(system, monkeypatch):
    app, db, sessions, _ = system
    device = _pair_personal(app, sessions, monkeypatch=monkeypatch)

    saved_movies = []
    saved_tv = []

    async def fake_movie_save(_self, uid, *, imdb_id, tmdb_id, status, rating, comment=None):
        del _self, comment
        saved_movies.append((uid, imdb_id, tmdb_id, status, rating))
        return {"status": status, "rating": rating}

    async def fake_tv_save(_self, uid, tmdb_id, *, status, rating, **kwargs):
        del _self, kwargs
        saved_tv.append((uid, tmdb_id, status, rating))
        return {"tmdb_id": tmdb_id, "status": status, "rating": rating}

    monkeypatch.setattr(MovieHistoryService, "save", fake_movie_save)
    monkeypatch.setattr(TVService, "save_history", fake_tv_save)

    # Anime completed with rating + progress
    anime_res = device.post(
        LIBRARY_API + "/track",
        json={
            "kind": "anime",
            "media_id": 100,
            "title": "Fixture episode",
            "status": "completed",
            "progress": 12,
            "episode_total": 12,
            "score": 9.0,
        },
    )
    assert anime_res.status_code == 200, anime_res.text
    assert anime_res.json()["watch_status"] == "completed"
    assert anime_res.json()["remove_from_for_you"] is True

    # Movie lookup via imdb_id when media_id is omitted
    db.execute(
        """INSERT INTO movie_catalog(user_id, imdb_id, tmdb_id, title, year, identity_verified, updated_at)
           VALUES(1, 'tt0811080', 7459, 'Speed Racer', 2008, 1, '2026-11-27T00:00:00Z')"""
    )
    db.commit()
    movie_res = device.post(
        LIBRARY_API + "/track",
        json={
            "kind": "movie",
            "imdb_id": "tt0811080",
            "title": "Speed Racer",
            "status": "completed",
            "score": 8.5,
        },
    )
    assert movie_res.status_code == 200, movie_res.text
    assert saved_movies == [(1, "tt0811080", 7459, "completed", 8.5)]

    # TV tracking and not_for_me dismissal
    tv_res = device.post(
        LIBRARY_API + "/track",
        json={"kind": "tv", "media_id": 153312, "title": "Tulsa King", "status": "watching"},
    )
    assert tv_res.status_code == 200
    assert saved_tv == [(1, 153312, "watching", TV_UNSET)]

    dismiss = device.post(
        LIBRARY_API + "/track",
        json={"kind": "tv", "media_id": 153313, "title": "Skip Show", "status": "not_for_me"},
    )
    assert dismiss.status_code == 200
    assert dismiss.json()["remove_from_for_you"] is True

    # Shared household scope cannot mutate personal tracker history
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    shared_id = households.create_household(db, user_id=1, name="Home")
    forbidden = device.post(
        LIBRARY_API + f"/track?library_id={shared_id}",
        json={"kind": "tv", "media_id": 1, "title": "X", "status": "completed"},
    )
    assert forbidden.status_code == 404


def test_native_for_you_cards_carry_metadata_and_validated_trailers(system, monkeypatch):
    app, _, sessions, _ = system
    device = _pair_personal(app, sessions, monkeypatch=monkeypatch)

    card = {
        "id": 9101,
        "kind": "anime",
        "title": "Frieren",
        "poster_url": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx154587.jpg",
        "summary": "A mage journeys after the demon king falls.",
        "year": 2023,
        "format": "Anime series",
        "episodes": 28,
        "score": 93,
        "score_label": "AniList",
        "genres": ["Adventure", "Drama", "Fantasy", "Action", "Ignored5th"],
        "airing": False,
        "trailer": {"site": "youtube", "id": "ZEf268R5pP0"},
        "owned": False,
        "saved": True,
        "can_request": True,
        "request": None,
    }
    bad_trailer_card = {
        **card,
        "id": 9102,
        "title": "Bad Trailer Pick",
        "trailer": {"site": "vimeo", "id": "javascript:alert(1)"},
    }
    monkeypatch.setattr(
        for_you,
        "recommendations",
        AsyncMock(
            return_value={
                "category": "anime",
                "rows": [
                    {"key": "top", "title": "Top Picks", "cards": [card, bad_trailer_card]},
                ],
                "requests": [],
                "warnings": [],
            }
        ),
    )

    res = device.get(LIBRARY_API + "/for-you?category=anime")
    assert res.status_code == 200, res.text
    cards = res.json()["rows"][0]["cards"]
    assert cards[0]["year"] == 2023
    assert cards[0]["format"] == "Anime series"
    assert cards[0]["episodes"] == 28
    assert cards[0]["score_label"] == "AniList"
    assert cards[0]["genres"] == ["Adventure", "Drama", "Fantasy", "Action"]
    assert cards[0]["trailer_id"] == "ZEf268R5pP0"
    assert cards[0]["saved"] is True
    assert cards[1]["trailer_id"] is None


def test_native_catalog_views_collection_watching_watched(system, monkeypatch):
    app, db, sessions, _ = system
    monkeypatch.setattr(connections, "_read", lambda *_: "")
    device = _pair_personal(app, sessions, monkeypatch=monkeypatch)

    # Insert one watching movie and one watched movie whose copy was removed
    db.execute(
        """INSERT INTO movie_catalog(user_id, imdb_id, tmdb_id, title, year, poster_url, identity_verified, updated_at)
           VALUES(1, 'tt1111111', 101, 'Watching Film', 2024, 'https://image.tmdb.org/t/p/w342/a.jpg', 1, '2026-11-27T00:00:00Z'),
                 (1, 'tt2222222', 102, 'Finished Cleaned Film', 2008, 'https://image.tmdb.org/t/p/w342/b.jpg', 1, '2026-11-27T00:00:00Z')"""
    )
    db.execute(
        """INSERT INTO movie_history(user_id, imdb_id, tmdb_id, status, rating, status_source, rating_source, created_at, updated_at)
           VALUES(1, 'tt1111111', 101, 'watching', NULL, 'manual', 'none', '2026-11-27T00:00:00Z', '2026-11-27T00:00:00Z'),
                 (1, 'tt2222222', 102, 'completed', 8.5, 'manual', 'manual', '2026-11-27T00:00:00Z', '2026-11-27T00:00:00Z')"""
    )
    db.execute(
        """INSERT INTO movie_requests(user_id, imdb_id, state, code, detail, created_at, updated_at)
           VALUES(1, 'tt2222222', 'unavailable', 'copy_removed', 'Copy removed', '2026-11-27T00:00:00Z', '2026-11-27T00:00:00Z')"""
    )
    db.commit()

    monkeypatch.setattr(
        native_tv_test, "personal_plex_movies", AsyncMock(return_value=("tt1111111",))
    )

    watching = device.get(LIBRARY_API + "/catalog?kind=movie&view=watching").json()["items"]
    assert [item["id"] for item in watching] == ["tt1111111"]
    assert watching[0]["tmdb_id"] == 101
    assert watching[0]["watch_status"] == "watching"

    watched = device.get(LIBRARY_API + "/catalog?kind=movie&view=watched").json()["items"]
    assert [item["id"] for item in watched] == ["tt2222222"]
    assert watched[0]["rating"] == 8.5
    assert watched[0]["watch_status"] == "completed"
    assert watched[0]["year"] == 2008


def test_native_requests_include_library_id_and_episode_counts(system, monkeypatch):
    app, db, sessions, _ = system
    device = _pair_personal(app, sessions, monkeypatch=monkeypatch)

    monkeypatch.setattr(
        for_you,
        "dashboard",
        AsyncMock(
            return_value={
                "categories": [
                    {
                        "key": "active",
                        "title": "Active",
                        "requests": [
                            {
                                "identity": "anime:100",
                                "kind": "anime",
                                "media_kind": "anime",
                                "title_id": "100",
                                "title": "Fixture episode",
                                "supported_stages": ["saved", "searched", "acquired", "library"],
                                "stage": "library",
                                "stage_label": "In your library",
                                "stages": [],
                                "state": "ready",
                                "label": "Ready to Play",
                                "status_label": "Ready to Play",
                                "detail": "In library",
                                "category": "active",
                                "resolved": True,
                                "removed": False,
                                "can_check": False,
                                "can_retry": False,
                            },
                            {
                                "identity": "tv:153312",
                                "kind": "tv",
                                "media_kind": "tv",
                                "title_id": "153312",
                                "title": "Tulsa King",
                                "supported_stages": ["saved", "library"],
                                "stage": "saved",
                                "stage_label": "Saved",
                                "stages": [],
                                "state": "partial",
                                "label": "4 / 39 Ready",
                                "status_label": "4 / 39 Ready",
                                "detail": "4 episodes ready",
                                "category": "active",
                                "resolved": False,
                                "removed": False,
                                "can_check": True,
                                "can_retry": False,
                                "progress": {"ready": 4, "expected": 39, "percent": 10},
                            },
                        ],
                    },
                    {"key": "attention", "title": "Needs attention", "requests": []},
                    {"key": "history", "title": "History", "requests": []},
                ]
            }
        ),
    )

    res = device.get(LIBRARY_API + "/requests")
    assert res.status_code == 200, res.text
    reqs = res.json()["categories"][0]["requests"]
    assert reqs[0]["library_id"] == "1"
    assert reqs[1]["library_id"] == "153312"
    assert reqs[1]["episodes_ready"] == 4
    assert reqs[1]["episodes_expected"] == 39
    assert json.loads(res.text)["categories"][0]["requests"][1]["title"] == "Tulsa King"
