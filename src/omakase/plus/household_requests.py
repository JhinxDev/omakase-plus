"""Actor-preserving shared intake. Qualified publication is an injected capability."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omakase.plus import households as core
from omakase.plus import language_store
from omakase.plus.acquisition_policy import load_user_acquisition_policy
from omakase.plus.household_media import binding, prepare_addition
from omakase.plus.language_policy import LanguageRequestSnapshot, resolve_effective_language
from omakase.plus.media_types import MediaTarget


@dataclass(frozen=True)
class SharedAnimeRequest:
    actor_user_id: int
    operational_owner_id: int
    library_id: str
    item_id: str
    event_id: str | None
    request_id: str | None
    monitor_id: int | None
    outcome: str
    detail: str


@dataclass(frozen=True)
class SharedAnimeDispatch:
    actor_user_id: int
    operational_owner_id: int
    library_id: str
    item_id: str
    resource_id: str
    monitor_id: int
    event_id: str
    request_id: str
    source_generation: str
    contract_version: str


def _target(target):
    core._require(isinstance(target, MediaTarget), "A resolved anime identity is required.", 422)
    season = target.season_number if target.season_number is not None else 0
    core._identity("anime", str(target.anilist_id), season)
    core._require(
        isinstance(target.title, str) and 0 < len(target.title.strip()) <= 300,
        "Choose a valid anime title.",
        422,
    )
    core._require(
        type(target.aired_episodes) is int
        and 0 <= target.aired_episodes <= 5000
        and (
            target.episodes is None or type(target.episodes) is int and 0 <= target.episodes <= 5000
        ),
        "Exact bounded episode metadata is required.",
        422,
    )
    return season


def _receipt(
    scope,
    item,
    event=None,
    request=None,
    monitor=None,
    outcome="catalog_added",
    detail="Added to the shared catalog. No media request was sent.",
):
    return SharedAnimeRequest(
        scope.viewer_id,
        scope.operational_owner_id,
        scope.library_id,
        item,
        event,
        request,
        monitor,
        outcome,
        detail,
    )


def _outcome(db, event, code, detail):
    db.execute(
        "UPDATE library_request_events SET result_code=?,detail=?,updated_at=? WHERE id=?",
        (code, detail, core._stamp(), event),
    )


def prepare_shared_anime(
    db,
    scope,
    *,
    target,
    idempotency_key,
    request_media=False,
    member_preference=None,
    request_override=None,
):
    """No provider/metadata/tracker calls. New monitors cannot enter legacy scheduling."""
    season = _target(target)
    core._require(type(request_media) is bool, "Choose an explicit request intent.", 422)
    with core._transaction(db):
        core.revalidate_scope(db, scope)
        core._require(scope.kind == "household", "Choose a shared library.", 422)
        if request_media:
            core._require(
                scope.acquisition_requests and (scope.role == "owner" or scope.members_can_add),
                "Shared media requests are disabled by the library owner.",
                403,
            )
        media = prepare_addition(
            db,
            scope,
            domain="anime",
            canonical_id=str(target.anilist_id),
            season=season,
            title=target.title,
        )
        if not request_media:
            return _receipt(scope, media.item_id)
        event = core.LibraryRepository(db, scope).queue_request(
            media.item_id, idempotency_key=idempotency_key
        )
        request_id = "household:" + event
        prior = db.execute(
            "SELECT * FROM language_request_policies WHERE id=? AND actor_user_id=?",
            (request_id, scope.viewer_id),
        ).fetchone()
        if prior is not None:
            # Replay returns the original immutable request, never current form/settings values.
            language_store.load_request(db, actor_user_id=scope.viewer_id, request_id=request_id)
            event_row = db.execute(
                "SELECT state,result_code,detail FROM library_request_events WHERE id=?", (event,)
            ).fetchone()
            monitor = int(media.legacy_id) if media.legacy_id else None
            return _receipt(
                scope,
                media.item_id,
                event,
                request_id,
                monitor,
                event_row["result_code"],
                event_row["detail"],
            )
        defaults, allow_member, allow_override = language_store.library_defaults(db, scope)
        if member_preference is None and (allow_member or scope.role == "owner"):
            from omakase.plus.language_preferences import (
                member_preference as saved_member_preference,
            )

            member_preference = saved_member_preference(db, scope, "anime")
        effective = resolve_effective_language(
            defaults.for_domain("anime"),
            member_preference=member_preference,
            request_override=request_override,
            allow_member=allow_member or scope.role == "owner",
            allow_override=allow_override or scope.role == "owner",
        )
        monitor = None
        if media.legacy_id:
            monitor = db.execute(
                "SELECT * FROM media_monitors WHERE id=? AND user_id=?",
                (int(media.legacy_id), scope.operational_owner_id),
            ).fetchone()
        # Do not reuse an owner's private asset just because a member guesses its title.
        private = (
            db.execute(
                "SELECT id FROM media_monitors WHERE user_id=? AND anilist_id=?",
                (scope.operational_owner_id, target.anilist_id),
            ).fetchone()
            if monitor is None
            else None
        )
        count = (
            int(monitor["expected_episodes"] or 0)
            if monitor is not None and monitor["release_status"] == "FINISHED"
            else int(monitor["aired_episodes"] or 0)
            if monitor is not None
            else int(target.episodes or 0)
            if target.status == "FINISHED"
            else target.aired_episodes
        )
        core._require(
            count <= 1000,
            "This request exceeds the supported 1,000-episode snapshot. Add the catalog title without requesting media.",
            422,
        )
        snapshot = LanguageRequestSnapshot(
            request_id,
            scope.viewer_id,
            scope.library_id,
            "anime",
            f"anilist:{target.anilist_id}:season:{season}",
            effective,
            media.item_id,
            None,
        )
        language_store.record_request(
            db,
            snapshot,
            acquisition_policy=load_user_acquisition_policy(db, scope.operational_owner_id),
            required_episodes=tuple(range(1, count + 1)),
            intent="acquire",
            household_event_id=event,
        )
        if private is not None:
            detail = (
                "Queued for owner review. An existing personal resource was not shared or changed."
            )
            _outcome(db, event, "owner_review_required", detail)
            return _receipt(
                scope, media.item_id, event, request_id, None, "owner_review_required", detail
            )
        if monitor is None:
            cursor = db.execute(
                "INSERT INTO media_monitors(user_id,anilist_id,title,search_titles_json,genres_json,format,release_status,season_number,expected_episodes,aired_episodes,next_airing_at,active,last_result) VALUES(?,?,?,?,?,?,?,?,?,?,?,0,'HOUSEHOLD_REQUEST_QUEUED')",
                (
                    scope.operational_owner_id,
                    target.anilist_id,
                    target.title,
                    json.dumps(target.search_titles),
                    json.dumps(target.genres),
                    target.format,
                    target.status,
                    season,
                    target.episodes,
                    target.aired_episodes,
                    target.next_airing_at,
                ),
            )
            monitor_id = int(cursor.lastrowid)
            core._bind_item(
                db,
                scope,
                domain="anime",
                canonical_id=str(target.anilist_id),
                season=season,
                title=target.title,
                legacy_id=str(monitor_id),
                connector_scope="household",
                authorized_event_id=event,
            )
            media = binding(db, scope, item_id=media.item_id)
        else:
            monitor_id = int(monitor["id"])
        from omakase.plus.language_source import resolve_current_language_source

        resource = db.execute(
            "SELECT * FROM library_resources WHERE id=?", (media.resource_id,)
        ).fetchone()
        source = resolve_current_language_source(db, scope=scope, resource=resource)
        if source is not None:
            language_store.bind_request_resource(
                db,
                actor_user_id=scope.viewer_id,
                request_id=request_id,
                resource_id=media.resource_id,
            )
        detail = (
            "Media request queued. Saved language criteria need qualified publication support; no provider request was sent."
            if source is not None
            else "Media request queued. The owner must confirm this library’s configured source; no provider request was sent."
        )
        _outcome(db, event, "pipeline_unavailable", detail)
        return _receipt(
            scope, media.item_id, event, request_id, monitor_id, "pipeline_unavailable", detail
        )


async def dispatch_shared_anime(db, *, actor_user_id, request_id, qualifier=None, driver=None):
    """Trusted driver only, behind native-qualified capability; absent by default.

    The driver receives explicit actor/owner IDs, not an owner User/session. It
    must use reconcile_monitor's typed language kwargs and never legacy dispatch.
    """
    row = db.execute(
        "SELECT * FROM language_request_policies WHERE id=? AND actor_user_id=?",
        (request_id, actor_user_id),
    ).fetchone()
    core._require(row is not None, "Shared request is not available.", 404)
    scope = core.resolve_scope(db, viewer_id=actor_user_id, library_id=row["library_id"])
    event = db.execute(
        "SELECT * FROM library_request_events WHERE id=? AND actor_user_id=?",
        (row["household_event_id"], actor_user_id),
    ).fetchone()
    core._require(
        event is not None and event["state"] in {"queued", "dispatched"},
        "Request is no longer queued.",
        409,
    )
    continuing = event["state"] == "dispatched"
    core._require(
        not continuing or event["result_code"] in CONTINUABLE,
        "The request needs review before another provider attempt.",
        409,
    )
    if event["next_attempt_at"]:
        due = datetime.fromisoformat(event["next_attempt_at"].replace("Z", "+00:00"))
        core._require(
            due <= datetime.now(UTC), "The request is paused until its next allowed attempt.", 409
        )
    if qualifier is None and driver is None:
        qualifier, driver = publication_qualifier, checked_anime_driver
    core._require(
        scope.membership_revision == event["membership_revision"]
        and scope.policy_revision == event["policy_revision"],
        "Request authority changed.",
        409,
    )
    media = binding(db, scope, item_id=row["library_item_id"])
    contract = qualifier() if qualifier is not None else None
    if contract != language_store.CONTRACT_VERSION or driver is None:
        detail = "Media request remains queued because qualified publication support is unavailable. No provider request was sent."
        with core._transaction(db):
            core.revalidate_scope(db, scope)
            _outcome(db, event["id"], "pipeline_unavailable", detail)
        return _receipt(
            scope,
            media.item_id,
            event["id"],
            request_id,
            int(media.legacy_id) if media.legacy_id else None,
            "pipeline_unavailable",
            detail,
        )
    snapshot, _, _ = language_store.load_request(
        db, actor_user_id=actor_user_id, request_id=request_id, for_acquisition=True
    )
    core._require(
        snapshot.domain == "anime" and media.legacy_id is not None,
        "Request needs an authorized anime resource.",
        409,
    )
    source = language_store.current_request_source(db, snapshot)
    context = SharedAnimeDispatch(
        actor_user_id,
        scope.operational_owner_id,
        scope.library_id,
        media.item_id,
        media.resource_id,
        int(media.legacy_id),
        event["id"],
        request_id,
        source.source_generation,
        contract,
    )
    from omakase.plus.media_store import (
        acquire_sync_lease,
        heartbeat_sync_lease,
        release_sync_lease,
    )

    owner = secrets.token_urlsafe(32)

    def timestamp():
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def expiry():
        return (datetime.now(UTC) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lease = acquire_sync_lease(
        db,
        owner=owner,
        entrypoint="household_language_request",
        acquired_at=timestamp(),
        expires_at=expiry(),
    )
    db.commit()
    if not lease.acquired:
        return _receipt(
            scope,
            media.item_id,
            event["id"],
            request_id,
            context.monitor_id,
            "already_running",
            "Media sync is already running. This request remains queued.",
        )

    def heartbeat():
        language_store.load_request(
            db, actor_user_id=actor_user_id, request_id=request_id, for_acquisition=True
        )
        language_store.current_request_source(
            db, snapshot, asserted_generation=context.source_generation
        )
        core._require(
            heartbeat_sync_lease(db, owner=owner, heartbeat_at=timestamp(), expires_at=expiry()),
            "Media sync lease was lost.",
            409,
        )
        db.commit()

    try:
        heartbeat()
        if not continuing:
            core.authorize_dispatch(db, event_id=event["id"])
        # A process interruption here is never an automatic resubmission.
        _outcome(db, event["id"], "dispatch_in_progress", "Checked request is in progress.")
        db.commit()
        result = await driver(db, context=context, heartbeat=heartbeat)
        code = result.get("outcome") if isinstance(result, dict) else result.outcome
        delay = getattr(result, "retry_after_seconds", None)
        complete = code in {"language_reused"}
        if code == "provider_rate_limited" and delay is None:
            code, state, next_attempt = "provider_manual_review", "dispatched", None
        elif complete:
            state, next_attempt = "completed", None
        elif code in CONTINUABLE:
            state = "dispatched"
            next_attempt = (
                datetime.now(UTC) + timedelta(seconds=delay if delay is not None else 900)
            ).isoformat()
        else:
            state, next_attempt = "uncertain", None
        detail = (
            "Checked media request completed."
            if complete
            else (
                "The provider requires manual review before any further request."
                if code == "provider_manual_review"
                else "Media request paused; saved criteria and source will be rechecked before continuing."
                if state == "dispatched"
                else "The checked request needs review before any retry. Existing media is retained."
            )
        )
        with core._transaction(db):
            db.execute(
                "UPDATE library_request_events SET state=?,result_code=?,detail=?,next_attempt_at=?,updated_at=? WHERE id=?",
                (state, code, detail, next_attempt, core._stamp(), event["id"]),
            )
            # Shared operational monitors are consumed only through this typed queue.
            db.execute(
                "UPDATE media_monitors SET active=0,next_check_at=NULL WHERE id=?",
                (context.monitor_id,),
            )
        return result
    except Exception:
        with core._transaction(db):
            db.execute(
                "UPDATE library_request_events SET state='uncertain',result_code='dispatch_uncertain',detail='The checked request needs review before any retry.',updated_at=? WHERE id=? AND state='dispatched'",
                (core._stamp(), event["id"]),
            )
        raise
    finally:
        release_sync_lease(db, owner=owner, released_at=timestamp())
        db.commit()


def prepare_movie_language_request(
    db, scope, *, item_id, idempotency_key, intent="evaluate", request_override=None
):
    """Freeze a viewer's movie rule against an already-authorized operational resource."""
    import hashlib

    from omakase.plus.language_preferences import member_preference

    core._require(intent in {"evaluate", "acquire"}, "Choose a movie request intent.", 422)
    core._require(
        isinstance(idempotency_key, str) and 16 <= len(idempotency_key) <= 100,
        "Reload the movie form before submitting.",
        422,
    )
    with core._transaction(db):
        media = binding(db, scope, item_id=item_id)
        core._require(
            media.domain == "movie"
            and media.resource_id is not None
            and media.legacy_id is not None,
            "The owner must bind this movie to a shared media resource before it can be checked.",
            409,
        )
        if intent == "acquire":
            declared = {
                row[0]
                for row in db.execute(
                    "SELECT integration FROM library_settings WHERE library_id=? AND owner_user_id=?",
                    (scope.library_id, scope.operational_owner_id),
                )
            }
            core._require(
                {"plex", "riven", "seerr"} <= declared,
                "The owner must explicitly share Plex, Riven and Seerr connections before movie acquisition.",
                403,
            )
        defaults, allow_member, allow_override = language_store.library_defaults(db, scope)
        effective = resolve_effective_language(
            defaults.for_domain("movie"),
            member_preference=member_preference(db, scope, "movie")
            if allow_member or scope.role == "owner"
            else None,
            request_override=request_override,
            allow_member=allow_member or scope.role == "owner",
            allow_override=allow_override or scope.role == "owner",
        )
        event = (
            core.LibraryRepository(db, scope).queue_request(
                item_id, idempotency_key=idempotency_key
            )
            if intent == "acquire"
            else None
        )
        request_id = (
            "movie-"
            + hashlib.sha256(
                f"{scope.viewer_id}:{scope.library_id}:{item_id}:{intent}:{idempotency_key}".encode()
            ).hexdigest()
        )
        existing = db.execute(
            "SELECT id FROM language_request_policies WHERE id=? AND actor_user_id=?",
            (request_id, scope.viewer_id),
        ).fetchone()
        if existing:
            language_store.load_request(
                db,
                actor_user_id=scope.viewer_id,
                request_id=request_id,
                for_acquisition=intent == "acquire",
            )
            return request_id
        snapshot = LanguageRequestSnapshot(
            request_id,
            scope.viewer_id,
            scope.library_id,
            "movie",
            "imdb:" + media.canonical_id,
            effective,
            item_id,
            media.resource_id,
        )
        language_store.record_request(
            db,
            snapshot,
            acquisition_policy=load_user_acquisition_policy(db, scope.operational_owner_id),
            intent=intent,
            household_event_id=event,
        )
        if intent == "evaluate":
            # Public metadata only, cached under the viewer for proof receipt FK integrity.
            # No history/list/rating or owner connection credentials are copied.
            source = db.execute(
                "SELECT * FROM movie_catalog WHERE user_id=? AND imdb_id=? AND identity_verified=1",
                (scope.operational_owner_id, media.canonical_id),
            ).fetchone()
            core._require(source is not None, "Movie identity needs an owner check.", 409)
            own = db.execute(
                "SELECT tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?",
                (scope.viewer_id, media.canonical_id),
            ).fetchone()
            core._require(
                own is None or own[0] == source["tmdb_id"],
                "Your cached movie identity needs review.",
                409,
            )
            db.execute(
                "INSERT OR IGNORE INTO movie_catalog(user_id,imdb_id,tmdb_id,title,year,poster_url,backdrop_url,overview,identity_verified,metadata_checked_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                (
                    scope.viewer_id,
                    source["imdb_id"],
                    source["tmdb_id"],
                    source["title"],
                    source["year"],
                    core._public_artwork(source["poster_url"]),
                    core._public_artwork(source["backdrop_url"]),
                    source["overview"],
                    source["metadata_checked_at"],
                    core._stamp(),
                ),
            )
        return request_id


CONTINUABLE = frozenset(
    {
        "provider_rate_limited",
        "language_pending_verification",
        "pending",
        "partial",
        "complete",
        "degraded",
        "no_candidates",
        "language_unknown",
        "language_pipeline_unsupported",
    }
)


def publication_qualifier():
    from omakase.plus import language_publication

    return (
        language_store.CONTRACT_VERSION
        if language_publication.LANGUAGE_PUBLICATION_QUALIFIED
        else None
    )


async def checked_anime_driver(db, *, context, heartbeat):
    """Actual typed provider adapter; no owner principal or legacy monitor dispatch."""
    from omakase.plus.media_sync import reconcile_monitor
    from omakase.plus.media_types import MediaSyncResult
    from omakase.plus.provider_limits import ProviderRateLimit, cooldown_hooks
    from omakase.plus.providers.realdebrid import RealDebridProvider
    from omakase.plus.secrets import read_secret

    heartbeat()
    core._require(
        publication_qualifier() == context.contract_version,
        "Qualified publication is unavailable.",
        409,
    )
    hooks = cooldown_hooks(db, context.operational_owner_id)
    try:
        # Before catalog discovery as well as every individual provider request.
        hooks["request_guard"]()
    except ProviderRateLimit as error:
        return MediaSyncResult(
            context.monitor_id,
            0,
            "provider_rate_limited",
            frozenset(),
            frozenset(),
            (),
            error.detail,
            error.retry_after_seconds,
        )
    key = read_secret(db, context.operational_owner_id, "realdebrid_api_key")
    core._require(bool(key), "The owner must configure this library's media provider.", 409)
    provider = RealDebridProvider(key, **hooks)
    return await reconcile_monitor(
        db,
        monitor_id=context.monitor_id,
        rd=provider,
        now=core._stamp(),
        heartbeat=heartbeat,
        language_request_id=context.request_id,
        language_actor_user_id=context.actor_user_id,
        language_source_generation=context.source_generation,
        language_contract_version=context.contract_version,
    )


async def consume_shared_anime_queue(db, *, limit=10, qualifier=None, driver=None):
    """One finite fair pass. No loop, no uncertain retry, capability off by default."""
    if not core.feature_enabled():
        return ()
    qualifier = qualifier or publication_qualifier
    driver = driver or checked_anime_driver
    if qualifier() != language_store.CONTRACT_VERSION:
        return ()
    limit = min(50, max(1, int(limit)))
    now = datetime.now(UTC).isoformat()
    rows = db.execute(
        """SELECT * FROM (
          SELECT p.id AS request_id,e.actor_user_id,e.id AS event_id,e.updated_at,
            ROW_NUMBER() OVER (PARTITION BY l.owner_user_id ORDER BY e.updated_at,e.id) AS turn
          FROM library_request_events e JOIN language_request_policies p ON p.household_event_id=e.id
          JOIN libraries l ON l.id=e.library_id JOIN library_items i ON i.id=e.library_item_id
          WHERE i.domain='anime' AND i.state='active' AND l.state='active'
            AND (e.state='queued' OR (e.state='dispatched' AND e.result_code IN (
              'provider_rate_limited','language_pending_verification','pending','partial',
              'complete','degraded','no_candidates','language_unknown','language_pipeline_unsupported')))
            AND (e.next_attempt_at IS NULL OR julianday(e.next_attempt_at)<=julianday(?))
        ) ORDER BY turn,updated_at,event_id LIMIT ?""",
        (now, limit),
    ).fetchall()
    outcomes = []
    for row in rows:
        try:
            result = await dispatch_shared_anime(
                db,
                actor_user_id=row["actor_user_id"],
                request_id=row["request_id"],
                qualifier=qualifier,
                driver=driver,
            )
            outcomes.append(result)
        except (core.HouseholdError, ValueError):
            # Revoked/cancelled/config-changed requests stop before provider work.
            with core._transaction(db):
                db.execute(
                    "UPDATE library_request_events SET state='cancelled',result_code='authority_changed',detail='Request authority changed; no automatic retry.',updated_at=? WHERE id=? AND state IN ('queued','dispatched')",
                    (core._stamp(), row["event_id"]),
                )
        except Exception:
            # dispatch_shared_anime already records its own uncertain boundary.
            outcomes.append("review_required")
    return tuple(outcomes)
