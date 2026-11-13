"""Bounded metadata discovery never establishes usable episode coverage."""

import asyncio
import importlib

import httpx
import pytest


def payload():
    return [
        {
            "id": 123,
            "title": "[Group] Bleach Sennen Kessen-hen - 44 [1080p]",
            "info_hash": "a" * 40,
            "magnet_uri": "magnet:?xt=urn:btih:" + "a" * 40,
            "timestamp": 1791800000,
            "seeders": 12,
            "leechers": 1,
            "total_size": 1234567,
            "num_files": 1,
            "status": "complete",
        }
    ]


def search(handler):
    adapter = importlib.import_module("omakase.plus.animetosho")
    return asyncio.run(
        adapter.AnimeToshoCatalog(transport=httpx.MockTransport(handler)).search("Bleach 44")
    )


def test_public_mirror_preserves_actual_metadata_without_promoting_trust_or_readiness():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=payload())

    result = search(handler)
    assert len(requests) == len(result) == 1
    assert str(requests[0].url) == "https://animetosho.org/feed/json?q=Bleach+44"
    assert (result[0].seeders, result[0].size_bytes, result[0].is_trusted) == (12, 1234567, False)
    assert result[0].magnet == "magnet:?xt=urn:btih:" + "A" * 40
    assert not result[0].torrent_url


@pytest.mark.parametrize(
    "change",
    [
        lambda rows: rows[0].update(info_hash="not-a-hash"),
        lambda rows: rows[0].update(magnet_uri="magnet:?xt=urn:btih:" + "b" * 40),
        lambda rows: rows[0].update(title="bad\x00title"),
        lambda rows: rows[0].update(timestamp=True),
        lambda rows: rows[0].update(seeders=-1),
        lambda rows: rows[0].update(total_size="invented"),
    ],
)
def test_malformed_source_metadata_is_rejected(change):
    rows = payload()
    change(rows)
    with pytest.raises(ValueError):
        search(lambda _: httpx.Response(200, json=rows))


def test_missing_peer_and_size_metadata_stays_unknown():
    rows = payload()
    rows[0].update(seeders=None, leechers=None, total_size=None)
    result = search(lambda _: httpx.Response(200, json=rows))
    assert (result[0].seeders, result[0].size_bytes, result[0].size_display) == (0, 0, "Unknown")


def test_redirects_and_oversized_bodies_stop_without_another_request():
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})

    with pytest.raises(httpx.HTTPStatusError):
        search(redirect)
    assert len(requests) == 1
    with pytest.raises(ValueError):
        search(lambda _: httpx.Response(200, content=b" " * 512001))


def test_additional_public_catalog_requires_explicit_runtime_enablement(monkeypatch):
    from omakase.plus import catalog_config

    monkeypatch.setattr(catalog_config, "read_secret", lambda *_: None)
    monkeypatch.delenv("OMAKASE_ANIMETOSHO_ENABLED", raising=False)
    assert catalog_config.configured_catalogs(object(), 2).sources == ()
    monkeypatch.setenv("OMAKASE_ANIMETOSHO_ENABLED", "true")
    config = catalog_config.configured_catalogs(object(), 2)
    assert [source.kind for source in config.sources] == ["animetosho"]
    assert config.state == "ready"


@pytest.mark.parametrize("url", ["https://catalog.example", "invalid"])
def test_optional_public_source_survives_incomplete_or_invalid_other_configuration(
    monkeypatch, url
):
    from omakase.plus import catalog_config

    monkeypatch.setenv("OMAKASE_ANIMETOSHO_ENABLED", "true")
    monkeypatch.setattr(
        catalog_config,
        "read_secret",
        lambda _, __, key: (
            url
            if key == "prowlarr_url"
            else "key"
            if key == "prowlarr_api_key" and url == "invalid"
            else None
        ),
    )
    config = catalog_config.configured_catalogs(object(), 2)
    assert config.state in {"invalid", "incomplete"}
    assert [source.kind for source in config.sources] == ["animetosho"]
