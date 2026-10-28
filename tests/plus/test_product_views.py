"""Consumer product-view tests for readiness, subtitles, privacy, and ownership."""

from __future__ import annotations

import json
import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus.manifest import store_curator_receipt
from omakase.plus.media_store import (
    acquire_sync_lease,
    ensure_media_provider,
    record_media_file_probe,
    record_sync_run,
    release_sync_lease,
    replace_subtitle_tracks,
    update_catalog_health,
    update_provider_health,
    upsert_asset,
    upsert_monitor,
)
from omakase.plus.product_views import (
    _readiness_label,
    activity_view,
    clean_detail,
    discover_view,
    health_view,
    home_view,
    library_titles,
    setup_view,
    title_detail,
)


@pytest.mark.parametrize(
    ("state", "missing", "expected"),
    [
        ("attention", 1, "1 episode missing"),
        ("attention", 2, "2 episodes missing"),
        ("acquiring", 2, "Finding 2 missing episodes"),
    ],
)
def test_readiness_label_explains_that_episode_count_is_missing(state, missing, expected):
    assert (
        _readiness_label(
            state=state,
            required=12,
            usable=12 - missing,
            plex_visible=12 - missing,
        )
        == expected
    )


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    yield conn
    conn.close()


def _user(db, email: str) -> int:
    return int(
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, 'hash')",
            (email,),
        ).lastrowid
    )


def _curator_receipt(*, anilist_id: int, season: int, episodes: int) -> dict:
    counts = {"Anime": 3, "Anime Movies": 0, "TV Shows": 0, "Movies": 0}
    return {
        "schema_version": "curator-receipt-v2",
        "run_id": "product-view-20260726",
        "started_at": "2026-07-26T12:00:00Z",
        "completed_at": "2026-07-26T12:00:08Z",
        "outcome": "success",
        "mode": "live",
        "topology_converged": True,
        "manifest_generated_at": "2026-07-26T11:59:50Z",
        "observed_sections": [
            {"key": index, "title": title, "count": count}
            for index, (title, count) in enumerate(counts.items(), start=1)
        ],
        "converged_section_counts": counts,
        "title_coverage": [
            {
                "anilist_id": anilist_id,
                "section": "Anime",
                "season_number": season,
                "usable_count": episodes,
                "plex_visible_count": episodes,
            }
        ],
        "blocked_assets": [],
        "errors": [],
    }


def _seed_ready_title(db, user_id: int) -> tuple[int, int]:
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=180560,
        title="Hell's Paradise Season 2",
        search_titles=("Jigokuraku 2nd Season",),
        format="TV",
        release_status="FINISHED",
        season_number=2,
        expected_episodes=3,
        aired_episodes=3,
        last_result="Identity, media, subtitles, and Plex agree",
    )
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="local_import",
        label="Local library",
        config={"import_roots": ["/imports"]},
        capabilities={"adoption": True, "local_storage": True},
    )
    asset_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="A" * 40,
        source_title="Jigokuraku S02 1080p",
        provider_id=provider_id,
        provider_asset_id="jigokuraku-s02",
        storage_backend="local",
        storage_locator="Jigokuraku/Season 02/Episode 01.mkv",
        quality="1080p",
        ownership="monitor_owned",
        provenance="adopted",
        planned_coverage=(1, 2, 3),
        usable_coverage=(1, 2, 3),
        inventory_fingerprint="b" * 64,
        inventory_verified_at="2026-07-26T11:58:00Z",
        status="downloaded",
        progress=100,
    )
    probe_id = record_media_file_probe(
        db,
        asset_id=asset_id,
        file_locator="Jigokuraku/Season 02/Episode 01.mkv",
        inventory_fingerprint="b" * 64,
        byte_range_receipt={},
        video_streams=[{"codec": "hevc"}],
        audio_streams=[{"codec": "aac", "language": "jpn"}],
        outcome="usable",
        detail="",
        verified_at="2026-07-26T11:58:00Z",
    )
    replace_subtitle_tracks(
        db,
        probe_id=probe_id,
        tracks=(
            {
                "stream_index": 2,
                "language": "eng",
                "codec": "ass",
                "switchable": True,
                "default_track": True,
                "forced": False,
                "source_kind": "embedded",
                "sync_policy": "exact_release",
            },
            {
                "stream_index": 3,
                "language": "fra",
                "codec": "ass",
                "switchable": True,
                "default_track": False,
                "forced": False,
                "source_kind": "embedded",
                "sync_policy": "exact_release",
            },
            {
                "stream_index": 100001,
                "language": "spa",
                "codec": "srt",
                "switchable": True,
                "default_track": False,
                "forced": False,
                "source_kind": "external",
                "sync_policy": "unverified_external",
            },
        ),
    )
    record_sync_run(
        db,
        monitor_id=monitor_id,
        outcome="success",
        required_count=3,
        covered_count=3,
        accepted_count=1,
        detail="Verified exact-release subtitle inventory",
        completed_at="2026-07-26T11:58:10Z",
    )
    db.commit()
    store_curator_receipt(
        db,
        _curator_receipt(anilist_id=180560, season=2, episodes=3),
        received_at="2026-07-26T12:00:10Z",
    )
    db.commit()
    return monitor_id, asset_id


def test_ready_title_requires_media_and_plex_evidence_and_lists_subtitle_choices(db):
    user_id = _user(db, "owner@example.com")
    monitor_id, _ = _seed_ready_title(db, user_id)

    titles = library_titles(db, user_id=user_id, provider="local_import")
    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)

    assert len(titles) == 1
    assert titles[0]["release_qualities"] == ["1080p"]
    assert titles[0]["fallback_episodes"] == []
    assert titles[0]["state"] == "ready"
    assert titles[0]["usable"] == 3
    assert titles[0]["plex_visible"] == 3
    assert detail is not None
    assert [
        (track["language"], track["sync_policy"]) for track in detail["assets"][0]["subtitles"]
    ] == [
        ("eng", "exact_release"),
        ("fra", "exact_release"),
        ("spa", "unverified_external"),
    ]
    assert home_view(db, user_id=user_id)["summary"]["ready"] == 1


def test_library_content_type_filter_separates_series_and_movies(db):
    user_id = _user(db, "owner@example.com")
    _seed_ready_title(db, user_id)
    upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=617,
        title="Ninja Scroll",
        format="MOVIE",
        release_status="FINISHED",
        expected_episodes=1,
        aired_episodes=1,
    )
    db.commit()

    series = library_titles(db, user_id=user_id, content_type="series")
    movies = library_titles(db, user_id=user_id, content_type="movies")

    assert [item["title"] for item in series] == ["Hell's Paradise Season 2"]
    assert [item["title"] for item in movies] == ["Ninja Scroll"]


def test_activity_uses_plain_language_and_paginates_twenty_rows(db):
    user_id = _user(db, "owner@example.com")
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=166613,
        title="Hell's Paradise Season 2",
        format="TV",
        release_status="FINISHED",
        expected_episodes=12,
        aired_episodes=12,
    )
    for index in range(25):
        record_sync_run(
            db,
            monitor_id=monitor_id,
            outcome="refreshed" if index % 2 == 0 else "degraded",
            required_count=12,
            covered_count=10,
            detail="Coverage 10/12; degraded; provider candidates checked",
            completed_at=f"2026-07-26T12:{index:02d}:00Z",
        )
    db.commit()

    first = activity_view(db, user_id=user_id, page=1)
    second = activity_view(db, user_id=user_id, page=2)

    assert len(first["events"]) == 20
    assert len(second["events"]) == 5
    assert first["total_pages"] == 2
    assert {item["outcome"] for item in first["events"]} == {
        "Checked existing files",
        "Episodes still missing",
    }
    degraded = next(item for item in first["events"] if item["outcome"] == "Episodes still missing")
    assert degraded["detail"] == "Checked more sources. 2 episodes are still missing."


def test_views_are_owner_scoped_even_when_another_user_has_ready_media(db):
    owner_id = _user(db, "owner@example.com")
    stranger_id = _user(db, "stranger@example.com")
    monitor_id, _ = _seed_ready_title(db, owner_id)

    assert library_titles(db, user_id=stranger_id) == []
    assert title_detail(db, user_id=stranger_id, monitor_id=monitor_id) is None


def test_health_uses_stable_reason_copy_without_returning_configuration(db):
    user_id = _user(db, "owner@example.com")
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind="qbittorrent",
        label="Local torrent client",
        config={"secret_name": "qbittorrent_password"},
        capabilities={"acquisition": True},
    )
    update_provider_health(
        db,
        provider_id=provider_id,
        status="error",
        reason_code="QBITTORRENT_BYPASS_POSSIBLE",
        detail="Interface binding does not prove a tunnel",
        checked_at="2026-07-26T12:00:00Z",
    )

    view = health_view(db, user_id=user_id)

    assert view["overall"] == "attention"
    assert view["providers"][0]["reason_label"] == "Torrent traffic can bypass the tunnel"
    assert "config" not in view["providers"][0]


def test_health_uses_latest_curator_receipt_when_no_legacy_plex_target_exists(db):
    user_id = _user(db, "owner@example.com")
    _seed_ready_title(db, user_id)

    view = health_view(db, user_id=user_id)

    assert view["plex_targets"] == [
        {
            "id": 0,
            "label": "Plex library",
            "policy": "Managed publication",
            "status": "healthy",
            "reason_code": "READY",
            "reason_label": "Verified by the latest Plex check",
            "detail": "Latest Plex scan and episode visibility check passed",
            "checked_at": "2026-07-26T12:00:08Z",
            "source": "Automatic Plex check",
            "status_label": "Ready",
        }
    ]


def test_health_reports_live_catalog_checks_in_plain_language(db):
    user_id = _user(db, "owner@example.com")
    update_catalog_health(
        db,
        user_id=user_id,
        catalog_kind="prowlarr",
        label="Prowlarr expanded search",
        status="degraded",
        reason_code="PARTIAL_CATALOG_ERROR",
        detail="Completed 3 searches; found 12 release results; 1 failed",
        candidate_count=12,
        checked_at="2026-07-26T12:00:00Z",
    )

    view = health_view(db, user_id=user_id)

    prowlarr = next(item for item in view["catalogs"] if item["kind"] == "prowlarr")
    assert prowlarr["reason_label"] == "Some source searches failed"
    assert prowlarr["status_label"] == "Some sources unavailable"
    assert prowlarr["candidate_count"] == 12
    assert "api_key" not in json.dumps(prowlarr)
    assert view["overall"] == "attention"


def test_health_only_reports_an_unexpired_unreleased_scheduler_lease(db):
    user_id = _user(db, "owner@example.com")
    receipt = acquire_sync_lease(
        db,
        owner="opaque-owner",
        entrypoint="timer",
        acquired_at="2099-07-26T12:00:00Z",
        expires_at="2099-07-26T12:30:00Z",
    )
    db.commit()

    assert receipt.acquired is True
    assert health_view(db, user_id=user_id)["scheduler"]["state"] == "running"

    assert release_sync_lease(
        db,
        owner="opaque-owner",
        released_at="2099-07-26T12:01:00Z",
    )
    db.commit()

    assert health_view(db, user_id=user_id)["scheduler"] == {"state": "idle"}


def test_health_ignores_an_expired_scheduler_lease(db):
    user_id = _user(db, "owner@example.com")
    acquire_sync_lease(
        db,
        owner="expired-owner",
        entrypoint="timer",
        acquired_at="2020-07-26T12:00:00Z",
        expires_at="2020-07-26T12:30:00Z",
    )
    db.commit()

    assert health_view(db, user_id=user_id)["scheduler"] == {"state": "idle"}


def test_discover_removes_recommendations_after_the_user_handles_them(db):
    user_id = _user(db, "owner@example.com")
    picks = [
        {"title": "Attack on Titan Final Season Part 2", "media_id": 131681},
        {"title": "Golden Kamuy", "media_id": 99699},
    ]
    run_id = db.execute(
        """INSERT INTO run_history (user_id, source, model, picks, lane)
           VALUES (?, 'anilist', 'test', ?, 'best_match')""",
        (user_id, json.dumps(picks)),
    ).lastrowid
    db.execute(
        """INSERT INTO recommendation_feedback
           (user_id, source, media_id, title, feedback_type, run_id)
           VALUES (?, 'anilist', 131681, ?, 'already_watched', ?)""",
        (user_id, picks[0]["title"], run_id),
    )
    db.commit()

    view = discover_view(db, user_id=user_id)

    assert [pick["title"] for pick in view["latest"]["picks"]] == ["Golden Kamuy"]


def test_title_detail_labels_burned_in_subtitles_as_watchable_fallback(db):
    user_id = _user(db, "owner@example.com")
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=190001,
        title="Example Anime",
        release_status="RELEASING",
        season_number=1,
        expected_episodes=2,
        aired_episodes=2,
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="C" * 40,
        source_title="[Group] Example Anime S01E01 [1080p] [Hardsubs]",
        quality="1080p",
        planned_coverage=(1,),
        usable_coverage=(1,),
        status="downloaded",
        progress=100,
    )

    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)
    assert detail is not None
    asset = detail["assets"][0]

    assert asset["subtitle_state"] == "burned_in"
    assert asset["subtitle_label"] == "English subtitles are built into the video"
    assert "cannot be switched" in asset["subtitle_detail"]


def test_title_status_names_the_problem_and_hides_unused_failed_versions(db):
    user_id = _user(db, "owner@example.com")
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=99547,
        title="Mobile Suit Gundam Thunderbolt: Bandit Flower",
        format="MOVIE",
        release_status="FINISHED",
        expected_episodes=1,
        aired_episodes=1,
    )
    selected_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="D" * 40,
        source_title="Bandit Flower 2160p.mkv",
        quality="2160p",
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="d" * 64,
        inventory_verified_at="2026-07-26T12:00:00Z",
        status="downloaded",
        progress=100,
    )
    unused_id = upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="E" * 40,
        source_title="Bandit Flower 1080p.mp4",
        quality="1080p",
        planned_coverage=(1,),
        status="error",
        progress=100,
        last_error="NO_SWITCHABLE_ENGLISH_SUBTITLE",
    )
    db.commit()

    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)

    assert detail is not None
    assert detail["state"] == "attention"
    assert detail["status_label"] == "Not in Plex"
    assert detail["release_status_label"] == "Finished airing"
    assert [asset["id"] for asset in detail["current_assets"]] == [selected_id]
    assert [asset["id"] for asset in detail["other_assets"]] == [unused_id]
    assert detail["current_assets"][0]["subtitle_label"] == "Automatic verification"
    assert "No action is required" in detail["current_assets"][0]["subtitle_detail"]


def test_verified_title_explains_automatic_plex_indexing(db):
    user_id = _user(db, "owner@example.com")
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=180523,
        title="A Wild Last Boss Appeared!",
        format="TV",
        release_status="FINISHED",
        season_number=1,
        expected_episodes=12,
        aired_episodes=12,
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="8" * 40,
        source_title="A Wild Last Boss Appeared! S01 1080p",
        quality="1080p",
        planned_coverage=range(1, 13),
        usable_coverage=range(1, 13),
        inventory_fingerprint="8" * 64,
        inventory_verified_at="2026-08-28T14:04:04Z",
        status="downloaded",
        progress=100,
    )
    receipt = _curator_receipt(anilist_id=180523, season=1, episodes=12)
    receipt["title_coverage"][0]["plex_visible_count"] = 0
    db.commit()
    store_curator_receipt(db, receipt, received_at="2026-08-28T14:11:14Z")
    db.commit()

    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)

    assert detail is not None
    assert detail["state"] == "acquiring"
    assert detail["lifecycle_label"] == "Publishing to Plex"
    assert detail["status_label"] == "Publishing to Plex · 0 of 12 visible"
    assert detail["publication_pending"] is True
    assert detail["publication_detail"] == (
        "All 12 episodes are verified and sent to the managed Plex library. "
        "Plex is indexing them automatically; 0 of 12 are visible so far."
    )


def test_complete_plex_title_ignores_an_unused_failed_version(db):
    user_id = _user(db, "owner@example.com")
    monitor_id = upsert_monitor(
        db,
        user_id=user_id,
        anilist_id=99547,
        title="Mobile Suit Gundam Thunderbolt: Bandit Flower",
        format="MOVIE",
        release_status="FINISHED",
        expected_episodes=1,
        aired_episodes=1,
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="F" * 40,
        source_title="Bandit Flower 2160p.mkv",
        quality="2160p",
        planned_coverage=(1,),
        usable_coverage=(1,),
        inventory_fingerprint="f" * 64,
        inventory_verified_at="2026-07-26T12:00:00Z",
        status="downloaded",
        progress=100,
    )
    upsert_asset(
        db,
        monitor_id=monitor_id,
        torrent_hash="9" * 40,
        source_title="Bandit Flower 1080p.mp4",
        quality="1080p",
        planned_coverage=(1,),
        status="error",
        progress=100,
        last_error="NO_SWITCHABLE_ENGLISH_SUBTITLE",
    )
    receipt = _curator_receipt(anilist_id=99547, season=1, episodes=1)
    receipt["title_coverage"][0]["section"] = "Anime Movies"
    receipt["title_coverage"][0]["season_number"] = None
    receipt["observed_sections"][1]["count"] = 1
    receipt["converged_section_counts"]["Anime Movies"] = 1
    db.commit()
    store_curator_receipt(db, receipt, received_at="2026-07-26T12:00:10Z")
    db.commit()

    detail = title_detail(db, user_id=user_id, monitor_id=monitor_id)

    assert detail is not None
    assert detail["state"] == "ready"
    assert detail["status_label"] == "Ready"
    assert detail["current_assets"][0]["subtitle_label"] == "Verified automatically"


def test_setup_uses_live_database_receipts_instead_of_redirect_placeholders(db):
    user_id = _user(db, "owner@example.com")
    _seed_ready_title(db, user_id)

    view = setup_view(db, user_id=user_id)

    assert view["steps"]["storage"]["status"] == "ready"
    assert view["steps"]["plex"]["status"] == "ready"
    assert view["steps"]["readiness"]["detail"]


def test_product_detail_redacts_credentials_magnets_and_sensitive_url_queries():
    detail = clean_detail(
        "Bearer abc.def magnet:?xt=urn:btih:ABC "
        "https://user:pass@example.test/scan?token=secret&section=anime "
        "API_KEY=sk-private"
    )

    assert "abc.def" not in detail
    assert "btih" not in detail
    assert "user:pass" not in detail
    assert "secret" not in detail
    assert "sk-private" not in detail
    assert "section=anime" in detail
