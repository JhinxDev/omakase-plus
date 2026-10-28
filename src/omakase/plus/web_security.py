"""Browser security helpers shared by every authenticated Plus route."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from fastapi import HTTPException, Request

_CSRF_FALLBACK_KEY = secrets.token_bytes(32)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def csrf_token(request: Request) -> str:
    """Return a session-bound CSRF token without exposing the session itself."""
    session_id = request.cookies.get("omakase_session", "")
    if not session_id:
        return ""
    configured = (
        os.getenv("OMAKASE_CSRF_KEY", "") or os.getenv("OMAKASE_PLUS_MASTER_KEY", "")
    ).encode("utf-8")
    key = configured or _CSRF_FALLBACK_KEY
    return hmac.new(key, session_id.encode("utf-8"), hashlib.sha256).hexdigest()


def require_csrf(request: Request, supplied: str) -> None:
    """Reject a missing or invalid session-bound CSRF token."""
    expected = csrf_token(request)
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


async def enforce_browser_csrf(request: Request) -> None:
    """Protect authenticated browser mutations at the shared dependency boundary.

    HTML forms submit ``csrf_token`` while JSON calls use ``X-CSRF-Token``.
    Header-less non-browser clients remain usable because they cannot initiate a
    browser CSRF attack; any request carrying browser fetch metadata must prove
    the session-bound token.
    """
    if request.method.upper() in _SAFE_METHODS:
        return

    supplied = request.headers.get("x-csrf-token", "")
    content_type = request.headers.get("content-type", "").lower()
    if not supplied and (
        content_type.startswith("application/x-www-form-urlencoded")
        or content_type.startswith("multipart/form-data")
    ):
        form = await request.form()
        raw = form.get("csrf_token", "")
        supplied = raw if isinstance(raw, str) else ""

    if supplied:
        require_csrf(request, supplied)
        return

    # Browser-originating unsafe requests expose at least one of these headers
    # in current engines. Their absence is reserved for API/CLI clients.
    if request.headers.get("origin") or request.headers.get("sec-fetch-site"):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
