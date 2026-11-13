"""The gateway forwards only the exact selected part, with bounded I/O."""

import asyncio
import os
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request

from omakase.plus import web_playback_delivery as delivery
from omakase.plus.web_playback_admission import Admission
from omakase.plus.web_playback_delivery import DeliveryError, open_local, open_part, parse_range


def test_ranges_are_single_and_exact():
    assert parse_range("bytes=10-20", 100) == (10, 20)
    assert parse_range("bytes=90-", 100) == (90, 99)
    assert parse_range("bytes=-10", 100) == (90, 99)
    assert parse_range("bytes=0-999", 100) == (0, 99)
    for value in ("bytes=100-", "bytes=2-1", "bytes=0-1,4-5", "items=1-2", "bytes=-0"):
        with pytest.raises(DeliveryError):
            parse_range(value, 100)


def request(method="GET", **headers):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/",
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )


def source(**updates):
    values = dict(
        base_url="https://plex.invalid",
        token="dummy-test",
        part_key="/library/parts/12/1234/file.mp4",
        part_size=100,
        media_version="a" * 64,
        container="mp4",
    )
    return SimpleNamespace(**(values | updates))


def test_exact_range_stream_and_credentials_stay_server_side():
    seen, checked = [], []

    def serve(req):
        seen.append(req)
        return httpx.Response(
            206,
            stream=httpx.ByteStream(b"x" * 11),
            headers={"content-range": "bytes 10-20/100", "content-type": "video/mp4"},
        )

    async def run():
        async def authorize():
            checked.append(True)

        response = await open_part(
            source(),
            request(range="bytes=10-20"),
            authorize=authorize,
            transport=httpx.MockTransport(serve),
        )
        assert response.status_code == 206
        assert response.headers["content-range"] == "bytes 10-20/100"
        assert response.headers["content-length"] == "11"
        assert response.headers["cache-control"] == "private, no-store"
        assert b"".join([chunk async for chunk in response.body_iterator]) == b"x" * 11
        assert "dummy-test" not in str(dict(response.headers))

    asyncio.run(run())
    assert checked
    assert seen[0].url.path == "/library/parts/12/1234/file.mp4"
    assert seen[0].headers["x-plex-token"] == "dummy-test"
    assert not seen[0].url.query


@pytest.mark.parametrize(
    "status,headers",
    [
        (302, {"location": "https://evil.invalid"}),
        (200, {}),
        (206, {"content-range": "bytes 10-20/101"}),
        (206, {"content-range": "bytes 0-10/100"}),
    ],
)
def test_wrong_response_never_becomes_selected_media(status, headers):
    async def run():
        with pytest.raises(DeliveryError):
            await open_part(
                source(),
                request(range="bytes=10-20"),
                authorize=lambda: None,
                transport=httpx.MockTransport(
                    lambda req: httpx.Response(status, headers=headers, content=b"x" * 11)
                ),
            )

    asyncio.run(run())


def test_head_and_unsatisfiable_range_do_not_fetch_body():
    async def run():
        def unexpected(req):
            raise AssertionError("A HEAD/invalid range must not open a Plex body")

        transport = httpx.MockTransport(unexpected)
        head = await open_part(
            source(), request("HEAD"), authorize=lambda: None, transport=transport
        )
        assert head.status_code == 200 and head.headers["content-length"] == "100"
        invalid = await open_part(
            source(), request(range="bytes=100-"), authorize=lambda: None, transport=transport
        )
        assert invalid.status_code == 416
        assert invalid.headers["content-range"] == "bytes */100"

    asyncio.run(run())


def test_revocation_checked_before_any_network_request():
    async def run():
        def revoked():
            raise PermissionError("revoked")

        with pytest.raises(PermissionError):
            await open_part(
                source(),
                request(),
                authorize=revoked,
                transport=httpx.MockTransport(lambda req: pytest.fail("network")),
            )

    asyncio.run(run())


def test_owned_derivative_ranges_head_and_read_lifetime(tmp_path):
    file = tmp_path / "chunk-0-00001.m4s"
    file.write_bytes(bytes(range(100)))
    closed = []

    async def run():
        head = await open_local(
            file,
            request("HEAD"),
            authorize=lambda: None,
            etag_seed="generation",
            on_close=lambda: closed.append("head"),
        )
        assert head.status_code == 200 and head.headers["content-length"] == "100"
        assert closed == ["head"]
        response = await open_local(
            file,
            request(range="bytes=-10"),
            authorize=lambda: None,
            etag_seed="generation",
            on_close=lambda: closed.append("body"),
        )
        assert response.status_code == 206
        assert b"".join([chunk async for chunk in response.body_iterator]) == bytes(range(90, 100))
        assert closed == ["head", "body"]
        invalid = await open_local(
            file, request(range="bytes=100-"), authorize=lambda: None, etag_seed="generation"
        )
        assert invalid.status_code == 416 and invalid.headers["content-range"] == "bytes */100"

    asyncio.run(run())


def test_derivative_false_authorization_and_symlink_never_read(tmp_path):
    file = tmp_path / "manifest.mpd"
    file.write_text("private derivative")

    async def run():
        with pytest.raises(DeliveryError):
            await open_local(file, request(), authorize=lambda: False, etag_seed="generation")

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["derivative", "direct"])
@pytest.mark.parametrize("early_failure", [True, False])
def test_asgi_socket_failure_and_backpressure_release_owned_resources(
    tmp_path, monkeypatch, kind, early_failure
):
    """Observe real descriptor/stream cleanup at the ASGI socket boundary."""

    class FileProbe:
        def __init__(self):
            self.opened, self.reads, self.closed = [], [], []

        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, *args, **kwargs):
            fd = os.open(*args, **kwargs)
            self.opened.append(fd)
            return fd

        def read(self, *args):
            data = os.read(*args)
            self.reads.append(len(data))
            return data

        def close(self, fd):
            self.closed.append(fd)
            return os.close(fd)

    class Body(httpx.AsyncByteStream):
        emitted, closed = 0, 0

        async def __aiter__(self):
            for _ in range(8):
                self.emitted += 65536
                yield b"x" * 65536

        async def aclose(self):
            self.closed += 1

    class Transport(httpx.MockTransport):
        closed = 0

        async def aclose(self):
            self.closed += 1

    file = tmp_path / "chunk-0-00001.m4s"
    file.write_bytes(b"x" * (8 * 65536))
    probe, body = FileProbe(), Body()
    monkeypatch.setattr(delivery, "os", probe)
    transport = Transport(
        lambda req: httpx.Response(200, headers={"Content-Length": str(8 * 65536)}, stream=body)
    )

    async def run():
        admission, releases = Admission(), []
        release = admission.acquire("read", 1, "synthetic")

        def on_close():
            releases.append(True)
            release()

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [],
            "asgi": {"version": "3.0", "spec_version": "2.4" if early_failure else "2.0"},
        }
        if kind == "derivative":
            response = await open_local(
                file,
                Request(scope),
                authorize=lambda: None,
                etag_seed="synthetic",
                on_close=on_close,
            )
        else:
            response = await open_part(
                source(part_size=8 * 65536),
                Request(scope),
                authorize=lambda: None,
                transport=transport,
                on_close=on_close,
            )
        disconnect, first, never = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def receive():
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start" and early_failure:
                raise OSError("synthetic closed socket")
            if message["type"] == "http.response.body":
                assert len(message["body"]) == 65536
                first.set()
                await never.wait()

        task = asyncio.create_task(response(scope, receive, send))
        if early_failure:
            from starlette.requests import ClientDisconnect

            with pytest.raises(ClientDisconnect):
                await asyncio.wait_for(task, 5)
        else:
            await asyncio.wait_for(first.wait(), 5)
            assert admission.active and not releases
            assert (sum(probe.reads) if kind == "derivative" else body.emitted) == 65536
            disconnect.set()
            await asyncio.wait_for(task, 5)
        assert releases == [True] and not admission.active
        assert (sum(probe.reads) if kind == "derivative" else body.emitted) == (
            0 if early_failure else 65536
        )
        if kind == "derivative":
            assert probe.opened == probe.closed and len(probe.opened) == 1
            with pytest.raises(OSError):
                os.fstat(probe.opened[0])
        else:
            assert body.closed == transport.closed == 1

    asyncio.run(run())
