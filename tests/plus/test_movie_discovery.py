"""Seerr public metadata is cached; taste ranking remains strictly local."""

from __future__ import annotations

import importlib
import importlib.util
import json
from datetime import timedelta

import httpx
import pytest

from tests.plus.test_movies import IMDB, NOW, TMDB, NativeMovies, run, service
from tests.plus.test_movies import db as db


class PublicDiscovery(NativeMovies):
    def __init__(self):
        super().__init__()
        self.detail["genres"] = [{"id": 27, "name": "Horror"}]
        self.genres = [
            {"id": 27, "name": "Horror"},
            {"id": 35, "name": "Comedy"},
            {"id": 878, "name": "Science Fiction"},
        ]
        self.rows = [
            {
                "id": 7001,
                "mediaType": "movie",
                "title": "Comedy candidate",
                "releaseDate": "2000-01-01",
                "genreIds": [35],
                "popularity": 90,
                "voteAverage": 7.2,
            },
            {
                "id": 7002,
                "mediaType": "movie",
                "title": "Horror candidate",
                "releaseDate": "2001-01-01",
                "genreIds": [27],
                "popularity": 10,
                "voteAverage": 7.1,
            },
        ]
        self.discovery_status = 200

    def __call__(self, request):
        path = request.url.path
        if request.url.host == "seerr.invalid" and path in {
            "/api/v1/genres/movie",
            "/api/v1/discover/movies",
        }:
            self.calls.append(request)
            assert request.headers["X-Api-Key"] == "seerr-secret"
            if path == "/api/v1/genres/movie":
                return httpx.Response(200, json=self.genres)
            return httpx.Response(
                self.discovery_status,
                json={"page": 1, "totalPages": 1, "results": self.rows},
                headers={"Retry-After": "120"},
            )
        if path in {"/api/v1/movie/7001", "/api/v1/movie/7002"}:
            self.calls.append(request)
            index = int(path.rsplit("/", 1)[1])
            row = next(item for item in self.rows if item["id"] == index)
            return httpx.Response(
                200,
                json={
                    "id": index,
                    "imdbId": f"tt{9_000_000 + index}",
                    "title": row["title"],
                    "releaseDate": row["releaseDate"],
                    "genres": [genre for genre in self.genres if genre["id"] in row["genreIds"]],
                    "posterPath": "/poster.jpg",
                    "backdropPath": "/backdrop.jpg",
                    "overview": "Public synopsis.",
                    "mediaInfo": {
                        "requestedBy": {"email": "private@example.invalid"},
                        "userRating": 10,
                    },
                    "apiKey": "seerr-secret",
                },
            )
        return super().__call__(request)


def discovery_service(db, fake=None, *, now=None):
    assert importlib.util.find_spec("omakase.plus.movie_discovery") is not None, (
        "Movie discovery is missing"
    )
    module = importlib.import_module("omakase.plus.movie_discovery")
    fake = fake or PublicDiscovery()
    movies = service(db, fake, now=now)
    return module.MovieDiscoveryService(movies), movies, fake


def history(movies):
    return importlib.import_module("omakase.plus.movie_history").MovieHistoryService(movies)


def test_options_return_actual_genres_and_supported_filter_choices(db):
    discovery, movies, fake = discovery_service(db)
    options = run(discovery.options(1))
    assert options["genres"] == fake.genres
    assert {row["value"] for row in options["sorts"]} == {
        "personalized",
        "popular",
        "top_rated",
        "newest",
    }
    assert options["exclude_completed_default"] is True
    assert (
        options["rating_min"] == 1 and options["rating_max"] == 10 and options["rating_step"] == 0.5
    )
    run(discovery.options(2))
    assert len(fake.calls) == 1


def test_saved_ratings_meaningfully_change_local_candidate_order_without_new_external_calls(db):
    discovery, movies, fake = discovery_service(db)
    before = run(discovery.discover(1))
    assert before[0]["tmdb_id"] == 7001
    h = history(movies)
    run(h.save(1, imdb_id=IMDB, tmdb_id=TMDB, status="completed", rating=9.5))
    calls = len(fake.calls)
    after = run(discovery.discover(1))
    assert after[0]["tmdb_id"] == 7002
    assert "Horror" in after[0]["recommendation_reason"]
    assert after[0]["ranking_basis"] == "saved_ratings"
    assert len(fake.calls) == calls
    run(h.save(1, imdb_id=IMDB, tmdb_id=TMDB, rating=1))
    assert run(discovery.discover(1))[0]["tmdb_id"] == 7001
    assert len(fake.calls) == calls
    assert not any(
        word in json.dumps(after)
        for word in ("fit_percent", "match_percent", "private@example", "secret")
    )
    for call in fake.calls:
        assert call.method == "GET" and call.url.host == "seerr.invalid"
        assert not any(
            key in call.url.params for key in ("user_id", "rating", "ratings", "history", "taste")
        )


def test_completed_history_is_excluded_by_default_and_can_be_requested_explicitly(db):
    discovery, movies, fake = discovery_service(db)
    cards = run(discovery.discover(1))
    row = next(card for card in cards if card["tmdb_id"] == 7002)
    run(history(movies).save(1, imdb_id=row["imdb_id"], tmdb_id=7002, status="completed", rating=9))
    assert all(card["tmdb_id"] != 7002 for card in run(discovery.discover(1)))
    included = run(discovery.discover(1, include_completed=True))
    assert next(card for card in included if card["tmdb_id"] == 7002)["watch_status"] == "completed"
    assert len(run(discovery.discover(2))) == 2


def test_genre_filter_is_sent_using_seerr_schema_and_checked_locally(db):
    discovery, movies, fake = discovery_service(db)
    cards = run(discovery.discover(1, genre_id=27, sort="popular", year_from=1990, year_to=2005))
    assert [card["tmdb_id"] for card in cards] == [7002]
    request = next(call for call in fake.calls if call.url.path == "/api/v1/discover/movies")
    assert request.url.params["genre"] == "27"
    assert request.url.params["sortBy"] == "popularity.desc"
    assert request.url.params["primaryReleaseDateGte"] == "1990-01-01"
    assert request.url.params["primaryReleaseDateLte"] == "2005-12-31"
    assert request.url.params["page"] == "1"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"genre_id": "27&token=bad"},
        {"sort": "http://evil.invalid"},
        {"page": 0},
        {"page": 4},
        {"limit": 1000},
        {"include_completed": "yes"},
        {"year_from": 2005, "year_to": 2001},
        {"year_from": True},
    ],
)
def test_invalid_filters_are_rejected_before_network(db, kwargs):
    discovery, movies, fake = discovery_service(db)
    with pytest.raises(ValueError):
        run(discovery.discover(1, **kwargs))
    assert fake.calls == []


def test_cached_candidate_rows_contain_public_metadata_only(db):
    discovery, movies, fake = discovery_service(db)
    fake.rows[0]["mediaInfo"] = {"user": "private@example.invalid", "token": "seerr-secret"}
    run(discovery.discover(1))
    cached = json.dumps([dict(row) for row in db.execute("SELECT * FROM movie_discovery_cache")])
    assert not any(
        word in cached for word in ("private@example", "seerr-secret", "requestedBy", "mediaInfo")
    )
    assert fake.probes == [] and fake.posts == []


def test_public_metadata_cache_expires_without_reusing_personal_order(db):
    discovery, movies, fake = discovery_service(db)
    run(discovery.discover(1))
    before = len([call for call in fake.calls if call.url.path == "/api/v1/discover/movies"])
    later, _, _ = discovery_service(db, fake, now=lambda: NOW + timedelta(hours=1))
    run(later.discover(1))
    assert (
        len([call for call in fake.calls if call.url.path == "/api/v1/discover/movies"])
        == before + 1
    )


def test_rate_limit_keeps_retry_after_and_does_not_repeat_during_cooldown(db):
    discovery, movies, fake = discovery_service(db)
    fake.discovery_status = 429
    module = importlib.import_module("omakase.plus.movies")
    with pytest.raises(module.MovieError) as error:
        run(discovery.discover(1))
    assert error.value.code == "rate_limited" and error.value.retry_after == "120"
    calls = len(fake.calls)
    with pytest.raises(module.MovieError):
        run(discovery.discover(1))
    assert len(fake.calls) == calls


def test_genres_and_ratings_are_per_user_but_candidates_are_public_cache(db):
    discovery, movies, fake = discovery_service(db)
    h = history(movies)
    run(h.save(1, imdb_id=IMDB, tmdb_id=TMDB, rating=10))
    run(h.save(2, imdb_id=IMDB, tmdb_id=TMDB, rating=1))
    assert run(discovery.discover(1))[0]["tmdb_id"] == 7002
    calls = len(fake.calls)
    assert run(discovery.discover(2))[0]["tmdb_id"] == 7001
    assert len(fake.calls) == calls
