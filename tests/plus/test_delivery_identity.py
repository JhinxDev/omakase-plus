"""A session's delivery mode and timeline generation are pinned for its lifetime.

The audit reproduced this defect: deleting a prepared asset between the index and
a segment request made the same URL answer with provider-time bytes from a
different timeline, and the player cached them under the URL it already held.
These checks pin the fix: same session, same timeline, or an honest failure.
"""

import asyncio

import pytest
from starlette.requests import Request

from omakase.plus.debrid_library import DebridLibraryError
from omakase.plus.debrid_stream import DebridMedia, DebridStreams, _playlist_digest
from omakase.plus.prepared_hls import PreparedStore, prepared_key

PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:4\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXT-X-PLAYLIST-TYPE:VOD\n"
    "#EXTINF:4.000,\n"
    "00000.ts\n"
    "#EXTINF:4.000,\n"
    "00001.ts\n"
    "#EXT-X-ENDLIST\n"
)
URL = "https://x.real-debrid.test/d/FILEKEY/name.mkv"


def request(asset="", **headers):
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/plus/tv/media/" + asset,
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        }
    )


def store(tmp_path, *, playlist=PLAYLIST):
    (tmp_path / prepared_key(URL)).mkdir(parents=True, exist_ok=True)
    directory = tmp_path / prepared_key(URL)
    (directory / "stream.m3u8").write_bytes(playlist.encode("ascii"))
    for name in ("00000.ts", "00001.ts"):
        (directory / name).write_bytes(b"prepared-bytes-for-" + name.encode())
    return directory


def entry(**updates):
    values = {
        "playlist": b"#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:4.000,\nprovider0.ts\n#EXT-X-ENDLIST\n",
        "segments": ("provider0.ts",),
        "prepared_key": prepared_key(URL),
    }
    return DebridMedia(**(values | updates))


def streams(tmp_path):
    return DebridStreams(prepared_root=tmp_path)


def run(coroutine):
    return asyncio.run(coroutine)


def test_prepared_bytes_are_served_for_a_pinned_pass(tmp_path):
    store(tmp_path)
    live = streams(tmp_path)
    pinned = entry(prepared_pinned=True, prepared_generation=_playlist_digest(PLAYLIST.encode()))

    async def case():
        return await live.deliver(pinned, "00000.ts", request(), lambda: None)

    response = run(case())
    assert response.status_code == 200
    assert response.body == b"prepared-bytes-for-00000.ts"


def test_a_missing_prepared_title_never_becomes_provider_bytes(tmp_path):
    directory = store(tmp_path)
    live = streams(tmp_path)
    pinned = entry(prepared_pinned=True, prepared_generation=_playlist_digest(PLAYLIST.encode()))

    async def before():
        return await live.deliver(pinned, "00000.ts", request(), lambda: None)

    assert run(before()).body == b"prepared-bytes-for-00000.ts"

    # The store disappears between the index and the segment, exactly the case the
    # audit reproduced against the deployed build.
    for path in directory.iterdir():
        path.unlink()
    directory.rmdir()

    async def after():
        with pytest.raises(DebridLibraryError) as caught:
            await live.deliver(pinned, "00000.ts", request(), lambda: None)
        return caught.value

    error = run(after())
    assert error.status == 404
    assert "prepared" in str(error)


def test_a_repared_title_is_refused_rather_than_mixed(tmp_path):
    directory = store(tmp_path)
    live = streams(tmp_path)
    pinned = entry(prepared_pinned=True, prepared_generation=_playlist_digest(PLAYLIST.encode()))

    # Same key, new timeline: this is what a re-prepare looks like from a live pass.
    changed = PLAYLIST.replace("00001.ts", "00009.ts")
    (directory / "stream.m3u8").write_bytes(changed.encode("ascii"))
    (directory / "00009.ts").write_bytes(b"new-generation")

    async def case():
        with pytest.raises(DebridLibraryError) as caught:
            await live.deliver(pinned, "00000.ts", request(), lambda: None)
        return caught.value

    error = run(case())
    assert error.status == 409
    assert "prepared again" in str(error)


def test_an_unprepared_title_still_uses_its_provider_timeline(tmp_path):
    live = streams(tmp_path)
    live.prepared = PreparedStore(tmp_path)
    unprepared = entry(prepared_pinned=False)

    async def case():
        return await live.deliver(unprepared, "index.m3u8", request(), lambda: None)

    response = run(case())
    assert response.status_code == 200
    assert b"provider0.ts" in response.body


def test_prepare_pins_the_generation_it_opened(tmp_path):
    store(tmp_path)
    live = streams(tmp_path)

    class Cloud:
        identity = "a" * 64
        media_version = "b" * 64
        duration = 8.0
        title = "Episode.mkv"
        account = "c" * 64
        direct = URL
        playlist = "https://abc.stream.real-debrid.com/transcode/index.m3u8"
        playlist_alt = ""

    async def read(url, **kwargs):
        return (
            b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
            b"#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            b"#EXTINF:4.000,\ns0.ts\n#EXTINF:4.000,\ns1.ts\n#EXT-X-ENDLIST\n",
            200,
            {},
        )

    live._read = read

    async def case():
        return await live.prepare(Cloud(), wait_first=False)

    media = run(case())
    assert media.prepared_pinned is True
    assert media.prepared_generation == _playlist_digest(PLAYLIST.encode())
