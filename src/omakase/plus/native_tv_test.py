"""Opt-in, one-title Apple TV qualification with read-only playback access."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from omakase.plus import households
from omakase.plus import web_playback_store as store
from omakase.plus.auth import authenticate_session
from omakase.plus.connections import private_connections_enabled
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_playback_admission import Admission, Busy
from omakase.plus.web_playback_catalog import (
    catalog_target,
    personal_scope,
    resolve_catalog_source,
    revalidate_catalog_source,
    revalidate_remote_catalog_source,
)
from omakase.plus.web_playback_delivery import DeliveryError, open_part
from omakase.plus.web_playback_navigation import enabled as playback_enabled
from omakase.plus.web_playback_source import (
    resolve_anime_source,
    revalidate_remote_source,
    revalidate_source,
)
from omakase.plus.web_security import require_csrf

PAGE = "/native-tv-test"
API = "/api/native-tv-test/v1"
COOKIE = "omakase_tv_test_media"
CODE_TTL = 300
PASS_TTL = 1200
NO_STORE = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
ANIME = re.compile(r"/plus/watch/([1-9][0-9]{0,17})/([0-9]{1,4})")
MOVIE = re.compile(r"/plus/watch/title/movie/(tt[0-9]{7,10})")
TV = re.compile(r"/plus/watch/title/tv/([1-9][0-9]{0,9})/([0-9]{1,3})/([1-9][0-9]{0,3})")


def _origin():
    try:
        value = urlsplit(os.getenv("OMAKASE_NATIVE_TV_ORIGIN", ""))
        if (
            value.scheme == "https"
            and value.hostname
            and value.username is None
            and value.password is None
            and value.path in {"", "/"}
            and not value.query
            and not value.fragment
            and (value.port is None or 1 <= value.port <= 65535)
        ):
            return f"https://{value.netloc.lower()}"
    except ValueError:
        pass
    return None


def native_tv_test_enabled():
    return (
        private_connections_enabled()
        and playback_enabled()
        and os.getenv("OMAKASE_NATIVE_TV_TEST", "") == "1"
        and _origin() is not None
    )


def _guard():
    if not native_tv_test_enabled():
        raise HTTPException(404, "Apple TV test unavailable", headers=NO_STORE)


def _https(request):
    # The ASGI server must trust its configured ingress, rather than
    # accepting an arbitrary client-supplied X-Forwarded-Proto header.
    if f"{request.url.scheme}://{request.url.netloc.lower()}" != _origin():
        raise HTTPException(404, "Apple TV test unavailable", headers=NO_STORE)


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


@dataclass(repr=False)
class TestPass:
    source: object
    browser_session: str
    target: store.ResolvedTarget
    title: str
    expires: float
    token_digest: str = field(default="")


def create_native_tv_test_router(
    templates,
    context_factory,
    *,
    resolve_anime=resolve_anime_source,
    local_anime=revalidate_source,
    remote_anime=revalidate_remote_source,
    transport=None,
    clock=time.monotonic,
):
    router = APIRouter(
        route_class=bounded_form_route(2048, "Apple TV test request too large", guard=_guard)
    )
    codes: dict[str, TestPass] = {}
    passes: dict[str, TestPass] = {}
    rejected_codes = deque()
    admission = Admission(clock)

    def prune():
        for registry in (codes, passes):
            for key, grant in tuple(registry.items()):
                if grant.expires <= clock():
                    del registry[key]

    def revoke(actor):
        for registry in (codes, passes):
            for key, grant in tuple(registry.items()):
                if grant.target.user_id == actor:
                    del registry[key]

    def checked(db, grant):
        source, actor = grant.source, grant.target.user_id
        authenticated, _ = authenticate_session(db, grant.browser_session, renew=False)
        if authenticated != actor or grant.expires <= clock():
            raise HTTPException(404, "Apple TV pass expired", headers=NO_STORE)
        try:
            if grant.target.domain in {"movie", "tv"}:
                revalidate_catalog_source(db, source)
            else:
                local_anime(db, source)
        except (ValueError, households.HouseholdError):
            raise HTTPException(
                404, "Apple TV copy no longer available", headers=NO_STORE
            ) from None

    def acquire(kind, actor, media=None):
        try:
            return admission.acquire(kind, actor, media)
        except Busy as error:
            raise HTTPException(
                429, str(error), headers=NO_STORE | {"Retry-After": str(error.retry_after)}
            ) from None

    def page(request, user, **context):
        return templates.TemplateResponse(
            request,
            "native_tv_test.html",
            {**context_factory(request, user, active_page="library"), **context},
            headers=NO_STORE,
        )

    async def exact_source(db, actor, path, library):
        scope = personal_scope(db, actor)
        if library and library != scope.library_id:
            raise ValueError("Personal titles only")
        if match := ANIME.fullmatch(path):
            episode = int(match[2])
            monitor = db.execute(
                "SELECT * FROM media_monitors WHERE id=? AND user_id=?", (int(match[1]), actor)
            ).fetchone()
            if not monitor or episode > 5000 or (episode == 0) != (monitor["format"] == "MOVIE"):
                raise ValueError("Episode unavailable")
            source = await resolve_anime(
                db,
                actor_user_id=actor,
                library_id=scope.library_id,
                anilist_id=monitor["anilist_id"],
                season_number=monitor["season_number"] or 0,
                episode_number=episode,
                media_version=None,
                transport=transport,
            )
            return source, monitor["title"]
        if match := MOVIE.fullmatch(path):
            domain, canonical, season, episode = "movie", match[1], 0, 0
        elif match := TV.fullmatch(path):
            domain, canonical, season, episode = "tv", match[1], int(match[2]), int(match[3])
            if int(canonical) > 2_147_483_647 or season > 200 or episode > 1000:
                raise ValueError("Episode unavailable")
        else:
            raise ValueError("Unsupported watch page")
        catalog, _ = catalog_target(db, scope, domain, canonical, season, episode)
        source = await resolve_catalog_source(
            db,
            scope=scope,
            domain=domain,
            canonical_id=canonical,
            season=season,
            episode=episode,
            media_version=None,
            transport=transport,
        )
        return source, catalog["title"]

    @router.post(PAGE)
    async def issue(request: Request, user=Depends(require_user), db=Depends(get_db)):
        _https(request)
        form = await request.form()
        require_csrf(request, str(form.get("csrf_token", "")))
        path, library = str(form.get("watch_path", "")), str(form.get("library_id", ""))
        release = acquire("start", user.id)
        try:
            async with asyncio.timeout(12):
                source, title = await exact_source(db, user.id, path, library)
            if (source.container, source.video_codec, source.audio_codec) != ("mp4", "h264", "aac"):
                return page(
                    request, user, error="For this test choose a title with an H.264/AAC MP4 copy."
                )
            target = store.ResolvedTarget(
                user.id,
                source._scope.library_id,
                getattr(source, "canonical_id", str(source.anilist_id)),
                source.season_number,
                source.episode_number,
                source.media_version,
                source.duration_ms / 1000,
                domain=getattr(source, "domain", "anime"),
            )
            grant = TestPass(
                source,
                request.cookies["omakase_session"],
                target,
                str(title)[:120],
                clock() + CODE_TTL,
            )
            checked(db, grant)
        except (ValueError, TimeoutError, households.HouseholdError):
            return page(
                request,
                user,
                error="That exact copy could not be opened from your personal library.",
            )
        finally:
            release()
        prune()
        revoke(user.id)
        if len(codes) + len(passes) >= 32:
            raise HTTPException(429, "Try again when another test pass expires", headers=NO_STORE)
        code = secrets.token_hex(10).upper()
        codes[_digest(code)] = grant
        return page(request, user, code=code, test_title=grant.title, server=_origin())

    @router.post(PAGE + "/revoke")
    async def revoke_browser(request: Request, user=Depends(require_user)):
        _https(request)
        form = await request.form()
        require_csrf(request, str(form.get("csrf_token", "")))
        revoke(user.id)
        return page(request, user, revoked=True)

    @router.post(API + "/redeem")
    async def redeem(request: Request, db=Depends(get_db)):
        _https(request)
        now = clock()
        while rejected_codes and rejected_codes[0] <= now - 60:
            rejected_codes.popleft()
        if len(rejected_codes) >= 12:
            raise HTTPException(429, "Try again shortly", headers=NO_STORE | {"Retry-After": "60"})
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        code = body.get("code") if isinstance(body, dict) and set(body) == {"code"} else None
        prune()
        grant = (
            codes.pop(_digest(code), None)
            if isinstance(code, str) and re.fullmatch(r"[0-9A-F]{20}", code)
            else None
        )
        if grant is None:
            rejected_codes.append(now)
            raise HTTPException(404, "Apple TV code unavailable", headers=NO_STORE)
        checked(db, grant)
        token, media_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        grant.expires, grant.token_digest = now + PASS_TTL, _digest(token)
        passes[media_id] = grant
        target = grant.target
        previous = store.get_resume(db, target=target)
        position = previous["position_seconds"] if previous else 0
        return JSONResponse(
            {
                "title": grant.title,
                "media_id": media_id,
                "media_token": token,
                "expires_in": PASS_TTL,
                "duration_seconds": target.duration_seconds,
                "position_seconds": position if 0 < position < target.duration_seconds else 0,
                "identity": {
                    "library_id": target.library_id,
                    "domain": target.domain,
                    "canonical_id": target.canonical_id,
                    "season_number": target.season_number,
                    "episode_number": target.episode_number,
                    "media_version": target.media_version,
                },
            },
            headers=NO_STORE,
        )

    @router.api_route(API + "/media/{media_id}", methods=["GET", "HEAD"])
    async def media(request: Request, media_id: str, db=Depends(get_db)):
        _https(request)
        prune()
        grant = passes.get(media_id)
        token = request.cookies.get(COOKIE, "")
        if (
            grant is None
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
            or not secrets.compare_digest(grant.token_digest, _digest(token))
        ):
            raise HTTPException(404, "Apple TV media unavailable", headers=NO_STORE)
        checked(db, grant)
        release = acquire("read", grant.target.user_id, media_id)

        async def authorize():
            _guard()
            if passes.get(media_id) is not grant:
                raise DeliveryError("Apple TV pass revoked", 404)
            checked(db, grant)
            try:
                if grant.target.domain in {"movie", "tv"}:
                    await revalidate_remote_catalog_source(db, grant.source, transport)
                else:
                    await remote_anime(db, grant.source, transport=transport)
            except ValueError:
                raise DeliveryError("Apple TV copy no longer available", 404) from None
            _guard()
            if passes.get(media_id) is not grant:
                raise DeliveryError("Apple TV pass revoked", 404)
            checked(db, grant)

        try:
            return await open_part(
                grant.source, request, authorize=authorize, transport=transport, on_close=release
            )
        except DeliveryError as error:
            release()
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        except BaseException:
            release()
            raise

    return router
