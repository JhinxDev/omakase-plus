"""Bounded public Nyaa metadata evidence for planned coverage, never usable media."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from omakase.plus.coverage import parse_single_file_coverage
from omakase.plus.identity import identity_rejection_reason

MAX_BYTES = 1024 * 1024
MAX_NODES = 10000
MAX_FILES = 2000
MEDIA_SUFFIXES = frozenset({".mkv", ".mp4", ".avi", ".m4v", ".m2ts", ".ts"})
EXTRA_RE = re.compile(
    r"\b(?:n[\s._-]?c[\s._-]?(?:op|ed)|pv|trailer|preview|sample|creditless)\b", re.I
)


class MetadataRejected(ValueError):
    """Fixed non-sensitive metadata rejection without provider/source data."""


def download_url_allowed(url: str) -> bool:
    try:
        value = urlsplit(url)
    except ValueError:
        return False
    return (
        value.scheme == "https"
        and value.netloc == "nyaa.si"
        and not value.query
        and not value.fragment
        and re.fullmatch(r"/download/[1-9][0-9]*\.torrent", value.path) is not None
    )


def _decode(data: bytes):
    if not data or len(data) > MAX_BYTES:
        raise MetadataRejected("METADATA_SIZE")
    position = 0
    nodes = 0
    info_span = None

    def parse(depth=0):
        nonlocal position, nodes, info_span
        nodes += 1
        if depth > 24 or nodes > MAX_NODES or position >= len(data):
            raise MetadataRejected("METADATA_BOUNDS")
        marker = data[position : position + 1]
        if marker == b"i":
            end = data.find(b"e", position)
            raw = data[position + 1 : end]
            if end < 0 or re.fullmatch(rb"(?:0|-?[1-9][0-9]*)", raw) is None:
                raise MetadataRejected("METADATA_INTEGER")
            position = end + 1
            return int(raw)
        if marker in (b"l", b"d"):
            mapping = marker == b"d"
            position += 1
            result = {} if mapping else []
            while position < len(data) and data[position : position + 1] != b"e":
                if mapping:
                    key = parse(depth + 1)
                    if not isinstance(key, bytes) or key in result:
                        raise MetadataRejected("METADATA_DICTIONARY")
                    start = position
                    result[key] = parse(depth + 1)
                    if depth == 0 and key == b"info":
                        info_span = (start, position)
                else:
                    result.append(parse(depth + 1))
            if position >= len(data):
                raise MetadataRejected("METADATA_TRUNCATED")
            position += 1
            return result
        colon = data.find(b":", position)
        if colon < 0 or re.fullmatch(rb"(?:0|[1-9][0-9]*)", data[position:colon]) is None:
            raise MetadataRejected("METADATA_STRING")
        length = int(data[position:colon])
        position = colon + 1
        if length > len(data) - position:
            raise MetadataRejected("METADATA_TRUNCATED")
        value = data[position : position + length]
        position += length
        return value

    try:
        payload = parse()
        if position != len(data) or not isinstance(payload, dict) or info_span is None:
            raise MetadataRejected("METADATA_ROOT")
        return payload[b"info"], data[info_span[0] : info_span[1]]
    except (IndexError, KeyError, TypeError, ValueError, OverflowError) as exc:
        raise MetadataRejected("METADATA_INVALID") from exc


def planned_coverage(
    data: bytes,
    *,
    torrent_hash: str,
    target_title: str,
    target_aliases: tuple[str, ...],
    season: int,
    expected: int | None,
    anilist_id: int | None,
    media_format: str = "TV",
) -> frozenset[int]:
    info, raw_info = _decode(data)
    if hashlib.sha1(raw_info).hexdigest().upper() != torrent_hash.upper():
        raise MetadataRejected("METADATA_HASH")
    identity = dict(
        target_title=target_title,
        target_aliases=target_aliases,
        season=season,
        anilist_id=anilist_id,
        media_format=media_format,
    )
    try:
        name = info[b"name"].decode("utf-8")
        if not name or name in {".", ".."} or any(ch in name for ch in "/\\:"):
            raise MetadataRejected("METADATA_ROOT_PATH")
        if identity_rejection_reason(name, **identity):
            raise MetadataRejected("METADATA_IDENTITY")
        files = info.get(b"files")
        if files is None:
            files = [{b"path": [info[b"name"]], b"length": info[b"length"]}]
        if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
            raise MetadataRejected("METADATA_FILES")
        covered = set()
        for file in files:
            if not isinstance(file, dict) or b"l" in file.get(b"attr", b""):
                raise MetadataRejected("METADATA_FILE")
            parts = file[b"path"]
            if not isinstance(parts, list) or not parts:
                raise MetadataRejected("METADATA_PATH")
            decoded = [part.decode("utf-8") for part in parts]
            if any(
                not part or part in {".", ".."} or "/" in part or "\\" in part or ":" in part
                for part in decoded
            ):
                raise MetadataRejected("METADATA_PATH")
            filename = decoded[-1]
            if PurePosixPath(filename).suffix.casefold() not in MEDIA_SUFFIXES or EXTRA_RE.search(
                filename
            ):
                continue
            if type(file[b"length"]) is not int or file[b"length"] <= 0:
                raise MetadataRejected("METADATA_EMPTY_MEDIA")
            if identity_rejection_reason(
                " ".join([name, *decoded]), **identity
            ) or identity_rejection_reason(filename, **identity):
                raise MetadataRejected("METADATA_FILE_IDENTITY")
            parsed = parse_single_file_coverage(
                filename, season, expected=expected, anilist_id=anilist_id
            )
            if len(parsed.episodes) != 1 or parsed.specials or parsed.episodes & covered:
                raise MetadataRejected("METADATA_EPISODE")
            covered.update(parsed.episodes)
        if not covered:
            raise MetadataRejected("METADATA_NO_MEDIA")
        return frozenset(covered)
    except (KeyError, TypeError, UnicodeError, AttributeError) as exc:
        raise MetadataRejected("METADATA_FILES_INVALID") from exc


async def fetch_planned_coverage(url: str, **kwargs) -> frozenset[int]:
    if not download_url_allowed(url):
        return frozenset()
    try:
        async with (
            asyncio.timeout(15),
            httpx.AsyncClient(timeout=10, follow_redirects=False) as client,
        ):
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return frozenset()
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(body) + len(chunk) > MAX_BYTES:
                        return frozenset()
                    body.extend(chunk)
                return planned_coverage(bytes(body), **kwargs)
    except (httpx.HTTPError, MetadataRejected, ValueError, TimeoutError):
        return frozenset()
