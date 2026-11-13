import asyncio

from starlette.requests import Request

from omakase.plus.debrid_stream import DebridMedia, DebridStreams
from omakase.plus.debrid_subs import SubtitleJob, SubtitleTrack


def test_provider_index_exposes_ready_subtitles():
    job = SubtitleJob("version")
    job.tracks = [
        SubtitleTrack(0, "en", "English", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"),
        SubtitleTrack(1, "da", "Danish", b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHej\n"),
    ]
    job.ready.set()
    job.done.set()
    streams = DebridStreams()
    streams.subtitles.jobs["version"] = job
    media = DebridMedia(
        b"#EXTM3U\n#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n", (), subtitle_key="version"
    )
    request = Request({"type": "http", "method": "GET", "headers": []})
    response = asyncio.run(streams.deliver(media, "index.m3u8", request, lambda: None))
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" in response.body
    assert b'NAME="English"' in response.body
    assert b'LANGUAGE="da"' in response.body
    assert b"stream.m3u8" in response.body


def test_provider_index_stays_media_without_subtitle_data():
    job = SubtitleJob("version")
    job.tracks = [SubtitleTrack(0, "en", "English")]
    job.ready.set()
    job.done.set()
    streams = DebridStreams()
    streams.subtitles.jobs["version"] = job
    media = DebridMedia(
        b"#EXTM3U\n#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n", (), subtitle_key="version"
    )
    request = Request({"type": "http", "method": "GET", "headers": []})
    response = asyncio.run(streams.deliver(media, "index.m3u8", request, lambda: None))
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" not in response.body
    assert response.body == media.playlist
