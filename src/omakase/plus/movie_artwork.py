"""Credential-free artwork from one fixed public CDN, cached on the server."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

import httpx

from omakase.plus.posters import PosterUnavailable, _bounded_body, _content_type, poster_cache_root


async def movie_artwork(url: str, *, client=None, cache_root=None):
    if not isinstance(url, str) or not re.fullmatch(
        r"https://image\.tmdb\.org/t/p/(?:w[0-9]{2,4}|original)/[A-Za-z0-9_-]{1,160}\.(?:jpg|png)",
        url,
    ):
        raise PosterUnavailable("Unsupported movie artwork")
    root = (cache_root or poster_cache_root()) / "movies"
    path = root / (hashlib.sha256(url.encode()).hexdigest() + ".image")
    try:
        with path.open("rb") as stream:
            payload = stream.read(6 * 1024 * 1024 + 1)
        if len(payload) <= 6 * 1024 * 1024:
            return payload, _content_type(payload)
    except (OSError, PosterUnavailable):
        pass
    if client is None:
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as owned:
            return await movie_artwork(url, client=owned, cache_root=cache_root)
    try:
        async with client.stream("GET", url, follow_redirects=False) as response:
            payload = await _bounded_body(response, 6 * 1024 * 1024)
        kind = _content_type(payload)
    except (httpx.HTTPError, ValueError) as exc:
        raise PosterUnavailable("Movie artwork unavailable") from exc
    temporary = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
            temporary = stream.name
            stream.write(payload)
        os.replace(temporary, path)
    except OSError:
        pass
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return payload, kind
