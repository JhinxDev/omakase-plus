"""Runtime movie HTTP pins validated destinations, independently of saved health."""

from __future__ import annotations

import importlib
import importlib.util
import ipaddress
from dataclasses import replace

import httpx
import pytest

from tests.plus.test_movies import NativeMovies, run, service
from tests.plus.test_movies import db as db


def outbound():
    assert importlib.util.find_spec("omakase.plus.outbound") is not None, (
        "Runtime outbound validation is missing"
    )
    return importlib.import_module("omakase.plus.outbound")


@pytest.fixture(autouse=True)
def private_policy(monkeypatch):
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "false")
    monkeypatch.delenv("OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS", raising=False)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:32400",
        "http://[::1]",
        "http://169.254.169.254",
        "http://168.63.129.16",
        "http://100.100.100.200",
        "http://[fd00:ec2::254]",
        "http://[::ffff:169.254.169.254]",
        "http://metadata.google.internal",
        "http://metadata.google.internal.",
    ],
)
def test_unsafe_literal_or_metadata_host_never_reaches_transport(url):
    module = outbound()
    transport = httpx.MockTransport(
        lambda request: pytest.fail("unsafe destination must not connect")
    )
    with pytest.raises(module.OutboundError) as error:
        run(module.pin_destination(url, transport=transport))
    assert error.value.code == "unsafe_destination"
    assert url not in str(error.value)


def test_private_lan_permission_does_not_implicitly_allow_loopback(monkeypatch):
    module = outbound()
    monkeypatch.setenv("OMAKASE_ALLOW_PRIVATE_CATALOG_URLS", "true")
    target = run(
        module.pin_destination(
            "http://192.168.50.99:32400", transport=httpx.MockTransport(lambda request: None)
        )
    )
    assert target.base_url.startswith("http://192.168.50.99:32400")
    with pytest.raises(module.OutboundError):
        run(
            module.pin_destination(
                "http://127.0.0.1:32400", transport=httpx.MockTransport(lambda request: None)
            )
        )
    monkeypatch.setenv("OMAKASE_ALLOW_LOOPBACK_MEDIA_URLS", "true")
    target = run(
        module.pin_destination(
            "http://127.0.0.1:32400", transport=httpx.MockTransport(lambda request: None)
        )
    )
    assert "127.0.0.1" in target.base_url
    with pytest.raises(module.OutboundError):
        run(
            module.pin_destination(
                "http://169.254.169.254", transport=httpx.MockTransport(lambda request: None)
            )
        )


def test_every_dns_answer_is_checked_and_mixed_private_answers_are_rejected():
    module = outbound()

    async def mixed(host, port):
        return (ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("169.254.169.254"))

    with pytest.raises(module.OutboundError) as error:
        run(module.pin_destination("https://service.example.invalid", resolver=mixed))
    assert error.value.code == "unsafe_destination"


def test_dual_stack_pinning_prefers_ipv4_without_losing_host_or_sni():
    module = outbound()

    async def dual_stack(_host, _port):
        return (ipaddress.ip_address("2606:4700:4700::1111"), ipaddress.ip_address("1.1.1.1"))

    target = run(module.pin_destination("https://service.example.invalid/api", resolver=dual_stack))
    assert target.base_url == "https://1.1.1.1/api"
    assert target.host_header == target.sni_hostname == "service.example.invalid"


def test_ipv6_only_pinning_remains_supported():
    module = outbound()

    async def ipv6_only(_host, _port):
        return (ipaddress.ip_address("2606:4700:4700::1111"),)

    target = run(module.pin_destination("https://service.example.invalid", resolver=ipv6_only))
    assert target.base_url == "https://[2606:4700:4700::1111]"
    assert target.host_header == target.sni_hostname == "service.example.invalid"


def test_runtime_pins_ip_preserving_host_sni_and_proxy_subpath(db):
    outbound()
    movie_module = importlib.import_module("omakase.plus.movies")
    fake = NativeMovies()
    original = service(db, fake)
    config = replace(original.connections, seerr_url="https://metadata.example.invalid:8443/seerr")
    resolutions, requests = [], []

    async def resolver(host, port):
        resolutions.append((host, port))
        return (ipaddress.ip_address("93.184.216.34"),)

    def route(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.url.port == 8443
        assert request.url.path == "/seerr/api/v1/ping"
        assert request.headers["Host"] == "metadata.example.invalid:8443"
        assert request.extensions["sni_hostname"] == "metadata.example.invalid"
        assert request.headers["X-Api-Key"] == "seerr-secret"
        assert "secret" not in str(request.url)
        return httpx.Response(200, json={"ok": True})

    svc = movie_module.MovieService(
        db, config, transport=httpx.MockTransport(route), resolver=resolver
    )
    assert run(svc._json("seerr", "/api/v1/ping")) == {"ok": True}
    assert resolutions == [("metadata.example.invalid", 8443)]
    assert len(requests) == 1


def test_runtime_revalidates_dns_on_each_operation_instead_of_trusting_health(db):
    outbound()
    movie_module = importlib.import_module("omakase.plus.movies")
    original = service(db, NativeMovies())
    calls, requests = [], []

    async def resolver(host, port):
        calls.append((host, port))
        return (ipaddress.ip_address("93.184.216.34" if len(calls) == 1 else "127.0.0.1"),)

    def route(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        return httpx.Response(200, json={"ok": True})

    svc = movie_module.MovieService(
        db, original.connections, transport=httpx.MockTransport(route), resolver=resolver
    )
    run(svc._json("seerr", "/api/v1/ping"))
    with pytest.raises(movie_module.MovieError) as error:
        run(svc._json("seerr", "/api/v1/ping"))
    assert error.value.code == "unsafe_destination"
    assert len(calls) == 2 and len(requests) == 1


def test_plex_probe_uses_the_same_runtime_pinning_boundary(db):
    outbound()
    movie_module = importlib.import_module("omakase.plus.movies")
    original = service(db, NativeMovies())
    requests = []

    async def resolver(host, port):
        return (ipaddress.ip_address("93.184.216.34"),)

    def route(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "plex.invalid:32400"
        assert request.headers["X-Plex-Token"] == "plex-secret"
        assert request.headers["Range"] == "bytes=0-65535"
        return httpx.Response(
            206, content=b"x" * 65536, headers={"Content-Range": "bytes 0-65535/1000000"}
        )

    svc = movie_module.MovieService(
        db, original.connections, transport=httpx.MockTransport(route), resolver=resolver
    )
    assert run(svc._probe("/library/parts/11/file.mkv"))["code"] == "ready"
    assert len(requests) == 1


def test_unsafe_probe_returns_safe_diagnostic_without_sending_plex_token(db):
    outbound()
    movie_module = importlib.import_module("omakase.plus.movies")
    original = service(db, NativeMovies())

    async def resolver(host, port):
        return (ipaddress.ip_address("169.254.169.254"),)

    svc = movie_module.MovieService(
        db,
        original.connections,
        transport=httpx.MockTransport(lambda request: pytest.fail("unsafe probe must not connect")),
        resolver=resolver,
    )
    result = run(svc._probe("/library/parts/11/file.mkv"))
    assert result["code"] == "unsafe_destination" and result["bytes_read"] == 0
    assert "secret" not in str(result)
