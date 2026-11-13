"""Opt-in Apple TV qualification with read-only catalog and playback access."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from omakase.plus import avatar, households
from omakase.plus import native_tv_devices as devices
from omakase.plus import web_playback_store as store
from omakase.plus.anime_synopsis import cached_synopsis
from omakase.plus.auth import authenticate_session
from omakase.plus.connections import private_connections_enabled
from omakase.plus.db import _connect
from omakase.plus.debrid_anime import (
    DebridTitleSource,
    revalidate_anime,
)
from omakase.plus.debrid_anime import (
    resolve_anime as resolve_debrid_anime,
)
from omakase.plus.debrid_catalog import (
    movie_ids as debrid_movie_ids,
)
from omakase.plus.debrid_catalog import (
    resolve_catalog as resolve_debrid_catalog,
)
from omakase.plus.debrid_catalog import (
    revalidate_catalog as revalidate_debrid_catalog,
)
from omakase.plus.debrid_catalog import (
    tv_episode_ids as debrid_tv_episode_ids,
)
from omakase.plus.debrid_library import (
    DebridLibrary,
    DebridLibraryError,
    account_binding,
    account_token,
    playback_provider,
)
from omakase.plus.debrid_stream import DebridMedia, DebridStreams
from omakase.plus.deps import get_db
from omakase.plus.history_artwork import (
    EpisodeArtworkUnavailable,
    anime_series_metadata,
    episode_artwork,
)
from omakase.plus.language_policy import LANGUAGES, normalize_language
from omakase.plus.middleware import get_current_user, require_user
from omakase.plus.native_tv_compatible import NativeCompatibleStream
from omakase.plus.native_tv_compatible import eligible as native_compatible_eligible
from omakase.plus.native_tv_hls import NativeHLS
from omakase.plus.native_tv_hls import eligible as native_hls_eligible
from omakase.plus.native_tv_library import (
    anime_artwork_id,
    collection,
    episode_image_identity,
    episodes,
    next_watch_path,
    personal_plex_movies,
    playback_monitor,
)
from omakase.plus.posters import PosterUnavailable, banner_bytes, poster_bytes
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.tv_service import plain
from omakase.plus.web_playback_admission import Admission, Busy
from omakase.plus.web_playback_catalog import (
    catalog_target,
    personal_scope,
    resolve_catalog_source,
    revalidate_catalog_source,
    revalidate_remote_catalog_source,
)
from omakase.plus.web_playback_delivery import DeliveryError, open_local, open_part
from omakase.plus.web_playback_navigation import enabled as playback_enabled
from omakase.plus.web_playback_source import (
    PlaybackSourceError,
    fresh_source_transaction,
    resolve_anime_source,
    revalidate_remote_source,
    revalidate_source,
)
from omakase.plus.web_security import require_csrf

PAGE = "/native-tv-test"
API = "/api/native-tv-test/v1"
LIBRARY_PAGE = "/native-tv-library"
DEVICES_PAGE = "/settings/devices"
DEVICE_PATH = "/plus" + DEVICES_PAGE
PAIRING_COOKIE = "omakase_device_pairing"
RESULT_COOKIE = "omakase_device_result"
LIBRARY_API = API + "/library"
COOKIE = "omakase_tv_test_media"
CODE_TTL = 300
PASS_TTL = 1200
LIBRARY_TTL = 3600
LIBRARY_MEDIA_TTL = 43200
NO_STORE = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
ANIME = re.compile(r"/plus/watch/([1-9][0-9]{0,17})/([0-9]{1,4})")
_LOG = logging.getLogger(__name__)
MOVIE = re.compile(r"/plus/watch/title/movie/(tt[0-9]{7,10})")
TV = re.compile(r"/plus/watch/title/tv/([1-9][0-9]{0,9})/([0-9]{1,3})/([1-9][0-9]{0,3})")
CLOUD = "/plus/watch/cloud/"


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


def native_tv_library_enabled():
    return native_tv_test_enabled() and os.getenv("OMAKASE_NATIVE_TV_LIBRARY") == "1"


def _library_guard(request):
    _https(request)
    if not native_tv_library_enabled():
        raise HTTPException(404, "Apple TV library unavailable", headers=NO_STORE)


def _https(request):
    # The ASGI server must trust its configured ingress, rather than
    # accepting an arbitrary client-supplied X-Forwarded-Proto header.
    if f"{request.url.scheme}://{request.url.netloc.lower()}" != _origin():
        raise HTTPException(404, "Apple TV test unavailable", headers=NO_STORE)


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _hls_database():
    return closing(_connect())


def resume_device_pairing(request):
    code = devices.normalized_code(request.cookies.get(PAIRING_COOKIE, ""))
    if code and native_tv_library_enabled():
        response = RedirectResponse(f"{DEVICE_PATH}?code={code}", status_code=303, headers=NO_STORE)
        response.delete_cookie(PAIRING_COOKIE, path="/plus", secure=True, httponly=True)
        return response
    return None


@dataclass(repr=False)
class TestPass:
    source: object
    browser_session: str
    target: store.ResolvedTarget
    title: str
    expires: float
    token_digest: str = field(default="")
    library_key: str | None = None
    hls: NativeHLS | NativeCompatibleStream | DebridMedia | None = None
    last_source_check: float = 0
    anime_monitor_id: int | None = None
    resume_cursor: tuple[int, float] | None = None
    resume_receipt: tuple[int, float, float] | None = None
    resume_last_save: float | None = None
    resume_barrier: int = 0
    watch_credited: bool = False


def _credit_movie_completion(db, target, stamp):
    catalog = db.execute(
        "SELECT tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if catalog is None:
        return None
    history = db.execute(
        "SELECT status_source FROM movie_history WHERE user_id=? AND imdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if history is not None and history["status_source"] == "manual":
        return True
    if history is None:
        db.execute(
            "INSERT INTO movie_history(user_id,imdb_id,tmdb_id,status,status_source,"
            "status_source_detail,status_updated_at,last_viewed_at,created_at,updated_at) "
            "VALUES(?,?,?,'completed','plex','apple_tv',?,?,?,?)",
            (
                target.user_id,
                target.canonical_id,
                catalog["tmdb_id"],
                stamp,
                stamp,
                stamp,
                stamp,
            ),
        )
    else:
        db.execute(
            "UPDATE movie_history SET status='completed',status_source='plex',"
            "status_source_detail='apple_tv',status_updated_at=?,last_viewed_at=?,"
            "updated_at=? WHERE user_id=? AND imdb_id=?",
            (stamp, stamp, stamp, target.user_id, target.canonical_id),
        )
    return True


def _credit_tv_completion(db, target, stamp):
    catalog = db.execute(
        "SELECT 1 FROM tv_catalog WHERE user_id=? AND tmdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if catalog is None:
        return None
    episode = db.execute(
        "SELECT watched,watch_source FROM tv_episodes "
        "WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
        (
            target.user_id,
            target.canonical_id,
            target.season_number,
            target.episode_number,
        ),
    ).fetchone()
    if episode is not None and episode["watch_source"] == "manual":
        return True
    if episode is None:
        db.execute(
            "INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,"
            "metadata_json,watched,watch_source,watch_source_detail,plex_present,updated_at) "
            "VALUES(?,?,?,?,'{}',1,'plex','apple_tv',0,?)",
            (
                target.user_id,
                target.canonical_id,
                target.season_number,
                target.episode_number,
                stamp,
            ),
        )
    else:
        db.execute(
            "UPDATE tv_episodes SET watched=1,watch_source='plex',"
            "watch_source_detail='apple_tv',updated_at=? "
            "WHERE user_id=? AND tmdb_id=? AND season_number=? AND episode_number=?",
            (
                stamp,
                target.user_id,
                target.canonical_id,
                target.season_number,
                target.episode_number,
            ),
        )
    history = db.execute(
        "SELECT status,status_source FROM tv_history WHERE user_id=? AND tmdb_id=?",
        (target.user_id, target.canonical_id),
    ).fetchone()
    if history is None:
        db.execute(
            "INSERT INTO tv_history(user_id,tmdb_id,status,status_source,"
            "status_source_detail,updated_at) VALUES(?,?,'watching','plex','apple_tv',?)",
            (target.user_id, target.canonical_id, stamp),
        )
    elif history["status_source"] != "manual":
        status = "completed" if history["status"] == "completed" else "watching"
        db.execute(
            "UPDATE tv_history SET status=?,status_source='plex',"
            "status_source_detail='apple_tv',updated_at=? WHERE user_id=? AND tmdb_id=?",
            (status, stamp, target.user_id, target.canonical_id),
        )
    return True


def _credit_native_watch(db, grant, *, position, observed_at):
    """Project an Apple TV completion into the durable personal watch record."""
    target = grant.target
    duration = target.duration_seconds
    if duration <= 0:
        return None
    fraction = min(1.0, max(0.0, position / duration))
    if fraction < 0.85:
        return None
    stamp = datetime.fromtimestamp(observed_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if target.domain == "movie":
        return _credit_movie_completion(db, target, stamp)
    if target.domain == "tv":
        return _credit_tv_completion(db, target, stamp)
    if target.domain != "anime" or grant.anime_monitor_id is None:
        return None
    from omakase.plus.watch.models import WatchEvent
    from omakase.plus.watch.service import WatchServiceError, ingest_watch_event

    event = WatchEvent.progress(
        source="apple_tv",
        episode=target.episode_number,
        event_key=(
            f"native-tv:{target.user_id}:{target.canonical_id}:"
            f"{target.season_number}:{target.episode_number}"
        ),
        playback_fraction=fraction,
        confidence="exact",
    )
    try:
        return ingest_watch_event(
            db,
            user_id=target.user_id,
            monitor_id=grant.anime_monitor_id,
            event=event,
            observed_at=stamp,
        )
    except WatchServiceError:
        _LOG.warning("native watch credit rejected", exc_info=True)
        return None


def _playback_languages(grant):
    if isinstance(grant.hls, NativeHLS):
        audio, subtitles = grant.hls.audio, grant.hls.subtitles
    elif isinstance(grant.hls, NativeCompatibleStream):
        # Plex's compatible stream chooses the audio; only indexed text tracks are enumerated.
        audio, subtitles = (), (track.stream for track in grant.hls.tracks)
    else:
        indexed = [
            track
            for track in getattr(grant.source, "streams", ())
            if track.container_index is not None
        ]
        audio = [
            track
            for track in indexed
            if track.kind == "audio" and track.codec in {"aac", "ac3", "eac3"}
        ]
        subtitles = [
            track
            for track in indexed
            if track.kind == "subtitle" and track.codec in {"mov_text", "tx3g"}
        ]

    def labels(tracks):
        return list(
            dict.fromkeys(
                LANGUAGES[language]
                for track in tracks
                if (language := normalize_language(track.language, observation=True))
            )
        )[:4]

    return {
        name: names
        for name, tracks in (("audio_languages", audio), ("subtitle_languages", subtitles))
        if (names := labels(tracks))
    }


class ResumeCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    seq: int = Field(strict=True, ge=1, le=100_000)
    position_seconds: float = Field(strict=True, ge=0, le=43_200)


@dataclass(repr=False)
class LibraryGrant:
    actor: int
    browser_session: str
    library_id: str
    expires: float
    last_seen_at: int | None = None
    network_hint: str | None = None


def create_native_tv_test_router(
    templates,
    context_factory,
    *,
    resolve_anime=resolve_anime_source,
    local_anime=revalidate_source,
    remote_anime=revalidate_remote_source,
    transport=None,
    clock=time.monotonic,
    hls_database=_hls_database,
    subtitle_root=None,
    seek_root=None,
):
    router = APIRouter(
        route_class=bounded_form_route(2048, "Apple TV test request too large", guard=_guard)
    )
    codes: dict[str, TestPass] = {}
    passes: dict[str, TestPass] = {}
    pairing_codes: dict[str, LibraryGrant] = {}
    pairing_links: dict[int, str] = {}
    libraries: dict[str, LibraryGrant] = {}
    rejected_codes = deque()
    pairing_attempts = deque()
    # Native HLS reads video, separate audio, and captions in parallel and
    # can fetch multiple windows within one minute of repeated seeks.
    admission = Admission(clock, read_rate_limit=360)
    artwork_slots = asyncio.Semaphore(5)
    hls_slots = asyncio.Semaphore(2)
    cloud_library = DebridLibrary(transport, clock)
    cloud_streams = DebridStreams(
        transport, clock, subtitle_root=subtitle_root, seek_root=seek_root
    )
    resolved_cache = OrderedDict()
    preload_tasks = {}
    warm_paths = set()

    async def close_hls(job):
        if isinstance(job, DebridMedia):
            return
        try:
            await job.close()
        finally:
            hls_slots.release()

    def drop_pass(media_id):
        grant = passes.pop(media_id, None)
        if grant and grant.hls:
            return asyncio.create_task(close_hls(grant.hls))
        return None

    def prune():
        for registry in (codes, pairing_codes, libraries):
            for key, grant in tuple(registry.items()):
                if grant.expires <= clock():
                    del registry[key]
        for actor, code in tuple(pairing_links.items()):
            if _digest(code) not in pairing_codes:
                del pairing_links[actor]
        for key, grant in tuple(passes.items()):
            if grant.expires <= clock():
                drop_pass(key)

    def revoke(actor):
        for key, grant in tuple(codes.items()):
            if grant.target.user_id == actor:
                del codes[key]
        for key, grant in tuple(passes.items()):
            if grant.target.user_id == actor and grant.library_key is None:
                drop_pass(key)

    def revoke_library(actor, *, all_devices=False):
        active_keys = {key for key, grant in libraries.items() if grant.actor == actor}
        for registry in (pairing_codes, libraries):
            for key, grant in tuple(registry.items()):
                if grant.actor == actor:
                    del registry[key]
        for key, grant in tuple(passes.items()):
            if (
                grant.target.user_id == actor
                and grant.library_key is not None
                and (all_devices or grant.library_key in active_keys)
            ):
                drop_pass(key)

    def device_rows(db, actor, *, shared=False):
        rows = []
        entries = (
            devices.for_household_owner(db, actor) if shared else devices.for_account(db, actor)
        )
        for row in entries:
            grant = LibraryGrant(
                row["user_id"], row["browser_session"], row["library_id"], clock() + 1
            )
            try:
                library_scope(db, grant)
            except HTTPException:
                continue
            rows.append(
                {
                    "reference": None if shared else devices.device_reference(row["token_digest"]),
                    "name": row["display_name"],
                    "kind": row["device_kind"],
                    "label": devices.DEVICE_LABELS[row["device_kind"]],
                    "member": row["member_name"] if shared else None,
                    "member_id": row["user_id"] if shared else None,
                    "shared": shared or row["shared_household_id"] is not None,
                    "expiry": datetime.fromtimestamp(row["expires_at"], timezone.utc),
                    "last_seen": (
                        datetime.fromtimestamp(row["last_seen_at"], timezone.utc)
                        if row["last_seen_at"] is not None
                        else None
                    ),
                    "network": row["network_hint"],
                    "temporary": False,
                }
            )
        if shared:
            return rows
        for key, grant in libraries.items():
            if grant.actor != actor:
                continue
            try:
                library_scope(db, grant)
            except HTTPException:
                continue
            rows.append(
                {
                    "reference": devices.device_reference(key),
                    "name": "Apple TV",
                    "kind": "apple-tv",
                    "label": devices.DEVICE_LABELS["apple-tv"],
                    "expiry": datetime.fromtimestamp(
                        time.time() + grant.expires - clock(), timezone.utc
                    ),
                    "last_seen": (
                        datetime.fromtimestamp(grant.last_seen_at, timezone.utc)
                        if grant.last_seen_at is not None
                        else None
                    ),
                    "network": grant.network_hint,
                    "temporary": True,
                }
            )
        return rows

    def account_device_key(db, actor, reference):
        return next(
            (
                row["token_digest"]
                for row in devices.for_account(db, actor)
                if devices.device_reference(row["token_digest"]) == reference
            ),
            None,
        )

    def disconnect_device(db, actor, reference):
        key = account_device_key(db, actor, reference)
        if key is not None:
            devices.revoke(db, actor, key=key)
        else:
            key = next(
                (
                    key
                    for key, grant in libraries.items()
                    if grant.actor == actor and devices.device_reference(key) == reference
                ),
                None,
            )
            if key is None:
                return False
            del libraries[key]
        for media_id, grant in tuple(passes.items()):
            if grant.library_key == key:
                drop_pass(media_id)
        return True

    def library_scope(db, grant):
        authenticated, _ = authenticate_session(db, grant.browser_session, renew=False)
        if authenticated != grant.actor or grant.expires <= clock():
            raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
        try:
            scope = personal_scope(db, grant.actor)
        except (ValueError, households.HouseholdError):
            raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE) from None
        if scope.library_id != grant.library_id:
            raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
        return scope

    def library_for_key(db, key):
        grant = libraries.get(key)
        if grant is not None and grant.expires > clock():
            return grant
        row = devices.lookup(db, key)
        if row and row["user_id"] is not None:
            return LibraryGrant(
                row["user_id"],
                row["browser_session"],
                row["library_id"],
                clock() + row["remaining"],
                last_seen_at=row["last_seen_at"],
            )
        return None

    def library_key(request):
        _library_guard(request)
        header = request.headers.get("Authorization", "")
        match = re.fullmatch(r"Bearer ([A-Za-z0-9_-]{43})", header)
        if not match:
            raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
        return _digest(match[1])

    def observe_device(request, db, key, grant):
        seen_at = int(time.time())
        if (
            grant.last_seen_at is not None
            and seen_at - grant.last_seen_at < devices.ACTIVITY_INTERVAL
        ):
            return
        # Honor only the ASGI server's configured proxy boundary.
        network = devices.network_hint(request.client.host) if request.client else None
        if key in libraries:
            grant.last_seen_at = seen_at
            grant.network_hint = network
        else:
            devices.record_activity(db, key, seen_at=seen_at, network=network)

    def active_library(request, db):
        key = library_key(request)
        prune()
        grant = library_for_key(db, key)
        if grant is None:
            raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
        scope = library_scope(db, grant)
        observe_device(request, db, key, grant)
        return key, grant, scope

    def requested_scope(request, db):
        key, grant, personal = active_library(request, db)
        library_id = request.query_params.get("library_id")
        if library_id in (None, personal.library_id):
            return key, grant, personal
        try:
            if households.feature_enabled() and re.fullmatch(r"[0-9a-f]{32}", library_id):
                scope = households.resolve_scope(db, viewer_id=grant.actor, library_id=library_id)
                if scope.kind == "household":
                    return key, grant, scope
        except households.HouseholdError:
            pass
        raise HTTPException(404, "Apple TV library unavailable", headers=NO_STORE)

    def cloud_token(db, scope):
        if scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id:
            raise HTTPException(403, "Cloud files use your personal library", headers=NO_STORE)
        return account_token(db, scope.viewer_id)

    def check_cloud(db, entry):
        if entry is None or not native_tv_library_enabled():
            raise HTTPException(404, "Cloud playback expired", headers=NO_STORE)
        grant = library_for_key(db, entry.binding)
        if (
            grant is None
            or grant.actor != entry.user_id
            or account_binding(account_token(db, grant.actor)) != entry.source.account
        ):
            raise HTTPException(404, "Cloud playback expired", headers=NO_STORE)
        library_scope(db, grant)

    async def cloud_catalog(request, db, scope, offset, *, q="", folder=None):
        token = cloud_token(db, scope)
        if not token:
            return {"items": [], "next_offset": None}
        try:
            rows = (
                await cloud_library.files(token, folder)
                if folder is not None
                else await cloud_library.entries(token, query=q)
            )
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        _, _, current = requested_scope(request, db)
        if not secrets.compare_digest(token, cloud_token(db, current)):
            raise HTTPException(403, "Real-Debrid connection changed", headers=NO_STORE)
        return {
            "items": [row.payload() for row in rows[offset : offset + 30]],
            "next_offset": offset + 30 if offset + 30 < len(rows) else None,
        }

    def checked(db, grant):
        source, actor = grant.source, grant.target.user_id
        authenticated, _ = authenticate_session(db, grant.browser_session, renew=False)
        if authenticated != actor or grant.expires <= clock():
            raise HTTPException(404, "Apple TV pass expired", headers=NO_STORE)
        if grant.hls and not isinstance(grant.hls, DebridMedia):
            eligible = (
                native_compatible_eligible
                if isinstance(grant.hls, NativeCompatibleStream)
                else native_hls_eligible
            )
            if not eligible(source):
                raise HTTPException(404, "Apple TV stream unavailable", headers=NO_STORE)
        if grant.library_key is not None:
            library = (
                library_for_key(db, grant.library_key) if native_tv_library_enabled() else None
            )
            if library is None or (library.actor, library.browser_session) != (
                actor,
                grant.browser_session,
            ):
                raise HTTPException(404, "Apple TV pass expired", headers=NO_STORE)
        try:
            if source._scope.kind == "household" and not households.feature_enabled():
                raise ValueError("Household playback is disabled")
            households.revalidate_scope(db, source._scope)
            if isinstance(source, DebridTitleSource):
                if playback_provider(db, actor) == "plex":
                    raise ValueError("Playback source changed")
                if source.domain in {"movie", "tv"}:
                    revalidate_debrid_catalog(db, source)
                else:
                    revalidate_anime(db, source)
            elif (
                grant.library_key
                and source._scope.kind == "personal"
                and playback_provider(db, actor) == "realdebrid"
            ):
                raise ValueError("Playback source changed")
            elif grant.target.domain in {"movie", "tv"}:
                revalidate_catalog_source(db, source)
            else:
                local_anime(db, source)
        except (ValueError, households.HouseholdError):
            raise HTTPException(
                404, "Apple TV copy no longer available", headers=NO_STORE
            ) from None

    def admission_rejected(error):
        return HTTPException(
            429, str(error), headers=NO_STORE | {"Retry-After": str(error.retry_after)}
        )

    def acquire(kind, actor, media=None):
        try:
            return admission.acquire(kind, actor, media)
        except Busy as error:
            raise admission_rejected(error) from None

    def page(request, user, **context):
        return templates.TemplateResponse(
            request,
            "native_tv_test.html",
            {**context_factory(request, user, active_page="library"), **context},
            headers=NO_STORE,
        )

    async def exact_source(db, scope, path):
        actor = scope.viewer_id
        if match := ANIME.fullmatch(path):
            episode = int(match[2])
            monitor = playback_monitor(db, scope, match[1])
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

    async def details_for_path(db, scope, path):
        if match := ANIME.fullmatch(path):
            episode = int(match[2])
            monitor = playback_monitor(db, scope, match[1])
            if not monitor or episode > 5000 or (episode == 0) != (monitor["format"] == "MOVIE"):
                raise ValueError("Episode unavailable")
            episode_title = f"Episode {episode}"
            summary = ""
            try:
                source = await resolve_anime(
                    db,
                    actor_user_id=scope.viewer_id,
                    library_id=scope.library_id,
                    anilist_id=monitor["anilist_id"],
                    season_number=monitor["season_number"] or 0,
                    episode_number=episode,
                    media_version=None,
                    transport=transport,
                )
            except Exception:
                # A Real-Debrid-only episode may not have a Plex copy. Keep the
                # details panel useful with the series synopsis instead of 404.
                source = None
            if source is not None:
                episode_title = getattr(source, "episode_name", "") or episode_title
                summary = getattr(source, "episode_summary", "") or ""
            if not summary and monitor["anilist_id"]:
                try:
                    summary = cached_synopsis(db, monitor["anilist_id"])["description"]
                except Exception:
                    summary = ""
            artwork = (
                f"episode-artwork/anime/{match[1]}/{monitor['season_number']}/{episode}"
                if monitor["season_number"] and episode
                else None
            )
            return {
                "title": monitor["title"],
                "episode_title": episode_title,
                "episode_number": episode or None,
                "note": f"Episode {episode}" if episode else "",
                "summary": summary,
                "artwork": artwork,
            }
        if match := MOVIE.fullmatch(path):
            catalog, _ = catalog_target(db, scope, "movie", match[1], 0, 0)
            return {
                "title": catalog["title"],
                "episode_title": "",
                "episode_number": None,
                "note": str(catalog.get("year") or ""),
                "summary": catalog.get("overview") or "",
                "artwork": None,
            }
        if match := TV.fullmatch(path):
            canonical, season, episode = match[1], int(match[2]), int(match[3])
            if int(canonical) > 2_147_483_647 or season > 200 or episode > 1000:
                raise ValueError("Episode unavailable")
            catalog, episode_row = catalog_target(db, scope, "tv", canonical, season, episode)
            metadata = json.loads(episode_row["episode_json"])
            return {
                "title": catalog["title"],
                "episode_title": metadata.get("title")
                or metadata.get("name")
                or f"Episode {episode}",
                "episode_number": episode,
                "note": f"S{season} E{episode}",
                "summary": plain(metadata.get("overview"))[:650]
                if metadata.get("overview")
                else "",
                "artwork": f"episode-artwork/tv/{canonical}/{season}/{episode}",
            }
        raise ValueError("Unsupported watch page")

    async def resolve_debrid_path(db, scope, path):
        if match := ANIME.fullmatch(path):
            return await resolve_debrid_anime(
                db, scope, int(match[1]), int(match[2]), cloud_library
            )
        if match := MOVIE.fullmatch(path):
            return await resolve_debrid_catalog(db, scope, "movie", match[1], 0, 0, cloud_library)
        if match := TV.fullmatch(path):
            season, episode = int(match[2]), int(match[3])
            if int(match[1]) <= 2_147_483_647 and season <= 200 and episode <= 1000:
                return await resolve_debrid_catalog(
                    db, scope, "tv", match[1], season, episode, cloud_library
                )
        return None

    async def debrid_source(db, scope, path, *, use_cache=False):
        cache_key = (scope.viewer_id, path)
        if use_cache:
            cached = resolved_cache.get(cache_key)
            if cached is not None and cached[0] > clock():
                resolved_cache.move_to_end(cache_key)
                return cached[1]
        result = await resolve_debrid_path(db, scope, path)
        if use_cache and result is not None:
            resolved_cache[cache_key] = (clock() + 150, result)
            while len(resolved_cache) > 16:
                resolved_cache.popitem(last=False)
        return result

    def schedule_path_warm(scope, path):
        if not isinstance(path, str) or not path or path in warm_paths:
            return
        if not (ANIME.fullmatch(path) or MOVIE.fullmatch(path) or TV.fullmatch(path)):
            return
        warm_paths.add(path)

        async def warm():
            try:
                with hls_database() as connection:
                    candidate = await debrid_source(connection, scope, path, use_cache=True)
                    if candidate:
                        await cloud_streams.prepare(candidate[0].cloud, wait_first=False)
            except (
                DebridLibraryError,
                TimeoutError,
                households.HouseholdError,
                sqlite3.Error,
                OSError,
            ):
                return
            finally:
                warm_paths.discard(path)

        asyncio.create_task(warm())

    def schedule_preload(key, grant):
        if key in preload_tasks:
            return
        scope = grant.source._scope
        source = grant.source
        monitor = grant.anime_monitor_id

        async def warm():
            try:
                with hls_database() as connection:
                    path = next_watch_path(connection, source, anime_monitor_id=monitor)
                    if path is None:
                        return
                    candidate = await debrid_source(connection, scope, path, use_cache=True)
                    if candidate:
                        await cloud_streams.prepare(candidate[0].cloud, wait_first=False)
            except (
                DebridLibraryError,
                TimeoutError,
                households.HouseholdError,
                sqlite3.Error,
                OSError,
            ):
                return
            finally:
                preload_tasks.pop(key, None)

        preload_tasks[key] = asyncio.create_task(warm())

    def test_pass(source, title, actor, session, ttl, library_key=None, *, hls=False):
        if library_key:
            supported = hls or (
                source.container == "mp4"
                and source.video_codec in {"h264", "hevc"}
                and source.audio_codec in {"aac", "ac3", "eac3"}
            )
        else:
            supported = (source.container, source.video_codec, source.audio_codec) == (
                "mp4",
                "h264",
                "aac",
            )
        if not supported:
            raise ValueError("For this test choose a title with an H.264/AAC MP4 copy.")
        target = store.ResolvedTarget(
            actor,
            source._scope.library_id,
            source.canonical_id if hasattr(source, "canonical_id") else str(source.anilist_id),
            source.season_number,
            source.episode_number,
            source.media_version,
            source.duration_ms / 1000,
            domain=getattr(source, "domain", "anime"),
        )
        return TestPass(
            source, session, target, str(title)[:120], clock() + ttl, library_key=library_key
        )

    @router.post(PAGE)
    async def issue(request: Request, user=Depends(require_user), db=Depends(get_db)):
        _https(request)
        form = await request.form()
        require_csrf(request, str(form.get("csrf_token", "")))
        path, library = str(form.get("watch_path", "")), str(form.get("library_id", ""))
        release = acquire("start", user.id)
        try:
            async with asyncio.timeout(12):
                scope = personal_scope(db, user.id)
                if library and library != scope.library_id:
                    raise ValueError("Personal titles only")
                source, title = await exact_source(db, scope, path)
            try:
                grant = test_pass(
                    source, title, user.id, request.cookies["omakase_session"], CODE_TTL
                )
            except ValueError as error:
                return page(request, user, error=str(error))
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

    async def take_code(request, registry):
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
            registry.pop(_digest(code), None)
            if isinstance(code, str) and re.fullmatch(r"[0-9A-F]{20}", code)
            else None
        )
        if grant is None:
            rejected_codes.append(now)
            raise HTTPException(404, "Apple TV code unavailable", headers=NO_STORE)
        return grant

    def media_reply(db, grant, ttl, *, next_path=None):
        token, media_id = secrets.token_urlsafe(32), secrets.token_hex(16)
        target = grant.target
        previous = store.get_resume(db, target=target)
        grant.resume_barrier = store.resume_watch_barrier(db, target)
        position = 0
        if previous is not None:
            grant.resume_cursor = (previous["revision"], previous["updated_at"])
            if store.native_resume_barrier(db, target) in (None, grant.resume_barrier):
                position = previous["position_seconds"]
        resume_sync = grant.library_key is not None and bool(
            re.fullmatch(r"[a-f0-9]{64}", getattr(grant.source, "source_identity", "") or "")
        )
        episode_metadata = {
            key: value
            for key in ("episode_name", "episode_summary")
            if target.episode_number and (value := getattr(grant.source, key, ""))
        }
        response = JSONResponse(
            {
                "title": grant.title,
                "media_id": media_id,
                "media_token": token,
                "media_format": "hls" if grant.hls else "mp4",
                **(
                    {"media_playlist": "vod"}
                    if isinstance(grant.hls, (NativeCompatibleStream, DebridMedia))
                    else {}
                ),
                "expires_in": ttl,
                "duration_seconds": target.duration_seconds,
                "position_seconds": position if 0 < position < target.duration_seconds else 0,
                **({"access_check": True} if grant.library_key is not None else {}),
                **episode_metadata,
                **_playback_languages(grant),
                **(
                    {
                        "resume_sync": True,
                        "resume_updated_at": previous["updated_at"] if previous else None,
                    }
                    if resume_sync
                    else {}
                ),
                **({"next_watch_path": next_path} if next_path else {}),
                **(
                    {
                        "resume_scope": hashlib.sha256(
                            f"household:{_origin()}:{target.library_id}:{target.user_id}".encode()
                        ).hexdigest()
                    }
                    if grant.source._scope.kind == "household"
                    else {}
                ),
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
        grant.expires, grant.token_digest = clock() + ttl, _digest(token)
        passes[media_id] = grant
        if grant.hls:
            asyncio.get_running_loop().call_later(ttl, drop_pass, media_id)
        return response

    @router.post(API + "/redeem")
    async def redeem(request: Request, db=Depends(get_db)):
        _https(request)
        grant = await take_code(request, codes)
        checked(db, grant)
        return media_reply(db, grant, PASS_TTL)

    @router.get(LIBRARY_PAGE)
    @router.get(DEVICES_PAGE)
    async def paired_devices(request: Request, user=Depends(get_current_user), db=Depends(get_db)):
        _library_guard(request)
        code = devices.normalized_code(request.query_params.get("code", ""))
        if user is None:
            if "text/html" not in request.headers.get("accept", "").lower():
                raise HTTPException(401, "Authentication required", headers=NO_STORE)
            response = RedirectResponse("/plus/login", status_code=303, headers=NO_STORE)
            if code:
                response.set_cookie(
                    PAIRING_COOKIE,
                    code,
                    max_age=devices.PAIRING_TTL,
                    path="/plus",
                    secure=True,
                    httponly=True,
                    samesite="lax",
                )
            return response
        if request.url.path == "/plus" + LIBRARY_PAGE:
            target = f"{DEVICE_PATH}?code={code}" if code else DEVICE_PATH
            return RedirectResponse(target, status_code=303, headers=NO_STORE)
        prune()
        context = context_factory(request, user, active_page="settings")
        manual_code = pairing_links.get(user.id)
        pending = pairing_codes.get(_digest(manual_code)) if manual_code else None
        if pending and pending.browser_session == request.cookies["omakase_session"]:
            context["invitation"] = f"{_origin()}#library-{manual_code}"
        result = request.cookies.get(RESULT_COOKIE, "")
        rows = device_rows(db, user.id)
        membership = (
            db.execute(
                """SELECT member.display_name, member.role, library.name
               FROM household_memberships AS member JOIN libraries AS library ON library.id=member.library_id
               WHERE member.user_id=? AND member.state='active' AND library.state='active'""",
                (user.id,),
            ).fetchone()
            if context.get("households_enabled")
            else None
        )
        is_household_owner = membership is not None and membership["role"] == "owner"
        shared_rows = device_rows(db, user.id, shared=True) if is_household_owner else []
        shared_by_member = {}
        for device in shared_rows:
            shared_by_member.setdefault(device["member_id"], []).append(device)
        context.update(
            device_code=f"{code[:4]}-{code[4:]}" if code else "",
            devices=rows,
            device_owner=(context.get("display_name") or "").strip(),
            device_household=membership,
            household_device_groups=list(shared_by_member.values()),
            household_device_count=len(shared_rows),
            is_household_owner=is_household_owner,
            device_result=result,
        )
        response = templates.TemplateResponse(
            request, "native_tv_library.html", context, headers=NO_STORE
        )
        if result:
            response.delete_cookie(RESULT_COOKIE, path=DEVICE_PATH, secure=True, httponly=True)
        return response

    @router.post(LIBRARY_PAGE)
    @router.post(DEVICES_PAGE)
    async def pair_browser(request: Request, user=Depends(require_user), db=Depends(get_db)):
        _library_guard(request)
        form = await request.form()
        require_csrf(request, str(form.get("csrf_token", "")))
        action = form.get("action")
        prune()
        browser_session = request.cookies["omakase_session"]
        if action == "issue":
            previous = pairing_links.pop(user.id, None)
            if previous:
                pairing_codes.pop(_digest(previous), None)
            if len(pairing_codes) + len(libraries) >= 32:
                raise HTTPException(429, "Try again when another TV link expires", headers=NO_STORE)
            scope = personal_scope(db, user.id)
            code = secrets.token_hex(10).upper()
            pairing_codes[_digest(code)] = LibraryGrant(
                user.id,
                browser_session,
                scope.library_id,
                clock() + CODE_TTL,
            )
            pairing_links[user.id] = code
            result = "issued"
        elif action == "approve":
            scope = personal_scope(db, user.id)
            device_kind = devices.approve(
                db,
                code=str(form.get("device_code", "")),
                actor=user.id,
                browser_session=browser_session,
                library_id=scope.library_id,
                display_name=str(form.get("device_name", "")),
            )
            if device_kind == "iphone":
                result = "iphone_linked"
            else:
                result = "linked" if device_kind else "invalid"
        elif action == "disconnect":
            reference = str(form.get("device_reference", ""))
            disconnected = disconnect_device(db, user.id, reference)
            result = "disconnected" if disconnected else "missing"
        elif action == "rename":
            reference = str(form.get("device_reference", ""))
            key = account_device_key(db, user.id, reference)
            renamed = key is not None and devices.rename(
                db, user.id, key, str(form.get("device_name", ""))
            )
            result = "renamed" if renamed else "missing"
        elif action in {"share", "unshare"} and households.feature_enabled():
            reference = str(form.get("device_reference", ""))
            key = account_device_key(db, user.id, reference)
            share = action == "share"
            changed = key is not None and devices.set_household_sharing(
                db, user.id, key, share=share
            )
            result = ("shared" if share else "unshared") if changed else "share_unavailable"
        elif action == "revoke":
            revoke_library(user.id, all_devices=True)
            devices.revoke(db, user.id)
            result = "revoked"
        else:
            raise HTTPException(400, "Choose a device action", headers=NO_STORE)
        response = RedirectResponse(DEVICE_PATH, status_code=303, headers=NO_STORE)
        response.set_cookie(
            RESULT_COOKIE,
            result,
            max_age=30,
            path=DEVICE_PATH,
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    @router.post(LIBRARY_API + "/redeem")
    async def redeem_library(request: Request, db=Depends(get_db)):
        _library_guard(request)
        grant = await take_code(request, pairing_codes)
        library_scope(db, grant)
        token = secrets.token_urlsafe(32)
        grant.expires = clock() + LIBRARY_TTL
        key = _digest(token)
        libraries[key] = grant
        observe_device(request, db, key, grant)
        return JSONResponse({"token": token, "expires_in": LIBRARY_TTL}, headers=NO_STORE)

    @router.post(LIBRARY_API + "/device")
    async def begin_device(request: Request, db=Depends(get_db)):
        _library_guard(request)
        kind = request.query_params.get("kind", "apple-tv")
        if kind not in devices.DEVICE_LABELS:
            raise HTTPException(422, "Unsupported device kind", headers=NO_STORE)
        now = clock()
        while pairing_attempts and pairing_attempts[0] <= now - 60:
            pairing_attempts.popleft()
        if len(pairing_attempts) >= 12 or (pairing := devices.begin(db, kind=kind)) is None:
            raise HTTPException(429, "Try again shortly", headers=NO_STORE | {"Retry-After": "60"})
        pairing_attempts.append(now)
        token, code = pairing
        return JSONResponse(
            {"device_token": token, "user_code": code, "expires_in": devices.PAIRING_TTL},
            headers=NO_STORE,
        )

    @router.get(LIBRARY_API + "/device")
    async def device_status(request: Request, db=Depends(get_db)):
        _library_guard(request)
        header = request.headers.get("Authorization", "")
        secret = header.removeprefix("Bearer ")
        key = (
            devices.digest(secret)
            if header != secret and devices.DEVICE_SECRET.fullmatch(secret)
            else None
        )
        row = devices.lookup(db, key) if key else None
        if row is None:
            raise HTTPException(401, "Apple TV pairing expired", headers=NO_STORE)
        if row["user_id"] is None:
            return JSONResponse({"pending": True}, status_code=202, headers=NO_STORE)
        grant = LibraryGrant(
            row["user_id"],
            row["browser_session"],
            row["library_id"],
            clock() + row["remaining"],
            last_seen_at=row["last_seen_at"],
        )
        library_scope(db, grant)
        observe_device(request, db, key, grant)
        return JSONResponse({"expires_in": row["remaining"]}, headers=NO_STORE)

    @router.get(LIBRARY_API + "/profile")
    async def linked_profile(request: Request, db=Depends(get_db)):
        _, grant, _ = active_library(request, db)
        identity = avatar.avatar_context(db, grant.actor)
        return JSONResponse(
            {
                "id": str(grant.actor),
                "name": identity["display_name"].strip()[:64] or "Profile",
                "has_avatar": identity["avatar_available"],
            },
            headers=NO_STORE,
        )

    @router.get(LIBRARY_API + "/profile/avatar")
    async def linked_profile_avatar(request: Request, db=Depends(get_db)):
        _, grant, _ = active_library(request, db)
        picture = avatar.get_avatar(db, grant.actor)
        if picture is None:
            raise HTTPException(404, "Profile picture unavailable", headers=NO_STORE)
        return Response(
            picture.image_bytes,
            media_type=picture.content_type,
            headers=NO_STORE | {"X-Content-Type-Options": "nosniff"},
        )

    @router.get(LIBRARY_API + "/catalog")
    async def library_catalog(
        request: Request,
        kind: Literal["anime", "movie", "tv", "cloud"],
        offset: int = 0,
        q: str = Query("", max_length=100),
        db=Depends(get_db),
    ):
        key, _, scope = requested_scope(request, db)
        if kind == "cloud":
            reply = await cloud_catalog(request, db, scope, max(0, offset), q=q.strip())
            return JSONResponse(reply, headers=NO_STORE)
        try:
            plex_movies = ()
            debrid_movies = ()
            debrid_torrents = None
            mode = playback_provider(db, scope.viewer_id) if scope.kind == "personal" else "plex"
            if kind == "anime" and mode == "realdebrid":
                token = account_token(db, scope.viewer_id)
                debrid_torrents, _ = await cloud_library.inventory(token) if token else ({}, {})
                _, _, scope = requested_scope(request, db)
            if kind == "movie" and scope.kind == "personal":
                if mode != "realdebrid":
                    plex_movies = await personal_plex_movies(db, scope, transport=transport)
                if mode != "plex":
                    try:
                        debrid_movies = await debrid_movie_ids(db, scope, cloud_library)
                    except DebridLibraryError:
                        if mode == "realdebrid":
                            raise
                if library_for_key(db, key) is None:
                    raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
            reply = collection(
                db,
                scope,
                kind,
                max(0, offset),
                q.strip(),
                plex_movies=plex_movies,
                debrid_movies=debrid_movies,
                debrid_only=mode == "realdebrid",
                debrid_torrents=debrid_torrents,
            )
            if mode == "realdebrid":
                featured = reply.get("featured") or {}
                path = featured.get("featured_path") or featured.get("watch_path")
                schedule_path_warm(scope, path)
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        except households.HouseholdError:
            raise HTTPException(404, "Apple TV library unavailable", headers=NO_STORE) from None
        return JSONResponse(reply, headers=NO_STORE)

    @router.get(LIBRARY_API + "/libraries")
    async def library_choices(request: Request, db=Depends(get_db)):
        _, grant, personal = active_library(request, db)
        items = [{"id": personal.library_id, "name": "Personal library", "kind": "personal"}]
        if households.feature_enabled():
            items = households.navigation_context(db, viewer_id=grant.actor)["libraries"]
        return JSONResponse({"items": items}, headers=NO_STORE)

    @router.get(LIBRARY_API + "/artwork/{title_id}/{style}")
    async def library_artwork(
        request: Request, title_id: str, style: Literal["poster", "banner"], db=Depends(get_db)
    ):
        _, _, scope = requested_scope(request, db)
        anilist_id = anime_artwork_id(db, scope, title_id)
        if anilist_id is None:
            raise HTTPException(404, "Artwork unavailable", headers=NO_STORE)
        fetch = banner_bytes if style == "banner" else poster_bytes
        try:
            async with asyncio.timeout(10), artwork_slots:
                data, media_type = await fetch(anilist_id)
        except (PosterUnavailable, TimeoutError):
            raise HTTPException(404, "Artwork unavailable", headers=NO_STORE) from None
        _, _, current = requested_scope(request, db)
        if anime_artwork_id(db, current, title_id) != anilist_id:
            raise HTTPException(404, "Artwork unavailable", headers=NO_STORE)
        return Response(data, media_type=media_type, headers=NO_STORE)

    @router.get(LIBRARY_API + "/episodes")
    async def library_episodes(
        request: Request,
        kind: Literal["anime", "tv", "cloud"],
        title_id: str,
        offset: int = 0,
        db=Depends(get_db),
    ):
        _, _, scope = requested_scope(request, db)
        if kind == "cloud":
            reply = await cloud_catalog(request, db, scope, max(0, offset), folder=title_id)
            return JSONResponse(reply, headers=NO_STORE)
        try:
            mode = playback_provider(db, scope.viewer_id) if scope.kind == "personal" else "plex"
            debrid_episodes = ()
            debrid_torrents = None
            if kind == "anime" and mode == "realdebrid":
                token = account_token(db, scope.viewer_id)
                debrid_torrents, _ = await cloud_library.inventory(token) if token else ({}, {})
                _, _, scope = requested_scope(request, db)
            if kind == "tv" and mode != "plex" and re.fullmatch(r"[1-9][0-9]{0,9}", title_id):
                try:
                    async with asyncio.timeout(25):
                        debrid_episodes = await debrid_tv_episode_ids(
                            db, scope, title_id, cloud_library
                        )
                    _, _, scope = requested_scope(request, db)
                except (DebridLibraryError, TimeoutError):
                    if mode == "realdebrid":
                        raise HTTPException(
                            502, "Real-Debrid episode inventory unavailable", headers=NO_STORE
                        ) from None
            reply = episodes(
                db,
                scope,
                kind,
                title_id,
                max(0, offset),
                **(
                    {
                        "debrid_episodes": debrid_episodes,
                        "debrid_only": True,
                        "debrid_torrents": debrid_torrents,
                    }
                    if mode == "realdebrid"
                    else {"debrid_episodes": debrid_episodes}
                    if debrid_episodes
                    else {}
                ),
            )
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        except (ValueError, households.HouseholdError):
            raise HTTPException(404, "Apple TV title unavailable", headers=NO_STORE) from None
        if mode == "realdebrid":
            candidates = [item for item in reply["items"] if item.get("watch_path")]
            next_episode = next(
                (item for item in candidates if item.get("watched") is not True),
                candidates[0] if candidates else None,
            )
            if next_episode:
                schedule_path_warm(scope, next_episode["watch_path"])
        if kind == "anime" and any(item.get("episode_artwork") for item in reply["items"]):
            try:
                first_path = next(
                    item["episode_artwork"]
                    for item in reply["items"]
                    if item.get("episode_artwork")
                )
                _, _, monitor, season, episode = first_path.split("/")
                identity = episode_image_identity(
                    db, scope, "anime", int(monitor), int(season), int(episode)
                )
                async with asyncio.timeout(2.5), artwork_slots:
                    metadata = await anime_series_metadata(db, identity)
            except (EpisodeArtworkUnavailable, TimeoutError):
                metadata = {}
            _, _, scope = requested_scope(request, db)
            if anime_artwork_id(db, scope, monitor) is None:
                raise HTTPException(404, "Apple TV title unavailable", headers=NO_STORE)
            for item in reply["items"]:
                details = metadata.get(int(item["id"]), {})
                title = details.get("title")
                if title:
                    item["title"] = title
                if details.get("summary"):
                    item["summary"] = details["summary"]
                if details.get("duration_seconds"):
                    item["duration_seconds"] = details["duration_seconds"]
        return JSONResponse(reply, headers=NO_STORE)

    @router.get(LIBRARY_API + "/episode-artwork/{kind}/{title_id}/{season}/{episode}")
    async def library_episode_artwork(
        request: Request,
        kind: Literal["anime", "tv"],
        title_id: int,
        season: int,
        episode: int,
        db=Depends(get_db),
    ):
        _, _, scope = requested_scope(request, db)
        try:
            identity = episode_image_identity(db, scope, kind, title_id, season, episode)
            async with asyncio.timeout(10), artwork_slots:
                data, media_type = await episode_artwork(db, identity)
        except (EpisodeArtworkUnavailable, households.HouseholdError, TimeoutError):
            raise HTTPException(404, "Artwork unavailable", headers=NO_STORE) from None
        _, _, scope = requested_scope(request, db)
        try:
            if episode_image_identity(db, scope, kind, title_id, season, episode) != identity:
                raise EpisodeArtworkUnavailable()
        except (EpisodeArtworkUnavailable, households.HouseholdError):
            raise HTTPException(404, "Artwork unavailable", headers=NO_STORE) from None
        return Response(data, media_type=media_type, headers=NO_STORE)

    @router.post(LIBRARY_API + "/diagnostic")
    async def library_diagnostic(request: Request, db=Depends(get_db)):
        _, _, scope = requested_scope(request, db)
        try:
            payload = await request.json()
        except ValueError:
            payload = {}
        message = payload.get("message") if isinstance(payload, dict) else ""
        if not isinstance(message, str):
            message = ""
        _LOG.warning("Apple TV playback diagnostic actor=%s: %s", scope.viewer_id, message[:1000])
        return JSONResponse({"ok": True}, headers=NO_STORE)

    @router.get(LIBRARY_API + "/details")
    async def library_details(
        request: Request, path: str = Query("", max_length=512), db=Depends(get_db)
    ):
        _, _, scope = requested_scope(request, db)
        try:
            payload = await details_for_path(db, scope, path)
        except (ValueError, KeyError, TypeError):
            raise HTTPException(404, "Details unavailable", headers=NO_STORE) from None
        except PlaybackSourceError:
            raise HTTPException(404, "Details unavailable", headers=NO_STORE) from None
        return JSONResponse(payload, headers=NO_STORE)

    @router.post(LIBRARY_API + "/play")
    async def play_library(request: Request, db=Depends(get_db)):
        key, library, scope = requested_scope(request, db)
        try:
            body = await request.json()
            path = body.get("watch_path") if isinstance(body, dict) else None
        except (ValueError, UnicodeError):
            path = None
        if not isinstance(path, str):
            raise HTTPException(400, "Choose a title", headers=NO_STORE)
        release = acquire("start", library.actor)
        try:
            if path.startswith(CLOUD):
                return await open_cloud_pass(
                    request, db, key, library, scope, path.removeprefix(CLOUD)
                )
            return await open_library_pass(request, db, key, library, scope, path)
        finally:
            release()

    async def open_cloud_pass(request, db, key, library, scope, file_id):
        token = cloud_token(db, scope)
        try:
            async with asyncio.timeout(60):
                source = await cloud_library.source(token, file_id)
                ttl = min(
                    int(library.expires - clock()),
                    LIBRARY_MEDIA_TTL,
                    max(PASS_TTL, int(source.duration) + 600),
                )
                if ttl < 1:
                    raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
                media_id, cookie = await cloud_streams.issue(source, library.actor, key, ttl)
        except DebridLibraryError as error:
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        except TimeoutError:
            raise HTTPException(
                504, "Real-Debrid took too long to open this file", headers=NO_STORE
            ) from None
        try:
            active_library(request, db)
            check_cloud(db, cloud_streams.for_client(media_id, key))
        except HTTPException:
            cloud_streams.passes.pop(media_id, None)
            raise
        return JSONResponse(
            {
                "title": source.title,
                "media_id": media_id,
                "media_token": cookie,
                "media_format": "hls",
                "media_playlist": "vod",
                "expires_in": ttl,
                "duration_seconds": source.duration,
                "position_seconds": 0,
                "access_check": True,
                "identity": {
                    "library_id": scope.library_id,
                    "domain": "cloud",
                    "canonical_id": source.identity,
                    "season_number": 0,
                    "episode_number": 0,
                    "media_version": source.media_version,
                },
            },
            headers=NO_STORE,
        )

    @router.post(LIBRARY_API + "/media/{media_id}/next")
    async def play_next(request: Request, media_id: str, db=Depends(get_db)):
        key, library, _ = active_library(request, db)
        current = passes.get(media_id)
        if current is None or current.library_key != key:
            raise HTTPException(404, "Apple TV media unavailable", headers=NO_STORE)
        release = acquire("start", library.actor)
        try:
            checked(db, current)
            path = next_watch_path(db, current.source, anime_monitor_id=current.anime_monitor_id)
            if path is None:
                raise HTTPException(404, "Next episode unavailable", headers=NO_STORE)
            # A retry may replace an abandoned successor. Keep the current pass until
            # the client has opened the new item and explicitly closes the old one.
            for sibling_id, sibling in tuple(passes.items()):
                if sibling.library_key == key and sibling_id != media_id:
                    cleanup = drop_pass(sibling_id)
                    if cleanup is not None:
                        await asyncio.shield(cleanup)
            return await open_library_pass(
                request, db, key, library, current.source._scope, path, previous=(media_id, current)
            )
        finally:
            release()

    async def open_library_pass(request, db, key, library, scope, path, *, previous=None):
        prepared = None
        cloud_error = None
        resolved = None
        mode = playback_provider(db, scope.viewer_id) if scope.kind == "personal" else "plex"
        if mode != "plex":
            try:
                async with asyncio.timeout(60):
                    candidate = await debrid_source(db, scope, path)
                    if candidate:
                        prepared = await cloud_streams.prepare(candidate[0].cloud, wait_first=False)
                        resolved = candidate
            except (DebridLibraryError, TimeoutError, households.HouseholdError) as error:
                cloud_error = error
        if mode == "realdebrid" and resolved is None:
            if isinstance(cloud_error, DebridLibraryError):
                raise HTTPException(
                    cloud_error.status, str(cloud_error), headers=NO_STORE
                ) from None
            if isinstance(cloud_error, TimeoutError):
                raise HTTPException(
                    504, "Real-Debrid took too long to open this file", headers=NO_STORE
                )
            raise HTTPException(
                404, "No matching Real-Debrid file for this title", headers=NO_STORE
            )
        try:
            if resolved:
                source, title = resolved
            else:
                async with asyncio.timeout(12):
                    source, title = await exact_source(db, scope, path)
        except (ValueError, TimeoutError, households.HouseholdError) as error:
            if isinstance(cloud_error, DebridLibraryError):
                raise HTTPException(
                    cloud_error.status, str(cloud_error), headers=NO_STORE
                ) from None
            if isinstance(error, PlaybackSourceError) and error.code == "viewer_plex_required":
                raise HTTPException(403, "viewer_plex_required", headers=NO_STORE) from None
            raise HTTPException(404, "That exact copy is unavailable", headers=NO_STORE) from None
        active_library(request, db)
        media_ttl = min(LIBRARY_MEDIA_TTL, max(PASS_TTL, int(source.duration_ms / 1000) + 600))
        ttl = min(media_ttl, int(library.expires - clock()))
        if ttl < 1:
            raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
        hls = prepared is None and native_hls_eligible(source)
        compatible = prepared is None and not hls and native_compatible_eligible(source)
        try:
            grant = test_pass(
                source,
                title,
                library.actor,
                library.browser_session,
                ttl,
                key,
                hls=bool(prepared) or hls or compatible,
            )
        except ValueError as error:
            raise HTTPException(415, str(error), headers=NO_STORE) from None
        grant.hls = prepared
        _LOG.warning(
            "library play title=%s handler=%s prepared=%s hls=%s compatible=%s source=%s/%s",
            title,
            type(prepared).__name__ if prepared is not None else "native",
            prepared is not None,
            hls,
            compatible,
            getattr(source, "video_codec", "?"),
            getattr(source, "audio_codec", "?"),
        )
        checked(db, grant)
        if match := ANIME.fullmatch(path):
            grant.anime_monitor_id = int(match[1])
        if previous is None:
            for media_id, old in tuple(passes.items()):
                if old.library_key == key:
                    if cleanup := drop_pass(media_id):
                        await asyncio.shield(cleanup)
        try:
            if hls or compatible:
                await prepare_hls(db, grant, compatible=compatible)
            _, refreshed, _ = active_library(request, db)
            checked(db, grant)
            ttl = min(media_ttl, int(refreshed.expires - clock()))
            if ttl < 1:
                raise HTTPException(401, "Apple TV library link expired", headers=NO_STORE)
            if previous is not None:
                media_id, current = previous
                if passes.get(media_id) is not current:
                    raise HTTPException(404, "Apple TV media unavailable", headers=NO_STORE)
                checked(db, current)
            next_path = next_watch_path(db, source, anime_monitor_id=grant.anime_monitor_id)
            return media_reply(db, grant, ttl, next_path=next_path)
        except BaseException:
            if grant.hls is not None:
                await close_hls(grant.hls)
            raise

    async def prepare_hls(db, grant, *, compatible=False):
        if hls_slots.locked():
            raise HTTPException(
                429, "Apple TV streaming is busy. Try again shortly.", headers=NO_STORE
            )
        await hls_slots.acquire()

        def authorize_feed():
            try:
                _guard()
                with hls_database() as connection:
                    checked(connection, grant)
            except HTTPException as error:
                raise DeliveryError("Apple TV pass revoked", error.status_code) from None
            except sqlite3.Error:
                raise DeliveryError("Apple TV stream unavailable", 503) from None

        job = None
        try:
            if compatible:
                job = NativeCompatibleStream(grant.source, transport)

                async def authorize_compatible():
                    authorize_feed()

                await job.ready(authorize_compatible)
            else:
                job = NativeHLS(grant.source, authorize=authorize_feed, transport=transport)
                await job.ready()
            checked(db, grant)
            grant.hls = job
            grant.last_source_check = clock()
        except BaseException as error:
            if job is None:
                hls_slots.release()
            else:
                await close_hls(job)
            if isinstance(error, (DeliveryError, OSError)):
                status = error.status if isinstance(error, DeliveryError) else 503
                raise HTTPException(
                    status, "Couldn’t prepare this copy.", headers=NO_STORE
                ) from None
            raise

    @router.post(LIBRARY_API + "/unlink")
    async def unlink_library(request: Request, db=Depends(get_db)):
        # A device can surrender only the credential it presents. A retry must
        # also succeed if the first reply was lost or the web account revoked it.
        key = library_key(request)
        prune()
        devices.revoke_credential(db, key)
        libraries.pop(key, None)
        for media_id, previous in tuple(passes.items()):
            if previous.library_key == key:
                drop_pass(media_id)
        return JSONResponse({"unlinked": True}, headers=NO_STORE)

    @router.post(LIBRARY_API + "/media/{media_id}/close")
    async def close_library_media(request: Request, media_id: str, db=Depends(get_db)):
        key, _, _ = active_library(request, db)
        if cloud_streams.for_client(media_id, key) is not None:
            cloud_streams.passes.pop(media_id, None)
        grant = passes.get(media_id)
        if grant is not None and grant.library_key == key:
            drop_pass(media_id)
        return JSONResponse({"closed": True}, headers=NO_STORE)

    def checked_library_media(db, key, media_id):
        grant = passes.get(media_id)
        if grant is None or grant.library_key != key:
            raise HTTPException(404, "Apple TV media unavailable", headers=NO_STORE)
        checked(db, grant)
        return grant

    @router.get(LIBRARY_API + "/media/{media_id}/access")
    async def check_library_media(request: Request, media_id: str, db=Depends(get_db)):
        key, _, _ = active_library(request, db)
        if entry := cloud_streams.for_client(media_id, key):
            check_cloud(db, entry)
        else:
            checked_library_media(db, key, media_id)
        return JSONResponse({"allowed": True}, headers=NO_STORE)

    @router.post(LIBRARY_API + "/media/{media_id}/resume")
    async def save_resume(
        request: Request, media_id: str, body: ResumeCheckpoint, db=Depends(get_db)
    ):
        key, library, _ = active_library(request, db)
        grant = checked_library_media(db, key, media_id)
        publication = getattr(grant.source, "source_identity", "") or ""
        if not re.fullmatch(r"[a-f0-9]{64}", publication):
            raise HTTPException(404, "Resume unavailable for this copy", headers=NO_STORE)
        if body.position_seconds > grant.target.duration_seconds:
            raise HTTPException(422, "Position is outside this copy", headers=NO_STORE)
        receipt = grant.resume_receipt
        if receipt is not None and body.seq <= receipt[0]:
            if (body.seq, body.position_seconds) != receipt[:2]:
                raise HTTPException(409, "Stale resume checkpoint", headers=NO_STORE)
            return JSONResponse({"saved_at": receipt[2]}, headers=NO_STORE)
        now = clock()
        if grant.resume_last_save is not None and now - grant.resume_last_save < 0.25:
            raise HTTPException(429, "Try again shortly", headers=NO_STORE)
        try:
            with fresh_source_transaction(db):
                library_scope(db, library)
                checked(db, grant)
                saved = store.save_native_resume(
                    db,
                    target=grant.target,
                    position=body.position_seconds,
                    publication=publication,
                    expected=grant.resume_cursor,
                    barrier=grant.resume_barrier,
                )
        except store.PlaybackStoreError:
            raise HTTPException(409, "Playback position changed", headers=NO_STORE) from None
        grant.resume_cursor = (saved["revision"], saved["updated_at"])
        grant.resume_receipt = (body.seq, body.position_seconds, saved["updated_at"])
        grant.resume_last_save = now
        if not grant.watch_credited:
            credited = _credit_native_watch(
                db, grant, position=body.position_seconds, observed_at=saved["updated_at"]
            )
            if credited is not None:
                grant.watch_credited = True
                if (
                    credited.outbox_id is not None
                    and os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").lower() != "true"
                ):
                    from omakase.plus.watch.anilist_sync import process_tracker_outbox

                    try:
                        await asyncio.to_thread(
                            process_tracker_outbox, db, outbox_id=credited.outbox_id
                        )
                    except Exception:
                        _LOG.warning("Apple TV tracker sync failed", exc_info=True)
        duration = grant.target.duration_seconds
        if duration and body.position_seconds >= max(duration - 300, duration * 0.9):
            schedule_preload(key, grant)
        return JSONResponse({"saved_at": saved["updated_at"]}, headers=NO_STORE)

    @router.api_route(API + "/media/{media_id}", methods=["GET", "HEAD"])
    async def media(request: Request, media_id: str, db=Depends(get_db)):
        return await deliver_media(request, media_id, None, db)

    @router.api_route(API + "/media/{media_id}/{asset}", methods=["GET", "HEAD"])
    async def media_asset(request: Request, media_id: str, asset: str, db=Depends(get_db)):
        return await deliver_media(request, media_id, asset, db)

    async def deliver_media(request, media_id, asset, db):
        _https(request)
        prune()
        grant = passes.get(media_id)
        token = request.cookies.get(COOKIE, "")
        if entry := cloud_streams.for_cookie(media_id, token):
            if not asset:
                raise HTTPException(404, "Cloud stream unavailable", headers=NO_STORE)

            def authorize_cloud():
                check_cloud(db, cloud_streams.for_client(media_id, entry.binding))

            try:
                return await cloud_streams.deliver(entry, asset, request, authorize_cloud)
            except DebridLibraryError as error:
                raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        if (
            grant is None
            or bool(asset) != bool(grant.hls)
            or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
            or not secrets.compare_digest(grant.token_digest, _digest(token))
        ):
            raise HTTPException(404, "Apple TV media unavailable", headers=NO_STORE)
        checked(db, grant)
        if grant.hls:
            try:
                release = await admission.acquire_buffered_read(grant.target.user_id, media_id)
            except Busy as error:
                raise admission_rejected(error) from None
        else:
            release = acquire("read", grant.target.user_id, media_id)

        async def authorize():
            _guard()
            if passes.get(media_id) is not grant:
                raise DeliveryError("Apple TV pass revoked", 404)
            checked(db, grant)
            if grant.hls and clock() - grant.last_source_check < 15:
                return
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
            grant.last_source_check = clock()

        try:
            if passes.get(media_id) is not grant:
                raise DeliveryError("Apple TV pass revoked", 404)
            _LOG.warning(
                "library media asset=%s handler=%s",
                asset,
                type(grant.hls).__name__ if grant.hls is not None else "source",
            )
            if isinstance(grant.hls, DebridMedia):

                def authorize_debrid():
                    _guard()
                    if passes.get(media_id) is not grant:
                        raise DeliveryError("Apple TV pass revoked", 404)
                    checked(db, grant)

                try:
                    return await cloud_streams.deliver(grant.hls, asset, request, authorize_debrid)
                except DebridLibraryError as error:
                    raise DeliveryError(str(error), error.status) from None
                finally:
                    release()
            if isinstance(grant.hls, NativeCompatibleStream):
                if playlist_asset := await grant.hls.playlist_asset(asset, authorize):
                    await authorize()
                    release()
                    body, media_type = playlist_asset
                    return Response(body, media_type=media_type, headers=NO_STORE)
                return await grant.hls.segment(
                    asset,
                    authorize,
                    release,
                    lambda: acquire("seek", grant.target.user_id, media_id),
                )
            if grant.hls:
                if playlist_asset := grant.hls.playlist_asset(asset):
                    await authorize()
                    release()
                    body, media_type = playlist_asset
                    return Response(body, media_type=media_type, headers=NO_STORE)
                return await open_local(
                    grant.hls.path(asset),
                    request,
                    authorize=authorize,
                    etag_seed=grant.target.media_version,
                    on_close=release,
                )
            return await open_part(
                grant.source, request, authorize=authorize, transport=transport, on_close=release
            )
        except DeliveryError as error:
            release()
            raise HTTPException(error.status, str(error), headers=NO_STORE) from None
        except FileNotFoundError:
            release()
            raise HTTPException(404, "Apple TV media unavailable", headers=NO_STORE) from None
        except BaseException:
            release()
            raise

    return router
