"""Authenticated private About & updates page; explicit CSRF POST checks only."""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from omakase.plus import update_service
from omakase.plus.connections import private_connections_enabled
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_security import csrf_token, require_csrf


def _private_deployment():
    if not private_connections_enabled():
        raise HTTPException(404, "Not found")


def create_updates_router(templates, context_factory=None):
    router = APIRouter(
        route_class=bounded_form_route(4096, "Update form is too large", guard=_private_deployment)
    )

    def render(request, user, *, status_code=200, retry_after=0):
        context = (
            context_factory(request, user, active_page="settings")
            if context_factory
            else {"email": user.email, "active_page": "settings"}
        )
        headers = {"Cache-Control": "private, no-store"}
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        return templates.TemplateResponse(
            request,
            "updates.html",
            {
                **context,
                "csrf_token": csrf_token(request),
                "installed_version": update_service.PRODUCT_VERSION,
                "updates": update_service.checker.status,
                "checks_enabled": update_service.checks_enabled(),
                "retry_after": retry_after,
            },
            status_code=status_code,
            headers=headers,
        )

    @router.get("/settings/updates", response_class=HTMLResponse)
    async def page(request: Request, user=Depends(require_user)):
        return render(request, user)

    @router.post("/settings/updates/check", response_class=HTMLResponse)
    async def check(request: Request, user=Depends(require_user)):
        if not request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            raise HTTPException(415, "Use the update form")
        form = await request.form()
        token = form.get("csrf_token", "")
        if (
            set(form) != {"csrf_token"}
            or len(form.getlist("csrf_token")) != 1
            or not isinstance(token, str)
        ):
            raise HTTPException(422, "Invalid update form")
        require_csrf(request, token)
        _, remaining = await update_service.checker.check()
        return render(request, user, status_code=429 if remaining else 200, retry_after=remaining)

    return router
