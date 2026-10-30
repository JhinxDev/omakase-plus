"""Authenticated episode actions keep explicit intent and replay/CSRF boundaries."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omakase.plus import routes
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import ingest_watch_event
from tests.plus.test_episode_quality_recheck import asset
from tests.plus.test_streaming_ui import _connect, _seed_continue_title, _signup
from tests.plus.test_streaming_ui import client as client


@pytest.fixture(autouse=True)
def no_external(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_REVIEW_MODE", "true")
    monkeypatch.setattr(routes, "ensure_anime_synopsis", AsyncMock(return_value={}))


def resurfaced(client):
    _signup(client)
    monitor = _seed_continue_title(client)
    with _connect(client) as db:
        db.execute(
            "UPDATE watch_entries SET progress_episodes=1,episode_total=2 WHERE monitor_id=?",
            (monitor,),
        )
        result = ingest_watch_event(
            db,
            user_id=1,
            monitor_id=monitor,
            event=WatchEvent.complete(source="manual", event_key="ui:old:complete"),
            score_ten=9,
            observed_at="2026-10-28T20:00:00Z",
        )
        db.execute(
            "UPDATE media_monitors SET release_status='RELEASING',expected_episodes=12,aired_episodes=3 WHERE id=?",
            (monitor,),
        )
        asset(db, monitor, "A", (1, 2, 3))
        return monitor, result.entry.revision


def token(client, monitor):
    return re.search(
        r'name="csrf_token" value="([^"]+)"', client.get(f"/plus/library/{monitor}").text
    ).group(1)


def test_resurfaced_title_renders_explicit_action_and_quality_without_watch_get_writes(client):
    monitor, revision = resurfaced(client)
    with _connect(client) as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM watch_entries")]
    page = client.get(f"/plus/library/{monitor}").text
    assert f'action="/plus/library/{monitor}/watch/continue"' in page
    assert f'name="watch_revision" value="{revision}"' in page
    assert "mark episode 3 watched" in page and "720p fallback release" in page
    assert 'name="intent" value="episodes_quality"' in page
    with _connect(client) as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM watch_entries")] == before
    assert "View new episode" in client.get("/plus/home").text


def test_continuation_post_is_guarded_and_duplicate_post_is_idempotent(client):
    monitor, revision = resurfaced(client)
    data = {"csrf_token": token(client, monitor), "episode": "3", "watch_revision": str(revision)}
    for _ in range(2):
        response = client.post(
            f"/plus/library/{monitor}/watch/continue", data=data, follow_redirects=False
        )
        assert response.status_code == 302
    with _connect(client) as db:
        row = db.execute(
            "SELECT status,progress_episodes,episode_total FROM watch_entries"
        ).fetchone()
        assert row[:] == ("watching", 3, 12)
        assert (
            db.execute("SELECT count(*) FROM watch_events WHERE kind='reopened'").fetchone()[0] == 1
        )
        assert (
            db.execute(
                "SELECT score_ten FROM watch_events WHERE event_key=?", ("ui:old:complete",)
            ).fetchone()[0]
            == 9
        )


@pytest.mark.parametrize("invalid", ["csrf", "revision", "owner"])
def test_continuation_rejects_invalid_post_without_watch_write(client, invalid):
    monitor, revision = resurfaced(client)
    csrf = token(client, monitor)
    if invalid == "owner":
        with _connect(client) as db:
            db.execute(
                "INSERT INTO users(id,email,password_hash) VALUES(2,'other@fixture.invalid','h')"
            )
            db.execute("UPDATE media_monitors SET user_id=2 WHERE id=?", (monitor,))
    with _connect(client) as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM watch_entries")]
    response = client.post(
        f"/plus/library/{monitor}/watch/continue",
        data={
            "csrf_token": "invalid" if invalid == "csrf" else csrf,
            "episode": "3",
            "watch_revision": str(revision + (1 if invalid == "revision" else 0)),
        },
        follow_redirects=False,
    )
    assert response.status_code == (403 if invalid == "csrf" else 302)
    with _connect(client) as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM watch_entries")] == before


@pytest.mark.parametrize("requested", ["episodes_quality", "missing"])
def test_recheck_route_sends_only_the_requested_intent_and_actual_monitor_result(
    client, monkeypatch, requested
):
    monitor, _ = resurfaced(client)
    run = AsyncMock(
        return_value=SimpleNamespace(
            outcome="complete",
            monitor_results=(
                SimpleNamespace(
                    monitor_id=monitor,
                    outcome="upgrade_pending",
                    detail="Higher release found; replacement publication is unconfirmed.",
                ),
            ),
        )
    )
    monkeypatch.setattr(routes, "run_media_sync", run)
    response = client.post(
        f"/plus/library/{monitor}/retry",
        data={"csrf_token": token(client, monitor), "intent": requested},
        follow_redirects=False,
    )
    assert (
        response.status_code == 302
        and "publication%20is%20unconfirmed" in response.headers["location"]
    )
    assert run.await_args.kwargs["recheck"].upgrade_to_1080 == (requested == "episodes_quality")
    assert run.await_args.kwargs["monitor_ids"] == (monitor,)


def test_recheck_busy_does_not_report_false_completion(client, monkeypatch):
    monitor, _ = resurfaced(client)
    monkeypatch.setattr(
        routes,
        "run_media_sync",
        AsyncMock(return_value=SimpleNamespace(outcome="already_running", monitor_results=())),
    )
    response = client.post(
        f"/plus/library/{monitor}/retry",
        data={"csrf_token": token(client, monitor), "intent": "episodes_quality"},
        follow_redirects=False,
    )
    assert "already%20running" in response.headers["location"]
    assert "complete" not in response.headers["location"]
