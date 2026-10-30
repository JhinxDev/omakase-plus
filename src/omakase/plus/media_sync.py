"""Idempotent media coverage reconciliation across acquisition providers."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from omakase.plus.acquisition_policy import AcquisitionPolicy, load_user_acquisition_policy
from omakase.plus.catalog import (
    AliasCatalogWindow,
    CatalogSearchReceipt,
    candidate_rejection_reason,
    is_hardsub_release,
    merge_alias_windows,
    rank_candidates,
    search_alias_windows,
)
from omakase.plus.catalog_config import configured_catalogs
from omakase.plus.coverage import (
    parse_episode_coverage,
    parse_single_file_coverage,
    select_episode_cover,
)
from omakase.plus.identity import identity_rejection_reason
from omakase.plus.media_store import (
    acquire_sync_lease,
    complete_sync_run,
    covered_episodes,
    ensure_media_provider,
    heartbeat_sync_lease,
    list_due_monitors,
    record_alias_search_receipt,
    record_asset_observation,
    record_asset_removal,
    record_candidate_attempt,
    record_sync_run,
    release_sync_lease,
    update_catalog_health,
    update_provider_health,
    upsert_asset,
    upsert_monitor,
)
from omakase.plus.media_types import (
    EpisodeCoverage,
    MediaAsset,
    MediaCandidate,
    MediaRecheckIntent,
    MediaSyncResult,
)
from omakase.plus.provider_limits import ProviderRateLimit, cooldown_hooks
from omakase.plus.providers.compat import (
    acquire,
    capabilities,
    delete_item,
    get_item,
    list_items,
    normalize_item,
    select_files,
)
from omakase.plus.providers.compat import (
    instant_available as provider_instant_available,
)
from omakase.plus.providers.contracts import ProviderBlock, ProviderItem
from omakase.plus.secrets import read_secret

_QUERY_VERSION = "catalog-v2"
_STALLED_STATUSES = frozenset({"queued", "downloading"})
_MEDIA_SUFFIXES = frozenset({".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts"})


class _MonitorWithdrawn(BaseException):
    """Cooperative cancellation which transport error handlers must not swallow."""


@dataclass(frozen=True, slots=True)
class LanguageDispatch:
    request_id: str
    actor_user_id: int
    source_generation: str
    contract_version: str


class _LanguagePermissionChanged(BaseException):
    pass


def _language_result(db, monitor, *, outcome, detail, now, required=None):
    required = frozenset(required) if required is not None else _required_episodes(monitor)
    covered = frozenset(covered_episodes(db, int(monitor["id"])))
    run_id = record_sync_run(
        db,
        monitor_id=int(monitor["id"]),
        outcome=outcome,
        required_count=len(required),
        covered_count=len(covered & required),
        detail=detail,
        completed_at=now,
    )
    return MediaSyncResult(int(monitor["id"]), run_id, outcome, required, covered, (), detail)


def _language_heartbeat(db, dispatch, heartbeat):
    def check():
        from omakase.plus.language_store import load_request

        try:
            load_request(
                db,
                actor_user_id=dispatch.actor_user_id,
                request_id=dispatch.request_id,
                for_acquisition=True,
            )
        except ValueError:
            raise _LanguagePermissionChanged() from None
        if heartbeat is not None:
            heartbeat()

    return check


def _legacy_language_monitor(db, monitor_id):
    return (
        db.execute(
            "SELECT 1 FROM legacy_language_monitors WHERE monitor_id=?", (monitor_id,)
        ).fetchone()
        is not None
    )


def _monitor_withdrawn(db: sqlite3.Connection, monitor_id: int) -> bool:
    return (
        db.execute(
            """SELECT 1 FROM media_title_removals WHERE monitor_id = ?
           UNION ALL SELECT 1 FROM completion_plex_cleanup_blocks WHERE monitor_id = ?
           LIMIT 1""",
            (monitor_id, monitor_id),
        ).fetchone()
        is not None
    )


def _guard_withdrawal(db: sqlite3.Connection, monitor_id: int) -> None:
    if _monitor_withdrawn(db, monitor_id):
        raise _MonitorWithdrawn()


def _withdrawn_result(db: sqlite3.Connection, monitor: sqlite3.Row) -> MediaSyncResult:
    return MediaSyncResult(
        monitor_id=int(monitor["id"]),
        run_id=0,
        outcome="withdrawn",
        required=_required_episodes(monitor),
        covered=covered_episodes(db, int(monitor["id"])),
        accepted_assets=(),
        detail="Publication withdrawn; acquisition stopped and source media retained",
    )


def _withdrawal_heartbeat(db, monitor_id, heartbeat):
    def check():
        if heartbeat is not None:
            heartbeat()
        _guard_withdrawal(db, monitor_id)

    return check


def _keep_withdrawn_inactive(db: sqlite3.Connection, monitor_id: int) -> bool:
    if not _monitor_withdrawn(db, monitor_id):
        return False
    db.execute(
        "UPDATE media_monitors SET active = 0, next_check_at = NULL WHERE id = ?", (monitor_id,)
    )
    return True


async def _download_access_error(provider, external_id: str) -> str:
    """Optional provider access check; transport errors are not policy blocks."""
    method = getattr(provider, "verify_download", None)
    if method is None:
        return ""
    try:
        return "" if await method(external_id) else "DOWNLOAD_ACCESS_UNVERIFIED"
    except ProviderRateLimit:
        raise
    except Exception as exc:
        if (
            isinstance(exc, ProviderBlock)
            or getattr(exc, "http_status", None) == 451
            or getattr(exc, "error_code", "") in {"35", "infringing_file"}
        ):
            return "PROVIDER_DOWNLOAD_BLOCKED"
        return "DOWNLOAD_ACCESS_UNVERIFIED"


@dataclass(frozen=True, slots=True)
class SchedulerMonitorResult:
    monitor_id: int
    outcome: str
    detail: str


@dataclass(frozen=True, slots=True)
class SchedulerReceipt:
    outcome: str
    monitor_results: tuple[SchedulerMonitorResult, ...]
    recovered_owner_hash: str = ""


def _monitor_value(monitor: sqlite3.Row, key: str, default=None):
    return monitor[key] if key in monitor.keys() else default


def _required_episodes(monitor: sqlite3.Row) -> frozenset[int]:
    status = str(_monitor_value(monitor, "release_status", ""))
    expected = _monitor_value(monitor, "expected_episodes")
    aired = int(_monitor_value(monitor, "aired_episodes", 0) or 0)
    count = int(expected or 0) if status == "FINISHED" and expected is not None else aired
    return frozenset(range(1, max(0, count) + 1))


def _alias_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _candidate_alias(candidate: MediaCandidate, searched_aliases: tuple[str, ...]) -> str:
    return candidate.search_alias.strip() or (searched_aliases[0] if searched_aliases else "direct")


def _inventory_coverage(item: ProviderItem | object, monitor: sqlite3.Row) -> EpisodeCoverage:
    item = normalize_item(item)
    if str(_monitor_value(monitor, "format", "")) == "MOVIE":
        selected_media = [
            provider_file
            for provider_file in item.files
            if provider_file.selected
            and provider_file.bytes > 0
            and PurePosixPath(provider_file.path.replace("\\", "/")).suffix.casefold()
            in _MEDIA_SUFFIXES
        ]
        if len(selected_media) != 1:
            raise ValueError("movie inventory must contain exactly one selected media file")
        return EpisodeCoverage(episodes=frozenset({1}))
    season = int(_monitor_value(monitor, "season_number", 1) or 1)
    expected = _monitor_value(monitor, "expected_episodes")
    anilist_id = int(_monitor_value(monitor, "anilist_id", 0) or 0) or None
    covered: set[int] = set()
    numbering_override: str | None = None
    for provider_file in item.files:
        if not provider_file.selected:
            continue
        filename = PurePosixPath(provider_file.path.replace("\\", "/")).name
        parsed = parse_episode_coverage(
            filename,
            season,
            expected=int(expected) if expected is not None else None,
            anilist_id=anilist_id,
        )
        covered.update(parsed.episodes)
        if parsed.numbering_override is not None:
            if numbering_override not in {None, parsed.numbering_override}:
                raise ValueError("conflicting numbering overrides in selected inventory")
            numbering_override = parsed.numbering_override
    return EpisodeCoverage(
        episodes=frozenset(covered),
        numbering_override=numbering_override,
    )


def _inventory_fingerprint(item: ProviderItem | object) -> str:
    item = normalize_item(item)
    selected = sorted(
        (PurePosixPath(provider_file.path.replace("\\", "/")).name, provider_file.bytes)
        for provider_file in item.files
        if provider_file.selected
    )
    payload = json.dumps(selected, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _provider_source_folder(item: ProviderItem | object) -> str:
    """Return the stable folder/leaf used by the provider's publication root."""
    item = normalize_item(item)
    locator = item.storage_locator.strip()
    if locator:
        return PurePosixPath(locator.replace("\\", "/")).name
    filename = PurePosixPath(item.name.replace("\\", "/")).name
    suffix = PurePosixPath(filename).suffix
    return filename[: -len(suffix)] if suffix.casefold() in _MEDIA_SUFFIXES else filename


def _rd_source_folder(item: ProviderItem | object) -> str:
    """Compatibility alias for existing adoption callers and tests."""
    return _provider_source_folder(item)


def _provider_context(
    db: sqlite3.Connection,
    monitor: sqlite3.Row,
    provider: object,
) -> tuple[int, str, str]:
    declared = capabilities(provider)
    provider_id = ensure_media_provider(
        db,
        user_id=int(monitor["user_id"]),
        kind=declared.kind,
        label="Real-Debrid"
        if declared.kind == "realdebrid"
        else declared.kind.replace("_", " ").title(),
        config={},
        capabilities=asdict(declared),
    )
    backend = "zurg" if declared.kind == "realdebrid" else "local"
    return provider_id, declared.kind, backend


async def _refresh_provider_health(
    db: sqlite3.Connection,
    *,
    user_id: int,
    provider: object,
    checked_at: str,
) -> None:
    """Persist a readiness check without requiring a due media monitor."""
    declared = capabilities(provider)
    provider_id = ensure_media_provider(
        db,
        user_id=user_id,
        kind=declared.kind,
        label="Real-Debrid"
        if declared.kind == "realdebrid"
        else declared.kind.replace("_", " ").title(),
        config={},
        capabilities=asdict(declared),
    )
    health_method = getattr(provider, "health", None)
    if not callable(health_method):
        return
    try:
        receipt = await health_method(checked_at=checked_at)
    except ProviderRateLimit:
        raise
    except Exception:
        update_provider_health(
            db,
            provider_id=provider_id,
            status="error",
            reason_code="HEALTH_CHECK_FAILED",
            detail="Provider readiness check failed",
            checked_at=checked_at,
        )
    else:
        update_provider_health(
            db,
            provider_id=provider_id,
            status=receipt.status,
            reason_code=receipt.reason_code,
            detail=receipt.detail,
            checked_at=receipt.checked_at,
        )


def _asset_contract(
    db: sqlite3.Connection,
    asset_id: int,
    *,
    planned: frozenset[int],
    usable: frozenset[int],
) -> MediaAsset:
    row = db.execute("SELECT * FROM media_assets WHERE id = ?", (asset_id,)).fetchone()
    if row is None:
        raise RuntimeError("media asset disappeared after upsert")
    return MediaAsset(
        id=asset_id,
        monitor_id=int(row["monitor_id"]),
        torrent_hash=str(row["torrent_hash"]),
        rd_torrent_id=str(row["rd_torrent_id"]),
        source_title=str(row["source_title"]),
        source_folder=str(row["source_folder"]),
        quality=str(row["quality"]),
        planned_coverage=planned,
        usable_coverage=usable,
        numbering_override=row["numbering_override"],
        status=str(row["status"]),
        inventory_verified_at=row["inventory_verified_at"],
        provider_id=row["provider_id"],
        provider_asset_id=str(row["provider_asset_id"]),
        storage_backend=str(row["storage_backend"]),
        storage_locator=str(row["storage_locator"]),
    )


def _refresh_shared_references(db: sqlite3.Connection, torrent_hash: str) -> None:
    rows = db.execute(
        "SELECT id FROM media_assets WHERE torrent_hash = ? ORDER BY id", (torrent_hash,)
    ).fetchall()
    shared_count = max(0, len(rows) - 1)
    db.execute(
        "UPDATE media_assets SET shared_reference_count = ? WHERE torrent_hash = ?",
        (shared_count, torrent_hash),
    )


def _record_attempt(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    run_id: int,
    candidate: MediaCandidate,
    searched_aliases: tuple[str, ...],
    rank: int,
    decision: str,
    now: str,
    reason: str = "",
    rd_outcome: str = "",
    instant_checked: bool = False,
    instant_available: bool = False,
    asset_id: int | None = None,
) -> int:
    return record_candidate_attempt(
        db,
        monitor_id=monitor_id,
        sync_run_id=run_id,
        asset_id=asset_id,
        search_alias=_candidate_alias(candidate, searched_aliases),
        source_title=candidate.source_title,
        torrent_hash=candidate.torrent_hash,
        candidate_rank=rank,
        quality=candidate.quality,
        seeders=candidate.seeders,
        proposed_coverage=candidate.coverage,
        decision=decision,
        reason=reason,
        rd_outcome=rd_outcome,
        catalog_kind=candidate.catalog_kind,
        instant_checked=instant_checked,
        instant_available=instant_available,
        terminal_at=now,
    )


def _strict_inventory_coverage(item, monitor, torrent_hash, planned) -> EpisodeCoverage:
    media = tuple(
        file
        for file in item.files
        if file.selected
        and PurePosixPath(file.path.replace("\\", "/")).suffix.casefold() in _MEDIA_SUFFIXES
    )
    if (
        item.status != "downloaded"
        or item.progress != 100.0
        or item.content_hash.upper() != torrent_hash.upper()
        or not media
        or any(file.bytes <= 0 for file in media)
    ):
        return EpisodeCoverage()
    parsed_files = []
    for file in media:
        if identity_rejection_reason(
            file.path.replace("\\", "/"),
            target_title=str(monitor["title"]),
            target_aliases=_monitor_aliases(monitor),
            season=int(monitor["season_number"]),
            anilist_id=int(monitor["anilist_id"]),
            media_format=str(monitor["format"]),
        ):
            return EpisodeCoverage()
        parsed = parse_single_file_coverage(
            PurePosixPath(file.path.replace("\\", "/")).name,
            int(monitor["season_number"]),
            expected=int(monitor["expected_episodes"] or 0) or None,
            anilist_id=int(monitor["anilist_id"]),
        )
        if not parsed.episodes:
            return EpisodeCoverage()
        parsed_files.append(parsed)
    observed = _inventory_coverage(replace(item, files=media), monitor)
    if len(observed.episodes) != len(parsed_files) or not observed.episodes <= frozenset(planned):
        return EpisodeCoverage()
    return observed


async def _attempt_candidate(
    db: sqlite3.Connection,
    monitor: sqlite3.Row,
    candidate: MediaCandidate,
    *,
    rd,
    run_id: int,
    rank: int,
    searched_aliases: tuple[str, ...],
    now: str,
    fallback: bool,
    existing_torrent: ProviderItem | object | None = None,
    heartbeat: Callable[[], None] | None = None,
) -> MediaAsset | None:
    def candidate_upsert(db, **kwargs):
        if not candidate.metadata_backed:
            return upsert_asset(db, **kwargs)
        db.execute("SAVEPOINT metadata_asset_upsert")
        try:
            asset_id = upsert_asset(db, **kwargs)
            if candidate.metadata_backed:
                db.execute(
                    "UPDATE media_assets SET requires_strict_inventory = 1 WHERE id = ?",
                    (asset_id,),
                )
            db.execute("RELEASE metadata_asset_upsert")
            return asset_id
        except BaseException:
            db.execute("ROLLBACK TO metadata_asset_upsert")
            db.execute("RELEASE metadata_asset_upsert")
            raise

    monitor_id = int(monitor["id"])
    _guard_withdrawal(db, monitor_id)
    adopting = existing_torrent is not None
    provider_id, provider_kind, storage_backend = _provider_context(db, monitor, rd)
    existing_item = normalize_item(existing_torrent) if existing_torrent is not None else None

    def guard_retained_source(provider_asset_id):
        if not _monitor_withdrawn(db, monitor_id):
            return
        if (
            provider_asset_id
            and not adopting
            and db.execute(
                "SELECT 1 FROM media_assets WHERE monitor_id = ? AND torrent_hash = ?",
                (monitor_id, candidate.torrent_hash),
            ).fetchone()
            is None
        ):
            # The already-sent acquire cannot be cancelled. Retain its source identity,
            # without selecting files, deleting it, or changing any existing asset.
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash=candidate.torrent_hash,
                source_title=candidate.source_title,
                provider_id=provider_id,
                provider_asset_id=provider_asset_id,
                storage_backend=storage_backend,
                rd_torrent_id=provider_asset_id if provider_kind == "realdebrid" else "",
                ownership="monitor_owned",
                provenance="omakase_created",
                created_by_run_id=run_id,
                planned_coverage=candidate.coverage,
                status="queued",
                last_error="WITHDRAWN_DURING_ACQUISITION",
            )
        raise _MonitorWithdrawn()

    def retain_paused(error):
        if adopting or not provider_asset_id:
            return
        previous = db.execute(
            "SELECT id FROM media_assets WHERE monitor_id=? AND torrent_hash=?",
            (monitor_id, candidate.torrent_hash),
        ).fetchone()
        if previous is not None:
            return
        asset_id = candidate_upsert(
            db,
            monitor_id=monitor_id,
            torrent_hash=candidate.torrent_hash,
            source_title=candidate.source_title,
            provider_id=provider_id,
            provider_asset_id=provider_asset_id,
            storage_backend=storage_backend,
            rd_torrent_id=provider_asset_id if provider_kind == "realdebrid" else "",
            ownership="monitor_owned",
            provenance="omakase_created",
            created_by_run_id=run_id,
            planned_coverage=candidate.coverage,
            status="queued",
            last_error="PROVIDER_RATE_LIMITED",
        )
        error.retained_asset_ids += (asset_id,)
        db.commit()

    instant_checked = candidate.seeders == 0 and not adopting
    instant_available = False
    if instant_checked:
        try:
            if heartbeat is not None:
                heartbeat()
            instant_available = await provider_instant_available(rd, candidate.torrent_hash) is True
        except ProviderRateLimit:
            raise
        except Exception:
            instant_available = False
        _guard_withdrawal(db, monitor_id)
        if not instant_available:
            _record_attempt(
                db,
                monitor_id=monitor_id,
                run_id=run_id,
                candidate=candidate,
                searched_aliases=searched_aliases,
                rank=rank,
                decision="NOT_INSTANT",
                reason="ZERO_SEED_NOT_INSTANT",
                rd_outcome="not_instant",
                instant_checked=True,
                instant_available=False,
                now=now,
            )
            return None

    _guard_withdrawal(db, monitor_id)
    if adopting:
        provider_asset_id = existing_item.external_id
    else:
        try:
            if heartbeat is not None:
                heartbeat()
            provider_asset_id = await acquire(rd, candidate.magnet)
        except ProviderBlock:
            _record_attempt(
                db,
                monitor_id=monitor_id,
                run_id=run_id,
                candidate=candidate,
                searched_aliases=searched_aliases,
                rank=rank,
                decision="PROVIDER_BLOCK",
                reason="PROVIDER_BLOCK",
                rd_outcome="provider_block",
                instant_checked=instant_checked,
                instant_available=instant_available,
                now=now,
            )
            return None
        except ProviderRateLimit:
            raise
        except Exception:
            provider_asset_id = None

    guard_retained_source(provider_asset_id)
    if not provider_asset_id:
        _record_attempt(
            db,
            monitor_id=monitor_id,
            run_id=run_id,
            candidate=candidate,
            searched_aliases=searched_aliases,
            rank=rank,
            decision="RD_ERROR",
            reason="ADD_FAILED",
            rd_outcome="add_failed",
            instant_checked=instant_checked,
            instant_available=instant_available,
            now=now,
        )
        return None

    legacy_rd_id = provider_asset_id if provider_kind == "realdebrid" else ""
    selected = True
    if not adopting:
        try:
            if heartbeat is not None:
                heartbeat()
            selected = await select_files(rd, provider_asset_id, "all")
        except ProviderRateLimit as error:
            retain_paused(error)
            raise
        except Exception:
            selected = False
    guard_retained_source(provider_asset_id)
    if not selected:
        asset_id = candidate_upsert(
            db,
            monitor_id=monitor_id,
            torrent_hash=candidate.torrent_hash,
            source_title=candidate.source_title,
            rd_torrent_id=legacy_rd_id,
            provider_id=provider_id,
            provider_asset_id=provider_asset_id,
            storage_backend=storage_backend,
            quality=candidate.quality,
            seeders=candidate.seeders,
            ownership="external" if adopting else "monitor_owned",
            provenance="adopted" if adopting else "omakase_created",
            created_by_run_id=None if adopting else run_id,
            planned_coverage=candidate.coverage,
            status="error",
            last_error="select_failed",
        )
        _refresh_shared_references(db, candidate.torrent_hash)
        _record_attempt(
            db,
            monitor_id=monitor_id,
            run_id=run_id,
            candidate=candidate,
            searched_aliases=searched_aliases,
            rank=rank,
            decision="RD_ERROR",
            reason="SELECT_FAILED",
            rd_outcome="select_failed",
            instant_checked=instant_checked,
            instant_available=instant_available,
            asset_id=asset_id,
            now=now,
        )
        return None

    try:
        if heartbeat is not None:
            heartbeat()
        item = await get_item(rd, provider_asset_id)
    except ProviderRateLimit as error:
        retain_paused(error)
        raise
    except Exception:
        item = None
    guard_retained_source(provider_asset_id)
    if item is None:
        asset_id = candidate_upsert(
            db,
            monitor_id=monitor_id,
            torrent_hash=candidate.torrent_hash,
            source_title=candidate.source_title,
            rd_torrent_id=legacy_rd_id,
            provider_id=provider_id,
            provider_asset_id=provider_asset_id,
            storage_backend=storage_backend,
            quality=candidate.quality,
            seeders=candidate.seeders,
            ownership="external" if adopting else "monitor_owned",
            provenance="adopted" if adopting else "omakase_created",
            created_by_run_id=None if adopting else run_id,
            planned_coverage=candidate.coverage,
            status="error",
            last_error="info_failed",
        )
        _refresh_shared_references(db, candidate.torrent_hash)
        _record_attempt(
            db,
            monitor_id=monitor_id,
            run_id=run_id,
            candidate=candidate,
            searched_aliases=searched_aliases,
            rank=rank,
            decision="RD_ERROR",
            reason="INFO_FAILED",
            rd_outcome="info_failed",
            instant_checked=instant_checked,
            instant_available=instant_available,
            asset_id=asset_id,
            now=now,
        )
        return None

    try:
        access_error = (
            await _download_access_error(rd, provider_asset_id)
            if item.status == "downloaded"
            else ""
        )
    except ProviderRateLimit as error:
        retain_paused(error)
        raise
    guard_retained_source(provider_asset_id)
    if access_error:
        asset_id = candidate_upsert(
            db,
            monitor_id=monitor_id,
            torrent_hash=candidate.torrent_hash,
            source_title=candidate.source_title,
            rd_torrent_id=legacy_rd_id,
            provider_id=provider_id,
            provider_asset_id=provider_asset_id,
            storage_backend=storage_backend,
            source_folder=_provider_source_folder(item),
            ownership="external" if adopting else "monitor_owned",
            provenance="adopted" if adopting else "omakase_created",
            created_by_run_id=None if adopting else run_id,
            planned_coverage=candidate.coverage,
            usable_coverage=(),
            status="quarantined" if access_error == "PROVIDER_DOWNLOAD_BLOCKED" else "error",
            last_error=access_error,
        )
        _record_attempt(
            db,
            monitor_id=monitor_id,
            run_id=run_id,
            candidate=candidate,
            searched_aliases=searched_aliases,
            rank=rank,
            decision="PROVIDER_BLOCK"
            if access_error == "PROVIDER_DOWNLOAD_BLOCKED"
            else "RD_ERROR",
            reason=access_error,
            rd_outcome="download_unavailable",
            instant_checked=instant_checked,
            instant_available=instant_available,
            asset_id=asset_id,
            now=now,
        )
        return None

    stored_requirement = db.execute(
        "SELECT requires_strict_inventory FROM media_assets WHERE monitor_id = ? AND torrent_hash = ?",
        (monitor_id, candidate.torrent_hash),
    ).fetchone()
    strict_required = candidate.metadata_backed or bool(
        stored_requirement and stored_requirement[0]
    )
    inventory = (
        _strict_inventory_coverage(item, monitor, candidate.torrent_hash, candidate.coverage)
        if strict_required
        else (
            _inventory_coverage(item, monitor) if item.status == "downloaded" else EpisodeCoverage()
        )
    )
    usable = inventory.episodes
    source_folder = _provider_source_folder(item)
    normalized_status = {
        "waiting_files_selection": "queued",
        "queued": "queued",
        "downloading": "downloading",
        "downloaded": "downloaded",
    }.get(item.status, "error")
    storage_locator = item.storage_locator or source_folder
    asset_id = candidate_upsert(
        db,
        monitor_id=monitor_id,
        torrent_hash=candidate.torrent_hash,
        source_title=candidate.source_title,
        rd_torrent_id=legacy_rd_id,
        provider_id=provider_id,
        provider_asset_id=provider_asset_id,
        storage_backend=storage_backend,
        storage_locator=storage_locator,
        source_folder=source_folder,
        quality=candidate.quality,
        seeders=candidate.seeders,
        ownership="external" if adopting else "monitor_owned",
        provenance="adopted" if adopting else "omakase_created",
        created_by_run_id=None if adopting else run_id,
        planned_coverage=candidate.coverage,
        usable_coverage=usable,
        numbering_override=inventory.numbering_override,
        inventory_fingerprint=_inventory_fingerprint(item) if usable else "",
        inventory_verified_at=now if usable else None,
        status=normalized_status,
        progress=item.progress,
        last_error="" if usable or normalized_status in _STALLED_STATUSES else "provider_error",
    )
    _refresh_shared_references(db, candidate.torrent_hash)

    if item.status == "downloaded" and usable:
        reason = (
            "BURNED_IN_SUBTITLE_FALLBACK"
            if is_hardsub_release(candidate.source_title)
            else (
                ("EXISTING_RD" if provider_kind == "realdebrid" else "EXISTING_PROVIDER")
                if adopting
                else ("FALLBACK_1080P_EXHAUSTED" if fallback else "")
            )
        )
        _record_attempt(
            db,
            monitor_id=monitor_id,
            run_id=run_id,
            candidate=candidate,
            searched_aliases=searched_aliases,
            rank=rank,
            decision="ADOPTED" if adopting else "ACCEPTED",
            reason=reason,
            rd_outcome="downloaded",
            instant_checked=instant_checked,
            instant_available=instant_available,
            asset_id=asset_id,
            now=now,
        )
        return _asset_contract(
            db,
            asset_id,
            planned=candidate.coverage,
            usable=usable,
        )

    decision = "INELIGIBLE" if item.status == "downloaded" else "PLANNED"
    reason = "INVENTORY_MISMATCH" if item.status == "downloaded" else normalized_status.upper()
    _record_attempt(
        db,
        monitor_id=monitor_id,
        run_id=run_id,
        candidate=candidate,
        searched_aliases=searched_aliases,
        rank=rank,
        decision=decision,
        reason=reason,
        rd_outcome=normalized_status,
        instant_checked=instant_checked,
        instant_available=instant_available,
        asset_id=asset_id,
        now=now,
    )
    return None


def _unique_aliases(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = value.strip()
        key = _alias_key(alias)
        if alias and key not in seen:
            seen.add(key)
            result.append(alias)
    return tuple(result)


def _identity_aliases(monitor: sqlite3.Row) -> tuple[str, ...]:
    return _monitor_aliases(monitor)


def _candidate_rejection(
    monitor: sqlite3.Row,
    candidate: MediaCandidate,
    *,
    allow_hardsub: bool = False,
    policy: AcquisitionPolicy | None = None,
    enforce_seeders: bool = False,
) -> str:
    rejection = candidate_rejection_reason(
        candidate,
        season=int(_monitor_value(monitor, "season_number", 1) or 1),
        target_title=str(_monitor_value(monitor, "title", "")),
        target_aliases=_identity_aliases(monitor),
        anilist_id=int(_monitor_value(monitor, "anilist_id", 0) or 0) or None,
        media_format=str(_monitor_value(monitor, "format", "TV")),
        allow_hardsub=allow_hardsub,
        policy=policy,
        enforce_seeders=enforce_seeders,
    )
    if not rejection and candidate.coverage.isdisjoint(_required_episodes(monitor)):
        return "OUTSIDE_REQUIRED_COVERAGE"
    return rejection


async def reconcile_candidates(
    db: sqlite3.Connection,
    monitor: sqlite3.Row,
    candidates: Sequence[MediaCandidate],
    *,
    rd,
    searched_aliases: Sequence[str],
    now: str,
    max_candidates: int = 20,
    provider_complete: bool = True,
    alias_windows: Sequence[AliasCatalogWindow] | None = None,
    existing_torrents: Mapping[str, ProviderItem | object] | None = None,
    heartbeat: Callable[[], None] | None = None,
    policy: AcquisitionPolicy | None = None,
    language_dispatch: LanguageDispatch | None = None,
    recheck: MediaRecheckIntent | None = None,
) -> MediaSyncResult:
    """Reconcile while honoring durable publication withdrawal at every await."""
    try:
        _guard_withdrawal(db, int(monitor["id"]))
        return await _reconcile_candidates_active(
            db,
            monitor,
            candidates,
            rd=rd,
            searched_aliases=searched_aliases,
            now=now,
            max_candidates=max_candidates,
            provider_complete=provider_complete,
            alias_windows=alias_windows,
            existing_torrents=existing_torrents,
            heartbeat=_withdrawal_heartbeat(db, int(monitor["id"]), heartbeat),
            policy=policy,
            language_dispatch=language_dispatch,
            recheck=recheck,
        )
    except _MonitorWithdrawn:
        _keep_withdrawn_inactive(db, int(monitor["id"]))
        return _withdrawn_result(db, monitor)


async def _reconcile_candidates_active(
    db: sqlite3.Connection,
    monitor: sqlite3.Row,
    candidates: Sequence[MediaCandidate],
    *,
    rd,
    searched_aliases: Sequence[str],
    now: str,
    max_candidates: int = 20,
    provider_complete: bool = True,
    alias_windows: Sequence[AliasCatalogWindow] | None = None,
    existing_torrents: Mapping[str, ProviderItem | object] | None = None,
    heartbeat: Callable[[], None] | None = None,
    policy: AcquisitionPolicy | None = None,
    language_dispatch: LanguageDispatch | None = None,
    recheck: MediaRecheckIntent | None = None,
) -> MediaSyncResult:
    """Try a deterministic candidate window and persist all terminal evidence."""
    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    monitor_id = int(monitor["id"])
    policy = policy or AcquisitionPolicy()
    checks = None
    if recheck is not None:
        from omakase.plus.episode_recheck import targets

        checks = targets(db, monitor, _required_episodes(monitor), policy, recheck)
    if policy.languages is not None:
        from omakase.plus.language_store import CONTRACT_VERSION
        from omakase.plus.media_store import language_request_reuse

        if (
            not isinstance(language_dispatch, LanguageDispatch)
            or language_dispatch.contract_version != CONTRACT_VERSION
        ):
            return _language_result(
                db,
                monitor,
                outcome="language_pipeline_unsupported",
                detail="An immutable language request and supported publication adapter are required. Existing media retained.",
                now=now,
            )
        snapshot, frozen, required_snapshot, status = language_request_reuse(
            db,
            actor_user_id=language_dispatch.actor_user_id,
            request_id=language_dispatch.request_id,
            monitor_id=monitor_id,
            source_generation=language_dispatch.source_generation,
        )
        if frozenset(required_snapshot) != _required_episodes(monitor):
            raise ValueError("Candidate dispatch does not match the immutable episode scope.")
        if frozen != policy:
            raise ValueError("Worker criteria do not match the immutable language request.")
        heartbeat = _language_heartbeat(db, language_dispatch, heartbeat)
    enforce_seeders = capabilities(rd).kind != "realdebrid"
    aliases = _unique_aliases(searched_aliases)
    run_id = record_sync_run(db, monitor_id=monitor_id, outcome="running")
    required = _required_episodes(monitor)
    covered = set(covered_episodes(db, monitor_id))
    upgrade_search = (
        (checks.upgrades - checks.pending) if checks and checks.upgrade_allowed else frozenset()
    )
    acquired_preferred: set[int] = set()
    in_flight = set(checks.downloading) if checks else set()
    in_flight_preferred: set[int] = set()

    def needed(quality=None):
        remaining = (set(required) - covered) - in_flight
        if quality is None or quality == "1080p":
            remaining.update(upgrade_search - acquired_preferred - in_flight_preferred)
        return remaining

    ranked = rank_candidates(tuple(candidates), policy=policy)
    eligible: list[MediaCandidate] = []
    hardsub_fallbacks: list[MediaCandidate] = []

    for rank, candidate in enumerate(ranked, start=1):
        rejection = _candidate_rejection(
            monitor,
            candidate,
            policy=policy,
            enforce_seeders=enforce_seeders,
        )
        if rejection == "HARDSUB_RELEASE":
            fallback_rejection = _candidate_rejection(
                monitor,
                candidate,
                allow_hardsub=True,
                policy=policy,
                enforce_seeders=enforce_seeders,
            )
            if policy.allow_hardsub_fallback and not fallback_rejection:
                hardsub_fallbacks.append(candidate)
                continue
            rejection = fallback_rejection
        if rejection:
            _record_attempt(
                db,
                monitor_id=monitor_id,
                run_id=run_id,
                candidate=candidate,
                searched_aliases=aliases,
                rank=rank,
                decision="INELIGIBLE",
                reason=rejection,
                rd_outcome="not_attempted",
                now=now,
            )
        else:
            eligible.append(candidate)

    candidates_by_quality = {
        quality: [candidate for candidate in eligible if candidate.quality == quality]
        for quality in policy.allowed_resolutions
    }
    accepted: list[MediaAsset] = []
    attempted_1080: set[str] = set()
    attempted_total = 0
    cap_reached = False
    rank_lookup = {candidate.torrent_hash: index for index, candidate in enumerate(ranked, start=1)}

    attempted_hashes: set[str] = set()
    provider_kind = capabilities(rd).kind
    blocked_hashes = {
        str(row["torrent_hash"]).upper()
        for row in db.execute(
            """SELECT DISTINCT torrent_hash
               FROM media_candidate_attempts
               WHERE monitor_id = ?
                 AND provider_kind = ?
                 AND decision = 'PROVIDER_BLOCK'
                 AND created_at >= datetime(?, '-7 days')""",
            (monitor_id, provider_kind, now),
        ).fetchall()
        if row["torrent_hash"]
    }
    skipped_provider_blocks = 0
    blocked_hashes.update(
        str(row["torrent_hash"]).upper()
        for row in db.execute(
            """SELECT torrent_hash FROM media_assets WHERE monitor_id = ?
               AND status = 'quarantined' AND last_error = 'PROVIDER_DOWNLOAD_BLOCKED'
               AND updated_at >= datetime(?, '-7 days')""",
            (monitor_id, now),
        )
    )

    async def attempt_plan(plan: Sequence[MediaCandidate], *, fallback: bool) -> None:
        nonlocal attempted_total, cap_reached, skipped_provider_blocks
        for candidate in plan:
            _guard_withdrawal(db, monitor_id)
            if not needed():
                break
            if candidate.coverage.isdisjoint(needed(candidate.quality)):
                _record_attempt(
                    db,
                    monitor_id=monitor_id,
                    run_id=run_id,
                    candidate=candidate,
                    searched_aliases=aliases,
                    rank=rank_lookup[candidate.torrent_hash],
                    decision="INELIGIBLE",
                    reason="ALREADY_COVERED",
                    rd_outcome="not_attempted",
                    now=now,
                )
                continue
            if candidate.torrent_hash in blocked_hashes:
                skipped_provider_blocks += 1
                attempted_hashes.add(candidate.torrent_hash)
                _record_attempt(
                    db,
                    monitor_id=monitor_id,
                    run_id=run_id,
                    candidate=candidate,
                    searched_aliases=aliases,
                    rank=rank_lookup[candidate.torrent_hash],
                    decision="SKIPPED_PREVIOUS_PROVIDER_BLOCK",
                    reason="RECENT_PROVIDER_BLOCK",
                    rd_outcome="not_attempted",
                    now=now,
                )
                continue
            if attempted_total >= max_candidates:
                cap_reached = True
                _record_attempt(
                    db,
                    monitor_id=monitor_id,
                    run_id=run_id,
                    candidate=candidate,
                    searched_aliases=aliases,
                    rank=rank_lookup[candidate.torrent_hash],
                    decision="SEARCH_CAP_REACHED",
                    reason="SEARCH_CAP_REACHED",
                    rd_outcome="not_attempted",
                    now=now,
                )
                break
            attempted_total += 1
            attempted_hashes.add(candidate.torrent_hash)
            if candidate.quality == "1080p":
                attempted_1080.add(candidate.torrent_hash)
            asset = await _attempt_candidate(
                db,
                monitor,
                candidate,
                rd=rd,
                run_id=run_id,
                rank=rank_lookup[candidate.torrent_hash],
                searched_aliases=aliases,
                now=now,
                fallback=fallback,
                existing_torrent=(existing_torrents or {}).get(candidate.torrent_hash),
                heartbeat=heartbeat,
            )
            _guard_withdrawal(db, monitor_id)
            if asset is not None:
                accepted.append(asset)
                covered.update(asset.usable_coverage)
                if asset.quality == "1080p":
                    acquired_preferred.update(asset.usable_coverage)
            elif checks is not None:
                pending_asset = db.execute(
                    "SELECT status,planned_coverage_json FROM media_assets WHERE monitor_id=? AND torrent_hash=?",
                    (monitor_id, candidate.torrent_hash),
                ).fetchone()
                if pending_asset is not None and pending_asset["status"] in {
                    "requested",
                    "queued",
                    "waiting_files",
                    "downloading",
                }:
                    scope = set(json.loads(pending_asset["planned_coverage_json"])) & required
                    in_flight.update(scope)
                    if candidate.quality == "1080p":
                        in_flight_preferred.update(scope)

    for quality in policy.allowed_resolutions:
        if not needed() or cap_reached:
            break
        if quality == "720p" and not provider_complete:
            continue
        primary = select_episode_cover(needed(quality), candidates_by_quality[quality])
        await attempt_plan(primary, fallback=quality == "720p")
        if not needed() or cap_reached:
            break
        backup = [
            candidate
            for candidate in candidates_by_quality[quality]
            if candidate.torrent_hash not in attempted_hashes
        ]
        await attempt_plan(backup, fallback=quality == "720p")
    if policy.allow_hardsub_fallback:
        for quality in policy.allowed_resolutions:
            if not needed() or cap_reached or not provider_complete:
                break
            hardsub_candidates = [
                candidate for candidate in hardsub_fallbacks if candidate.quality == quality
            ]
            await attempt_plan(
                select_episode_cover(needed(quality), hardsub_candidates),
                fallback=True,
            )
            if not needed() or cap_reached:
                break
            await attempt_plan(
                [
                    candidate
                    for candidate in hardsub_candidates
                    if candidate.torrent_hash not in attempted_hashes
                ],
                fallback=True,
            )

    receipt_windows = (
        tuple(alias_windows)
        if alias_windows is not None
        else tuple(
            AliasCatalogWindow(
                alias=alias,
                candidates=tuple(
                    candidate
                    for candidate in ranked
                    if _alias_key(_candidate_alias(candidate, aliases)) == _alias_key(alias)
                ),
                provider_complete=provider_complete,
            )
            for alias in (aliases or ("direct",))
        )
    )
    for window in receipt_windows:
        alias = window.alias
        alias_candidates = list(window.candidates)
        alias_1080 = [
            candidate
            for candidate in alias_candidates
            if candidate.quality == "1080p"
            and not _candidate_rejection(
                monitor,
                candidate,
                policy=policy,
                enforce_seeders=enforce_seeders,
            )
        ]
        attempted_count = sum(candidate.torrent_hash in attempted_1080 for candidate in alias_1080)
        if cap_reached:
            exhaustion = "SEARCH_CAP_REACHED"
        elif skipped_provider_blocks and window.provider_complete:
            exhaustion = "CONFIGURED_CATALOG_CHECKED_PROVIDER_REJECTED"
        elif window.provider_complete and attempted_count == len(alias_1080):
            exhaustion = "CONFIGURED_CATALOG_CHECKED"
        elif required <= covered:
            exhaustion = "COVERED"
        else:
            exhaustion = "PROVIDER_INCOMPLETE"
        record_alias_search_receipt(
            db,
            monitor_id=monitor_id,
            sync_run_id=run_id,
            query_version=_QUERY_VERSION,
            search_alias=alias,
            started_at=now,
            completed_at=now,
            candidate_count=len(alias_candidates),
            eligible_1080p_count=len(alias_1080),
            attempted_1080p_count=attempted_count,
            cursor="",
            provider_complete=window.provider_complete,
            exhaustion_outcome=exhaustion,
        )

    usable_count = len(required & covered)
    outcome = "complete" if required <= covered else "degraded"
    detail = f"Coverage {usable_count}/{len(required)}; {outcome}"
    if checks is not None:
        detail = f"Episode recheck finished; playable inventory {usable_count}/{len(required)}."
        if checks.upgrades:
            downloads = (checks.preferred_downloading | in_flight_preferred) & checks.upgrades
            pending = checks.pending | (
                (acquired_preferred | in_flight_preferred) & checks.upgrades
            )
            if not checks.upgrade_allowed:
                outcome = "upgrade_blocked"
                detail += " 1080p upgrades are excluded by your download criteria."
            elif pending:
                outcome = "upgrade_pending"
                if downloads:
                    detail += (
                        f" 1080p downloads are in progress for {len(downloads)} fallback episodes."
                    )
                if pending - downloads:
                    detail += f" Higher releases found for {len(pending - downloads)} fallback episodes; replacement publication is unconfirmed."
            else:
                outcome = "upgrade_not_found"
                detail += f" No publishable 1080p replacement found for {len(checks.upgrades)} fallback episodes."
            detail += " Existing usable copies retained."
        if not required <= covered:
            if (required - covered) & in_flight:
                outcome = "download_pending"
                detail += f" Downloads are in progress for {len((required - covered) & in_flight)} missing episodes."
            else:
                outcome = "degraded"
    if skipped_provider_blocks:
        detail += (
            f"; skipped {skipped_provider_blocks} recently rejected provider "
            "candidate(s) and checked deeper results"
        )
    if outcome != "complete":
        provider_blocks = db.execute(
            """SELECT COUNT(*) FROM media_candidate_attempts
               WHERE sync_run_id = ? AND decision = 'PROVIDER_BLOCK'""",
            (run_id,),
        ).fetchone()[0]
        identity_rejections = db.execute(
            """SELECT COUNT(*) FROM media_candidate_attempts
               WHERE sync_run_id = ? AND reason IN (
                   'AMBIGUOUS_TARGET_IDENTITY',
                   'CONFLICTING_SEASON',
                   'TARGET_TITLE_MISMATCH',
                   'WRONG_SEASON'
               )""",
            (run_id,),
        ).fetchone()[0]
        if provider_blocks:
            detail += f"; provider blocked {provider_blocks} candidate(s)"
        if identity_rejections:
            detail += f"; rejected {identity_rejections} identity mismatch(es)"
        if cap_reached:
            detail += f"; search budget {max_candidates} reached"
        if provider_complete:
            detail += "; configured Nyaa catalog checked"
    detail = detail[:500]
    complete_sync_run(
        db,
        sync_run_id=run_id,
        outcome=outcome,
        required_count=len(required),
        covered_count=usable_count,
        accepted_count=len(accepted),
        detail=detail,
        completed_at=now,
    )
    db.execute(
        """UPDATE media_monitors
           SET last_checked_at = ?, last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
               last_result = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (now, int(outcome == "complete"), now, detail, monitor_id),
    )
    planning_id = _monitor_value(monitor, "anilist_planning_id")
    if planning_id is not None:
        latest_rd_id = accepted[-1].rd_torrent_id if accepted else ""
        db.execute(
            """UPDATE anilist_plannings
               SET download_status = ?, download_info = ?,
                   rd_torrent_id = CASE WHEN ? <> '' THEN ? ELSE rd_torrent_id END
               WHERE id = ?""",
            (outcome, detail, latest_rd_id, latest_rd_id, planning_id),
        )
    db.commit()
    return MediaSyncResult(
        monitor_id=monitor_id,
        run_id=run_id,
        outcome=outcome,
        required=required,
        covered=frozenset(covered),
        accepted_assets=tuple(accepted),
        detail=detail,
    )


def _provider_observation_status(status: str) -> str:
    if status in {"waiting_files_selection", "queued"}:
        return "queued"
    if status in {"downloading", "downloaded"}:
        return status
    return "error"


def _excluded_asset_episodes(row: sqlite3.Row) -> frozenset[int]:
    try:
        values = json.loads(row["unusable_coverage_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(values, list):
        return frozenset()
    normalized: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return frozenset()
        normalized.add(value)
    return frozenset(normalized)


async def reprobe_identity_assets(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
    asset_ids: Sequence[int],
    rd,
    now: str,
    lease_owner: str,
    heartbeat: Callable[[], None] | None = None,
) -> tuple[int, ...]:
    """Explicitly recover at most two owned canonical identity quarantines.

    Reuse ordinary refresh identity, inventory and download access validation.
    Unlike ordinary refresh, failed evidence leaves the original quarantine
    untouched. This operation never acquires, selects, deletes or adopts media.
    """
    ids = tuple(asset_ids)
    if not 1 <= len(ids) <= 2 or len(set(ids)) != len(ids):
        raise ValueError("identity reprobe requires one or two distinct assets")
    if not _lease_is_held(db, lease_owner, now):
        raise ValueError("identity reprobe requires the current sync lease")
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE id = ? AND user_id = ?",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None or int(monitor["anilist_id"]) not in {182205, 185874}:
        raise ValueError("identity reprobe requires an owned canonical monitor")
    pending = db.execute(
        """SELECT COUNT(*) FROM media_asset_removals AS removal
           JOIN media_assets AS asset ON asset.id = removal.asset_id
           JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
           WHERE monitor.user_id = ? AND removal.outcome = 'pending'""",
        (user_id,),
    ).fetchone()[0]
    if pending:
        raise ValueError("pending removal blocks identity reprobe")
    if not callable(getattr(rd, "verify_download", None)):
        raise ValueError("identity reprobe requires provider access verification")
    rows = []
    for asset_id in ids:
        row = db.execute(
            "SELECT * FROM media_assets WHERE id = ? AND monitor_id = ?",
            (asset_id, monitor_id),
        ).fetchone()
        if (
            row is None
            or row["status"] != "quarantined"
            or row["last_error"] != "TARGET_TITLE_MISMATCH"
            or row["ownership"] != "monitor_owned"
            or not (row["provider_asset_id"] or row["rd_torrent_id"])
        ):
            raise ValueError("asset is not an owned identity quarantine")
        rows.append(row)
    recovered = []
    for row in rows:
        if heartbeat is not None:
            heartbeat()
        # Preserve the same seven-day provider-block cooldown as acquisition.
        blocked = db.execute(
            """SELECT 1 FROM media_candidate_attempts WHERE monitor_id = ?
               AND torrent_hash = ? AND decision = 'PROVIDER_BLOCK'
               AND created_at >= datetime(?, '-7 days') LIMIT 1""",
            (monitor_id, row["torrent_hash"], now),
        ).fetchone()
        if blocked:
            continue
        aliases = _monitor_aliases(monitor)
        identity = dict(
            target_title=str(monitor["title"]),
            target_aliases=aliases,
            season=int(monitor["season_number"]),
            anilist_id=int(monitor["anilist_id"]),
            media_format=str(monitor["format"]),
        )
        if identity_rejection_reason(str(row["source_title"]), **identity):
            continue
        source = parse_episode_coverage(
            str(row["source_title"]),
            identity["season"],
            expected=monitor["expected_episodes"],
            anilist_id=identity["anilist_id"],
        )
        try:
            planned = frozenset(int(ep) for ep in json.loads(row["planned_coverage_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            len(planned) != 1
            or source.episodes != planned
            or not planned <= _required_episodes(monitor)
        ):
            continue
        external_id = str(row["provider_asset_id"] or row["rd_torrent_id"])
        try:
            item = await get_item(rd, external_id)
        except ProviderRateLimit:
            raise
        except Exception:
            continue
        if (
            item is None
            or item.external_id != external_id
            or item.content_hash.upper() != str(row["torrent_hash"]).upper()
            or item.status != "downloaded"
            or item.progress != 100.0
            or identity_rejection_reason(item.name, **identity)
        ):
            continue
        selected = [file for file in item.files if file.selected]
        if len(selected) != 1 or selected[0].bytes <= 0:
            continue
        filename = PurePosixPath(selected[0].path.replace("\\", "/")).name
        if PurePosixPath(filename).suffix.casefold() not in _MEDIA_SUFFIXES:
            continue
        if identity_rejection_reason(filename, **identity):
            continue
        inventory = _inventory_coverage(item, monitor)
        if inventory.episodes != planned or await _download_access_error(rd, external_id):
            continue
        if heartbeat is not None:
            heartbeat()
        usable = inventory.episodes - _excluded_asset_episodes(row)
        if not usable:
            continue
        # Provider IO and heartbeats precede this short transaction. A stable
        # SQLite snapshot protects final validation through the evidence write;
        # a concurrent writer in WAL mode causes a safe failed write upgrade.
        db.execute("SAVEPOINT identity_asset_reprobe")
        try:
            fresh = db.execute("SELECT * FROM media_assets WHERE id = ?", (row["id"],)).fetchone()
            fresh_monitor = db.execute(
                "SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)
            ).fetchone()
            pending = db.execute(
                """SELECT 1 FROM media_asset_removals AS removal
                   JOIN media_assets AS asset ON asset.id = removal.asset_id
                   JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
                   WHERE monitor.user_id = ? AND removal.outcome = 'pending' LIMIT 1""",
                (user_id,),
            ).fetchone()
            blocked = db.execute(
                """SELECT 1 FROM media_candidate_attempts WHERE monitor_id = ?
                   AND torrent_hash = ? AND decision = 'PROVIDER_BLOCK'
                   AND created_at >= datetime(?, '-7 days') LIMIT 1""",
                (monitor_id, row["torrent_hash"], now),
            ).fetchone()
            if (
                fresh is None
                or tuple(fresh) != tuple(row)
                or fresh_monitor is None
                or tuple(fresh_monitor) != tuple(monitor)
                or pending
                or blocked
                or not _lease_is_held(db, lease_owner, now)
            ):
                raise ValueError("identity reprobe evidence changed before apply")
            provider_id, _, backend = _provider_context(db, monitor, rd)
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash=row["torrent_hash"],
                source_title=row["source_title"],
                provider_id=provider_id,
                storage_backend=backend,
                usable_coverage=usable,
                numbering_override=inventory.numbering_override,
                inventory_fingerprint=_inventory_fingerprint(item),
                inventory_verified_at=now,
                status="downloaded",
                progress=100.0,
                last_error="",
            )
            db.execute("RELEASE SAVEPOINT identity_asset_reprobe")
        except ProviderRateLimit:
            raise
        except Exception:
            db.execute("ROLLBACK TO SAVEPOINT identity_asset_reprobe")
            db.execute("RELEASE SAVEPOINT identity_asset_reprobe")
            raise
        # Commit each verified asset before the next provider call. Earlier
        # successes remain committed if a later asset has stale evidence.
        db.commit()
        recovered.append(int(row["id"]))
    db.commit()
    return tuple(recovered)


async def _refresh_existing_assets(
    db: sqlite3.Connection,
    monitor: sqlite3.Row,
    *,
    rd,
    now: str,
    heartbeat: Callable[[], None] | None = None,
) -> None:
    monitor_id = int(monitor["id"])
    _guard_withdrawal(db, monitor_id)
    provider_id, provider_kind, storage_backend = _provider_context(db, monitor, rd)
    rows = db.execute(
        """SELECT * FROM media_assets
           WHERE monitor_id = ?
             AND (provider_asset_id <> '' OR rd_torrent_id <> '')
           ORDER BY id""",
        (monitor_id,),
    ).fetchall()
    if not rows:
        return
    try:
        if heartbeat is not None:
            heartbeat()
        provider_summaries = await list_items(rd)
    except ProviderRateLimit:
        raise
    except Exception:
        provider_summaries = []
    _guard_withdrawal(db, monitor_id)
    current_by_hash = {
        item.content_hash: item
        for item in provider_summaries
        if item.status == "downloaded" and item.content_hash and item.external_id
    }
    run_id = record_sync_run(db, monitor_id=monitor_id, outcome="refreshing_assets")
    observations: list[tuple[int, str, int, float]] = []
    for row in rows:
        _guard_withdrawal(db, monitor_id)
        # Provider inventory proves that a file exists, not that Plex can read it.
        # Keep curator failures out of coverage until explicitly reprobed; otherwise
        # every refresh revives broken media and rejects replacements as covered.
        if row["status"] == "quarantined":
            if row["last_error"] == "PLEX_MEDIA_PROBE_FAILED":
                if heartbeat is not None:
                    heartbeat()
                error = await _download_access_error(
                    rd, str(row["provider_asset_id"] or row["rd_torrent_id"])
                )
                _guard_withdrawal(db, monitor_id)
                if error == "PROVIDER_DOWNLOAD_BLOCKED":
                    upsert_asset(
                        db,
                        monitor_id=monitor_id,
                        torrent_hash=row["torrent_hash"],
                        source_title=row["source_title"],
                        last_error=error,
                        status="quarantined",
                        usable_coverage=(),
                    )
            continue
        identity_reason = identity_rejection_reason(
            str(row["source_title"]),
            target_title=str(monitor["title"]),
            target_aliases=_monitor_aliases(monitor),
            season=int(_monitor_value(monitor, "season_number", 1) or 1),
            anilist_id=int(_monitor_value(monitor, "anilist_id", 0) or 0) or None,
            media_format=str(_monitor_value(monitor, "format", "TV")),
        )
        if identity_reason:
            try:
                planned = frozenset(
                    int(value) for value in json.loads(row["planned_coverage_json"])
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                planned = frozenset()
            quarantined = MediaCandidate(
                torrent_hash=str(row["torrent_hash"]),
                source_title=str(row["source_title"]),
                magnet="",
                seeders=int(row["seeders"] or 0),
                size_bytes=0,
                quality=str(row["quality"]),
                score=0,
                coverage=planned,
                search_alias=str(monitor["title"]),
            )
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash=row["torrent_hash"],
                source_title=row["source_title"],
                usable_coverage=(),
                numbering_override=None,
                inventory_fingerprint="",
                inventory_verified_at=None,
                status="quarantined",
                last_error=identity_reason,
            )
            _record_attempt(
                db,
                monitor_id=monitor_id,
                run_id=run_id,
                candidate=quarantined,
                searched_aliases=(str(monitor["title"]),),
                rank=0,
                decision="QUARANTINED",
                reason=identity_reason,
                rd_outcome="not_attempted",
                asset_id=int(row["id"]),
                now=now,
            )
            observations.append((int(row["id"]), "error", 0, float(row["progress"] or 0)))
            continue
        external_id = str(row["provider_asset_id"] or row["rd_torrent_id"])
        try:
            if heartbeat is not None:
                heartbeat()
            item = await get_item(rd, external_id)
        except ProviderRateLimit:
            raise
        except Exception:
            item = None
        _guard_withdrawal(db, monitor_id)
        rebound_id = ""
        if item is None:
            current = current_by_hash.get(str(row["torrent_hash"]).upper())
            if current is not None and current.external_id != external_id:
                try:
                    if heartbeat is not None:
                        heartbeat()
                    item = await get_item(rd, current.external_id)
                except ProviderRateLimit:
                    raise
                except Exception:
                    item = None
                _guard_withdrawal(db, monitor_id)
                if item is not None:
                    rebound_id = current.external_id
        if item is None:
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash=row["torrent_hash"],
                source_title=row["source_title"],
                status="error",
                last_error="info_failed",
            )
            observations.append((int(row["id"]), "error", 0, 0.0))
            continue

        status = _provider_observation_status(item.status)
        rebound: dict[str, object] = {}
        if rebound_id:
            rebound["provider_asset_id"] = rebound_id
            if provider_kind == "realdebrid":
                rebound["rd_torrent_id"] = rebound_id
        if item.status == "downloaded":
            # Already verified healthy assets keep their existing evidence. New
            # downloads and failed attempts must prove access before coverage.
            access_error = (
                await _download_access_error(rd, item.external_id)
                if row["status"] != "downloaded" or row["requires_strict_inventory"]
                else ""
            )
            _guard_withdrawal(db, monitor_id)
            if access_error:
                upsert_asset(
                    db,
                    monitor_id=monitor_id,
                    torrent_hash=row["torrent_hash"],
                    source_title=row["source_title"],
                    usable_coverage=(),
                    status="quarantined"
                    if access_error == "PROVIDER_DOWNLOAD_BLOCKED"
                    else "error",
                    last_error=access_error,
                )
                continue
            inventory = (
                _strict_inventory_coverage(
                    item,
                    monitor,
                    str(row["torrent_hash"]),
                    json.loads(row["planned_coverage_json"]),
                )
                if row["requires_strict_inventory"]
                else _inventory_coverage(item, monitor)
            )
            excluded = _excluded_asset_episodes(row)
            usable = inventory.episodes - excluded
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash=row["torrent_hash"],
                source_title=row["source_title"],
                provider_id=provider_id,
                storage_backend=storage_backend,
                storage_locator=item.storage_locator or _provider_source_folder(item),
                source_folder=_provider_source_folder(item),
                usable_coverage=usable,
                numbering_override=inventory.numbering_override,
                inventory_fingerprint=_inventory_fingerprint(item) if usable else "",
                inventory_verified_at=now if usable else None,
                status="downloaded",
                progress=item.progress,
                last_error=(
                    str(row["last_error"]) if excluded else ("" if usable else "inventory_mismatch")
                ),
                **rebound,
            )
        else:
            upsert_asset(
                db,
                monitor_id=monitor_id,
                torrent_hash=row["torrent_hash"],
                source_title=row["source_title"],
                provider_id=provider_id,
                storage_backend=storage_backend,
                status=status,
                progress=item.progress,
                last_error="" if status in _STALLED_STATUSES else "provider_error",
                **rebound,
            )
        observations.append((int(row["id"]), status, item.bytes_done, item.progress))

    refreshed_coverage = covered_episodes(db, monitor_id)
    complete_sync_run(
        db,
        sync_run_id=run_id,
        outcome="refreshed",
        required_count=len(_required_episodes(monitor)),
        covered_count=len(_required_episodes(monitor) & refreshed_coverage),
        accepted_count=0,
        detail=f"Refreshed {len(observations)} provider asset(s)",
        completed_at=now,
    )
    for asset_id, status, bytes_done, progress in observations:
        record_asset_observation(
            db,
            asset_id=asset_id,
            sync_run_id=run_id,
            observed_at=now,
            rd_status=status,
            provider_status=status,
            bytes_done=bytes_done,
            progress=progress,
        )
    db.commit()


def reconcile_curator_blocked_assets(db: sqlite3.Connection, payload: object) -> tuple[int, ...]:
    """Quarantine assets the Plex curator withheld so retryable episodes reopen.

    The curator validates every manifest asset independently (subtitle audit,
    coverage, readability) right before it can publish to Plex. When it
    reports a *retryable* per-asset block, Omakase's own earlier probe judged
    the file usable, so it stays selected forever and silently blocks that
    episode from ever appearing in Plex. Quarantining the asset here removes
    it from ``covered_episodes()``, which reopens the episode for the next
    scheduled or retried sync so a different release can be found. No
    provider media is deleted; a non-retryable block (policy exclusion or a
    destination conflict) is left untouched because searching again cannot
    resolve it.
    """
    from omakase.plus.manifest import validate_curator_receipt

    receipt = validate_curator_receipt(payload)
    if receipt["mode"] != "live" or not receipt["topology_converged"]:
        return ()
    quarantined: list[int] = []
    for blocked in receipt["blocked_assets"]:
        if not blocked["retryable"]:
            continue
        monitor = db.execute(
            """SELECT id FROM media_monitors
               WHERE anilist_id = ? AND season_number IS ?""",
            (blocked["anilist_id"], blocked["season_number"]),
        ).fetchone()
        if monitor is None:
            continue
        marker = f"PLEX_{blocked['code']}"
        asset = db.execute(
            """SELECT id, status, last_error FROM media_assets
               WHERE monitor_id = ? AND torrent_hash = ?""",
            (monitor["id"], blocked["torrent_hash"]),
        ).fetchone()
        if asset is None or asset["status"] not in {"downloaded", "quarantined"}:
            continue
        if asset["status"] == "quarantined" and asset["last_error"] == marker:
            continue
        db.execute(
            """UPDATE media_assets
               SET status = 'quarantined', last_error = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (marker[:500], asset["id"]),
        )
        quarantined.append(int(asset["id"]))
    if quarantined:
        db.commit()
    return tuple(quarantined)


def _monitor_aliases(monitor: sqlite3.Row) -> tuple[str, ...]:
    try:
        raw_aliases = json.loads(monitor["search_titles_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_aliases = []
    values = raw_aliases if isinstance(raw_aliases, list) else []
    aliases = _unique_aliases(
        [str(value) for value in values if isinstance(value, str)] + [str(monitor["title"])]
    )
    return aliases


def _store_catalog_health(
    db: sqlite3.Connection,
    *,
    user_id: int,
    windows: Sequence[AliasCatalogWindow],
    now: str,
) -> None:
    grouped: dict[str, list[CatalogSearchReceipt]] = {}
    for window in windows:
        for receipt in window.source_receipts:
            grouped.setdefault(receipt.catalog_kind, []).append(receipt)
    labels = {
        "nyaa": "Built-in anime source",
        "prowlarr": "Prowlarr expanded search",
        "animetosho": "AnimeTosho release metadata",
    }
    for catalog_kind, receipts in grouped.items():
        failures = sum(receipt.status == "error" for receipt in receipts)
        candidate_count = sum(receipt.candidate_count for receipt in receipts)
        if failures == len(receipts):
            status = "error"
            reason_code = "CATALOG_UNAVAILABLE"
        elif failures:
            status = "degraded"
            reason_code = "PARTIAL_CATALOG_ERROR"
        else:
            status = "healthy"
            reason_code = "READY"
        searches = len(receipts)
        noun = "search" if searches == 1 else "searches"
        detail = f"Completed {searches} {noun}; found {candidate_count} release results"
        if failures:
            detail += f"; {failures} failed"
        update_catalog_health(
            db,
            user_id=user_id,
            catalog_kind=catalog_kind,
            label=labels.get(catalog_kind, catalog_kind.replace("_", " ").title()),
            status=status,
            reason_code=reason_code,
            detail=detail,
            candidate_count=candidate_count,
            checked_at=now,
        )


async def reconcile_monitor(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    rd,
    now: str,
    max_candidates: int = 20,
    heartbeat: Callable[[], None] | None = None,
    language_request_id: str | None = None,
    language_actor_user_id: int | None = None,
    language_source_generation: str | None = None,
    language_contract_version: str | None = None,
    recheck: MediaRecheckIntent | None = None,
) -> MediaSyncResult:
    """Reconcile while honoring durable publication withdrawal at every await."""
    monitor = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    if monitor is None:
        raise ValueError("media monitor not found")
    try:
        _guard_withdrawal(db, int(monitor["id"]))
        if language_request_id is not None:
            return await reconcile_language_request(
                db,
                monitor_id=monitor_id,
                rd=rd,
                now=now,
                request_id=language_request_id,
                actor_user_id=language_actor_user_id,
                source_generation=language_source_generation,
                contract_version=language_contract_version,
                max_candidates=max_candidates,
                heartbeat=heartbeat,
            )
        return await _reconcile_monitor_active(
            db,
            monitor_id=monitor_id,
            rd=rd,
            now=now,
            max_candidates=max_candidates,
            heartbeat=_withdrawal_heartbeat(db, monitor_id, heartbeat),
            recheck=recheck,
        )
    except ProviderRateLimit as error:
        return replace(
            _language_result(
                db, monitor, outcome="provider_rate_limited", detail=error.detail, now=now
            ),
            retry_after_seconds=error.retry_after_seconds,
        )
    except _MonitorWithdrawn:
        _keep_withdrawn_inactive(db, int(monitor["id"]))
        return _withdrawn_result(db, monitor)


async def _reconcile_monitor_active(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    rd,
    now: str,
    max_candidates: int = 20,
    heartbeat: Callable[[], None] | None = None,
    policy_override: AcquisitionPolicy | None = None,
    language_dispatch: LanguageDispatch | None = None,
    recheck: MediaRecheckIntent | None = None,
) -> MediaSyncResult:
    """Refresh provider assets, search every alias, and reconcile one monitor."""
    monitor = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    if monitor is None:
        raise ValueError("media monitor not found")
    policy = policy_override or load_user_acquisition_policy(db, int(monitor["user_id"]))
    legacy_note = False
    if policy.languages is not None and language_dispatch is None:
        if _legacy_language_monitor(db, monitor_id) and not (recheck and recheck.upgrade_to_1080):
            policy = replace(policy, languages=None)
            legacy_note = True
        else:
            return _language_result(
                db,
                monitor,
                outcome="language_request_required",
                detail="Create an explicit immutable language request before acquisition. Existing media retained.",
                now=now,
            )
    await _refresh_existing_assets(db, monitor, rd=rd, now=now, heartbeat=heartbeat)

    _guard_withdrawal(db, monitor_id)
    aliases = _monitor_aliases(monitor)
    monitor_user_id = int(monitor["user_id"])
    catalog_configuration = configured_catalogs(db, monitor_user_id)
    # Metadata discovery is for unowned releases, never a route around an
    # existing asset/quarantine or the normal seven-day provider cooldown.
    metadata_excluded_hashes = frozenset(
        str(row[0]).upper()
        for row in db.execute(
            """SELECT torrent_hash FROM media_assets WHERE monitor_id = ?
           UNION SELECT torrent_hash FROM media_candidate_attempts
           WHERE monitor_id = ? AND provider_kind = ? AND decision = 'PROVIDER_BLOCK'
             AND created_at >= datetime(?, '-7 days')""",
            (monitor_id, monitor_id, capabilities(rd).kind, now),
        )
        if row[0]
    )
    search_episodes = _required_episodes(monitor) - covered_episodes(db, monitor_id)
    if recheck is not None:
        from omakase.plus.episode_recheck import targets

        search_episodes = targets(db, monitor, _required_episodes(monitor), policy, recheck).search
    windows = await search_alias_windows(
        aliases,
        season=int(_monitor_value(monitor, "season_number", 1) or 1),
        expected=_monitor_value(monitor, "expected_episodes"),
        anilist_id=int(_monitor_value(monitor, "anilist_id", 0) or 0) or None,
        heartbeat=heartbeat,
        additional_catalogs=catalog_configuration.sources,
        policy=policy,
        missing_episodes=sorted(search_episodes),
        media_format=str(monitor["format"]),
        metadata_excluded_hashes=metadata_excluded_hashes,
    )
    _guard_withdrawal(db, monitor_id)
    _store_catalog_health(
        db,
        user_id=int(monitor["user_id"]),
        windows=windows,
        now=now,
    )
    candidates = merge_alias_windows(windows, policy=policy)
    try:
        if heartbeat is not None:
            heartbeat()
        existing_items = await list_items(rd)
    except ProviderRateLimit:
        raise
    except Exception:
        existing_items = []
    _guard_withdrawal(db, monitor_id)
    existing_torrents = {
        item.content_hash: item
        for item in existing_items
        if item.status == "downloaded" and item.content_hash
    }
    existing_hashes = {
        str(row["torrent_hash"])
        for row in db.execute(
            "SELECT torrent_hash FROM media_assets WHERE monitor_id = ?", (monitor_id,)
        )
    }
    candidates = tuple(
        candidate for candidate in candidates if candidate.torrent_hash not in existing_hashes
    )
    result = await reconcile_candidates(
        db,
        monitor,
        candidates,
        rd=rd,
        searched_aliases=tuple(window.alias for window in windows) or aliases,
        now=now,
        max_candidates=max_candidates,
        provider_complete=all(window.provider_complete for window in windows),
        alias_windows=windows,
        existing_torrents=existing_torrents,
        heartbeat=heartbeat,
        policy=policy,
        language_dispatch=language_dispatch,
        **({"recheck": recheck} if recheck is not None else {}),
    )
    _guard_withdrawal(db, monitor_id)
    if language_dispatch is None and recheck is None:
        plan_stalled_asset_removals(db, monitor_id=monitor_id, now=now)
    if legacy_note:
        result = replace(
            result,
            detail=(
                result.detail
                + "; Existing monitor retains legacy English publication. New language criteria require a separate request."
            )[:500],
        )
    return result


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _removal_evidence(
    db: sqlite3.Connection, source: sqlite3.Row, now: str
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row] | None:
    if (
        source["ownership"] != "monitor_owned"
        or source["provenance"] != "omakase_created"
        or source["downloaded_once"] != 0
        or source["shared_reference_count"] != 0
        or source["status"] not in _STALLED_STATUSES
    ):
        return None
    observations = db.execute(
        """SELECT observation.*, run.completed_at AS run_completed_at
           FROM media_asset_observations AS observation
           JOIN media_sync_runs AS run ON run.id = observation.sync_run_id
           WHERE observation.asset_id = ? AND run.completed_at IS NOT NULL
           ORDER BY observation.observed_at DESC, observation.id DESC
           LIMIT 2""",
        (source["id"],),
    ).fetchall()
    if len(observations) != 2:
        return None
    newest, oldest = observations
    if (
        oldest["provider_status"] not in _STALLED_STATUSES
        or newest["provider_status"] not in _STALLED_STATUSES
        or oldest["bytes_done"] != newest["bytes_done"]
        or oldest["progress"] != newest["progress"]
        or (_parse_utc(newest["observed_at"]) - _parse_utc(oldest["observed_at"])).total_seconds()
        < 4 * 60 * 60
    ):
        return None
    replacement = db.execute(
        """SELECT * FROM media_assets
           WHERE monitor_id = ? AND replaces_asset_id = ?
             AND status = 'downloaded' AND downloaded_once = 1
             AND inventory_verified_at IS NOT NULL
             AND json_valid(usable_coverage_json) = 1
             AND json_array_length(usable_coverage_json) > 0
           ORDER BY id DESC LIMIT 1""",
        (source["monitor_id"], source["id"]),
    ).fetchone()
    if replacement is None:
        return None
    attempt = db.execute(
        """SELECT attempt.*
           FROM media_candidate_attempts AS attempt
           JOIN media_sync_runs AS run ON run.id = attempt.sync_run_id
           WHERE attempt.monitor_id = ? AND attempt.asset_id = ?
             AND attempt.decision = 'ACCEPTED' AND attempt.terminal_at IS NOT NULL
             AND run.completed_at IS NOT NULL
           ORDER BY attempt.id DESC LIMIT 1""",
        (source["monitor_id"], replacement["id"]),
    ).fetchone()
    if attempt is None:
        return None
    if _parse_utc(replacement["inventory_verified_at"]) > _parse_utc(now):
        return None
    return replacement, attempt, oldest, newest


def plan_stalled_asset_removals(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    now: str,
) -> tuple[int, ...]:
    """Commit pending removal receipts; never call RD from the decision run."""
    sources = db.execute(
        "SELECT * FROM media_assets WHERE monitor_id = ? ORDER BY id", (monitor_id,)
    ).fetchall()
    evidence: list[tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row, sqlite3.Row]] = []
    for source in sources:
        found = _removal_evidence(db, source, now)
        if found is not None:
            evidence.append((source, *found))
    if not evidence:
        return ()

    run_id = record_sync_run(db, monitor_id=monitor_id, outcome="planning_removals")
    complete_sync_run(
        db,
        sync_run_id=run_id,
        outcome="removal_planned",
        required_count=0,
        covered_count=0,
        accepted_count=len(evidence),
        detail=f"Planned {len(evidence)} guarded removal(s)",
        completed_at=now,
    )
    removal_ids: list[int] = []
    for source, replacement, attempt, oldest, newest in evidence:
        operation_key = hashlib.sha256(
            f"{source['id']}:{replacement['id']}:{attempt['id']}:{newest['id']}".encode()
        ).hexdigest()
        removal_ids.append(
            record_asset_removal(
                db,
                operation_key=operation_key,
                asset_id=int(source["id"]),
                replacement_asset_id=int(replacement["id"]),
                candidate_attempt_id=int(attempt["id"]),
                sync_run_id=run_id,
                support_observation_oldest_id=int(oldest["id"]),
                support_observation_newest_id=int(newest["id"]),
                requested_at=now,
                outcome="pending",
                detail="guarded stalled replacement",
            )
        )
    db.commit()
    return tuple(removal_ids)


def _lease_is_held(db: sqlite3.Connection, lease_owner: str, now: str) -> bool:
    row = db.execute("SELECT owner, expires_at FROM media_sync_lease WHERE id = 1").fetchone()
    return bool(row and row["owner"] == lease_owner and row["expires_at"] >= now)


async def process_pending_asset_removals(
    db: sqlite3.Connection,
    *,
    rd,
    lease_owner: str,
    now: str,
    monitor_id: int | None = None,
) -> int:
    """Idempotently execute already-committed pending removals under the DB lease."""
    if not _lease_is_held(db, lease_owner, now):
        return 0
    rows = db.execute(
        """SELECT removal.*, asset.provider_asset_id, asset.rd_torrent_id
           FROM media_asset_removals AS removal
           JOIN media_assets AS asset ON asset.id = removal.asset_id
           WHERE removal.outcome = 'pending'
             AND (? IS NULL OR asset.monitor_id = ?)
           ORDER BY removal.id""",
        (monitor_id, monitor_id),
    ).fetchall()
    processed = 0
    for removal in rows:
        source_monitor = db.execute(
            "SELECT monitor_id FROM media_assets WHERE id = ?", (removal["asset_id"],)
        ).fetchone()
        if source_monitor is None or _monitor_withdrawn(db, int(source_monitor["monitor_id"])):
            continue
        if not capabilities(rd).deletion:
            db.execute(
                """UPDATE media_asset_removals
                   SET outcome = 'failed', completed_at = ?, detail = ?
                   WHERE id = ? AND outcome = 'pending'""",
                (now, "provider deletion capability is disabled", removal["id"]),
            )
            processed += 1
            continue
        source = db.execute(
            "SELECT * FROM media_assets WHERE id = ?", (removal["asset_id"],)
        ).fetchone()
        evidence = _removal_evidence(db, source, now) if source is not None else None
        if (
            evidence is None
            or int(evidence[0]["id"]) != int(removal["replacement_asset_id"])
            or int(evidence[1]["id"]) != int(removal["candidate_attempt_id"])
            or int(evidence[2]["id"]) != int(removal["support_observation_oldest_id"])
            or int(evidence[3]["id"]) != int(removal["support_observation_newest_id"])
        ):
            db.execute(
                """UPDATE media_asset_removals
                   SET outcome = 'failed', completed_at = ?, detail = ?
                   WHERE id = ? AND outcome = 'pending'""",
                (now, "guard revalidation failed", removal["id"]),
            )
            processed += 1
            continue
        external_id = str(removal["provider_asset_id"] or removal["rd_torrent_id"])
        try:
            item = await get_item(rd, external_id)
            if _monitor_withdrawn(db, int(source_monitor["monitor_id"])):
                continue
            deleted = item is None or await delete_item(rd, external_id)
        except ProviderRateLimit:
            raise
        except Exception:
            deleted = False
        db.execute(
            """UPDATE media_asset_removals
               SET outcome = ?, completed_at = ?, detail = ?
               WHERE id = ? AND outcome = 'pending'""",
            (
                "deleted" if deleted else "failed",
                now,
                "provider item absent or deleted" if deleted else "provider deletion failed",
                removal["id"],
            ),
        )
        processed += 1
    db.commit()
    return processed


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_now() -> str:
    return _format_utc(datetime.now(timezone.utc))


def _plus_hours(now: str, hours: int) -> str:
    return _format_utc(_parse_utc(now) + timedelta(hours=hours))


def _plus_minutes(now: str, minutes: int) -> str:
    return _format_utc(_parse_utc(now) + timedelta(minutes=minutes))


def _airing_check_at(now: str, next_airing_at: int | None, *, complete: bool) -> str:
    """Schedule near an announced airtime without allowing a stale timestamp."""
    if next_airing_at:
        announced = datetime.fromtimestamp(next_airing_at, tz=timezone.utc) + timedelta(minutes=20)
        if announced > _parse_utc(now):
            announced_at = _format_utc(announced)
            return announced_at if complete else min(announced_at, _plus_minutes(now, 45))
    return _plus_hours(now, 4) if complete else _plus_minutes(now, 45)


def failure_backoff_hours(consecutive_failures: int) -> int:
    if consecutive_failures <= 0:
        return 4
    return min(48, 4 * (2 ** min(consecutive_failures - 1, 4)))


def _schedule_monitor_failure(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    now: str,
    detail: str,
) -> None:
    if _keep_withdrawn_inactive(db, monitor_id):
        return
    row = db.execute(
        "SELECT consecutive_failures FROM media_monitors WHERE id = ?", (monitor_id,)
    ).fetchone()
    if row is None:
        raise ValueError("media monitor not found")
    failures = int(row["consecutive_failures"] or 0) + 1
    db.execute(
        """UPDATE media_monitors
           SET active = 1, consecutive_failures = ?, next_check_at = ?,
               last_checked_at = ?, last_result = ?, updated_at = datetime('now')
           WHERE id = ?
             AND NOT EXISTS (SELECT 1 FROM media_title_removals r WHERE r.monitor_id = media_monitors.id)
             AND NOT EXISTS (SELECT 1 FROM completion_plex_cleanup_blocks b WHERE b.monitor_id = media_monitors.id)""",
        (
            failures,
            _plus_hours(now, failure_backoff_hours(failures)),
            now,
            detail[:500],
            monitor_id,
        ),
    )


def schedule_monitor(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    now: str,
    sync_result: MediaSyncResult,
) -> None:
    """Apply completion, airing cadence, or bounded failure scheduling."""
    if _keep_withdrawn_inactive(db, monitor_id):
        return
    row = db.execute(
        """SELECT release_status, next_airing_at, consecutive_failures
           FROM media_monitors WHERE id = ?""",
        (monitor_id,),
    ).fetchone()
    if row is None:
        raise ValueError("media monitor not found")
    finished = row["release_status"] == "FINISHED"
    complete = sync_result.required <= sync_result.covered
    if finished and complete:
        active = 0
        next_check_at = None
        failures = 0
    elif finished:
        active = 1
        next_check_at = _plus_hours(now, 12)
        failures = 0
    elif sync_result.outcome == "complete" or sync_result.covered:
        active = 1
        next_check_at = _airing_check_at(
            now,
            row["next_airing_at"],
            complete=complete,
        )
        failures = 0
    else:
        active = 1
        next_check_at = _airing_check_at(
            now,
            row["next_airing_at"],
            complete=False,
        )
        failures = min(int(row["consecutive_failures"] or 0) + 1, 96)
    db.execute(
        """UPDATE media_monitors
           SET active = ?, consecutive_failures = ?, next_check_at = ?,
               last_checked_at = ?, last_success_at = ?, last_result = ?,
               updated_at = datetime('now')
           WHERE id = ?
             AND NOT EXISTS (SELECT 1 FROM media_title_removals r WHERE r.monitor_id = media_monitors.id)
             AND NOT EXISTS (SELECT 1 FROM completion_plex_cleanup_blocks b WHERE b.monitor_id = media_monitors.id)""",
        (
            active,
            failures,
            next_check_at,
            now,
            now,
            sync_result.detail[:500],
            monitor_id,
        ),
    )


def _selected_monitors(
    db: sqlite3.Connection,
    *,
    now: str,
    monitor_ids: Sequence[int] | None,
    include_finished_inactive: bool = False,
) -> list[sqlite3.Row]:
    if monitor_ids is None:
        return [
            row
            for row in list_due_monitors(db, now=now)
            if not _monitor_withdrawn(db, int(row["id"]))
        ]
    normalized = tuple(dict.fromkeys(int(value) for value in monitor_ids if int(value) > 0))
    if not normalized:
        return []
    placeholders = ",".join("?" for _ in normalized)
    return list(
        db.execute(
            f"""SELECT * FROM media_monitors WHERE (active = 1 OR (? = 1 AND release_status = 'FINISHED')) AND id IN ({placeholders})
                AND NOT EXISTS (SELECT 1 FROM media_title_removals r WHERE r.monitor_id = media_monitors.id)
                AND NOT EXISTS (SELECT 1 FROM completion_plex_cleanup_blocks b WHERE b.monitor_id = media_monitors.id)
                ORDER BY id""",
            (int(include_finished_inactive), *normalized),
        ).fetchall()
    )


async def run_media_sync(
    db: sqlite3.Connection,
    *,
    entrypoint: str,
    monitor_ids: Sequence[int] | None = None,
    now: str | None = None,
    lease_owner: str | None = None,
    identity_resolver: Callable[[str, str], object] | None = None,
    provider_factory: Callable[[int], object] | None = None,
    rd_factory: Callable[[int], object] | None = None,
    recheck: MediaRecheckIntent | None = None,
) -> SchedulerReceipt:
    """Shared lease-acquiring entrypoint for every immediate and scheduled sync."""
    if recheck is not None:
        if (
            not isinstance(recheck, MediaRecheckIntent)
            or monitor_ids is None
            or tuple(monitor_ids) != (recheck.monitor_id,)
        ):
            raise ValueError("A recheck must select exactly its single authorized title.")
        owned = db.execute(
            "SELECT user_id,active,release_status,format FROM media_monitors WHERE id=?",
            (recheck.monitor_id,),
        ).fetchone()
        if owned is None or owned["user_id"] != recheck.actor_user_id:
            raise ValueError("A recheck must belong to its requesting owner.")
        if recheck.upgrade_to_1080 and owned["format"] == "MOVIE":
            raise ValueError("Episode upgrade intent is not a movie acquisition policy.")
        if _monitor_withdrawn(db, recheck.monitor_id) or (
            not owned["active"] and owned["release_status"] != "FINISHED"
        ):
            return SchedulerReceipt(
                outcome="not_eligible", monitor_results=(), recovered_owner_hash=""
            )
        if recheck.upgrade_to_1080:
            from omakase.plus.language_publication import LANGUAGE_PUBLICATION_QUALIFIED
            from omakase.plus.quality_publication import QUALITY_PUBLICATION_QUALIFIED

            if not (QUALITY_PUBLICATION_QUALIFIED and LANGUAGE_PUBLICATION_QUALIFIED):
                return SchedulerReceipt(
                    outcome="not_qualified", monitor_results=(), recovered_owner_hash=""
                )
    fixed_now = now
    started_at = fixed_now or _utc_now()
    owner = lease_owner or secrets.token_urlsafe(32)
    lease = acquire_sync_lease(
        db,
        owner=owner,
        entrypoint=entrypoint,
        acquired_at=started_at,
        expires_at=_format_utc(_parse_utc(started_at) + timedelta(minutes=30)),
    )
    db.commit()
    if not lease.acquired:
        return SchedulerReceipt(
            outcome="already_running",
            monitor_results=(),
            recovered_owner_hash="",
        )

    def heartbeat() -> None:
        heartbeat_at = fixed_now or _utc_now()
        expires_at = _format_utc(_parse_utc(heartbeat_at) + timedelta(minutes=30))
        if not heartbeat_sync_lease(
            db,
            owner=owner,
            heartbeat_at=heartbeat_at,
            expires_at=expires_at,
        ):
            raise RuntimeError("media sync lease lost")
        db.commit()

    if identity_resolver is None:
        from omakase.plus.direct import resolve_direct_request

        identity_resolver = resolve_direct_request

    if provider_factory is not None and rd_factory is not None:
        raise ValueError("provider_factory and legacy rd_factory cannot both be set")
    if provider_factory is None:
        provider_factory = rd_factory
    if provider_factory is None:

        def default_provider_factory(user_id: int):
            from omakase.plus.providers.realdebrid import RealDebridProvider

            rd_key = read_secret(db, user_id, "realdebrid_api_key")
            if not rd_key:
                raise RuntimeError("Real-Debrid API key is not configured")
            return RealDebridProvider(rd_key, **cooldown_hooks(db, user_id))

        provider_factory = default_provider_factory

    results: list[SchedulerMonitorResult] = []
    try:
        selected_monitors = _selected_monitors(
            db,
            now=started_at,
            monitor_ids=monitor_ids,
            include_finished_inactive=recheck is not None,
        )
        health_user_ids = {int(row["user_id"]) for row in selected_monitors}
        if monitor_ids is None:
            health_user_ids.update(
                int(row["user_id"])
                for row in db.execute(
                    """SELECT DISTINCT user_id FROM media_providers
                       WHERE enabled = 1 AND kind = 'realdebrid'"""
                ).fetchall()
            )
        providers_by_user: dict[int, object] = {}
        paused_users = set()
        for health_user_id in sorted(health_user_ids):
            try:
                provider = provider_factory(health_user_id)
            except ProviderRateLimit:
                raise
            except Exception:
                existing_provider = db.execute(
                    """SELECT id FROM media_providers
                       WHERE user_id = ? AND kind = 'realdebrid' AND enabled = 1
                       ORDER BY id LIMIT 1""",
                    (health_user_id,),
                ).fetchone()
                if existing_provider is not None:
                    update_provider_health(
                        db,
                        provider_id=int(existing_provider["id"]),
                        status="error",
                        reason_code="PROVIDER_AUTH_REQUIRED",
                        detail="Real-Debrid connection could not be created",
                        checked_at=started_at,
                    )
                db.commit()
                continue
            providers_by_user[health_user_id] = provider
            try:
                await _refresh_provider_health(
                    db,
                    user_id=health_user_id,
                    provider=provider,
                    checked_at=started_at,
                )
            except ProviderRateLimit:
                paused_users.add(health_user_id)
            db.commit()

        for monitor in selected_monitors:
            monitor_id = int(monitor["id"])
            if int(monitor["user_id"]) in paused_users:
                results.append(
                    SchedulerMonitorResult(
                        monitor_id,
                        "provider_rate_limited",
                        "Provider pause is active; no request sent.",
                    )
                )
                continue
            try:
                heartbeat()
                if _keep_withdrawn_inactive(db, monitor_id):
                    db.commit()
                    results.append(
                        SchedulerMonitorResult(monitor_id, "withdrawn", "Acquisition stopped")
                    )
                    continue
                season_number = monitor["season_number"]
                season_hint = f"Season {season_number}" if season_number else ""
                target = identity_resolver(str(monitor["anilist_id"]), season_hint)
                _guard_withdrawal(db, monitor_id)
                upsert_monitor(
                    db,
                    user_id=int(monitor["user_id"]),
                    anilist_id=int(target.anilist_id),
                    title=str(target.title),
                    search_titles=target.search_titles,
                    genres=target.genres,
                    anilist_planning_id=monitor["anilist_planning_id"],
                    format=target.format,
                    release_status=target.status,
                    season_number=target.season_number,
                    expected_episodes=target.episodes,
                    aired_episodes=target.aired_episodes,
                    next_airing_at=target.next_airing_at,
                )
                db.commit()
                _guard_withdrawal(db, monitor_id)
                monitor_user_id = int(monitor["user_id"])
                provider = providers_by_user.get(monitor_user_id)
                if provider is None:
                    provider = provider_factory(monitor_user_id)
                if recheck is None:
                    await process_pending_asset_removals(
                        db,
                        rd=provider,
                        lease_owner=owner,
                        now=started_at,
                        monitor_id=monitor_id,
                    )
                heartbeat()
                sync_result = await reconcile_monitor(
                    db,
                    monitor_id=monitor_id,
                    rd=provider,
                    now=started_at,
                    heartbeat=heartbeat,
                    **({"recheck": recheck} if recheck is not None else {}),
                )
                if recheck is None:
                    schedule_monitor(
                        db,
                        monitor_id=monitor_id,
                        now=started_at,
                        sync_result=sync_result,
                    )
                db.commit()
                results.append(
                    SchedulerMonitorResult(
                        monitor_id,
                        sync_result.outcome,
                        sync_result.detail,
                    )
                )
            except _MonitorWithdrawn:
                _keep_withdrawn_inactive(db, monitor_id)
                db.commit()
                results.append(
                    SchedulerMonitorResult(monitor_id, "withdrawn", "Acquisition stopped")
                )
            except ProviderRateLimit:
                paused_users.add(int(monitor["user_id"]))
                db.commit()
                results.append(
                    SchedulerMonitorResult(
                        monitor_id, "provider_rate_limited", "Provider requested a pause."
                    )
                )
            except Exception:
                if recheck is None:
                    _schedule_monitor_failure(
                        db, monitor_id=monitor_id, now=started_at, detail="SYNC_FAILED"
                    )
                else:
                    record_sync_run(
                        db,
                        monitor_id=monitor_id,
                        outcome="degraded",
                        detail="SYNC_FAILED",
                        completed_at=started_at,
                    )
                    db.execute(
                        "UPDATE media_monitors SET last_checked_at=?,last_result='SYNC_FAILED' WHERE id=?",
                        (started_at, monitor_id),
                    )
                db.commit()
                results.append(SchedulerMonitorResult(monitor_id, "degraded", "SYNC_FAILED"))
        outcome = "degraded" if any(item.outcome == "degraded" for item in results) else "complete"
        return SchedulerReceipt(
            outcome=outcome,
            monitor_results=tuple(results),
            recovered_owner_hash=lease.recovered_owner_hash,
        )
    finally:
        released_at = fixed_now or _utc_now()
        release_sync_lease(db, owner=owner, released_at=released_at)
        db.commit()


async def reconcile_language_request(
    db,
    *,
    monitor_id,
    rd,
    now,
    request_id,
    actor_user_id,
    source_generation,
    contract_version=None,
    max_candidates=20,
    heartbeat=None,
):
    """Reuse before acquiring; preserve every incompatible working copy."""
    from omakase.plus.language_store import CONTRACT_VERSION, bind_acquired_assets
    from omakase.plus.media_store import language_request_reuse

    monitor = db.execute("SELECT * FROM media_monitors WHERE id=?", (monitor_id,)).fetchone()
    if monitor is None:
        raise ValueError("media monitor not found")
    if not isinstance(source_generation, str) or not re.fullmatch(
        r"[0-9a-f]{64}", source_generation
    ):
        return _language_result(
            db,
            monitor,
            outcome="language_source_unconfirmed",
            detail="Confirm the exact source generation before using a language request.",
            now=now,
        )
    snapshot, policy, required, status = language_request_reuse(
        db,
        actor_user_id=actor_user_id,
        request_id=request_id,
        monitor_id=monitor_id,
        source_generation=source_generation,
    )
    if not required or frozenset(required) != _required_episodes(monitor):
        return _language_result(
            db,
            monitor,
            outcome="language_scope_unsupported",
            detail="The immutable episode scope must match this monitor's exact current acquisition scope. No acquisition attempted.",
            now=now,
            required=required,
        )
    if status["language_coverage_complete"]:
        return _language_result(
            db,
            monitor,
            outcome="language_reused",
            detail="Existing exact multitrack media satisfies this request; no acquisition or stream selection changed.",
            now=now,
            required=required,
        )
    existing = covered_episodes(db, monitor_id) & set(required)
    if existing - set(status["covered_episodes"]):
        return _language_result(
            db,
            monitor,
            outcome=status["state"],
            detail="This copy does not yet confirm the requested languages. Existing media stays available; no replacement attempted.",
            now=now,
            required=required,
        )
    if contract_version != CONTRACT_VERSION:
        return _language_result(
            db,
            monitor,
            outcome="language_pipeline_unsupported",
            detail="The publication adapter does not support this language contract. No acquisition attempted.",
            now=now,
            required=required,
        )
    dispatch = LanguageDispatch(request_id, actor_user_id, source_generation, contract_version)
    check = _language_heartbeat(db, dispatch, heartbeat)
    try:
        check()
        result = await _reconcile_monitor_active(
            db,
            monitor_id=monitor_id,
            rd=rd,
            now=now,
            max_candidates=max_candidates,
            heartbeat=check,
            policy_override=policy,
            language_dispatch=dispatch,
        )
        check()
        bind_acquired_assets(
            db,
            actor_user_id=actor_user_id,
            request_id=request_id,
            asset_ids=[a.id for a in result.accepted_assets],
        )
        if result.accepted_assets:
            result = replace(
                result,
                outcome="language_pending_verification",
                detail="Acquired media awaits exact-file language verification and its approved publication proof.",
            )
        return result
    except ProviderRateLimit as error:
        if error.retained_asset_ids:
            try:
                check()
                bind_acquired_assets(
                    db,
                    actor_user_id=actor_user_id,
                    request_id=request_id,
                    asset_ids=list(error.retained_asset_ids),
                )
            except (Exception, _LanguagePermissionChanged):
                for asset_id in error.retained_asset_ids:
                    db.execute(
                        "UPDATE media_assets SET status='quarantined',last_error='LANGUAGE_CONTRACT_UNCONFIRMED' WHERE id=? AND created_by_run_id IS NOT NULL AND status='queued'",
                        (asset_id,),
                    )
            db.commit()
        raise
    except _LanguagePermissionChanged:
        return _language_result(
            db,
            monitor,
            outcome="language_permissions_changed",
            detail="Request permissions changed; acquisition stopped and existing media retained.",
            now=now,
            required=required,
        )
