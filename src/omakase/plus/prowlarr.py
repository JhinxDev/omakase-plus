"""Bounded, secret-safe Prowlarr catalog adapter."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from omakase.plus.btih import normalize_btih, torrent_hash_from_magnet
from omakase.plus.nyaa import NyaaTorrent

_BATCH_RE = re.compile(
    r"\b(?:complete|batch|all[\s._-]?episodes?)\b|\b\d{2,3}-\d{2,3}\b",
    re.IGNORECASE,
)
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_RESULTS = 500
_MAX_RELEASE_BYTES = 150 * 1024**3
_MAX_MAGNET_RESOLUTIONS = 200
_MAGNET_RESOLUTION_CONCURRENCY = 8
_MAGNET_RESOLUTION_BUDGET_SECONDS = 20.0
_DEFAULT_CATEGORIES = (5070, 5000)
_APOSTROPHE_RE = re.compile(r"['’‘`ʼ]")
_SEARCH_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)


class ProwlarrConfigurationError(ValueError):
    """Raised before a request when a catalog configuration is unsafe."""


def private_catalog_urls_allowed() -> bool:
    return os.getenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def parse_positive_ids(value: str | None, *, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    """Parse a bounded comma-separated list of positive Prowlarr IDs."""
    text = str(value or "").strip()
    if not text:
        return default
    values: list[int] = []
    for raw in text.split(","):
        normalized = raw.strip()
        if not normalized.isdigit():
            raise ProwlarrConfigurationError("IDs must be positive numbers separated by commas")
        number = int(normalized)
        if number <= 0 or number > 2_147_483_647:
            raise ProwlarrConfigurationError("IDs are outside the supported range")
        if number not in values:
            values.append(number)
        if len(values) > 100:
            raise ProwlarrConfigurationError("Too many IDs were configured")
    return tuple(values)


def normalize_base_url(value: str) -> str:
    """Return a query-free Prowlarr base URL suitable for endpoint construction."""
    candidate = value.strip()
    if len(candidate) > 500:
        raise ProwlarrConfigurationError("Prowlarr URL is too long")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProwlarrConfigurationError("Prowlarr URL must start with http:// or https://")
    if parsed.username or parsed.password:
        raise ProwlarrConfigurationError("Prowlarr URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ProwlarrConfigurationError("Prowlarr URL must not contain a query or fragment")
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1/search", "/api/v1"):
        if path.casefold().endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")).rstrip("/")


IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


async def _resolved_ips(hostname: str, port: int) -> tuple[IPAddress, ...]:
    try:
        rows = await asyncio.to_thread(
            socket.getaddrinfo,
            hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise ProwlarrConfigurationError("Prowlarr hostname could not be resolved") from exc
    addresses = {
        ipaddress.ip_address(row[4][0].split("%", maxsplit=1)[0])
        for row in rows
        if row[4] and row[4][0]
    }
    if not addresses:
        raise ProwlarrConfigurationError("Prowlarr hostname returned no network address")
    return tuple(sorted(addresses, key=str))


def _address_is_blocked(address: IPAddress, *, allow_private: bool) -> bool:
    if address.is_link_local or address.is_multicast or address.is_unspecified:
        return True
    if allow_private:
        return False
    return address.is_private or address.is_loopback or address.is_reserved


def _endpoint(base_url: str) -> str:
    return f"{normalize_base_url(base_url)}/api/v1/search"


def normalize_search_query(value: str) -> str:
    """Make one title safe for indexers that interpret punctuation as operators."""
    raw = value.strip()
    if not raw or len(raw) > 300:
        raise ProwlarrConfigurationError("Catalog search query is invalid")
    normalized = unicodedata.normalize("NFKC", raw)
    normalized = _APOSTROPHE_RE.sub("", normalized)
    normalized = " ".join(_SEARCH_SEPARATOR_RE.sub(" ", normalized).split())
    if not normalized or len(normalized) > 300:
        raise ProwlarrConfigurationError("Catalog search query is invalid")
    return normalized


def _safe_int(value: object, *, minimum: int = 0, maximum: int) -> int:
    if isinstance(value, bool):
        return minimum
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError):
        return minimum
    return min(maximum, max(minimum, normalized))


def _published_at(value: object) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _size_display(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


def _release_hash(row: dict[str, object]) -> str:
    for key in ("infoHash", "releaseHash"):
        normalized = normalize_btih(row.get(key))
        if normalized is not None:
            return normalized
    return torrent_hash_from_magnet(row.get("magnetUrl")) or ""


def _proxy_download_url(base_url: str, value: object) -> str:
    """Rebuild a Prowlarr download URL without its generated query-string key."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 12_000:
        return ""
    parsed = urlsplit(raw)
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    expected = rf"{re.escape(base_path)}/[1-9][0-9]*/download"
    if parsed.scheme not in {"http", "https"} or not re.fullmatch(expected, parsed.path):
        return ""
    query: list[tuple[str, str]] = []
    for key, item in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.casefold()
        if normalized_key in {"apikey", "api_key"}:
            continue
        if normalized_key not in {"file", "link"} or len(item) > 4_000:
            return ""
        query.append((key, item))
    if not query:
        return ""
    rebuilt_query = urlencode(query)
    if len(rebuilt_query) > 8_000:
        return ""
    return urlunsplit((base.scheme, base.netloc, parsed.path, rebuilt_query, ""))


async def _resolve_proxy_magnets(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    rows: list[object],
) -> list[object]:
    """Resolve bounded same-Prowlarr redirects into magnet URIs."""
    if not rows:
        return []
    semaphore = asyncio.Semaphore(_MAGNET_RESOLUTION_CONCURRENCY)

    async def resolve(index: int, raw: object) -> tuple[int, object]:
        if not isinstance(raw, dict) or _release_hash(raw):
            return index, raw
        title = str(raw.get("title") or "")
        if not re.search(r"\b(?:2160p|1080p|720p|4k|uhd)\b", title, re.IGNORECASE):
            return index, raw
        for field_name in ("magnetUrl", "downloadUrl"):
            endpoint = _proxy_download_url(base_url, raw.get(field_name))
            if not endpoint:
                continue
            try:
                async with (
                    semaphore,
                    client.stream(
                        "GET",
                        endpoint,
                        headers=headers,
                    ) as response,
                ):
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        continue
                    magnet = response.headers.get("location", "").strip()
            except httpx.HTTPError:
                continue
            if torrent_hash_from_magnet(magnet) is not None:
                resolved = dict(raw)
                resolved["magnetUrl"] = magnet
                return index, resolved
        return index, raw

    eligible = list(enumerate(rows[:_MAX_MAGNET_RESOLUTIONS]))
    tasks = [asyncio.create_task(resolve(index, raw)) for index, raw in eligible]
    done, pending = await asyncio.wait(tasks, timeout=_MAGNET_RESOLUTION_BUDGET_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    result = list(rows)
    for task in done:
        if not task.cancelled() and task.exception() is None:
            index, raw = task.result()
            result[index] = raw
    return result


def parse_releases(payload: object) -> tuple[NyaaTorrent, ...]:
    """Convert Prowlarr's JSON response into the catalog's neutral torrent shape."""
    if not isinstance(payload, list):
        raise ValueError("Prowlarr search response must be a JSON array")
    releases: list[NyaaTorrent] = []
    for raw in payload[:_MAX_RESULTS]:
        if not isinstance(raw, dict):
            continue
        protocol = str(raw.get("protocol") or "torrent").strip().casefold()
        if protocol not in {"torrent", "1"}:
            continue
        title = str(raw.get("title") or "").strip()
        if not title or len(title) > 1000:
            continue
        torrent_hash = _release_hash(raw)
        if not torrent_hash:
            continue
        size_bytes = _safe_int(raw.get("size"), maximum=_MAX_RELEASE_BYTES + 1)
        if size_bytes > _MAX_RELEASE_BYTES:
            continue
        seeders = _safe_int(raw.get("seeders"), maximum=2_147_483_647)
        leechers = _safe_int(raw.get("leechers"), maximum=2_147_483_647)
        releases.append(
            NyaaTorrent(
                title=title,
                magnet=f"magnet:?xt=urn:btih:{torrent_hash}",
                seeders=seeders,
                leechers=leechers,
                size_bytes=size_bytes,
                size_display=_size_display(size_bytes),
                pub_date=_published_at(raw.get("publishDate")),
                is_trusted=False,
                is_batch=bool(_BATCH_RE.search(title)),
            )
        )
    return tuple(releases)


@dataclass(frozen=True, slots=True)
class ProwlarrCatalog:
    """Search every enabled Prowlarr indexer through one bounded API."""

    base_url: str
    api_key: str = field(repr=False)
    indexer_ids: tuple[int, ...] = ()
    categories: tuple[int, ...] = _DEFAULT_CATEGORIES
    allow_private: bool = False
    timeout_seconds: float = 15.0
    resolved_addresses: tuple[str, ...] | None = None
    transport: httpx.AsyncBaseTransport | None = None
    kind: str = "prowlarr"

    async def search(self, query: str) -> tuple[NyaaTorrent, ...]:
        normalized_query = normalize_search_query(query)
        api_key = self.api_key.strip()
        if not api_key or len(api_key) > 500:
            raise ProwlarrConfigurationError("Prowlarr API key is invalid")

        base_url = normalize_base_url(self.base_url)
        parsed = urlsplit(base_url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if self.resolved_addresses is None:
            addresses = await _resolved_ips(str(parsed.hostname), port)
        else:
            try:
                addresses = tuple(ipaddress.ip_address(value) for value in self.resolved_addresses)
            except ValueError as exc:
                raise ProwlarrConfigurationError("Prowlarr address validation failed") from exc
        if not addresses or any(
            _address_is_blocked(address, allow_private=self.allow_private) for address in addresses
        ):
            raise ProwlarrConfigurationError(
                "Prowlarr address is blocked; explicitly allow private catalog URLs "
                "for a trusted local server"
            )

        params: list[tuple[str, str]] = [
            ("query", normalized_query),
            ("type", "search"),
            ("limit", str(_MAX_RESULTS)),
        ]
        params.extend(("categories", str(category)) for category in self.categories)
        params.extend(("indexerIds", str(indexer_id)) for indexer_id in self.indexer_ids)
        timeout = httpx.Timeout(self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
        headers = {
            "Accept": "application/json",
            "X-Api-Key": api_key,
            "User-Agent": "Omakase-Plus/catalog",
        }
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            async with client.stream(
                "GET",
                _endpoint(base_url),
                params=params,
                headers=headers,
            ) as response:
                response.raise_for_status()
                declared = _safe_int(
                    response.headers.get("content-length"),
                    maximum=_MAX_RESPONSE_BYTES + 1,
                )
                if declared > _MAX_RESPONSE_BYTES:
                    raise ValueError("Prowlarr search response is too large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_RESPONSE_BYTES:
                        raise ValueError("Prowlarr search response is too large")
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Prowlarr search response is not valid JSON") from exc
            if not isinstance(payload, list):
                raise ValueError("Prowlarr search response must be a JSON array")
            resolved_payload = await _resolve_proxy_magnets(
                client,
                base_url=base_url,
                headers=headers,
                rows=payload,
            )
            return parse_releases(resolved_payload)
