"""Owner-scoped, bounded repair operations with no implicit deletion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from omakase.plus.media_probe import persist_probe_result, probe_media_file
from omakase.plus.plex import PlexClient
from omakase.plus.storage import configure_root

REMATCH_PLAN_SCHEMA = "media-rematch-plan-v1"


class RepairError(ValueError):
    """A repair is unsafe, unsupported, or outside the user's ownership."""


def quarantine_asset(
    db,
    *,
    user_id: int,
    monitor_id: int,
    asset_id: int,
    reason: str,
) -> dict[str, Any]:
    """Quarantine one exact owned asset without deleting provider or local data."""
    row = _owned_asset(db, user_id=user_id, monitor_id=monitor_id, asset_id=asset_id)
    detail = " ".join(reason.split()).strip()
    if not detail or len(detail) > 200:
        raise RepairError("quarantine reason must be between 1 and 200 characters")
    if row["status"] == "quarantined" and row["last_error"] == detail:
        return {"asset_id": asset_id, "status": "quarantined", "changed": False}
    db.execute(
        """UPDATE media_assets
           SET status = 'quarantined', last_error = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (detail, asset_id),
    )
    db.commit()
    return {"asset_id": asset_id, "status": "quarantined", "changed": True}


def rematch_plan(
    db,
    *,
    user_id: int,
    monitor_id: int,
) -> dict[str, Any]:
    """Build a read-only identity receipt for a later explicit rematch."""
    monitor = db.execute(
        """SELECT id, anilist_id, title, search_titles_json, format,
                  season_number, expected_episodes, aired_episodes
           FROM media_monitors WHERE id = ? AND user_id = ?""",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        raise RepairError("media title does not exist for this user")
    identity = {
        "monitor_id": int(monitor["id"]),
        "anilist_id": int(monitor["anilist_id"]),
        "title": monitor["title"],
        "search_titles": _json_strings(monitor["search_titles_json"]),
        "format": monitor["format"],
        "season_number": monitor["season_number"],
        "expected_episodes": monitor["expected_episodes"],
        "aired_episodes": int(monitor["aired_episodes"]),
    }
    unsigned = {
        "schema_version": REMATCH_PLAN_SCHEMA,
        "identity": identity,
        "proposed_actions": [
            "resolve AniList identity again",
            "compare aliases and season numbering",
            "preview asset coverage changes",
        ],
        "mutation": False,
    }
    return {
        **unsigned,
        "plan_hash": hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def reprobe_asset(
    db,
    *,
    user_id: int,
    monitor_id: int,
    asset_id: int,
    storage_roots: dict[str, Path],
    verified_at: str,
) -> dict[str, Any]:
    """Re-run bounded media inspection for one exact owned local/Zurg file."""
    asset = _owned_asset(db, user_id=user_id, monitor_id=monitor_id, asset_id=asset_id)
    backend = str(asset["storage_backend"] or "")
    configured = storage_roots.get(backend)
    if configured is None:
        raise RepairError("this storage backend has no configured probe root")
    locator = str(asset["storage_locator"] or "")
    if not locator or not Path(locator).suffix:
        probe = db.execute(
            """SELECT file_locator FROM media_file_probes
               WHERE asset_id = ? ORDER BY verified_at DESC, id DESC LIMIT 1""",
            (asset_id,),
        ).fetchone()
        locator = str(probe["file_locator"]) if probe is not None else ""
    fingerprint = str(asset["inventory_fingerprint"] or "")
    if not locator or len(fingerprint) != 64:
        raise RepairError("asset lacks an exact file locator or inventory fingerprint")
    root = configure_root(configured, owner_id=user_id)
    result = probe_media_file(
        root,
        locator,
        inventory_fingerprint=fingerprint,
    )
    persist_probe_result(
        db,
        asset_id=asset_id,
        file_locator=locator,
        verified_at=verified_at,
        result=result,
    )
    db.execute(
        """UPDATE media_assets
           SET status = ?,
               last_error = ?,
               inventory_verified_at = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (
            "downloaded" if result.usable else "quarantined",
            "" if result.usable else result.detail,
            verified_at,
            asset_id,
        ),
    )
    db.commit()
    return {
        "asset_id": asset_id,
        "outcome": result.outcome,
        "subtitle_tracks": len(result.subtitle_tracks),
    }


def rescan_title(
    db,
    *,
    user_id: int,
    monitor_id: int,
    token_reader,
) -> dict[str, Any]:
    """Request one exact Plex section/path scan from stored target mapping."""
    monitor = db.execute(
        "SELECT format FROM media_monitors WHERE id = ? AND user_id = ?",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        raise RepairError("media title does not exist for this user")
    target = db.execute(
        """SELECT * FROM plex_targets
           WHERE user_id = ? AND publication_policy = 'managed'
           ORDER BY id LIMIT 1""",
        (user_id,),
    ).fetchone()
    if target is None:
        raise RepairError("no managed Plex target is configured")
    mapping = _json_object(target["library_map_json"])
    library_kind = "movie" if monitor["format"] == "MOVIE" else "show"
    library = mapping.get(library_kind)
    if (
        not isinstance(library, dict)
        or isinstance(library.get("section_key"), bool)
        or not isinstance(library.get("section_key"), int)
        or library["section_key"] <= 0
        or not isinstance(library.get("path"), str)
    ):
        raise RepairError("managed Plex target lacks an exact library mapping")
    token = token_reader(db, user_id, str(target["token_secret_name"]))
    if not token:
        raise RepairError("managed Plex target credential is unavailable")
    with PlexClient(str(target["base_url"]), token) as client:
        client.scan_section(int(library["section_key"]), path=str(library["path"]))
    return {
        "monitor_id": monitor_id,
        "target_id": int(target["id"]),
        "section_key": int(library["section_key"]),
        "scan": "requested",
    }


def _owned_asset(db, *, user_id: int, monitor_id: int, asset_id: int):
    row = db.execute(
        """SELECT asset.*
           FROM media_assets AS asset
           JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
           WHERE asset.id = ? AND asset.monitor_id = ? AND monitor.user_id = ?""",
        (asset_id, monitor_id, user_id),
    ).fetchone()
    if row is None:
        raise RepairError("media asset does not exist for this title and user")
    return row


def _json_strings(raw: object) -> list[str]:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _json_object(raw: object) -> dict:
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
