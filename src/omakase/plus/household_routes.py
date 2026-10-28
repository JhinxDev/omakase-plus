"""Default-disabled household and scoped media views; no acquisition or deletion."""

from __future__ import annotations

import os
import re
import sqlite3
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from omakase.plus import households as core
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_security import csrf_token, require_csrf

HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").casefold() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Not found", headers=HEADERS)


async def _form(request, names):
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(415, "Use the household form", headers=HEADERS)
    form = await request.form()
    if set(form) - set(names) - {"csrf_token"} or any(
        len(form.getlist(k)) != 1 or not isinstance(form[k], str) for k in form
    ):
        raise HTTPException(422, "Invalid household form", headers=HEADERS)
    require_csrf(request, form.get("csrf_token", ""))
    return form


def create_household_router(
    templates, context_factory=None, *, enabled: bool = False, movie_service_factory=None
):
    def gate():
        _private()
        if not (enabled() if callable(enabled) else enabled):
            raise HTTPException(404, "Not found", headers=HEADERS)

    bounded = bounded_form_route(8192, "Household form is too large", guard=gate)

    class PrivateHouseholdRoute(bounded):
        def get_route_handler(self):
            handler = super().get_route_handler()

            async def wrapped(request):
                try:
                    response = await handler(request)
                except HTTPException as error:
                    error.headers = {**(error.headers or {}), **HEADERS}
                    raise
                response.headers.update(HEADERS)
                return response

            return wrapped

    router = APIRouter(route_class=PrivateHouseholdRoute)

    def scope(db, user, library_id):
        return core.resolve_scope(db, viewer_id=user.id, library_id=library_id)

    def render(
        request,
        user,
        db,
        *,
        page="overview",
        error="",
        status=200,
        invite=None,
        invite_preview=None,
    ):
        state = core.overview(db, viewer_id=user.id)
        context = (
            context_factory(request, user, active_page="settings")
            if context_factory
            else {"email": user.email, "active_page": "settings"}
        )
        preview = core.promotion_preview(db, user_id=user.id) if page == "create" else None
        invitations = []
        services = []
        if state["household"]:
            selected = scope(db, user, state["household"]["id"])
            services = core.connector_status(db, selected)
            if selected.role == "owner":
                invitations = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,expires_at,consumed_at,revoked_at FROM household_invites WHERE library_id=? ORDER BY created_at DESC LIMIT 20",
                        (selected.library_id,),
                    )
                ]
        return templates.TemplateResponse(
            request,
            "household.html",
            {
                **context,
                **state,
                "csrf_token": csrf_token(request),
                "household_page": page,
                "household_now": int(time.time()),
                "household_error": error,
                "promotion": preview,
                "created_invite": invite,
                "invite_preview": invite_preview,
                "invitations": invitations,
                "shared_services": services,
            },
            status_code=status,
            headers=HEADERS,
        )

    def failure(request, user, db, error, page="overview"):
        message = (
            str(error)
            if isinstance(error, core.HouseholdError)
            else "Household state changed. Reload and try again."
        )
        return render(
            request,
            user,
            db,
            page=page,
            error=message,
            status=error.status if isinstance(error, core.HouseholdError) else 409,
        )

    def redirect():
        return RedirectResponse("/plus/settings/household", status_code=303, headers=HEADERS)

    @router.get("/settings/household", response_class=HTMLResponse)
    async def home(request: Request, user=Depends(require_user), db=Depends(get_db)):
        return render(request, user, db)

    @router.get("/settings/household/create", response_class=HTMLResponse)
    async def preview(request: Request, user=Depends(require_user), db=Depends(get_db)):
        try:
            return render(request, user, db, page="create")
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    @router.post("/settings/household/create")
    async def create(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(
            request,
            {
                "name",
                "starting_library",
                "preview_digest",
                "members_can_add",
                "acquisition_requests",
            },
        )
        if form.get("starting_library") not in {"empty", "promote"}:
            return failure(
                request,
                user,
                db,
                core.HouseholdError("Choose the starting library.", 422),
                "create",
            )
        try:
            core.create_household(
                db,
                user_id=user.id,
                name=form.get("name", ""),
                promote=form["starting_library"] == "promote",
                preview_digest=form.get("preview_digest", ""),
                members_can_add=form.get("members_can_add") == "1",
                acquisition_requests=form.get("acquisition_requests") == "1",
            )
            return redirect()
        except (core.HouseholdError, sqlite3.IntegrityError) as error:
            return failure(request, user, db, error, "create")

    @router.post("/settings/household/{library_id}/invite")
    async def invite(
        library_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        await _form(request, set())
        try:
            return render(
                request, user, db, invite=core.create_invite(db, scope(db, user, library_id))
            )
        except (core.HouseholdError, sqlite3.IntegrityError) as error:
            return failure(request, user, db, error)

    @router.post("/settings/household/{library_id}/invite/{invite_id}/revoke")
    async def revoke(
        library_id: str,
        invite_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        await _form(request, set())
        try:
            core.revoke_invite(db, scope(db, user, library_id), invite_id)
            return redirect()
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    @router.get("/households/join", response_class=HTMLResponse)
    async def join_page(request: Request, user=Depends(require_user), db=Depends(get_db)):
        return render(request, user, db, page="join")

    @router.post("/households/invitation/preview", response_class=HTMLResponse)
    async def invitation_preview(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request, {"invite_token"})
        try:
            details = core.inspect_invite(db, token=form.get("invite_token", ""))
            return render(
                request,
                user,
                db,
                page="join",
                invite_preview={**details, "token": form["invite_token"]},
            )
        except core.HouseholdError as error:
            return failure(request, user, db, error, "join")

    @router.post("/households/join")
    async def join(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request, {"invite_token", "preview_library_id", "display_name"})
        try:
            preview = core.inspect_invite(db, token=form.get("invite_token", ""))
            if preview["library_id"] != form.get("preview_library_id"):
                raise core.HouseholdError("Review this invitation before joining.", 422)
            core.accept_invite(db, viewer_id=user.id, token=form.get("invite_token", ""))
            return redirect()
        except (core.HouseholdError, sqlite3.IntegrityError) as error:
            return failure(request, user, db, error, "join")

    @router.post("/settings/household/{library_id}/leave")
    async def leave(
        library_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        await _form(request, set())
        try:
            core.leave_or_revoke(db, scope(db, user, library_id))
            return redirect()
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    @router.post("/settings/household/{library_id}/members/{member_id}/revoke")
    async def remove(
        library_id: str,
        member_id: int,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        await _form(request, set())
        try:
            core.leave_or_revoke(db, scope(db, user, library_id), member_id=member_id)
            return redirect()
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    @router.post("/settings/household/{library_id}/policy")
    async def policy(
        library_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        form = await _form(request, {"members_can_add", "acquisition_requests"})
        try:
            core.set_policy(
                db,
                scope(db, user, library_id),
                members_can_add=form.get("members_can_add") == "1",
                acquisition_requests=form.get("acquisition_requests") == "1",
            )
            return redirect()
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    @router.post("/settings/household/{library_id}/connections")
    async def connection(
        library_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        form = await _form(request, {"integration"})
        try:
            core.bind_connector(
                db, scope(db, user, library_id), integration=form.get("integration", "")
            )
            return redirect()
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    @router.post("/settings/household/select")
    async def select(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request, {"library_id", "next"})
        try:
            core.select_library(db, scope(db, user, form.get("library_id", "")))
            destination = form.get("next", "")
            if destination not in {
                "/plus/home",
                "/plus/library",
                "/plus/movies",
                "/plus/tv",
                "/plus/search",
                "/plus/settings/household",
            }:
                destination = "/plus/home"
            return RedirectResponse(destination, status_code=303, headers=HEADERS)
        except core.HouseholdError as error:
            return failure(request, user, db, error)

    def media_scope(db, user, library_id):
        try:
            return scope(db, user, library_id)
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None

    @router.get("/libraries/{library_id}", response_class=HTMLResponse)
    async def library_page(
        library_id: str,
        request: Request,
        q: str = "",
        offset: int = 0,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.household_views import collection

        selected = media_scope(db, user, library_id)
        try:
            data = collection(db, selected, query=q, offset=offset)
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        context = (
            context_factory(request, user, active_page="library")
            if context_factory
            else {"email": user.email}
        )
        return templates.TemplateResponse(
            request,
            "household_media.html",
            {**context, **data, "item": None, "csrf_token": csrf_token(request)},
            headers=HEADERS,
        )

    @router.get("/libraries/{library_id}/items/{item_id}", response_class=HTMLResponse)
    async def media_page(
        library_id: str,
        item_id: str,
        request: Request,
        season: int | None = None,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.household_views import detail, library_name

        selected = media_scope(db, user, library_id)
        archived = core.LibraryRepository(db, selected).item(item_id, include_removed=True)
        if archived["state"] == "removed":
            histories = core.PersonalTrackingRepository(db, viewer_id=user.id).history(
                archived["domain"]
            )
            key = (
                "anilist_id"
                if archived["domain"] == "anime"
                else "imdb_id"
                if archived["domain"] == "movie"
                else "tmdb_id"
            )
            personal = next(
                (
                    row
                    for row in histories
                    if str(row[key]) == archived["canonical_id"]
                    and (
                        archived["domain"] != "anime"
                        or row["season_number"] == archived["season_number"]
                    )
                ),
                None,
            )
            interest = db.execute(
                "SELECT keep_available,active_use FROM library_item_interest WHERE library_item_id=? AND user_id=?",
                (item_id, user.id),
            ).fetchone()
            context = (
                context_factory(request, user, active_page="library")
                if context_factory
                else {"email": user.email}
            )
            return templates.TemplateResponse(
                request,
                "household_item_removed.html",
                {
                    **context,
                    "scope": selected,
                    "item": dict(archived),
                    "history": personal,
                    "has_interest": bool(interest and (interest[0] or interest[1])),
                    "csrf_token": csrf_token(request),
                },
                headers=HEADERS,
            )
        try:
            item = detail(db, selected, item_id=item_id)
            movie_status = None
            if item["domain"] == "movie":
                from omakase.plus.movies import MovieConnections, MovieService

                latest = db.execute(
                    "SELECT id FROM language_request_policies WHERE actor_user_id=? AND library_item_id=? AND domain='movie' ORDER BY created_at DESC,id DESC LIMIT 1",
                    (user.id, item_id),
                ).fetchone()
                if latest:
                    svc = (
                        movie_service_factory(db, user)
                        if movie_service_factory
                        else MovieService(db, MovieConnections())
                    )
                    status = svc.language_status(user.id, request_id=latest["id"])
                    movie_status = {
                        key: status.get(key)
                        for key in ("state", "code", "detail", "verified_at", "expires_at")
                    }
                    if movie_status["state"] == "ready" and status.get("proof_schema") != 2:
                        movie_status.update(
                            state="verifying",
                            detail="A current personal playback proof is required.",
                        )
                    if movie_status["state"] == "ready":
                        from urllib.parse import urlsplit

                        link = status.get("plex_url")
                        parsed = urlsplit(link) if isinstance(link, str) else None
                        if (
                            parsed
                            and parsed.scheme == "https"
                            and parsed.netloc == "app.plex.tv"
                            and parsed.path == "/desktop/"
                        ):
                            movie_status["plex_url"] = link

            episode_seasons, episodes = [], []
            if item["domain"] == "tv":
                from omakase.plus.household_media import binding, tv_episode_rows

                core._require(
                    season is None or 0 <= season <= 200, "Choose a valid TV season.", 422
                )
                episode_seasons = [
                    row[0]
                    for row in db.execute(
                        "SELECT DISTINCT season_number FROM tv_episodes WHERE user_id=? AND tmdb_id=? ORDER BY season_number",
                        (selected.operational_owner_id, int(item["canonical_id"])),
                    )
                ]
                season = (
                    season
                    if season is not None
                    else next((value for value in episode_seasons if value > 0), 0)
                )
                episodes = tv_episode_rows(
                    db, binding(db, selected, item_id=item_id), season=season
                )

        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        context = (
            context_factory(request, user, active_page="library")
            if context_factory
            else {"email": user.email}
        )
        return templates.TemplateResponse(
            request,
            "household_media.html",
            {
                **context,
                "item": item,
                "scope": selected,
                "library_name": library_name(db, selected),
                "tracking_event_key": uuid.uuid4().hex,
                "movie_status": movie_status,
                "episode_seasons": episode_seasons,
                "episodes": episodes,
                "selected_season": season,
                "csrf_token": csrf_token(request),
            },
            headers=HEADERS,
        )

    @router.get("/libraries/{library_id}/items/{item_id}/artwork")
    async def media_artwork(
        library_id: str,
        item_id: str,
        variant: str = "poster",
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from fastapi.responses import Response
        from starlette.concurrency import run_in_threadpool

        from omakase.plus.artwork_delivery import optimized_artwork
        from omakase.plus.household_media import binding, public_metadata
        from omakase.plus.movie_artwork import movie_artwork
        from omakase.plus.posters import PosterUnavailable, banner_bytes, poster_bytes

        if variant not in {"poster", "backdrop"}:
            raise HTTPException(422, "Invalid artwork variant", headers=HEADERS)
        selected = media_scope(db, user, library_id)
        try:
            media = binding(db, selected, item_id=item_id)
            # Membership and exact item/resource identity checked before any cache/HTTP access.
            if media.domain == "anime":
                payload, kind = await (
                    banner_bytes(int(media.canonical_id))
                    if variant == "backdrop"
                    else poster_bytes(int(media.canonical_id))
                )
            else:
                metadata = public_metadata(db, media)
                url = metadata.get("backdrop_url" if variant == "backdrop" else "poster_url")
                if not url:
                    raise PosterUnavailable("Artwork unavailable")
                payload, kind = await movie_artwork(url)
            payload, kind = await run_in_threadpool(
                optimized_artwork, payload, kind="banner" if variant == "backdrop" else "poster"
            )
            # A revoked member must never reuse a cached private response.
            core.revalidate_scope(db, selected)
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except PosterUnavailable:
            raise HTTPException(404, "Artwork unavailable", headers=HEADERS) from None
        return Response(payload, media_type=kind, headers=HEADERS)

    @router.post("/libraries/{library_id}/items/{item_id}/track")
    async def media_track(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        await _form(request, set())
        try:
            core.PersonalTrackingRepository(db, viewer_id=user.id).start(
                scope(db, user, library_id), item_id=item_id
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/items/{item_id}/interest")
    async def media_interest(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        form = await _form(request, {"keep"})
        try:
            core.LibraryRepository(db, scope(db, user, library_id)).set_interest(
                item_id, keep=form.get("keep") == "1"
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/items/{item_id}/watch")
    async def media_watch(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.household_media import binding, save_anime_watch
        from omakase.plus.watch.models import WatchEvent

        form = await _form(request, {"kind", "episode", "rating", "event_key"})
        try:
            kind = form.get("kind")
            core._require(kind in {"progress", "completed"}, "Choose a watching action.", 422)
            episode = int(form.get("episode", ""))
            core._require(1 <= episode <= 10000, "Choose an episode from 1 to 10000.", 422)
            rating = int(form["rating"]) if form.get("rating") else None
            media = binding(db, scope(db, user, library_id), item_id=item_id, for_history=True)
            save_anime_watch(
                db,
                media,
                event=WatchEvent(
                    source="manual",
                    kind=kind,
                    episode=episode,
                    event_key=form.get("event_key", ""),
                    confidence="manual",
                ),
                score_ten=rating,
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except ValueError:
            raise HTTPException(422, "Check the watching fields.", headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/items/{item_id}/history")
    async def media_history(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.household_media import binding, save_series_or_movie_history

        form = await _form(request, {"status", "rating", "rating_action"})
        try:
            kwargs = {"status": form.get("status", "")}
            action = form.get("rating_action", "keep")
            core._require(action in {"keep", "save", "clear"}, "Choose a rating action.", 422)
            if action != "keep":
                kwargs["rating"] = float(form.get("rating", "")) if action == "save" else None
            save_series_or_movie_history(
                db,
                binding(db, scope(db, user, library_id), item_id=item_id, for_history=True),
                **kwargs,
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except ValueError:
            raise HTTPException(
                422, "Check the personal tracking fields.", headers=HEADERS
            ) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/items/{item_id}/episode")
    async def media_episode(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.household_media import binding, save_tv_episode

        form = await _form(request, {"season", "episode", "watched"})
        try:
            season, episode = int(form.get("season", "")), int(form.get("episode", ""))
            core._require(form.get("watched") in {"0", "1"}, "Choose a watched state.", 422)
            save_tv_episode(
                db,
                binding(db, scope(db, user, library_id), item_id=item_id),
                season=season,
                episode=episode,
                watched=form["watched"] == "1",
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except ValueError:
            raise HTTPException(422, "Check the episode fields.", headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}?season={season}",
            status_code=303,
            headers=HEADERS,
        )

    @router.post("/libraries/{library_id}/items/{item_id}/remove-membership")
    async def remove_membership(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        await _form(request, set())
        try:
            core.LibraryRepository(db, scope(db, user, library_id)).remove_membership(item_id)
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/items/{item_id}/withdraw-interest")
    async def withdraw_interest(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        await _form(request, set())
        try:
            core.LibraryRepository(db, scope(db, user, library_id)).withdraw_interest(item_id)
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.get("/history/anime/{entry_id}", response_class=HTMLResponse)
    async def personal_anime_history(
        entry_id: int, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        row = db.execute(
            "SELECT w.*,(SELECT i.title FROM watch_library_links link JOIN library_items i ON i.id=link.library_item_id WHERE link.user_id=w.user_id AND link.watch_entry_id=w.id ORDER BY i.created_at DESC,i.id LIMIT 1) AS title,(SELECT e.score_ten FROM watch_events e WHERE e.user_id=w.user_id AND e.watch_entry_id=w.id AND e.disposition='accepted' AND e.score_ten IS NOT NULL ORDER BY e.id DESC LIMIT 1) AS rating FROM watch_entries w WHERE w.id=? AND w.user_id=?",
            (entry_id, user.id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Personal history not available", headers=HEADERS)
        context = (
            context_factory(request, user, active_page="activity")
            if context_factory
            else {"email": user.email}
        )
        return templates.TemplateResponse(
            request,
            "household_personal_history.html",
            {
                **context,
                "entry": dict(row),
                "csrf_token": csrf_token(request),
                "tracking_event_key": uuid.uuid4().hex,
            },
            headers=HEADERS,
        )

    @router.post("/history/anime/{entry_id}")
    async def personal_anime_update(
        entry_id: int, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        from omakase.plus.household_media import save_anime_personal_entry
        from omakase.plus.watch.models import WatchEvent

        form = await _form(request, {"kind", "episode", "rating", "event_key"})
        try:
            core._require(
                form.get("kind") in {"progress", "completed"},
                "Choose a personal watching action.",
                422,
            )
            episode = int(form.get("episode", ""))
            core._require(1 <= episode <= 10000, "Choose a valid episode.", 422)
            rating = int(form["rating"]) if form.get("rating") else None
            save_anime_personal_entry(
                db,
                viewer_id=user.id,
                entry_id=entry_id,
                event=WatchEvent(
                    source="manual",
                    kind=form["kind"],
                    episode=episode,
                    event_key=form.get("event_key", ""),
                    confidence="manual",
                ),
                score_ten=rating,
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except ValueError:
            raise HTTPException(
                422, "Check the personal watching fields.", headers=HEADERS
            ) from None
        return RedirectResponse(f"/plus/history/anime/{entry_id}", status_code=303, headers=HEADERS)

    @router.post("/libraries/{library_id}/items/{item_id}/verify-movie")
    async def verify_shared_movie(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        import re

        from omakase.plus.household_media import binding
        from omakase.plus.household_requests import prepare_movie_language_request
        from omakase.plus.movies import MovieConnections, MovieService

        form = await _form(request, {"request_key"})
        try:
            selected = scope(db, user, library_id)
            media = binding(db, selected, item_id=item_id)
            core._require(media.domain == "movie", "Choose a movie playback check.", 422)
            key = db.execute(
                "SELECT plex_key FROM movie_verifications WHERE user_id=? AND imdb_id=? AND plex_key!='' ORDER BY verified_at DESC LIMIT 1",
                (selected.operational_owner_id, media.canonical_id),
            ).fetchone()
            core._require(
                key is not None and re.fullmatch(r"[1-9][0-9]{0,19}", key[0]),
                "The library owner must index this exact movie before a personal playback check.",
                409,
            )
            request_id = prepare_movie_language_request(
                db, selected, item_id=item_id, idempotency_key=form.get("request_key", "")
            )
            # Commit the immutable local request before the bounded personal HTTP proof.
            db.commit()
            svc = (
                movie_service_factory(db, user)
                if movie_service_factory
                else MovieService(db, MovieConnections())
            )
            await svc.verify_language(user.id, request_id=request_id, rating_key=key[0])
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except ValueError:
            raise HTTPException(
                409,
                "Confirm this library's source and your personal Plex connection before checking playback.",
                headers=HEADERS,
            ) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/items/{item_id}/request-movie")
    async def request_shared_movie(
        library_id: str,
        item_id: str,
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.household_requests import prepare_movie_language_request
        from omakase.plus.movies import MovieConnections, MovieService
        from omakase.plus.product_views import clean_detail

        form = await _form(request, {"request_key"})
        try:
            selected = scope(db, user, library_id)
            request_id = prepare_movie_language_request(
                db,
                selected,
                item_id=item_id,
                idempotency_key=form.get("request_key", ""),
                intent="acquire",
            )
            db.commit()
            svc = (
                movie_service_factory(db, user)
                if movie_service_factory
                else MovieService(db, MovieConnections())
            )
            operation = getattr(svc, "request_language", None)
            if operation is None:
                result = {
                    "code": "pipeline_unavailable",
                    "detail": "The checked movie acquisition adapter is unavailable. The request remains queued; no provider action was sent.",
                }
            else:
                result = await operation(user.id, request_id=request_id)
            event = db.execute(
                "SELECT household_event_id FROM language_request_policies WHERE id=? AND actor_user_id=?",
                (request_id, user.id),
            ).fetchone()
            code = str(result.get("code", "request_checked"))
            if not re.fullmatch(r"[a-z0-9_]{1,80}", code):
                code = "request_checked"
            with core._transaction(db):
                db.execute(
                    "UPDATE library_request_events SET result_code=?,detail=?,updated_at=? WHERE id=? AND actor_user_id=?",
                    (
                        code,
                        clean_detail(
                            result.get("detail", "The checked shared movie request was processed.")
                        ),
                        core._stamp(),
                        event[0],
                        user.id,
                    ),
                )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except ValueError:
            raise HTTPException(
                409,
                "The shared movie request needs a current owner connection or policy review. No automatic retry was made.",
                headers=HEADERS,
            ) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item_id}", status_code=303, headers=HEADERS
        )

    @router.post("/libraries/{library_id}/catalog/add")
    async def add_shared_catalog(
        library_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        from omakase.plus.household_catalog import add_catalog_entry

        form = await _form(request, {"domain", "canonical_id"})
        try:
            selected = scope(db, user, library_id)
            core._require(selected.kind == "household", "Choose a shared library.", 422)
            item = add_catalog_entry(
                db,
                selected,
                domain=form.get("domain", ""),
                canonical_id=form.get("canonical_id", ""),
            )
        except core.HouseholdError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        return RedirectResponse(
            f"/plus/libraries/{library_id}/items/{item}", status_code=303, headers=HEADERS
        )

    return router
