"""Strict YouTube trailer parsing and privacy-enhanced embed helpers."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_EMBED_BASE = "https://www.youtube-nocookie.com/embed/"
_WATCH_BASE = "https://www.youtube.com/watch?v="


def youtube_trailer(video_id: str | None) -> dict[str, str] | None:
    """Return validated YouTube embed and external watch URLs, or ``None``."""
    if not isinstance(video_id, str):
        return None
    candidate = video_id.strip()
    if not YOUTUBE_ID_RE.fullmatch(candidate):
        return None
    return {
        "site": "youtube",
        "id": candidate,
        "embed_url": f"{_EMBED_BASE}{candidate}?autoplay=1&rel=0",
        "watch_url": f"{_WATCH_BASE}{candidate}",
    }


def _id_from_url(raw_url: Any) -> str | None:
    if not isinstance(raw_url, str) or not raw_url.strip():
        return None
    try:
        parsed = urlparse(raw_url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"}:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        parts = [segment for segment in parsed.path.split("/") if segment]
        if len(parts) >= 2 and parts[0] in {"embed", "shorts", "v"}:
            return parts[1]
    elif host == "youtu.be":
        parts = [segment for segment in parsed.path.split("/") if segment]
        if parts:
            return parts[0]
    return None


def extract_anilist_trailer(raw: Any) -> dict[str, str] | None:
    """Parse AniList ``trailer { id site }`` safely."""
    if not isinstance(raw, dict):
        return None
    site = raw.get("site")
    if not isinstance(site, str) or site.strip().lower() != "youtube":
        return None
    return youtube_trailer(raw.get("id"))


def extract_seerr_trailer(related_videos: Any) -> dict[str, str] | None:
    """Pick the best official YouTube trailer/teaser from Seerr ``relatedVideos``."""
    if not isinstance(related_videos, list):
        return None
    candidates: list[tuple[int, dict[str, str]]] = []
    for item in related_videos:
        if not isinstance(item, dict):
            continue
        site = item.get("site")
        if isinstance(site, str) and site.strip() and site.strip().lower() != "youtube":
            continue
        vid = item.get("key") or _id_from_url(item.get("url"))
        trailer = youtube_trailer(vid)
        if trailer is None:
            continue
        kind = str(item.get("type") or "").strip().lower()
        priority = 0 if kind == "trailer" else 1 if kind == "teaser" else 2
        candidates.append((priority, trailer))
    if not candidates:
        return None
    candidates.sort(key=lambda entry: entry[0])
    return candidates[0][1]
