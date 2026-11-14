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
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from omakase.plus.outbound import (
    blocked_address,
    loopback_destinations_allowed,
    private_destinations_allowed,
)
from omakase.plus.web_playback_delivery import DeliveryError, parse_range

HEADERS = {
    "Cache-Control": "private, no-store",
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

# Provider links rotate. One refresh per request keeps an expired link from
# failing playback while refusing to loop indefinitely.
_REFRESH_LIMIT = 1

# Ranges the player may ask for are one byte span. Multipart ranges are refused so
# the reply always has exactly one Content-Range to validate.
_RANGE = re.compile(r"bytes=[0-9]{1,18}-[0-9]{0,18}")
_REDIRECT_LIMIT = 3
_HOST_TTL = 60.0
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


def _safe_host(host: str) -> None:
    """Reject destinations that cannot be a public media origin.

    The link itself comes from the provider API and is constrained to the
    provider's own domains when it is resolved. Redirect hops are validated here
    so a compromised or mistaken reply cannot aim the server at the local network.
    """
    host = (host or "").casefold().rstrip(".")
    if not host or len(host) > 253:
        raise DirectError("This source is unavailable.", 502)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        allow_private = private_destinations_allowed()
        if blocked_address(
            literal,
            allow_private=allow_private,
            allow_loopback=allow_private and loopback_destinations_allowed(),
        ):
            raise DirectError("This source is unavailable.", 502)
        return
    if host == "localhost" or host.endswith(".localhost"):
        raise DirectError("This source is unavailable.", 502)
    if re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", host):
        raise DirectError("This source is unavailable.", 502)


@dataclass
class DirectStream:
    """One authenticated playback pass over a remote original file.

    ``origin`` is mutable transport state: the provider rotates it. Everything
    else is pinned. ``refresh`` re-resolves the same content identity and must
    return a new URL for it, or raise.
    """

    source: DirectSource
    origin: str = field(repr=False)
    refresh: Callable[[], object] | None = field(default=None, repr=False)
    bytes_sent: int = 0
    ranges: int = 0
    refreshes: int = 0
    requests: int = 0
    opened_at: float = field(default_factory=time.monotonic)
    _hosts: dict[str, float] = field(default_factory=dict, repr=False)

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
        if not re.fullmatch(r"https://[^\s]{1,4000}", self.origin or ""):
            raise DirectError("This source is unavailable.", 502)
        if type(self.source.size) is not int or self.source.size <= 0:
            raise DirectError("This source is unavailable.", 502)

    def _check_hop(self, url: str) -> str:
        try:
            parts = urlsplit(url)
        except ValueError:
            raise DirectError("This source is unavailable.", 502) from None
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or (parts.port is not None and not 0 < parts.port <= 65535)
        ):
            raise DirectError("This source is unavailable.", 502)
        host = parts.hostname.casefold()
        now = time.monotonic()
        if self._hosts.get(host, 0.0) > now:
            return url
        _safe_host(host)
        self._hosts[host] = now + _HOST_TTL
        return url

    async def _resolved_addresses(self, host: str, port: int) -> tuple[object, ...]:
        rows = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, port, type=socket.SOCK_STREAM), timeout=2.0
        )
        return tuple({row[4][0].split("%", 1)[0] for row in rows})

    async def _check_resolved(self, url: str, *, offline: bool = False) -> None:
        """Refuse a redirect target that resolves into a private range.

        An injected ``httpx.MockTransport`` is the offline test seam, the same
        convention ``outbound.pin_destination`` uses: it cannot open a socket, so
        there is no answer to check.
        """
        if offline:
            return
        parts = urlsplit(url)
        host = (parts.hostname or "").casefold()
        try:
            ipaddress.ip_address(host)
            return
        except ValueError:
            pass
        if self._hosts.get("resolved:" + host, 0.0) > time.monotonic():
            return
        try:
            answers = await self._resolved_addresses(host, parts.port or 443)
        except (OSError, TimeoutError, ValueError):
            raise DirectError("This source is unavailable.", 502) from None
        allow_private = private_destinations_allowed()
        allow_loopback = allow_private and loopback_destinations_allowed()
        for answer in answers:
            try:
                address = ipaddress.ip_address(str(answer))
            except ValueError:
                raise DirectError("This source is unavailable.", 502) from None
            if blocked_address(address, allow_private=allow_private, allow_loopback=allow_loopback):
                raise DirectError("This source is unavailable.", 502)
        self._hosts["resolved:" + host] = time.monotonic() + _HOST_TTL

    async def refresh_origin(self) -> bool:
        """Ask the resolver for a fresh link to the same content identity."""
        if self.refresh is None or self.refreshes >= _REFRESH_LIMIT:
            return False
        self.refreshes += 1
        try:
            result = self.refresh()
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                result = await result
        except DirectError:
            raise
        except Exception:
            return False
        url, size = (result or ("", 0)) if isinstance(result, tuple) else (result, self.source.size)
        if not isinstance(url, str) or not re.fullmatch(r"https://[^\s]{1,4000}", url):
            return False
        # A rotated link that reports another byte length is different content.
        # Keeping the old origin is the only honest outcome.
        if type(size) is int and size not in (0, self.source.size):
            raise DirectError("This copy changed on the provider. Reopen the title.", 409)
        self.origin = url
        self._hosts.clear()
        return True


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
    bounds,
    *,
    method: str,
    offline: bool = False,
) -> tuple[httpx.Response, int, int, int]:
    """Open the remote body and prove it is the exact requested byte span.

    Returns the live response, the ``(left, right)`` span actually offered and the
    expected body length.
    """
    left, right = bounds if bounds is not None else (0, stream.source.size - 1)
    length = right - left + 1
    url = stream._check_hop(stream.origin)
    upstream = await client.send(
        client.build_request(method, url, headers=_upstream_headers(bounds)),
        stream=True,
    )
    if upstream.status_code in {401, 403, 404, 410, 451}:
        await upstream.aclose()
        return upstream, left, right, length
    try:
        stream._check_hop(str(upstream.url))
        await stream._check_resolved(str(upstream.url), offline=offline)
    except DirectError:
        await upstream.aclose()
        raise
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

    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(20, connect=6, read=20),
        trust_env=False,
        follow_redirects=False,
    )
    offline = isinstance(transport, httpx.MockTransport)
    upstream = None
    try:
        try:
            upstream, left, right, length = await _send_range(
                client, stream, bounds, method=request.method, offline=offline
            )
        except httpx.HTTPError:
            raise DirectError("Real-Debrid did not answer this request.", 502) from None
        except TimeoutError:
            raise DirectError("Real-Debrid did not answer this request.", 504) from None
        if upstream.status_code in {401, 403, 404, 410, 451}:
            # An expired signed link looks like this. Refresh once and ask for the
            # same span again; a different identity is refused, not served.
            if not await stream.refresh_origin():
                raise DirectError("This copy is no longer available from Real-Debrid.", 404)
            try:
                upstream, left, right, length = await _send_range(
                    client, stream, bounds, method=request.method, offline=offline
                )
            except httpx.HTTPError:
                raise DirectError("Real-Debrid did not answer this request.", 502) from None
            if upstream.status_code in {401, 403, 404, 410, 451}:
                await upstream.aclose()
                raise DirectError("This copy is no longer available from Real-Debrid.", 404)
        reply["Content-Length"] = str(length)
        if bounds is not None:
            reply["Content-Range"] = f"bytes {left}-{right}/{stream.source.size}"
            stream.ranges += 1
    except BaseException:
        if upstream is not None:
            await upstream.aclose()
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
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(timeout, connect=min(4.0, timeout)),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        bounds = (0, min(_PROBE_BYTES, stream.source.size) - 1)
        upstream, left, right, length = await _send_range(
            client,
            stream,
            bounds,
            method="GET",
            offline=isinstance(transport, httpx.MockTransport),
        )
    except DirectError:
        await client.aclose()
        raise
    except (httpx.HTTPError, TimeoutError):
        await client.aclose()
        return {"probed": False, "reason": "transport"}
    try:
        if upstream.status_code in {401, 403, 404, 410, 451}:
            if not await stream.refresh_origin():
                raise DirectError("This copy is no longer available from Real-Debrid.", 404)
            return {"probed": False, "reason": "refreshed"}
        return {"probed": True, "bytes": right - left + 1}
    finally:
        await upstream.aclose()
        await client.aclose()


def stream_id(identity: str) -> str:
    """A per-pass identifier that is not derived from the signed URL."""
    return hashlib.sha256(f"direct\0{identity}\0{secrets.token_hex(8)}".encode()).hexdigest()[:32]
