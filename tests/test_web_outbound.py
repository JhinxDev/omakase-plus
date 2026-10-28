"""Security contracts for user-selected LLM endpoints."""

from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from omakase.web.outbound import OutboundURLError, validate_user_outbound_url


def _resolution(address: str):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


def test_public_endpoint_accepts_only_global_http_resolution():
    with patch("socket.getaddrinfo", return_value=_resolution("8.8.8.8")):
        assert (
            validate_user_outbound_url("https://models.example/v1") == "https://models.example/v1"
        )


@pytest.mark.parametrize(
    "url,address",
    [
        ("http://localhost:11434", "127.0.0.1"),
        ("http://metadata.internal", "169.254.169.254"),
        ("http://router.internal", "192.168.50.1"),
        ("http://cluster.internal", "10.0.0.4"),
        ("http://ipv6.internal", "::1"),
    ],
)
def test_public_endpoint_rejects_non_global_resolutions(url, address):
    with (
        patch("socket.getaddrinfo", return_value=_resolution(address)),
        pytest.raises(OutboundURLError, match="disabled"),
    ):
        validate_user_outbound_url(url)


def test_endpoint_rejects_credentials_and_non_http_schemes():
    with pytest.raises(OutboundURLError, match="credentials"):
        validate_user_outbound_url("https://user:password@example.com")
    with pytest.raises(OutboundURLError, match="HTTP or HTTPS"):
        validate_user_outbound_url("file:///etc/passwd")


def test_production_endpoint_rejects_unapproved_public_host():
    with (
        patch("socket.getaddrinfo", return_value=_resolution("8.8.8.8")),
        pytest.raises(OutboundURLError, match="Custom LLM hosts"),
    ):
        validate_user_outbound_url(
            "https://attacker.example",
            allowed_hosts={"api.openai.com"},
        )


def test_private_self_hosted_endpoint_requires_explicit_override():
    assert (
        validate_user_outbound_url(
            "http://192.168.50.139:1234",
            allow_private=True,
        )
        == "http://192.168.50.139:1234"
    )
