"""Deterministic watch-state transitions with no I/O."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from omakase.plus.watch.models import WatchDecision, WatchEntry, WatchEvent, WatchStatus

_EPISODE_COMPLETION_THRESHOLD = 0.85
_UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _validate_timestamp(value: str) -> None:
    try:
        datetime.strptime(value, _UTC_TIMESTAMP_FORMAT)
    except (TypeError, ValueError) as exc:
        raise ValueError("observed_at must be canonical UTC YYYY-MM-DDTHH:MM:SSZ") from exc


def _unchanged(entry: WatchEntry, *, disposition: str, reason: str) -> WatchDecision:
    return WatchDecision(
        entry=entry,
        disposition=disposition,
        reason=reason,
        enqueue_tracker_action=None,
    )


def reduce_watch_event(entry: WatchEntry, event: WatchEvent, *, observed_at: str) -> WatchDecision:
    """Apply a normalized watch event to an immutable projection."""
    _validate_timestamp(observed_at)

    if entry.last_observed_at is not None and observed_at < entry.last_observed_at:
        return _unchanged(entry, disposition="ignored", reason="out_of_order_observation")
    if event.confidence == "inferred":
        return _unchanged(
            entry,
            disposition="confirmation_required",
            reason="inferred_identity",
        )
    if (
        event.playback_fraction is not None
        and event.playback_fraction < _EPISODE_COMPLETION_THRESHOLD
    ):
        return _unchanged(
            entry,
            disposition="ignored",
            reason="playback_below_completion_threshold",
        )
    if entry.status == "completed" and event.kind not in {"reopened", "correction"}:
        return _unchanged(entry, disposition="ignored", reason="completed_is_sticky")

    current_progress = entry.progress_episodes
    next_progress = current_progress
    next_status: WatchStatus = entry.status
    completed_at = entry.completed_at

    if event.kind == "progress":
        assert event.episode is not None
        if entry.episode_total is not None and event.episode > entry.episode_total:
            raise ValueError("episode cannot exceed episode_total")
        if event.episode <= current_progress:
            return _unchanged(entry, disposition="ignored", reason="progress_not_advanced")
        next_progress = event.episode
        if entry.episode_total is not None and next_progress == entry.episode_total:
            next_status = "completed"
            completed_at = observed_at
        else:
            next_status = "watching"
            completed_at = None
    elif event.kind == "completed":
        if event.episode is not None:
            if entry.episode_total is not None and event.episode > entry.episode_total:
                raise ValueError("episode cannot exceed episode_total")
            next_progress = max(current_progress, event.episode)
        elif entry.episode_total is not None:
            next_progress = entry.episode_total
        next_status = "completed"
        completed_at = observed_at
    elif event.kind in {"correction", "reopened"}:
        assert event.episode is not None
        if entry.episode_total is not None and event.episode > entry.episode_total:
            raise ValueError("episode cannot exceed episode_total")
        next_progress = event.episode
        if event.kind == "correction" and (
            entry.episode_total is not None and next_progress == entry.episode_total
        ):
            next_status = "completed"
            completed_at = observed_at
        else:
            next_status = "watching" if next_progress > 0 else "unwatched"
            completed_at = None

    updated = replace(
        entry,
        status=next_status,
        progress_episodes=next_progress,
        last_source=event.source,
        confidence=event.confidence,
        last_observed_at=observed_at,
        completed_at=completed_at,
        revision=entry.revision + 1,
    )
    tracker_action = "complete" if next_status == "completed" else None
    if tracker_action is None and next_progress > 0:
        tracker_action = "progress"
    return WatchDecision(
        entry=updated,
        disposition="accepted",
        reason="state_updated",
        enqueue_tracker_action=tracker_action,
    )
