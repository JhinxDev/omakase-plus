"""Validation for user-selected outbound LLM endpoints."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class OutboundURLError(ValueError):
    """A user-selected URL is malformed or crosses the server trust boundary."""


def validate_user_outbound_url(
    value: str,
    *,
    allow_private: bool = False,
    allowed_hosts: set[str] | None = None,
) -> str:
    """Return a validated HTTP(S) base URL.

    Production defaults reject loopback, private, link-local, multicast,
    unspecified, and otherwise non-global resolutions. Self-hosters can
    explicitly allow private LLM endpoints.
    """
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise OutboundURLError("The LLM URL is invalid.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OutboundURLError("The LLM URL must be an absolute HTTP or HTTPS URL.")
    if parsed.username is not None or parsed.password is not None:
        raise OutboundURLError("The LLM URL cannot contain credentials.")
    if allow_private:
        return candidate.rstrip("/")
    if allowed_hosts is not None and parsed.hostname.casefold() not in {
        host.casefold() for host in allowed_hosts
    }:
        raise OutboundURLError("Custom LLM hosts are disabled on this server.")
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname,
                port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        }
    except OSError as exc:
        raise OutboundURLError("The LLM host could not be resolved.") from exc
    if not addresses:
        raise OutboundURLError("The LLM host could not be resolved.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise OutboundURLError("The LLM host resolved to an invalid address.") from exc
        if not ip.is_global:
            raise OutboundURLError("Private or local LLM URLs are disabled on this server.")
    return candidate.rstrip("/")
