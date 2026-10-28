"""Checked shared-media capabilities. No provider calls or owner impersonation."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from omakase.plus import households as core


@dataclass(frozen=True)
class MediaBinding:
    scope: core.LibraryScope
    item_id: str
    domain: str
    canonical_id: str
    season: int
    resource_id: str | None
    legacy_id: str | None
    history_only: bool = False

    @property
    def actor_user_id(self):
        return self.scope.viewer_id

    @property
    def operational_owner_id(self):
        return self.scope.operational_owner_id


def binding(db, scope: core.LibraryScope, *, item_id: str, for_history=False) -> MediaBinding:
    """Resolve membership before touching operational data, on every request."""
    item = core.LibraryRepository(db, scope).item(item_id, include_removed=for_history)
    if item["state"] == "removed":
        # A retired catalog membership can authorize personal history only, never media.
        return MediaBinding(
            scope,
            item["id"],
            item["domain"],
            item["canonical_id"],
            item["season_number"],
            None,
            None,
            True,
        )
    legacy = None
    if item["resource_id"]:
        row = db.execute(
            "SELECT * FROM library_resources WHERE id=? AND owner_user_id=? AND domain=? AND canonical_id=? AND COALESCE(season_number,0)=?",
            (
                item["resource_id"],
                scope.operational_owner_id,
                item["domain"],
                item["canonical_id"],
                item["season_number"],
            ),
        ).fetchone()
        core._require(row is not None, "Library resource is unavailable.", 404)
        legacy = row["legacy_id"]
        if legacy:
            if item["domain"] == "anime":
                exists = db.execute(
                    "SELECT 1 FROM media_monitors WHERE id=? AND user_id=? AND anilist_id=? AND season_number=?",
                    (
                        legacy,
                        scope.operational_owner_id,
                        int(item["canonical_id"]),
                        item["season_number"],
                    ),
                ).fetchone()
            else:
                table, key = (
                    ("movie_catalog", "imdb_id")
                    if item["domain"] == "movie"
                    else ("tv_catalog", "tmdb_id")
                )
                exists = db.execute(
                    f"SELECT 1 FROM {table} WHERE user_id=? AND {key}=?",
                    (scope.operational_owner_id, item["canonical_id"]),
                ).fetchone()
            core._require(exists is not None, "Library resource is unavailable.", 404)
    return MediaBinding(
        scope,
        item["id"],
        item["domain"],
        item["canonical_id"],
        item["season_number"],
        item["resource_id"],
        legacy,
    )


def matching_item(db, scope, *, domain, canonical_id, season=0):
    core.revalidate_scope(db, scope)
    canonical_id = core._identity(domain, canonical_id, season)
    row = db.execute(
        "SELECT id FROM library_items WHERE library_id=? AND state='active' AND domain=? AND canonical_id=? AND season_number=?",
        (scope.library_id, domain, canonical_id, season),
    ).fetchone()
    return row[0] if row else None


def public_metadata(db, media: MediaBinding) -> dict[str, Any]:
    """Only public catalog metadata; never owner history, request details or paths."""
    media = binding(db, media.scope, item_id=media.item_id)
    item = core.LibraryRepository(db, media.scope).item(media.item_id)
    result = {
        "title": item["title"],
        "overview": "",
        "year": None,
        "poster_url": None,
        "backdrop_url": None,
    }
    saved = json.loads(item["metadata_json"])
    if isinstance(saved, dict):
        result.update(core.public_catalog_metadata(saved))
    if not media.legacy_id:
        return result
    if media.domain == "movie":
        row = db.execute(
            "SELECT title,year,overview,poster_url,backdrop_url,tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?",
            (media.operational_owner_id, media.canonical_id),
        ).fetchone()
        result.update(dict(row))
    elif media.domain == "tv":
        row = db.execute(
            "SELECT metadata_json FROM tv_catalog WHERE user_id=? AND tmdb_id=?",
            (media.operational_owner_id, int(media.canonical_id)),
        ).fetchone()
        saved = json.loads(row[0])
        if isinstance(saved, dict):
            for key in ("title", "overview", "year", "poster_url", "backdrop_url"):
                value = saved.get(key)
                if isinstance(value, (str, int, type(None))):
                    result[key] = value
    for key in ("poster_url", "backdrop_url"):
        result[key] = core._public_artwork(result.get(key))
    return result


def viewer_history(db, media: MediaBinding) -> dict[str, Any] | None:
    media = binding(db, media.scope, item_id=media.item_id, for_history=media.history_only)
    if media.domain == "anime":
        row = db.execute(
            "SELECT id,status,progress_episodes,episode_total,monitor_id FROM watch_entries WHERE user_id=? AND anilist_id=? AND season_number=?",
            (media.actor_user_id, int(media.canonical_id), media.season),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        rating = db.execute(
            "SELECT score_ten FROM watch_events WHERE user_id=? AND watch_entry_id=? AND disposition='accepted' AND score_ten IS NOT NULL ORDER BY id DESC LIMIT 1",
            (media.actor_user_id, row["id"]),
        ).fetchone()
        result["rating"] = rating[0] if rating else None
        return result
    key = "imdb_id" if media.domain == "movie" else "tmdb_id"
    row = db.execute(
        f"SELECT status,rating,status_source,rating_source FROM {media.domain}_history WHERE user_id=? AND {key}=?",
        (media.actor_user_id, media.canonical_id),
    ).fetchone()
    return dict(row) if row else None


def physical_facts(db, media: MediaBinding) -> dict[str, Any]:
    """Saved physical observations are distinct from this viewer's playback readiness."""
    media = binding(db, media.scope, item_id=media.item_id)
    result = {
        "label": "Not checked",
        "verified_episodes": [],
        "plex_episodes": 0,
        "observed_seasons": 0,
        "playback_label": "Your playback has not been checked",
    }
    if not media.legacy_id:
        return result
    if media.domain == "anime":
        from omakase.plus.media_store import covered_episodes

        result["verified_episodes"] = sorted(covered_episodes(db, int(media.legacy_id)))
        result["label"] = (
            "Verified media available" if result["verified_episodes"] else "Saved in this library"
        )
    elif media.domain == "movie":
        # A saved owner's selected-audio receipt is never the member's active playback proof.
        row = db.execute(
            "SELECT 1 FROM movie_verifications WHERE user_id=? AND imdb_id=? AND plex_key!='' LIMIT 1",
            (media.operational_owner_id, media.canonical_id),
        ).fetchone()
        result["label"] = "Previously indexed in Plex" if row else "Saved in this library"
    else:
        row = db.execute(
            "SELECT COUNT(*) AS episodes,COUNT(DISTINCT season_number) AS seasons FROM tv_episodes WHERE user_id=? AND tmdb_id=? AND plex_present=1",
            (media.operational_owner_id, int(media.canonical_id)),
        ).fetchone()
        result.update(plex_episodes=row["episodes"], observed_seasons=row["seasons"])
        result["label"] = (
            "Previously indexed in Plex" if row["episodes"] else "Saved in this library"
        )
    return result


def prepare_addition(db, scope, *, domain, canonical_id, title, season=0) -> MediaBinding:
    """Explicit catalog addition only. Acquisition remains a separate checked request.

    Members cannot bind an owner's private existing asset by guessing its ID.
    An owner can explicitly bind/promote it using LibraryRepository.add.
    """
    with core._transaction(db):
        item_id = core.LibraryRepository(db, scope).add(
            domain=domain, canonical_id=canonical_id, title=title, season=season
        )
        return binding(db, scope, item_id=item_id)


def bind_acquired_resource(
    db, media: MediaBinding, *, event_id: str, legacy_id: str, connector_scope: str
) -> MediaBinding:
    """Bind a worker-created resource only to its dispatched, still-authorized request.

    This is NOT provider dispatch or a substitute for the backend-wide movie reservation.
    Caller must retain uncertain remote outcomes when authority changed during HTTP.
    """
    with core._transaction(db):
        media = binding(db, media.scope, item_id=media.item_id)
        event = db.execute(
            "SELECT * FROM library_request_events WHERE id=? AND library_id=? AND library_item_id=? AND actor_user_id=? AND state='dispatched'",
            (event_id, media.scope.library_id, media.item_id, media.actor_user_id),
        ).fetchone()
        core._require(
            event is not None
            and event["membership_revision"] == media.scope.membership_revision
            and event["policy_revision"] == media.scope.policy_revision,
            "Acquisition authorization changed.",
            409,
        )
        core._require(media.domain != "tv", "TV acquisition is unavailable.", 422)
        # The worker has an explicit actor capability; no synthetic owner account or session.
        core._bind_item(
            db,
            media.scope,
            domain=media.domain,
            canonical_id=media.canonical_id,
            title=core.LibraryRepository(db, media.scope).item(media.item_id)["title"],
            season=media.season,
            legacy_id=legacy_id,
            connector_scope=connector_scope,
            authorized_event_id=event_id,
        )
        return binding(db, media.scope, item_id=media.item_id)


def save_anime_watch(db, media: MediaBinding, *, event, score_ten=None):
    """Checked catalog action; current viewer remains independent of operational owner."""
    with core._transaction(db):
        media = binding(db, media.scope, item_id=media.item_id, for_history=media.history_only)
        core._require(media.domain == "anime", "Choose anime tracking.", 422)
        entry_id = core.start_anime_tracking(
            db, media.scope, item_id=media.item_id, include_removed=media.history_only
        )
        return save_anime_personal_entry(
            db, viewer_id=media.actor_user_id, entry_id=entry_id, event=event, score_ten=score_ten
        )


def save_anime_personal_entry(db, *, viewer_id, entry_id, event, score_ten=None):
    """Account-owned history survives leaving a library; never grants physical access."""
    from omakase.plus.watch import repository
    from omakase.plus.watch.reducer import reduce_watch_event
    from omakase.plus.watch.service import WatchIngestResult

    core._require(
        event.source == "manual" and event.confidence == "manual",
        "Use a manual personal tracking action.",
        422,
    )
    core._require(
        score_ten is None
        or (type(score_ten) is int and 1 <= score_ten <= 10 and event.kind == "completed"),
        "Rating requires explicit completion and an integer from 1 to 10.",
        422,
    )
    with core._transaction(db):
        entry = repository.get_watch_entry_by_id(db, user_id=viewer_id, entry_id=entry_id)
        core._require(entry is not None, "Personal history not available.", 404)
        duplicate = repository.find_watch_event(
            db, user_id=viewer_id, source=event.source, event_key=event.event_key
        )
        if duplicate:
            core._require(
                duplicate["watch_entry_id"] == entry_id,
                "Tracking event belongs to another title.",
                409,
            )
            return WatchIngestResult(entry, "ignored", "duplicate_event", int(duplicate["id"]))
        stamp = core._stamp().replace("+00:00", "Z")
        decision = reduce_watch_event(entry, event, observed_at=stamp)
        if score_ten is not None and decision.reason == "completed_is_sticky":
            decision = replace(
                decision,
                entry=replace(entry, revision=entry.revision + 1, last_observed_at=stamp),
                disposition="accepted",
                reason="completion_score_updated",
                enqueue_tracker_action="complete",
            )
        event_id = repository.insert_watch_event(
            db,
            user_id=viewer_id,
            entry_id=entry_id,
            event=event,
            decision=decision,
            observed_at=stamp,
            score_ten=score_ten,
        )
        if decision.disposition == "accepted":
            repository.update_watch_entry(db, previous=entry, updated=decision.entry)
        outbox = None
        if decision.enqueue_tracker_action:
            outbox = repository.insert_tracker_outbox(
                db,
                user_id=viewer_id,
                entry_id=entry_id,
                event_id=event_id,
                action=decision.enqueue_tracker_action,
                progress_episodes=decision.entry.progress_episodes,
                score_ten=score_ten,
            )
        return WatchIngestResult(
            decision.entry, decision.disposition, decision.reason, event_id, outbox_id=outbox
        )


_UNSET = object()


def save_series_or_movie_history(db, media: MediaBinding, *, status=_UNSET, rating=_UNSET):
    """Explicit local personal status/rating; no metadata lookup or Plex/history import."""
    from omakase.plus.movie_history import WATCH_STATUSES, MovieHistoryService, _rating
    from omakase.plus.movies import MovieConnections, MovieError, MovieService

    core._require(media.domain in {"movie", "tv"}, "Choose movie or TV tracking.", 422)
    core._require(
        status is not _UNSET or rating is not _UNSET, "Choose a personal tracking change.", 422
    )
    core._require(status is _UNSET or status in WATCH_STATUSES, "Choose a valid watch status.", 422)
    try:
        rating = _rating(rating) if rating is not _UNSET else _UNSET
    except MovieError:
        raise core.HouseholdError("Choose a rating from 1 to 10 in half steps.", 422) from None
    with core._transaction(db):
        media = binding(db, media.scope, item_id=media.item_id, for_history=media.history_only)
        core._require(media.domain in {"movie", "tv"}, "Choose movie or TV tracking.", 422)
        if media.history_only:
            core._require(
                viewer_history(db, media) is not None,
                "Use your personal catalog to start new tracking.",
                422,
            )
        else:
            core.PersonalTrackingRepository(db, viewer_id=media.actor_user_id).start(
                media.scope, item_id=media.item_id
            )
        if media.domain == "movie":
            history = MovieHistoryService(MovieService(db, MovieConnections()))
            if status is not _UNSET:
                history._change(
                    media.actor_user_id, media.canonical_id, "status", status, "manual", "manual"
                )
            if rating is not _UNSET:
                history._change(
                    media.actor_user_id, media.canonical_id, "rating", rating, "manual", "manual"
                )
        else:
            for field, value in (("status", status), ("rating", rating)):
                if value is not _UNSET:
                    db.execute(
                        f"UPDATE tv_history SET {field}=?,{field}_source='manual',updated_at=? WHERE user_id=? AND tmdb_id=?",
                        (value, core._stamp(), media.actor_user_id, int(media.canonical_id)),
                    )
        return viewer_history(db, media)


def read_route_class(base):
    """Translate checked-scope failures into safe HTTP statuses at adapter routes."""
    from fastapi import HTTPException

    class ScopedReadRoute(base):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def checked(request):
                try:
                    return await handler(request)
                except core.HouseholdError as error:
                    raise HTTPException(
                        error.status,
                        str(error),
                        headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
                    ) from None

            return checked

    return ScopedReadRoute


def managed_removal_guard(db, *, operational_owner_id, domain, canonical_id, season=0):
    """Coarse cross-scope blocker for existing preview AND execution transactions.

    Caller must also retain existing ownership, leases, inventory and provider proofs.
    This never approves deletion by itself and must run while the owning removal
    plan is serialized; a preflight result is not an execution authorization.
    """
    canonical_id = core._identity(domain, canonical_id, season)
    rows = db.execute(
        "SELECT i.id,i.library_id FROM library_resources r JOIN library_items i ON i.resource_id=r.id JOIN libraries l ON l.id=i.library_id AND l.state='active' WHERE r.owner_user_id=? AND r.domain=? AND r.canonical_id=? AND r.season_number=?",
        (operational_owner_id, domain, canonical_id, season),
    ).fetchall()
    if not rows:
        return {"has_shared_references": False, "blocked": False}
    blocked = False
    shared = False
    for row in rows:
        scope = core.resolve_scope(db, viewer_id=operational_owner_id, library_id=row["library_id"])
        item = core.LibraryRepository(db, scope).item(row["id"], include_removed=True)
        shared |= scope.kind == "household" and item["state"] == "active"
        blocked |= core.LibraryRepository(db, scope).removal_blocked(
            row["id"], include_removed=True
        )
        blocked |= bool(
            db.execute(
                "SELECT 1 FROM library_item_interest i JOIN household_memberships m ON m.user_id=i.user_id AND m.library_id=? AND m.state='active' WHERE i.library_item_id=? AND (i.keep_available=1 OR i.active_use=1) LIMIT 1",
                (scope.library_id, row["id"]),
            ).fetchone()
        )
    return {"has_shared_references": shared, "blocked": blocked}


def personal_legacy_visible(db, scope, *, domain, canonical_id, season=0):
    """Unbound migrated inventory stays personal; explicit promotion moves its catalog."""
    core.revalidate_scope(db, scope)
    core._require(scope.kind == "personal", "Choose a personal library.", 422)
    canonical_id = core._identity(domain, canonical_id, season)
    return (
        db.execute(
            "SELECT 1 FROM library_resources r JOIN library_items i ON i.resource_id=r.id WHERE r.owner_user_id=? AND r.domain=? AND r.canonical_id=? AND r.season_number=? AND i.state='active' AND i.library_id!=? AND NOT EXISTS(SELECT 1 FROM library_items own WHERE own.library_id=? AND own.state='active' AND own.resource_id=r.id) LIMIT 1",
            (
                scope.operational_owner_id,
                domain,
                canonical_id,
                season,
                scope.library_id,
                scope.library_id,
            ),
        ).fetchone()
        is None
    )


def tv_episode_rows(db, media: MediaBinding, *, season: int, limit=100):
    """Public cached episode facts + real viewer watch state, with no GET copying."""
    media = binding(db, media.scope, item_id=media.item_id)
    core._require(
        media.domain == "tv"
        and type(season) is int
        and 0 <= season <= 200
        and type(limit) is int
        and 1 <= limit <= 100,
        "Choose a valid TV season.",
        422,
    )
    if not media.legacy_id:
        return []
    rows = db.execute(
        "SELECT e.episode_number,e.metadata_json,mine.watched,mine.watch_source FROM tv_episodes e LEFT JOIN tv_episodes mine ON mine.user_id=? AND mine.tmdb_id=e.tmdb_id AND mine.season_number=e.season_number AND mine.episode_number=e.episode_number WHERE e.user_id=? AND e.tmdb_id=? AND e.season_number=? ORDER BY e.episode_number LIMIT ?",
        (media.actor_user_id, media.operational_owner_id, int(media.canonical_id), season, limit),
    ).fetchall()
    result = []
    for row in rows:
        metadata = json.loads(row["metadata_json"])
        if not isinstance(metadata, dict):
            metadata = {}
        from datetime import UTC, date, datetime

        try:
            released = date.fromisoformat(metadata.get("air_date", "")) <= datetime.now(UTC).date()
        except (ValueError, TypeError):
            released = False
        result.append(
            {
                "can_mark_watched": released,
                "episode_number": row["episode_number"],
                "title": str(
                    metadata.get("name")
                    or metadata.get("title")
                    or f"Episode {row['episode_number']}"
                )[:300],
                "air_date": metadata.get("air_date")
                if isinstance(metadata.get("air_date"), str)
                else None,
                "watched": bool(row["watched"])
                if row["watch_source"] not in (None, "none")
                else None,
            }
        )
    return result


def save_tv_episode(db, media: MediaBinding, *, season: int, episode: int, watched: bool):
    """Record one real viewer's episode; presence and series completion stay separate."""
    from datetime import UTC, date, datetime

    core._require(
        type(watched) is bool
        and type(season) is int
        and 0 <= season <= 200
        and type(episode) is int
        and 1 <= episode <= 1000,
        "Choose a valid TV episode.",
        422,
    )
    with core._transaction(db):
        media = binding(db, media.scope, item_id=media.item_id)
        core._require(
            media.domain == "tv" and media.legacy_id is not None,
            "Load a cached TV episode first.",
            422,
        )
        row = db.execute(
            "SELECT metadata_json FROM tv_episodes WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
            (media.operational_owner_id, int(media.canonical_id), season, episode),
        ).fetchone()
        core._require(row is not None, "Load a cached TV episode first.", 422)
        saved = json.loads(row[0])
        core._require(isinstance(saved, dict), "Episode metadata needs review.", 422)
        try:
            released = date.fromisoformat(saved.get("air_date", "")) <= datetime.now(UTC).date()
        except (TypeError, ValueError):
            released = False
        core._require(
            not watched or released, "Only a known released episode can be marked watched.", 422
        )
        core.PersonalTrackingRepository(db, viewer_id=media.actor_user_id).start(
            media.scope, item_id=media.item_id
        )
        metadata = {
            key: value
            for key, value in saved.items()
            if key in {"name", "title", "overview", "air_date", "episode_number", "season_number"}
            and isinstance(value, (str, int, type(None)))
        }
        db.execute(
            "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,watched,watch_source,plex_present,updated_at) VALUES(?,?,?,?,?,?,'manual',0,?) ON CONFLICT(user_id,tmdb_id,season_number,episode_number) DO UPDATE SET watched=excluded.watched,watch_source='manual',updated_at=excluded.updated_at",
            (
                media.actor_user_id,
                int(media.canonical_id),
                season,
                episode,
                json.dumps(metadata),
                int(watched),
                core._stamp(),
            ),
        )
