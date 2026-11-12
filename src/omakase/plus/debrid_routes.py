"""Owner-scoped Real-Debrid browser and playback routes."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from dataclasses import replace
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse

from omakase.plus import cloud_progress
from omakase.plus import web_playback_store as store
from omakase.plus.debrid_anime import anime_episodes, resolve_anime
from omakase.plus.debrid_catalog import catalog_title, resolve_catalog
from omakase.plus.debrid_library import (
    DebridLibrary,
    DebridLibraryError,
    account_binding,
    account_token,
    playback_provider,
)
from omakase.plus.debrid_stream import HEADERS, DebridStreams
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_playback_admission import Admission, Busy
from omakase.plus.web_playback_catalog import personal_scope
from omakase.plus.web_security import csrf_token, require_csrf

_COOKIE = "omakase_cloud_media"
_PAGE = 30


def create_debrid_router(templates, context_factory, *, transport=None):
    router = APIRouter(route_class=bounded_form_route(1024, "Invalid cloud playback request"))
    library = DebridLibrary(transport)
    streams = DebridStreams(transport)
    admission = Admission()

    def render(request, user, *, headers=None, **context):
        return templates.TemplateResponse(
            request,
            "debrid_library.html",
            {
                **context_factory(request, user, active_page="cloud"),
                "csrf_token": csrf_token(request),
                **context,
            },
            headers=HEADERS if headers is None else headers,
        )

    def browser_binding(request, user):
        value = f"browser:{user.id}:{request.cookies.get('omakase_session', '')}"
        return hashlib.sha256(value.encode()).hexdigest()

    @router.get("/cloud")
    async def browse(
        request: Request,
        folder: str = Query("", max_length=66),
        q: str = Query("", max_length=120),
        offset: int = Query(0, ge=0, le=20000),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        token = account_token(db, user.id)
        if not token:
            return render(request, user, disconnected=True)
        try:
            if folder:
                rows = await library.files(token, folder)
            else:
                rows = await library.entries(token, query=q.strip())
        except DebridLibraryError as error:
            return render(request, user, folder=folder, query=q, error=str(error))
        if not secrets.compare_digest(token, account_token(db, user.id)):
            raise HTTPException(403, "Real-Debrid connection changed", headers=HEADERS)
        page_rows = rows[offset : offset + _PAGE]
        resumes = cloud_progress.positions(db, user.id, [row.id for row in page_rows])
        return render(
            request,
            user,
            rows=page_rows,
            resumes=resumes,
            folder=folder,
            query=q,
            prev_offset=max(0, offset - _PAGE) if offset else None,
            next_offset=offset + _PAGE if offset + _PAGE < len(rows) else None,
            total=len(rows),
        )

    @router.post("/api/cloud/progress")
    async def save_progress(request: Request, user=Depends(require_user), db=Depends(get_db)):
        form = await request.form()
        require_csrf(request, form.get("csrf_token", ""))
        file_key = form.get("file_key", "")
        title = form.get("title", "")
        try:
            saved = cloud_progress.save_position(
                db,
                user_id=user.id,
                file_key=file_key,
                title=title if isinstance(title, str) else "",
                position=form.get("position", ""),
                duration=form.get("duration", ""),
            )
        except ValueError as error:
            raise HTTPException(422, str(error), headers=HEADERS) from None
        return JSONResponse(saved, headers=HEADERS)

    @router.api_route("/watch/cloud/{file_id}", methods=["GET", "POST"])
    async def play(
        request: Request,
        file_id: str,
        play: bool = Query(False),
        title: str = Query("", max_length=300),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        token = account_token(db, user.id)
        if not token:
            return render(request, user, disconnected=True)
        if request.method == "POST":
            form = await request.form()
            require_csrf(request, form.get("csrf_token", ""))
            target = f"/plus/watch/cloud/{file_id}?play=1"
            if title.strip():
                target += "&title=" + quote(title.strip(), safe="")
            return RedirectResponse(target, status_code=303, headers=HEADERS)
        scope = personal_scope(db, user.id)
        return templates.TemplateResponse(
            request,
            "player.html",
            {
                **context_factory(request, user, active_page="cloud"),
                "csrf_token": csrf_token(request),
                "title": title.strip() or "Cloud file",
                "episode_number": 0,
                "monitor_id": None,
                "library_id": scope.library_id,
                "start_immediately": play,
                "overview_url": "/plus/cloud",
                "subtitle_viewer_id": user.id,
                "cloud_file": file_id,
            },
            headers=HEADERS
            | {
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "require-corp",
            },
        )

    @router.post("/api/playback/cloud-sessions")
    async def cloud_session(request: Request, user=Depends(require_user), db=Depends(get_db)):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        file_key = body.get("file_key") if isinstance(body, dict) else None
        resume = not isinstance(body, dict) or body.get("resume", True) is not False
        if not cloud_progress.valid_key(file_key):
            raise HTTPException(422, "Unknown cloud file", headers=HEADERS)
        token = account_token(db, user.id)
        if not token:
            raise HTTPException(403, "Connect Real-Debrid to play cloud files", headers=HEADERS)
        binding = browser_binding(request, user)
        session_binding = hashlib.sha256(
            request.cookies.get("omakase_session", "").encode()
        ).hexdigest()
        try:
            async with asyncio.timeout(90):
                source = await library.source(token, file_key)
                ttl = min(43200, max(1200, int(source.duration) + 600))
                key, cookie = await streams.issue(source, user.id, binding, ttl)
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except TimeoutError:
            raise HTTPException(
                504, "Real-Debrid took too long to open this file", headers=HEADERS
            ) from None
        scope = personal_scope(db, user.id)
        canonical = str(
            int(hashlib.sha256(("cloud:" + source.identity).encode()).hexdigest()[:17], 16)
            % (10**15)
            + 1
        )
        target = store.ResolvedTarget(
            user_id=user.id,
            library_id=scope.library_id,
            canonical_id=canonical,
            season_number=0,
            episode_number=0,
            media_version=source.media_version,
            duration_seconds=float(source.duration),
            domain="anime-film",
        )
        snapshot = {
            "membership": {"library_id": scope.library_id, "viewer_id": user.id},
            "policy": {"kind": "realdebrid-cloud"},
            "source": {
                "kind": "debrid-cloud",
                "identity": source.identity,
                "media_version": source.media_version,
                "file_key": file_key,
                "title": source.title,
            },
        }
        state = store.create_session(
            db, target=target, binding_digest=session_binding, snapshot=snapshot
        )
        saved = cloud_progress.get_position(db, user.id, file_key) if resume else None
        if saved and saved["position_seconds"] > 0:
            db.execute(
                "UPDATE web_playback_resumes SET position_seconds=? WHERE target_key=?",
                (saved["position_seconds"], store._key(target)),
            )
            db.commit()
            state["position_seconds"] = saved["position_seconds"]
        response = JSONResponse(
            state
            | {
                "media_url": f"/plus/cloud/media/{key}/index.m3u8",
                "media_type": "application/vnd.apple.mpegurl",
                "next_url": None,
                "subtitles": [],
                "fonts": [],
                "track_map": [],
                "skip_segments": [],
                "resume_version_changed": False,
                "preparation_required": False,
                "prepare_url": None,
                "streaming": True,
                "subtitle_url": None,
                "subtitle_track": None,
                "source": {"mode": "realdebrid-cloud"},
            },
            status_code=201,
            headers=HEADERS,
        )
        response.set_cookie(
            _COOKIE,
            cookie,
            max_age=ttl,
            path=f"/plus/cloud/media/{key}",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    def exact_title(db, scope, domain, key, season, episode):
        if domain == "anime" and re.fullmatch(r"[1-9][0-9]{0,17}", key) and 0 <= episode <= 5000:
            if episode in anime_episodes(db, scope, key):
                return db.execute(
                    "SELECT title FROM media_monitors WHERE user_id=? AND id=?",
                    (scope.viewer_id, key),
                ).fetchone()[0]
        elif (
            domain == "movie" and re.fullmatch(r"tt[0-9]{7,10}", key) and season == episode == 0
        ) or (
            domain == "tv"
            and re.fullmatch(r"[1-9][0-9]{0,9}", key)
            and int(key) <= 2_147_483_647
            and 0 <= season <= 200
            and 1 <= episode <= 1000
        ):
            if title := catalog_title(db, scope, domain, key, season, episode):
                return title.title
        return None

    @router.api_route("/cloud/title/{domain}/{key}/{season}/{episode}", methods=["GET", "POST"])
    async def named_play(
        request: Request,
        domain: str,
        key: str,
        season: int,
        episode: int,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        token = account_token(db, user.id)
        if not token:
            return render(request, user, disconnected=True)
        if playback_provider(db, user.id) == "plex":
            raise HTTPException(409, "Your playback source is Plex", headers=HEADERS)
        scope = personal_scope(db, user.id)
        title = exact_title(db, scope, domain, key, season, episode)
        if title is None:
            raise HTTPException(404, "Title unavailable in your personal library", headers=HEADERS)
        if request.method == "GET":
            return render(request, user, named_title=title, play_action=request.url.path)

        async def resolve():
            candidate = (
                await resolve_anime(db, scope, key, episode, library)
                if domain == "anime"
                else await resolve_catalog(db, scope, domain, key, season, episode, library)
            )
            if candidate is None:
                raise DebridLibraryError("No matching Real-Debrid file for this title.", 404)
            return replace(candidate[0].cloud, title=candidate[1][:120])

        return await open_player(
            request,
            db,
            user,
            token,
            resolve,
            file_key=f"named:{domain}:{key}:{season}:{episode}",
        )

    async def open_player(request, db, user, token, resolve, *, file_key=""):
        form = await request.form()
        require_csrf(request, form.get("csrf_token", ""))
        try:
            release = admission.acquire("start", user.id)
        except Busy as error:
            raise HTTPException(
                429,
                "Cloud playback is busy",
                headers=HEADERS | {"Retry-After": str(error.retry_after)},
            ) from None
        try:
            async with asyncio.timeout(60):
                source = await resolve()
                ttl = min(43200, max(1200, int(source.duration) + 600))
                key, cookie = await streams.issue(
                    source, user.id, browser_binding(request, user), ttl
                )
        except (DebridLibraryError, TimeoutError) as error:
            message = (
                str(error)
                if isinstance(error, DebridLibraryError)
                else "Real-Debrid took too long to open this file."
            )
            return render(request, user, error=message)
        finally:
            release()
        if not secrets.compare_digest(token, account_token(db, user.id)):
            streams.passes.pop(key, None)
            raise HTTPException(403, "Real-Debrid connection changed", headers=HEADERS)
        target = f"/plus/cloud/player/{key}"
        if cloud_progress.valid_key(file_key):
            target += "?file=" + quote(file_key, safe="")
        response = RedirectResponse(target, status_code=303, headers=HEADERS)
        response.set_cookie(
            _COOKIE,
            cookie,
            max_age=ttl,
            path=f"/plus/cloud/media/{key}",
            secure=request.url.scheme == "https",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.get("/cloud/player/{key}")
    async def player(request: Request, key: str, user=Depends(require_user), db=Depends(get_db)):
        entry = streams.for_client(key, browser_binding(request, user))
        if entry is None or account_binding(account_token(db, user.id)) != entry.source.account:
            raise HTTPException(404, "Cloud playback expired", headers=HEADERS)
        file_key = request.query_params.get("file", "")
        resume = cloud_progress.get_position(db, user.id, file_key) if file_key else None
        return render(
            request,
            user,
            playing=entry.source.title,
            media_path=f"/plus/cloud/media/{key}/index.m3u8",
            cloud_file_key=file_key if cloud_progress.valid_key(file_key) else "",
            resume_position=resume["position_seconds"] if resume else 0,
            cloud_title=entry.source.title,
        )

    @router.api_route("/cloud/media/{key}/{asset}", methods=["GET", "HEAD"])
    async def media(
        request: Request, key: str, asset: str, user=Depends(require_user), db=Depends(get_db)
    ):
        entry = streams.for_cookie(key, request.cookies.get(_COOKIE, ""))

        def authorize():
            if (
                entry is None
                or streams.for_client(key, browser_binding(request, user)) is not entry
                or account_binding(account_token(db, user.id)) != entry.source.account
            ):
                raise DebridLibraryError("Cloud playback expired", 404)

        try:
            return await streams.deliver(entry, asset, request, authorize)
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None

    return router
