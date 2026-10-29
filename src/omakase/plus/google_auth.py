"""Optional Google OpenID Connect for explicitly linked private Plus accounts.

No Google access/refresh tokens or email addresses are retained. Only verified
Google subject IDs can authenticate; matching email never grants account access.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from omakase.plus.auth import create_session, delete_session, validate_session
from omakase.plus.deps import get_db
from omakase.plus.middleware import get_current_user
from omakase.plus.web_security import csrf_token, require_csrf

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
FLOW_COOKIE = "omakase_google_flow"
FLOW_PATH = "/plus/auth/google"
FLOW_TTL = 300
MAX_PROVIDER_BYTES = 65536
_starts: dict[str, list[float]] = {}


class GoogleAuthError(ValueError):
    """Safe, non-provider-derived authentication failure."""


@dataclass(frozen=True)
class GoogleConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def binding(self) -> str:
        return _hash(self.client_id + "\n" + self.redirect_uri)


def google_config() -> GoogleConfig | None:
    """Read configuration through normal runtime authority, never expose it."""
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip()
    secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    uri = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
    try:
        parsed = urlsplit(uri)
        parsed.port
    except ValueError:
        return None
    if (
        not client_id
        or not secret
        or len(client_id) > 512
        or len(secret) > 4096
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != FLOW_PATH + "/callback"
        or len(uri) > 2048
    ):
        return None
    return GoogleConfig(client_id, secret, uri)


def google_enabled() -> bool:
    return google_config() is not None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 16384:
        raise GoogleAuthError("Invalid Google identity response")
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise GoogleAuthError("Invalid Google identity response") from exc


def verify_id_token(
    token: str, jwks: dict, config: GoogleConfig, nonce: str, *, now: int | None = None
) -> str:
    """Verify RS256 against fixed Google JWKs and every identity-binding claim."""
    current = int(time.time()) if now is None else now
    try:
        if not isinstance(token, str) or len(token) > 16384:
            raise ValueError
        header_part, claims_part, signature_part = token.split(".")
        header = json.loads(_decode(header_part))
        claims = json.loads(_decode(claims_part))
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise ValueError
        if (
            header.get("alg") != "RS256"
            or not isinstance(header.get("kid"), str)
            or header.get("crit")
        ):
            raise ValueError
        keys = jwks.get("keys", [])
        if not isinstance(keys, list) or not 1 <= len(keys) <= 10:
            raise ValueError
        matches = [key for key in keys if isinstance(key, dict) and key.get("kid") == header["kid"]]
        if len(matches) != 1:
            raise ValueError
        key = matches[0]
        if (
            key.get("kty") != "RSA"
            or key.get("alg", "RS256") != "RS256"
            or key.get("use", "sig") != "sig"
        ):
            raise ValueError
        modulus = _decode(key["n"])
        exponent = _decode(key["e"])
        if not 256 <= len(modulus) <= 512 or len(exponent) > 4:
            raise ValueError
        public_key = rsa.RSAPublicNumbers(
            int.from_bytes(exponent, "big"), int.from_bytes(modulus, "big")
        ).public_key()
        public_key.verify(
            _decode(signature_part),
            f"{header_part}.{claims_part}".encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        audience = claims.get("aud")
        if not (
            audience == config.client_id
            or (isinstance(audience, list) and config.client_id in audience)
        ):
            raise ValueError
        if "azp" in claims and claims["azp"] != config.client_id:
            raise ValueError
        if (
            isinstance(audience, list)
            and len(audience) > 1
            and claims.get("azp") != config.client_id
        ):
            raise ValueError
        if claims.get("iss") not in {"https://accounts.google.com", "accounts.google.com"}:
            raise ValueError
        for field in ("exp", "iat"):
            if type(claims.get(field)) is not int:
                raise ValueError
        if (
            claims["exp"] <= current
            or claims["iat"] > current + 60
            or claims["iat"] < current - 3600
        ):
            raise ValueError
        if claims["exp"] <= claims["iat"]:
            raise ValueError
        if not isinstance(claims.get("nonce"), str) or not secrets.compare_digest(
            claims["nonce"], nonce
        ):
            raise ValueError
        subject = claims.get("sub")
        if not isinstance(subject, str) or not 1 <= len(subject) <= 255 or not subject.isascii():
            raise ValueError
        return subject
    except (ValueError, KeyError, TypeError, InvalidSignature, AttributeError) as exc:
        raise GoogleAuthError("Google identity could not be verified") from exc


async def _provider_json(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    async with client.stream(method, url, **kwargs) as response:
        if response.status_code != 200:
            raise GoogleAuthError("Google sign-in could not be completed")
        data = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=8192):
            data.extend(chunk)
            if len(data) > MAX_PROVIDER_BYTES:
                raise GoogleAuthError("Google identity response is too large")
    try:
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise ValueError
        return payload
    except (ValueError, TypeError) as exc:
        raise GoogleAuthError("Invalid Google identity response") from exc


async def exchange_identity(config: GoogleConfig, code: str, flow) -> str:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(10), follow_redirects=False, trust_env=False
    ) as client:
        tokens = await _provider_json(
            client,
            "POST",
            TOKEN_URL,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": config.redirect_uri,
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": flow["code_verifier"],
            },
        )
        jwks = await _provider_json(client, "GET", JWKS_URL)
        return verify_id_token(tokens.get("id_token"), jwks, config, flow["nonce"])


def create_flow(
    db,
    config: GoogleConfig,
    *,
    purpose: str,
    browser: str,
    user_id: int | None = None,
    session: str = "",
    now: int | None = None,
) -> tuple[str, str, str]:
    current = int(time.time()) if now is None else now
    if (
        purpose not in {"login", "link"}
        or not browser
        or (purpose == "link" and (not user_id or not session))
    ):
        raise GoogleAuthError("Invalid Google sign-in flow")
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    with db:
        db.execute(
            "DELETE FROM google_login_flows WHERE expires_at <= ? OR browser_hash = ?",
            (current, _hash(browser)),
        )
        if db.execute("SELECT count(*) FROM google_login_flows").fetchone()[0] >= 256:
            raise GoogleAuthError("Google sign-in is busy. Try again shortly")
        db.execute(
            """INSERT INTO google_login_flows
            (state_hash,browser_hash,nonce,code_verifier,config_hash,purpose,user_id,session_hash,expires_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                _hash(state),
                _hash(browser),
                nonce,
                verifier,
                config.binding,
                purpose,
                user_id if purpose == "link" else None,
                _hash(session) if purpose == "link" else "",
                current + FLOW_TTL,
            ),
        )
    return state, nonce, _b64(hashlib.sha256(verifier.encode()).digest())


def consume_flow(
    db,
    config: GoogleConfig,
    *,
    state: str,
    browser: str,
    session: str = "",
    user_id: int | None = None,
    now: int | None = None,
):
    current = int(time.time()) if now is None else now
    if not state or not browser or len(state) > 128 or len(browser) > 128:
        return None
    with db:
        row = db.execute(
            "SELECT * FROM google_login_flows WHERE state_hash=? AND browser_hash=?",
            (_hash(state), _hash(browser)),
        ).fetchone()
        if row is None:
            return None
        removed = db.execute("DELETE FROM google_login_flows WHERE state_hash=?", (_hash(state),))
        if removed.rowcount != 1:
            return None
    if row["expires_at"] <= current or row["config_hash"] != config.binding:
        return None
    if row["purpose"] == "link" and (
        row["user_id"] != user_id or not session or row["session_hash"] != _hash(session)
    ):
        return None
    return row


def link_identity(db, user_id: int, subject: str) -> None:
    """No replacement or cross-account reassignment, including same-email users."""
    try:
        with db:
            existing = db.execute(
                "SELECT user_id FROM google_identities WHERE google_sub=?", (subject,)
            ).fetchone()
            if existing and existing["user_id"] == user_id:
                return
            db.execute(
                "INSERT INTO google_identities(google_sub,user_id) VALUES (?,?)", (subject, user_id)
            )
    except sqlite3.IntegrityError as exc:
        raise GoogleAuthError("This Google account or private account is already linked") from exc


def _private(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _redirect(url: str):
    return _private(RedirectResponse(url, status_code=303))


async def _form_csrf(request: Request) -> None:
    if (
        not request.headers.get("content-type", "")
        .lower()
        .startswith("application/x-www-form-urlencoded")
    ):
        raise HTTPException(415, "Expected an account form")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 4096:
            raise HTTPException(413, "Account form is too large")
    try:
        fields = parse_qs(data.decode("utf-8"), max_num_fields=4, strict_parsing=True)
        tokens = fields.get("csrf_token", [])
        if len(tokens) != 1:
            raise ValueError
    except (ValueError, UnicodeError):
        raise HTTPException(403, "Invalid CSRF token") from None
    require_csrf(request, tokens[0])


def create_google_router(templates, set_session_cookie, context_factory=None) -> APIRouter:
    """Include once in routes.router; reuse its template globals/session policy."""
    router = APIRouter(prefix="/auth/google")

    async def owner(request, db):
        user = await get_current_user(request, db)
        if user is None:
            raise HTTPException(401, "Sign in with your password first")
        return user

    def start(request, db, config, purpose, user_id=None):
        address = _hash(request.client.host if request.client else "unknown")
        tick = time.monotonic()
        for key in list(_starts):
            _starts[key] = [stamp for stamp in _starts[key] if stamp > tick - 60]
            if not _starts[key]:
                del _starts[key]
        if len(_starts.get(address, [])) >= 5 or (address not in _starts and len(_starts) >= 256):
            raise HTTPException(429, "Too many Google sign-in attempts. Try again shortly")
        _starts.setdefault(address, []).append(tick)
        browser = request.cookies.get(FLOW_COOKIE, "")
        if len(browser) != 43:
            browser = secrets.token_urlsafe(32)
        try:
            state, nonce, challenge = create_flow(
                db,
                config,
                purpose=purpose,
                browser=browser,
                user_id=user_id,
                session=request.cookies.get("omakase_session", ""),
            )
        except GoogleAuthError:
            raise HTTPException(503, "Google sign-in is busy. Try again shortly") from None
        response = _redirect(
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "client_id": config.client_id,
                    "redirect_uri": config.redirect_uri,
                    "response_type": "code",
                    "scope": "openid",
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "select_account",
                }
            )
        )
        response.set_cookie(
            FLOW_COOKIE,
            browser,
            max_age=FLOW_TTL,
            path=FLOW_PATH,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.get("/login")
    async def login(request: Request, db=Depends(get_db)):
        if await get_current_user(request, db):
            return _redirect("/plus/home")
        config = google_config()
        if config is None:
            return _redirect(
                "/plus/login?error=Google+sign-in+is+not+configured.+Use+your+password."
            )
        return start(request, db, config, "login")

    @router.get("/account")
    async def account(request: Request, db=Depends(get_db)):
        user = await owner(request, db)
        linked = (
            db.execute("SELECT 1 FROM google_identities WHERE user_id=?", (user.id,)).fetchone()
            is not None
        )
        messages = {
            "linked": "Google sign-in is linked.",
            "unlinked": "Google sign-in was removed. Your password still works.",
            "failed": "Google sign-in could not be completed. Try again from this page.",
        }
        return _private(
            templates.TemplateResponse(
                request,
                "google_account.html",
                {
                    **(
                        context_factory(request, user, active_page="settings")
                        if context_factory
                        else {}
                    ),
                    "email": user.email,
                    "active_page": "settings",
                    "csrf_token": csrf_token(request),
                    "google_enabled": google_enabled(),
                    "google_linked": linked,
                    "message": messages.get(request.query_params.get("result", ""), ""),
                },
            )
        )

    @router.post("/link")
    async def link(request: Request, db=Depends(get_db)):
        user = await owner(request, db)
        await _form_csrf(request)
        config = google_config()
        if config is None:
            raise HTTPException(503, "Google sign-in is not configured")
        return start(request, db, config, "link", user.id)

    @router.post("/unlink")
    async def unlink(request: Request, db=Depends(get_db)):
        user = await owner(request, db)
        await _form_csrf(request)
        with db:
            db.execute("DELETE FROM google_identities WHERE user_id=?", (user.id,))
            db.execute("DELETE FROM google_login_flows WHERE user_id=?", (user.id,))
        return _redirect(FLOW_PATH + "/account?result=unlinked")

    @router.get("/callback")
    async def callback(request: Request, db=Depends(get_db)):
        config = google_config()
        if config is None:
            return _redirect(
                "/plus/login?error=Google+sign-in+is+not+configured.+Use+your+password."
            )
        query = request.query_params
        if len(request.scope.get("query_string", b"")) > 8192 or any(
            len(query.getlist(key)) > 1 for key in ("state", "code", "error")
        ):
            raise HTTPException(400, "Invalid Google callback")
        session = request.cookies.get("omakase_session", "")
        user_id = validate_session(db, session) if session else None
        flow = consume_flow(
            db,
            config,
            state=query.get("state", ""),
            browser=request.cookies.get(FLOW_COOKIE, ""),
            session=session,
            user_id=user_id,
        )
        target = (
            FLOW_PATH + "/account?result=failed"
            if flow is not None and flow["purpose"] == "link"
            else "/plus/login?error=Google+sign-in+expired+or+could+not+be+verified.+Try+again."
        )
        code = query.get("code", "")
        if flow is not None and not query.get("error") and 1 <= len(code) <= 4096:
            try:
                subject = await asyncio.wait_for(exchange_identity(config, code, flow), timeout=20)
                if flow["expires_at"] <= int(time.time()):
                    raise GoogleAuthError("Google sign-in expired")
                if flow["purpose"] == "link":
                    # Recheck ownership after network I/O, in case logout/revocation occurred.
                    if validate_session(db, session) != user_id:
                        raise GoogleAuthError("Account session expired")
                    link_identity(db, user_id, subject)
                    target = FLOW_PATH + "/account?result=linked"
                else:
                    row = db.execute(
                        "SELECT user_id FROM google_identities WHERE google_sub=?", (subject,)
                    ).fetchone()
                    if row is None:
                        target = "/plus/login?error=Log+in+with+your+password+once,+then+link+Google+in+Account+settings."
                    else:
                        new_session = create_session(db, row["user_id"])
                        if session:
                            delete_session(db, session)
                        response = _redirect("/plus/home")
                        set_session_cookie(response, new_session)
                        response.delete_cookie(
                            FLOW_COOKIE, path=FLOW_PATH, secure=True, httponly=True, samesite="lax"
                        )
                        return response
            except (GoogleAuthError, httpx.HTTPError, asyncio.TimeoutError):
                pass
        response = _redirect(target)
        response.delete_cookie(
            FLOW_COOKIE, path=FLOW_PATH, secure=True, httponly=True, samesite="lax"
        )
        return response

    return router
