"""Semantic contracts for the Omakase Plus streaming interface."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from html import unescape
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.db import _db, run_migrations
from omakase.plus.deps import get_db as plus_get_db
from omakase.plus.media_store import upsert_monitor
from omakase.plus.posters import PosterUnavailable
from omakase.plus.routes import _login_attempts, router
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.plex_sync import store_plex_library_snapshot
from omakase.plus.watch.service import ingest_watch_event


@pytest.fixture(autouse=True)
def _clear_login_state():
    _login_attempts.clear()
    yield
    os.environ.pop("OMAKASE_PLUS_INVITE", None)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "omakase-plus.db")

    def database():
        connection = sqlite3.connect(db_path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        run_migrations(connection)
        return connection

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[plus_get_db] = database
    test_client = TestClient(app)
    test_client.db_path = db_path
    yield test_client
    for connection in _db.values():
        connection.close()
    _db.clear()
    shutil.rmtree(temp_dir, ignore_errors=True)


def _signup(client: TestClient) -> None:
    os.environ["OMAKASE_PLUS_INVITE"] = "test123"
    response = client.post(
        "/plus/signup",
        data={
            "email": "viewer@example.com",
            "password": "secret123",
            "confirm_password": "secret123",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == (
        "/plus/onboarding" if os.getenv("OMAKASE_PLUS_PRIVATE") == "true" else "/plus/home"
    )


def _connect(client: TestClient) -> sqlite3.Connection:
    connection = sqlite3.connect(client.db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _seed_continue_title(client: TestClient) -> int:
    with _connect(client) as connection:
        user_id = int(
            connection.execute(
                "SELECT id FROM users WHERE email = 'viewer@example.com'"
            ).fetchone()[0]
        )
        monitor_id = upsert_monitor(
            connection,
            user_id=user_id,
            anilist_id=151807,
            title="Solo Leveling",
            format="TV",
            release_status="FINISHED",
            season_number=1,
            expected_episodes=12,
            aired_episodes=12,
        )
        connection.commit()
        ingest_watch_event(
            connection,
            user_id=user_id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(
                source="manual",
                episode=4,
                confidence="manual",
                event_key="manual:streaming-ui:e4",
            ),
            observed_at="2026-08-27T12:00:00Z",
        )
    return monitor_id


def test_home_prioritizes_continue_watching_and_uses_viewer_navigation(client):
    _signup(client)
    _seed_continue_title(client)

    response = client.get("/plus/home")

    assert response.status_code == 200
    assert response.text.index("Continue watching") < response.text.index("Ready to watch")
    assert 'aria-label="Primary navigation"' in response.text
    assert 'aria-current="page"' in response.text
    assert "nav-number" not in response.text
    assert "product-sidebar" not in response.text
    assert "私設" not in response.text
    assert "おまかせ" not in response.text


def test_home_keeps_continue_watching_in_a_compact_scannable_rail(client):
    _signup(client)
    _seed_continue_title(client)

    html = client.get("/plus/home").text

    assert 'aria-label="Continue watching"' in html
    assert "continue-feature" not in html
    assert "featured_media" not in html


def test_review_mode_is_unmistakable_and_discloses_disconnected_external_actions(
    client,
    monkeypatch,
):
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    _signup(client)

    html = client.get("/plus/home").text

    assert 'class="review-mode-banner"' in html
    assert "Real production snapshot" in html
    assert "external actions are disconnected" in html


def test_library_reports_the_latest_real_plex_inventory_count(client):
    _signup(client)
    with _connect(client) as connection:
        store_plex_library_snapshot(
            connection,
            {
                "schema_version": "plex-library-snapshot-v1",
                "generated_at": "2026-08-27T15:00:00Z",
                "items": [
                    {
                        "rating_key": "42",
                        "section": "Anime",
                        "media_type": "show",
                        "title": "Hell's Paradise",
                        "season_number": 2,
                        "episode_total": 12,
                        "watched_through": 9,
                        "last_viewed_at": "2026-08-27T14:55:00Z",
                    },
                    {
                        "rating_key": "77",
                        "section": "Anime Movies",
                        "media_type": "movie",
                        "title": "Ninja Scroll",
                        "season_number": 0,
                        "episode_total": 1,
                        "watched_through": 0,
                        "last_viewed_at": None,
                    },
                ],
            },
            received_at="2026-08-27T15:00:02Z",
        )

    html = client.get("/plus/library").text

    assert "2 titles in Plex" in html
    assert 'data-local-datetime="2026-08-27T15:00:00Z"' in html


@pytest.mark.parametrize("private_deployment", [False, True])
def test_mobile_navigation_exposes_search_history_and_available_media_destinations(
    client, monkeypatch, private_deployment
):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true" if private_deployment else "false")
    _signup(client)

    html = client.get("/plus/home").text
    match = re.search(r'<nav class="mobile-primary-nav"[^>]*>(.*?)</nav>', html, re.DOTALL)

    assert match is not None
    links = re.findall(r'<a href="([^"]+)"([^>]*)>(.*?)</a>', match.group(1), re.DOTALL)
    destinations = [
        (href, unescape(re.sub(r"<[^>]+>", "", content)).strip()) for href, _, content in links
    ]
    expected = [("/plus/home", "Home"), ("/plus/library", "Anime")]
    if private_deployment:
        expected.extend([("/plus/movies", "Movies"), ("/plus/tv", "TV Shows")])
    expected.extend(
        [
            ("/plus/search", "Search"),
            ("/plus/discover", "For you"),
            ("/plus/activity", "History"),
        ]
    )
    assert destinations == expected
    assert [href for href, attrs, _ in links if 'aria-current="page"' in attrs] == ["/plus/home"]
    assert not {"/plus/health/overview", "/plus/setup", "/plus/settings", "/plus/add"} & {
        href for href, _ in destinations
    }


def test_continue_card_exposes_progress_source_and_artwork_fallback(client):
    _signup(client)
    monitor_id = _seed_continue_title(client)

    html = client.get("/plus/home").text

    assert f'href="/plus/library/{monitor_id}"' in html
    assert '<progress max="100" value="33"' in html
    assert '<span class="watched-count">Watched <b>4/12</b></span>' in html
    assert 'aria-label="Solo Leveling: 4 of 12 episodes watched"' in html
    assert '<span class="source-badge">Updated by Manual</span>' in html
    assert 'class="media-art-fallback"' in html
    assert 'data-fallback-title="Solo Leveling"' in html


def test_library_cards_keep_a_visible_title_fallback_when_artwork_is_unavailable(client):
    _signup(client)
    _seed_continue_title(client)

    html = client.get("/plus/library").text

    assert 'class="library-poster poster-frame" data-fallback-title="Solo Leveling"' in html
    assert '<span class="media-art-fallback" aria-hidden="true">SO</span>' in html


def test_library_posters_stack_real_artwork_above_the_title_fallback(client):
    css = client.get("/plus/static/dashboard.css").text
    rules = re.findall(r"\.library-poster img\s*\{([^}]+)\}", css)

    assert rules
    assert any("position: relative" in rule and "z-index: 2" in rule for rule in rules)


def test_discover_recommendation_grid_keeps_order_copy_and_score_in_their_columns(client):
    css = client.get("/plus/static/dashboard.css").text
    product_css = css.split("Omakase Plus product shell", maxsplit=1)[1]

    lane_index_rules = re.findall(
        r"\.lane-selector\s+label\s*>\s*span\s+b\s*\{([^}]+)\}", product_css
    )
    order_rules = re.findall(r"\.tasting-order\s*\{([^}]+)\}", product_css)
    copy_rules = re.findall(r"\.tasting-list\s*>\s*article\s*>\s*div\s*\{([^}]+)\}", product_css)

    assert any("display: block" in rule for rule in lane_index_rules)
    assert any("display: block" in rule for rule in order_rules)
    assert any("min-width: 0" in rule for rule in copy_rules)


def test_legacy_dashboard_remains_a_hidden_compatibility_surface(client):
    _signup(client)

    dashboard = client.get("/plus/dashboard")
    home = client.get("/plus/home")

    assert dashboard.status_code == 200
    assert 'href="/plus/dashboard"' not in home.text
    assert 'href="/plus/add"' not in home.text


def test_streaming_tokens_and_reduced_motion_contract(client):
    css = client.get("/plus/static/dashboard.css").text

    assert "--stream-background: #080d1a" in css
    assert "--stream-surface: #121c30" in css
    assert "--stream-accent: #c3b1ff" in css
    assert "min-height: 44px" in css
    assert "@media (prefers-reduced-motion: reduce)" in css


def test_cross_page_viewer_shell_has_one_title_and_no_ornamental_labels(client):
    _signup(client)

    for path in (
        "/plus/home",
        "/plus/library",
        "/plus/discover",
        "/plus/activity",
        "/plus/health/overview",
        "/plus/setup",
        "/plus/settings",
    ):
        html = client.get(path).text
        assert len(re.findall(r"<h1(?:\s|>)", html)) == 1, path
        assert "product-sidebar" not in html, path
        assert "nav-number" not in html, path
        for removed in ("発見", "履歴", "健全性", "導入", "設定", "蔵書"):
            assert removed not in html, (path, removed)


@pytest.mark.parametrize(
    ("section", "field", "absent"),
    [
        ("preferences", 'id="llm_backend"', 'id="minimum_seeders"'),
        ("tracking", 'id="remove_completed_from_plex"', 'id="llm_backend"'),
        ("downloads", 'id="minimum_seeders"', 'id="llm_backend"'),
        ("sources", 'id="prowlarr_categories"', 'id="remove_completed_from_plex"'),
        ("ai", 'id="llm_api_key"', 'id="minimum_seeders"'),
        ("storage", "Local or NAS storage", 'id="llm_api_key"'),
    ],
)
def test_settings_sections_are_independent_pages(client, section, field, absent):
    _signup(client)
    response = client.get(f"/plus/settings/{section}")
    assert response.status_code == 200
    assert field in response.text
    assert absent not in response.text
    assert f'href="/plus/settings/{section}" aria-current="page"' in response.text
    assert "/plus/settings#" not in response.text


def test_account_menu_contains_only_settings_and_sign_out(client):
    _signup(client)
    html = client.get("/plus/home").text
    menu = re.search(r'<div class="account-popover">(.*?)</div>', html, re.DOTALL).group(1)
    assert re.findall(r'<a href="([^"]+)"', menu) == ["/plus/settings"]
    assert 'action="/plus/logout"' in menu


def test_settings_section_save_returns_to_its_own_page(client):
    _signup(client)
    html = client.get("/plus/settings/preferences").text
    token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
    response = client.post(
        "/plus/settings",
        data={"csrf_token": token, "settings_section": "preferences", "anilist_username": "Tester"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"].startswith("/plus/settings/preferences?saved=")


def test_health_leads_with_concise_status_and_disclosed_diagnostics(client):
    _signup(client)
    html = client.get("/plus/health/overview").text
    assert 'class="health-orb"' in html
    assert 'aria-label="Status colors"' in html
    assert 'class="health-check-details"' in html or "No checks yet" in html


def test_add_get_folds_into_discover_while_post_contract_stays_reachable(client):
    _signup(client)

    redirect = client.get("/plus/add", follow_redirects=False)
    discover = client.get("/plus/discover?mode=add")

    assert redirect.status_code == 302
    assert redirect.headers["location"] == "/plus/discover?mode=add"
    assert discover.status_code == 200
    assert 'aria-current="page">Find a specific anime</a>' in discover.text
    assert 'action="/plus/add/preview"' in discover.text
    assert 'name="csrf_token"' in discover.text


def test_title_page_leads_with_artwork_progress_and_separates_technical_details(client):
    _signup(client)
    monitor_id = _seed_continue_title(client)

    html = client.get(f"/plus/library/{monitor_id}").text

    assert 'class="title-view-hero streaming-title-hero"' in html
    assert 'data-fallback-title="Solo Leveling"' in html
    assert '<span class="watched-count">Watched <b>4/12</b></span>' in html
    assert 'aria-label="Solo Leveling: 4 of 12 episodes watched"' in html
    assert '<progress max="100" value="33"' in html
    assert "Media availability &amp; playback" in html


def test_activity_defaults_to_visual_viewing_history_and_keeps_system_work_separate(client):
    _signup(client)
    _seed_continue_title(client)

    response = client.get("/plus/activity")
    html = response.text

    assert response.status_code == 200
    assert 'href="/plus/activity?view=watch" aria-current="page"' in html
    assert 'href="/plus/activity?view=system"' in html
    assert '<div class="history-grid">' in html
    card = re.search(r'<article class="history-card">(.*?)</article>', html, re.DOTALL)
    assert card is not None
    assert 'href="/plus/catalog/anime/151807"' in card.group(1)
    assert 'src="/plus/media/151807/poster"' not in card.group(1)
    assert 'aria-label="Episode 4 image unavailable"' in card.group(1)
    assert "data-history-placeholder" in card.group(1)
    assert "Image unavailable" in card.group(1)
    assert 'aria-label="Solo Leveling, episode 4"' in card.group(1)
    assert '<h3 title="Episode 4">E4</h3>' in card.group(1)
    assert "Progress saved" in card.group(1)
    assert 'Observed <time datetime="2026-08-27T12:00:00Z"' in card.group(1)
    assert "Added manually" in card.group(1)
    assert "Media and library work" not in html
    assert 'class="activity-table"' not in html

    system = client.get("/plus/activity?view=system")
    assert system.status_code == 200
    assert 'href="/plus/activity?view=system" aria-current="page"' in system.text
    assert "Media and library work" in system.text
    assert 'aria-label="Activity outcome"' in system.text
    assert 'href="/plus/activity?view=system&outcome=attention"' in system.text
    assert 'class="history-grid"' not in system.text


def test_title_exposes_episode_availability_without_confusing_watched_with_ready(client):
    _signup(client)
    monitor_id = _seed_continue_title(client)

    html = client.get(f"/plus/library/{monitor_id}").text

    episodes = re.search(r'<ol class="viewer-episode-grid">(.*?)</ol>', html, re.DOTALL)
    assert episodes is not None
    assert len(re.findall(r'<li class="viewer-episode', episodes.group(1))) == 12
    assert episodes.group(1).count("Not yet verified") == 12
    assert episodes.group(1).count(">Watched</span>") == 4
    assert episodes.group(1).count(">Up next</span>") == 1
    assert html.index('id="episode-heading"') < html.index(
        '<details class="title-technical-details">'
    )
    assert "not individual episode playback" in html
    assert 'href="#episode-heading"' in html
    assert 'id="watch-tracking"' in html


@pytest.mark.parametrize("scheduled", [True, False])
def test_title_includes_future_slots_without_counting_them_as_missing_files(client, scheduled):
    from datetime import UTC, datetime, timedelta

    _signup(client)
    monitor_id = _seed_continue_title(client)
    next_at = int((datetime.now(UTC) + timedelta(days=7)).timestamp()) if scheduled else None
    with _connect(client) as connection:
        connection.execute(
            "UPDATE media_monitors SET release_status='RELEASING', expected_episodes=10, aired_episodes=8, next_airing_at=? WHERE id=?",
            (next_at, monitor_id),
        )
        connection.commit()
    html = client.get(f"/plus/library/{monitor_id}").text
    for number in (9, 10):
        future = re.search(rf'<li[^>]*data-episode="{number}".*?</li>', html, re.S)
        assert future is not None
        assert ("Upcoming" if scheduled else "Release status unknown") in future.group(0)
        assert "Not yet verified" not in future.group(0)
    assert html.count("Not yet verified") == 8
    assert 'data-episode="11"' not in html and 'data-episode="12"' not in html


def test_title_exposes_missing_episode_search_and_removal_in_library_actions(client):
    _signup(client)
    monitor_id = _seed_continue_title(client)

    html = client.get(f"/plus/library/{monitor_id}").text

    technical_start = html.index('<details class="title-technical-details">')
    assert html.index(f'action="/plus/library/{monitor_id}/retry"') < technical_start
    assert "Find missing episodes" in html
    assert "Library actions" in html
    assert html.index(f'href="/plus/library/{monitor_id}/remove"') < technical_start


def test_missing_poster_returns_quiet_same_origin_placeholder(client):
    _signup(client)

    with patch(
        "omakase.plus.routes.poster_bytes",
        new=AsyncMock(side_effect=PosterUnavailable("not available")),
    ):
        response = client.get("/plus/media/151807/poster")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.headers["x-omakase-poster"] == "unavailable"


def test_completion_dispatches_only_its_new_anilist_work(client, monkeypatch):
    monkeypatch.delenv("OMAKASE_PLUS_REVIEW_MODE", raising=False)
    _signup(client)
    monitor_id = _seed_continue_title(client)
    page = client.get(f"/plus/library/{monitor_id}").text
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
    with patch("omakase.plus.watch.anilist_sync.process_tracker_outbox") as sync:
        response = client.post(
            f"/plus/library/{monitor_id}/watch/complete",
            data={"csrf_token": token, "score_ten": "9"},
        )
    assert response.status_code == 200
    with _connect(client) as db:
        last_id = db.execute("SELECT max(id) FROM tracker_sync_outbox").fetchone()[0]
    sync.assert_called_once()
    assert sync.call_args.kwargs == {"outbox_id": last_id}


@pytest.mark.parametrize("score", ["0", "11", "8.5", "nan"])
def test_invalid_completion_score_does_not_change_progress(client, score):
    _signup(client)
    monitor_id = _seed_continue_title(client)
    page = client.get(f"/plus/library/{monitor_id}").text
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
    response = client.post(
        f"/plus/library/{monitor_id}/watch/complete", data={"csrf_token": token, "score_ten": score}
    )
    assert response.status_code == 200
    with _connect(client) as db:
        assert (
            db.execute(
                "SELECT status FROM watch_entries WHERE monitor_id=?", (monitor_id,)
            ).fetchone()[0]
            == "watching"
        )


def test_completion_offers_score_and_cleanup_without_deleting_media(client, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    _signup(client)
    monitor_id = _seed_continue_title(client)
    page = client.get(f"/plus/library/{monitor_id}").text
    assert 'name="score_ten"' in page
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
    response = client.post(
        f"/plus/library/{monitor_id}/watch/complete",
        data={"csrf_token": token, "score_ten": "8"},
    )
    assert response.status_code == 200
    assert "Keep in library" in response.text
    assert "Review media removal" in response.text
    assert "Queued for AniList" in response.text
    with _connect(client) as db:
        row = db.execute(
            "SELECT action,score_ten FROM tracker_sync_outbox ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert tuple(row) == ("complete", 8)
        assert (
            db.execute("SELECT active FROM media_monitors WHERE id=?", (monitor_id,)).fetchone()[0]
            == 1
        )


def _enable_completion_cleanup(client):
    from omakase.plus.completion_cleanup import set_completion_cleanup_preference

    with _connect(client) as db:
        user_id = db.execute("SELECT id FROM users WHERE email='viewer@example.com'").fetchone()[0]
        set_completion_cleanup_preference(db, user_id=user_id, remove_from_plex=True)


def test_completion_settings_save_and_reopen_native_preference(client):
    _signup(client)
    page = client.get("/plus/settings/tracking").text
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
    response = client.post(
        "/plus/settings",
        data={
            "csrf_token": token,
            "settings_section": "tracking",
            "completion_cleanup_present": "1",
            "remove_completed_from_plex": "1",
        },
    )
    assert response.status_code == 200
    assert '<option value="1" selected>Remove from Plex when finished</option>' in response.text
    response = client.post(
        "/plus/settings",
        data={
            "csrf_token": token,
            "settings_section": "tracking",
            "completion_cleanup_present": "1",
            "remove_completed_from_plex": "0",
        },
    )
    assert response.status_code == 200
    assert '<option value="0" selected>Keep in Plex when finished</option>' in response.text


@pytest.mark.parametrize("keep", [False, True])
def test_native_completion_queues_plex_only_or_honors_keep_exception(client, monkeypatch, keep):
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    _signup(client)
    monitor_id = _seed_continue_title(client)
    _enable_completion_cleanup(client)
    page = client.get(f"/plus/library/{monitor_id}").text
    assert 'name="keep_completed_in_plex"' in page
    token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', page).group(1)
    data = {"csrf_token": token, "score_ten": "8", "completion_policy_present": "1"}
    if keep:
        data["keep_completed_in_plex"] = "1"
    response = client.post(f"/plus/library/{monitor_id}/watch/complete", data=data)
    assert response.status_code == 200
    assert "Your rating:" in response.text and "8 / 10" in response.text
    with _connect(client) as db:
        assert (
            db.execute(
                "SELECT status FROM watch_entries WHERE monitor_id=?", (monitor_id,)
            ).fetchone()[0]
            == "completed"
        )
        removal = db.execute(
            "SELECT scope,status FROM media_title_removals WHERE monitor_id=?", (monitor_id,)
        ).fetchone()
        assert (removal is None) == keep
        if not keep:
            assert tuple(removal) == ("plex_only", "awaiting_plex")
            assert "Removal from Plex is queued" in response.text
        assert db.execute(
            "SELECT keep_completed_in_plex FROM media_monitors WHERE id=?", (monitor_id,)
        ).fetchone()[0] == int(keep)


def test_invalid_score_does_not_save_keep_exception_or_queue_cleanup(client):
    _signup(client)
    monitor_id = _seed_continue_title(client)
    _enable_completion_cleanup(client)
    token = re.search(
        r'name="csrf_token" value="([0-9a-f]{64})"', client.get(f"/plus/library/{monitor_id}").text
    ).group(1)
    response = client.post(
        f"/plus/library/{monitor_id}/watch/complete",
        data={
            "csrf_token": token,
            "score_ten": "11",
            "completion_policy_present": "1",
            "keep_completed_in_plex": "1",
        },
    )
    assert response.status_code == 200
    with _connect(client) as db:
        assert (
            db.execute(
                "SELECT status FROM watch_entries WHERE monitor_id=?", (monitor_id,)
            ).fetchone()[0]
            == "watching"
        )
        assert (
            db.execute(
                "SELECT keep_completed_in_plex FROM media_monitors WHERE id=?", (monitor_id,)
            ).fetchone()[0]
            == 0
        )
        assert db.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 0


def test_invalid_settings_section_cannot_mutate_preferences(client):
    from omakase.plus.secrets import read_secret

    _signup(client)
    token = re.search(
        r'name="csrf_token" value="([^"]+)"', client.get("/plus/settings").text
    ).group(1)
    response = client.post(
        "/plus/settings",
        data={"csrf_token": token, "settings_section": "../profile", "anilist_username": "Changed"},
    )
    assert response.status_code == 422
    with _connect(client) as db:
        assert read_secret(db, 1, "anilist_username") is None


def test_preferences_save_preserves_tracking_cleanup(client):
    from omakase.plus.completion_cleanup import get_completion_cleanup_preference
    from omakase.plus.secrets import read_secret

    _signup(client)
    _enable_completion_cleanup(client)
    token = re.search(
        r'name="csrf_token" value="([^"]+)"', client.get("/plus/settings/preferences").text
    ).group(1)
    assert (
        client.post(
            "/plus/settings",
            data={
                "csrf_token": token,
                "settings_section": "preferences",
                "anilist_username": "Viewer",
            },
        ).status_code
        == 200
    )
    with _connect(client) as db:
        assert read_secret(db, 1, "anilist_username") == "Viewer"
        assert get_completion_cleanup_preference(db, user_id=1)


def test_service_artwork_is_packaged_and_allowlisted(client):
    for service in ("plex", "riven", "seerr", "realdebrid", "prowlarr", "anilist"):
        response = client.get(f"/plus/static/services/{service}.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")
    assert client.get("/plus/static/services/provenance.json").status_code == 404


def test_private_signup_starts_onboarding_returning_login_opens_home(client):
    _signup(client)
    token = re.search(r'name="csrf_token" value="([^"]+)"', client.get("/plus/home").text).group(1)
    client.post("/plus/logout", data={"csrf_token": token})
    response = client.post(
        "/plus/login",
        data={"email": "viewer@example.com", "password": "secret123"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/plus/home"
