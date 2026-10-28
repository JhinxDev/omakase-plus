"""Omakase Plus web routes.

Mounted at ``/plus`` via an ``APIRouter``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from omakase.adapters.anilist import AniListCatalogError
from omakase.engine import LLMOutputParseError
from omakase.engine import run as run_pipeline
from omakase.llm.codex import request_context
from omakase.plus.acquisition_policy import (
    ACQUISITION_POLICY_STORAGE_KEY,
    SUPPORTED_RESOLUTIONS,
    AcquisitionPolicy,
    load_user_acquisition_policy,
    policy_from_form,
)
from omakase.plus.agent_routes import create_agent_router
from omakase.plus.anilist import (
    AniListWriteError,
    add_to_planning,
    build_authorize_url,
    ensure_in_planning,
    exchange_code,
    generate_pkce,
    save_completed_entry,
    with_valid_token,
)
from omakase.plus.anime_synopsis import ensure_anime_synopsis
from omakase.plus.artwork_delivery import optimized_artwork
from omakase.plus.auth import (
    create_session,
    hash_password,
    session_max_age,
    verify_password,
)
from omakase.plus.auth import (
    delete_session as _delete_session,
)
from omakase.plus.avatar_routes import create_avatar_router
from omakase.plus.catalog_config import configured_catalogs
from omakase.plus.codex_jobs import CodexJobError, get_worker_status
from omakase.plus.completion_cleanup import (
    get_completion_cleanup_preference,
    retry_completion_cleanup,
    set_completion_cleanup_preference,
    set_keep_completed_in_plex,
)
from omakase.plus.connections_routes import create_connections_router
from omakase.plus.deps import get_db
from omakase.plus.discovery import DiscoveryRequest, save_preferences, saved_preferences
from omakase.plus.feedback import feedback_for_prompt, save_feedback
from omakase.plus.google_auth import create_google_router, google_enabled
from omakase.plus.manifest import (
    MANIFEST_SCHEMA,
    MAX_RECEIPT_BYTES,
    ManifestContractError,
    ReceiptConflictError,
    build_library_manifest,
    store_curator_receipt,
)
from omakase.plus.media_store import upsert_monitor
from omakase.plus.media_sync import reconcile_curator_blocked_assets, run_media_sync
from omakase.plus.middleware import require_user
from omakase.plus.movies_routes import create_movies_router
from omakase.plus.notifications_routes import create_notifications_router
from omakase.plus.oauth_flows import consume_oauth_flow, create_oauth_flow
from omakase.plus.onboarding_routes import create_onboarding_router
from omakase.plus.organization import build_plex_organization
from omakase.plus.posters import PosterUnavailable, banner_bytes, poster_bytes
from omakase.plus.product_views import (
    ProductViewError,
    activity_view,
    dashboard_view,
    discover_view,
    health_view,
    home_view,
    library_titles,
    release_status_label,
    saved_watchlist,
    setup_view,
    title_detail,
)
from omakase.plus.product_views import (
    clean_detail as _clean_download_detail,
)
from omakase.plus.product_views import (
    normalize_lane as _normalize_lane,
)
from omakase.plus.providers.realdebrid import RealDebridProvider
from omakase.plus.repairs import (
    RepairError,
    quarantine_asset,
    rematch_plan,
    reprobe_asset,
    rescan_title,
)
from omakase.plus.search_routes import create_search_router
from omakase.plus.secrets import delete_secret, read_secret, store_secret
from omakase.plus.title_removal import (
    TitleRemovalError,
    bulk_title_removal_preview,
    confirm_plex_removals,
    process_provider_removals,
    request_bulk_title_removal,
    request_title_removal,
    title_removal_preview,
)
from omakase.plus.tracker import (
    TrackerAction,
    TrackerActionError,
    enqueue_completed,
    get_action_by_id,
    mark_attempt,
    mark_failed,
    mark_synced,
)
from omakase.plus.tv_routes import create_tv_router
from omakase.plus.update_routes import create_updates_router
from omakase.plus.viewing_history import viewing_history
from omakase.plus.watch.apple_tv import (
    MAX_APPLE_TV_EVENT_BYTES,
    AppleTVWatchError,
    classify_observation,
    create_or_rotate_integration,
    disable_integration,
    integration_status,
    parse_observation,
    verify_signed_request,
)
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.plex_sync import (
    MAX_PLEX_SNAPSHOT_BYTES,
    PlexSnapshotError,
    latest_plex_library_summary,
    store_plex_library_snapshot,
)
from omakase.plus.watch.repository import get_watch_entry
from omakase.plus.watch.service import (
    WatchServiceError,
    ingest_watch_event,
    resolve_watch_confirmation,
)
from omakase.plus.web_security import csrf_token as _csrf_token
from omakase.plus.web_security import require_csrf as _require_csrf
from omakase.types import OmakaseConfig, resolve_model_preset

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
templates.context_processors.append(lambda request: {"google_enabled": google_enabled()})

router = APIRouter(prefix="/plus")
_STATIC_ASSETS = {
    "dashboard.css": "text/css",
    "streaming.css": "text/css",
    "atmosphere.css": "text/css",
    "cinema.css": "text/css",
    "connections.css": "text/css",
    "refinement.css": "text/css",
    "status-clarity.css": "text/css",
    "onboarding.css": "text/css",
    "tv.css": "text/css",
    "avatar.css": "text/css",
    "hero-quality.css": "text/css",
    "removal.css": "text/css",
    "feedback.css": "text/css",
    "semantic-states.css": "text/css",
    "notifications.css": "text/css",
    "notifications.js": "application/javascript",
    "episode-states.css": "text/css",
    "hero-quality.js": "application/javascript",
    "shelves.js": "application/javascript",
    "profile.js": "application/javascript",
    "search.js": "application/javascript",
    "geist-variable.woff2": "font/woff2",
    "dashboard.js": "application/javascript",
    "omakase-seal.svg": "image/svg+xml",
    "omakase-icon.svg": "image/svg+xml",
}
_TRANSPARENT_POSTER_PLACEHOLDER = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 2 3" '
    b'width="2" height="3" aria-hidden="true"></svg>'
)


def _static_asset_version() -> str:
    try:
        return str(
            max((_STATIC_DIR / asset_name).stat().st_mtime_ns for asset_name in _STATIC_ASSETS)
        )
    except OSError:
        return os.getenv("OMAKASE_REVISION", "dev")


templates.env.globals["asset_version"] = _static_asset_version()
templates.env.globals["release_status_label"] = release_status_label

# ── Rate limiting ───────────────────────────────────────────
# Simple in-memory: {ip: [timestamp, ...]}
_login_attempts: dict[str, list[float]] = defaultdict(list)
_RATE_LIMIT = 5
_RATE_WINDOW = 60  # seconds
_MACHINE_CACHE_HEADERS = {"Cache-Control": "private, no-store"}


class _RollingRateLimiter:
    def __init__(self, *, limit: int, window_seconds: int = 60):
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, source: str, fingerprint: str, *, now: float | None = None) -> int | None:
        current = time.monotonic() if now is None else now
        key = (source, fingerprint)
        with self._lock:
            events = self._events[key]
            cutoff = current - self.window_seconds
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.limit:
                return max(1, math.ceil(self.window_seconds - (current - events[0])))
            events.append(current)
        return None

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_manifest_rate_limiter = _RollingRateLimiter(limit=30)
_receipt_rate_limiter = _RollingRateLimiter(limit=12)
_snapshot_rate_limiter = _RollingRateLimiter(limit=12)


def _machine_response(payload: dict, status_code: int, **headers: str) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={**_MACHINE_CACHE_HEADERS, **headers},
    )


def _canonical_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _removal_provider(db, user_id: int) -> RealDebridProvider:
    api_key = read_secret(db, user_id, "realdebrid_api_key")
    if not api_key:
        raise ValueError("Real-Debrid is not connected")
    return RealDebridProvider(api_key)


@router.get("/health")
async def private_plus_health():
    """Expose a secret-free exact-source receipt only when the private router exists."""
    return _machine_response(
        {
            "status": "ok",
            "revision": os.getenv("OMAKASE_REVISION", "unknown"),
            "manifest_schema": MANIFEST_SCHEMA,
        },
        200,
    )


@router.post("/api/watch/apple-tv")
async def ingest_apple_tv_watch(request: Request, db=Depends(get_db)):
    """Accept a bounded HMAC-signed Home Assistant Apple TV observation."""
    raw_body = await request.body()
    if len(raw_body) > MAX_APPLE_TV_EVENT_BYTES:
        return _machine_response({"detail": "Request body is too large"}, 413)
    if os.getenv("OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED", "false").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return _machine_response({"detail": "Apple TV watch integration is disabled"}, 503)
    try:
        integration = verify_signed_request(
            db,
            raw_body=raw_body,
            key_id=request.headers.get("x-omakase-key-id", ""),
            timestamp_header=request.headers.get("x-omakase-timestamp", ""),
            signature_header=request.headers.get("x-omakase-signature", ""),
        )
    except AppleTVWatchError:
        return _machine_response({"detail": "Invalid integration authorization"}, 401)
    try:
        observation = parse_observation(raw_body, reference_epoch=integration.signed_at)
        classification = classify_observation(
            db,
            user_id=integration.user_id,
            observation=observation,
        )
    except AppleTVWatchError as exc:
        return _machine_response({"detail": str(exc)}, 400)
    if classification.event is None or classification.monitor_id is None:
        return _machine_response(
            {
                "disposition": classification.disposition,
                "reason": classification.reason,
                "progress_episodes": 0,
                "confirmation_id": None,
            },
            202,
        )
    try:
        result = ingest_watch_event(
            db,
            user_id=integration.user_id,
            monitor_id=classification.monitor_id,
            event=classification.event,
            observed_at=observation.observed_at,
        )
    except (WatchServiceError, ValueError):
        return _machine_response({"detail": "Watch event could not be applied"}, 400)
    return _machine_response(
        {
            "disposition": result.disposition,
            "reason": result.reason,
            "progress_episodes": result.entry.progress_episodes,
            "confirmation_id": result.confirmation_id,
        },
        202,
    )


def _machine_auth(request: Request) -> tuple[str, str] | JSONResponse:
    expected = os.getenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "")
    if not expected:
        return _machine_response({"detail": "Machine endpoint unavailable"}, 503)
    authorization = request.headers.get("authorization", "")
    match = re.fullmatch(r"Bearer ([^\s]+)", authorization)
    if match is None:
        return _machine_response({"detail": "Invalid machine authorization"}, 401)
    candidate = match.group(1)
    fingerprint = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(candidate, expected):
        return _machine_response({"detail": "Invalid machine authorization"}, 401)
    source = request.client.host if request.client else "unknown"
    return source, fingerprint


def _machine_rate_limit(
    limiter: _RollingRateLimiter,
    source: str,
    fingerprint: str,
) -> JSONResponse | None:
    retry_after = limiter.check(source, fingerprint)
    if retry_after is None:
        return None
    return _machine_response(
        {"detail": "Rate limit exceeded"},
        429,
        **{"Retry-After": str(retry_after)},
    )


@router.get("/api/library-manifest")
async def library_manifest(request: Request, db=Depends(get_db)):
    """Return the strict redacted manifest consumed only by the private curator."""
    auth = _machine_auth(request)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _machine_rate_limit(_manifest_rate_limiter, *auth)
    if limited is not None:
        return limited
    try:
        manifest = build_library_manifest(db)
    except (ManifestContractError, sqlite3.Error):
        return _machine_response({"detail": "Manifest contract unavailable"}, 500)
    return _machine_response(manifest, 200)


@router.get("/api/plex-organization")
async def plex_organization(request: Request, db=Depends(get_db)):
    """Return private, verified viewing shelves to the existing Plex curator."""
    auth = _machine_auth(request)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _machine_rate_limit(_manifest_rate_limiter, *auth)
    if limited is not None:
        return limited
    try:
        payload = build_plex_organization(db)
    except (ValueError, sqlite3.Error):
        return _machine_response({"detail": "Plex organization unavailable"}, 500)
    return _machine_response(payload, 200)


@router.post("/api/curator-receipts")
async def curator_receipts(request: Request, db=Depends(get_db)):
    """Validate curator evidence and retain only the latest converged live receipt."""
    auth = _machine_auth(request)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _machine_rate_limit(_receipt_rate_limiter, *auth)
    if limited is not None:
        return limited
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RECEIPT_BYTES:
                return _machine_response({"detail": "Receipt body too large"}, 413)
        except ValueError:
            return _machine_response({"detail": "Invalid receipt body"}, 422)
    body = await request.body()
    if len(body) > MAX_RECEIPT_BYTES:
        return _machine_response({"detail": "Receipt body too large"}, 413)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _machine_response({"detail": "Invalid curator receipt"}, 422)
    try:
        result = store_curator_receipt(db, payload)
    except ReceiptConflictError:
        return _machine_response({"detail": "Curator receipt conflicts with stored evidence"}, 409)
    except ManifestContractError:
        return _machine_response({"detail": "Invalid curator receipt"}, 422)
    except sqlite3.Error:
        return _machine_response({"detail": "Curator receipt storage unavailable"}, 500)
    if result["stored"]:
        confirm_plex_removals(db, payload)
        reconcile_curator_blocked_assets(db, payload)
        await process_provider_removals(
            db,
            provider_factory=lambda user_id: _removal_provider(db, user_id),
            completed_at=_canonical_now(),
        )
    return _machine_response(result, 200)


@router.post("/api/plex-library-snapshot")
async def plex_library_snapshot(request: Request, db=Depends(get_db)):
    """Store CT111's bounded anime inventory and apply exact Plex progress."""
    auth = _machine_auth(request)
    if isinstance(auth, JSONResponse):
        return auth
    limited = _machine_rate_limit(_snapshot_rate_limiter, *auth)
    if limited is not None:
        return limited
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_PLEX_SNAPSHOT_BYTES:
                return _machine_response({"detail": "Snapshot body too large"}, 413)
        except ValueError:
            return _machine_response({"detail": "Invalid snapshot body"}, 422)
    body = await request.body()
    if len(body) > MAX_PLEX_SNAPSHOT_BYTES:
        return _machine_response({"detail": "Snapshot body too large"}, 413)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return _machine_response({"detail": "Invalid Plex library snapshot"}, 422)
    try:
        result = store_plex_library_snapshot(
            db,
            payload,
            received_at=_canonical_now(),
        )
    except PlexSnapshotError:
        return _machine_response({"detail": "Invalid Plex library snapshot"}, 422)
    except sqlite3.Error:
        return _machine_response({"detail": "Plex library snapshot unavailable"}, 500)
    if result["stored"]:
        # The native curator posts its converged receipt before the actual Plex
        # inventory. A withdrawal completes only when both pieces prove absence.
        receipt = db.execute(
            "SELECT receipt_json FROM media_curator_receipt_latest WHERE id = 1"
        ).fetchone()
        if receipt is not None:
            confirm_plex_removals(db, json.loads(receipt["receipt_json"]))
    return _machine_response(result, 200)


# ── Background recommendation jobs ──────────────────────────
# A run can take 1-2 minutes (Pro mode / reasoning models). Calling the
# synchronous run_pipeline inline both exceeds the reverse-proxy read timeout
# (504s) and blocks uvicorn's single event loop for the whole run. So
# POST /api/run starts the run in a daemon thread and returns a job_id at once;
# the client polls /api/run/status/{job_id}. The container runs a single
# uvicorn worker, so an in-memory registry needs no cross-process coordination.
# Each background run saves its result through its own SQLite connection, so
# closing a browser tab cannot discard a completed tasting menu.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **fields: object) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _run_job(job_id: str, cfg: OmakaseConfig, profile_path: str) -> None:
    """Run the pipeline off the event loop; publish recs or error to the registry.

    Writes the payload before flipping ``status`` to its terminal value so a
    reader that sees a terminal status always sees a complete result.
    """
    try:
        with _jobs_lock:
            context = dict(_jobs[job_id])
        if cfg.llm_type == "codex":
            with request_context(context["user_id"], data_dir=context["data_dir"]):
                recs = run_pipeline(cfg)
        else:
            recs = run_pipeline(cfg)
        with sqlite3.connect(Path(context["data_dir"]) / "omakase-plus.db") as result_db:
            cursor = result_db.execute(
                "INSERT INTO run_history (user_id, source, model, picks, lane) VALUES (?, ?, ?, ?, ?)",
                (
                    context["user_id"],
                    context["source"],
                    context["model"],
                    json.dumps([asdict(rec) for rec in recs]),
                    context["lane"],
                ),
            )
            run_id = cursor.lastrowid
    except Exception as error:
        # Provider HTTP errors may embed keys in query strings. The browser
        # receives a safe explanation, never a provider URL or raw exception.
        if isinstance(error, AniListCatalogError):
            if error.category == "rate_limit":
                detail = "AniList reached a catalog rate limit. Wait a minute and try again."
            elif error.category == "transport":
                detail = "AniList could not respond to the catalog search. Try again later."
            else:
                detail = "AniList could not complete the catalog search. Try again later."
        elif isinstance(error, CodexJobError):
            detail = _clean_download_detail(error)
        elif isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
            detail = "Your AI provider refused authentication. Check its connection in Settings."
        elif isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 429:
            detail = "Your AI provider reached a rate limit. Wait a little and try again."
        elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
            detail = "Your AI provider took too long to respond. Try this run again."
        elif isinstance(error, (ValueError, LLMOutputParseError)):
            detail = (
                "The AI returned an incomplete or invalid shortlist. Try another mood or model."
            )
        else:
            detail = "Discovery could not complete. Check your AI connection or try again."
        _set_job(job_id, detail=detail, status="error")
    else:
        _set_job(job_id, recs=recs, run_id=run_id, status="completed")
    finally:
        if profile_path:
            try:
                os.unlink(profile_path)
            except OSError:
                pass


def _signup_allowed() -> bool:
    """Signup is only enabled in private mode or when an invite code is set."""
    private = os.getenv("OMAKASE_PLUS_PRIVATE", "false").lower() == "true"
    invite = bool(os.getenv("OMAKASE_PLUS_INVITE"))
    return private or invite


# ── Session cookie helpers ──────────────────────────────────

_SESSION_COOKIE = "omakase_session"


def _secure_session_cookie() -> bool:
    configured = os.getenv("OMAKASE_SESSION_COOKIE_SECURE", "").strip().lower()
    if configured:
        return configured in {"1", "true", "yes", "on"}
    return os.getenv("OMAKASE_PLUS_URL", "").lower().startswith("https://")


def _set_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE,
        value=session_id,
        httponly=True,
        max_age=session_max_age(),
        path="/plus",
        samesite="lax",
        secure=_secure_session_cookie(),
    )


def _delete_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=_SESSION_COOKIE,
        httponly=True,
        path="/plus",
        samesite="lax",
        secure=_secure_session_cookie(),
    )


router.include_router(create_google_router(templates, _set_session_cookie))


# ── Login ───────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login", response_class=HTMLResponse)
async def login_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db=Depends(get_db),
):
    ip = request.client.host if request.client else "unknown"
    now = datetime.now(timezone.utc).timestamp()
    _login_attempts[ip] = [t for t in _login_attempts[ip] if now - t < _RATE_WINDOW]

    # Check rate limit before verifying credentials
    if len(_login_attempts[ip]) >= _RATE_LIMIT:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many login attempts. Try again later."},
            status_code=429,
        )

    _login_attempts[ip].append(now)

    row = db.execute(
        "SELECT id, password_hash FROM users WHERE email = ?",
        (email,),
    ).fetchone()

    if row is None or not verify_password(password, row["password_hash"]):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid email or password."},
        )

    session_id = create_session(db, row["id"])
    response = RedirectResponse(url="/plus/home", status_code=302)
    _set_session_cookie(response, session_id)
    return response


# ── Signup ──────────────────────────────────────────────────


@router.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, error: str = ""):
    if not _signup_allowed():
        return RedirectResponse(url="/plus/login", status_code=302)
    return templates.TemplateResponse(request, "signup.html", {"error": error})


@router.post("/signup", response_class=HTMLResponse)
async def signup_post(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db=Depends(get_db),
):
    if not _signup_allowed():
        return RedirectResponse(url="/plus/login", status_code=302)

    if not email or not password:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "Email and password are required."},
        )

    if password != confirm_password:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "Passwords do not match."},
        )

    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if existing:
        return templates.TemplateResponse(
            request,
            "signup.html",
            {"error": "Email already registered."},
        )

    password_hash = hash_password(password)
    cursor = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email, password_hash),
    )
    user_id = cursor.lastrowid
    # Create an empty taste profile row
    db.execute(
        "INSERT INTO taste_profiles (user_id, content) VALUES (?, ?)",
        (user_id, ""),
    )
    db.commit()

    session_id = create_session(db, user_id)
    response = RedirectResponse(
        url="/plus/onboarding"
        if os.getenv("OMAKASE_PLUS_PRIVATE", "false").lower() == "true"
        else "/plus/home",
        status_code=302,
    )
    _set_session_cookie(response, session_id)
    return response


# ── Logout ──────────────────────────────────────────────────


@router.post("/logout")
async def logout(request: Request, db=Depends(get_db), user=Depends(require_user)):
    del user
    session_id = request.cookies.get(_SESSION_COOKIE)
    if session_id:
        _delete_session(db, session_id)
    response = RedirectResponse(url="/", status_code=302)
    _delete_session_cookie(response)
    return response


# ── Dashboard ───────────────────────────────────────────────


@router.get("/static/{asset_name}")
async def plus_static_asset(asset_name: str, request: Request):
    media_type = _STATIC_ASSETS.get(asset_name)
    if media_type is None:
        raise HTTPException(status_code=404)
    asset_path = _STATIC_DIR / asset_name
    if not asset_path.is_file():
        raise HTTPException(status_code=404)
    from omakase.plus.static_delivery import static_response

    return await run_in_threadpool(static_response, asset_path, media_type, request)


@router.get("/static/services/{asset_name}")
async def service_identity_asset(asset_name: str, request: Request):
    if asset_name not in {
        "realdebrid.png",
        "plex.png",
        "riven.png",
        "seerr.png",
        "prowlarr.png",
        "anilist.png",
    }:
        raise HTTPException(404)
    path = _STATIC_DIR / "services" / asset_name
    if not path.is_file():
        raise HTTPException(404)
    from omakase.plus.static_delivery import static_response

    return await run_in_threadpool(static_response, path, "image/png", request)


def _coerce_optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{field_name} must be an integer") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _coerce_optional_score(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("score must be a number from 1 to 10")
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise ValueError("score must be a number from 1 to 10") from None
    if not math.isfinite(score) or not 1 <= score <= 10:
        raise ValueError("score must be a number from 1 to 10")
    return round(score, 1)


async def _render_dashboard(
    request: Request,
    *,
    run: str = "",
    error: str = "",
    user,
    db,
    bulk_preview: list[dict] | None = None,
    bulk_query: str = "",
    status_code: int = 200,
):
    anilist_username = read_secret(db, user.id, "anilist_username") or ""
    view = dashboard_view(db, user_id=user.id, run=run)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "email": user.email,
            "anilist_username": anilist_username,
            **view,
            "bulk_preview": bulk_preview,
            "bulk_query": bulk_query,
            "csrf_token": _csrf_token(request),
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/dashboard")
async def dashboard(
    request: Request,
    run: str = "",
    error: str = "",
    user=Depends(require_user),
    db=Depends(get_db),
):
    return await _render_dashboard(
        request,
        run=run,
        error=error,
        user=user,
        db=db,
    )


def _product_context(request: Request, user, *, active_page: str) -> dict[str, object]:
    return {
        "email": user.email,
        **getattr(request.state, "account_avatar", {}),
        "active_page": active_page,
        "csrf_token": _csrf_token(request),
        "movies_enabled": os.getenv("OMAKASE_PLUS_PRIVATE", "").strip().casefold()
        in {"1", "true", "yes", "on"},
        "review_mode": os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").strip().casefold()
        in {"1", "true", "yes", "on"},
    }


router.include_router(create_connections_router(templates, _product_context))
router.include_router(create_updates_router(templates, _product_context))
router.include_router(create_agent_router(templates, _product_context))
router.include_router(create_avatar_router(templates, _product_context))
router.include_router(create_movies_router(templates, _product_context))
router.include_router(create_tv_router(templates, _product_context))
router.include_router(create_search_router(templates, _product_context))
router.include_router(create_notifications_router())
router.include_router(create_onboarding_router(templates, _product_context))


@router.get("/", include_in_schema=False)
async def plus_index(user=Depends(require_user)):
    del user
    return RedirectResponse(url="/plus/home", status_code=302)


@router.get("/home", response_class=HTMLResponse)
async def home_page(
    request: Request,
    message: str = "",
    user=Depends(require_user),
    db=Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            **_product_context(request, user, active_page="home"),
            **home_view(db, user_id=user.id),
            "watch_history": viewing_history(db, user_id=user.id, limit=6)["items"],
            "message": message,
        },
    )


@router.get("/media/{anilist_id}/poster")
async def anime_poster(
    anilist_id: int,
    user=Depends(require_user),
):
    """Serve cached AniList artwork through the authenticated Omakase origin."""
    del user
    try:
        payload, content_type = await poster_bytes(anilist_id)
        payload, content_type = await run_in_threadpool(optimized_artwork, payload, kind="poster")
    except PosterUnavailable:
        return Response(
            content=_TRANSPARENT_POSTER_PLACEHOLDER,
            media_type="image/svg+xml",
            headers={
                "Cache-Control": "private, max-age=300",
                "X-Omakase-Poster": "unavailable",
            },
        )
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400, stale-while-revalidate=604800"},
    )


@router.get("/media/{anilist_id}/banner")
async def anime_banner(anilist_id: int, user=Depends(require_user)):
    """Serve bounded wide AniList artwork through the authenticated origin."""
    del user
    try:
        payload, content_type = await banner_bytes(anilist_id)
        payload, content_type = await run_in_threadpool(optimized_artwork, payload, kind="banner")
    except PosterUnavailable:
        return Response(
            content=_TRANSPARENT_POSTER_PLACEHOLDER,
            media_type="image/svg+xml",
            headers={"Cache-Control": "private, max-age=300"},
        )
    return Response(
        content=payload,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=86400, stale-while-revalidate=604800"},
    )


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    lifecycle: str = "active",
    state: str = "all",
    provider: str = "all",
    content_type: str = "all",
    query: str = "",
    message: str = "",
    user=Depends(require_user),
    db=Depends(get_db),
):
    try:
        titles = library_titles(
            db,
            user_id=user.id,
            lifecycle=lifecycle,
            state=state,
            provider=provider,
            content_type=content_type,
            query=query,
        )
        title_suggestions = [
            item["title"]
            for item in library_titles(
                db,
                user_id=user.id,
                lifecycle="all",
            )
        ]
    except ProductViewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "library.html",
        {
            **_product_context(request, user, active_page="library"),
            "titles": titles,
            "saved_picks": saved_watchlist(db, user_id=user.id, query=query)
            if lifecycle in {"active", "all"}
            and state == "all"
            and provider == "all"
            and content_type == "all"
            else [],
            "lifecycle_filter": lifecycle,
            "state_filter": state,
            "provider_filter": provider,
            "content_type_filter": content_type,
            "title_suggestions": title_suggestions,
            "plex_library": latest_plex_library_summary(db),
            "query": query,
            "message": message,
        },
    )


@router.post("/watchlist/{anilist_id}/remove")
async def remove_saved_pick(
    request: Request,
    anilist_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    # This action removes a local saved pick only. The media pipeline and AniList
    # history remain separate.
    _require_csrf(request, csrf_token)
    db.execute(
        "DELETE FROM anilist_plannings WHERE user_id = ? AND anilist_id = ? "
        "AND NOT EXISTS (SELECT 1 FROM media_monitors "
        "WHERE user_id = ? AND anilist_id = ?)",
        (user.id, anilist_id, user.id, anilist_id),
    )
    db.commit()
    return RedirectResponse("/plus/library", status_code=303)


def _render_add_page(
    request: Request,
    user,
    *,
    options=(),
    query: str = "",
    season: str = "",
    message: str = "",
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "add.html",
        {
            **_product_context(request, user, active_page="add"),
            "options": tuple(options),
            "query": query,
            "season": season,
            "message": message,
        },
        status_code=status_code,
    )


@router.get("/add", response_class=HTMLResponse)
async def add_page(
    request: Request,
    message: str = "",
    user=Depends(require_user),
):
    del request, user
    location = "/plus/discover?mode=add"
    if message:
        location += f"&message={quote(message, safe='')}"
    return RedirectResponse(url=location, status_code=302)


@router.post("/add/preview", response_class=HTMLResponse)
async def add_preview(
    request: Request,
    query: str = Form(""),
    season: str = Form(""),
    csrf_token: str = Form(""),
    user=Depends(require_user),
):
    _require_csrf(request, csrf_token)
    from omakase.plus.direct import search_direct_requests

    try:
        options = search_direct_requests(query, season=season)
    except (ValueError, httpx.HTTPError) as exc:
        return _render_add_page(
            request,
            user,
            query=query,
            season=season,
            message=_clean_download_detail(exc),
            status_code=422,
        )
    if not options:
        return _render_add_page(
            request,
            user,
            query=query,
            season=season,
            message="No matching anime was found on AniList.",
            status_code=404,
        )
    return _render_add_page(
        request,
        user,
        options=options,
        query=query,
        season=season,
    )


@router.post("/add/acquire")
async def add_acquire(
    request: Request,
    anilist_id: int = Form(...),
    season: str = Form(""),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    from omakase.plus.direct import resolve_direct_request

    try:
        target = resolve_direct_request(str(anilist_id), season=season)
    except (ValueError, httpx.HTTPError) as exc:
        return RedirectResponse(
            url=f"/plus/add?message={quote(_clean_download_detail(exc), safe='')}",
            status_code=302,
        )
    monitor_id = _enroll_media_target(db, user, target)
    receipt = await run_media_sync(
        db,
        entrypoint="product_add",
        monitor_ids=(monitor_id,),
    )
    message = (
        "Added. Omakase will keep looking while the current media sync finishes."
        if receipt.outcome == "already_running"
        else "Added. The first media check has completed."
    )
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/add/plan")
async def add_plan(
    request: Request,
    anilist_id: int = Form(...),
    title: str = Form(""),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    existing = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    ).fetchone()
    if existing is None:
        tracker_status = _ensure_anilist_planning_best_effort(db, user.id, anilist_id)
        db.execute(
            """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
               VALUES (?, ?, ?, ?)""",
            (user.id, anilist_id, title, tracker_status),
        )
        db.commit()
    message = "Already in your plan." if existing else "Added to your AniList plan."
    return RedirectResponse(
        url=f"/plus/add?message={quote(message, safe='')}",
        status_code=302,
    )


@router.get("/library/{monitor_id}", response_class=HTMLResponse)
async def title_page(
    request: Request,
    monitor_id: int,
    message: str = "",
    completed: bool = False,
    user=Depends(require_user),
    db=Depends(get_db),
):
    title = title_detail(db, user_id=user.id, monitor_id=monitor_id)
    if title is None:
        raise HTTPException(status_code=404, detail="Media title not found")
    title.update(await ensure_anime_synopsis(db, user_id=user.id, anilist_id=title["anilist_id"]))
    tracker = db.execute(
        """SELECT o.sync_state FROM tracker_sync_outbox o
           JOIN watch_entries w ON w.id=o.watch_entry_id
           WHERE w.monitor_id=? AND o.user_id=? ORDER BY o.id DESC LIMIT 1""",
        (monitor_id, user.id),
    ).fetchone()
    return templates.TemplateResponse(
        request,
        "title.html",
        {
            **_product_context(request, user, active_page="library"),
            "title": title,
            "message": message,
            "completed": completed,
            "tracker_state": tracker["sync_state"] if tracker else None,
            "remove_completed_from_plex": get_completion_cleanup_preference(db, user_id=user.id),
        },
    )


def _watch_action_redirect(
    monitor_id: int, message: str, *, completed: bool = False
) -> RedirectResponse:
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}{'&completed=1' if completed else ''}",
        status_code=302,
    )


def _watch_action_error(monitor_id: int, error: Exception) -> RedirectResponse:
    detail = " ".join(str(error).split()).strip()[:180] or "Watch progress was not changed."
    return _watch_action_redirect(monitor_id, detail)


def _completion_message(result) -> str:
    message = "Marked complete and moved to Completed."
    cleanup = result.cleanup or {}
    if cleanup.get("status") == "awaiting_plex":
        message += " Removal from Plex is queued. Your history, rating and source media are saved."
    elif cleanup.get("status") == "blocked":
        message += " Plex removal needs attention; your history and rating are saved."
    elif cleanup.get("status") == "kept":
        message += " Kept in Plex for rewatching."
    return message


async def _sync_manual_watch(db, result) -> None:
    if result.outbox_id is None or os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").lower() == "true":
        return
    from omakase.plus.watch.anilist_sync import process_tracker_outbox

    await run_in_threadpool(process_tracker_outbox, db, outbox_id=result.outbox_id)


@router.post("/library/{monitor_id}/watch/next")
async def watch_next_episode(
    request: Request,
    monitor_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    entry = get_watch_entry(db, user_id=user.id, monitor_id=monitor_id)
    episode = (entry.progress_episodes if entry is not None else 0) + 1
    try:
        result = ingest_watch_event(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(
                source="manual",
                episode=episode,
                confidence="manual",
                event_key=f"manual:{uuid.uuid4().hex}",
            ),
            observed_at=_canonical_now(),
        )
    except (ValueError, WatchServiceError) as exc:
        return _watch_action_error(monitor_id, exc)
    message = (
        _completion_message(result)
        if result.entry.status == "completed"
        else f"Progress updated through episode {result.entry.progress_episodes}."
    )
    await _sync_manual_watch(db, result)
    return _watch_action_redirect(monitor_id, message, completed=result.entry.status == "completed")


@router.post("/library/{monitor_id}/watch/episode")
async def watch_exact_episode(
    request: Request,
    monitor_id: int,
    episode: int = Form(...),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        result = ingest_watch_event(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            event=WatchEvent.progress(
                source="manual",
                episode=episode,
                confidence="manual",
                event_key=f"manual:{uuid.uuid4().hex}",
            ),
            observed_at=_canonical_now(),
        )
    except (ValueError, WatchServiceError) as exc:
        return _watch_action_error(monitor_id, exc)
    message = (
        _completion_message(result)
        if result.entry.status == "completed"
        else f"Progress updated through episode {result.entry.progress_episodes}."
    )
    await _sync_manual_watch(db, result)
    return _watch_action_redirect(monitor_id, message, completed=result.entry.status == "completed")


@router.post("/library/{monitor_id}/watch/complete")
async def watch_mark_complete(
    request: Request,
    monitor_id: int,
    csrf_token: str = Form(""),
    score_ten: str = Form(""),
    completion_policy_present: str = Form(""),
    keep_completed_in_plex: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        score = int(score_ten) if score_ten.strip() else None
        if score is not None and not 1 <= score <= 10:
            raise ValueError("Score must be from 1 to 10")
        if completion_policy_present == "1":
            set_keep_completed_in_plex(
                db, user_id=user.id, monitor_id=monitor_id, keep=keep_completed_in_plex == "1"
            )
        result = ingest_watch_event(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            event=WatchEvent.complete(
                source="manual",
                event_key=f"manual:{uuid.uuid4().hex}",
            ),
            observed_at=_canonical_now(),
            score_ten=score,
        )
        db.commit()
    except (ValueError, WatchServiceError) as exc:
        db.rollback()
        return _watch_action_error(monitor_id, exc)
    await _sync_manual_watch(db, result)
    return _watch_action_redirect(monitor_id, _completion_message(result), completed=True)


@router.post("/library/{monitor_id}/completion-cleanup/retry")
async def retry_completed_plex_cleanup(
    request: Request,
    monitor_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        result = retry_completion_cleanup(
            db, user_id=user.id, monitor_id=monitor_id, requested_at=_canonical_now()
        )
        db.commit()
    except TitleRemovalError as exc:
        db.rollback()
        return _watch_action_error(monitor_id, exc)
    message = (
        "Removal from Plex is queued."
        if result["status"] == "awaiting_plex"
        else result.get("detail", "Your current completion preference is active.")
    )
    return _watch_action_redirect(monitor_id, message, completed=True)


@router.post("/library/{monitor_id}/watch/reopen")
@router.post("/library/{monitor_id}/watch/correct")
async def watch_reopen_or_correct(
    request: Request,
    monitor_id: int,
    episode: int = Form(...),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        result = ingest_watch_event(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            event=WatchEvent.reopen(
                episode=episode,
                event_key=f"manual:{uuid.uuid4().hex}",
                force=True,
            ),
            observed_at=_canonical_now(),
        )
    except (ValueError, WatchServiceError) as exc:
        return _watch_action_error(monitor_id, exc)
    return _watch_action_redirect(
        monitor_id,
        f"Progress corrected to episode {result.entry.progress_episodes}.",
    )


@router.post("/library/{monitor_id}/watch/confirm/{confirmation_id}/accept")
async def watch_confirmation_accept(
    request: Request,
    monitor_id: int,
    confirmation_id: int,
    episode: int = Form(0),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        resolved = resolve_watch_confirmation(
            db,
            user_id=user.id,
            confirmation_id=confirmation_id,
            accepted=True,
            episode=episode if episode > 0 else None,
            observed_at=_canonical_now(),
        )
    except (ValueError, WatchServiceError) as exc:
        return _watch_action_error(monitor_id, exc)
    progress = resolved.ingest_result.entry.progress_episodes if resolved.ingest_result else 0
    return _watch_action_redirect(monitor_id, f"Confirmed through episode {progress}.")


@router.post("/library/{monitor_id}/watch/confirm/{confirmation_id}/dismiss")
async def watch_confirmation_dismiss(
    request: Request,
    monitor_id: int,
    confirmation_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        resolve_watch_confirmation(
            db,
            user_id=user.id,
            confirmation_id=confirmation_id,
            accepted=False,
            observed_at=_canonical_now(),
        )
    except (ValueError, WatchServiceError) as exc:
        return _watch_action_error(monitor_id, exc)
    return _watch_action_redirect(monitor_id, "Watch suggestion dismissed.")


def _render_title_removal(
    request: Request,
    user,
    preview: dict,
    *,
    error: str = "",
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "title_remove.html",
        {
            **_product_context(request, user, active_page="library"),
            "removal": preview,
            "error": error,
        },
        status_code=status_code,
    )


def _selected_monitor_ids(values: list[object]) -> tuple[int, ...]:
    try:
        monitor_ids = tuple(int(str(value)) for value in values)
    except (TypeError, ValueError) as exc:
        raise TitleRemovalError("Every selected title id must be a positive integer") from exc
    if any(monitor_id <= 0 for monitor_id in monitor_ids):
        raise TitleRemovalError("Every selected title id must be a positive integer")
    return monitor_ids


def _render_bulk_title_removal(
    request: Request,
    user,
    preview: dict,
    *,
    error: str = "",
    status_code: int = 200,
):
    return templates.TemplateResponse(
        request,
        "bulk_cleanup.html",
        {
            **_product_context(request, user, active_page="library"),
            "cleanup": preview,
            "error": error,
        },
        status_code=status_code,
    )


@router.post("/library/completed/cleanup/preview", response_class=HTMLResponse)
async def completed_library_cleanup_preview(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    try:
        monitor_ids = _selected_monitor_ids(list(form.getlist("monitor_id")))
        preview = bulk_title_removal_preview(
            db,
            user_id=user.id,
            monitor_ids=monitor_ids,
        )
    except TitleRemovalError as exc:
        preview = bulk_title_removal_preview(db, user_id=user.id, monitor_ids=())
        return _render_bulk_title_removal(
            request,
            user,
            preview,
            error=str(exc),
            status_code=422,
        )
    return _render_bulk_title_removal(request, user, preview)


@router.post("/library/completed/cleanup/request")
async def request_completed_library_cleanup(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token", "")))
    raw_monitor_ids = list(form.getlist("monitor_id"))
    try:
        monitor_ids = _selected_monitor_ids(raw_monitor_ids)
        preview = bulk_title_removal_preview(
            db,
            user_id=user.id,
            monitor_ids=monitor_ids,
        )
        receipt = request_bulk_title_removal(
            db,
            user_id=user.id,
            monitor_ids=monitor_ids,
            confirmation=str(form.get("confirmation", "")),
            requested_at=_canonical_now(),
        )
    except TitleRemovalError as exc:
        try:
            preview = bulk_title_removal_preview(
                db,
                user_id=user.id,
                monitor_ids=_selected_monitor_ids(raw_monitor_ids),
            )
        except TitleRemovalError:
            preview = bulk_title_removal_preview(db, user_id=user.id, monitor_ids=())
        return _render_bulk_title_removal(
            request,
            user,
            preview,
            error=str(exc),
            status_code=422,
        )
    message = (
        f"Cleanup started for {receipt['requested']} completed title"
        f"{'s' if receipt['requested'] != 1 else ''}. Plex proof is required before "
        "eligible Real-Debrid media is deleted; AniList was not changed."
    )
    return RedirectResponse(
        url=f"/plus/library?lifecycle=completed&message={quote(message, safe='')}",
        status_code=302,
    )


@router.get("/library/{monitor_id}/remove", response_class=HTMLResponse)
async def title_removal_page(
    request: Request,
    monitor_id: int,
    user=Depends(require_user),
    db=Depends(get_db),
):
    try:
        preview = title_removal_preview(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
        )
    except TitleRemovalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _render_title_removal(request, user, preview)


@router.post("/library/{monitor_id}/remove")
async def remove_title_media(
    request: Request,
    monitor_id: int,
    confirmation: str = Form(""),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        preview = title_removal_preview(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
        )
        request_title_removal(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            confirmation=confirmation,
            requested_at=_canonical_now(),
        )
    except TitleRemovalError as exc:
        if str(exc) == "Media title not found":
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _render_title_removal(
            request,
            user,
            preview,
            error=str(exc),
            status_code=422,
        )
    message = (
        "Removal started. Plex will unpublish this title first; eligible "
        "Real-Debrid media will be deleted only after that proof. AniList was not changed."
    )
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}",
        status_code=302,
    )


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(
    request: Request,
    mode: str = "recommend",
    run: int | None = None,
    message: str = "",
    user=Depends(require_user),
    db=Depends(get_db),
):
    if mode not in {"recommend", "add"}:
        raise HTTPException(status_code=422, detail="Discover mode is unsupported")
    try:
        recommendations = discover_view(db, user_id=user.id, run_id=run)
    except ProductViewError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    profile = db.execute(
        "SELECT content, updated_at FROM taste_profiles WHERE user_id = ?", (user.id,)
    ).fetchone()
    with _jobs_lock:
        active_job_id = next(
            (
                key
                for key, job in _jobs.items()
                if job["user_id"] == user.id and time.monotonic() - job.get("started_at", 0) <= 1200
            ),
            "",
        )
    return templates.TemplateResponse(
        request,
        "discover.html",
        {
            **_product_context(request, user, active_page="discover"),
            **recommendations,
            "discovery_preferences": saved_preferences(db, user.id),
            "active_job_id": active_job_id,
            "codex_worker": get_worker_status(db),
            "configured_backend": read_secret(db, user.id, "llm_backend")
            or ("openai" if read_secret(db, user.id, "llm_api_key") else "ollama"),
            "taste_profile": profile["content"] if profile else "",
            "taste_profile_updated": profile["updated_at"] if profile else "",
            "anilist_username": read_secret(db, user.id, "anilist_username") or "",
            "discover_mode": mode,
            "message": message,
        },
    )


@router.get("/discover/title/{media_id}", response_class=HTMLResponse)
async def recommendation_page(
    request: Request,
    media_id: int,
    run: int,
    user=Depends(require_user),
    db=Depends(get_db),
):
    try:
        view = discover_view(db, user_id=user.id, run_id=run)
    except ProductViewError as exc:
        raise HTTPException(status_code=404, detail="Recommendation not found") from exc
    selected = view["latest"]
    pick = next(
        (item for item in selected["picks"] if item.get("media_id") == media_id),
        None,
    )
    if pick is None:
        raise HTTPException(status_code=404, detail="Recommendation not found")
    return templates.TemplateResponse(
        request,
        "recommendation.html",
        {
            **_product_context(request, user, active_page="discover"),
            "pick": pick,
            "run": selected,
            "media_id": media_id,
        },
    )


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    view: str = "watch",
    outcome: str = "all",
    page: int = 1,
    message: str = "",
    user=Depends(require_user),
    db=Depends(get_db),
):
    if view not in {"watch", "system"}:
        raise HTTPException(422, "Invalid history view")
    try:
        activity_data = activity_view(db, user_id=user.id, outcome=outcome, page=page)
        history = viewing_history(db, user_id=user.id, limit=24, page=page)
    except ProductViewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            **_product_context(request, user, active_page="activity"),
            **activity_data,
            "activity_view": view,
            "watch_history": history["items"],
            "history_page": history["page"],
            "history_has_more": history["has_more"],
            "message": message,
        },
    )


@router.get("/health/overview", response_class=HTMLResponse)
async def health_page(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    apple_tv_watch_enabled = os.getenv("OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    return templates.TemplateResponse(
        request,
        "health.html",
        {
            **_product_context(request, user, active_page="health"),
            **health_view(db, user_id=user.id),
            "revision": os.getenv("OMAKASE_REVISION", "unknown"),
            "apple_tv_integration": integration_status(db, user_id=user.id),
            "apple_tv_watch_enabled": apple_tv_watch_enabled,
            "remove_completed_from_plex": get_completion_cleanup_preference(db, user_id=user.id),
        },
    )


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    if os.getenv("OMAKASE_PLUS_PRIVATE", "false").lower() == "true":
        return RedirectResponse("/plus/onboarding", status_code=303)
    live = setup_view(db, user_id=user.id)
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            **_product_context(request, user, active_page="setup"),
            "setup_state": live,
            "setup_progress": live["progress"],
        },
    )


@router.post("/library/{monitor_id}/retry")
async def retry_title(
    request: Request,
    monitor_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    owned = db.execute(
        "SELECT 1 FROM media_monitors WHERE id = ? AND user_id = ?",
        (monitor_id, user.id),
    ).fetchone()
    if owned is None:
        raise HTTPException(status_code=404, detail="Media title not found")
    receipt = await run_media_sync(
        db,
        entrypoint="product_retry",
        monitor_ids=(monitor_id,),
    )
    message = f"Retry {receipt.outcome}"
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/library/{monitor_id}/reprobe")
async def reprobe_title_asset(
    request: Request,
    monitor_id: int,
    asset_id: int = Form(...),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    roots = {}
    for backend, variable in (
        ("local", "OMAKASE_IMPORT_ROOT"),
        ("zurg", "OMAKASE_ZURG_ROOT"),
    ):
        value = os.getenv(variable, "")
        if value:
            roots[backend] = Path(value)
    try:
        receipt = reprobe_asset(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            asset_id=asset_id,
            storage_roots=roots,
            verified_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        message = f"Media probe {receipt['outcome']}"
    except RepairError as exc:
        message = str(exc)
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/library/{monitor_id}/rescan")
async def rescan_title_in_plex(
    request: Request,
    monitor_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        rescan_title(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            token_reader=read_secret,
        )
        message = "Scoped Plex scan requested"
    except RepairError as exc:
        message = str(exc)
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/library/{monitor_id}/rematch-plan", response_class=HTMLResponse)
async def rematch_title_plan(
    request: Request,
    monitor_id: int,
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        plan = rematch_plan(db, user_id=user.id, monitor_id=monitor_id)
    except RepairError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "rematch.html",
        {
            **_product_context(request, user, active_page="library"),
            "plan": plan,
        },
    )


@router.post("/library/{monitor_id}/quarantine")
async def quarantine_title_asset(
    request: Request,
    monitor_id: int,
    asset_id: int = Form(...),
    reason: str = Form("Operator quarantined this release"),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    try:
        quarantine_asset(
            db,
            user_id=user.id,
            monitor_id=monitor_id,
            asset_id=asset_id,
            reason=reason,
        )
        message = "Release quarantined; no provider or local files were deleted"
    except RepairError as exc:
        message = str(exc)
    return RedirectResponse(
        url=f"/plus/library/{monitor_id}?message={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/dashboard/profile")
async def dashboard_profile(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
    profile: str = Form(""),
    csrf_token: str = Form(""),
    return_to: str = Form("/plus/dashboard"),
):
    _require_csrf(request, csrf_token)
    if len(profile) > 12000:
        raise HTTPException(
            status_code=422, detail="Taste notes must be 12,000 characters or fewer."
        )
    existing = db.execute("SELECT id FROM taste_profiles WHERE user_id = ?", (user.id,)).fetchone()
    if existing:
        db.execute(
            "UPDATE taste_profiles SET content = ?, updated_at = datetime('now') WHERE id = ?",
            (profile, existing["id"]),
        )
    else:
        db.execute(
            "INSERT INTO taste_profiles (user_id, content, updated_at) VALUES (?, ?, datetime('now'))",
            (user.id, profile),
        )
    db.commit()
    destination = "/plus/discover" if return_to == "/plus/discover" else "/plus/dashboard"
    return RedirectResponse(url=destination, status_code=302)


@router.post("/api/run")
async def api_run(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Start a recommendation run in the background and return a job id.

    The run executes in a daemon thread so this request returns in
    milliseconds; the client polls ``/plus/api/run/status/{job_id}``. This
    decouples the multi-minute LLM run from any single request's lifetime —
    which previously tripped the reverse-proxy read timeout — and keeps the
    event loop free to serve health checks and polls.
    """
    try:
        if len(await request.body()) > 8000:
            return JSONResponse(
                {"status": "error", "detail": "Discovery request is too large."}, status_code=413
            )
        options = DiscoveryRequest.model_validate(await request.json())
    except (ValidationError, ValueError, TypeError):
        return JSONResponse(
            {
                "status": "error",
                "detail": "Check the source, count (3 to 15), model and viewing preferences.",
            },
            status_code=422,
        )
    body = options.model_dump()
    source = body.get("source", "anilist")
    username = body.get("username", "")
    mode = body.get("mode", "fast")
    count = body.get("count", 8)
    temperature = body.get("temperature", 0.4)
    model = body.get("model", "")
    use_planning = body.get("use_planning", False)
    skip_profile = body.get("skip_profile", False)
    lane = _normalize_lane(body.get("lane"))
    if lane == "plan_list":
        use_planning = True

    if options.provider == "codex":
        if not get_worker_status(db)["online"]:
            return JSONResponse(
                {
                    "status": "error",
                    "detail": "Your Codex worker is offline. Start it on your PC or choose your configured AI.",
                },
                status_code=503,
            )
        if model and model not in {"gpt-6-astra", "gpt-6.1-sol"}:
            return JSONResponse(
                {"status": "error", "detail": "Choose Astra or Sol for Codex discovery."},
                status_code=422,
            )
    with _jobs_lock:
        expired = [
            key
            for key, job in _jobs.items()
            if time.monotonic() - job.get("started_at", time.monotonic()) > 1200
        ]
        for key in expired:
            _jobs.pop(key, None)
        for key, job in _jobs.items():
            if job["user_id"] == user.id and job["status"] == "running":
                return {"status": "running", "job_id": key, "resumed": True}

    tp = db.execute("SELECT content FROM taste_profiles WHERE user_id = ?", (user.id,)).fetchone()
    taste_profile_content = tp["content"] if tp else ""

    api_key = read_secret(db, user.id, "llm_api_key")
    llm_type = read_secret(db, user.id, "llm_backend") or ""
    if not llm_type:
        llm_type = "openai" if api_key else "ollama"
    if options.provider == "codex":
        llm_type = "codex"
        api_key = None
        model = model or "gpt-6-astra"

    default_model = "qwen2.5:7b" if mode == "fast" else "qwen2.5:14b"
    llm_url, llm_type_resolved, model_resolved, supports_json = resolve_model_preset(
        "http://localhost:11434",
        llm_type,
        model if model else default_model,
        mode,
    )

    profile_path = ""
    if taste_profile_content and not skip_profile:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write(taste_profile_content)
        tmp.close()
        profile_path = tmp.name

    default_username = read_secret(db, user.id, "anilist_username") or ""
    feedback = feedback_for_prompt(db, user.id)
    cfg = OmakaseConfig(
        source=source,
        username=username.strip() or default_username or user.email.split("@")[0],
        llm_url=llm_url,
        model=model_resolved,
        profile_path=profile_path,
        # Over-fetch: the genre-targeted pool is dominated by sequels of shows
        # already in the user's history, which the franchise filter then drops.
        # Fetching only `count` would leave ~1 survivor. Fetch a generous pool
        # so enough non-franchise candidates remain for `count` picks.
        candidate_pool_size=min(300, max(count * 12, 120)),
        num_recommendations=count,
        temperature=temperature,
        llm_type=llm_type_resolved,
        mode=mode,
        supports_json_mode=supports_json,
        use_planning=use_planning,
        recommendation_lane=lane,
        feedback=feedback,
        api_key=api_key,
        max_episodes=options.max_episodes,
        completed_only=options.completed_only,
        mood=options.mood,
    )

    save_preferences(db, user.id, options)
    db.commit()

    job_id = uuid.uuid4().hex
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "user_id": user.id,
            "source": source,
            "model": model_resolved,
            "lane": lane,
            "recs": None,
            "detail": "",
            "run_id": None,
            "started_at": time.monotonic(),
            "data_dir": str(Path(db.execute("PRAGMA database_list").fetchone()["file"]).parent),
        }
    threading.Thread(target=_run_job, args=(job_id, cfg, profile_path), daemon=True).start()

    return {"status": "running", "job_id": job_id}


@router.get("/api/run/status/{job_id}")
async def api_run_status(
    job_id: str,
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Poll a background recommendation job.

    Returns ``running`` while in flight or ``ok`` with an already-saved run.
    Failed or unknown/expired jobs return ``error``. No poll writes a second run.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None or job["user_id"] != user.id:
            return {"status": "error", "detail": "Unknown or expired job."}
        status = job["status"]
        if status == "running":
            return {"status": "running"}
        if status == "error":
            detail = job["detail"]
            _jobs.pop(job_id, None)
            return {"status": "error", "detail": detail}
        # status == "completed": persist once, then report ok.
        recs = job["recs"]
        source = job["source"]
        model = job["model"]
        lane = job["lane"]
        run_id = job["run_id"]

    with _jobs_lock:
        _jobs.pop(job_id, None)

    return {
        "status": "ok",
        "run_id": run_id,
        "source": source,
        "model": model,
        "lane": lane,
        "recommendations": [asdict(r) for r in recs],
    }


@router.post("/dashboard/run")
async def dashboard_run(
    db=Depends(get_db),
    user=Depends(require_user),
    source: str = Form("anilist"),
    mode: str = Form("fast"),
    count: int = Form(8),
    temperature: float = Form(0.4),
    username: str = Form(""),
    model: str = Form(""),
    use_planning: bool = Form(False),
    skip_profile: bool = Form(False),
    lane: str = Form("best_match"),
):
    lane = _normalize_lane(lane)
    if lane == "plan_list":
        use_planning = True

    # Read taste profile from DB
    tp = db.execute("SELECT content FROM taste_profiles WHERE user_id = ?", (user.id,)).fetchone()
    taste_profile_content = tp["content"] if tp else ""

    # Read LLM API key and preferred backend from stored secrets
    api_key = read_secret(db, user.id, "llm_api_key")
    llm_type = read_secret(db, user.id, "llm_backend") or ""
    if not llm_type:
        llm_type = "openai" if api_key else "ollama"

    default_model = "qwen2.5:7b" if mode == "fast" else "qwen2.5:14b"
    llm_url, llm_type_resolved, model_resolved, supports_json = resolve_model_preset(
        "http://localhost:11434",
        llm_type,
        model if model else default_model,
        mode,
    )

    # Write taste profile to a temp file so the engine can read it
    import tempfile

    profile_path = ""
    if taste_profile_content and not skip_profile:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False)
        tmp.write(taste_profile_content)
        tmp.close()
        profile_path = tmp.name

    default_username = read_secret(db, user.id, "anilist_username") or ""
    feedback = feedback_for_prompt(db, user.id)
    cfg = OmakaseConfig(
        source=source,
        username=username.strip() or default_username or user.email.split("@")[0],
        llm_url=llm_url,
        model=model_resolved,
        profile_path=profile_path,
        candidate_pool_size=300,
        num_recommendations=count,
        temperature=temperature,
        llm_type=llm_type_resolved,
        mode=mode,
        supports_json_mode=supports_json,
        use_planning=use_planning,
        recommendation_lane=lane,
        feedback=feedback,
        api_key=api_key,
    )

    try:
        recs = run_pipeline(cfg)
    except Exception as e:
        if profile_path:
            try:
                os.unlink(profile_path)
            except OSError:
                pass
        import urllib.parse

        msg = urllib.parse.quote(f"Recommendation failed: {e}")
        return RedirectResponse(
            url=f"/plus/dashboard?error={msg}",
            status_code=302,
        )

    # Clean up temp file
    if profile_path:
        try:
            os.unlink(profile_path)
        except OSError:
            pass

    picks_json = json.dumps([asdict(r) for r in recs])
    cursor = db.execute(
        "INSERT INTO run_history (user_id, source, model, picks, lane) VALUES (?, ?, ?, ?, ?)",
        (user.id, source, model_resolved, picks_json, lane),
    )
    db.commit()
    run_id = cursor.lastrowid

    return RedirectResponse(url=f"/plus/dashboard?run={run_id}", status_code=302)


@router.post("/dashboard/plan")
async def dashboard_plan(
    db=Depends(get_db),
    user=Depends(require_user),
    anilist_id: int = Form(...),
    title: str = Form(""),
):
    # Check if already planned locally
    existing = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    ).fetchone()
    if existing:
        return RedirectResponse(url="/plus/dashboard", status_code=302)

    tracker_status = _ensure_anilist_planning_best_effort(db, user.id, anilist_id)

    # Always insert locally
    db.execute(
        "INSERT INTO anilist_plannings (user_id, anilist_id, title, status) VALUES (?, ?, ?, ?)",
        (user.id, anilist_id, title, tracker_status),
    )
    db.commit()

    return RedirectResponse(url="/plus/dashboard", status_code=302)


def _ensure_anilist_planning_best_effort(db, user_id: int, anilist_id: int) -> str:
    client_id = os.getenv("ANILIST_CLIENT_ID", "")
    client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
    base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
    redirect_uri = f"{base_url}/plus/integrations/anilist/callback"
    try:
        with with_valid_token(db, user_id, client_id, client_secret, redirect_uri) as token:
            receipt = ensure_in_planning(token, anilist_id)
            status = str(receipt["entry"].get("status") or "").strip().upper()
            if status:
                return status
    except (AniListWriteError, KeyError, TypeError, ValueError, httpx.HTTPError):
        pass
    return "PLANNING"


def _enroll_media_target(db, user, target) -> int:
    existing = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, target.anilist_id),
    ).fetchone()
    if not existing:
        tracker_status = _ensure_anilist_planning_best_effort(db, user.id, target.anilist_id)
        cursor = db.execute(
            """INSERT INTO anilist_plannings
               (user_id, anilist_id, title, status)
               VALUES (?, ?, ?, ?)""",
            (user.id, target.anilist_id, target.title, tracker_status),
        )
        planning_id = cursor.lastrowid
    else:
        planning_id = existing["id"]
    monitor_id = upsert_monitor(
        db,
        user_id=user.id,
        anilist_id=target.anilist_id,
        title=target.title,
        search_titles=target.search_titles,
        genres=target.genres,
        anilist_planning_id=planning_id,
        format=target.format,
        release_status=target.status,
        season_number=target.season_number,
        expected_episodes=target.episodes,
        aired_episodes=target.aired_episodes,
        next_airing_at=target.next_airing_at,
        active=True,
    )
    db.commit()
    return monitor_id


@router.post("/dashboard/bulk-preview")
async def dashboard_bulk_preview(
    request: Request,
    bulk_query: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Resolve up to 25 requests without mutating local or provider state."""
    from omakase.plus.direct import AmbiguousMediaError, resolve_direct_request

    lines = [line.strip() for line in bulk_query.splitlines() if line.strip()]
    if not lines:
        return await _render_dashboard(
            request,
            error="Enter at least one anime title or AniList ID.",
            user=user,
            db=db,
            bulk_query=bulk_query,
            status_code=422,
        )
    if len(lines) > 25 or len(bulk_query) > 10_000 or any(len(line) > 500 for line in lines):
        return await _render_dashboard(
            request,
            error="Bulk preview accepts at most 25 lines of 500 characters each.",
            user=user,
            db=db,
            bulk_query=bulk_query[:10_000],
            status_code=422,
        )

    preview: list[dict] = []
    for index, line in enumerate(lines):
        try:
            target = resolve_direct_request(line, season="")
        except AmbiguousMediaError as exc:
            preview.append(
                {
                    "index": index,
                    "query": line,
                    "state": "ambiguous",
                    "options": exc.options,
                }
            )
        except (ValueError, httpx.HTTPError) as exc:
            preview.append(
                {
                    "index": index,
                    "query": line,
                    "state": "error",
                    "detail": _clean_download_detail(exc),
                }
            )
        else:
            preview.append(
                {
                    "index": index,
                    "query": line,
                    "state": "ready",
                    "target": target,
                }
            )
    return await _render_dashboard(
        request,
        user=user,
        db=db,
        bulk_preview=preview,
        bulk_query=bulk_query,
    )


@router.post("/dashboard/bulk-confirm")
async def dashboard_bulk_confirm(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Enroll exact previewed AniList IDs and invoke the shared scheduler once."""
    from omakase.plus.direct import resolve_direct_request

    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token") or ""))
    raw_ids = [str(value) for value in form.getlist("ready_id")]
    raw_ids.extend(
        str(value) for key, value in form.multi_items() if str(key).startswith("choice_")
    )
    ids: list[int] = []
    for raw in raw_ids:
        try:
            anilist_id = int(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid AniList selection") from None
        if anilist_id <= 0:
            raise HTTPException(status_code=422, detail="Invalid AniList selection")
        if anilist_id not in ids:
            ids.append(anilist_id)
    if not ids or len(ids) > 25:
        raise HTTPException(status_code=422, detail="Choose between 1 and 25 exact titles")

    targets = []
    for anilist_id in ids:
        try:
            target = resolve_direct_request(str(anilist_id), season="")
        except (ValueError, httpx.HTTPError):
            raise HTTPException(status_code=422, detail="AniList identity refresh failed") from None
        if target.anilist_id != anilist_id:
            raise HTTPException(status_code=422, detail="AniList identity mismatch")
        targets.append(target)
    monitor_ids = tuple(_enroll_media_target(db, user, target) for target in targets)
    receipt = await run_media_sync(
        db,
        entrypoint="bulk_confirmation",
        monitor_ids=monitor_ids,
    )
    message = f"Enrolled {len(monitor_ids)} title(s); sync {receipt.outcome}"
    return RedirectResponse(
        url=f"/plus/dashboard?error={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/dashboard/sync-monitor")
async def dashboard_sync_monitor(
    request: Request,
    monitor_id: int = Form(...),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    monitor = db.execute(
        "SELECT id, active FROM media_monitors WHERE id = ? AND user_id = ?",
        (monitor_id, user.id),
    ).fetchone()
    if monitor is None:
        raise HTTPException(status_code=404, detail="Media monitor not found")
    if not monitor["active"]:
        raise HTTPException(status_code=409, detail="Resume the monitor before syncing")
    receipt = await run_media_sync(
        db,
        entrypoint="dashboard_sync",
        monitor_ids=(monitor_id,),
    )
    message = (
        "Media sync is already running"
        if receipt.outcome == "already_running"
        else f"Monitor sync {receipt.outcome}"
    )
    return RedirectResponse(
        url=f"/plus/dashboard?error={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/dashboard/monitor")
async def dashboard_monitor_change(
    request: Request,
    monitor_id: int = Form(...),
    action: str = Form(...),
    csrf_token: str = Form(""),
    user=Depends(require_user),
    db=Depends(get_db),
):
    _require_csrf(request, csrf_token)
    if action not in {"pause", "resume"}:
        raise HTTPException(status_code=422, detail="Invalid monitor action")
    cursor = db.execute(
        """UPDATE media_monitors SET active = ?, updated_at = datetime('now')
           WHERE id = ? AND user_id = ?""",
        (1 if action == "resume" else 0, monitor_id, user.id),
    )
    if cursor.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=404, detail="Media monitor not found")
    db.commit()
    message = "Monitor resumed" if action == "resume" else "Monitor paused"
    return RedirectResponse(
        url=f"/plus/dashboard?error={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/dashboard/unplan")
async def dashboard_unplan(
    db=Depends(get_db),
    user=Depends(require_user),
    anilist_id: int = Form(...),
):
    """Remove an anime from the local planning list."""
    db.execute(
        "DELETE FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    )
    db.commit()
    return RedirectResponse(url="/plus/dashboard", status_code=302)


async def _run_download_for_planning(
    db,
    user,
    *,
    anilist_id: int,
    title: str,
    search_titles: list[str] | None = None,
) -> str:
    messages: list[str] = []

    existing = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    ).fetchone()
    if not existing:
        cursor = db.execute(
            "INSERT INTO anilist_plannings (user_id, anilist_id, title, status) VALUES (?, ?, ?, ?)",
            (user.id, anilist_id, title, "PLANNING"),
        )
        planning_id = cursor.lastrowid
        db.commit()
        messages.append("Queued")
    else:
        planning_id = existing["id"]

    from omakase.plus.automation import search_and_download

    result = await search_and_download(
        db,
        user.id,
        title,
        planning_id=planning_id,
        search_titles=search_titles,
    )

    if result["status"] == "ok":
        rd_id = result.get("rd_id", "")
        info = f"{result.get('torrent_title', title)} ({result.get('size', '?')}, {result.get('seeders', 0)}s)"
        messages.append(f"Downloading: {info}")
        db.execute(
            "UPDATE anilist_plannings SET download_status = ?, download_info = ?, rd_torrent_id = ? WHERE user_id = ? AND anilist_id = ?",
            ("requested", info, rd_id, user.id, anilist_id),
        )
        db.commit()
    elif result["status"] == "no_rd_key":
        detail = _clean_download_detail(result.get("detail", "Real-Debrid API key not configured"))
        messages.append("Set Real-Debrid API key in Settings to auto-download")
        db.execute(
            """UPDATE anilist_plannings
               SET download_status = ?, download_info = ?, rd_torrent_id = ''
               WHERE user_id = ? AND anilist_id = ?""",
            ("no_rd_key", detail, user.id, anilist_id),
        )
        db.commit()
    elif result["status"] == "not_found":
        detail = _clean_download_detail(result.get("detail", f'No torrents found for "{title}"'))
        messages.append(f'No torrents found on nyaa.si for "{title}"')
        db.execute(
            """UPDATE anilist_plannings
               SET download_status = ?, download_info = ?, rd_torrent_id = ''
               WHERE user_id = ? AND anilist_id = ?""",
            ("not_found", detail, user.id, anilist_id),
        )
        db.commit()
    elif result["status"] == "rd_error":
        detail = _clean_download_detail(result.get("detail", "error"))
        messages.append(f"Real-Debrid: {detail}")
        db.execute(
            """UPDATE anilist_plannings
               SET download_status = ?, download_info = ?, rd_torrent_id = ''
               WHERE user_id = ? AND anilist_id = ?""",
            ("error", detail, user.id, anilist_id),
        )
        db.commit()
    elif result["status"] == "rd_provider_block":
        detail = _clean_download_detail(
            result.get("detail", "Real-Debrid provider blocked this torrent")
        )
        messages.append(f"Real-Debrid provider blocked this batch: {detail}")
        db.execute(
            """UPDATE anilist_plannings
               SET download_status = ?, download_info = ?, rd_torrent_id = ''
               WHERE user_id = ? AND anilist_id = ?""",
            ("rd_provider_block", detail, user.id, anilist_id),
        )
        db.commit()

    return " · ".join(messages)


@router.post("/dashboard/download")
async def dashboard_download(
    db=Depends(get_db),
    user=Depends(require_user),
    anilist_id: int = Form(...),
    title: str = Form(""),
):
    """Create a local planning target, then search nyaa.si and add to Real-Debrid."""
    msg = await _run_download_for_planning(
        db,
        user,
        anilist_id=anilist_id,
        title=title,
    )
    return RedirectResponse(url=f"/plus/dashboard?error={quote(msg, safe='')}", status_code=302)


@router.post("/dashboard/direct-download")
async def dashboard_direct_download(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
    query: str = Form(""),
    season: str = Form(""),
    csrf_token: str = Form(""),
):
    """Resolve a known anime request, queue it locally, and start a Real-Debrid download."""
    _require_csrf(request, csrf_token)
    from omakase.plus.direct import resolve_direct_request

    try:
        target = resolve_direct_request(query, season=season)
    except (ValueError, httpx.HTTPError) as e:
        return RedirectResponse(
            url=f"/plus/dashboard?error={quote(str(e), safe='')}",
            status_code=302,
        )

    monitor_id = _enroll_media_target(db, user, target)
    receipt = await run_media_sync(
        db,
        entrypoint="direct_request",
        monitor_ids=(monitor_id,),
    )
    message = (
        "Media sync already running; request remains monitored"
        if receipt.outcome == "already_running"
        else f"Media monitor enrolled; sync {receipt.outcome}"
    )
    return RedirectResponse(
        url=f"/plus/dashboard?error={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/dashboard/remove-plan")
async def dashboard_remove_plan(
    db=Depends(get_db),
    user=Depends(require_user),
    anilist_id: int = Form(...),
):
    """Remove a local queue row without deleting non-planning AniList history."""
    row = db.execute(
        """SELECT title, status FROM anilist_plannings
           WHERE user_id = ? AND anilist_id = ?""",
        (user.id, anilist_id),
    ).fetchone()
    title = str(row["title"]) if row is not None else ""

    # Only a confirmed planning entry may be deleted remotely. Completed,
    # current, paused, dropped, and repeating history remains authoritative.
    if row is not None and row["status"] == "PLANNING":
        client_id = os.getenv("ANILIST_CLIENT_ID", "")
        client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
        base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
        redirect_uri = f"{base_url}/plus/integrations/anilist/callback"
        try:
            with with_valid_token(db, user.id, client_id, client_secret, redirect_uri) as token:
                from omakase.plus.anilist import remove_from_planning

                remove_from_planning(token, anilist_id)
        except (ValueError, httpx.HTTPError):
            pass

    # Remove from local database
    if row:
        db.execute(
            "DELETE FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
            (user.id, anilist_id),
        )
        db.commit()

    msg = f'Removed "{title}" from planning list' if title else "Removed from planning list"
    return RedirectResponse(url=f"/plus/dashboard?error={msg}", status_code=302)


@router.post("/dashboard/remove-from-rd")
async def dashboard_remove_from_rd(
    db=Depends(get_db),
    user=Depends(require_user),
    anilist_id: int = Form(...),
):
    """Refuse direct RD deletion; guarded replacement owns destructive cleanup."""
    row = db.execute(
        "SELECT title, rd_torrent_id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    ).fetchone()

    if not row or not row["rd_torrent_id"]:
        return RedirectResponse(
            url="/plus/dashboard?error=No+Real-Debrid+torrent+found+for+this+anime",
            status_code=302,
        )

    message = "RD deletion requires guarded replacement evidence; remove the planning row instead"
    return RedirectResponse(
        url=f"/plus/dashboard?error={quote(message, safe='')}",
        status_code=302,
    )


# ── Settings (secrets management) ───────────────────────────

_SECRET_KEYS = {
    "llm_api_key": "LLM API Key — your OpenAI / Anthropic / Gemini / DeepSeek key",
    "llm_backend": "LLM Backend — openai, anthropic, gemini, deepseek, openrouter, groq, together",
    "realdebrid_api_key": "Real-Debrid API key (from https://real-debrid.com/apitoken)",
}
_CATALOG_SECRET_KEYS = {
    "prowlarr_url": "Prowlarr server address",
    "prowlarr_api_key": "Prowlarr API key",
    "prowlarr_indexer_ids": "Enabled source IDs",
    "prowlarr_categories": "Search categories",
}


@router.get("/settings/storage", response_class=HTMLResponse)
@router.get("/settings/downloads", response_class=HTMLResponse)
@router.get("/settings/ai", response_class=HTMLResponse)
@router.get("/settings/tracking", response_class=HTMLResponse)
@router.get("/settings/preferences", response_class=HTMLResponse)
@router.get("/settings/sources", response_class=HTMLResponse)
@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, saved: str = "", user=Depends(require_user), db=Depends(get_db)
):
    from omakase.llm import list_backends

    section = request.url.path.rsplit("/", 1)[-1]
    if section == "settings":
        section = "preferences"
    stored = {}
    for key_name in _SECRET_KEYS:
        val = read_secret(db, user.id, key_name)
        stored[key_name] = "••••••••" if val else ""

    # Exclude the backend selector from the API key list — it's rendered separately
    api_keys = {k: v for k, v in _SECRET_KEYS.items() if k != "llm_backend"}
    api_stored = {k: v for k, v in stored.items() if k != "llm_backend"}

    stored_backend = read_secret(db, user.id, "llm_backend") or "openai"
    backends = sorted(backend for backend in list_backends() if backend != "codex")

    anilist_username = read_secret(db, user.id, "anilist_username") or ""
    anilist_connected = bool(read_secret(db, user.id, "anilist_oauth_token"))
    anilist_oauth_ready = bool(
        os.getenv("ANILIST_CLIENT_ID", "") and os.getenv("ANILIST_CLIENT_SECRET", "")
    )
    catalog_configuration = configured_catalogs(db, user.id)
    catalog_stored = {
        key_name: bool(read_secret(db, user.id, key_name)) for key_name in _CATALOG_SECRET_KEYS
    }
    policy_warning = ""
    try:
        acquisition_policy = load_user_acquisition_policy(db, user.id)
    except ValueError:
        acquisition_policy = AcquisitionPolicy()
        policy_warning = "Saved download criteria were invalid. Safe defaults are active."
    apple_tv_integration = integration_status(db, user_id=user.id)
    apple_tv_watch_enabled = os.getenv("OMAKASE_PLUS_APPLE_TV_WATCH_ENABLED", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            **_product_context(request, user, active_page="settings"),
            "settings_section": section,
            "storage_local_configured": bool(os.getenv("OMAKASE_IMPORT_ROOT")),
            "storage_download_configured": bool(os.getenv("OMAKASE_QBITTORRENT_URL")),
            "storage_cloud_configured": bool(read_secret(db, user.id, "realdebrid_api_key")),
            "keys": api_keys,
            "stored": api_stored,
            "saved": saved,
            "backends": backends,
            "stored_backend": stored_backend,
            "anilist_username": anilist_username,
            "anilist_connected": anilist_connected,
            "anilist_oauth_ready": anilist_oauth_ready,
            "catalog_configuration": catalog_configuration,
            "catalog_keys": _CATALOG_SECRET_KEYS,
            "catalog_stored": catalog_stored,
            "acquisition_policy": acquisition_policy,
            "acquisition_policy_view": acquisition_policy.settings_view(),
            "acquisition_policy_warning": policy_warning,
            "supported_resolutions": SUPPORTED_RESOLUTIONS,
            "apple_tv_integration": apple_tv_integration,
            "apple_tv_watch_enabled": apple_tv_watch_enabled,
            "remove_completed_from_plex": get_completion_cleanup_preference(db, user_id=user.id),
        },
    )


@router.post("/settings", response_class=HTMLResponse)
async def settings_post(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
):
    form = await request.form()
    _require_csrf(request, str(form.get("csrf_token") or ""))
    section = str(form.get("settings_section") or "")
    if section not in {"", "preferences", "tracking", "downloads", "sources", "ai"}:
        raise HTTPException(422, "Unknown settings section")
    return_path = "/plus/settings" + ("/" + section if section else "")
    moved_keys = {"realdebrid_api_key", "prowlarr_url", "prowlarr_api_key"}
    if any(form.get(key) or form.get(f"delete_{key}") for key in moved_keys):
        raise HTTPException(422, "Use Settings > Connections to change media credentials")
    user_id = user.id
    saved_keys: list[str] = []

    if form.get("completion_cleanup_present") == "1":
        set_completion_cleanup_preference(
            db, user_id=user_id, remove_from_plex=form.get("remove_completed_from_plex") == "1"
        )
        db.commit()
        saved_keys.append("completed anime preference")

    for key_name in (*_SECRET_KEYS, *_CATALOG_SECRET_KEYS):
        if key_name in moved_keys:
            continue
        value = form.get(key_name, "").strip()
        if value and value != "••••••••":
            store_secret(db, user_id, key_name, value)
            saved_keys.append(key_name)
        elif form.get(f"delete_{key_name}", ""):
            delete_secret(db, user_id, key_name)

    if form.get("acquisition_policy_present", ""):
        if form.get("reset_acquisition_policy", ""):
            delete_secret(db, user_id, ACQUISITION_POLICY_STORAGE_KEY)
            saved_keys.append("default download criteria restored")
        else:
            try:
                acquisition_policy = policy_from_form(form)
            except ValueError as exc:
                message = quote(str(exc), safe="")
                return RedirectResponse(
                    url=f"{return_path}?saved={message}",
                    status_code=302,
                )
            store_secret(
                db,
                user_id,
                ACQUISITION_POLICY_STORAGE_KEY,
                acquisition_policy.to_json(),
            )
            saved_keys.append("download criteria")

    # AniList username is a plain (non-masked) preference, not a secret key —
    # store the submitted value directly, or clear it when blanked.
    anilist_username = form.get("anilist_username", "").strip()
    if anilist_username:
        store_secret(db, user_id, "anilist_username", anilist_username)
        saved_keys.append("anilist_username")
    elif "anilist_username" in form:
        delete_secret(db, user_id, "anilist_username")

    msg = f"Saved: {', '.join(saved_keys)}" if saved_keys else "No changes made."
    return RedirectResponse(
        url=f"{return_path}?saved={quote(msg, safe='')}",
        status_code=302,
    )


@router.post("/settings/watch-integrations/apple-tv/create", response_class=HTMLResponse)
@router.post("/settings/watch-integrations/apple-tv/rotate", response_class=HTMLResponse)
async def apple_tv_integration_create_or_rotate(
    request: Request,
    csrf_token: str = Form(""),
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Create fresh bridge material and reveal the shared secret once."""
    _require_csrf(request, csrf_token)
    created = create_or_rotate_integration(db, user_id=user.id)
    return templates.TemplateResponse(
        request,
        "apple_tv_secret.html",
        {
            **_product_context(request, user, active_page="settings"),
            "key_id": created.key_id,
            "shared_secret": created.secret,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/settings/watch-integrations/apple-tv/disable")
async def apple_tv_integration_disable(
    request: Request,
    csrf_token: str = Form(""),
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Disable signed Apple TV observation ingestion for the owner."""
    _require_csrf(request, csrf_token)
    disable_integration(db, user_id=user.id)
    return RedirectResponse(
        url="/plus/settings/tracking?saved=Apple+TV+watch+bridge+disabled",
        status_code=302,
    )


# ── AniList OAuth integration ──────────────────────────────


@router.get("/integrations/anilist/connect")
async def anilist_connect(db=Depends(get_db), user=Depends(require_user)):
    """Initiate AniList OAuth flow (Authorization Code with PKCE)."""
    client_id = os.getenv("ANILIST_CLIENT_ID", "")
    client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return HTMLResponse("AniList OAuth is not configured.", status_code=500)

    code_verifier, code_challenge = generate_pkce()
    state = create_oauth_flow(
        db,
        user_id=user.id,
        provider="anilist",
        code_verifier=code_verifier,
    )

    base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
    redirect_uri = f"{base_url}/plus/integrations/anilist/callback"

    authorize_url = build_authorize_url(client_id, redirect_uri, code_challenge, state)
    return RedirectResponse(url=authorize_url, status_code=302)


@router.get("/integrations/anilist/callback")
async def anilist_callback(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Handle the AniList OAuth redirect and store the access token."""
    code = request.query_params.get("code")
    state = request.query_params.get("state", "")
    if not code or not state:
        return HTMLResponse("Missing OAuth callback parameters.", status_code=400)

    code_verifier = consume_oauth_flow(
        db,
        user_id=user.id,
        provider="anilist",
        state=state,
    )
    if code_verifier is None:
        return HTMLResponse(
            "OAuth state is invalid or expired. Please start the connect flow again.",
            status_code=400,
        )

    client_id = os.getenv("ANILIST_CLIENT_ID", "")
    client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
    base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
    redirect_uri = f"{base_url}/plus/integrations/anilist/callback"

    if not client_id or not client_secret:
        return HTMLResponse("AniList credentials not configured.", status_code=500)

    try:
        token = exchange_code(client_id, client_secret, redirect_uri, code, code_verifier)
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return HTMLResponse(
            "AniList did not complete the connection. Return to Settings and try again.",
            status_code=502,
        )

    store_secret(db, user.id, "anilist_oauth_token", token)
    return RedirectResponse(url="/plus/settings/tracking", status_code=302)


@router.post("/integrations/anilist/disconnect")
async def anilist_disconnect(
    request: Request,
    csrf_token: str = Form(""),
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Remove the stored AniList OAuth token."""
    _require_csrf(request, csrf_token)
    delete_secret(db, user.id, "anilist_oauth_token")
    return RedirectResponse(
        url="/plus/settings/tracking?saved=AniList+disconnected", status_code=302
    )


def _sync_completed_tracker_action(
    db,
    *,
    user_id: int,
    action: TrackerAction,
) -> dict[str, object]:
    if action.sync_state == "synced":
        return {"tracker_status": "synced", "idempotent": True}
    if action.tracker != "anilist":
        mark_failed(
            db,
            user_id=user_id,
            action_id=action.id,
            detail="Tracker writes are not implemented for this source",
            unsupported=True,
        )
        return {
            "tracker_status": "unsupported",
            "detail": "Tracker write support is not configured for this source.",
        }

    client_id = os.getenv("ANILIST_CLIENT_ID", "")
    client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
    base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
    redirect_uri = f"{base_url}/plus/integrations/anilist/callback"
    mark_attempt(db, user_id=user_id, action_id=action.id)
    try:
        with with_valid_token(db, user_id, client_id, client_secret, redirect_uri) as token:
            remote = save_completed_entry(
                token,
                action.media_id,
                score_ten=action.score_ten,
            )
    except (AniListWriteError, httpx.HTTPError):
        mark_failed(
            db,
            user_id=user_id,
            action_id=action.id,
            detail="AniList list update failed and can be retried",
        )
        return {
            "tracker_status": "retryable",
            "detail": "AniList did not confirm the update. Open History, then Library & services to retry.",
        }
    except ValueError:
        mark_failed(
            db,
            user_id=user_id,
            action_id=action.id,
            detail="AniList connection is missing or expired",
        )
        return {
            "tracker_status": "connection_required",
            "detail": "Connect AniList in Settings to sync the watched entry.",
        }
    mark_synced(
        db,
        user_id=user_id,
        action_id=action.id,
        remote_entry_id=int(remote["id"]),
    )
    return {
        "tracker_status": "synced",
        "remote_status": remote["status"],
        "score": action.score_ten,
    }


@router.post("/activity/tracker/{action_id}/retry")
async def retry_tracker_action(
    request: Request,
    action_id: int,
    csrf_token: str = Form(""),
    db=Depends(get_db),
    user=Depends(require_user),
):
    _require_csrf(request, csrf_token)
    action = get_action_by_id(db, user_id=user.id, action_id=action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Tracker action not found")
    result = _sync_completed_tracker_action(db, user_id=user.id, action=action)
    state = str(result["tracker_status"]).replace("_", " ")
    message = f"{action.tracker.title()} watched sync: {state}"
    return RedirectResponse(
        url=f"/plus/activity?view=system&message={quote(message, safe='')}",
        status_code=302,
    )


@router.post("/api/feedback")
async def feedback_api(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
):
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "detail": "Invalid JSON body"}
    if not isinstance(body, dict):
        return {"status": "error", "detail": "Invalid JSON body"}

    feedback_type = body.get("feedback_type", "")
    title = body.get("title", "")
    source = body.get("source")

    if not isinstance(feedback_type, str):
        return {"status": "error", "detail": "feedback_type must be a string"}
    if not isinstance(title, str):
        return {"status": "error", "detail": "title must be a string"}
    if source is None:
        source = "anilist"
    elif not isinstance(source, str):
        return {"status": "error", "detail": "source must be a string"}
    if not title:
        return {"status": "error", "detail": "title is required"}

    try:
        media_id = _coerce_optional_int(body.get("media_id"), "media_id")
        run_id = _coerce_optional_int(body.get("run_id"), "run_id")
        score = _coerce_optional_score(body.get("score"))
    except ValueError as e:
        return {"status": "error", "detail": str(e)}
    if score is not None and feedback_type != "already_watched":
        return {"status": "error", "detail": "score is only valid for already watched"}

    if run_id is not None:
        run_row = db.execute(
            "SELECT id FROM run_history WHERE id = ? AND user_id = ?",
            (run_id, user.id),
        ).fetchone()
        if run_row is None:
            return {"status": "error", "detail": "run_id is invalid"}

    try:
        save_feedback(db, user.id, source, media_id, title, feedback_type, run_id)
    except ValueError as e:
        return {"status": "error", "detail": str(e)}
    if feedback_type != "already_watched":
        return {"status": "ok", "local_saved": True}
    if media_id is None:
        return {
            "status": "ok",
            "local_saved": True,
            "tracker_status": "unavailable",
            "detail": "Saved locally; this recommendation has no tracker media ID.",
        }
    try:
        action = enqueue_completed(
            db,
            user_id=user.id,
            tracker=source,
            media_id=media_id,
            title=title,
            score_ten=score,
        )
    except TrackerActionError as exc:
        return {
            "status": "ok",
            "local_saved": True,
            "tracker_status": "unsupported",
            "detail": str(exc),
        }
    if action.sync_state == "synced":
        return {
            "status": "ok",
            "local_saved": True,
            "tracker_status": "synced",
            "idempotent": True,
        }
    return {
        "status": "ok",
        "local_saved": True,
        **_sync_completed_tracker_action(db, user_id=user.id, action=action),
    }


@router.post("/api/plan")
async def plan_api(
    request: Request,
    db=Depends(get_db),
    user=Depends(require_user),
):
    """Add an anime to the user's AniList Planning list (JSON endpoint).

    Accepts ``{"anilist_id": 123, "title": "...", "status": "PLANNING"}``.
    Deduplicates against the local ``anilist_plannings`` table.
    """
    try:
        body = await request.json()
    except Exception:
        return {"status": "error", "detail": "Invalid JSON body"}

    anilist_id = body.get("anilist_id")
    title = body.get("title", "")
    status_val = body.get("status", "PLANNING")

    if not anilist_id or not title:
        return {"status": "error", "detail": "anilist_id and title are required"}

    # Local dedupe
    existing = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    ).fetchone()
    if existing:
        return {"status": "already_planned"}

    client_id = os.getenv("ANILIST_CLIENT_ID", "")
    client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
    base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
    redirect_uri = f"{base_url}/plus/integrations/anilist/callback"

    tracker_status = status_val
    try:
        with with_valid_token(db, user.id, client_id, client_secret, redirect_uri) as token:
            if status_val == "PLANNING":
                receipt = ensure_in_planning(token, anilist_id)
                tracker_status = str(receipt["entry"].get("status") or status_val).upper()
            else:
                add_to_planning(token, anilist_id, status_val)
    except ValueError as e:
        return {"status": "error", "detail": str(e)}
    except httpx.HTTPError as e:
        return {"status": "error", "detail": f"AniList API error: {e}"}

    db.execute(
        "INSERT INTO anilist_plannings (user_id, anilist_id, title, status) VALUES (?, ?, ?, ?)",
        (user.id, anilist_id, title, tracker_status),
    )
    db.commit()

    return {"status": "ok"}


# ── Plan & Download (Nyaa + Real-Debrid) ─────────────────────


@router.post("/api/plan-and-download")
async def plan_and_download(
    request: Request,
    user=Depends(require_user),
    db=Depends(get_db),
):
    """Plan an anime on AniList, then search nyaa.si and add to Real-Debrid.

    Body: {"anilist_id": 123, "title": "Anime Title"}
    """
    body = await request.json()
    anilist_id = body.get("anilist_id")
    title = body.get("title", "")

    if not anilist_id or not title:
        return {"status": "error", "detail": "anilist_id and title are required"}

    # 1. Add to AniList Planning if not already planned
    existing = db.execute(
        "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ?",
        (user.id, anilist_id),
    ).fetchone()

    if not existing:
        tracker_status = _ensure_anilist_planning_best_effort(db, user.id, anilist_id)

        cursor = db.execute(
            "INSERT INTO anilist_plannings (user_id, anilist_id, title, status) VALUES (?, ?, ?, ?)",
            (user.id, anilist_id, title, tracker_status),
        )
        planning_id = cursor.lastrowid
        db.commit()
    else:
        planning_id = existing["id"]

    # 2. Search nyaa.si for the best torrent
    from omakase.plus.automation import search_and_download

    result = await search_and_download(db, user.id, title, planning_id=planning_id)
    return result
