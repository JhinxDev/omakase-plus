"""Transactional watch-event ingestion."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from omakase.plus.completion_cleanup import queue_completion_cleanup
from omakase.plus.watch import repository
from omakase.plus.watch.models import WatchEntry, WatchEvent
from omakase.plus.watch.reducer import reduce_watch_event


class WatchServiceError(ValueError):
    """A normalized watch event cannot be applied safely."""


@dataclass(frozen=True)
class WatchIngestResult:
    entry: WatchEntry
    disposition: str
    reason: str
    event_id: int | None
    confirmation_id: int | None = None
    outbox_id: int | None = None
    cleanup: dict[str, object] | None = None


@dataclass(frozen=True)
class WatchConfirmationResult:
    status: str
    confirmation_id: int
    ingest_result: WatchIngestResult | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@contextmanager
def _transaction(db: sqlite3.Connection) -> Iterator[None]:
    nested = db.in_transaction
    if nested:
        db.execute("SAVEPOINT watch_event_ingest")
    else:
        db.execute("BEGIN IMMEDIATE")
    try:
        yield
        if nested:
            db.execute("RELEASE SAVEPOINT watch_event_ingest")
        else:
            db.commit()
    except Exception:
        if nested:
            db.execute("ROLLBACK TO SAVEPOINT watch_event_ingest")
            db.execute("RELEASE SAVEPOINT watch_event_ingest")
        else:
            db.rollback()
        raise


def ingest_watch_event(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
    event: WatchEvent,
    observed_at: str | None = None,
    score_ten: int | None = None,
) -> WatchIngestResult:
    """Persist one idempotent event, projection, confirmation, and sync intent."""
    if score_ten is not None:
        if (
            isinstance(score_ten, bool)
            or not isinstance(score_ten, int)
            or not 1 <= score_ten <= 10
        ):
            raise WatchServiceError("score must be an integer from 1 to 10")
        if event.source != "manual" or event.kind != "completed":
            raise WatchServiceError("score requires manual completion")
    timestamp = observed_at or _utc_now()
    try:
        with _transaction(db):
            entry = repository.ensure_watch_entry(
                db,
                user_id=user_id,
                monitor_id=monitor_id,
                created_at=timestamp,
            )
            duplicate = repository.find_watch_event(
                db,
                user_id=user_id,
                source=event.source,
                event_key=event.event_key,
            )
            if duplicate is not None:
                return WatchIngestResult(
                    entry=entry,
                    disposition="ignored",
                    reason="duplicate_event",
                    event_id=int(duplicate["id"]),
                )

            decision = reduce_watch_event(entry, event, observed_at=timestamp)
            if score_ten is not None and decision.reason == "completed_is_sticky":
                decision = replace(
                    decision,
                    entry=replace(entry, revision=entry.revision + 1, last_observed_at=timestamp),
                    disposition="accepted",
                    reason="completion_score_updated",
                    enqueue_tracker_action="complete",
                )
            if entry.id is None:
                raise RuntimeError("watch entry identity is missing")
            event_id = repository.insert_watch_event(
                db,
                user_id=user_id,
                entry_id=entry.id,
                event=event,
                decision=decision,
                observed_at=timestamp,
                score_ten=score_ten,
            )
            updated_entry = decision.entry
            if decision.disposition == "accepted":
                repository.update_watch_entry(db, previous=entry, updated=updated_entry)

            confirmation_id = None
            if decision.disposition == "confirmation_required":
                episode_label = (
                    f"episode {event.episode}" if event.episode is not None else "this episode"
                )
                confirmation_id = repository.insert_watch_confirmation(
                    db,
                    user_id=user_id,
                    entry_id=entry.id,
                    event_id=event_id,
                    proposed_episode=event.episode,
                    prompt=f"Confirm {episode_label} observed from {event.source.replace('_', ' ')}.",
                )

            outbox_id = None
            if decision.enqueue_tracker_action is not None and event.source != "anilist":
                outbox_id = repository.insert_tracker_outbox(
                    db,
                    user_id=user_id,
                    entry_id=entry.id,
                    event_id=event_id,
                    action=decision.enqueue_tracker_action,
                    progress_episodes=updated_entry.progress_episodes,
                    score_ten=score_ten,
                )
            cleanup = None
            if (
                decision.disposition == "accepted"
                and entry.status != "completed"
                and updated_entry.status == "completed"
                and event.source != "migration"
            ):
                cleanup = queue_completion_cleanup(
                    db,
                    user_id=user_id,
                    monitor_id=monitor_id,
                    requested_at=timestamp,
                )
            return WatchIngestResult(
                entry=updated_entry,
                disposition=decision.disposition,
                reason=decision.reason,
                event_id=event_id,
                confirmation_id=confirmation_id,
                outbox_id=outbox_id,
                cleanup=cleanup,
            )
    except repository.WatchRepositoryError as exc:
        raise WatchServiceError(str(exc)) from exc


def continue_new_episode(
    db: sqlite3.Connection,
    *,
    user_id: int,
    monitor_id: int,
    episode: int,
    expected_revision: int,
    observed_at: str | None = None,
) -> WatchIngestResult:
    """Explicitly continue a completed season against current exact episode evidence."""
    from omakase.plus.media_store import covered_episodes

    if isinstance(episode, bool) or not isinstance(episode, int) or episode <= 0:
        raise WatchServiceError("continuation requires an exact positive episode")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 0
    ):
        raise WatchServiceError("continuation requires the current watch revision")
    timestamp = observed_at or _utc_now()
    try:
        with _transaction(db):
            monitor = db.execute(
                "SELECT * FROM media_monitors WHERE id=? AND user_id=?", (monitor_id, user_id)
            ).fetchone()
            entry = repository.get_watch_entry(db, user_id=user_id, monitor_id=monitor_id)
            if monitor is None or entry is None or entry.id is None:
                raise WatchServiceError("title does not exist for this user")
            key = f"manual:continue:{entry.id}:{expected_revision}:{episode}"
            duplicate = repository.find_watch_event(
                db, user_id=user_id, source="manual", event_key=key
            )
            if duplicate is not None:
                return WatchIngestResult(entry, "ignored", "duplicate_event", int(duplicate["id"]))
            excluded = db.execute(
                """SELECT 1 FROM media_title_removals WHERE monitor_id=?
                   UNION ALL SELECT 1 FROM completion_plex_cleanup_blocks WHERE monitor_id=?
                   LIMIT 1""",
                (monitor_id, monitor_id),
            ).fetchone()
            expected = int(monitor["expected_episodes"] or 0)
            required = (
                expected
                if monitor["release_status"] == "FINISHED"
                else int(monitor["aired_episodes"] or 0)
            )
            if (
                entry.revision != expected_revision
                or entry.status != "completed"
                or not entry.episode_total
                or entry.progress_episodes < entry.episode_total
                or episode != entry.progress_episodes + 1
                or episode > required
                or monitor["format"] == "MOVIE"
                or excluded
                or episode not in covered_episodes(db, monitor_id)
            ):
                raise WatchServiceError("new episode or watch progress changed; refresh this title")
            current_total = expected if expected >= max(required, episode) else None
            reopened = WatchEvent.reopen(
                episode=entry.progress_episodes, event_key=f"{key}:reopen", force=True
            )
            decision = reduce_watch_event(
                replace(entry, episode_total=current_total), reopened, observed_at=timestamp
            )
            if decision.disposition != "accepted":
                raise WatchServiceError("continuation cannot precede the current watch observation")
            repository.insert_watch_event(
                db,
                user_id=user_id,
                entry_id=entry.id,
                event=reopened,
                decision=decision,
                observed_at=timestamp,
                previous_completion=entry,
            )
            repository.update_watch_entry(db, previous=entry, updated=decision.entry)
            # Only the final user progress produces a tracker intent. Existing consent and
            # remote-completion conflict rules still apply to that ordinary outbox action.
            return ingest_watch_event(
                db,
                user_id=user_id,
                monitor_id=monitor_id,
                event=WatchEvent.progress(
                    source="manual", episode=episode, confidence="manual", event_key=key
                ),
                observed_at=timestamp,
            )
    except repository.WatchRepositoryError as exc:
        raise WatchServiceError(str(exc)) from exc


def resolve_watch_confirmation(
    db: sqlite3.Connection,
    *,
    user_id: int,
    confirmation_id: int,
    accepted: bool,
    observed_at: str | None = None,
    episode: int | None = None,
) -> WatchConfirmationResult:
    """Accept or dismiss one owner confirmation in the same transaction as progress."""
    timestamp = observed_at or _utc_now()
    if not isinstance(accepted, bool):
        raise WatchServiceError("accepted must be a boolean")
    try:
        with _transaction(db):
            confirmation = repository.get_pending_confirmation(
                db,
                user_id=user_id,
                confirmation_id=confirmation_id,
            )
            if confirmation is None:
                raise WatchServiceError("watch confirmation does not exist for this user")
            ingest_result = None
            status = "accepted" if accepted else "rejected"
            if accepted:
                proposed = confirmation["proposed_episode"]
                resolved_episode = episode if episode is not None else proposed
                if (
                    isinstance(resolved_episode, bool)
                    or not isinstance(resolved_episode, int)
                    or resolved_episode <= 0
                ):
                    raise WatchServiceError("confirmation requires an exact episode")
                monitor_id = confirmation["monitor_id"]
                if isinstance(monitor_id, bool) or not isinstance(monitor_id, int):
                    raise WatchServiceError("watch confirmation is no longer attached to a title")
                ingest_result = ingest_watch_event(
                    db,
                    user_id=user_id,
                    monitor_id=monitor_id,
                    event=WatchEvent.progress(
                        source="manual",
                        episode=resolved_episode,
                        confidence="manual",
                        event_key=f"manual:confirmation:{confirmation_id}:accepted",
                    ),
                    observed_at=timestamp,
                )
            repository.mark_watch_confirmation_resolved(
                db,
                user_id=user_id,
                confirmation_id=confirmation_id,
                status=status,
                resolved_at=timestamp,
            )
            return WatchConfirmationResult(
                status=status,
                confirmation_id=confirmation_id,
                ingest_result=ingest_result,
            )
    except repository.WatchRepositoryError as exc:
        raise WatchServiceError(str(exc)) from exc
