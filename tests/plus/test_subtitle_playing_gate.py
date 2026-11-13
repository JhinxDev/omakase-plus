"""Subtitle delivery helpers that are easy to regress silently.

The first play of a title is the hard case: AVPlayer fetches the master playlist
once, so an index without a SUBTITLES group leaves the television with no
subtitle menu for the whole session. A cached job may therefore hold that one
request, but a job that needs OCR or ASR must not, and a source that only needs
its embedded text tracks copied has to do that while the pass is open instead of
waiting for a pause that never ends.
"""

import asyncio
import time

from starlette.requests import Request

from omakase.plus.debrid_stream import DebridMedia, DebridStreams
from omakase.plus.debrid_subs import SubtitleJob, SubtitleTrack

WEBVTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n"
PROVIDER = b"#EXTM3U\n#EXTINF:5,\n00000.ts\n#EXT-X-ENDLIST\n"


def _media(**kwargs):
    return DebridMedia(PROVIDER, (), subtitle_key="version", **kwargs)


def _request():
    return Request({"type": "http", "method": "GET", "headers": []})


def test_text_only_job_holds_the_index_for_its_cues():
    """A text copy finishes in seconds, so the first index waits for its cues."""

    async def run():
        job = SubtitleJob("version")
        job.tracks = [SubtitleTrack(0, "en", "English")]
        job.text_only = True

        async def convert():
            await asyncio.sleep(0.2)
            job.tracks[0].data = WEBVTT
            job.ready.set()

        job.task = asyncio.create_task(convert())
        streams = DebridStreams()
        streams.subtitles.jobs["version"] = job
        response = await streams.deliver(_media(), "index.m3u8", _request(), lambda: None)
        await job.task
        return response

    response = asyncio.run(run())
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" in response.body
    assert b'NAME="English"' in response.body
    assert b'LANGUAGE="en"' in response.body


def test_bitmap_job_is_released_instead_of_waiting_for_ocr():
    """OCR can run for minutes, so the index answers without the group at once."""

    async def run():
        job = SubtitleJob("version")
        job.text_only = False
        job.task = asyncio.create_task(asyncio.sleep(30))
        streams = DebridStreams()
        streams.subtitles.jobs["version"] = job
        started = time.monotonic()
        response = await streams.deliver(_media(), "index.m3u8", _request(), lambda: None)
        elapsed = time.monotonic() - started
        job.task.cancel()
        await asyncio.gather(job.task, return_exceptions=True)
        return response, elapsed

    response, elapsed = asyncio.run(run())
    assert b"#EXT-X-MEDIA:TYPE=SUBTITLES" not in response.body
    assert elapsed < 1.0


def test_text_tracks_convert_during_a_pass_instead_of_waiting_for_idle(tmp_path):
    """A first anime play used to deadlock waiting for its own pass to end."""
    import omakase.plus.debrid_subs as subs

    cache = subs.SubtitleCache(tmp_path)
    converted = []

    async def streams(direct):
        return [
            {"index": 2, "codec_name": "ass", "tags": {"language": "eng", "title": "English"}},
            {"index": 3, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}},
        ]

    async def convert(job, direct, workdir):
        converted.append(len(job.tracks))

    cache._streams = streams
    cache._convert = convert
    cache.playing = True
    job = subs.SubtitleJob(key="anime")

    asyncio.run(cache._run(job, "https://example.invalid/one.mkv"))

    assert converted == [1]
    assert job.text_only is True
    assert job.done.is_set()


def test_bitmap_source_marks_the_job_as_ocr_only(tmp_path):
    """A PGS source must keep the idle wait for OCR, not run the text converter."""
    import omakase.plus.debrid_subs as subs

    cache = subs.SubtitleCache(tmp_path)
    ocr = []

    async def streams(direct):
        return [{"index": 5, "codec_name": "hdmv_pgs_subtitle", "tags": {"language": "eng"}}]

    async def run_ocr(job, direct, workdir, pgs):
        ocr.append(len(pgs))

    cache._streams = streams
    cache._ocr = run_ocr
    job = subs.SubtitleJob(key="movie")

    asyncio.run(cache._run(job, "https://example.invalid/two.mkv"))

    assert ocr == [1]
    assert job.text_only is False
    assert job.done.is_set()
