"""Authenticated episode images, with owned identity checked before any image work."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute

from omakase.plus.deps import get_db
from omakase.plus.history_artwork import (
    EpisodeArtworkUnavailable,
    anime_identity,
    episode_artwork,
    tv_identity,
)
from omakase.plus.middleware import require_user

_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
}


class _PrivateImageRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def wrapped(request):
            try:
                response = await handler(request)
            except HTTPException as error:
                error.headers = {**(error.headers or {}), **_HEADERS}
                raise
            except RequestValidationError:
                return JSONResponse(
                    {"detail": "Image unavailable"}, status_code=422, headers=_HEADERS
                )
            response.headers.update(_HEADERS)
            return response

        return wrapped


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Image unavailable", headers=_HEADERS)


def create_history_artwork_router():
    router = APIRouter(dependencies=[Depends(_private)], route_class=_PrivateImageRoute)

    async def image(db, user, *, event_id=None, media_id=None, season=None, episode=None):
        if os.getenv("OMAKASE_PLUS_PRIVATE", "").lower() not in {"1", "true", "yes", "on"}:
            raise HTTPException(404, "Image unavailable", headers=_HEADERS)
        try:
            identity = (
                anime_identity(db, user.id, event_id)
                if event_id is not None
                else tv_identity(db, user.id, media_id, season, episode)
            )
            payload, kind = await episode_artwork(db, identity)
        except EpisodeArtworkUnavailable:
            raise HTTPException(404, "Image unavailable", headers=_HEADERS) from None
        return Response(payload, media_type=kind, headers=_HEADERS)

    @router.get("/history/artwork/anime/{event_id}")
    async def anime(event_id: int, user=Depends(require_user), db=Depends(get_db)):
        return await image(db, user, event_id=event_id)

    @router.get("/history/artwork/tv/{media_id}/{season}/{episode}")
    async def tv(
        media_id: int, season: int, episode: int, user=Depends(require_user), db=Depends(get_db)
    ):
        return await image(db, user, media_id=media_id, season=season, episode=episode)

    return router
