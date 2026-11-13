"""Private profile-picture forms and account-scoped image serving."""

from __future__ import annotations

import unicodedata

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from omakase.plus import avatar
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.secrets import store_secret
from omakase.plus.web_security import csrf_token, require_csrf

_PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}
_MAX_FORM_BYTES = avatar.MAX_INPUT_BYTES + 65536
_NOTICES = {
    "saved": "Your profile picture was saved.",
    "name_saved": "Your display name was saved.",
    "removed": "Your profile picture was removed.",
}


def _private_deployment() -> None:
    if not avatar.private_avatar_enabled():
        raise HTTPException(status_code=404, detail="Not found")


_AvatarRoute = bounded_form_route(
    _MAX_FORM_BYTES, "Choose a picture smaller than 8 MiB", guard=_private_deployment
)


async def _form(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    if not content_type.startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
        raise HTTPException(status_code=415, detail="Use the profile picture form")
    form = await request.form(max_files=1, max_fields=2, max_part_size=4096)
    supplied = form.get("csrf_token", "")
    if not isinstance(supplied, str) or len(form.getlist("csrf_token")) != 1:
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    require_csrf(request, supplied)
    return form


def create_avatar_router(templates, context_factory=None) -> APIRouter:
    """Mount relative to /plus; context_factory uses the shared product signature."""
    router = APIRouter(route_class=_AvatarRoute)

    def render(request, user, db, *, notice="", error="", status_code=200):
        context = (
            context_factory(request, user, active_page="settings")
            if context_factory
            else {"email": user.email, "active_page": "settings"}
        )
        return templates.TemplateResponse(
            request,
            "avatar.html",
            {
                **context,
                **avatar.avatar_context(db, user.id),
                "csrf_token": csrf_token(request),
                "profile_notice": _NOTICES.get(notice, ""),
                "profile_error": error,
            },
            status_code=status_code,
            headers=_PRIVATE_HEADERS,
        )

    def redirect(notice):
        return RedirectResponse(
            f"/plus/settings/profile?notice={notice}", status_code=303, headers=_PRIVATE_HEADERS
        )

    @router.get("/settings/profile", response_class=HTMLResponse)
    async def profile(
        request: Request, notice: str = "", user=Depends(require_user), db=Depends(get_db)
    ):
        return render(request, user, db, notice=notice)

    @router.post("/settings/profile")
    async def upload(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request)
        files = form.getlist("avatar")
        if len(files) != 1 or not isinstance(files[0], UploadFile):
            return render(
                request, user, db, error="Choose one JPEG, PNG or WebP picture.", status_code=422
            )
        raw = await files[0].read(avatar.MAX_INPUT_BYTES + 1)
        try:
            await run_in_threadpool(avatar.save_avatar, db, user.id, raw)
        except avatar.AvatarInputError as error:
            return render(
                request,
                user,
                db,
                error=str(error),
                status_code=413 if error.code == "image_too_large" else 422,
            )
        return redirect("saved")

    @router.post("/settings/profile/remove")
    async def remove(request: Request, user=Depends(require_user), db=Depends(get_db)):
        await _form(request)
        avatar.remove_avatar(db, user.id)
        return redirect("removed")

    profile_router = APIRouter(
        route_class=bounded_form_route(4096, "Profile form is too large", guard=_private_deployment)
    )

    @profile_router.post("/account/profile")
    async def save_profile(request: Request, user=Depends(require_user), db=Depends(get_db)):
        if (
            not request.headers.get("content-type", "")
            .lower()
            .startswith("application/x-www-form-urlencoded")
        ):
            raise HTTPException(415, "Use the profile form")
        form = await _form(request)
        names = form.getlist("display_name")
        if len(names) != 1 or not isinstance(names[0], str):
            raise HTTPException(422, "Enter one display name")
        raw = names[0]
        if any(unicodedata.category(char).startswith("C") or char in "<>" for char in raw):
            raise HTTPException(422, "Use a plain-text display name without control characters")
        name = unicodedata.normalize("NFC", raw).strip()
        if len(name) > 80:
            raise HTTPException(422, "Use a display name of at most 80 characters")
        store_secret(db, user.id, "display_name", name)
        return redirect("name_saved")

    router.include_router(profile_router)

    @router.get("/account/avatar")
    async def image(user=Depends(require_user), db=Depends(get_db)):
        current = avatar.get_avatar(db, user.id)
        if current is None:
            raise HTTPException(
                status_code=404, detail="No profile picture", headers=_PRIVATE_HEADERS
            )
        return Response(
            current.image_bytes,
            media_type=current.content_type,
            headers={
                **_PRIVATE_HEADERS,
                "Vary": "Cookie",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'inline; filename="avatar.png"',
            },
        )

    return router
