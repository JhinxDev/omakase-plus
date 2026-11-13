"""Personal MAL connection and explicit bridge review routes (factory, not auto-wired)."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from omakase.plus.deps import get_db
from omakase.plus.mal import (
    TokenBundle,
    TrackerHTTPError,
    authorize_url,
    pkce_verifier,
)
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.secrets import read_secret
from omakase.plus.tracker_reconciliation import Provider
from omakase.plus.tracker_sync import BridgeError, configured_service
from omakase.plus.web_security import csrf_token, require_csrf

_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_MESSAGES = {
    "operator_configuration_required": "An administrator must configure the MyAnimeList OAuth client and public callback first.",
    "persistent_encryption_configuration_required": "An administrator must configure persistent secret encryption first.",
    "connection_required": "Connect and verify both personal tracker accounts first.",
    "provider_identity_changed": "The tracker account changed. Reconnect and review a new preview.",
    "identity_linked_elsewhere": "This tracker identity is already linked to another Omakase account. Disconnect it there before linking it here.",
    "invalid_oauth_state": "This connection request does not belong to this session or has already been used. Start again.",
    "expired_oauth_state": "This connection request expired. Start again.",
    "writer_busy": "Another tracker action is in progress. Try this action again after it finishes.",
    "writer_coordination_required": "Connection and previews are available. An administrator must finish tracker-worker coordination before enabling list updates.",
    "first_preview_approval_required": "Review and confirm a first sync preview before enabling updates.",
    "plan_changed_or_expired": "This preview changed or expired. Read a fresh preview before confirming.",
    "plan_not_found": "This preview is unavailable for your account.",
    "unsupported_field_or_clear": "That field cannot be written safely yet. Keep it separate instead.",
    "unsupported_or_unknown_item": "One selected change is unsupported or no longer part of this preview.",
    "reconnect_required": "The tracker needs you to reconnect before another check.",
    "rate_limited": "The tracker asked us to wait. Saved progress and your lists are preserved; continue after the indicated delay.",
    "inventory_incomplete": "This list read was incomplete. No sync plan was applied.",
    "job_budget_exhausted": "This bounded check paused. Continue to resume saved list pages.",
}


def _private():
    if os.getenv("OMAKASE_PLUS_PRIVATE", "").strip().casefold() not in {"1", "true", "yes", "on"}:
        raise HTTPException(404, "Not found", headers=_HEADERS)


def oauth_config():
    client_id = os.getenv("MAL_CLIENT_ID", "").strip()
    secret = os.getenv("MAL_CLIENT_SECRET", "").strip()
    origin = os.getenv("OMAKASE_PLUS_URL", "").rstrip("/")
    u = urlsplit(origin)
    valid = bool(
        u.hostname
        and not u.username
        and not u.password
        and not u.query
        and not u.fragment
        and u.path in ("", "/")
        and (
            u.scheme == "https"
            or (u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1", "::1"))
        )
    )
    return (
        client_id,
        secret,
        origin + "/plus/integrations/myanimelist/callback",
        bool(client_id and secret and valid),
    )


async def _form(request):
    if not request.headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
        raise HTTPException(415, "Use the tracker form", headers=_HEADERS)
    form = await request.form()
    _single = {"csrf_token", "plan_hash", "enabled"}
    for name in _single:
        if len(form.getlist(name)) > 1 or not isinstance(form.get(name, ""), str):
            raise HTTPException(422, "Invalid tracker form", headers=_HEADERS)
    require_csrf(request, form.get("csrf_token", ""))
    return form


def create_mal_router(templates, context_factory=None, *, http_factory=None):
    """Root includes this relative router under /plus after migration qualification.

    GET renders saved state only. Every provider action is explicit POST or the
    state-bound OAuth callback; injected factory supports fully mocked tests.
    """
    router = APIRouter(
        route_class=bounded_form_route(16384, "Tracker form is too large", guard=_private)
    )
    factory = http_factory or (lambda: httpx.Client(trust_env=False))

    def build(db, http=None):
        return configured_service(db, http)

    def render(request, user, db, *, plan=None, notice="", error="", reading=None, code=200):
        ctx = (
            context_factory(request, user, active_page="settings")
            if context_factory
            else {"email": user.email, "active_page": "settings"}
        )
        return templates.TemplateResponse(
            request,
            "tracker_bridge.html",
            {
                **ctx,
                "csrf_token": csrf_token(request),
                "tracker_status": build(db).status(user.id),
                "mal_oauth_ready": oauth_config()[3],
                "tracker_plan": plan,
                "tracker_notice": notice,
                "tracker_error": error,
                "tracker_reading": reading,
            },
            status_code=code,
            headers=_HEADERS,
        )

    def failure(request, user, db, error):
        if isinstance(error, TrackerHTTPError):
            build(db).record_provider_error(user.id, error)
        code = error.code if isinstance(error, TrackerHTTPError) else str(error)
        status = 429 if code == "rate_limited" else 400
        response = render(
            request,
            user,
            db,
            error=_MESSAGES.get(
                code,
                "This action could not finish. Review the saved sync state before trying again.",
            ),
            code=status,
        )
        if isinstance(error, TrackerHTTPError) and error.retry_after:
            response.headers["Retry-After"] = str(error.retry_after)
        return response

    @router.get("/settings/myanimelist")
    def page(request: Request, user=Depends(require_user), db=Depends(get_db)):
        return render(request, user, db)

    @router.get("/api/tracker-bridge/status")
    def status(user=Depends(require_user), db=Depends(get_db)):
        from fastapi.responses import JSONResponse

        return JSONResponse(build(db).status(user.id), headers=_HEADERS)

    @router.post("/integrations/myanimelist/connect")
    async def connect(request: Request, user=Depends(require_user), db=Depends(get_db)):
        await _form(request)
        cid, secret, redirect, ready = oauth_config()
        try:
            if not ready:
                raise BridgeError("operator_configuration_required")
            verifier = pkce_verifier()
            state = build(db).begin_oauth(
                user.id,
                request.cookies.get("omakase_session", ""),
                cid + "\0" + secret + "\0" + redirect,
                verifier,
            )
            return RedirectResponse(
                authorize_url(cid, redirect, verifier, state), 303, headers=_HEADERS
            )
        except BridgeError as exc:
            return failure(request, user, db, exc)

    @router.get("/integrations/myanimelist/callback")
    async def callback(request: Request, user=Depends(require_user), db=Depends(get_db)):
        cid, secret, redirect, ready = oauth_config()
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        request.scope["query_string"] = (
            b""  # Avoid app access-log leakage; upstream logs need equivalent redaction.
        )
        try:
            if not ready:
                raise BridgeError("operator_configuration_required")
            if not code or len(code) > 4096 or not state or len(state) > 128:
                raise BridgeError("invalid_oauth_state")
            verifier = build(db).consume_oauth(
                user.id,
                request.cookies.get("omakase_session", ""),
                cid + "\0" + secret + "\0" + redirect,
                state,
            )

            def finish():
                with factory() as http:
                    service = build(db, http)
                    bundle = service.mal.exchange(code, verifier, redirect)
                    remote, name = service.mal.viewer(bundle.access_token)
                    service.save_connection(user.id, Provider.MAL, remote, name, bundle)

            await run_in_threadpool(finish)
            return RedirectResponse("/plus/settings/myanimelist", 303, headers=_HEADERS)
        except (BridgeError, TrackerHTTPError) as exc:
            return failure(request, user, db, exc)

    @router.post("/integrations/myanimelist/disconnect")
    async def disconnect(request: Request, user=Depends(require_user), db=Depends(get_db)):
        await _form(request)
        build(db).disconnect(user.id, Provider.MAL)
        return render(
            request,
            user,
            db,
            notice="MyAnimeList was disconnected locally. Pending bridge work was cancelled; your lists and watched history were kept. Review application access in MyAnimeList to revoke remote access.",
        )

    @router.post("/tracker-bridge/verify-anilist")
    async def verify_anilist(request: Request, user=Depends(require_user), db=Depends(get_db)):
        await _form(request)
        try:
            token = read_secret(db, user.id, "anilist_oauth_token")
            if not token:
                raise BridgeError("connection_required")

            def verify():
                with factory() as http:
                    service = build(db, http)
                    remote, name = service.anilist.viewer(token)
                    # Expiry is unknown for the existing bearer. Viewer is checked before each job; no refresh fabricated.
                    service.save_connection(
                        user.id, Provider.ANILIST, remote, name, TokenBundle(token, None, 2**63 - 1)
                    )

            await run_in_threadpool(verify)
            return render(
                request,
                user,
                db,
                notice="AniList identity verified. Read a preview before enabling any list changes.",
            )
        except (BridgeError, TrackerHTTPError) as exc:
            return failure(request, user, db, exc)

    @router.post("/tracker-bridge/preview")
    async def preview(request: Request, user=Depends(require_user), db=Depends(get_db)):
        await _form(request)
        try:

            def read():
                with factory() as http:
                    return build(db, http).preview(user.id)

            result = await run_in_threadpool(read)
            if result.get("status") == "reading":
                return render(
                    request,
                    user,
                    db,
                    reading=result,
                    notice="List reading paused at a safe page boundary. Continue to finish the preview. No list changes were sent.",
                )
            return render(request, user, db, plan=result)
        except (BridgeError, TrackerHTTPError) as exc:
            return failure(request, user, db, exc)

    @router.get("/tracker-bridge/plans/{plan_id}")
    def saved_plan(plan_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)):
        try:
            return render(request, user, db, plan=build(db).plan(user.id, plan_id))
        except BridgeError as exc:
            return failure(request, user, db, exc)

    @router.post("/tracker-bridge/plans/{plan_id}/resolve")
    async def resolve(
        plan_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)
    ):
        form = await _form(request)
        try:
            choices = []
            for name, value in form.multi_items():
                if not name.startswith("choice_") or not value:
                    continue
                if len(form.getlist(name)) != 1 or len(choices) >= 20:
                    raise BridgeError("invalid_choices")
                choice = {"conflict": name[7:], "source": value}
                rounded = form.get("round_" + name[7:], "")
                if rounded:
                    choice["round_to"] = int(rounded)
                choices.append(choice)
            result = build(db).resolve_plan(user.id, plan_id, form.get("plan_hash", ""), choices)
            return render(
                request,
                user,
                db,
                plan=result,
                notice="Choices saved as a new preview. Confirm selected changes separately.",
            )
        except (BridgeError, TrackerHTTPError, ValueError) as exc:
            return failure(request, user, db, exc)

    @router.post("/tracker-bridge/plans/{plan_id}/apply")
    async def apply(plan_id: str, request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request)
        try:

            def update():
                with factory() as http:
                    return build(db, http).apply(
                        user.id,
                        plan_id,
                        form.get("plan_hash", ""),
                        approved_items=form.getlist("approved"),
                    )

            result = await run_in_threadpool(update)
            return render(
                request,
                user,
                db,
                plan=build(db).plan(user.id, plan_id),
                notice=f"Confirmed: {result['acknowledged']}. Needs review: {result['conflicts']}. Waiting: {result['deferred'] + result['pending']}. Uncertain: {result['ambiguous']}. No uncertain action is automatically replayed.",
            )
        except (BridgeError, TrackerHTTPError) as exc:
            return failure(request, user, db, exc)

    @router.post("/tracker-bridge/preference")
    async def preference(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await _form(request)
        try:
            if form.get("enabled") not in ("0", "1"):
                raise BridgeError("invalid_preference")
            build(db).set_enabled(user.id, form.get("enabled") == "1")
            return render(
                request,
                user,
                db,
                notice="Tracker sync preference saved. Scheduled execution requires the configured worker; new titles and conflicts always wait for review.",
            )
        except BridgeError as exc:
            return failure(request, user, db, exc)

    return router
