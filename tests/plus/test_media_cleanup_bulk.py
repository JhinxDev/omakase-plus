"""Tests for unified Watched tab UX and Real-Debrid cleanup across Anime, Movies, and TV."""

from __future__ import annotations

from unittest.mock import AsyncMock

from omakase.plus import media_cleanup_bulk
from omakase.plus.debrid_library import DebridLibrary
from omakase.plus.providers import playback as playback_providers
from omakase.plus.secrets import store_secret
from tests.plus.test_dashboard import (
    _connect_client_db,
    _csrf_token,
    _signup_and_login,
    _user_id,
)
from tests.plus.test_dashboard import client as client


def test_watched_tabs_share_clean_selection_ui_without_poster_overlay(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")

    with _connect_client_db(client) as db:
        store_secret(db, user_id, "realdebrid_api_key", "rd-token-test")
        assert playback_providers.select(db, user_id).cloud_route is True
        db.execute(
            "INSERT INTO movie_catalog (user_id, imdb_id, tmdb_id, title, year, poster_url, identity_verified, updated_at) "
            "VALUES (?, 'tt1375666', 27205, 'Inception', 2010, 'https://image.tmdb.org/t/p/w342/in.jpg', 1, '2026-11-26T00:00:00Z')",
            (user_id,),
        )
        db.execute(
            "INSERT INTO movie_history (user_id, imdb_id, tmdb_id, status, rating, status_source, rating_source, created_at, updated_at) "
            "VALUES (?, 'tt1375666', 27205, 'completed', 9.0, 'manual', 'manual', '2026-11-26T00:00:00Z', '2026-11-26T00:00:00Z')",
            (user_id,),
        )
        db.execute(
            "INSERT INTO tv_catalog (user_id, tmdb_id, metadata_json, updated_at) VALUES (?, 1396, ?, '2026-11-26T00:00:00Z')",
            (
                user_id,
                '{"tmdb_id":1396,"title":"Breaking Bad","year":2008,"in_collection":true,"seasons":[{"season_number":1,"name":"Season 1","episodes":[]}]}',
            ),
        )
        db.execute(
            "INSERT INTO tv_history (user_id, tmdb_id, status, rating, status_source, rating_source, updated_at) "
            "VALUES (?, 1396, 'completed', 9.5, 'manual', 'manual', '2026-11-26T00:00:00Z')",
            (user_id,),
        )
        db.commit()

    from omakase.plus import movies_routes, tv_routes
    from omakase.plus.movies import MovieConnections

    monkeypatch.setattr(
        movies_routes,
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
    monkeypatch.setattr(movies_routes, "movie_ids", AsyncMock(return_value={"tt1375666"}))
    monkeypatch.setattr(tv_routes, "tv_ids", AsyncMock(return_value={1396}))

    movies_html = client.get("/plus/movies?view=completed").text
    assert "Watched movies" in movies_html
    assert "Clean up selected media" in movies_html
    assert 'class="cleanup-select-pill"' in movies_html
    assert "data-cleanup-select-all" in movies_html
    assert "completed-title-selector" not in movies_html

    tv_html = client.get("/plus/tv?view=completed").text
    assert "Watched TV shows" in tv_html
    assert "Explore TV shows" in tv_html
    assert "Clean up selected media" in tv_html
    assert 'class="cleanup-select-pill"' in tv_html
    assert "data-cleanup-select-all" in tv_html


def test_movie_and_tv_completed_cleanup_preview_and_execute_preserve_history(client, monkeypatch):
    _signup_and_login(client)
    user_id = _user_id(client)
    csrf = _csrf_token(client)
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")

    with _connect_client_db(client) as db:
        store_secret(db, user_id, "realdebrid_api_key", "rd-token-test")
        assert playback_providers.select(db, user_id).cloud_route is True
        db.execute(
            "INSERT INTO movie_catalog (user_id, imdb_id, tmdb_id, title, year, identity_verified, updated_at) "
            "VALUES (?, 'tt1375666', 27205, 'Inception', 2010, 1, '2026-11-26T00:00:00Z')",
            (user_id,),
        )
        db.execute(
            "INSERT INTO movie_history (user_id, imdb_id, tmdb_id, status, rating, status_source, rating_source, created_at, updated_at) "
            "VALUES (?, 'tt1375666', 27205, 'completed', 9.0, 'manual', 'manual', '2026-11-26T00:00:00Z', '2026-11-26T00:00:00Z')",
            (user_id,),
        )
        db.execute(
            "INSERT INTO tv_catalog (user_id, tmdb_id, metadata_json, updated_at) VALUES (?, 1396, ?, '2026-11-26T00:00:00Z')",
            (
                user_id,
                '{"tmdb_id":1396,"title":"Breaking Bad","year":2008,"in_collection":true,"seasons":[{"season_number":1,"name":"Season 1","episodes":[]}]}',
            ),
        )
        db.execute(
            "INSERT INTO tv_history (user_id, tmdb_id, status, rating, status_source, rating_source, updated_at) "
            "VALUES (?, 1396, 'completed', 9.5, 'manual', 'manual', '2026-11-26T00:00:00Z')",
            (user_id,),
        )
        db.commit()

    torrents = {
        "MOV123": {
            "id": "MOV123",
            "filename": "Inception.2010.2160p.BluRay.mkv",
            "status": "downloaded",
            "bytes": 12_000_000_000,
        },
        "TV456": {
            "id": "TV456",
            "filename": "Breaking.Bad.S01.1080p.BluRay",
            "status": "downloaded",
            "bytes": 24_000_000_000,
        },
    }
    monkeypatch.setattr(DebridLibrary, "fresh_torrents", AsyncMock(return_value=torrents))
    deleted = []

    class DummyRD:
        def __init__(self, token):
            self.token = token

        async def delete_torrent(self, torrent_id, *, require_exact_id=False):
            deleted.append((torrent_id, require_exact_id))
            return True

    monkeypatch.setattr(media_cleanup_bulk, "RealDebridClient", DummyRD)

    movie_preview = client.post(
        "/plus/movies/completed/cleanup/preview",
        data={"csrf_token": csrf, "item_id": "tt1375666"},
    )
    assert movie_preview.status_code == 200
    assert "Inception.2010.2160p.BluRay.mkv" in movie_preview.text
    assert "Back to watched movies" in movie_preview.text

    movie_exec = client.post(
        "/plus/movies/completed/cleanup/execute",
        data={"csrf_token": csrf, "item_id": "tt1375666"},
        follow_redirects=False,
    )
    assert movie_exec.status_code == 303
    assert "notice=cleaned" in movie_exec.headers["location"]

    tv_preview = client.post(
        "/plus/tv/completed/cleanup/preview",
        data={"csrf_token": csrf, "item_id": "1396"},
    )
    assert tv_preview.status_code == 200
    assert "Breaking.Bad.S01.1080p.BluRay" in tv_preview.text
    assert "Back to watched TV shows" in tv_preview.text

    tv_exec = client.post(
        "/plus/tv/completed/cleanup/execute",
        data={"csrf_token": csrf, "item_id": "1396"},
        follow_redirects=False,
    )
    assert tv_exec.status_code == 303
    assert "notice=cleaned" in tv_exec.headers["location"]

    assert deleted == [("MOV123", True), ("TV456", True)]
    with _connect_client_db(client) as db:
        assert (
            db.execute(
                "SELECT rating FROM movie_history WHERE user_id=? AND imdb_id='tt1375666'",
                (user_id,),
            ).fetchone()["rating"]
            == 9.0
        )
        assert (
            db.execute(
                "SELECT rating FROM tv_history WHERE user_id=? AND tmdb_id=1396",
                (user_id,),
            ).fetchone()["rating"]
            == 9.5
        )
