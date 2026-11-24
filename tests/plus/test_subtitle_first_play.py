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
        return fetched, next(iter(streams.pumps.values()), None)

    fetched, pump = asyncio.run(scenario())
    assert fetched, "readahead fetched nothing"
    assert fetched[0].endswith("00006.ts")
    assert fetched[-1].endswith("00013.ts")
    assert pump is not None
    assert pump["next"] == 14


def test_warm_position_starts_at_the_saved_point():
    streams = DebridStreams()
    body, segments = _playlist(100)
    media = DebridMedia(body, segments)
    streams.warm_position(media, 106.4)
    pump = next(iter(streams.pumps.values()), None)
    assert pump is not None
    assert pump["next"] == 21
    assert pump["target"] == 29


def test_warm_position_ignores_play_from_start():
    streams = DebridStreams()
    body, segments = _playlist(100)
    media = DebridMedia(body, segments)
    streams.warm_position(media, 0)
    assert not streams.pumps


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


def test_stalled_provider_segment_fails_instead_of_serving_other_content():
    """A neighbour that never arrives must fail, not replay another segment."""

    async def scenario():
        streams = DebridStreams()
        streams._advance = lambda media, index: None
        stalled = asyncio.Event()

        async def never_ready(url):
            await stalled.wait()
            return None

        streams._fetch_segment = never_ready
        segments = (
            "https://s.stream.real-debrid.com/v/00000.ts",
            "https://s.stream.real-debrid.com/v/00001.ts",
        )
        media = DebridMedia(b"#EXTM3U\n", segments, b"\x47first")
        opening = await streams.deliver(media, "00000.ts", _request(), lambda: None)
        assert opening.body == b"\x47first"
        streams.warm[segments[1]] = asyncio.create_task(asyncio.sleep(60))
        started = asyncio.get_running_loop().time()
        try:
            await streams.deliver(media, "00001.ts", _request(), lambda: None)
            outcome = "served"
        except DebridLibraryError as error:
            outcome = error.status
        elapsed = asyncio.get_running_loop().time() - started
        stalled.set()
        return outcome, elapsed

    from omakase.plus.debrid_library import DebridLibraryError

    outcome, elapsed = asyncio.run(scenario())
    assert outcome == 503
    assert elapsed < 8


def test_uncached_segment_without_history_uses_the_provider():
    async def scenario():
        streams = DebridStreams()
        streams._advance = lambda media, index: None
        calls = []

        async def fake_read(url, **kwargs):
            calls.append(url)
            return b"\x47fresh", 200, {"content-length": "6"}

        streams._read = fake_read
        segments = (
            "https://s.stream.real-debrid.com/v/00000.ts",
            "https://s.stream.real-debrid.com/v/00001.ts",
        )
        media = DebridMedia(b"#EXTM3U\n", segments)
        response = await streams.deliver(media, "00001.ts", _request(), lambda: None)
        return response, calls

    response, calls = asyncio.run(scenario())
    assert response.body == b"\x47fresh"
    assert calls and calls[0].endswith("00001.ts")


def test_a_stall_after_a_seek_fails_instead_of_replaying_old_content():
    """A skip of 39 segments must not replay segment 0 as a stall bridge."""
    from omakase.plus.debrid_library import DebridLibraryError

    async def scenario():
        streams = DebridStreams()
        streams._advance = lambda media, index: None
        segments = tuple(f"https://s.stream.real-debrid.com/v/{n:05d}.ts" for n in range(60))
        media = DebridMedia(b"#EXTM3U\n", segments, b"\x47first")
        await streams.deliver(media, "00000.ts", _request(), lambda: None)
        # The player resumed three minutes in and the provider never answers.
        streams.warm[segments[39]] = asyncio.create_task(asyncio.sleep(60))
        try:
            response = await streams.deliver(media, "00039.ts", _request(), lambda: None)
            return response.status_code
        except DebridLibraryError as error:
            return error.status

    assert asyncio.run(scenario()) == 503


def test_a_long_provider_stall_fails_instead_of_looping_forever():
    from omakase.plus.debrid_library import DebridLibraryError

    async def scenario():
        streams = DebridStreams()
        streams._advance = lambda media, index: None
        segments = tuple(f"https://s.stream.real-debrid.com/v/{n:05d}.ts" for n in range(8))
        media = DebridMedia(b"#EXTM3U\n", segments, b"\x47first")
        await streams.deliver(media, "00000.ts", _request(), lambda: None)
        outcomes = []
        for index in range(1, 6):
            streams.warm[segments[index]] = asyncio.create_task(asyncio.sleep(60))
            try:
                response = await streams.deliver(media, f"{index:05d}.ts", _request(), lambda: None)
                outcomes.append(response.status_code)
            except DebridLibraryError as error:
                outcomes.append(error.status)
        return outcomes

    outcomes = asyncio.run(scenario())
    # No segment is ever replaced by another one, so the first stalled request
    # fails honestly with a retryable status instead of looping frozen pictures.
    assert outcomes[0] == 503
    assert all(outcome == 503 for outcome in outcomes)
