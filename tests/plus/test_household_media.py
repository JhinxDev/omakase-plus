"""Scoped media adapters use three accounts and real migration/reducer persistence."""

from __future__ import annotations

import json
import sqlite3

import pytest

from omakase.plus import household_media as media
from omakase.plus import household_views as views
from omakase.plus import households as hh
from omakase.plus.db import run_migrations
from omakase.plus.watch.models import WatchEvent


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    for user in (1, 2, 3):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"viewer{user}@example.invalid", "synthetic"),
        )
    conn.commit()
    yield conn
    conn.close()


def shared(db):
    lib = hh.create_household(db, user_id=1, name="Shared media", acquisition_requests=True)
    owner = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    invite = hh.create_invite(db, owner, now=100)
    hh.accept_invite(db, viewer_id=2, token=invite["token"], now=101)
    return lib


def scopes(db, lib):
    return [hh.resolve_scope(db, viewer_id=n, library_id=lib) for n in (1, 2)]


def anime(db, owner):
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number,expected_episodes) VALUES(1,100,'Shared anime',1,12)"
    ).lastrowid
    db.commit()
    item = hh.LibraryRepository(db, owner).add(
        domain="anime", canonical_id="100", season=1, title="Shared anime", legacy_id=str(monitor)
    )
    return item, monitor


def test_shared_collection_keeps_real_viewer_history_and_get_is_read_only(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    item, monitor = anime(db, owner)
    original = db.total_changes
    assert views.detail(db, member, item_id=item)["history"] is None
    assert db.total_changes == original
    own = media.binding(db, owner, item_id=item)
    mine = media.binding(db, member, item_id=item)
    media.save_anime_watch(
        db,
        own,
        event=WatchEvent(
            source="manual",
            kind="completed",
            event_key="owner-complete",
            episode=12,
            confidence="manual",
        ),
        score_ten=9,
    )
    media.save_anime_watch(
        db,
        mine,
        event=WatchEvent(
            source="manual",
            kind="progress",
            event_key="member-progress",
            episode=3,
            confidence="manual",
        ),
    )
    assert views.detail(db, owner, item_id=item)["history"]["rating"] == 9
    current = views.detail(db, member, item_id=item)
    assert current["history"]["progress_episodes"] == 3
    assert current["history"]["rating"] is None
    assert current["history"]["monitor_id"] is None
    assert (
        db.execute("SELECT user_id FROM media_monitors WHERE id=?", (monitor,)).fetchone()[0] == 1
    )
    assert {r[0] for r in db.execute("SELECT user_id FROM tracker_sync_outbox")} == {1, 2}
    assert db.execute("SELECT COUNT(*) FROM media_title_removals").fetchone()[0] == 0
    with pytest.raises(hh.HouseholdError):
        views.detail(db, hh.resolve_scope(db, viewer_id=3, library_id=lib), item_id=item)


def test_personal_and_shared_same_title_preserve_monitor_binding_and_ratings(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    item, monitor = anime(db, owner)
    db.execute(
        "INSERT INTO watch_entries(user_id,anilist_id,season_number,monitor_id) VALUES(1,100,1,?)",
        (monitor,),
    )
    with hh._transaction(db):
        personal_id = hh.ensure_personal_library(db, user_id=1)
    personal = hh.resolve_scope(db, viewer_id=1, library_id=personal_id)
    private_item = hh.LibraryRepository(db, personal).add(
        domain="anime",
        canonical_id="100",
        season=1,
        title="Same personal title",
        legacy_id=str(monitor),
    )
    media.save_anime_watch(
        db,
        media.binding(db, owner, item_id=item),
        event=WatchEvent(
            source="manual", kind="completed", event_key="complete", episode=12, confidence="manual"
        ),
        score_ten=8,
    )
    assert views.detail(db, personal, item_id=private_item)["history"]["rating"] == 8
    assert views.detail(db, owner, item_id=item)["history"]["monitor_id"] == monitor
    assert views.detail(db, member, item_id=item)["history"] is None
    assert hh.LibraryRepository(db, owner).removal_blocked(item)


def test_revocation_immediately_denies_detail_art_and_tracking_but_preserves_history(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    item, _ = anime(db, owner)
    bound = media.binding(db, member, item_id=item)
    media.save_anime_watch(
        db,
        bound,
        event=WatchEvent(
            source="manual",
            kind="completed",
            event_key="member-complete",
            episode=12,
            confidence="manual",
        ),
        score_ten=7,
    )
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    for operation in (
        lambda: media.public_metadata(db, bound),
        lambda: views.detail(db, member, item_id=item),
        lambda: media.save_anime_watch(
            db,
            bound,
            event=WatchEvent(
                source="manual",
                kind="reopened",
                event_key="revoked",
                confidence="manual",
                force=True,
            ),
        ),
    ):
        with pytest.raises(hh.HouseholdError):
            operation()
    assert hh.PersonalTrackingRepository(db, viewer_id=2).history("anime")[0]["rating"] == 7


def test_movie_and_tv_public_metadata_strip_owner_history_and_private_fields(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,poster_url,overview,updated_at) VALUES(1,'tt1234567',42,'Movie','https://example.invalid/private','Public synopsis','now')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,rating,created_at,updated_at) VALUES(1,'tt1234567',42,'completed',9,'now','now')"
    )
    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(1,55,?,'now')",
        (
            json.dumps(
                {
                    "title": "TV",
                    "poster_url": "https://image.tmdb.org/t/p/w500/safe.jpg",
                    "rating": 10,
                    "token": "private",
                    "watched": True,
                    "plex_key": "private",
                }
            ),
        ),
    )
    db.execute(
        "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(1,55,1,1,'{}',1,'plex',1,'now')"
    )
    db.commit()
    movie = hh.LibraryRepository(db, owner).add(
        domain="movie", canonical_id="tt1234567", title="Movie", legacy_id="tt1234567"
    )
    tv = hh.LibraryRepository(db, owner).add(
        domain="tv", canonical_id="55", title="TV", legacy_id="55"
    )
    for item in (movie, tv):
        card = views.detail(db, member, item_id=item)
        assert card["history"] is None
        assert "private" not in json.dumps(card["metadata"])
        assert card["facts"]["playback_label"] == "Your playback has not been checked"
    assert views.detail(db, member, item_id=movie)["metadata"]["poster_url"] is None
    assert views.detail(db, member, item_id=tv)["facts"]["plex_episodes"] == 1
    assert views.detail(db, member, item_id=tv)["metadata"]["poster_url"].startswith(
        "https://image.tmdb.org/"
    )


def test_member_add_does_not_expose_personal_asset_or_impersonate_owner(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(1,200,'Private asset',1)"
    ).lastrowid
    db.commit()
    addition = media.prepare_addition(
        db, member, domain="anime", canonical_id="200", season=1, title="Public title"
    )
    assert addition.actor_user_id == 2 and addition.operational_owner_id == 1
    assert addition.legacy_id is None
    event = hh.LibraryRepository(db, member).queue_request(
        addition.item_id, idempotency_key="member-add"
    )
    with pytest.raises(hh.HouseholdError):
        media.bind_acquired_resource(
            db, addition, event_id=event, legacy_id=str(monitor), connector_scope="shared"
        )
    hh.authorize_dispatch(db, event_id=event)
    bound = media.bind_acquired_resource(
        db, addition, event_id=event, legacy_id=str(monitor), connector_scope="shared"
    )
    assert (
        bound.scope.role == "member"
        and bound.actor_user_id == 2
        and bound.legacy_id == str(monitor)
    )
    assert (
        db.execute(
            "SELECT created_by FROM library_items WHERE id=?", (addition.item_id,)
        ).fetchone()[0]
        == 2
    )
    assert (
        db.execute(
            "SELECT actor_user_id FROM library_request_events WHERE id=?", (event,)
        ).fetchone()[0]
        == 2
    )


def test_changed_policy_prevents_dispatch_binding_and_duplicate_watch_event_is_idempotent(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    item, monitor = anime(db, owner)
    bound = media.binding(db, member, item_id=item)
    event = WatchEvent(
        source="manual", kind="progress", event_key="same-key", episode=2, confidence="manual"
    )
    first = media.save_anime_watch(db, bound, event=event)
    second = media.save_anime_watch(db, bound, event=event)
    assert first.event_id == second.event_id and second.reason == "duplicate_event"
    queued = hh.LibraryRepository(db, member).queue_request(item, idempotency_key="dispatch")
    hh.authorize_dispatch(db, event_id=queued)
    hh.set_policy(db, owner, members_can_add=False, acquisition_requests=False)
    with pytest.raises(hh.HouseholdError):
        media.bind_acquired_resource(
            db, bound, event_id=queued, legacy_id=str(monitor), connector_scope="legacy"
        )


def test_member_personal_monitor_and_shared_same_title_keep_distinct_resources(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    item, owner_monitor = anime(db, owner)
    member_monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(2,100,'My personal version',1)"
    ).lastrowid
    db.execute(
        "INSERT INTO watch_entries(user_id,anilist_id,season_number,monitor_id) VALUES(2,100,1,?)",
        (member_monitor,),
    )
    with hh._transaction(db):
        personal_id = hh.ensure_personal_library(db, user_id=2)
    personal = hh.resolve_scope(db, viewer_id=2, library_id=personal_id)
    own_item = hh.LibraryRepository(db, personal).add(
        domain="anime",
        canonical_id="100",
        season=1,
        title="My personal version",
        legacy_id=str(member_monitor),
    )
    media.save_anime_watch(
        db,
        media.binding(db, member, item_id=item),
        event=WatchEvent(
            source="manual",
            kind="progress",
            event_key="member-own-binding",
            episode=4,
            confidence="manual",
        ),
    )
    assert views.detail(db, member, item_id=item)["history"]["monitor_id"] == member_monitor
    assert views.detail(db, personal, item_id=own_item)["history"]["progress_episodes"] == 4
    assert media.binding(db, member, item_id=item).legacy_id == str(owner_monitor)
    assert media.binding(db, personal, item_id=own_item).legacy_id == str(member_monitor)
    with pytest.raises(hh.HouseholdError):
        views.detail(db, member, item_id=own_item)
    assert media.managed_removal_guard(
        db, operational_owner_id=1, domain="anime", canonical_id="100", season=1
    ) == {"has_shared_references": True, "blocked": True}


def test_explicit_promotion_changes_personal_scope_without_auto_sharing_history(db):
    from omakase.plus.product_views import library_titles

    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(1,101,'Move this media',1)"
    ).lastrowid
    db.commit()
    preview = hh.promotion_preview(db, user_id=1)
    lib = hh.create_household(
        db, user_id=1, name="Shared Home", promote=True, preview_digest=preview["digest"]
    )
    personal_id = db.execute(
        "SELECT library_id FROM personal_libraries WHERE user_id=1"
    ).fetchone()[0]
    personal = hh.resolve_scope(db, viewer_id=1, library_id=personal_id)
    shared_scope = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    assert library_titles(db, user_id=1, library_scope=personal) == []
    assert len(library_titles(db, user_id=1, library_scope=shared_scope)) == 1
    assert not media.personal_legacy_visible(
        db, personal, domain="anime", canonical_id="101", season=1
    )
    hh.LibraryRepository(db, personal).add(
        domain="anime",
        canonical_id="101",
        season=1,
        title="Keep personally too",
        legacy_id=str(monitor),
    )
    assert len(library_titles(db, user_id=1, library_scope=personal)) == 1
    assert media.personal_legacy_visible(db, personal, domain="anime", canonical_id="101", season=1)
    with pytest.raises(hh.HouseholdError):
        library_titles(db, user_id=2, library_scope=shared_scope)


def test_tv_episodes_use_viewer_watch_flags_not_owner_presence_or_season_completion(db):
    lib = shared(db)
    owner, member = scopes(db, lib)
    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(1,55,'{\"title\":\"TV\"}','now')"
    )
    for number, air in [(1, "2000-01-01"), (2, None), (3, "2999-01-01")]:
        db.execute(
            "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(1,55,1,?,?,1,'plex',1,'now')",
            (
                number,
                json.dumps({"name": f"Episode {number}", "air_date": air, "owner_rating": 10}),
            ),
        )
    db.commit()
    item = hh.LibraryRepository(db, owner).add(
        domain="tv", canonical_id="55", title="TV", legacy_id="55"
    )
    bound = media.binding(db, member, item_id=item)
    before = db.total_changes
    assert [episode["watched"] for episode in media.tv_episode_rows(db, bound, season=1)] == [
        None,
        None,
        None,
    ]
    assert db.total_changes == before
    media.save_tv_episode(db, bound, season=1, episode=1, watched=True)
    row = db.execute(
        "SELECT watched,plex_present,metadata_json FROM tv_episodes WHERE user_id=2"
    ).fetchone()
    assert tuple(row[:2]) == (1, 0) and "owner_rating" not in row[2]
    assert media.viewer_history(db, bound)["status"] == "unwatched"
    assert media.viewer_history(db, bound)["rating"] is None
    for episode in (2, 3):
        with pytest.raises(hh.HouseholdError):
            media.save_tv_episode(db, bound, season=1, episode=episode, watched=True)
    assert db.execute("SELECT COUNT(*) FROM tv_episodes WHERE user_id=2").fetchone()[0] == 1
