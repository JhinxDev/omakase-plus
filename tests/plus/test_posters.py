from __future__ import annotations

import asyncio

import httpx
import pytest

from omakase.plus.posters import PosterUnavailable, poster_bytes


def test_poster_is_verified_cached_and_never_refetched(tmp_path):
    payload = b"\x89PNG\r\n\x1a\n" + b"poster"
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "graphql.anilist.co":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "coverImage": {
                                "extraLarge": "https://s4.anilist.co/file/anilistcdn/poster.png"
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, content=payload, headers={"content-type": "image/png"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await poster_bytes(21, cache_root=tmp_path, client=client)
            second = await poster_bytes(21, cache_root=tmp_path, client=client)
        return first, second

    first, second = asyncio.run(run())

    assert first == second == (payload, "image/png")
    assert len(requests) == 2
    assert (tmp_path / "21.image").read_bytes() == payload


def test_poster_rejects_untrusted_metadata_host(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {"coverImage": {"extraLarge": "https://example.invalid/private.png"}}
                }
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await poster_bytes(21, cache_root=tmp_path, client=client)

    with pytest.raises(PosterUnavailable, match="trusted artwork"):
        asyncio.run(run())


def test_banner_uses_wide_artwork_and_separate_cache(tmp_path):
    from omakase.plus.posters import banner_bytes

    poster = b"\x89PNG\r\n\x1a\nposter"
    banner = b"\x89PNG\r\n\x1a\nwide-banner"
    (tmp_path / "21.image").write_bytes(poster)
    requests = []

    def handler(request):
        requests.append(str(request.url))
        if request.url.host == "graphql.anilist.co":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {"bannerImage": "https://s4.anilist.co/file/anilistcdn/banner.png"}
                    }
                },
            )
        return httpx.Response(200, content=banner, headers={"content-type": "image/png"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await banner_bytes(21, cache_root=tmp_path, client=client)
            second = await banner_bytes(21, cache_root=tmp_path, client=client)
        return first, second

    assert asyncio.run(run()) == ((banner, "image/png"), (banner, "image/png"))
    assert len(requests) == 2
    assert (tmp_path / "21.image").read_bytes() == poster


def test_artwork_stops_reading_oversize_stream(tmp_path, monkeypatch):
    import omakase.plus.posters as posters

    monkeypatch.setattr(posters, "_MAX_POSTER_BYTES", 12)
    consumed = []

    class OversizeImage(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(20):
                consumed.append(index)
                yield b"\x89PNG\r\n\x1a\n" if index == 0 else b"01234567"

    def handler(request):
        if request.url.host == "graphql.anilist.co":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "Media": {
                            "coverImage": {
                                "large": "https://s4.anilist.co/file/anilistcdn/poster.png"
                            }
                        }
                    }
                },
            )
        return httpx.Response(200, stream=OversizeImage(), headers={"content-type": "image/png"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await poster_bytes(21, cache_root=tmp_path, client=client)

    with pytest.raises(PosterUnavailable, match="too large"):
        asyncio.run(run())
    assert len(consumed) <= 2
    assert not (tmp_path / "21.image").exists()
