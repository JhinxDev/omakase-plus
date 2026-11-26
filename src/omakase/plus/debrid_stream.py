"""Same-origin delivery of a Real-Debrid Apple HLS stream."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from urllib.parse import urljoin, urlsplit

import httpx
from starlette.responses import Response

from omakase.plus.debrid_library import CloudSource, DebridLibraryError
from omakase.plus.debrid_pgs import BitmapError, render_window
from omakase.plus.debrid_subs import SubtitleCache
from omakase.plus.http_pool import client_scope
from omakase.plus.native_hls import (
    _AUDIO_INIT,
    _AUDIO_PLAYLIST,
    _AUDIO_SEGMENT,
    _SUBTITLE_PLAYLIST,
    _SUBTITLE_SEGMENT,
    _VIDEO_SEGMENT,
    NativeError,
    NativeStreams,
)
from omakase.plus.outbound import OutboundError, _resolved_ips, blocked_address, pin_destination
from omakase.plus.playback_diagnostics import diagnostic_id, warn

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
# A source whose only subtitle work is copying embedded text tracks to WebVTT
# costs a demux and no decode, so the first index can wait for it too. The
# probe wait is how the index learns which kind of job it is holding: the track
# list arrives when ffprobe returns, and only then can a seconds-long text copy
# be told apart from a minutes-long OCR or ASR job.
_SUBTITLE_PROBE_WAIT = 6.0
_TEXT_SUBTITLE_WAIT = 10.0
# Keep a forward window of provider segments warm so a resume or a seek never
# has to wait for Real-Debrid to generate the next segment mid-playback.
_SEGMENT_READAHEAD = 8
_SEGMENT_WAIT = 4.0
# AVPlayer abandons a segment after roughly five seconds and reports
# CoreMedia -12889, so a provider segment gets this first budget. If it is still
# missing, the same fetch gets one short second window and the request then fails
# with a retryable status. The fallback never serves another segment's bytes: the
# player caches the response under the requested URL, so a substituted segment
# would show the wrong moment for the rest of the session.
_SEGMENT_GRACE = 2.5
_SEGMENT_RETRY_WAIT = 2.0
_BROWSER_SEGMENT_RETRY_WAIT = 15.0
_SEGMENT_CACHE_ENTRIES = 64
_SEGMENT_CACHE_BYTES = 96 * 1024 * 1024
_SEGMENT_TTL = 180.0
# A cold stream-copy segment costs about five seconds (the source bitrate is
# copied, not reduced), so the index waits for the player's opening segment
# instead of letting AVPlayer abandon its five-second segment budget.
_NATIVE_OPEN_WAIT = 30
_NATIVE_WARM_WAIT = 30
_NATIVE_OPEN_WINDOWS = 2
_NATIVE_SUBTITLE_LIMIT = 6
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


def _binary_reply(body, media_type, request):
    headers = HEADERS | {"Content-Length": str(len(body))}
    return Response(
        b"" if request.method == "HEAD" else body,
        media_type=media_type,
        headers=headers,
    )


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
    stream = "#EXT-X-STREAM-INF:BANDWIDTH=40000000"
    if job is not None:
        stream += ',SUBTITLES="subs"'
    lines.append(stream)
    lines.append("stream.m3u8")
    return ("\n".join(lines) + "\n").encode("utf-8")


_VTT_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})$")


def _shift_webvtt(data, offset):
    """Move every cue earlier by ``offset`` seconds, clamped at zero."""
    if not offset or abs(offset) < 0.02 or not data:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    out = []
    for line in text.splitlines():
        if "-->" not in line:
            out.append(line)
            continue
        parts = line.split("-->")
        shifted = []
        for part in parts:
            token = part.strip()
            pieces = token.split(" ")
            stamp = pieces[0]
            match = _VTT_TIME.match(stamp)
            if not match:
                shifted.append(part)
                continue
            hours, minutes, seconds, millis = (int(value) for value in match.groups())
            total = hours * 3600 + minutes * 60 + seconds + millis / 1000 - offset
            total = max(0.0, total)
            whole = int(total)
            stamp = (
                f"{whole // 3600:02d}:{(whole % 3600) // 60:02d}:{whole % 60:02d}"
                f".{round((total - whole) * 1000):03d}"
            )
            pieces[0] = stamp
            shifted.append(" ".join(pieces))
        out.append(" --> ".join(shifted))
    return ("\n".join(out) + "\n").encode("utf-8")


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
    # Set when this entry is served by the bounded native HLS timeline.
    native_key: str = ""


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
    # Set when the client plays the original file itself. A direct pass has no
    # timeline here at all, so nothing in this registry can answer it with
    # generated bytes.
    direct: object = field(default=None, repr=False)
    # Set when this pass is served by the bounded native HLS timeline that
    # AVPlayer plays with Apple's own controls.
    native_key: str = ""


def subtitle_cache_key(source):
    direct = getattr(source, "direct", "")
    path = urlsplit(direct).path if direct else getattr(source, "media_version", "")
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _record_phase(plan, name, started):
    """Record one startup phase on the plan, in whole milliseconds.

    A saved-position open spends its time in distinct phases: resolving the
    provider file, building the timeline, holding for caption readiness and
    preparing the first playable window. Keeping them apart is what lets a slow
    title be attributed to a phase instead of to "the player".
    """
    phases = getattr(plan, "phases", None)
    if phases is None:
        return
    phases[name] = round((time.monotonic() - started) * 1000)


class DebridStreams:
    def __init__(
        self,
        transport=None,
        clock=time.monotonic,
        subtitle_root=None,
        native_root=None,
        subtitle_embedded_only=False,
    ):
        self.transport = transport
        self.clock = clock
        self.subtitles = SubtitleCache(subtitle_root, clock, embedded_only=subtitle_embedded_only)
        self.native = NativeStreams(native_root, clock)
        # A restart must not inherit another session's windows.
        self.native.reset()
        self.passes = {}
        self.reads = asyncio.Semaphore(8)
        self.bitmap_reads = asyncio.Semaphore(2)
        self.bitmap_requests = {}
        # First segments that are being warmed without blocking the play reply.
        self.warm = OrderedDict()
        self.first_cache = OrderedDict()
        self.segment_cache = OrderedDict()
        self.media_cache = OrderedDict()
        self.cached_bytes = 0
        self.pumps = OrderedDict()

    @staticmethod
    def _consume_task_result(task):
        if task.done() and not task.cancelled():
            task.exception()

    @staticmethod
    def _same_loop(task) -> bool:
        try:
            return task.get_loop() is asyncio.get_running_loop()
        except RuntimeError:
            return False

    @classmethod
    def _reusable_warm(cls, task) -> bool:
        return task is not None and not task.cancelled() and cls._same_loop(task)

    @staticmethod
    async def _await_warm(task, timeout: float) -> None:
        """Wait on a shared warm task without letting its own cancellation fail the caller."""
        try:
            async with asyncio.timeout(timeout):
                await asyncio.shield(task)
        except TimeoutError:
            return
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise

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
        pump_key = media.segments[0]
        state = self.pumps.get(pump_key)
        if state is None or state["segments"] is not media.segments:
            state = {
                "segments": media.segments,
                "next": index + 1,
                "target": target,
                "task": None,
            }
            self.pumps[pump_key] = state
        else:
            state["next"] = max(state["next"], index + 1)
            state["target"] = max(state["target"], target)
        self.pumps.move_to_end(pump_key)
        while len(self.pumps) > 4:
            _, evicted = self.pumps.popitem(last=False)
            evicted_task = evicted.get("task")
            if evicted_task is not None and not evicted_task.done():
                evicted_task.cancel()
        task = state.get("task")
        if task is not None and not task.done() and self._same_loop(task):
            return
        pump = self._pump(state)
        try:
            task = asyncio.create_task(pump)
            task.add_done_callback(self._consume_task_result)
            state["task"] = task
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
                owns = not self._reusable_warm(task) or task.done()
                if owns:
                    task = asyncio.create_task(self._fetch_segment(url))
                    task.add_done_callback(self._consume_task_result)
                    self.warm[url] = task
                try:
                    await self._await_warm(task, 65)
                except asyncio.CancelledError:
                    if owns:
                        task.cancel()
                    return
                finally:
                    if self.warm.get(url) is task:
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
                client_scope(
                    key="debrid-stream",
                    transport=self.transport,
                    sni_hostname=destination.sni_hostname,
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

    def drop_pass(self, key):
        grant = self.passes.pop(key, None)
        if grant and grant.native_key:
            if task := self.bitmap_requests.get(grant.native_key):
                task.cancel()
            self.native.close(grant.native_key)

    def prune(self):
        now = self.clock()
        for key, entry in tuple(self.passes.items()):
            if entry.expires <= now:
                self.drop_pass(key)
        self.native.sweep()

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
        if not self.subtitles.embedded_only:
            await self._subtitle_ready(job)
        medias = [replace(media, subtitle_key=subtitle_key) for media in medias]
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
        except (DebridLibraryError, TimeoutError):
            return None
        if status == 200 and data:
            self._cache_segment(url, data)
        return data

    def _start_warm(self, media):
        if not media.segments:
            return
        url = media.segments[0]
        existing = self.warm.get(url)
        if existing is not None and not existing.done() and self._same_loop(existing):
            return
        if existing is not None:
            self.warm.pop(url, None)
        stored = self.first_cache.get(url)
        if stored is not None and stored[0] > self.clock():
            self.first_cache.move_to_end(url)
            return
        while len(self.warm) >= 8:
            _, evicted = self.warm.popitem(last=False)
            if not evicted.done():
                evicted.cancel()
        task = asyncio.create_task(self._fetch_warm(media))
        task.add_done_callback(self._consume_task_result)
        self.warm[url] = task

    async def _fetch_warm(self, media):
        try:
            data, _, _ = await self._read(
                media.segments[0], limit=_SEGMENT_LIMIT, timeout=45, attempts=1
            )
        except (DebridLibraryError, TimeoutError):
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

    async def issue(
        self, source, user_id, binding, ttl, *, direct=None, native=None, subtitle_key=""
    ):
        self.prune()
        if len(self.passes) >= 64:
            raise DebridLibraryError("Playback is busy. Try again shortly.", 429)
        if native is not None:
            # The native pass owns the whole timeline: AVPlayer plays Apple's own
            # controls over a VOD playlist this registry generates window by
            # window. No provider playlist and no prepared title are involved.
            media = DebridMedia(b"", (), subtitle_key=subtitle_key)
        elif direct is not None:
            # A direct pass forwards provider ranges, so it needs no provider HLS
            # playlist at all. That is the point: original-file playback must not
            # depend on transcode readiness.
            media = DebridMedia(b"", ())
        else:
            media = await self.prepare(source)
        for key, existing in tuple(self.passes.items()):
            if existing.binding == binding:
                self.drop_pass(key)
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
            direct,
            native.key if native is not None else "",
        )
        if native is not None:
            asyncio.get_running_loop().call_later(ttl, self.drop_pass, key)
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

    async def _wait_for_subtitles(self, job):
        """Hold a first index long enough to carry the SUBTITLES group.

        AVPlayer fetches the master playlist once per session, so an index that
        opens without a subtitle group leaves the television with no subtitle
        menu for the whole play. Waiting is therefore worth a few seconds, but
        only for work that finishes in seconds: cached output, or a text-track
        copy. OCR and ASR can run for minutes and are never waited for.
        """
        if job is None or job.done.is_set():
            return
        if not job.warm and job.text_only is None:
            # Wait for the decision, not the work: once the source is known to
            # need OCR or ASR, the index is released instead of holding the
            # player for minutes.
            if job.task is None:
                return
            await self.subtitles.wait_decision(job, _SUBTITLE_PROBE_WAIT)
        if job.done.is_set():
            return
        if job.warm:
            await self.subtitles.wait_tracks(job, _WARM_SUBTITLE_WAIT)
        elif job.text_only:
            await self.subtitles.wait_tracks(job, _TEXT_SUBTITLE_WAIT)

    async def _native_response(self, entry, plan, asset, request):
        """Serve one asset of the bounded native timeline.

        The timeline is complete before the first playlist is answered, so a seek
        anywhere in the episode is a segment request rather than a rebuild. Only
        the requested window is generated, and its own content clock is patched
        in, so far seeks never walk the file from the beginning.
        """
        job = self.subtitles.get(getattr(entry, "subtitle_key", ""))
        tracks = tuple(getattr(job, "tracks", ()) or ())[:_NATIVE_SUBTITLE_LIMIT]
        if plan.web_codecs and (sidecar := re.fullmatch(r"subtitles([0-9]{1,2})\.vtt", asset)):
            track = await self._subtitle_track(job, int(sidecar[1]))
            offset = plan.timeline.origin if plan.timeline is not None else 0
            return _binary_reply(_shift_webvtt(track.data, offset), "text/vtt", request)
        if asset == "index.m3u8":

            async def caption_tracks():
                started = time.monotonic()
                if plan.web_codecs:
                    result = ()
                else:
                    await self._wait_for_subtitles(job)
                    result = tuple(getattr(job, "tracks", ()) or ())[:_NATIVE_SUBTITLE_LIMIT]
                _record_phase(plan, "captions", started)
                return result

            async def prepare_opening(subtitles):
                started = time.monotonic()
                try:
                    await self._prepare_native_opening(plan, timeline, request, subtitles)
                except NativeError as error:
                    return error
                _record_phase(plan, "opening", started)
                return None

            started = time.monotonic()
            timeline = await self.native.ready(plan, _NATIVE_OPEN_WAIT)
            _record_phase(plan, "timeline", started)
            if timeline is None:
                warn(_LOG, "native_open", "unsupported_media", "native", plan.diagnostic_id)
                device = "web" if plan.web_codecs else "Apple TV"
                raise DebridLibraryError(f"This copy cannot use the {device} player yet.", 415)
            if (
                plan.continuous
                and not plan.web_codecs
                and job is not None
                and request.method != "HEAD"
            ):
                # Continuous media windows and caption discovery have independent
                # output. Keep caption tracks in the first master playlist.
                async with asyncio.TaskGroup() as tasks:
                    captions = tasks.create_task(caption_tracks())
                    opening = tasks.create_task(prepare_opening(1))
                tracks, error = captions.result(), opening.result()
            else:
                tracks = await caption_tracks()
                error = await prepare_opening(len(tracks))
            if error is not None:
                warn(_LOG, "native_open", error.code, "native", plan.diagnostic_id)
                raise DebridLibraryError("This position could not be prepared.", 503) from None
            return _playlist_reply(self.native.master_playlist(plan, tracks), request)
        if asset == "stream.m3u8":
            return _playlist_reply(self.native.video_playlist(plan), request)
        if match := _AUDIO_PLAYLIST.fullmatch(asset):
            return _playlist_reply(self.native.audio_playlist(plan, int(match[1])), request)
        if match := _SUBTITLE_PLAYLIST.fullmatch(asset):
            number = int(match[1])
            if job is None or number >= len(tracks):
                raise DebridLibraryError("This subtitle track is unavailable.", 404)
            return _playlist_reply(self.native.subtitle_playlist(plan, number), request)
        if asset == "init.mp4" or _AUDIO_INIT.fullmatch(asset):
            path = plan.root / asset
            # Continuous media uses a pinned init for the full pass. Re-reading
            # it after eviction or a seek must not restart the opening window.
            if not (plan.continuous and path.is_file()):
                number = 0
                if plan.timeline is not None and plan.continuous and plan.warm is not None:
                    number = self.native._position_index(plan.timeline, plan.warm)
                await self._native_window(plan, number, len(tracks))
            if not path.is_file():
                raise DebridLibraryError("This stream is unavailable.", 404)
            return _binary_reply(path.read_bytes(), "video/mp4", request)
        if match := _VIDEO_SEGMENT.fullmatch(asset):
            number = int(match[1])
            timeline = plan.timeline
            if timeline is None or not 0 <= number < len(timeline.segments):
                raise DebridLibraryError("This stream is unavailable.", 404)
            await self._native_window(plan, number, len(tracks))
            path = plan.video_segment_path(number)
            if not path.is_file():
                raise DebridLibraryError("This stream is unavailable.", 404)
            return _binary_reply(path.read_bytes(), "video/mp4", request)
        if match := _AUDIO_SEGMENT.fullmatch(asset):
            ordinal, number = int(match[1]), int(match[2])
            timeline = plan.timeline
            audio = (
                next((item for item in timeline.audios if item.ordinal == ordinal), None)
                if timeline is not None
                else None
            )
            if audio is None:
                raise DebridLibraryError("This audio track is unavailable.", 404)
            if not 0 <= number < len(timeline.segments):
                raise DebridLibraryError("This stream is unavailable.", 404)
            await self._native_window(plan, number, len(tracks))
            path = plan.audio_segment_path(audio, number)
            if not path.is_file():
                raise DebridLibraryError("This stream is unavailable.", 404)
            return _binary_reply(path.read_bytes(), "video/mp4", request)
        if match := _SUBTITLE_SEGMENT.fullmatch(asset):
            track, number = int(match[1]), int(match[2])
            if job is None or track >= len(tracks):
                raise DebridLibraryError("This subtitle track is unavailable.", 404)
            timeline = plan.timeline
            if timeline is None or not 0 <= number < len(timeline.segments):
                raise DebridLibraryError("This subtitle is unavailable.", 404)
            await self._native_subtitles(plan, number, len(tracks), request)
            path = plan.subtitle_segment_path(track, number)
            if not path.is_file():
                raise DebridLibraryError("This subtitle is unavailable.", 404)
            return _binary_reply(path.read_bytes(), "text/vtt", request)
        raise DebridLibraryError("This stream is unavailable.", 404)

    async def _prepare_native_opening(self, plan, timeline, request, tracks):
        async with plan.opening_lock:
            if plan.continuous:
                if request.method == "HEAD":
                    return
                hint = request.query_params.get("start_ms", "")
                if plan.opening_prepared:
                    shifted = False
                    if (
                        plan.prewarmed_opening is not None
                        and plan.warm is None
                        and re.fullmatch(r"[0-9]{1,8}", hint)
                    ):
                        position = int(hint) / 1000
                        if 0 < position < timeline.duration:
                            target = self.native._position_index(timeline, position)
                            if target != plan.prewarmed_opening:
                                plan.warm = position
                                plan.prewarmed_opening = None
                                shifted = True
                    if not shifted:
                        return
                elif (
                    plan.warm is None and plan.session is None and re.fullmatch(r"[0-9]{1,8}", hint)
                ):
                    position = int(hint) / 1000
                    if 0 < position < timeline.duration:
                        plan.warm = position

            opening = 0 if plan.warm is None else self.native._position_index(timeline, plan.warm)
            # The continuous reader already reads ahead within its budget.
            # Let the player begin with the first complete window while that
            # reader prepares subsequent windows.
            windows = 1 if plan.continuous else _NATIVE_OPEN_WINDOWS
            for number in range(opening, min(opening + windows, len(timeline.segments))):
                await self.native.ensure(plan, number, _NATIVE_WARM_WAIT, tracks)
            plan.opening_prepared = True
            plan.prewarmed_opening = None

    async def _native_window(self, plan, number, tracks):
        try:
            await self.native.ensure(plan, number, _NATIVE_WARM_WAIT, tracks)
        except NativeError as error:
            warn(_LOG, "native_delivery", error.code, "native", plan.diagnostic_id)
            raise DebridLibraryError("This position could not be prepared.", 503) from None

    async def _native_subtitles(self, plan, number, tracks, request):
        if self.native._subtitle_ready(plan, number, tracks):
            return
        try:
            await self._while_connected(
                request, asyncio.create_task(self.native.ensure_subtitles(plan, number, tracks))
            )
        except NativeError as error:
            warn(_LOG, "native_delivery", error.code, "native", plan.diagnostic_id)
            raise DebridLibraryError("This subtitle is unavailable.", 503) from None

    async def _while_connected(self, request, generation):
        async def wait_for_disconnect():
            try:
                while (await request.receive())["type"] != "http.disconnect":
                    pass
            except (RuntimeError, OSError):
                return

        disconnect = asyncio.create_task(wait_for_disconnect())
        try:
            done, _ = await asyncio.wait(
                (generation, disconnect), return_when=asyncio.FIRST_COMPLETED
            )
            if generation not in done or generation.cancelled():
                raise DebridLibraryError("This subtitle request was closed.", 499)
            return generation.result()
        finally:
            disconnect.cancel()
            generation.cancel()
            # An ASGI receive can remain suspended until this response is sent.
            # Await producer cleanup, never transport teardown.
            await asyncio.gather(generation, return_exceptions=True)

    async def deliver_bitmap(self, entry, ordinal, position, request):
        plan = self.native.get(entry.native_key)
        timeline = plan.timeline if plan and plan.web_codecs else None
        if timeline is None or not any(
            track.ordinal == ordinal for track in timeline.bitmap_subtitles
        ):
            raise DebridLibraryError("This subtitle track is unavailable.", 404)
        if not 0 <= position < timeline.duration:
            raise DebridLibraryError("Invalid subtitle position.", 422)
        if self.bitmap_reads.locked() or plan.key in self.bitmap_requests:
            raise DebridLibraryError("Subtitles are still preparing.", 503)
        async with self.bitmap_reads:
            number = self.native._position_index(timeline, position)
            generation = asyncio.create_task(render_window(self.native, plan, number, ordinal))
            self.bitmap_requests[plan.key] = generation
            try:
                return await self._while_connected(request, generation)
            except (BitmapError, NativeError) as error:
                warn(
                    _LOG, "bitmap_delivery", "generation_unavailable", "native", plan.diagnostic_id
                )
                raise DebridLibraryError("This subtitle window is unavailable.", 503) from error
            finally:
                self.bitmap_requests.pop(plan.key, None)

    async def deliver(self, entry, asset, request, authorize: Callable[[], None]):
        authorize()
        request_id = diagnostic_id()
        native_key = getattr(entry, "native_key", "")
        if native_key and asset:
            plan = self.native.get(native_key)
            if plan is None:
                # The timeline this pass was opened on is gone. Answering from a
                # different timeline would be the substitution this work removes.
                raise DebridLibraryError("This stream expired. Reopen the title.", 410)
            return await self._native_response(entry, plan, asset, request)
        job = self.subtitles.get(getattr(entry, "subtitle_key", ""))
        if asset == "index.m3u8":
            if not self.subtitles.embedded_only:
                await self._wait_for_subtitles(job)
            if job is not None and job.tracks and all(track.data for track in job.tracks):
                return _playlist_reply(_master_playlist(entry, job), request)
            # AVPlayer rejects the provider's master wrapper with CoreMedia
            # -12971. Serve the single provider media playlist directly; the
            # provider already chose one quality. Text subtitles are requested
            # separately and do not require a master for this path.
            _LOG.debug("provider media playlist selected")
            return _playlist_reply(entry.playlist, request)
        if asset == "stream.m3u8":
            return _playlist_reply(entry.playlist, request)
        if match := re.fullmatch(r"subtitles([0-9]{1,2})\.m3u8", asset):
            track = await self._subtitle_track(job, int(match[1]))
            return _playlist_reply(_subtitle_playlist(entry, int(match[1])), request)
        if match := re.fullmatch(r"subtitles([0-9]{1,2})\.vtt", asset):
            track = await self._subtitle_track(job, int(match[1]))
            body = track.data
            headers = HEADERS | {"Content-Length": str(len(body))}
            return Response(
                b"" if request.method == "HEAD" else body,
                media_type="text/vtt",
                headers=headers,
            )
        match = re.fullmatch(r"([0-9]{5})\.(ts|m4s)", asset)
        if not match:
            raise DebridLibraryError("This stream is unavailable.", 404)
        index = int(match[1])
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
            if not self._reusable_warm(task) or task.done():
                task = asyncio.create_task(self._fetch_segment(url))
                task.add_done_callback(self._consume_task_result)
                self.warm[url] = task
            await self._await_warm(task, _SEGMENT_GRACE)
            stored = self.segment_cache.get(url)
            if stored is None or stored[0] <= self.clock():
                # The provider has not delivered this segment inside the player's
                # first budget. Serving another segment's bytes would show the
                # wrong moment, and the player would cache it under this URL for
                # the rest of the session. Give the same fetch a short second
                # window, then fail with a status the player may retry.
                grace = (
                    _BROWSER_SEGMENT_RETRY_WAIT
                    if self.subtitles.embedded_only
                    else _SEGMENT_RETRY_WAIT
                )
                if not self._reusable_warm(task) or (task.done() and task.cancelled()):
                    task = asyncio.create_task(self._fetch_segment(url))
                    task.add_done_callback(self._consume_task_result)
                    self.warm[url] = task
                if not task.done():
                    await self._await_warm(task, grace)
                stored = self.segment_cache.get(url)
                if stored is None or stored[0] <= self.clock():
                    warn(_LOG, "provider_delivery", "segment_not_ready", "provider", request_id)
                    raise DebridLibraryError(
                        "Real-Debrid is not delivering this part of the video right now.",
                        503,
                    )
        if stored is not None and stored[0] > self.clock():
            self.segment_cache.move_to_end(url)
            cached_segment = stored[1]
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
                if self._reusable_warm(task):
                    await self._await_warm(task, 45)
                stored = self.first_cache.get(url)
                if stored is not None and stored[0] > self.clock():
                    self.first_cache.move_to_end(url)
                    cached = stored[1] or cached
                elif stored is not None:
                    self.first_cache.pop(url, None)
        if index == 0 and cached:
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

                        result = None
                        for budget in (15, 20, 60):
                            result = await attempt(entry.segments[0], budget)
                            if result is not None:
                                break
                        if result is None:
                            # Never answer the first segment with the second one's
                            # bytes: the player caches the response under this URL,
                            # so it would play a moment from later in the film and
                            # keep doing it. A retryable failure lets it retry the
                            # same segment instead.
                            raise DebridLibraryError(
                                "Real-Debrid is not delivering this part of the video right now.",
                                503,
                            )
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
        length = upstream.get("content-length", "")
        if request.method == "HEAD" and length.isdecimal() and len(length) <= 9:
            headers["Content-Length"] = length
        return Response(data, status_code=status, media_type="video/mp2t", headers=headers)
