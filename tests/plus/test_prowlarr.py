from __future__ import annotations

import asyncio
import base64
import json
from datetime import datetime, timezone

import httpx
import pytest

from omakase.plus import catalog, catalog_config
from omakase.plus.nyaa import NyaaTorrent
from omakase.plus.prowlarr import (
    ProwlarrCatalog,
    ProwlarrConfigurationError,
    normalize_base_url,
    parse_positive_ids,
    parse_releases,
)


def _row(
    *,
    title: str = "Show S01E01 [1080p] [Multi-Subs]",
    torrent_hash: str = "A" * 40,
) -> dict[str, object]:
    return {
        "protocol": "torrent",
        "title": title,
        "infoHash": torrent_hash,
        "size": 1_073_741_824,
        "seeders": 42,
        "leechers": 3,
        "publishDate": "2026-07-26T12:00:00Z",
    }


def _torrent(title: str, torrent_hash: str) -> NyaaTorrent:
    return NyaaTorrent(
        title=title,
        magnet=f"magnet:?xt=urn:btih:{torrent_hash}",
        seeders=10,
        leechers=0,
        size_bytes=1024,
        size_display="1 KiB",
        pub_date=datetime(2026, 7, 26, tzinfo=timezone.utc),
        is_trusted=False,
        is_batch=False,
    )


def test_parse_releases_constructs_minimal_magnet_and_filters_unsafe_rows():
    huge = _row(torrent_hash="B" * 40)
    huge["size"] = 151 * 1024**3
    usenet = _row(torrent_hash="C" * 40)
    usenet["protocol"] = "usenet"

    releases = parse_releases([_row(), huge, usenet, {"title": "missing hash"}])

    assert len(releases) == 1
    assert releases[0].magnet == f"magnet:?xt=urn:btih:{'A' * 40}"
    assert releases[0].seeders == 42
    assert releases[0].size_display == "1.0 GiB"


def test_parse_releases_accepts_hash_from_a_real_magnet():
    row = _row()
    row["infoHash"] = ""
    row["magnetUrl"] = f"magnet:?xt=urn:btih:{'D' * 40}&dn=show"

    releases = parse_releases([row])

    assert releases[0].magnet == f"magnet:?xt=urn:btih:{'D' * 40}"


def test_parse_releases_canonicalizes_a_base32_magnet():
    raw_hash = bytes.fromhex("AB" * 20)
    base32_hash = base64.b32encode(raw_hash).decode()
    row = _row()
    row["infoHash"] = ""
    row["magnetUrl"] = f"magnet:?xt=urn:btih:{base32_hash}&dn=show"

    releases = parse_releases([row])

    assert releases[0].magnet == f"magnet:?xt=urn:btih:{'AB' * 20}"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://catalog.example",
        "https://user:pass@catalog.example",
        "https://catalog.example?api_key=secret",
        "https://catalog.example/#fragment",
    ],
)
def test_normalize_base_url_rejects_unsafe_shapes(value):
    with pytest.raises(ProwlarrConfigurationError):
        normalize_base_url(value)


def test_normalize_base_url_accepts_a_reverse_proxy_base_path():
    assert (
        normalize_base_url("https://catalog.example/prowlarr/api/v1/search")
        == "https://catalog.example/prowlarr"
    )


def test_positive_id_parser_deduplicates_and_rejects_non_numeric_values():
    assert parse_positive_ids("5070, 5000,5070") == (5070, 5000)
    assert parse_positive_ids("", default=(5070,)) == (5070,)
    with pytest.raises(ProwlarrConfigurationError):
        parse_positive_ids("5070,anime")


def test_catalog_configuration_distinguishes_optional_incomplete_and_ready(monkeypatch):
    values: dict[str, str] = {}
    monkeypatch.setattr(
        catalog_config,
        "read_secret",
        lambda _db, _user_id, key: values.get(key),
    )

    assert catalog_config.configured_catalogs(object(), 1).state == "optional"
    values["prowlarr_url"] = "http://prowlarr:9696"
    assert catalog_config.configured_catalogs(object(), 1).state == "incomplete"
    values["prowlarr_api_key"] = "fixture-key"
    configuration = catalog_config.configured_catalogs(object(), 1)
    assert configuration.state == "ready"
    assert configuration.sources[0].kind == "prowlarr"
    assert "fixture-key" not in repr(configuration)


def test_search_keeps_api_key_out_of_url_and_uses_bounded_filters():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[_row()])

    source = ProwlarrCatalog(
        base_url="https://catalog.example/prowlarr",
        api_key="fixture-key",
        indexer_ids=(4, 7),
        categories=(5070, 5000),
        resolved_addresses=("93.184.216.34",),
        transport=httpx.MockTransport(handler),
    )

    releases = asyncio.run(source.search("Show"))

    assert len(releases) == 1
    request = requests[0]
    assert request.url.path == "/prowlarr/api/v1/search"
    assert "fixture-key" not in str(request.url)
    assert request.headers["X-Api-Key"] == "fixture-key"
    assert request.url.params.get_list("indexerIds") == ["4", "7"]
    assert request.url.params.get_list("categories") == ["5070", "5000"]
    assert request.url.params["limit"] == "500"
    assert "fixture-key" not in repr(source)


def test_search_normalizes_punctuation_that_indexers_treat_as_operators():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[_row()])

    source = ProwlarrCatalog(
        base_url="https://catalog.example/prowlarr",
        api_key="fixture-key",
        resolved_addresses=("93.184.216.34",),
        transport=httpx.MockTransport(handler),
    )

    asyncio.run(source.search("KAMUI ---He's behind you"))

    assert requests[0].url.params["query"] == "KAMUI Hes behind you"


def test_search_resolves_same_server_proxy_magnet_without_query_string_key():
    requests: list[httpx.Request] = []
    raw_hash = bytes.fromhex("CD" * 20)
    base32_hash = base64.b32encode(raw_hash).decode()
    row = _row()
    row["infoHash"] = ""
    row["magnetUrl"] = (
        "https://untrusted-host.invalid/2/download?apikey=fixture-key&link=opaque-link&file=show"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/search":
            return httpx.Response(200, json=[row])
        assert request.url.host == "catalog.example"
        assert request.url.path == "/2/download"
        assert "apikey" not in request.url.params
        assert "fixture-key" not in str(request.url)
        assert request.headers["X-Api-Key"] == "fixture-key"
        return httpx.Response(
            301,
            headers={"Location": f"magnet:?xt=urn:btih:{base32_hash}&dn=show"},
        )

    source = ProwlarrCatalog(
        base_url="https://catalog.example",
        api_key="fixture-key",
        resolved_addresses=("93.184.216.34",),
        transport=httpx.MockTransport(handler),
    )

    releases = asyncio.run(source.search("Show"))

    assert len(releases) == 1
    assert releases[0].magnet == f"magnet:?xt=urn:btih:{'CD' * 20}"
    assert len(requests) == 2


def test_search_blocks_private_addresses_until_the_operator_opts_in():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    source = ProwlarrCatalog(
        base_url="http://prowlarr:9696",
        api_key="fixture-key",
        resolved_addresses=("172.18.0.2",),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProwlarrConfigurationError, match="explicitly allow"):
        asyncio.run(source.search("Show"))
    assert called is False


def test_search_allows_an_explicit_trusted_private_catalog():
    source = ProwlarrCatalog(
        base_url="http://prowlarr:9696",
        api_key="fixture-key",
        allow_private=True,
        resolved_addresses=("172.18.0.2",),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=[_row()])),
    )

    assert len(asyncio.run(source.search("Show"))) == 1


def test_search_refuses_an_oversized_response():
    payload = json.dumps([_row()]).encode() + (b" " * (2 * 1024 * 1024))
    source = ProwlarrCatalog(
        base_url="https://catalog.example",
        api_key="fixture-key",
        resolved_addresses=("93.184.216.34",),
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=payload)),
    )

    with pytest.raises(ValueError, match="too large"):
        asyncio.run(source.search("Show"))


def test_multi_catalog_alias_window_survives_one_outage_and_tracks_provenance(monkeypatch):
    async def nyaa_down(_alias: str):
        raise RuntimeError("temporarily unavailable")

    class ExtraCatalog:
        kind = "prowlarr"

        async def search(self, _query: str):
            return [_torrent("Show S01E01 [1080p] [Multi-Subs]", "E" * 40)]

    monkeypatch.setattr(catalog, "search", nyaa_down)

    windows = asyncio.run(
        catalog.search_alias_windows(
            ["Show"],
            season=1,
            expected=1,
            additional_catalogs=(ExtraCatalog(),),
        )
    )

    assert windows[0].provider_complete is False
    assert windows[0].error_code == "NYAA_ERROR"
    assert len(windows[0].candidates) == 1
    assert windows[0].candidates[0].catalog_kind == "prowlarr"


def test_successful_empty_http_alias_does_not_degrade_sibling_window(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[_row()] if request.url.params["query"] == "Show" else [])

    source = ProwlarrCatalog(
        base_url="https://catalog.example",
        api_key="test-key",
        resolved_addresses=("93.184.216.34",),
        transport=httpx.MockTransport(handler),
    )

    async def empty_nyaa(_query):
        return []

    monkeypatch.setattr(catalog, "search", empty_nyaa)
    windows = asyncio.run(
        catalog.search_alias_windows(
            ["Show", "Romaji"],
            season=1,
            expected=1,
            additional_catalogs=[source],
        )
    )
    assert len(requests) == 2
    assert all(window.provider_complete and not window.error_code for window in windows)
    assert [window.source_receipts[-1].candidate_count for window in windows] == [1, 0]
    assert all(window.source_receipts[-1].status == "healthy" for window in windows)
    assert [candidate.torrent_hash for candidate in catalog.merge_alias_windows(windows)] == [
        "A" * 40
    ]
