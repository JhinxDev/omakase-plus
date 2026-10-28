"""Private movie pages and bounded explicit actions, separate from anime."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from omakase.plus.artwork_delivery import optimized_artwork
from omakase.plus.connections import movie_connections
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.movie_artwork import movie_artwork
from omakase.plus.movies import MovieError, MovieService
from omakase.plus.movies_store import get_movie
from omakase.plus.posters import PosterUnavailable
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_security import csrf_token, require_csrf


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Not found")


def create_movies_router(templates, context_factory=None, *, service_factory=None):
    router = APIRouter(
        route_class=bounded_form_route(16_384, "Movie form is too large", guard=_private)
    )

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
        request, user, db, *, q="", view="all", notice="", action_card=None, sync_result=None
    ):
        svc = service(db, user)
        cards, error = [], ""
        try:
            cards = await svc.search(user.id, q) if q else await svc.list(user.id, view=view)
        except MovieError as exc:
            error = exc.detail
        from omakase.plus.movie_history import MovieHistoryService

        history = MovieHistoryService(svc)
        for card in cards:
            card["history"] = history.get(user.id, card["imdb_id"]) if card.get("imdb_id") else None
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
                "discover_movies": False,
                "sync_result": sync_result,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    @router.get("/movies", response_class=HTMLResponse)
    async def page(
        request: Request,
        q: str = "",
        view: str = "all",
        notice: str = "",
        remaining: int | None = None,
        imported: int | None = None,
        skipped: int | None = None,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        if view not in {"all", "requests", "ready", "discover"} or len(q) > 200:
            raise HTTPException(422, "Invalid movie search")
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
            view=view,
            notice=notice if notice in {"saved", "synced", "updated"} else "",
            sync_result=sync_result,
        )

    @router.get("/movies/{imdb_id}/artwork")
    async def artwork(
        imdb_id: str, variant: str = "poster", user=Depends(require_user), db=Depends(get_db)
    ):
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
        target = (
            f"/plus/movies/{imdb_id}?notice=updated" if imdb_id else "/plus/movies?notice=updated"
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
        from omakase.plus.movie_history import MovieHistoryService
        from omakase.plus.movies import _imdb

        try:
            _imdb(imdb_id)
            svc = service(db, user)
            cards = await svc.list(user.id)
            card = next((item for item in cards if item["imdb_id"] == imdb_id), None)
            if card is None:
                card = await svc.detail(user.id, imdb_id)
            if card is None:
                raise HTTPException(404, "Movie not found")
            card["history"] = MovieHistoryService(svc).get(user.id, imdb_id)
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
                "notice": notice if notice in {"saved", "updated"} else "",
                "movies_error": "",
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def action(request, user, db, operation, **kwargs):
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
            await MovieHistoryService(service(db, user)).save(
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
        return RedirectResponse(f"/plus/movies/{imdb_id}?notice=saved", status_code=303)

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

    @router.get("/discover/movies", response_class=HTMLResponse)
    async def discover(
        request: Request,
        genre_id: str | None = None,
        sort: str = "personalized",
        include_completed: bool = False,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        from omakase.plus.movie_discovery import MovieDiscoveryService
        from omakase.plus.movie_history import MovieHistoryService

        svc = service(db, user)
        discovery = MovieDiscoveryService(svc)
        cards, options, error = [], {}, ""
        selected_genre = None
        try:
            # A native GET form submits the All genres option as genre_id=.
            # Normalize it here so both ordinary submits and bad input render HTML.
            raw_genre = (genre_id or "").strip()
            if raw_genre:
                if len(raw_genre) > 10 or not raw_genre.isascii() or not raw_genre.isdecimal():
                    raise ValueError("Invalid genre")
                selected_genre = int(raw_genre)
                if not 0 < selected_genre <= 2_147_483_647:
                    raise ValueError("Invalid genre")
            options = await discovery.options(user.id)
            result = await discovery.discover(
                user.id, genre_id=selected_genre, sort=sort, include_completed=include_completed
            )
            cards = result["results"] if isinstance(result, dict) else result
            history = MovieHistoryService(svc)
            for card in cards:
                card["history"] = (
                    history.get(user.id, card["imdb_id"]) if card.get("imdb_id") else None
                )
        except (MovieError, ValueError) as exc:
            error = (
                exc.detail if isinstance(exc, MovieError) else "Choose a valid discovery filter."
            )
        return templates.TemplateResponse(
            request,
            "movies.html",
            {
                **context(request, user),
                "active_page": "discover",
                "cards": cards,
                "query": "",
                "view": "discover",
                "movies_error": error,
                "discover_movies": True,
                "genre_options": options,
                "genre_id": selected_genre,
                "sort": sort,
                "include_completed": include_completed,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    return router
