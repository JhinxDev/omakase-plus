"""Authenticated direct catalog search, deliberately separate from AI discovery."""

from __future__ import annotations

import asyncio
import math
import os
import re
import time
from collections import OrderedDict, deque
from itertools import zip_longest

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute

from omakase.plus.catalog_search import AnimeCatalogSearch, CatalogSearchError, _plain
from omakase.plus.connections import movie_connections
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.movie_history import MovieHistoryService
from omakase.plus.movies import MovieError, MovieService
from omakase.plus.tv_service import TVError, TVService
from omakase.plus.web_security import csrf_token

_SEARCH_TIMEOUT = 15.0
_SEARCH_RATE_LIMIT = 30
_SEARCH_WINDOW = 60
_SEARCH_OWNER_INFLIGHT = 2
_SEARCH_GLOBAL_INFLIGHT = 8
_KINDS = {"anime": "Anime", "movies": "Movies", "tv": "TV"}


class _SearchLimit(ValueError):
    def __init__(self, detail, retry_after):
        self.detail, self.retry_after = detail, retry_after
        self.code, self.status_code = "rate_limited", 429


def _retry_seconds(value):
    """Preserve valid provider delays without converting large integers to floats."""
    if isinstance(value, bool):
        return 60
    if isinstance(value, int):
        return max(1, value)
    if isinstance(value, str):
        value = value.strip()
        if value.isascii() and value.isdecimal():
            try:
                return max(1, int(value))
            except ValueError:
                return 60
    if isinstance(value, float) and math.isfinite(value):
        return max(1, math.ceil(value))
    return 60


_CACHE = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
}


class _PrivateRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                response = await handler(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **_CACHE}
                raise
            except RequestValidationError:
                return JSONResponse(
                    {"results": [], "detail": "Choose a valid catalog query or identity."},
                    status_code=422,
                    headers=_CACHE,
                )
            response.headers.update(_CACHE)
            return response

        return wrapped


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").strip().casefold() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Not found", headers=_CACHE)


def _input(q, kind):
    if kind not in {"all", "anime", "movies", "tv"}:
        raise CatalogSearchError("invalid_query")
    if not isinstance(q, str) or len(q) > 120 or any(ord(char) < 32 for char in q):
        raise CatalogSearchError("invalid_query")
    return q.strip()


def create_search_router(
    templates,
    context_factory=None,
    *,
    anime_factory=None,
    movie_factory=None,
    tv_factory=None,
    households_enabled=False,
):
    """Relative routes, to be included by the root Plus router once."""
    from omakase.plus.household_media import read_route_class

    router = APIRouter(
        dependencies=[Depends(_private)], route_class=read_route_class(_PrivateRoute)
    )

    def context(request, user):
        return (
            context_factory(request, user, active_page="search")
            if context_factory
            else {
                "email": user.email,
                "active_page": "search",
                "csrf_token": csrf_token(request),
                "movies_enabled": True,
            }
        )

    def anime(db, user):
        return anime_factory(db, user) if anime_factory else AnimeCatalogSearch(db, user.id)

    def movies(db, user):
        if (
            households_enabled() if callable(households_enabled) else households_enabled
        ) and movie_factory is None:
            from omakase.plus.households import selected_scope

            selected = selected_scope(db, viewer_id=user.id)
            if selected.kind == "household":
                from omakase.plus.household_catalog import catalog_connections

                return MovieService(db, catalog_connections(db, selected))
        return (
            movie_factory(db, user)
            if movie_factory
            else MovieService(db, movie_connections(db, user.id))
        )

    async def cards_for(db, user, q, kind):
        if len(q) < 2:
            return []
        if kind == "anime":
            return [
                {**card, "media_type": "anime"} for card in (await anime(db, user).search(q))[:8]
            ]
        if kind == "tv":
            config = None
            if (
                households_enabled() if callable(households_enabled) else households_enabled
            ) and tv_factory is None:
                from omakase.plus.households import selected_scope

                selected = selected_scope(db, viewer_id=user.id)
                if selected.kind == "household":
                    from omakase.plus.household_catalog import catalog_connections

                    config = catalog_connections(db, selected)
            svc = (
                tv_factory(db, user)
                if tv_factory
                else TVService(db, config or movie_connections(db, user.id))
            )
            records = await svc.search(user.id, q)
            return [
                {
                    "id": r["tmdb_id"],
                    "tmdb_id": r["tmdb_id"],
                    "media_type": "tv",
                    "title": r["title"],
                    "year": r.get("year"),
                    "format": "TV series",
                    "poster_url": r.get("poster_url"),
                    "url": f"/plus/tv/{r['tmdb_id']}",
                    "watch_status": r.get("status")
                    if r.get("status_source") != "none"
                    else "unknown",
                    "progress": r.get("watched_episodes"),
                    "episode_total": None,
                    "rating": r.get("rating"),
                    "library_id": r["tmdb_id"] if r.get("in_collection") else None,
                    "description": r.get("overview", ""),
                }
                for r in records[:8]
            ]
        svc = movies(db, user)
        records = await svc.search(user.id, q)
        history = MovieHistoryService(svc)
        cards = []
        for record in records[:8]:
            imdb = record.get("imdb_id")
            if not isinstance(imdb, str) or not re.fullmatch(r"tt[0-9]{7,10}", imdb):
                continue
            saved = history.get(user.id, imdb)
            if saved is not None and saved["tmdb_id"] != record.get("tmdb_id"):
                saved = None
            cards.append(
                {
                    "id": imdb,
                    "imdb_id": imdb,
                    "tmdb_id": record.get("tmdb_id"),
                    "media_type": "movie",
                    "title": record["title"],
                    "year": record.get("year"),
                    "format": "MOVIE",
                    "poster_url": f"/plus/movies/{imdb}/artwork"
                    if record.get("poster_url")
                    else None,
                    "backdrop_url": None,
                    "url": f"/plus/movies/{imdb}",
                    "watch_status": saved["status"] if saved else "unknown",
                    "progress": None,
                    "episode_total": None,
                    "rating": saved["rating"] if saved else None,
                    "history": saved,
                    "library_id": None,
                    "library_status": record.get("state") if record.get("requested") else None,
                    "description": _plain(record.get("overview")),
                    "genres": record.get("genres", []),
                }
            )
        return cards

    def failure(error):
        from omakase.plus.households import HouseholdError

        if isinstance(error, HouseholdError):
            return str(error), "shared_catalog_unavailable", 503, None
        if isinstance(error, (CatalogSearchError, _SearchLimit)):
            return error.detail, error.code, error.status_code, error.retry_after
        if isinstance(error, (MovieError, TVError)):
            return (
                error.detail,
                error.code,
                429 if error.code == "rate_limited" else 503,
                getattr(error, "retry_after", None),
            )
        return "Catalog search is temporarily unavailable.", "unavailable", 503, None

    budgets = OrderedDict()
    pauses = {}
    active = {}
    inflight = 0

    async def search_for(db, user, query, kind):
        nonlocal inflight
        if len(query) < 2:
            return {"results": [], "warnings": []}, 200, {}
        now = time.monotonic()
        if inflight >= _SEARCH_GLOBAL_INFLIGHT or active.get(user.id, 0) >= _SEARCH_OWNER_INFLIGHT:
            raise _SearchLimit("Search is already running. Wait a moment before trying again.", 1)
        history = budgets.setdefault(user.id, deque())
        while history and now - history[0] >= _SEARCH_WINDOW:
            history.popleft()
        if len(history) >= _SEARCH_RATE_LIMIT:
            raise _SearchLimit(
                "Too many catalog searches. Please wait before trying again.",
                max(1, math.ceil(_SEARCH_WINDOW - now + history[0])),
            )
        history.append(now)
        budgets.move_to_end(user.id)
        while len(budgets) > 256:
            owner = next((owner for owner in budgets if not active.get(owner)), None)
            if owner is None:
                break
            budgets.pop(owner)
            for key in [key for key in pauses if key[0] == owner]:
                pauses.pop(key)
        inflight += 1
        active[user.id] = active.get(user.id, 0) + 1

        async def one(category):
            paused = pauses.get((user.id, category))
            if paused and paused[0] > time.monotonic_ns():
                return [], {
                    **paused[1],
                    "retry_after": max(
                        1, (paused[0] - time.monotonic_ns() + 999_999_999) // 1_000_000_000
                    ),
                }
            try:
                return await asyncio.wait_for(
                    cards_for(db, user, query, category), _SEARCH_TIMEOUT
                ), None
            except Exception as exc:
                detail, code, status, retry = failure(exc)
                warning = {
                    "kind": category,
                    "label": _KINDS[category],
                    "detail": detail,
                    "code": code,
                    "status": status,
                    "retry_after": retry,
                }
                if code == "rate_limited":
                    warning["retry_after"] = _retry_seconds(retry)
                    pauses[(user.id, category)] = (
                        time.monotonic_ns() + warning["retry_after"] * 1_000_000_000,
                        warning,
                    )
                return [], warning

        try:
            categories = list(_KINDS) if kind == "all" else [kind]
            # Exactly one bounded task per selected domain. Domains fail independently.
            outcomes = await asyncio.gather(*(one(category) for category in categories))
            if households_enabled() if callable(households_enabled) else households_enabled:
                from omakase.plus.household_views import search_cards
                from omakase.plus.households import selected_scope

                selected = selected_scope(db, viewer_id=user.id)
                if selected.kind == "household":
                    outcomes = [
                        (
                            search_cards(
                                db,
                                selected,
                                query=query,
                                domain={"anime": "anime", "movies": "movie", "tv": "tv"}[category],
                            )
                            + [
                                {**record, "library_id": None, "library_status": None}
                                for record in records
                            ],
                            warning,
                        )
                        for category, (records, warning) in zip(categories, outcomes, strict=True)
                    ]
            warnings = [warning for _, warning in outcomes if warning]
            cards, seen = [], set()
            for row in zip_longest(*(cards for cards, _ in outcomes)):
                for card in row:
                    if card is None:
                        continue
                    identity = (card.get("media_type"), card.get("id"))
                    if identity not in seen:
                        seen.add(identity)
                        cards.append(card)
            # Exact title matches lead, while round-robin order prevents a domain
            # with many results from hiding another domain behind the eight-card cap.
            cards.sort(key=lambda card: card["title"].casefold() != query.casefold())
            status = 200
            if not cards and len(warnings) == len(categories):
                status = 429 if all(w["status"] == 429 for w in warnings) else 503
            if not cards and kind != "all" and warnings:
                status = warnings[0]["status"]
            retry = max((w["retry_after"] or 0 for w in warnings), default=0)
            headers = {"Retry-After": str(retry)} if retry else {}
            return {"results": cards[:8], "warnings": warnings}, status, headers
        finally:
            inflight -= 1
            active[user.id] -= 1
            if not active[user.id]:
                active.pop(user.id)

    @router.get("/search", response_class=HTMLResponse)
    async def page(
        request: Request,
        q: str = "",
        kind: str = "all",
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        cards, warnings, error, status = [], [], "", 200
        headers = dict(_CACHE)
        try:
            q = _input(q, kind)
            payload, status, retry_headers = await search_for(db, user, q, kind)
            cards, warnings = payload["results"], payload["warnings"]
            headers.update(retry_headers)
            if kind != "all" and warnings:
                error = warnings[0]["detail"]
        except (CatalogSearchError, MovieError, TVError, ValueError, _SearchLimit) as exc:
            error, _, status, retry = failure(exc)
            if retry is not None:
                headers["Retry-After"] = str(retry)
        return templates.TemplateResponse(
            request,
            "search.html",
            {
                **context(request, user),
                "query": q,
                "q": q,
                "kind": kind if kind in {"all", "anime", "movies", "tv"} else "all",
                "cards": cards,
                "results": cards,
                "search_error": error,
                "search_warnings": warnings if kind == "all" else [],
            },
            status_code=status,
            headers=headers,
        )

    @router.get("/search/suggestions")
    async def suggestions(
        q: str = "", kind: str = "all", user=Depends(require_user), db=Depends(get_db)
    ):
        try:
            q = _input(q, kind)
            payload, status, headers = await search_for(db, user, q, kind)
            if kind != "all" and payload["warnings"]:
                payload.update(
                    {key: payload["warnings"][0][key] for key in ("detail", "code", "retry_after")}
                )
            return JSONResponse(payload, status_code=status, headers={**_CACHE, **headers})
        except (CatalogSearchError, MovieError, TVError, ValueError, _SearchLimit) as exc:
            detail, code, status, retry = failure(exc)
            headers = {**_CACHE}
            if retry is not None:
                headers["Retry-After"] = str(retry)
            return JSONResponse(
                {"results": [], "detail": detail, "code": code, "retry_after": retry},
                status_code=status,
                headers=headers,
            )

    @router.get("/catalog/anime/{media_id}", response_class=HTMLResponse)
    async def detail(
        request: Request, media_id: int, user=Depends(require_user), db=Depends(get_db)
    ):
        if households_enabled() if callable(households_enabled) else households_enabled:
            from omakase.plus.household_views import detail_url
            from omakase.plus.households import selected_scope

            selected = selected_scope(db, viewer_id=user.id)
            if selected.kind == "household":
                # Prefer an exact linked anime season; multiple seasons require explicit selection.
                matches = db.execute(
                    "SELECT id FROM library_items WHERE library_id=? AND state='active' AND domain='anime' AND canonical_id=? ORDER BY season_number",
                    (selected.library_id, str(media_id)),
                ).fetchall()
                if len(matches) == 1:
                    return RedirectResponse(
                        detail_url(selected, matches[0]["id"]), status_code=307, headers=_CACHE
                    )
                if len(matches) > 1:
                    return RedirectResponse(
                        f"/plus/libraries/{selected.library_id}",
                        status_code=307,
                        headers=_CACHE,
                    )
        card, error, status = None, "", 200
        headers = dict(_CACHE)
        try:
            if type(media_id) is not int or media_id <= 0:
                raise CatalogSearchError("invalid_identity")
            card = await anime(db, user).detail(media_id)
            if (
                (households_enabled() if callable(households_enabled) else households_enabled)
                and selected.kind == "household"
                and card
            ):
                # Personal membership is not membership in the selected shared catalog.
                card = {**card, "library_id": None, "library_status": None}

        except (CatalogSearchError, ValueError) as exc:
            error, _, status, retry = failure(exc)
            if retry is not None:
                headers["Retry-After"] = str(retry)
        return templates.TemplateResponse(
            request,
            "catalog_anime.html",
            {
                **context(request, user),
                "card": card,
                "kind": "anime",
                "query": "",
                "search_error": error,
            },
            status_code=status,
            headers=headers,
        )

    return router
