"""Deterministic, offline performance budgets for the provider playback core.

These gates exist because index latency regressions were previously found by hand.
The 6 second Anime index and the Apple TV play reply are the two paths where an
accidental superlinear change is felt directly by a viewer, and both depend on
in-process work that can be measured without a provider, a server or a browser.

Every test in this module builds its own fixture in memory, makes no network call,
reads no database and spawns no process. Each measurement is the best of several
timed runs (``time.perf_counter``), so a busy CI machine adds noise to the worst
run, not to the result that is compared against the budget.

The budgets are deliberately not the live provider numbers below. Only in-process
work is gated here. Provider and device costs are recorded so that a future reader
can tell an architectural cost apart from a code regression, and so that nobody
"fixes" a 272 second OCR pass by relaxing an in-process budget.

In-process costs measured on 2026-11-12 (Windows, CPython 3.12.10, this checkout).
These are the values the budgets were derived from:

    target                                        measured      budget     ratio
    provider playlist parse, 712 segments         9.1 ms        70 ms      7.7x
    master playlist, 16 subtitle renditions       8.5 ms        60 ms      7.1x
      (2000 calls per timed sample, 4.2 us each)
    subtitle media playlist, 700 segments         91 ms         600 ms     6.6x
      (500 calls per timed sample, 181 us each)
    WebVTT serialization, 700 cues                1.3 ms        10 ms      7.7x
    subtitle readiness, pending job (ceiling)     404 ms        1500 ms    3.7x
    subtitle readiness, ready job                 1.1 ms        250 ms     227x

Each budget is above the measured local best so a loaded runner does not flake,
and below ten times the measured local best so that a ten times regression still
fails. A budget set at exactly ten times the baseline would be blind to the
regression it is supposed to catch, which is why the ratio column is not 10x.
The readiness budgets are not scaled from a cost: 400 ms is a declared ceiling in
the code, so the gate asserts the ceiling still bounds the reply, and the ready job
budget asserts that an already-ready job never waits out that ceiling.

Provider-bound costs, measured live and not gated here. They depend on Real-Debrid
transcode scheduling, local CPU for OCR and Whisper, and the network, so asserting
them in CI would only measure the CI machine:

    cold Real-Debrid first segment                about 9.8 s
    OCR of one 60 minute episode                  about 272.5 s
    ASR of one minute of audio (4.1 s per 60 s)   about 4.1 s
    warm Apple TV play reply                      1.2 s to 2.0 s
    cached first segment                          milliseconds

The warm play reply is the number the in-process budgets protect: the reply must
stay in the low seconds while the cold first segment is warmed in the background.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from omakase.plus.debrid_stream import (
    DebridMedia,
    DebridStreams,
    _master_playlist,
    _subtitle_playlist,
    parse_playlist,
)
from omakase.plus.debrid_subs import SubtitleCache, SubtitleJob, SubtitleTrack

PROVIDER_PLAYLIST_URL = (
    "https://a1b2c3.stream.real-debrid.com/torrent/ABCDEF1234567890/123/transcode/index.m3u8"
)
PROVIDER_SEGMENT_COUNT = 712
SEGMENT_LENGTH = "5.000000"
PROVIDER_DURATION = PROVIDER_SEGMENT_COUNT * 5.0

MASTER_PLAYLIST_BUDGET = 0.060
MASTER_PLAYLIST_CALLS = 2000
MASTER_PLAYLIST_BASELINE = 0.0085

PROVIDER_PLAYLIST_BUDGET = 0.070
PROVIDER_PLAYLIST_BASELINE = 0.0091

SUBTITLE_PLAYLIST_BUDGET = 0.600
SUBTITLE_PLAYLIST_CALLS = 500
SUBTITLE_PLAYLIST_BASELINE = 0.091

WEBVTT_BUDGET = 0.010
WEBVTT_BASELINE = 0.0013

READINESS_PENDING_BUDGET = 1.500
READINESS_PENDING_BASELINE = 0.404
READINESS_READY_BUDGET = 0.250
READINESS_READY_BASELINE = 0.0011

SAMPLES = 5


def best_of(operation, samples=SAMPLES):
    """Return the fastest of several timed runs of ``operation``."""
    best = None
    for _ in range(samples):
        started = time.perf_counter()
        operation()
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


def assert_within(name, elapsed, budget, baseline):
    assert elapsed < budget, (
        f"{name} took {elapsed * 1000:.1f} ms, over its {budget * 1000:.1f} ms budget. "
        f"The measured local baseline is {baseline * 1000:.1f} ms. This budget exists to "
        "catch an order of magnitude regression; re-measure with the module docstring "
        "table before changing it."
    )


def provider_playlist(segments=PROVIDER_SEGMENT_COUNT, length=SEGMENT_LENGTH):
    """A provider playlist with ``segments`` real segments and the filler sentinel."""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:6",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for _ in range(segments):
        lines.extend((f"#EXTINF:{length},", "segment.ts"))
    lines.extend(("#EXTINF:0.000000,", "segment.ts"))
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines).encode("ascii")


def provider_media():
    return DebridMedia(
        *parse_playlist(provider_playlist(), PROVIDER_PLAYLIST_URL, PROVIDER_DURATION)
    )


def subtitle_job(tracks=16):
    languages = [
        "en",
        "da",
        "de",
        "es",
        "fr",
        "it",
        "ja",
        "ko",
        "nl",
        "no",
        "pl",
        "pt",
        "ru",
        "sv",
        "zh",
        "und",
    ]
    return SimpleNamespace(
        tracks=[
            SubtitleTrack(number, languages[number % len(languages)], f"Track {number}")
            for number in range(tracks)
        ]
    )


def test_provider_playlist_parse_stays_within_budget():
    data = provider_playlist()

    playlist, segments = parse_playlist(data, PROVIDER_PLAYLIST_URL, PROVIDER_DURATION)

    assert len(segments) == PROVIDER_SEGMENT_COUNT
    assert playlist.count(b"#EXTINF:") == PROVIDER_SEGMENT_COUNT
    assert playlist.startswith(b"#EXTM3U\n") and playlist.endswith(b"#EXT-X-ENDLIST\n")

    elapsed = best_of(lambda: parse_playlist(data, PROVIDER_PLAYLIST_URL, PROVIDER_DURATION))
    assert_within(
        f"parsing a {PROVIDER_SEGMENT_COUNT} segment provider playlist",
        elapsed,
        PROVIDER_PLAYLIST_BUDGET,
        PROVIDER_PLAYLIST_BASELINE,
    )


def test_master_playlist_stays_within_budget():
    media = provider_media()
    job = subtitle_job()

    body = _master_playlist(media, job)

    assert body.count(b'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs"') == 16
    assert b',SUBTITLES="subs"' in body
    assert body.endswith(b"stream.m3u8\n")

    elapsed = best_of(lambda: [_master_playlist(media, job) for _ in range(MASTER_PLAYLIST_CALLS)])
    assert_within(
        f"{MASTER_PLAYLIST_CALLS} master playlists with a 16 track subtitle group",
        elapsed,
        MASTER_PLAYLIST_BUDGET,
        MASTER_PLAYLIST_BASELINE,
    )


def test_subtitle_playlist_stays_within_budget():
    media = provider_media()

    body = _subtitle_playlist(media, 0)

    assert body.startswith(b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:")
    assert b"subtitles0.vtt" in body and body.endswith(b"#EXT-X-ENDLIST\n")
    assert f"#EXTINF:{PROVIDER_DURATION:.3f},".encode() in body

    elapsed = best_of(
        lambda: [_subtitle_playlist(media, 0) for _ in range(SUBTITLE_PLAYLIST_CALLS)]
    )
    assert_within(
        f"{SUBTITLE_PLAYLIST_CALLS} subtitle media playlists over 700 segments",
        elapsed,
        SUBTITLE_PLAYLIST_BUDGET,
        SUBTITLE_PLAYLIST_BASELINE,
    )


def webvtt_rows(count=700):
    return [
        (number * 4.0, number * 4.0 + 3.9, f"Cue number {number} with representative caption text.")
        for number in range(count)
    ]


def test_webvtt_serialization_stays_within_budget():
    rows = webvtt_rows()

    data = SubtitleCache._vtt_from_segments(rows)

    assert data.startswith(b"WEBVTT\n\n")
    assert data.count(b" --> ") == 700
    assert data.endswith(b"\n")

    elapsed = best_of(lambda: SubtitleCache._vtt_from_segments(rows))
    assert_within(
        "serializing 700 WebVTT cues",
        elapsed,
        WEBVTT_BUDGET,
        WEBVTT_BASELINE,
    )


def test_subtitle_readiness_decision_stays_within_budget():
    streams = DebridStreams()
    ready = SubtitleJob("ready")
    ready.ready.set()

    # A ready job must never wait for the ceiling.
    elapsed = best_of(lambda: asyncio.run(streams._subtitle_ready(ready)))
    assert_within(
        "the subtitle readiness decision for a ready job",
        elapsed,
        READINESS_READY_BUDGET,
        READINESS_READY_BASELINE,
    )

    # An unready job must fall through after the declared ceiling instead of
    # holding the index or the play reply. A fresh job is required per run: an
    # asyncio.Event binds to the first loop that awaits it.
    elapsed = best_of(
        lambda: asyncio.run(streams._subtitle_ready(SubtitleJob("pending"))), samples=3
    )
    assert_within(
        "the subtitle readiness decision for an unready job",
        elapsed,
        READINESS_PENDING_BUDGET,
        READINESS_PENDING_BASELINE,
    )
