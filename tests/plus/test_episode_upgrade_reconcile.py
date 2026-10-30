"""Manual upgrade work cannot borrow playable fallback coverage as success."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from omakase.plus.acquisition_policy import AcquisitionPolicy
from omakase.plus.media_store import upsert_asset
from omakase.plus.media_sync import reconcile_candidates
from omakase.plus.media_types import MediaRecheckIntent
from omakase.plus.realdebrid import RDFile
from tests.plus.test_media_sync import _candidate, _downloaded, _monitor, _rd
from tests.plus.test_media_sync import db as db
from tests.plus.test_media_sync import monitor_id as monitor_id


def existing(db, monitor, suffix="A", quality="720p", coverage=(1, 2)):
    return upsert_asset(
        db,
        monitor_id=monitor,
        torrent_hash=suffix * 40,
        source_title=f"Show S01 [Multi-Subs] [{quality}]",
        source_folder=f"Show {suffix}",
        quality=quality,
        ownership="monitor_owned",
        provenance="adopted",
        downloaded_once=True,
        planned_coverage=coverage,
        usable_coverage=coverage,
        inventory_fingerprint=suffix.lower() * 64,
        inventory_verified_at="2026-10-29T12:00:00Z",
        status="downloaded",
        progress=100,
    )


def run(db, monitor, candidates, rd, *, recheck=True, policy=None):
    owned = _monitor(db, monitor)
    args = (
        {"recheck": MediaRecheckIntent(owned["user_id"], monitor, upgrade_to_1080=True)}
        if recheck
        else {}
    )
    return asyncio.run(
        reconcile_candidates(
            db,
            owned,
            candidates,
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-29T15:00:00Z",
            policy=policy,
            **args,
        )
    )


def ready_provider():
    rd = _rd()
    rd.add_magnet.return_value = "rd-upgrade"
    rd.get_torrent.return_value = _downloaded(
        "rd-upgrade",
        RDFile(id=1, path="Show S01E01.mkv", bytes=100, selected=True),
        RDFile(id=2, path="Show S01E02.mkv", bytes=100, selected=True),
    )
    return rd


def test_only_explicit_recheck_searches_beyond_complete_playable_fallback(db, monitor_id):
    old = existing(db, monitor_id)
    rd = ready_provider()
    normal = run(db, monitor_id, [_candidate("B", {1, 2})], rd, recheck=False)
    assert normal.outcome == "complete" and not rd.add_magnet.await_count
    result = run(db, monitor_id, [_candidate("B", {1, 2})], rd)
    assert rd.add_magnet.await_count == 1 and result.outcome == "upgrade_pending"
    assert result.covered == {1, 2}
    assert "publication is unconfirmed" in result.detail
    assert db.execute(
        "SELECT quality,status,usable_coverage_json FROM media_assets WHERE id=?", (old,)
    ).fetchone()[:] == ("720p", "downloaded", "[1,2]")
    assert db.execute("SELECT count(*) FROM media_asset_removals").fetchone()[0] == 0


def test_downloaded_preferred_waiting_publication_does_not_create_duplicate_upgrade(db, monitor_id):
    existing(db, monitor_id)
    existing(db, monitor_id, suffix="B", quality="1080p")
    rd = ready_provider()
    result = run(db, monitor_id, [_candidate("C", {1, 2})], rd)
    assert not rd.add_magnet.await_count and result.outcome == "upgrade_pending"


def test_2160p_is_preserved_without_a_1080p_downgrade(db, monitor_id):
    existing(db, monitor_id, quality="2160p")
    rd = ready_provider()
    result = run(db, monitor_id, [_candidate("C", {1, 2})], rd)
    assert not rd.add_magnet.await_count and result.outcome == "complete"


def test_720p_can_fill_missing_episode_without_satisfying_upgrade(db, monitor_id):
    existing(db, monitor_id, coverage=(1,))
    rd = ready_provider()
    rd.get_torrent.return_value = _downloaded(
        "rd-upgrade", RDFile(id=2, path="Show S01E02.mkv", bytes=100, selected=True)
    )
    result = run(db, monitor_id, [_candidate("C", {2}, quality="720p")], rd)
    assert result.covered == {1, 2} and result.outcome == "upgrade_not_found"
    assert rd.add_magnet.await_count == 1


def test_user_resolution_criteria_block_unapproved_upgrade(db, monitor_id):
    existing(db, monitor_id)
    rd = ready_provider()
    result = run(
        db,
        monitor_id,
        [_candidate("C", {1, 2})],
        rd,
        policy=AcquisitionPolicy(allowed_resolutions=("720p",)),
    )
    assert result.outcome == "upgrade_blocked" and not rd.add_magnet.await_count


def test_new_download_is_pending_and_does_not_start_an_overlapping_second_copy(db, monitor_id):
    existing(db, monitor_id)
    rd = ready_provider()
    rd.add_magnet.side_effect = ["queued-b", "queued-c"]
    rd.get_torrent.return_value = replace(
        rd.get_torrent.return_value, status="downloading", progress=25
    )
    result = run(db, monitor_id, [_candidate("B", {1, 2}), _candidate("C", {1, 2})], rd)
    assert result.outcome == "upgrade_pending"
    assert "downloads are in progress" in result.detail
    assert "not found" not in result.detail
    assert rd.add_magnet.await_count == 1
    assert result.covered == {1, 2}
    assert (
        db.execute(
            "SELECT count(*) FROM media_assets WHERE quality='720p' AND status='downloaded'"
        ).fetchone()[0]
        == 1
    )


def test_old_fallback_download_does_not_mislabel_downloaded_preferred_as_downloading(
    db, monitor_id
):
    existing(db, monitor_id)
    existing(db, monitor_id, suffix="B", quality="1080p")
    pending = existing(db, monitor_id, suffix="C")
    db.execute(
        "UPDATE media_assets SET status='downloading',usable_coverage_json='[]' WHERE id=?",
        (pending,),
    )
    result = run(db, monitor_id, [], ready_provider())
    assert result.outcome == "upgrade_pending"
    assert "publication is unconfirmed" in result.detail
    assert "1080p downloads" not in result.detail
