from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omakase.plus import db as plus_db
from omakase.plus.catalog import AliasCatalogWindow, CatalogSearchReceipt
from omakase.plus.media_store import (
    exclude_asset_episodes,
    record_asset_observation,
    record_sync_run,
    upsert_asset,
    upsert_monitor,
)
from omakase.plus.media_sync import (
    _inventory_coverage,
    _rd_source_folder,
    _store_catalog_health,
    plan_stalled_asset_removals,
    process_pending_asset_removals,
    reconcile_candidates,
    reconcile_curator_blocked_assets,
    reconcile_monitor,
    schedule_monitor,
)
from omakase.plus.media_types import MediaCandidate, MediaSyncResult
from omakase.plus.realdebrid import RDFile, RDTorrent, RealDebridProviderBlock


@pytest.fixture
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def monitor_id(db: sqlite3.Connection) -> int:
    user_id = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("sync@example.com", "hash"),
    ).lastrowid
    assert user_id is not None
    return upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=999,
        title="Show Season 1",
        search_titles=("Show Season 1", "Show"),
        release_status="FINISHED",
        season_number=1,
        expected_episodes=2,
        aired_episodes=2,
    )


def _monitor(db: sqlite3.Connection, monitor_id: int) -> sqlite3.Row:
    row = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    assert row is not None
    return row


def _candidate(
    suffix: str,
    coverage: set[int],
    *,
    quality: str = "1080p",
    seeders: int = 10,
    alias: str = "Show",
) -> MediaCandidate:
    torrent_hash = (suffix * 40)[:40]
    return MediaCandidate(
        torrent_hash=torrent_hash,
        source_title=f"Show S01E{min(coverage):02d} [{quality}][Multi-Subs]",
        magnet=f"magnet:?xt=urn:btih:{torrent_hash}",
        seeders=seeders,
        size_bytes=1024,
        quality=quality,
        score=100,
        coverage=frozenset(coverage),
        search_alias=alias,
    )


def _downloaded(rd_id: str, *files: RDFile, filename: str = "Show Season 1") -> RDTorrent:
    return RDTorrent(
        id=rd_id,
        filename=filename,
        status="downloaded",
        progress=100,
        bytes_total=sum(file.bytes for file in files),
        bytes_done=sum(file.bytes for file in files),
        links=("https://example.invalid/media",),
        files=files,
    )


def _rd() -> SimpleNamespace:
    return SimpleNamespace(
        instant_available=AsyncMock(return_value=True),
        add_magnet=AsyncMock(),
        select_files=AsyncMock(return_value=True),
        get_torrent=AsyncMock(),
        list_torrents=AsyncMock(return_value=[]),
        delete_torrent=AsyncMock(return_value=True),
    )


def test_rd_source_folder_matches_zurg_single_file_and_batch_layouts() -> None:
    single = _downloaded("rd-single", filename="[Group] Show S01E01.mkv")
    batch = _downloaded("rd-batch", filename="[Group] Show Season 1")

    assert _rd_source_folder(single) == "[Group] Show S01E01"
    assert _rd_source_folder(batch) == "[Group] Show Season 1"


def test_download_link_block_tries_another_release_without_false_coverage(db, monitor_id):
    rd = _rd()
    rd.add_magnet.side_effect = ["rd-blocked", "rd-good"]
    rd.get_torrent.return_value = _downloaded("rd-good", RDFile(1, "Show S01E01.mkv", 100, True))
    rd.verify_download = AsyncMock(
        side_effect=[
            RealDebridProviderBlock(
                http_status=451, error_code="infringing_file", detail="private-url"
            ),
            True,
        ]
    )
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("A", {1}), _candidate("B", {1})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-09-19T08:00:00Z",
        )
    )
    assert result.covered == frozenset({1})
    assert len(result.accepted_assets) == 1
    rows = db.execute(
        "SELECT status,last_error,usable_coverage_json FROM media_assets ORDER BY id"
    ).fetchall()
    assert tuple(rows[0]) == ("quarantined", "PROVIDER_DOWNLOAD_BLOCKED", "[]")
    assert rows[1]["status"] == "downloaded"
    assert [
        r[0] for r in db.execute("SELECT decision FROM media_candidate_attempts ORDER BY id")
    ] == ["PROVIDER_BLOCK", "ACCEPTED"]
    rd.delete_torrent.assert_not_awaited()


@pytest.mark.parametrize("blocked", [True, False])
def test_quarantined_probe_reclassification_never_revives_asset(db, monitor_id, blocked):
    from omakase.plus.media_sync import _refresh_existing_assets

    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E01 [1080p]",
        rd_torrent_id="broken",
        status="quarantined",
        last_error="PLEX_MEDIA_PROBE_FAILED",
    )
    rd = _rd()
    rd.verify_download = AsyncMock(return_value=True)
    if blocked:
        rd.verify_download.side_effect = RealDebridProviderBlock(
            http_status=451, error_code="infringing_file", detail="private-url"
        )
    asyncio.run(
        _refresh_existing_assets(db, _monitor(db, monitor_id), rd=rd, now="2026-09-19T08:00:00Z")
    )
    row = db.execute(
        "SELECT status,last_error FROM media_assets WHERE id=?", (asset_id,)
    ).fetchone()
    assert tuple(row) == (
        "quarantined",
        "PROVIDER_DOWNLOAD_BLOCKED" if blocked else "PLEX_MEDIA_PROBE_FAILED",
    )
    rd.delete_torrent.assert_not_awaited()


def _blocked_asset_receipt(
    *,
    anilist_id: int,
    season_number: int,
    torrent_hash: str,
    code: str = "MEDIA_PROBE_FAILED",
    retryable: bool = True,
) -> dict:
    detail = {
        "MEDIA_PROBE_FAILED": (
            "The provider-backed media could not be read during the subtitle probe. "
            "It may be locked or temporarily unavailable."
        ),
        "POLICY_EXCLUDED": "This source is excluded from managed Plex publication by policy.",
    }[code]
    return {
        "schema_version": "curator-receipt-v2",
        "run_id": "curator-blocked-0001",
        "started_at": "2026-08-04T12:00:00Z",
        "completed_at": "2026-08-04T12:00:08Z",
        "outcome": "degraded",
        "mode": "live",
        "topology_converged": True,
        "manifest_generated_at": "2026-08-04T11:59:00Z",
        "observed_sections": [
            {"key": 1, "title": "Anime", "count": 10},
            {"key": 2, "title": "Anime Movies", "count": 0},
            {"key": 3, "title": "TV Shows", "count": 0},
            {"key": 4, "title": "Movies", "count": 0},
        ],
        "converged_section_counts": {
            "Anime": 10,
            "Anime Movies": 0,
            "TV Shows": 0,
            "Movies": 0,
        },
        "title_coverage": [
            {
                "anilist_id": anilist_id,
                "section": "Anime",
                "season_number": season_number,
                "usable_count": 1,
                "plex_visible_count": 0,
            }
        ],
        "blocked_assets": [
            {
                "anilist_id": anilist_id,
                "section": "Anime",
                "season_number": season_number,
                "torrent_hash": torrent_hash,
                "affected_episodes": [5],
                "code": code,
                "detail": detail,
                "retryable": retryable,
                "last_observed_at": "2026-08-04T12:00:08Z",
            }
        ],
        "errors": [],
    }


def test_reconcile_curator_blocked_asset_quarantines_it_for_retry(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    """Regression test for the KAMUI S01E05 incident: a curator-reported
    retryable block must reopen that episode for the next search instead of
    leaving the asset marked usable forever."""
    monitor = _monitor(db, monitor_id)
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E05 [1080p]",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=_completed_run(db, monitor_id, "2026-08-04T04:00:00Z"),
        downloaded_once=True,
        planned_coverage=(5,),
        usable_coverage=(5,),
        inventory_fingerprint="a" * 64,
        inventory_verified_at="2026-08-04T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    receipt = _blocked_asset_receipt(
        anilist_id=int(monitor["anilist_id"]),
        season_number=int(monitor["season_number"]),
        torrent_hash="A" * 40,
    )

    quarantined = reconcile_curator_blocked_assets(db, receipt)

    assert quarantined == (asset_id,)
    asset = db.execute(
        "SELECT status, last_error FROM media_assets WHERE id = ?", (asset_id,)
    ).fetchone()
    assert tuple(asset) == ("quarantined", "PLEX_MEDIA_PROBE_FAILED")
    # covered_episodes no longer counts the blocked asset, so episode 5 is
    # open for a fresh candidate search on the next scheduled sync.
    from omakase.plus.media_store import covered_episodes

    assert covered_episodes(db, monitor_id) == set()


def test_reconcile_curator_blocked_asset_is_idempotent(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    monitor = _monitor(db, monitor_id)
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E05 [1080p]",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=_completed_run(db, monitor_id, "2026-08-04T04:00:00Z"),
        downloaded_once=True,
        planned_coverage=(5,),
        usable_coverage=(5,),
        inventory_fingerprint="a" * 64,
        inventory_verified_at="2026-08-04T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    receipt = _blocked_asset_receipt(
        anilist_id=int(monitor["anilist_id"]),
        season_number=int(monitor["season_number"]),
        torrent_hash="A" * 40,
    )

    first = reconcile_curator_blocked_assets(db, receipt)
    second = reconcile_curator_blocked_assets(db, receipt)

    asset_id = db.execute(
        "SELECT id FROM media_assets WHERE torrent_hash = ?", ("A" * 40,)
    ).fetchone()[0]
    assert first == (asset_id,)
    assert second == ()


def test_reconcile_curator_blocked_asset_ignores_nonretryable_codes(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    monitor = _monitor(db, monitor_id)
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E05 [1080p]",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=_completed_run(db, monitor_id, "2026-08-04T04:00:00Z"),
        downloaded_once=True,
        planned_coverage=(5,),
        usable_coverage=(5,),
        inventory_fingerprint="a" * 64,
        inventory_verified_at="2026-08-04T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    receipt = _blocked_asset_receipt(
        anilist_id=int(monitor["anilist_id"]),
        season_number=int(monitor["season_number"]),
        torrent_hash="A" * 40,
        code="POLICY_EXCLUDED",
        retryable=False,
    )

    quarantined = reconcile_curator_blocked_assets(db, receipt)

    assert quarantined == ()
    asset = db.execute(
        "SELECT status FROM media_assets WHERE torrent_hash = ?", ("A" * 40,)
    ).fetchone()
    assert asset["status"] == "downloaded"


def test_catalog_health_aggregates_partial_alias_failures(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    user_id = int(_monitor(db, monitor_id)["user_id"])
    windows = (
        AliasCatalogWindow(
            alias="English",
            candidates=(),
            provider_complete=True,
            source_receipts=(CatalogSearchReceipt("prowlarr", "healthy", 9),),
        ),
        AliasCatalogWindow(
            alias="Romaji",
            candidates=(),
            provider_complete=False,
            error_code="PROWLARR_ERROR",
            source_receipts=(
                CatalogSearchReceipt(
                    "prowlarr",
                    "error",
                    0,
                    "PROWLARR_ERROR",
                ),
            ),
        ),
    )

    _store_catalog_health(
        db,
        user_id=user_id,
        windows=windows,
        now="2026-07-26T07:00:00Z",
    )

    row = db.execute(
        """SELECT health_status, reason_code, candidate_count, detail
           FROM media_catalog_health
           WHERE user_id = ? AND catalog_kind = 'prowlarr'""",
        (user_id,),
    ).fetchone()
    assert tuple(row) == (
        "degraded",
        "PARTIAL_CATALOG_ERROR",
        9,
        "Completed 2 searches; found 9 release results; 1 failed",
    )


def test_movie_inventory_requires_exactly_one_selected_media_file(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    db.execute(
        """UPDATE media_monitors
           SET format = 'MOVIE', season_number = NULL,
               expected_episodes = 1, aired_episodes = 1
           WHERE id = ?""",
        (monitor_id,),
    )
    monitor = _monitor(db, monitor_id)
    movie = _downloaded(
        "rd-movie",
        RDFile(id=1, path="Movie.Name.2160p.mkv", bytes=100, selected=True),
        RDFile(id=2, path="Movie.Name.nfo", bytes=10, selected=True),
        filename="Movie Name 2160p",
    )

    assert _inventory_coverage(movie, monitor).episodes == frozenset({1})

    ambiguous = _downloaded(
        "rd-ambiguous",
        RDFile(id=1, path="Movie.Name.mkv", bytes=100, selected=True),
        RDFile(id=2, path="Movie.Name.extra.mp4", bytes=50, selected=True),
        filename="Movie Name",
    )
    with pytest.raises(ValueError, match="exactly one selected media file"):
        _inventory_coverage(ambiguous, monitor)


def test_provider_block_falls_through_and_selected_inventory_is_authoritative(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    rd = _rd()
    rd.add_magnet.side_effect = [
        RealDebridProviderBlock(http_status=451, error_code="infringing_file", detail="blocked"),
        "rd-good",
    ]
    rd.get_torrent.return_value = _downloaded(
        "rd-good",
        RDFile(id=1, path="Show S01E01.mkv", bytes=100, selected=True),
        RDFile(id=2, path="Show S01E02.mkv", bytes=100, selected=False),
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("A", {1}), _candidate("B", {1, 2})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset({1})
    assert len(result.accepted_assets) == 1
    assert result.accepted_assets[0].usable_coverage == frozenset({1})
    attempts = db.execute(
        "SELECT decision, reason, rd_outcome FROM media_candidate_attempts ORDER BY id"
    ).fetchall()
    assert [row["decision"] for row in attempts] == ["PROVIDER_BLOCK", "ACCEPTED"]
    assert all("rd-good" not in row["rd_outcome"] for row in attempts)
    assert (
        db.execute(
            "SELECT usable_coverage_json FROM media_assets WHERE id = ?",
            (result.accepted_assets[0].id,),
        ).fetchone()[0]
        == "[1]"
    )


def test_complete_release_stops_before_redundant_lower_ranked_candidate(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    primary = replace(
        _candidate("A", {1, 2}, quality="1080p"),
        score=200,
        catalog_kind="prowlarr",
    )
    redundant = replace(_candidate("B", {1, 2}, quality="2160p"), score=100)
    rd = _rd()
    rd.add_magnet.return_value = "rd-primary"
    rd.get_torrent.return_value = _downloaded(
        "rd-primary",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [primary, redundant],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-26T10:30:00Z",
        )
    )

    assert result.outcome == "complete"
    assert result.covered == frozenset({1, 2})
    rd.add_magnet.assert_awaited_once_with(primary.magnet)
    assert (
        db.execute(
            "SELECT COUNT(*) FROM media_candidate_attempts WHERE torrent_hash = ?",
            (redundant.torrent_hash,),
        ).fetchone()[0]
        == 0
    )
    catalog_kind = db.execute(
        "SELECT catalog_kind FROM media_candidate_attempts WHERE torrent_hash = ?",
        (primary.torrent_hash,),
    ).fetchone()[0]
    assert catalog_kind == "prowlarr"


def test_zero_seed_negative_instant_check_never_creates_rd_item(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    rd = _rd()
    rd.instant_available.return_value = False

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("C", {1}, seeders=0)],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset()
    rd.add_magnet.assert_not_awaited()
    attempt = db.execute(
        "SELECT decision, instant_checked, instant_available FROM media_candidate_attempts"
    ).fetchone()
    assert tuple(attempt) == ("NOT_INSTANT", 1, 0)
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0


def test_reconcile_never_adds_a_candidate_with_no_uncovered_episode(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="E" * 40,
        source_title="Existing episode one",
        quality="1080p",
        ownership="external",
        provenance="adopted",
        downloaded_once=True,
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="e" * 64,
        inventory_verified_at="2026-07-15T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    rd = _rd()

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("D", {1, 88})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset({1})
    rd.add_magnet.assert_not_awaited()
    skipped = db.execute(
        "SELECT decision, reason FROM media_candidate_attempts WHERE torrent_hash = ?",
        ("D" * 40,),
    ).fetchone()
    assert tuple(skipped) == ("INELIGIBLE", "ALREADY_COVERED")


def test_reconcile_never_adds_a_candidate_outside_required_episode_set(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    rd = _rd()

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("X", {88})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-31T06:20:34Z",
        )
    )

    assert result.covered == frozenset()
    rd.add_magnet.assert_not_awaited()
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    skipped = db.execute(
        "SELECT decision, reason FROM media_candidate_attempts WHERE torrent_hash = ?",
        ("X" * 40,),
    ).fetchone()
    assert tuple(skipped) == ("INELIGIBLE", "OUTSIDE_REQUIRED_COVERAGE")
    receipt = db.execute(
        "SELECT eligible_1080p_count, attempted_1080p_count FROM media_alias_search_receipts"
    ).fetchone()
    assert tuple(receipt) == (0, 0)


def test_720p_is_used_only_after_every_1080p_attempt_is_terminal(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    rd = _rd()
    rd.add_magnet.side_effect = [
        RealDebridProviderBlock(http_status=451, error_code="blocked", detail="blocked"),
        RealDebridProviderBlock(http_status=451, error_code="blocked", detail="blocked"),
        "rd-720",
    ]
    rd.get_torrent.return_value = _downloaded(
        "rd-720",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [
                _candidate("D", {1}),
                _candidate("E", {2}),
                _candidate("F", {1, 2}, quality="720p"),
            ],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset({1, 2})
    fallback = db.execute(
        "SELECT decision, reason FROM media_candidate_attempts WHERE quality = '720p'"
    ).fetchone()
    assert tuple(fallback) == ("ACCEPTED", "FALLBACK_1080P_EXHAUSTED")
    receipt = db.execute(
        "SELECT eligible_1080p_count, attempted_1080p_count, exhaustion_outcome "
        "FROM media_alias_search_receipts"
    ).fetchone()
    assert tuple(receipt) == (2, 2, "CONFIGURED_CATALOG_CHECKED")


def test_reconcile_updates_the_existing_planning_download_columns(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    monitor = _monitor(db, monitor_id)
    planning_id = db.execute(
        """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
           VALUES (?, ?, ?, 'PLANNING')""",
        (monitor["user_id"], monitor["anilist_id"], monitor["title"]),
    ).lastrowid
    assert planning_id is not None
    upsert_monitor(
        db,
        user_id=monitor["user_id"],
        anilist_id=monitor["anilist_id"],
        title=monitor["title"],
        anilist_planning_id=planning_id,
    )
    rd = _rd()
    rd.add_magnet.return_value = "rd-planning"
    rd.get_torrent.return_value = _downloaded(
        "rd-planning",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("P", {1, 2})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.outcome == "complete"
    planning = db.execute(
        "SELECT download_status, download_info, rd_torrent_id FROM anilist_plannings"
    ).fetchone()
    assert tuple(planning) == ("complete", "Coverage 2/2; complete", "rd-planning")


def test_search_cap_blocks_720p_persistence(db: sqlite3.Connection, monitor_id: int) -> None:
    rd = _rd()
    rd.add_magnet.side_effect = RealDebridProviderBlock(
        http_status=451, error_code="blocked", detail="blocked"
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [
                _candidate("1", {1}),
                _candidate("2", {2}),
                _candidate("3", {1, 2}, quality="720p"),
            ],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
            max_candidates=1,
        )
    )

    assert result.covered == frozenset()
    assert rd.add_magnet.await_count == 1
    assert db.execute("SELECT COUNT(*) FROM media_assets WHERE quality = '720p'").fetchone()[0] == 0
    assert (
        db.execute("SELECT exhaustion_outcome FROM media_alias_search_receipts").fetchone()[0]
        == "SEARCH_CAP_REACHED"
    )


def test_recent_provider_block_is_skipped_so_the_next_run_checks_deeper_results(
    db: sqlite3.Connection,
    monitor_id: int,
) -> None:
    blocked = replace(_candidate("A", {1, 2}), score=200)
    first_provider = _rd()
    first_provider.add_magnet.side_effect = RealDebridProviderBlock(
        http_status=451,
        error_code="blocked",
        detail="blocked",
    )
    asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [blocked],
            rd=first_provider,
            searched_aliases=("Show",),
            now="2026-07-26T08:00:00Z",
            max_candidates=1,
        )
    )

    deeper = _candidate("B", {1, 2})
    second_provider = _rd()
    second_provider.add_magnet.return_value = "rd-deeper"
    second_provider.get_torrent.return_value = _downloaded(
        "rd-deeper",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
    )
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [blocked, deeper],
            rd=second_provider,
            searched_aliases=("Show",),
            now="2026-07-26T09:00:00Z",
            max_candidates=1,
        )
    )

    assert result.covered == frozenset({1, 2})
    second_provider.add_magnet.assert_awaited_once_with(deeper.magnet)
    latest_run = result.run_id
    decisions = [
        row["decision"]
        for row in db.execute(
            """SELECT decision FROM media_candidate_attempts
               WHERE sync_run_id = ? ORDER BY id""",
            (latest_run,),
        ).fetchall()
    ]
    assert decisions == ["SKIPPED_PREVIOUS_PROVIDER_BLOCK", "ACCEPTED"]
    assert "checked deeper results" in result.detail


def test_burned_in_subtitles_are_used_only_after_selectable_options_fail(
    db: sqlite3.Connection,
    monitor_id: int,
) -> None:
    selectable = _candidate("A", {1, 2})
    hardsub = MediaCandidate(
        torrent_hash="B" * 40,
        source_title="Show S01E01-E02 [1080p] [Hardsubs]",
        magnet=f"magnet:?xt=urn:btih:{'B' * 40}",
        seeders=10,
        size_bytes=1024,
        quality="1080p",
        score=90,
        coverage=frozenset({1, 2}),
        search_alias="Show",
    )
    rd = _rd()
    rd.add_magnet.side_effect = [
        RealDebridProviderBlock(http_status=451, error_code="blocked", detail="blocked"),
        "rd-hardsub",
    ]
    rd.get_torrent.return_value = _downloaded(
        "rd-hardsub",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
        filename=hardsub.source_title,
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [selectable, hardsub],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-26T10:00:00Z",
        )
    )

    assert result.outcome == "complete"
    accepted = db.execute(
        """SELECT reason FROM media_candidate_attempts
           WHERE decision = 'ACCEPTED'"""
    ).fetchone()
    assert accepted["reason"] == "BURNED_IN_SUBTITLE_FALLBACK"


def test_airing_monitor_checks_shortly_after_announced_release(
    db: sqlite3.Connection,
    monitor_id: int,
) -> None:
    next_airing_at = int(datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc).timestamp())
    db.execute(
        """UPDATE media_monitors
           SET release_status = 'RELEASING', next_airing_at = ?
           WHERE id = ?""",
        (next_airing_at, monitor_id),
    )
    result = MediaSyncResult(
        monitor_id=monitor_id,
        run_id=1,
        outcome="complete",
        required=frozenset({1, 2}),
        covered=frozenset({1, 2}),
        accepted_assets=(),
        detail="Coverage 2/2; complete",
    )

    schedule_monitor(
        db,
        monitor_id=monitor_id,
        now="2026-07-27T00:00:00Z",
        sync_result=result,
    )

    row = db.execute(
        "SELECT active, next_check_at FROM media_monitors WHERE id = ?",
        (monitor_id,),
    ).fetchone()
    assert tuple(row) == (1, "2026-07-27T01:20:00Z")


def test_reconcile_monitor_refreshes_existing_inventory_without_readding_hash(
    db: sqlite3.Connection, monitor_id: int, monkeypatch
) -> None:
    created_run = _completed_run(db, monitor_id, "2026-07-15T04:00:00Z")
    torrent_hash = "A" * 40
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash=torrent_hash,
        source_title="Show Season 1 Complete [1080p][Multi-Subs]",
        rd_torrent_id="rd-existing",
        quality="1080p",
        seeders=10,
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=created_run,
        planned_coverage=(1, 2),
        status="downloading",
    )
    candidate = _candidate("A", {1, 2})
    monkeypatch.setattr(
        "omakase.plus.media_sync.search_alias_windows",
        AsyncMock(
            return_value=(
                AliasCatalogWindow(
                    alias="Show",
                    candidates=(candidate,),
                    provider_complete=True,
                ),
            )
        ),
    )
    rd = _rd()
    rd.get_torrent.return_value = _downloaded(
        "rd-existing",
        RDFile(id=1, path="Show S01E01.mkv", bytes=100, selected=True),
        RDFile(id=2, path="Show S01E02.mkv", bytes=100, selected=True),
    )

    result = asyncio.run(
        reconcile_monitor(
            db,
            monitor_id=monitor_id,
            rd=rd,
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.outcome == "complete"
    assert result.covered == frozenset({1, 2})
    rd.add_magnet.assert_not_awaited()
    asset = db.execute(
        "SELECT status, usable_coverage_json, inventory_verified_at FROM media_assets"
    ).fetchone()
    assert tuple(asset) == ("downloaded", "[1,2]", "2026-07-15T08:00:00Z")
    observation = db.execute(
        """SELECT observation.rd_status, run.completed_at
           FROM media_asset_observations AS observation
           JOIN media_sync_runs AS run ON run.id = observation.sync_run_id"""
    ).fetchone()
    assert tuple(observation) == ("downloaded", "2026-07-15T08:00:00Z")


def test_existing_rd_hash_is_adopted_without_adding_duplicate(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    candidate = _candidate("A", {1, 2})
    summary = RDTorrent(
        id="rd-existing",
        filename="Show Season 1",
        status="downloaded",
        progress=100,
        bytes_total=200,
        bytes_done=200,
        links=(),
        torrent_hash=candidate.torrent_hash,
    )
    rd = _rd()
    rd.get_torrent.return_value = _downloaded(
        "rd-existing",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
    )

    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [candidate],
            rd=rd,
            searched_aliases=("Show",),
            existing_torrents={candidate.torrent_hash: summary},
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset({1, 2})
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    asset = db.execute(
        "SELECT ownership, provenance FROM media_assets WHERE torrent_hash = ?",
        (candidate.torrent_hash,),
    ).fetchone()
    assert tuple(asset) == ("external", "adopted")
    attempt = db.execute(
        "SELECT decision, reason, source_title FROM media_candidate_attempts"
    ).fetchone()
    assert tuple(attempt) == ("ADOPTED", "EXISTING_RD", candidate.source_title)


def test_refresh_preserves_playback_episode_exclusions(
    db: sqlite3.Connection, monitor_id: int, monkeypatch
) -> None:
    created_run = _completed_run(db, monitor_id, "2026-07-15T04:00:00Z")
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="F" * 40,
        source_title="Show Season 1 Complete [1080p][Multi-Subs]",
        rd_torrent_id="rd-health",
        quality="1080p",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=created_run,
        planned_coverage=(1, 2),
        usable_coverage=(1, 2),
        inventory_fingerprint="f" * 64,
        inventory_verified_at="2026-07-15T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    exclude_asset_episodes(
        db,
        asset_id=asset_id,
        episodes=(2,),
        reason="PLAYBACK_IO_ERROR",
    )
    gap_search = AsyncMock(return_value=())
    monkeypatch.setattr("omakase.plus.media_sync.search_alias_windows", gap_search)
    rd = _rd()
    rd.get_torrent.return_value = _downloaded(
        "rd-health",
        RDFile(id=1, path="Show S01E01.mkv", bytes=100, selected=True),
        RDFile(id=2, path="Show S01E02.mkv", bytes=100, selected=True),
    )

    result = asyncio.run(
        reconcile_monitor(
            db,
            monitor_id=monitor_id,
            rd=rd,
            now="2026-07-15T08:00:00Z",
        )
    )

    assert gap_search.await_args.kwargs["missing_episodes"] == [2]
    assert result.covered == frozenset({1})
    asset = db.execute(
        """SELECT usable_coverage_json, unusable_coverage_json, last_error
           FROM media_assets WHERE id = ?""",
        (asset_id,),
    ).fetchone()
    assert tuple(asset) == ("[1]", "[2]", "PLAYBACK_IO_ERROR")


def test_refresh_rebinds_rotated_rd_id_by_torrent_hash(
    db: sqlite3.Connection, monitor_id: int, monkeypatch
) -> None:
    created_run = _completed_run(db, monitor_id, "2026-07-15T04:00:00Z")
    torrent_hash = "9" * 40
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash=torrent_hash,
        source_title="Show Season 1 Complete [1080p][Multi-Subs]",
        rd_torrent_id="rd-stale",
        quality="1080p",
        ownership="external",
        provenance="adopted",
        created_by_run_id=created_run,
        planned_coverage=(1, 2),
        usable_coverage=(1, 2),
        inventory_fingerprint="9" * 64,
        inventory_verified_at="2026-07-15T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    exclude_asset_episodes(
        db,
        asset_id=asset_id,
        episodes=(2,),
        reason="PLAYBACK_IO_ERROR",
    )
    monkeypatch.setattr(
        "omakase.plus.media_sync.search_alias_windows",
        AsyncMock(return_value=()),
    )
    rd = _rd()
    rd.list_torrents.return_value = [
        RDTorrent(
            id="rd-current",
            filename="Show Season 1",
            status="downloaded",
            progress=100,
            bytes_total=200,
            bytes_done=200,
            links=(),
            torrent_hash=torrent_hash,
        )
    ]
    rd.get_torrent.side_effect = [
        None,
        _downloaded(
            "rd-current",
            RDFile(id=1, path="Show S01E01.mkv", bytes=100, selected=True),
            RDFile(id=2, path="Show S01E02.mkv", bytes=100, selected=True),
        ),
    ]

    result = asyncio.run(
        reconcile_monitor(
            db,
            monitor_id=monitor_id,
            rd=rd,
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset({1})
    asset = db.execute(
        """SELECT rd_torrent_id, usable_coverage_json, unusable_coverage_json
           FROM media_assets WHERE id = ?""",
        (asset_id,),
    ).fetchone()
    assert tuple(asset) == ("rd-current", "[1]", "[2]")


@pytest.mark.parametrize("provider_available", [True, False])
def test_refresh_preserves_plex_quarantine(
    db: sqlite3.Connection, monitor_id: int, provider_available: bool
) -> None:
    from omakase.plus.media_store import covered_episodes
    from omakase.plus.media_sync import _refresh_existing_assets

    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E01 [1080p]",
        rd_torrent_id="rd-broken",
        usable_coverage=(1,),
        inventory_fingerprint="a" * 64,
        inventory_verified_at="2026-09-19T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    receipt = _blocked_asset_receipt(anilist_id=999, season_number=1, torrent_hash="A" * 40)
    assert reconcile_curator_blocked_assets(db, receipt) == (asset_id,)
    rd = _rd()
    rd.get_torrent.return_value = (
        _downloaded("rd-broken", RDFile(id=1, path="Show S01E01.mkv", bytes=100, selected=True))
        if provider_available
        else None
    )
    for _ in range(2):
        asyncio.run(
            _refresh_existing_assets(
                db, _monitor(db, monitor_id), rd=rd, now="2026-09-19T08:00:00Z"
            )
        )
        assert covered_episodes(db, monitor_id) == set()
        row = db.execute(
            "SELECT status, last_error FROM media_assets WHERE id = ?", (asset_id,)
        ).fetchone()
        assert tuple(row) == ("quarantined", "PLEX_MEDIA_PROBE_FAILED")
    rd.get_torrent.assert_not_awaited()


def test_refresh_quarantines_wrong_title_without_touching_rd(
    db: sqlite3.Connection, monitor_id: int, monkeypatch
) -> None:
    db.execute(
        """UPDATE media_monitors
           SET anilist_id = 182205,
               title = 'That Time I Got Reincarnated as a Slime Season 4',
               search_titles_json = ?,
               season_number = 4,
               expected_episodes = NULL,
               aired_episodes = 16
           WHERE id = ?""",
        (
            '["That Time I Got Reincarnated as a Slime Season 4",'
            '"Tensei Shitara Slime Datta Ken 4th Season","Tensura 4"]',
            monitor_id,
        ),
    )
    created_run = _completed_run(db, monitor_id, "2026-07-15T04:00:00Z")
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="D" * 40,
        source_title="[SubsPlease] Tensura Nikki - Tensei Shitara Slime Datta Ken - 04",
        rd_torrent_id="rd-wrong",
        quality="1080p",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=created_run,
        downloaded_once=True,
        planned_coverage=(4,),
        usable_coverage=(4,),
        inventory_fingerprint="d" * 64,
        inventory_verified_at="2026-07-15T04:00:00Z",
        status="downloaded",
        progress=100,
    )
    monkeypatch.setattr(
        "omakase.plus.media_sync.search_alias_windows",
        AsyncMock(return_value=()),
    )
    rd = _rd()

    result = asyncio.run(
        reconcile_monitor(
            db,
            monitor_id=monitor_id,
            rd=rd,
            now="2026-07-15T08:00:00Z",
        )
    )

    assert result.covered == frozenset()
    rd.get_torrent.assert_not_awaited()
    asset = db.execute(
        "SELECT status, usable_coverage_json, inventory_verified_at, last_error FROM media_assets"
    ).fetchone()
    assert tuple(asset) == (
        "quarantined",
        "[]",
        None,
        "AMBIGUOUS_TARGET_IDENTITY",
    )
    attempt = db.execute(
        "SELECT decision, reason, source_title FROM media_candidate_attempts "
        "WHERE decision = 'QUARANTINED'"
    ).fetchone()
    assert tuple(attempt) == (
        "QUARANTINED",
        "AMBIGUOUS_TARGET_IDENTITY",
        "[SubsPlease] Tensura Nikki - Tensei Shitara Slime Datta Ken - 04",
    )


def _completed_run(db: sqlite3.Connection, monitor_id: int, completed_at: str) -> int:
    return record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="completed",
        completed_at=completed_at,
    )


def _removal_fixture(
    db: sqlite3.Connection,
    monitor_id: int,
    *,
    failed_guard: str | None = None,
) -> tuple[int, int]:
    created_run = _completed_run(db, monitor_id, "2026-07-14T20:00:00Z")
    source_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="D" * 40,
        source_title="Stalled S01E01",
        rd_torrent_id="rd-stalled",
        quality="1080p",
        ownership="external" if failed_guard == "external" else "monitor_owned",
        provenance="adopted" if failed_guard == "adopted" else "omakase_created",
        created_by_run_id=created_run,
        shared_reference_count=1 if failed_guard == "shared" else 0,
        downloaded_once=failed_guard == "downloaded_once",
        planned_coverage=(1,),
        status="downloading",
        progress=0,
    )
    first_run = _completed_run(db, monitor_id, "2026-07-15T00:00:00Z")
    first_observation_id = record_asset_observation(
        db,
        asset_id=source_id,
        sync_run_id=first_run,
        observed_at="2026-07-15T00:00:00Z",
        rd_status="downloading",
        bytes_done=0,
        progress=0,
    )
    if failed_guard != "one_observation":
        second_time = (
            "2026-07-15T03:00:00Z" if failed_guard == "too_soon" else "2026-07-15T04:00:00Z"
        )
        second_run = _completed_run(db, monitor_id, second_time)
        record_asset_observation(
            db,
            asset_id=source_id,
            sync_run_id=second_run,
            observed_at=second_time,
            rd_status="error" if failed_guard == "wrong_state" else "downloading",
            bytes_done=1 if failed_guard == "changed_bytes" else 0,
            progress=1 if failed_guard == "changed_progress" else 0,
        )

    replacement_run = _completed_run(db, monitor_id, "2026-07-15T05:00:00Z")
    replacement_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="E" * 40,
        source_title="Replacement S01E01",
        rd_torrent_id="rd-replacement",
        quality="1080p",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=replacement_run,
        planned_coverage=(1,),
        usable_coverage=() if failed_guard == "unusable_replacement" else (1,),
        inventory_fingerprint="verified",
        inventory_verified_at="2026-07-15T05:00:00Z",
        status="downloaded",
        replaces_asset_id=None if failed_guard == "missing_replacement_link" else source_id,
    )
    attempt_id = db.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, torrent_hash,
            candidate_rank, quality, seeders, proposed_coverage_json, decision,
            reason, rd_outcome, terminal_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            replacement_run,
            replacement_id,
            "Show",
            "E" * 40,
            1,
            "1080p",
            10,
            "[1]",
            "ACCEPTED",
            "",
            "downloaded",
            None if failed_guard == "nonterminal_attempt" else "2026-07-15T05:00:00Z",
        ),
    ).lastrowid
    assert attempt_id is not None and first_observation_id > 0
    return source_id, replacement_id


def test_stalled_owned_asset_plans_then_executes_deletion_under_lease(
    db: sqlite3.Connection, monitor_id: int
) -> None:
    source_id, replacement_id = _removal_fixture(db, monitor_id)

    removal_ids = plan_stalled_asset_removals(
        db,
        monitor_id=monitor_id,
        now="2026-07-15T08:00:00Z",
    )

    assert len(removal_ids) == 1
    removal = db.execute("SELECT * FROM media_asset_removals").fetchone()
    assert removal["outcome"] == "pending"
    assert removal["asset_id"] == source_id
    assert removal["replacement_asset_id"] == replacement_id

    db.execute(
        """INSERT INTO media_sync_lease
           (id, owner, acquired_at, heartbeat_at, expires_at, entrypoint)
           VALUES (1, ?, ?, ?, ?, ?)""",
        (
            "test-lease",
            "2026-07-15T07:59:00Z",
            "2026-07-15T08:00:00Z",
            "2026-07-15T08:30:00Z",
            "test",
        ),
    )
    rd = _rd()
    rd.get_torrent.return_value = RDTorrent(
        id="rd-stalled",
        filename="Stalled",
        status="downloading",
        progress=0,
        bytes_total=100,
        bytes_done=0,
        links=(),
        files=(),
    )

    processed = asyncio.run(
        process_pending_asset_removals(
            db,
            rd=rd,
            lease_owner="test-lease",
            now="2026-07-15T08:01:00Z",
        )
    )

    assert processed == 1
    rd.delete_torrent.assert_awaited_once_with("rd-stalled")
    assert db.execute("SELECT outcome FROM media_asset_removals").fetchone()[0] == "deleted"
    assert (
        db.execute("SELECT status FROM media_assets WHERE id = ?", (source_id,)).fetchone()[0]
        == "downloading"
    )


@pytest.mark.parametrize(
    "failed_guard",
    [
        "external",
        "adopted",
        "shared",
        "downloaded_once",
        "one_observation",
        "too_soon",
        "changed_bytes",
        "changed_progress",
        "wrong_state",
        "unusable_replacement",
        "missing_replacement_link",
        "nonterminal_attempt",
    ],
)
def test_stalled_asset_is_retained_when_a_deletion_guard_fails(
    db: sqlite3.Connection,
    monitor_id: int,
    failed_guard: str,
) -> None:
    _removal_fixture(db, monitor_id, failed_guard=failed_guard)

    assert (
        plan_stalled_asset_removals(
            db,
            monitor_id=monitor_id,
            now="2026-07-15T08:00:00Z",
        )
        == ()
    )
    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 0


def test_canonical_cour_inventory_uses_same_contract_as_release_titles(db, monitor_id):
    db.execute(
        "UPDATE media_monitors SET anilist_id=185874, season_number=2, expected_episodes=10, aired_episodes=8, release_status='RELEASING' WHERE id=?",
        (monitor_id,),
    )
    inventory = _downloaded(
        "fixture-cour4",
        RDFile(id=1, path="[Later] Bleach TYBW 41 Cour 4 Episode 1.mkv", bytes=100, selected=True),
        RDFile(id=2, path="[Later] Bleach TYBW 42 Cour 4 Episode 2.mkv", bytes=100, selected=True),
        RDFile(id=3, path="Bleach TYBW 50.mkv", bytes=100, selected=False),
        RDFile(id=4, path="Bleach TYBW 28 Cour 3 Episode 2.mkv", bytes=100, selected=True),
    )
    parsed = _inventory_coverage(inventory, _monitor(db, monitor_id))
    assert parsed.episodes == frozenset({1, 2})
    assert parsed.numbering_override == "bleach-tybw-cour4-absolute-v1"
    from omakase.plus.media_sync import _required_episodes

    assert _required_episodes(_monitor(db, monitor_id)) == frozenset(range(1, 9))


def test_slime_inventory_canonical_numbering_agrees_with_local04(db, monitor_id):
    db.execute(
        "UPDATE media_monitors SET anilist_id=182205, season_number=4, expected_episodes=24, aired_episodes=24 WHERE id=?",
        (monitor_id,),
    )
    inventory = _downloaded(
        "fixture-slime4",
        RDFile(
            id=1,
            path="Tensei Shitara Slime Datta Ken Season 4 - 04 [1080p] Episode 76.mkv",
            bytes=100,
            selected=True,
        ),
    )
    parsed = _inventory_coverage(inventory, _monitor(db, monitor_id))
    assert parsed.episodes == frozenset({4})
    assert parsed.numbering_override == "slime-s4-absolute-v1"


def test_1080p_alternative_is_exhausted_before_720p_fallback(db, monitor_id):
    rd = _rd()
    rd.add_magnet.side_effect = [
        RealDebridProviderBlock(http_status=451, error_code="blocked", detail="blocked"),
        "rd-alternative",
    ]
    rd.get_torrent.return_value = _downloaded(
        "rd-alternative",
        RDFile(id=1, path="Show S01E01-E02.mkv", bytes=200, selected=True),
    )
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [
                replace(_candidate("D", {1, 2}), score=200),
                replace(_candidate("E", {1, 2}), score=100),
                _candidate("F", {1, 2}, quality="720p"),
            ],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )
    assert result.covered == frozenset({1, 2})
    assert rd.add_magnet.await_count == 2
    assert all(asset.quality == "1080p" for asset in result.accepted_assets)
    assert db.execute("SELECT COUNT(*) FROM media_assets WHERE quality='720p'").fetchone()[0] == 0


def test_720p_gap_fill_preserves_usable_1080p(db, monitor_id):
    run = _completed_run(db, monitor_id, "2026-07-15T04:00:00Z")
    original = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E01 [1080p] [Multi-Subs]",
        quality="1080p",
        planned_coverage=(1,),
        usable_coverage=(1,),
        status="downloaded",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=run,
        inventory_fingerprint="f" * 64,
        inventory_verified_at="2026-07-15T04:00:00Z",
    )
    rd = _rd()
    rd.add_magnet.return_value = "rd-gap"
    rd.get_torrent.return_value = _downloaded(
        "rd-gap",
        RDFile(id=1, path="Show S01E02.mkv", bytes=100, selected=True),
    )
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("F", {2}, quality="720p")],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
        )
    )
    assert result.covered == frozenset({1, 2})
    assert result.accepted_assets[0].quality == "720p"
    assert result.accepted_assets[0].usable_coverage == frozenset({2})
    original_row = db.execute(
        "SELECT quality,usable_coverage_json,status FROM media_assets WHERE id=?", (original,)
    ).fetchone()
    assert tuple(original_row) == ("1080p", "[1]", "downloaded")
    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 0


@pytest.mark.parametrize("failure", ["blocked", "uncached"])
def test_hardsub_backup_fills_gap_after_failed_primary_and_retains_original(
    db, monitor_id, failure
):
    from omakase.plus.acquisition_policy import AcquisitionPolicy

    run = _completed_run(db, monitor_id, "2026-07-15T04:00:00Z")
    original = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E01 [1080p] [Multi-Subs]",
        quality="1080p",
        planned_coverage=(1,),
        usable_coverage=(1,),
        status="downloaded",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=run,
        inventory_fingerprint="f" * 64,
        inventory_verified_at="2026-07-15T04:00:00Z",
    )
    primary = replace(
        _candidate("D", {2}, seeders=0 if failure == "uncached" else 10),
        source_title="Show S01E02 [1080p] [English Hardsubs]",
        score=200,
    )
    backup = replace(
        _candidate("E", {2}), source_title="Show S01E02 [1080p] [English Hardsubs]", score=100
    )
    rd = _rd()
    if failure == "blocked":
        rd.add_magnet.side_effect = [
            RealDebridProviderBlock(http_status=451, error_code="blocked", detail="blocked"),
            "rd-hardsub-backup",
        ]
    else:
        rd.instant_available.return_value = False
        rd.add_magnet.return_value = "rd-hardsub-backup"
    rd.get_torrent.return_value = _downloaded(
        "rd-hardsub-backup", RDFile(id=1, path="Show S01E02.mkv", bytes=100, selected=True)
    )
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [primary, backup],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-07-15T08:00:00Z",
            policy=AcquisitionPolicy(allow_hardsub_fallback=True),
        )
    )
    assert result.covered == frozenset({1, 2})
    assert len(result.accepted_assets) == 1
    assert result.accepted_assets[0].torrent_hash == "E" * 40
    assert result.accepted_assets[0].quality == "1080p"
    attempts = db.execute(
        "SELECT torrent_hash,decision,reason FROM media_candidate_attempts ORDER BY id"
    ).fetchall()
    assert tuple(attempts[-1]) == ("E" * 40, "ACCEPTED", "BURNED_IN_SUBTITLE_FALLBACK")
    assert tuple(attempts[-2]) == (
        "D" * 40,
        "PROVIDER_BLOCK" if failure == "blocked" else "NOT_INSTANT",
        "PROVIDER_BLOCK" if failure == "blocked" else "ZERO_SEED_NOT_INSTANT",
    )
    original_row = db.execute(
        "SELECT quality,usable_coverage_json,status FROM media_assets WHERE id=?", (original,)
    ).fetchone()
    assert tuple(original_row) == ("1080p", "[1]", "downloaded")
    assert db.execute("SELECT COUNT(*) FROM media_asset_removals").fetchone()[0] == 0


@pytest.fixture
def identity_reprobe(db, monitor_id):
    from omakase.plus.media_store import acquire_sync_lease

    original = _monitor(db, monitor_id)
    upsert_monitor(
        db,
        user_id=int(original["user_id"]),
        anilist_id=182205,
        title="That Time I Got Reincarnated as a Slime Season 4",
        search_titles=("Tensei Shitara Slime Datta Ken 4th Season",),
        release_status="FINISHED",
        season_number=4,
        expected_episodes=24,
        aired_episodes=24,
    )
    monitor = db.execute("SELECT * FROM media_monitors WHERE anilist_id=182205").fetchone()
    source = "Tensei Shitara Slime Datta Ken 4th Season - 16 [1080p]"
    run = record_sync_run(db, monitor_id=monitor["id"], outcome="running")
    asset_id = upsert_asset(
        db,
        monitor_id=monitor["id"],
        torrent_hash="A" * 40,
        source_title=source,
        rd_torrent_id="owned-rd",
        quality="1080p",
        ownership="monitor_owned",
        provenance="omakase_created",
        created_by_run_id=run,
        planned_coverage=(16,),
        usable_coverage=(),
        status="quarantined",
        last_error="TARGET_TITLE_MISMATCH",
        downloaded_once=True,
    )
    lease = acquire_sync_lease(
        db,
        owner="reprobe-test",
        entrypoint="identity-reprobe",
        acquired_at="2026-10-11T09:00:00Z",
        expires_at="2026-10-11T10:00:00Z",
    )
    assert lease.acquired
    rd = _rd()
    rd.verify_download = AsyncMock(return_value=True)
    rd.get_torrent.return_value = replace(
        _downloaded("owned-rd", RDFile(1, source + ".mkv", 100, True), filename=source),
        torrent_hash="A" * 40,
    )
    db.commit()
    return monitor, asset_id, rd


def _identity_reprobe_call(db, target):
    from omakase.plus.media_sync import reprobe_identity_assets

    monitor, asset_id, rd = target
    return asyncio.run(
        reprobe_identity_assets(
            db,
            user_id=monitor["user_id"],
            monitor_id=monitor["id"],
            asset_ids=(asset_id,),
            rd=rd,
            now="2026-10-11T09:30:00Z",
            lease_owner="reprobe-test",
        )
    )


def test_explicit_identity_reprobe_recovers_without_adoption_or_quarantine_clearing(
    db, identity_reprobe
):
    from omakase.plus.media_sync import _refresh_existing_assets

    monitor, asset_id, rd = identity_reprobe
    before = db.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    asyncio.run(_refresh_existing_assets(db, monitor, rd=rd, now="2026-10-11T09:30:00Z"))
    rd.get_torrent.assert_not_awaited()
    assert _identity_reprobe_call(db, identity_reprobe) == (asset_id,)
    after = db.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
    assert (after["status"], after["last_error"], after["usable_coverage_json"]) == (
        "downloaded",
        "",
        "[16]",
    )
    assert len(after["inventory_fingerprint"]) == 64
    for field in (
        "ownership",
        "provenance",
        "created_by_run_id",
        "rd_torrent_id",
        "provider_asset_id",
        "planned_coverage_json",
        "downloaded_once",
    ):
        assert after[field] == before[field]
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    rd.delete_torrent.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    [
        "blocked",
        "access",
        "hash",
        "id",
        "queued",
        "progress",
        "empty",
        "unselected",
        "zero_bytes",
        "wrong_episode",
        "wrong_identity",
    ],
)
def test_failed_identity_reprobe_preserves_original_quarantine_evidence(
    db, identity_reprobe, failure
):
    monitor, asset_id, rd = identity_reprobe
    item = rd.get_torrent.return_value
    if failure == "blocked":
        rd.verify_download.side_effect = RealDebridProviderBlock(
            http_status=451, error_code="infringing_file", detail="private"
        )
    elif failure == "access":
        rd.verify_download.return_value = False
    elif failure == "hash":
        rd.get_torrent.return_value = replace(item, torrent_hash="B" * 40)
    elif failure == "id":
        rd.get_torrent.return_value = replace(item, id="different-rd")
    elif failure == "queued":
        rd.get_torrent.return_value = replace(item, status="queued")
    elif failure == "progress":
        rd.get_torrent.return_value = replace(item, progress=99.0)
    elif failure == "empty":
        rd.get_torrent.return_value = replace(item, files=())
    elif failure in {"unselected", "zero_bytes"}:
        file = replace(
            item.files[0],
            selected=failure != "unselected",
            bytes=0 if failure == "zero_bytes" else 100,
        )
        rd.get_torrent.return_value = replace(item, files=(file,))
    elif failure == "wrong_episode":
        rd.get_torrent.return_value = replace(
            item,
            files=(
                replace(item.files[0], path="Tensei Shitara Slime Datta Ken 4th Season - 17.mkv"),
            ),
        )
    elif failure == "wrong_identity":
        rd.get_torrent.return_value = replace(
            item, filename="Tensei Shitara Slime Datta Ken 5th Season - 16"
        )
    before = tuple(db.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone())
    assert _identity_reprobe_call(db, identity_reprobe) == ()
    assert (
        tuple(db.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()) == before
    )
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    rd.delete_torrent.assert_not_awaited()


def test_identity_reprobe_respects_recent_provider_block_cooldown(db, identity_reprobe):
    from omakase.plus.media_store import record_candidate_attempt

    monitor, asset_id, rd = identity_reprobe
    run = record_sync_run(db, monitor_id=monitor["id"], outcome="running")
    record_candidate_attempt(
        db,
        monitor_id=monitor["id"],
        sync_run_id=run,
        search_alias="Slime",
        torrent_hash="A" * 40,
        source_title="Slime Season 4",
        candidate_rank=1,
        quality="1080p",
        seeders=10,
        proposed_coverage=(16,),
        decision="PROVIDER_BLOCK",
        reason="PROVIDER_BLOCK",
        rd_outcome="provider_block",
        terminal_at="2026-10-10T09:00:00Z",
    )
    assert _identity_reprobe_call(db, identity_reprobe) == ()
    rd.get_torrent.assert_not_awaited()


@pytest.mark.parametrize(
    "reason", ["PROVIDER_DOWNLOAD_BLOCKED", "PLEX_MEDIA_PROBE_FAILED", "AMBIGUOUS_TARGET_IDENTITY"]
)
def test_identity_reprobe_cannot_override_other_quarantines(db, identity_reprobe, reason):
    monitor, asset_id, rd = identity_reprobe
    db.execute("UPDATE media_assets SET last_error=? WHERE id=?", (reason, asset_id))
    with pytest.raises(ValueError, match="identity quarantine"):
        _identity_reprobe_call(db, identity_reprobe)
    rd.get_torrent.assert_not_awaited()


def test_identity_reprobe_does_not_revive_excluded_episode(db, identity_reprobe):
    monitor, asset_id, rd = identity_reprobe
    exclude_asset_episodes(db, asset_id=asset_id, episodes=(16,), reason="owner excluded")
    before = tuple(db.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone())
    with pytest.raises(ValueError, match="identity quarantine"):
        _identity_reprobe_call(db, identity_reprobe)
    assert (
        tuple(db.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()) == before
    )


def test_identity_reprobe_requires_current_lease(db, identity_reprobe):
    monitor, asset_id, rd = identity_reprobe
    db.execute("UPDATE media_sync_lease SET expires_at='2026-10-11T09:00:00Z'")
    with pytest.raises(ValueError, match="current sync lease"):
        _identity_reprobe_call(db, identity_reprobe)
    rd.get_torrent.assert_not_awaited()


def test_identity_reprobe_fails_if_asset_changes_during_access_validation(db, identity_reprobe):
    monitor, asset_id, rd = identity_reprobe

    async def changed(_):
        db.execute("UPDATE media_assets SET seeders=777 WHERE id=?", (asset_id,))
        return True

    rd.verify_download.side_effect = changed
    with pytest.raises(ValueError, match="evidence changed"):
        _identity_reprobe_call(db, identity_reprobe)
    row = db.execute(
        "SELECT status,last_error,usable_coverage_json FROM media_assets WHERE id=?", (asset_id,)
    ).fetchone()
    assert tuple(row) == ("quarantined", "TARGET_TITLE_MISMATCH", "[]")


def test_identity_reprobe_concurrent_owner_quarantine_cannot_be_overwritten(
    db,
    identity_reprobe,
    tmp_path,
    monkeypatch,
):
    from omakase.plus import media_sync

    path = tmp_path / "reprobe-race.db"
    disk = sqlite3.connect(path, timeout=0)
    db.backup(disk)
    disk.row_factory = sqlite3.Row
    disk.execute("PRAGMA journal_mode=WAL")
    other = sqlite3.connect(path, timeout=0)
    monitor, asset_id, rd = identity_reprobe
    target = (_monitor(disk, monitor["id"]), asset_id, rd)
    original_context = media_sync._provider_context
    provider_count = disk.execute("SELECT COUNT(*) FROM media_providers").fetchone()[0]
    interleaved = []

    def owner_quarantines_between_validation_and_write(conn, current_monitor, provider):
        # WAL permits this independent owner commit after the reprobe's fresh
        # reads. Its transaction must fail the write upgrade, never overwrite it.
        assert conn.in_transaction
        other.execute(
            "UPDATE media_assets SET last_error='OWNER_QUARANTINE' WHERE id=?",
            (asset_id,),
        )
        other.commit()
        interleaved.append(True)
        return original_context(conn, current_monitor, provider)

    monkeypatch.setattr(
        media_sync, "_provider_context", owner_quarantines_between_validation_and_write
    )
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _identity_reprobe_call(disk, target)
        assert interleaved == [True]
        row = disk.execute(
            "SELECT status,last_error,usable_coverage_json,inventory_fingerprint FROM media_assets WHERE id=?",
            (asset_id,),
        ).fetchone()
        assert tuple(row) == ("quarantined", "OWNER_QUARANTINE", "[]", "")
        assert not disk.in_transaction
        assert disk.execute("SELECT COUNT(*) FROM media_providers").fetchone()[0] == provider_count
    finally:
        other.close()
        disk.close()


@pytest.mark.parametrize(
    "failure", ["", "hash", "progress", "empty", "identity", "ambiguous", "access", "uncached"]
)
def test_metadata_plan_requires_independent_selected_provider_evidence(db, monitor_id, failure):
    candidate = replace(
        _candidate("A", {1, 2}),
        metadata_backed=True,
        source_title="Show Season 1 [1080p][Multi-Subs]",
        seeders=0 if failure == "uncached" else 10,
    )
    file = RDFile(1, "Show S01E01.mkv", 100, True)
    if failure == "empty":
        file = replace(file, bytes=0)
    elif failure == "identity":
        file = replace(file, path="Unrelated Series S01E01.mkv")
    elif failure == "ambiguous":
        file = replace(file, path="Show Season 1.mkv")
    item = replace(
        _downloaded("rd-one", file),
        torrent_hash=("B" if failure == "hash" else "A") * 40,
        progress=99 if failure == "progress" else 100,
    )
    rd = _rd()
    rd.add_magnet.return_value = "rd-one"
    rd.get_torrent.return_value = item
    rd.verify_download = AsyncMock(return_value=failure != "access")
    rd.instant_available.return_value = failure != "uncached"
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [candidate],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T08:00:00Z",
        )
    )
    assert result.covered == (frozenset({1}) if not failure else frozenset())
    if failure == "uncached":
        rd.instant_available.assert_awaited_once()
        rd.add_magnet.assert_not_awaited()
    else:
        rd.instant_available.assert_not_awaited()
    if not failure:
        row = db.execute(
            "SELECT planned_coverage_json,usable_coverage_json FROM media_assets"
        ).fetchone()
        assert row[0] == "[1,2]"
        assert row[1] == "[1]"


def test_reconcile_metadata_budget_excludes_owned_assets_and_current_provider_cooldowns(
    db, monitor_id, monkeypatch
):
    from omakase.plus.media_sync import _record_attempt

    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E01 [1080p]",
        status="quarantined",
        last_error="TARGET_TITLE_MISMATCH",
    )
    run_id = record_sync_run(db, monitor_id=monitor_id, outcome="running")
    _record_attempt(
        db,
        monitor_id=monitor_id,
        run_id=run_id,
        candidate=_candidate("B", {1}),
        searched_aliases=("Show",),
        rank=1,
        decision="PROVIDER_BLOCK",
        now="2026-10-11T08:00:00Z",
    )
    now = db.execute("SELECT datetime('now')").fetchone()[0].replace(" ", "T") + "Z"
    search = AsyncMock(return_value=())
    monkeypatch.setattr("omakase.plus.media_sync.search_alias_windows", search)
    rd = _rd()
    asyncio.run(reconcile_monitor(db, monitor_id=monitor_id, rd=rd, now=now))
    assert search.await_args.kwargs["metadata_excluded_hashes"] == frozenset({"A" * 40, "B" * 40})
    row = db.execute("SELECT status,last_error FROM media_assets").fetchone()
    assert tuple(row) == ("quarantined", "TARGET_TITLE_MISMATCH")
    rd.add_magnet.assert_not_awaited()


@pytest.mark.parametrize("initial", ["rejected", "queued"])
@pytest.mark.parametrize(
    "failure", ["hash", "progress", "empty", "identity", "range", "conflict", ""]
)
def test_metadata_strict_validation_survives_later_refresh_and_normal_candidate_upserts(
    db, monitor_id, initial, failure
):
    from omakase.plus.media_sync import _refresh_existing_assets

    candidate = replace(
        _candidate("A", {1, 2}),
        metadata_backed=True,
        source_title="Show Season 1 [1080p][Multi-Subs]",
    )
    good = replace(
        _downloaded("rd-one", RDFile(1, "Show S01E01.mkv", 100, True)), torrent_hash="A" * 40
    )
    initial_item = (
        replace(good, progress=99)
        if initial == "rejected"
        else replace(good, status="downloading", progress=50)
    )
    rd = _rd()
    rd.add_magnet.return_value = "rd-one"
    rd.get_torrent.return_value = initial_item
    rd.verify_download = AsyncMock(return_value=True)
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [candidate],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T08:00:00Z",
        )
    )
    assert not result.covered
    row = db.execute(
        "SELECT requires_strict_inventory,usable_coverage_json FROM media_assets"
    ).fetchone()
    assert tuple(row) == (1, "[]")
    file = good.files[0]
    if failure == "empty":
        file = replace(file, bytes=0)
    elif failure == "identity":
        file = replace(file, path="Unrelated Series S01E01.mkv")
    elif failure == "range":
        file = replace(file, path="Show S01E02-E03.mkv")
    elif failure == "conflict":
        file = replace(file, path="Show S01E01 EP02.mkv")
    current = replace(
        good,
        files=(file,),
        torrent_hash=("B" if failure == "hash" else "A") * 40,
        progress=99 if failure == "progress" else 100,
    )
    rd.get_torrent.return_value = current
    asyncio.run(
        _refresh_existing_assets(db, _monitor(db, monitor_id), rd=rd, now="2026-10-11T09:00:00Z")
    )
    row = db.execute(
        "SELECT requires_strict_inventory,usable_coverage_json,inventory_verified_at FROM media_assets"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == ("[]" if failure else "[1]")
    assert bool(row[2]) == (not failure)
    # A later ordinary candidate cannot remove the sticky requirement either.
    asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [replace(candidate, metadata_backed=False)],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T10:00:00Z",
        )
    )
    row = db.execute(
        "SELECT requires_strict_inventory,usable_coverage_json FROM media_assets"
    ).fetchone()
    assert row[0] == 1
    assert row[1] == ("[]" if failure else "[1]")


def test_metadata_requirement_upsert_rolls_back_together_on_flag_write_failure(db, monitor_id):
    db.execute(
        "CREATE TRIGGER reject_strict_flag BEFORE UPDATE OF requires_strict_inventory ON media_assets BEGIN SELECT RAISE(ABORT, 'test'); END"
    )
    rd = _rd()
    rd.add_magnet.return_value = "rd-one"
    rd.get_torrent.return_value = replace(
        _downloaded("rd-one", RDFile(1, "Show S01E01.mkv", 100, True)), torrent_hash="A" * 40
    )
    rd.verify_download = AsyncMock(return_value=True)
    candidate = replace(_candidate("A", {1}), metadata_backed=True)
    with pytest.raises(sqlite3.IntegrityError):
        asyncio.run(
            reconcile_candidates(
                db,
                _monitor(db, monitor_id),
                [candidate],
                rd=rd,
                searched_aliases=("Show",),
                now="2026-10-11T08:00:00Z",
            )
        )
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 0


def test_previously_verified_metadata_asset_rechecks_access_before_refresh(db, monitor_id):
    from omakase.plus.media_sync import _refresh_existing_assets

    candidate = replace(_candidate("A", {1}), metadata_backed=True)
    rd = _rd()
    rd.add_magnet.return_value = "rd-one"
    rd.get_torrent.return_value = replace(
        _downloaded("rd-one", RDFile(1, "Show S01E01.mkv", 100, True)), torrent_hash="A" * 40
    )
    rd.verify_download = AsyncMock(return_value=True)
    asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [candidate],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T08:00:00Z",
        )
    )
    rd.verify_download.return_value = False
    asyncio.run(
        _refresh_existing_assets(db, _monitor(db, monitor_id), rd=rd, now="2026-10-11T09:00:00Z")
    )
    row = db.execute(
        "SELECT requires_strict_inventory,usable_coverage_json FROM media_assets"
    ).fetchone()
    assert tuple(row) == (1, "[]")
    assert rd.verify_download.await_count == 2


def _complete_with_cleanup(db, monitor_id):
    from omakase.plus.completion_cleanup import set_completion_cleanup_preference
    from omakase.plus.watch.models import WatchEvent
    from omakase.plus.watch.service import ingest_watch_event

    owner = _monitor(db, monitor_id)["user_id"]
    set_completion_cleanup_preference(db, user_id=owner, remove_from_plex=True)
    db.commit()
    return ingest_watch_event(
        db,
        user_id=owner,
        monitor_id=monitor_id,
        event=WatchEvent.complete(source="manual", event_key="completed-during-sync"),
        score_ten=9,
        observed_at="2026-10-11T12:00:00Z",
    )


def test_completion_during_catalog_await_stops_remaining_searches_and_acquisition(
    db, monitor_id, monkeypatch
):
    async def exercise():
        entered = asyncio.Event()
        completed = asyncio.Event()

        async def catalog_search(alias):
            entered.set()
            await completed.wait()
            return []

        search = AsyncMock(side_effect=catalog_search)
        monkeypatch.setattr("omakase.plus.catalog.search", search)
        rd = _rd()
        task = asyncio.create_task(
            reconcile_monitor(db, monitor_id=monitor_id, rd=rd, now="2026-10-11T11:59:00Z")
        )
        await entered.wait()
        completion = _complete_with_cleanup(db, monitor_id)
        assert completion.cleanup["status"] == "awaiting_plex"
        completed.set()
        result = await task
        assert result.outcome == "withdrawn"
        assert search.await_count == 1
        rd.add_magnet.assert_not_awaited()
        assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 0
        assert db.execute("SELECT status FROM watch_entries").fetchone()[0] == "completed"
        assert db.execute("SELECT score_ten FROM watch_events").fetchone()[0] == 9
        assert db.execute("SELECT score_ten FROM tracker_sync_outbox").fetchone()[0] == 9

    asyncio.run(exercise())


@pytest.mark.parametrize("withdrawal", ["removal", "blocked"])
def test_normal_and_failed_scheduler_cannot_resurrect_durable_withdrawal(
    db, monitor_id, withdrawal
):
    from omakase.plus.media_sync import _schedule_monitor_failure, _selected_monitors

    if withdrawal == "removal":
        _complete_with_cleanup(db, monitor_id)
    else:
        db.execute(
            """INSERT INTO completion_plex_cleanup_blocks(monitor_id,user_id,requested_at,detail)
            VALUES (?, ?, '2026-10-11T12:00:00Z', 'scope conflict')""",
            (monitor_id, _monitor(db, monitor_id)["user_id"]),
        )
        db.execute(
            "UPDATE media_monitors SET active=0,next_check_at=NULL,last_result='COMPLETION_CLEANUP_BLOCKED' WHERE id=?",
            (monitor_id,),
        )
        db.commit()
    before = dict(_monitor(db, monitor_id))
    result = MediaSyncResult(
        monitor_id=monitor_id,
        run_id=0,
        outcome="degraded",
        required=frozenset({1, 2}),
        covered=frozenset(),
        accepted_assets=(),
        detail="empty search",
    )
    schedule_monitor(db, monitor_id=monitor_id, now="2026-10-11T13:00:00Z", sync_result=result)
    _schedule_monitor_failure(
        db, monitor_id=monitor_id, now="2026-10-11T13:00:00Z", detail="SYNC_FAILED"
    )
    assert dict(_monitor(db, monitor_id)) == before
    # Even a stale old process which has already written active=1 cannot reselect it.
    db.execute("UPDATE media_monitors SET active=1,next_check_at=NULL WHERE id=?", (monitor_id,))
    assert _selected_monitors(db, now="2026-10-11T13:00:00Z", monitor_ids=None) == []
    assert _selected_monitors(db, now="2026-10-11T13:00:00Z", monitor_ids=[monitor_id]) == []


def test_withdrawn_monitor_entrypoints_never_create_provider_or_acquire(db, monitor_id):
    from omakase.plus.media_sync import run_media_sync

    _complete_with_cleanup(db, monitor_id)

    def forbidden_factory(owner_id):
        raise AssertionError("withdrawn monitor must not create provider")

    receipt = asyncio.run(
        run_media_sync(
            db,
            entrypoint="test",
            monitor_ids=[monitor_id],
            now="2026-10-11T13:00:00Z",
            provider_factory=forbidden_factory,
        )
    )
    assert receipt.monitor_results == ()
    rd = _rd()
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("A", {1, 2})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T13:00:00Z",
        )
    )
    assert result.outcome == "withdrawn"
    rd.add_magnet.assert_not_awaited()


def test_completion_during_provider_refresh_preserves_existing_assets(db, monitor_id):
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Show S01E01 [1080p][Multi-Subs]",
        rd_torrent_id="existing",
        quality="1080p",
        planned_coverage=(1,),
        status="downloading",
    )
    db.commit()
    before = dict(db.execute("SELECT * FROM media_assets").fetchone())
    rd = _rd()

    async def complete_in_inventory():
        _complete_with_cleanup(db, monitor_id)
        return []

    rd.list_torrents.side_effect = complete_in_inventory
    result = asyncio.run(
        reconcile_monitor(db, monitor_id=monitor_id, rd=rd, now="2026-10-11T11:59:00Z")
    )
    assert result.outcome == "withdrawn"
    assert dict(db.execute("SELECT * FROM media_assets").fetchone()) == before
    rd.get_torrent.assert_not_awaited()
    rd.add_magnet.assert_not_awaited()


def test_completion_during_instant_lookup_prevents_new_provider_add(db, monitor_id):
    rd = _rd()

    async def complete_during_check(content_hash):
        _complete_with_cleanup(db, monitor_id)
        return True

    rd.instant_available.side_effect = complete_during_check
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("A", {1, 2}, seeders=0)],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T11:59:00Z",
        )
    )
    assert result.outcome == "withdrawn"
    rd.add_magnet.assert_not_awaited()
    assert db.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["add", "select", "info"])
def test_inflight_source_on_completion_is_retained_without_further_provider_work(
    db, monitor_id, stage
):
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="C" * 40,
        source_title="Show S01E01 [1080p][Multi-Subs]",
        rd_torrent_id="prior-source",
        quality="1080p",
        planned_coverage=(1,),
        status="queued",
    )
    db.commit()
    original = dict(db.execute("SELECT * FROM media_assets").fetchone())
    rd = _rd()

    async def already_sent_add(magnet):
        if stage == "add":
            _complete_with_cleanup(db, monitor_id)
        return "remote-created-before-cancellation"

    async def already_sent_select(external_id, selection):
        if stage == "select":
            _complete_with_cleanup(db, monitor_id)
        return True

    async def already_sent_info(external_id):
        _complete_with_cleanup(db, monitor_id)
        return _downloaded(external_id, RDFile(1, "Show S01E01.mkv", 100, True))

    rd.add_magnet.side_effect = already_sent_add
    rd.select_files.side_effect = already_sent_select
    rd.get_torrent.side_effect = already_sent_info
    result = asyncio.run(
        reconcile_candidates(
            db,
            _monitor(db, monitor_id),
            [_candidate("A", {1, 2}), _candidate("B", {1, 2})],
            rd=rd,
            searched_aliases=("Show",),
            now="2026-10-11T11:59:00Z",
        )
    )
    assert result.outcome == "withdrawn" and rd.add_magnet.await_count == 1
    assert rd.select_files.await_count == (0 if stage == "add" else 1)
    assert rd.get_torrent.await_count == (1 if stage == "info" else 0)
    rd.delete_torrent.assert_not_awaited()
    assert (
        dict(db.execute("SELECT * FROM media_assets WHERE torrent_hash=?", ("C" * 40,)).fetchone())
        == original
    )
    asset = db.execute(
        "SELECT * FROM media_assets WHERE provider_asset_id=?",
        ("remote-created-before-cancellation",),
    ).fetchone()
    assert asset["provider_asset_id"] == "remote-created-before-cancellation"
    assert asset["status"] == "queued" and asset["last_error"] == "WITHDRAWN_DURING_ACQUISITION"
    assert _monitor(db, monitor_id)["active"] == 0
