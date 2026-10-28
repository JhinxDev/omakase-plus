"""Three-account, offline household core contracts; no provider/network effects."""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from omakase.plus import households as hh
from omakase.plus.db import run_migrations


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    for user in (1, 2, 3):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"person{user}@example.invalid", "synthetic"),
        )
    conn.commit()
    yield conn
    conn.close()


def scope(db, library, user=1):
    return hh.resolve_scope(db, viewer_id=user, library_id=library)


def shared(db, *, requests=False):
    library = hh.create_household(db, user_id=1, name="Home library", acquisition_requests=requests)
    invite = hh.create_invite(db, scope(db, library), now=1000)
    hh.accept_invite(db, viewer_id=2, token=invite["token"], now=1001)
    return library


def monitor(db, user=1, anilist=100, season=1, active=1):
    return db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number,active) VALUES(?,?,?,?,?)",
        (user, anilist, "A shared title", season, active),
    ).lastrowid


def test_invite_hash_only_single_use_expiry_and_no_auto_sharing(db):
    assert hh.overview(db, viewer_id=2) == {"household": None}
    library = hh.create_household(db, user_id=1, name="Home")
    invite = hh.create_invite(db, scope(db, library), now=1000, ttl=60)
    assert invite["token"] not in "\n".join(db.iterdump())
    assert (
        db.execute("SELECT token_digest FROM household_invites").fetchone()[0]
        == hashlib.sha256(invite["token"].encode()).hexdigest()
    )
    with pytest.raises(hh.HouseholdError):
        hh.accept_invite(db, viewer_id=2, token=invite["token"], now=1060)
    assert hh.accept_invite(db, viewer_id=2, token=invite["token"], now=1059) == library
    with pytest.raises(hh.HouseholdError):
        hh.accept_invite(db, viewer_id=3, token=invite["token"], now=1059)
    assert hh.overview(db, viewer_id=3) == {"household": None}


def test_revoked_invite_and_owner_last_membership_guard(db):
    library = hh.create_household(db, user_id=1, name="Home")
    invite = hh.create_invite(db, scope(db, library), now=1000)
    hh.revoke_invite(db, scope(db, library), invite["id"])
    with pytest.raises(hh.HouseholdError):
        hh.accept_invite(db, viewer_id=2, token=invite["token"], now=1001)
    with pytest.raises(hh.HouseholdError):
        hh.leave_or_revoke(db, scope(db, library))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM users WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM household_memberships WHERE user_id=1")


def test_same_title_separate_membership_reuses_resource_without_exposing_personal_catalog(db):
    legacy = monitor(db)
    db.commit()
    preview = hh.promotion_preview(db, user_id=1)
    library = hh.create_household(
        db, user_id=1, name="Home", promote=True, preview_digest=preview["digest"]
    )
    personal = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[0]
    original = hh.LibraryRepository(db, scope(db, library)).items()[0]
    own = hh.LibraryRepository(db, scope(db, personal)).add(
        domain="anime", canonical_id="100", season=1, title="Personal title", legacy_id=str(legacy)
    )
    assert db.execute("SELECT COUNT(*) FROM library_resources").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 2
    assert (
        db.execute("SELECT resource_id FROM library_items WHERE id=?", (own,)).fetchone()[0]
        == db.execute(
            "SELECT resource_id FROM library_items WHERE id=?", (original["id"],)
        ).fetchone()[0]
    )
    assert hh.LibraryRepository(db, scope(db, library)).removal_blocked(original["id"])
    hh.LibraryRepository(db, scope(db, personal)).add(
        domain="anime", canonical_id="200", title="Private only"
    )
    invite = hh.create_invite(db, scope(db, library), now=1000)
    hh.accept_invite(db, viewer_id=2, token=invite["token"], now=1001)
    assert [i["title"] for i in hh.LibraryRepository(db, scope(db, library, 2)).items()] == [
        "A shared title"
    ]
    with pytest.raises(hh.HouseholdError):
        scope(db, personal, 2)
    with pytest.raises(hh.HouseholdError):
        scope(db, library, 3)


def test_promotion_preview_is_actual_inventory_not_history_only_and_digest_is_rechecked(db):
    monitor(db)
    monitor(db, anilist=101, active=0)
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,title,updated_at) VALUES(1,'tt1234567','History-only movie','now')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,created_at,updated_at) VALUES(1,'tt1234567',10,'completed','now','now')"
    )
    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(1,55,?, 'now')",
        (json.dumps({"name": "History-only TV"}),),
    )
    db.execute(
        "INSERT INTO tv_history(user_id,tmdb_id,status,updated_at) VALUES(1,55,'completed','now')"
    )
    db.commit()
    preview = hh.promotion_preview(db, user_id=1)
    assert preview["count"] == 1 and preview["items"][0]["canonical_id"] == "100"
    assert "History-only" not in str(preview)
    monitor(db, anilist=102)
    db.commit()
    with pytest.raises(hh.HouseholdError, match="inventory changed"):
        hh.create_household(
            db, user_id=1, name="Home", promote=True, preview_digest=preview["digest"]
        )
    assert hh.overview(db, viewer_id=1) == {"household": None}


def test_member_personal_progress_and_rating_never_copy_owner_history(db):
    legacy = monitor(db)
    db.execute(
        "INSERT INTO watch_entries(user_id,monitor_id,anilist_id,season_number,status,progress_episodes,completed_at) VALUES(1,?,100,1,'completed',12,'2026-10-28T01:00:00Z')",
        (legacy,),
    )
    db.commit()
    preview = hh.promotion_preview(db, user_id=1)
    library = hh.create_household(
        db, user_id=1, name="Home", promote=True, preview_digest=preview["digest"]
    )
    invite = hh.create_invite(db, scope(db, library), now=1000)
    hh.accept_invite(db, viewer_id=2, token=invite["token"], now=1001)
    item = hh.LibraryRepository(db, scope(db, library, 2)).items()[0]
    entry = hh.start_anime_tracking(db, scope(db, library, 2), item_id=item["id"])
    before = hh.personal_anime_history(db, viewer_id=2)[0]
    assert (
        before["status"] == "unwatched"
        and before["progress_episodes"] == 0
        and before["rating"] is None
    )
    assert (
        db.execute("SELECT monitor_id FROM watch_entries WHERE id=?", (entry,)).fetchone()[0]
        is None
    )
    db.execute(
        "UPDATE watch_entries SET status='watching',progress_episodes=3 WHERE id=?", (entry,)
    )
    db.commit()
    assert hh.LibraryRepository(db, scope(db, library)).removal_blocked(item["id"]) is True
    owner = hh.personal_anime_history(db, viewer_id=1)[0]
    assert owner["status"] == "completed" and owner["progress_episodes"] == 12
    hh.leave_or_revoke(db, scope(db, library, 2))
    assert hh.personal_anime_history(db, viewer_id=2)[0]["progress_episodes"] == 3
    with pytest.raises(hh.HouseholdError):
        hh.LibraryRepository(db, scope(db, library, 2)).items()


def test_revocation_cancels_queued_only_dispatched_work_retained_and_stale_scope_denied(db):
    library = shared(db, requests=True)
    member = scope(db, library, 2)
    repo = hh.LibraryRepository(db, member)
    item = repo.add(domain="movie", canonical_id="tt1234567", title="Shared movie")
    queued = repo.queue_request(item, idempotency_key="queued")
    dispatched = repo.queue_request(item, idempotency_key="sent")
    assert repo.queue_request(item, idempotency_key="queued") == queued
    authority = hh.authorize_dispatch(db, event_id=dispatched)
    assert authority["actor_id"] == 2 and authority["operational_owner_id"] == 1
    hh.leave_or_revoke(db, scope(db, library), member_id=2)
    assert (
        db.execute("SELECT state FROM library_request_events WHERE id=?", (queued,)).fetchone()[0]
        == "cancelled"
    )
    assert (
        db.execute("SELECT state FROM library_request_events WHERE id=?", (dispatched,)).fetchone()[
            0
        ]
        == "dispatched"
    )
    with pytest.raises(hh.HouseholdError):
        repo.items()
    with pytest.raises(hh.HouseholdError):
        hh.authorize_dispatch(db, event_id=queued)


def test_member_keep_and_cross_library_references_protect_removal_without_private_details(db):
    library = shared(db)
    item = hh.LibraryRepository(db, scope(db, library, 2)).add(
        domain="anime", canonical_id="100", title="Shared anime"
    )
    hh.LibraryRepository(db, scope(db, library, 2)).set_interest(item, keep=True)
    assert hh.LibraryRepository(db, scope(db, library)).removal_blocked(item) is True
    with pytest.raises(hh.HouseholdError):
        hh.LibraryRepository(db, scope(db, library, 2)).removal_blocked(item)
    hh.leave_or_revoke(db, scope(db, library, 2))
    assert hh.LibraryRepository(db, scope(db, library)).removal_blocked(item) is False
    assert db.execute("SELECT keep_available FROM library_item_interest").fetchone()[0] == 1


def test_owner_only_connector_status_never_exposes_config_and_generation_invalidates_work(db):
    library = shared(db, requests=True)
    db.execute(
        "INSERT INTO user_connection_health VALUES(1,'plex','synthetic',?,'now')",
        (json.dumps({"state": "connected", "detail": "NEVER-EXPOSE-OWNER-ADDRESS"}),),
    )
    db.commit()
    hh.bind_connector(db, scope(db, library), integration="plex")
    assert hh.connector_status(db, scope(db, library, 2)) == [
        {"integration": "plex", "state": "connected"}
    ]
    assert "NEVER-EXPOSE" not in str(hh.connector_status(db, scope(db, library, 2)))
    with pytest.raises(hh.HouseholdError):
        hh.bind_connector(db, scope(db, library, 2), integration="riven")
    repo = hh.LibraryRepository(db, scope(db, library, 2))
    item = repo.add(domain="movie", canonical_id="tt1234567", title="Movie")
    event = repo.queue_request(item, idempotency_key="generation")
    hh.bind_connector(db, scope(db, library), integration="plex")
    with pytest.raises(hh.HouseholdError):
        hh.authorize_dispatch(db, event_id=event)


def test_scope_cannot_be_forged_or_tv_acquisition_invented(db):
    from dataclasses import replace

    library = shared(db, requests=True)
    good = scope(db, library, 2)
    with pytest.raises(hh.HouseholdError):
        hh.revalidate_scope(db, replace(good, role="owner"), owner=True)
    repo = hh.LibraryRepository(db, good)
    item = repo.add(domain="tv", canonical_id="45", title="TV")
    with pytest.raises(hh.HouseholdError, match="TV acquisition"):
        repo.queue_request(item, idempotency_key="tv")


def test_migration_keeps_existing_rows_and_is_idempotent(tmp_path, monkeypatch):
    import shutil

    from omakase.plus import db as manager

    earlier = tmp_path / "earlier"
    earlier.mkdir()
    original = manager._MIGRATIONS_DIR
    through033 = tmp_path / "through033"
    through033.mkdir()
    for source in original.glob("*.sql"):
        if int(source.name.split("-", 1)[0]) <= 33:
            shutil.copy2(source, through033 / source.name)
        if int(source.name.split("-", 1)[0]) < 33:
            shutil.copy2(source, earlier / source.name)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(manager, "_MIGRATIONS_DIR", earlier)
    manager.run_migrations(conn)
    conn.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(2,'preserved@example.invalid','synthetic')"
    )
    conn.commit()
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='_migrations'"
        )
    ]
    before = {
        name: [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        for name in names
    }
    monkeypatch.setattr(manager, "_MIGRATIONS_DIR", through033)
    manager.run_migrations(conn)
    manager.run_migrations(conn)
    assert before == {
        name: [tuple(r) for r in conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
        for name in names
    }
    assert conn.execute("SELECT count(*) FROM household_memberships").fetchone()[0] == 0
    assert (
        conn.execute("SELECT count(*) FROM personal_libraries WHERE user_id=2").fetchone()[0] == 1
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM _migrations WHERE name='033-household-libraries.sql'"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_explicit_personal_catalog_membership_is_listed_in_promotion_preview(db):
    with db:
        personal = hh.ensure_personal_library(db, user_id=1)
    item = hh.LibraryRepository(db, scope(db, personal)).add(
        domain="anime", canonical_id="555", title="Explicit catalog addition", season=2
    )
    preview = hh.promotion_preview(db, user_id=1)
    assert preview["items"][0]["existing_item"] == item
    library = hh.create_household(
        db, user_id=1, name="Home", promote=True, preview_digest=preview["digest"]
    )
    assert hh.selected_scope(db, viewer_id=1).library_id == library
    assert hh.LibraryRepository(db, scope(db, library)).items()[0]["id"] == item


def test_independent_personal_ratings_survive_revoke(db):
    library = shared(db)
    item = hh.LibraryRepository(db, scope(db, library)).add(
        domain="anime", canonical_id="100", title="Rating example", season=1
    )
    entries = {
        user: hh.start_anime_tracking(db, scope(db, library, user), item_id=item) for user in (1, 2)
    }
    for user, rating in ((1, 9), (2, 7)):
        db.execute(
            "UPDATE watch_entries SET status='completed',completed_at='2026-10-28T01:00:00Z',progress_episodes=12 WHERE id=?",
            (entries[user],),
        )
        db.execute(
            "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,disposition,observed_at,score_ten) VALUES(?,?,'manual',?,'completed','accepted','2026-10-28T01:00:00Z',?)",
            (entries[user], user, f"rating-{user}", rating),
        )
    db.commit()
    assert hh.personal_anime_history(db, viewer_id=1)[0]["rating"] == 9
    assert hh.personal_anime_history(db, viewer_id=2)[0]["rating"] == 7
    assert hh.personal_anime_history(db, viewer_id=3) == []
    hh.leave_or_revoke(db, scope(db, library), member_id=2)
    assert hh.personal_anime_history(db, viewer_id=2)[0]["rating"] == 7


def test_two_accounts_cannot_consume_one_invite_concurrently(db, tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    library = hh.create_household(db, user_id=1, name="Concurrent home")
    invite = hh.create_invite(db, scope(db, library), now=1000)
    filename = tmp_path / "concurrent.sqlite"
    target = sqlite3.connect(filename)
    db.backup(target)
    target.close()

    def accept(user):
        connection = sqlite3.connect(filename, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            hh.accept_invite(connection, viewer_id=user, token=invite["token"], now=1001)
            return "joined"
        except hh.HouseholdError:
            return "blocked"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, (2, 3)))
    assert sorted(results) == ["blocked", "joined"]
    check = sqlite3.connect(filename)
    assert (
        check.execute("SELECT count(*) FROM household_memberships WHERE state='active'").fetchone()[
            0
        ]
        == 2
    )
    check.close()


def test_private_account_deletion_is_not_blocked_by_empty_personal_scope(db):
    with db:
        hh.ensure_personal_library(db, user_id=3)
    db.execute("DELETE FROM users WHERE id=3")
    db.commit()
    assert db.execute("SELECT count(*) FROM personal_libraries WHERE user_id=3").fetchone()[0] == 0


def test_member_cannot_bind_foreign_operational_resource_or_watch_entry(db):
    legacy = monitor(db)
    db.commit()
    library = shared(db)
    with pytest.raises(hh.HouseholdError):
        hh.LibraryRepository(db, scope(db, library, 2)).add(
            domain="anime", canonical_id="100", season=1, title="Forged", legacy_id=str(legacy)
        )
    item = hh.LibraryRepository(db, scope(db, library)).add(
        domain="anime", canonical_id="100", season=1, title="Owned"
    )
    owner_entry = hh.start_anime_tracking(db, scope(db, library), item_id=item)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO watch_library_links(user_id,library_item_id,watch_entry_id) VALUES(2,?,?)",
            (item, owner_entry),
        )


def test_movie_tv_public_metadata_overlay_preserves_each_viewers_history(db):
    library = shared(db)
    db.execute(
        "INSERT INTO movie_catalog(user_id,imdb_id,tmdb_id,title,updated_at) VALUES(1,'tt1234567',88,'Shared movie','now')"
    )
    db.execute(
        "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,rating,created_at,updated_at) VALUES(1,'tt1234567',88,'completed',9,'now','now')"
    )
    db.execute(
        "INSERT INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(1,45,?,'now')",
        (
            json.dumps(
                {
                    "id": 45,
                    "name": "Shared TV",
                    "poster_path": "/poster.jpg",
                    "watched": True,
                    "private_source": "NEVER-COPY",
                }
            ),
        ),
    )
    db.execute(
        "INSERT INTO tv_history(user_id,tmdb_id,status,rating,updated_at) VALUES(1,45,'completed',8,'now')"
    )
    db.execute(
        "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(1,45,1,1,'{}',1,'manual',1,'now')"
    )
    db.execute(
        "UPDATE movie_catalog SET poster_url='https://untrusted.invalid/PRIVATE-ADDRESS' WHERE user_id=1"
    )
    db.commit()
    owner = hh.LibraryRepository(db, scope(db, library))
    movie = owner.add(
        domain="movie", canonical_id="tt1234567", title="Shared movie", legacy_id="tt1234567"
    )
    tv = owner.add(domain="tv", canonical_id="45", title="Shared TV", legacy_id="45")
    member = hh.PersonalTrackingRepository(db, viewer_id=2)
    member.start(scope(db, library, 2), item_id=movie)
    member.start(scope(db, library, 2), item_id=tv)
    assert member.history("movie") == [
        {"imdb_id": "tt1234567", "status": "unwatched", "rating": None}
    ]
    assert member.history("tv") == [{"tmdb_id": 45, "status": "unwatched", "rating": None}]
    assert hh.PersonalTrackingRepository(db, viewer_id=1).history("movie")[0]["rating"] == 9
    assert hh.PersonalTrackingRepository(db, viewer_id=1).history("tv")[0]["rating"] == 8
    assert db.execute("SELECT poster_url FROM movie_catalog WHERE user_id=2").fetchone()[0] is None
    copied = db.execute("SELECT metadata_json FROM tv_catalog WHERE user_id=2").fetchone()[0]
    assert "NEVER-COPY" not in copied and "watched" not in copied
    assert db.execute("SELECT count(*) FROM tv_episodes WHERE user_id=2").fetchone()[0] == 0
    db.execute("UPDATE movie_history SET status='watching',rating=7 WHERE user_id=2")
    db.commit()
    member.start(scope(db, library, 2), item_id=movie)
    assert member.history("movie")[0]["rating"] == 7
    assert hh.PersonalTrackingRepository(db, viewer_id=3).history("movie") == []
    with pytest.raises(hh.HouseholdError):
        hh.PersonalTrackingRepository(db, viewer_id=3).start(scope(db, library, 2), item_id=movie)
    hh.leave_or_revoke(db, scope(db, library, 2))
    assert member.history("movie")[0]["rating"] == 7
    with pytest.raises(hh.HouseholdError):
        member.start(scope(db, library, 2), item_id=movie)


def test_actual_connection_writer_hook_invalidates_queued_shared_authority(db):
    library = shared(db, requests=True)
    hh.bind_connector(db, scope(db, library), integration="riven")
    member = hh.LibraryRepository(db, scope(db, library, 2))
    item = member.add(domain="movie", canonical_id="tt1234567", title="Movie")
    queued = member.queue_request(item, idempotency_key="before-key-change")
    scopes = hh.owner_connection_scopes(db, owner_user_id=1, integration="riven")
    assert {s["kind"] for s in scopes} == {"personal", "household"}
    old = scope(db, library, 2)
    assert hh.connection_generation_changed(db, owner_user_id=1, integration="riven") == [library]
    assert (
        db.execute("SELECT state FROM library_request_events WHERE id=?", (queued,)).fetchone()[0]
        == "cancelled"
    )
    with pytest.raises(hh.HouseholdError):
        hh.revalidate_scope(db, old)
    assert hh.connection_generation_changed(db, owner_user_id=3, integration="riven") == []
