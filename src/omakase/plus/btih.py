"""Canonical BitTorrent v1 info-hash parsing."""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import parse_qs, urlsplit

_HEX_BTIH = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_BASE32_BTIH = re.compile(r"^[a-z2-7]{32}$", re.IGNORECASE)


def normalize_btih(value: object) -> str | None:
    """Return one v1 BTIH as uppercase hexadecimal."""
    normalized = str(value or "").strip()
    if _HEX_BTIH.fullmatch(normalized):
        return normalized.upper()
    if not _BASE32_BTIH.fullmatch(normalized):
        return None
    try:
        decoded = base64.b32decode(normalized.upper(), casefold=True)
    except (binascii.Error, ValueError):
        return None
    return decoded.hex().upper() if len(decoded) == 20 else None


def torrent_hash_from_magnet(value: object) -> str | None:
    """Extract and canonicalize the first v1 BTIH from a magnet URI."""
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme.casefold() != "magnet":
        return None
    for exact_topic in parse_qs(parsed.query).get("xt", ()):
        prefix, separator, raw_hash = exact_topic.rpartition(":")
        if separator and prefix.casefold() == "urn:btih":
            normalized = normalize_btih(raw_hash)
            if normalized is not None:
                return normalized
    return None
