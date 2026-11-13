"""Bounded automatic bridge runner; every provider boundary is a local fake."""

from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from omakase.cli import cli
from omakase.plus.db import get_db
from omakase.plus.mal import TokenBundle, TrackerHTTPError
from omakase.plus.tracker_bridge_worker import run_tracker_bridge, sync_trackers_once
from omakase.plus.tracker_reconciliation import Entry, ExactMapping, Inventory, Provider, State
from omakase.plus.tracker_sync import BridgeError, BridgeService, writer_lease


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_MASTER_KEY", "synthetic-worker-master")
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_TRACKER_BRIDGE_WORKER_ENABLED", "true")
    monkeypatch.setenv("OMAKASE_TRACKER_BRIDGE_WRITERS_READY", "true")
    from omakase.plus import secrets

    monkeypatch.setattr(secrets, "_KEY", None)
    conn = get_db(tmp_path)
    for uid in (1, 2):
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (uid, f"worker{uid}@example.test", "synthetic"),
        )
    conn.commit()
    yield conn
    conn.close()


def opted(db):
    with db:
        db.executemany("INSERT INTO tracker_bridge_preferences VALUES(?,1,1000)", [(1,), (2,)])


def run(db, **kwargs):
    return run_tracker_bridge(
        db, now=lambda: 1000, http_factory=lambda: nullcontext(None), **kwargs
    )


def test_fair_rotation_persists_across_invocations_and_has_no_http_transaction(db):
    opted(db)
    visited = []

    class Tick:
        def sync_tick(self, user, **kwargs):
            assert not db.in_transaction
            kwargs["guard"]()
            visited.append(user)
            return {"state": "checked"}

    factory = Mock(return_value=Tick())
    for _ in range(3):
        assert run(db, max_accounts=1, service_factory=factory)["attempted"] == 1
    assert visited == [1, 2, 1]
    assert db.execute("SELECT lease_token FROM tracker_bridge_worker_state").fetchone()[0] is None


@pytest.mark.parametrize(
    "flag",
    [
        "OMAKASE_TRACKER_BRIDGE_WORKER_ENABLED",
        "OMAKASE_TRACKER_BRIDGE_WRITERS_READY",
        "OMAKASE_PLUS_PRIVATE",
    ],
)
def test_off_zero_service_or_http_and_no_lease(db, monkeypatch, flag):
    opted(db)
    monkeypatch.delenv(flag)
    factory = Mock(side_effect=AssertionError("No provider/client construction"))
    assert run(db, service_factory=factory)["state"] == "disabled"
    assert not factory.called
    assert db.execute("SELECT last_user_id FROM tracker_bridge_worker_state").fetchone()[0] == 0
    monkeypatch.setattr(
        "omakase.plus.db._connect", Mock(side_effect=AssertionError("Off must not open/migrate DB"))
    )
    assert sync_trackers_once()["state"] == "disabled"


def test_no_optin_and_unqualified_migration_make_no_client(db):
    factory = Mock(side_effect=AssertionError("No provider"))
    assert run(db, service_factory=factory)["state"] == "idle"
    opted(db)
    with db:
        db.execute("DELETE FROM _migrations WHERE name='035-myanimelist-bridge.sql'")
    assert run(db, service_factory=factory)["state"] == "unavailable"
    assert not factory.called


def test_global_lock_and_budget_never_duplicate_accounts_or_steal_replacement(db):
    opted(db)
    with db:
        db.execute("UPDATE tracker_bridge_worker_state SET lease_token='other',lease_until=1100")
    factory = Mock(side_effect=AssertionError("No provider"))
    assert run(db, service_factory=factory)["state"] == "busy"
    with db:
        db.execute("UPDATE tracker_bridge_worker_state SET lease_until=999")
    clock = [0]

    class Tick:
        def sync_tick(self, user, **kwargs):
            clock[0] = 61
            with db:
                db.execute(
                    "UPDATE tracker_bridge_worker_state SET lease_token='replacement',lease_until=1200"
                )
            with pytest.raises(BridgeError):
                kwargs["guard"]()
            return {"state": "paused"}

    result = run(db, service_factory=lambda *a, **k: Tick(), monotonic=lambda: clock[0])
    assert result["attempted"] == 1
    assert (
        db.execute("SELECT lease_token FROM tracker_bridge_worker_state").fetchone()[0]
        == "replacement"
    )


class ProviderFake:
    def __init__(self, user, provider):
        self.user = user
        self.provider = provider
        self.calls = 0
        self.writes = []
        self.hook = None
        self.rows = {
            11 if provider == Provider.ANILIST else 22: Entry(
                11 if provider == Provider.ANILIST else 22, State()
            )
        }
        self.titles = {}

    def viewer(self, token):
        self.calls += 1
        return (
            100 + self.user if self.provider == Provider.ANILIST else 200 + self.user,
            "Synthetic member",
        )

    def inventory(self, token, identity):
        self.calls += 1
        inv = Inventory(identity, tuple(self.rows.values()), complete=True)
        return (inv, (ExactMapping(11, 22, 22),)) if self.provider == Provider.ANILIST else inv

    def mapping(self, token, media_id):
        return ExactMapping(11, 22, 22)

    def entry(self, token, media_id, identity=None):
        self.calls += 1
        if self.hook:
            self.hook()
        return self.rows.get(media_id)

    def patch(self, token, media_id, changes):
        self.writes.append(changes)
        self.rows[media_id] = replace(
            self.rows[media_id], state=replace(self.rows[media_id].state, **changes)
        )
        if self.hook:
            self.hook()
        return self.rows[media_id]


def services(db):
    result = {}
    for user in (1, 2):
        a = ProviderFake(user, Provider.ANILIST)
        m = ProviderFake(user, Provider.MAL)
        s = BridgeService(db, m, a, now=lambda: 1000, legacy_writers_ready=True)
        for provider, remote in [(Provider.ANILIST, 100 + user), (Provider.MAL, 200 + user)]:
            s.save_connection(
                user,
                provider,
                remote,
                "Synthetic member",
                TokenBundle("synthetic-access", "synthetic-refresh", 99999),
            )
        preview = s.preview(user)
        s.apply(user, preview["id"], preview["hash"], approved_items=[])
        s.set_enabled(user, True)
        a.calls = m.calls = 0
        result[user] = (s, a, m)
    return result


def test_real_service_unchanged_noop_two_accounts_and_first_approval_required(db):
    s = BridgeService(db, None, None, now=lambda: 1000, legacy_writers_ready=True)
    with pytest.raises(BridgeError):
        s.set_enabled(1, True)
    pool = services(db)
    order = []

    def factory(*args, **kwargs):
        user = db.execute("SELECT last_user_id FROM tracker_bridge_worker_state").fetchone()[0]
        order.append(user)
        return pool[user][0]

    result = run(db, service_factory=factory)
    assert order == [1, 2] and result["outcomes"] == {"checked": 2}
    assert all(not a.writes and not m.writes for _, a, m in pool.values())
    pool[1][0].legacy_writers_ready = False
    assert pool[1][0].status(1)["enabled"] is False
    assert pool[1][0].status(1)["opted_in"] is True


def test_real_service_rate_pause_revoke_and_personal_writer_lock(db):
    pool = services(db)
    s, a, m = pool[1]
    with writer_lease(db, 1, now=1000):
        result = run(db, max_accounts=1, service_factory=lambda *args, **kwargs: s)
    assert result["outcomes"] == {"deferred": 1} and a.calls == m.calls == 0
    with db:
        db.execute("UPDATE tracker_bridge_worker_state SET last_user_id=0")
        db.execute("UPDATE tracker_bridge_connections SET retry_at=2000 WHERE user_id=1")
    result = run(db, max_accounts=1, service_factory=lambda *args, **kwargs: s)
    assert result["outcomes"] == {"deferred": 1} and a.calls == m.calls == 0
    with db:
        db.execute("UPDATE tracker_bridge_connections SET retry_at=0 WHERE user_id=1")
        db.execute("UPDATE tracker_bridge_worker_state SET last_user_id=0")
    a.viewer = lambda token: (_ for _ in ()).throw(
        TrackerHTTPError("reconnect_required", provider=Provider.ANILIST)
    )
    assert run(db, service_factory=lambda *args, **kwargs: s, max_accounts=1)["outcomes"] == {
        "review_required": 1
    }
    assert not s.status(1)["enabled"]


@pytest.mark.parametrize("when", ["preflight", "after_dispatch"])
@pytest.mark.parametrize("stop_reason", ["optout", "worker_lease_expired"])
def test_actual_automatic_optout_blocks_dispatch_or_ack_and_never_replays(db, when, stop_reason):
    s, a, m = services(db)[1]
    a.rows[11] = replace(a.rows[11], state=State(progress=4))

    def stop():
        with db:
            if stop_reason == "optout":
                db.execute("UPDATE tracker_bridge_preferences SET enabled=0 WHERE user_id=1")
            else:
                db.execute("UPDATE tracker_bridge_worker_state SET lease_until=1000")

    if when == "preflight":
        a.hook = stop
    else:
        original = m.patch

        def patch(*args):
            result = original(*args)
            stop()
            return result

        m.patch = patch
    result = run(db, max_accounts=1, service_factory=lambda *args, **kwargs: s)
    if when == "preflight":
        assert not m.writes
    else:
        assert len(m.writes) == 1
        assert (
            db.execute(
                "SELECT count(*) FROM tracker_bridge_outbox WHERE user_id=1 AND state='ambiguous'"
            ).fetchone()[0]
            == 1
        )
        with db:
            db.execute("UPDATE tracker_bridge_preferences SET enabled=1 WHERE user_id=1")
        assert s.sync_tick(1)["reason"] == "uncertain_previous_write"
        assert len(m.writes) == 1
    assert result["attempted"] == 1


def test_existing_runner_and_explicit_cli_only_invoke_bounded_adapter(db, monkeypatch):
    import omakase.plus.tracker_bridge_worker as worker

    calls = []
    monkeypatch.setattr(
        worker,
        "sync_trackers_once",
        lambda **kwargs: calls.append(kwargs) or {"state": "disabled", "attempted": 0},
    )
    result = CliRunner().invoke(
        cli, ["plus", "sync-trackers", "--max-accounts", "1", "--budget-seconds", "10"]
    )
    assert result.exit_code == 0 and calls == [
        {"data_dir": None, "max_accounts": 1, "budget_seconds": 10}
    ]
    from omakase.plus import media_cli

    async def media(*args, **kwargs):
        return "synthetic receipt"

    monkeypatch.setattr(media_cli, "get_db", lambda: db)
    monkeypatch.setattr(media_cli, "run_media_sync", media)
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    assert media_cli.sync_media_once() == "synthetic receipt"
    assert calls[-1] == {}


def test_absolute_expired_provider_budget_stops_before_any_http(db):
    import time

    import httpx

    from omakase.plus.mal import AniListBridgeClient, MALClient

    services(db)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(500))
    ) as http:
        service = BridgeService(
            db,
            MALClient(http, "synthetic-client", "synthetic-secret", now=lambda: 1000),
            AniListBridgeClient(http, now=lambda: 1000),
            now=lambda: 1000,
            legacy_writers_ready=True,
        )
        with pytest.raises(TrackerHTTPError, match="job_budget_exhausted"):
            service.sync_tick(1, deadline=time.monotonic() - 1)
    assert calls == []


def test_runner_fresh_connection_does_not_close_existing_cli_cache(db, monkeypatch):
    from pathlib import Path

    from omakase.plus import tracker_bridge_worker as worker

    data_dir = Path(db.execute("PRAGMA database_list").fetchone()[2]).parent

    def check(other, **kwargs):
        assert other is not db
        return {"state": "idle", "attempted": 0}

    monkeypatch.setattr(worker, "run_tracker_bridge", check)
    assert sync_trackers_once(data_dir=data_dir)["state"] == "idle"
    assert db.execute("SELECT count(*) FROM users").fetchone()[0] == 2
