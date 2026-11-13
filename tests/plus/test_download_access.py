import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from omakase.plus.provider_limits import ProviderRateLimit
from omakase.plus.realdebrid import RDFile, RDTorrent, RealDebridClient, RealDebridProviderBlock


@pytest.mark.parametrize("code", [451, 403])
def test_downloaded_inventory_does_not_hide_link_restriction(monkeypatch, code):
    torrent = RDTorrent(
        "id",
        "Show",
        "downloaded",
        100,
        100,
        100,
        ("https://example.invalid/private-link",),
        (RDFile(1, "/Show.mkv", 100, True),),
    )
    rd = RealDebridClient("secret")
    monkeypatch.setattr(rd, "get_torrent", AsyncMock(return_value=torrent))
    transport = httpx.MockTransport(lambda req: httpx.Response(code, json={"error_code": 35}))
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        "omakase.plus.realdebrid.httpx.AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    with pytest.raises(RealDebridProviderBlock):
        asyncio.run(rd.verify_download("id"))


def test_success_requires_actual_download_link(monkeypatch):
    rd = RealDebridClient("secret")
    torrent = RDTorrent(
        "id",
        "Show",
        "downloaded",
        100,
        100,
        100,
        ("https://example.invalid/private-link",),
        (RDFile(1, "/Show.mkv", 100, True),),
    )
    monkeypatch.setattr(rd, "get_torrent", AsyncMock(return_value=torrent))
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    factory = httpx.AsyncClient
    monkeypatch.setattr(
        "omakase.plus.realdebrid.httpx.AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    assert asyncio.run(rd.verify_download("id")) is False


@pytest.mark.parametrize(
    "response_code, payload, expected",
    [
        (200, {"download": "https://example.invalid/download"}, True),
        (503, {"error": "unavailable"}, False),
    ],
)
def test_download_access_results_are_distinguished(monkeypatch, response_code, payload, expected):
    rd = RealDebridClient("secret")
    torrent = RDTorrent(
        "id",
        "Show",
        "downloaded",
        100,
        100,
        100,
        ("https://example.invalid/private-link",),
        (RDFile(1, "/Show.mkv", 100, True),),
    )
    monkeypatch.setattr(rd, "get_torrent", AsyncMock(return_value=torrent))
    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(response_code, json=payload))
    monkeypatch.setattr(
        "omakase.plus.realdebrid.httpx.AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    assert asyncio.run(rd.verify_download("id")) is expected


def test_already_selected_is_success(monkeypatch):
    factory = httpx.AsyncClient
    transport = httpx.MockTransport(lambda req: httpx.Response(202))
    monkeypatch.setattr(
        "omakase.plus.realdebrid.httpx.AsyncClient",
        lambda **kwargs: factory(transport=transport, **kwargs),
    )
    assert asyncio.run(RealDebridClient("secret").select_files("id"))


def test_mismatched_links_never_count_as_verified(monkeypatch):
    rd = RealDebridClient("secret")
    torrent = RDTorrent(
        "id", "Show", "downloaded", 100, 100, 100, (), (RDFile(1, "/Show.mkv", 100, True),)
    )
    monkeypatch.setattr(rd, "get_torrent", AsyncMock(return_value=torrent))
    assert asyncio.run(rd.verify_download("id")) is False


@pytest.mark.parametrize(("retry_after", "expected_delay"), [(None, 900), ("7200", 7200)])
def test_download_link_429_stops_verification_and_retains_provider_pause(
    monkeypatch, retry_after, expected_delay
):
    observed = []
    rd = RealDebridClient("synthetic", rate_limit_observer=observed.append)
    torrent = RDTorrent(
        "id",
        "Show",
        "downloaded",
        100,
        200,
        200,
        ("https://example.invalid/private-first", "https://example.invalid/private-second"),
        (RDFile(1, "/Show1.mkv", 100, True), RDFile(2, "/Show2.mkv", 100, True)),
    )
    monkeypatch.setattr(rd, "get_torrent", AsyncMock(return_value=torrent))
    requests = []

    def response(request):
        requests.append(request.method)
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return httpx.Response(429, json={"error": "too_many_requests"}, headers=headers)

    factory = httpx.AsyncClient
    monkeypatch.setattr(
        "omakase.plus.realdebrid.httpx.AsyncClient",
        lambda **kwargs: factory(transport=httpx.MockTransport(response), **kwargs),
    )
    with pytest.raises(ProviderRateLimit) as error:
        asyncio.run(rd.verify_download("id"))
    assert error.value.retry_after_seconds == expected_delay
    assert observed == [error.value]
    assert requests == ["POST"]  # The second media link must never be attempted.
    assert "private-first" not in error.value.detail and "synthetic" not in error.value.detail
    # A subsequent operation on this client cannot turn a pause into a normal failure.
    with pytest.raises(ProviderRateLimit) as paused:
        asyncio.run(rd.list_torrents())
    assert 0 < paused.value.retry_after_seconds <= expected_delay
    assert requests == ["POST"]
