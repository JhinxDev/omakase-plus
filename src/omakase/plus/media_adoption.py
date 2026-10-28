"""Dry-run-first adoption of verified existing provider anime movies.

Planning is read-only and binds every requested asset to its BTIH plus the
observed selected-file inventory. Applying re-observes that same evidence
before writing local monitor state; it never acquires, selects, or deletes a
provider item.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from omakase.plus.media_store import ensure_media_provider, upsert_asset, upsert_monitor
from omakase.plus.media_sync import _inventory_fingerprint, _provider_source_folder
from omakase.plus.providers.compat import capabilities, get_item, list_items

_SCHEMA_VERSION = "existing-movie-adoption-plan-v1"
_BTIH_RE = re.compile(r"[0-9A-F]{40}")
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_MEDIA_SUFFIXES = frozenset({".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts"})
_QUALITY_RANK = {"2160p": 3, "1080p": 2, "720p": 1}


class MovieAdoptionError(ValueError):
    """An existing movie cannot be adopted without ambiguity or mutation."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _plan_hash(plan_without_hash: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(plan_without_hash).encode("utf-8")).hexdigest()


def _timestamp(name: str, value: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise MovieAdoptionError(f"{name} must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise MovieAdoptionError(f"{name} must be canonical UTC") from exc
    return value


def _positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MovieAdoptionError(f"{name} must be a positive integer")
    return value


def _normalize_requests(requests: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence) or not requests:
        raise MovieAdoptionError("requests must be a nonempty sequence")
    normalized: list[dict[str, object]] = []
    seen_hashes: set[str] = set()
    for item in requests:
        if not isinstance(item, dict) or set(item) != {"anilist_id", "torrent_hash", "quality"}:
            raise MovieAdoptionError("each request must contain anilist_id, torrent_hash, quality")
        anilist_id = _positive_int("anilist_id", item["anilist_id"])
        torrent_hash = str(item["torrent_hash"]).strip().upper()
        quality = str(item["quality"]).strip()
        if not _BTIH_RE.fullmatch(torrent_hash):
            raise MovieAdoptionError("torrent_hash must be a 40-character BTIH")
        if torrent_hash in seen_hashes:
            raise MovieAdoptionError("torrent_hash requests must be unique")
        if quality not in _QUALITY_RANK:
            raise MovieAdoptionError("quality must be 2160p, 1080p, or 720p")
        seen_hashes.add(torrent_hash)
        normalized.append(
            {"anilist_id": anilist_id, "torrent_hash": torrent_hash, "quality": quality}
        )
    return sorted(
        normalized,
        key=lambda item: (
            int(item["anilist_id"]),
            -_QUALITY_RANK[str(item["quality"])],
            str(item["torrent_hash"]),
        ),
    )


def _string_tuple(name: str, value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise MovieAdoptionError(f"identity {name} must be a sequence")
    try:
        values = tuple(str(item).strip() for item in value)  # type: ignore[union-attr]
    except TypeError as exc:
        raise MovieAdoptionError(f"identity {name} must be a sequence") from exc
    return tuple(item for item in values if item)


def _identity_record(target: object, requested_id: int) -> dict[str, object]:
    try:
        anilist_id = _positive_int("resolved anilist_id", getattr(target, "anilist_id"))
        title = str(getattr(target, "title")).strip()
        search_titles = _string_tuple("search_titles", getattr(target, "search_titles"))
        genres = _string_tuple("genres", getattr(target, "genres"))
        format_value = str(getattr(target, "format"))
        release_status = str(getattr(target, "status"))
        episodes = getattr(target, "episodes")
        aired_episodes = getattr(target, "aired_episodes")
        season_number = getattr(target, "season_number")
    except (AttributeError, TypeError) as exc:
        raise MovieAdoptionError("identity resolver returned incomplete metadata") from exc
    if anilist_id != requested_id:
        raise MovieAdoptionError("identity resolver returned a different AniList ID")
    if not title or format_value != "MOVIE" or release_status != "FINISHED":
        raise MovieAdoptionError("identity must be a finished movie")
    if episodes != 1 or aired_episodes != 1 or season_number is not None:
        raise MovieAdoptionError("movie identity must describe exactly one released movie")
    if not genres:
        raise MovieAdoptionError("movie identity must include rich genres")
    return {
        "anilist_id": anilist_id,
        "canonical_title": title,
        "search_titles": list(search_titles),
        "genres": list(genres),
        "format": "MOVIE",
        "release_status": "FINISHED",
        "expected_episodes": 1,
        "aired_episodes": 1,
        "season_number": None,
    }


def _selected_movie_file(torrent: object) -> object:
    selected = [
        item
        for item in getattr(torrent, "files", ())
        if bool(getattr(item, "selected", False))
        and int(getattr(item, "bytes", 0)) > 0
        and PurePosixPath(str(getattr(item, "path", "")).replace("\\", "/")).suffix.casefold()
        in _MEDIA_SUFFIXES
    ]
    if len(selected) != 1:
        raise MovieAdoptionError("movie inventory must contain exactly one selected media file")
    return selected[0]


def _non_movie_preservation(db: sqlite3.Connection) -> dict[str, object]:
    """Hash all non-movie publication state without serializing provider identifiers."""
    monitors = [
        list(row)
        for row in db.execute(
            """SELECT user_id, anilist_id, title, search_titles_json, genres_json,
                      format, release_status, season_number, expected_episodes,
                      aired_episodes, active, next_check_at, consecutive_failures, last_result
               FROM media_monitors
               WHERE format <> 'MOVIE'
               ORDER BY user_id, anilist_id"""
        ).fetchall()
    ]
    assets = [
        list(row)
        for row in db.execute(
            """SELECT monitor.user_id, monitor.anilist_id, asset.torrent_hash,
                      asset.rd_torrent_id, asset.source_title, asset.source_folder, asset.quality,
                      asset.ownership, asset.provenance, asset.downloaded_once,
                      asset.planned_coverage_json, asset.usable_coverage_json,
                      asset.numbering_override, asset.inventory_fingerprint,
                      asset.inventory_verified_at, asset.status, asset.progress
               FROM media_assets AS asset
               JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
               WHERE monitor.format <> 'MOVIE'
               ORDER BY monitor.user_id, monitor.anilist_id, asset.torrent_hash"""
        ).fetchall()
    ]
    pending_removals = [
        list(row)
        for row in db.execute(
            """SELECT monitor.user_id, monitor.anilist_id, asset.torrent_hash,
                      removal.operation_key, removal.requested_at
               FROM media_asset_removals AS removal
               JOIN media_assets AS asset ON asset.id = removal.asset_id
               JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
               WHERE removal.outcome = 'pending'
               ORDER BY monitor.user_id, monitor.anilist_id, asset.torrent_hash,
                        removal.operation_key"""
        ).fetchall()
    ]
    state_hash = hashlib.sha256(
        _canonical_json(
            {"monitors": monitors, "assets": assets, "pending_removals": pending_removals}
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": "non-movie-preservation-v1",
        "monitor_count": len(monitors),
        "asset_count": len(assets),
        "pending_removal_count": len(pending_removals),
        "state_hash": state_hash,
    }


async def _observe_plan(
    db: sqlite3.Connection,
    *,
    user_id: int,
    requests: Sequence[dict[str, object]],
    rd: object,
    identity_resolver: Callable[[str, str], object],
    created_at: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    user_id = _positive_int("user_id", user_id)
    if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise MovieAdoptionError("user_id does not exist")
    created_at = _timestamp("created_at", created_at)
    normalized = _normalize_requests(requests)
    requested_ids = sorted({int(item["anilist_id"]) for item in normalized})
    placeholders = ",".join("?" for _ in requested_ids)
    collision = db.execute(
        f"""SELECT 1 FROM media_monitors
            WHERE user_id = ? AND anilist_id IN ({placeholders}) AND format <> 'MOVIE'
            LIMIT 1""",
        (user_id, *requested_ids),
    ).fetchone()
    if collision is not None:
        raise MovieAdoptionError("requested movie identity collides with a non-movie monitor")
    preservation = _non_movie_preservation(db)
    if preservation["pending_removal_count"] != 0:
        raise MovieAdoptionError("pending Real-Debrid removal blocks movie adoption")

    try:
        summaries = await list_items(rd)
    except Exception as exc:
        raise MovieAdoptionError("provider inventory could not be listed") from exc
    by_hash: dict[str, object] = {}
    requested_hashes = {str(item["torrent_hash"]) for item in normalized}
    for summary in summaries:
        torrent_hash = summary.content_hash
        if not torrent_hash or torrent_hash not in requested_hashes:
            continue
        if torrent_hash in by_hash:
            raise MovieAdoptionError("provider returned a duplicate torrent hash")
        by_hash[torrent_hash] = summary

    grouped: dict[int, list[dict[str, object]]] = defaultdict(list)
    provider_ids: dict[str, str] = {}
    identities: dict[int, dict[str, object]] = {}
    for request in normalized:
        anilist_id = int(request["anilist_id"])
        torrent_hash = str(request["torrent_hash"])
        summary = by_hash.get(torrent_hash)
        if summary is None:
            raise MovieAdoptionError("a requested torrent hash is absent from the provider")
        provider_id = summary.external_id
        if not provider_id:
            raise MovieAdoptionError("provider inventory omitted an item identifier")
        try:
            torrent = await get_item(rd, provider_id)
        except Exception as exc:
            raise MovieAdoptionError("a requested torrent could not be re-observed") from exc
        if torrent is None:
            raise MovieAdoptionError("a requested torrent disappeared during observation")
        observed_hash = torrent.content_hash
        if observed_hash != torrent_hash:
            raise MovieAdoptionError("provider content hash changed during observation")
        if torrent.status != "downloaded" or torrent.progress != 100.0:
            raise MovieAdoptionError("requested torrent is not fully downloaded")
        selected = _selected_movie_file(torrent)
        if anilist_id not in identities:
            try:
                target = identity_resolver(str(anilist_id), "Movie")
            except Exception as exc:
                raise MovieAdoptionError("movie identity could not be resolved") from exc
            identities[anilist_id] = _identity_record(target, anilist_id)
        filename = PurePosixPath(str(getattr(selected, "path")).replace("\\", "/")).name
        grouped[anilist_id].append(
            {
                "torrent_hash": torrent_hash,
                "quality": str(request["quality"]),
                "source_title": torrent.name,
                "source_folder": _provider_source_folder(torrent),
                "selected_filename": filename,
                "selected_bytes": int(getattr(selected, "bytes")),
                "inventory_fingerprint": _inventory_fingerprint(torrent),
                "episode_coverage": [1],
            }
        )
        provider_ids[torrent_hash] = provider_id

    movies: list[dict[str, object]] = []
    for anilist_id in sorted(grouped):
        assets = sorted(
            grouped[anilist_id],
            key=lambda item: (-_QUALITY_RANK[str(item["quality"])], str(item["torrent_hash"])),
        )
        movies.append({**identities[anilist_id], "assets": assets})
    unsigned: dict[str, Any] = {
        "schema_version": _SCHEMA_VERSION,
        "created_at": created_at,
        "user_id": user_id,
        "requests": normalized,
        "movies": movies,
        "preservation": preservation,
    }
    return {**unsigned, "plan_hash": _plan_hash(unsigned)}, provider_ids


async def build_existing_movie_adoption_plan(
    db: sqlite3.Connection,
    *,
    user_id: int,
    requests: Sequence[dict[str, object]],
    rd: object,
    identity_resolver: Callable[[str, str], object],
    created_at: str,
) -> dict[str, Any]:
    """Build a hash- and inventory-bound read-only adoption plan."""
    plan, _ = await _observe_plan(
        db,
        user_id=user_id,
        requests=requests,
        rd=rd,
        identity_resolver=identity_resolver,
        created_at=created_at,
    )
    return plan


def _validated_plan(plan: object) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise MovieAdoptionError("plan hash validation failed")
    expected_keys = {
        "schema_version",
        "created_at",
        "user_id",
        "requests",
        "movies",
        "preservation",
        "plan_hash",
    }
    if set(plan) != expected_keys or plan.get("schema_version") != _SCHEMA_VERSION:
        raise MovieAdoptionError("plan hash validation failed")
    supplied_hash = plan.get("plan_hash")
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if (
        not isinstance(supplied_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", supplied_hash)
        or not hmac.compare_digest(supplied_hash, _plan_hash(unsigned))
    ):
        raise MovieAdoptionError("plan hash validation failed")
    _timestamp("created_at", plan.get("created_at"))
    _positive_int("user_id", plan.get("user_id"))
    if not isinstance(plan.get("requests"), list) or not isinstance(plan.get("movies"), list):
        raise MovieAdoptionError("plan hash validation failed")
    return plan


async def apply_existing_movie_adoption_plan(
    db: sqlite3.Connection,
    *,
    plan: object,
    expected_plan_hash: str,
    rd: object,
    identity_resolver: Callable[[str, str], object],
    applied_at: str,
) -> dict[str, object]:
    """Revalidate a signed plan and transactionally adopt its local records."""
    validated = _validated_plan(plan)
    if (
        not isinstance(expected_plan_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_plan_hash)
        or not hmac.compare_digest(expected_plan_hash, str(validated["plan_hash"]))
    ):
        raise MovieAdoptionError("approved plan hash does not match")
    applied_at = _timestamp("applied_at", applied_at)
    observed, provider_ids = await _observe_plan(
        db,
        user_id=int(validated["user_id"]),
        requests=validated["requests"],
        rd=rd,
        identity_resolver=identity_resolver,
        created_at=str(validated["created_at"]),
    )
    if observed != validated:
        raise MovieAdoptionError("plan hash-bound inventory changed before apply")

    assets_written = 0
    declared = capabilities(rd)
    provider_label = (
        "Real-Debrid" if declared.kind == "realdebrid" else declared.kind.replace("_", " ").title()
    )
    storage_backend = "zurg" if declared.kind == "realdebrid" else "local"
    db.execute("SAVEPOINT existing_movie_adoption")
    try:
        current_preservation = _non_movie_preservation(db)
        if current_preservation != validated["preservation"]:
            raise MovieAdoptionError("non-movie preservation state changed before apply")
        if current_preservation["pending_removal_count"] != 0:
            raise MovieAdoptionError("pending Real-Debrid removal blocks movie adoption")
        provider_id = ensure_media_provider(
            db,
            user_id=int(validated["user_id"]),
            kind=declared.kind,
            label=provider_label,
            config={},
            capabilities=asdict(declared),
        )
        for movie in validated["movies"]:
            monitor_id = upsert_monitor(
                db,
                user_id=int(validated["user_id"]),
                anilist_id=int(movie["anilist_id"]),
                title=str(movie["canonical_title"]),
                search_titles=movie["search_titles"],
                genres=movie["genres"],
                format="MOVIE",
                release_status="FINISHED",
                season_number=None,
                expected_episodes=1,
                aired_episodes=1,
                active=False,
                last_checked_at=applied_at,
                last_success_at=applied_at,
                next_check_at=None,
                consecutive_failures=0,
                last_result=(
                    "ADOPTED_EXISTING_RD_MOVIE"
                    if declared.kind == "realdebrid"
                    else "ADOPTED_EXISTING_PROVIDER_MOVIE"
                ),
            )
            for asset in movie["assets"]:
                torrent_hash = str(asset["torrent_hash"])
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
                    planned_coverage=(1,),
                    usable_coverage=(1,),
                    inventory_fingerprint=str(asset["inventory_fingerprint"]),
                    inventory_verified_at=applied_at,
                    status="downloaded",
                    progress=100.0,
                    last_error="",
                )
                assets_written += 1
        db.execute("RELEASE SAVEPOINT existing_movie_adoption")
        db.commit()
    except Exception:
        db.execute("ROLLBACK TO SAVEPOINT existing_movie_adoption")
        db.execute("RELEASE SAVEPOINT existing_movie_adoption")
        raise
    return {
        "movies": len(validated["movies"]),
        "assets": assets_written,
        "plan_hash": validated["plan_hash"],
    }
