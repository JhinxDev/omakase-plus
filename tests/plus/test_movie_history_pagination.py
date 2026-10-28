"""Repeated bounded syncs must reach every eligible movie."""

import asyncio
from dataclasses import replace
from xml.etree import ElementTree

from omakase.plus.movie_history import MovieHistoryService
from omakase.plus.movies_store import upsert_movie
from tests.plus.test_movies import NativeMovies, service
from tests.plus.test_movies import db as db


def library(db, monkeypatch, count=101, *, verified=True):
    movies = service(db, NativeMovies())
    videos = []
    for index in range(count):
        imdb = f"tt{9000000 + index}"
        tmdb = 8000 + index
        for user in (1, 2):
            upsert_movie(
                db,
                user,
                dict(imdb_id=imdb, tmdb_id=tmdb, title=f"Fixture {index}", year=2020),
                now="2026-10-27T00:00:00Z",
                identity_verified=verified,
            )
        video = ElementTree.Element(
            "Video",
            {
                "type": "movie",
                "ratingKey": str(index + 1),
                "librarySectionID": "6",
                "librarySectionTitle": "Movies",
                "viewCount": "1",
                "lastViewedAt": "1793000000",
            },
        )
        ElementTree.SubElement(video, "Guid", {"id": "imdb://" + imdb})
        ElementTree.SubElement(video, "Guid", {"id": "tmdb://" + str(tmdb)})
        videos.append(video)
    db.commit()

    async def inventory(section, **kwargs):
        return videos

    monkeypatch.setattr(movies, "_videos", inventory)
    history = MovieHistoryService(movies)
    return history, movies, videos


def test_successive_default_batches_reach_movie_101(db, monkeypatch):
    history, movies, videos = library(db, monkeypatch)
    first = asyncio.run(history.sync_plex(1))
    second = asyncio.run(history.sync_plex(1))
    assert first["observed"] == 100
    assert second["observed"] == 1
    assert second["remaining"] == 0
    assert history.get(1, "tt9000100")["status"] == "completed"
    assert db.execute("SELECT count(*) FROM movie_history WHERE user_id=1").fetchone()[0] == 101


def test_cursor_recovers_and_advances_after_library_shrinks(db, monkeypatch):
    history, movies, videos = library(db, monkeypatch, count=3)
    db.execute(
        "INSERT INTO movie_sync_cursors VALUES (1,?,5,?)",
        (history._scope(), "2026-10-27T00:00:00Z"),
    )
    db.commit()
    assert asyncio.run(history.sync_plex(1, limit=1))["next_offset"] == 1
    assert asyncio.run(history.sync_plex(1, limit=1))["next_offset"] == 2
    assert db.execute("SELECT next_offset FROM movie_sync_cursors").fetchone()[0] == 2


def test_mapping_budget_continues_at_the_thirteenth_unknown_identity(db, monkeypatch):
    history, movies, videos = library(db, monkeypatch, count=13, verified=False)
    resolved = []

    async def identify(user, imdb, tmdb):
        resolved.append(imdb)
        return upsert_movie(
            db,
            user,
            dict(imdb_id=imdb, tmdb_id=tmdb, title="Fixture", year=2020),
            now="2026-10-27T00:00:00Z",
        )

    monkeypatch.setattr(history, "_identity", identify)
    first = asyncio.run(history.sync_plex(1))
    assert first["observed"] == 12 and first["next_offset"] == 12 and first["remaining"] == 1
    second = asyncio.run(history.sync_plex(1))
    assert second["observed"] == 1 and second["remaining"] == 0 and len(resolved) == 13


def test_sync_cursor_is_scoped_to_user_and_plex_token(db, monkeypatch):
    history, movies, videos = library(db, monkeypatch, count=3)
    assert asyncio.run(history.sync_plex(1, limit=1))["next_offset"] == 1
    assert asyncio.run(history.sync_plex(2, limit=1))["next_offset"] == 1
    old_scope = history._scope()
    movies.connections = replace(movies.connections, plex_token="different-fixture-token")

    async def sections():
        return 6, None

    monkeypatch.setattr(movies, "_sections", sections)
    assert history._scope() != old_scope
    assert asyncio.run(history.sync_plex(1, limit=1))["next_offset"] == 1
    assert db.execute("SELECT count(*) FROM movie_sync_cursors").fetchone()[0] == 3
