"""Private discovery contracts: bounded requests, saved runs and honest availability."""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from omakase.plus.codex_jobs import handle_message
from omakase.plus.discovery import DiscoveryRequest, saved_preferences
from omakase.plus.media_store import upsert_monitor
from omakase.plus.routes import _jobs, _jobs_lock, _login_attempts
from omakase.types import Recommendation
from tests.plus.test_dashboard import (
    _connect_client_db,
    _csrf_token,
    _drain_job,
    _seed_run,
    _signup_and_login,
    _user_id,
)
from tests.plus.test_dashboard import client as client


def test_recommendation_details_are_owned_escaped_and_link_to_exact_intake(client):
    _signup_and_login(client)
    with _connect_client_db(client) as db:
        import json

        cursor = db.execute(
            "INSERT INTO run_history (user_id, source, model, picks, lane) VALUES (?, ?, ?, ?, ?)",
            (
                _user_id(client),
                "anilist",
                "fixture",
                json.dumps(
                    [
                        {
                            "title": "Canonical pick",
                            "media_id": 42,
                            "anilist_id": 42,
                            "source": "anilist",
                            "predicted_score": 8.4,
                            "reasoning": "Patient storytelling",
                            "best_match_from_history": "Known favorite",
                            "description": "A story <script>bad()</script>",
                            "genres": ["Drama"],
                            "episodes": 12,
                            "airing_status": "FINISHED",
                        }
                    ]
                ),
                "best_match",
            ),
        )
        run = cursor.lastrowid
        db.commit()
    response = client.get(f"/plus/discover/title/42?run={run}")
    assert response.status_code == 200
    assert "Why it made your menu" in response.text and "Patient storytelling" in response.text
    assert "&lt;script&gt;bad()&lt;/script&gt;" in response.text
    assert 'value="https://anilist.co/anime/42/"' in response.text
    assert "Not in your library" in response.text
    assert client.get(f"/plus/discover/title/99?run={run}").status_code == 404
    assert client.get("/plus/discover/title/42?run=99999").status_code == 404


def test_saved_watchlist_removal_requires_csrf_and_preserves_media_monitors(client):
    _signup_and_login(client)
    with _connect_client_db(client) as db:
        user_id = _user_id(client)
        db.execute(
            "INSERT INTO anilist_plannings (user_id, anilist_id, title, status) VALUES (?, 42, 'Saved pick', 'PLANNING')",
            (user_id,),
        )
        db.commit()
    page = client.get("/plus/library")
    assert "Saved pick" in page.text and "Saved for later" in page.text
    assert client.post("/plus/watchlist/42/remove", data={}).status_code == 403
    response = client.post(
        "/plus/watchlist/42/remove",
        data={"csrf_token": _csrf_token(client)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Saved pick" not in client.get("/plus/library").text


@pytest.fixture(autouse=True)
def reset_discovery_state():
    _login_attempts.clear()
    with _jobs_lock:
        _jobs.clear()
    yield
    os.environ.pop("OMAKASE_PLUS_INVITE", None)


@pytest.mark.parametrize(
    "changes",
    [
        {"count": 2},
        {"count": 16},
        {"count": True},
        {"count": "8"},
        {"max_episodes": -1},
        {"completed_only": "yes"},
        {"mood": "x" * 501},
        {"temperature": float("nan")},
        {"source": "arbitrary"},
        {"provider": "api"},
        {"lane": "made_up"},
        {"unknown": "field"},
    ],
)
def test_discovery_request_refuses_unbounded_or_ambiguous_input(changes):
    with pytest.raises(ValidationError):
        DiscoveryRequest.model_validate(changes)


def test_preferences_and_controls_reach_pipeline_without_changing_list_account(client):
    _signup_and_login(client)
    captured = []
    with patch(
        "omakase.plus.routes.run_pipeline", side_effect=lambda cfg: captured.append(cfg) or []
    ):
        response = client.post(
            "/plus/api/run",
            json={
                "count": 5,
                "max_episodes": 13,
                "completed_only": True,
                "mood": "A warm mystery",
                "mode": "pro",
                "lane": "hidden_gems",
                "username": "transient-user",
            },
        )
        assert response.status_code == 200
        _drain_job(client, response)
    cfg = captured[0]
    assert (cfg.max_episodes, cfg.completed_only, cfg.mood, cfg.num_recommendations) == (
        13,
        True,
        "A warm mystery",
        5,
    )
    with _connect_client_db(client) as db:
        prefs = saved_preferences(db, _user_id(client))
    assert prefs["lane"] == "hidden_gems" and prefs["max_episodes"] == 13
    assert prefs["username"] == ""
    page = client.get("/plus/discover")
    assert 'value="13" selected' in page.text
    assert 'name="completed_only" checked' in page.text
    assert "A warm mystery" in page.text


def test_completed_run_survives_leaving_before_any_poll(client):
    _signup_and_login(client)
    pick = Recommendation(
        "Real candidate",
        8.1,
        "Grounded test result",
        "A real favorite",
        anilist_id=42,
        media_id=42,
        source="anilist",
    )
    with patch("omakase.plus.routes.run_pipeline", return_value=[pick]):
        response = client.post("/plus/api/run", json={})
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with _jobs_lock:
                if _jobs[job_id]["status"] == "completed":
                    break
            time.sleep(0.01)
        with _connect_client_db(client) as db:
            assert (
                db.execute(
                    "SELECT COUNT(*) FROM run_history WHERE user_id=?", (_user_id(client),)
                ).fetchone()[0]
                == 1
            )
        assert f'data-active-job="{job_id}"' in client.get("/plus/discover").text
        status = client.get(f"/plus/api/run/status/{job_id}").json()
        assert status["status"] == "ok"
        assert status["run_id"] is not None
        client.get(f"/plus/api/run/status/{job_id}")
        with _connect_client_db(client) as db:
            assert db.execute("SELECT COUNT(*) FROM run_history").fetchone()[0] == 1


def test_duplicate_click_resumes_the_same_inference(client):
    _signup_and_login(client)
    gate = threading.Event()

    def wait_for_gate(cfg):
        assert gate.wait(5)
        return []

    with patch("omakase.plus.routes.run_pipeline", side_effect=wait_for_gate) as pipeline:
        first = client.post("/plus/api/run", json={})
        second = client.post("/plus/api/run", json={})
        assert first.json()["job_id"] == second.json()["job_id"]
        assert second.json()["resumed"] is True
        gate.set()
        _drain_job(client, first)
        assert pipeline.call_count == 1


def test_offline_codex_is_explicit_and_never_calls_paid_provider(client):
    _signup_and_login(client)
    with patch("omakase.plus.routes.run_pipeline") as pipeline:
        response = client.post("/plus/api/run", json={"provider": "codex", "model": "gpt-6-astra"})
        assert response.status_code == 503
        assert "offline" in response.json()["detail"]
        pipeline.assert_not_called()
    page = client.get("/plus/discover")
    assert '<option value="codex" disabled' in page.text


def test_online_codex_binds_user_and_accepts_only_ui_models(client):
    _signup_and_login(client)
    with _connect_client_db(client) as db:
        handle_message(db, {"op": "heartbeat", "worker_id": "test-worker"})
    assert (
        client.post("/plus/api/run", json={"provider": "codex", "model": "unsupported"}).status_code
        == 422
    )
    captured = []

    def recommend(cfg):
        from omakase.llm.codex import _REQUEST

        captured.append((cfg, _REQUEST.get()))
        return []

    with patch("omakase.plus.routes.run_pipeline", side_effect=recommend):
        response = client.post("/plus/api/run", json={"provider": "codex", "model": "gpt-6-astra"})
        _drain_job(client, response)
    assert captured[0][0].llm_type == "codex"
    assert captured[0][0].api_key is None
    assert captured[0][0].model == "gpt-6-astra"
    assert captured[0][1][0] == _user_id(client)
    assert captured[0][1][1] == os.path.dirname(client.db_path)


def test_saved_run_is_user_scoped_and_library_readiness_is_not_model_claim(client):
    _signup_and_login(client)
    run_id = _seed_run(
        client,
        [
            {
                "title": "A real title",
                "media_id": 42,
                "predicted_score": 8.2,
                "reasoning": "Model calls it ready",
                "availability": "Ready to watch",
                "source": "anilist",
            }
        ],
    )
    with _connect_client_db(client) as db:
        monitor_id = upsert_monitor(
            db,
            user_id=_user_id(client),
            anilist_id=42,
            title="A real title",
            release_status="FINISHED",
            expected_episodes=12,
            aired_episodes=12,
        )
        db.commit()
    page = client.get(f"/plus/discover?run={run_id}")
    assert page.status_code == 200
    assert f'href="/plus/library/{monitor_id}"' in page.text
    assert "0/12 verified" in page.text
    assert "availability-ready" not in page.text
    assert client.get("/plus/discover?run=999999").status_code == 404
    with _connect_client_db(client) as db:
        db.execute(
            "INSERT INTO users(email,password_hash) VALUES('other@example.invalid','unused')"
        )
        other_id = db.execute(
            "SELECT id FROM users WHERE email='other@example.invalid'"
        ).fetchone()[0]
        cursor = db.execute(
            "INSERT INTO run_history(user_id,source,model,picks,lane) VALUES(?, 'anilist', 'test', '[]','best_match')",
            (other_id,),
        )
        db.commit()
        other_run = cursor.lastrowid
    assert client.get(f"/plus/discover?run={other_run}").status_code == 404


def test_taste_editor_returns_to_discovery_and_refuses_open_redirect(client):
    _signup_and_login(client)
    csrf = _csrf_token(client)
    response = client.post(
        "/plus/dashboard/profile",
        data={"profile": "Patient storytelling", "return_to": "/plus/discover", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/plus/discover"
    assert "Patient storytelling" in client.get("/plus/discover").text
    response = client.post(
        "/plus/dashboard/profile",
        data={"profile": "Updated", "return_to": "https://example.invalid", "csrf_token": csrf},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/plus/dashboard"
    assert (
        client.post(
            "/plus/dashboard/profile", data={"profile": "x" * 12001, "csrf_token": csrf}
        ).status_code
        == 422
    )
