"""Dry-run-first adoption of verified existing provider anime series.

The planner binds an existing torrent to a monitor, its BTIH, and the exact
selected media inventory. Applying the plan re-observes all evidence before
writing local publication state. Neither phase acquires, selects, or deletes a
provider item.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from omakase.plus.coverage import parse_episode_coverage
from omakase.plus.identity import identity_rejection_reason
from omakase.plus.identity_history import successful_exact_hash_identity_title
from omakase.plus.media_store import ensure_media_provider, upsert_asset
from omakase.plus.media_sync import (
    _inventory_fingerprint,
    _provider_source_folder,
    _refresh_shared_references,
)
from omakase.plus.providers.compat import capabilities, get_item, list_items

_SCHEMA_VERSION = "existing-series-adoption-plan-v2"
_BTIH_RE = re.compile(r"[0-9A-F]{40}")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_MEDIA_SUFFIXES = frozenset({".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts"})
_QUALITY_RANK = {"2160p": 3, "1080p": 2, "720p": 1}


class SeriesAdoptionError(ValueError):
    """An existing series cannot be adopted without ambiguity or mutation."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _plan_hash(plan_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(plan_without_hash).encode("utf-8")).hexdigest()


def _timestamp(name: str, value: object) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise SeriesAdoptionError(f"{name} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SeriesAdoptionError(f"{name} must be canonical UTC") from exc
    return value


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SeriesAdoptionError(f"{name} must be a positive integer")
    return value


def _normalize_requests(requests: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence) or not requests:
        raise SeriesAdoptionError("requests must be a nonempty sequence")
    normalized: list[dict[str, object]] = []
    seen_hashes: set[str] = set()
    for item in requests:
        if not isinstance(item, dict) or set(item) != {"monitor_id", "torrent_hash", "quality"}:
            raise SeriesAdoptionError(
                "each request must contain monitor_id, torrent_hash, and quality"
            )
        monitor_id = _positive_int("monitor_id", item["monitor_id"])
        torrent_hash = str(item["torrent_hash"]).strip().upper()
        quality = str(item["quality"]).strip()
        if not _BTIH_RE.fullmatch(torrent_hash):
            raise SeriesAdoptionError("torrent_hash must be a 40-character BTIH")
        if torrent_hash in seen_hashes:
            raise SeriesAdoptionError("torrent_hash requests must be unique")
        if quality not in _QUALITY_RANK:
            raise SeriesAdoptionError("quality must be 2160p, 1080p, or 720p")
        seen_hashes.add(torrent_hash)
        normalized.append(
            {"monitor_id": monitor_id, "torrent_hash": torrent_hash, "quality": quality}
        )
    return sorted(
        normalized,
        key=lambda item: (
            int(item["monitor_id"]),
            -_QUALITY_RANK[str(item["quality"])],
            str(item["torrent_hash"]),
        ),
    )


def _json_strings(value: object) -> tuple[str, ...]:
    try:
        decoded = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise SeriesAdoptionError("monitor search titles are invalid") from exc
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise SeriesAdoptionError("monitor search titles are invalid")
    return tuple(item.strip() for item in decoded if item.strip())


def _user_preservation(db: sqlite3.Connection, user_id: int) -> dict[str, object]:
    """Hash all publication state for the owner without exposing provider IDs."""
    monitors = [
        list(row)
        for row in db.execute(
            "SELECT * FROM media_monitors WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
    ]
    assets = [
        list(row)
        for row in db.execute(
            """SELECT asset.*
               FROM media_assets AS asset
               JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
               WHERE monitor.user_id = ?
               ORDER BY asset.id""",
            (user_id,),
        ).fetchall()
    ]
    pending_removals = [
        list(row)
        for row in db.execute(
            """SELECT removal.*
               FROM media_asset_removals AS removal
               JOIN media_assets AS asset ON asset.id = removal.asset_id
               JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
               WHERE monitor.user_id = ? AND removal.outcome = 'pending'
               ORDER BY removal.id""",
            (user_id,),
        ).fetchall()
    ]
    return {
        "schema_version": "series-adoption-preservation-v1",
        "monitor_count": len(monitors),
        "asset_count": len(assets),
        "pending_removal_count": len(pending_removals),
        "state_hash": hashlib.sha256(
            _canonical_json(
                {
                    "monitors": monitors,
                    "assets": assets,
                    "pending_removals": pending_removals,
                }
            ).encode("utf-8")
        ).hexdigest(),
    }


def _required_episode_count(monitor: sqlite3.Row) -> int:
    expected = monitor["expected_episodes"]
    aired = int(monitor["aired_episodes"] or 0)
    if str(monitor["release_status"]) == "FINISHED" and expected is not None:
        return int(expected)
    return aired


def _selected_series_inventory(
    torrent: object,
    monitor: sqlite3.Row,
) -> tuple[frozenset[int], str | None, list[dict[str, object]]]:
    expected = monitor["expected_episodes"]
    season = int(monitor["season_number"])
    anilist_id = int(monitor["anilist_id"])
    coverage: set[int] = set()
    numbering_override: str | None = None
    selected_files: list[dict[str, object]] = []
    for item in getattr(torrent, "files", ()):
        path = PurePosixPath(str(getattr(item, "path", "")).replace("\\", "/"))
        if (
            not bool(getattr(item, "selected", False))
            or int(getattr(item, "bytes", 0)) <= 0
            or path.suffix.casefold() not in _MEDIA_SUFFIXES
        ):
            continue
        parsed = parse_episode_coverage(
            path.name,
            season,
            expected=int(expected) if expected is not None else None,
            anilist_id=anilist_id,
        )
        if len(parsed.episodes) != 1 or parsed.specials:
            raise SeriesAdoptionError(
                "every selected series media file must map to exactly one target episode"
            )
        episode = next(iter(parsed.episodes))
        if episode in coverage:
            raise SeriesAdoptionError("selected series inventory contains duplicate episodes")
        if parsed.numbering_override is not None:
            if numbering_override not in {None, parsed.numbering_override}:
                raise SeriesAdoptionError("selected inventory has conflicting numbering overrides")
            numbering_override = parsed.numbering_override
        coverage.add(episode)
        selected_files.append(
            {
                "filename": path.name,
                "bytes": int(getattr(item, "bytes")),
                "episode": episode,
            }
        )
    if not selected_files:
        raise SeriesAdoptionError("series inventory has no selected media files")
    required_count = _required_episode_count(monitor)
    if required_count <= 0 or max(coverage) > required_count:
        raise SeriesAdoptionError("series inventory falls outside released episode coverage")
    return (
        frozenset(coverage),
        numbering_override,
        sorted(selected_files, key=lambda item: (int(item["episode"]), str(item["filename"]))),
    )


def _identity_evidence(
    db: sqlite3.Connection,
    *,
    monitor: sqlite3.Row,
    torrent_hash: str,
    source_title: str,
) -> tuple[str, str]:
    aliases = _json_strings(monitor["search_titles_json"])

    def rejection_reason(title: str) -> str:
        return identity_rejection_reason(
            title,
            target_title=str(monitor["title"]),
            target_aliases=aliases,
            season=int(monitor["season_number"]),
            anilist_id=int(monitor["anilist_id"]),
            media_format=str(monitor["format"]),
        )

    current_reason = rejection_reason(source_title)
    if not current_reason:
        return "provider_title", source_title
    if current_reason not in {"AMBIGUOUS_TARGET_IDENTITY", "TARGET_TITLE_MISMATCH"}:
        raise SeriesAdoptionError(f"series identity validation failed: {current_reason}")

    historical_title = successful_exact_hash_identity_title(
        db,
        user_id=int(monitor["user_id"]),
        anilist_id=int(monitor["anilist_id"]),
        torrent_hash=torrent_hash,
        target_title=str(monitor["title"]),
        target_aliases=aliases,
        season=int(monitor["season_number"]),
        media_format=str(monitor["format"]),
    )
    if historical_title is not None:
        return "successful_exact_hash_history", historical_title
    raise SeriesAdoptionError(f"series identity validation failed: {current_reason}")


async def _observe_plan(
    db: sqlite3.Connection,
    *,
    user_id: int,
    requests: Sequence[dict[str, object]],
    rd: object,
    created_at: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    user_id = _positive_int("user_id", user_id)
    if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise SeriesAdoptionError("user_id does not exist")
    created_at = _timestamp("created_at", created_at)
    normalized = _normalize_requests(requests)
    preservation = _user_preservation(db, user_id)
    if preservation["pending_removal_count"] != 0:
        raise SeriesAdoptionError("pending Real-Debrid removal blocks series adoption")

    try:
        summaries = await list_items(rd)
    except Exception as exc:
        raise SeriesAdoptionError("provider inventory could not be listed") from exc
    requested_hashes = {str(item["torrent_hash"]) for item in normalized}
    by_hash: dict[str, object] = {}
    for summary in summaries:
        torrent_hash = summary.content_hash
        if torrent_hash not in requested_hashes:
            continue
        if torrent_hash in by_hash:
            raise SeriesAdoptionError("provider returned a duplicate torrent hash")
        by_hash[torrent_hash] = summary

    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    identities: dict[int, dict[str, object]] = {}
    provider_ids: dict[str, str] = {}
    for request in normalized:
        monitor_id = int(request["monitor_id"])
        monitor = db.execute(
            "SELECT * FROM media_monitors WHERE id = ? AND user_id = ?",
            (monitor_id, user_id),
        ).fetchone()
        if monitor is None:
            raise SeriesAdoptionError("requested monitor does not exist for this user")
        if str(monitor["format"]) == "MOVIE" or monitor["season_number"] is None:
            raise SeriesAdoptionError("requested monitor must be an episodic series")
        torrent_hash = str(request["torrent_hash"])
        summary = by_hash.get(torrent_hash)
        if summary is None:
            raise SeriesAdoptionError("a requested torrent hash is absent from the provider")
        provider_id = summary.external_id
        if not provider_id:
            raise SeriesAdoptionError("provider inventory omitted an item identifier")
        try:
            torrent = await get_item(rd, provider_id)
        except Exception as exc:
            raise SeriesAdoptionError("a requested torrent could not be re-observed") from exc
        if torrent is None:
            raise SeriesAdoptionError("a requested torrent disappeared during observation")
        if torrent.content_hash != torrent_hash:
            raise SeriesAdoptionError("provider content hash changed during observation")
        if torrent.status != "downloaded" or torrent.progress != 100.0:
            raise SeriesAdoptionError("requested torrent is not fully downloaded")
        source_title = torrent.name
        identity_evidence, identity_title = _identity_evidence(
            db,
            monitor=monitor,
            torrent_hash=torrent_hash,
            source_title=source_title,
        )
        coverage, numbering_override, selected_files = _selected_series_inventory(torrent, monitor)
        if monitor_id not in identities:
            identities[monitor_id] = {
                "monitor_id": monitor_id,
                "anilist_id": int(monitor["anilist_id"]),
                "canonical_title": str(monitor["title"]),
                "format": str(monitor["format"]),
                "season_number": int(monitor["season_number"]),
            }
        asset: dict[str, object] = {
            "torrent_hash": torrent_hash,
            "quality": str(request["quality"]),
            "source_title": source_title,
            "identity_evidence": identity_evidence,
            "identity_title": identity_title,
            "source_folder": _provider_source_folder(torrent),
            "episode_coverage": sorted(coverage),
            "selected_files": selected_files,
            "inventory_fingerprint": _inventory_fingerprint(torrent),
        }
        if numbering_override is not None:
            asset["numbering_override"] = numbering_override
        grouped[monitor_id].append(asset)
        provider_ids[torrent_hash] = provider_id

    series = [
        {
            **identities[monitor_id],
            "assets": sorted(
                grouped[monitor_id],
                key=lambda item: (
                    -_QUALITY_RANK[str(item["quality"])],
                    str(item["torrent_hash"]),
                ),
            ),
        }
        for monitor_id in sorted(grouped)
    ]
    unsigned: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "user_id": user_id,
        "requests": normalized,
        "series": series,
        "preservation": preservation,
    }
    return {**unsigned, "plan_hash": _plan_hash(unsigned)}, provider_ids


async def build_existing_series_adoption_plan(
    db: sqlite3.Connection,
    *,
    user_id: int,
    requests: Sequence[dict[str, object]],
    rd: object,
    created_at: str,
) -> dict[str, Any]:
    """Build a hash- and inventory-bound read-only series adoption plan."""
    plan, _ = await _observe_plan(
        db,
        user_id=user_id,
        requests=requests,
        rd=rd,
        created_at=created_at,
    )
    return plan


def _validated_plan(plan: object) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise SeriesAdoptionError("plan hash validation failed")
    expected_keys = {
        "schema_version",
        "created_at",
        "user_id",
        "requests",
        "series",
        "preservation",
        "plan_hash",
    }
    if set(plan) != expected_keys or plan.get("schema_version") != _SCHEMA_VERSION:
        raise SeriesAdoptionError("plan hash validation failed")
    supplied_hash = plan.get("plan_hash")
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if (
        not isinstance(supplied_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supplied_hash)
        or not hmac.compare_digest(supplied_hash, _plan_hash(unsigned))
    ):
        raise SeriesAdoptionError("plan hash validation failed")
    _timestamp("created_at", plan.get("created_at"))
    _positive_int("user_id", plan.get("user_id"))
    if not isinstance(plan.get("requests"), list) or not isinstance(plan.get("series"), list):
        raise SeriesAdoptionError("plan hash validation failed")
    return plan


async def apply_existing_series_adoption_plan(
    db: sqlite3.Connection,
    *,
    plan: object,
    expected_plan_hash: str,
    rd: object,
    applied_at: str,
) -> dict[str, object]:
    """Revalidate a signed plan and transactionally adopt its local records."""
    validated = _validated_plan(plan)
    if (
        not isinstance(expected_plan_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_hash)
        or not hmac.compare_digest(expected_plan_hash, str(validated["plan_hash"]))
    ):
        raise SeriesAdoptionError("approved plan hash does not match")
    applied_at = _timestamp("applied_at", applied_at)
    observed, provider_ids = await _observe_plan(
        db,
        user_id=int(validated["user_id"]),
        requests=validated["requests"],
        rd=rd,
        created_at=str(validated["created_at"]),
    )
    if observed != validated:
        raise SeriesAdoptionError("plan hash-bound inventory changed before apply")

    assets_written = 0
    coverage_written: set[tuple[int, int]] = set()
    declared = capabilities(rd)
    provider_label = (
        "Real-Debrid" if declared.kind == "realdebrid" else declared.kind.replace("_", " ").title()
    )
    storage_backend = "zurg" if declared.kind == "realdebrid" else "local"
    db.execute("SAVEPOINT existing_series_adoption")
    try:
        current_preservation = _user_preservation(db, int(validated["user_id"]))
        if current_preservation != validated["preservation"]:
            raise SeriesAdoptionError("series publication state changed before apply")
        if current_preservation["pending_removal_count"] != 0:
            raise SeriesAdoptionError("pending Real-Debrid removal blocks series adoption")
        provider_id = ensure_media_provider(
            db,
            user_id=int(validated["user_id"]),
            kind=declared.kind,
            label=provider_label,
            config={},
            capabilities=asdict(declared),
        )
        for series in validated["series"]:
            monitor_id = int(series["monitor_id"])
            for asset in series["assets"]:
                torrent_hash = str(asset["torrent_hash"])
                coverage = tuple(int(value) for value in asset["episode_coverage"])
                upsert_asset(
                    db,
                    monitor_id=monitor_id,
                    torrent_hash=torrent_hash,
                    rd_torrent_id=(
                        provider_ids[torrent_hash] if declared.kind == "realdebrid" else ""
                    ),
                    provider_id=provider_id,
                    provider_asset_id=provider_ids[torrent_hash],
                    storage_backend=storage_backend,
                    storage_locator=str(asset["source_folder"]),
                    source_title=str(asset["source_title"]),
                    source_folder=str(asset["source_folder"]),
                    quality=str(asset["quality"]),
                    ownership="external",
                    provenance="adopted",
                    downloaded_once=True,
                    planned_coverage=coverage,
                    usable_coverage=coverage,
                    numbering_override=asset.get("numbering_override"),
                    inventory_fingerprint=str(asset["inventory_fingerprint"]),
                    inventory_verified_at=applied_at,
                    status="downloaded",
                    progress=100.0,
                    last_error="",
                )
                _refresh_shared_references(db, torrent_hash)
                coverage_written.update((monitor_id, episode) for episode in coverage)
                assets_written += 1
            db.execute(
                """UPDATE media_monitors
                   SET last_checked_at = ?, last_success_at = ?,
                       consecutive_failures = 0,
                       last_result = ?,
                       updated_at = datetime('now')
                   WHERE id = ?""",
                (
                    applied_at,
                    applied_at,
                    (
                        "ADOPTED_EXISTING_RD_SERIES"
                        if declared.kind == "realdebrid"
                        else "ADOPTED_EXISTING_PROVIDER_SERIES"
                    ),
                    monitor_id,
                ),
            )
        db.execute("RELEASE SAVEPOINT existing_series_adoption")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT existing_series_adoption")
        db.execute("RELEASE SAVEPOINT existing_series_adoption")
        raise
    return {
        "series": len(validated["series"]),
        "assets": assets_written,
        "episodes": len(coverage_written),
        "plan_hash": validated["plan_hash"],
    }
