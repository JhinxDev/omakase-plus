"""Authenticated Settings forms for per-user media connections."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from omakase.plus import connections
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_security import csrf_token, require_csrf

_PRIVATE_HEADERS = {"Cache-Control": "private, no-store"}
_NOTICES = {
    "saved": "Connection saved. Test it to confirm access.",
    "tested": "Connection check finished. Review its result below.",
    "disconnected": "Connection removed from your account.",
}


def _private_deployment() -> None:
    if not connections.private_connections_enabled():
        raise HTTPException(status_code=404, detail="Not found")


async def _form(request: Request):
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(status_code=415, detail="Use the connection form")
    if len(await request.body()) > 4096:
        raise HTTPException(status_code=413, detail="Connection form is too large")
    form = await request.form()
    for name in ("csrf_token", "url", "api_key"):
        if len(form.getlist(name)) > 1 or not isinstance(form.get(name, ""), str):
            raise HTTPException(status_code=422, detail="Invalid connection form")
    require_csrf(request, form.get("csrf_token", ""))
    return form


def create_connections_router(templates, context_factory=None) -> APIRouter:
    """Return routes relative to the existing /plus router.

    ``context_factory(request, user, active_page='settings')`` may supply the
    shared product shell. Without it, the factory remains independently usable.
    """
    router = APIRouter(
        route_class=bounded_form_route(
            4096, "Connection form is too large", guard=_private_deployment
        )
    )

    def render(request, user, db, *, notice="", error="", status_code=200):
        context = (
            context_factory(request, user, active_page="settings")
            if context_factory
            else {"email": user.email, "active_page": "settings"}
        )
        from omakase.plus import households

        affected_scopes = (
            {
                integration: households.owner_connection_scopes(
                    db, owner_user_id=user.id, integration=integration
                )
                for integration in ("plex", "realdebrid", "prowlarr", "riven", "seerr")
            }
            if households.feature_enabled()
            else {}
        )
        return templates.TemplateResponse(
            request,
            "connections.html",
            {
                **context,
                "csrf_token": csrf_token(request),
                "connections": connections.connection_cards(db, user.id),
                "connection_scopes": affected_scopes,
                "connections_notice": _NOTICES.get(notice, ""),
                "connections_error": error,
                "movies_ready": connections.movie_connections(db, user.id).enabled,
            },
            status_code=status_code,
            headers=_PRIVATE_HEADERS,
        )

    def redirect(integration: str, notice: str):
        return RedirectResponse(
            f"/plus/settings/connections?notice={notice}#connection-{integration}",
            status_code=303,
            headers=_PRIVATE_HEADERS,
        )

    def known(integration: str):
        try:
            connections._spec(integration)
        except connections.ConnectionInputError:
            raise HTTPException(status_code=404, detail="Unknown connection") from None

    @router.get("/settings/connections", response_class=HTMLResponse)
    async def page(
        request: Request, notice: str = "", user=Depends(require_user), db=Depends(get_db)
    ):
        return render(request, user, db, notice=notice)

    @router.post("/settings/connections/{integration}/save")
    async def save(
        integration: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        known(integration)
        form = await _form(request)
        try:
            connections.save_connection(
                db, user.id, integration, url=form.get("url", ""), api_key=form.get("api_key", "")
            )
        except connections.ConnectionInputError as error:
            return render(request, user, db, error=str(error), status_code=422)
        return redirect(integration, "saved")

    @router.post("/settings/connections/{integration}/test")
    async def test(
        integration: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        known(integration)
        await _form(request)
        try:
            await connections.test_connection(db, user.id, integration)
        except connections.ConnectionInputError as error:
            return render(request, user, db, error=str(error), status_code=422)
        return redirect(integration, "tested")

    @router.post("/settings/connections/{integration}/disconnect")
    async def disconnect(
        integration: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        known(integration)
        await _form(request)
        connections.disconnect_connection(db, user.id, integration)
        return redirect(integration, "disconnected")

    return router
