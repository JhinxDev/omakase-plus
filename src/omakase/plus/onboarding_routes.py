"""Private first-run guidance; viewing never probes or configures services."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from omakase.plus.connections import connection_cards, private_connections_enabled
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.secrets import read_secret, store_secret
from omakase.plus.web_security import csrf_token, require_csrf

_HEADERS = {"Cache-Control": "private, no-store"}
_SETUP_PATHS = {
    "standalone": "Explore with Omakase only",
    "existing": "Connect an existing stack",
    "fresh_plex": "Set up optional Plex",
}
_INTENTS = {"cloud": "Cloud downloads", "local": "Local or NAS library", "explore": "Explore first"}


async def _form(request: Request):
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(415, "Use the onboarding form")
    form = await request.form()
    for key in ("csrf_token", "intent", "setup_path"):
        if len(form.getlist(key)) > 1 or not isinstance(form.get(key, ""), str):
            raise HTTPException(422, "Invalid onboarding form")
    require_csrf(request, form.get("csrf_token", ""))
    return form


def create_onboarding_router(templates, context_factory=None) -> APIRouter:
    """Return authenticated routes relative to /plus, using the shared shell."""

    def private_guard():
        if not private_connections_enabled():
            raise HTTPException(404, "Not found")

    router = APIRouter(
        route_class=bounded_form_route(4096, "Onboarding form is too large", guard=private_guard)
    )

    @router.get("/onboarding", response_class=HTMLResponse)
    async def page(
        request: Request,
        step: int = Query(1, ge=1, le=4),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        intent = read_secret(db, user.id, "onboarding_intent") or "explore"
        if intent not in _INTENTS:
            intent = "explore"
        setup_path = read_secret(db, user.id, "onboarding_setup_path") or "standalone"
        if setup_path not in _SETUP_PATHS:
            setup_path = "standalone"
        context = (
            context_factory(request, user, active_page="setup")
            if context_factory
            else {"email": user.email, "active_page": "setup"}
        )
        # Saved, credential-free receipts only. No live health_view/setup_view:
        # those may contact configured services or inspect host state.
        cards = connection_cards(db, user.id)
        return templates.TemplateResponse(
            request,
            "onboarding.html",
            {
                **context,
                "csrf_token": csrf_token(request),
                "onboarding_step": step,
                "onboarding_intent": intent,
                "onboarding_setup_path": setup_path,
                "onboarding_setup_path_label": _SETUP_PATHS[setup_path],
                "onboarding_intent_label": _INTENTS[intent],
                "onboarding_connections": cards,
                "onboarding_completed": read_secret(db, user.id, "onboarding_completed") == "true",
                "onboarding_import_configured": bool(os.getenv("OMAKASE_IMPORT_ROOT", "").strip()),
                "onboarding_library_configured": bool(
                    os.getenv("OMAKASE_LIBRARY_ROOT", "").strip()
                ),
            },
            headers=_HEADERS,
        )

    @router.post("/onboarding/intent")
    async def intent(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request)
        selected = form.get("intent", "")
        if selected not in _INTENTS:
            raise HTTPException(422, "Choose how you want to start")
        # Older forms and saved accounts have only a storage intent.
        setup_path = form.get("setup_path")
        if setup_path is not None and setup_path not in _SETUP_PATHS:
            raise HTTPException(422, "Choose a setup path")
        store_secret(db, user.id, "onboarding_intent", selected)
        if setup_path is not None:
            store_secret(db, user.id, "onboarding_setup_path", setup_path)
        return RedirectResponse("/plus/onboarding?step=2", status_code=303, headers=_HEADERS)

    @router.post("/onboarding/finish")
    async def finish(request: Request, user=Depends(require_user), db=Depends(get_db)):
        await _form(request)
        store_secret(db, user.id, "onboarding_completed", "true")
        return RedirectResponse("/plus/home", status_code=303, headers=_HEADERS)

    return router
