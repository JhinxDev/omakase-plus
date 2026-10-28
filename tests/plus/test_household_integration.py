"""Default-gated main routes, real viewer navigation, connector and removal fences."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from omakase.plus import household_media as media
from omakase.plus import households as hh
from omakase.plus.deps import get_db
from omakase.plus.watch.models import WatchEvent
from tests.plus.test_household_media_routes import client, fixture_app


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    monkeypatch.setattr(
        httpx.AsyncClient,
        "send",
        AsyncMock(side_effect=AssertionError("No provider in household integration tests")),
    )
    _, db, sessions, lib, items = fixture_app()
    from omakase.plus.routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    yield app, db, sessions, lib, items
    db.close()


def test_main_navigation_scope_switch_and_revoked_refresh_keep_actual_viewer(state):
    app, db, sessions, lib, items = state
    member, token = client(app, sessions[2])
    response = member.get("/plus/home")
    assert (
        response.status_code == 200
        and "Switch library" in response.text
        and "Shared Home" in response.text
    )
    assert (
        "person2@example.invalid" in response.text
        and "person1@example.invalid" not in response.text
    )
    personal = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=2").fetchone()[0]
    result = member.post(
        "/plus/settings/household/select",
        data={"csrf_token": token, "library_id": personal, "next": "/plus/library"},
    )
    assert result.status_code == 303 and result.headers["location"] == "/plus/library"
    assert hh.selected_scope(db, viewer_id=2).library_id == personal
    assert (
        member.post(
            "/plus/settings/household/select",
            data={"csrf_token": token, "library_id": lib, "next": "https://foreign.invalid/"},
        ).headers["location"]
        == "/plus/home"
    )
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    response = member.get("/plus/home")
    assert response.status_code == 200 and "Shared Home" not in response.text
    assert hh.selected_scope(db, viewer_id=2).library_id == personal
    assert member.get(f"/plus/libraries/{lib}").status_code == 404


def test_main_collection_monitor_url_and_art_links_are_selected_scope_checked(state):
    app, db, sessions, lib, items = state
    member, _ = client(app, sessions[2])
    before = db.total_changes
    result = member.get("/plus/library")
    assert (
        result.status_code == 200 and f"/plus/libraries/{lib}/items/{items['anime']}" in result.text
    )
    assert "Your rating 9" not in result.text
    monitor = db.execute("SELECT id FROM media_monitors WHERE user_id=1").fetchone()[0]
    result = member.get(f"/plus/library/{monitor}")
    assert result.status_code == 307 and result.headers["location"].endswith(items["anime"])
    assert member.get("/plus/library/999999").status_code == 404
    assert db.total_changes == before


def test_personal_history_retained_after_revoke_with_unknown_episode_art_and_no_media_access(state):
    from omakase.plus.product_views import activity_view
    from omakase.plus.viewing_history import viewing_history

    app, db, sessions, lib, items = state
    selected = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    bound = media.binding(db, selected, item_id=items["anime"])
    saved = media.save_anime_watch(
        db,
        bound,
        event=WatchEvent(
            source="manual",
            kind="completed",
            event_key="history-stays",
            episode=12,
            confidence="manual",
        ),
        score_ten=7,
    )
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    card = viewing_history(db, user_id=2)["items"][0]
    assert card["rating"] == 7 and card["title"].startswith("Code Geass:")
    assert (
        card["artwork_url"] is None
        and card["detail_url"] == f"/plus/history/anime/{saved.entry.id}"
    )
    assert activity_view(db, user_id=2)["watch_activity"][0]["title"].startswith("Code Geass:")
    member, token = client(app, sessions[2])
    result = member.get(card["detail_url"])
    assert result.status_code == 200 and "Your rating 7/10" in result.text
    assert "/artwork" not in result.text
    assert (
        member.post(
            card["detail_url"],
            data={
                "csrf_token": token,
                "kind": "completed",
                "episode": "12",
                "rating": "8",
                "event_key": "own-rerating",
            },
        ).status_code
        == 303
    )
    assert viewing_history(db, user_id=2)["items"][0]["rating"] == 8
    assert member.get(f"/plus/libraries/{lib}/items/{items['anime']}/artwork").status_code == 404
    outsider, _ = client(app, sessions[3])
    assert outsider.get(card["detail_url"]).status_code == 404


def test_removed_catalog_membership_keeps_files_history_and_claim_until_viewer_withdraws(state):
    from omakase.plus.title_removal import (
        TitleRemovalError,
        request_title_removal,
        title_removal_preview,
    )

    app, db, sessions, lib, items = state
    member_scope = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    repo = hh.LibraryRepository(db, member_scope)
    repo.set_interest(items["anime"], keep=True)
    monitor = db.execute("SELECT id FROM media_monitors WHERE user_id=1").fetchone()[0]
    assert title_removal_preview(db, user_id=1, monitor_id=monitor)["household_blocked"]
    with pytest.raises(TitleRemovalError, match="Shared media"):
        request_title_removal(
            db,
            user_id=1,
            monitor_id=monitor,
            confirmation="anything",
            requested_at="2026-10-28T10:00:00Z",
        )
    owner_client, owner_token = client(app, sessions[1])
    member, member_token = client(app, sessions[2])
    path = f"/plus/libraries/{lib}/items/{items['anime']}"
    assert (
        member.post(path + "/remove-membership", data={"csrf_token": member_token}).status_code
        == 403
    )
    assert (
        owner_client.post(path + "/remove-membership", data={"csrf_token": owner_token}).status_code
        == 303
    )
    assert "Removed from this library" in member.get(path).text
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM media_title_removals").fetchone()[0] == 0
    assert title_removal_preview(db, user_id=1, monitor_id=monitor)["household_blocked"]
    assert (
        member.post(path + "/withdraw-interest", data={"csrf_token": member_token}).status_code
        == 303
    )
    assert not title_removal_preview(db, user_id=1, monitor_id=monitor)["household_blocked"]


def test_owner_connection_changes_invalidate_generation_only_on_real_change(state):
    from omakase.plus import connections

    app, db, sessions, lib, items = state
    connections.save_connection(
        db, 1, "riven", url="https://service.example.invalid", api_key="fixture-b"
    )
    hh.bind_connector(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), integration="riven")
    previous = db.execute(
        "SELECT generation FROM library_settings WHERE library_id=? AND integration='riven'", (lib,)
    ).fetchone()[0]
    connections.save_connection(db, 1, "riven", url="", api_key="")
    assert (
        db.execute(
            "SELECT generation FROM library_settings WHERE library_id=? AND integration='riven'",
            (lib,),
        ).fetchone()[0]
        == previous
    )
    connections.save_connection(db, 1, "riven", api_key="fixture-c")
    assert (
        db.execute(
            "SELECT generation FROM library_settings WHERE library_id=? AND integration='riven'",
            (lib,),
        ).fetchone()[0]
        == previous + 1
    )
    connections.disconnect_connection(db, 1, "riven")
    assert (
        db.execute(
            "SELECT generation FROM library_settings WHERE library_id=? AND integration='riven'",
            (lib,),
        ).fetchone()[0]
        == previous + 2
    )
    connections.disconnect_connection(db, 1, "riven")
    assert (
        db.execute(
            "SELECT generation FROM library_settings WHERE library_id=? AND integration='riven'",
            (lib,),
        ).fetchone()[0]
        == previous + 2
    )


def test_default_off_and_navigation_failure_do_not_change_authentication(state, monkeypatch):
    app, db, sessions, lib, items = state
    member, _ = client(app, sessions[2])
    monkeypatch.setattr(
        hh,
        "navigation_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            sqlite3.OperationalError("synthetic missing optional metadata")
        ),
    )
    response = member.get("/plus/home")
    assert (
        response.status_code == 200
        and "Library selection is temporarily unavailable" in response.text
    )
    assert "person2@example.invalid" in response.text
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "false")
    response = member.get("/plus/home")
    assert response.status_code == 200 and "Switch library" not in response.text
    assert member.get(f"/plus/libraries/{lib}").status_code == 404


def test_removal_writer_fence_prevents_new_bind_or_keep_claim_before_and_after_commit(
    tmp_path, monkeypatch
):
    from omakase.plus.db import run_migrations
    from omakase.plus.title_removal import request_title_removal

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_HOUSEHOLDS_ENABLED", "true")
    path = tmp_path / "household-removal-fence.db"
    db = sqlite3.connect(path, timeout=0.03)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    db.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'fence@example.invalid','synthetic')"
    )
    db.commit()
    lib = hh.create_household(db, user_id=1, name="Empty Home")
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number) VALUES(1,200,'Sole private asset',1)"
    ).lastrowid
    db.execute(
        "INSERT INTO media_assets(monitor_id,torrent_hash,source_title,status) VALUES(?,'0123456789012345678901234567890123456789','Synthetic fixture','queued')",
        (monitor,),
    )
    db.commit()
    private = db.execute("SELECT library_id FROM personal_libraries WHERE user_id=1").fetchone()[0]
    private_scope = hh.resolve_scope(db, viewer_id=1, library_id=private)
    item = hh.LibraryRepository(db, private_scope).add(
        domain="anime",
        canonical_id="200",
        season=1,
        title="Sole private asset",
        legacy_id=str(monitor),
    )
    second = sqlite3.connect(path, timeout=0.03)
    second.row_factory = sqlite3.Row
    second.execute("PRAGMA foreign_keys=ON")
    try:
        receipt = request_title_removal(
            db,
            user_id=1,
            monitor_id=monitor,
            confirmation="Sole private asset",
            requested_at="2026-10-28T10:00:00Z",
            commit=False,
        )
        assert receipt["status"] == "awaiting_plex" and db.in_transaction
        other_scope = hh.resolve_scope(second, viewer_id=1, library_id=lib)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            hh.LibraryRepository(second, other_scope).add(
                domain="anime",
                canonical_id="200",
                season=1,
                title="Cannot race",
                legacy_id=str(monitor),
            )
        db.commit()
        with pytest.raises(hh.HouseholdError, match="Removal is in progress"):
            hh.LibraryRepository(second, other_scope).add(
                domain="anime",
                canonical_id="200",
                season=1,
                title="Cannot share",
                legacy_id=str(monitor),
            )
        with pytest.raises(hh.HouseholdError, match="Removal is in progress"):
            hh.LibraryRepository(
                second, hh.resolve_scope(second, viewer_id=1, library_id=private)
            ).set_interest(item, keep=True)
    finally:
        second.close()
        db.close()


if __name__ == "__main__":
    import argparse
    import os
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("--render-to", required=True)
    args = parser.parse_args()
    os.environ["OMAKASE_PLUS_PRIVATE"] = "true"
    os.environ["OMAKASE_PLUS_HOUSEHOLDS_ENABLED"] = "true"
    _, db, sessions, lib, items = fixture_app()
    from omakase.plus.routes import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    member, token = client(app, sessions[2])
    selected = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    saved = media.save_anime_watch(
        db,
        media.binding(db, selected, item_id=items["anime"]),
        event=WatchEvent(
            source="manual",
            kind="progress",
            episode=3,
            event_key="synthetic-browser",
            confidence="manual",
        ),
    )
    target = Path(args.render_to)
    target.mkdir(parents=True, exist_ok=True)
    for name, path in [("main-home", "/plus/home"), ("main-library", "/plus/library")]:
        result = member.get(path)
        assert result.status_code == 200
        (target / (name + ".html")).write_text(result.text, encoding="utf-8")
    hh.LibraryRepository(db, selected).set_interest(items["anime"], keep=True)
    hh.LibraryRepository(db, hh.resolve_scope(db, viewer_id=1, library_id=lib)).remove_membership(
        items["anime"]
    )
    result = member.get(f"/plus/libraries/{lib}/items/{items['anime']}")
    assert result.status_code == 200
    (target / "retired.html").write_text(result.text, encoding="utf-8")
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    result = member.get(f"/plus/history/anime/{saved.entry.id}")
    assert result.status_code == 200
    (target / "personal-history.html").write_text(result.text, encoding="utf-8")
    db.close()


def test_actual_shared_add_and_request_forms_queue_under_owner_without_history_import_or_provider(
    state, monkeypatch
):
    from omakase.plus import direct, language_store, routes
    from tests.plus.test_household_requests import target

    app, db, sessions, lib, items = state
    monkeypatch.setattr(direct, "resolve_direct_request", lambda *args, **kwargs: target())
    forbidden = AsyncMock(side_effect=AssertionError("No legacy sync in shared scope"))
    monkeypatch.setattr(routes, "run_media_sync", forbidden)
    member, token = client(app, sessions[2])
    data = {
        "csrf_token": token,
        "anilist_id": "200",
        "season": "1",
        "household_request_key": "a" * 32,
        "shared_intent": "catalog",
    }
    result = member.post("/plus/add/acquire", data=data)
    assert result.status_code == 303
    location = result.headers["location"]
    assert member.get(location).status_code == 200
    assert db.execute("SELECT COUNT(*) FROM media_monitors WHERE anilist_id=200").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM watch_entries WHERE anilist_id=200").fetchone()[0] == 0
    outsider, _ = client(app, sessions[3])
    assert outsider.get(location).status_code == 404
    hh.set_policy(
        db,
        hh.resolve_scope(db, viewer_id=1, library_id=lib),
        members_can_add=True,
        acquisition_requests=True,
    )
    data.update(shared_intent="request", household_request_key="b" * 32)
    assert member.post("/plus/add/acquire", data=data).status_code == 303
    assert member.post("/plus/add/acquire", data=data).status_code == 303
    row = db.execute("SELECT user_id,active FROM media_monitors WHERE anilist_id=200").fetchone()
    assert tuple(row) == (1, 0)
    request = db.execute("SELECT id,actor_user_id FROM language_request_policies").fetchone()
    assert request["actor_user_id"] == 2
    assert language_store.load_request(db, actor_user_id=2, request_id=request["id"])[2] == tuple(
        range(1, 13)
    )
    page = member.get(location)
    assert "My media request" in page.text and "no provider request was sent" in page.text
    assert forbidden.await_count == 0
    assert db.execute("SELECT COUNT(*) FROM anilist_plannings").fetchone()[0] == 0


def test_explicit_movie_check_uses_real_actor_and_rejects_legacy_ready_on_render(state):
    from pathlib import Path

    from fastapi.templating import Jinja2Templates

    from omakase.plus import language_store
    from omakase.plus.household_routes import create_household_router
    from omakase.plus.secrets import store_secret

    _, db, sessions, lib, items = state
    for key, value in [
        ("plex_url", "https://plex.fixture.invalid"),
        ("plex_token", "synthetic-owner-token"),
        ("plex_machine_id", "synthetic-server"),
        ("plex_movies_section_id", "1"),
    ]:
        store_secret(db, 1, key, value)
    hh.bind_connector(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), integration="plex")
    db.execute(
        "INSERT INTO movie_verifications(user_id,imdb_id,fingerprint,plex_key,state,code,detail,verified_at,expires_at) VALUES(1,'tt1234567',?,'41','verifying','pending','Synthetic lookup reference','2026-10-28T12:00:00Z','2026-10-28T12:15:00Z')",
        ("b" * 64,),
    )
    db.commit()
    calls = []

    class FakeProof:
        async def verify_language(self, actor_user_id, *, request_id, rating_key):
            snapshot, _, _ = language_store.load_request(
                db, actor_user_id=actor_user_id, request_id=request_id
            )
            calls.append((actor_user_id, snapshot.media_identity, rating_key))
            assert snapshot.library_id == lib and snapshot.library_item_id == items["movie"]
            return {"state": "verifying", "code": "personal_proof_required"}

        def language_status(self, actor_user_id, *, request_id):
            return {
                "state": "ready",
                "proof_schema": 1,
                "detail": "This old receipt must never become personal Ready",
                "plex_url": "https://app.plex.tv/desktop/#!/unsafe-old",
            }

    app = FastAPI()
    root = Path(__file__).resolve().parents[2] / "src/omakase/plus"
    templates = Jinja2Templates(directory=str(root / "templates"))
    templates.env.globals["asset_version"] = "synthetic-proof"

    def factory(db, user):
        assert user.id == 2
        return FakeProof()

    app.include_router(
        create_household_router(templates, enabled=True, movie_service_factory=factory),
        prefix="/plus",
    )
    app.dependency_overrides[get_db] = lambda: db
    member, token = client(app, sessions[2])
    outsider, _ = client(app, sessions[3])
    path = f"/plus/libraries/{lib}/items/{items['movie']}"
    assert member.post(path + "/verify-movie", data={"request_key": "p" * 32}).status_code == 403
    assert (
        outsider.post(
            path + "/verify-movie",
            data={"csrf_token": client(app, sessions[3])[1], "request_key": "p" * 32},
        ).status_code
        == 404
    )
    assert calls == []
    result = member.post(
        path + "/verify-movie", data={"csrf_token": token, "request_key": "p" * 32}
    )
    assert result.status_code == 303 and calls == [(2, "imdb:tt1234567", "41")]
    assert db.execute("SELECT COUNT(*) FROM movie_history WHERE user_id=2").fetchone()[0] == 0
    page = member.get(path)
    assert "Playback not yet verified" in page.text
    assert "Ready for your playback" not in page.text and "Open in Plex" not in page.text
    assert "synthetic-owner-token" not in page.text and "unsafe-old" not in page.text
