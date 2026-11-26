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
        assert initial["detail"] == "Saved for review."
        assert run(for_you.submit_request(db, first, "tv", 123)) == initial
        assert for_you.request_status(db, another, "tv", 123) is None
        assert db.execute("SELECT count(*) FROM for_you_requests").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 0
    exact.assert_awaited_once_with(123)


def test_tv_intake_submits_once_and_reads_provider_episode_progress(client, monkeypatch):
    from omakase.plus.movies import MovieError

    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setenv("OMAKASE_TV_REQUEST_RIVEN", "1")
    metadata = {
        "title": "A TV show",
        "tmdb_id": 123,
        "imdb_id": "tt0000123",
        "seasons": [
            {"season_number": 1, "released": True, "episode_count": 2},
            {"season_number": 2, "released": False, "episode_count": 1},
        ],
    }
    provider_show = {
        "tmdb_id": "123",
        "imdb_id": "tt0000123",
        "type": "Show",
        "state": "PartiallyCompleted",
        "seasons": [
            {
                "number": 1,
                "episodes": [
                    {"number": 1, "state": "Completed"},
                    {"number": 2, "state": "Downloading"},
                ],
            },
            {"number": 2, "episodes": [{"number": 1, "state": "Indexed"}]},
        ],
    }
    check = AsyncMock(side_effect=[MovieError("invalid_input", http_status=404), provider_show])
    add = AsyncMock(return_value=b'{"message":"Added 1 item(s) to the queue"}')
    monkeypatch.setattr(for_you.TVService, "_metadata_tv", AsyncMock(return_value=metadata))
    monkeypatch.setattr(for_you.TVService, "_json", check)
    monkeypatch.setattr(for_you.TVService, "_bytes", add)
    with _connect_client_db(client) as db:
        initial = run(for_you.submit_request(db, user_id, "tv", 123))
        assert initial["status_label"] == "Queued in Riven"
        assert initial["tv_provider"]["seasons"] == [1]
        assert run(for_you.submit_request(db, user_id, "tv", 123)) == initial
        observation = run(for_you.refresh_request(db, user_id, "tv", 123))
        assert observation["resolved"] is False
        assert observation["tv_provider"]["snapshot"]["seasons"] == [
            {
                "season": 1,
                "episodes": [
                    {"episode": 1, "status": "Sent to the library"},
                    {"episode": 2, "status": "Transferring"},
                ],
            }
        ]
        assert db.execute("SELECT count(*) FROM tv_provider_requests").fetchone()[0] == 1
    add.assert_awaited_once_with(
        "riven", "POST", "/api/v1/items/add", params={"imdb_ids": "tt0000123"}
    )


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
        assert saved["detail"] == "Saved for review."
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


def test_trailer_parser_rejects_unsafe_inputs_and_extracts_youtube():
    from omakase.plus.trailers import (
        extract_anilist_trailer,
        extract_seerr_trailer,
        youtube_trailer,
    )

    assert youtube_trailer("dQw4w9WgXcQ") == {
        "site": "youtube",
        "id": "dQw4w9WgXcQ",
        "embed_url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0",
        "watch_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    }
    assert youtube_trailer("bad<script>") is None
    assert youtube_trailer("short") is None
    assert extract_anilist_trailer({"site": "vimeo", "id": "dQw4w9WgXcQ"}) is None
    assert extract_anilist_trailer({"site": "youtube", "id": "dQw4w9WgXcQ"})["id"] == "dQw4w9WgXcQ"
    assert (
        extract_seerr_trailer(
            [
                {"site": "YouTube", "type": "Teaser", "key": "aaaaaaaaaaa"},
                {"site": "YouTube", "type": "Trailer", "key": "bbbbbbbbbbb"},
            ]
        )["id"]
        == "bbbbbbbbbbb"
    )
    assert extract_seerr_trailer([{"url": "https://youtu.be/ccccccccccc"}])["id"] == "ccccccccccc"


def test_for_you_csp_allows_nocookie_trailer_frames():
    from omakase.plus.web_security import plus_content_security_policy

    policy = plus_content_security_policy()
    assert "frame-src https://www.youtube-nocookie.com;" in policy


def test_for_you_header_edit_layout_and_refresh_settings(client):
    from datetime import UTC, datetime, timedelta

    from omakase.plus import for_you_layout

    _signup_and_login(client)
    user_id = _user_id(client)
    csrf = _csrf_token(client)

    for kind in ("anime", "movies", "tv"):
        html = client.get(f"/plus/discover?kind={kind}").text
        assert "Edit For You" in html
        assert "AI Picks" in html
        assert "Find anime" not in html
        assert "Filter movies" not in html
        assert 'action="/plus/discover/layout"' in html

    res = client.post(
        "/plus/discover/layout",
        data={
            "csrf_token": csrf,
            "kind": "movies",
            "rows": "hidden_gems,top,because_you_liked,new_releases",
            "hidden": ["new_releases"],
        },
        follow_redirects=False,
    )
    assert res.status_code == 303
    assert "/plus/discover?kind=movies" in res.headers["location"]
    with _connect_client_db(client) as db:
        saved = for_you_layout.load_layout(db, user_id, "movies")["rows"]
        assert [row["id"] for row in saved][:2] == ["hidden_gems", "top"]
        assert next(row for row in saved if row["id"] == "new_releases")["hidden"] is True

    sched = client.post(
        "/plus/discover/refresh-settings",
        data={"csrf_token": csrf, "kind": "anime", "cadence": "on_new_data"},
        follow_redirects=False,
    )
    assert sched.status_code == 303
    with _connect_client_db(client) as db:
        assert for_you_layout.is_ai_refresh_due(db, user_id) is True
        now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
        for_you_layout.mark_ai_refreshed(db, user_id, now=now)
        assert for_you_layout.is_ai_refresh_due(db, user_id, now=now) is False
        db.execute(
            "INSERT INTO recommendation_feedback(user_id,source,media_id,title,feedback_type) "
            "VALUES (?,'anilist',9001,'New rating signal','not_for_me')",
            (user_id,),
        )
        db.commit()
        assert for_you_layout.is_ai_refresh_due(db, user_id, now=now) is True
        for_you_layout.save_refresh_settings(
            db, user_id, auto_ai_refresh=True, cadence="weekly", now=now
        )
        assert for_you_layout.is_ai_refresh_due(db, user_id, now=now + timedelta(days=2)) is False
        assert for_you_layout.is_ai_refresh_due(db, user_id, now=now + timedelta(days=8)) is True


def test_movie_and_tv_feedback_actions_save_and_exclude_locally(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    csrf = _csrf_token(client)
    monkeypatch.setattr(discover_hub, "_screen_rows", discover_hub._screen_rows.__class__())

    res_tv = client.post(
        "/plus/api/feedback",
        json={
            "feedback_type": "already_watched",
            "source": "tv",
            "media_id": 555,
            "title": "Seen Show",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res_tv.status_code == 200
    assert res_tv.json()["status"] == "ok"
    assert res_tv.json()["tracker_status"] == "local_only"

    res_movie = client.post(
        "/plus/api/feedback",
        json={
            "feedback_type": "not_for_me",
            "source": "movie",
            "media_id": 777,
            "title": "Skip Movie",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res_movie.status_code == 200 and res_movie.json()["status"] == "ok"
    with _connect_client_db(client) as db:
        fb_rows = db.execute(
            "SELECT source, media_id FROM recommendation_feedback WHERE user_id=? ORDER BY id",
            (user_id,),
        ).fetchall()
        assert [(r["source"], r["media_id"]) for r in fb_rows] == [("tv", 555), ("movie", 777)]

    movie_card = discover_hub._project_movie_card(
        {
            "imdb_id": "tt0000999",
            "tmdb_id": 999,
            "title": "Keep Movie",
            "year": 2025,
            "vote_average": 8.4,
            "trailer": {"site": "youtube", "id": "dQw4w9WgXcQ"},
            "state": "verifying",
        }
    )
    monkeypatch.setattr(
        discover_hub,
        "screen_shelves",
        AsyncMock(
            return_value=[
                discover_hub._shelf("top", "Top Picks for You", [movie_card], kind="movie")
            ]
        ),
    )
    monkeypatch.setattr(for_you, "_movie_library", AsyncMock(return_value={}))
    rows_html = client.get("/plus/discover/rows?kind=movies").text
    assert "Keep Movie" in rows_html
    assert "84% TMDB" in rows_html
    assert 'data-trailer-id="dQw4w9WgXcQ"' in rows_html
    assert 'data-hub-source="movie"' in rows_html
    assert 'data-feedback="not_for_me"' in rows_html
    assert 'data-feedback="already_watched"' in rows_html
    assert "from=discover" in rows_html


def test_catalog_anime_uses_unified_hero_with_trailer_and_discover_back(client, monkeypatch):
    _signup_and_login(client)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    detail = {
        "id": 9101,
        "anilist_id": 9101,
        "title": "Unified Hero Show",
        "year": 2025,
        "format": "TV",
        "episode_total": 12,
        "status": "FINISHED",
        "genres": ["Action", "Drama"],
        "description": "Clean unified detail test.",
        "studio": "Test Studio",
        "trailer": {
            "site": "youtube",
            "id": "dQw4w9WgXcQ",
            "embed_url": "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?autoplay=1&rel=0",
            "watch_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        },
        "poster_url": "/plus/media/9101/poster",
        "url": "/plus/catalog/anime/9101",
        "watch_status": "unknown",
        "progress": None,
        "rating": None,
        "library_id": None,
        "library_status": None,
    }
    monkeypatch.setattr(
        search_routes.AnimeCatalogSearch,
        "detail",
        AsyncMock(return_value=detail),
    )
    html = client.get("/plus/catalog/anime/9101?from=discover").text
    assert "series-detail.css" in html
    assert "series-hero" in html
    assert "For You" in html
    assert "data-trailer-open" in html and 'data-trailer-id="dQw4w9WgXcQ"' in html
    assert 'id="trailer-modal"' in html
    assert 'class="movie-poster" src="/plus/media/9101/poster"' in html
    assert "data-catalog-track-form" in html
    assert "data-detail-dismiss" in html


def test_tv_discover_seed_from_tv_catalog_metadata_json(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        db.execute(
            "INSERT INTO tv_catalog (user_id, tmdb_id, metadata_json, updated_at) VALUES (?, 1396, ?, '2026-11-20T00:00:00Z')",
            (
                user_id,
                '{"tmdb_id":1396,"title":"Breaking Bad","year":2008,"genres":[{"id":18,"name":"Drama"}]}',
            ),
        )
        db.execute(
            "INSERT INTO tv_history (user_id, tmdb_id, status, rating, status_source, rating_source, updated_at) "
            "VALUES (?, 1396, 'completed', 9.0, 'manual', 'manual', '2026-11-20T00:00:00Z')",
            (user_id,),
        )
        db.commit()
    discover_hub._screen_rows.clear()

    async def fake_json(_self, _service, path, *, params=None):
        del _self, _service, params
        results = [
            {
                "id": 6000 + i,
                "name": f"Similar Show {i}",
                "posterPath": f"/show{i}.jpg",
                "firstAirDate": "2024-01-01",
                "voteAverage": 8.2,
                "genreIds": [18],
                "overview": "Critically acclaimed crime drama.",
            }
            for i in range(4)
        ]
        return {"results": results}

    monkeypatch.setattr(discover_hub.MovieService, "_json", fake_json)
    from omakase.plus.movies import MovieConnections

    monkeypatch.setattr(
        discover_hub,
        "movie_connections",
        lambda _db, _uid: MovieConnections(
            enabled=True,
            riven_url="http://riven.local",
            riven_api_key="riven-key",
            seerr_url="http://seerr.local",
            seerr_api_key="seerr-key",
            plex_url="http://plex.local",
            plex_token="plex-token",
        ),
    )
    res = client.get("/plus/discover/rows?kind=tv")
    assert res.status_code == 200
    assert "Because You Liked Breaking Bad" in res.text
    assert "Similar Show 0" in res.text


def test_movie_because_you_liked_persists_catalog_for_artwork(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    with _connect_client_db(client) as db:
        db.execute(
            "INSERT INTO movie_catalog (user_id, imdb_id, tmdb_id, title, year, identity_verified, updated_at) "
            "VALUES (?, 'tt0450385', 3021, '1408', 2007, 1, '2026-11-20T00:00:00Z')",
            (user_id,),
        )
        db.execute(
            "INSERT INTO movie_history (user_id, imdb_id, tmdb_id, status, rating, status_source, rating_source, created_at, updated_at) "
            "VALUES (?, 'tt0450385', 3021, 'completed', 8.0, 'manual', 'manual', '2026-11-20T00:00:00Z', '2026-11-20T00:00:00Z')",
            (user_id,),
        )
        db.commit()
    discover_hub._screen_rows.clear()

    async def fake_discover(_self, _uid, *, sort="personalized", limit=15):
        del _self, _uid, sort, limit
        return []

    async def fake_json(_self, _service, path, *, params=None):
        del _self, _service, params
        if "/recommendations" in path:
            return {
                "results": [
                    {"id": 9501 + i, "title": f"Rec Movie {i}", "voteAverage": 7.5}
                    for i in range(4)
                ]
            }
        return {"results": []}

    async def fake_metadata(_self, tmdb_id, imdb_id=None):
        del _self, imdb_id
        return {
            "imdb_id": f"tt9501{tmdb_id}",
            "tmdb_id": tmdb_id,
            "title": f"Rec Movie {tmdb_id - 9501}",
            "original_title": None,
            "year": 2020,
            "poster_url": f"https://image.tmdb.org/t/p/w342/rec{tmdb_id}.jpg",
            "backdrop_url": f"https://image.tmdb.org/t/p/w780/bg{tmdb_id}.jpg",
            "overview": "Psychological thriller.",
            "genres": [{"id": 27, "name": "Horror"}],
        }

    monkeypatch.setattr(discover_hub.MovieDiscoveryService, "discover", fake_discover)
    monkeypatch.setattr(discover_hub.MovieService, "_json", fake_json)
    monkeypatch.setattr(discover_hub.MovieService, "_metadata", fake_metadata)
    from omakase.plus.movies import MovieConnections

    monkeypatch.setattr(
        discover_hub,
        "movie_connections",
        lambda _db, _uid: MovieConnections(
            enabled=True,
            riven_url="http://riven.local",
            riven_api_key="riven-key",
            seerr_url="http://seerr.local",
            seerr_api_key="seerr-key",
            plex_url="http://plex.local",
            plex_token="plex-token",
        ),
    )
    res = client.get("/plus/discover/rows?kind=movies")
    assert res.status_code == 200
    assert "Because You Liked 1408" in res.text
    with _connect_client_db(client) as db:
        row = db.execute(
            "SELECT imdb_id, poster_url FROM movie_catalog WHERE user_id=? AND tmdb_id=9501",
            (user_id,),
        ).fetchone()
        assert row is not None and row["poster_url"] is not None


def test_discover_track_api_anime_movie_tv_and_dismiss_without_downloads(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    csrf = _csrf_token(client)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")

    from omakase.plus import routes as plus_routes
    from omakase.plus.secrets import store_secret

    with _connect_client_db(client) as db:
        store_secret(db, user_id, "anilist_oauth_token", "tok-secret")
        db.commit()

    synced_calls = []

    class DummyLease:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class DummyTokenCtx:
        def __enter__(self):
            return "tok-secret"

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(plus_routes, "writer_lease", lambda *_a, **_k: DummyLease())
    monkeypatch.setattr(plus_routes, "anilist_credential_fingerprint", lambda *_a: "fp-1")
    monkeypatch.setattr(plus_routes, "require_anilist_credential", lambda *_a: None)
    monkeypatch.setattr(plus_routes, "require_writer_lease", lambda *_a: None)
    monkeypatch.setattr(plus_routes, "with_valid_token", lambda *_a, **_k: DummyTokenCtx())

    def fake_save_entry(token, anilist_id, *, status, progress=None, score_ten=None):
        synced_calls.append((token, anilist_id, status, progress, score_ten))
        return {
            "id": 55,
            "mediaId": anilist_id,
            "status": status,
            "progress": progress,
            "score": score_ten,
        }

    monkeypatch.setattr(plus_routes, "save_media_list_entry", fake_save_entry)

    anime_res = client.post(
        "/plus/api/discover/track",
        json={
            "kind": "anime",
            "media_id": 92001,
            "title": "Tracked Discovery Anime",
            "status": "watching",
            "progress": 5,
            "episode_total": 12,
            "score": 8.5,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert anime_res.status_code == 200
    body = anime_res.json()
    assert body["status"] == "ok"
    assert body["tracker_status"] == "synced"
    assert body["remove_from_for_you"] is True
    assert synced_calls == [("tok-secret", 92001, "CURRENT", 5, 8.5)]

    with _connect_client_db(client) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM media_monitors WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            == 0
        )
        entry = db.execute(
            "SELECT monitor_id, status, progress_episodes FROM watch_entries WHERE user_id=? AND anilist_id=92001",
            (user_id,),
        ).fetchone()
        assert entry["monitor_id"] is None
        assert entry["status"] == "watching"
        assert entry["progress_episodes"] == 5

    # Tracked anime must now be excluded from anime For You shelves.
    candidate = discover_hub._anime_card(92001, "Tracked Discovery Anime") | {"genres": ("Action",)}
    unseen = discover_hub._anime_card(92002, "Fresh Pick") | {"genres": ("Action",)}
    monkeypatch.setattr(discover_hub, "anime_catalog", AsyncMock(return_value=(candidate, unseen)))
    monkeypatch.setattr(discover_hub, "tracker_watched_ids", AsyncMock(return_value=frozenset()))
    rows_html = client.get("/plus/discover/rows?kind=anime").text
    assert "Fresh Pick" in rows_html
    assert "Tracked Discovery Anime" not in rows_html

    # Movie and TV tracking save cleanly to local history.
    from omakase.plus.movie_history import MovieHistoryService
    from omakase.plus.tv_service import TVService

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

    movie_res = client.post(
        "/plus/api/discover/track",
        json={
            "kind": "movie",
            "media_id": 500,
            "imdb_id": "tt0100500",
            "title": "Track Movie",
            "status": "completed",
            "score": 9,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert movie_res.status_code == 200 and movie_res.json()["remove_from_for_you"] is True
    assert saved_movies == [(user_id, "tt0100500", 500, "completed", 9.0)]

    tv_res = client.post(
        "/plus/api/discover/track",
        json={
            "kind": "tv",
            "media_id": 600,
            "title": "Track TV",
            "status": "completed",
            "score": 8.5,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert tv_res.status_code == 200 and tv_res.json()["remove_from_for_you"] is True
    assert saved_tv == [(user_id, 600, "completed", 8.5)]

    dismiss_res = client.post(
        "/plus/api/discover/track",
        json={"kind": "tv", "media_id": 601, "title": "Hidden Show", "status": "not_for_me"},
        headers={"X-CSRF-Token": csrf},
    )
    assert dismiss_res.status_code == 200 and dismiss_res.json()["remove_from_for_you"] is True


def test_ai_recommendations_layout_provider_logos_and_clean_autocomplete(client):
    _signup_and_login(client)
    rec_html = client.get("/plus/discover?mode=recommend").text
    assert 'class="ai-provider-strip"' in rec_html
    assert 'class="ai-provider-logo"' in rec_html
    assert 'class="discovery-controls-primary"' in rec_html
    assert 'class="discovery-controls-aside"' in rec_html
    add_html = client.get("/plus/discover?mode=add").text
    assert 'name="query"' in add_html and 'autocomplete="off"' in add_html
