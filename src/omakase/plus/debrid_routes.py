"""Owner-scoped Real-Debrid browser and playback routes."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
)
from omakase.plus.debrid_stream import HEADERS, DebridStreams, subtitle_cache_key
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.native_hls import NativeError
from omakase.plus.providers import playback as playback_providers
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_playback_admission import Admission, Busy
from omakase.plus.web_playback_catalog import personal_scope
from omakase.plus.web_playback_codecs import hls_audio_codec, hls_init_metadata
from omakase.plus.web_playback_context import episode_presentation, watch_context
from omakase.plus.web_security import csrf_token, require_csrf

_COOKIE = "omakase_cloud_media"
_PAGE = 30


def create_debrid_router(
    templates, context_factory, *, transport=None, subtitle_root=None, streams=None
):
    router = APIRouter(route_class=bounded_form_route(1024, "Invalid cloud playback request"))
    library = DebridLibrary(transport)
    streams = streams or DebridStreams(
        transport, subtitle_root=subtitle_root, subtitle_embedded_only=True
    )
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

    def watch_page(request, user, scope, file_key, title, play, overview_url, **context):
        return templates.TemplateResponse(
            request,
            "player.html",
            {
                **context_factory(request, user, active_page="cloud"),
                "csrf_token": csrf_token(request),
                "title": title,
                "episode_number": 0,
                "monitor_id": None,
                "library_id": scope.library_id,
                "start_immediately": play,
                "overview_url": overview_url,
                "subtitle_viewer_id": user.id,
                "cloud_file": file_key,
                **context,
            },
            headers=HEADERS
            | {
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "require-corp",
            },
        )

    def reserve_start(user_id):
        try:
            return admission.acquire("start", user_id)
        except Busy as error:
            raise HTTPException(
                429,
                "Cloud playback is busy",
                headers=HEADERS | {"Retry-After": str(error.retry_after)},
            ) from None

    async def issue_browser_file(source, user_id, binding, ttl, body, position):
        raw_codecs = body.get("codecs")
        if not isinstance(raw_codecs, list) or len(raw_codecs) > 12:
            return None
        codecs = {codec for codec in raw_codecs if isinstance(codec, str)}
        supported_video = codecs.intersection({"h264", "hevc"})
        if not supported_video or "aac" not in codecs:
            return None
        plan = streams.native.start(
            secrets.token_hex(16), source.direct, source.duration, warm=position
        )
        if plan is None:
            return None
        issued = False
        try:
            timeline = await streams.native.ready(plan, timeout=10)
            if timeline is None or timeline.video_codec not in supported_video:
                return None
            if any(audio.copyable and audio.codec != "aac" for audio in timeline.audios):
                return None
            opening = streams.native._position_index(timeline, position or 0)
            await streams.native.ensure(plan, opening, timeout=10)
            video = hls_init_metadata(plan.root, "init.mp4").codecs
            audio = {hls_audio_codec(plan.root, track.init) for track in timeline.audios}
            if not video or None in audio or len(audio) > 1:
                return None
            plan.web_codecs = ",".join([video, *sorted(audio)])
            plan.opening_prepared = True
            captions = streams.subtitles.start(
                subtitle_cache_key(source), source.direct, source.duration
            )
            key, cookie = await streams.issue(
                source,
                user_id,
                binding,
                ttl,
                native=plan,
                subtitle_key=captions.key if captions else "",
            )
            issued = True
            return key, cookie
        except NativeError:
            return None
        finally:
            if not issued:
                streams.native.close(plan.key)

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
        return watch_page(
            request, user, scope, file_id, title.strip() or "Cloud file", play, "/plus/cloud"
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
        scope = personal_scope(db, user.id)
        named = None
        if file_key.startswith("named:"):
            _, domain, canonical_key, season, episode = file_key.split(":")
            named = (domain, canonical_key, int(season), int(episode))
            verify_named_target(db, user, scope, *named)
        binding = browser_binding(request, user)
        session_binding = hashlib.sha256(
            request.cookies.get("omakase_session", "").encode()
        ).hexdigest()
        saved = cloud_progress.get_position(db, user.id, file_key) if resume else None
        release = reserve_start(user.id)
        want_on_demand = (
            bool(named)
            and isinstance(body, dict)
            and body.get("prefer_on_demand") is not False
            and isinstance(body.get("codecs"), list)
        )
        try:
            async with asyncio.timeout(90):
                source = (
                    await resolve_named(db, scope, *named, need_transcode=not want_on_demand)
                    if named
                    else await library.source(token, file_key)
                )
                ttl = min(43200, max(1200, int(source.duration) + 600))
                position = saved["position_seconds"] if saved else 0
                direct = (
                    await issue_browser_file(source, user.id, binding, ttl, body, position)
                    if named
                    and body.get("prefer_on_demand") is not False
                    and 0 <= position < source.duration
                    else None
                )
                if direct is None and not source.playlist:
                    source = await library.resolve_playlists(token, source)
                key, cookie = direct or await streams.issue(source, user.id, binding, ttl)
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None
        except TimeoutError:
            raise HTTPException(
                504, "Real-Debrid took too long to open this file", headers=HEADERS
            ) from None
        finally:
            release()
        if not secrets.compare_digest(token, account_token(db, user.id)):
            streams.drop_pass(key)
            raise HTTPException(403, "Real-Debrid connection changed", headers=HEADERS)
        canonical = str(
            int(hashlib.sha256(("cloud:" + source.identity).encode()).hexdigest()[:17], 16)
            % (10**15)
            + 1
        )
        catalog = named if named and named[0] in {"movie", "tv"} else None
        target = store.ResolvedTarget(
            user_id=user.id,
            library_id=scope.library_id,
            canonical_id=catalog[1] if catalog else canonical,
            season_number=catalog[2] if catalog else 0,
            episode_number=catalog[3] if catalog else 0,
            media_version=source.media_version,
            duration_seconds=float(source.duration),
            domain=catalog[0] if catalog else "anime-film",
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
                "media_pass": key,
            },
        }
        try:
            state = store.create_session(
                db, target=target, binding_digest=session_binding, snapshot=snapshot
            )
        except Exception:
            streams.drop_pass(key)
            raise
        if saved and saved["position_seconds"] > 0:
            db.execute(
                "UPDATE web_playback_resumes SET position_seconds=? WHERE target_key=?",
                (saved["position_seconds"], store._key(target)),
            )
            db.commit()
            state["position_seconds"] = saved["position_seconds"]
        elif not resume:
            state["position_seconds"] = 0
        entry = streams.for_client(key, binding)
        if entry and state["position_seconds"] > 0:
            streams.warm_position(entry, state["position_seconds"])
        subtitle_url = (
            f"/plus/cloud/media/{key}/subtitles.json" if entry and entry.subtitle_key else None
        )
        plan = streams.native.peek(entry.native_key) if direct and entry else None
        bitmap_tracks = [
            {
                "id": track.ordinal,
                "name": track.name,
                "language": track.language,
                "url": f"/plus/cloud/media/{key}/bitmap{track.ordinal}.json",
            }
            for track in getattr(plan.timeline if plan else None, "bitmap_subtitles", ())
        ]
        # Use the unadorned media playlist. Captions are discovered separately
        # so a slow or unsupported text stream cannot hold the first frame.
        manifest = "index.m3u8" if direct or not subtitle_url else "stream.m3u8"
        response = JSONResponse(
            state
            | {
                "media_url": f"/plus/cloud/media/{key}/{manifest}",
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
                "cloud_subtitles_url": subtitle_url,
                "bitmap_subtitles": bitmap_tracks,
                "source": {
                    "mode": "realdebrid-cloud",
                    "delivery": "fragmented" if direct else "provider",
                },
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
        if (
            domain == "anime"
            and season == 0
            and re.fullmatch(r"[1-9][0-9]{0,17}", key)
            and 0 <= episode <= 5000
        ):
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

    def verify_named_target(db, user, scope, domain, key, season, episode):
        selection = playback_providers.select(db, user.id)
        if not selection.cloud_route:
            raise HTTPException(409, f"Your playback source is {selection.label}", headers=HEADERS)
        title = exact_title(db, scope, domain, key, season, episode)
        if title is None:
            raise HTTPException(404, "Title unavailable in your personal library", headers=HEADERS)
        return title

    async def resolve_named(
        db, scope, domain, key, season, episode, *, need_transcode: bool = True
    ):
        candidate = (
            await resolve_anime(db, scope, key, episode, library, need_transcode=need_transcode)
            if domain == "anime"
            else await resolve_catalog(
                db, scope, domain, key, season, episode, library, need_transcode=need_transcode
            )
        )
        if candidate is None:
            raise DebridLibraryError("No matching Real-Debrid file for this title.", 404)
        return replace(candidate[0].cloud, title=candidate[1][:120])

    def named_watch_context(db, scope, domain, key, season, episode):
        if domain == "anime":
            monitor = db.execute(
                "SELECT * FROM media_monitors WHERE user_id=? AND id=?", (scope.viewer_id, key)
            ).fetchone()
            return {
                "monitor_id": int(key),
                **watch_context(
                    db, monitor, scope, episode, candidates=anime_episodes(db, scope, key)
                ),
            }
        if domain == "movie":
            row = db.execute(
                "SELECT overview FROM movie_catalog WHERE user_id=? AND imdb_id=?",
                (scope.viewer_id, key),
            ).fetchone()
            return {
                "catalog_target": {"domain": "movie", "imdb_id": key},
                "description_preview": row["overview"] if row else None,
            }
        data = db.execute(
            """SELECT e.metadata_json, c.metadata_json FROM tv_episodes e
               JOIN tv_catalog c USING(user_id, tmdb_id)
               WHERE e.user_id=? AND e.tmdb_id=? AND e.season_number=? AND e.episode_number=?""",
            (scope.viewer_id, key, season, episode),
        ).fetchone()
        episode_data, series_data = map(json.loads, data) if data else ({}, {})
        navigation = {}
        for name, number in (("previous_url", episode - 1), ("next_url", episode + 1)):
            if number > 0 and catalog_title(db, scope, domain, key, season, number):
                navigation[name] = f"/plus/watch/title/tv/{key}/{season}/{number}?play=1"
        return {
            "season_number": season,
            "catalog_target": {
                "domain": "tv",
                "tmdb_id": int(key),
                "season_number": season,
                "episode_number": episode,
            },
            "description_preview": series_data.get("overview"),
            **episode_presentation(episode_data.get("title"), episode_data.get("overview")),
            **navigation,
        }

    @router.get("/cloud/title/{domain}/{key}/{season}/{episode}")
    async def named_play(
        request: Request,
        domain: str,
        key: str,
        season: int,
        episode: int,
        play: bool = Query(False),
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        token = account_token(db, user.id)
        if not token:
            return render(request, user, disconnected=True)
        scope = personal_scope(db, user.id)
        title = verify_named_target(db, user, scope, domain, key, season, episode)
        file_key = f"named:{domain}:{key}:{season}:{episode}"
        if domain == "anime":
            overview = f"/plus/library/{key}?library_id={scope.library_id}"
            native_path = f"/plus/watch/{key}/{episode}"
        elif domain == "movie":
            overview = f"/plus/movies/{key}"
            native_path = f"/plus/watch/title/movie/{key}"
        else:
            overview = f"/plus/tv/{key}?season={season}#tv-seasons"
            native_path = f"/plus/watch/title/tv/{key}/{season}/{episode}"
        return watch_page(
            request,
            user,
            scope,
            file_key,
            title,
            play,
            overview,
            episode_number=episode,
            player_native_watch_path=native_path,
            **named_watch_context(db, scope, domain, key, season, episode),
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
            if request.method == "GET" and (
                match := re.fullmatch(r"bitmap([0-9]{1,3})\.json", asset)
            ):
                authorize()
                position = request.query_params.get("position", "")
                if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,6})?", position):
                    raise DebridLibraryError("Invalid subtitle position.", 422)
                bitmap = await streams.deliver_bitmap(
                    entry, int(match[1]), float(position), request
                )
                authorize()
                return JSONResponse(bitmap, headers=HEADERS)
            if asset == "subtitles.json" and request.method == "GET":
                authorize()
                job = streams.subtitles.get(entry.subtitle_key)
                if job is not None and not job.done.is_set():
                    return JSONResponse(
                        {"tracks": []}, status_code=202, headers=HEADERS | {"Retry-After": "2"}
                    )
                tracks = (
                    [
                        {
                            "name": track.name,
                            "language": track.language,
                            "url": f"/plus/cloud/media/{key}/subtitles{index}.vtt",
                        }
                        for index, track in enumerate(job.tracks)
                        if track.data
                    ]
                    if job
                    else []
                )
                return JSONResponse({"tracks": tracks}, headers=HEADERS)
            return await streams.deliver(entry, asset, request, authorize)
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=HEADERS) from None

    return router
