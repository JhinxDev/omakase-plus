"""Safe, same-origin AniList poster retrieval with a durable local cache."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from omakase.plus.direct import API_URL, USER_AGENT

_MAX_POSTER_BYTES = 6 * 1024 * 1024
_ALLOWED_HOSTS = frozenset({"s4.anilist.co"})
_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_POSTER_QUERY = """
query ($id: Int!) {
  Media(id: $id, type: ANIME) {
    coverImage { extraLarge large }
    bannerImage
  }
}
"""


class PosterUnavailable(RuntimeError):
    """The requested artwork could not be safely retrieved."""


def poster_cache_root() -> Path:
    configured = os.getenv("OMAKASE_POSTER_CACHE", "").strip()
    if configured:
        return Path(configured)
    data_root = os.getenv("OMAKASE_DATA_DIR", "").strip()
    return Path(data_root) / "posters" if data_root else Path("data") / "posters"


def _content_type(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    raise PosterUnavailable("Artwork was not a supported image")


async def poster_bytes(
    anilist_id: int,
    *,
    cache_root: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str]:
    """Return verified poster bytes, preferring the local on-disk cache."""
    return await _artwork_bytes(anilist_id, kind="poster", cache_root=cache_root, client=client)


async def banner_bytes(
    anilist_id: int,
    *,
    cache_root: Path | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[bytes, str]:
    """Return the title's actual wide artwork without substituting a stretched poster."""
    return await _artwork_bytes(anilist_id, kind="banner", cache_root=cache_root, client=client)


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    response.raise_for_status()
    size = response.headers.get("content-length", "")
    if size.isdigit() and int(size) > limit:
        raise PosterUnavailable("Artwork response was too large")
    payload = bytearray()
    async for chunk in response.aiter_bytes():
        if len(payload) + len(chunk) > limit:
            raise PosterUnavailable("Artwork response was too large")
        payload.extend(chunk)
    return bytes(payload)


async def _artwork_bytes(anilist_id, *, kind, cache_root, client) -> tuple[bytes, str]:
    if type(anilist_id) is not int or not 0 < anilist_id <= 2_147_483_647:
        raise PosterUnavailable("Anime identity is invalid")
    root = cache_root or poster_cache_root()
    cache_path = root / f"{anilist_id}{'.banner' if kind == 'banner' else ''}.image"
    try:
        with cache_path.open("rb") as stream:
            cached = stream.read(_MAX_POSTER_BYTES + 1)
        if len(cached) > _MAX_POSTER_BYTES:
            raise PosterUnavailable("Cached artwork was too large")
        return cached, _content_type(cached)
    except (OSError, PosterUnavailable):
        pass

    owns_client = client is None
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        async with http.stream(
            "POST",
            API_URL,
            json={"query": _POSTER_QUERY, "variables": {"id": anilist_id}},
        ) as metadata:
            metadata_payload = await _bounded_body(metadata, 512 * 1024)
        media = (json.loads(metadata_payload).get("data") or {}).get("Media") or {}
        cover = media.get("coverImage") or {}
        poster_url = str(
            media.get("bannerImage") or ""
            if kind == "banner"
            else cover.get("extraLarge") or cover.get("large") or ""
        ).strip()
        parsed = urlsplit(poster_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_HOSTS
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or len(poster_url) > 2048
        ):
            raise PosterUnavailable("AniList did not return trusted artwork")
        async with http.stream("GET", poster_url) as image:
            declared_type = image.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if declared_type not in _CONTENT_TYPES:
                raise PosterUnavailable("Artwork was not a supported image")
            payload = await _bounded_body(image, _MAX_POSTER_BYTES)
        verified_type = _content_type(payload)
        if verified_type != declared_type:
            raise PosterUnavailable("Artwork type did not match its content")
        temporary = None
        try:
            root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=root, prefix=f".{anilist_id}-", suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
            temporary.replace(cache_path)
        except OSError:
            pass  # A cache write failure must not hide safely retrieved artwork.
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return payload, verified_type
    except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise PosterUnavailable("Artwork is temporarily unavailable") from exc
    finally:
        if owns_client:
            await http.aclose()
