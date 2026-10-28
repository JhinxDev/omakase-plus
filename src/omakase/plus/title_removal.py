"""Proof-ordered user removal from Plex publication and media providers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import Any

from omakase.plus.manifest import validate_curator_receipt
from omakase.plus.media_store import record_sync_run


class TitleRemovalError(ValueError):
    """A title cannot safely enter the user-requested removal workflow."""


def _timestamp(value: str, field: str) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError) as exc:
        raise TitleRemovalError(f"{field} must be a canonical UTC timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise TitleRemovalError(f"{field} must be a canonical UTC timestamp")
    return value


def _removal_context(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
) -> tuple[sqlite3.Row, list[sqlite3.Row], bool]:
    monitor = db.execute(
        """SELECT id, user_id, anilist_id, season_number, title
           FROM media_monitors WHERE id = ? AND user_id = ?""",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        raise TitleRemovalError("Media title not found")
    shared = (
        db.execute(
            """SELECT 1
               FROM media_monitors AS other
               JOIN media_assets AS asset ON asset.monitor_id = other.id
               WHERE other.id <> ?
                 AND other.anilist_id = ?
                 AND other.season_number IS ?
                 AND NOT EXISTS (SELECT 1 FROM media_title_removals r
                     WHERE r.monitor_id = other.id)
                 AND asset.status = 'downloaded'
                 AND asset.inventory_verified_at IS NOT NULL
                 AND asset.usable_coverage_json <> '[]'
               LIMIT 1""",
            (monitor_id, monitor["anilist_id"], monitor["season_number"]),
        ).fetchone()
        is not None
    )
    assets = db.execute(
        """SELECT asset.id, asset.provider_asset_id, asset.ownership, asset.provenance,
                  asset.shared_reference_count,
                  COALESCE(provider.kind, 'realdebrid') AS provider_kind
           FROM media_assets AS asset
           LEFT JOIN media_providers AS provider ON provider.id = asset.provider_id
           WHERE asset.monitor_id = ?
           ORDER BY asset.id""",
        (monitor_id,),
    ).fetchall()
    return monitor, list(assets), shared


def _household_removal_guard(db, monitor):
    from omakase.plus import households

    if not households.feature_enabled():
        return {"has_shared_references": False, "blocked": False}
    from omakase.plus.household_media import managed_removal_guard

    return managed_removal_guard(
        db,
        operational_owner_id=int(monitor["user_id"]),
        domain="anime",
        canonical_id=str(monitor["anilist_id"]),
        season=int(monitor["season_number"] or 0),
    )


def _household_removal_denied(db, monitor):
    guard = _household_removal_guard(db, monitor)
    return guard["has_shared_references"] or guard["blocked"]


def _provider_deletion_eligible(asset: sqlite3.Row) -> bool:
    return (
        asset["provider_kind"] == "realdebrid"
        and bool(asset["provider_asset_id"])
        and asset["ownership"] == "monitor_owned"
        and asset["provenance"] == "omakase_created"
        and int(asset["shared_reference_count"]) == 0
    )


def title_removal_preview(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
) -> dict[str, Any]:
    """Describe exactly what one title removal may delete and retain."""
    monitor, assets, shared = _removal_context(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
    )
    household_guard = _household_removal_guard(db, monitor)
    eligible_count = sum(_provider_deletion_eligible(asset) for asset in assets)
    existing = db.execute(
        """SELECT id, status, scope, detail FROM media_title_removals
           WHERE monitor_id = ? AND user_id = ? ORDER BY id DESC LIMIT 1""",
        (monitor_id, user_id),
    ).fetchone()
    progress = None
    if existing is not None:
        outcomes = {
            str(row["outcome"]): int(row["total"])
            for row in db.execute(
                """SELECT outcome, COUNT(*) AS total FROM media_title_removal_assets
                   WHERE removal_id = ? GROUP BY outcome""",
                (existing["id"],),
            )
        }
        progress = {
            "status": str(existing["status"]),
            "scope": str(existing["scope"]),
            "detail": str(existing["detail"] or ""),
            "deleted_count": outcomes.get("deleted", 0) + outcomes.get("absent", 0),
            "failed_count": outcomes.get("failed", 0),
            "pending_count": outcomes.get("pending", 0),
            "retained_count": outcomes.get("retained", 0),
        }
    return {
        "monitor_id": int(monitor["id"]),
        "title": str(monitor["title"]),
        "anilist_id": int(monitor["anilist_id"]),
        "season_number": monitor["season_number"],
        "asset_count": len(assets),
        "eligible_provider_count": eligible_count,
        "retained_asset_count": len(assets) - eligible_count,
        "shared_plex_title": shared,
        "household_blocked": household_guard["has_shared_references"] or household_guard["blocked"],
        "progress": progress,
    }


def _bulk_monitor_ids(monitor_ids: object) -> tuple[int, ...]:
    if isinstance(monitor_ids, (str, bytes)):
        raise TitleRemovalError("monitor_ids must be a collection of title ids")
    try:
        submitted = list(monitor_ids)
    except TypeError as exc:
        raise TitleRemovalError("monitor_ids must be a collection of title ids") from exc
    if len(submitted) > 100:
        raise TitleRemovalError("Select no more than 100 completed titles")
    normalized: list[int] = []
    seen: set[int] = set()
    for value in submitted:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TitleRemovalError("Every selected title id must be a positive integer")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def bulk_title_removal_preview(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_ids: object,
) -> dict[str, Any]:
    """Classify completed owner titles through the existing removal preview."""
    normalized_ids = _bulk_monitor_ids(monitor_ids)
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for monitor_id in normalized_ids:
        identity = db.execute(
            """SELECT monitor.title, entry.status AS watch_status
               FROM media_monitors AS monitor
               LEFT JOIN watch_entries AS entry
                 ON entry.monitor_id = monitor.id AND entry.user_id = monitor.user_id
               WHERE monitor.id = ? AND monitor.user_id = ?""",
            (monitor_id, user_id),
        ).fetchone()
        if identity is None:
            blocked.append({"monitor_id": monitor_id, "reason": "Media title not found"})
            continue
        if identity["watch_status"] != "completed":
            blocked.append({"monitor_id": monitor_id, "reason": "Title is not completed"})
            continue
        preview = title_removal_preview(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
        )
        if preview["shared_plex_title"]:
            blocked.append({"monitor_id": monitor_id, "reason": "Shared Plex title"})
            continue
        if preview["eligible_provider_count"] <= 0:
            blocked.append({"monitor_id": monitor_id, "reason": "No eligible Real-Debrid media"})
            continue
        existing = db.execute(
            """SELECT id, status FROM media_title_removals
               WHERE monitor_id = ? AND status <> 'complete'
               ORDER BY id DESC LIMIT 1""",
            (monitor_id,),
        ).fetchone()
        eligible.append(
            {
                **preview,
                "existing_removal_id": int(existing["id"]) if existing is not None else None,
                "existing_removal_status": (
                    str(existing["status"]) if existing is not None else ""
                ),
            }
        )
    eligible_ids = tuple(int(item["monitor_id"]) for item in eligible)
    return {
        "requested_count": len(normalized_ids),
        "requested_ids": normalized_ids,
        "eligible_ids": eligible_ids,
        "eligible": tuple(eligible),
        "blocked": tuple(blocked),
        "confirmation": f"REMOVE {len(eligible_ids)} COMPLETED TITLES",
    }


def request_bulk_title_removal(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_ids: object,
    confirmation: str,
    requested_at: str,
) -> dict[str, Any]:
    """Open independent Plex-first removals and preserve per-title results."""
    requested_at = _timestamp(requested_at, "requested_at")
    preview = bulk_title_removal_preview(
        db,
        user_id=user_id,
        monitor_ids=monitor_ids,
    )
    if not preview["eligible_ids"]:
        raise TitleRemovalError("No eligible completed titles were selected")
    if confirmation.strip() != preview["confirmation"]:
        raise TitleRemovalError(f"Type the exact batch confirmation: {preview['confirmation']}")
    results: list[dict[str, Any]] = []
    requested_count = 0
    failed_count = 0
    for item in preview["eligible"]:
        try:
            receipt = request_title_removal(
                db,
                user_id=user_id,
                monitor_id=int(item["monitor_id"]),
                confirmation=str(item["title"]),
                requested_at=requested_at,
            )
        except TitleRemovalError as exc:
            failed_count += 1
            results.append(
                {
                    "monitor_id": int(item["monitor_id"]),
                    "title": str(item["title"]),
                    "status": "failed",
                    "detail": str(exc)[:240],
                    "idempotent": False,
                }
            )
            continue
        requested_count += 1
        results.append(
            {
                "monitor_id": int(item["monitor_id"]),
                "title": str(item["title"]),
                "removal_id": int(receipt["id"]),
                "status": str(receipt["status"]),
                "detail": "Removal already open" if receipt["idempotent"] else "Removal requested",
                "idempotent": bool(receipt["idempotent"]),
            }
        )
    return {
        "requested": requested_count,
        "failed": failed_count,
        "blocked": preview["blocked"],
        "results": tuple(results),
    }


def request_title_removal(
    db, *, user_id, monitor_id, confirmation, requested_at, scope="plex_and_provider", commit=True
):
    """Existing removal pipeline with a serialized, default-gated shared-resource fence."""
    from omakase.plus import households

    if not households.feature_enabled():
        return _request_title_removal_impl(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            confirmation=confirmation,
            requested_at=requested_at,
            scope=scope,
            commit=commit,
        )
    started = not db.in_transaction
    if started:
        db.execute("BEGIN IMMEDIATE")
    else:
        # Acquire the SQLite writer fence before checking references in an outer batch.
        db.execute(
            "UPDATE media_monitors SET id=id WHERE id=? AND user_id=?", (monitor_id, user_id)
        )
    try:
        monitor, _, _ = _removal_context(db, user_id=user_id, monitor_id=monitor_id)
        if _household_removal_denied(db, monitor):
            raise TitleRemovalError(
                "Shared media must stay available. Remove its library membership instead; physical files and personal history are retained."
            )
        return _request_title_removal_impl(
            db,
            user_id=user_id,
            monitor_id=monitor_id,
            confirmation=confirmation,
            requested_at=requested_at,
            scope=scope,
            commit=commit,
        )
    except Exception:
        if started:
            db.rollback()
        raise


def _request_title_removal_impl(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
    confirmation: str,
    requested_at: str,
    scope: str = "plex_and_provider",
    commit: bool = True,
) -> dict[str, Any]:
    """Unpublish one owner-scoped title and stage provider deletion after Plex proof."""
    requested_at = _timestamp(requested_at, "requested_at")
    monitor, assets, shared = _removal_context(
        db,
        user_id=user_id,
        monitor_id=monitor_id,
    )
    if scope not in {"plex_only", "plex_and_provider"}:
        raise TitleRemovalError("Invalid removal scope")
    title = str(monitor["title"])
    if confirmation.strip() != title:
        raise TitleRemovalError("Type the exact title to confirm removal")
    if shared and scope != "plex_only":
        raise TitleRemovalError("This is a shared Plex title and cannot be removed here")
    existing = db.execute(
        """SELECT id, status, scope FROM media_title_removals
           WHERE monitor_id = ? AND status <> 'complete'""",
        (monitor_id,),
    ).fetchone()
    if existing is not None:
        if existing["scope"] != scope:
            raise TitleRemovalError("A removal with a different scope is already open")
        return {"id": int(existing["id"]), "status": existing["status"], "idempotent": True}

    if not assets and scope != "plex_only":
        raise TitleRemovalError("This title has no media to remove")

    try:
        cursor = db.execute(
            """INSERT INTO media_title_removals
               (user_id, monitor_id, anilist_id, season_number, title, requested_at,
                status, detail, scope)
               VALUES (?, ?, ?, ?, ?, ?, 'awaiting_plex', ?, ?)""",
            (
                user_id,
                monitor_id,
                monitor["anilist_id"],
                monitor["season_number"],
                title,
                requested_at,
                (
                    "Owner publication withdrawn; shared Plex title remains until other owners withdraw"
                    if shared
                    else "Waiting for a converged Plex curator receipt"
                ),
                scope,
            ),
        )
        removal_id = int(cursor.lastrowid)
        for asset in assets:
            eligible = scope == "plex_and_provider" and _provider_deletion_eligible(asset)
            db.execute(
                """INSERT INTO media_title_removal_assets
                   (removal_id, asset_id, provider_kind, provider_asset_id, outcome, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    removal_id,
                    asset["id"],
                    asset["provider_kind"],
                    asset["provider_asset_id"],
                    "pending" if eligible else "retained",
                    (
                        "Eligible Omakase-owned Real-Debrid media"
                        if eligible
                        else (
                            "Provider/source media retained by Plex-only scope"
                            if scope == "plex_only"
                            else "Retained because the asset is not an unshared Omakase-owned RD item"
                        )
                    ),
                ),
            )
        db.execute(
            """UPDATE media_monitors
               SET active = 0, next_check_at = NULL,
                   last_result = 'USER_REMOVAL_AWAITING_PLEX',
                   updated_at = datetime('now')
               WHERE id = ? AND user_id = ?""",
            (monitor_id, user_id),
        )
        if scope == "plex_and_provider":
            db.execute(
                """UPDATE media_assets
               SET status = 'quarantined', last_error = 'USER_REMOVAL_REQUESTED',
                   updated_at = datetime('now')
               WHERE monitor_id = ?""",
                (monitor_id,),
            )
        record_sync_run(
            db,
            monitor_id=monitor_id,
            outcome="removal_requested",
            detail=(
                "Plex-only unpublish; provider/source media retained"
                if scope == "plex_only"
                else "User requested Plex unpublish before provider deletion"
            ),
            completed_at=requested_at,
        )
        if commit:
            db.commit()
    except Exception:
        if commit:
            db.rollback()
        raise
    return {"id": removal_id, "status": "awaiting_plex", "idempotent": False}


def _native_plex_absence_proved(
    db: sqlite3.Connection, removal: sqlite3.Row, receipt: Any
) -> str | None:
    # Import locally: plex_sync ingests watch events, which can request removals.
    from omakase.plus.watch.plex_sync import PlexSnapshotItem, _monitor_matches

    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE id = ?", (removal["monitor_id"],)
    ).fetchone()
    if monitor is None or monitor["format"] not in {
        "TV",
        "TV_SHORT",
        "ONA",
        "OVA",
        "MOVIE",
        "SPECIAL",
    }:
        return None
    season = int(monitor["season_number"] or 0)
    if (monitor["format"] == "MOVIE") != (season == 0):
        return None
    snapshot = db.execute(
        """SELECT * FROM plex_library_snapshots ORDER BY generated_at DESC, id DESC LIMIT 1"""
    ).fetchone()
    if snapshot is None or (
        snapshot["generated_at"] < max(removal["requested_at"], receipt["completed_at"])
        or snapshot["received_at"] < snapshot["generated_at"]
    ):
        return None
    items = db.execute(
        "SELECT * FROM plex_library_items WHERE snapshot_id = ?", (snapshot["id"],)
    ).fetchall()
    if len(items) != snapshot["item_count"]:
        return None
    for item in items:
        candidate = PlexSnapshotItem(
            rating_key=item["rating_key"],
            section=item["section"],
            media_type=item["media_type"],
            title=item["title"],
            season_number=item["season_number"],
            episode_total=item["episode_total"],
            watched_through=item["watched_through"],
            last_viewed_at=item["last_viewed_at"],
            canonical_title=item["canonical_title"],
            managed_paths=tuple(tuple(ref) for ref in json.loads(item["managed_paths_json"]))
            if item["managed_paths_json"] is not None
            else None,
            path_identity_complete=bool(item["path_identity_complete"])
            if item["path_identity_complete"] is not None
            else None,
        )
        if candidate.managed_paths and (monitor["title"], season) in candidate.managed_paths:
            return None
        # Any exact-season native match blocks proof, including section/type ambiguity.
        if _monitor_matches(candidate, monitor):
            return None

        if _monitor_matches(
            replace(
                candidate, canonical_title=None, managed_paths=None, path_identity_complete=None
            ),
            monitor,
        ):
            return None
        expected_type = "movie" if monitor["format"] == "MOVIE" else "show"
        if candidate.media_type == expected_type and candidate.path_identity_complete is not True:
            return None
    return str(snapshot["generated_at"])


def confirm_plex_removals(db: sqlite3.Connection, receipt: Any) -> tuple[int, ...]:
    """Unlock provider deletion only after a later converged receipt omits the title."""
    receipt = validate_curator_receipt(receipt)
    if receipt["mode"] != "live" or not receipt["topology_converged"]:
        return ()
    if any(error["anilist_id"] is None for error in receipt["errors"]):
        return ()
    covered = {
        (int(item["anilist_id"]), item["season_number"]) for item in receipt["title_coverage"]
    }
    errored_ids = {
        int(item["anilist_id"]) for item in receipt["errors"] if item["anilist_id"] is not None
    }
    confirmed: list[int] = []
    rows = db.execute(
        """SELECT * FROM media_title_removals
           WHERE status = 'awaiting_plex' ORDER BY id"""
    ).fetchall()
    for row in rows:
        monitor = db.execute(
            "SELECT id,user_id,anilist_id,season_number FROM media_monitors WHERE id=? AND user_id=?",
            (row["monitor_id"], row["user_id"]),
        ).fetchone()
        if monitor is None or _household_removal_denied(db, monitor):
            continue
        key = (int(row["anilist_id"]), row["season_number"])
        if (
            receipt["manifest_generated_at"] <= row["requested_at"]
            or receipt["completed_at"] < row["requested_at"]
            or key in covered
            or int(row["anilist_id"]) in errored_ids
        ):
            continue
        if row["scope"] == "plex_only":
            native_proved_at = _native_plex_absence_proved(db, row, receipt)
            if native_proved_at is None:
                db.execute(
                    """UPDATE media_title_removals SET detail =
                       'Waiting for a fresh native Plex snapshot proving the exact season/movie absent'
                       WHERE id = ?""",
                    (row["id"],),
                )
                continue
            db.execute(
                """UPDATE media_title_removals
                   SET status = 'complete', curator_run_id = ?, plex_removed_at = ?,
                       completed_at = ?, detail = 'Plex absence proved; provider/source media retained'
                   WHERE id = ? AND status = 'awaiting_plex'""",
                (receipt["run_id"], native_proved_at, native_proved_at, row["id"]),
            )
            db.execute(
                """UPDATE media_monitors SET last_result = 'USER_REMOVAL_COMPLETE'
                   WHERE id = ?""",
                (row["monitor_id"],),
            )
            confirmed.append(int(row["id"]))
            continue
        cursor = db.execute(
            """UPDATE media_title_removals
               SET status = 'plex_removed', curator_run_id = ?, plex_removed_at = ?,
                   detail = 'Plex curator proved the title absent'
               WHERE id = ? AND status = 'awaiting_plex'""",
            (receipt["run_id"], receipt["completed_at"], row["id"]),
        )
        if cursor.rowcount == 1:
            confirmed.append(int(row["id"]))
    db.commit()
    return tuple(confirmed)


async def process_provider_removals(
    db: sqlite3.Connection,
    *,
    provider_factory: Callable[[int], object],
    completed_at: str,
) -> int:
    """Idempotently delete confirmed, eligible RD items and retain all other assets."""
    completed_at = _timestamp(completed_at, "completed_at")
    removals = db.execute(
        """SELECT * FROM media_title_removals
           WHERE status IN ('plex_removed', 'provider_failed') AND scope = 'plex_and_provider'
           ORDER BY id"""
    ).fetchall()
    processed = 0
    providers: dict[int, object] = {}
    for removal in removals:
        monitor = db.execute(
            "SELECT id,user_id,anilist_id,season_number FROM media_monitors WHERE id=? AND user_id=?",
            (removal["monitor_id"], removal["user_id"]),
        ).fetchone()
        if monitor is None or _household_removal_denied(db, monitor):
            continue
        effective_completed_at = max(completed_at, str(removal["plex_removed_at"]))
        user_id = int(removal["user_id"])
        pending = db.execute(
            """SELECT * FROM media_title_removal_assets
               WHERE removal_id = ? AND outcome IN ('pending', 'failed')
               ORDER BY id""",
            (removal["id"],),
        ).fetchall()
        failed = False
        if pending:
            try:
                provider = providers.get(user_id)
                if provider is None:
                    provider = provider_factory(user_id)
                    providers[user_id] = provider
            except Exception:
                provider = None
            for asset in pending:
                if asset["provider_kind"] != "realdebrid" or not asset["provider_asset_id"]:
                    db.execute(
                        """UPDATE media_title_removal_assets
                           SET outcome = 'retained', detail = 'Provider deletion is unsupported'
                           WHERE id = ?""",
                        (asset["id"],),
                    )
                    continue
                if provider is None:
                    failed = True
                    db.execute(
                        """UPDATE media_title_removal_assets
                           SET outcome = 'failed', detail = 'Real-Debrid connection unavailable'
                           WHERE id = ?""",
                        (asset["id"],),
                    )
                    continue
                try:
                    item = await provider.get_item(str(asset["provider_asset_id"]))
                    deleted = item is None or await provider.delete_item(
                        str(asset["provider_asset_id"])
                    )
                except Exception:
                    deleted = False
                    item = object()
                outcome = "absent" if item is None else ("deleted" if deleted else "failed")
                failed = failed or outcome == "failed"
                db.execute(
                    """UPDATE media_title_removal_assets
                       SET outcome = ?, detail = ?
                       WHERE id = ?""",
                    (
                        outcome,
                        (
                            "Provider item absent or deleted"
                            if outcome in {"absent", "deleted"}
                            else "Real-Debrid deletion failed"
                        ),
                        asset["id"],
                    ),
                )
                if outcome in {"absent", "deleted"}:
                    db.execute(
                        """UPDATE media_assets
                           SET status = 'removed', last_error = '', updated_at = datetime('now')
                           WHERE id = ?""",
                        (asset["asset_id"],),
                    )
        remaining = db.execute(
            """SELECT COUNT(*) FROM media_title_removal_assets
               WHERE removal_id = ? AND outcome IN ('pending', 'failed')""",
            (removal["id"],),
        ).fetchone()[0]
        if failed or remaining:
            db.execute(
                """UPDATE media_title_removals
                   SET status = 'provider_failed',
                       detail = 'Plex removal is complete; Real-Debrid deletion will retry'
                   WHERE id = ?""",
                (removal["id"],),
            )
        else:
            db.execute(
                """UPDATE media_title_removals
                   SET status = 'complete', completed_at = ?,
                       detail = 'Removed from Plex and eligible Real-Debrid media deleted'
                   WHERE id = ?""",
                (effective_completed_at, removal["id"]),
            )
            db.execute(
                """UPDATE media_monitors
                   SET last_result = 'USER_REMOVAL_COMPLETE', updated_at = datetime('now')
                   WHERE id = ?""",
                (removal["monitor_id"],),
            )
            record_sync_run(
                db,
                monitor_id=int(removal["monitor_id"]),
                outcome="removal_complete",
                detail="Plex absence proved before eligible provider deletion",
                completed_at=effective_completed_at,
            )
        db.commit()
        processed += 1
    return processed
