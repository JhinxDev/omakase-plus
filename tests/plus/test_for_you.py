"""Shared For You intake and request proof, with offline catalog/provider fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omakase.plus import discover_hub, for_you, households, movies_store, search_routes
from omakase.plus.media_types import MediaTarget
from tests.plus.test_dashboard import (
    _connect_client_db,
    _csrf_token,
    _signup_and_login,
    _user_id,
)
from tests.plus.test_dashboard import client as client
from tests.plus.test_movies import IMDB, TMDB, NativeMovies, run, service


def test_anime_browse_is_read_only_and_explicit_request_queues_exact_target(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    anime_id = 81001
    candidate = discover_hub._anime_card(anime_id, "Test anime") | {
        "genres": ("Drama",),
        "poster_url": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx81001-test.jpg",
    }
    monkeypatch.setattr(discover_hub, "anime_catalog", AsyncMock(return_value=(candidate,)))
    resolved = Mock(
        return_value=MediaTarget(
            anilist_id=anime_id,
            title="Test anime",
            search_titles=("Test anime",),
            format="TV",
            status="FINISHED",
            episodes=8,
            aired_episodes=8,
            season_number=1,
            season="WINTER",
            season_year=2020,
            genres=("Drama",),
            next_airing_at=None,
        )
    )
    monkeypatch.setattr(for_you, "resolve_direct_request", resolved)
    monkeypatch.setattr(for_you, "read_secret", lambda *_: "test-connection")

    cards = client.get("/plus/discover/rows?kind=anime")
    assert cards.status_code == 200
    assert 'aria-label="Request Test anime"' in cards.text
    assert "Your requests" not in cards.text
    resolved.assert_not_called()
    with _connect_client_db(client) as db:
        assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM for_you_requests").fetchone()[0] == 0

    form = {"kind": "anime", "title_id": str(anime_id)}
    no_csrf = client.post("/plus/discover/requests/submit", data=form, follow_redirects=False)
    assert no_csrf.status_code == 403
    resolved.assert_not_called()

    response = client.post(
        "/plus/discover/requests/submit",
        data={**form, "csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Requested" in response.headers["location"]
    with _connect_client_db(client) as db:
        studio = run(for_you.recommendations(db, user_id, "anime"))
        assert studio["rows"][0]["cards"][0]["id"] == str(anime_id)
        assert studio["rows"][0]["cards"][0]["poster_url"] == candidate["poster_url"]
        status = for_you.request_status(db, user_id, "anime", anime_id)
        assert status == studio["requests"][0]
        assert status["state"] == "requested"
        assert run(for_you.submit_request(db, user_id, "anime", anime_id)) == status
        assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM anilist_plannings").fetchone()[0] == 1
    resolved.assert_called_once_with(str(anime_id), season="")
    updated_cards = client.get("/plus/discover/rows?kind=anime")
    assert "Your requests" in updated_cards.text
    assert "Queued for the next scheduled anime media check." in updated_cards.text
    assert 'aria-label="Request Test anime"' not in updated_cards.text


def test_tv_request_is_personal_owner_review_and_never_an_acquisition(client, monkeypatch):
    _signup_and_login(client)
    first = _user_id(client)
    exact = AsyncMock(return_value={"title": "A TV show", "tmdb_id": 123})
    monkeypatch.setattr(for_you.TVService, "_metadata_tv", exact)
    with _connect_client_db(client) as db:
        another = db.execute(
            "INSERT INTO users(email,password_hash) VALUES('second@example.test','test')"
        ).lastrowid
        households.ensure_personal_library(db, user_id=another)
        db.commit()

        initial = run(for_you.submit_request(db, first, "tv", "123"))
        assert initial["state"] == "requested"
        assert (
            initial["detail"] == "Waiting for owner review. TV acquisition requires a manual step."
        )
        assert run(for_you.submit_request(db, first, "tv", 123)) == initial
        assert for_you.request_status(db, another, "tv", 123) is None
        assert db.execute("SELECT count(*) FROM for_you_requests").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 0
    exact.assert_awaited_once_with(123)


def test_movie_request_and_refresh_use_existing_pipeline_and_verification(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)

    class NewlyRequestedMovie(NativeMovies):
        reported_state = "Indexed"

        def movie(self):
            return {**super().movie(), "state": self.reported_state}

    fake = NewlyRequestedMovie()
    monkeypatch.setattr(
        for_you,
        "MovieService",
        lambda db, _connections: service(db, fake, now=lambda: datetime.now(UTC)),
    )
    with _connect_client_db(client) as db:
        movies_store.upsert_movie(
            db,
            user_id,
            {"imdb_id": IMDB, "tmdb_id": TMDB, "title": "The Thing", "year": 1982},
            now="2026-11-14T00:00:00Z",
        )
        status = run(for_you.submit_request(db, user_id, "movie", IMDB))
        assert status["state"] == "requested"
        assert run(for_you.submit_request(db, user_id, "movie", IMDB)) == status
        assert len(fake.posts) == 1
        fake.reported_state = "Downloading"
        changed = run(for_you.refresh_request(db, user_id, "movie", IMDB))
        assert changed["state"] == "preparing"
        fake.reported_state = "Completed"
        verified = run(for_you.refresh_request(db, user_id, "movie", IMDB))
        assert verified["state"] == "ready"
        assert for_you.request_status(db, user_id, "movie", IMDB) == verified
        db.execute("UPDATE movie_verifications SET expires_at='2000-01-01T00:00:00Z'")
        db.commit()
        assert for_you.request_status(db, user_id, "movie", IMDB)["state"] == "preparing"
        assert len(fake.posts) == 1


def test_catalog_search_keeps_other_domains_and_projects_shared_request_state(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setattr(for_you, "shared_catalog_search", search_routes.CatalogSearchBudget())
    anime = AsyncMock(
        return_value=[
            {
                "id": 81001,
                "title": "The Thing",
                "description": "A synthetic catalog match.",
                "catalog_poster_url": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx81001-test.jpg",
            },
            {"id": 81001, "title": "Duplicate title"},
        ]
    )
    tv = AsyncMock(side_effect=for_you.MovieError("rate_limited", retry_after="60"))
    monkeypatch.setattr(for_you.AnimeCatalogSearch, "search", anime)
    monkeypatch.setattr(for_you.TVService, "search", tv)

    fake = NativeMovies()
    fake.xml_override = b'<MediaContainer librarySectionID="6" size="0" totalSize="0"/>'
    monkeypatch.setattr(for_you, "MovieService", lambda db, _connections: service(db, fake))
    with _connect_client_db(client) as db:
        result = run(for_you.search(db, user_id, "  The Thing ", "all"))
        rows = {row["key"]: row["cards"] for row in result["rows"]}
        assert set(rows) == {"anime", "movies"}
        assert len(rows["anime"]) == 1
        assert rows["anime"][0]["id"] == "81001"
        assert rows["anime"][0]["summary"] == "A synthetic catalog match."
        assert rows["anime"][0]["poster_url"].startswith("https://s4.anilist.co/")
        assert rows["anime"][0]["can_request"]
        assert rows["movies"][0]["id"] == IMDB and rows["movies"][0]["can_request"]
        assert result["warnings"][0]["kind"] == "tv"
        assert result["warnings"][0]["retry_after"] == 60
        assert result["requests"] == [] and fake.posts == []
        for table in ("media_monitors", "for_you_requests", "movie_requests"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

        request = run(for_you.submit_request(db, user_id, "movie", IMDB))
        updated = run(for_you.search(db, user_id, "The Thing", "movies"))
        assert len(updated["rows"]) == 1
        assert updated["rows"][0]["cards"][0]["request"] == request
        assert not updated["rows"][0]["cards"][0]["can_request"]
        assert updated["requests"] == [request]
        assert len(fake.posts) == 1

        paused = run(for_you.search(db, user_id, "The Thing", "tv"))
        assert paused["warnings"][0]["code"] == "rate_limited"
        assert paused["rows"] == []

    anime.assert_awaited_once_with("The Thing")
    tv.assert_awaited_once_with(user_id, "The Thing")


def test_catalog_search_validates_before_fanout_and_keeps_the_budget(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setattr(for_you, "shared_catalog_search", search_routes.CatalogSearchBudget())
    monkeypatch.setattr(search_routes, "_SEARCH_RATE_LIMIT", 1)
    providers = AsyncMock(return_value=[])
    monkeypatch.setattr(for_you, "_search_cards", providers)
    with _connect_client_db(client) as db:
        assert run(for_you.search(db, user_id, " a ", "anime"))["rows"] == []
        with pytest.raises(for_you.ForYouError, match="Search with 2 to 120 characters"):
            run(for_you.search(db, user_id, "a" * 121, "anime"))
        with pytest.raises(for_you.ForYouError):
            run(for_you.search(db, user_id, "anime", "unknown"))
        providers.assert_not_called()

        run(for_you.search(db, user_id, "valid search", "anime"))
        with pytest.raises(for_you.ForYouError) as error:
            run(for_you.search(db, user_id, "another query", "all"))
        assert error.value.status == 429 and error.value.retry_after > 0
        providers.assert_awaited_once()


def test_catalog_tv_search_and_request_reuse_exact_read_only_seerr(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setattr(for_you, "shared_catalog_search", search_routes.CatalogSearchBudget())
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        assert request.url.host == "seerr.invalid"
        assert request.method == "GET"
        show = {"id": 123, "name": "Catalog TV", "posterPath": "/demo.jpg", "overview": "A show."}
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json={"results": [{**show, "mediaType": "tv"}]})
        assert request.url.path == "/api/v1/tv/123"
        return httpx.Response(200, json=show)

    tv_service = for_you.TVService

    def connected_service(db, _connections):
        baseline = service(db, NativeMovies())
        return tv_service(db, baseline.connections, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(for_you, "TVService", connected_service)
    with _connect_client_db(client) as db:
        found = run(for_you.search(db, user_id, "Catalog TV", "tv"))
        card = found["rows"][0]["cards"][0]
        assert card["id"] == "123" and card["kind"] == "tv"
        assert card["poster_url"] == "https://image.tmdb.org/t/p/w342/demo.jpg"
        assert card["summary"] == "A show." and card["can_request"]
        assert card["request"] is None
        assert db.execute("SELECT count(*) FROM for_you_requests").fetchone()[0] == 0

        saved = run(for_you.submit_request(db, user_id, "tv", card["id"]))
        assert saved["state"] == "requested"
        assert "Waiting for owner review" in saved["detail"]
        updated = run(for_you.search(db, user_id, "Catalog TV", "tv"))
        assert updated["rows"][0]["cards"][0]["request"] == saved
        assert updated["requests"] == [saved]

    assert requests == [
        ("GET", "/api/v1/search"),
        ("GET", "/api/v1/tv/123"),
        ("GET", "/api/v1/search"),
    ]


def test_verified_library_exits_queue_and_confirmed_removal_keeps_history(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    removed = "tt0450385"
    fake = NativeMovies()
    monkeypatch.setattr(for_you, "MovieService", lambda db, _connections: service(db, fake))
    monkeypatch.setattr(
        discover_hub,
        "screen_shelves",
        AsyncMock(
            return_value=[
                discover_hub._shelf(
                    "movies",
                    "Movies for You",
                    [{"id": IMDB, "title": "The Thing"}, {"id": removed, "title": "1408"}],
                )
            ]
        ),
    )
    at = "2026-10-27T14:00:00Z"
    with _connect_client_db(client) as db:
        for imdb_id, tmdb_id, title, code in [
            (IMDB, TMDB, "The Thing", "acknowledgement_unconfirmed"),
            (removed, 3021, "1408", "copy_removed"),
        ]:
            movies_store.upsert_movie(
                db,
                user_id,
                {"imdb_id": imdb_id, "tmdb_id": tmdb_id, "title": title, "year": 1982},
                now=at,
            )
            movies_store.ensure_request(db, user_id, imdb_id, now=at, edition=None)
            movies_store.update_request(
                db,
                user_id,
                imdb_id,
                now=at,
                code=code,
                state="unavailable" if imdb_id == removed else "deferred",
                detail="Riven persistence diagnostic text",
            )
        db.execute(
            """INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,created_at,updated_at)
               VALUES(?,?,3021,'completed','manual',?,?)""",
            (user_id, removed, at, at),
        )
        db.commit()
        initial = for_you.request_status(db, user_id, "movie", IMDB)
        assert initial["state"] == "requested"
        assert [step["key"] for step in initial["timeline"]] == ["requested", "received"]
        assert initial["timeline"][1]["label"] == "Riven acknowledged the request"
        assert "persistence" not in initial["detail"]

        page = run(for_you.recommendations(db, user_id, "movies"))
        assert page["requests"] == []
        cards = {card["id"]: card for card in page["rows"][0]["cards"]}
        present = cards[IMDB]["request"]
        assert present["state"] == "in_library" and present["label"] == "In your library"
        assert not cards[IMDB]["can_request"]
        assert [step["key"] for step in present["timeline"]] == [
            "requested",
            "received",
            "in_library",
        ]
        assert cards[removed]["request"] is None

        movie_service = service(db, fake)
        detail = next(card for card in run(movie_service.list(user_id)) if card["imdb_id"] == IMDB)
        assert detail["plex_present"] is True
        detail_status = for_you.request_status(db, user_id, "movie", IMDB, movie_card=detail)
        assert detail_status == present
        assert for_you.request_status(db, user_id, "movie", removed) is None
        assert movies_store.get_request(db, user_id, removed)["code"] == "copy_removed"
        assert (
            db.execute("SELECT status FROM movie_history WHERE imdb_id=?", (removed,)).fetchone()[0]
            == "completed"
        )
        assert fake.posts == []

        # A similarly titled movie with another exact GUID cannot finish this request.
        fake.plex_guid = removed
        changed = run(for_you.recommendations(db, user_id, "movies"))
        assert changed["requests"][0]["state"] == "requested"
