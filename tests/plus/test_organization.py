from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db
from omakase.plus.manifest import store_curator_receipt
from omakase.plus.media_store import upsert_asset, upsert_monitor
from omakase.plus.organization import build_plex_organization
from omakase.plus.routes import _manifest_rate_limiter, router
from tests.plus.test_product_views import _curator_receipt


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    run_migrations(connection)
    yield connection
    connection.close()


def _seed(
    db,
    *,
    identity=42,
    season=1,
    email="private-owner@example.com",
    coverage=(1, 2, 3),
    verified=True,
    status="downloaded",
    aired=3,
    expected=3,
):
    user = db.execute(
        "INSERT INTO users(email, password_hash) VALUES (?, 'private-hash')", (email,)
    ).lastrowid
    monitor = upsert_monitor(
        db,
        user_id=user,
        anilist_id=identity,
        title=f"Fixture Season {season}",
        search_titles=("Fixture",),
        genres=("Fantasy",),
        season_number=season,
        release_status="RELEASING",
        expected_episodes=expected,
        aired_episodes=aired,
    )
    asset = upsert_asset(
        db,
        monitor_id=monitor,
        torrent_hash=f"{monitor:040x}",
        source_title="Private provider source",
        source_folder="private-provider-folder",
        storage_backend="zurg",
        storage_locator="private-provider-locator",
        quality="1080p",
        ownership="monitor_owned",
        provenance="adopted",
        planned_coverage=coverage,
        usable_coverage=coverage,
        inventory_fingerprint="a" * 64 if verified else "",
        inventory_verified_at=_now() if verified else None,
        status=status,
        progress=100,
    )
    return user, monitor, asset


def _visible(db, *, identity=42, season=1, count=3, when=None):
    receipt = _curator_receipt(anilist_id=identity, season=season, episodes=count)
    if when is None:
        from datetime import timedelta

        latest = db.execute(
            "SELECT completed_at FROM media_curator_receipt_latest WHERE id=1"
        ).fetchone()
        candidate = datetime.now(timezone.utc)
        if latest is not None:
            previous = datetime.strptime(latest["completed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            candidate = max(candidate, previous + timedelta(seconds=1))
        when = candidate.strftime("%Y-%m-%dT%H:%M:%SZ")
    receipt.update(
        run_id="organization-" + when.replace(":", "").replace("-", ""),
        started_at=when,
        completed_at=when,
        manifest_generated_at=when,
    )
    db.commit()
    store_curator_receipt(db, receipt, received_at=when)


def _watch(db, user, monitor, *, identity=42, season=1, progress=1, status="watching"):
    db.execute(
        "INSERT INTO watch_entries(user_id,monitor_id,anilist_id,season_number,status,progress_episodes,episode_total,completed_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            user,
            monitor,
            identity,
            season,
            status,
            progress,
            3,
            _now() if status == "completed" else None,
        ),
    )


def _shelves(db, identity=42):
    return next(
        item["shelves"]
        for item in build_plex_organization(db)["titles"]
        if item["anilist_id"] == identity
    )


def test_ready_requires_current_verified_coverage_and_current_plex_counts(db):
    _, _, asset = _seed(db)
    assert _shelves(db) == []
    _visible(db, count=2)
    assert _shelves(db) == []
    _visible(db)
    assert _shelves(db) == ["Ready"]
    db.execute("UPDATE media_assets SET inventory_verified_at=NULL WHERE id=?", (asset,))
    assert _shelves(db) == []
    db.execute(
        "UPDATE media_assets SET inventory_verified_at=?, status='quarantined' WHERE id=?",
        (_now(), asset),
    )
    assert _shelves(db) == []


def test_equal_file_count_with_missing_required_episode_is_not_ready(db):
    _seed(db, coverage=(2, 3, 4), aired=3, expected=4)
    _visible(db)
    assert _shelves(db) == []


def test_watching_requires_progress_and_some_verified_visible_media(db):
    user, monitor, asset = _seed(db, coverage=(1,))
    _visible(db, count=1)
    assert _shelves(db) == []
    _watch(db, user, monitor)
    assert _shelves(db) == ["Watching"]
    _visible(db, count=0)
    assert _shelves(db) == []
    _visible(db, count=1)
    db.execute("UPDATE media_assets SET inventory_verified_at=NULL WHERE id=?", (asset,))
    assert _shelves(db) == []


def test_completed_titles_remain_in_clearable_contract_without_viewing_shelves(db):
    user, monitor, _ = _seed(db)
    _visible(db)
    _watch(db, user, monitor, progress=3, status="completed")
    assert _shelves(db) == []
    assert len(build_plex_organization(db)["titles"]) == 1


def test_household_users_aggregate_both_shelves_without_exposing_people(db):
    user, monitor, _ = _seed(db, email="first-private@example.com")
    _seed(db, email="second-private@example.com")
    _visible(db)
    _watch(db, user, monitor)
    payload = build_plex_organization(db)
    assert payload["titles"][0]["shelves"] == ["Ready", "Watching"]
    assert len(payload["titles"]) == 1
    encoded = json.dumps(payload)
    for secret in (
        "first-private",
        "second-private",
        "private-hash",
        "private-provider",
        "user_id",
        "monitor_id",
        "progress_episodes",
    ):
        assert secret not in encoded


def test_plex_counts_from_another_season_cannot_make_this_title_ready(db):
    _seed(db, season=2)
    _visible(db, season=1)
    assert _shelves(db) == []


@pytest.mark.parametrize("progress", [0, 1])
def test_stale_plex_receipt_does_not_become_fresh_by_exporting_new_organization(db, progress):
    from datetime import timedelta

    user, monitor, _ = _seed(db)
    if progress:
        _watch(db, user, monitor, progress=progress)
    old = (datetime.now(timezone.utc) - timedelta(minutes=36)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _visible(db, when=old)
    assert _shelves(db) == []


def test_ready_shelf_survives_one_delayed_fifteen_minute_curator_cycle(db):
    from datetime import timedelta

    _seed(db)
    observed = (datetime.now(timezone.utc) - timedelta(minutes=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _visible(db, when=observed)
    assert _shelves(db) == ["Ready"]


def test_machine_endpoint_requires_bearer_auth_and_returns_private_redacted_contract(
    db, monkeypatch
):
    _seed(db)
    _visible(db)
    monkeypatch.setenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "organization-secret")
    _manifest_rate_limiter.clear()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        assert client.get("/plus/api/plex-organization").status_code == 401
        assert (
            client.get(
                "/plus/api/plex-organization", headers={"Authorization": "Bearer incorrect"}
            ).status_code
            == 401
        )
        response = client.get(
            "/plus/api/plex-organization", headers={"Authorization": "Bearer organization-secret"}
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["schema_version"] == "plex-organization-v1"
    assert response.json()["titles"][0]["shelves"] == ["Ready"]
    assert "organization-secret" not in response.text
    assert "private-owner" not in response.text
    assert "private-provider" not in response.text


def test_ready_uses_aired_episodes_without_demanding_future_releases(db):
    _seed(db, coverage=(1, 2, 3), aired=3, expected=4)
    _visible(db, count=3)
    assert _shelves(db) == ["Ready"]
