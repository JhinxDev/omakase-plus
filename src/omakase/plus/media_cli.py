"""Operator CLI adapters for the private media orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from omakase.plus.db import get_db
from omakase.plus.direct import resolve_direct_request
from omakase.plus.local_import import (
    LocalImportRequest,
    apply_local_import_plan,
    plan_local_import,
)
from omakase.plus.media_adoption import (
    apply_existing_movie_adoption_plan,
    build_existing_movie_adoption_plan,
)
from omakase.plus.media_probe import ExternalSubtitle
from omakase.plus.media_sync import SchedulerReceipt, run_media_sync
from omakase.plus.plex import PlexClient
from omakase.plus.plex_setup import (
    PlexLibrarySpec,
    apply_plex_setup_plan,
    plan_plex_setup,
)
from omakase.plus.provider_limits import cooldown_hooks
from omakase.plus.providers.realdebrid import RealDebridProvider
from omakase.plus.secrets import read_secret
from omakase.plus.series_adoption import (
    apply_existing_series_adoption_plan,
    build_existing_series_adoption_plan,
)
from omakase.plus.storage import configure_root

_REQUEST_SCHEMA = "existing-movie-adoption-request-v1"
_SERIES_REQUEST_SCHEMA = "existing-series-adoption-request-v1"
_LOCAL_REQUEST_SCHEMA = "local-import-request-v1"
_PLEX_REQUEST_SCHEMA = "plex-setup-request-v1"
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def sync_media_once() -> SchedulerReceipt:
    """Reconcile every due monitor under the shared database lease."""
    from omakase.plus.watch.anilist_sync import process_tracker_outbox

    if os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").lower() != "true":
        # Retry only work already submitted by the live watch controls. Do not
        # replay the dormant historical outbox as a side effect of this release.
        process_tracker_outbox(get_db(), attempted_only=True)
    from omakase.plus.tracker_bridge_worker import sync_trackers_once

    # Independent opt-in tracker work; no household acquisition is activated.
    sync_trackers_once()

    async def reconcile_once():
        from omakase.plus.household_requests import consume_shared_anime_queue

        db = get_db()
        await consume_shared_anime_queue(db)
        return await run_media_sync(db, entrypoint="cli")

    return asyncio.run(reconcile_once())


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json_object(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as out:
            temporary_name = out.name
            os.chmod(temporary_name, 0o600)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _provider_for_user(db, user_id: int) -> RealDebridProvider:
    api_key = read_secret(db, user_id, "realdebrid_api_key")
    if not api_key:
        raise ValueError("Real-Debrid credentials are unavailable for this user")
    return RealDebridProvider(api_key, **cooldown_hooks(db, user_id))


def _request_rows(path: Path) -> list[dict[str, object]]:
    payload = _read_json_object(path, "adoption request")
    if (
        set(payload) != {"schema_version", "requests"}
        or payload["schema_version"] != _REQUEST_SCHEMA
    ):
        raise ValueError("adoption request schema is unsupported")
    requests = payload["requests"]
    if not isinstance(requests, list):
        raise ValueError("adoption request rows are invalid")
    return requests


def _series_request_rows(path: Path) -> list[dict[str, object]]:
    payload = _read_json_object(path, "series adoption request")
    if (
        set(payload) != {"schema_version", "requests"}
        or payload["schema_version"] != _SERIES_REQUEST_SCHEMA
    ):
        raise ValueError("series adoption request schema is unsupported")
    requests = payload["requests"]
    if not isinstance(requests, list):
        raise ValueError("series adoption request rows are invalid")
    return requests


def _local_request_rows(path: Path) -> tuple[LocalImportRequest, ...]:
    payload = _read_json_object(path, "local import request")
    if (
        set(payload) != {"schema_version", "requests"}
        or payload["schema_version"] != _LOCAL_REQUEST_SCHEMA
        or not isinstance(payload["requests"], list)
    ):
        raise ValueError("local import request schema is unsupported")
    requests: list[LocalImportRequest] = []
    try:
        for raw in payload["requests"]:
            if not isinstance(raw, dict):
                raise TypeError
            expected = {"monitor_id", "locator", "quality", "external_subtitles"}
            if not set(raw) <= expected or not {"monitor_id", "locator"} <= set(raw):
                raise TypeError
            subtitles: list[ExternalSubtitle] = []
            for subtitle in raw.get("external_subtitles", []):
                if not isinstance(subtitle, dict) or set(subtitle) != {
                    "locator",
                    "language",
                    "release_fingerprint",
                    "sync_state",
                }:
                    raise TypeError
                subtitles.append(ExternalSubtitle(**subtitle))
            requests.append(
                LocalImportRequest(
                    monitor_id=raw["monitor_id"],
                    locator=raw["locator"],
                    quality=raw.get("quality", "1080p"),
                    external_subtitles=tuple(subtitles),
                )
            )
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("local import request rows are invalid") from exc
    return tuple(requests)


def _plex_libraries(path: Path) -> tuple[PlexLibrarySpec, ...]:
    payload = _read_json_object(path, "Plex setup request")
    if (
        set(payload) != {"schema_version", "libraries"}
        or payload["schema_version"] != _PLEX_REQUEST_SCHEMA
        or not isinstance(payload["libraries"], list)
    ):
        raise ValueError("Plex setup request schema is unsupported")
    if any(
        not isinstance(item, dict) or set(item) != {"title", "section_type", "path"}
        for item in payload["libraries"]
    ):
        raise ValueError("Plex setup libraries are invalid")
    try:
        return tuple(
            PlexLibrarySpec(
                title=item["title"],
                section_type=item["section_type"],
                path=item["path"],
            )
            for item in payload["libraries"]
        )
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError("Plex setup libraries are invalid") from exc


def plan_existing_movies(
    *,
    user_id: int,
    request_path: Path,
    plan_path: Path,
    data_dir: str = "data",
) -> dict:
    """Observe existing RD movies and atomically persist a non-mutating plan."""
    db = get_db(data_dir)
    plan = asyncio.run(
        build_existing_movie_adoption_plan(
            db,
            user_id=user_id,
            requests=_request_rows(request_path),
            rd=_provider_for_user(db, user_id),
            identity_resolver=resolve_direct_request,
            created_at=_utc_now(),
        )
    )
    _write_private_json(plan_path, plan)
    return plan


def plan_existing_series(
    *,
    user_id: int,
    request_path: Path,
    plan_path: Path,
    data_dir: str = "data",
) -> dict:
    """Observe existing RD series and atomically persist a non-mutating plan."""
    db = get_db(data_dir)
    plan = asyncio.run(
        build_existing_series_adoption_plan(
            db,
            user_id=user_id,
            requests=_series_request_rows(request_path),
            rd=_provider_for_user(db, user_id),
            created_at=_utc_now(),
        )
    )
    _write_private_json(plan_path, plan)
    return plan


def plan_local_media(
    *,
    user_id: int,
    root_path: Path,
    request_path: Path,
    plan_path: Path,
    data_dir: str = "data",
) -> dict:
    """Observe an owned local root and persist a hash-bound import plan."""
    plan = plan_local_import(
        get_db(data_dir),
        user_id=user_id,
        root=configure_root(root_path, owner_id=user_id),
        requests=_local_request_rows(request_path),
        created_at=_utc_now(),
    )
    _write_private_json(plan_path, plan)
    return plan


def plan_plex_libraries(
    *,
    plex_url: str,
    plex_token: str,
    request_path: Path,
    plan_path: Path,
) -> dict:
    """Read Plex topology and persist a non-destructive setup plan."""
    with PlexClient(plex_url, plex_token) as client:
        plan = plan_plex_setup(
            client.list_sections(),
            libraries=_plex_libraries(request_path),
        )
    _write_private_json(plan_path, plan)
    return plan


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _create_backup(
    db: sqlite3.Connection,
    *,
    backup_path: Path,
    user_id: int,
    plan_hash: str,
    receipt_schema: str = "existing-movie-adoption-backup-v1",
) -> dict:
    """Create and validate one fresh SQLite rollback bound to the approved plan."""
    receipt_path = backup_path.with_name(f"{backup_path.name}.receipt.json")
    if backup_path.exists() or receipt_path.exists():
        raise ValueError("fresh backup path already exists")
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(backup_path)
    try:
        db.backup(destination)
        destination.commit()
        quick_check = destination.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise ValueError("fresh backup failed SQLite integrity validation")
        tables = {
            str(row[0])
            for row in destination.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        required = {"_migrations", "users", "media_monitors", "media_assets"}
        if not required <= tables:
            raise ValueError("fresh backup schema is incomplete")
        if destination.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
            raise ValueError("fresh backup does not contain the adoption user")
        schema_rows = destination.execute(
            """SELECT type, name, tbl_name, COALESCE(sql, '')
               FROM sqlite_master
               WHERE name NOT LIKE 'sqlite_%'
               ORDER BY type, name"""
        ).fetchall()
        schema_hash = hashlib.sha256(
            json.dumps(schema_rows, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        migrations = [
            str(row[0])
            for row in destination.execute("SELECT name FROM _migrations ORDER BY id").fetchall()
        ]
    finally:
        destination.close()
    os.chmod(backup_path, 0o600)
    receipt = {
        "schema_version": receipt_schema,
        "created_at": _utc_now(),
        "plan_hash": plan_hash,
        "user_id": user_id,
        "backup_path": str(backup_path.resolve()),
        "backup_sha256": _file_sha256(backup_path),
        "schema_sha256": schema_hash,
        "migrations": migrations,
        "quick_check": "ok",
    }
    _write_private_json(receipt_path, receipt)
    return receipt


def apply_existing_movies(
    *,
    plan_path: Path,
    backup_path: Path,
    expected_plan_hash: str,
    data_dir: str = "data",
) -> dict:
    """Require rollback evidence, re-observe the plan, and adopt local records."""
    plan = _read_json_object(plan_path, "adoption plan")
    if (
        not _SHA256_RE.fullmatch(expected_plan_hash)
        or not isinstance(plan.get("plan_hash"), str)
        or not hmac.compare_digest(plan["plan_hash"], expected_plan_hash.casefold())
    ):
        raise ValueError("approved plan hash does not match")
    user_id = plan.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("adoption plan user is invalid")
    db = get_db(data_dir)
    _create_backup(
        db,
        backup_path=backup_path,
        user_id=user_id,
        plan_hash=expected_plan_hash.casefold(),
    )
    return asyncio.run(
        apply_existing_movie_adoption_plan(
            db,
            plan=plan,
            expected_plan_hash=expected_plan_hash.casefold(),
            rd=_provider_for_user(db, user_id),
            identity_resolver=resolve_direct_request,
            applied_at=_utc_now(),
        )
    )


def apply_existing_series(
    *,
    plan_path: Path,
    backup_path: Path,
    expected_plan_hash: str,
    data_dir: str = "data",
) -> dict:
    """Require rollback evidence, re-observe the plan, and adopt series records."""
    plan = _read_json_object(plan_path, "series adoption plan")
    if (
        not _SHA256_RE.fullmatch(expected_plan_hash)
        or not isinstance(plan.get("plan_hash"), str)
        or not hmac.compare_digest(plan["plan_hash"], expected_plan_hash.casefold())
    ):
        raise ValueError("approved plan hash does not match")
    user_id = plan.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("series adoption plan user is invalid")
    db = get_db(data_dir)
    _create_backup(
        db,
        backup_path=backup_path,
        user_id=user_id,
        plan_hash=expected_plan_hash.casefold(),
        receipt_schema="existing-series-adoption-backup-v1",
    )
    return asyncio.run(
        apply_existing_series_adoption_plan(
            db,
            plan=plan,
            expected_plan_hash=expected_plan_hash.casefold(),
            rd=_provider_for_user(db, user_id),
            applied_at=_utc_now(),
        )
    )


def apply_local_media(
    *,
    plan_path: Path,
    root_path: Path,
    backup_path: Path,
    expected_plan_hash: str,
    data_dir: str = "data",
) -> dict:
    """Back up SQLite, re-observe content, probe streams, and import safely."""
    plan = _read_json_object(plan_path, "local import plan")
    if (
        not _SHA256_RE.fullmatch(expected_plan_hash)
        or not isinstance(plan.get("plan_hash"), str)
        or not hmac.compare_digest(plan["plan_hash"], expected_plan_hash.casefold())
    ):
        raise ValueError("approved plan hash does not match")
    user_id = plan.get("user_id")
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise ValueError("local import plan user is invalid")
    db = get_db(data_dir)
    _create_backup(
        db,
        backup_path=backup_path,
        user_id=user_id,
        plan_hash=expected_plan_hash.casefold(),
        receipt_schema="local-import-backup-v1",
    )
    return apply_local_import_plan(
        db,
        plan=plan,
        expected_plan_hash=expected_plan_hash.casefold(),
        root=configure_root(root_path, owner_id=user_id),
        applied_at=_utc_now(),
    )


def apply_plex_libraries(
    *,
    plex_url: str,
    plex_token: str,
    plan_path: Path,
    expected_plan_hash: str,
) -> dict:
    """Re-observe and apply an approved non-destructive Plex library plan."""
    plan = _read_json_object(plan_path, "Plex setup plan")
    with PlexClient(plex_url, plex_token) as client:
        return apply_plex_setup_plan(
            client,
            plan=plan,
            expected_plan_hash=expected_plan_hash,
        )
