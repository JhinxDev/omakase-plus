import asyncio
from pathlib import Path

import httpx
from starlette.requests import Request

from omakase.plus.debrid_seek import SeekIndex, SeekPlan, SeekStreams, _audio_args, _segments
from omakase.plus.debrid_stream import DebridMedia, DebridStreams

ORIGIN = "https://example.stream.real-debrid.com"


def test_segments_follow_keyframes_and_cover_duration():
    keyframes = (0.0, 1.0, 2.0, 7.0, 8.0, 13.5, 14.0, 20.0, 26.0)
    segments = _segments(keyframes, 31.0)
    assert segments[0][0] == 0.0
    assert segments[-1][1] == 31.0
    assert all(end > start for start, end in segments)
    assert all(later[0] == earlier[1] for earlier, later in zip(segments, segments[1:]))
    assert all(6.0 <= end - start <= 7.5 for start, end in segments[:-1])


def test_seekable_plan_serves_accurate_playlist_and_segment(tmp_path):
    index = SeekIndex(
        url="https://chi8-4.download.real-debrid.com/x/y.mkv",
        duration=30.0,
        video_codec="h264",
        audio_codec="aac",
        keyframes=(0.0, 6.0, 12.0, 18.0, 24.0),
        segments=((0.0, 6.0), (6.0, 12.0), (12.0, 18.0), (18.0, 24.0), (24.0, 30.0)),
    )
    plan = SeekPlan(
        key="version",
        url=index.url,
        duration=30.0,
        root=tmp_path,
    )
    plan.index = index
    plan.ready.set()

    async def run():
        streams = DebridStreams(seek_root=tmp_path)
        streams.seek.plans["version"] = plan

        async def segment(current, number):
            return b"\x47" + bytes([number]) * 204

        streams.seek.segment = segment
        media = DebridMedia(b"", (), seek_key="version")
        request = Request({"type": "http", "method": "GET", "headers": []})
        direct = await streams.deliver(media, "index.m3u8", request, lambda: None)
        assert b"stream.m3u8" not in direct.body
        assert direct.body == index.playlist
        stream = await streams.deliver(media, "stream.m3u8", request, lambda: None)
        body = stream.body.decode()
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
        assert "#EXTINF:6.000," in body
        assert plan.mode == "seek"
        third = await streams.deliver(media, "00003.ts", request, lambda: None)
        assert third.status_code == 200
        assert third.body == b"\x47" + bytes([3]) * 204
        head = await streams.deliver(
            media,
            "00003.ts",
            Request({"type": "http", "method": "HEAD", "headers": []}),
            lambda: None,
        )
        assert head.body == b""

    asyncio.run(run())


def test_failed_seek_index_falls_back_to_the_provider_playlist(tmp_path):
    playlist = (
        b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n#EXT-X-MEDIA-SEQUENCE:0\n"
        b"#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n"
    )

    def no_call(request):
        raise AssertionError("fallback should not read upstream")

    async def run():
        streams = DebridStreams(httpx.MockTransport(no_call), seek_root=tmp_path)
        plan = SeekPlan(
            key="version",
            url="https://chi8-4.download.real-debrid.com/x/y.mkv",
            duration=5.0,
            root=tmp_path,
        )
        plan.ready.set()
        streams.seek.plans["version"] = plan
        media = DebridMedia(playlist, (ORIGIN + "/00000.ts",), seek_key="version")
        request = Request({"type": "http", "method": "GET", "headers": []})
        response = await streams.deliver(media, "index.m3u8", request, lambda: None)
        assert response.body == playlist
        stream = await streams.deliver(media, "stream.m3u8", request, lambda: None)
        assert stream.body == playlist

    asyncio.run(run())


def test_hevc_prefers_realdebrid_transcode_over_seek(tmp_path):
    index = SeekIndex(
        url="https://chi9-4.download.real-debrid.com/x/y.mkv",
        duration=12.0,
        video_codec="hevc",
        audio_codec="aac",
        keyframes=(0.0, 6.0),
        segments=((0.0, 6.0), (6.0, 12.0)),
        container="fmp4",
    )
    plan = SeekPlan(key="hevc", url=index.url, duration=12.0, root=tmp_path)
    plan.index = index
    plan.ready.set()
    provider = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\n00000.ts\n#EXT-X-ENDLIST\n"

    async def run():
        streams = DebridStreams(seek_root=tmp_path)
        streams.seek.plans["hevc"] = plan
        media = DebridMedia(
            provider, ("https://chi9-4.download.real-debrid.com/x/00000.ts",), seek_key="hevc"
        )
        request = Request({"type": "http", "method": "GET", "headers": []})
        index_reply = await streams.deliver(media, "index.m3u8", request, lambda: None)
        stream = await streams.deliver(media, "stream.m3u8", request, lambda: None)
        assert index_reply.status_code == 200
        assert stream.body == provider

    asyncio.run(run())


def test_hevc_seek_generates_exact_h264_segments(tmp_path, monkeypatch):
    import omakase.plus.debrid_seek as seek

    calls = []

    async def fake_ffmpeg(args):
        calls.append(args)
        Path(args[-1]).write_bytes(b"x" * 256)

    monkeypatch.setattr(seek, "_run_ffmpeg", fake_ffmpeg)
    index = SeekIndex(
        url="https://chi9-4.download.real-debrid.com/x/y.mkv",
        duration=12.0,
        video_codec="hevc",
        audio_codec="aac",
        keyframes=(),
        segments=((0.0, 6.0), (6.0, 12.0)),
        container="ts",
        transcode=True,
    )
    plan = SeekPlan(key="hevc", url=index.url, duration=12.0, root=tmp_path)
    plan.index = index
    plan.ready.set()
    streams = SeekStreams(root=tmp_path)
    asyncio.run(streams._generate(plan, 0))
    assert calls
    args = calls[0]
    assert "libx264" in args and "ultrafast" in args
    assert args[args.index("-output_ts_offset") + 1] == "0.000"
    assert args[args.index("-muxdelay") + 1] == "0"
    # The seek is nudged just past the boundary so it cannot round down to the
    # previous keyframe and replay the tail of the last segment.
    assert args[args.index("-ss") + 1] == "0.050"
    assert (tmp_path / "00000.ts").read_bytes() == b"x" * 256


def test_audio_policy_copies_ts_compatible_codecs_and_transcodes_the_rest():
    """AVPlayer needs audio that muxes into MPEG-TS unchanged.

    A transcoded track starts at the seek point while copied video starts at the
    keyframe, which measured about a second of drift per segment and made the
    Apple TV reject the item with CoreMedia -12971.
    """
    for codec in ("aac", "ac3", "eac3", "mp3"):
        assert _audio_args(codec) == ["-c:a", "copy"], codec
    for codec in ("truehd", "dts", "flac", "opus", "vorbis", ""):
        args = _audio_args(codec)
        assert args[:2] == ["-c:a", "aac"], codec
        assert args[args.index("-ac") + 1] == "2", codec
