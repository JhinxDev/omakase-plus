"""Signed, confidence-gated Apple TV watch ingestion."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.auth import hash_password
from omakase.plus.db import _db, run_migrations
from omakase.plus.deps import get_db as _deps_get_db
from omakase.plus.watch.apple_tv import create_or_rotate_integration


def _make_test_conn(directory: str) -> sqlite3.Connection:
    connection = sqlite3.connect(
        os.path.join(directory, "omakase-plus.db"), check_same_thread=False
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)
    return connection


@pytest.fixture
def client(tmp_path, monkeypatch):
    directory = str(tmp_path)

    def override_db():
        return _make_test_conn(directory)

    app = FastAPI()
    from omakase.plus.routes import router

    app.include_router(router)
    app.dependency_overrides[_deps_get_db] = override_db
    monkeypatch.setenv("OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED", "true")
    test_client = TestClient(app)
    test_client.db_path = os.path.join(directory, "omakase-plus.db")
    try:
        yield test_client
    finally:
        for connection in _db.values():
            connection.close()
        _db.clear()


def _seed_owner(
    client: TestClient,
    *,
    email: str = "owner@example.com",
    title: str = "Solo Leveling",
    anilist_id: int = 151807,
) -> tuple[int, int]:
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        user_id = int(
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                (email, hash_password("password123")),
            ).lastrowid
        )
        planning_id = int(
            db.execute(
                """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
                   VALUES (?, ?, ?, 'PLANNING')""",
                (user_id, anilist_id, title),
            ).lastrowid
        )
        monitor_id = int(
            db.execute(
                """INSERT INTO media_monitors
                   (user_id, anilist_planning_id, anilist_id, title, search_titles_json,
                    season_number, expected_episodes)
                   VALUES (?, ?, ?, ?, ?, 1, 12)""",
                (user_id, planning_id, anilist_id, title, json.dumps([title])),
            ).lastrowid
        )
        db.commit()
    return user_id, monitor_id


def _integration(client: TestClient, user_id: int) -> tuple[str, str]:
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        created = create_or_rotate_integration(db, user_id=user_id)
    return created.key_id, created.secret


def _observation(**overrides) -> dict[str, object]:
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body: dict[str, object] = {
        "event_id": "living-room:20260827T120000Z:e7",
        "observed_at": observed_at,
        "device_id": "living-room",
        "app_name": "Crunchyroll",
        "series_title": "Solo Leveling",
        "season": 1,
        "episode": 7,
        "position_seconds": 1375,
        "duration_seconds": 1500,
    }
    body.update(overrides)
    return body


def _post_signed(
    client: TestClient,
    *,
    key_id: str,
    secret: str,
    body: dict[str, object],
    timestamp: int | None = None,
    signature: str | None = None,
):
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    resolved_timestamp = int(time.time()) if timestamp is None else timestamp
    digest = (
        signature
        or hmac.new(
            secret.encode(), f"{resolved_timestamp}.".encode() + raw, hashlib.sha256
        ).hexdigest()
    )
    return client.post(
        "/plus/api/watch/apple-tv",
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Omakase-Key-Id": key_id,
            "X-Omakase-Timestamp": str(resolved_timestamp),
            "X-Omakase-Signature": f"sha256={digest}",
        },
    )


def test_exact_crunchyroll_episode_above_threshold_is_accepted(client) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)

    response = _post_signed(client, key_id=key_id, secret=secret, body=_observation())

    assert response.status_code == 202
    assert response.json()["disposition"] == "accepted"
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        row = db.execute(
            "SELECT progress_episodes, status, last_source FROM watch_entries"
        ).fetchone()
    assert dict(row) == {
        "progress_episodes": 7,
        "status": "watching",
        "last_source": "apple_tv",
    }


def test_signed_event_replay_is_idempotent(client) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)
    body = _observation()

    first = _post_signed(client, key_id=key_id, secret=secret, body=body)
    second = _post_signed(client, key_id=key_id, secret=secret, body=body)

    assert first.json()["disposition"] == "accepted"
    assert second.json() == {
        "disposition": "ignored",
        "reason": "duplicate_event",
        "progress_episodes": 7,
        "confirmation_id": None,
    }
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 1


@pytest.mark.parametrize(
    "case",
    ["stale", "invalid_signature", "disabled_key"],
)
def test_signature_and_key_fail_closed(client, case: str) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)
    if case == "disabled_key":
        with _make_test_conn(os.path.dirname(client.db_path)) as db:
            db.execute("UPDATE watch_integrations SET enabled = 0 WHERE key_id = ?", (key_id,))
            db.commit()

    response = _post_signed(
        client,
        key_id=key_id,
        secret=secret,
        body=_observation(),
        timestamp=int(time.time()) - 301 if case == "stale" else None,
        signature="0" * 64 if case == "invalid_signature" else None,
    )

    assert response.status_code == 401


def test_disabled_server_integration_returns_service_unavailable(client, monkeypatch) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)
    monkeypatch.setenv("OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED", "false")

    response = _post_signed(client, key_id=key_id, secret=secret, body=_observation())

    assert response.status_code == 503


def test_non_crunchyroll_app_is_ignored_without_event(client) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)

    response = _post_signed(
        client,
        key_id=key_id,
        secret=secret,
        body=_observation(app_name="Netflix"),
    )

    assert response.status_code == 202
    assert response.json()["reason"] == "non_crunchyroll_app"
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0


def test_stale_observation_is_rejected_even_with_fresh_signature(client) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)

    response = _post_signed(
        client,
        key_id=key_id,
        secret=secret,
        body=_observation(observed_at="2026-08-26T12:00:00Z"),
    )

    assert response.status_code == 400
    assert "fresh" in response.json()["detail"].lower()


def test_missing_episode_creates_one_tap_confirmation(client) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)
    body = _observation()
    body.pop("episode")

    response = _post_signed(client, key_id=key_id, secret=secret, body=body)

    assert response.status_code == 202
    assert response.json()["disposition"] == "confirmation_required"
    assert response.json()["confirmation_id"] is not None
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        assert db.execute("SELECT progress_episodes FROM watch_entries").fetchone()[0] == 0


def test_below_threshold_is_recorded_without_advancing(client) -> None:
    user_id, _ = _seed_owner(client)
    key_id, secret = _integration(client, user_id)

    response = _post_signed(
        client,
        key_id=key_id,
        secret=secret,
        body=_observation(position_seconds=1200, duration_seconds=1500),
    )

    assert response.status_code == 202
    assert response.json()["reason"] == "playback_below_completion_threshold"
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        assert db.execute("SELECT progress_episodes FROM watch_entries").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 1


def test_key_cannot_match_another_owners_monitor(client) -> None:
    first_user_id, _ = _seed_owner(
        client, email="first@example.com", title="First Show", anilist_id=1
    )
    _seed_owner(client, email="second@example.com", title="Solo Leveling", anilist_id=2)
    key_id, secret = _integration(client, first_user_id)

    response = _post_signed(client, key_id=key_id, secret=secret, body=_observation())

    assert response.status_code == 202
    assert response.json()["reason"] == "title_not_matched"
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0


def test_duplicate_owner_title_match_fails_closed(client) -> None:
    user_id, _ = _seed_owner(client)
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        planning_id = int(
            db.execute(
                """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
                   VALUES (?, 151808, 'Solo Leveling', 'PLANNING')""",
                (user_id,),
            ).lastrowid
        )
        db.execute(
            """INSERT INTO media_monitors
               (user_id, anilist_planning_id, anilist_id, title, search_titles_json,
                season_number, expected_episodes)
               VALUES (?, ?, 151808, 'Solo Leveling', '["Solo Leveling"]', 1, 12)""",
            (user_id, planning_id),
        )
        db.commit()
    key_id, secret = _integration(client, user_id)

    response = _post_signed(client, key_id=key_id, secret=secret, body=_observation())

    assert response.status_code == 202
    assert response.json()["reason"] == "title_match_ambiguous"
    with _make_test_conn(os.path.dirname(client.db_path)) as db:
        assert db.execute("SELECT count(*) FROM watch_events").fetchone()[0] == 0


def test_oversized_body_is_rejected_before_json_or_signature_work(client) -> None:
    response = client.post(
        "/plus/api/watch/apple-tv",
        content=b"{" + (b"x" * (16 * 1024)) + b"}",
        headers={
            "Content-Type": "application/json",
            "X-Omakase-Key-Id": "k" * 24,
            "X-Omakase-Timestamp": str(int(time.time())),
            "X-Omakase-Signature": f"sha256={'0' * 64}",
        },
    )

    assert response.status_code == 413
