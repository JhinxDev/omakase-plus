"""Provider restrictions survive jobs, actors and new client instances without network."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from omakase.plus.provider_limits import ProviderRateLimit, cooldown_hooks, retry_delay
from omakase.plus.realdebrid import RealDebridClient, RealDebridProviderBlock
from tests.plus.test_household_media import db as db


def test_retry_after_numeric_date_and_unrepresentable_delay():
    now = datetime(2026, 10, 28, tzinfo=UTC)
    assert retry_delay("7200", now=now) == 7200
    assert retry_delay("Wed, 28 Oct 2026 03:00:00 GMT", now=now) == 10800
    assert retry_delay("9" * 10000, now=now) is None
    assert retry_delay("999999999999", now=now) is None
    for malformed in (None, "", "inf", "NaN", "-1", "1.5", "not a date"):
        assert retry_delay(malformed, now=now) == 900


def test_same_owner_two_actors_new_client_and_restart_hold_until_full_deadline(db):
    now = [datetime(2026, 10, 28, 0, 0, 0, 500000, tzinfo=UTC)]
    hooks = cooldown_hooks(db, 1, clock=lambda: now[0])
    hooks["rate_limit_observer"](ProviderRateLimit(7200))
    db.commit()
    for advance in (0, 3601, 7199):
        now[0] = datetime(2026, 10, 28, 0, 0, 0, 500000, tzinfo=UTC) + timedelta(seconds=advance)
        # Two independent jobs/viewers using the same operational owner, fresh factories.
        for _actor in (1, 2):
            with pytest.raises(ProviderRateLimit) as error:
                cooldown_hooks(db, 1, clock=lambda: now[0])["request_guard"]()
            assert error.value.retry_after_seconds == 7200 - advance
        cooldown_hooks(db, 3, clock=lambda: now[0])["request_guard"]()
    now[0] += timedelta(seconds=1)
    cooldown_hooks(db, 1, clock=lambda: now[0])["request_guard"]()
    assert db.execute("SELECT COUNT(*) FROM provider_cooldowns").fetchone()[0] == 1


def test_longer_pause_and_manual_hold_never_shortened(db):
    def clock():
        return datetime(2026, 10, 28, tzinfo=UTC)

    hooks = cooldown_hooks(db, 1, clock=clock)
    for delay in (7200, 900, None, 1):
        hooks["rate_limit_observer"](ProviderRateLimit(delay))
    with pytest.raises(ProviderRateLimit) as error:
        hooks["request_guard"]()
    assert error.value.manual_review_required
    row = db.execute("SELECT * FROM provider_cooldowns").fetchone()
    assert set(row.keys()) == {
        "owner_user_id",
        "provider_kind",
        "not_before",
        "manual_review",
        "updated_at",
    }


def test_actual_rd_client_all_request_methods_block_without_http_after_429(db):
    calls = []

    async def handle(request):
        calls.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "7200"})

    original = httpx.AsyncClient

    def client_factory(**kwargs):
        return original(transport=httpx.MockTransport(handle), **kwargs)

    async def exercise():
        with patch("omakase.plus.realdebrid.httpx.AsyncClient", client_factory):
            first = RealDebridClient("synthetic", **cooldown_hooks(db, 1))
            with pytest.raises(ProviderRateLimit) as error:
                await first.list_torrents()
            assert error.value.retry_after_seconds == 7200
            # Legacy and shared clients have identical per-request durable guard.
            for _actor in (1, 2):
                client = RealDebridClient("rotated-synthetic", **cooldown_hooks(db, 1))
                for method, args in (
                    ("add_magnet", ("magnet:synthetic",)),
                    ("select_files", ("id",)),
                    ("get_torrent", ("id",)),
                    ("instant_available", ("A" * 40,)),
                    ("list_torrents", ()),
                    ("delete_torrent", ("id",)),
                    ("verify_download", ("id",)),
                ):
                    with pytest.raises(ProviderRateLimit):
                        await getattr(client, method)(*args)

    asyncio.run(exercise())
    assert calls == ["GET"]


def test_existing_451_provider_block_is_preserved():
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(451, json={"error": "infringing_file"})
            ),
            **kwargs,
        )

    async def exercise():
        with patch("omakase.plus.realdebrid.httpx.AsyncClient", factory):
            with pytest.raises(RealDebridProviderBlock):
                await RealDebridClient("synthetic").add_magnet("magnet:synthetic")

    asyncio.run(exercise())


def test_legacy_scheduler_owner_pause_skips_identity_and_every_http(db):
    from omakase.plus.media_store import upsert_monitor
    from omakase.plus.media_sync import run_media_sync
    from omakase.plus.providers.realdebrid import RealDebridProvider

    cooldown_hooks(db, 1)["rate_limit_observer"](ProviderRateLimit(7200))
    for identity in (1001, 1002):
        upsert_monitor(
            db,
            user_id=1,
            anilist_id=identity,
            title="Synthetic",
            search_titles=("Synthetic",),
            season_number=1,
            expected_episodes=2,
            aired_episodes=2,
        )
    db.commit()
    calls = []

    def identity(*args):
        calls.append("metadata")
        raise AssertionError("cooldown must precede metadata")

    def factory(owner):
        return RealDebridProvider("synthetic", **cooldown_hooks(db, owner))

    async def transport(request):
        calls.append("HTTP")
        raise AssertionError("cooldown must precede HTTP")

    original = httpx.AsyncClient
    with patch(
        "omakase.plus.realdebrid.httpx.AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(transport), **kw),
    ):
        for _restart in range(2):
            result = asyncio.run(
                run_media_sync(
                    db, entrypoint="test", provider_factory=factory, identity_resolver=identity
                )
            )
            assert all(row.outcome == "provider_rate_limited" for row in result.monitor_results)
    assert calls == []


def test_created_asset_is_retained_queued_if_select_hits_429(db):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omakase.plus.media_store import record_sync_run, upsert_monitor
    from omakase.plus.media_sync import _attempt_candidate
    from tests.plus.test_media_sync import _candidate

    monitor_id = upsert_monitor(
        db,
        user_id=1,
        anilist_id=1001,
        title="Show",
        search_titles=("Show",),
        season_number=1,
        expected_episodes=2,
        aired_episodes=2,
    )
    monitor = db.execute("SELECT * FROM media_monitors WHERE id=?", (monitor_id,)).fetchone()
    run_id = record_sync_run(db, monitor_id=monitor_id, outcome="running")
    rd = SimpleNamespace(
        add_magnet=AsyncMock(return_value="created-before-pause"),
        select_files=AsyncMock(side_effect=ProviderRateLimit(7200)),
    )
    with pytest.raises(ProviderRateLimit) as error:
        asyncio.run(
            _attempt_candidate(
                db,
                monitor,
                _candidate("a", {1}),
                rd=rd,
                run_id=run_id,
                rank=1,
                searched_aliases=("Show",),
                now="2026-10-28T00:00:00Z",
                fallback=False,
            )
        )
    row = db.execute("SELECT * FROM media_assets").fetchone()
    assert row["provider_asset_id"] == "created-before-pause"
    assert row["status"] == "queued" and row["inventory_verified_at"] is None
    assert row["provenance"] == "omakase_created" and row["created_by_run_id"] == run_id
    assert error.value.retained_asset_ids == (row["id"],)
    assert rd.add_magnet.await_count == rd.select_files.await_count == 1
