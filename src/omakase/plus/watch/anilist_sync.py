"""AniList outbox processing for durable watch progress."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import httpx

from omakase.plus.anilist import (
    AniListWriteError,
    get_authenticated_media_entry,
    save_completed_entry,
    save_progress_entry,
)
from omakase.plus.secrets import read_secret
from omakase.plus.tracker import safe_tracker_detail
from omakase.plus.watch.repository import (
    claim_tracker_outbox,
    get_tracker_outbox_target,
    mark_tracker_outbox_failed,
    mark_tracker_outbox_synced,
)

_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _retry_at(now: str, attempt_count: int) -> str:
    parsed = datetime.strptime(now, _UTC_FORMAT).replace(tzinfo=timezone.utc)
    delay_seconds = min(3600, 30 * (2 ** max(0, attempt_count - 1)))
    return (parsed + timedelta(seconds=delay_seconds)).strftime(_UTC_FORMAT)


def _remote_id(entry: dict[str, object]) -> int:
    value = entry.get("id")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AniListWriteError("AniList returned an invalid list entry id")
    return value


def process_tracker_outbox(
    db: sqlite3.Connection,
    *,
    limit: int = 10,
    now: str | None = None,
    outbox_id: int | None = None,
    attempted_only: bool = False,
) -> dict[str, int]:
    """Lease and process a bounded batch without rolling local progress back."""
    timestamp = now or datetime.now(timezone.utc).strftime(_UTC_FORMAT)
    items = claim_tracker_outbox(
        db, limit=limit, now=timestamp, outbox_id=outbox_id, attempted_only=attempted_only
    )
    counts = {"claimed": len(items), "synced": 0, "failed": 0, "conflict": 0}
    for item in items:
        try:
            target = get_tracker_outbox_target(db, item=item)
            if item.action == "complete" and target["newer_completion_exists"]:
                mark_tracker_outbox_failed(
                    db,
                    item=item,
                    detail="Superseded by a newer completion update",
                    updated_at=timestamp,
                    not_before=None,
                    conflict=True,
                )
                counts["conflict"] += 1
                continue
            anilist_id = int(target["anilist_id"])
            token = read_secret(db, item.user_id, "anilist_oauth_token")
            if not token:
                raise ValueError("AniList connection is missing or expired")
            remote = get_authenticated_media_entry(token, anilist_id)

            if item.action == "complete":
                if (
                    remote is not None
                    and remote.get("status") == "COMPLETED"
                    and item.score_ten is None
                ):
                    remote_entry_id = _remote_id(remote)
                else:
                    result = (
                        save_completed_entry(token, anilist_id, score_ten=item.score_ten)
                        if item.score_ten is not None
                        else save_completed_entry(token, anilist_id)
                    )
                    remote_entry_id = _remote_id(result)
            else:
                remote_progress = int(remote.get("progress", 0)) if remote is not None else 0
                if remote is not None and (
                    remote.get("status") == "COMPLETED" or remote_progress > item.progress_episodes
                ):
                    mark_tracker_outbox_failed(
                        db,
                        item=item,
                        detail="AniList progress is ahead; local progress was preserved",
                        updated_at=timestamp,
                        not_before=None,
                        conflict=True,
                    )
                    counts["conflict"] += 1
                    continue
                if remote is not None and remote_progress == item.progress_episodes:
                    remote_entry_id = _remote_id(remote)
                else:
                    remote_entry_id = _remote_id(
                        save_progress_entry(
                            token,
                            anilist_id,
                            progress=item.progress_episodes,
                        )
                    )

            mark_tracker_outbox_synced(
                db,
                item=item,
                remote_entry_id=remote_entry_id,
                synced_at=timestamp,
            )
            counts["synced"] += 1
        except (AniListWriteError, httpx.HTTPError, ValueError) as exc:
            detail = safe_tracker_detail(exc)
            mark_tracker_outbox_failed(
                db,
                item=item,
                detail=detail or "AniList update failed",
                updated_at=timestamp,
                not_before=_retry_at(timestamp, item.attempt_count),
            )
            counts["failed"] += 1
    return counts
