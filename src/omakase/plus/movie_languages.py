"""Policy-aware movie actions: real actor, shared operational resource and exact proof.

The legacy MovieService remains the sole backend-wide submission owner. This adapter
never forges an owner User, mutates playback selection or replaces existing media.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

import httpx

from omakase.plus import movies_store as store
from omakase.plus.movies import MovieError, MovieService, _imdb, _iso, _time


async def request_language(service, actor_user_id: int, *, request_id: str) -> dict[str, Any]:
    """Acquire for an immutable actor intent while retaining backend-wide deduplication."""
    from omakase.plus import connections as configured
    from omakase.plus.households import authorize_dispatch, resolve_scope
    from omakase.plus.language_store import load_request

    event_id = None
    try:
        snapshot, _, _ = load_request(
            service.db, actor_user_id=actor_user_id, request_id=request_id, for_acquisition=True
        )
        if snapshot.domain != "movie":
            raise MovieError("invalid_input")
        scope = resolve_scope(service.db, viewer_id=actor_user_id, library_id=snapshot.library_id)
        imdb_id = _imdb(snapshot.media_identity.removeprefix("imdb:"))
        metadata = store.get_movie(service.db, scope.operational_owner_id, imdb_id)
        if metadata is None or not metadata["identity_verified"] or metadata["tmdb_id"] is None:
            raise MovieError("identity_mismatch")
        intent = service.db.execute(
            "SELECT household_event_id FROM language_request_policies WHERE id=? AND actor_user_id=?",
            (request_id, actor_user_id),
        ).fetchone()
        event_id = intent[0]

        def require_shared_authority():
            if scope.kind == "household":
                shared = {
                    row[0]
                    for row in service.db.execute(
                        "SELECT integration FROM library_settings WHERE library_id=? AND owner_user_id=?",
                        (scope.library_id, scope.operational_owner_id),
                    )
                }
                if not {"plex", "riven", "seerr"} <= shared:
                    raise MovieError("connection_rejected")
            bound = service.db.execute(
                "SELECT r.owner_user_id FROM language_request_bindings b "
                "JOIN library_resources r ON r.id=b.resource_id WHERE b.request_id=?",
                (request_id,),
            ).fetchone()
            if bound is None or bound[0] != scope.operational_owner_id:
                raise MovieError("invalid_input")

        require_shared_authority()
        owner_config = configured.movie_connections(service.db, scope.operational_owner_id)
        backend = MovieService(
            service.db,
            owner_config,
            transport=service._transport,
            now=service._now,
            resolver=service._resolver,
        )

        def authorize():
            # This is the final local dispatch boundary before the single
            # globally reserved write. Later revocation cannot undo HTTP.
            load_request(
                service.db,
                actor_user_id=actor_user_id,
                request_id=request_id,
                for_acquisition=True,
            )
            require_shared_authority()
            if configured.movie_connections(service.db, scope.operational_owner_id) != owner_config:
                raise MovieError("connection_rejected")
            if event_id is not None:
                authorize_dispatch(service.db, event_id=event_id)

        result = await backend.request(
            scope.operational_owner_id,
            imdb_id=imdb_id,
            tmdb_id=metadata["tmdb_id"],
            _authorize_submission=authorize,
        )
        # Do not expose owner request records or let a backend delivery flag
        # certify this viewer's requested language/selection.
        load_request(service.db, actor_user_id=actor_user_id, request_id=request_id)
        return {
            **service._card(actor_user_id, metadata),
            "requested": True,
            "request_id": request_id,
            "state": "verifying",
            "code": result["code"],
            "detail": "Request recorded. Configured backend filters apply; requested languages are checked against the delivered file.",
            "criteria_applied_by_backend": False,
            "plex_url": None,
        }
    except (ValueError, httpx.HTTPError, sqlite3.IntegrityError):
        # Keep dispatched/uncertain evidence. Never reset a backend-wide
        # reservation merely because a member left or a response was lost.
        return service._language_unavailable()


def language_status(service, actor_user_id: int, *, request_id: str) -> dict[str, Any]:
    """Read prior exact proof with fresh scope/config checks and no provider I/O."""
    from omakase.plus.playback_proof import receipt_kwargs

    try:
        card = service._language_card(actor_user_id, request_id)
        saved = service.db.execute(
            "SELECT v.* FROM movie_verifications v JOIN language_evaluations e "
            "ON e.id=v.language_evaluation_id WHERE v.user_id=? AND v.imdb_id=? "
            "AND v.proof_schema=2 AND e.request_id=? ORDER BY v.verified_at DESC LIMIT 1",
            (actor_user_id, card["imdb_id"], request_id),
        ).fetchone()
        if saved is None or (_time(saved["expires_at"]) or service._clock()) <= service._clock():
            return {**card, **service._language_unavailable()}
        proof = receipt_kwargs(
            service.db,
            actor_user_id=actor_user_id,
            request_id=request_id,
            evaluation_id=saved["language_evaluation_id"],
            now=service._clock(),
        )
        # Never combine independent historical file identities with a proof.
        if any(
            saved[name] != proof[name]
            for name in (
                "fingerprint",
                "plex_key",
                "physical_fingerprint",
                "playback_selection_id",
                "movie_file_observation_id",
                "movie_byte_observation_id",
                "source_generation",
            )
        ):
            return {**card, **service._language_unavailable()}
        service._machine = service.db.execute(
            "SELECT server_id FROM movie_file_observations WHERE id=?",
            (proof["movie_file_observation_id"],),
        ).fetchone()[0]
        return {
            **card,
            **{
                name: saved[name]
                for name in (
                    "state",
                    "code",
                    "detail",
                    "resolution",
                    "selected_audio",
                    "verified_at",
                    "expires_at",
                    "http_status",
                    "bytes_read",
                )
            },
            "proof_schema": 2,
            "plex_url": service._plex_link(proof["plex_key"]),
            "language_state": "verified",
            "physical_compatible": True,
        }
    except ValueError:
        return service._language_unavailable()


async def verify_language(
    service, actor_user_id: int, *, request_id: str, rating_key: str
) -> dict[str, Any]:
    """Explicit bounded personal-Plex reads; no acquisition or selection mutation."""
    from omakase.plus.language_store import evaluate_request
    from omakase.plus.playback_proof import observe_movie_playback, receipt_kwargs

    try:
        card = service._language_card(actor_user_id, request_id)
        selection = await observe_movie_playback(
            service.db,
            actor_user_id=actor_user_id,
            request_id=request_id,
            rating_key=rating_key,
            transport=service._transport,
            now=service._clock(),
        )
        evaluation_id, evaluation = evaluate_request(
            service.db,
            actor_user_id=actor_user_id,
            request_id=request_id,
            playback_selection_id=selection,
        )
        if not evaluation.physical_compatible or not evaluation.selection_confirmed:
            return {
                **card,
                **service._language_unavailable(),
                "language_state": evaluation.state,
                "physical_compatible": evaluation.physical_compatible,
            }
        proof = receipt_kwargs(
            service.db,
            actor_user_id=actor_user_id,
            request_id=request_id,
            evaluation_id=evaluation_id,
            now=service._clock(),
        )
        verified = service._clock()
        receipt = {
            **proof,
            "state": "ready",
            "code": "ready",
            "detail": "Exact movie, requested languages, personal stream selection and accessible bytes verified.",
            "verified_at": _iso(verified),
            "expires_at": _iso(verified + timedelta(seconds=min(900, service._ttl))),
        }
        store.save_verification(service.db, actor_user_id, card["imdb_id"], receipt)
        return service.language_status(actor_user_id, request_id=request_id)
    except (ValueError, httpx.HTTPError, sqlite3.IntegrityError):
        return service._language_unavailable()
