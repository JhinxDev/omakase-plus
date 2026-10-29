"""Account notifications derive only from saved domain state, never provider calls."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.auth import create_session
from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.web_security import csrf_token

STAMP = "2026-10-28T08:00:00Z"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    conn.executemany(
        "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
        [(1, "one@example.test", "x"), (2, "two@example.test", "x")],
    )
    conn.commit()
    yield conn
    conn.close()


def module():
    assert importlib.util.find_spec("omakase.plus.notifications") is not None, (
        "Derived notifications service is missing"
    )
    return importlib.import_module("omakase.plus.notifications")


def movie(db, number=1, user=1, status="completed", rating=None):
    imdb = f"tt{number:07d}"
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,updated_at) VALUES(?,?,?,?,?)",
        (user, imdb, number, f"Movie {number}", STAMP),
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,rating,status_source,rating_source,status_updated_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            user,
            imdb,
            number,
            status,
            rating,
            "manual",
            "manual" if rating else "none",
            STAMP,
            STAMP,
            STAMP,
        ),
    )
    db.commit()
    return imdb


def anime(db, number=1, user=1, status="completed"):
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number,expected_episodes) VALUES(?,?,?,1,2)",
        (user, number, f"Anime {number}"),
    ).lastrowid
    entry = db.execute(
        "INSERT INTO watch_entries(user_id,monitor_id,anilist_id,season_number,status,progress_episodes,episode_total,completed_at,last_source) VALUES(?,?,?,1,?,?,2,?,?)",
        (
            user,
            monitor,
            number,
            status,
            2 if status == "completed" else 1,
            STAMP if status == "completed" else None,
            "manual",
        ),
    ).lastrowid
    db.commit()
    return monitor, entry


def score(db, entry, user=1, value=8, disposition="accepted"):
    db.execute(
        "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,confidence,disposition,observed_at,score_ten) VALUES(?,?,'manual',?,'completed','manual',?,?,?)",
        (entry, user, f"synthetic:{entry}:{disposition}", disposition, STAMP, value),
    )
    db.commit()


def asset(
    db,
    monitor,
    *,
    user=1,
    kind="realdebrid",
    ownership="monitor_owned",
    provenance="omakase_created",
    shared=0,
    status="downloaded",
    verified=True,
    number=1,
):
    provider = db.execute(
        "INSERT OR IGNORE INTO media_providers(user_id,kind,label) VALUES(?,?,?)",
        (user, kind, kind),
    ).lastrowid
    provider = db.execute(
        "SELECT id FROM media_providers WHERE user_id=? AND kind=?", (user, kind)
    ).fetchone()[0]
    run = db.execute(
        "INSERT INTO media_sync_runs(monitor_id,outcome) VALUES(?,'complete')", (monitor,)
    ).lastrowid
    ident = db.execute(
        "INSERT INTO media_assets(monitor_id,torrent_hash,source_title,provider_id,provider_asset_id,ownership,provenance,created_by_run_id,shared_reference_count,downloaded_once,inventory_verified_at,usable_coverage_json,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            monitor,
            f"{number:040x}",
            "Synthetic release",
            provider,
            f"item-{number}",
            ownership,
            provenance,
            run if provenance == "omakase_created" else None,
            shared,
            1,
            STAMP if verified else None,
            "[1,2]",
            status,
        ),
    ).lastrowid
    db.commit()
    return ident


def test_empty_feed_is_read_only(db):
    svc = module()
    before = db.total_changes
    assert svc.notification_feed(db, user_id=1) == {
        "items": [],
        "count": 0,
        "unread_count": 0,
        "count_capped": False,
        "unread_count_capped": False,
        "limit": 20,
    }
    assert db.total_changes == before


def test_movie_only_explicit_completed_unrated_and_owner_scope(db):
    svc = module()
    movie(db, 1)
    movie(db, 2, status="watching")
    movie(db, 3, rating=7)
    movie(db, 4, user=2)
    result = svc.notification_feed(db, user_id=1)
    assert result["count"] == 1 and result["items"][0]["title"] == "Movie 1"
    assert result["items"][0]["href"] == "/plus/movies/tt0000001#movie-rating"
    assert (
        result["items"][0]["kind"] == "completed_unrated"
        and result["items"][0]["artwork_url"] is None
    )


def test_tv_single_finished_season_does_not_imply_series_completion(db):
    svc = module()
    metadata = {
        "tmdb_id": 42,
        "title": "Two seasons",
        "seasons": [
            {"season_number": 1, "episode_count": 1},
            {"season_number": 2, "episode_count": 2},
        ],
    }
    db.execute("INSERT INTO tv_catalog VALUES(1,42,?,?)", (json.dumps(metadata), STAMP))
    db.execute(
        "INSERT INTO tv_history(user_id,tmdb_id,status,status_source,updated_at) VALUES(1,42,'watching','manual',?)",
        (STAMP,),
    )
    db.execute(
        "INSERT INTO tv_episodes VALUES(1,42,1,1,?,1,?,0,?)",
        (json.dumps({"air_date": "2020-01-01"}), "manual", STAMP),
    )
    db.commit()
    assert svc.notification_feed(db, user_id=1)["items"] == []
    db.execute("UPDATE tv_history SET status='completed' WHERE user_id=1")
    db.commit()
    item = svc.notification_feed(db, user_id=1)["items"][0]
    assert item["media_type"] == "tv" and item["href"] == "/plus/tv/42#tv-history"
    db.execute("UPDATE tv_history SET rating=8 WHERE user_id=1")
    db.commit()
    assert svc.notification_feed(db, user_id=1)["items"] == []


def test_anime_uses_accepted_personal_score_not_pending_score(db):
    svc = module()
    monitor, entry = anime(db)
    score(db, entry, disposition="ignored")
    assert svc.notification_feed(db, user_id=1)["count"] == 1
    score(db, entry)
    assert svc.notification_feed(db, user_id=1)["count"] == 0


def test_rated_completed_managed_media_gets_one_review_with_current_rating(db):
    svc = module()
    monitor, entry = anime(db)
    score(db, entry, value=8)
    asset(db, monitor)
    asset(db, monitor, number=2)
    items = svc.notification_feed(db, user_id=1)["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "removal_reminder" and items[0]["rating"] == 8
    assert (
        items[0]["href"] == f"/plus/library/{monitor}/remove"
        and items[0]["action_label"] == "Review media"
    )
    assert "saved" in items[0]["message"].lower() and "deleted" not in items[0]["message"].lower()
    assert "provider_asset_id" not in items[0]


@pytest.mark.parametrize(
    "change",
    [
        {"ownership": "external", "provenance": "adopted"},
        {"shared": 1},
        {"status": "removed"},
        {"status": "error"},
        {"verified": False},
        {"kind": "local_import"},
    ],
)
def test_media_review_requires_saved_owned_verified_unshared_rd(db, change):
    svc = module()
    monitor, entry = anime(db)
    score(db, entry)
    asset(db, monitor, **change)
    assert svc.notification_feed(db, user_id=1)["items"] == []


def test_in_progress_anime_never_gets_rating_or_media_cleanup(db):
    svc = module()
    monitor, entry = anime(db, status="watching")
    asset(db, monitor)
    assert svc.notification_feed(db, user_id=1)["items"] == []


def test_foreign_provider_and_cross_account_shared_asset_do_not_create_review(db):
    svc = module()
    m, e = anime(db)
    score(db, e)
    with pytest.raises(sqlite3.IntegrityError, match="provider owner mismatch"):
        asset(db, m, user=2)
    db.rollback()
    asset(db, m)
    other, _ = anime(db, number=2, user=2)
    asset(db, other, user=2)
    assert svc.notification_feed(db, user_id=1)["items"] == []


def test_removed_asset_auto_resolves_media_review_without_losing_rating(db):
    svc = module()
    monitor, entry = anime(db)
    score(db, entry)
    a = asset(db, monitor)
    assert svc.notification_feed(db, user_id=1)["count"] == 1
    db.execute("UPDATE media_assets SET status='removed' WHERE id=?", (a,))
    db.commit()
    assert svc.notification_feed(db, user_id=1)["items"] == []
    assert (
        db.execute(
            "SELECT score_ten FROM watch_events WHERE watch_entry_id=?", (entry,)
        ).fetchone()[0]
        == 8
    )


def test_read_dismiss_are_idempotent_owner_scoped_and_recompletion_is_new_signal(db):
    svc = module()
    imdb = movie(db)
    movie(db, user=2)
    ident = svc.notification_feed(db, user_id=1)["items"][0]["id"]
    assert svc.set_notification_state(db, user_id=1, notification_id=ident, action="read")
    assert svc.set_notification_state(db, user_id=1, notification_id=ident, action="read")
    assert svc.notification_feed(db, user_id=1)["unread_count"] == 0
    assert svc.notification_feed(db, user_id=2)["unread_count"] == 1
    assert svc.set_notification_state(db, user_id=1, notification_id=ident, action="dismiss")
    assert svc.set_notification_state(db, user_id=1, notification_id=ident, action="dismiss")
    assert svc.notification_feed(db, user_id=1)["count"] == 0
    db.execute(
        "UPDATE movie_history SET status_updated_at='2026-10-29T08:00:00Z' WHERE user_id=1 AND imdb_id=?",
        (imdb,),
    )
    db.commit()
    new = svc.notification_feed(db, user_id=1)["items"][0]
    assert new["id"] != ident and new["read"] is False


def test_rating_auto_resolves_and_unknown_state_cannot_be_written(db):
    svc = module()
    movie(db)
    ident = svc.notification_feed(db, user_id=1)["items"][0]["id"]
    db.execute("UPDATE movie_history SET rating=6.5 WHERE user_id=1")
    db.commit()
    before = db.total_changes
    assert not svc.set_notification_state(db, user_id=1, notification_id=ident, action="dismiss")
    assert svc.notification_feed(db, user_id=1)["items"] == [] and db.total_changes == before


def test_feed_limits_items_and_caps_count_and_unread(db):
    svc = module()
    for number in range(1, 106):
        movie(db, number)
    feed = svc.notification_feed(db, user_id=1)
    assert len(feed["items"]) == 20 and feed["count"] == feed["unread_count"] == 100
    assert feed["count_capped"] and feed["unread_count_capped"]
    assert len({x["id"] for x in feed["items"]}) == 20


@pytest.fixture
def browser(db, monkeypatch):
    assert importlib.util.find_spec("omakase.plus.notifications_routes") is not None, (
        "Notifications routes missing"
    )
    router = importlib.import_module("omakase.plus.notifications_routes")
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    app = FastAPI()
    app.include_router(router.create_notifications_router(), prefix="/plus")
    app.dependency_overrides[get_db] = lambda: db
    session = create_session(db, 1)
    client = TestClient(app)
    client.cookies.set("omakase_session", session)
    from starlette.requests import Request

    token = csrf_token(
        Request({"type": "http", "headers": [(b"cookie", f"omakase_session={session}".encode())]})
    )
    yield client, token
    client.close()


def test_routes_require_auth_private_scope_and_strict_csrf(db, browser, monkeypatch):
    client, token = browser
    movie(db)
    feed = client.get("/plus/api/notifications")
    assert feed.status_code == 200
    assert feed.headers["cache-control"] == "private, no-store"
    ident = quote(feed.json()["items"][0]["id"], safe="")
    assert client.post(f"/plus/api/notifications/{ident}/read").status_code == 403
    assert (
        client.post(
            f"/plus/api/notifications/{ident}/read", headers={"X-CSRF-Token": token}
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/plus/api/notifications/{ident}/dismiss", headers={"X-CSRF-Token": token}
        ).json()["count"]
        == 0
    )
    client.cookies.clear()
    assert client.get("/plus/api/notifications").status_code == 401
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "false")
    assert client.get("/plus/api/notifications").status_code == 404


def test_route_get_never_calls_network_or_writes_notification_state(db, browser, monkeypatch):
    client, token = browser
    movie(db)

    def forbidden(*a, **k):
        raise AssertionError("Notification GET must stay local")

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    import socket

    monkeypatch.setattr(socket, "create_connection", forbidden)
    before = db.execute("SELECT COUNT(*) FROM notification_state").fetchone()[0]
    assert client.get("/plus/api/notifications").status_code == 200
    assert db.execute("SELECT COUNT(*) FROM notification_state").fetchone()[0] == before


def test_foreign_or_unknown_notification_post_is_404_and_never_inserts_state(db, browser):
    client, token = browser
    anime(db, user=2)
    ident = module().notification_feed(db, user_id=2)["items"][0]["id"]
    assert (
        client.post(
            "/plus/api/notifications/" + quote(ident, safe="") + "/dismiss",
            headers={"X-CSRF-Token": token},
        ).status_code
        == 404
    )
    assert db.execute("SELECT COUNT(*) FROM notification_state").fetchone()[0] == 0


def test_shared_plex_title_with_different_provider_assets_has_no_cleanup_reminder(db):
    svc = module()
    monitor, entry = anime(db)
    score(db, entry)
    asset(db, monitor)
    other, _ = anime(db, user=2)
    asset(db, other, user=2, number=9)
    assert svc.notification_feed(db, user_id=1)["items"] == []


def test_legacy_saved_anime_rating_prevents_unrated_and_labels_managed_review(db):
    svc = module()
    monitor, entry = anime(db)
    asset(db, monitor)
    db.execute(
        "INSERT INTO tracker_media_actions(user_id,tracker,media_id,title,action,score_ten) VALUES(1,'anilist',1,'Anime 1','completed',9)"
    )
    db.commit()
    feed = svc.notification_feed(db, user_id=1)
    assert feed["count"] == 1 and feed["items"][0]["kind"] == "removal_reminder"
    assert feed["items"][0]["rating"] == 9


def test_completed_movie_without_explicit_status_source_does_not_invent_completion(db):
    svc = module()
    movie(db)
    db.execute("UPDATE movie_history SET status_source='none'")
    db.commit()
    assert svc.notification_feed(db, user_id=1)["items"] == []


def test_notification_migration_is_additive_and_does_not_modify_domain_records(db):
    movie(db)
    before = [tuple(row) for row in db.execute("SELECT * FROM movie_history")]
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    ident = module().notification_feed(db, user_id=1)["items"][0]["id"]
    module().set_notification_state(db, user_id=1, notification_id=ident, action="read")
    assert [tuple(row) for row in db.execute("SELECT * FROM movie_history")] == before
    assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_route_security_failures_are_private_not_cacheable(db, browser):
    client, token = browser
    response = client.post(
        "/plus/api/notifications/not-a-signal/dismiss", headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 404 and response.headers["cache-control"] == "private, no-store"
    response = client.post(
        "/plus/api/notifications/not-a-signal/delete", headers={"X-CSRF-Token": token}
    )
    assert response.status_code == 422 and response.headers["vary"] == "Cookie"
    client.cookies.clear()
    response = client.get("/plus/api/notifications")
    assert response.status_code == 401 and response.headers["cache-control"] == "private, no-store"


def test_header_windows_use_account_indexes_without_materialized_history_or_global_asset_scans(db):
    svc = module()
    params = {
        "user_id": 1,
        "cap": 101,
        "unread_only": False,
        "include_dismissed": False,
        "target_id": None,
    }
    expected = {
        "anime_rating": "idx_notification_anime_completed",
        "anime_media": "idx_notification_anime_completed",
        "movie_rating": "idx_notification_movie_completed",
        "tv_rating": "idx_notification_tv_completed",
    }
    for source, index in expected.items():
        plan = [
            row["detail"]
            for row in db.execute("EXPLAIN QUERY PLAN " + svc._window_sql(source), params)
        ]
        assert any(index in line for line in plan)
        assert not any("MATERIALIZE" in line for line in plan)
        assert not any(line.startswith(("SCAN r", "SCAN other", "SCAN shared")) for line in plan), (
            plan
        )


def test_read_and_dismiss_filters_apply_before_per_domain_windows(db):
    svc = module()
    for number in range(1, 106):
        movie(db, number)
    db.executemany(
        "INSERT INTO notification_state(user_id,notification_id,read_at) VALUES(1,?,?)",
        [(f"completed_unrated:movie:tt{number:07d}:{STAMP}", STAMP) for number in range(1, 102)],
    )
    db.commit()
    feed = svc.notification_feed(db, user_id=1)
    assert feed["unread_count"] == 4 and not feed["unread_count_capped"]
    db.execute("UPDATE notification_state SET dismissed_at=? WHERE user_id=1", (STAMP,))
    db.commit()
    feed = svc.notification_feed(db, user_id=1)
    assert feed["count"] == feed["unread_count"] == len(feed["items"]) == 4
    assert not feed["count_capped"]


def test_serialized_current_rating_preserves_zero_instead_of_truthiness():
    row = {
        "kind": "removal_reminder",
        "media_type": "anime",
        "title": "Synthetic",
        "id": "removal_reminder:anime:1:version",
        "rating": 0,
        "href": "/plus/library/1/remove",
        "created_at": STAMP,
        "read_at": None,
        "artwork_ref": 1,
    }
    assert module()._item(row)["rating"] == 0


def _notification_poster_movie(db, url, *, number=1, user=1):
    imdb = movie(db, number=number, user=user)
    db.execute(
        "UPDATE movie_catalog SET poster_url=? WHERE user_id=? AND imdb_id=?", (url, user, imdb)
    )
    db.commit()
    return imdb


def _notification_poster_tv(db, url, *, number=42, user=1):
    db.execute(
        "INSERT INTO tv_catalog VALUES(?,?,?,?)",
        (user, number, json.dumps({"title": "Synthetic TV", "poster_url": url}), STAMP),
    )
    db.execute(
        "INSERT INTO tv_history(user_id,tmdb_id,status,status_source,updated_at) VALUES(?,?,'completed','manual',?)",
        (user, number, STAMP),
    )
    db.commit()


def test_notification_posters_use_owned_local_routes_all_domains_and_cleanup(db):
    monitor, entry = anime(db, number=12)
    asset(db, monitor)
    _notification_poster_movie(db, "https://image.tmdb.org/t/p/w500/movie.jpg", number=34)
    _notification_poster_tv(db, "https://image.tmdb.org/t/p/original/tv.png")
    anime(db, number=99, user=2)
    _notification_poster_movie(db, "https://image.tmdb.org/t/p/w500/foreign.jpg", number=98, user=2)
    _notification_poster_tv(db, "https://image.tmdb.org/t/p/w500/foreign-tv.jpg", number=97, user=2)
    items = module().notification_feed(db, user_id=1)["items"]
    assert len(items) == 3
    assert {item["artwork_url"] for item in items} == {
        "/plus/media/12/poster",
        "/plus/movies/tt0000034/artwork",
        "/plus/tv/42/artwork",
    }
    assert not any("http" in item["artwork_url"] for item in items)
    assert (
        next(item for item in items if item["kind"] == "removal_reminder")["artwork_url"]
        == "/plus/media/12/poster"
    )


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "http://image.tmdb.org/t/p/w500/a.jpg",
        "https://localhost/a.jpg",
        "https://image.tmdb.org.evil.test/t/p/w500/a.jpg",
        "https://user@image.tmdb.org/t/p/w500/a.jpg",
        "https://image.tmdb.org:444/t/p/w500/a.jpg",
        "//image.tmdb.org/t/p/w500/a.jpg",
        "https://image.tmdb.org/t/p/w500/a.jpg?token=secret",
        "https://image.tmdb.org/t/p/w500/../a.jpg",
        "javascript:alert(1)",
    ],
)
def test_notification_posters_missing_or_untrusted_metadata_stays_neutral(db, url):
    _notification_poster_movie(db, url)
    _notification_poster_tv(db, url)
    assert all(
        item["artwork_url"] is None for item in module().notification_feed(db, user_id=1)["items"]
    )


def test_notification_posters_never_borrow_other_account_catalog_artwork(db):
    _notification_poster_movie(db, None, number=1, user=1)
    _notification_poster_movie(db, "https://image.tmdb.org/t/p/w500/foreign.jpg", number=1, user=2)
    _notification_poster_tv(db, None, user=1)
    _notification_poster_tv(db, "https://image.tmdb.org/t/p/w500/foreign-tv.jpg", user=2)
    assert all(
        item["artwork_url"] is None for item in module().notification_feed(db, user_id=1)["items"]
    )


def test_notification_poster_feed_is_sqlite_only_no_image_disk_or_network(db, browser, monkeypatch):
    import socket
    from pathlib import Path

    import omakase.plus.movie_artwork as movie_art
    import omakase.plus.posters as posters

    anime(db, number=12)
    _notification_poster_movie(db, "https://image.tmdb.org/t/p/w500/movie.jpg")
    _notification_poster_tv(db, "https://image.tmdb.org/t/p/w500/tv.jpg")

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "The notification feed must not load images, cache files, metadata or providers"
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(posters, "poster_bytes", forbidden)
    monkeypatch.setattr(movie_art, "movie_artwork", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    before = db.total_changes
    response = browser[0].get("/plus/api/notifications")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 3
    assert all(item["artwork_url"].startswith("/plus/") for item in response.json()["items"])
    assert db.total_changes == before


@pytest.mark.parametrize("media_type", ["movie", "tv"])
def test_notification_movie_tv_artwork_routes_recheck_catalog_account(
    db, browser, monkeypatch, media_type
):
    import io

    from PIL import Image

    client, _ = browser
    own = "https://image.tmdb.org/t/p/w500/own.jpg"
    other = "https://image.tmdb.org/t/p/w500/foreign.jpg"
    if media_type == "movie":
        import omakase.plus.movies_routes as routes

        _notification_poster_movie(db, own, number=1)
        _notification_poster_movie(db, other, number=2, user=2)
        client.app.include_router(routes.create_movies_router(None), prefix="/plus")
        owned_url = "/plus/movies/tt0000001/artwork"
        foreign_url = "/plus/movies/tt0000002/artwork"
    else:
        import omakase.plus.tv_routes as routes
        from omakase.plus.movies import MovieConnections

        _notification_poster_tv(db, own, number=1)
        _notification_poster_tv(db, other, number=2, user=2)
        monkeypatch.setattr(routes, "movie_connections", lambda *_: MovieConnections())
        client.app.include_router(routes.create_tv_router(None), prefix="/plus")
        owned_url = "/plus/tv/1/artwork"
        foreign_url = "/plus/tv/2/artwork"
    stream = io.BytesIO()
    Image.new("RGB", (20, 30), "purple").save(stream, format="PNG")
    called = []

    async def artwork(url):
        called.append(url)
        return stream.getvalue(), "image/png"

    monkeypatch.setattr(routes, "movie_artwork", artwork)
    response = client.get(owned_url)
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert called == [own]
    assert client.get(foreign_url).status_code == 404
    assert called == [own]
    client.cookies.clear()
    assert client.get(owned_url).status_code == 401


def test_completed_unrated_managed_title_is_one_item_with_two_actions_and_one_unread(db):
    monitor, entry = anime(db)
    asset(db, monitor)
    feed = module().notification_feed(db, user_id=1)
    assert feed["count"] == feed["unread_count"] == len(feed["items"]) == 1
    item = feed["items"][0]
    assert item["artwork_url"] == "/plus/media/1/poster" and item["rating"] is None
    assert item["actions"] == [
        {"kind": "rate", "label": "Rate title", "href": f"/plus/library/{monitor}#watch-tracking"},
        {
            "kind": "review_media",
            "label": "Review media",
            "href": f"/plus/library/{monitor}/remove",
        },
    ]
    assert module().set_notification_state(db, user_id=1, notification_id=item["id"], action="read")
    score(db, entry, value=7)
    updated = module().notification_feed(db, user_id=1)
    assert updated["count"] == 1 and updated["unread_count"] == 0
    assert updated["items"][0]["id"] == item["id"] and updated["items"][0]["rating"] == 7
    assert [a["kind"] for a in updated["items"][0]["actions"]] == ["review_media"]


def test_combined_dismissal_survives_rating_or_media_resolution_and_is_owner_scoped(db):
    monitor, entry = anime(db)
    owned = asset(db, monitor)
    other, _ = anime(db, number=2, user=2)
    asset(db, other, user=2, number=2)
    svc = module()
    item = svc.notification_feed(db, user_id=1)["items"][0]
    assert svc.set_notification_state(db, user_id=1, notification_id=item["id"], action="dismiss")
    assert svc.set_notification_state(db, user_id=1, notification_id=item["id"], action="dismiss")
    assert svc.notification_feed(db, user_id=1)["count"] == 0
    assert svc.notification_feed(db, user_id=2)["count"] == 1
    db.execute("UPDATE media_assets SET status='removed' WHERE id=?", (owned,))
    db.commit()
    assert svc.notification_feed(db, user_id=1)["count"] == 0  # Rating reason was dismissed too.
    db.execute("UPDATE watch_entries SET completed_at='2026-11-01T08:00:00Z' WHERE id=?", (entry,))
    db.commit()
    rewatch = svc.notification_feed(db, user_id=1)
    assert rewatch["count"] == rewatch["unread_count"] == 1
    assert [a["kind"] for a in rewatch["items"][0]["actions"]] == ["rate"]


def test_new_managed_asset_resurfaces_once_without_score_edit_resurfacing(db):
    monitor, entry = anime(db)
    asset(db, monitor)
    svc = module()
    old = svc.notification_feed(db, user_id=1)["items"][0]
    assert svc.set_notification_state(db, user_id=1, notification_id=old["id"], action="dismiss")
    score(db, entry, value=8)
    assert svc.notification_feed(db, user_id=1)["count"] == 0
    added = asset(db, monitor, number=2)
    # Same-second creation must use the new asset revision, not old timestamp aliases.
    assert added is not None
    fresh = svc.notification_feed(db, user_id=1)
    assert fresh["count"] == fresh["unread_count"] == 1 and fresh["items"][0]["id"] != old["id"]
    assert fresh["items"][0]["rating"] == 8
    assert [a["kind"] for a in fresh["items"][0]["actions"]] == ["review_media"]


def test_prior_media_signal_dismissal_honored_only_for_assets_present_then(db):
    monitor, _ = anime(db)
    first = asset(db, monitor)
    db.execute("UPDATE media_assets SET created_at='2026-01-01T00:00:00Z' WHERE id=?", (first,))
    db.execute(
        "INSERT INTO notification_state(user_id,notification_id,read_at,dismissed_at) VALUES(1,?,?,?)",
        (f"removal_reminder:anime:{monitor}:{STAMP}", STAMP, STAMP),
    )
    db.commit()
    svc = module()
    assert svc.notification_feed(db, user_id=1)["count"] == 0
    newer = asset(db, monitor, number=2)
    db.execute("UPDATE media_assets SET created_at='2026-11-01T00:00:00Z' WHERE id=?", (newer,))
    db.commit()
    assert (
        svc.notification_feed(db, user_id=1)["count"]
        == svc.notification_feed(db, user_id=1)["unread_count"]
        == 1
    )
