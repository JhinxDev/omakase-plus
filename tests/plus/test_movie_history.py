"""Movie history is personal, explicit and independent of delivery readiness."""

from __future__ import annotations

import importlib
import importlib.util
import json
from xml.etree import ElementTree

import pytest

from tests.plus.test_movies import IMDB, NOW, TMDB, NativeMovies, run, service
from tests.plus.test_movies import db as db


def history_service(db, fake=None):
    assert importlib.util.find_spec("omakase.plus.movie_history") is not None, (
        "Movie history is missing"
    )
    module = importlib.import_module("omakase.plus.movie_history")
    fake = fake or NativeMovies()
    fake.detail["genres"] = [{"id": 27, "name": "Horror"}]
    movies = service(db, fake)
    return module.MovieHistoryService(movies), movies, fake


def set_plex_fields(fake, fields):
    original = fake.video_xml

    def xml():
        root = ElementTree.fromstring(original())
        root.find("Video").attrib.update(fields)
        return ElementTree.tostring(root)

    fake.video_xml = xml


def save(history, user_id=1, **values):
    return run(history.save(user_id, imdb_id=IMDB, tmdb_id=TMDB, **values))


def test_manual_status_and_half_step_rating_have_independent_provenance(db):
    history, movies, fake = history_service(db)
    row = save(history, status="completed", rating=8.5)
    assert row["status"] == "completed"
    assert row["rating"] == 8.5
    assert row["status_source"] == row["rating_source"] == "manual"
    assert row["status_updated_at"] == "2026-10-27T15:00:00Z"
    assert history.get(1, IMDB) == row
    assert row["last_viewed_at"] is None
    count = db.execute("SELECT count(*) FROM movie_history_events").fetchone()[0]
    save(history, status="completed", rating=8.5)
    assert db.execute("SELECT count(*) FROM movie_history_events").fetchone()[0] == count
    assert db.execute("SELECT count(*) FROM anilist_plannings").fetchone()[0] == 0
    assert all(call.method == "GET" for call in fake.calls)


@pytest.mark.parametrize("rating", [0, 10.5, 7.25, True, "8.5", float("nan"), float("inf")])
def test_invalid_ratings_are_rejected_before_network_or_storage(db, rating):
    history, movies, fake = history_service(db)
    with pytest.raises(ValueError):
        save(history, rating=rating)
    assert fake.calls == []
    assert db.execute("SELECT count(*) FROM movie_history").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["ready", "downloaded", "watched", "", None])
def test_only_explicit_watch_statuses_are_accepted(db, status):
    history, movies, fake = history_service(db)
    with pytest.raises(ValueError):
        save(history, status=status)
    assert fake.calls == []


def test_mapping_is_verified_before_manual_history_is_saved(db):
    history, movies, fake = history_service(db)
    fake.detail["imdbId"] = fake.detail["externalIds"]["imdbId"] = "tt0000001"
    with pytest.raises(ValueError):
        save(history, status="completed")
    assert db.execute("SELECT count(*) FROM movie_history").fetchone()[0] == 0


def test_plex_positive_history_import_is_idempotent_and_read_only(db):
    history, movies, fake = history_service(db)
    fields = {
        "viewCount": "1",
        "viewOffset": "0",
        "lastViewedAt": str(int(NOW.timestamp()) - 3600),
        "userRating": "8.5",
    }
    set_plex_fields(fake, fields)
    result = run(history.sync_plex(1))
    row = history.get(1, IMDB)
    assert result["imported"] == 1
    assert row["status"] == "completed"
    assert row["rating"] == 8.5
    assert row["status_source"] == row["rating_source"] == "plex"
    assert row["last_viewed_at"] == "2026-10-27T14:00:00Z"
    events = db.execute("SELECT count(*) FROM movie_history_events").fetchone()[0]
    assert run(history.sync_plex(1))["unchanged"] == 1
    assert db.execute("SELECT count(*) FROM movie_history_events").fetchone()[0] == events
    assert all(
        call.method == "GET" and "/library/parts/" not in call.url.path for call in fake.calls
    )


def test_manual_corrections_and_explicit_rating_clear_survive_plex_sync(db):
    history, movies, fake = history_service(db)
    save(history, status="unwatched", rating=None)
    set_plex_fields(
        fake, {"viewCount": "5", "userRating": "10", "lastViewedAt": str(int(NOW.timestamp()) - 1)}
    )
    run(history.sync_plex(1))
    row = history.get(1, IMDB)
    assert row["status"] == "unwatched"
    assert row["rating"] is None
    assert row["status_source"] == row["rating_source"] == "manual"


def test_rating_override_does_not_block_automatic_watch_status(db):
    history, movies, fake = history_service(db)
    save(history, rating=10)
    set_plex_fields(fake, {"viewCount": "1", "userRating": "4"})
    run(history.sync_plex(1))
    row = history.get(1, IMDB)
    assert row["status"] == "completed"
    assert row["status_source"] == "plex"
    assert row["rating"] == 10
    assert row["rating_source"] == "manual"


def test_reset_or_absent_plex_fields_do_not_erase_saved_positive_history(db):
    history, movies, fake = history_service(db)
    fields = {
        "viewCount": "1",
        "viewOffset": "1000",
        "userRating": "7.5",
        "lastViewedAt": str(int(NOW.timestamp()) - 20),
    }
    set_plex_fields(fake, fields)
    run(history.sync_plex(1))
    fields.update(viewCount="0", viewOffset="0", userRating="0", lastViewedAt="0")
    run(history.sync_plex(1))
    row = history.get(1, IMDB)
    assert row["status"] == "completed"
    assert row["rating"] == 7.5
    assert row["last_viewed_at"] is not None


def test_explicit_follow_plex_releases_only_the_selected_manual_field(db):
    history, movies, fake = history_service(db)
    set_plex_fields(fake, {"viewCount": "1", "userRating": "9"})
    run(history.sync_plex(1))
    save(history, status="dropped", rating=5.5)
    calls = len(fake.calls)
    row = history.use_plex(1, IMDB, fields=("status",))
    assert row["status"] == "completed" and row["status_source"] == "plex"
    assert row["rating"] == 5.5 and row["rating_source"] == "manual"
    row = history.use_plex(1, IMDB, fields=("rating",))
    assert row["rating"] == 9 and row["rating_source"] == "plex"
    assert len(fake.calls) == calls


def test_no_playback_fields_and_core_ready_state_never_create_watched_history(db):
    history, movies, fake = history_service(db)
    run(movies.search(1, "The Thing"))
    assert run(movies.verify(1, IMDB))["state"] == "ready"
    run(movies.list(1))
    assert history.get(1, IMDB) is None
    run(history.sync_plex(1))
    row = history.get(1, IMDB)
    assert row is None or (row["status"] == "unwatched" and row["status_source"] == "none")


@pytest.mark.parametrize("mutation", ["wrong-guid", "anime-library", "ambiguous-guid"])
def test_sync_excludes_wrong_or_ambiguous_movie_identity_and_anime(db, mutation):
    history, movies, fake = history_service(db)
    if mutation == "wrong-guid":
        fake.plex_guid = "tt0000001"
    elif mutation == "anime-library":
        fake.plex_library = "Anime Movies"
    else:
        original = fake.video_xml

        def xml():
            root = ElementTree.fromstring(original())
            ElementTree.SubElement(root.find("Video"), "Guid", {"id": "imdb://tt0000001"})
            return ElementTree.tostring(root)

        fake.video_xml = xml
    set_plex_fields(fake, {"viewCount": "1", "userRating": "9"})
    result = run(history.sync_plex(1))
    assert result["skipped"] == 1
    assert db.execute("SELECT count(*) FROM movie_history").fetchone()[0] == 0


def test_user_history_isolation_export_and_card_augmentation(db):
    history, movies, fake = history_service(db)
    save(history, 1, status="completed", rating=9)
    save(history, 2, status="dropped", rating=2)
    payload = history.export_history(1)
    assert payload["schema_version"] == 1
    assert len(payload["history"]) == 1
    assert payload["history"][0]["rating"] == 9
    assert not any(
        word in json.dumps(payload) for word in ("secret", "http://", "password", "backend_scope")
    )
    cards = run(movies.search(1, "The Thing"))
    card = history.augment(1, cards[0])
    assert card["watch_status"] == "completed"
    assert card["user_rating"] == 9
    assert card["history"]["rating_source"] == "manual"
    assert history.list(2)[0]["rating"] == 2
