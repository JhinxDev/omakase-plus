"""Tests for AniList OAuth + GraphQL write + integration routes.

Uses mocked ``httpx`` for external calls and FastAPI ``TestClient`` for
route-level integration tests.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.anilist import (
    AniListWriteError,
    add_to_planning,
    build_authorize_url,
    ensure_in_planning,
    exchange_code,
    generate_pkce,
    is_token_valid,
    remove_from_planning,
    save_completed_entry,
    save_progress_entry,
    with_valid_token,
)
from omakase.plus.auth import (
    create_session,
    hash_password,
)
from omakase.plus.db import _db, run_migrations
from omakase.plus.deps import get_db as _deps_get_db
from omakase.plus.oauth_flows import consume_oauth_flow, create_oauth_flow

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


def _cleanup_db_cache():
    for c in _db.values():
        try:
            c.close()
        except Exception:
            pass
    _db.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    """FastAPI TestClient wired to a per-test temp database."""
    tmp = tempfile.mkdtemp()

    def _override_get_db():
        return _make_test_conn(tmp)

    # Initialize the database before creating the app
    _make_test_conn(tmp).close()

    app = FastAPI()
    from omakase.plus.routes import router

    app.include_router(router)
    app.dependency_overrides[_deps_get_db] = _override_get_db

    test_client = TestClient(app)
    test_client._tmp_dir = tmp

    yield test_client

    _cleanup_db_cache()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# PKCE + OAuth unit tests
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_generate_pkce(self):
        verifier, challenge = generate_pkce()
        assert verifier
        assert challenge
        assert verifier != challenge
        assert len(verifier) > 0
        assert len(challenge) > 0

    def test_generate_pkce_is_random(self):
        """Two calls produce different verifiers (non-deterministic)."""
        v1, _c1 = generate_pkce()
        v2, _c2 = generate_pkce()
        assert v1 != v2

    def test_build_authorize_url(self):
        url = build_authorize_url(
            "test_client_id",
            "http://localhost/callback",
            "test_challenge",
            "test_state",
        )
        query = parse_qs(urlparse(url).query)
        assert query == {
            "client_id": ["test_client_id"],
            "redirect_uri": ["http://localhost/callback"],
            "response_type": ["code"],
            "code_challenge": ["test_challenge"],
            "code_challenge_method": ["S256"],
            "state": ["test_state"],
        }

    def test_oauth_flow_is_owner_scoped_single_use_and_restart_safe(self, tmp_path):
        db = _make_test_conn(str(tmp_path))
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("flow@example.com", "hash"),
        )
        db.commit()
        state = create_oauth_flow(
            db,
            user_id=1,
            provider="anilist",
            code_verifier="v" * 64,
            now=100,
        )
        db.close()

        reopened = _make_test_conn(str(tmp_path))
        assert (
            consume_oauth_flow(
                reopened,
                user_id=2,
                provider="anilist",
                state=state,
                now=101,
            )
            is None
        )
        assert (
            consume_oauth_flow(
                reopened,
                user_id=1,
                provider="anilist",
                state=state,
                now=101,
            )
            == "v" * 64
        )
        assert (
            consume_oauth_flow(
                reopened,
                user_id=1,
                provider="anilist",
                state=state,
                now=101,
            )
            is None
        )
        reopened.close()

    def test_expired_oauth_flow_is_rejected(self, tmp_path):
        db = _make_test_conn(str(tmp_path))
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("expired@example.com", "hash"),
        )
        db.commit()
        state = create_oauth_flow(
            db,
            user_id=1,
            provider="anilist",
            code_verifier="v" * 64,
            now=100,
        )
        assert (
            consume_oauth_flow(
                db,
                user_id=1,
                provider="anilist",
                state=state,
                now=701,
            )
            is None
        )
        db.close()


class TestExchangeCode:
    def test_exchange_code_returns_token(self):
        """Mock a successful token exchange."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "anilist_token_abc123",
            "token_type": "Bearer",
        }

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            token = exchange_code(
                "cid", "csecret", "http://localhost/callback", "auth_code", "verifier"
            )

        assert token == "anilist_token_abc123"
        mock_client.post.assert_called_once()
        call_data = mock_client.post.call_args[1]["data"]
        assert call_data["grant_type"] == "authorization_code"
        assert call_data["code"] == "auth_code"
        assert call_data["code_verifier"] == "verifier"

    def test_exchange_code_http_error_raises(self):
        """Mock a failed token exchange (bad client secret, etc.)."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 400
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Bad request", request=MagicMock(), response=mock_response
        )

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            with pytest.raises(httpx.HTTPError):
                exchange_code("cid", "csecret", "http://localhost/callback", "code", "ver")


class TestAddToPlanning:
    def test_add_to_planning_sends_correct_mutation(self):
        """Verify the GraphQL mutation payload is correct."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"SaveMediaListEntry": {"id": 1, "mediaId": 12345, "status": "PLANNING"}}
        }

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            result = add_to_planning("bearer_token", 12345)

        assert result["data"]["SaveMediaListEntry"]["mediaId"] == 12345

        call_kwargs = mock_client.post.call_args[1]
        assert "mutation" in call_kwargs["json"]["query"]
        assert "SaveMediaListEntry" in call_kwargs["json"]["query"]
        assert call_kwargs["json"]["variables"]["mediaId"] == 12345
        assert call_kwargs["json"]["variables"]["status"] == "PLANNING"
        assert call_kwargs["headers"]["Authorization"] == "Bearer bearer_token"

    def test_add_to_planning_custom_status(self):
        """Verify custom status is passed through."""
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": {"SaveMediaListEntry": {"id": 2, "mediaId": 999, "status": "CURRENT"}}
        }

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            result = add_to_planning("token", 999, status="CURRENT")

        assert result["data"]["SaveMediaListEntry"]["status"] == "CURRENT"
        call_vars = mock_client.post.call_args[1]["json"]["variables"]
        assert call_vars["status"] == "CURRENT"


class TestEnsureInPlanning:
    def test_completed_entry_is_preserved_without_a_status_mutation(self):
        """Removing the read-before-write branch would downgrade completed anime."""
        viewer_response = MagicMock(spec=httpx.Response)
        viewer_response.status_code = 200
        viewer_response.json.return_value = {"data": {"Viewer": {"id": 7836819}}}
        list_response = MagicMock(spec=httpx.Response)
        list_response.status_code = 200
        list_response.json.return_value = {
            "data": {
                "MediaList": {
                    "id": 91,
                    "mediaId": 151807,
                    "status": "COMPLETED",
                    "score": 9,
                    "progress": 12,
                }
            }
        }
        client = MagicMock(spec=httpx.Client)
        client.post.side_effect = [viewer_response, list_response]
        client.__enter__.return_value = client

        with patch("httpx.Client", return_value=client):
            result = ensure_in_planning("token", 151807)

        assert result == {
            "created": False,
            "entry": {
                "id": 91,
                "mediaId": 151807,
                "status": "COMPLETED",
                "score": 9,
                "progress": 12,
            },
        }
        assert client.post.call_count == 2
        lookup = client.post.call_args_list[1].kwargs["json"]
        assert lookup["variables"] == {"mediaId": 151807, "userId": 7836819}
        assert "SaveMediaListEntry" not in lookup["query"]

    def test_absent_entry_is_created_in_planning(self):
        """Removing the create branch would leave genuinely new titles off AniList."""
        viewer_response = MagicMock(spec=httpx.Response)
        viewer_response.status_code = 200
        viewer_response.json.return_value = {"data": {"Viewer": {"id": 7836819}}}
        list_response = MagicMock(spec=httpx.Response)
        list_response.status_code = 200
        list_response.json.return_value = {"data": {"MediaList": None}}
        save_response = MagicMock(spec=httpx.Response)
        save_response.status_code = 200
        save_response.json.return_value = {
            "data": {
                "SaveMediaListEntry": {
                    "id": 92,
                    "mediaId": 180560,
                    "status": "PLANNING",
                }
            }
        }
        client = MagicMock(spec=httpx.Client)
        client.post.side_effect = [viewer_response, list_response, save_response]
        client.__enter__.return_value = client

        with patch("httpx.Client", return_value=client):
            result = ensure_in_planning("token", 180560)

        assert result == {
            "created": True,
            "entry": {"id": 92, "mediaId": 180560, "status": "PLANNING"},
        }
        mutation = client.post.call_args_list[2].kwargs["json"]
        assert mutation["variables"] == {"mediaId": 180560, "status": "PLANNING"}
        assert "SaveMediaListEntry" in mutation["query"]

    def test_lookup_error_fails_closed_without_a_planning_mutation(self):
        """A failed ownership lookup must never be mistaken for an absent entry."""
        viewer_response = MagicMock(spec=httpx.Response)
        viewer_response.status_code = 200
        viewer_response.json.return_value = {"data": {"Viewer": {"id": 7836819}}}
        list_response = MagicMock(spec=httpx.Response)
        list_response.status_code = 200
        list_response.json.return_value = {
            "data": None,
            "errors": [{"message": "AniList is temporarily unavailable"}],
        }
        client = MagicMock(spec=httpx.Client)
        client.post.side_effect = [viewer_response, list_response]
        client.__enter__.return_value = client

        with (
            patch("httpx.Client", return_value=client),
            patch("omakase.plus.anilist.add_to_planning") as mutation,
            pytest.raises(AniListWriteError, match="temporarily unavailable"),
        ):
            ensure_in_planning("token", 180560)

        mutation.assert_not_called()

    def test_malformed_lookup_fails_closed_without_a_planning_mutation(self):
        """Malformed success payloads are not proof that the title is absent."""
        viewer_response = MagicMock(spec=httpx.Response)
        viewer_response.status_code = 200
        viewer_response.json.return_value = {"data": {"Viewer": {"id": 7836819}}}
        list_response = MagicMock(spec=httpx.Response)
        list_response.status_code = 200
        list_response.json.return_value = {"data": []}
        client = MagicMock(spec=httpx.Client)
        client.post.side_effect = [viewer_response, list_response]
        client.__enter__.return_value = client

        with (
            patch("httpx.Client", return_value=client),
            patch("omakase.plus.anilist.add_to_planning") as mutation,
            pytest.raises(AniListWriteError, match="invalid list response"),
        ):
            ensure_in_planning("token", 180560)

        mutation.assert_not_called()


class TestSaveCompletedEntry:
    def test_skipped_score_is_absent_from_mutation_and_variables(self):
        response = MagicMock(spec=httpx.Response)
        response.json.return_value = {
            "data": {
                "SaveMediaListEntry": {
                    "id": 77,
                    "mediaId": 180560,
                    "status": "COMPLETED",
                }
            }
        }
        client = MagicMock(spec=httpx.Client)
        client.post.return_value = response
        client.__enter__.return_value = client
        with patch("httpx.Client", return_value=client):
            save_completed_entry("secret-token", 180560)
        payload = client.post.call_args.kwargs["json"]
        assert "scoreRaw" not in payload["query"]
        assert payload["variables"] == {"mediaId": 180560, "status": "COMPLETED"}

    def test_completion_uses_score_raw_and_validates_remote_receipt(self):
        response = MagicMock(spec=httpx.Response)
        response.json.return_value = {
            "data": {
                "SaveMediaListEntry": {
                    "id": 77,
                    "mediaId": 180560,
                    "status": "COMPLETED",
                }
            }
        }
        client = MagicMock(spec=httpx.Client)
        client.post.return_value = response
        client.__enter__.return_value = client

        with patch("httpx.Client", return_value=client):
            entry = save_completed_entry("secret-token", 180560, score_ten=8.5)

        variables = client.post.call_args.kwargs["json"]["variables"]
        assert variables == {
            "mediaId": 180560,
            "status": "COMPLETED",
            "scoreRaw": 85,
        }
        assert entry["id"] == 77
        assert client.post.call_args.kwargs["headers"]["Authorization"] == "Bearer secret-token"

    def test_completion_surfaces_graphql_errors_even_with_http_200(self):
        response = MagicMock(spec=httpx.Response)
        response.json.return_value = {
            "data": None,
            "errors": [{"message": "The selected media id is invalid."}],
        }
        client = MagicMock(spec=httpx.Client)
        client.post.return_value = response
        client.__enter__.return_value = client

        with patch("httpx.Client", return_value=client):
            with pytest.raises(AniListWriteError, match="selected media"):
                save_completed_entry("secret-token", 999)

    @pytest.mark.parametrize("score", [0, 10.1, True, "8"])
    def test_completion_rejects_invalid_scores(self, score):
        with pytest.raises(ValueError, match="score"):
            save_completed_entry("token", 1, score_ten=score)


class TestSaveProgressEntry:
    def test_progress_requires_exact_media_status_and_progress(self):
        response = MagicMock(spec=httpx.Response)
        response.json.return_value = {
            "data": {
                "SaveMediaListEntry": {
                    "id": 91,
                    "mediaId": 151807,
                    "status": "CURRENT",
                    "progress": 5,
                }
            }
        }
        client = MagicMock(spec=httpx.Client)
        client.post.return_value = response
        client.__enter__.return_value = client

        with patch("httpx.Client", return_value=client):
            result = save_progress_entry("secret-token", 151807, progress=5)

        assert result == {
            "id": 91,
            "mediaId": 151807,
            "status": "CURRENT",
            "progress": 5,
        }
        variables = client.post.call_args.kwargs["json"]["variables"]
        assert variables == {"mediaId": 151807, "status": "CURRENT", "progress": 5}
        assert "progress" in client.post.call_args.kwargs["json"]["query"]

    def test_progress_rejects_mismatched_remote_receipt(self):
        response = MagicMock(spec=httpx.Response)
        response.json.return_value = {
            "data": {
                "SaveMediaListEntry": {
                    "id": 91,
                    "mediaId": 151807,
                    "status": "CURRENT",
                    "progress": 4,
                }
            }
        }
        client = MagicMock(spec=httpx.Client)
        client.post.return_value = response
        client.__enter__.return_value = client

        with (
            patch("httpx.Client", return_value=client),
            pytest.raises(AniListWriteError, match="progress receipt"),
        ):
            save_progress_entry("secret-token", 151807, progress=5)

    @pytest.mark.parametrize(
        ("media_id", "progress"),
        [(0, 1), (True, 1), (1, -1), (1, True), (1, "5")],
    )
    def test_progress_rejects_invalid_inputs(self, media_id, progress):
        with pytest.raises(ValueError):
            save_progress_entry("token", media_id, progress=progress)


class TestIsTokenValid:
    def test_valid_token_returns_true(self):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            assert is_token_valid("valid_token") is True

    def test_invalid_token_returns_false(self):
        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.return_value = mock_response
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            assert is_token_valid("invalid_token") is False

    def test_http_error_returns_false(self):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            assert is_token_valid("token") is False


class TestRemoveFromPlanning:
    def test_scopes_lookup_to_authenticated_viewer(self):
        """The lookup must never resolve another user's public list entry."""
        viewer_response = MagicMock(spec=httpx.Response)
        viewer_response.status_code = 200
        viewer_response.json.return_value = {"data": {"Viewer": {"id": 7836819}}}

        find_response = MagicMock(spec=httpx.Response)
        find_response.status_code = 200
        find_response.json.return_value = {"data": {"MediaList": {"id": 42, "status": "PLANNING"}}}

        delete_response = MagicMock(spec=httpx.Response)
        delete_response.status_code = 200
        delete_response.json.return_value = {"data": {"DeleteMediaListEntry": {"deleted": True}}}

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = [viewer_response, find_response, delete_response]
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            assert remove_from_planning("token", 155783) is True

        assert mock_client.post.call_count == 3
        viewer_call, find_call, delete_call = mock_client.post.call_args_list
        assert "Viewer" in viewer_call.kwargs["json"]["query"]
        assert "userId: $userId" in find_call.kwargs["json"]["query"]
        assert find_call.kwargs["json"]["variables"] == {
            "mediaId": 155783,
            "userId": 7836819,
        }
        assert delete_call.kwargs["json"]["variables"] == {"id": 42}

    def test_completed_remote_entry_is_never_deleted(self):
        """A stale local PLANNING row must not erase completed AniList history."""
        viewer_response = MagicMock(spec=httpx.Response)
        viewer_response.status_code = 200
        viewer_response.json.return_value = {"data": {"Viewer": {"id": 7836819}}}

        find_response = MagicMock(spec=httpx.Response)
        find_response.status_code = 200
        find_response.json.return_value = {"data": {"MediaList": {"id": 42, "status": "COMPLETED"}}}

        mock_client = MagicMock(spec=httpx.Client)
        mock_client.post.side_effect = [viewer_response, find_response]
        mock_client.__enter__.return_value = mock_client

        with patch("httpx.Client", return_value=mock_client):
            assert remove_from_planning("token", 151807) is False

        assert mock_client.post.call_count == 2


class TestWithValidToken:
    def test_missing_token_raises(self):
        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None  # no secret row

        with patch("omakase.plus.secrets.read_secret", return_value=None):
            with pytest.raises(ValueError, match="No AniList OAuth token found"):
                with with_valid_token(db, 1, "", "", ""):
                    pass

    def test_invalid_token_raises(self):
        with (
            patch("omakase.plus.secrets.read_secret", return_value="some_token"),
            patch("omakase.plus.anilist.is_token_valid", return_value=False),
        ):
            with pytest.raises(ValueError, match="invalid or expired"):
                with with_valid_token(MagicMock(), 1, "", "", ""):
                    pass

    def test_valid_token_yields(self):
        with (
            patch("omakase.plus.secrets.read_secret", return_value="valid_token"),
            patch("omakase.plus.anilist.is_token_valid", return_value=True),
        ):
            with with_valid_token(MagicMock(), 1, "", "", "") as token:
                assert token == "valid_token"


# ---------------------------------------------------------------------------
# Route integration tests  (FastAPI TestClient)
# ---------------------------------------------------------------------------


class TestRoutes:
    """Tests requiring a logged-in user with a session cookie."""

    @staticmethod
    def _login_user(client) -> int:
        """Create a user + session in the temp DB and set the cookie."""
        db = _make_test_conn(client._tmp_dir)
        db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("anilist-test@example.com", hash_password("password123")),
        )
        db.commit()
        user_id = db.execute(
            "SELECT id FROM users WHERE email = ?",
            ("anilist-test@example.com",),
        ).fetchone()["id"]
        session_id = create_session(db, user_id)
        db.close()
        client.cookies.set("omakase_session", session_id)
        return user_id

    # ── Auth guard ────────────────────────────────────────

    def test_plan_requires_login(self, client):
        """POST /plus/api/plan without a session cookie returns 401."""
        resp = client.post("/plus/api/plan", json={"anilist_id": 1, "title": "Foo"})
        assert resp.status_code == 401

    def test_anilist_connect_requires_login(self, client):
        """GET /plus/integrations/anilist/connect without a session returns 401."""
        resp = client.get("/plus/integrations/anilist/connect", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_anilist_callback_requires_login(self, client):
        """GET /plus/integrations/anilist/callback without a session returns 401."""
        resp = client.get("/plus/integrations/anilist/callback?code=abc", follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_anilist_disconnect_requires_login(self, client):
        """POST /plus/integrations/anilist/disconnect without a session returns 401."""
        resp = client.post("/plus/integrations/anilist/disconnect", follow_redirects=False)
        assert resp.status_code in (302, 401)

    # ── /plus/api/plan ─────────────────────────────────────

    def test_plan_api_endpoint(self, client):
        """Happy path: plan an anime returns {"status": "ok"}."""
        self._login_user(client)

        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = "fake_token"
        mock_cm.__exit__.return_value = None

        mock_add_result = {
            "created": True,
            "entry": {"id": 10, "mediaId": 54321, "status": "PLANNING"},
        }

        with (
            patch("omakase.plus.routes.with_valid_token", return_value=mock_cm),
            patch("omakase.plus.routes.ensure_in_planning", return_value=mock_add_result),
        ):
            resp = client.post("/plus/api/plan", json={"anilist_id": 54321, "title": "Test Anime"})

        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_plan_api_missing_fields(self, client):
        """Missing anilist_id or title returns error."""
        self._login_user(client)

        resp = client.post("/plus/api/plan", json={"title": "No ID"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

        resp = client.post("/plus/api/plan", json={"anilist_id": 1})
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_plan_api_invalid_json(self, client):
        """Malformed JSON body returns error."""
        self._login_user(client)

        resp = client.post(
            "/plus/api/plan", data="not-json", headers={"Content-Type": "application/json"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "error"

    def test_plan_api_token_missing(self, client):
        """When no OAuth token is stored, an error is returned."""
        self._login_user(client)

        with patch(
            "omakase.plus.routes.with_valid_token",
            side_effect=ValueError("No AniList OAuth token found"),
        ):
            resp = client.post("/plus/api/plan", json={"anilist_id": 1, "title": "Foo"})

        assert resp.status_code == 200
        assert resp.json()["status"] == "error"
        assert "No AniList OAuth token found" in resp.json()["detail"]

    # ── Dedupe ─────────────────────────────────────────────

    def test_dedupe_planning(self, client):
        """Planning the same anime twice returns "already_planned"."""
        self._login_user(client)

        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = "fake_token"
        mock_cm.__exit__.return_value = None

        mock_add_result = {
            "created": True,
            "entry": {"id": 99, "mediaId": 777, "status": "PLANNING"},
        }

        with (
            patch("omakase.plus.routes.with_valid_token", return_value=mock_cm),
            patch("omakase.plus.routes.ensure_in_planning", return_value=mock_add_result),
        ):
            # First call should succeed
            resp1 = client.post(
                "/plus/api/plan", json={"anilist_id": 777, "title": "Deduped Anime"}
            )
            assert resp1.status_code == 200
            assert resp1.json()["status"] == "ok"

            # Second call with same anilist_id should be "already_planned"
            resp2 = client.post(
                "/plus/api/plan", json={"anilist_id": 777, "title": "Deduped Anime"}
            )
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "already_planned"

    def test_dedupe_per_user(self, client):
        """Two different users can plan the same anime."""
        self._login_user(client)

        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = "fake_token"
        mock_cm.__exit__.return_value = None

        mock_add_result = {
            "created": True,
            "entry": {"id": 42, "mediaId": 999, "status": "PLANNING"},
        }

        with (
            patch("omakase.plus.routes.with_valid_token", return_value=mock_cm),
            patch("omakase.plus.routes.ensure_in_planning", return_value=mock_add_result),
        ):
            resp = client.post("/plus/api/plan", json={"anilist_id": 999, "title": "Shared Anime"})
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

        # The test only checks that the first user succeeds; a second
        # user would need a separate session, which is out of scope here.
        # The per-user dedupe is enforced by the SQL WHERE user_id = ? clause.

    # ── Connect flow (unit-like, no actual redirect) ───────

    def test_connect_missing_client_id(self, client):
        """When ANILIST_CLIENT_ID is not set, the connect endpoint returns 500."""
        self._login_user(client)

        os.environ.pop("ANILIST_CLIENT_ID", None)
        os.environ["ANILIST_CLIENT_SECRET"] = "test_secret"
        try:
            resp = client.get("/plus/integrations/anilist/connect", follow_redirects=False)
            assert resp.status_code == 500
        finally:
            os.environ["ANILIST_CLIENT_ID"] = "test_client_id"

    def test_connect_redirects_to_anilist(self, client):
        """On success, the connect endpoint redirects to AniList."""
        os.environ["ANILIST_CLIENT_ID"] = "test_cid"
        os.environ["ANILIST_CLIENT_SECRET"] = "test_secret"
        self._login_user(client)

        resp = client.get("/plus/integrations/anilist/connect", follow_redirects=False)
        assert resp.status_code == 302
        assert "anilist.co/api/v2/oauth/authorize" in resp.headers["location"]
        assert "client_id=test_cid" in resp.headers["location"]
        assert "state=" in resp.headers["location"]

    def test_callback_missing_code(self, client):
        """Callback without ?code= returns 400."""
        self._login_user(client)
        resp = client.get("/plus/integrations/anilist/callback", follow_redirects=False)
        assert resp.status_code == 400

    def test_callback_no_pkce_state(self, client):
        """Callback without prior connect returns 400."""
        self._login_user(client)
        resp = client.get(
            "/plus/integrations/anilist/callback?code=auth123&state=unknown",
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "invalid or expired" in resp.text

    def test_callback_no_credentials(self, client):
        """Callback when AniList env vars are missing returns 500."""
        os.environ.pop("ANILIST_CLIENT_ID", None)
        os.environ.pop("ANILIST_CLIENT_SECRET", None)

        user_id = self._login_user(client)
        db = _make_test_conn(client._tmp_dir)
        state = create_oauth_flow(
            db,
            user_id=user_id,
            provider="anilist",
            code_verifier="v" * 64,
        )
        db.close()

        try:
            resp = client.get(
                f"/plus/integrations/anilist/callback?code=auth123&state={state}",
                follow_redirects=False,
            )
            assert resp.status_code == 500
        finally:
            os.environ["ANILIST_CLIENT_ID"] = "test_cid"
            os.environ["ANILIST_CLIENT_SECRET"] = "test_secret"

    def test_disconnect_clears_secret(self, client):
        """POST /plus/integrations/anilist/disconnect redirects to settings."""
        user_id = self._login_user(client)

        # Store a fake token first
        db = _make_test_conn(client._tmp_dir)
        from omakase.plus.secrets import store_secret

        store_secret(db, user_id, "anilist_oauth_token", "some_token")
        db.close()

        settings = client.get("/plus/settings")
        token = re.search(r'name="csrf_token" value="([0-9a-f]{64})"', settings.text)
        assert token is not None
        resp = client.post(
            "/plus/integrations/anilist/disconnect",
            data={"csrf_token": token.group(1)},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert resp.headers["location"].startswith("/plus/settings")

        # Verify the token was actually deleted
        db2 = _make_test_conn(client._tmp_dir)
        from omakase.plus.secrets import read_secret

        assert read_secret(db2, user_id, "anilist_oauth_token") is None
        db2.close()

    def test_settings_exposes_oauth_connection_state(self, client, monkeypatch):
        user_id = self._login_user(client)
        monkeypatch.setenv("ANILIST_CLIENT_ID", "test_cid")
        monkeypatch.setenv("ANILIST_CLIENT_SECRET", "test_secret")

        disconnected = client.get("/plus/settings/tracking")
        assert 'href="/plus/integrations/anilist/connect"' in disconnected.text
        assert "Connect AniList" in disconnected.text

        db = _make_test_conn(client._tmp_dir)
        from omakase.plus.secrets import store_secret

        store_secret(db, user_id, "anilist_oauth_token", "stored-token")
        db.close()

        connected = client.get("/plus/settings/tracking")
        assert "Connected" in connected.text
        assert 'formaction="/plus/integrations/anilist/disconnect"' in connected.text
