"""Local list removal never changes a tracker or a provider."""

import sqlite3

import pytest

from omakase.plus import households
from omakase.plus.db import run_migrations
from omakase.plus.local_list import local_removed, monitoring_excluded, set_local_removed
from omakase.plus.media_store import list_due_monitors, upsert_monitor
from omakase.plus.product_views import library_titles


@pytest.fixture
def data():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    run_migrations(db)
    for uid in [1, 2]:
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (uid, f"{uid}@example.invalid", "fixture"),
        )
        households.ensure_personal_library(db, user_id=uid)
    mid = upsert_monitor(
        db,
        user_id=1,
        anilist_id=100,
        title="Unaired fixture",
        season_number=1,
        release_status="NOT_YET_RELEASED",
        active=True,
    )
    db.commit()
    yield db, mid
    db.close()


def test_zero_asset_remove_restore_is_local_and_survives_import_and_binding_refresh(data):
    db, mid = data
    original = dict(db.execute("SELECT * FROM media_monitors WHERE id=?", (mid,)).fetchone())
    set_local_removed(db, user_id=1, monitor_id=mid, removed=True)
    assert local_removed(db, user_id=1, anilist_id=100, season=1)
    assert monitoring_excluded(db, mid)
    assert library_titles(db, user_id=1) == []
    assert list_due_monitors(db, now="2026-11-01T00:00:00Z") == []
    assert (
        dict(db.execute("SELECT * FROM media_monitors WHERE id=?", (mid,)).fetchone()) == original
    )
    upsert_monitor(
        db, user_id=1, anilist_id=100, title="Imported fixture", season_number=1, active=True
    )
    scope = households.resolve_scope(
        db, viewer_id=1, library_id=households.ensure_personal_library(db, user_id=1)
    )
    households.LibraryRepository(db, scope).add(
        domain="anime", canonical_id="100", season=1, title="Catalog import", legacy_id=mid
    )
    assert local_removed(db, user_id=1, anilist_id=100, season=1)
    assert monitoring_excluded(db, mid)
    set_local_removed(db, user_id=1, monitor_id=mid, removed=True)
    set_local_removed(db, user_id=1, monitor_id=mid, removed=False)
    assert not monitoring_excluded(db, mid)
    assert len(library_titles(db, user_id=1)) == 1
    assert len(list_due_monitors(db, now="2026-11-01T00:00:00Z")) == 1
    assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM tracker_media_actions").fetchone()[0] == 0


def test_other_owner_cannot_remove_and_other_library_tracking_is_preserved(data):
    db, mid = data
    with pytest.raises(households.HouseholdError):
        set_local_removed(db, user_id=2, monitor_id=mid, removed=True)
    shared = households.create_household(db, user_id=1, name="Fixture shared")
    scope = households.resolve_scope(db, viewer_id=1, library_id=shared)
    item = households.LibraryRepository(db, scope).add(
        domain="anime", canonical_id="100", season=1, title="Shared", legacy_id=mid
    )
    set_local_removed(db, user_id=1, monitor_id=mid, removed=True)
    assert local_removed(db, user_id=1, anilist_id=100, season=1)
    assert not monitoring_excluded(db, mid)
    assert households.LibraryRepository(db, scope).item(item)["state"] == "active"
    assert len(list_due_monitors(db, now="2026-11-01T00:00:00Z")) == 1


def test_local_list_route_csrf_ownership_and_zero_asset_review(data, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.requests import Request

    from omakase.plus import routes
    from omakase.plus.deps import get_db
    from omakase.plus.web_security import csrf_token

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    db, mid = data
    app = FastAPI()
    app.include_router(routes.router)
    actor = [1]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[routes.require_user] = lambda: SimpleNamespace(
        id=actor[0], email="fixture@example.invalid"
    )
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set("omakase_session", "fixture")
        req = Request({"type": "http", "headers": [(b"cookie", b"omakase_session=fixture")]})
        token = csrf_token(req)
        review = client.get(f"/plus/library/{mid}/remove")
        assert review.status_code == 200
        assert "Remove from Omakase list" in review.text
        assert 'name="confirmation"' not in review.text
        assert client.post(f"/plus/library/{mid}/local-list/remove").status_code == 403
        result = client.post(
            f"/plus/library/{mid}/local-list/remove",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert result.status_code == 303
        assert local_removed(db, user_id=1, anilist_id=100, season=1)
        actor[0] = 2
        assert (
            client.post(
                f"/plus/library/{mid}/local-list/restore",
                data={"csrf_token": token},
                follow_redirects=False,
            ).status_code
            == 404
        )
        actor[0] = 1
        assert (
            client.post(
                f"/plus/library/{mid}/local-list/restore",
                data={"csrf_token": token},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert not local_removed(db, user_id=1, anilist_id=100, season=1)


def test_existing_assets_tracker_history_and_monitor_schedule_survive_local_remove():
    from omakase.plus.media_sync import _monitor_withdrawn, _selected_monitors
    from tests.plus.test_title_removal import _seed_title

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    run_migrations(db)
    user, mid, asset = _seed_title(db)
    db.execute(
        "INSERT INTO watch_entries(user_id,monitor_id,anilist_id,status,progress_episodes,episode_total,last_source) VALUES(?,?,151807,'watching',2,12,'manual')",
        (user, mid),
    )
    db.commit()
    names = [
        "anilist_plannings",
        "watch_entries",
        "watch_events",
        "media_assets",
        "media_file_probes",
        "media_sync_runs",
        "media_monitors",
    ]
    before = {name: [tuple(row) for row in db.execute("SELECT * FROM " + name)] for name in names}
    set_local_removed(db, user_id=user, monitor_id=mid, removed=True)
    assert _monitor_withdrawn(db, mid)
    assert (
        _selected_monitors(
            db, now="2026-11-01T00:00:00Z", monitor_ids=[mid], include_finished_inactive=True
        )
        == []
    )
    assert {
        name: [tuple(row) for row in db.execute("SELECT * FROM " + name)] for name in names
    } == before
    set_local_removed(db, user_id=user, monitor_id=mid, removed=False)
    assert not _monitor_withdrawn(db, mid)
    assert {
        name: [tuple(row) for row in db.execute("SELECT * FROM " + name)] for name in names
    } == before
    db.close()


def test_language_summary_uses_only_saved_tracks_and_keeps_missing_evidence_unknown():
    from omakase.plus.product_views import cached_title_languages

    assert cached_title_languages({}) == {
        "audio_languages": ["Unknown"],
        "subtitle_languages": ["Unknown"],
    }
    title = {
        "current_assets": [
            {
                "probe": {"audio": [{"language": "jpn"}, {"language": "eng"}]},
                "subtitles": [{"language": "eng"}],
            }
        ]
    }
    assert cached_title_languages(title) == {
        "audio_languages": ["English", "Japanese"],
        "subtitle_languages": ["English"],
    }
    title["current_assets"].append({"source": "Japanese English release title", "subtitles": []})
    assert "Unknown" in cached_title_languages(title)["audio_languages"]
    assert "Unknown" in cached_title_languages(title)["subtitle_languages"]


def test_explicit_other_library_cannot_be_used_to_remove_or_restore(data):
    db, mid = data
    other = households.ensure_personal_library(db, user_id=2)
    with pytest.raises(households.HouseholdError):
        set_local_removed(db, user_id=1, monitor_id=mid, removed=True, library_id=other)
    assert not local_removed(db, user_id=1, anilist_id=100, season=1)
    shared = households.create_household(db, user_id=1, name="Separate membership")
    with pytest.raises(households.HouseholdError):
        set_local_removed(db, user_id=1, monitor_id=mid, removed=True, library_id=shared)
    assert not local_removed(db, user_id=1, anilist_id=100, season=1)


def test_real_inflight_local_removal_catch_preserves_schedule_then_restore(data, monkeypatch):
    import asyncio

    from omakase.plus import media_sync
    from tests.plus.test_media_sync import _rd

    db, mid = data
    db.execute(
        "UPDATE media_monitors SET release_status='FINISHED',expected_episodes=2,aired_episodes=2,active=1,next_check_at='2026-11-01T01:00:00Z' WHERE id=?",
        (mid,),
    )
    db.commit()
    original = dict(db.execute("SELECT * FROM media_monitors WHERE id=?", (mid,)).fetchone())
    rd = _rd()

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked_search(*args, **kwargs):
            started.set()
            await release.wait()
            return []

        monkeypatch.setattr(media_sync, "search_alias_windows", blocked_search)
        task = asyncio.create_task(
            media_sync.reconcile_monitor(db, monitor_id=mid, rd=rd, now="2026-11-01T00:00:00Z")
        )
        await asyncio.wait_for(started.wait(), 1)
        set_local_removed(db, user_id=1, monitor_id=mid, removed=True)
        release.set()
        return await asyncio.wait_for(task, 1)

    result = asyncio.run(scenario())
    media_sync._schedule_monitor_failure(
        db, monitor_id=mid, now="2026-11-01T00:00:00Z", detail="Already excluded"
    )
    media_sync.schedule_monitor(db, monitor_id=mid, now="2026-11-01T00:00:00Z", sync_result=result)
    assert result.outcome == "withdrawn"
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    assert (
        dict(db.execute("SELECT * FROM media_monitors WHERE id=?", (mid,)).fetchone()) == original
    )
    set_local_removed(db, user_id=1, monitor_id=mid, removed=False)
    assert [row["id"] for row in list_due_monitors(db, now="2026-11-01T02:00:00Z")] == [mid]
    assert (
        dict(db.execute("SELECT * FROM media_monitors WHERE id=?", (mid,)).fetchone()) == original
    )


def test_physical_withdrawal_still_deactivates_even_with_local_exclusion(data):
    from omakase.plus.media_sync import _keep_withdrawn_inactive

    db, mid = data
    db.execute(
        "UPDATE media_monitors SET active=1,next_check_at='2026-11-01T01:00:00Z' WHERE id=?", (mid,)
    )
    db.commit()
    set_local_removed(db, user_id=1, monitor_id=mid, removed=True)
    db.execute(
        "INSERT INTO completion_plex_cleanup_blocks(monitor_id,user_id,requested_at,detail) VALUES(?,1,'2026-11-01T00:00:00Z','Fixture physical withdrawal')",
        (mid,),
    )
    assert _keep_withdrawn_inactive(db, mid)
    row = db.execute(
        "SELECT active,next_check_at FROM media_monitors WHERE id=?", (mid,)
    ).fetchone()
    assert tuple(row) == (0, None)
