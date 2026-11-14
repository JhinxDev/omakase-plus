"""The direct path forwards exactly the requested bytes, or fails honestly.

These are the decisive checks for the low-storage transport. Each "known bad"
case below is a failure mode that previously produced a wrong picture on the
television: an expired link, a provider that ignores ranges, a provider that
answers with a different byte length, and a provider that sends a short body.
Every one of them must surface an error instead of bytes.
"""

import asyncio
import hashlib
import pathlib

import httpx
import pytest
from starlette.requests import Request

from omakase.plus import direct_stream as direct
from omakase.plus.direct_stream import (
    DirectError,
    DirectSource,
    DirectStream,
    open_direct,
    probe_origin,
    source_for,
)

SIZE = 4096
BODY = bytes((index * 7 + 11) % 251 for index in range(SIZE))


def request(method="GET", **headers):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        }
    )


def source(size=SIZE, name="Episode 01.mkv"):
    version = hashlib.sha256(f"v{size}{name}".encode()).hexdigest()
    identity = hashlib.sha256(b"identity").hexdigest()
    return DirectSource(identity, version, name, size, direct.container_of(name), 1440.0)


def stream(**updates):
    values = {
        "source": source(),
        "origin": "https://files.real-debrid.test/d/abc123",
        "refresh": None,
    }
    return DirectStream(**(values | updates))


class Provider:
    """A tiny range-capable origin that can be told to misbehave."""

    def __init__(self, body=BODY, *, mode="ok", total=None, calls=None, expired=None):
        self.body = body
        self.mode = mode
        self.total = len(body) if total is None else total
        self.calls = [] if calls is None else calls
        self.closed = 0
        self.expired = (1 if expired is None else expired) if mode == "expire" else 0

    def __call__(self, request):
        self.calls.append(request)
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(self.body))})
        if self.mode == "always-expired" or (self.mode == "expire" and self.expired):
            if self.mode == "expire":
                self.expired -= 1
            return httpx.Response(403, headers={"content-type": "text/plain"})
        requested = request.headers.get("range")
        if not requested:
            return httpx.Response(200, stream=httpx.ByteStream(self.body))
        if self.mode == "ignore-range":
            # A provider that answers 200 with the whole file instead of a range.
            return httpx.Response(200, stream=httpx.ByteStream(self.body))
        left, right = requested.removeprefix("bytes=").split("-")
        left = int(left)
        right = min(int(right) if right else len(self.body) - 1, len(self.body) - 1)
        piece = self.body[left : right + 1]
        total = self.total if self.mode == "wrong-total" else len(self.body)
        if self.mode == "short":
            # Honest headers, then a body that stops early: a dropped connection.
            return httpx.Response(
                206,
                stream=httpx.ByteStream(piece[:-1]),
                headers={
                    "content-range": f"bytes {left}-{right}/{total}",
                    "content-length": str(len(piece)),
                },
            )
        return httpx.Response(
            206,
            stream=httpx.ByteStream(piece),
            headers={
                "content-range": f"bytes {left}-{right}/{total}",
                "content-length": str(len(piece)),
            },
        )

    def transport(self):
        return httpx.MockTransport(self)


async def collect(response):
    return b"".join([chunk async for chunk in response.body_iterator])


def run(coroutine):
    return asyncio.run(coroutine)


# --- exact delivery -------------------------------------------------------


def test_full_get_forwards_the_whole_file_without_buffering():
    provider = Provider()

    async def case():
        response = await open_direct(
            stream(), request(), authorize=lambda: None, transport=provider.transport()
        )
        assert response.status_code == 200
        assert response.headers["content-length"] == str(SIZE)
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["cache-control"] == "private, no-store"
        assert "content-range" not in response.headers
        assert response.media_type == "video/x-matroska"
        return await collect(response)

    assert run(case()) == BODY


def test_a_range_returns_exactly_that_span():
    provider = Provider()

    async def case():
        response = await open_direct(
            stream(),
            request(range="bytes=100-199"),
            authorize=lambda: None,
            transport=provider.transport(),
        )
        assert response.status_code == 206
        assert response.headers["content-range"] == f"bytes 100-199/{SIZE}"
        assert response.headers["content-length"] == "100"
        return await collect(response)

    assert run(case()) == BODY[100:200]


def test_head_answers_from_pinned_size_without_touching_the_provider():
    provider = Provider()

    async def case():
        response = await open_direct(
            stream(),
            request("HEAD", range="bytes=0-0"),
            authorize=lambda: None,
            transport=provider.transport(),
        )
        return response

    response = run(case())
    assert response.status_code == 206
    assert response.headers["content-length"] == "1"
    assert provider.calls == []


def test_unsatisfiable_range_is_416():
    provider = Provider()

    async def case():
        return await open_direct(
            stream(),
            request(range=f"bytes={SIZE}-"),
            authorize=lambda: None,
            transport=provider.transport(),
        )

    response = run(case())
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{SIZE}"
    assert provider.calls == []


def test_etag_short_circuit_and_if_range_reset():
    provider = Provider()

    async def case():
        pinned = stream()
        cached = await open_direct(
            pinned,
            request("GET", **{"if-none-match": pinned.etag}),
            authorize=lambda: None,
            transport=provider.transport(),
        )
        stale = await open_direct(
            stream(),
            request("GET", **{"range": "bytes=0-9", "if-range": '"old"'}),
            authorize=lambda: None,
            transport=Provider().transport(),
        )
        return cached, stale

    cached, stale = run(case())
    assert cached.status_code == 304
    # A stale validator means the client's partial copy is useless: answer whole.
    assert stale.status_code == 200


# --- known bad fixtures: these must fail, never substitute -----------------


def test_provider_that_ignores_ranges_fails_instead_of_streaming_the_file():
    provider = Provider(mode="ignore-range")

    async def case():
        with pytest.raises(DirectError) as caught:
            await open_direct(
                stream(),
                request(range="bytes=0-99"),
                authorize=lambda: None,
                transport=provider.transport(),
            )
        return caught.value

    error = run(case())
    assert error.status == 502
    assert "range" in str(error).casefold()


def test_a_reply_for_different_content_is_refused_not_served():
    # Same URL, but the provider now reports a different total size.
    provider = Provider(mode="wrong-total", total=SIZE + 1)

    async def case():
        with pytest.raises(DirectError) as caught:
            await open_direct(
                stream(),
                request(range="bytes=0-99"),
                authorize=lambda: None,
                transport=provider.transport(),
            )
        return caught.value

    error = run(case())
    assert error.status == 409


def test_a_short_body_is_an_error_not_a_shorter_segment():
    provider = Provider(mode="short")

    async def case():
        response = await open_direct(
            stream(),
            request(range="bytes=0-99"),
            authorize=lambda: None,
            transport=provider.transport(),
        )
        with pytest.raises(DirectError):
            await collect(response)

    run(case())


# --- rotating links preserve identity -------------------------------------


def test_expired_link_refreshes_once_and_serves_the_same_span():
    provider = Provider(mode="expire")
    refreshed = []

    async def refresh():
        refreshed.append(True)
        return "https://files.real-debrid.test/d/new456"

    async def case():
        live = stream(refresh=refresh)
        response = await open_direct(
            live,
            request(range="bytes=0-99"),
            authorize=lambda: None,
            transport=provider.transport(),
        )
        return response, await collect(response), live

    response, body, live = run(case())
    assert response.status_code == 206
    assert body == BODY[:100]
    assert refreshed == [True]
    assert live.refreshes == 1
    assert live.snapshot()["refreshes"] == 1


def test_refresh_that_describes_other_content_is_refused():
    provider = Provider(mode="expire")

    async def refresh():
        return "https://files.real-debrid.test/d/other", SIZE + 10

    async def case():
        with pytest.raises(DirectError) as caught:
            await open_direct(
                stream(refresh=refresh),
                request(range="bytes=0-99"),
                authorize=lambda: None,
                transport=provider.transport(),
            )
        return caught.value

    assert run(case()).status == 409


def test_refresh_is_never_looped_and_reports_not_found():
    attempts = []

    def refresh():
        attempts.append(True)
        return "https://files.real-debrid.test/d/new456"

    async def case():
        live = stream(refresh=refresh)
        with pytest.raises(DirectError) as caught:
            await open_direct(
                live,
                request(),
                authorize=lambda: None,
                transport=Provider(mode="always-expired").transport(),
            )
        return caught.value

    error = run(case())
    assert error.status == 404
    assert len(attempts) == 1


# --- cancellation and resource accounting ---------------------------------


def test_closing_the_player_stops_upstream_work():
    big = 4 * direct.CHUNK
    body = bytes((index * 13 + 5) % 251 for index in range(big))
    released = []
    pulled = []

    class Tracked(httpx.AsyncByteStream):
        def __init__(self, payload):
            self.payload = payload

        async def __aiter__(self):
            for offset in range(0, len(self.payload), 4096):
                pulled.append(offset)
                yield self.payload[offset : offset + 4096]

        async def aclose(self):
            released.append(True)

    def serve(req):
        requested = req.headers.get("range", "")
        left, right = requested.removeprefix("bytes=").split("-")
        left, right = int(left), int(right or big - 1)
        return httpx.Response(
            206,
            stream=Tracked(body[left : right + 1]),
            headers={
                "content-range": f"bytes {left}-{right}/{big}",
                "content-length": str(right - left + 1),
            },
        )

    pinned = DirectSource(
        hashlib.sha256(b"identity").hexdigest(),
        hashlib.sha256(b"version").hexdigest(),
        "Big.mkv",
        big,
        "mkv",
        600.0,
    )

    async def case():
        live = DirectStream(pinned, "https://files.real-debrid.test/d/big")
        response = await open_direct(
            live,
            request(range=f"bytes=0-{big - 1}"),
            authorize=lambda: None,
            transport=httpx.MockTransport(serve),
        )
        chunks = 0
        async for chunk in response.body_iterator:
            chunks += 1
            assert len(chunk) <= direct.CHUNK
            if chunks == 2:
                break
        await response.close()
        return live, chunks

    live, chunks = run(case())
    assert chunks == 2
    assert released, "the abandoned upstream connection must be closed"
    assert live.bytes_sent == 2 * direct.CHUNK
    # The title is four chunks long; the abandoned read stopped well before the end.
    assert len(pulled) < big // 4096


def test_revoked_access_stops_a_live_body():
    calls = []

    def authorize():
        calls.append(True)
        if len(calls) > 1:
            raise DirectError("Apple TV pass revoked", 403)

    provider = Provider()

    async def case():
        response = await open_direct(
            stream(), request(), authorize=authorize, transport=provider.transport()
        )
        with pytest.raises(DirectError):
            await collect(response)

    run(case())
    assert len(calls) >= 2


def test_delivery_writes_nothing_to_disk(tmp_path, monkeypatch):
    provider = Provider()

    async def case():
        live = stream()
        # Walk the whole title the way a player does: opening range, then seeks.
        total = 0
        for left in range(0, SIZE, 512):
            right = min(left + 511, SIZE - 1)
            response = await open_direct(
                live,
                request(range=f"bytes={left}-{right}"),
                authorize=lambda: None,
                transport=provider.transport(),
            )
            total += len(await collect(response))
        return live, total

    before = sorted(path.name for path in pathlib.Path(tmp_path).iterdir())
    live, total = run(case())
    after = sorted(path.name for path in pathlib.Path(tmp_path).iterdir())
    assert total == SIZE
    assert before == after
    # Eight full passes over the file still leave process state the same size.
    assert live.bytes_sent == SIZE
    assert live.ranges == 8
    assert live.requests == 8
    assert live.snapshot()["version"] == live.source.version[:12]


# --- identity probing -----------------------------------------------------


def test_probe_confirms_a_link_that_serves_the_pinned_size():
    provider = Provider()

    async def case():
        return await probe_origin(stream(), transport=provider.transport())

    assert run(case())["probed"] is True


def test_probe_refuses_a_link_for_other_content():
    provider = Provider(mode="wrong-total", total=SIZE + 1)

    async def case():
        with pytest.raises(DirectError):
            await probe_origin(stream(), transport=provider.transport())

    run(case())


def test_probe_treats_a_provider_hiccup_as_unproven_not_as_an_identity_change():
    def serve(req):
        raise httpx.ConnectError("offline")

    async def case():
        return await probe_origin(stream(), transport=httpx.MockTransport(serve))

    result = run(case())
    assert result == {"probed": False, "reason": "transport"}


# --- source construction --------------------------------------------------


def test_source_requires_a_known_size_and_container():
    class Cloud:
        identity = hashlib.sha256(b"i").hexdigest()
        media_version = hashlib.sha256(b"v").hexdigest()
        title = "Show.mkv"
        duration = 100.0

    assert source_for(Cloud(), size=SIZE) is not None
    assert source_for(Cloud(), size=0) is None
    Cloud.title = "Show.txt"
    assert source_for(Cloud(), size=SIZE) is None


def test_duration_and_size_are_bounded():
    live = stream()
    live.validate()
    broken = stream(source=DirectSource("a" * 64, "b" * 64, "x.mkv", 0, "mkv", 10.0))
    with pytest.raises(DirectError):
        broken.validate()
