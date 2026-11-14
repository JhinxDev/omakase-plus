"""Regression tests for defects found by the end-to-end audit.

Each test pins a failure that was proven by source reading and a synthetic
reproduction, not by a symptom on the television:

* a provider timeout cached an empty inventory as a success and poisoned the
  cache for every later caller,
* an unreadable first segment was answered with the second segment's bytes,
* an unresolved provider segment was answered with another segment's content,
* SubRip cues after the first hour were dropped, and a rounded timestamp could
  carry four millisecond digits.
"""

import asyncio
import contextlib

import httpx
import pytest

from omakase.plus.debrid_library import DebridLibrary, DebridLibraryError
from omakase.plus.debrid_stream import DebridMedia, DebridStreams
from omakase.plus.debrid_subs import SubtitleCache

ORIGIN = "https://3.stream.real-debrid.com"


def _request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "headers": []})


def test_inventory_timeout_never_caches_an_empty_success(monkeypatch):
    """A timed out inventory must raise, not cache a one element tuple."""
    library = DebridLibrary()
    calls = {"count": 0}

    async def never(*args, **kwargs):
        calls["count"] += 1
        await asyncio.sleep(3600)

    @contextlib.asynccontextmanager
    async def instant_timeout(seconds):
        raise TimeoutError("synthetic provider timeout")
        yield  # pragma: no cover - the context never enters

    monkeypatch.setattr(library, "_all", never)
    monkeypatch.setattr("omakase.plus.debrid_library.asyncio.timeout", instant_timeout)

    async def fast_sleep(seconds):
        return None

    monkeypatch.setattr("omakase.plus.debrid_library.asyncio.sleep", fast_sleep)

    async def run():
        with pytest.raises(DebridLibraryError):
            await library.inventory("token")
        return dict(library.cache)

    cache = asyncio.run(run())
    assert cache == {}, "a failed inventory must never be cached as success"
    assert calls["count"] == 0, "an expiring timeout must not start the provider read"

    # The next real read must work and unpack into the two expected values.
    async def good(token, kind):
        return {"torrent": kind} if kind == "torrents" else {"download": kind}

    monkeypatch.setattr(library, "_all", good)

    @contextlib.asynccontextmanager
    async def instant_ok(seconds):
        yield

    monkeypatch.setattr("omakase.plus.debrid_library.asyncio.timeout", instant_ok)

    async def second():
        torrents, downloads = await library.inventory("token")
        return torrents, downloads

    torrents, downloads = asyncio.run(second())
    assert torrents and downloads


def test_unreadable_first_segment_is_never_answered_with_the_second():
    """The player caches a substituted response under the requested URL."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("00000.ts"):
            return httpx.Response(503)
        return httpx.Response(200, content=b"\x47second-segment")

    async def run():
        streams = DebridStreams(httpx.MockTransport(handler))
        media = DebridMedia(
            b"#EXTM3U\n#EXTINF:5,\n00000.ts\n#EXTINF:5,\n00001.ts\n#EXT-X-ENDLIST\n",
            (ORIGIN + "/00000.ts", ORIGIN + "/00001.ts"),
        )
        with pytest.raises(DebridLibraryError) as caught:
            await streams.deliver(media, "00000.ts", _request(), lambda: None)
        return caught.value.status

    status = asyncio.run(run())
    assert status == 503
    assert not any(path.endswith("00001.ts") for path in calls), (
        "the second segment must never be read for a first segment request"
    )


def test_unresolved_provider_segment_fails_instead_of_replaying_another():
    """A stalled neighbour must not be replaced by the previously served bytes."""
    from omakase.plus.debrid_library import DebridLibraryError

    async def scenario():
        streams = DebridStreams()
        streams._advance = lambda media, index: None
        release = asyncio.Event()

        async def never_ready(url):
            await release.wait()
            return None

        streams._fetch_segment = never_ready
        segments = tuple(f"{ORIGIN}/{number:05d}.ts" for number in range(4))
        media = DebridMedia(b"#EXTM3U\n", segments, b"\x47first")
        first = await streams.deliver(media, "00000.ts", _request(), lambda: None)
        streams.warm[segments[1]] = asyncio.create_task(asyncio.sleep(60))
        try:
            await streams.deliver(media, "00001.ts", _request(), lambda: None)
            outcome = "served"
        except DebridLibraryError as error:
            outcome = error.status
        finally:
            release.set()
        return first, outcome

    first, outcome = asyncio.run(scenario())
    assert first.body == b"\x47first"
    assert outcome == 503


def test_srt_conversion_keeps_cues_after_the_first_hour():
    data = b"1\n01:00:01,000 --> 01:00:02,500\nA late line\n"
    out = SubtitleCache._srt_vtt(data)
    assert b"01:00:01.000 --> 01:00:02.500" in out
    assert b"A late line" in out


def test_asr_timestamp_rounding_stays_valid():
    out = SubtitleCache._vtt_from_segments([(1.9996, 2.5, "Rounded")])
    assert b"00:00:02.000 --> 00:00:02.500" in out
    assert b".1000" not in out


def test_a_provider_503_uses_a_usable_last_good_inventory(monkeypatch):
    """A transient provider 5xx must not blank a section that we just read.

    The judgment reproduced this residual: inventory raised straight out on a 503
    even though a recent last-good snapshot was available, so the browse failed
    for a few minutes over a hiccup the stale fallback already existed for.
    """
    library = DebridLibrary()
    now = {"value": 100}
    library.clock = lambda: now["value"]

    async def ok(_token, kind):
        return {("t" if kind == "torrents" else "d") + "x": {"id": "x"}}

    async def failing(_token, kind):
        raise DebridLibraryError(status=503)

    async def fast_sleep(seconds):
        return None

    monkeypatch.setattr("omakase.plus.debrid_library.asyncio.sleep", fast_sleep)

    async def run():
        monkeypatch.setattr(library, "_all", ok)
        first = await library.inventory("token")
        # Just past the short TTL, still inside the bounded stale window.
        now["value"] += 60
        monkeypatch.setattr(library, "_all", failing)
        second = await library.inventory("token")
        return first, second

    first, second = asyncio.run(run())
    assert second == first
    assert set(second[0]) == {"tx"}


def test_access_denied_is_not_hidden_behind_a_stale_inventory(monkeypatch):
    """Only transient 5xx is recoverable. A 403 must reach the caller."""
    library = DebridLibrary()
    now = {"value": 100}
    library.clock = lambda: now["value"]

    async def ok(_token, kind):
        return {("t" if kind == "torrents" else "d") + "x": {"id": "x"}}

    async def denied(_token, kind):
        raise DebridLibraryError("Real-Debrid denied access to this account or file.", 403)

    async def fast_sleep(seconds):
        return None

    monkeypatch.setattr("omakase.plus.debrid_library.asyncio.sleep", fast_sleep)

    async def run():
        monkeypatch.setattr(library, "_all", ok)
        await library.inventory("token")
        now["value"] += 60
        monkeypatch.setattr(library, "_all", denied)
        await library.inventory("token")

    with pytest.raises(DebridLibraryError) as caught:
        asyncio.run(run())
    assert caught.value.status == 403
