"""One-page public release metadata; local provider verification stays authoritative."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from omakase.plus.btih import torrent_hash_from_magnet
from omakase.plus.nyaa import NyaaTorrent

_ENDPOINT = "https://animetosho.org/feed/json"
_MAX_BYTES = 512_000


def _number(value: object, maximum: int, *, unknown: bool = False) -> int:
    if value is None and unknown:
        return 0
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("AnimeTosho numeric metadata is invalid")
    return value


@dataclass(frozen=True, slots=True)
class AnimeToshoCatalog:
    kind: str = field(default="animetosho", init=False)
    transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = field(
        default=None, repr=False
    )

    async def search(self, query: str) -> tuple[NyaaTorrent, ...]:
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError("AnimeTosho query is invalid")
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=self.transport
        ) as client:
            async with client.stream("GET", _ENDPOINT, params={"q": query}) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BYTES:
                        raise ValueError("AnimeTosho metadata exceeds the bound")
        rows = json.loads(body)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError("AnimeTosho metadata shape is invalid")
        torrents = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("AnimeTosho metadata item is invalid")
            title = row.get("title")
            info_hash = row.get("info_hash")
            if (
                not isinstance(title, str)
                or not title.strip()
                or len(title) > 1000
                or any(ord(char) < 32 for char in title)
            ):
                raise ValueError("AnimeTosho title is invalid")
            if not isinstance(info_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", info_hash):
                raise ValueError("AnimeTosho hash is invalid")
            info_hash = info_hash.upper()
            magnet = row.get("magnet_uri")
            if magnet is not None and (
                not isinstance(magnet, str) or torrent_hash_from_magnet(magnet) != info_hash
            ):
                raise ValueError("AnimeTosho hash claims disagree")
            size = _number(row.get("total_size"), 100 * 1024**4, unknown=True)
            torrents.append(
                NyaaTorrent(
                    title=title,
                    magnet="magnet:?xt=urn:btih:" + info_hash,
                    seeders=_number(row.get("seeders"), 10_000_000, unknown=True),
                    leechers=_number(row.get("leechers"), 10_000_000, unknown=True),
                    size_bytes=size,
                    size_display=f"{size} B" if size else "Unknown",
                    pub_date=datetime.fromtimestamp(
                        _number(row.get("timestamp"), 4_102_444_800), timezone.utc
                    ),
                    is_trusted=False,
                    is_batch=bool(
                        re.search(r"\b(?:batch|complete|\d{1,3}\s*[-~]\s*\d{1,3})\b", title, re.I)
                    ),
                )
            )
        return tuple(torrents)
