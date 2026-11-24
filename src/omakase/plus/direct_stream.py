"""Direct original-file delivery from Real-Debrid through bounded HTTP ranges.

This is the transport behind the low-storage playback direction. The server
forwards only the bytes the player asks for, so a title never has to exist as a
local file: no preparation, no segment store, no client-side video cache. The
player receives an authenticated, same-origin URL and issues its own range
requests; seeking asks the provider for a different range instead of waiting for
a generated timeline.

Three properties are load bearing and are enforced here rather than assumed by
callers:

* **Identity, not URL, is the session.** A Real-Debrid link is transport state and
  rotates. A stream is bound to ``identity``/``version``/``size`` captured when the
  pass was issued, and every upstream reply is checked against that byte size.
  A refreshed link that describes different content is refused; it can never
  become "the same segment" of a different timeline.
* **Honest failure beats substitution.** If the provider will not serve the
  requested range, or stalls, the player gets an error status it may retry. It is
  never answered with bytes from elsewhere in the file.
* **Nothing accumulates.** Bytes are read in fixed chunks, re-authorized while the
  body is in flight, and dropped as they are forwarded. There is no temp file,
  no write, and no full-file read anywhere on this path.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from omakase.plus.http_pool import acquire_client
from omakase.plus.outbound import OutboundError, _resolved_ips, blocked_address, pin_destination
from omakase.plus.web_playback_delivery import DeliveryError, parse_range

HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Accept-Ranges": "bytes",
}

# One bounded read size. This is the whole player-facing buffer: a range body is
# forwarded chunk by chunk and never accumulated, so process memory does not grow
# with title size.
CHUNK = 64 * 1024

# A remote file may be probed with a single small range to learn its real size.
_PROBE_BYTES = 1

_EXPIRED = {401, 403, 404, 410, 451}
_VIDEO_TYPES = {
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "ts": "video/mp2t",
    "m2ts": "video/mp2t",
}
_SUBTITLE_TYPES = {
    "ass": "text/x-ssa",
    "ssa": "text/x-ssa",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}


class DirectError(DeliveryError):
    """A direct-stream failure with a status the player may act on."""


def media_type(container: str) -> str:
    return _VIDEO_TYPES.get(container) or "application/octet-stream"


def subtitle_type(container: str) -> str:
    return _SUBTITLE_TYPES.get(container) or "text/plain"


def container_of(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].casefold() if "." in name else ""
    return suffix if suffix in _VIDEO_TYPES else ""


@dataclass(frozen=True, slots=True)
class DirectSource:
    """Immutable content identity for one playable original file.

    ``version`` folds the account binding, provider content hash, name and byte
    size. Those are the facts a rotated download URL must still satisfy.
    """

    identity: str
    version: str
    name: str
    size: int
    container: str
    duration: float

    @property
    def media_type(self) -> str:
        return media_type(self.container)


def source_for(cloud, *, size: int) -> DirectSource | None:
    """Build a direct source from a resolved cloud file, or ``None`` if unusable.

    A zero size means the provider did not report a byte length. Without a length
    a range cannot be validated against content identity, so the direct path is
    not offered rather than being offered unverifiably.
    """
    identity = getattr(cloud, "identity", "")
    version = getattr(cloud, "media_version", "")
    name = getattr(cloud, "title", "")
    duration = getattr(cloud, "duration", 0.0)
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
        return None
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return None
    if type(size) is not int or not 0 < size < 2**62:
        return None
    container = container_of(name if isinstance(name, str) else "")
    if not container:
        return None
    if type(duration) not in (int, float) or not duration > 0:
        return None
    return DirectSource(identity, version, name, size, container, float(duration))


@dataclass(frozen=True, slots=True)
class RefreshedOrigin:
    url: str = field(repr=False)
    identity: str
    version: str
    size: int


async def _public_addresses(host: str, port: int):
    addresses = await _resolved_ips(host, port)
    if any(blocked_address(address, allow_private=False) for address in addresses):
        raise OutboundError("unsafe_destination")
    return addresses


@dataclass
class DirectStream:
    """One authenticated playback pass over a remote original file.

    ``origin`` is mutable transport state: the provider rotates it. Everything
    else is pinned. ``refresh`` re-resolves the same content identity and must
    return a new URL for it, or raise.
    """

    source: DirectSource
    origin: str = field(repr=False)
    refresh: Callable[[], Awaitable[RefreshedOrigin | None]] | None = field(
        default=None, repr=False
    )
    bytes_sent: int = 0
    ranges: int = 0
    refreshes: int = 0
    requests: int = 0
    opened_at: float = field(default_factory=time.monotonic)
    _refresh_task: asyncio.Task[bool] | None = field(default=None, init=False, repr=False)
    _refresh_origin: str = field(default="", init=False, repr=False)

    @property
    def etag(self) -> str:
        return '"' + self.source.version + '"'

    def snapshot(self) -> dict[str, object]:
        """Bounded, credential-free delivery accounting for logs and receipts."""
        return {
            "identity": self.source.identity[:12],
            "version": self.source.version[:12],
            "bytes": self.bytes_sent,
            "ranges": self.ranges,
            "requests": self.requests,
            "refreshes": self.refreshes,
            "seconds": round(time.monotonic() - self.opened_at, 3),
        }

    def validate(self) -> None:
        if not _origin_url(self.origin):
            raise DirectError("This source is unavailable.", 502)
        if type(self.source.size) is not int or self.source.size <= 0:
            raise DirectError("This source is unavailable.", 502)

    def completed_refresh(self) -> asyncio.Task[bool] | None:
        task = self._refresh_task
        return task if task is not None and task.done() else None

    async def refresh_origin(
        self, failed_origin: str, previous_attempt: asyncio.Task[bool] | None
    ) -> bool:
        """Share one resolver call across requests that failed on the same link."""
        if self.origin != failed_origin:
            return True
        if self.refresh is None:
            return False
        task = self._refresh_task
        if task is None or self._refresh_origin != failed_origin or task is previous_attempt:
            self._refresh_origin = failed_origin
            task = asyncio.create_task(self._resolve_origin())
            self._refresh_task = task
            # The initiating player may disconnect while other ranges need the
            # result. Observe failures even if all waiters have been cancelled.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)

    async def _resolve_origin(self) -> bool:
        self.refreshes += 1
        try:
            async with asyncio.timeout(60):
                fresh = await self.refresh()
        except DirectError:
            raise
        except Exception:
            return False
        if fresh is None:
            return False
        if (fresh.identity, fresh.version, fresh.size) != (
            self.source.identity,
            self.source.version,
            self.source.size,
        ):
            raise DirectError("This copy changed on the provider. Reopen the title.", 409)
        if not _origin_url(fresh.url):
            return False
        self.origin = fresh.url
        return True


def _origin_url(value: str) -> httpx.URL | None:
    if not isinstance(value, str) or not re.fullmatch(r"https://[^\s\\#]{1,4000}", value):
        return None
    try:
        url = httpx.URL(value)
        if url.host and not url.userinfo and "@" not in value.split("/", 3)[2]:
            return url
    except (httpx.InvalidURL, UnicodeError, ValueError):
        pass
    return None


class _ClosingStream(StreamingResponse):
    """Close upstream resources even when the player disconnects mid-body."""

    def __init__(self, *args, close, **kwargs):
        super().__init__(*args, **kwargs)
        # Exposed as ``close`` so callers (and tests) can release the provider
        # connection deterministically instead of waiting for collection.
        self.close = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            result = self.close()
            if hasattr(result, "__await__"):
                await result


async def _authorize(callback: Callable) -> None:
    result = callback()
    if hasattr(result, "__await__"):
        result = await result
    if result is False:
        raise DeliveryError("Playback access changed.", 403)


def _upstream_headers(bounds) -> dict[str, str]:
    headers = {"Accept-Encoding": "identity", "Accept": "*/*"}
    if bounds is not None:
        headers["Range"] = f"bytes={bounds[0]}-{bounds[1]}"
    return headers


async def _send_range(
    client: httpx.AsyncClient,
    stream: DirectStream,
    origin: str,
    bounds,
    *,
    method: str,
    transport,
) -> tuple[httpx.Response, int, int, int]:
    """Open the remote body and prove it is the exact requested byte span.

    Returns the live response, the ``(left, right)`` span actually offered and the
    expected body length.
    """
    left, right = bounds if bounds is not None else (0, stream.source.size - 1)
    length = right - left + 1
    url = _origin_url(origin)
    if url is None:
        raise DirectError("This source is unavailable.", 502)
    try:
        try:
            literal = ipaddress.ip_address(url.host)
        except ValueError:
            literal = None
        if literal is not None and blocked_address(literal, allow_private=False):
            raise OutboundError("unsafe_destination")
        destination = await pin_destination(
            f"https://{url.netloc.decode('ascii')}",
            transport=transport,
            resolver=None if isinstance(transport, httpx.MockTransport) else _public_addresses,
        )
    except (OutboundError, ValueError):
        raise DirectError("This source is unavailable.", 502) from None
    upstream = await client.send(
        client.build_request(
            method,
            destination.base_url + url.raw_path.decode("ascii"),
            headers=_upstream_headers(bounds) | {"Host": destination.host_header},
            extensions={"sni_hostname": destination.sni_hostname},
        ),
        stream=True,
    )
    if upstream.status_code in _EXPIRED:
        await upstream.aclose()
        return upstream, left, right, length
    encoding = upstream.headers.get("content-encoding", "identity")
    if encoding != "identity":
        await upstream.aclose()
        raise DirectError("The provider returned a compressed body.", 502)
    if bounds is None:
        if upstream.status_code != 200 or "content-range" in upstream.headers:
            await upstream.aclose()
            raise DirectError("The provider did not return the whole file.", 502)
        declared = upstream.headers.get("content-length", "")
        if declared and declared != str(stream.source.size):
            await upstream.aclose()
            raise DirectError("This copy changed on the provider. Reopen the title.", 409)
        return upstream, left, right, length
    if upstream.status_code != 206:
        await upstream.aclose()
        # The provider answered in full instead of honouring the range. Draining
        # that body would transfer the whole title to find a few minutes of it, so
        # fail explicitly and let the player decide.
        raise DirectError("This source does not support byte ranges.", 502)
    value = upstream.headers.get("content-range", "")
    match = re.fullmatch(r"bytes ([0-9]{1,18})-([0-9]{1,18})/([0-9]{1,18})", value)
    if match is None:
        await upstream.aclose()
        raise DirectError("The provider returned an unreadable range.", 502)
    got_left, got_right, total = (int(part) for part in match.groups())
    if total != stream.source.size:
        await upstream.aclose()
        # Same link, different content. This is the substitution the audit found:
        # the player would cache these bytes under the identity it already holds.
        raise DirectError("This copy changed on the provider. Reopen the title.", 409)
    if got_left != left or got_right != right:
        await upstream.aclose()
        raise DirectError("The provider returned a different range.", 502)
    declared = upstream.headers.get("content-length", "")
    if declared and declared != str(length):
        await upstream.aclose()
        raise DirectError("The provider returned a different range.", 502)
    return upstream, left, right, length


async def open_direct(
    stream: DirectStream,
    request,
    *,
    authorize: Callable,
    transport=None,
    on_close: Callable = lambda: None,
    chunk: int = CHUNK,
) -> Response:
    """Serve one authenticated byte range of a remote original file.

    Nothing is buffered beyond ``chunk`` bytes and nothing is written to disk. A
    player that walks away closes the upstream connection through the response
    background task, so abandoned work stops at the provider rather than draining
    the file.
    """
    await _authorize(authorize)
    if request.method not in {"GET", "HEAD"}:
        raise DirectError("Unsupported media method.", 405)
    stream.validate()
    stream.requests += 1
    headers = HEADERS | {"ETag": stream.etag}
    if request.headers.get("if-none-match") in {stream.etag, "*"}:
        on_close()
        return Response(status_code=304, headers=headers)
    requested = request.headers.get("range", "")
    if requested and request.headers.get("if-range", stream.etag) != stream.etag:
        requested = ""
    try:
        bounds = parse_range(requested, stream.source.size) if requested else None
    except DeliveryError:
        on_close()
        return Response(
            status_code=416,
            headers=headers | {"Content-Range": f"bytes */{stream.source.size}"},
        )
    left, right = bounds if bounds is not None else (0, stream.source.size - 1)
    length = right - left + 1
    status = 206 if bounds is not None else 200
    reply = headers | {"Content-Length": str(length)}
    if bounds is not None:
        reply["Content-Range"] = f"bytes {left}-{right}/{stream.source.size}"
    if request.method == "HEAD":
        # A HEAD costs no provider traffic: the size is already pinned.
        on_close()
        return Response(status_code=status, headers=reply, media_type=stream.source.media_type)

    sni_host = parsed.host if (parsed := _origin_url(stream.origin)) is not None else None
    client, owns_client = acquire_client(
        key="direct-stream",
        transport=transport,
        sni_hostname=sni_host,
        timeout=httpx.Timeout(20, connect=6, read=20),
        trust_env=False,
        follow_redirects=False,
    )
    upstream = None
    origin, previous_attempt = stream.origin, stream.completed_refresh()
    try:
        try:
            upstream, left, right, length = await _send_range(
                client, stream, origin, bounds, method=request.method, transport=transport
            )
        except httpx.HTTPError:
            raise DirectError("Real-Debrid did not answer this request.", 502) from None
        except TimeoutError:
            raise DirectError("Real-Debrid did not answer this request.", 504) from None
        if upstream.status_code in _EXPIRED:
            # An expired signed link looks like this. Refresh once and ask for the
            # same span again; a different identity is refused, not served.
            if not await stream.refresh_origin(origin, previous_attempt):
                raise DirectError("This copy is no longer available from Real-Debrid.", 404)
            await _authorize(authorize)
            try:
                upstream, left, right, length = await _send_range(
                    client,
                    stream,
                    stream.origin,
                    bounds,
                    method=request.method,
                    transport=transport,
                )
            except httpx.HTTPError:
                raise DirectError("Real-Debrid did not answer this request.", 502) from None
            if upstream.status_code in _EXPIRED:
                await upstream.aclose()
                raise DirectError("This copy is no longer available from Real-Debrid.", 404)
        reply["Content-Length"] = str(length)
        if bounds is not None:
            reply["Content-Range"] = f"bytes {left}-{right}/{stream.source.size}"
            stream.ranges += 1
    except BaseException:
        if upstream is not None:
            await upstream.aclose()
        if owns_client:
            await client.aclose()
        on_close()
        raise

    closed = False

    async def close():
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            await upstream.aclose()
            if owns_client:
                await client.aclose()
        finally:
            on_close()

    async def body():
        received, checked_at = 0, 0.0
        try:
            async for piece in upstream.aiter_raw(chunk_size=chunk):
                if time.monotonic() - checked_at >= 1:
                    await _authorize(authorize)
                    checked_at = time.monotonic()
                received += len(piece)
                if received > length:
                    raise DirectError("The provider sent more than the requested range.", 502)
                stream.bytes_sent += len(piece)
                yield piece
            if received != length:
                raise DirectError("The provider sent a short range.", 503)
        finally:
            await close()

    return _ClosingStream(
        body(),
        close=close,
        status_code=status,
        headers=reply,
        media_type=stream.source.media_type,
        background=BackgroundTask(close),
    )


async def probe_origin(
    stream: DirectStream, *, transport=None, timeout: float = 6.0
) -> dict[str, object]:
    """Confirm a freshly issued link still describes the pinned content.

    A mismatch is a hard refusal. A network or provider hiccup is not: the pass is
    allowed to open and the first range request revalidates the same facts. This
    keeps a transient probe failure from blocking a working title while still
    refusing a silently different file.
    """
    stream.validate()
    sni_host = parsed.host if (parsed := _origin_url(stream.origin)) is not None else None
    client, owns_client = acquire_client(
        key="direct-probe",
        transport=transport,
        sni_hostname=sni_host,
        timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)),
        trust_env=False,
        follow_redirects=False,
    )
    origin, previous_attempt = stream.origin, stream.completed_refresh()
    try:
        bounds = (0, min(_PROBE_BYTES, stream.source.size) - 1)
        upstream, left, right, length = await _send_range(
            client,
            stream,
            origin,
            bounds,
            method="GET",
            transport=transport,
        )
    except DirectError:
        if owns_client:
            await client.aclose()
        raise
    except (httpx.HTTPError, TimeoutError):
        if owns_client:
            await client.aclose()
        return {"probed": False, "reason": "transport"}
    try:
        if upstream.status_code in _EXPIRED:
            if not await stream.refresh_origin(origin, previous_attempt):
                raise DirectError("This copy is no longer available from Real-Debrid.", 404)
            return {"probed": False, "reason": "refreshed"}
        return {"probed": True, "bytes": right - left + 1}
    finally:
        await upstream.aclose()
        if owns_client:
            await client.aclose()


def stream_id(identity: str) -> str:
    """A per-pass identifier that is not derived from the signed URL."""
    return hashlib.sha256(f"direct\0{identity}\0{secrets.token_hex(8)}".encode()).hexdigest()[:32]
