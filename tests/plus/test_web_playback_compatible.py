"""Qualified conversion manifests must stay bound to one owned part and session."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from omakase.plus import web_playback_compatible as compat
from omakase.plus.web_playback_delivery import DeliveryError


def manifest(upstream_id):
    path = f"session/{upstream_id}/$RepresentationID$/"
    template = (
        '<SegmentTemplate timescale="1" duration="5" startNumber="0" '
        f'initialization="{path}header" media="{path}$Number$.m4s"/>'
    )
    return (
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" '
        'mediaPresentationDuration="PT10M5S">'
        '<Period start="PT0S" duration="PT10M5S">'
        f'<AdaptationSet>{template}<Representation id="0" mimeType="video/mp4" '
        'codecs="avc1.640029" width="1920" height="1080" bandwidth="28000000"/></AdaptationSet>'
        f'<AdaptationSet>{template}<Representation id="1" mimeType="audio/mp4" '
        'codecs="mp4a.40.2" audioSamplingRate="48000" bandwidth="256000"/></AdaptationSet>'
        "</Period></MPD>"
    ).encode()


def decision(part="2", action="copy"):
    return (
        '<MediaContainer><Video ratingKey="8"><Media>'
        f'<Part id="{part}"><Stream streamType="1" decision="{action}" codec="h264"/>'
        '<Stream streamType="2" decision="transcode" codec="aac"/></Part>'
        "</Media></Video></MediaContainer>"
    ).encode()


class Bytes(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data = data

    async def __aiter__(self):
        yield self.data


def test_published_manifest_has_only_scoped_routes():
    data, tracks, count = compat._manifest(manifest("private"), "private", "local", 605)
    assert b"private" not in data
    assert b"compatible/local/$RepresentationID$/$Number$.m4s" in data
    assert tracks == {"0": "video/mp4", "1": "audio/mp4"}
    assert count == 121


@pytest.mark.parametrize(
    "body",
    [
        b"<!DOCTYPE MPD><MPD/>",
        manifest("other"),
        manifest("private").replace(
            b"<Period", b"<BaseURL>https://example.invalid</BaseURL><Period"
        ),
        manifest("private").replace(b"mp4a.40.2", b"dtsc"),
        manifest("private").replace(b"PT10M5S", b"PT10M0S"),
    ],
)
def test_unqualified_manifest_rejected(body):
    with pytest.raises(DeliveryError):
        compat._manifest(body, "private", "local", 605)


def test_scoped_conversion_rotates_on_seek_and_releases_transport(monkeypatch):
    source = SimpleNamespace(
        base_url="https://plex.invalid",
        token="fixture",
        metadata_key="8",
        part_key="/library/parts/2/123/fixture.mkv",
        duration_ms=605000,
    )

    async def destination(base_url, *, transport):
        assert base_url == source.base_url
        assert isinstance(transport, httpx.MockTransport)
        return SimpleNamespace(
            base_url=base_url, host_header="plex.invalid", sni_hostname="plex.invalid"
        )

    monkeypatch.setattr(compat, "pin_destination", destination)
    decisions = []
    offsets = []
    stopped = []
    released = []

    def provider(request):
        assert request.headers["x-plex-token"] == "fixture"
        upstream_id = request.headers["x-plex-session-identifier"]
        path = request.url.path.removeprefix(compat._PREFIX)
        if path == "decision":
            decisions.append(upstream_id)
            return httpx.Response(200, stream=Bytes(decision()))
        if path == "start.mpd":
            assert decisions[-1] == upstream_id
            offsets.append(request.url.params["offset"])
            return httpx.Response(200, stream=Bytes(manifest(upstream_id)))
        if path == "stop":
            assert upstream_id == request.url.params["session"]
            stopped.append(upstream_id)
            return httpx.Response(200)
        assert path == f"session/{upstream_id}/0/60.m4s"
        return httpx.Response(200, stream=Bytes(b"video sample"), headers={"content-length": "12"})

    async def authorize():
        return None

    async def run():
        stream = compat.CompatibleStream(source, httpx.MockTransport(provider))
        first = await stream.open(0, authorize)
        second = await stream.open(300, authorize)
        assert offsets == ["0.0", "290.0"]
        assert stopped == [first.upstream_id]
        for generation, asset in [
            (first, "0.m4s"),
            (second, "57.m4s"),
            (second, "../60.m4s"),
            (second, "121.m4s"),
        ]:
            with pytest.raises(DeliveryError, match="unavailable"):
                await stream.resource(generation, "0", asset, authorize, lambda: None)
        response = await stream.resource(
            second, "0", "60.m4s", authorize, lambda: released.append(True)
        )
        assert b"".join([block async for block in response.body_iterator]) == b"video sample"
        await response.close()
        assert released == [True]
        await stream.close()
        assert stopped == [first.upstream_id, second.upstream_id]

    asyncio.run(run())
    with pytest.raises(DeliveryError):
        compat._require_decision(decision(part="9"), source)
    with pytest.raises(DeliveryError):
        compat._require_decision(decision(action="transcode"), source)
