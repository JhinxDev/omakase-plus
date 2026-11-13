"""Project server-measured playback into exact, personal viewing evidence."""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone

from omakase.plus import households as core
from omakase.plus.watch import repository
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.reducer import reduce_watch_event
from omakase.plus.watch.service import ingest_watch_event
from omakase.plus.web_playback_store import watch_barrier


def observe_playback(db, *, source, state, now=None):
    """Called inside the authenticated event transaction, never on a read/seek.

    Only server-accepted playing intervals are evidence. Manual corrections fence
    old sessions; a fresh explicit session accumulates a separate completion union.
    """
    core.revalidate_scope(db, source._scope)
    core._require(
        source._scope.viewer_id == source.actor_user_id
        and source._scope.library_id == source.library_id,
        "Playback viewer changed.",
        409,
    )
    covered, duration = state["covered_seconds"], state["duration_seconds"]
    credited = state.get("credited_seconds", 0)
    cycle_covered = state.get("watch_covered_seconds", 0)
    core._require(
        all(
            type(v) in (int, float) and math.isfinite(v)
            for v in (covered, duration, credited, cycle_covered)
        )
        and 0 <= covered <= duration
        and 0 <= cycle_covered <= duration
        and credited >= 0
        and duration > 0,
        "Invalid server playback evidence.",
        422,
    )
    identity = (
        source.actor_user_id,
        source.anilist_id,
        source.season_number,
        source.episode_number,
    )
    previous = db.execute(
        "SELECT * FROM web_playback_watch WHERE user_id=? AND anilist_id=? AND season_number=? AND episode_number=? AND media_version=?",
        (*identity, source.media_version),
    ).fetchone()
    barrier = watch_barrier(
        db,
        user_id=source.actor_user_id,
        anilist_id=source.anilist_id,
        season_number=source.season_number,
    )
    completed = (
        db.execute(
            "SELECT 1 FROM web_playback_completions WHERE user_id=? AND anilist_id=? AND season_number=? AND episode_number=? AND barrier=?",
            (*identity, barrier),
        ).fetchone()
        is not None
    )
    result = {
        "completed": completed,
        "newly_completed": False,
        "event_id": None,
        "progress_episodes": None,
    }
    if credited <= 0 or (previous and state["revision"] < previous["revision"]):
        return result
    # Retries carry the original server acceptance time, so they cannot fabricate
    # later viewing activity even though the saved event receipt is replayable.
    stamp = datetime.fromtimestamp(
        state.get("accepted_at", time.time() if now is None else now), timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    newly = (
        not completed and state.get("watch_barrier") == barrier and cycle_covered / duration >= 0.85
    )
    done = stamp if newly else (previous["completed_at"] if previous else None)
    db.execute(
        "INSERT INTO web_playback_watch(user_id,library_id,anilist_id,season_number,episode_number,media_version,covered_seconds,duration_seconds,revision,last_played_at,completed_at,completion_after_event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,anilist_id,season_number,episode_number,media_version) DO UPDATE SET covered_seconds=MAX(web_playback_watch.covered_seconds,excluded.covered_seconds),revision=excluded.revision,last_played_at=MAX(web_playback_watch.last_played_at,excluded.last_played_at),completed_at=COALESCE(excluded.completed_at,web_playback_watch.completed_at),completion_after_event_id=CASE WHEN excluded.completed_at IS NOT NULL THEN excluded.completion_after_event_id ELSE web_playback_watch.completion_after_event_id END",
        (
            source.actor_user_id,
            source.library_id,
            source.anilist_id,
            source.season_number,
            source.episode_number,
            source.media_version,
            covered,
            duration,
            state["revision"],
            stamp,
            done,
            barrier,
        ),
    )
    if not newly:
        return result
    db.execute(
        "INSERT INTO web_playback_completions(user_id,anilist_id,season_number,episode_number,media_version,barrier,completed_at) VALUES(?,?,?,?,?,?,?)",
        (*identity, source.media_version, barrier, stamp),
    )
    result.update(completed=True, newly_completed=True)
    item = db.execute(
        "SELECT id FROM library_items WHERE library_id=? AND domain='anime' AND canonical_id=? AND season_number=? AND state='active'",
        (source.library_id, str(source.anilist_id), source.season_number),
    ).fetchone()
    core._require(item is not None, "Playback library changed.", 409)
    # The same checked personal-entry pattern used by household tracking. Never
    # attach a member's progress to the operational owner's monitor.
    own_monitor = db.execute(
        "SELECT id FROM media_monitors WHERE user_id=? AND anilist_id=? AND season_number=?",
        (source.actor_user_id, source.anilist_id, source.season_number),
    ).fetchone()
    if own_monitor:
        repository.ensure_watch_entry(
            db, user_id=source.actor_user_id, monitor_id=own_monitor[0], created_at=stamp
        )
    entry_id = core.start_anime_tracking(db, source._scope, item_id=item[0])
    entry = repository.get_watch_entry_by_id(db, user_id=source.actor_user_id, entry_id=entry_id)
    known = {
        row[0]
        for row in db.execute(
            "SELECT episode_number FROM web_playback_completions WHERE user_id=? AND anilist_id=? AND season_number=? AND barrier=?",
            (source.actor_user_id, source.anilist_id, source.season_number, barrier),
        )
    }
    contiguous = entry.progress_episodes
    while contiguous + 1 in known:
        contiguous += 1
    result["progress_episodes"] = entry.progress_episodes
    if contiguous == entry.progress_episodes or entry.status == "completed":
        return result
    event = WatchEvent.progress(
        source="web_player",
        episode=contiguous,
        event_key=f"web:coverage:{source.anilist_id}:{source.season_number}:{source.episode_number}:{barrier}",
        confidence="exact",
    )
    if entry.monitor_id is not None:
        saved = ingest_watch_event(
            db,
            user_id=source.actor_user_id,
            monitor_id=entry.monitor_id,
            event=event,
            observed_at=stamp,
        )
        result.update(event_id=saved.event_id, progress_episodes=saved.entry.progress_episodes)
    else:
        # Monitorless personal history uses the existing household save pattern:
        # ordinary reducer/repository/outbox, with no owner cleanup or impersonation.
        decision = reduce_watch_event(entry, event, observed_at=stamp)
        event_id = repository.insert_watch_event(
            db,
            user_id=source.actor_user_id,
            entry_id=entry_id,
            event=event,
            decision=decision,
            observed_at=stamp,
        )
        if decision.disposition == "accepted":
            repository.update_watch_entry(db, previous=entry, updated=decision.entry)
        if decision.enqueue_tracker_action:
            repository.insert_tracker_outbox(
                db,
                user_id=source.actor_user_id,
                entry_id=entry_id,
                event_id=event_id,
                action=decision.enqueue_tracker_action,
                progress_episodes=decision.entry.progress_episodes,
            )
        result.update(event_id=event_id, progress_episodes=decision.entry.progress_episodes)
    return result
