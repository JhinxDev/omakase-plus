"""Tests for Omakase Plus dashboard (Phase 5).

Uses FastAPI TestClient with a per-test temp database.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.db import _db, run_migrations
from omakase.plus.deps import get_db as _deps_get_db
from omakase.plus.media_store import (
    ensure_media_provider,
    record_sync_run,
    upsert_asset,
    upsert_monitor,
)
from omakase.plus.media_types import MediaTarget
from omakase.plus.routes import _login_attempts
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import ingest_watch_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_conn(db_dir: str) -> sqlite3.Connection:
    """Create a fresh SQLite connection at *db_dir*/omakase-plus.db."""
    db_path = os.path.join(db_dir, "omakase-plus.db")
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    run_migrations(conn)
    return conn


def _connect_client_db(client: TestClient) -> sqlite3.Connection:
    conn = sqlite3.connect(client.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _cleanup_db_cache():
    """Close and clear Sub-agent A's connection cache (best-effort)."""
    for c in _db.values():
        try:
            c.close()
        except Exception:
            pass
    _db.clear()


def _signup_and_login(client: TestClient) -> None:
    """Helper: sign up a test user so subsequent requests are authenticated."""
    os.environ["OMAKASE_PLUS_INVITE"] = "test123"
    resp = client.post(
        "/plus/signup",
        data={
            "email": "alice@example.com",
            "password": "secret123",
            "confirm_password": "secret123",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == (
        "/plus/onboarding" if os.getenv("OMAKASE_PLUS_PRIVATE") == "true" else "/plus/home"
    )
    assert "omakase_session" in resp.cookies


def _drain_job(client: TestClient, resp, timeout: float = 5.0) -> None:
    """Wait for an async ``/plus/api/run`` job to leave the 'running' state.

    ``/plus/api/run`` now runs the pipeline in a background thread, so callers
    that patch ``run_pipeline`` must drain the job *inside* the patch context —
    otherwise the patch is torn down before the worker invokes it.
    """
    job_id = resp.json()["job_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = client.get(f"/plus/api/run/status/{job_id}")
        if status.status_code == 200 and status.json().get("status") != "running":
            return
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def _user_id(client: TestClient) -> int:
    with _connect_client_db(client) as conn:
        return conn.execute(
            "SELECT id FROM users WHERE email = ?",
            ("alice@example.com",),
        ).fetchone()["id"]


def _csrf_token(client: TestClient) -> str:
    response = client.get("/plus/dashboard")
    match = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', response.text)
    assert match is not None
    return match.group(1)


def _seed_run(
    client: TestClient,
    picks: list[dict],
    *,
    source: str = "anilist",
    model: str = "test-model",
    lane: str = "best_match",
) -> int:
    with _connect_client_db(client) as conn:
        cursor = conn.execute(
            "INSERT INTO run_history (user_id, source, model, picks, lane) VALUES (?, ?, ?, ?, ?)",
            (_user_id(client), source, model, json.dumps(picks), lane),
        )
        conn.commit()
        return cursor.lastrowid


def _seed_removable_title(client: TestClient) -> int:
    with _connect_client_db(client) as conn:
        user_id = _user_id(client)
        planning_id = int(
            conn.execute(
                """INSERT INTO anilist_plannings
                   (user_id, anilist_id, title, status)
                   VALUES (?, 151807, 'Solo Leveling', 'COMPLETED')""",
                (user_id,),
            ).lastrowid
        )
        monitor_id = upsert_monitor(
            conn,
            user_id=user_id,
            anilist_id=151807,
            anilist_planning_id=planning_id,
            title="Solo Leveling",
            search_titles=("Ore dake Level Up na Ken",),
            format="TV",
            release_status="FINISHED",
            season_number=1,
            expected_episodes=12,
            aired_episodes=12,
        )
        provider_id = ensure_media_provider(
            conn,
            user_id=user_id,
            kind="realdebrid",
            label="Real-Debrid",
            capabilities={"deletion": True},
            config={},
        )
        run_id = record_sync_run(
            conn,
            monitor_id=monitor_id,
            outcome="complete",
            required_count=12,
            covered_count=12,
            accepted_count=1,
            detail="ready",
            completed_at="2026-07-28T08:00:00Z",
        )
        upsert_asset(
            conn,
            monitor_id=monitor_id,
            torrent_hash="A" * 40,
            source_title="Solo Leveling S01 1080p",
            source_folder="Solo.Leveling.S01",
            provider_id=provider_id,
            provider_asset_id="rd-solo-leveling",
            storage_backend="zurg",
            storage_locator="Solo.Leveling.S01",
            ownership="monitor_owned",
            provenance="omakase_created",
            created_by_run_id=run_id,
            planned_coverage=range(1, 13),
            usable_coverage=range(1, 13),
            inventory_fingerprint="b" * 64,
            inventory_verified_at="2026-07-28T08:00:00Z",
            status="downloaded",
            progress=100,
        )
        conn.commit()
        return monitor_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_rate_limiter():
    _login_attempts.clear()


@pytest.fixture
def client():
    """FastAPI TestClient wired to a per-test temp database."""
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "omakase-plus.db")

    def _override_deps_get_db():
        return _make_test_conn(tmp)

    app = FastAPI()
    from omakase.plus.routes import router

    app.include_router(router)
    app.dependency_overrides[_deps_get_db] = _override_deps_get_db

    test_client = TestClient(app)
    test_client.db_path = db_path

    yield test_client

    _cleanup_db_cache()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDashboardAccess:
    def test_dashboard_requires_login(self, client):
        """GET /plus/dashboard without a session cookie redirects or 401s."""
        resp = client.get("/plus/dashboard", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_dashboard_redirects_to_home_for_logged_in_user(self, client):
        """Logged-in GET /plus/dashboard redirects cleanly to /plus/home."""
        try:
            _signup_and_login(client)

            redirect = client.get("/plus/dashboard", follow_redirects=False)
            assert redirect.status_code == 307
            assert redirect.headers["location"] == "/plus/home"

            html = client.get("/plus/dashboard").text
            assert "Omakase Plus" in html
            assert "Settings" in html
            assert "Sign out" in html
            assert 'href="/plus/static/dashboard.css?v=' in html
            assert 'src="/plus/static/dashboard.js?v=' in html
            assert "<style>" not in html
            assert "<script>" not in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_plus_static_asset_route_serves_dashboard_css(self, client):
        """The Plus router serves allowlisted packaged dashboard CSS."""
        resp = client.get("/plus/static/dashboard.css")

        assert resp.status_code == 200
        assert "dashboard-shell" in resp.text
        assert "text/css" in resp.headers["content-type"]

    def test_plus_static_asset_route_serves_dashboard_js(self, client):
        """The Plus router serves allowlisted packaged dashboard JavaScript."""
        resp = client.get("/plus/static/dashboard.js")

        assert resp.status_code == 200
        assert "window.runRecs" in resp.text
        assert "application/javascript" in resp.headers["content-type"]

    def test_plus_static_asset_route_rejects_unknown_assets(self, client):
        """Static assets are constrained to the packaged dashboard files."""
        assert client.get("/plus/static/nope.css").status_code == 404
        assert client.get("/plus/static/..%2F..%2Fpyproject.toml").status_code == 404

    def test_plus_static_asset_route_serves_bitmap_subtitle_renderer(self, client):
        response = client.get("/plus/static/web-bitmap.js?v=subtitle-release")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert "export class BitmapSubtitles" in response.text


class TestTasteProfile:
    def test_profile_form_includes_csrf_token(self, client):
        """The discover taste editor form carries the authenticated session's CSRF token."""
        try:
            _signup_and_login(client)

            html = client.get("/plus/discover?mode=recommend").text
            form_match = re.search(
                r'<form[^>]+action="/plus/dashboard/profile"[^>]*>(.*?)</form>',
                html,
                re.DOTALL,
            )
            assert form_match is not None
            assert 'name="csrf_token"' in form_match.group(1)
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_save_taste_profile_requires_csrf(self, client):
        """POST /plus/dashboard/profile rejects a missing CSRF token."""
        try:
            _signup_and_login(client)

            resp = client.post(
                "/plus/dashboard/profile",
                data={"profile": "Untrusted overwrite.", "return_to": "/plus/discover"},
                follow_redirects=False,
            )
            assert resp.status_code == 403

            discover = client.get("/plus/discover?mode=recommend")
            assert "Untrusted overwrite." not in discover.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_save_taste_profile(self, client):
        """POST /plus/dashboard/profile saves content and discover shows it."""
        try:
            _signup_and_login(client)
            token = _csrf_token(client)

            resp = client.post(
                "/plus/dashboard/profile",
                data={
                    "profile": "I love slice of life and romance anime.",
                    "csrf_token": token,
                    "return_to": "/plus/discover?mode=recommend",
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert resp.headers["location"] == "/plus/discover?mode=recommend"

            resp2 = client.get("/plus/discover?mode=recommend")
            assert "I love slice of life and romance anime." in resp2.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_shows_saved_profile(self, client):
        """Saved taste profile text is preloaded in the textarea."""
        try:
            _signup_and_login(client)
            token = _csrf_token(client)

            client.post(
                "/plus/dashboard/profile",
                data={
                    "profile": "Prefer dark fantasy and psychological thrillers.",
                    "csrf_token": token,
                    "return_to": "/plus/discover",
                },
            )

            resp = client.get("/plus/discover?mode=recommend")
            assert "Prefer dark fantasy and psychological thrillers." in resp.text
            assert 'name="profile"' in resp.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_save_empty_profile(self, client):
        """Saving an empty profile is allowed (clears the stored profile)."""
        try:
            _signup_and_login(client)
            token = _csrf_token(client)

            client.post(
                "/plus/dashboard/profile",
                data={
                    "profile": "Some profile text.",
                    "csrf_token": token,
                    "return_to": "/plus/discover",
                },
            )

            resp = client.post(
                "/plus/dashboard/profile",
                data={"profile": "", "csrf_token": token, "return_to": "/plus/discover"},
                follow_redirects=False,
            )
            assert resp.status_code == 302

            resp2 = client.get("/plus/discover?mode=recommend")
            assert 'name="profile"' in resp2.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)


class TestDefaultAniListUsername:
    def test_settings_saves_and_dashboard_prefills_username(self, client):
        """Saving a default AniList username pre-fills the dashboard form input."""
        try:
            _signup_and_login(client)

            resp = client.post(
                "/plus/settings",
                data={
                    "anilist_username": "demo-anilist-user",
                    "csrf_token": _csrf_token(client),
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302

            html = client.get("/plus/discover?mode=recommend").text
            assert 'name="username"' in html
            assert 'value="demo-anilist-user"' in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_blank_run_uses_stored_default_username(self, client):
        """A run with a blank username falls back to the stored default, not email."""
        captured = {}

        def _capture(cfg):
            captured["username"] = cfg.username
            return []

        try:
            _signup_and_login(client)
            client.post(
                "/plus/settings",
                data={
                    "anilist_username": "demo-anilist-user",
                    "csrf_token": _csrf_token(client),
                },
            )

            with patch("omakase.plus.routes.run_pipeline", side_effect=_capture):
                resp = client.post(
                    "/plus/api/run",
                    json={"source": "anilist", "mode": "fast", "count": 8, "username": ""},
                )
                _drain_job(client, resp)
            assert resp.status_code == 200
            assert captured["username"] == "demo-anilist-user"
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_typed_username_overrides_stored_default(self, client):
        """An explicitly typed username wins over the stored default."""
        captured = {}

        def _capture(cfg):
            captured["username"] = cfg.username
            return []

        try:
            _signup_and_login(client)
            client.post(
                "/plus/settings",
                data={
                    "anilist_username": "demo-anilist-user",
                    "csrf_token": _csrf_token(client),
                },
            )

            with patch("omakase.plus.routes.run_pipeline", side_effect=_capture):
                resp = client.post(
                    "/plus/api/run",
                    json={
                        "source": "anilist",
                        "mode": "fast",
                        "count": 8,
                        "username": "someoneelse",
                    },
                )
                _drain_job(client, resp)
            assert captured["username"] == "someoneelse"
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)


class TestAddRoutes:
    def test_add_anime_is_a_first_class_preview_before_acquisition(self, client):
        try:
            _signup_and_login(client)
            csrf_token = _csrf_token(client)
            target = MediaTarget(
                anilist_id=21,
                title="Cowboy Bebop",
                search_titles=("Cowboy Bebop",),
                format="TV",
                status="FINISHED",
                episodes=26,
                aired_episodes=26,
                season_number=1,
                season="SPRING",
                season_year=1998,
                genres=("Action", "Sci-Fi"),
                next_airing_at=None,
            )

            page = client.get("/plus/add", follow_redirects=False)
            assert page.status_code == 302
            assert page.headers["location"] == "/plus/discover?mode=add"
            add_mode = client.get(page.headers["location"])
            assert "Add to your library" in add_mode.text
            assert 'action="/plus/add/preview"' in add_mode.text
            with patch(
                "omakase.plus.direct.search_direct_requests",
                return_value=(target,),
            ):
                preview = client.post(
                    "/plus/add/preview",
                    data={
                        "query": "Cowboy Bebop",
                        "season": "",
                        "csrf_token": csrf_token,
                    },
                )

            assert preview.status_code == 200
            assert "Confirm the exact title" in preview.text
            assert "AniList 21" in preview.text
            assert "/plus/media/21/poster" in preview.text
            assert 'action="/plus/add/acquire"' in preview.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_add_anime_acquires_the_previewed_anilist_identity(self, client):
        try:
            _signup_and_login(client)
            csrf_token = _csrf_token(client)
            target = MediaTarget(
                anilist_id=178789,
                title="Mushoku Tensei: Jobless Reincarnation Season 3",
                search_titles=("Mushoku Tensei III",),
                format="TV",
                status="RELEASING",
                episodes=14,
                aired_episodes=3,
                season_number=3,
                season="SUMMER",
                season_year=2026,
                genres=("Adventure", "Fantasy"),
                next_airing_at=1783900000,
            )
            sync = AsyncMock(return_value=SimpleNamespace(outcome="degraded", monitor_results=()))

            with (
                patch(
                    "omakase.plus.direct.resolve_direct_request", return_value=target
                ) as resolver,
                patch("omakase.plus.routes.run_media_sync", new=sync),
                patch("omakase.plus.routes.with_valid_token", side_effect=ValueError),
            ):
                response = client.post(
                    "/plus/add/acquire",
                    data={
                        "anilist_id": "178789",
                        "season": "Season 3",
                        "csrf_token": csrf_token,
                    },
                    follow_redirects=False,
                )

            assert response.status_code == 302
            assert response.headers["location"].startswith("/plus/library/")
            resolver.assert_called_once_with("178789", season="Season 3")
            sync.assert_awaited_once()
            with _connect_client_db(client) as conn:
                monitor = conn.execute(
                    """SELECT next_airing_at FROM media_monitors
                       WHERE anilist_id = 178789"""
                ).fetchone()
            assert monitor["next_airing_at"] == 1783900000
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)


class TestProductShell:
    def test_product_pages_render_semantic_navigation_and_empty_states(self, client):
        try:
            _signup_and_login(client)
            for path in (
                "/plus/home",
                "/plus/library",
                "/plus/discover",
                "/plus/activity",
                "/plus/health/overview",
                "/plus/setup",
                "/plus/settings",
            ):
                response = client.get(path)
                assert response.status_code == 200, path
                assert 'id="main-content"' in response.text
                assert 'aria-label="Primary navigation"' in response.text
                assert 'name="csrf_token"' in response.text
                assert "Omakase Plus" in response.text

            home = client.get("/plus/home").text
            library = client.get("/plus/library").text
            # Empty Home keeps the three Continue Watching shelves, each with its
            # own empty line, and a neutral banner that still offers a way in.
            assert "Nothing in progress yet" in home
            assert "Nothing here yet." in home
            assert 'href="/plus/discover">Find something to watch</a>' in home
            assert 'href="/plus/library">Browse your library</a>' in home
            assert 'href="/plus/add"' not in home
            assert "Continue Watching Anime" in home
            assert "Continue Watching Movies" in home
            assert "Continue Watching TV Shows" in home
            assert "nav-number" not in home
            assert 'list="library-title-suggestions"' in library
            assert 'name="content_type"' in library
            assert ">Series</option>" in library
            assert ">Movies</option>" in library
            assert "No titles match these filters." in library
            assert (
                "<legend>Where should we look?</legend>"
                in client.get("/plus/discover?mode=recommend").text
            )
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_product_css_avoids_gradient_and_glass_effects(self, client):
        css = client.get("/plus/static/dashboard.css").text
        product_css = css.split("Omakase Plus product shell", maxsplit=1)[1]

        assert "linear-gradient" not in product_css
        assert "backdrop-filter" not in product_css
        assert "--p-vermilion: #ed6246" in product_css
        assert "@media (max-width: 900px)" in product_css

    def test_activity_times_are_converted_to_the_viewers_timezone(self, client):
        script = client.get("/plus/static/dashboard.js").text

        assert "data-local-datetime" in script
        assert "Intl.DateTimeFormat(undefined" in script
        assert 'timeZoneName: "short"' in script
        assert "shown in your local timezone" in script
        template_root = (
            Path(__file__).resolve().parents[2] / "src" / "omakase" / "plus" / "templates"
        )
        for name in ("activity.html", "discover.html", "health.html", "home.html", "title.html"):
            assert 'data-local-datetime="' in (template_root / name).read_text(encoding="utf-8")

    def test_settings_configures_expanded_search_without_returning_secrets(
        self, client, monkeypatch
    ):
        try:
            monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
            monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")
            _signup_and_login(client)
            rejected = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "prowlarr_url": "http://private-prowlarr:9696",
                    "prowlarr_api_key": "catalog-secret-key",
                    "llm_api_key": "must-not-be-partially-saved",
                },
                follow_redirects=False,
            )
            assert rejected.status_code == 422
            with _connect_client_db(client) as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) FROM user_secrets WHERE key_name IN ('prowlarr_url','prowlarr_api_key','llm_api_key')"
                    ).fetchone()[0]
                    == 0
                )
            saved = client.post(
                "/plus/settings/connections/prowlarr/save",
                data={
                    "csrf_token": _csrf_token(client),
                    "url": "http://private-prowlarr:9696",
                    "api_key": "catalog-secret-key",
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303
            response = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "prowlarr_indexer_ids": "4,7",
                    "prowlarr_categories": "5070,5000",
                },
                follow_redirects=False,
            )

            assert response.status_code == 302
            html = client.get("/plus/settings/sources").text
            assert "Expanded search active" in html
            assert "Nyaa and every enabled Prowlarr source" in html
            assert "http://private-prowlarr:9696" not in html
            assert "catalog-secret-key" not in html
            with _connect_client_db(client) as conn:
                keys = {
                    row["key_name"]
                    for row in conn.execute(
                        """SELECT key_name FROM user_secrets
                           WHERE key_name LIKE 'prowlarr_%'"""
                    ).fetchall()
                }
            assert keys == {
                "prowlarr_url",
                "prowlarr_api_key",
                "prowlarr_indexer_ids",
                "prowlarr_categories",
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_owner_can_inspect_change_and_restore_download_criteria(self, client):
        try:
            _signup_and_login(client)
            defaults = client.get("/plus/settings/downloads").text
            assert "Video quality &amp; downloads" in defaults
            assert "Advanced download rules" in defaults
            assert "1080p → 2160p → 720p" in defaults
            assert "Policy revision" in defaults

            response = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "acquisition_policy_present": "1",
                    "resolution_1": "1080p",
                    "resolution_2": "720p",
                    "resolution_3": "",
                    "minimum_seeders": "3",
                    "preferred_seeders": "10",
                    "max_release_size_gib": "80",
                    "prefer_trusted": "on",
                },
                follow_redirects=False,
            )

            assert response.status_code == 302
            changed = client.get("/plus/settings/downloads").text
            assert "1080p → 720p" in changed
            assert 'value="3"' in changed
            assert "80 GiB" in changed
            assert "Selectable subtitles required" in changed

            reset = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "acquisition_policy_present": "1",
                    "reset_acquisition_policy": "1",
                },
                follow_redirects=False,
            )

            assert reset.status_code == 302
            restored = client.get("/plus/settings/downloads").text
            assert "1080p → 2160p → 720p" in restored
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_settings_changes_require_csrf(self, client):
        try:
            _signup_and_login(client)

            response = client.post(
                "/plus/settings",
                data={
                    "acquisition_policy_present": "1",
                    "resolution_1": "720p",
                    "minimum_seeders": "1",
                    "preferred_seeders": "5",
                    "max_release_size_gib": "80",
                },
                follow_redirects=False,
            )

            assert response.status_code == 403
            assert "1080p → 2160p → 720p" in client.get("/plus/settings/downloads").text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_settings_validation_errors_normalize_and_reset_controls_work(self, client):
        try:
            _signup_and_login(client)
            downloads_html = client.get("/plus/settings/downloads").text
            assert 'name="reset_acquisition_policy" value="1" formnovalidate' in downloads_html
            assert '<span class="privacy-stamp">All media</span>' in downloads_html

            bad_policy = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "settings_section": "downloads",
                    "acquisition_policy_present": "1",
                    "resolution_1": "1080p",
                    "minimum_seeders": "50",
                    "preferred_seeders": "10",
                    "max_release_size_gib": "80",
                },
                follow_redirects=False,
            )
            assert bad_policy.status_code == 302
            assert bad_policy.headers["location"].startswith("/plus/settings/downloads?error=")
            err_page = client.get(bad_policy.headers["location"]).text
            assert 'role="alert"' in err_page
            assert '<details class="settings-advanced" open>' in err_page
            assert "1080p → 2160p → 720p" in err_page

            bad_sources = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "settings_section": "sources",
                    "prowlarr_indexer_ids": "abc, 0",
                    "prowlarr_categories": "5070",
                },
                follow_redirects=False,
            )
            assert bad_sources.status_code == 302
            assert bad_sources.headers["location"].startswith("/plus/settings/sources?error=")
            sources_err = client.get(bad_sources.headers["location"]).text
            assert 'role="alert"' in sources_err
            assert "<b>Stored</b>" not in sources_err
            assert "/plus/settings/connections#connection-prowlarr" in sources_err

            good_sources = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "settings_section": "sources",
                    "prowlarr_indexer_ids": "4, 7",
                    "prowlarr_categories": "5070, 5000",
                },
                follow_redirects=False,
            )
            assert good_sources.status_code == 302
            assert good_sources.headers["location"].startswith("/plus/settings/sources?saved=")
            sources_saved = client.get(good_sources.headers["location"]).text
            assert "<b>Stored</b>" in sources_saved
            assert 'name="delete_prowlarr_indexer_ids" value="1" formnovalidate' in sources_saved

            from omakase.plus.secrets import read_secret

            with _connect_client_db(client) as conn:
                assert read_secret(conn, 1, "prowlarr_indexer_ids") == "4,7"
                assert read_secret(conn, 1, "prowlarr_categories") == "5070,5000"

            set_ani = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "settings_section": "preferences",
                    "anilist_username": "Tester",
                },
                follow_redirects=False,
            )
            assert set_ani.status_code == 302
            clear_ani = client.post(
                "/plus/settings",
                data={
                    "csrf_token": _csrf_token(client),
                    "settings_section": "preferences",
                    "anilist_username": "",
                },
                follow_redirects=False,
            )
            assert clear_ani.status_code == 302
            assert "anilist_username%20removed" in clear_ani.headers["location"]
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_apple_tv_bridge_secret_is_revealed_once_and_controls_require_csrf(self, client):
        try:
            _signup_and_login(client)
            without_csrf = client.post(
                "/plus/settings/watch-integrations/apple-tv/create",
                follow_redirects=False,
            )
            assert without_csrf.status_code == 403

            created = client.post(
                "/plus/settings/watch-integrations/apple-tv/create",
                data={"csrf_token": _csrf_token(client)},
            )
            assert created.status_code == 200
            assert "Copy this secret now" in created.text
            key_match = re.search(r"data-apple-tv-key-id>([^<]+)<", created.text)
            secret_match = re.search(r"data-apple-tv-secret>([^<]+)<", created.text)
            assert key_match is not None
            assert secret_match is not None
            key_id = key_match.group(1)
            secret = secret_match.group(1)

            settings = client.get("/plus/settings/tracking").text
            assert key_id in settings
            assert secret not in settings
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT encrypted_secret, enabled FROM watch_integrations
                       WHERE user_id = ?""",
                    (_user_id(client),),
                ).fetchone()
            assert row["encrypted_secret"] != secret
            assert row["enabled"] == 1

            rotated = client.post(
                "/plus/settings/watch-integrations/apple-tv/rotate",
                data={"csrf_token": _csrf_token(client)},
            )
            rotated_secret = re.search(r"data-apple-tv-secret>([^<]+)<", rotated.text).group(1)
            assert rotated_secret != secret

            disabled = client.post(
                "/plus/settings/watch-integrations/apple-tv/disable",
                data={"csrf_token": _csrf_token(client)},
                follow_redirects=False,
            )
            assert disabled.status_code == 302
            with _connect_client_db(client) as conn:
                assert (
                    conn.execute(
                        "SELECT enabled FROM watch_integrations WHERE user_id = ?",
                        (_user_id(client),),
                    ).fetchone()[0]
                    == 0
                )
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_library_watch_actions_archive_completion_and_resolve_confirmations(self, client):
        try:
            _signup_and_login(client)
            with _connect_client_db(client) as conn:
                user_id = _user_id(client)
                monitor_id = upsert_monitor(
                    conn,
                    user_id=user_id,
                    anilist_id=154587,
                    title="Frieren",
                    search_titles=("Frieren",),
                    format="TV",
                    release_status="FINISHED",
                    season_number=1,
                    expected_episodes=3,
                    aired_episodes=3,
                )
                conn.commit()

            no_csrf = client.post(f"/plus/library/{monitor_id}/watch/next", follow_redirects=False)
            assert no_csrf.status_code == 403
            advanced = client.post(
                f"/plus/library/{monitor_id}/watch/next",
                data={"csrf_token": _csrf_token(client)},
                follow_redirects=False,
            )
            assert advanced.status_code == 302
            with _connect_client_db(client) as conn:
                assert (
                    conn.execute(
                        "SELECT progress_episodes FROM watch_entries WHERE monitor_id = ?",
                        (monitor_id,),
                    ).fetchone()[0]
                    == 1
                )

            completed = client.post(
                f"/plus/library/{monitor_id}/watch/episode",
                data={"csrf_token": _csrf_token(client), "episode": "3"},
                follow_redirects=False,
            )
            assert completed.status_code == 302
            active_html = client.get("/plus/library?lifecycle=active").text
            collection_html = client.get("/plus/library").text
            completed_html = client.get("/plus/library?lifecycle=completed").text
            assert f'href="/plus/library/{monitor_id}"' not in active_html
            assert f'href="/plus/library/{monitor_id}"' not in collection_html
            assert f'href="/plus/library/{monitor_id}"' in completed_html

            reopened = client.post(
                f"/plus/library/{monitor_id}/watch/reopen",
                data={"csrf_token": _csrf_token(client), "episode": "1"},
                follow_redirects=False,
            )
            assert reopened.status_code == 302
            with _connect_client_db(client) as conn:
                user_id = _user_id(client)
                inferred = ingest_watch_event(
                    conn,
                    user_id=user_id,
                    monitor_id=monitor_id,
                    event=WatchEvent.progress(
                        source="apple_tv",
                        episode=2,
                        confidence="inferred",
                        event_key="apple:frieren:e2",
                    ),
                    observed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                )
                confirmation_id = inferred.confirmation_id
            assert confirmation_id is not None
            title_html = client.get(f"/plus/library/{monitor_id}").text
            assert "Confirm episode 2" in title_html

            accepted = client.post(
                f"/plus/library/{monitor_id}/watch/confirm/{confirmation_id}/accept",
                data={"csrf_token": _csrf_token(client)},
                follow_redirects=False,
            )
            assert accepted.status_code == 302
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT entry.progress_episodes, confirmation.status
                       FROM watch_entries AS entry
                       JOIN watch_confirmations AS confirmation
                         ON confirmation.watch_entry_id = entry.id
                       WHERE confirmation.id = ?""",
                    (confirmation_id,),
                ).fetchone()
            assert dict(row) == {"progress_episodes": 2, "status": "accepted"}
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_completed_library_bulk_cleanup_previews_and_opens_safe_removals(self, client):
        try:
            _signup_and_login(client)
            monitor_id = _seed_removable_title(client)
            with _connect_client_db(client) as conn:
                ingest_watch_event(
                    conn,
                    user_id=_user_id(client),
                    monitor_id=monitor_id,
                    event=WatchEvent.complete(source="manual", event_key="manual:solo:complete"),
                    observed_at="2026-08-27T12:00:00Z",
                )

            completed = client.get("/plus/library?lifecycle=completed")
            assert 'name="monitor_id"' in completed.text
            assert "Clean up selected media" in completed.text
            without_csrf = client.post(
                "/plus/library/completed/cleanup/preview",
                data={"monitor_id": str(monitor_id)},
            )
            assert without_csrf.status_code == 403

            preview = client.post(
                "/plus/library/completed/cleanup/preview",
                data={"csrf_token": _csrf_token(client), "monitor_id": str(monitor_id)},
            )
            assert preview.status_code == 200
            assert "REMOVE 1 COMPLETED TITLES" in preview.text
            assert "1 Real-Debrid item eligible" in preview.text
            assert "AniList progress stays intact" in preview.text

            requested = client.post(
                "/plus/library/completed/cleanup/request",
                data={
                    "csrf_token": _csrf_token(client),
                    "monitor_id": str(monitor_id),
                    "confirmation": "REMOVE 1 COMPLETED TITLES",
                },
                follow_redirects=False,
            )
            assert requested.status_code == 302
            assert "lifecycle=completed" in requested.headers["location"]
            with _connect_client_db(client) as conn:
                assert conn.execute("SELECT count(*) FROM media_title_removals").fetchone()[0] == 1
                assert (
                    conn.execute(
                        "SELECT status FROM anilist_plannings WHERE anilist_id = 151807"
                    ).fetchone()[0]
                    == "COMPLETED"
                )
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_title_dossier_and_repairs_are_owner_scoped_and_csrf_protected(self, client):
        try:
            _signup_and_login(client)
            with _connect_client_db(client) as conn:
                owner_id = _user_id(client)
                monitor_id = upsert_monitor(
                    conn,
                    user_id=owner_id,
                    anilist_id=180560,
                    title="Hell's Paradise Season 2",
                    search_titles=("Jigokuraku 2nd Season",),
                    format="TV",
                    release_status="RELEASING",
                    season_number=2,
                    expected_episodes=12,
                    aired_episodes=4,
                )
                stranger_id = conn.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, 'hash')",
                    ("stranger@example.com",),
                ).lastrowid
                stranger_monitor_id = upsert_monitor(
                    conn,
                    user_id=int(stranger_id),
                    anilist_id=999,
                    title="Private Stranger Title",
                    format="TV",
                    release_status="RELEASING",
                    aired_episodes=1,
                )
                conn.commit()

            dossier = client.get(f"/plus/library/{monitor_id}")
            assert dossier.status_code == 200
            assert "Hell&#39;s Paradise Season 2" in dossier.text
            assert "Playback and media help" in dossier.text
            assert "Check the title and season match" in dossier.text
            assert client.get(f"/plus/library/{stranger_monitor_id}").status_code == 404

            rejected = client.post(
                f"/plus/library/{monitor_id}/rematch-plan",
                data={},
            )
            assert rejected.status_code == 403

            preview = client.post(
                f"/plus/library/{monitor_id}/rematch-plan",
                data={"csrf_token": _csrf_token(client)},
            )
            assert preview.status_code == 200
            assert "Title and season check" in preview.text
            assert "No download, file, or Plex library has changed" in preview.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_discover_javascript_returns_to_the_product_surface(self, client):
        script = client.get("/plus/static/dashboard.js").text

        assert "dataset.successUrl" in script
        assert 'successUrl === "/plus/discover"' in script
        assert "window.location.assign(destination)" in script
        assert '"X-CSRF-Token": csrfToken()' in script


class TestTitleRemovalRoutes:
    def test_title_dossier_opens_explicit_removal_confirmation(self, client):
        try:
            _signup_and_login(client)
            monitor_id = _seed_removable_title(client)

            dossier = client.get(f"/plus/library/{monitor_id}")
            confirmation = client.get(f"/plus/library/{monitor_id}/remove")

            assert dossier.status_code == confirmation.status_code == 200
            assert f'href="/plus/library/{monitor_id}/remove"' in dossier.text
            assert "Library actions" in dossier.text
            assert dossier.text.index(
                f'href="/plus/library/{monitor_id}/remove"'
            ) < dossier.text.index('<details class="title-technical-details"')
            assert "Type Solo Leveling to confirm" in confirmation.text
            assert "AniList" in confirmation.text
            assert "AniList will not change" in confirmation.text
            assert confirmation.text.index("Remove from Plex") < confirmation.text.index(
                "Delete eligible Real-Debrid downloads"
            )
            assert "Omakase-owned Real-Debrid" in confirmation.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_title_removal_requires_exact_confirmation_and_preserves_anilist(self, client):
        try:
            _signup_and_login(client)
            monitor_id = _seed_removable_title(client)
            csrf = _csrf_token(client)

            rejected = client.post(
                f"/plus/library/{monitor_id}/remove",
                data={"confirmation": "solo leveling", "csrf_token": csrf},
                follow_redirects=False,
            )

            assert rejected.status_code == 422
            with _connect_client_db(client) as conn:
                before = conn.execute(
                    """SELECT monitor.active, asset.status, planning.status
                       FROM media_monitors AS monitor
                       JOIN media_assets AS asset ON asset.monitor_id = monitor.id
                       JOIN anilist_plannings AS planning
                         ON planning.id = monitor.anilist_planning_id
                       WHERE monitor.id = ?""",
                    (monitor_id,),
                ).fetchone()
            assert tuple(before) == (1, "downloaded", "COMPLETED")

            accepted = client.post(
                f"/plus/library/{monitor_id}/remove",
                data={"confirmation": "Solo Leveling", "csrf_token": csrf},
                follow_redirects=False,
            )

            assert accepted.status_code == 302
            assert f"/plus/library/{monitor_id}" in accepted.headers["location"]
            with _connect_client_db(client) as conn:
                after = conn.execute(
                    """SELECT monitor.active, asset.status, planning.status, removal.status
                       FROM media_monitors AS monitor
                       JOIN media_assets AS asset ON asset.monitor_id = monitor.id
                       JOIN anilist_plannings AS planning
                         ON planning.id = monitor.anilist_planning_id
                       JOIN media_title_removals AS removal
                         ON removal.monitor_id = monitor.id
                       WHERE monitor.id = ?""",
                    (monitor_id,),
                ).fetchone()
            assert tuple(after) == (0, "quarantined", "COMPLETED", "awaiting_plex")
            pending_page = client.get(f"/plus/library/{monitor_id}")
            assert "Removing from Plex" in pending_page.text
            assert "Playback and media help" not in pending_page.text
            assert "Retry now" not in pending_page.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)
