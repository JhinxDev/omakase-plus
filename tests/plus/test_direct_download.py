from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omakase.plus.direct import (
    API_URL,
    DirectDownloadTarget,
    parse_anilist_id,
    resolve_direct_request,
)


def _mock_anilist_client(payload: dict):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status.return_value = None

    client = MagicMock()
    client.post.return_value = response
    client.__enter__.return_value = client
    return client


def test_parse_anilist_id_accepts_url_and_bare_id():
    assert parse_anilist_id("https://anilist.co/anime/1/Cowboy-Bebop/") == 1
    assert parse_anilist_id("21") == 21
    assert parse_anilist_id("Cowboy Bebop") is None


def test_resolve_direct_request_rejects_blank_query():
    with pytest.raises(ValueError, match="anime title"):
        resolve_direct_request("  ")


def test_resolve_direct_request_fetches_media_by_id():
    client = _mock_anilist_client(
        {
            "data": {
                "Media": {
                    "id": 1,
                    "title": {
                        "romaji": "Cowboy Bebop",
                        "english": "Cowboy Bebop",
                        "native": "\u30ab\u30a6\u30dc\u30fc\u30a4\u30d3\u30d0\u30c3\u30d7",
                    },
                    "format": "TV",
                    "status": "FINISHED",
                    "episodes": 26,
                    "season": "SPRING",
                    "seasonYear": 1998,
                }
            }
        }
    )

    with patch("httpx.Client", return_value=client):
        result = resolve_direct_request("https://anilist.co/anime/1/Cowboy-Bebop/")

    assert isinstance(result, DirectDownloadTarget)
    assert result.anilist_id == 1
    assert result.title == "Cowboy Bebop"
    assert result.search_titles[:2] == (
        "Cowboy Bebop",
        "\u30ab\u30a6\u30dc\u30fc\u30a4\u30d3\u30d0\u30c3\u30d7",
    )
    assert result.format == "TV"
    assert result.status == "FINISHED"
    assert result.episodes == 26
    assert result.season == "SPRING"
    assert result.season_year == 1998

    call_kwargs = client.post.call_args.kwargs
    assert client.post.call_args.args == (API_URL,)
    assert "Media(id: $id, type: ANIME)" in call_kwargs["json"]["query"]
    assert call_kwargs["json"]["variables"] == {"id": 1}


def test_resolve_direct_request_searches_title_with_season_hint():
    client = _mock_anilist_client(
        {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 99699,
                            "title": {
                                "romaji": "Golden Kamuy 3rd Season",
                                "english": "Golden Kamuy Season 3",
                                "native": "\u30b4\u30fc\u30eb\u30c7\u30f3\u30ab\u30e0\u30a4 \u7b2c\u4e09\u671f",
                            },
                            "format": "TV",
                            "status": "FINISHED",
                            "episodes": 12,
                            "season": "FALL",
                            "seasonYear": 2020,
                        }
                    ]
                }
            }
        }
    )

    with patch("httpx.Client", return_value=client):
        result = resolve_direct_request("Golden Kamuy", season="3")

    assert result.anilist_id == 99699
    assert result.title == "Golden Kamuy Season 3"
    assert "Golden Kamuy 3rd Season" in result.search_titles
    assert "Golden Kamuy Season 3 Season 3" not in result.search_titles

    call_kwargs = client.post.call_args.kwargs
    assert "Page(page: 1, perPage: 5)" in call_kwargs["json"]["query"]
    assert "isAdult: false" in call_kwargs["json"]["query"]
    assert call_kwargs["json"]["variables"] == {"search": "Golden Kamuy Season 3"}


def test_resolve_direct_request_appends_textual_arc_hint_as_is():
    client = _mock_anilist_client(
        {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 131681,
                            "title": {
                                "romaji": "Shingeki no Kyojin: The Final Season Part 2",
                                "english": "Attack on Titan Final Season Part 2",
                                "native": "\u9032\u6483\u306e\u5de8\u4eba The Final Season Part 2",
                            },
                            "format": "TV",
                            "status": "FINISHED",
                            "episodes": 12,
                            "season": "WINTER",
                            "seasonYear": 2022,
                        }
                    ]
                }
            }
        }
    )

    with patch("httpx.Client", return_value=client):
        result = resolve_direct_request("Attack on Titan", season="Final Season Part 2")

    assert result.anilist_id == 131681
    assert result.title == "Attack on Titan Final Season Part 2"
    assert (
        "Attack on Titan Final Season Part 2 Season Final Season Part 2" not in result.search_titles
    )
    assert "Shingeki no Kyojin: The Final Season Part 2" in result.search_titles

    call_kwargs = client.post.call_args.kwargs
    assert call_kwargs["json"]["variables"] == {"search": "Attack on Titan Final Season Part 2"}


def test_resolve_direct_request_deduplicates_title_aliases():
    client = _mock_anilist_client(
        {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 1,
                            "title": {
                                "romaji": "Frieren",
                                "english": "Frieren",
                                "native": "Sousou no Frieren",
                            },
                            "format": "TV",
                            "status": "FINISHED",
                            "episodes": 28,
                            "season": "FALL",
                            "seasonYear": 2023,
                        }
                    ]
                }
            }
        }
    )

    with patch("httpx.Client", return_value=client):
        result = resolve_direct_request("Frieren", season="1")

    assert result.search_titles == (
        "Frieren",
        "Sousou no Frieren",
        "Frieren Season 1",
        "Sousou no Frieren Season 1",
    )
