"""Authenticated notification reads and per-account UI-state changes only."""

from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.notifications import notification_feed, set_notification_state
from omakase.plus.web_security import require_csrf

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
                    {"detail": "Choose a valid notification action."},
                    status_code=422,
                    headers=_CACHE,
                )
            response.headers.update(_CACHE)
            return response

        return wrapped


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").strip().casefold() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Not found", headers=_CACHE)


def create_notifications_router():
    router = APIRouter(dependencies=[Depends(_private)], route_class=_PrivateRoute)

    @router.get("/api/notifications")
    async def feed(user=Depends(require_user), db=Depends(get_db)):
        return notification_feed(db, user_id=user.id)

    @router.post("/api/notifications/{notification_id}/{action}")
    async def change(
        notification_id: str,
        action: Literal["read", "dismiss"],
        request: Request,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, request.headers.get("X-CSRF-Token", ""))
        if not set_notification_state(
            db, user_id=user.id, notification_id=notification_id, action=action
        ):
            raise HTTPException(404, "Notification not found")
        return {"ok": True, **notification_feed(db, user_id=user.id)}

    return router
