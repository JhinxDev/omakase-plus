"""Same-origin delivery of one resolved Plex Part; no arbitrary proxy surface."""

from __future__ import annotations

import hashlib
import inspect
import os
import re
import stat
import time
from collections.abc import Callable

import httpx
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse

from omakase.plus.outbound import pin_destination

HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Accept-Ranges": "bytes",
}


class DeliveryError(ValueError):
    def __init__(self, message="The selected media could not be delivered.", status=502):
        super().__init__(message)
        self.status = status


class _ClosingStream(StreamingResponse):
    """Close resources even when ASGI send fails before iteration starts."""

    def __init__(self, *args, close, **kwargs):
        super().__init__(*args, **kwargs)
        self.close = close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            result = self.close()
            if inspect.isawaitable(result):
                await result


def parse_range(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
    if not match or not any(match.groups()) or len(value) > 100 or size <= 0:
        raise DeliveryError("Use one satisfiable byte range.", 416)
    start, end = match.groups()
    if start:
        left, right = int(start), min(int(end), size - 1) if end else size - 1
    else:
        suffix = int(end)
        if suffix <= 0:
            raise DeliveryError("Use one satisfiable byte range.", 416)
        left, right = max(0, size - suffix), size - 1
    if left >= size or left > right:
        raise DeliveryError("Use one satisfiable byte range.", 416)
    return left, right


async def _authorize(callback: Callable):
    result = callback()
    if inspect.isawaitable(result):
        result = await result
    if result is False:
        raise DeliveryError("Playback access changed.", 403)


async def open_local(path, request, *, authorize, etag_seed, on_close=lambda: None):
    """Open an already allowlisted derivative without following its final symlink.

    The caller resolves the immutable READY inventory, retains the generation and
    bounds concurrent reads. Authorization is refreshed during long transfers.
    """
    await _authorize(authorize)
    descriptor = None
    closed = False

    def close():
        nonlocal closed
        if not closed:
            closed = True
            if descriptor is not None:
                os.close(descriptor)
            on_close()

    try:
        if request.method not in {"GET", "HEAD"} or path.is_symlink():
            raise DeliveryError("Media resource unavailable.", 404)
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0:
            raise DeliveryError("Media resource unavailable.", 404)
        etag = (
            '"'
            + hashlib.sha256(
                f"{etag_seed}:{path.name}:{info.st_size}:{info.st_mtime_ns}".encode()
            ).hexdigest()
            + '"'
        )
        headers = HEADERS | {"ETag": etag}
        media_type = {
            ".mpd": "application/dash+xml",
            ".m3u8": "application/vnd.apple.mpegurl",
            ".m4s": "video/iso.segment",
            ".mp4": "video/mp4",
            ".ass": "text/x-ssa",
            ".vtt": "text/vtt",
            ".bin": "application/octet-stream",
        }.get(path.suffix)
        if not media_type:
            raise DeliveryError("Media resource unavailable.", 404)
        if request.headers.get("if-none-match") in {etag, "*"}:
            close()
            return Response(status_code=304, headers=headers)
        value = request.headers.get("range")
        if value and request.headers.get("if-range", etag) != etag:
            value = None
        try:
            bounds = parse_range(value, info.st_size) if value else None
        except DeliveryError:
            close()
            return Response(
                status_code=416, headers=headers | {"Content-Range": f"bytes */{info.st_size}"}
            )
        left, right = bounds or (0, info.st_size - 1)
        length = right - left + 1
        headers["Content-Length"] = str(length)
        if bounds:
            headers["Content-Range"] = f"bytes {left}-{right}/{info.st_size}"
        if request.method == "HEAD":
            close()
            return Response(
                status_code=206 if bounds else 200, headers=headers, media_type=media_type
            )
        os.lseek(descriptor, left, os.SEEK_SET)
    except BaseException:
        close()
        raise

    async def body():
        remaining, checked_at = length, 0.0
        try:
            while remaining:
                if time.monotonic() - checked_at >= 1:
                    await _authorize(authorize)
                    checked_at = time.monotonic()
                chunk = await run_in_threadpool(os.read, descriptor, min(65536, remaining))
                if not chunk:
                    raise DeliveryError()
                remaining -= len(chunk)
                yield chunk
        finally:
            close()

    return _ClosingStream(
        body(),
        close=close,
        status_code=206 if bounds else 200,
        headers=headers,
        media_type=media_type,
        background=BackgroundTask(close),
    )


async def open_part(
    source, request, *, authorize, transport=None, on_close=lambda: None, internal_matroska=False
):
    """Stream bounded chunks, validating exact ranges and promptly closing on exit.

    ``source`` is server-resolved, never deserialized from a browser request.
    Authorization includes current login, entitlement and selected-source version.
    An active body revalidates once per second as well as before opening it.
    """
    await _authorize(authorize)
    if request.method not in {"GET", "HEAD"}:
        raise DeliveryError("Unsupported media method.", 405)
    if not re.fullmatch(r"/library/parts/[0-9]+/[0-9]+/[^/?#\\]+", source.part_key):
        raise DeliveryError()
    if type(source.part_size) is not int or source.part_size <= 0:
        raise DeliveryError()
    if not re.fullmatch(r"[a-f0-9]{64}", source.media_version):
        raise DeliveryError()
    etag = '"' + source.media_version + '"'
    headers = HEADERS | {"ETag": etag}
    if request.headers.get("if-none-match") in {etag, "*"}:
        on_close()
        return Response(status_code=304, headers=headers)
    requested = request.headers.get("range")
    if requested and request.headers.get("if-range", etag) != etag:
        requested = None
    try:
        bounds = parse_range(requested, source.part_size) if requested else None
    except DeliveryError:
        on_close()
        return Response(
            status_code=416, headers=headers | {"Content-Range": f"bytes */{source.part_size}"}
        )
    left, right = bounds or (0, source.part_size - 1)
    length = right - left + 1
    status = 206 if bounds else 200
    headers["Content-Length"] = str(length)
    if bounds:
        headers["Content-Range"] = f"bytes {left}-{right}/{source.part_size}"
    media_type = {"mp4": "video/mp4", "webm": "video/webm"}.get(source.container)
    if internal_matroska and source.container == "mkv":
        media_type = "video/x-matroska"
    if not media_type:
        raise DeliveryError("This container needs the external player.", 415)
    if request.method == "HEAD":
        on_close()
        return Response(status_code=status, media_type=media_type, headers=headers)
    destination = await pin_destination(source.base_url, transport=transport)
    upstream_headers = {
        "X-Plex-Token": source.token,
        "X-Plex-Product": "Omakase Plus",
        "X-Plex-Client-Identifier": "omakase-plus-web-player",
        "Accept-Encoding": "identity",
        "Host": destination.host_header,
    }
    if bounds:
        upstream_headers["Range"] = f"bytes={left}-{right}"
    client = httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(15, connect=5),
        trust_env=False,
        follow_redirects=False,
    )
    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                destination.base_url + source.part_key,
                headers=upstream_headers,
                extensions={"sni_hostname": destination.sni_hostname},
            ),
            stream=True,
        )
        if upstream.status_code == 451:
            raise DeliveryError("This source is unavailable in its current region.", 451)
        expected_range = f"bytes {left}-{right}/{source.part_size}"
        if (
            upstream.status_code != status
            or upstream.headers.get("content-encoding", "identity") != "identity"
            or (bounds and upstream.headers.get("content-range") != expected_range)
            or (not bounds and "content-range" in upstream.headers)
            or (
                "content-length" in upstream.headers
                and upstream.headers["content-length"] != str(length)
            )
        ):
            raise DeliveryError()
    except BaseException as error:
        if "upstream" in locals():
            await upstream.aclose()
        await client.aclose()
        if isinstance(error, httpx.HTTPError):
            raise DeliveryError() from None
        raise

    closed = False

    async def close():
        nonlocal closed
        if not closed:
            closed = True
            try:
                await upstream.aclose()
                await client.aclose()
            finally:
                on_close()

    async def body():
        received, checked_at = 0, 0.0
        try:
            async for chunk in upstream.aiter_raw(chunk_size=65536):
                if time.monotonic() - checked_at >= 1:
                    await _authorize(authorize)
                    checked_at = time.monotonic()
                received += len(chunk)
                if received > length:
                    raise DeliveryError()
                yield chunk
            if received != length:
                raise DeliveryError()
        finally:
            await close()

    return _ClosingStream(
        body(),
        close=close,
        status_code=status,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(close),
    )
