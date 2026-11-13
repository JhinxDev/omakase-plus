"""Safe shared intake freezes actor/owner criteria; unqualified jobs never enter providers."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from omakase.plus import households as hh
from omakase.plus import language_store
from omakase.plus.acquisition_policy import AcquisitionPolicy
from omakase.plus.household_requests import dispatch_shared_anime, prepare_shared_anime
from omakase.plus.media_types import MediaTarget
from omakase.plus.secrets import store_secret
from tests.plus.test_household_media import db as db
from tests.plus.test_households import shared


def target(identity=200):
    return MediaTarget(
        anilist_id=identity,
        title="A new shared anime",
        search_titles=("A new shared anime",),
        format="TV",
        status="FINISHED",
        episodes=12,
        aired_episodes=12,
        season_number=1,
        season="WINTER",
        season_year=2026,
        genres=("Adventure",),
        next_airing_at=None,
    )


def test_catalog_add_is_separate_from_acquisition_and_never_imports_history(db):
    lib = shared(db, requests=False)
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    receipt = prepare_shared_anime(
        db, member, target=target(), idempotency_key="catalog", request_media=False
    )
    assert (
        receipt.outcome == "catalog_added"
        and receipt.actor_user_id == 2
        and receipt.operational_owner_id == 1
    )
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM language_request_policies").fetchone()[0] == 0
    with pytest.raises(hh.HouseholdError, match="disabled"):
        prepare_shared_anime(
            db, member, target=target(201), idempotency_key="no-permission", request_media=True
        )
    assert db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM watch_entries").fetchone()[0] == 0


def test_queue_freezes_owner_full_criteria_real_actor_and_no_legacy_scheduling(db):
    from omakase.plus.media_sync import run_media_sync

    lib = shared(db, requests=True)
    owner_policy = AcquisitionPolicy(
        allowed_resolutions=("2160p", "1080p"),
        minimum_seeders=7,
        preferred_seeders=12,
        max_release_size_gib=60,
    )
    store_secret(db, 1, "acquisition_policy", owner_policy.to_json())
    store_secret(
        db,
        2,
        "acquisition_policy",
        AcquisitionPolicy(minimum_seeders=2, preferred_seeders=3).to_json(),
    )
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    receipt = prepare_shared_anime(
        db, member, target=target(), idempotency_key="queue", request_media=True
    )
    assert (
        receipt.outcome == "pipeline_unavailable"
        and receipt.actor_user_id == 2
        and receipt.operational_owner_id == 1
    )
    row = db.execute("SELECT * FROM media_monitors").fetchone()
    assert row["user_id"] == 1 and row["active"] == 0 and row["anilist_planning_id"] is None
    snapshot, policy, episodes = language_store.load_request(
        db, actor_user_id=2, request_id=receipt.request_id
    )
    assert (
        snapshot.actor_user_id == 2
        and snapshot.library_id == lib
        and snapshot.library_item_id == receipt.item_id
    )
    assert (
        policy.minimum_seeders == 7
        and policy.preferred_seeders == 12
        and policy.max_release_size_gib == 60
    )
    assert policy.allowed_resolutions == ("2160p", "1080p") and episodes == tuple(range(1, 13))
    assert db.execute("SELECT COUNT(*) FROM anilist_plannings").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM watch_entries").fetchone()[0] == 0
    calls = []

    def provider(_):
        calls.append(1)
        raise AssertionError("Inactive shared request must never dispatch through legacy")

    result = asyncio.run(
        run_media_sync(
            db, entrypoint="synthetic", monitor_ids=(receipt.monitor_id,), provider_factory=provider
        )
    )
    assert result.monitor_results == () and calls == []
    blocked = asyncio.run(dispatch_shared_anime(db, actor_user_id=2, request_id=receipt.request_id))
    assert blocked.outcome == "pipeline_unavailable" and "No provider request" in blocked.detail
    assert (
        db.execute(
            "SELECT state FROM library_request_events WHERE id=?", (receipt.event_id,)
        ).fetchone()[0]
        == "queued"
    )


def test_replay_preserves_immutable_snapshot_and_no_duplicate_monitor(db):
    lib = shared(db, requests=True)
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    first = prepare_shared_anime(
        db, member, target=target(), idempotency_key="same", request_media=True
    )
    second = prepare_shared_anime(
        db,
        member,
        target=replace(target(), episodes=24, aired_episodes=24),
        idempotency_key="same",
        request_media=True,
    )
    assert first.request_id == second.request_id and first.monitor_id == second.monitor_id
    assert language_store.load_request(db, actor_user_id=2, request_id=first.request_id)[
        2
    ] == tuple(range(1, 13))
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 1
    with pytest.raises(hh.HouseholdError):
        prepare_shared_anime(
            db, member, target=target(201), idempotency_key="same", request_media=True
        )
    assert db.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 1


def test_existing_owner_personal_monitor_is_not_silently_shared_or_changed(db):
    lib = shared(db, requests=True)
    db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number,active) VALUES(1,200,'Private saved title',1,1)"
    )
    db.commit()
    before = dict(db.execute("SELECT * FROM media_monitors").fetchone())
    receipt = prepare_shared_anime(
        db,
        hh.resolve_scope(db, viewer_id=2, library_id=lib),
        target=target(),
        idempotency_key="private-conflict",
        request_media=True,
    )
    assert receipt.outcome == "owner_review_required" and receipt.monitor_id is None
    assert dict(db.execute("SELECT * FROM media_monitors").fetchone()) == before
    assert (
        db.execute(
            "SELECT resource_id FROM library_items WHERE id=?", (receipt.item_id,)
        ).fetchone()[0]
        is None
    )


def test_foreign_revoked_or_cancelled_request_never_reaches_qualifier_or_driver(db):
    lib = shared(db, requests=True)
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    receipt = prepare_shared_anime(
        db, member, target=target(), idempotency_key="revoke", request_media=True
    )
    calls = []

    def qualifier():
        calls.append(1)
        return "language-policy-v1"

    async def driver(*args, **kwargs):
        calls.append(2)

    with pytest.raises(hh.HouseholdError):
        asyncio.run(
            dispatch_shared_anime(
                db,
                actor_user_id=3,
                request_id=receipt.request_id,
                qualifier=qualifier,
                driver=driver,
            )
        )
    hh.leave_or_revoke(db, hh.resolve_scope(db, viewer_id=1, library_id=lib), member_id=2)
    with pytest.raises(hh.HouseholdError):
        asyncio.run(
            dispatch_shared_anime(
                db,
                actor_user_id=2,
                request_id=receipt.request_id,
                qualifier=qualifier,
                driver=driver,
            )
        )
    assert calls == []


def test_qualified_fake_driver_receives_real_actor_exact_source_and_global_sync_lease(db):
    from omakase.plus.media_store import acquire_sync_lease

    lib = shared(db, requests=True)
    store_secret(db, 1, "realdebrid_api_key", "synthetic-owned-rd")
    hh.bind_connector(
        db, hh.resolve_scope(db, viewer_id=1, library_id=lib), integration="realdebrid"
    )
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    receipt = prepare_shared_anime(
        db, member, target=target(), idempotency_key="qualified-fake", request_media=True
    )
    seen = []

    async def driver(db, *, context, heartbeat):
        seen.append(context)
        heartbeat()
        assert context.actor_user_id == 2 and context.operational_owner_id == 1
        assert context.request_id == receipt.request_id and context.event_id == receipt.event_id
        assert (
            len(context.source_generation) == 64
            and context.contract_version == "language-policy-v1"
        )
        row = db.execute(
            "SELECT user_id,active FROM media_monitors WHERE id=?", (context.monitor_id,)
        ).fetchone()
        assert tuple(row) == (1, 0)
        other = acquire_sync_lease(
            db,
            owner="other-synthetic-job",
            entrypoint="legacy",
            acquired_at="2000-01-01T00:00:00Z",
            expires_at="2999-01-01T00:00:00Z",
        )
        assert not other.acquired
        return {"outcome": "language_reused"}

    result = asyncio.run(
        dispatch_shared_anime(
            db,
            actor_user_id=2,
            request_id=receipt.request_id,
            qualifier=lambda: "language-policy-v1",
            driver=driver,
        )
    )
    assert result == {"outcome": "language_reused"} and len(seen) == 1
    assert (
        db.execute(
            "SELECT state FROM library_request_events WHERE id=?", (receipt.event_id,)
        ).fetchone()[0]
        == "completed"
    )
    assert db.execute("SELECT entrypoint FROM media_sync_lease").fetchone()[0] == "released"
    assert db.execute("SELECT COUNT(*) FROM anilist_plannings").fetchone()[0] == 0


def test_uncertain_dispatch_cannot_repeat_fake_provider_or_lose_actor(db):
    lib = shared(db, requests=True)
    store_secret(db, 1, "realdebrid_api_key", "synthetic-owned-rd")
    hh.bind_connector(
        db, hh.resolve_scope(db, viewer_id=1, library_id=lib), integration="realdebrid"
    )
    receipt = prepare_shared_anime(
        db,
        hh.resolve_scope(db, viewer_id=2, library_id=lib),
        target=target(),
        idempotency_key="uncertain-fake",
        request_media=True,
    )
    calls = []

    async def driver(db, *, context, heartbeat):
        calls.append(context.actor_user_id)
        raise RuntimeError("synthetic uncertain outcome")

    with pytest.raises(RuntimeError):
        asyncio.run(
            dispatch_shared_anime(
                db,
                actor_user_id=2,
                request_id=receipt.request_id,
                qualifier=lambda: "language-policy-v1",
                driver=driver,
            )
        )
    assert (
        db.execute(
            "SELECT state FROM library_request_events WHERE id=?", (receipt.event_id,)
        ).fetchone()[0]
        == "uncertain"
    )
    with pytest.raises(hh.HouseholdError):
        asyncio.run(
            dispatch_shared_anime(
                db,
                actor_user_id=2,
                request_id=receipt.request_id,
                qualifier=lambda: "language-policy-v1",
                driver=driver,
            )
        )
    assert calls == [2]


def test_saved_member_preference_applies_only_to_future_permitted_snapshots(db):
    from omakase.plus.language_policy import LanguageRule
    from omakase.plus.language_preferences import set_member_preference

    lib = shared(db, requests=True)
    member = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    french = LanguageRule(audio_required=("fr",))
    japanese = LanguageRule(audio_required=("ja",))
    set_member_preference(db, member, "anime", french)
    first = prepare_shared_anime(
        db, member, target=target(), idempotency_key="french", request_media=True
    )
    set_member_preference(db, member, "anime", japanese)
    second = prepare_shared_anime(
        db, member, target=target(), idempotency_key="japanese", request_media=True
    )
    assert (
        language_store.load_request(db, actor_user_id=2, request_id=first.request_id)[
            0
        ].effective.rule
        == french
    )
    assert (
        language_store.load_request(db, actor_user_id=2, request_id=second.request_id)[
            0
        ].effective.rule
        == japanese
    )


def test_finite_queue_continues_known_progress_but_not_before_retry_or_uncertain(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from omakase.plus.household_requests import consume_shared_anime_queue
    from omakase.plus.media_types import MediaSyncResult

    monkeypatch.setattr(hh, "feature_enabled", lambda: True)
    lib = shared(db, requests=True)
    store_secret(db, 1, "realdebrid_api_key", "synthetic-owned-rd")
    hh.bind_connector(
        db, hh.resolve_scope(db, viewer_id=1, library_id=lib), integration="realdebrid"
    )
    receipt = prepare_shared_anime(
        db,
        hh.resolve_scope(db, viewer_id=2, library_id=lib),
        target=target(),
        idempotency_key="bounded-continuation",
        request_media=True,
    )
    calls = []

    async def driver(db, *, context, heartbeat):
        heartbeat()
        calls.append(context.actor_user_id)
        return MediaSyncResult(
            context.monitor_id,
            0,
            "provider_rate_limited",
            frozenset({1}),
            frozenset(),
            (),
            "Provider pause.",
            7200,
        )

    def qualifier():
        return "language-policy-v1"

    asyncio.run(consume_shared_anime_queue(db, limit=1, qualifier=qualifier, driver=driver))
    event = db.execute(
        "SELECT * FROM library_request_events WHERE id=?", (receipt.event_id,)
    ).fetchone()
    assert event["state"] == "dispatched" and event["result_code"] == "provider_rate_limited"
    assert datetime.fromisoformat(event["next_attempt_at"]) > datetime.now(UTC) + timedelta(
        seconds=7190
    )
    asyncio.run(consume_shared_anime_queue(db, qualifier=qualifier, driver=driver))
    assert calls == [2]
    # Resume only this known result after the full persisted deadline.
    db.execute(
        "UPDATE library_request_events SET next_attempt_at='2000-01-01T00:00:00Z' WHERE id=?",
        (receipt.event_id,),
    )
    db.commit()
    asyncio.run(consume_shared_anime_queue(db, qualifier=qualifier, driver=driver))
    assert calls == [2, 2]
    db.execute(
        "UPDATE library_request_events SET result_code='dispatch_in_progress',next_attempt_at=NULL WHERE id=?",
        (receipt.event_id,),
    )
    db.commit()
    asyncio.run(consume_shared_anime_queue(db, qualifier=qualifier, driver=driver))
    assert calls == [2, 2]
    assert tuple(
        db.execute(
            "SELECT active,next_check_at FROM media_monitors WHERE id=?", (receipt.monitor_id,)
        ).fetchone()
    ) == (0, None)


def test_disabled_or_unqualified_consumer_does_not_open_provider(db, monkeypatch):
    from omakase.plus.household_requests import consume_shared_anime_queue

    calls = []

    async def driver(*args, **kwargs):
        calls.append(1)
        raise AssertionError("provider must remain untouched")

    monkeypatch.setattr(hh, "feature_enabled", lambda: False)
    assert (
        asyncio.run(
            consume_shared_anime_queue(db, qualifier=lambda: "language-policy-v1", driver=driver)
        )
        == ()
    )
    monkeypatch.setattr(hh, "feature_enabled", lambda: True)
    assert asyncio.run(consume_shared_anime_queue(db, qualifier=lambda: None, driver=driver)) == ()
    assert calls == []
