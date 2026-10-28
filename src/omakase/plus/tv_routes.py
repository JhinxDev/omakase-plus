"""Private TV routes: browsing reads metadata; personal/Plex imports are explicit."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from omakase.plus.artwork_delivery import optimized_artwork
from omakase.plus.connections import movie_connections
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.movie_artwork import movie_artwork
from omakase.plus.movies import MovieError, _positive
from omakase.plus.movies_routes import _private
from omakase.plus.posters import PosterUnavailable
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.tv_service import UNSET, TVError, TVService, number
from omakase.plus.web_security import csrf_token, require_csrf


def create_tv_router(templates, context_factory=None, service_factory=None):
    router = APIRouter(
        route_class=bounded_form_route(16384, "TV form is too large", guard=_private)
    )

    def service(db, user):
        return (
            service_factory(db, user)
            if service_factory
            else TVService(db, movie_connections(db, user.id))
        )

    def context(request, user):
        return (
            context_factory(request, user, active_page="tv")
            if context_factory
            else {
                "email": user.email,
                "active_page": "tv",
                "tv_enabled": True,
                "csrf_token": csrf_token(request),
            }
        )

    async def render(request, user, db, q="", view="all", error="", offset=0, **extra):
        cards = []
        collection_page = None
        try:
            svc = service(db, user)
            if q:
                cards = await svc.search(user.id, q)
            elif hasattr(svc, "collection_page"):
                collection_page = await svc.collection_page(user.id, view=view, offset=offset)
                cards = collection_page["cards"]
            else:
                cards = await svc.list(user.id, view=view)
        except (TVError, MovieError) as exc:
            error = exc.detail
        return templates.TemplateResponse(
            request,
            "tv.html",
            {
                **context(request, user),
                "cards": cards,
                "collection_page": collection_page,
                "query": q,
                "view": view,
                "tv_error": error,
                "notice": "",
                **extra,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/tv", response_class=HTMLResponse)
    async def page(
        request: Request,
        q: str = "",
        view: str = "all",
        offset: int = 0,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        if len(q) > 200 or view not in {"all", "watching", "completed"} or not 0 <= offset <= 10000:
            raise HTTPException(422, "Invalid TV search")
        return await render(request, user, db, q, view, offset=offset)

    @router.get("/tv/{tmdb_id}/artwork")
    async def artwork(
        tmdb_id: int, variant: str = "poster", user=Depends(require_user), db=Depends(get_db)
    ):
        if variant not in {"poster", "backdrop"} or not 0 < tmdb_id <= 2147483647:
            raise HTTPException(422, "Invalid TV artwork")
        metadata = TVService(db, movie_connections(db, user.id))._raw(user.id, tmdb_id)
        url = (metadata or {}).get("backdrop_url" if variant == "backdrop" else "poster_url")
        if not url:
            raise HTTPException(404, "Artwork unavailable")
        try:
            payload, kind = await movie_artwork(url)
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

    @router.get("/tv/{tmdb_id}", response_class=HTMLResponse)
    async def detail(
        request: Request,
        tmdb_id: int,
        season: int | None = None,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        try:
            _positive(tmdb_id)
            if season is not None:
                number(season, 200, zero=True)
            card = await service(db, user).detail(user.id, tmdb_id)
            seasons = card.get("seasons", [])
            if seasons and season not in {row["season_number"] for row in seasons}:
                season = next(
                    (
                        row["season_number"]
                        for row in seasons
                        if row["season_number"] > 0 and row.get("released")
                    ),
                    seasons[0]["season_number"],
                )
        except (TVError, MovieError) as exc:
            if exc.code == "invalid_input":
                raise HTTPException(422, exc.detail) from None
            return await render(request, user, db, error=exc.detail)
        return templates.TemplateResponse(
            request,
            "tv_detail.html",
            {
                **context(request, user),
                "card": card,
                "selected_season": season,
                "tv_error": "",
                "notice": "",
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def action(request, user, db, operation, **kwargs):
        try:
            result = await getattr(service(db, user), operation)(user.id, **kwargs)
        except (TVError, MovieError, ValueError) as exc:
            error = (
                exc.as_dict()
                if isinstance(exc, (TVError, MovieError))
                else {"code": "invalid_input", "detail": "Invalid TV input."}
            )
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    error,
                    status_code=422 if error["code"] == "invalid_input" else 409,
                    headers={"Cache-Control": "private, no-store"},
                )
            return await render(request, user, db, error=error["detail"])
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse(result, headers={"Cache-Control": "private, no-store"})
        if operation == "sync":
            return await render(request, user, db, sync_result=result)
        target = f"/plus/tv/{kwargs['tmdb_id']}"
        if "season_number" in kwargs:
            target += f"?season={kwargs['season_number']}"
        return RedirectResponse(target, status_code=303)

    @router.post("/tv/sync")
    async def sync(
        request: Request,
        offset: int = Form(0),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(request, user, db, "sync", offset=offset)

    @router.post("/tv/history")
    async def history(
        request: Request,
        tmdb_id: int = Form(...),
        status: str = Form(""),
        rating: str = Form(""),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        form = await request.form()
        try:
            parsed_rating = (float(rating) if rating else None) if "rating" in form else UNSET
        except ValueError:
            raise HTTPException(422, "Invalid TV rating") from None
        return await action(
            request,
            user,
            db,
            "save_history",
            tmdb_id=tmdb_id,
            status=status if status else UNSET,
            rating=parsed_rating,
        )

    @router.post("/tv/episode")
    async def episode(
        request: Request,
        tmdb_id: int = Form(...),
        season_number: int = Form(...),
        episode_number: int = Form(...),
        watched: str = Form(...),
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        if watched not in {"true", "false"}:
            raise HTTPException(422, "Invalid watched state")
        return await action(
            request,
            user,
            db,
            "save_episode",
            tmdb_id=tmdb_id,
            season_number=season_number,
            episode_number=episode_number,
            watched=watched == "true",
        )

    @router.post("/tv/{tmdb_id}/season/{season_number}")
    async def season_load(
        request: Request,
        tmdb_id: int,
        season_number: int,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(
            request, user, db, "season", tmdb_id=tmdb_id, season_number=season_number
        )

    @router.post("/tv/{tmdb_id}/refresh")
    async def refresh(
        request: Request,
        tmdb_id: int,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(request, user, db, "refresh", tmdb_id=tmdb_id)

    @router.post("/tv/{tmdb_id}/check")
    async def check(
        request: Request,
        tmdb_id: int,
        csrf: str = Form("", alias="csrf_token"),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, csrf)
        return await action(request, user, db, "check", tmdb_id=tmdb_id)

    return router
