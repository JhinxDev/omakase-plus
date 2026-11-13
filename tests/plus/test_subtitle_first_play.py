"""First-play subtitle readiness and segment readahead for the provider path.

The Apple TV fetches ``index.m3u8`` once when it opens a title. If that first
request returns a plain media playlist, AVPlayer never discovers the subtitle
renditions for the rest of the session, so the language menu stays empty. These
tests pin the two behaviours that make the first open subtitle-ready and keep
provider segments ahead of playback:

* a subtitle job whose WebVTT output is already cached holds the first index
  request long enough to return a master with a SUBTITLES group,
* a job that still has extraction work to do never holds the index open,
* segment readahead stays ahead of the current position so a resume or a seek
  finds generated segments instead of waiting on Real-Debrid.
"""

import asyncio

from starlette.requests import Request

from omakase.plus.debrid_stream import DebridMedia, DebridStreams
from omakase.plus.debrid_subs import SubtitleJob, SubtitleTrack


def _playlist(count):
    segments = tuple(f"{number:05d}.ts" for number in range(count))
    body = b"#EXTM3U\n#EXT-X-TARGETDURATION:5\n" + b"".join(
        b"#EXTINF:5,\n" + name.encode() + b"\n" for name in segments
    )
    return body + b"#EXT-X-ENDLIST\n", segments


def _request():
    return Request({"type": "http", "method": "GET", "headers": []})


def test_warm_subtitle_job_holds_first_index_until_tracks_are_ready():
    async def scenario():
        streams = DebridStreams()
        job = SubtitleJob("version", warm=True)
        job.tracks = [SubtitleTrack(0, "en", "English")]
        streams.subtitles.jobs["version"] = job

        async def produce():
            await asyncio.sleep(0.05)
            job.tracks[0].data = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
            job.ready.set()
            job.done.set()

        producer = asyncio.create_task(produce())
        media = DebridMedia(
            b"#EXTM3U\n#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n", (), subtitle_key="version"
        )
        response = await streams.deliver(media, "index.m3u8", _request(), lambda: None)
        await producer
        return response

    response = asyncio.run(scenario())
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" in response.body
    assert b'LANGUAGE="en"' in response.body


def test_uncached_subtitle_job_still_does_not_hold_index():
    async def scenario():
        streams = DebridStreams()
        job = SubtitleJob("version")
        job.tracks = [SubtitleTrack(0, "en", "English")]
        streams.subtitles.jobs["version"] = job
        media = DebridMedia(
            b"#EXTM3U\n#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n", (), subtitle_key="version"
        )
        started = asyncio.get_running_loop().time()
        response = await streams.deliver(media, "index.m3u8", _request(), lambda: None)
        return response, asyncio.get_running_loop().time() - started

    response, elapsed = asyncio.run(scenario())
    assert response.body.startswith(b"#EXTM3U")
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" not in response.body
    assert elapsed < 2


def test_readahead_keeps_a_forward_window_warm():
    async def scenario():
        streams = DebridStreams()
        fetched = []

        async def fake_fetch(url):
            fetched.append(url)
            return b"segment"

        streams._fetch_segment = fake_fetch
        _, segments = _playlist(40)
        media = DebridMedia(_playlist(40)[0], segments)
        streams._advance(media, 5)
        await asyncio.sleep(0.05)
        return fetched, streams.pump

    fetched, pump = asyncio.run(scenario())
    assert fetched, "readahead fetched nothing"
    assert fetched[0].endswith("00006.ts")
    assert fetched[-1].endswith("00013.ts")
    assert pump["next"] == 14


def test_warm_position_starts_at_the_saved_point():
    streams = DebridStreams()
    body, segments = _playlist(100)
    media = DebridMedia(body, segments)
    streams.warm_position(media, 106.4)
    assert streams.pump is not None
    assert streams.pump["next"] == 21
    assert streams.pump["target"] == 29


def test_warm_position_ignores_play_from_start():
    streams = DebridStreams()
    body, segments = _playlist(100)
    media = DebridMedia(body, segments)
    streams.warm_position(media, 0)
    assert streams.pump is None


def test_segment_cache_respects_its_entry_budget():
    streams = DebridStreams()
    for number in range(80):
        streams._cache_segment(f"segment-{number}", b"payload")
    assert len(streams.segment_cache) <= 64
    assert streams.cached_bytes == len(streams.segment_cache) * len(b"payload")


def test_stale_segment_cache_entry_is_retried_not_served():
    streams = DebridStreams()
    streams.segment_cache["segment"] = (-1.0, b"stale")
    streams.cached_bytes = len(b"stale")
    streams._cache_segment("segment", b"fresh")
    stored = streams.segment_cache["segment"]
    assert stored[1] == b"fresh"
    assert streams.cached_bytes == len(b"fresh")
