"""Pre-materialized HLS serving for a prepared title.

A prepared title is materialized once by a single ffmpeg HLS pass that writes
both the segments and the playlist, so the declared durations cannot disagree
with the content. These tests pin the read contract:

* ``index.m3u8`` is a master that declares the video variant and every
  prepared subtitle rendition,
* ``stream.m3u8`` is the prepared playlist byte for byte,
* segments are read from disk with byte ranges and the MPEG-TS content type,
* a title without a prepared directory keeps the existing on-demand provider
  path unchanged, and a directory that is absent, partial, or still being
  written is ignored.
"""

import asyncio

import httpx
from starlette.requests import Request

from omakase.plus.debrid_library import CloudSource
from omakase.plus.debrid_stream import CloudPass, DebridMedia, DebridStreams
from omakase.plus.debrid_subs import SubtitleJob, SubtitleTrack
from omakase.plus.prepared_hls import prepared_key, prepared_root

ORIGIN = "https://3.stream.real-debrid.com"
DIRECT = ORIGIN + "/d/ABC123DEF/Show.S01E01.mkv"
# The provider wraps its media playlist; this is the on-demand fallback body.
PROVIDER_PLAYLIST = (
    b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
    b"#EXTINF:5,\n00000.ts\n#EXTINF:5,\n00001.ts\n#EXT-X-ENDLIST\n"
)
# Written by one ffmpeg pass with real keyframe gaps, so the durations vary.
PREPARED_PLAYLIST = (
    b"#EXTM3U\n"
    b"#EXT-X-VERSION:3\n"
    b"#EXT-X-TARGETDURATION:7\n"
    b"#EXT-X-MEDIA-SEQUENCE:0\n"
    b"#EXT-X-PLAYLIST-TYPE:VOD\n"
    b"#EXTINF:6.256000,\n"
    b"s000.ts\n"
    b"#EXTINF:6.632000,\n"
    b"s001.ts\n"
    b"#EXT-X-ENDLIST\n"
)
WEBVTT = b"WEBVTT\n\n00:00:00.500 --> 00:00:02.000\nHello\n"


class Stream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def _request(method="GET", headers=()):
    return Request({"type": "http", "method": method, "headers": list(headers)})


def _prepared(root, *, sidecars=(0, 1), playlist=PREPARED_PLAYLIST):
    directory = root / prepared_key(DIRECT)
    directory.mkdir(parents=True)
    (directory / "stream.m3u8").write_bytes(playlist)
    for name in _playlist_segments(playlist):
        (directory / name).write_bytes(b"\x47" + name.encode() + b"-payload")
    for number in sidecars:
        (directory / f"subtitles-{number}.vtt").write_bytes(
            WEBVTT.replace(b"Hello", f"Track {number}".encode())
        )
    return directory


def _playlist_segments(playlist):
    return tuple(
        line
        for line in playlist.decode("ascii").splitlines()
        if line and not line.startswith("#")
    )


def _media(**kwargs):
    return DebridMedia(
        PROVIDER_PLAYLIST,
        (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
        prepared_key=prepared_key(DIRECT),
        **kwargs,
    )


def test_prepared_title_serves_a_master_with_its_subtitle_renditions(tmp_path):
    _prepared(tmp_path)
    streams = DebridStreams(prepared_root=tmp_path)

    index = asyncio.run(streams.deliver(_media(), "index.m3u8", _request(), lambda: None))

    body = index.body.decode()
    assert index.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "#EXT-X-MEDIA:TYPE=SUBTITLES" in body
    assert 'NAME="Subtitles 1"' in body and 'NAME="Subtitles 2"' in body
    assert 'STREAM-INF:BANDWIDTH=40000000,SUBTITLES="subs"' in body
    assert body.splitlines()[-1] == "stream.m3u8"


def test_prepared_playlist_is_served_byte_for_byte(tmp_path):
    _prepared(tmp_path)
    streams = DebridStreams(prepared_root=tmp_path)

    response = asyncio.run(streams.deliver(_media(), "stream.m3u8", _request(), lambda: None))

    assert response.body == PREPARED_PLAYLIST
    assert response.headers["content-type"].startswith("application/vnd.apple.mpegurl")


def test_prepared_subtitle_rendition_and_sidecar_are_served(tmp_path):
    directory = _prepared(tmp_path)
    streams = DebridStreams(prepared_root=tmp_path)

    rendition = asyncio.run(
        streams.deliver(_media(), "subtitles1.m3u8", _request(), lambda: None)
    )
    # The subtitle playlist spans the prepared timeline, not the prediction.
    assert b"#EXTINF:12.888," in rendition.body
    assert b"subtitles1.vtt" in rendition.body

    vtt = asyncio.run(streams.deliver(_media(), "subtitles1.vtt", _request(), lambda: None))
    assert vtt.body == (directory / "subtitles-1.vtt").read_bytes()
    assert vtt.headers["content-type"].startswith("text/vtt")


def test_prepared_segment_is_served_from_disk(tmp_path):
    directory = _prepared(tmp_path)
    streams = DebridStreams(prepared_root=tmp_path)
    payload = (directory / "s001.ts").read_bytes()

    response = asyncio.run(streams.deliver(_media(), "s001.ts", _request(), lambda: None))

    assert response.body == payload
    assert response.headers["content-type"] == "video/mp2t"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(payload))


def test_prepared_segment_honours_a_range_and_head(tmp_path):
    directory = _prepared(tmp_path)
    payload = b"0123456789abcdef"
    (directory / "s000.ts").write_bytes(payload)
    streams = DebridStreams(prepared_root=tmp_path)

    partial = asyncio.run(
        streams.deliver(
            _media(),
            "s000.ts",
            _request(headers=[(b"range", b"bytes=4-7")]),
            lambda: None,
        )
    )
    assert partial.status_code == 206
    assert partial.body == b"4567"
    assert partial.headers["content-range"] == "bytes 4-7/16"
    assert partial.headers["content-length"] == "4"

    suffix = asyncio.run(
        streams.deliver(
            _media(),
            "s000.ts",
            _request(headers=[(b"range", b"bytes=-4")]),
            lambda: None,
        )
    )
    assert suffix.status_code == 206 and suffix.body == b"cdef"

    unsatisfiable = asyncio.run(
        streams.deliver(
            _media(),
            "s000.ts",
            _request(headers=[(b"range", b"bytes=99-")]),
            lambda: None,
        )
    )
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == "bytes */16"

    head = asyncio.run(streams.deliver(_media(), "s000.ts", _request("HEAD"), lambda: None))
    assert head.body == b""
    assert head.headers["content-length"] == "16"


def test_prepared_assets_never_read_the_provider(tmp_path):
    """Every prepared asset is answered from disk, never from Real-Debrid."""
    _prepared(tmp_path)

    def no_call(request):
        raise AssertionError(request.url.path)

    streams = DebridStreams(httpx.MockTransport(no_call), prepared_root=tmp_path)
    for asset in (
        "index.m3u8",
        "stream.m3u8",
        "s000.ts",
        "s001.ts",
        "subtitles0.m3u8",
        "subtitles1.vtt",
    ):
        response = asyncio.run(streams.deliver(_media(), asset, _request(), lambda: None))
        assert response is not None, asset


def test_serving_a_prepared_title_logs_its_store_key(tmp_path, caplog):
    _prepared(tmp_path)
    streams = DebridStreams(prepared_root=tmp_path)

    with caplog.at_level("INFO", logger="omakase.plus.debrid_stream"):
        asyncio.run(streams.deliver(_media(), "stream.m3u8", _request(), lambda: None))

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("prepared timeline served segments=2 ")
        and prepared_key(DIRECT) in message
        for message in messages
    )


def test_cloud_pass_finds_the_prepared_directory_from_the_source_path(tmp_path):
    """Real-Debrid rotates host and token on every play; the path is the key."""
    _prepared(tmp_path)
    rotated = "https://9.stream.real-debrid.com/d/ABC123DEF/Show.S01E01.mkv?token=rotated"
    source = CloudSource(
        "Test", "identity", "version", 30, "account", ORIGIN + "/a/primary.m3u8", "", rotated
    )
    entry = CloudPass(
        source,
        1,
        "binding",
        "digest",
        1000,
        PROVIDER_PLAYLIST,
        (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
    )
    streams = DebridStreams(prepared_root=tmp_path)

    response = asyncio.run(streams.deliver(entry, "stream.m3u8", _request(), lambda: None))

    assert response.body == PREPARED_PLAYLIST


def test_absent_prepared_directory_keeps_the_on_demand_provider_path(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, stream=Stream(b"\x47provider"))

    streams = DebridStreams(httpx.MockTransport(handler), prepared_root=tmp_path)
    media = DebridMedia(PROVIDER_PLAYLIST, (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"))

    index = asyncio.run(streams.deliver(media, "index.m3u8", _request(), lambda: None))
    assert index.body == PROVIDER_PLAYLIST

    segment = asyncio.run(streams.deliver(media, "00000.ts", _request(), lambda: None))
    assert segment.body == b"\x47provider"
    assert calls == ["/00000.ts"]


def test_playlist_without_an_endlist_is_treated_as_unfinished(tmp_path):
    unfinished = PREPARED_PLAYLIST.replace(b"#EXT-X-ENDLIST\n", b"")
    _prepared(tmp_path, playlist=unfinished)
    streams = DebridStreams(prepared_root=tmp_path)

    response = asyncio.run(streams.deliver(_media(), "stream.m3u8", _request(), lambda: None))

    assert response.body == PROVIDER_PLAYLIST


def test_playlist_naming_a_missing_segment_is_not_served(tmp_path):
    directory = _prepared(tmp_path)
    (directory / "s001.ts").unlink()
    streams = DebridStreams(prepared_root=tmp_path)

    response = asyncio.run(streams.deliver(_media(), "stream.m3u8", _request(), lambda: None))

    assert response.body == PROVIDER_PLAYLIST


def test_prepared_key_follows_the_stable_provider_path():
    rotated = "https://9.stream.real-debrid.com/d/ABC123DEF/Show.S01E01.mkv?token=rotated"
    assert prepared_key(rotated) == prepared_key(DIRECT)
    assert prepared_key("") == ""


def test_prepared_root_environment_variable_is_honoured(tmp_path, monkeypatch):
    _prepared(tmp_path)
    monkeypatch.setenv("OMAKASE_PREPARED_ROOT", str(tmp_path))
    assert prepared_root() == tmp_path
    streams = DebridStreams()

    response = asyncio.run(streams.deliver(_media(), "stream.m3u8", _request(), lambda: None))

    assert response.body == PREPARED_PLAYLIST


def test_prepare_records_the_prepared_key_for_the_source_path():
    def handler(request):
        if request.url.path.endswith(".m3u8"):
            return httpx.Response(200, stream=Stream(PROVIDER_PLAYLIST))
        return httpx.Response(200, stream=Stream(b"\x47segment"))

    async def run():
        streams = DebridStreams(httpx.MockTransport(handler))
        source = CloudSource(
            "Test", "identity", "version", 10, "account", ORIGIN + "/a/index.m3u8", "", DIRECT
        )
        media = await streams.prepare(source, wait_first=False)
        return media.prepared_key

    assert asyncio.run(run()) == prepared_key(DIRECT)


def test_whole_title_prepared_stream_reuses_ready_source_subtitles(tmp_path):
    """A preparation that spans the whole title starts where the cues start."""
    _prepared(tmp_path, sidecars=())
    job = SubtitleJob("version")
    job.tracks = [SubtitleTrack(0, "en", "English", WEBVTT)]
    job.ready.set()
    job.done.set()
    streams = DebridStreams(prepared_root=tmp_path)
    streams.subtitles.jobs["version"] = job

    master = asyncio.run(
        streams.deliver(_media(subtitle_key="version"), "index.m3u8", _request(), lambda: None)
    )
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" in master.body
    assert b'NAME="English"' in master.body and b'LANGUAGE="en"' in master.body

    vtt = asyncio.run(
        streams.deliver(_media(subtitle_key="version"), "subtitles0.vtt", _request(), lambda: None)
    )
    assert vtt.body == WEBVTT


def test_prepared_index_does_not_wait_for_an_uncached_subtitle_job(tmp_path):
    """A cold extraction job must not hold the player's opening request open."""
    _prepared(tmp_path, sidecars=())
    job = SubtitleJob("version")
    job.tracks = [SubtitleTrack(0, "en", "English")]
    streams = DebridStreams(prepared_root=tmp_path)
    streams.subtitles.jobs["version"] = job

    async def run():
        started = asyncio.get_running_loop().time()
        response = await streams.deliver(
            _media(subtitle_key="version"), "index.m3u8", _request(), lambda: None
        )
        return response, asyncio.get_running_loop().time() - started

    response, elapsed = asyncio.run(run())
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" not in response.body
    assert elapsed < 2


def test_prepared_window_does_not_declare_source_subtitles(tmp_path):
    """Cues timed against the source would run away from a prepared window."""
    _prepared(tmp_path, sidecars=())
    windowed = b"#EXTM3U\n#EXTINF:600,\n00000.ts\n#EXT-X-ENDLIST\n"
    job = SubtitleJob("version")
    job.tracks = [SubtitleTrack(0, "en", "English", WEBVTT)]
    job.ready.set()
    job.done.set()
    streams = DebridStreams(prepared_root=tmp_path)
    streams.subtitles.jobs["version"] = job
    media = DebridMedia(
        windowed,
        (ORIGIN + "/00000.ts",),
        subtitle_key="version",
        prepared_key=prepared_key(DIRECT),
    )

    master = asyncio.run(streams.deliver(media, "index.m3u8", _request(), lambda: None))

    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" not in master.body
    assert master.body.splitlines()[-1] == b"stream.m3u8"
