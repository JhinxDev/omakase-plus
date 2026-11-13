"""Actual legacy writers share bridge leases; every provider boundary is mocked."""

from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from omakase.plus import routes
from omakase.plus.db import get_db
from omakase.plus.secrets import store_secret
from omakase.plus.tracker import enqueue_completed
from omakase.plus.tracker_sync import BridgeError, writer_lease
from omakase.plus.watch import anilist_sync
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import ingest_watch_event


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_MASTER_KEY", "synthetic-writer-master")
    from omakase.plus import secrets

    monkeypatch.setattr(secrets, "_KEY", None)
    db = get_db(tmp_path)
    for user in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"writer{user}@example.test", "hash"),
        )
    db.commit()
    for user in (1, 2):
        store_secret(db, user, "anilist_oauth_token", f"synthetic-token-{user}")
    return db


def seed_watch(db, user=1):
    planning = db.execute(
        "INSERT INTO anilist_plannings(user_id,anilist_id,title,status) VALUES(?,11,'Synthetic anime','PLANNING')",
        (user,),
    ).lastrowid
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_planning_id,anilist_id,title,season_number,expected_episodes) VALUES(?,?,11,'Synthetic anime',1,12)",
        (user, planning),
    ).lastrowid
    db.commit()
    ingest_watch_event(
        db,
        user_id=user,
        monitor_id=monitor,
        event=WatchEvent.progress(source="plex", episode=3, event_key="synthetic-progress"),
        observed_at="2026-10-28T12:00:00Z",
    )
    return monitor


def history(db):
    return {
        name: [tuple(r) for r in db.execute("SELECT * FROM " + name + " ORDER BY id")]
        for name in ("watch_entries", "watch_events")
    }


@contextmanager
def token_context(db, user, *args):
    yield f"synthetic-token-{user}"


def action(db, user=1):
    return enqueue_completed(
        db, user_id=user, tracker="anilist", media_id=11, title="Synthetic anime", score_ten=8
    )


def test_old_direct_action_defers_without_any_provider_call_while_bridge_locked(db, monkeypatch):
    seed_watch(db)
    before = history(db)
    valid = Mock(side_effect=token_context)
    save = Mock(return_value={"id": 9, "status": "COMPLETED"})
    monkeypatch.setattr(routes, "with_valid_token", valid)
    monkeypatch.setattr(routes, "save_completed_entry", save)
    intent = action(db)
    with writer_lease(db, 1):
        result = routes._sync_completed_tracker_action(db, user_id=1, action=intent)
        assert result["tracker_status"] == "deferred"
    assert not valid.called and not save.called and history(db) == before
    result = routes._sync_completed_tracker_action(db, user_id=1, action=intent)
    assert result["tracker_status"] == "synced" and save.call_count == 1 and history(db) == before


def test_old_watch_outbox_defers_then_succeeds_after_bridge_lease_release(db, monkeypatch):
    seed_watch(db)
    before = history(db)
    read = Mock(return_value={"id": 9, "mediaId": 11, "status": "CURRENT", "progress": 0})
    save = Mock(return_value={"id": 9, "mediaId": 11, "status": "CURRENT", "progress": 3})
    monkeypatch.setattr(anilist_sync, "get_authenticated_media_entry", read)
    monkeypatch.setattr(anilist_sync, "save_progress_entry", save)
    with writer_lease(db, 1):
        result = anilist_sync.process_tracker_outbox(db, now="2026-10-28T12:01:00Z")
    assert result["failed"] == 1 and not read.called and not save.called and history(db) == before
    result = anilist_sync.process_tracker_outbox(db, now="2026-10-28T12:03:00Z")
    assert result["synced"] == 1 and save.call_count == 1 and history(db) == before


@pytest.mark.parametrize("boundary", ["dispatch", "ack"])
def test_direct_lease_expiry_never_dispatches_or_acknowledges_without_guard(
    db, monkeypatch, boundary
):
    monkeypatch.setattr(routes, "with_valid_token", token_context)
    save = Mock(return_value={"id": 9, "status": "COMPLETED"})
    monkeypatch.setattr(routes, "save_completed_entry", save)
    calls = []

    def require(*args, **kwargs):
        calls.append(1)
        if len(calls) == (2 if boundary == "dispatch" else 3):
            raise BridgeError("writer_lease_expired")

    monkeypatch.setattr(routes, "require_writer_lease", require)
    result = routes._sync_completed_tracker_action(db, user_id=1, action=action(db))
    assert result["tracker_status"] != "synced"
    assert save.call_count == (0 if boundary == "dispatch" else 1)
    assert db.execute("SELECT sync_state FROM tracker_media_actions").fetchone()[0] != "synced"


def test_other_personal_user_is_independent_of_held_bridge_writer(db, monkeypatch):
    monkeypatch.setattr(routes, "with_valid_token", token_context)
    save = Mock(return_value={"id": 9, "status": "COMPLETED"})
    monkeypatch.setattr(routes, "save_completed_entry", save)
    intent = action(db, 2)
    with writer_lease(db, 1):
        result = routes._sync_completed_tracker_action(db, user_id=2, action=intent)
    assert result["tracker_status"] == "synced"
    assert save.call_args.args[0] == "synthetic-token-2"


def test_best_effort_planning_also_skips_provider_when_bridge_writer_held(db, monkeypatch):
    valid = Mock(side_effect=token_context)
    ensure = Mock(return_value={"entry": {"status": "PLANNING"}})
    monkeypatch.setattr(routes, "with_valid_token", valid)
    monkeypatch.setattr(routes, "ensure_in_planning", ensure)
    with writer_lease(db, 1):
        assert routes._ensure_anilist_planning_best_effort(db, 1, 11) == "PLANNING"
    assert not valid.called and not ensure.called
    assert routes._ensure_anilist_planning_best_effort(db, 1, 11) == "PLANNING"
    assert ensure.call_count == 1


@pytest.mark.parametrize("boundary", ["dispatch", "ack"])
def test_watch_lease_expiry_before_dispatch_or_ack_preserves_history(db, monkeypatch, boundary):
    seed_watch(db)
    before = history(db)
    monkeypatch.setattr(
        anilist_sync,
        "get_authenticated_media_entry",
        lambda *args: {"id": 9, "mediaId": 11, "status": "CURRENT", "progress": 0},
    )
    save = Mock(return_value={"id": 9, "mediaId": 11, "status": "CURRENT", "progress": 3})
    monkeypatch.setattr(anilist_sync, "save_progress_entry", save)
    calls = []

    def require(*args, **kwargs):
        calls.append(1)
        if len(calls) == (2 if boundary == "dispatch" else 3):
            raise BridgeError("writer_lease_expired")

    monkeypatch.setattr(anilist_sync, "require_writer_lease", require)
    result = anilist_sync.process_tracker_outbox(db, now="2026-10-28T12:01:00Z")
    assert result["synced"] == 0 and save.call_count == (0 if boundary == "dispatch" else 1)
    assert result["conflict" if boundary == "ack" else "failed"] == 1 and history(db) == before


def test_local_credential_replacement_between_read_and_write_stops_old_bearer(db, monkeypatch):
    seed_watch(db)
    before = history(db)

    def changed(*args):
        store_secret(db, 1, "anilist_oauth_token", "synthetic-replacement")
        return {"id": 9, "mediaId": 11, "status": "CURRENT", "progress": 0}

    monkeypatch.setattr(anilist_sync, "get_authenticated_media_entry", changed)
    save = Mock()
    monkeypatch.setattr(anilist_sync, "save_progress_entry", save)
    result = anilist_sync.process_tracker_outbox(db, now="2026-10-28T12:01:00Z")
    assert result["failed"] == 1 and not save.called and history(db) == before


@pytest.mark.parametrize("operation", ["unlink", "relink"])
def test_actual_anilist_unlink_relink_routes_invalidate_only_owned_bridge(
    db, monkeypatch, operation
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from starlette.requests import Request

    from omakase.plus.auth import create_session
    from omakase.plus.deps import get_db as dependency_db
    from omakase.plus.mal import TokenBundle
    from omakase.plus.oauth_flows import create_oauth_flow
    from omakase.plus.secrets import read_secret
    from omakase.plus.tracker_reconciliation import Provider
    from omakase.plus.tracker_sync import BridgeService
    from omakase.plus.web_security import csrf_token

    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("ANILIST_CLIENT_ID", "synthetic-client")
    monkeypatch.setenv("ANILIST_CLIENT_SECRET", "synthetic-secret")
    seed_watch(db)
    before = history(db)
    service = BridgeService(db, None, None)
    for user in (1, 2):
        for provider, remote in ((Provider.ANILIST, 100 + user), (Provider.MAL, 200 + user)):
            service.save_connection(
                user,
                provider,
                remote,
                "Synthetic member",
                TokenBundle(f"synthetic-token-{user}", None, 9999999999),
            )
    db.execute(
        "INSERT INTO tracker_bridge_plans VALUES(?,?,?,?,?,?,?)",
        ("a" * 32, 1, "b" * 64, "synthetic-preview", 1, 9999999999, "approved"),
    )
    db.execute(
        "INSERT INTO tracker_bridge_outbox(plan_id,user_id,item_key,encrypted_patch,state,updated_at) VALUES(?,?,?,?,?,?)",
        ("a" * 32, 1, "0", "synthetic-patch", "pending", 1),
    )
    db.execute(
        "INSERT INTO tracker_bridge_baselines VALUES(?,?,?,?,?)",
        (1, 11, 22, "synthetic-baseline", 1),
    )
    db.execute("INSERT INTO tracker_bridge_preferences VALUES(?,?,?)", (1, 1, 1))
    db.commit()
    other = [
        tuple(r)
        for r in db.execute(
            "SELECT provider,generation,state FROM tracker_bridge_connections WHERE user_id=2 ORDER BY provider"
        )
    ]
    session = create_session(db, 1)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[dependency_db] = lambda: db
    client = TestClient(app)
    client.cookies.set("omakase_session", session)
    if operation == "unlink":
        csrf = csrf_token(
            Request(
                {"type": "http", "headers": [(b"cookie", ("omakase_session=" + session).encode())]}
            )
        )
        response = client.post(
            "/plus/integrations/anilist/disconnect",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
        assert read_secret(db, 1, "anilist_oauth_token") is None
    else:
        state = create_oauth_flow(db, user_id=1, provider="anilist", code_verifier="a" * 64)
        exchange = Mock(return_value="synthetic-new-token")
        monkeypatch.setattr(routes, "exchange_code", exchange)
        response = client.get(
            "/plus/integrations/anilist/callback",
            params={"state": state, "code": "synthetic-code"},
            follow_redirects=False,
        )
        assert (
            exchange.call_count == 1
            and read_secret(db, 1, "anilist_oauth_token") == "synthetic-new-token"
        )
    assert response.status_code == 302
    row = db.execute(
        "SELECT generation,state FROM tracker_bridge_connections WHERE user_id=1 AND provider='anilist'"
    ).fetchone()
    assert tuple(row) == (2, "disconnected")
    assert (
        db.execute(
            "SELECT state FROM tracker_bridge_connections WHERE user_id=1 AND provider='myanimelist'"
        ).fetchone()[0]
        == "verified"
    )
    assert (
        db.execute("SELECT enabled FROM tracker_bridge_preferences WHERE user_id=1").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT count(*) FROM tracker_bridge_baselines WHERE user_id=1").fetchone()[0]
        == 0
    )
    assert (
        db.execute("SELECT state FROM tracker_bridge_outbox WHERE user_id=1").fetchone()[0]
        == "cancelled"
    )
    assert [
        tuple(r)
        for r in db.execute(
            "SELECT provider,generation,state FROM tracker_bridge_connections WHERE user_id=2 ORDER BY provider"
        )
    ] == other
    assert history(db) == before


def test_actual_add_and_remove_planning_routes_do_not_dispatch_through_held_lease(db, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from starlette.requests import Request

    user = SimpleNamespace(id=1, email="writer1@example.test")
    valid = Mock(side_effect=token_context)
    ensure = Mock()
    remove = Mock()
    monkeypatch.setattr(routes, "with_valid_token", valid)
    monkeypatch.setattr(routes, "ensure_in_planning", ensure)
    from omakase.plus import anilist

    monkeypatch.setattr(anilist, "remove_from_planning", remove)
    body = b'{"anilist_id":11,"title":"Synthetic anime","status":"PLANNING"}'

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/plus/api/anilist/plan",
            "headers": [(b"content-type", b"application/json")],
        },
        receive=receive,
    )
    with writer_lease(db, 1):
        result = asyncio.run(routes.plan_api(request, db=db, user=user))
    assert result["status"] == "deferred" and not valid.called and not ensure.called
    db.execute(
        "INSERT INTO anilist_plannings(user_id,anilist_id,title,status) VALUES(1,11,'Synthetic anime','PLANNING')"
    )
    db.commit()
    with writer_lease(db, 1):
        response = asyncio.run(routes.dashboard_remove_plan(db=db, user=user, anilist_id=11))
    assert response.status_code == 302 and not remove.called and not valid.called
    assert db.execute("SELECT count(*) FROM anilist_plannings WHERE user_id=1").fetchone()[0] == 0


def test_old_writer_respects_saved_pair_retry_pause_before_any_provider_call(db, monkeypatch):
    import time

    from omakase.plus.mal import TokenBundle
    from omakase.plus.tracker_reconciliation import Provider
    from omakase.plus.tracker_sync import BridgeService

    BridgeService(db, None, None).save_connection(
        1,
        Provider.ANILIST,
        101,
        "Synthetic member",
        TokenBundle("synthetic-token-1", None, 9999999999),
    )
    db.execute(
        "UPDATE tracker_bridge_connections SET retry_at=? WHERE user_id=1",
        (int(time.time()) + 120,),
    )
    db.commit()
    valid = Mock(side_effect=token_context)
    save = Mock()
    monkeypatch.setattr(routes, "with_valid_token", valid)
    monkeypatch.setattr(routes, "save_completed_entry", save)
    result = routes._sync_completed_tracker_action(db, user_id=1, action=action(db))
    assert result["tracker_status"] == "deferred" and not valid.called and not save.called


def test_old_outbox_uncertain_transport_after_dispatch_requires_review_not_auto_retry(
    db, monkeypatch
):
    import httpx

    seed_watch(db)
    before = history(db)
    monkeypatch.setattr(
        anilist_sync,
        "get_authenticated_media_entry",
        lambda *args: {"id": 9, "mediaId": 11, "status": "CURRENT", "progress": 0},
    )
    save = Mock(side_effect=httpx.ReadTimeout("synthetic timeout"))
    monkeypatch.setattr(anilist_sync, "save_progress_entry", save)
    result = anilist_sync.process_tracker_outbox(db, now="2026-10-28T12:01:00Z")
    assert (
        result["conflict"] == 1
        and result["synced"] == 0
        and save.call_count == 1
        and history(db) == before
    )
    again = anilist_sync.process_tracker_outbox(db, now="2026-10-28T12:05:00Z")
    assert again["claimed"] == 0 and save.call_count == 1
