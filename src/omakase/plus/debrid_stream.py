"""Same-origin delivery of a Real-Debrid Apple HLS stream."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import subprocess
import tempfile
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from starlette.responses import Response

from omakase.plus.debrid_library import CloudSource, DebridLibraryError
from omakase.plus.debrid_seek import SeekStreams
from omakase.plus.debrid_subs import SubtitleCache
from omakase.plus.outbound import OutboundError, _resolved_ips, blocked_address, pin_destination

_LOG = logging.getLogger(__name__)

HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
}
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
_PLAYLIST_LIMIT = 512 * 1024
_SEGMENT_LIMIT = 32 * 1024 * 1024
_MAX_SEGMENTS = 10000
# A cached subtitle source only needs its WebVTT files read from disk, which is
# fast enough to keep the first index request open so AVPlayer receives the
# SUBTITLES group immediately. An uncached PGS/ASR job can take minutes.
_WARM_SUBTITLE_WAIT = 12.0
_COLD_SUBTITLE_WAIT = 0.2
# Keep a forward window of provider segments warm so a resume or a seek never
# has to wait for Real-Debrid to generate the next segment mid-playback.
_SEGMENT_READAHEAD = 8
_SEGMENT_WAIT = 4.0
# AVPlayer abandons a segment after roughly five seconds and reports
# CoreMedia -12889. A provider segment that is not ready inside this budget is
# answered with the last served segment so the player keeps its connection and
# recovers on the next request instead of failing the item.
_SEGMENT_GRACE = 2.5
# Repeating one stored segment keeps the player alive through a short provider
# hiccup, but a long outage must fail honestly: looping a frozen picture while
# the subtitle timeline advances desyncs captions and hides the real problem.
_SEGMENT_REPEAT_LIMIT = 3
_SEGMENT_CACHE_ENTRIES = 64
_SEGMENT_CACHE_BYTES = 96 * 1024 * 1024
_SEGMENT_TTL = 180.0
_MEDIA_ID = re.compile(r"[a-f0-9]{32}")
_SECRET = re.compile(r"[a-zA-Z0-9_-]{43}")


def _unavailable():
    return DebridLibraryError("The Real-Debrid stream could not be opened.", 502)


def _stream_url(url):
    try:
        parsed = urlsplit(url)
        if (
            len(url) > 4096
            or any(ord(char) < 33 for char in url)
            or "\\" in url
            or parsed.scheme != "https"
            or not re.fullmatch(r"[a-z0-9-]{1,60}\.stream\.real-debrid\.com", parsed.hostname or "")
            or parsed.netloc != parsed.hostname
            or parsed.port is not None
            or not parsed.path.startswith("/")
            or parsed.fragment
        ):
            raise ValueError
    except ValueError:
        raise _unavailable() from None
    return parsed


async def _public_addresses(host, port):
    addresses = await _resolved_ips(host, port)
    if any(blocked_address(address, allow_private=False) for address in addresses):
        raise OutboundError("unsafe_destination")
    return addresses


def parse_playlist(data, url, duration):
    root = _stream_url(url)
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise _unavailable() from None
    if not lines or lines[0] != "#EXTM3U" or lines[-1] != "#EXT-X-ENDLIST":
        raise _unavailable()
    target = None
    segment_length = None
    entries = []
    for line in lines[1:-1]:
        if not line or line in {
            "#EXT-X-ALLOW-CACHE:NO",
            "#EXT-X-ALLOW-CACHE:YES",
            "#EXT-X-VERSION:3",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        }:
            continue
        if match := re.fullmatch(r"#EXT-X-TARGETDURATION:([1-9][0-9]?)", line):
            if target is not None or entries or segment_length is not None:
                raise _unavailable()
            target = int(match[1])
            continue
        if line == "#EXT-X-MEDIA-SEQUENCE:0" and not entries:
            continue
        if match := re.fullmatch(r"#EXTINF:([0-9]{1,2}(?:\.[0-9]{1,8})?),[^\r\n]*", line):
            if (
                target is None
                or segment_length is not None
                or not 0 <= float(match[1]) <= target + 0.5
            ):
                raise _unavailable()
            segment_length = match[1]
            continue
        if line.startswith("#") or segment_length is None or len(entries) >= _MAX_SEGMENTS:
            raise _unavailable()
        upstream = urljoin(url, line)
        part = _stream_url(upstream)
        if part.hostname != root.hostname or not part.path.endswith(".ts"):
            raise _unavailable()
        entries.append((segment_length, upstream))
        segment_length = None
    if target is None or segment_length is not None or not entries:
        raise _unavailable()
    # Real-Debrid appends a zero-length filler segment after the last real one.
    while entries and float(entries[-1][0]) == 0:
        entries.pop()
    if not entries or any(float(length) == 0 for length, _ in entries):
        raise _unavailable()
    total = sum(float(length) for length, _ in entries)
    if abs(total - duration) > 15:
        raise _unavailable()
    manifest = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index, (length, _) in enumerate(entries):
        manifest.extend((f"#EXTINF:{length},", f"{index:05d}.ts"))
    manifest.extend(("#EXT-X-ENDLIST", ""))
    return "\n".join(manifest).encode("ascii"), tuple(upstream for _, upstream in entries)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _playlist_reply(body, request):
    return Response(
        body if request.method == "GET" else b"",
        media_type=PLAYLIST_TYPE,
        headers=HEADERS | {"Content-Length": str(len(body))},
    )


def _media_duration(playlist):
    total = 0.0
    for match in re.finditer(rb"#EXTINF:([0-9.]+),", playlist):
        total += float(match[1])
    return max(total, 1.0)


def _master_playlist(entry, job=None):
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    if job is not None:
        for number, track in enumerate(job.tracks):
            name = track.name.replace('"', "'")[:40]
            language = track.language or "und"
            default = "YES" if number == 0 else "NO"
            lines.append(
                f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="{name}",'
                f'LANGUAGE="{language}",DEFAULT={default},AUTOSELECT=YES,FORCED=NO,'
                f'URI="subtitles{number}.m3u8"'
            )
    stream = "#EXT-X-STREAM-INF:BANDWIDTH=8000000"
    if job is not None:
        stream += ',SUBTITLES="subs"'
    lines.append(stream)
    lines.append("stream.m3u8")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _subtitle_playlist(entry, number):
    duration = _media_duration(entry.playlist)
    target = max(1, int(duration + 0.999))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXTINF:{duration:.3f},",
        f"subtitles{number}.vtt",
        "#EXT-X-ENDLIST",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


@dataclass(frozen=True, repr=False)
class DebridMedia:
    playlist: bytes
    segments: tuple[str, ...]
    first_segment: bytes = b""
    subtitle_key: str = ""
    seek_key: str = ""


@dataclass(frozen=True, repr=False)
class CloudPass:
    source: CloudSource
    user_id: int
    binding: str
    cookie_digest: str
    expires: float
    playlist: bytes
    segments: tuple[str, ...]
    first_segment: bytes = b""
    subtitle_key: str = ""
    seek_key: str = ""


def _repair_h264_level(data):
    """Upgrade an invalid 1080p H.264 level to 4.0 without re-encoding."""
    if len(data) < 188 or len(data) > _SEGMENT_LIMIT:
        return data
    with tempfile.TemporaryDirectory(prefix="omakase-level-") as folder:
        source = Path(folder) / "segment.ts"
        target = Path(folder) / "fixed.ts"
        source.write_bytes(data)
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-c",
                    "copy",
                    "-bsf:v",
                    "h264_metadata=level=4.0",
                    "-muxdelay",
                    "0",
                    "-f",
                    "mpegts",
                    str(target),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if result.returncode == 0 and target.stat().st_size > 188:
                return target.read_bytes()
        except (OSError, subprocess.SubprocessError):
            pass
    return data


def subtitle_cache_key(source):
    direct = getattr(source, "direct", "")
    path = urlsplit(direct).path if direct else getattr(source, "media_version", "")
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


class DebridStreams:
    def __init__(self, transport=None, clock=time.monotonic, subtitle_root=None, seek_root=None):
        self.transport = transport
        self.clock = clock
        self.subtitles = SubtitleCache(subtitle_root, clock)
        self.seek = SeekStreams(seek_root, clock)
        self.passes = {}
        self.reads = asyncio.Semaphore(8)
        # First segments that are being warmed without blocking the play reply.
        self.warm = {}
        self.first_cache = OrderedDict()
        self.segment_cache = OrderedDict()
        self.media_cache = OrderedDict()
        self.cached_bytes = 0
        self.pump = None
        self.last_served = {}
        self.repeat_streak = {}

    @staticmethod
    def _playback_key(media):
        return media.segments[0] if media.segments else ""

    def _repeat_last_segment(self, media, index, request, headers):
        """Answer with the last segment actually served for this media."""
        key = self._playback_key(media)
        record = self.last_served.get(key)
        if record is None or record[0] > index:
            return None
        data = record[1]
        if not data or request.method == "HEAD":
            return None
        streak = self.repeat_streak.get(key, 0) + 1
        self.repeat_streak[key] = streak
        if streak > _SEGMENT_REPEAT_LIMIT:
            _LOG.warning(
                "provider segment %05d still not ready after %d repeats; failing the segment",
                index,
                streak,
            )
            raise DebridLibraryError(
                "Real-Debrid is not delivering this part of the video right now.", 503
            )
        _LOG.warning(
            "provider segment %05d not ready within %.1fs; repeating segment %05d (%d bytes)",
            index,
            _SEGMENT_GRACE,
            record[0],
            len(data),
        )
        return Response(
            data,
            media_type="video/mp2t",
            headers=headers | {"Content-Length": str(len(data))},
        )

    def _remember(self, media, index, data):
        if data:
            key = self._playback_key(media)
            self.last_served[key] = (index, data)
            self.repeat_streak[key] = 0

    def _cache_segment(self, url, data):
        if not data:
            return
        previous = self.segment_cache.pop(url, None)
        if previous is not None:
            self.cached_bytes -= len(previous[1])
        self.segment_cache[url] = (self.clock() + _SEGMENT_TTL, data)
        self.cached_bytes += len(data)
        while self.segment_cache and (
            self.cached_bytes > _SEGMENT_CACHE_BYTES
            or len(self.segment_cache) > _SEGMENT_CACHE_ENTRIES
        ):
            _, (_, evicted) = self.segment_cache.popitem(last=False)
            self.cached_bytes -= len(evicted)

    def _advance(self, media, index):
        """Keep provider segments ahead of playback without blocking it."""
        if not media.segments:
            return
        target = min(len(media.segments), index + 1 + _SEGMENT_READAHEAD)
        state = self.pump
        if state is None or state["segments"] is not media.segments:
            self.pump = {"segments": media.segments, "next": index + 1, "target": target}
            state = self.pump
        else:
            state["next"] = max(state["next"], index + 1)
            state["target"] = max(state["target"], target)
        task = state.get("task")
        if task is not None and not task.done():
            return
        pump = self._pump(state)
        try:
            state["task"] = asyncio.create_task(pump)
        except RuntimeError:
            # No running loop (a sync caller): the next async call restarts it.
            pump.close()
            state["task"] = None

    async def _pump(self, state):
        segments = state["segments"]
        try:
            while state["next"] < len(segments) and state["next"] < state["target"]:
                url = segments[state["next"]]
                stored = self.segment_cache.get(url)
                if stored is not None and stored[0] > self.clock():
                    state["next"] += 1
                    continue
                task = self.warm.get(url)
                owns = task is None
                if owns:
                    task = asyncio.create_task(self._fetch_segment(url))
                    self.warm[url] = task
                try:
                    await asyncio.shield(task)
                except TimeoutError:
                    return
                except asyncio.CancelledError:
                    if owns:
                        task.cancel()
                    return
                finally:
                    self.warm.pop(url, None)
                state["next"] += 1
        finally:
            state["task"] = None

    async def _read(
        self, url, *, limit, method="GET", byte_range=None, authorize=None, timeout=12, attempts=3
    ):
        parsed = _stream_url(url)
        # A transcode stream's first request can briefly answer 5xx while
        # Real-Debrid starts the output. Three bounded attempts cover that window.
        for attempt in range(attempts):
            try:
                return await self._read_once(
                    parsed,
                    limit=limit,
                    method=method,
                    byte_range=byte_range,
                    authorize=authorize,
                    timeout=timeout,
                )
            except DebridLibraryError as error:
                if attempt + 1 == attempts or error.status not in {502, 503, 504}:
                    raise
            except (httpx.HTTPError, OutboundError, OSError, TimeoutError):
                if attempt + 1 == attempts:
                    raise _unavailable() from None
            await asyncio.sleep(0.5 * (attempt + 1))

    async def _read_once(self, parsed, *, limit, method, byte_range, authorize, timeout):
        try:
            destination = await pin_destination(
                "https://" + parsed.hostname,
                transport=self.transport,
                resolver=None if self.transport is not None else _public_addresses,
            )
            headers = {"Host": destination.host_header, "Accept-Encoding": "identity"}
            if byte_range:
                headers["Range"] = byte_range
            async with (
                httpx.AsyncClient(
                    transport=self.transport,
                    timeout=httpx.Timeout(timeout, connect=5),
                    follow_redirects=False,
                    trust_env=False,
                ) as client,
                client.stream(
                    method,
                    destination.base_url
                    + parsed.path
                    + ("?" + parsed.query if parsed.query else ""),
                    headers=headers,
                    extensions={"sni_hostname": destination.sni_hostname},
                ) as response,
            ):
                if response.status_code == 451:
                    raise DebridLibraryError("This file is unavailable from Real-Debrid.", 451)
                if (
                    response.status_code not in ({200, 206} if byte_range else {200})
                    or response.headers.get("content-encoding", "identity") != "identity"
                ):
                    raise _unavailable()
                if authorize:
                    authorize()
                data = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    data.extend(chunk)
                    if len(data) > limit:
                        raise _unavailable()
                if authorize:
                    authorize()
                if not data and method != "HEAD":
                    # An empty 200 is Real-Debrid's cold-transcode placeholder.
                    raise _unavailable()
                return bytes(data), response.status_code, response.headers
        except (httpx.HTTPError, OutboundError, OSError, TimeoutError):
            raise _unavailable() from None

    def prune(self):
        now = self.clock()
        for key, entry in tuple(self.passes.items()):
            if entry.expires <= now:
                self.passes.pop(key, None)

    def _start_seek(self, source):
        # The provider's own HLS transcode is the reliable Apple TV path for
        # both H.264 and HEVC. Keep the seek engine available for tests and
        # future direct-file work, but do not select it during playback.
        return None

    def _start_seek_disabled(self, source):
        direct = getattr(source, "direct", "")
        if not direct:
            return None
        return self.seek.start(source.media_version, direct, source.duration, source.title)

    def _start_subtitles(self, source, media):
        direct = getattr(source, "direct", "")
        if not direct or not media.segments:
            return None
        return self.subtitles.start(subtitle_cache_key(source), direct, source.duration)

    async def prepare(self, source, *, wait_first=True):
        cache_key = source.identity + ":" + source.media_version
        cached = self.media_cache.get(cache_key)
        if cached is not None and cached[0] > self.clock():
            self.media_cache.move_to_end(cache_key)
            media = cached[1]
            job = self._start_subtitles(source, media)
            media = replace(media, subtitle_key=job.key if job is not None else "")
            await self._subtitle_ready(job)
            self._start_seek(source)
            if wait_first and not media.first_segment:
                winner = await self._race_first_segment([media])
                return winner or media
            if not wait_first:
                self._start_warm(media)
            return media
        urls = [source.playlist]
        if source.playlist_alt:
            urls.append(source.playlist_alt)

        async def load(url):
            try:
                data, _, _ = await self._read(url, limit=_PLAYLIST_LIMIT, timeout=15, attempts=2)
                return DebridMedia(*parse_playlist(data, url, source.duration))
            except DebridLibraryError:
                return None

        medias = [media for media in await asyncio.gather(*(load(url) for url in urls)) if media]
        if not medias:
            raise _unavailable()
        job = self._start_subtitles(source, medias[0])
        subtitle_key = job.key if job is not None else ""
        await self._subtitle_ready(job)
        seek_plan = self._start_seek(source)
        seek_key = source.media_version if seek_plan is not None else ""
        medias = [replace(media, subtitle_key=subtitle_key, seek_key=seek_key) for media in medias]
        self.media_cache[cache_key] = (self.clock() + 300, medias[0])
        while len(self.media_cache) > 16:
            self.media_cache.popitem(last=False)
        if not wait_first:
            primary = medias[0]
            self._start_warm(primary)
            return primary
        if len(medias) == 1:
            return medias[0]
        # Real-Debrid can make one transcode variant start quickly while another
        # answers slowly or with an empty cold-start body. Race the first segment
        # across variants so playback starts from whichever is ready.
        winner = await self._race_first_segment(medias)
        return winner or medias[0]

    async def _subtitle_ready(self, job):
        if job is None:
            return
        try:
            async with asyncio.timeout(0.4):
                await job.ready.wait()
        except TimeoutError:
            return

    def warm_position(self, media, position):
        if position <= 0 or not media.segments:
            return
        duration = _media_duration(media.playlist) or max(1.0, len(media.segments) * 5.0)
        step = max(1.0, duration / len(media.segments))
        index = min(len(media.segments) - 1, max(0, int(position / step)))
        # Resume at the saved point and keep a forward window warm, so the
        # first segment AVPlayer asks for is already generated.
        self._advance(media, max(0, index - 1))

    async def _fetch_segment(self, url):
        try:
            data, status, _ = await self._read(url, limit=_SEGMENT_LIMIT, timeout=60, attempts=1)
        except (DebridLibraryError, TimeoutError, asyncio.CancelledError):
            return None
        if status == 200 and data:
            self._cache_segment(url, data)
        return data

    def _start_warm(self, media):
        if not media.segments:
            return
        url = media.segments[0]
        if url in self.warm:
            return
        stored = self.first_cache.get(url)
        if stored is not None and stored[0] > self.clock():
            self.first_cache.move_to_end(url)
            return
        while len(self.warm) >= 8:
            self.warm.pop(next(iter(self.warm)), None)
        self.warm[url] = asyncio.create_task(self._fetch_warm(media))

    async def _fetch_warm(self, media):
        try:
            data, _, _ = await self._read(
                media.segments[0], limit=_SEGMENT_LIMIT, timeout=45, attempts=1
            )
        except (DebridLibraryError, TimeoutError, asyncio.CancelledError):
            return None
        if data:
            self.first_cache[media.segments[0]] = (self.clock() + 300, data)
            while len(self.first_cache) > 8:
                self.first_cache.popitem(last=False)
        return data

    async def _race_first_segment(self, medias):
        async def attempt(media):
            data, _, _ = await self._read(
                media.segments[0], limit=_SEGMENT_LIMIT, timeout=30, attempts=1
            )
            return replace(media, first_segment=data)

        tasks = [asyncio.create_task(attempt(media)) for media in medias]
        try:
            async with asyncio.timeout(25):
                pending = set(tasks)
                while pending:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        if task.cancelled():
                            continue
                        if task.exception() is None:
                            return task.result()
                return None
        except TimeoutError:
            return None
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def issue(self, source, user_id, binding, ttl):
        self.prune()
        if len(self.passes) >= 64:
            raise DebridLibraryError("Playback is busy. Try again shortly.", 429)
        media = await self.prepare(source)
        for key, existing in tuple(self.passes.items()):
            if existing.binding == binding:
                self.passes.pop(key, None)
        key, cookie = secrets.token_hex(16), secrets.token_urlsafe(32)
        self.passes[key] = CloudPass(
            source,
            user_id,
            binding,
            _digest(cookie),
            self.clock() + ttl,
            media.playlist,
            media.segments,
            media.first_segment,
            media.subtitle_key,
            media.seek_key,
        )
        return key, cookie

    def for_client(self, key, binding):
        self.prune()
        current = self.passes.get(key) if _MEDIA_ID.fullmatch(key) else None
        return current if current is not None and current.binding == binding else None

    def for_cookie(self, key, cookie):
        self.prune()
        current = self.passes.get(key) if _MEDIA_ID.fullmatch(key) else None
        if (
            current
            and _SECRET.fullmatch(cookie)
            and secrets.compare_digest(current.cookie_digest, _digest(cookie))
        ):
            return current
        return None

    async def _subtitle_track(self, job, index):
        if job is None:
            raise DebridLibraryError("This stream has no subtitles.", 404)
        if not job.done.is_set():
            try:
                async with asyncio.timeout(600):
                    await job.done.wait()
            except TimeoutError:
                raise DebridLibraryError("Subtitles are still preparing.", 503) from None
        if index >= len(job.tracks) or not job.tracks[index].data:
            raise DebridLibraryError("This subtitle track is unavailable.", 404)
        return job.tracks[index]

    async def deliver(self, entry, asset, request, authorize: Callable[[], None]):
        authorize()
        job = self.subtitles.get(getattr(entry, "subtitle_key", ""))
        plan = self.seek.get(getattr(entry, "seek_key", ""))
        if asset == "index.m3u8":
            if job is not None and not job.done.is_set():
                # A source whose WebVTT files are already cached only needs them
                # read back, so hold the first index long enough to serve a
                # master with a SUBTITLES group. An uncached PGS/ASR job can take
                # minutes and must not hold the player's index request open.
                await self.subtitles.wait_tracks(
                    job, _WARM_SUBTITLE_WAIT if job.warm else _COLD_SUBTITLE_WAIT
                )
            if plan is not None:
                if not plan.ready.is_set():
                    try:
                        async with asyncio.timeout(7):
                            await plan.ready.wait()
                    except TimeoutError:
                        pass
                # The host cannot transcode HEVC faster than realtime. Real-Debrid
                # already exposes a server-side H.264 transcode; use it directly
                # instead of generating a slow local seek stream.
                if plan.index is not None and plan.index.video_codec != "hevc":
                    plan.mode = "seek"
                    # The Apple TV rejects the master wrapper with CoreMedia
                    # -12971. A seek timeline is a single VOD playlist already;
                    # expose it directly so AVPlayer never takes the master hop.
                    return _playlist_reply(plan.index.playlist, request)
            if job is not None and job.tracks and all(track.data for track in job.tracks):
                return _playlist_reply(_master_playlist(entry, job), request)
            # AVPlayer rejects the provider's master wrapper with CoreMedia
            # -12971. Serve the single provider media playlist directly; the
            # provider already chose one quality. Text subtitles are requested
            # separately and do not require a master for this path.
            _LOG.warning(
                "provider index class=%s seek=%s codec=%s master=%s bytes=%d",
                type(entry).__name__,
                bool(plan),
                getattr(getattr(plan, "index", None), "video_codec", None),
                b"#EXT-X-STREAM-INF" in entry.playlist,
                len(entry.playlist),
            )
            return _playlist_reply(entry.playlist, request)
        if asset == "stream.m3u8":
            if (
                plan is not None
                and plan.mode == "seek"
                and plan.index is not None
                and plan.index.video_codec != "hevc"
            ):
                return _playlist_reply(plan.index.playlist, request)
            return _playlist_reply(entry.playlist, request)
        if match := re.fullmatch(r"subtitles([0-9]{1,2})\.m3u8", asset):
            track = await self._subtitle_track(job, int(match[1]))
            return _playlist_reply(_subtitle_playlist(entry, int(match[1])), request)
        if match := re.fullmatch(r"subtitles([0-9]{1,2})\.vtt", asset):
            track = await self._subtitle_track(job, int(match[1]))
            headers = HEADERS | {"Content-Length": str(len(track.data))}
            return Response(
                b"" if request.method == "HEAD" else track.data,
                media_type="text/vtt",
                headers=headers,
            )
        if (
            asset == "init.mp4"
            and plan is not None
            and plan.mode == "seek"
            and plan.index is not None
            and plan.index.container == "fmp4"
        ):
            try:
                data = await self.seek.init(plan)
            except (LookupError, RuntimeError):
                raise DebridLibraryError("This position is still preparing.", 503) from None
            headers = HEADERS | {"Content-Length": str(len(data))}
            return Response(
                b"" if request.method == "HEAD" else data,
                media_type="video/mp4",
                headers=headers,
            )
        match = re.fullmatch(r"([0-9]{5})\.(ts|m4s)", asset)
        if not match:
            raise DebridLibraryError("This stream is unavailable.", 404)
        index = int(match[1])
        seek_asset = (
            plan is not None
            and plan.mode == "seek"
            and plan.index is not None
            and 0 <= index < len(plan.index.segments)
            and asset.endswith("." + plan.index.suffix)
        )
        if seek_asset:
            try:
                data = await self.seek.segment(plan, index)
            except LookupError:
                raise DebridLibraryError("This position is still preparing.", 503) from None
            except RuntimeError:
                raise DebridLibraryError("This position could not be prepared.", 503) from None
            headers = HEADERS | {"Content-Length": str(len(data))}
            if request.method == "HEAD":
                return Response(b"", headers=headers)
            byte_range = request.headers.get("range")
            if byte_range and re.fullmatch(r"bytes=[0-9]{1,9}-[0-9]{0,9}", byte_range):
                start_text, end_text = byte_range.removeprefix("bytes=").split("-")
                start = int(start_text)
                end = min(int(end_text) if end_text else len(data) - 1, len(data) - 1)
                if start > end:
                    return Response(status_code=416, headers=HEADERS)
                body = data[start : end + 1]
                return Response(
                    body,
                    status_code=206,
                    media_type="video/mp2t",
                    headers=HEADERS
                    | {
                        "Content-Range": f"bytes {start}-{end}/{len(data)}",
                        "Content-Length": str(len(body)),
                    },
                )
            media_type = "video/mp2t" if asset.endswith(".ts") else "video/iso.segment"
            return Response(data, media_type=media_type, headers=headers)
        if index >= len(entry.segments):
            raise DebridLibraryError("This stream is unavailable.", 404)
        byte_range = request.headers.get("range")
        if byte_range and not re.fullmatch(r"bytes=[0-9]{1,9}-[0-9]{0,9}", byte_range):
            return Response(status_code=416, headers=HEADERS)
        url = entry.segments[index]
        # Keep later segments generating while this request is being served.
        self._advance(entry, index)
        prepared_first = index == 0 and bool(getattr(entry, "first_segment", b""))
        stored = self.segment_cache.get(url)
        if stored is None and not prepared_first and index > 0:
            # One in-flight provider fetch per segment. The player's request
            # reuses it instead of racing a second upstream read, which
            # Real-Debrid answers by stalling both.
            task = self.warm.get(url)
            if task is None:
                task = asyncio.create_task(self._fetch_segment(url))
                self.warm[url] = task
            try:
                async with asyncio.timeout(_SEGMENT_GRACE):
                    await asyncio.shield(task)
            except (TimeoutError, asyncio.CancelledError):
                pass
            stored = self.segment_cache.get(url)
            if stored is None or stored[0] <= self.clock():
                # Real-Debrid stalled a segment past the player's deadline.
                # Answer with the last real segment so AVPlayer keeps the item
                # alive and retries, instead of failing with -12889.
                repeated = self._repeat_last_segment(entry, index, request, HEADERS.copy())
                if repeated is not None:
                    return repeated
                # Nothing served yet for this media: wait the fetch out.
                try:
                    await asyncio.shield(task)
                except (TimeoutError, asyncio.CancelledError):
                    pass
                stored = self.segment_cache.get(url)
        if stored is not None and stored[0] > self.clock():
            self.segment_cache.move_to_end(url)
            cached_segment = stored[1]
            self._remember(entry, index, cached_segment)
            if request.method == "GET":
                return Response(
                    cached_segment,
                    media_type="video/mp2t",
                    headers=HEADERS | {"Content-Length": str(len(cached_segment))},
                )
        elif stored is not None:
            self.segment_cache.pop(url, None)
        cached = getattr(entry, "first_segment", b"")
        if index == 0 and not cached and entry.segments:
            url = entry.segments[0]
            stored = self.first_cache.get(url)
            if stored is not None and stored[0] > self.clock():
                self.first_cache.move_to_end(url)
                cached = stored[1]
            elif stored is not None:
                self.first_cache.pop(url, None)
            if not cached:
                task = self.warm.pop(url, None)
                if task is not None:
                    try:
                        async with asyncio.timeout(45):
                            await asyncio.shield(task)
                    except (TimeoutError, asyncio.CancelledError):
                        pass
                stored = self.first_cache.get(url)
                if stored is not None and stored[0] > self.clock():
                    self.first_cache.move_to_end(url)
                    cached = stored[1] or cached
                elif stored is not None:
                    self.first_cache.pop(url, None)
        if index == 0 and cached:
            self._remember(entry, 0, cached)
            if plan is not None and plan.index is not None and plan.index.video_codec == "hevc":
                cached = await asyncio.to_thread(_repair_h264_level, cached)
            headers = HEADERS | {"Content-Length": str(len(cached))}
            if request.method == "HEAD":
                return Response(b"", headers=headers)
            if byte_range:
                start_text, end_text = byte_range.removeprefix("bytes=").split("-")
                start = int(start_text)
                end = min(int(end_text) if end_text else len(cached) - 1, len(cached) - 1)
                if start > end:
                    return Response(status_code=416, headers=HEADERS)
                body = cached[start : end + 1]
                return Response(
                    body,
                    status_code=206,
                    media_type="video/mp2t",
                    headers=HEADERS
                    | {
                        "Content-Range": f"bytes {start}-{end}/{len(cached)}",
                        "Content-Length": str(len(body)),
                    },
                )
            return Response(cached, media_type="video/mp2t", headers=headers)
        if self.reads.locked():
            raise DebridLibraryError("Playback is busy. Try again shortly.", 429)
        async with self.reads:
            try:
                # Real-Debrid starts the transcode when the first segment is
                # requested. On a cold file segment one can take a minute or be
                # permanently empty while the following segments are already
                # available. Prefer the exact first segment; if it is slow,
                # serve the next segment in its place so playback still starts
                # (one short repeated moment instead of a failed open).
                if index == 0:
                    async with asyncio.timeout(115):

                        async def attempt(segment, timeout):
                            try:
                                return await self._read(
                                    segment,
                                    limit=_SEGMENT_LIMIT,
                                    method=request.method,
                                    byte_range=byte_range,
                                    authorize=authorize,
                                    timeout=timeout,
                                    attempts=1,
                                )
                            except DebridLibraryError as error:
                                if error.status not in {502, 503, 504}:
                                    raise
                                return None

                        result = await attempt(entry.segments[0], 15)
                        if result is None and len(entry.segments) > 1 and not byte_range:
                            result = await attempt(entry.segments[1], 20)
                        if result is None:
                            result = await attempt(entry.segments[0], 60)
                        if result is None:
                            raise _unavailable()
                        data, status, upstream = result
                else:
                    async with asyncio.timeout(65):
                        # One long attempt inside a wider budget: the previous
                        # 35 s wrapper around two 20 s attempts could never let
                        # the retry finish, so a slow but valid provider segment
                        # always surfaced as a 502.
                        data, status, upstream = await self._read(
                            entry.segments[index],
                            limit=_SEGMENT_LIMIT,
                            method=request.method,
                            byte_range=byte_range,
                            authorize=authorize,
                            timeout=60,
                            attempts=1,
                        )
            except TimeoutError:
                raise _unavailable() from None
        if request.method == "GET" and status == 200 and not byte_range and data:
            self._cache_segment(url, data)
            self._remember(entry, index, data)
        headers = HEADERS.copy()
        if status == 206:
            value = upstream.get("content-range", "")
            range_match = re.fullmatch(r"bytes ([0-9]{1,9})-([0-9]{1,9})/([0-9]{1,9})", value)
            if range_match is None:
                raise _unavailable()
            left, right, total = map(int, range_match.groups())
            requested = byte_range.removeprefix("bytes=").split("-")
            expected_end = min(int(requested[1]), total - 1) if requested[1] else total - 1
            if not (left == int(requested[0]) and left <= right == expected_end < total):
                raise _unavailable()
            if request.method == "GET" and len(data) != right - left + 1:
                raise _unavailable()
            headers["Content-Range"] = value
        if (
            plan is not None
            and plan.index is not None
            and plan.index.video_codec == "hevc"
            and request.method == "GET"
            and status == 200
        ):
            data = await asyncio.to_thread(_repair_h264_level, data)
        length = upstream.get("content-length", "")
        if request.method == "HEAD" and length.isdecimal() and len(length) <= 9:
            headers["Content-Length"] = length
        return Response(data, status_code=status, media_type="video/mp2t", headers=headers)
