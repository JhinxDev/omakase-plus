from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omakase.plus.db import run_migrations
from omakase.plus.deps import get_db as dependency_get_db
from omakase.plus.manifest import (
    MAX_RECEIPT_BYTES,
    ManifestContractError,
    build_library_manifest,
    validate_curator_receipt,
    validate_library_manifest,
)
from omakase.plus.media_store import upsert_asset, upsert_monitor
from omakase.plus.routes import (
    _manifest_rate_limiter,
    _receipt_rate_limiter,
    router,
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    run_migrations(conn)
    return conn


@pytest.fixture(autouse=True)
def _machine_environment(monkeypatch):
    monkeypatch.setenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "machine-secret")
    _manifest_rate_limiter.clear()
    _receipt_rate_limiter.clear()


@pytest.fixture
def client():
    temp_dir = tempfile.mkdtemp()
    db_path = os.path.join(temp_dir, "omakase-plus.db")

    def override_db():
        return _connect(db_path)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[dependency_get_db] = override_db
    test_client = TestClient(app)
    test_client.db_path = db_path
    yield test_client
    shutil.rmtree(temp_dir, ignore_errors=True)


def _headers(token: str = "machine-secret") -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_private_health_exposes_secret_free_revision_receipt(client, monkeypatch):
    monkeypatch.setenv("OMAKASE_REVISION", "a" * 40)

    response = client.get("/plus/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "revision": "a" * 40,
        "manifest_schema": "manifest-v2",
    }
    assert response.headers["cache-control"] == "private, no-store"
    assert "machine-secret" not in response.text


def _seed_manifest_asset(client: TestClient, *, duplicate_user: bool = False) -> None:
    with _connect(client.db_path) as db:
        users = [
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("manifest@example.com", "hash"),
            ).lastrowid
        ]
        if duplicate_user:
            users.append(
                db.execute(
                    "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                    ("other@example.com", "hash"),
                ).lastrowid
            )
        for user_id in users:
            monitor_id = upsert_monitor(
                db,
                user_id=int(user_id),
                anilist_id=104276,
                title="My Hero Academia",
                search_titles=("Boku no Hero Academia", "My Hero Academia"),
                genres=("Action", "Adventure"),
                format="TV",
                release_status="FINISHED",
                season_number=4,
                expected_episodes=25,
                aired_episodes=25,
            )
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash="0123456789abcdef0123456789abcdef01234567",
                source_title="My Hero Academia S4 64-66 720p Dual Audio",
                source_folder="My Hero Academia Season 4",
                quality="720p",
                seeders=8,
                ownership="monitor_owned",
                provenance="adopted",
                downloaded_once=True,
                planned_coverage=(1, 2, 3),
                usable_coverage=(1, 2, 3),
                numbering_override="mha-s4-absolute-v1",
                inventory_fingerprint="1" * 64,
                inventory_verified_at="2026-07-15T18:20:00Z",
                status="downloaded",
                progress=100,
            )
        db.commit()


def _receipt(
    *,
    run_id: str = "curator-20260715T182500Z-7f31a20c",
    completed_at: str = "2026-07-15T18:25:08Z",
    mode: str = "live",
    outcome: str = "success",
    topology_converged: bool = True,
) -> dict:
    counts = {
        "Anime": 412,
        "Anime Movies": 38,
        "TV Shows": 97,
        "Movies": 221,
    }
    return {
        "schema_version": "curator-receipt-v2",
        "run_id": run_id,
        "started_at": "2026-07-15T18:25:00Z",
        "completed_at": completed_at,
        "outcome": outcome,
        "mode": mode,
        "topology_converged": topology_converged,
        "manifest_generated_at": "2026-07-15T18:22:11Z",
        "observed_sections": [
            {"key": index, "title": title, "count": count}
            for index, (title, count) in enumerate(counts.items(), start=1)
        ],
        "converged_section_counts": counts if topology_converged and mode == "live" else None,
        "title_coverage": [
            {
                "anilist_id": 104276,
                "section": "Anime",
                "season_number": 4,
                "usable_count": 3,
                "plex_visible_count": 3,
            }
        ],
        "blocked_assets": [],
        "errors": [],
    }


def _plex_snapshot() -> dict:
    return {
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
            }
        ],
    }


def test_manifest_machine_auth_unset_missing_malformed_and_invalid(client, monkeypatch):
    monkeypatch.delenv("OMAKASE_LIBRARY_MANIFEST_TOKEN")
    assert client.get("/plus/api/library-manifest").status_code == 503

    monkeypatch.setenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "machine-secret")
    for headers in ({}, {"Authorization": "Basic abc"}, _headers("wrong")):
        response = client.get("/plus/api/library-manifest", headers=headers)
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"


def test_receipt_uses_the_same_machine_auth_contract(client, monkeypatch):
    monkeypatch.delenv("OMAKASE_LIBRARY_MANIFEST_TOKEN")
    assert client.post("/plus/api/curator-receipts", json=_receipt()).status_code == 503

    monkeypatch.setenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "machine-secret")
    for headers in ({}, {"Authorization": "Bearer"}, _headers("wrong")):
        response = client.post("/plus/api/curator-receipts", headers=headers, json=_receipt())
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"


def test_plex_snapshot_endpoint_stores_real_inventory_and_updates_progress(client):
    with _connect(client.db_path) as db:
        user_id = int(
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES ('plex@example.com', 'hash')"
            ).lastrowid
        )
        monitor_id = upsert_monitor(
            db,
            user_id=user_id,
            anilist_id=166613,
            title="Hell's Paradise Season 2",
            format="TV",
            release_status="FINISHED",
            season_number=2,
            expected_episodes=12,
            aired_episodes=12,
        )
        db.commit()

    assert client.post("/plus/api/plex-library-snapshot", json=_plex_snapshot()).status_code == 401
    response = client.post(
        "/plus/api/plex-library-snapshot",
        headers=_headers(),
        json=_plex_snapshot(),
    )

    assert response.status_code == 200
    assert response.json() == {
        "stored": True,
        "item_count": 1,
        "title_count": 1,
        "matched_monitors": 1,
        "progress_updates": 1,
    }
    with _connect(client.db_path) as db:
        progress = db.execute(
            "SELECT progress_episodes, last_source FROM watch_entries WHERE monitor_id = ?",
            (monitor_id,),
        ).fetchone()
        assert dict(progress) == {"progress_episodes": 9, "last_source": "plex"}


def test_manifest_deduplicates_users_exposes_only_usable_and_redacts(client):
    _seed_manifest_asset(client, duplicate_user=True)
    response = client.get("/plus/api/library-manifest", headers=_headers())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    manifest = response.json()
    assert manifest["schema_version"] == "manifest-v2"
    assert len(manifest["titles"]) == 1
    title = manifest["titles"][0]
    assert title["anilist_id"] == 104276
    assert title["title_aliases"] == ["Boku no Hero Academia"]
    assert title["coverage"]["usable_episodes"] == [1, 2, 3]
    assert len(title["assets"]) == 1
    assert title["assets"][0] == {
        "torrent_hash": "0123456789ABCDEF0123456789ABCDEF01234567",
        "source_folder": "My Hero Academia Season 4",
        "storage_backend": "zurg",
        "storage_locator": "My Hero Academia Season 4",
        "source_title": "My Hero Academia S4 64-66 720p Dual Audio",
        "quality": "720p",
        "episode_coverage": [1, 2, 3],
        "numbering_override": "mha-s4-absolute-v1",
    }
    serialized = response.text.casefold()
    for forbidden in (
        "manifest@example.com",
        "other@example.com",
        "user_id",
        "rd_torrent_id",
        "magnet:",
        "api_key",
        "1" * 64,
    ):
        assert forbidden not in serialized


def test_manifest_bounds_rich_search_identity_to_ten_public_aliases(client):
    _seed_manifest_asset(client)
    aliases = ["My Hero Academia", *(f"Hero Alias {index}" for index in range(1, 13))]
    with _connect(client.db_path) as db:
        db.execute(
            "UPDATE media_monitors SET search_titles_json = ?",
            (json.dumps(aliases),),
        )
        db.commit()

    with _connect(client.db_path) as db:
        manifest = build_library_manifest(db)

    assert manifest["titles"][0]["title_aliases"] == [
        f"Hero Alias {index}" for index in range(1, 11)
    ]


def test_manifest_publishes_each_episode_from_only_one_verified_asset(client):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        monitor_id = db.execute("SELECT id FROM media_monitors").fetchone()[0]
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="F" * 40,
            source_title="My Hero Academia S4E03-E04 verified release",
            source_folder="Overlapping Release",
            quality="1080p",
            ownership="external",
            provenance="adopted",
            downloaded_once=True,
            planned_coverage=(3, 4),
            usable_coverage=(3, 4),
            inventory_fingerprint="f" * 64,
            inventory_verified_at="2026-07-15T18:21:00Z",
            status="downloaded",
            progress=100,
        )
        db.commit()
        manifest = build_library_manifest(db)

    assets = manifest["titles"][0]["assets"]
    assert [asset["episode_coverage"] for asset in assets] == [[1, 2, 3], [4]]


def test_identity_invalid_title_isolated_without_blocking_valid_manifest(client):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        user_id = db.execute(
            "INSERT INTO users (email, password_hash) VALUES (?, ?)",
            ("slime@example.com", "hash"),
        ).lastrowid
        monitor_id = upsert_monitor(
            db,
            user_id=int(user_id),
            anilist_id=182205,
            title="That Time I Got Reincarnated as a Slime Season 4",
            search_titles=(
                "Tensei Shitara Slime Datta Ken 4th Season",
                "Tensura 4",
            ),
            format="TV",
            release_status="RELEASING",
            season_number=4,
            aired_episodes=16,
        )
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="D" * 40,
            source_title="[SubsPlease] Tensura Nikki - Tensei Shitara Slime Datta Ken - 04",
            source_folder="Wrong Slime Diaries Episode",
            quality="1080p",
            ownership="external",
            provenance="adopted",
            downloaded_once=True,
            planned_coverage=(4,),
            usable_coverage=(4,),
            inventory_fingerprint="d" * 64,
            inventory_verified_at="2026-07-15T18:21:00Z",
            status="downloaded",
            progress=100,
        )
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="E" * 40,
            source_title=("[Asakura] Tensei Shitara Slime Datta Ken 4th Season - 05 [1080p]"),
            source_folder="Valid Slime Season 4 Episode",
            quality="1080p",
            ownership="external",
            provenance="adopted",
            downloaded_once=True,
            planned_coverage=(5,),
            usable_coverage=(5,),
            inventory_fingerprint="e" * 64,
            inventory_verified_at="2026-07-15T18:22:00Z",
            status="downloaded",
            progress=100,
        )
        db.commit()

        manifest = build_library_manifest(db)

    assert [title["anilist_id"] for title in manifest["titles"]] == [104276, 182205]
    slime = manifest["titles"][1]
    assert slime["coverage"]["usable_episodes"] == [5]
    assert [asset["source_folder"] for asset in slime["assets"]] == ["Valid Slime Season 4 Episode"]


def test_manifest_retains_verified_movie_versions_and_prefers_higher_quality(client):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        monitor_id = db.execute("SELECT id FROM media_monitors").fetchone()[0]
        db.execute(
            """UPDATE media_monitors
               SET format = 'MOVIE', season_number = NULL,
                   expected_episodes = 1, aired_episodes = 1
               WHERE id = ?""",
            (monitor_id,),
        )
        db.execute(
            """UPDATE media_assets
               SET planned_coverage_json = '[1]', usable_coverage_json = '[1]',
                   numbering_override = NULL,
                   source_title = 'My Hero Academia Movie 720p'
               WHERE monitor_id = ?""",
            (monitor_id,),
        )
        upsert_asset(
            db,
            monitor_id=monitor_id,
            torrent_hash="F" * 40,
            source_title="My Hero Academia Movie 2160p",
            source_folder="Fixture Movie 2160p",
            quality="2160p",
            ownership="external",
            provenance="adopted",
            downloaded_once=True,
            planned_coverage=(1,),
            usable_coverage=(1,),
            inventory_fingerprint="f" * 64,
            inventory_verified_at="2026-07-16T06:20:00Z",
            status="downloaded",
            progress=100,
        )
        db.commit()
        manifest = build_library_manifest(db)

    title = manifest["titles"][0]
    assert title["format"] == "MOVIE"
    assert title["season_number"] is None
    assert title["coverage"]["usable_episodes"] == [1]
    assert [asset["quality"] for asset in title["assets"]] == ["2160p", "720p"]
    assert [asset["episode_coverage"] for asset in title["assets"]] == [[1], [1]]

    invalid_identity = json.loads(json.dumps(manifest))
    invalid_identity["titles"][0]["season_number"] = 1
    with pytest.raises(ManifestContractError, match="movie identity"):
        validate_library_manifest(invalid_identity)


def test_manifest_uses_only_converged_receipt_for_full_visibility(client):
    _seed_manifest_asset(client)
    posted = client.post("/plus/api/curator-receipts", headers=_headers(), json=_receipt())
    assert posted.status_code == 200
    assert posted.json()["stored"] is True

    manifest = client.get("/plus/api/library-manifest", headers=_headers()).json()
    coverage = manifest["titles"][0]["coverage"]
    assert coverage["plex_visible_episodes"] == [1, 2, 3]
    assert coverage["plex_visible_count"] == 3


def test_manifest_treats_a_prior_schema_receipt_as_no_visibility_evidence(client):
    """Regression test: rolling the receipt schema forward (e.g. v1 -> v2)
    must not 500 the manifest endpoint for a row stored under the old
    schema. That failure mode itself blocked the curator from ever fetching
    a fresh manifest to publish a current-schema receipt."""
    _seed_manifest_asset(client)
    stale = _receipt()
    stale["schema_version"] = "curator-receipt-v1"
    with _connect(client.db_path) as db:
        db.execute(
            """INSERT INTO media_curator_receipt_latest
               (id, receipt_json, receipt_hash, curator_run_id, outcome,
                received_at, completed_at)
               VALUES (1, ?, ?, ?, ?, ?, ?)""",
            (
                json.dumps(stale),
                "0" * 64,
                stale["run_id"],
                stale["outcome"],
                "2026-07-15T18:25:10Z",
                stale["completed_at"],
            ),
        )
        db.commit()

    response = client.get("/plus/api/library-manifest", headers=_headers())

    assert response.status_code == 200
    coverage = response.json()["titles"][0]["coverage"]
    assert coverage["plex_visible_episodes"] == []
    assert coverage["plex_visible_count"] == 0


def test_stored_curator_receipt_advances_proof_ordered_title_removals(client):
    receipt = _receipt()
    provider_cleanup = AsyncMock(return_value=1)

    with (
        patch(
            "omakase.plus.routes.confirm_plex_removals",
            return_value=(17,),
            create=True,
        ) as confirm,
        patch(
            "omakase.plus.routes.process_provider_removals",
            new=provider_cleanup,
            create=True,
        ),
    ):
        response = client.post(
            "/plus/api/curator-receipts",
            headers=_headers(),
            json=receipt,
        )

    assert response.status_code == 200
    confirm.assert_called_once()
    assert confirm.call_args.args[1] == receipt
    provider_cleanup.assert_awaited_once()


def test_stored_curator_receipt_reopens_a_retryable_blocked_episode(client):
    """End-to-end regression test for the KAMUI S01E05 incident: a curator
    receipt reporting one retryable blocked asset must reopen that episode
    for the next search, without erasing the rest of the manifest."""
    _seed_manifest_asset(client)
    receipt = _receipt()
    receipt["blocked_assets"] = [
        {
            "anilist_id": 104276,
            "section": "Anime",
            "season_number": 4,
            "torrent_hash": "0123456789ABCDEF0123456789ABCDEF01234567",
            "affected_episodes": [3],
            "code": "MEDIA_PROBE_FAILED",
            "detail": (
                "The provider-backed media could not be read during the subtitle probe. "
                "It may be locked or temporarily unavailable."
            ),
            "retryable": True,
            "last_observed_at": "2026-07-15T18:25:08Z",
        }
    ]
    receipt["outcome"] = "degraded"
    receipt["title_coverage"][0]["plex_visible_count"] = 0

    response = client.post("/plus/api/curator-receipts", headers=_headers(), json=receipt)

    assert response.status_code == 200
    assert response.json()["stored"] is True
    with _connect(client.db_path) as db:
        asset = db.execute(
            "SELECT status, last_error FROM media_assets "
            "WHERE torrent_hash = '0123456789ABCDEF0123456789ABCDEF01234567'"
        ).fetchone()
    assert tuple(asset) == ("quarantined", "PLEX_MEDIA_PROBE_FAILED")


def test_manifest_validator_rejects_unknown_fields_and_contract_mismatches(client):
    _seed_manifest_asset(client)
    manifest = client.get("/plus/api/library-manifest", headers=_headers()).json()
    manifest["unknown"] = "not allowed"
    with pytest.raises(ManifestContractError):
        validate_library_manifest(manifest)

    del manifest["unknown"]
    manifest["titles"][0]["coverage"]["usable_count"] = 99
    with pytest.raises(ManifestContractError):
        validate_library_manifest(manifest)


def test_invalid_database_asset_fails_manifest_closed(client):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        db.execute(
            "UPDATE media_assets SET source_folder = ?",
            ("../../host/path",),
        )
        db.commit()

    response = client.get("/plus/api/library-manifest", headers=_headers())
    assert response.status_code == 500
    assert "host/path" not in response.text


def test_manifest_rejects_provider_locator_escape(client):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        db.execute(
            "UPDATE media_assets SET storage_locator = ?",
            ("../outside/episode.mkv",),
        )
        db.commit()

    response = client.get("/plus/api/library-manifest", headers=_headers())
    assert response.status_code == 500
    assert "outside/episode" not in response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("inventory_verified_at", "not-a-time"),
        ("inventory_fingerprint", "not-selected-file-evidence"),
    ],
)
def test_manifest_requires_canonical_selected_inventory_evidence(client, field, value):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        db.execute(f"UPDATE media_assets SET {field} = ?", (value,))
        db.commit()

    response = client.get("/plus/api/library-manifest", headers=_headers())
    assert response.status_code == 500
    assert value not in response.text


def test_manifest_rolling_limit_uses_retry_after(client):
    for _ in range(30):
        assert client.get("/plus/api/library-manifest", headers=_headers()).status_code == 200
    response = client.get("/plus/api/library-manifest", headers=_headers())
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_receipt_rejects_oversize_before_json_parsing(client):
    response = client.post(
        "/plus/api/curator-receipts",
        headers={**_headers(), "Content-Type": "application/json"},
        content=b"{" + b"x" * MAX_RECEIPT_BYTES,
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(extra="unknown"),
        lambda value: value["title_coverage"].append(dict(value["title_coverage"][0])),
        lambda value: value["errors"].append(
            {"code": "OVERLAY_FAILED", "anilist_id": None, "detail": "/secret/path"}
        ),
        lambda value: value.update(mode="dry-run", topology_converged=True),
    ],
)
def test_invalid_receipt_does_not_replace_latest(client, mutate):
    accepted = client.post("/plus/api/curator-receipts", headers=_headers(), json=_receipt())
    assert accepted.status_code == 200
    invalid = _receipt(run_id="curator-invalid-0001")
    mutate(invalid)

    response = client.post("/plus/api/curator-receipts", headers=_headers(), json=invalid)
    assert response.status_code == 422
    with _connect(client.db_path) as db:
        row = db.execute(
            "SELECT curator_run_id FROM media_curator_receipt_latest WHERE id = 1"
        ).fetchone()
    assert row["curator_run_id"] == "curator-20260715T182500Z-7f31a20c"


def test_receipt_is_idempotent_and_rejects_run_reuse_or_older_completion(client):
    receipt = _receipt()
    first = client.post("/plus/api/curator-receipts", headers=_headers(), json=receipt)
    again = client.post("/plus/api/curator-receipts", headers=_headers(), json=receipt)
    assert first.status_code == again.status_code == 200
    assert again.json()["idempotent"] is True

    changed = _receipt()
    changed["manifest_generated_at"] = "2026-07-15T18:22:12Z"
    assert (
        client.post("/plus/api/curator-receipts", headers=_headers(), json=changed).status_code
        == 409
    )

    older = _receipt(
        run_id="curator-older-0001",
        completed_at="2026-07-15T18:25:07Z",
    )
    assert (
        client.post("/plus/api/curator-receipts", headers=_headers(), json=older).status_code == 409
    )


def test_dry_run_and_nonconverged_receipts_are_evidence_only(client):
    dry_run = _receipt(
        run_id="curator-dry-run-0001",
        mode="dry-run",
        outcome="degraded",
        topology_converged=False,
    )
    dry_run["observed_sections"] = []
    dry_run["title_coverage"] = []
    result = client.post("/plus/api/curator-receipts", headers=_headers(), json=dry_run)
    assert result.status_code == 200
    assert result.json()["stored"] is False
    with _connect(client.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM media_curator_receipt_latest").fetchone()[0] == 0


def test_receipt_rolling_limit_is_twelve_per_minute(client):
    dry_run = _receipt(
        run_id="curator-dry-rate-0001",
        mode="dry-run",
        outcome="degraded",
        topology_converged=False,
    )
    dry_run["observed_sections"] = []
    dry_run["title_coverage"] = []
    for index in range(12):
        payload = dict(dry_run, run_id=f"curator-dry-rate-{index:04d}")
        assert (
            client.post("/plus/api/curator-receipts", headers=_headers(), json=payload).status_code
            == 200
        )
    response = client.post("/plus/api/curator-receipts", headers=_headers(), json=dry_run)
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) >= 1


def test_receipt_validator_rejects_completion_before_start():
    receipt = _receipt(completed_at="2026-07-15T18:24:59Z")
    with pytest.raises(ManifestContractError):
        validate_curator_receipt(receipt)


def test_manifest_builder_caps_serialized_output(client):
    _seed_manifest_asset(client)
    with _connect(client.db_path) as db:
        manifest = build_library_manifest(
            db,
            generated_at=(datetime.now(timezone.utc) + timedelta(seconds=0)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        )
    assert len(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode()) < 2**21
