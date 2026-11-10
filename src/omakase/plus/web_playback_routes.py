"""Authenticated player pages and exact-source session/byte capabilities."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from omakase.plus import households
from omakase.plus import web_playback_compatible as compatibility
from omakase.plus import web_playback_remux as remux
from omakase.plus import web_playback_store as store
from omakase.plus.auth import authenticate_session
from omakase.plus.completion_cleanup import process_deferred_completion_cleanup
from omakase.plus.deps import get_db
from omakase.plus.middleware import require_user
from omakase.plus.playback_proof import _insert_binding
from omakase.plus.request_limits import bounded_form_route
from omakase.plus.web_playback_admission import Admission, Busy
from omakase.plus.web_playback_catalog import (
    catalog_target,
    personal_scope,
    resolve_catalog_source,
    revalidate_catalog_source,
    revalidate_remote_catalog_source,
)
from omakase.plus.web_playback_context import normalize_skip_segments, watch_context
from omakase.plus.web_playback_delivery import HEADERS, DeliveryError, open_local, open_part
from omakase.plus.web_playback_navigation import enabled, exact_next_url
from omakase.plus.web_playback_source import (
    PlaybackSourceError,
    fresh_source_transaction,
    public_projection,
    resolve_anime_source,
    revalidate_remote_source,
    revalidate_source,
)
from omakase.plus.web_playback_watch import observe_playback
from omakase.plus.web_security import require_csrf

_LOG = logging.getLogger(__name__)
_CODEC_ERRORS = {"unsupported_browser", "unsupported_browser_audio", "unsupported_codec"}
_FORMAT_ERRORS = {
    "unsupported_source",
    "stream_capacity",
    "unsupported_multi_audio",
    "unsupported_subtitle_codec",
    "ambiguous_track_inventory",
    "styled_subtitles_unsupported",
}
_STREAM_TIMEOUT = 10
_SUBTITLE_LIMIT = 8 * 1024**2
_SUBTITLE_LEAD_SECONDS = 15
_ASS_DIALOGUE = re.compile(rb"(?m)^Dialogue: [^,]*,(\d+):(\d{2}):(\d{2}\.\d{2}),")


def _failure(code, status, detail, *, source=None, retry_after=None):
    headers = HEADERS if retry_after is None else HEADERS | {"Retry-After": str(retry_after)}
    data = {"detail": detail, "code": code}
    if source is not None:
        data["source"] = source
    return JSONResponse(data, status_code=status, headers=headers)


def _remux_failure(error, source):
    code = error.code
    _LOG.warning("playback_start_failed reason=%s", code)
    if code in _CODEC_ERRORS:
        return _failure(
            code, 415, "This browser cannot decode the selected video or audio.", source=source
        )
    if code == "stream_capacity":
        return _failure(
            code, 415, "This copy needs more room than the player can reserve.", source=source
        )
    if code in _FORMAT_ERRORS:
        return _failure(
            code, 415, "This media format is unavailable in the web player.", source=source
        )
    if code == "authorization_changed":
        return _failure(code, 409, "Episode access changed during preparation.", source=source)
    return _failure(
        code,
        503,
        "The episode could not be prepared. Try again shortly.",
        source=source,
        retry_after=2,
    )


async def _stderr_tail(process, tail):
    while chunk := await process.stderr.read(1024):
        tail.extend(chunk)
        del tail[:-4096]


def _worker_diagnostics(phase, reason, process, tail):
    # ffmpeg can echo input URLs, headers and paths. Keep the bounded tail in
    # memory and emit only allowlisted, constant categories from that tail.
    raw = bytes(tail).lower()
    categories = [
        label
        for marker, label in (
            (b"401 unauthorized", "upstream_401"),
            (b"403 forbidden", "upstream_403"),
            (b"404 not found", "upstream_404"),
            (b"connection refused", "connection_refused"),
            (b"timed out", "io_timeout"),
            (b"no space left", "disk_full"),
            (b"permission denied", "permission_denied"),
            (b"invalid data", "invalid_input"),
            (b"stream map", "track_missing"),
            (b"error opening", "open_failed"),
            (b"error writing", "write_failed"),
        )
        if marker in raw
    ]
    _LOG.warning(
        "playback_worker phase=%s reason=%s return_code=%s stderr_categories=%s stderr_tail_bytes=%d",
        phase,
        reason,
        process.returncode,
        ",".join(categories) or "none",
        len(raw),
    )


def feature_enabled():
    return enabled()


def _enabled():
    if not feature_enabled():
        raise HTTPException(404, "Player unavailable")


class StartOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media_version: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    codecs: list[Literal["h264", "hevc", "av1", "aac", "opus", "ac3", "eac3"]] = Field(
        default_factory=list, max_length=8
    )
    ass_supported: StrictBool = False
    defer_prepare: StrictBool = False
    resume: StrictBool = True


class Start(StartOptions):
    monitor_id: StrictInt = Field(gt=0, le=2**63 - 1)
    episode_number: StrictInt = Field(ge=0, le=5000)
    library_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    prefer_on_demand: StrictBool = True


class MovieTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["movie"] = "movie"
    imdb_id: str = Field(pattern=r"^tt[0-9]{7,10}$")


class TVTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain: Literal["tv"] = "tv"
    tmdb_id: StrictInt = Field(gt=0, le=2147483647)
    season_number: StrictInt = Field(ge=0, le=200)
    episode_number: StrictInt = Field(gt=0, le=1000)


class CatalogStart(StartOptions):
    target: Annotated[MovieTarget | TVTarget, Field(discriminator="domain")]


def _catalog_key(target):
    if isinstance(target, MovieTarget):
        return target.domain, target.imdb_id, 0, 0
    return target.domain, str(target.tmdb_id), target.season_number, target.episode_number


def _tv_watch_url(tmdb_id, season, episode):
    return f"/plus/watch/title/tv/{tmdb_id}/{season}/{episode}?play=1"


def _catalog_neighbor(db, scope, target, offset):
    if not isinstance(target, TVTarget):
        return None
    number = target.episode_number + offset
    if not 1 <= number <= 1000:
        return None
    try:
        catalog_target(db, scope, "tv", str(target.tmdb_id), target.season_number, number)
    except PlaybackSourceError:
        return None
    return _tv_watch_url(target.tmdb_id, target.season_number, number)


class Prepare(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CompatibilitySeek(BaseModel):
    model_config = ConfigDict(extra="forbid")
    position_seconds: float = Field(strict=True, ge=0, le=86400)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)
    seq: StrictInt = Field(gt=0, le=1000000)
    position_seconds: float = Field(ge=0, le=43200)
    playing: StrictBool
    event: str = Field(pattern=r"^(progress|play|pause|seek|ended)$")
    playback_rate: float = Field(ge=0.25, le=2)
    audio_id: str | None = Field(default=None, max_length=128)
    text_id: str | None = Field(default=None, max_length=128)


class Close(BaseModel):
    model_config = ConfigDict(extra="forbid")
    final_event: Event | None = None


def _binding(request):
    return hashlib.sha256(request.cookies.get("omakase_session", "").encode()).hexdigest()


def _snapshot(source):
    scope = source._scope
    return {
        "membership": {
            "library_id": scope.library_id,
            "viewer_id": scope.viewer_id,
            "revision": scope.membership_revision,
        },
        "policy": {"revision": scope.policy_revision},
        "source": {
            "media_version": source.media_version,
            "identity": source.source_identity,
            "server": source.server_id,
            "account": source.account_id,
            "credential_generation": hashlib.sha256(source.token.encode()).hexdigest(),
        },
    }


def _monitor(db, user_id, monitor_id, library_id=None, *, prepare=False):
    if library_id:
        scope = households.resolve_scope(db, viewer_id=user_id, library_id=library_id)
    else:
        row = db.execute(
            "SELECT library_id FROM personal_libraries WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Personal library unavailable")
        scope = households.resolve_scope(db, viewer_id=user_id, library_id=row[0])
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE id=? AND user_id=?",
        (monitor_id, scope.operational_owner_id),
    ).fetchone()
    if monitor is None:
        raise HTTPException(404, "Episode unavailable in this library")
    if scope.kind == "personal" and prepare:
        # An explicit Play in the viewer's own library can materialize their
        # already-owned legacy monitor. Shared catalog additions cannot do this.
        households.LibraryRepository(db, scope).add(
            domain="anime",
            canonical_id=str(monitor["anilist_id"]),
            title=monitor["title"],
            season=monitor["season_number"] or 0,
            legacy_id=str(monitor["id"]),
        )
    if scope.kind == "household":
        match = db.execute(
            """SELECT 1 FROM library_items i JOIN library_resources r
            ON r.id=i.resource_id WHERE i.library_id=? AND i.state='active'
            AND r.owner_user_id=? AND r.domain='anime' AND r.canonical_id=?
            AND r.season_number=? AND r.legacy_id=?""",
            (
                scope.library_id,
                scope.operational_owner_id,
                str(monitor["anilist_id"]),
                monitor["season_number"] or 0,
                str(monitor["id"]),
            ),
        ).fetchone()
        if match is None:
            raise HTTPException(404, "Episode unavailable in this library")
    return monitor, scope


def create_player_router(
    templates,
    product_context,
    *,
    resolver=None,
    source_guard=None,
    remote_guard=None,
    transport=None,
    remux_prepare=None,
    resource_getter=None,
    stream_launcher=None,
    cache_root=None,
):
    resolver = resolver or resolve_anime_source
    anime_source_guard = source_guard or revalidate_source
    anime_remote_guard = remote_guard or revalidate_remote_source

    def source_guard(db, source):
        if getattr(source, "domain", "anime") in {"movie", "tv"}:
            return revalidate_catalog_source(db, source)
        return anime_source_guard(db, source)

    def remote_guard(db, source, transport=None):
        if getattr(source, "domain", "anime") in {"movie", "tv"}:
            return revalidate_remote_catalog_source(db, source, transport)
        return anime_remote_guard(db, source, transport=transport)

    remux_prepare = remux_prepare or remux.prepare_remux
    resource_getter = resource_getter or remux.get_resource
    cache_root = Path(
        cache_root or os.getenv("OMAKASE_PLAYBACK_CACHE_ROOT", "/run/omakase-playback")
    )
    streaming_enabled = os.getenv("OMAKASE_PLAYBACK_STREAMING", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    stream_cache_root = Path(
        os.getenv(
            "OMAKASE_PLAYBACK_STREAM_CACHE_ROOT",
            "/home/omakase/data/playback-streams",
        )
    )
    try:
        stream_cache_max_bytes = int(
            os.getenv("OMAKASE_PLAYBACK_STREAM_CACHE_MAX_BYTES", str(6 * 1024**3))
        )
    except ValueError:
        stream_cache_max_bytes = 6 * 1024**3
    router = APIRouter(
        route_class=bounded_form_route(
            4096, "Playback request too large", guard=_enabled, forward_disconnect=True
        )
    )
    # Process-local delivery handles contain provider credentials. Restarting a
    # worker requires a fresh resolution; persistent personal resume is retained.
    handles = {}
    admission = Admission()
    terminal_saves = {}

    def admit(kind, user_id, sid=None):
        try:
            return admission.acquire(kind, user_id, sid)
        except Busy as error:
            raise HTTPException(
                429, str(error), headers={"Retry-After": str(error.retry_after)}
            ) from None

    def held_versions(db):
        return {row["media_version"] for row in store.active_media_holds(db)} | set(
            remux.held_media_versions(db)
        )

    def _stop_process(process):
        if process is None:
            return
        try:
            if process.returncode is None:
                process.terminate()
        except ProcessLookupError:
            pass

    def cleanup_stream(item):
        if not item:
            return
        if stream := item.pop("compatible_stream", None):
            asyncio.create_task(stream.close())
        _stop_process(item.get("stream_process"))
        _stop_process(item.get("subtitle_process"))
        if task := item.get("subtitle_task"):
            task.cancel()
        folder = item.pop("stream_dir", None)
        item.pop("stream_subtitle", None)
        item.pop("subtitle_track", None)
        item.pop("subtitle_preview_bytes", None)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)

    async def stop_stream(item):
        if stream := item.pop("compatible_stream", None):
            await stream.close()
        if task := item.pop("subtitle_task", None):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        processes = [item.get("stream_process"), item.get("subtitle_process")]
        _stop_process(processes[0])
        _stop_process(processes[1])
        for process in processes:
            if process is None or process.returncode is not None:
                continue
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if reader := item.pop("stream_stderr_task", None):
            await reader
        cleanup_stream(item)

    def prune_stream_cache():
        active = {
            item.get("stream_dir")
            for item in handles.values()
            if item.get("stream_dir") is not None
        }
        try:
            stream_cache_root.mkdir(parents=True, exist_ok=True)
            folders = [folder for folder in stream_cache_root.iterdir() if folder.is_dir()]
        except OSError:
            return None
        entries = []
        total = 0
        for folder in folders:
            size = 0
            try:
                for path in folder.iterdir():
                    if path.is_file():
                        size += path.stat().st_size
            except OSError:
                continue
            entries.append((folder, size))
            total += size
        for folder, size in sorted(entries, key=lambda entry: entry[0].stat().st_mtime):
            if total <= stream_cache_max_bytes:
                break
            if folder in active:
                continue
            try:
                shutil.rmtree(folder)
            except OSError:
                continue
            total -= size
        return total, dict(entries)

    def stream_checks(source, body):
        if source.container != "mkv":
            raise remux.RemuxError("unsupported_source")
        if source.video_codec not in body.codecs:
            raise remux.RemuxError("unsupported_browser")
        audio = next((track for track in source.streams if track.kind == "audio"), None)
        if audio is None or audio.codec not in body.codecs:
            raise remux.RemuxError("unsupported_browser_audio")

    def stream_budget(source):
        # MPEG-TS framing and per-segment headers add overhead to copied streams.
        return (source.part_size * 5) // 4 + 16 * 1024**2

    def ffmpeg_input(source):
        return [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-headers",
            f"X-Plex-Token: {source.token}\r\n",
            "-i",
            source.base_url + source.part_key,
        ]

    async def spawn_stream(source, sid, item):
        inventory = prune_stream_cache()
        try:
            stream_cache_root.mkdir(parents=True, exist_ok=True)
            if getattr(source, "domain", "anime") != "anime":
                if inventory is None:
                    raise remux.RemuxError("stream_cache_unavailable")
                cached, sizes = inventory
                outstanding = sum(
                    max(0, candidate["stream_budget"] - sizes.get(candidate.get("stream_dir"), 0))
                    for candidate in handles.values()
                    if candidate is not item
                    and "stream_budget" in candidate
                    and (
                        candidate.get("stream_process") is None
                        or candidate["stream_process"].returncode is None
                    )
                )
                required = outstanding + stream_budget(source)
                free = shutil.disk_usage(stream_cache_root).free
                if required > min(stream_cache_max_bytes - cached, free - 512 * 1024**2):
                    raise remux.RemuxError("stream_capacity")
            item["stream_budget"] = stream_budget(source)
            folder = item.get("stream_dir")
            if folder is None:
                folder = stream_cache_root / sid
                folder.mkdir(mode=0o700)
                item["stream_dir"] = folder
        except OSError:
            raise remux.RemuxError("stream_cache_unavailable") from None
        prepare_stream_subtitle(source, sid, item, codecs={"ass", "ssa"})
        base = ffmpeg_input(source)
        playlist = folder / "index.m3u8"
        command = base + [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_playlist_type",
            "event",
            "-hls_flags",
            "independent_segments+temp_file",
            "-hls_list_size",
            "0",
            "-hls_base_url",
            "segments/",
            "-hls_segment_filename",
            str(folder / "seg-%05d.ts"),
            str(playlist),
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
            )
        except OSError:
            raise remux.RemuxError("stream_worker_unavailable") from None
        item.update(
            streaming=True, stream_dir=folder, stream_playlist=playlist, stream_process=process
        )
        tail = bytearray()
        reader = asyncio.create_task(_stderr_tail(process, tail))
        item["stream_stderr_task"] = reader
        deadline = time.monotonic() + _STREAM_TIMEOUT
        while not (playlist.is_file() and (folder / "seg-00000.ts").is_file()):
            if process.returncode is not None:
                await reader
                _worker_diagnostics("hls", "stream_start_failed", process, tail)
                raise remux.RemuxError("stream_start_failed")
            if time.monotonic() >= deadline:
                _worker_diagnostics("hls", "stream_start_timeout", process, tail)
                raise remux.RemuxError("stream_start_timeout")
            await asyncio.sleep(0.1)

    def prepare_stream_subtitle(source, sid, item, *, codecs):
        if not item["allow_subtitles"] or "subtitle_task" in item:
            return
        subtitles = [track for track in source.streams if track.kind == "subtitle"]
        candidates = [
            (index, track) for index, track in enumerate(subtitles) if track.codec in codecs
        ]
        if not candidates:
            return
        anime = getattr(source, "domain", "anime") in {"anime", "anime-film"}
        index, track = max(
            candidates,
            key=lambda pair: (
                anime and not pair[1].forced,
                anime and pair[1].language.lower().split("-")[0] in {"en", "eng"},
                pair[1].selected,
                pair[1].default,
            ),
        )
        folder = item.get("stream_dir")
        if folder is None:
            folder = stream_cache_root / sid
            try:
                stream_cache_root.mkdir(parents=True, exist_ok=True)
                folder.mkdir(mode=0o700)
            except OSError:
                return
            item["stream_dir"] = folder
        item["stream_subtitle"] = folder / "subtitle.ass"
        item["subtitle_track"] = {
            "plex_stream_id": track.stream_id,
            "codec": "ass" if track.codec == "srt" else track.codec,
            "language": track.language,
            "default": track.default,
            "forced": track.forced,
            "selected": track.selected,
        }
        codec = "ass" if track.codec == "srt" else "copy"
        item["subtitle_task"] = asyncio.create_task(
            extract_subtitle(ffmpeg_input(source), item, index, codec)
        )

    async def extract_subtitle(base, item, index, codec):
        subtitle = item["stream_subtitle"]
        command = base + [
            "-map",
            f"0:s:{index}",
            "-c",
            codec,
            "-flush_packets",
            "1",
            "-f",
            "ass",
            "pipe:1",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except OSError:
            _LOG.warning("playback_worker phase=ass reason=subtitle_worker_unavailable")
            return False
        item["subtitle_process"] = process
        tail = bytearray()
        reader = asyncio.create_task(_stderr_tail(process, tail))
        try:
            async with asyncio.timeout(120):
                size = 0
                pending = b""
                preview_until = item["subtitle_preview_until"]
                with subtitle.open("wb", buffering=0) as script:
                    while chunk := await process.stdout.read(65536):
                        size += len(chunk)
                        if size >= _SUBTITLE_LIMIT:
                            return False
                        script.write(chunk)
                        if "subtitle_preview_bytes" not in item:
                            lines, separator, pending = (pending + chunk).rpartition(b"\n")
                            if separator:
                                for cue in _ASS_DIALOGUE.finditer(lines):
                                    hours, minutes, seconds = map(float, cue.groups())
                                    if hours * 3600 + minutes * 60 + seconds >= preview_until:
                                        item["subtitle_preview_bytes"] = size - len(pending)
                                        break
                await process.wait()
        except TimeoutError:
            _worker_diagnostics("ass", "subtitle_timeout", process, tail)
            return False
        except OSError:
            _LOG.warning("playback_worker phase=ass reason=subtitle_output_unavailable")
            return False
        finally:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # A cancelled or size-limited reader may leave the stdout pipe paused.
            await process.stdout.read()
            await reader
            await process.wait()
        if (
            process.returncode != 0
            or not subtitle.is_file()
            or not 0 < subtitle.stat().st_size < _SUBTITLE_LIMIT
        ):
            _worker_diagnostics("ass", "subtitle_failed", process, tail)
            return False
        return True

    async def start_stream(source, sid, item):
        launcher = stream_launcher or spawn_stream
        for retry in (False, True):
            try:
                await launcher(source, sid, item)
                return
            except remux.RemuxError as error:
                if error.code != "stream_start_timeout" or retry:
                    raise
                await stop_stream(item)

    def next_url(db, item):
        if getattr(item["source"], "domain", "anime") == "anime-film":
            return None
        if item["catalog_target"] is not None:
            return _catalog_neighbor(db, item["source"]._scope, item["catalog_target"], 1)
        return exact_next_url(db, source=item["source"], monitor_id=item["monitor_id"])

    def stream_response(db, item, state):
        source = item["source"]
        prefix = f"/plus/api/playback/sessions/{state['session_id']}/"
        return JSONResponse(
            state
            | {
                "source": public_projection(source) | {"mode": "stream"},
                "skip_segments": normalize_skip_segments(source),
                "media_url": prefix + "playlist.m3u8",
                "media_type": "application/vnd.apple.mpegurl",
                "next_url": next_url(db, item),
                "subtitles": [],
                "fonts": [],
                "track_map": [],
                "resume_version_changed": item["version_changed"],
                "preparation_required": False,
                "prepare_url": None,
                "streaming": True,
                "subtitle_url": prefix + "subtitle.ass" if item.get("stream_subtitle") else None,
                "subtitle_track": item.get("subtitle_track"),
            },
            status_code=201,
            headers=HEADERS,
        )

    def compatible_response(db, item, state):
        source = item["source"]
        sid = state["session_id"]
        prefix = f"/plus/api/playback/sessions/{sid}/"
        return JSONResponse(
            state
            | {
                "source": public_projection(source) | {"mode": "stream"},
                "skip_segments": normalize_skip_segments(source),
                "media_url": prefix + "compatible.mpd",
                "media_type": "application/dash+xml",
                "seek_url": prefix + "compatible-seek",
                "next_url": next_url(db, item),
                "subtitles": [],
                "fonts": [],
                "track_map": [],
                "resume_version_changed": item["version_changed"],
                "preparation_required": False,
                "streaming": False,
                "subtitle_url": prefix + "subtitle.ass" if item.get("stream_subtitle") else None,
                "subtitle_track": item.get("subtitle_track"),
            },
            status_code=201,
            headers=HEADERS,
        )

    def abandon(db, request, user_id, state):
        if state is not None:
            with households._transaction(db):
                db.execute(
                    "UPDATE web_playback_sessions SET closed=1,closed_at=? "
                    "WHERE session_id=? AND user_id=? AND binding_digest=?",
                    (time.time(), state["session_id"], user_id, _binding(request)),
                )
                process_deferred_completion_cleanup(db)
            item = handles.pop(state["session_id"], None)
            cleanup_stream(item)

    def current(db, request, user_id, sid):
        item = handles.get(sid)
        if item is None or item["source"].actor_user_id != user_id:
            _LOG.info("playback_session_rejected reason=session_unavailable")
            raise HTTPException(404, "Playback session unavailable")
        source = item["source"]
        authenticated, _ = authenticate_session(db, request.cookies.get("omakase_session", ""))
        if authenticated != user_id:
            _LOG.info("playback_session_rejected reason=session_auth_mismatch")
            raise HTTPException(401, "Sign in again")
        try:
            source_guard(db, source)
            state = store.get_session(
                db,
                session_id=sid,
                user_id=user_id,
                binding_digest=_binding(request),
                expected_snapshot=_snapshot(source),
            )
        except (PlaybackSourceError, store.PlaybackStoreError) as error:
            _LOG.warning("playback_session_rejected reason=%s", error.code)
            if error.code == "publication_read_conflict":
                raise HTTPException(503, error.code, headers={"Retry-After": "1"}) from None
            raise HTTPException(409, error.code) from None
        except (ValueError, households.HouseholdError):
            _LOG.warning("playback_session_rejected reason=source_scope_changed")
            raise HTTPException(409, "source_scope_changed") from None
        return source, state

    async def authorized_bytes(db, request, user_id, sid):
        source, _ = current(db, request, user_id, sid)
        try:
            result = remote_guard(db, source, transport=transport)
            import inspect

            if inspect.isawaitable(result):
                result = await result
            if result is False:
                raise ValueError("Access changed")
        except ValueError:
            raise HTTPException(
                409, "Personal Plex access or the selected media changed."
            ) from None
        current(db, request, user_id, sid)

    async def prepare_item(db, request, user_id, state, item):
        async def authorize_job():
            await authorized_bytes(db, request, user_id, state["session_id"])
            if await request.is_disconnected():
                raise remux.RemuxError("client_disconnected")

        item["artifact"] = await remux_prepare(
            db,
            source=item["source"],
            authorize=authorize_job,
            cache_root=cache_root,
            transport=transport,
            held_versions=lambda: held_versions(db),
        )
        current(db, request, user_id, state["session_id"])

    def prepared_response(db, state, item, *, pending=False):
        source, artifact = item["source"], item["artifact"]
        copied = source.container not in {"mp4", "webm"}
        prefix = f"/plus/api/playback/sessions/{state['session_id']}/"
        resource_prefix = prefix + "resources/"
        subtitles = (
            []
            if artifact is None
            else [
                {k: v for k, v in track.items() if k not in {"filename", "container_index"}}
                | {"url": resource_prefix + track["filename"]}
                for track in artifact.get("subtitles", [])
            ]
        )
        fonts = (
            []
            if artifact is None
            else [resource_prefix + font["filename"] for font in artifact.get("fonts", [])]
        )
        return JSONResponse(
            state
            | {
                "source": public_projection(source)
                | {"mode": "copy-remux" if copied else "direct"},
                # Existing exact-source Plex reads supply optional trusted markers.
                "skip_segments": normalize_skip_segments(source),
                "media_url": None
                if pending
                else resource_prefix + "manifest.mpd"
                if copied
                else prefix + "media",
                "media_type": None
                if pending
                else "application/dash+xml"
                if copied
                else "video/mp4"
                if source.container == "mp4"
                else "video/webm",
                "next_url": next_url(db, item),
                "subtitles": subtitles,
                "fonts": fonts,
                "track_map": [] if artifact is None else artifact["track_map"],
                "resume_version_changed": item["version_changed"],
                "preparation_required": pending,
                "prepare_url": prefix + "prepare" if pending else None,
            },
            status_code=202 if pending else 201,
            headers=HEADERS,
        )

    @router.get("/watch/{monitor_id}/{episode_number}")
    async def page(
        request: Request,
        monitor_id: int,
        episode_number: int,
        library_id: str | None = None,
        play: bool = False,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        if not 0 <= episode_number <= 5000:
            raise HTTPException(404, "Episode unavailable")
        monitor, scope = _monitor(db, user.id, monitor_id, library_id)
        if (episode_number == 0) != (monitor["format"] == "MOVIE"):
            raise HTTPException(404, "Title unavailable")
        return templates.TemplateResponse(
            request,
            "player.html",
            {
                **product_context(request, user, active_page="library"),
                **watch_context(db, monitor, scope, episode_number),
                "title": monitor["title"],
                "monitor_id": monitor_id,
                "episode_number": episode_number,
                "library_id": scope.library_id,
                "subtitle_viewer_id": user.id,
                "start_immediately": play,
                "overview_url": f"/plus/library/{monitor_id}?library_id={scope.library_id}",
            },
            headers=HEADERS
            | {
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "require-corp",
            },
        )

    def render_catalog_watch(request, db, user, target, play):
        domain, canonical, season, episode = _catalog_key(target)
        try:
            scope = personal_scope(db, user.id)
            catalog, entry = catalog_target(db, scope, domain, canonical, season, episode)
        except PlaybackSourceError:
            raise HTTPException(404, "Title unavailable in your library") from None
        title = catalog["title"]
        episode_data = json.loads(entry["episode_json"]) if entry is not None else {}
        overview = episode_data.get("overview") if entry is not None else catalog.get("overview")
        back_url = (
            f"/plus/tv/{canonical}?season={season}#tv-seasons"
            if entry
            else f"/plus/movies/{canonical}"
        )
        return templates.TemplateResponse(
            request,
            "player.html",
            {
                **product_context(request, user, active_page="tv" if entry else "movies"),
                "title": title,
                "episode_number": episode,
                "season_number": season,
                "library_id": scope.library_id,
                "catalog_target": target.model_dump(),
                "subtitle_viewer_id": user.id,
                "start_immediately": play,
                "overview_url": back_url,
                "description_preview": overview,
                "next_url": _catalog_neighbor(db, scope, target, 1),
                "previous_url": _catalog_neighbor(db, scope, target, -1),
            },
            headers=HEADERS
            | {
                "Cross-Origin-Opener-Policy": "same-origin",
                "Cross-Origin-Embedder-Policy": "require-corp",
            },
        )

    @router.get("/watch/title/movie/{imdb_id}")
    def watch_movie(
        request: Request,
        imdb_id: str,
        play: bool = False,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        try:
            target = MovieTarget(imdb_id=imdb_id)
        except ValueError:
            raise HTTPException(404, "Movie unavailable") from None
        return render_catalog_watch(request, db, user, target, play)

    @router.get("/watch/title/tv/{tmdb_id}/{season}/{episode}")
    def watch_tv(
        request: Request,
        tmdb_id: int,
        season: int,
        episode: int,
        play: bool = False,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        try:
            target = TVTarget(tmdb_id=tmdb_id, season_number=season, episode_number=episode)
        except ValueError:
            raise HTTPException(404, "Episode unavailable") from None
        return render_catalog_watch(request, db, user, target, play)

    @router.post("/api/playback/sessions")
    async def start(request: Request, body: Start, user=Depends(require_user), db=Depends(get_db)):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        monitor, scope = _monitor(db, user.id, body.monitor_id, body.library_id)
        if (body.episode_number == 0) != (monitor["format"] == "MOVIE"):
            raise HTTPException(404, "Title unavailable")
        if scope.kind == "personal":
            monitor, scope = _monitor(db, user.id, body.monitor_id, scope.library_id, prepare=True)
        return await start_session(request, body, user, db, scope, monitor=monitor)

    @router.post("/api/playback/catalog-sessions")
    async def start_catalog(
        request: Request, body: CatalogStart, user=Depends(require_user), db=Depends(get_db)
    ):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        try:
            scope = personal_scope(db, user.id)
        except PlaybackSourceError:
            raise HTTPException(404, "Personal library unavailable") from None
        return await start_session(request, body, user, db, scope)

    async def start_session(request, body, user, db, scope, *, monitor=None):
        for sid, item in list(handles.items()):
            active = db.execute(
                "SELECT 1 FROM web_playback_sessions s JOIN web_playback_resumes r "
                "USING(target_key) WHERE s.session_id=? AND s.closed=0 "
                "AND s.expires_at>? AND s.last_seen>? AND s.revision=r.revision",
                (sid, time.time(), time.time() - 1200),
            ).fetchone()
            if not active:
                item = handles.pop(sid, None)
                cleanup_stream(item)
        if len(handles) + admission.active[("start",)] >= 16:
            raise HTTPException(429, "The player is busy. Try after an existing session ends.")
        release_start = admit("start", user.id)
        state, delivered, projection = None, False, None
        try:
            pending = [
                future for actor, future, _ in tuple(terminal_saves.values()) if actor == user.id
            ]
            if pending:
                try:
                    await asyncio.wait_for(asyncio.gather(*(asyncio.shield(f) for f in pending)), 1)
                except TimeoutError:
                    raise HTTPException(
                        429, "Your last save is finishing. Try again shortly."
                    ) from None
            catalog = body.target if isinstance(body, CatalogStart) else None
            if catalog is not None:
                domain, canonical, season, episode = _catalog_key(catalog)
                source = await resolve_catalog_source(
                    db,
                    scope=scope,
                    domain=domain,
                    canonical_id=canonical,
                    season=season,
                    episode=episode,
                    media_version=body.media_version,
                    transport=transport,
                )
            else:
                source = await resolver(
                    db,
                    actor_user_id=user.id,
                    library_id=scope.library_id,
                    anilist_id=monitor["anilist_id"],
                    season_number=monitor["season_number"] or 0,
                    episode_number=body.episode_number,
                    media_version=body.media_version,
                    transport=transport,
                )
            source_guard(db, source)
            projection = public_projection(source)
            copied = source.container not in {"mp4", "webm"}
            stream_mode = False
            anime_episode = getattr(source, "domain", "anime") == "anime"
            compatible_available = streaming_enabled and compatibility.eligible(source, body.codecs)
            compatible_slots_full = (
                sum("compatible_stream" in value for value in handles.values()) >= 2
            )
            # The growing HLS playlist starts at zero. For a qualified episode,
            # prefer an on-demand stream that can open directly at the saved time.
            fast_anime = (
                compatible_available
                and isinstance(body, Start)
                and body.prefer_on_demand
                and anime_episode
                and source.container == "mkv"
                and source.audio_codec == "aac"
                and sum(track.kind == "audio" for track in source.streams) == 1
                and not compatible_slots_full
            )
            compatible_mode = (
                compatible_available
                and not anime_episode
                and (
                    source.audio_codec not in body.codecs
                    or (copied and stream_budget(source) > stream_cache_max_bytes)
                )
            )
            if compatible_mode and compatible_slots_full:
                raise HTTPException(429, "The compatible player is busy. Try after a stream ends.")
            if copied and not compatible_mode:
                if streaming_enabled and source.container == "mkv":
                    stream_checks(source, body)
                    stream_mode = True
                else:
                    remux._inventory(source)
                    if source.container != "mkv" or source.video_codec not in body.codecs:
                        raise remux.RemuxError("unsupported_browser")
                    if any(s.codec not in body.codecs for s in source.streams if s.kind == "audio"):
                        raise remux.RemuxError("unsupported_browser_audio")
                    if (
                        any(
                            s.kind == "subtitle" and s.codec in {"ass", "ssa"}
                            for s in source.streams
                        )
                        and not body.ass_supported
                    ):
                        raise remux.RemuxError("styled_subtitles_unsupported")
            target = store.ResolvedTarget(
                user.id,
                scope.library_id,
                getattr(source, "canonical_id", str(source.anilist_id)),
                source.season_number,
                source.episode_number,
                source.media_version,
                source.duration_ms / 1000,
                domain=getattr(source, "domain", "anime"),
            )
            previous = store.get_resume(db, target=target)
            version_changed = (
                previous is None
                and db.execute(
                    "SELECT 1 FROM web_playback_resumes WHERE user_id=? AND library_id=? "
                    "AND domain=? AND canonical_id=? AND season_number=? AND episode_number=? "
                    "AND media_version!=? AND position_seconds>0 LIMIT 1",
                    (
                        user.id,
                        scope.library_id,
                        target.domain,
                        target.canonical_id,
                        target.season_number,
                        target.episode_number,
                        source.media_version,
                    ),
                ).fetchone()
                is not None
            )
            with fresh_source_transaction(db):
                source_guard(db, source)
                if (
                    target.domain == "movie"
                    and db.execute(
                        "SELECT 1 FROM movie_cleanup_jobs WHERE imdb_id=? AND state='queued' "
                        "AND stage IN ('withdrawal_intent','awaiting_plex','provider_intent') LIMIT 1",
                        (target.canonical_id,),
                    ).fetchone()
                ):
                    raise PlaybackSourceError("This movie is being removed from the library.")
                now = datetime.now(UTC)
                _insert_binding(
                    db,
                    (
                        user.id,
                        source.account_id,
                        hashlib.sha256(source.token.encode()).hexdigest(),
                        source.server_id,
                        source.source_identity,
                        now.isoformat(),
                        (now + timedelta(hours=4)).isoformat(),
                    ),
                )
                state = store.create_session(
                    db, target=target, binding_digest=_binding(request), snapshot=_snapshot(source)
                )
            position = state["position_seconds"] if body.resume and not version_changed else 0
            item = {
                "source": source,
                "subtitle_preview_until": position + _SUBTITLE_LEAD_SECONDS,
                "expires_at": state["expires_at"],
                "artifact": None,
                "reads": 0,
                "monitor_id": body.monitor_id if catalog is None else None,
                "catalog_target": catalog,
                "version_changed": version_changed,
                "streaming": stream_mode,
                "allow_subtitles": body.ass_supported,
            }
            handles[state["session_id"]] = item
            if compatible_mode or fast_anime:
                sid = state["session_id"]
                compatible = compatibility.CompatibleStream(source, transport)
                item["compatible_stream"] = compatible
                prepare_stream_subtitle(source, sid, item, codecs={"ass", "ssa", "srt"})

                async def authorize():
                    current(db, request, user.id, sid)

                try:
                    if fast_anime:
                        async with asyncio.timeout(5):
                            await compatible.open(position, authorize)
                    else:
                        await compatible.open(position, authorize)
                except (DeliveryError, TimeoutError):
                    if not fast_anime:
                        raise
                    await compatible.close()
                    item.pop("compatible_stream")
                    _LOG.info("playback_start_fallback reason=on_demand_unavailable")
            if "compatible_stream" in item:
                item["streaming"] = False
                response = compatible_response(db, item, state)
            elif stream_mode:
                await start_stream(source, state["session_id"], item)
                current(db, request, user.id, state["session_id"])
                response = stream_response(db, item, state)
            else:
                pending = copied and body.defer_prepare
                if copied and not pending:
                    await prepare_item(db, request, user.id, state, item)
                response = prepared_response(db, state, item, pending=pending)
            delivered = True
            return response
        except remux.RemuxError as error:
            return _remux_failure(error, projection)
        except DeliveryError as error:
            return _failure(
                "compatible_stream_unavailable", error.status, str(error), source=projection
            )
        except PlaybackSourceError as error:
            _LOG.warning("playback_start_failed reason=%s", error.code)
            retryable = error.code == "publication_read_conflict"
            return _failure(
                error.code,
                503 if retryable else 409,
                str(error),
                source=projection,
                retry_after=1 if retryable else None,
            )
        except ValueError:
            return _failure(
                "source_unavailable", 409, "Exact personal playback could not be confirmed."
            )
        finally:
            release_start()
            if not delivered:
                abandon(db, request, user.id, state)

    @router.post("/api/playback/sessions/{sid}/prepare")
    async def prepare(
        request: Request, sid: str, body: Prepare, user=Depends(require_user), db=Depends(get_db)
    ):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        source, state = current(db, request, user.id, sid)
        item = handles[sid]
        if source.container != "mkv":
            raise HTTPException(409, "This source does not need preparation")
        release_start = admit("start", user.id)
        delivered = False
        try:
            await prepare_item(db, request, user.id, state, item)
            response = prepared_response(db, state, item)
            delivered = True
            return response
        except remux.RemuxError as error:
            return _remux_failure(error, public_projection(source))
        finally:
            release_start()
            if not delivered:
                abandon(db, request, user.id, state)

    @router.api_route("/api/playback/sessions/{sid}/resources/{filename}", methods=["GET", "HEAD"])
    async def derivative(
        request: Request, sid: str, filename: str, user=Depends(require_user), db=Depends(get_db)
    ):
        source, _ = current(db, request, user.id, sid)
        item = handles[sid]
        artifact = item["artifact"]
        if artifact is None or filename not in artifact["resources"]:
            raise HTTPException(404, "Media resource unavailable")
        release = admit("read", user.id, sid)

        try:
            path = resource_getter(
                db,
                artifact["job_id"],
                filename,
                cache_root=cache_root,
                held_versions=lambda: (source.media_version,),
            )
            return await open_local(
                path,
                request,
                authorize=lambda: authorized_bytes(db, request, user.id, sid),
                etag_seed=artifact["job_id"],
                on_close=release,
            )
        except (remux.RemuxError, DeliveryError):
            release()
            raise HTTPException(404, "Media resource unavailable") from None
        except BaseException:
            release()
            raise

    @router.api_route("/api/playback/sessions/{sid}/media", methods=["GET", "HEAD"])
    async def media(request: Request, sid: str, user=Depends(require_user), db=Depends(get_db)):
        source, _ = current(db, request, user.id, sid)
        release = admit("read", user.id, sid)
        try:
            return await open_part(
                source,
                request,
                authorize=lambda: authorized_bytes(db, request, user.id, sid),
                transport=transport,
                on_close=release,
            )
        except DeliveryError as error:
            release()
            raise HTTPException(error.status, str(error)) from None
        except BaseException:
            release()
            raise

    @router.post("/api/playback/sessions/{sid}/events")
    async def event(
        request: Request, sid: str, body: Event, user=Depends(require_user), db=Depends(get_db)
    ):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        source, _ = current(db, request, user.id, sid)
        try:
            with households._transaction(db):
                result = store.record_event(
                    db,
                    session_id=sid,
                    user_id=user.id,
                    binding_digest=_binding(request),
                    expected_snapshot=_snapshot(source),
                    **body.model_dump(),
                )
                if result["domain"] == "anime":
                    observe_playback(db, source=source, state=result)
            return JSONResponse(result, headers=HEADERS)
        except store.PlaybackStoreError as error:
            _LOG.warning("playback_event_rejected reason=%s", error.code)
            if error.code == "event_rate_limited":
                return _failure(error.code, 429, "Playback save is busy.", retry_after=1)
            return _failure(error.code, 409, "Playback save is stale or invalid.")

    @router.post("/api/playback/sessions/{sid}/close", status_code=204)
    async def close(
        request: Request,
        sid: str,
        body: Close | None = None,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        final = body.final_event if body else None
        if final and (final.playing or final.event not in {"seek", "pause", "ended"}):
            raise HTTPException(422, "Invalid terminal playback state")
        fingerprint = (
            store.event_fingerprint(
                final.position_seconds,
                final.playing,
                final.event,
                final.playback_rate,
                final.audio_id,
                final.text_id,
            )
            if final
            else None
        )

        def acknowledged():
            row = db.execute(
                "SELECT closed,binding_digest FROM web_playback_sessions WHERE session_id=? AND user_id=?",
                (sid, user.id),
            ).fetchone()
            if not row or not row["closed"] or row["binding_digest"] != _binding(request):
                return None
            if final:
                receipt = db.execute(
                    "SELECT fingerprint FROM web_playback_events WHERE session_id=? AND seq=?",
                    (sid, final.seq),
                ).fetchone()
                if not receipt or receipt[0] != fingerprint:
                    raise HTTPException(409, "Terminal save does not match the closed session")
            return Response(status_code=204, headers=HEADERS)

        response = acknowledged()
        if response is not None:
            return response
        source, _ = current(db, request, user.id, sid)
        pending = terminal_saves.get(sid)
        if pending is not None:
            actor, future, pending_fingerprint = pending
            if actor != user.id or pending_fingerprint != (
                final.seq if final else None,
                fingerprint,
            ):
                raise HTTPException(409, "Another terminal save is finishing")
            release_replay = admit("close_replay", user.id, sid)
            try:
                try:
                    await asyncio.wait_for(asyncio.shield(future), 1)
                except TimeoutError:
                    raise HTTPException(429, "Playback save is still finishing") from None
                response = acknowledged()
                if response is None:
                    raise HTTPException(409, "The previous terminal save was interrupted")
                return response
            finally:
                release_replay()
        release = admit("close", user.id, sid)
        future = asyncio.get_running_loop().create_future()
        terminal_saves[sid] = (user.id, future, (final.seq if final else None, fingerprint))
        try:
            if final:
                deadline = time.monotonic() + 0.8
                while True:
                    current(db, request, user.id, sid)
                    row = db.execute(
                        "SELECT last_seen,last_seq FROM web_playback_sessions WHERE session_id=?",
                        (sid,),
                    ).fetchone()
                    gap = 0.25 - (time.time() - row["last_seen"]) if row["last_seq"] else 0
                    if gap <= 0:
                        break
                    if time.monotonic() + gap + 0.01 > deadline:
                        raise HTTPException(429, "Playback save is busy. Try again shortly.")
                    await asyncio.sleep(gap + 0.01)
            current(db, request, user.id, sid)
            with households._transaction(db):
                if final:
                    result = store.record_event(
                        db,
                        session_id=sid,
                        user_id=user.id,
                        binding_digest=_binding(request),
                        expected_snapshot=_snapshot(source),
                        **final.model_dump(),
                    )
                    if result["domain"] == "anime":
                        observe_playback(db, source=source, state=result)
                store.close_session(
                    db,
                    session_id=sid,
                    user_id=user.id,
                    binding_digest=_binding(request),
                    expected_snapshot=_snapshot(source),
                )
                process_deferred_completion_cleanup(db)
            item = handles.pop(sid, None)
            if item is not None:
                await stop_stream(item)
            return Response(status_code=204, headers=HEADERS)
        except store.PlaybackStoreError:
            raise HTTPException(
                409, "Playback save is stale or invalid. Reload the player."
            ) from None
        finally:
            terminal_saves.pop(sid, None)
            if not future.done():
                future.set_result(None)
            release()

    @router.get("/api/playback/sessions/{sid}/compatible.mpd")
    async def compatible_manifest(
        request: Request, sid: str, user=Depends(require_user), db=Depends(get_db)
    ):
        current(db, request, user.id, sid)
        stream = handles[sid].get("compatible_stream")
        if stream is None or stream.generation is None:
            raise HTTPException(404, "Manifest unavailable")
        return Response(
            stream.generation.manifest, media_type="application/dash+xml", headers=HEADERS
        )

    @router.post("/api/playback/sessions/{sid}/compatible-seek")
    async def compatible_seek(
        request: Request,
        sid: str,
        body: CompatibilitySeek,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        require_csrf(request, request.headers.get("x-csrf-token", ""))
        source, _ = current(db, request, user.id, sid)
        stream = handles[sid].get("compatible_stream")
        if stream is None or body.position_seconds > source.duration_ms / 1000:
            raise HTTPException(404, "Playback position unavailable")
        release = admit("seek", user.id, sid)
        try:

            async def authorize():
                current(db, request, user.id, sid)

            await authorized_bytes(db, request, user.id, sid)
            await stream.open(body.position_seconds, authorize)
            return JSONResponse(
                {"media_url": f"/plus/api/playback/sessions/{sid}/compatible.mpd"}, headers=HEADERS
            )
        except DeliveryError as error:
            return _failure("compatible_stream_unavailable", error.status, str(error))
        finally:
            release()

    @router.get("/api/playback/sessions/{sid}/compatible/{generation}/{representation}/{asset}")
    async def compatible_segment(
        request: Request,
        sid: str,
        generation: str,
        representation: str,
        asset: str,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        current(db, request, user.id, sid)
        stream = handles[sid].get("compatible_stream")
        active = stream.generation if stream else None
        if active is None or active.key != generation:
            raise HTTPException(404, "Media resource unavailable")
        release = admit("read", user.id, sid)
        try:

            async def authorize():
                current(db, request, user.id, sid)

            return await stream.resource(active, representation, asset, authorize, release)
        except DeliveryError as error:
            release()
            raise HTTPException(error.status, str(error)) from None

    @router.get("/api/playback/sessions/{sid}/playlist.m3u8")
    async def stream_playlist(
        request: Request, sid: str, user=Depends(require_user), db=Depends(get_db)
    ):
        current(db, request, user.id, sid)
        item = handles.get(sid)
        playlist = item.get("stream_playlist") if item else None
        if playlist is None or not Path(playlist).is_file():
            raise HTTPException(404, "Playlist unavailable")
        return FileResponse(
            playlist,
            media_type="application/vnd.apple.mpegurl",
            headers=HEADERS | {"Cache-Control": "private, no-store"},
        )

    @router.get("/api/playback/sessions/{sid}/segments/{name}")
    async def stream_segment(
        request: Request,
        sid: str,
        name: str,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        if not re.fullmatch(r"seg-[0-9]{5}\.ts", name):
            raise HTTPException(404, "Segment unavailable")
        current(db, request, user.id, sid)
        item = handles.get(sid)
        folder = item.get("stream_dir") if item else None
        path = Path(folder) / name if folder is not None else None
        if path is None or not path.is_file():
            raise HTTPException(404, "Segment unavailable")
        return FileResponse(
            path,
            media_type="video/mp2t",
            headers=HEADERS | {"Cache-Control": "private, no-store"},
        )

    @router.api_route("/api/playback/sessions/{sid}/subtitle.ass", methods=["GET", "HEAD"])
    async def stream_subtitle(
        request: Request,
        sid: str,
        complete: bool = False,
        user=Depends(require_user),
        db=Depends(get_db),
    ):
        current(db, request, user.id, sid)
        item = handles.get(sid)
        subtitle = item.get("stream_subtitle") if item else None
        task = item.get("subtitle_task") if item else None
        media_type = "text/plain; charset=utf-8"
        headers = HEADERS | {"Cache-Control": "private, no-store"}
        if task is not None and not task.done():
            if complete or not (preview_bytes := item.get("subtitle_preview_bytes")):
                return Response(status_code=202, headers=headers | {"Retry-After": "1"})
            content = b""
            if request.method == "GET":
                with subtitle.open("rb") as script:
                    content = script.read(preview_bytes)
            return Response(
                content,
                media_type=media_type,
                headers=headers
                | {"X-Omakase-Subtitle-Complete": "0", "Content-Length": str(preview_bytes)},
            )
        if task is not None and (
            task.cancelled() or task.exception() is not None or not task.result()
        ):
            raise HTTPException(404, "Subtitles unavailable")
        if subtitle is None or not Path(subtitle).is_file() or Path(subtitle).stat().st_size <= 0:
            raise HTTPException(404, "Subtitles unavailable")
        return FileResponse(
            subtitle,
            media_type=media_type,
            headers=headers | {"X-Omakase-Subtitle-Complete": "1"},
        )

    return router
