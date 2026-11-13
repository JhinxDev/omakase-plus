"""Cloud inventory and exact-file playback without a Plex dependency."""

import asyncio

import httpx
import pytest
from starlette.requests import Request

from omakase.plus.debrid_library import CloudSource, DebridLibrary, DebridLibraryError
from omakase.plus.debrid_stream import CloudPass, DebridMedia, DebridStreams, parse_playlist

ORIGIN = "https://3.stream.real-debrid.com"
MANIFEST = b"""#EXTM3U
#EXT-X-TARGETDURATION:5
#EXT-X-ALLOW-CACHE:NO
#EXT-X-MEDIA-SEQUENCE:0
#EXTINF:5,
video0.ts?provider=secret
#EXTINF:4,
video1.ts?provider=secret
#EXT-X-ENDLIST
"""


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


class Provider:
    def __init__(self):
        self.calls = []
        self.new_item = False
        self.wrong_file = False
        self.registered = False
        self.drift_once = False
        self.drift_always = False
        self.torrent_calls = 0
        self.media_info_ids = []

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        if request.url.host == "3.stream.real-debrid.com":
            assert "authorization" not in request.headers and "cookie" not in request.headers
            content = MANIFEST if request.url.path.endswith(".m3u8") else b"synthetic segment"
            return httpx.Response(200, stream=Stream(content))
        assert request.url.host == "api.real-debrid.com"
        assert request.headers["authorization"] in {"Bearer owner-token", "Bearer different-token"}
        path = request.url.path.removeprefix("/rest/1.0/")
        if path in {"torrents", "downloads"}:
            rows = (
                [{"id": "torrent1", "filename": "A collection", "status": "downloaded"}]
                if path == "torrents"
                else [
                    {"id": "download1", "filename": "Existing.mp4", "streamable": 1, "filesize": 60}
                ]
            )
            if path == "torrents":
                self.torrent_calls += 1
                if self.drift_always or (self.drift_once and self.torrent_calls == 1):
                    return httpx.Response(200, json=rows, headers={"X-Total-Count": "2"})
            if request.headers["authorization"] == "Bearer different-token":
                rows = []
            elif self.new_item:
                rows.append({"id": "addition", "filename": "New entry", "status": "downloading"})
            if path == "downloads" and self.registered and not self.wrong_file:
                rows = [
                    *rows,
                    {
                        "id": "download2",
                        "filename": "Test.mkv",
                        "filesize": 60,
                        "streamable": 1,
                        "link": "https://real-debrid.com/d/provider-secret",
                    },
                ]
            return httpx.Response(200, json=rows, headers={"X-Total-Count": str(len(rows))})
        if path == "torrents/info/torrent1":
            return httpx.Response(
                200,
                json={
                    "id": "torrent1",
                    "status": "downloaded",
                    "files": [
                        {"id": 1, "path": "/Test.mkv", "bytes": 60, "selected": 1},
                        {"id": 2, "path": "/Other.mkv", "bytes": 50, "selected": 0},
                        {"id": 3, "path": "/Readme.txt", "bytes": 10, "selected": 0},
                    ],
                    "links": ["https://real-debrid.com/d/provider-secret"],
                },
            )
        if path == "unrestrict/link":
            assert request.method == "POST" and b"provider-secret" in request.content
            if not self.wrong_file:
                self.registered = True
            return httpx.Response(
                200,
                json={
                    "id": "generated1",
                    "filename": "Wrong.mkv" if self.wrong_file else "Test.mkv",
                    "filesize": 60,
                    "streamable": 1,
                },
            )
        if path.startswith("streaming/mediaInfos/"):
            self.media_info_ids.append(path.removeprefix("streaming/mediaInfos/"))
            return httpx.Response(200, json={"duration": 9, "size": 60})
        if path.startswith("streaming/transcode/"):
            return httpx.Response(
                200, json={"apple": {"high": ORIGIN + "/video.m3u8?credential=hidden"}}
            )
        raise AssertionError(request.url.path)


def test_catalog_refreshes_and_never_reuses_another_accounts_inventory():
    provider = Provider()
    now = 100

    async def run():
        nonlocal now
        library = DebridLibrary(httpx.MockTransport(provider), clock=lambda: now)
        assert {row.id for row in await library.entries("owner-token")} == {
            "t-torrent1",
            "d-download1",
        }
        assert not await library.entries("different-token")
        assert not [call for call in provider.calls if call[0] == "POST"]
        provider.new_item = True
        now += 26
        assert len(await library.entries("owner-token", query="NEW")) == 2

    asyncio.run(run())


def test_selected_torrent_file_requires_the_exact_link_and_allows_cloud_playback():
    provider = Provider()

    async def run():
        transport = httpx.MockTransport(provider)
        library = DebridLibrary(transport)
        streams = DebridStreams(transport)
        files = await library.files("owner-token", "t-torrent1")
        assert {row.title: row.playable for row in files} == {
            "Other.mkv": False,
            "Readme.txt": False,
            "Test.mkv": True,
        }
        with pytest.raises(DebridLibraryError):
            await library.source("owner-token", "t-torrent1-2")
        provider.wrong_file = True
        with pytest.raises(DebridLibraryError, match="different file"):
            await library.source("owner-token", "t-torrent1-1")
        provider.wrong_file = False
        source = await library.source("owner-token", "t-torrent1-1")
        assert provider.registered
        assert provider.media_info_ids == ["download2"]
        assert len(source.identity) == 64
        key, cookie = await streams.issue(source, 1, "browser", 60)
        entry = streams.for_cookie(key, cookie)
        assert entry and streams.for_client(key, "another-browser") is None
        assert b"credential" not in entry.playlist and b"secret" not in entry.playlist
        assert b"00000.ts" in entry.playlist
        request = Request({"type": "http", "method": "GET", "headers": []})
        response = await streams.deliver(entry, "00001.ts", request, lambda: None)
        assert response.body == b"synthetic segment"

    asyncio.run(run())


@pytest.mark.parametrize(
    "playlist",
    [
        MANIFEST.replace(b"video0.ts", b"https://elsewhere.invalid/file.ts"),
        MANIFEST.replace(b"video0.ts", b"https://4.stream.real-debrid.com/file.ts"),
        MANIFEST.replace(b"#EXTINF:5,", b'#EXT-X-KEY:METHOD=AES-128,URI="file"'),
        MANIFEST.replace(b"#EXT-X-ENDLIST", b""),
        MANIFEST.replace(b"#EXTINF:5,", b"#EXTINF:11,"),
        MANIFEST.replace(
            b"#EXTINF:5,\nvideo0.ts?provider=secret",
            b"#EXTINF:0,\nvideo0.ts?provider=secret",
        ),
    ],
)
def test_hls_never_uses_untrusted_or_unsupported_playlist_references(playlist):
    with pytest.raises(DebridLibraryError):
        parse_playlist(playlist, ORIGIN + "/index.m3u8", 9)


def test_zero_duration_trailing_filler_segment_is_dropped():
    playlist = MANIFEST.replace(
        b"#EXT-X-ENDLIST", b"#EXTINF:0,\nvideo2.ts?provider=secret\n#EXT-X-ENDLIST"
    )
    manifest, segments = parse_playlist(playlist, ORIGIN + "/index.m3u8", 9)
    assert segments == (
        ORIGIN + "/video0.ts?provider=secret",
        ORIGIN + "/video1.ts?provider=secret",
    )
    assert b"00001.ts" in manifest and b"00002.ts" not in manifest
    assert b"#EXTINF:0," not in manifest


def test_inventory_restarts_once_when_the_provider_snapshot_changes():
    provider = Provider()
    provider.drift_once = True

    async def run():
        library = DebridLibrary(httpx.MockTransport(provider))
        torrents, downloads = await library.inventory("owner-token")
        assert set(torrents) == {"torrent1"}
        assert set(downloads) == {"download1"}
        assert provider.torrent_calls == 2

    asyncio.run(run())


def test_transient_provider_error_is_retried():
    provider = Provider()
    failures = {"count": 1}

    def flaky(request):
        if request.url.path.endswith("/torrents") and failures["count"]:
            failures["count"] -= 1
            return httpx.Response(503)
        return provider(request)

    async def run():
        library = DebridLibrary(httpx.MockTransport(flaky))
        torrents, _ = await library.inventory("owner-token")
        assert set(torrents) == {"torrent1"}

    asyncio.run(run())


def test_playlist_read_retries_one_transient_stream_error():
    calls = {"count": 0}

    def flaky(request):
        if request.url.host == "3.stream.real-debrid.com":
            calls["count"] += 1
            if calls["count"] == 1:
                return httpx.Response(503)
            return httpx.Response(200, stream=Stream(MANIFEST))
        raise AssertionError(request.url.path)

    async def run():
        streams = DebridStreams(httpx.MockTransport(flaky))
        source = CloudSource("Test", "identity", "version", 9, "account", ORIGIN + "/video.m3u8")
        media = await streams.prepare(source)
        assert len(media.segments) == 2
        assert calls["count"] == 2

    asyncio.run(run())


def test_first_segment_gets_a_cold_start_deadline():
    captured = []
    source = CloudSource("Test", "identity", "version", 10, "account", ORIGIN + "/video.m3u8")
    entry = CloudPass(
        source,
        1,
        "binding",
        "digest",
        1000,
        b"#EXTM3U\n#EXT-X-ENDLIST\n",
        (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
    )

    async def fake_read(url, **kwargs):
        captured.append((url, kwargs))
        return b"\x47" + b"x" * 187, 200, httpx.Headers({"content-length": "188"})

    async def run():
        streams = DebridStreams()
        streams._read = fake_read
        request = Request({"type": "http", "method": "GET", "headers": []})
        first = await streams.deliver(entry, "00000.ts", request, lambda: None)
        assert first.body.startswith(b"\x47")
        second = await streams.deliver(entry, "00001.ts", request, lambda: None)
        assert second.body.startswith(b"\x47")
        assert captured[0][0].endswith("00000.ts")
        assert captured[0][1]["timeout"] == 15 and captured[0][1]["attempts"] == 1
        # A later segment gets one long attempt inside a wider outer budget.
        # The old 35 s wrapper around two 20 s attempts could never let the
        # retry finish, so a slow but valid provider segment always surfaced
        # as a 502 and was thrown away instead of being cached.
        assert captured[1][1]["timeout"] == 60 and captured[1][1]["attempts"] == 1

    asyncio.run(run())


def test_first_segment_falls_back_to_the_next_segment_when_it_is_empty():
    calls = {"segment": 0}

    def handler(request):
        path = request.url.path
        if path.endswith("00000.ts"):
            calls["segment"] += 1
            return httpx.Response(200, stream=Stream(b""))
        if path.endswith("00001.ts"):
            calls["segment"] += 1
            return httpx.Response(200, stream=Stream(b"\x47next"))
        raise AssertionError(path)

    async def run():
        streams = DebridStreams(httpx.MockTransport(handler))
        media = DebridMedia(
            b"#EXTM3U\n",
            (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
        )
        request = Request({"type": "http", "method": "GET", "headers": []})
        response = await streams.deliver(media, "00000.ts", request, lambda: None)
        assert response.body == b"\x47next"
        assert calls["segment"] == 2

    asyncio.run(run())


def test_first_segment_retries_once_after_a_cold_provider_timeout():
    calls = {"count": 0}
    playlist = (
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
        b"#EXTINF:5,\n00000.ts\n#EXTINF:5,\n00001.ts\n#EXT-X-ENDLIST\n"
    )

    def handler(request):
        if request.url.path == "/a/index.m3u8":
            return httpx.Response(200, stream=Stream(playlist))
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(200, stream=Stream(b""))
        return httpx.Response(200, stream=Stream(b"\x47warm"))

    async def run():
        streams = DebridStreams(httpx.MockTransport(handler))
        media = DebridMedia(
            playlist,
            (ORIGIN + "/a/00000.ts", ORIGIN + "/a/00001.ts"),
        )
        request = Request({"type": "http", "method": "GET", "headers": []})
        response = await streams.deliver(media, "00000.ts", request, lambda: None)
        assert response.body == b"\x47warm"
        assert calls["count"] == 2

    asyncio.run(run())


def test_recent_inventory_survives_a_transient_snapshot_failure():
    provider = Provider()
    now = {"value": 100}
    transport = httpx.MockTransport(provider)

    async def run():
        library = DebridLibrary(transport, clock=lambda: now["value"])
        first = await library.inventory("owner-token")
        assert set(first[0]) == {"torrent1"}
        provider.drift_always = True
        provider.torrent_calls = 0
        now["value"] += 26
        fallback = await library.inventory("owner-token")
        assert fallback == first

    asyncio.run(run())


def test_prepare_races_variants_and_caches_the_first_segment():
    primary = ORIGIN + "/a/primary.m3u8"
    alt = ORIGIN + "/b/original.m3u8"
    playlist = (
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
        b"#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n"
    )

    def handler(request):
        path = request.url.path
        if path.endswith(".m3u8"):
            return httpx.Response(200, stream=Stream(playlist))
        if path.startswith("/a/"):
            return httpx.Response(200, stream=Stream(b""))
        if path.startswith("/b/"):
            return httpx.Response(200, stream=Stream(b"\x47cached"))
        raise AssertionError(path)

    async def run():
        streams = DebridStreams(httpx.MockTransport(handler))
        source = CloudSource("Test", "identity", "version", 5, "account", primary, alt)
        media = await streams.prepare(source)
        assert media.first_segment == b"\x47cached"
        assert media.segments[0] == ORIGIN + "/b/00000.ts"

    asyncio.run(run())


def test_cached_first_segment_is_served_without_upstream_reads():
    def no_call(request):
        raise AssertionError("cached first segment should not read upstream")

    async def run():
        streams = DebridStreams(httpx.MockTransport(no_call))
        media = DebridMedia(
            b"#EXTM3U\n",
            (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
            b"\x47cached",
        )
        request = Request({"type": "http", "method": "GET", "headers": []})
        response = await streams.deliver(media, "00000.ts", request, lambda: None)
        assert response.body == b"\x47cached"
        ranged = Request({"type": "http", "method": "GET", "headers": [(b"range", b"bytes=1-2")]})
        partial = await streams.deliver(media, "00000.ts", ranged, lambda: None)
        assert partial.status_code == 206 and partial.body == b"ca"
        assert partial.headers["content-range"] == "bytes 1-2/7"

    asyncio.run(run())


def test_prepare_can_return_before_the_first_segment_is_ready():
    calls = {"segment": 0, "paths": []}
    playlist = (
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
        b"#EXTINF:5,\n00000.ts\n#EXTINF:5,\n00001.ts\n#EXT-X-ENDLIST\n"
    )

    def handler(request):
        if request.url.path.endswith(".m3u8"):
            return httpx.Response(200, stream=Stream(playlist))
        calls["segment"] += 1
        calls["paths"].append(request.url.path)
        return httpx.Response(200, stream=Stream(b"\x47warm"))

    async def run():
        streams = DebridStreams(httpx.MockTransport(handler))
        source = CloudSource("Test", "identity", "version", 10, "account", ORIGIN + "/video.m3u8")
        media = await streams.prepare(source, wait_first=False)
        assert media.first_segment == b""
        request = Request({"type": "http", "method": "GET", "headers": []})
        response = await streams.deliver(media, "00000.ts", request, lambda: None)
        assert response.body == b"\x47warm"
        # The warmed segment is reused, not fetched a second time. Any other
        # upstream read here is the forward readahead for the next segment.
        assert [path for path in calls["paths"] if path.endswith("00000.ts")] == ["/00000.ts"]

    asyncio.run(run())


def test_embedded_subtitles_are_served_as_hls_renditions():
    from omakase.plus.debrid_subs import SubtitleJob, SubtitleTrack

    playlist = (
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
        b"#EXTINF:5,\n00000.ts\n#EXTINF:5,\n00001.ts\n#EXT-X-ENDLIST\n"
    )
    english = b"WEBVTT\n\n00:01.000 --> 00:02.000\nHello\n"
    french = b"WEBVTT\n\n00:01.000 --> 00:02.000\nBonjour\n"
    job = SubtitleJob("version")
    job.tracks = [
        SubtitleTrack(2, "eng", "English", english),
        SubtitleTrack(3, "fre", "French", french),
    ]
    job.ready.set()
    job.done.set()

    def no_call(request):
        raise AssertionError("subtitles should not read upstream")

    async def run():
        streams = DebridStreams(httpx.MockTransport(no_call))
        streams.subtitles.jobs["version"] = job
        media = DebridMedia(
            playlist,
            (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
            subtitle_key="version",
        )
        request = Request({"type": "http", "method": "GET", "headers": []})
        master = await streams.deliver(media, "index.m3u8", request, lambda: None)
        body = master.body.decode()
        assert "#EXT-X-MEDIA:TYPE=SUBTITLES" in body
        assert 'NAME="English"' in body and 'NAME="French"' in body
        assert 'STREAM-INF:BANDWIDTH=8000000,SUBTITLES="subs"' in body
        assert body.rstrip().endswith("stream.m3u8")
        media_playlist = await streams.deliver(media, "stream.m3u8", request, lambda: None)
        assert media_playlist.body == playlist
        subtitle_playlist = await streams.deliver(media, "subtitles0.m3u8", request, lambda: None)
        assert b"subtitles0.vtt" in subtitle_playlist.body
        vtt = await streams.deliver(media, "subtitles0.vtt", request, lambda: None)
        assert vtt.body == english
        assert vtt.headers["content-type"].startswith("text/vtt")
        head = await streams.deliver(
            media,
            "subtitles0.vtt",
            Request({"type": "http", "method": "HEAD", "headers": []}),
            lambda: None,
        )
        assert head.body == b""

    asyncio.run(run())
