"""FastAPI web server for the Omakase recommendation and Plus products.

Deployment modes
----------------
* BYOK/Lite public mode (``OMAKASE_PLUS_PRIVATE`` unset or false): the public
  recommendation product at ``/`` and its ``/api/*`` endpoints are available
  exactly as before.
* Plus private mode (``OMAKASE_PLUS_PRIVATE=true``, set by the shipped Plus
  compose files): the authenticated product under ``/plus`` is mounted and the
  unauthenticated legacy product is gated off that origin. ``/`` redirects into
  the Plus UI and the legacy ``/api/*`` endpoints no longer exist. Public
  front-door routes a Plus deployment needs (``/plus/login``, ``/plus/health``,
  static assets, ``/favicon.ico``) stay reachable.

Neither mode hosts an API console on the app origin: ``/docs`` redirects to the
maintained documentation site and ``/openapi.json``/``/redoc`` are absent, so an
app origin cannot enumerate its authenticated or administrative routes.

Run with:  omakase web
Or:        python -m omakase web
"""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

from omakase import __version__
from omakase.adapters.base import list_sources
from omakase.adapters.myanimelist import MALExportError
from omakase.engine import EmptyHistoryError, LLMOutputParseError
from omakase.engine import run as run_pipeline
from omakase.llm import list_backends
from omakase.types import DEFAULT_URLS, MODEL_PRESETS, OmakaseConfig, resolve_model_preset
from omakase.web.outbound import OutboundURLError, validate_user_outbound_url

# Hard cap on the uploaded MAL export. A 5000-entry list compresses to
# well under 1 MB; this is the "user uploaded the wrong file" guardrail.
_MAX_EXPORT_BYTES = 10 * 1024 * 1024

_HERE = Path(__file__).resolve().parent


def _asset_version() -> str:
    """Cache-bust query string for the static stylesheet.

    Built once at import time from the stylesheet's mtime — changes
    every time someone edits style.css and rebuilds the container,
    which is exactly when we need users to skip their browser cache.
    Falls back to the package version if stat() fails (e.g. inside an
    odd packaging scenario).
    """
    css = _HERE / "static" / "style.css"
    try:
        return str(int(css.stat().st_mtime))
    except OSError:
        return __version__


_ASSET_VERSION = _asset_version()

_PRODUCTION = os.getenv("OMAKASE_ENV", "").strip().lower() == "production"
_ALLOW_PRIVATE_LLM_URLS = (
    os.getenv("OMAKASE_ALLOW_PRIVATE_LLM_URLS", "").strip().lower() == "true" or not _PRODUCTION
)
_PUBLIC_LLM_HOSTS = {
    parsed.hostname
    for value in DEFAULT_URLS.values()
    if (parsed := urlsplit(value)).hostname is not None
}

# Documentation is hosted separately. The app origin exposes no API console or
# OpenAPI document, so a deployment cannot enumerate its authenticated or
# administrative routes for anonymous visitors.
_DOCS_SITE = "https://omakase-plus.jhinx.dev/docs"

_TRUTHY = {"1", "true", "yes", "on"}


def _private_plus_mode() -> bool:
    """Return True for a deployed (authenticated) Plus instance.

    ``OMAKASE_PLUS_PRIVATE`` describes the deployment, not source visibility:
    the shipped Plus compose files set it to ``true`` and mount ``/plus``.
    """
    return os.getenv("OMAKASE_PLUS_PRIVATE", "").strip().lower() in _TRUTHY


app = FastAPI(
    title="Omakase",
    version=__version__,
    # Never expose an API console or route inventory on the served origin; see
    # the ``/docs`` route below for the documentation-site redirect.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

_allowed_hosts = [
    host.strip() for host in os.getenv("OMAKASE_ALLOWED_HOSTS", "").split(",") if host.strip()
]
if _allowed_hosts and "*" not in _allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)


def _legacy_surface_response(request) -> Response | None:
    """Gate the unauthenticated legacy product on Plus origins.

    BYOK/Lite public deployments (``OMAKASE_PLUS_PRIVATE`` unset or false) keep
    the legacy recommendation product at ``/`` and ``/api/*`` unchanged. A Plus
    deployment serves only the authenticated product: browsers entering the
    origin's root are sent to the Plus UI, and the legacy JSON endpoints no
    longer exist on that origin. The routes a Plus deployment still needs in
    public (``/plus/login``, ``/plus/health``, static assets, ``/favicon.ico``)
    are not affected.
    """
    if not _private_plus_mode():
        return None
    path = request.url.path
    if path == "/" and request.method in {"GET", "HEAD"}:
        return RedirectResponse(
            url="/plus/home",
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )
    if path == "/api" or path.startswith("/api/"):
        return JSONResponse(
            {"detail": "Not Found"},
            status_code=404,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    return None


@app.middleware("http")
async def plus_security_headers(request, call_next):
    """Apply a self-contained browser policy to the private Plus product."""
    gated = _legacy_surface_response(request)
    if gated is not None:
        return gated
    response = await call_next(request)
    if request.url.path == "/plus" or request.url.path.startswith("/plus/"):
        refresh = getattr(request.state, "session_cookie_refresh", None)
        # Explicit login/logout cookie changes take precedence over idle renewal.
        has_session_cookie = any(
            value.lower().startswith("omakase_session=")
            for value in response.headers.getlist("set-cookie")
        )
        if refresh and not has_session_cookie:
            from omakase.plus.routes import _set_session_cookie

            _set_session_cookie(response, refresh)
        form_sources = "'self'"
        if request.url.path in {"/plus/auth/google/account", "/plus/auth/google/link"}:
            from omakase.plus.google_auth import google_enabled

            if google_enabled():
                # Chromium checks form-action on a POST's redirect destination,
                # so both the account document and link response need this fixed
                # Google origin. Never derive it from configuration or headers.
                form_sources += " https://accounts.google.com"
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
            "font-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "media-src 'self' blob:; worker-src 'self' blob:; object-src 'none'; "
            f"base-uri 'self'; form-action {form_sources}; frame-ancestors 'none'",
        )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        if not request.url.path.startswith("/plus/static/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if request.url.scheme == "https" or forwarded_proto == "https":
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
    return response


app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# Conditionally mount Plus routes in private mode (workstation/self-hosted app).
if _private_plus_mode():
    from omakase.plus.routes import router as plus_router  # noqa: E402

    app.include_router(plus_router)


@app.get("/docs", include_in_schema=False)
async def docs_redirect():
    """Redirect API explorers to the maintained documentation site."""
    return RedirectResponse(url=_DOCS_SITE, status_code=307)


# ── Request / response models ─────────────────────────────


class RecommendRequest(BaseModel):
    llm_type: str = "ollama"
    llm_url: str = "http://localhost:11434"
    api_key: str | None = ""  # LLM API key
    mal_client_id: str | None = ""  # MAL Client ID (source-specific)
    mal_export_b64: str | None = ""  # base64-encoded MAL XML export (alternative to Client ID)
    model: str = "qwen2.5:7b"
    source: str = "anilist"
    username: str = ""
    profile: str = ""
    pool_size: int = 100
    temperature: float = 0.4
    mode: str = "fast"
    use_planning: bool = False
    skip_profile: bool = False  # broader recs inferred from scoring history alone


class RecommendationOut(BaseModel):
    title: str
    predicted_score: float
    reasoning: str
    best_match_from_history: str
    url: str | None = None
    source: str | None = None


class RecommendResponse(BaseModel):
    source: str
    username: str
    recommendations: list[RecommendationOut]


# ── Routes ────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def index():
    path = _HERE / "templates" / "index.html"
    default_profile = _get_default_profile()
    html = path.read_text(encoding="utf-8")
    html = html.replace(
        "{% if default_profile %}{{ default_profile }}{% endif %}",
        _escape_html(default_profile),
    )
    # Cache-bust the CSS bundle on every deploy so users who had the
    # old template HTML never end up with the old stylesheet — the
    # layout of `.rec-card` switched from <div> to <a>, and stale CSS
    # rendered the new anchor cards as inline links with browser
    # default underlines. Version is derived from style.css mtime at
    # import time (see `_asset_version`).
    html = html.replace("{{ asset_version }}", _ASSET_VERSION)
    return html


@app.get("/favicon.ico")
async def favicon_ico():
    return FileResponse(_HERE / "static" / "favicon.svg", media_type="image/svg+xml")


@app.post("/api/recommend", response_model=RecommendResponse)
async def recommend(req: RecommendRequest):
    export_data: bytes | None = None
    if req.mal_export_b64:
        try:
            export_data = base64.b64decode(req.mal_export_b64, validate=True)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=400,
                detail="Uploaded export couldn't be decoded. Try re-uploading the file.",
            )
        if len(export_data) > _MAX_EXPORT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Export file is too large ({len(export_data) // 1024} KB). "
                    f"Max is {_MAX_EXPORT_BYTES // (1024 * 1024)} MB — "
                    "are you sure you uploaded the right file?"
                ),
            )
        if req.source != "myanimelist":
            raise HTTPException(
                status_code=400,
                detail="Export upload is currently MAL-only. Switch the Source to MyAnimeList.",
            )

    if not export_data and not req.username.strip():
        raise HTTPException(status_code=400, detail="Username is required")
    if not req.profile.strip() and not req.use_planning and not req.skip_profile:
        raise HTTPException(
            status_code=400,
            detail=(
                "Taste profile is required — or enable Plan to Watch, or check "
                "'Skip profile' for broader recs from your scores alone."
            ),
        )

    # Use a request-unique temporary profile. Public BYOK requests must not
    # overwrite another visitor's profile or persist it after completion.
    profile_path_str = ""
    if req.skip_profile and not req.profile.strip():
        pass
    elif req.profile.strip():
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".md",
            encoding="utf-8",
            delete=False,
        ) as profile_file:
            profile_file.write(req.profile)
            profile_path_str = profile_file.name

    llm_url, llm_type, model, supports_json = resolve_model_preset(
        req.llm_url,
        req.llm_type,
        req.model,
        req.mode,
    )
    try:
        llm_url = validate_user_outbound_url(
            llm_url,
            allow_private=_ALLOW_PRIVATE_LLM_URLS,
            allowed_hosts=None
            if _ALLOW_PRIVATE_LLM_URLS
            else {
                urlsplit(DEFAULT_URLS.get(llm_type, "")).hostname or "",
            },
        )
    except OutboundURLError as exc:
        if profile_path_str:
            Path(profile_path_str).unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    cfg = OmakaseConfig(
        source=req.source,
        username=req.username.strip() or "uploaded-list",
        llm_url=llm_url,
        model=model,
        profile_path=profile_path_str,
        candidate_pool_size=req.pool_size,
        temperature=req.temperature,
        llm_type=llm_type,
        mode=req.mode,
        supports_json_mode=supports_json,
        use_planning=req.use_planning,
        export_data=export_data,
        api_key=req.api_key or None,
        mal_client_id=(req.mal_client_id or None) if not export_data else None,
    )

    try:
        recs = run_pipeline(cfg)
    except MALExportError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except EmptyHistoryError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LLMOutputParseError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except httpx.ConnectError:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Couldn't reach the LLM at {req.llm_url}. "
                "If this is a local backend (Ollama / LM Studio), is it running? "
                "Otherwise check the base URL."
            ),
        )
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail=(
                "The LLM took too long to respond. Try the Fast mode, a smaller model, "
                "or a different backend."
            ),
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=_friendly_llm_error(e, req.llm_type, req.model))
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail="Recommendation failed unexpectedly. Try again or contact the operator.",
        ) from e
    finally:
        if profile_path_str:
            Path(profile_path_str).unlink(missing_ok=True)

    return RecommendResponse(
        source=req.source,
        username=req.username,
        recommendations=[
            RecommendationOut(
                title=r.title,
                predicted_score=r.predicted_score,
                reasoning=r.reasoning,
                best_match_from_history=r.best_match_from_history,
                url=r.url,
                source=r.source,
            )
            for r in recs
        ],
    )


@app.get("/api/profile")
async def get_profile():
    """Load taste profile from the default locations."""
    candidates = [
        Path.cwd() / "taste-profile.md",
        Path.home() / ".omakase" / "profile.md",
    ]
    for p in candidates:
        if p.exists():
            return {"profile": p.read_text(encoding="utf-8")}
    raise HTTPException(status_code=404, detail="No taste profile found")


@app.get("/api/sources")
async def get_sources():
    return {"sources": list_sources()}


@app.get("/api/backends")
async def get_backends():
    """Return registered LLM backends with default URLs + presets."""
    presets: dict[str, dict[str, str]] = {}
    for k, v in MODEL_PRESETS.items():
        presets[k] = {"model": v["model"], "supports_json": v["supports_json"]}
    return {
        "backends": list_backends(),
        "default_urls": DEFAULT_URLS,
        "presets": presets,
    }


@app.get("/api/models")
async def discover_models(url: str = "http://localhost:1234"):
    """Discover models from any OpenAI-compatible /v1/models endpoint."""
    try:
        validated = validate_user_outbound_url(
            url,
            allow_private=_ALLOW_PRIVATE_LLM_URLS,
            allowed_hosts=None if _ALLOW_PRIVATE_LLM_URLS else _PUBLIC_LLM_HOSTS,
        )
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{validated}/v1/models")
            resp.raise_for_status()
            data = resp.json()
            models = [m["id"] for m in data.get("data", [])]
            return {"models": sorted(models)}
    except (OutboundURLError, httpx.HTTPError, KeyError, TypeError, ValueError):
        return {"models": []}


# ── Helpers ───────────────────────────────────────────────

_DEFAULT_PROFILE_FALLBACK = """## Things I love
- Morally complex protagonists, not pure-hearted heroes
- Dense world-building over slice-of-life
- Stories that earn their ending — strong third act

## Things I bounce off
- [What do you dislike?]

## Characters that resonate
- [Character] — [why they resonate]

## Recent loves (don't recommend these — just calibration)
- [Title you scored highly]"""


def _get_default_profile() -> str:
    candidates = [
        Path.cwd() / "taste-profile.md",
        Path.home() / ".omakase" / "profile.md",
        _HERE.parent.parent.parent / "examples" / "taste-profile.md",
    ]
    for p in candidates:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return _DEFAULT_PROFILE_FALLBACK


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _friendly_llm_error(e: httpx.HTTPStatusError, llm_type: str, model: str) -> str:
    """Translate a raw LLM HTTP error into a message the visitor can act on."""
    code = e.response.status_code
    body = e.response.text[:300]
    if code in (401, 403):
        return (
            f"{llm_type.title()} rejected your API key. "
            "Double-check the key you pasted, or that it has access to this model."
        )
    if code == 404:
        return (
            f"{llm_type.title()} doesn't recognize the model '{model}'. "
            "Try a different model or switch the Fast/Pro toggle."
        )
    if code == 429:
        return (
            f"{llm_type.title()} rate-limited your key. Wait a minute, "
            "or try a different backend / your own paid key."
        )
    if code >= 500:
        return (
            f"{llm_type.title()} is having issues right now (HTTP {code}). Try again in a moment."
        )
    return f"{llm_type.title()} returned HTTP {code}: {body}"


def run_server(host: str = "127.0.0.1", port: int = 8765):
    """Launch the Omakase web UI server."""
    print(f"  Omakase setup UI -> http://{host}:{port}")
    print("  Press Ctrl+C to stop")
    uvicorn.run(app, host=host, port=port, log_level="info")
