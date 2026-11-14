"""Shared For You intake and request proof, with offline catalog/provider fixtures."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

from omakase.plus import discover_hub, for_you, households, movies_store
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
