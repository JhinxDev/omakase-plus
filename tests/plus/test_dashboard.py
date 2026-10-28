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
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.db import _db, run_migrations
from omakase.plus.deps import get_db as _deps_get_db
from omakase.plus.feedback import save_feedback
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
from omakase.types import Recommendation

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

    def test_dashboard_renders_for_logged_in_user(self, client):
        """After login the dashboard returns 200 with all expected sections."""
        try:
            _signup_and_login(client)

            resp = client.get("/plus/dashboard")
            assert resp.status_code == 200
            html = resp.text

            # Header
            assert "Omakase Plus" in html
            assert "alice@example.com" in html
            assert "Settings" in html
            assert "Log out" in html

            # Sections
            assert "Taste Profile" in html
            assert "Add Anime" in html
            assert "Run Recommendation" in html
            assert "Recent Runs" in html
            assert "Planning Queue" in html

            # Form elements
            assert 'name="profile"' in html
            assert 'name="query"' in html
            assert 'name="season"' in html
            assert 'name="source"' in html
            assert 'name="username"' in html
            assert 'name="mode"' in html
            assert 'name="count"' in html
            assert 'name="temperature"' in html
            assert 'name="skip_profile"' in html
            assert 'name="use_planning"' in html
            assert 'name="model"' in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_references_external_assets(self, client):
        """Dashboard CSS/JS is served as Plus static assets, not inline blocks."""
        try:
            _signup_and_login(client)

            html = client.get("/plus/dashboard").text

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

    def test_dashboard_renders_direct_download_card(self, client):
        """The dashboard exposes a phone-friendly direct auto-download form."""
        try:
            _signup_and_login(client)

            html = client.get("/plus/dashboard").text

            assert "Add Anime" in html
            assert 'action="/plus/dashboard/direct-download"' in html
            assert 'name="query"' in html
            assert 'name="season"' in html
            assert "Add + Download" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_has_mobile_first_shell_and_priority_order(self, client):
        """Dashboard markup exposes the new shell and phone-first section order."""
        try:
            _signup_and_login(client)

            html = client.get("/plus/dashboard").text

            assert 'class="dashboard-shell"' in html
            assert 'class="hero-panel"' in html
            assert 'class="quick-stats"' in html
            assert html.index("Add Anime") < html.index("Run Recommendation")
            assert html.index("Run Recommendation") < html.index("Tonight's Tasting Menu")
            assert html.index("Planning Queue") < html.index("Taste Profile")
            assert html.index("Taste Profile") < html.index("Recent Runs")
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_renders_lane_control(self, client):
        """The run form exposes all recommendation lanes to authenticated users."""
        try:
            _signup_and_login(client)

            html = client.get("/plus/dashboard").text
            assert 'name="lane"' in html
            assert 'value="best_match"' in html
            assert 'value="new_seasons"' in html
            assert 'value="hidden_gems"' in html
            assert 'value="plan_list"' in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)


class TestTasteProfile:
    def test_profile_form_includes_csrf_token(self, client):
        """The profile form carries the authenticated session's CSRF token."""
        try:
            _signup_and_login(client)

            html = client.get("/plus/dashboard").text
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
                data={"profile": "Untrusted overwrite."},
                follow_redirects=False,
            )
            assert resp.status_code == 403

            dashboard = client.get("/plus/dashboard")
            assert "Untrusted overwrite." not in dashboard.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_save_taste_profile(self, client):
        """POST /plus/dashboard/profile saves content and dashboard shows it."""
        try:
            _signup_and_login(client)
            token = _csrf_token(client)

            resp = client.post(
                "/plus/dashboard/profile",
                data={
                    "profile": "I love slice of life and romance anime.",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert resp.headers["location"] == "/plus/dashboard"

            resp2 = client.get("/plus/dashboard")
            assert "I love slice of life and romance anime." in resp2.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_shows_saved_profile(self, client):
        """Saved taste profile text is preloaded in the textarea."""
        try:
            _signup_and_login(client)
            token = _csrf_token(client)

            # Save a profile
            client.post(
                "/plus/dashboard/profile",
                data={
                    "profile": "Prefer dark fantasy and psychological thrillers.",
                    "csrf_token": token,
                },
            )

            # Verify it shows up
            resp = client.get("/plus/dashboard")
            assert "Prefer dark fantasy and psychological thrillers." in resp.text
            assert 'name="profile"' in resp.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_save_empty_profile(self, client):
        """Saving an empty profile is allowed (clears the stored profile)."""
        try:
            _signup_and_login(client)
            token = _csrf_token(client)

            # First save something
            client.post(
                "/plus/dashboard/profile",
                data={"profile": "Some profile text.", "csrf_token": token},
            )

            # Then clear it
            resp = client.post(
                "/plus/dashboard/profile",
                data={"profile": "", "csrf_token": token},
                follow_redirects=False,
            )
            assert resp.status_code == 302

            resp2 = client.get("/plus/dashboard")
            # textarea should be present (empty is fine)
            assert 'name="profile"' in resp2.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)


class TestRunRecommendation:
    def test_run_recommendation_endpoint(self, client):
        """POST /plus/dashboard/run with mocked pipeline creates a run_history row."""
        mock_recs = [
            Recommendation(
                title="One Piece",
                predicted_score=9.2,
                reasoning="Matches your love for long-running shonen.",
                best_match_from_history="Naruto",
                url="https://anilist.co/anime/21/",
                source="anilist",
            ),
            Recommendation(
                title="Attack on Titan",
                predicted_score=8.7,
                reasoning="Dark fantasy with complex characters.",
                best_match_from_history="Death Note",
                url="https://anilist.co/anime/16498/",
                source="anilist",
            ),
        ]
        try:
            _signup_and_login(client)

            with patch("omakase.plus.routes.run_pipeline", return_value=mock_recs):
                resp = client.post(
                    "/plus/dashboard/run",
                    data={
                        "source": "anilist",
                        "mode": "fast",
                        "count": "5",
                        "temperature": "0.5",
                        "username": "testuser",
                        "skip_profile": "true",
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            assert "?run=" in resp.headers["location"]

            # Follow the redirect to the run results
            resp2 = client.get(resp.headers["location"])
            assert resp2.status_code == 200
            html = resp2.text
            assert "One Piece" in html
            assert "Attack on Titan" in html
            assert "9.2" in html
            assert "8.7" in html
            assert "Recent Runs" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_run_with_missing_username_uses_email(self, client):
        """When username is empty, the route falls back to the email local part."""
        mock_recs = [
            Recommendation(
                title="Test Anime",
                predicted_score=7.5,
                reasoning="Good show.",
                best_match_from_history="",
                url=None,
                source="anilist",
            ),
        ]
        try:
            _signup_and_login(client)

            with patch("omakase.plus.routes.run_pipeline", return_value=mock_recs):
                resp = client.post(
                    "/plus/dashboard/run",
                    data={
                        "source": "anilist",
                        "mode": "fast",
                        "count": "5",
                        "username": "",
                        "skip_profile": "true",
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            assert "?run=" in resp.headers["location"]
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_run_requires_auth(self, client):
        """POST /plus/dashboard/run without login is rejected."""
        resp = client.post(
            "/plus/dashboard/run",
            data={"source": "anilist", "mode": "fast", "count": "5"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 401)

    def test_dashboard_run_passes_lane_feedback_and_persists_lane(self, client):
        """Form runs pass lane/feedback into the engine and persist the selected lane."""
        captured = {}
        mock_recs = [
            Recommendation(
                title="Base 2",
                predicted_score=8.4,
                reasoning="Direct continuation of a liked series.",
                best_match_from_history="Base",
                url="https://anilist.co/anime/123/",
                source="anilist",
            ),
        ]

        def _capture(cfg):
            captured["lane"] = cfg.recommendation_lane
            captured["use_planning"] = cfg.use_planning
            captured["feedback_types"] = [f.feedback_type for f in cfg.feedback]
            return mock_recs

        try:
            _signup_and_login(client)
            with _connect_client_db(client) as conn:
                user_id = conn.execute(
                    "SELECT id FROM users WHERE email = ?",
                    ("alice@example.com",),
                ).fetchone()["id"]
                save_feedback(conn, user_id, "anilist", 123, "Base 2", "interested", None)

            with patch("omakase.plus.routes.run_pipeline", side_effect=_capture):
                resp = client.post(
                    "/plus/dashboard/run",
                    data={
                        "source": "anilist",
                        "mode": "fast",
                        "count": "5",
                        "temperature": "0.5",
                        "username": "testuser",
                        "skip_profile": "true",
                        "lane": "plan_list",
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            run_id = int(resp.headers["location"].split("run=")[1])
            assert captured == {
                "lane": "plan_list",
                "use_planning": True,
                "feedback_types": ["interested"],
            }
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    "SELECT lane FROM run_history WHERE id = ?",
                    (run_id,),
                ).fetchone()
            assert row["lane"] == "plan_list"
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
                    "anilist_username": "HeyiTzSenpai",
                    "csrf_token": _csrf_token(client),
                },
                follow_redirects=False,
            )
            assert resp.status_code == 302

            html = client.get("/plus/dashboard").text
            assert 'name="username"' in html
            assert 'value="HeyiTzSenpai"' in html
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
                    "anilist_username": "HeyiTzSenpai",
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
            assert captured["username"] == "HeyiTzSenpai"
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
                    "anilist_username": "HeyiTzSenpai",
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


class TestLibraryOperations:
    @staticmethod
    def _target(anilist_id: int, title: str, *, status: str = "FINISHED") -> MediaTarget:
        return MediaTarget(
            anilist_id=anilist_id,
            title=title,
            search_titles=(title, f"{title} alias"),
            format="TV",
            status=status,
            episodes=12,
            aired_episodes=3 if status == "RELEASING" else 12,
            season_number=1,
            season="SUMMER",
            season_year=2026,
            genres=("Action",),
            next_airing_at=1783900000 if status == "RELEASING" else None,
        )

    def test_dashboard_has_bulk_add_and_monitor_status(self, client):
        try:
            _signup_and_login(client)
            user_id = _user_id(client)
            with _connect_client_db(client) as db:
                monitor_id = upsert_monitor(
                    db,
                    user_id=user_id,
                    anilist_id=178789,
                    title="Mushoku Tensei Season 3",
                    search_titles=("Mushoku Tensei III",),
                    genres=("Adventure", "Fantasy"),
                    format="TV",
                    release_status="RELEASING",
                    season_number=3,
                    expected_episodes=14,
                    aired_episodes=3,
                    next_check_at="2026-07-16T02:00:00Z",
                    last_result="Coverage incomplete",
                )
                db.commit()

            html = client.get("/plus/dashboard").text
            assert 'name="bulk_query"' in html
            assert 'action="/plus/dashboard/bulk-preview"' in html
            assert 'id="library-jobs"' in html
            assert "Airing monitors" in html
            assert "Mushoku Tensei Season 3" in html
            assert "0 / 3 usable" in html
            assert f'value="{monitor_id}"' in html
            assert 'name="csrf_token"' in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_bulk_preview_mutates_nothing_and_disambiguates_only_ambiguous_rows(self, client):
        try:
            _signup_and_login(client)
            ready = self._target(178789, "Mushoku Tensei Season 3", status="RELEASING")
            ambiguous = (
                self._target(1001, "Ambiguous Hero"),
                self._target(1002, "Ambiguous Hero: Reborn"),
            )

            def resolve(value: str, season: str = ""):
                assert season == ""
                if value == "178789":
                    return ready
                from omakase.plus.direct import AmbiguousMediaError

                raise AmbiguousMediaError(ambiguous)

            with patch("omakase.plus.direct.resolve_direct_request", side_effect=resolve):
                response = client.post(
                    "/plus/dashboard/bulk-preview",
                    data={"bulk_query": "178789\nAmbiguous Hero"},
                )

            assert response.status_code == 200
            assert "Ready" in response.text
            assert "Choose the intended title" in response.text
            assert "Mushoku Tensei Season 3" in response.text
            assert "Ambiguous Hero: Reborn" in response.text
            assert 'name="choice_1"' in response.text
            with _connect_client_db(client) as db:
                assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 0
                assert db.execute("SELECT COUNT(*) FROM anilist_plannings").fetchone()[0] == 0
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_bulk_preview_rejects_more_than_25_nonblank_lines(self, client):
        try:
            _signup_and_login(client)
            response = client.post(
                "/plus/dashboard/bulk-preview",
                data={"bulk_query": "\n".join(str(value) for value in range(1, 27))},
            )
            assert response.status_code == 422
            assert "25" in response.text
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_bulk_confirm_requires_csrf_and_uses_shared_scheduler_once(self, client):
        try:
            _signup_and_login(client)
            target = self._target(178789, "Mushoku Tensei Season 3", status="RELEASING")
            sync = AsyncMock(return_value=SimpleNamespace(outcome="degraded"))

            with patch("omakase.plus.direct.resolve_direct_request", return_value=target):
                rejected = client.post(
                    "/plus/dashboard/bulk-confirm",
                    data={"ready_id": "178789"},
                    follow_redirects=False,
                )
            assert rejected.status_code == 403

            token = _csrf_token(client)
            with (
                patch("omakase.plus.direct.resolve_direct_request", return_value=target),
                patch("omakase.plus.routes.run_media_sync", new=sync),
                patch("omakase.plus.routes.with_valid_token", side_effect=ValueError),
            ):
                accepted = client.post(
                    "/plus/dashboard/bulk-confirm",
                    data={"ready_id": "178789", "csrf_token": token},
                    follow_redirects=False,
                )

            assert accepted.status_code == 302
            with _connect_client_db(client) as db:
                monitor = db.execute(
                    "SELECT id, anilist_id FROM media_monitors WHERE anilist_id = 178789"
                ).fetchone()
                planning_count = db.execute(
                    "SELECT COUNT(*) FROM anilist_plannings WHERE anilist_id = 178789"
                ).fetchone()[0]
            assert monitor["anilist_id"] == 178789
            assert planning_count == 1
            sync.assert_awaited_once()
            assert sync.await_args.kwargs == {
                "entrypoint": "bulk_confirmation",
                "monitor_ids": (monitor["id"],),
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_sync_now_and_monitor_change_enforce_owner_and_csrf(self, client):
        try:
            _signup_and_login(client)
            own_user = _user_id(client)
            with _connect_client_db(client) as db:
                own_monitor = upsert_monitor(
                    db,
                    user_id=own_user,
                    anilist_id=1,
                    title="Owned Show",
                    search_titles=("Owned Show",),
                    release_status="RELEASING",
                    season_number=1,
                    expected_episodes=12,
                    aired_episodes=1,
                )
                other_user = db.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                    ("other-owner@example.com", "hash"),
                ).lastrowid
                other_monitor = upsert_monitor(
                    db,
                    user_id=int(other_user),
                    anilist_id=2,
                    title="Other Show",
                    search_titles=("Other Show",),
                )
                db.commit()
            token = _csrf_token(client)
            sync = AsyncMock(return_value=SimpleNamespace(outcome="complete"))

            missing_csrf = client.post(
                "/plus/dashboard/sync-monitor",
                data={"monitor_id": own_monitor},
                follow_redirects=False,
            )
            assert missing_csrf.status_code == 403
            with patch("omakase.plus.routes.run_media_sync", new=sync):
                not_owned = client.post(
                    "/plus/dashboard/sync-monitor",
                    data={"monitor_id": other_monitor, "csrf_token": token},
                    follow_redirects=False,
                )
                accepted = client.post(
                    "/plus/dashboard/sync-monitor",
                    data={"monitor_id": own_monitor, "csrf_token": token},
                    follow_redirects=False,
                )
            assert not_owned.status_code == 404
            assert accepted.status_code == 302
            sync.assert_awaited_once()
            assert sync.await_args.kwargs == {
                "entrypoint": "dashboard_sync",
                "monitor_ids": (own_monitor,),
            }

            paused = client.post(
                "/plus/dashboard/monitor",
                data={
                    "monitor_id": own_monitor,
                    "action": "pause",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert paused.status_code == 302
            with _connect_client_db(client) as db:
                assert (
                    db.execute(
                        "SELECT active FROM media_monitors WHERE id = ?", (own_monitor,)
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    db.execute(
                        "SELECT active FROM media_monitors WHERE id = ?", (other_monitor,)
                    ).fetchone()[0]
                    == 1
                )
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)


class TestPlanButton:
    def test_remove_local_completed_entry_never_deletes_remote_anilist_history(self, client):
        try:
            _signup_and_login(client)
            user_id = _user_id(client)
            with _connect_client_db(client) as conn:
                conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status)
                       VALUES (?, 151807, 'Solo Leveling', 'COMPLETED')""",
                    (user_id,),
                )
                conn.commit()
            token_context = MagicMock()
            token_context.__enter__.return_value = "anilist-token"
            token_context.__exit__.return_value = False

            with (
                patch("omakase.plus.routes.with_valid_token", return_value=token_context),
                patch("omakase.plus.anilist.remove_from_planning") as remote_remove,
            ):
                response = client.post(
                    "/plus/dashboard/remove-plan",
                    data={"anilist_id": 151807},
                    follow_redirects=False,
                )

            assert response.status_code == 302
            remote_remove.assert_not_called()
            with _connect_client_db(client) as conn:
                local = conn.execute(
                    """SELECT COUNT(*) FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = 151807""",
                    (user_id,),
                ).fetchone()[0]
            assert local == 0
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

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

    def test_direct_download_rejects_empty_query(self, client):
        """Direct auto-download needs a title, AniList URL, or AniList ID."""
        try:
            _signup_and_login(client)
            csrf_token = _csrf_token(client)

            resp = client.post(
                "/plus/dashboard/direct-download",
                data={"query": "", "season": "", "csrf_token": csrf_token},
                follow_redirects=False,
            )

            assert resp.status_code == 302
            assert "Enter%20an%20anime%20title%20or%20AniList%20URL" in resp.headers["location"]
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_direct_download_creates_planning_monitor_and_runs_sync(self, client):
        """Direct requests persist rich identity and use the shared sync lease."""
        try:
            _signup_and_login(client)
            csrf_token = _csrf_token(client)
            target = MediaTarget(
                anilist_id=99699,
                title="Golden Kamuy Season 3",
                search_titles=("Golden Kamuy Season 3", "Golden Kamuy 3rd Season"),
                format="TV",
                status="FINISHED",
                episodes=12,
                aired_episodes=12,
                season_number=3,
                season="FALL",
                season_year=2020,
                genres=("Action", "Adventure"),
                next_airing_at=None,
            )
            token_context = MagicMock()
            token_context.__enter__.return_value = "token"
            sync = AsyncMock(return_value=SimpleNamespace(outcome="degraded", monitor_results=()))

            with (
                patch(
                    "omakase.plus.direct.resolve_direct_request", return_value=target
                ) as resolve_mock,
                patch("omakase.plus.routes.with_valid_token", return_value=token_context),
                patch(
                    "omakase.plus.routes.ensure_in_planning",
                    return_value={
                        "created": True,
                        "entry": {
                            "id": 10,
                            "mediaId": 99699,
                            "status": "PLANNING",
                        },
                    },
                ) as ensure_mock,
                patch("omakase.plus.routes.run_media_sync", new=sync),
            ):
                resp = client.post(
                    "/plus/dashboard/direct-download",
                    data={
                        "query": "Golden Kamuy",
                        "season": "3",
                        "csrf_token": csrf_token,
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            assert "Media%20monitor%20enrolled%3B%20sync%20degraded" in resp.headers["location"]
            resolve_mock.assert_called_once_with("Golden Kamuy", season="3")
            ensure_mock.assert_called_once_with("token", 99699)

            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT id, title, status
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (_user_id(client), 99699),
                ).fetchone()
                monitor = conn.execute(
                    """SELECT id, search_titles_json, genres_json, release_status,
                              expected_episodes, aired_episodes
                       FROM media_monitors WHERE anilist_id = 99699"""
                ).fetchone()

            assert {key: row[key] for key in ("title", "status")} == {
                "title": "Golden Kamuy Season 3",
                "status": "PLANNING",
            }
            assert monitor["search_titles_json"] == (
                '["Golden Kamuy Season 3","Golden Kamuy 3rd Season"]'
            )
            assert monitor["genres_json"] == '["Action","Adventure"]'
            assert tuple(monitor)[3:] == ("FINISHED", 12, 12)
            sync.assert_awaited_once()
            assert sync.await_args.kwargs == {
                "entrypoint": "direct_request",
                "monitor_ids": (monitor["id"],),
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_releasing_direct_request_auto_enrolls_and_uses_shared_lease(self, client):
        try:
            _signup_and_login(client)
            csrf_token = _csrf_token(client)
            target = MediaTarget(
                anilist_id=178789,
                title="Mushoku Tensei: Jobless Reincarnation Season 3",
                search_titles=("Mushoku Tensei III", "無職転生Ⅲ"),
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
                patch("omakase.plus.direct.resolve_direct_request", return_value=target),
                patch("omakase.plus.routes.run_media_sync", new=sync),
                patch("omakase.plus.routes.with_valid_token", side_effect=ValueError),
            ):
                response = client.post(
                    "/plus/dashboard/direct-download",
                    data={
                        "query": "178789",
                        "season": "Season 3",
                        "csrf_token": csrf_token,
                    },
                    follow_redirects=False,
                )

            assert response.status_code == 302
            with _connect_client_db(client) as conn:
                monitor = conn.execute(
                    """SELECT id, anilist_id, release_status, season_number,
                              aired_episodes, active
                       FROM media_monitors WHERE anilist_id = 178789"""
                ).fetchone()
                planning_count = conn.execute(
                    "SELECT COUNT(*) FROM anilist_plannings WHERE anilist_id = 178789"
                ).fetchone()[0]
            assert tuple(monitor)[1:] == (178789, "RELEASING", 3, 3, 1)
            assert planning_count == 1
            sync.assert_awaited_once()
            assert sync.await_args.kwargs == {
                "entrypoint": "direct_request",
                "monitor_ids": (monitor["id"],),
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_direct_download_reuses_existing_planning_row(self, client):
        """Retrying a direct request should not duplicate the local queue row."""
        try:
            _signup_and_login(client)
            csrf_token = _csrf_token(client)
            user_id = _user_id(client)
            with _connect_client_db(client) as conn:
                conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, 21, "Cowboy Bebop", "PLANNING"),
                )
                conn.commit()

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
            sync = AsyncMock(return_value=SimpleNamespace(outcome="complete", monitor_results=()))

            with (
                patch("omakase.plus.direct.resolve_direct_request", return_value=target),
                patch("omakase.plus.routes.add_to_planning") as add_mock,
                patch("omakase.plus.routes.run_media_sync", new=sync),
            ):
                resp = client.post(
                    "/plus/dashboard/direct-download",
                    data={
                        "query": "https://anilist.co/anime/21/Cowboy-Bebop/",
                        "csrf_token": csrf_token,
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            add_mock.assert_not_called()
            with _connect_client_db(client) as conn:
                count = conn.execute(
                    """SELECT COUNT(*) AS count
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (user_id, 21),
                ).fetchone()["count"]

            assert count == 1
            sync.assert_awaited_once()
            with _connect_client_db(client) as conn:
                monitor = conn.execute(
                    "SELECT id, search_titles_json FROM media_monitors WHERE anilist_id = 21"
                ).fetchone()
            assert monitor["search_titles_json"] == '["Cowboy Bebop"]'
            assert sync.await_args.kwargs == {
                "entrypoint": "direct_request",
                "monitor_ids": (monitor["id"],),
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_direct_download_preserves_an_existing_completed_anilist_entry(self, client):
        """Calling the blind save mutation here would downgrade completed anime."""
        try:
            _signup_and_login(client)
            target = MediaTarget(
                anilist_id=151807,
                title="Solo Leveling",
                search_titles=("Ore dake Level Up na Ken",),
                format="TV",
                status="FINISHED",
                episodes=12,
                aired_episodes=12,
                season_number=1,
                season="WINTER",
                season_year=2024,
                genres=("Action", "Fantasy"),
                next_airing_at=None,
            )
            token_context = MagicMock()
            token_context.__enter__.return_value = "anilist-token"
            token_context.__exit__.return_value = False
            sync = AsyncMock(return_value=SimpleNamespace(outcome="complete", monitor_results=()))
            completed = {
                "created": False,
                "entry": {
                    "id": 91,
                    "mediaId": 151807,
                    "status": "COMPLETED",
                    "score": 9,
                    "progress": 12,
                },
            }

            with (
                patch("omakase.plus.direct.resolve_direct_request", return_value=target),
                patch("omakase.plus.routes.run_media_sync", new=sync),
                patch("omakase.plus.routes.with_valid_token", return_value=token_context),
                patch("omakase.plus.routes.add_to_planning") as blind_save,
                patch(
                    "omakase.plus.routes.ensure_in_planning",
                    return_value=completed,
                    create=True,
                ) as ensure,
            ):
                response = client.post(
                    "/plus/dashboard/direct-download",
                    data={
                        "query": "Solo Leveling",
                        "csrf_token": _csrf_token(client),
                    },
                    follow_redirects=False,
                )

            assert response.status_code == 302
            ensure.assert_called_once_with("anilist-token", 151807)
            blind_save.assert_not_called()
            with _connect_client_db(client) as conn:
                planning = conn.execute(
                    """SELECT status FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (_user_id(client), 151807),
                ).fetchone()
            assert planning["status"] == "COMPLETED"
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_manual_rd_removal_refuses_unguarded_deletion(self, client):
        """Human routes cannot bypass the durable replacement and lease guards."""
        try:
            _signup_and_login(client)
            user_id = _user_id(client)
            with _connect_client_db(client) as conn:
                conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status, rd_torrent_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (user_id, 21, "Cowboy Bebop", "PLANNING", "rd-existing"),
                )
                conn.commit()
            rd_client = MagicMock()
            rd_client.delete_torrent = AsyncMock(return_value=True)

            with (
                patch("omakase.plus.routes.read_secret", return_value="rd-key"),
                patch(
                    "omakase.plus.realdebrid.RealDebridClient",
                    return_value=rd_client,
                ),
            ):
                response = client.post(
                    "/plus/dashboard/remove-from-rd",
                    data={"anilist_id": 21},
                    follow_redirects=False,
                )

            assert response.status_code == 302
            assert "guarded" in response.headers["location"].lower()
            rd_client.delete_torrent.assert_not_awaited()
            with _connect_client_db(client) as conn:
                rd_id = conn.execute(
                    "SELECT rd_torrent_id FROM anilist_plannings WHERE anilist_id = 21"
                ).fetchone()[0]
            assert rd_id == "rd-existing"
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_cards_have_split_plan_and_download_actions(self, client):
        """Recommendation cards render separate Plan and Download form actions."""
        try:
            _signup_and_login(client)
            run_id = _seed_run(
                client,
                [
                    {
                        "title": "One Piece",
                        "predicted_score": 9.2,
                        "reasoning": "Matches your love for long-running shonen.",
                        "best_match_from_history": "Naruto",
                        "url": "https://anilist.co/anime/21/",
                        "source": "anilist",
                        "anilist_id": 21,
                        "media_id": 21,
                    }
                ],
            )

            html = client.get(f"/plus/dashboard?run={run_id}").text
            assert "Plan &amp; Download" not in html
            assert ">Plan<" in html
            assert ">Download<" in html
            assert 'action="/plus/dashboard/plan"' in html
            assert 'action="/plus/dashboard/download"' in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_cards_have_clear_feedback_and_watched_score_actions(self, client):
        """Recommendation actions avoid overlap and can update the anime list."""
        try:
            _signup_and_login(client)
            run_id = _seed_run(
                client,
                [
                    {
                        "title": "One Piece",
                        "predicted_score": 9.2,
                        "reasoning": "Matches your love for long-running shonen.",
                        "best_match_from_history": "Naruto",
                        "url": "https://anilist.co/anime/21/",
                        "source": "anilist",
                        "anilist_id": 21,
                        "media_id": 21,
                    }
                ],
            )

            html = client.get(f"/plus/dashboard?run={run_id}").text
            assert 'data-feedback="interested"' not in html
            assert 'data-feedback="not_for_me"' in html
            assert 'data-feedback="wrong_sequel"' in html
            assert 'data-feedback="already_watched"' in html
            assert "Save to my anime list" in html
            assert "Score (optional)" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_cards_link_titles_show_chips_and_preserve_stored_anilist_id(self, client):
        """Stored recommendation metadata drives card links, chips, and actions."""
        try:
            _signup_and_login(client)
            run_id = _seed_run(
                client,
                [
                    {
                        "title": "Preserved ID",
                        "predicted_score": 8.5,
                        "reasoning": "A good sequel candidate.",
                        "best_match_from_history": "Base Show",
                        "url": "https://anilist.co/anime/999/",
                        "source": "anilist",
                        "anilist_id": 777,
                        "media_id": 888,
                        "airing_status": "RELEASING",
                        "franchise_note": "Loved franchise continuation.",
                        "sequence_warning": "Start with season 1.",
                    }
                ],
            )

            html = client.get(f"/plus/dashboard?run={run_id}").text
            assert 'href="https://anilist.co/anime/999/"' in html
            assert ">Preserved ID</a>" in html
            assert 'value="777"' in html
            assert 'value="999"' not in html
            assert "Currently airing" in html
            assert "Loved franchise continuation." in html
            assert "Start with season 1." in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_uses_media_id_for_anilist_actions_when_anilist_id_missing(self, client):
        """AniList run cards can fall back to media_id when anilist_id is absent."""
        try:
            _signup_and_login(client)
            run_id = _seed_run(
                client,
                [
                    {
                        "title": "Media ID Pick",
                        "predicted_score": 8.2,
                        "reasoning": "A solid candidate.",
                        "best_match_from_history": "Base Show",
                        "url": None,
                        "source": "anilist",
                        "anilist_id": None,
                        "media_id": 4242,
                    }
                ],
            )

            html = client.get(f"/plus/dashboard?run={run_id}").text
            assert 'value="4242"' in html
            assert ">Plan<" in html
            assert ">Download<" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_download_route_creates_local_planning_without_anilist_write(self, client):
        """The dashboard Download action does not mutate AniList remotely."""
        try:
            _signup_and_login(client)
            download_mock = AsyncMock(
                return_value={
                    "status": "no_rd_key",
                    "detail": "Real-Debrid API key not configured",
                }
            )

            with (
                patch("omakase.plus.routes.with_valid_token") as token_mock,
                patch("omakase.plus.routes.add_to_planning") as add_mock,
                patch("omakase.plus.automation.search_and_download", new=download_mock),
            ):
                resp = client.post(
                    "/plus/dashboard/download",
                    data={"anilist_id": "21", "title": "One Piece"},
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            token_mock.assert_not_called()
            add_mock.assert_not_called()
            download_mock.assert_awaited_once()
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT id, title, status, download_status
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (_user_id(client), 21),
                ).fetchone()
            assert download_mock.await_args.kwargs["planning_id"] == row["id"]
            assert {key: row[key] for key in ("title", "status", "download_status")} == {
                "title": "One Piece",
                "status": "PLANNING",
                "download_status": "no_rd_key",
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_download_route_persists_rd_provider_block_detail(self, client):
        """Provider-blocked RD batches are persisted distinctly from generic errors."""
        try:
            _signup_and_login(client)
            download_mock = AsyncMock(
                return_value={
                    "status": "rd_provider_block",
                    "detail": "Provider blocked this batch (451 infringing_file).",
                    "http_status": 451,
                    "error_code": "infringing_file",
                }
            )

            with patch("omakase.plus.automation.search_and_download", new=download_mock):
                resp = client.post(
                    "/plus/dashboard/download",
                    data={
                        "anilist_id": "170732",
                        "title": "BLEACH: Thousand-Year Blood War - The Conflict",
                    },
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            download_mock.assert_awaited_once()
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT title, download_status, download_info
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (_user_id(client), 170732),
                ).fetchone()
            assert dict(row) == {
                "title": "BLEACH: Thousand-Year Blood War - The Conflict",
                "download_status": "rd_provider_block",
                "download_info": "Provider blocked this batch (451 infringing_file).",
            }
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_download_route_caps_rd_provider_block_detail(self, client):
        """Provider-block detail is bounded before DB persistence and redirects."""
        try:
            _signup_and_login(client)
            huge_detail = "Provider blocked " + ("X" * 1200)
            download_mock = AsyncMock(
                return_value={
                    "status": "rd_provider_block",
                    "detail": huge_detail,
                    "http_status": 451,
                    "error_code": "infringing_file",
                }
            )

            with patch("omakase.plus.automation.search_and_download", new=download_mock):
                resp = client.post(
                    "/plus/dashboard/download",
                    data={"anilist_id": "170733", "title": "Blocked Long Detail"},
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            assert len(resp.headers["location"]) < 800
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT download_status, download_info
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (_user_id(client), 170733),
                ).fetchone()

            assert row["download_status"] == "rd_provider_block"
            assert len(row["download_info"]) <= 240
            assert row["download_info"].endswith("...")
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_download_route_caps_rd_error_detail_redirect(self, client):
        """Generic RD errors are bounded before redirect when provider detail is included."""
        try:
            _signup_and_login(client)
            huge_detail = "Real-Debrid rejected all candidates; last RD response: " + ("Y" * 1200)
            download_mock = AsyncMock(
                return_value={
                    "status": "rd_error",
                    "detail": huge_detail,
                }
            )

            with patch("omakase.plus.automation.search_and_download", new=download_mock):
                resp = client.post(
                    "/plus/dashboard/download",
                    data={"anilist_id": "170734", "title": "Mixed RD Failure"},
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            assert len(resp.headers["location"]) < 800
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT download_status
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (_user_id(client), 170734),
                ).fetchone()
            assert row["download_status"] == "error"
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_download_route_overwrites_stale_rd_info_on_retry(self, client):
        """Retry results should replace old provider-block details and RD ids."""
        try:
            _signup_and_login(client)
            user_id = _user_id(client)
            with _connect_client_db(client) as conn:
                conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status, download_status,
                        download_info, rd_torrent_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        170735,
                        "Retry Target",
                        "PLANNING",
                        "rd_provider_block",
                        "old provider-block detail",
                        "old-rd-id",
                    ),
                )
                conn.commit()

            download_mock = AsyncMock(
                return_value={
                    "status": "rd_error",
                    "detail": "Real-Debrid rejected all retry candidates",
                }
            )

            with patch("omakase.plus.automation.search_and_download", new=download_mock):
                resp = client.post(
                    "/plus/dashboard/download",
                    data={"anilist_id": "170735", "title": "Retry Target"},
                    follow_redirects=False,
                )

            assert resp.status_code == 302
            with _connect_client_db(client) as conn:
                row = conn.execute(
                    """SELECT download_status, download_info, rd_torrent_id
                       FROM anilist_plannings
                       WHERE user_id = ? AND anilist_id = ?""",
                    (user_id, 170735),
                ).fetchone()

            assert row["download_status"] == "error"
            assert row["download_info"] == "Real-Debrid rejected all retry candidates"
            assert row["rd_torrent_id"] == ""
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_plan_button_creates_planning_and_request(self, client):
        """POST /plus/dashboard/plan inserts planning row."""
        try:
            _signup_and_login(client)

            resp = client.post(
                "/plus/dashboard/plan",
                data={"anilist_id": "21", "title": "One Piece"},
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert resp.headers["location"] == "/plus/dashboard"

            # Dashboard should now show the planned entry
            resp2 = client.get("/plus/dashboard")
            html = resp2.text
            assert "One Piece" in html
            # The AniList status badge should show "Planned"
            assert "Planned" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_shows_planning_queue(self, client):
        """Multiple planned entries appear in the planning queue table."""
        try:
            _signup_and_login(client)

            # Plan two anime
            client.post(
                "/plus/dashboard/plan",
                data={"anilist_id": "1", "title": "Cowboy Bebop"},
            )
            client.post(
                "/plus/dashboard/plan",
                data={"anilist_id": "5", "title": "Samurai Champloo"},
            )

            resp = client.get("/plus/dashboard")
            html = resp.text
            assert "Cowboy Bebop" in html
            assert "Samurai Champloo" in html
            # AniList links should use the correct IDs
            assert "https://anilist.co/anime/1/" in html
            assert "https://anilist.co/anime/5/" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_shows_rd_provider_block_badge(self, client):
        """Planning queue shows provider-blocked RD batches as their own state."""
        try:
            _signup_and_login(client)
            with _connect_client_db(client) as conn:
                conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status, download_status, download_info)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        _user_id(client),
                        170732,
                        "BLEACH: Thousand-Year Blood War - The Conflict",
                        "PLANNING",
                        "rd_provider_block",
                        "Provider blocked this batch (451 infringing_file).",
                    ),
                )
                conn.commit()

            html = client.get("/plus/dashboard").text

            assert "RD blocked" in html
            assert "Provider blocked this batch (451 infringing_file)." in html
            assert "planning-queue-card" in html
            assert 'data-label="RD"' in html
            assert 'data-label="Title"' in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_shows_rd_attempts_and_retry_for_blocked_rows(self, client):
        """Blocked queue rows expose safe attempt telemetry plus a retry affordance."""
        try:
            _signup_and_login(client)
            user_id = _user_id(client)
            with _connect_client_db(client) as conn:
                planning_id = conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status, download_status, download_info)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        170732,
                        "BLEACH: Thousand-Year Blood War - The Conflict",
                        "PLANNING",
                        "rd_provider_block",
                        "Provider blocked this batch (451 infringing_file).",
                    ),
                ).lastrowid
                conn.execute(
                    """INSERT INTO download_attempts
                       (user_id, anilist_planning_id, request_id, candidate_rank,
                        total_candidates, torrent_title, torrent_hash, seeders,
                        size_display, is_batch, status, http_status, error_code, detail)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        user_id,
                        planning_id,
                        "req-ui",
                        1,
                        2,
                        "[Group] BLEACH - 451 [1080p][HEVC]",
                        "ABCDEF1234567890",
                        44,
                        "1.4 GiB",
                        0,
                        "provider_block",
                        451,
                        "infringing_file",
                        "Provider blocked this file",
                    ),
                )
                conn.commit()

            html = client.get("/plus/dashboard").text

            assert "RD attempts" in html
            assert "[Group] BLEACH - 451 [1080p][HEVC]" in html
            assert "ABCDEF1234567890" in html
            assert "451 infringing_file" in html
            assert "Provider blocked this file" in html
            assert ">Retry<" in html
            assert 'action="/plus/dashboard/download"' in html
            assert "magnet:" not in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_dashboard_hides_retry_for_requested_download(self, client):
        """Rows already handed to Real-Debrid should not show a retry button."""
        try:
            _signup_and_login(client)
            with _connect_client_db(client) as conn:
                conn.execute(
                    """INSERT INTO anilist_plannings
                       (user_id, anilist_id, title, status, download_status, download_info)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        _user_id(client),
                        21,
                        "One Piece",
                        "PLANNING",
                        "requested",
                        "One Piece batch (26 GiB, 200s)",
                    ),
                )
                conn.commit()

            html = client.get("/plus/dashboard").text

            assert "Downloading" in html
            assert ">Retry<" not in html
            assert ">Remove<" in html
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_double_plan_is_idempotent(self, client):
        """Planning the same anime twice does not create duplicate rows."""
        try:
            _signup_and_login(client)

            client.post(
                "/plus/dashboard/plan",
                data={"anilist_id": "21", "title": "One Piece"},
            )
            resp = client.post(
                "/plus/dashboard/plan",
                data={"anilist_id": "21", "title": "One Piece"},
                follow_redirects=False,
            )
            assert resp.status_code == 302

            # Dashboard should show the title exactly once
            resp2 = client.get("/plus/dashboard")
            assert resp2.text.count("One Piece") >= 1
        finally:
            os.environ.pop("OMAKASE_PLUS_INVITE", None)

    def test_plan_requires_auth(self, client):
        """POST /plus/dashboard/plan without login is rejected."""
        resp = client.post(
            "/plus/dashboard/plan",
            data={"anilist_id": "1", "title": "Test"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 401)


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
            assert "No fully verified titles yet." in home
            assert 'href="/plus/discover">Find something to watch</a>' in home
            assert 'href="/plus/library">Your anime</a>' in home
            assert 'href="/plus/add"' not in home
            assert "Continue watching" in home
            assert "Ready to watch" in home
            assert "nav-number" not in home
            assert 'list="library-title-suggestions"' in library
            assert 'name="content_type"' in library
            assert ">Series</option>" in library
            assert ">Movies</option>" in library
            assert "No titles match these filters." in library
            assert "<legend>Where should we look?</legend>" in client.get("/plus/discover").text
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
            assert "Download criteria" in defaults
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
            active_html = client.get("/plus/library").text
            completed_html = client.get("/plus/library?lifecycle=completed").text
            assert f'href="/plus/library/{monitor_id}"' not in active_html
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
            ) < dossier.text.index('<details class="title-technical-details">')
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
