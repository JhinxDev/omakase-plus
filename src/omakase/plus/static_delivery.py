"""Compress fixed public CSS/JS only; never compress private reflected pages."""

from __future__ import annotations

import gzip
import hashlib
from functools import lru_cache
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response


def _allows_gzip(value: str) -> bool:
    qualities = {}
    for entry in value.lower().split(","):
        name, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            key, _, raw = parameter.strip().partition("=")
            if key == "q":
                try:
                    quality = float(raw)
                except ValueError:
                    quality = 0.0
        if not 0 <= quality <= 1:
            quality = 0.0
        qualities[name] = min(quality, qualities.get(name, 1.0))
    return qualities.get("gzip", qualities.get("*", 0.0)) > 0


@lru_cache(maxsize=32)
def _compressed(path: str, modified: int, size: int) -> bytes:
    del modified, size  # Cache identity includes file revision, not just pathname.
    return gzip.compress(Path(path).read_bytes(), compresslevel=6, mtime=0)


def static_response(path: Path, media_type: str, request: Request) -> Response:
    headers = {
        "Cache-Control": "public, max-age=3600",
        "Vary": "Accept-Encoding",
        "X-Content-Type-Options": "nosniff",
    }
    if (
        media_type in {"text/css", "application/javascript"}
        and "range" not in request.headers
        and _allows_gzip(request.headers.get("accept-encoding", ""))
    ):
        stat = path.stat()
        body = _compressed(str(path), stat.st_mtime_ns, stat.st_size)
        if len(body) < stat.st_size:
            return Response(
                body,
                media_type=media_type,
                headers={
                    **headers,
                    "Content-Encoding": "gzip",
                    "ETag": '"' + hashlib.sha256(body).hexdigest() + '"',
                },
            )
    return FileResponse(path, media_type=media_type, headers=headers)
