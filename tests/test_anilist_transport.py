"""Catalog transport failures stay bounded and safe before inference."""

import io
import json
from urllib.error import HTTPError, URLError

import pytest

from omakase.adapters import anilist


@pytest.mark.parametrize(
    "status,category", [(400, "request"), (403, "access"), (429, "rate_limit"), (503, "upstream")]
)
def test_catalog_http_error_is_safe_without_retry(monkeypatch, status, category):
    calls = []

    def fail(request, timeout):
        calls.append(timeout)
        raise HTTPError(
            "https://example.invalid/?secret=private-value",
            status,
            "private-body",
            {},
            io.BytesIO(b"private-provider-body"),
        )

    monkeypatch.setattr(anilist, "urlopen", fail)
    with pytest.raises(anilist.AniListCatalogError) as result:
        anilist.AniListAdapter()._graphql("query { id }", {"username": "private-user"})
    assert result.value.category == category
    assert result.value.status_code == status
    assert str(result.value) == "AniList catalog request failed"
    assert calls == [20]


@pytest.mark.parametrize("error", [TimeoutError("private-host"), URLError("private-url")])
def test_catalog_transport_error_is_safe(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(anilist, "urlopen", fail)
    with pytest.raises(anilist.AniListCatalogError) as result:
        anilist.AniListAdapter()._graphql("query { id }")
    assert result.value.category == "transport"
    assert str(result.value) == "AniList catalog request failed"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        json.dumps({"errors": [{"message": "private-data"}], "data": None}).encode(),
        b"[]",
    ],
)
def test_catalog_invalid_response_is_not_silent_empty_candidates(monkeypatch, payload):
    monkeypatch.setattr(anilist, "urlopen", lambda *args, **kwargs: io.BytesIO(payload))
    with pytest.raises(anilist.AniListCatalogError) as result:
        anilist.AniListAdapter()._graphql("query { id }")
    assert result.value.category == "invalid_response"
