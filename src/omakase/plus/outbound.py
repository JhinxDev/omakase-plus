"""Runtime outbound destination policy and DNS pinning for private integrations.

Every network operation validates current DNS and connects to a checked IP. The
saved hostname remains HTTP Host/TLS SNI. Redirects and environment proxies must
stay disabled in the caller. OMAKASE_ALLOW_PRIVATE_CATALOG_URLS is a trusted
server policy permitting private LAN endpoints. Loopback additionally requires
OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS. Metadata/link-local destinations stay blocked.

An explicitly injected httpx.MockTransport with no resolver is an offline test
seam: it preserves synthetic hostnames and cannot create network connections.
Literal addresses and metadata hostnames are still checked in that seam.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[tuple[IPAddress, ...]]]
_METADATA_HOSTS = {
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}
_METADATA_IPS = {"168.63.129.16", "100.100.100.200", "fd00:ec2::254"}


class OutboundError(ValueError):
    """Constant diagnostics; never includes a submitted URL or DNS answer."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("The outbound service destination could not be used safely.")


def private_destinations_allowed() -> bool:
    return os.getenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def loopback_destinations_allowed() -> bool:
    return os.getenv("OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS", "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def blocked_address(
    address: IPAddress, *, allow_private: bool, allow_loopback: bool = False
) -> bool:
    address = getattr(address, "ipv4_mapped", None) or address
    if (
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or str(address) in _METADATA_IPS
    ):
        return True
    if address.is_loopback:
        return not (allow_private and allow_loopback)
    # Avoid embedded-address transition routes that obscure the actual target.
    if isinstance(address, ipaddress.IPv6Address) and (
        address.sixtofour is not None or address.teredo is not None
    ):
        return True
    if address.is_reserved:
        return True
    return not address.is_global and not allow_private


def normalize_service_url(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1000
        or any(ord(c) < 33 for c in value)
    ):
        raise OutboundError("invalid_input")
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or (parsed.port is not None and not 0 < parsed.port <= 65535)
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or "\\" in value
            or "%" in value
            or "//" in parsed.path
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError
        url = httpx.URL(value.rstrip("/").removesuffix("/api/v1"))
    except (ValueError, httpx.InvalidURL, UnicodeError):
        raise OutboundError("invalid_input") from None
    return str(url)


async def _resolved_ips(hostname: str, port: int) -> tuple[IPAddress, ...]:
    rows = await asyncio.wait_for(
        asyncio.to_thread(socket.getaddrinfo, hostname, port, type=socket.SOCK_STREAM), timeout=3.0
    )
    return tuple(
        sorted({ipaddress.ip_address(row[4][0].split("%", 1)[0]) for row in rows}, key=str)
    )


@dataclass(frozen=True, slots=True)
class PinnedDestination:
    base_url: str = field(repr=False)
    host_header: str = field(repr=False)
    sni_hostname: str = field(repr=False)


async def pin_destination(
    base_url: str,
    *,
    transport: httpx.AsyncBaseTransport | httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> PinnedDestination:
    normalized = normalize_service_url(base_url)
    url = httpx.URL(normalized)
    hostname = url.host.casefold().rstrip(".")
    allow_private, allow_loopback = private_destinations_allowed(), loopback_destinations_allowed()
    if hostname in _METADATA_HOSTS:
        raise OutboundError("unsafe_destination")
    if (hostname == "localhost" or hostname.endswith(".localhost")) and not (
        allow_private and allow_loopback
    ):
        raise OutboundError("unsafe_destination")
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None and blocked_address(
        literal, allow_private=allow_private, allow_loopback=allow_loopback
    ):
        raise OutboundError("unsafe_destination")
    if literal is None and re.fullmatch(r"(?:0x[0-9a-f]+|[0-9.]+)", hostname):
        raise OutboundError("unsafe_destination")
    host_header = url.netloc.decode("ascii")
    # This seam cannot perform network I/O. All real/custom network transports
    # must resolve and pin, including explicit AsyncHTTPTransport injections.
    if isinstance(transport, httpx.MockTransport) and resolver is None:
        return PinnedDestination(normalized, host_header, hostname)
    try:
        addresses = (
            (literal,)
            if literal is not None
            else await (resolver or _resolved_ips)(
                hostname, url.port or (443 if url.scheme == "https" else 80)
            )
        )
        if not isinstance(addresses, (tuple, list)) or not addresses or len(addresses) > 32:
            raise OutboundError("unsafe_destination")
        addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except asyncio.TimeoutError:
        raise OutboundError("transport_timeout") from None
    except (OSError, ValueError, TypeError):
        raise OutboundError("transport_error") from None
    if any(
        blocked_address(address, allow_private=allow_private, allow_loopback=allow_loopback)
        for address in addresses
    ):
        raise OutboundError("unsafe_destination")
    # Many self-hosted Docker networks have no IPv6 route. Prefer a validated
    # IPv4 answer when present; retain IPv6-only support and validate every
    # answer above before choosing either family.
    selected = min(addresses, key=lambda address: (address.version, str(address)))
    pinned = str(url.copy_with(host=str(selected)))
    return PinnedDestination(pinned, host_header, hostname)
