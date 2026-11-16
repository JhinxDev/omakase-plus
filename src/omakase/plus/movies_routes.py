"""Private movie pages and bounded explicit actions, separate from anime."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from math import ceil
from time import monotonic
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from omakase.plus.artwork_delivery import optimized_artwork
from omakase.plus.connections import movie_connections
from omakase.plus.debrid_catalog import movie_ids
from omakase.plus.debrid_library import DebridLibrary, DebridLibraryError
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.movie_artwork import movie_artwork
from omakase.plus.movie_cleanup import MovieCleanupUnavailable, movie_cleanup_enabled
from omakase.plus.movie_cleanup_confirmation import cleanup_status
from omakase.plus.movie_presentation import present_movie
from omakase.plus.movies import MovieError, MovieService
from omakase.plus.movies_store import get_movie
from omakase.plus.posters import PosterUnavailable
from omakase.plus.providers import playback as playback_providers
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_playback_catalog import personal_scope
from omakase.plus.web_playback_navigation import catalog_resume_progress
from omakase.plus.web_playback_source import PlaybackSourceError
from omakase.plus.web_security import csrf_token, require_csrf


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Not found")


def create_movies_router(
    templates, context_factory=None, *, service_factory=None, households_enabled=False
):
    from omakase.plus.household_media import read_route_class

    router = APIRouter(
        route_class=read_route_class(
            bounded_form_route(16_384, "Movie form is too large", guard=_private)
        )
    )
    cloud_library = DebridLibrary()

    async def cloud_availability(db, user, *, timeout=8):
        if not playback_providers.select(db, user.id).cloud_route:
            return None, False
        try:
            async with asyncio.timeout(timeout):
                return await movie_ids(db, personal_scope(db, user.id), cloud_library), False
        except (DebridLibraryError, PlaybackSourceError, TimeoutError):
            return set(), True

    def service(db, user):
        return (
            service_factory(db, user)
            if service_factory
            else MovieService(db, movie_connections(db, user.id))
        )

    def context(request, user):
        return (
            context_factory(request, user, active_page="movies")
            if context_factory
            else {
                "email": user.email,
                "active_page": "movies",
                "movies_enabled": True,
                "csrf_token": csrf_token(request),
            }
        )

    async def render(
        request,
        user,
        db,
        *,
        q="",
        view="all",
        notice="",
        action_card=None,
        sync_result=None,
        offset=0,
    ):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.household_views import collection
            from omakase.plus.households import selected_scope

            selected = selected_scope(db, viewer_id=user.id)
            if selected.kind == "household":
                data = collection(db, selected, domain="movie", query=q, offset=offset)
                return templates.TemplateResponse(
                    request,
                    "household_media.html",
                    {
                        **context(request, user),
                        **data,
                        "item": None,
                        "csrf_token": csrf_token(request),
                    },
                    headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
                )
        svc = service(db, user)
        cloud_ids, cloud_error = await cloud_availability(db, user)
        cards, error = [], ""
        try:
            if q and view in {"all", "discover"}:
                cards = await svc.search(user.id, q)
            else:
                cards = (
                    await svc.list(user.id, view=view)
                    if cloud_ids is None
                    else await svc.list(user.id, view=view, cloud_ids=cloud_ids)
                )
                if q:
                    needle = q.strip().casefold()
                    cards = [
                        card
                        for card in cards
                        if needle
                        in f"{card['title']} {card.get('year') or ''} {card['imdb_id']}".casefold()
                    ]
        except MovieError as exc:
            error = exc.detail
        from omakase.plus.movie_history import MovieHistoryService

        history = MovieHistoryService(svc)
        try:
            progress = catalog_resume_progress(
                db, personal_scope(db, user.id), "movie", cloud=cloud_ids is not None
            )
        except PlaybackSourceError:
            progress = {}
        for card in cards:
            card["history"] = history.get(user.id, card["imdb_id"]) if card.get("imdb_id") else None
            card["watched_percent"] = (
                100
                if (card["history"] or {}).get("status") == "completed"
                else progress.get(card.get("imdb_id"))
            )
        if view == "requests":
            cards = [
                card
                for card in cards
                if (
                    card["imdb_id"] not in cloud_ids
                    if cloud_ids is not None
                    else not card.get("plex_present")
                )
                and (card["history"] or {}).get("status") != "completed"
            ]
        cards = [present_movie(card, cloud_ids=cloud_ids) for card in cards]
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.household_media import personal_legacy_visible
            from omakase.plus.households import selected_scope

            personal = selected_scope(db, viewer_id=user.id)
            cards = [
                card
                for card in cards
                if personal_legacy_visible(
                    db, personal, domain="movie", canonical_id=card["imdb_id"]
                )
            ]
        return templates.TemplateResponse(
            request,
            "movies.html",
            {
                **context(request, user),
                "cards": cards,
                "query": q,
                "view": view,
                "movies_error": error,
                "notice": notice,
                "action_card": action_card,
                "sync_result": sync_result,
                "cloud_playback": cloud_ids is not None,
                "cloud_error": cloud_error,
                "history_sync": history.sync_status(user.id) if cloud_ids is None else None,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/movies", response_class=HTMLResponse)
    async def page(
        request: Request,
        q: str = "",
        offset: int = 0,
        view: str = "all",
        notice: str = "",
        remaining: int | None = None,
        imported: int | None = None,
        skipped: int | None = None,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        if (
            view
            not in {"all", "requests", "ready", "discover", "available", "watching", "completed"}
            or len(q) > 200
        ):
            raise HTTPException(422, "Invalid movie search")
        if not 0 <= offset <= 10000:
            raise HTTPException(422, "Invalid library page")
        counts = (remaining, imported, skipped)
        if any(value is not None and not 0 <= value <= 1000 for value in counts):
            raise HTTPException(422, "Invalid sync summary")
        sync_result = (
            {"remaining": remaining, "imported": imported, "skipped": skipped}
            if notice == "synced" and all(value is not None for value in counts)
            else None
        )
        return await render(
            request,
            user,
            db,
            q=q,
            offset=offset,
            view="available" if view == "ready" else view,
            notice=notice if notice in {"saved", "synced", "updated"} else "",
            sync_result=sync_result,
        )

    @router.get("/movies/{imdb_id}/artwork")
    async def artwork(
        imdb_id: str, variant: str = "poster", user=Depends(require_user), db=Depends(get_db)
    ):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.household_media import matching_item
            from omakase.plus.household_views import detail_url
            from omakase.plus.households import selected_scope

            selected = selected_scope(db, viewer_id=user.id)
            if selected.kind == "household":
                if variant not in {"poster", "backdrop"}:
                    raise HTTPException(422, "Invalid artwork variant")
                item_id = matching_item(db, selected, domain="movie", canonical_id=imdb_id)
                if item_id is not None:
                    return RedirectResponse(
                        detail_url(selected, item_id) + "/artwork?variant=" + variant,
                        status_code=307,
                        headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
                    )
        if variant not in {"poster", "backdrop"}:
            raise HTTPException(422, "Invalid artwork variant")
        movie = get_movie(db, user.id, imdb_id)
        field = "backdrop_url" if variant == "backdrop" else "poster_url"
        if not movie or not movie.get(field):
            raise HTTPException(404, "Artwork unavailable")
        try:
            payload, kind = await movie_artwork(movie[field])
            payload, kind = await run_in_threadpool(
                optimized_artwork, payload, kind="banner" if variant == "backdrop" else "poster"
            )
        except PosterUnavailable:
            raise HTTPException(404, "Artwork unavailable") from None
        return Response(
            payload,
            media_type=kind,
            headers={
                "Cache-Control": "private, max-age=86400",
                "X-Content-Type-Options": "nosniff",
            },
        )

    def reply(request, result):
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
        imdb_id = result.get("imdb_id") if isinstance(result, dict) else None
        outcome = (
            (result.get("request_check") or {}).get("outcome") if isinstance(result, dict) else None
        )
        notice = (
            f"request_{outcome}"
            if outcome in {"updated", "unchanged", "saved", "failed"}
            else "updated"
        )
        target = (
            f"/plus/movies/{imdb_id}?notice={notice}" if imdb_id else "/plus/movies?notice=updated"
        )
        return RedirectResponse(target, status_code=303)

    @router.get("/movies/{imdb_id}", response_class=HTMLResponse)
    async def detail(
        request: Request,
        imdb_id: str,
        notice: str = "",
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.household_media import matching_item
            from omakase.plus.household_views import detail_url
            from omakase.plus.households import selected_scope

            selected = selected_scope(db, viewer_id=user.id)
            if selected.kind == "household":
                item_id = matching_item(db, selected, domain="movie", canonical_id=imdb_id)
                if item_id is None:
                    from omakase.plus.household_catalog import (
                        actor_catalog_metadata,
                        catalog_connections,
                    )
                    from omakase.plus.household_views import library_name

                    cached = get_movie(db, user.id, imdb_id)
                    if not cached or not cached.get("identity_verified"):
                        catalog_config = catalog_connections(db, selected)
                        reader = (
                            service_factory(db, user)
                            if service_factory
                            else MovieService(db, catalog_config)
                        )
                        await reader.detail(user.id, imdb_id)
                    metadata = actor_catalog_metadata(
                        db, viewer_id=user.id, domain="movie", canonical_id=imdb_id
                    )
                    return templates.TemplateResponse(
                        request,
                        "household_catalog_preview.html",
                        {
                            **context(request, user),
                            "scope": selected,
                            "library_name": library_name(db, selected),
                            "metadata": metadata,
                            "domain": "movie",
                            "canonical_id": imdb_id,
                            "poster_url": f"/plus/movies/{imdb_id}/artwork",
                            "csrf_token": csrf_token(request),
                        },
                        headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
                    )
                return RedirectResponse(
                    detail_url(selected, item_id),
                    status_code=307,
                    headers={"Cache-Control": "private, no-store", "Vary": "Cookie"},
                )
        from omakase.plus.for_you import request_status
        from omakase.plus.movie_history import MovieHistoryService
        from omakase.plus.movies import _imdb
        from omakase.plus.web_playback_navigation import enabled as web_player_enabled

        try:
            _imdb(imdb_id)
            svc = service(db, user)
            cloud_ids, cloud_error = await cloud_availability(db, user)
            cards = await svc.list(user.id) if cloud_ids is None else []
            card = next((item for item in cards if item["imdb_id"] == imdb_id), None)
            if card is None:
                card = await svc.detail(user.id, imdb_id)
            if card is None:
                raise HTTPException(404, "Movie not found")
            card["history"] = MovieHistoryService(svc).get(user.id, imdb_id)
            card = present_movie(card, cloud_ids=cloud_ids)
            title_request = request_status(db, user.id, "movie", imdb_id, movie_card=card)
            removal = cleanup_status(db, user.id, imdb_id)
            if removal == "removed" and (
                card.get("plex_present") or card.get("requested") or card.get("request_observation")
            ):
                removal = None
        except MovieError as exc:
            if exc.code == "invalid_input":
                raise HTTPException(422, "Invalid movie identity") from None
            return await render(
                request, user, db, action_card={"title": "Movie unavailable", "detail": exc.detail}
            )
        return templates.TemplateResponse(
            request,
            "movie_detail.html",
            {
                **context(request, user),
                "card": card,
                "cloud_playback": cloud_ids is not None,
                "cloud_error": cloud_error,
                "title_request": title_request,
                "notice": notice
                if notice
                in {
                    "saved",
                    "updated",
                    "request_updated",
                    "request_unchanged",
                    "request_saved",
                    "request_failed",
                }
                else "",
                "movies_error": "",
                "cleanup_enabled": movie_cleanup_enabled(),
                "cleanup_status": removal,
                "removal_refresh": request.query_params.get("removal_refresh") != "1",
                "watch_in_app": web_player_enabled(),
            },
            headers={"Cache-Control": "private, no-store"},
        )

    request_reads: OrderedDict[
        tuple[int, str, str, bool],
        tuple[float, object, tuple[tuple[str, str], ...], tuple[bool, bool] | None],
    ] = OrderedDict()
    polling: set[int] = set()
    request_read_after: OrderedDict[int, float] = OrderedDict()

    @router.get("/movies/{imdb_id}/request-status")
    async def request_timeline_status(imdb_id: str, user=Depends(require_user), db=Depends(get_db)):
        from omakase.plus.for_you import (
            _MOVIE_PROGRESS_LABELS,
            ForYouError,
            _movie_status,
            request_status,
        )

        headers = {"Cache-Control": "private, no-store", "Vary": "Cookie"}
        try:
            if households_enabled() if callable(households_enabled) else households_enabled:
                from omakase.plus.households import selected_scope

                if selected_scope(db, viewer_id=user.id).kind != "personal":
                    raise HTTPException(404, "Request unavailable", headers=headers)
            cloud_playback = playback_providers.select(db, user.id).cloud_route
            movie_card = {"imdb_id": imdb_id, "cloud_match": False} if cloud_playback else None
            status = request_status(db, user.id, "movie", imdb_id, movie_card=movie_card)
        except ForYouError:
            raise HTTPException(404, "Request unavailable", headers=headers) from None
        if status is None:
            raise HTTPException(404, "Request unavailable", headers=headers)

        async def read(operation, *args):
            try:
                async with asyncio.timeout(5):
                    return await operation(*args)
            except (MovieError, TimeoutError):
                return None

        key = (user.id, imdb_id, status["timeline"][0]["occurred_at"], cloud_playback)
        cached = request_reads.get(key)
        now = monotonic()
        if status["can_check"] and (cached is None or cached[0] <= now):
            earliest = request_read_after.get(user.id, 0)
            if user.id in polling or len(polling) >= 4 or earliest > now:
                retry_after = max(5, ceil(earliest - now))
                raise HTTPException(
                    429,
                    "Status check in progress",
                    headers={**headers, "Retry-After": str(retry_after)},
                )
            polling.add(user.id)
            request_read_after[user.id] = now + 15
            request_read_after.move_to_end(user.id)
            if len(request_read_after) > 128:
                request_read_after.popitem(last=False)
            try:
                svc = service(db, user)
                inventory, observation = await asyncio.gather(
                    cloud_availability(db, user, timeout=5)
                    if cloud_playback
                    else read(svc.indexed_requests, user.id, [imdb_id]),
                    read(svc.observe_request, user.id, imdb_id),
                )
            finally:
                polling.discard(user.id)
            observations = dict(cached[2]) if cached else {}
            if observation and observation[0] in _MOVIE_PROGRESS_LABELS:
                observations.setdefault(*observation)
            if cloud_playback:
                cloud_ids, cloud_error = inventory
                library, cloud_match = None, (imdb_id in (cloud_ids or ()), cloud_error)
            else:
                library, cloud_match = (inventory or {}).get(imdb_id), None
            cached = (monotonic() + 50, library, tuple(observations.items()), cloud_match)
            request_reads[key] = cached
            request_reads.move_to_end(key)
            if len(request_reads) > 128:
                request_reads.popitem(last=False)
        availability = None
        if cached and status["can_check"]:
            if movie_card is not None:
                matched, failed = cached[3]
                movie_card["cloud_match"] = matched
                display = present_movie(movie_card, cloud_ids={imdb_id} if matched else set())[
                    "display"
                ]
                availability = {
                    "label": display["status_label"],
                    "detail": (
                        "Could not check the Real-Debrid library right now."
                        if failed
                        else display["summary"]
                    ),
                    "tone": display["status_tone"],
                }
            status = _movie_status(
                db, user.id, imdb_id, cached[1], movie_card=movie_card, observations=cached[2]
            )
        if status is None:
            raise HTTPException(404, "Request unavailable", headers=headers)
        from omakase.plus.web_playback_navigation import enabled as web_player_enabled

        can_play = status["state"] in {"in_library", "ready"} and web_player_enabled()
        return JSONResponse(
            {
                **request_payload(status),
                "label": status["status_label"],
                "availability": availability,
                "play_url": f"/plus/watch/title/movie/{imdb_id}?play=1" if can_play else None,
            },
            headers=headers,
        )

    # The Requests dashboard reads the same shared status model as the movie
    # timeline and the native client. Reads are bounded per viewer; explicit
    # actions stay separate and CSRF-checked.
    request_status_reads: OrderedDict[int, tuple[float, bool, dict]] = OrderedDict()
    request_status_polling: set[int] = set()
    request_status_read_after: OrderedDict[int, float] = OrderedDict()

    def _no_store():
        return {"Cache-Control": "private, no-store", "Vary": "Cookie"}

    def requests_scope(db, user):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.households import selected_scope

            if selected_scope(db, viewer_id=user.id).kind != "personal":
                raise HTTPException(404, "Requests unavailable", headers=_no_store())

    def request_payload(model):
        return {
            key: model[key]
            for key in (
                "identity",
                "kind",
                "media_kind",
                "title_id",
                "title",
                "season",
                "stage",
                "stage_label",
                "stages",
                "state",
                "label",
                "status_label",
                "detail",
                "category",
                "manual",
                "manual_note",
                "stopped_at",
                "reason",
                "unknown_reason",
                "reason_text",
                "last_checked_at",
                "last_confirmed_at",
                "next_action",
                "can_check",
                "can_retry",
                "resolved",
                "removed",
                "candidates_rejected",
                "candidates_considered",
                "detail_href",
                "timeline",
            )
        }

    def requests_context(request, user):
        base = context(request, user)
        base["active_page"] = "requests"
        return base

    def wants_json(request):
        return "application/json" in request.headers.get("accept", "")

    @router.get("/requests", response_class=HTMLResponse)
    async def requests_page(request: Request, user=Depends(require_user), db=Depends(get_db)):
        from omakase.plus import for_you

        requests_scope(db, user)
        try:
            page = await for_you.dashboard(db, user.id)
        except for_you.ForYouError:
            raise HTTPException(404, "Requests unavailable", headers=_no_store()) from None
        return templates.TemplateResponse(
            request,
            "requests.html",
            {
                **requests_context(request, user),
                "categories": page["categories"],
                "counts": page["counts"],
                "csrf_token": csrf_token(request),
            },
            headers=_no_store(),
        )

    @router.get("/requests/status")
    async def requests_status(user=Depends(require_user), db=Depends(get_db)):
        """Read-only dashboard projection with the movie timeline backoff."""
        from omakase.plus import for_you

        headers = _no_store()
        requests_scope(db, user)
        now = monotonic()
        cloud_playback = playback_providers.select(db, user.id).cloud_route
        cached = request_status_reads.get(user.id)
        if cached is not None and cached[0] > now and cached[1] == cloud_playback:
            return JSONResponse(cached[2], headers=headers)
        earliest = request_status_read_after.get(user.id, 0.0)
        if user.id in request_status_polling or len(request_status_polling) >= 4 or earliest > now:
            raise HTTPException(
                429,
                "Status check in progress",
                headers={**headers, "Retry-After": str(max(5, ceil(earliest - now)))},
            )
        request_status_polling.add(user.id)
        request_status_read_after[user.id] = now + 15
        request_status_read_after.move_to_end(user.id)
        if len(request_status_read_after) > 128:
            request_status_read_after.popitem(last=False)
        try:
            page = await for_you.dashboard(db, user.id)
        except for_you.ForYouError:
            raise HTTPException(404, "Requests unavailable", headers=headers) from None
        finally:
            request_status_polling.discard(user.id)
        payload = {
            "categories": [
                {
                    "key": category["key"],
                    "title": category["title"],
                    "requests": [request_payload(model) for model in category["requests"]],
                }
                for category in page["categories"]
            ],
            "counts": page["counts"],
        }
        request_status_reads[user.id] = (monotonic() + 50, cloud_playback, payload)
        request_status_reads.move_to_end(user.id)
        if len(request_status_reads) > 128:
            request_status_reads.popitem(last=False)
        return JSONResponse(payload, headers=headers)

    @router.get("/requests/{kind}/{title_id}", response_class=HTMLResponse)
    async def requests_detail(
        request: Request,
        kind: Literal["movie", "anime", "tv"],
        title_id: str,
        checked: int = 0,
        retried: int = 0,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus import for_you, request_candidates

        requests_scope(db, user)
        try:
            model = await for_you.request_detail(db, user.id, kind, title_id)
        except for_you.ForYouError as error:
            raise HTTPException(error.status or 404, error.detail, headers=_no_store()) from None
        return templates.TemplateResponse(
            request,
            "request_detail.html",
            {
                **requests_context(request, user),
                "item": model,
                # Owner review W-06: the page states what Omakase's own search found and
                # why each candidate failed, or says plainly that the search is switched
                # off or could not run. It never presents a re-stamped check time as
                # progress. The search is bounded and cached, so rendering stays cheap.
                "request_search": await request_candidates.refresh(
                    db,
                    user.id,
                    kind,
                    title_id,
                    request_candidates.JudgeRequest(
                        title=str(
                            getattr(model, "title", "")
                            or getattr(model, "title_label", "")
                            or title_id
                        ),
                        kind=kind,
                        year=getattr(model, "year", None),
                        season=getattr(model, "season", None),
                        episode=getattr(model, "episode", None),
                    ),
                ),
                "notice": "checked" if checked else ("retried" if retried else ""),
                "csrf_token": csrf_token(request),
            },
            headers=_no_store(),
        )

    @router.post("/requests/{kind}/{title_id}/check")
    async def requests_check(
        request: Request,
        kind: Literal["movie", "anime", "tv"],
        title_id: str,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        """Explicit status check. Reads provider/library state; no new request."""
        from omakase.plus import for_you

        require_csrf(request, csrf)
        requests_scope(db, user)
        try:
            model = await for_you.refresh_request(db, user.id, kind, title_id)
        except for_you.ForYouError as error:
            if wants_json(request):
                return JSONResponse(
                    {"detail": error.detail}, status_code=error.status, headers=_no_store()
                )
            raise HTTPException(error.status, error.detail, headers=_no_store()) from None
        request_status_reads.pop(user.id, None)
        if wants_json(request):
            return JSONResponse(request_payload(model), headers=_no_store())
        return RedirectResponse(
            f"/plus/requests/{kind}/{title_id}?checked=1", status_code=303, headers=_no_store()
        )

    @router.post("/requests/{kind}/{title_id}/retry")
    async def requests_retry(
        request: Request,
        kind: Literal["movie", "anime", "tv"],
        title_id: str,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        """Explicit idempotent retry; returns the existing request, never a second one."""
        from omakase.plus import for_you

        require_csrf(request, csrf)
        requests_scope(db, user)
        try:
            model = await for_you.retry_request(db, user.id, kind, title_id)
        except for_you.ForYouError as error:
            if wants_json(request):
                return JSONResponse(
                    {"detail": error.detail}, status_code=error.status, headers=_no_store()
                )
            raise HTTPException(error.status, error.detail, headers=_no_store()) from None
        request_status_reads.pop(user.id, None)
        if wants_json(request):
            return JSONResponse(request_payload(model), headers=_no_store())
        return RedirectResponse(
            f"/plus/requests/{kind}/{title_id}?retried=1", status_code=303, headers=_no_store()
        )

    async def action(request, user, db, operation, **kwargs):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.households import selected_scope

            if selected_scope(db, viewer_id=user.id).kind == "household":
                raise HTTPException(
                    409,
                    "Use the checked shared-library request flow. No operational action was sent.",
                    headers={"Cache-Control": "private, no-store"},
                )
        try:
            return reply(request, await getattr(service(db, user), operation)(user.id, **kwargs))
        except MovieError as exc:
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    exc.as_dict(), status_code=409, headers={"Cache-Control": "private, no-store"}
                )
            return await render(
                request, user, db, action_card={"title": "Movie check", "detail": exc.detail}
            )

    @router.post("/movies/request")
    async def request_movie(
        request: Request,
        imdb_id: str = Form(..., max_length=12),
        tmdb_id: int = Form(...),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(request, user, db, "request", imdb_id=imdb_id, tmdb_id=tmdb_id)

    @router.post("/movies/{imdb_id}/verify")
    async def verify(
        request: Request,
        imdb_id: str,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(request, user, db, "verify", imdb_id=imdb_id)

    @router.post("/movies/{imdb_id}/refresh")
    async def refresh(
        request: Request,
        imdb_id: str,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(request, user, db, "refresh", imdb_id=imdb_id)

    def guard_personal_removal(db, user):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.households import selected_scope

            if selected_scope(db, viewer_id=user.id).kind == "household":
                raise HTTPException(409, "Movie removal is available from your personal library.")

    @router.post("/movies/{imdb_id}/plex-review", response_class=HTMLResponse)
    async def plex_review(
        request: Request,
        imdb_id: str,
        mode: str = Form("lookup"),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        from omakase.plus.movie_cleanup import verify_movie_cleanup_copy
        from omakase.plus.movie_cleanup_confirmation import issue_movie_confirmation
        from omakase.plus.movie_cleanup_worker import verify_manual_cleanup_selection
        from omakase.plus.movie_history import MovieHistoryService
        from omakase.plus.provider_limits import ProviderRateLimit

        removal = mode == "removal"
        if removal:
            guard_personal_removal(db, user)
        svc = service(db, user)
        movie = get_movie(db, user.id, imdb_id)
        history = MovieHistoryService(svc).get(user.id, imdb_id) if movie else None
        if not history or history["status"] != "completed":
            raise HTTPException(404, "Watched movie not found")
        link, result = None, "matched"
        try:
            link = await svc.plex_review(user.id, imdb_id)
        except MovieError as exc:
            result = "absent" if exc.code == "plex_not_found" else "unknown"
        cleanup = confirmation = None
        if link and movie_cleanup_enabled():
            try:
                async with asyncio.timeout(10):
                    proof = await verify_movie_cleanup_copy(svc, user.id, imdb_id)
                    cleanup = "verified" if proof.owned else "protected"
                    if removal and proof.owned:
                        tag, selection = await verify_manual_cleanup_selection(
                            svc, user.id, imdb_id, proof
                        )
                        confirmation = issue_movie_confirmation(
                            db,
                            user_id=user.id,
                            imdb_id=imdb_id,
                            scope=svc._scope,
                            tag=tag,
                            selection_digest=selection,
                        )
            except MovieCleanupUnavailable as exc:
                cleanup = "protected" if str(exc) == "movie_in_shared_library" else "unverified"
            except (MovieError, ProviderRateLimit, TimeoutError):
                cleanup = "unverified"
        payload = {"result": result, "plex_url": link}
        if cleanup:
            payload["cleanup"] = cleanup
        if confirmation:
            payload["confirmation"] = confirmation
        if request.headers.get("accept") == "application/json":
            return JSONResponse(
                payload,
                headers={"Cache-Control": "private, no-store"},
            )
        return templates.TemplateResponse(
            request,
            "movie_plex_review.html",
            {**context(request, user), "movie": movie, "reviewing_removal": removal, **payload},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/movies/{imdb_id}/removal")
    async def removal_status(imdb_id: str, user=Depends(require_user), db=Depends(get_db)):
        guard_personal_removal(db, user)
        if not get_movie(db, user.id, imdb_id):
            raise HTTPException(404, "Movie not found")
        return JSONResponse(
            {"status": cleanup_status(db, user.id, imdb_id) or "idle"},
            headers={"Cache-Control": "private, no-store"},
        )

    @router.post("/movies/{imdb_id}/removal", response_class=HTMLResponse)
    async def remove_copy(
        request: Request,
        imdb_id: str,
        confirmation: str = Form("", max_length=64),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.movie_cleanup_confirmation import confirm_movie_cleanup

        require_csrf(request, csrf)
        guard_personal_removal(db, user)
        movie = get_movie(db, user.id, imdb_id)
        if not movie:
            raise HTTPException(404, "Movie not found")
        headers = {"Cache-Control": "private, no-store"}
        json_reply = request.headers.get("accept") == "application/json"
        try:
            confirm_movie_cleanup(service(db, user), user.id, imdb_id, confirmation)
        except (MovieCleanupUnavailable, MovieError):
            if json_reply:
                return JSONResponse({"result": "review_required"}, status_code=409, headers=headers)
            return templates.TemplateResponse(
                request,
                "movie_plex_review.html",
                {
                    **context(request, user),
                    "movie": movie,
                    "reviewing_removal": True,
                    "result": "review_required",
                },
                status_code=409,
                headers=headers,
            )
        if json_reply:
            return JSONResponse({"result": "queued"}, status_code=202, headers=headers)
        return RedirectResponse(
            f"/plus/movies/{imdb_id}#movie-after-watch", status_code=303, headers=headers
        )

    @router.post("/movies/history")
    async def save_history(
        request: Request,
        imdb_id: str = Form(...),
        tmdb_id: int = Form(...),
        status: str = Form(""),
        rating: str = Form(""),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        from omakase.plus.movie_history import UNSET, MovieHistoryService

        try:
            form = await request.form()
            history = await MovieHistoryService(service(db, user)).save(
                user.id,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                status=status if status else UNSET,
                rating=(float(rating) if rating else None) if "rating" in form else UNSET,
            )
        except (MovieError, ValueError) as exc:
            detail = (
                exc.detail
                if isinstance(exc, MovieError)
                else "Choose a valid watched status and rating."
            )
            return await render(
                request, user, db, action_card={"title": "Rating not saved", "detail": detail}
            )
        fragment = "#movie-after-watch" if history.get("status") == "completed" else "#movie-rating"
        return RedirectResponse(f"/plus/movies/{imdb_id}?notice=saved{fragment}", status_code=303)

    @router.post("/movies/sync")
    async def sync(
        request: Request,
        offset: int | None = Form(None),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        from omakase.plus.movie_history import MovieHistoryService

        try:
            result = await MovieHistoryService(service(db, user)).sync_plex(user.id, offset=offset)
        except MovieError as exc:
            return await render(
                request, user, db, action_card={"title": "Sync unavailable", "detail": exc.detail}
            )
        if "application/json" not in request.headers.get("accept", ""):
            return RedirectResponse(
                f"/plus/movies?notice=synced&remaining={result['remaining']}&imported={result['imported']}&skipped={result['skipped']}",
                status_code=303,
            )
        return reply(request, result)

    @router.get("/movies/history/export")
    async def export(user=Depends(require_user), db=Depends(get_db)):
        from omakase.plus.movie_history import MovieHistoryService

        return JSONResponse(
            MovieHistoryService(service(db, user)).export_history(user.id),
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": 'attachment; filename="omakase-movies.json"',
            },
        )

    @router.get("/discover/movies", response_class=RedirectResponse)
    async def discover_movies_redirect(request: Request, user=Depends(require_user)):
        params = {"kind": "movies"}
        for field in ("genre_id", "sort", "include_completed"):
            if field in request.query_params:
                params[field] = request.query_params[field]
        return RedirectResponse("/plus/discover?" + urlencode(params), status_code=307)

    return router
