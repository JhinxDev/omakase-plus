"""Explicit single-title rechecks retain schedule, lease and ownership boundaries."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from omakase.plus import media_sync
from omakase.plus.media_store import acquire_sync_lease
from tests.plus.test_media_scheduler import _monitor, _sync_result, _target
from tests.plus.test_media_scheduler import db as db
from tests.plus.test_media_scheduler import user_id as user_id


@pytest.fixture(autouse=True)
def qualified_quality_pair(monkeypatch):
    from omakase.plus import language_publication, quality_publication

    monkeypatch.setattr(quality_publication, "QUALITY_PUBLICATION_QUALIFIED", True)
    monkeypatch.setattr(language_publication, "LANGUAGE_PUBLICATION_QUALIFIED", True)


def intent(user, monitor, *, upgrade=True):
    from omakase.plus.media_types import MediaRecheckIntent

    return MediaRecheckIntent(actor_user_id=user, monitor_id=monitor, upgrade_to_1080=upgrade)


def run(db, user, monitor, *, request=None, monitor_ids=None, provider=None):
    return asyncio.run(
        media_sync.run_media_sync(
            db,
            entrypoint="product_recheck",
            monitor_ids=(monitor,) if monitor_ids is None else monitor_ids,
            recheck=request or intent(user, monitor),
            now="2026-10-29T14:00:00Z",
            lease_owner="fixture-recheck-owner",
            identity_resolver=lambda aid, _: _target(int(aid), status="FINISHED"),
            provider_factory=provider or (lambda _: object()),
        )
    )


@pytest.mark.parametrize("failure", [False, True])
def test_finished_inactive_recheck_is_one_off_even_on_failure(db, user_id, monkeypatch, failure):
    selected, other = _monitor(db, user_id, 1), _monitor(db, user_id, 2)
    db.execute(
        "UPDATE media_monitors SET active=0,release_status='FINISHED',next_check_at=NULL WHERE id=?",
        (selected,),
    )
    db.commit()
    reconcile = (
        AsyncMock(side_effect=RuntimeError("fixture failure"))
        if failure
        else AsyncMock(return_value=_sync_result(selected))
    )
    monkeypatch.setattr(media_sync, "reconcile_monitor", reconcile)
    request = intent(user_id, selected)
    receipt = run(db, user_id, selected, request=request)
    reconcile.assert_awaited_once()
    assert reconcile.await_args.kwargs["recheck"] == request
    assert [row.monitor_id for row in receipt.monitor_results] == [selected]
    row = db.execute(
        "SELECT active,next_check_at,consecutive_failures FROM media_monitors WHERE id=?",
        (selected,),
    ).fetchone()
    assert tuple(row) == (0, None, 0)
    assert (
        db.execute("SELECT last_checked_at FROM media_monitors WHERE id=?", (other,)).fetchone()[0]
        is None
    )


@pytest.mark.parametrize("wrong", ["actor", "scope"])
def test_invalid_recheck_owner_or_multi_title_scope_fails_before_lease_or_provider(
    db, user_id, wrong
):
    selected = _monitor(db, user_id, 1)
    db.commit()
    request = intent(user_id + 99 if wrong == "actor" else user_id, selected)
    provider = Mock()
    changes = db.total_changes
    with pytest.raises(ValueError):
        run(
            db,
            user_id,
            selected,
            request=request,
            monitor_ids=(selected, selected + 1) if wrong == "scope" else (selected,),
            provider=provider,
        )
    assert db.total_changes == changes
    provider.assert_not_called()


def test_current_cleanup_block_prevents_one_off_recheck_before_any_work(db, user_id):
    selected = _monitor(db, user_id, 1)
    db.execute(
        "INSERT INTO completion_plex_cleanup_blocks(monitor_id,user_id,requested_at,detail) VALUES(?,?,'2026-10-29T12:00:00Z','Fixture cleanup block')",
        (selected, user_id),
    )
    db.commit()
    provider = Mock()
    changes = db.total_changes
    receipt = run(db, user_id, selected, provider=provider)
    assert receipt.outcome == "not_eligible" and receipt.monitor_results == ()
    assert db.total_changes == changes
    provider.assert_not_called()


def test_repeated_recheck_uses_existing_busy_lease_without_provider_call(db, user_id):
    selected = _monitor(db, user_id, 1)
    acquire_sync_lease(
        db,
        owner="first-request",
        entrypoint="product_recheck",
        acquired_at="2026-10-29T13:59:00Z",
        expires_at="2026-10-29T14:29:00Z",
    )
    db.commit()
    provider = Mock()
    receipt = run(db, user_id, selected, provider=provider)
    assert receipt.outcome == "already_running"
    provider.assert_not_called()


def test_old_missing_only_intent_never_silently_requests_quality_upgrade(user_id):
    assert intent(user_id, 1, upgrade=False).upgrade_to_1080 is False
    with pytest.raises(ValueError):
        intent(True, 1)


@pytest.mark.parametrize("language,quality", [(True, False), (False, True), (False, False)])
def test_unqualified_pair_cannot_begin_a_quality_acquisition(
    db, user_id, monkeypatch, language, quality
):
    from omakase.plus import language_publication, quality_publication

    monkeypatch.setattr(quality_publication, "QUALITY_PUBLICATION_QUALIFIED", quality)
    monkeypatch.setattr(language_publication, "LANGUAGE_PUBLICATION_QUALIFIED", language)
    selected = _monitor(db, user_id, 1)
    db.commit()
    provider = Mock()
    changes = db.total_changes
    receipt = run(db, user_id, selected, provider=provider)
    assert receipt.outcome == "not_qualified" and not receipt.monitor_results
    assert db.total_changes == changes
    provider.assert_not_called()
