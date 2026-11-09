"""Same-origin DASH for an exact Plex part with video copy and compatible audio."""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass
from uuid import uuid4
from xml.etree import ElementTree as ET

import httpx

from omakase.plus.outbound import OutboundError, pin_destination
from omakase.plus.web_playback_delivery import HEADERS, DeliveryError, _ClosingStream

_PREFIX = "/video/:/transcode/universal/"
_PROFILE = (
    "add-transcode-target(type=videoProfile&context=streaming&protocol=dash&"
    "container=mp4&videoCodec=h264&audioCodec=aac&replace=true)"
)
_NS = "{urn:mpeg:dash:schema:mpd:2011}"
_XML_LIMIT = 128 * 1024
_SEGMENT_LIMIT = 48 * 1024**2
_SEGMENT_SECONDS = 5


def _fail(message="The compatible stream is temporarily unavailable.", status=502):
    return DeliveryError(message, status)


def _document(data):
    if not data or b"<!" in data or b"\x00" in data:
        raise _fail()
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        raise _fail() from None


def _integer(value, low, high):
    if not re.fullmatch(r"[0-9]{1,12}", value or ""):
        raise _fail()
    result = int(value)
    if not low <= result <= high:
        raise _fail()
    return result


def _require_decision(data, source):
    root = _document(data)
    videos = root.findall("Video")
    if root.tag != "MediaContainer" or len(videos) != 1:
        raise _fail()
    video = videos[0]
    media = video.findall("Media")
    if video.get("ratingKey") != source.metadata_key or len(media) != 1:
        raise _fail()
    parts = media[0].findall("Part")
    if len(parts) != 1 or parts[0].get("id") != source.part_key.split("/")[3]:
        raise _fail()
    streams = [track for track in parts[0].findall("Stream") if track.get("decision")]
    decisions = [
        (track.get("streamType"), track.get("decision"), track.get("codec")) for track in streams
    ]
    if (
        decisions.count(("1", "copy", "h264")) != 1
        or sum(kind == "1" for kind, _, _ in decisions) != 1
        or sum(kind == "2" for kind, _, _ in decisions) != 1
        or not any(
            kind == "2" and action in {"copy", "transcode"} and codec == "aac"
            for kind, action, codec in decisions
        )
        or any(kind == "3" and action == "burn" for kind, action, _ in decisions)
    ):
        raise _fail("This copy needs a different playback format.", 415)


def _seconds(value):
    match = re.fullmatch(r"PT(?:(\d{1,2})H)?(?:(\d{1,2})M)?(\d{1,2}(?:\.\d{1,9})?)S", value or "")
    if not match:
        raise _fail()
    hours, minutes, seconds = match.groups()
    return 3600 * int(hours or 0) + 60 * int(minutes or 0) + float(seconds)


def _manifest(data, upstream_id, generation, duration):
    upstream = _document(data)
    total = _seconds(upstream.get("mediaPresentationDuration"))
    if (
        upstream.tag != _NS + "MPD"
        or upstream.get("type") != "static"
        or abs(total - duration) > 2
        or len(upstream) != 1
    ):
        raise _fail()
    period = upstream[0]
    if (
        period.tag != _NS + "Period"
        or _seconds(period.get("start")) != 0
        or abs(_seconds(period.get("duration")) - total) > 0.01
        or len(period) != 2
    ):
        raise _fail()

    # Generate an allowlisted document. No BaseURL, arbitrary attributes, external
    # links, or Plex session identifiers reach the browser.
    mpd = ET.Element(
        "MPD",
        {
            "xmlns": _NS[1:-1],
            "type": "static",
            "profiles": "urn:mpeg:dash:profile:isoff-live:2011",
            "mediaPresentationDuration": f"PT{total:.6f}S",
            "minBufferTime": "PT3S",
            "maxSegmentDuration": "PT10S",
        },
    )
    output_period = ET.SubElement(mpd, "Period", {"start": "PT0S", "duration": f"PT{total:.6f}S"})
    representations = {}
    upstream_path = f"session/{upstream_id}/$RepresentationID$/"
    local_path = f"compatible/{generation}/$RepresentationID$/"
    for adaptation in period:
        if adaptation.tag != _NS + "AdaptationSet" or len(adaptation) != 2:
            raise _fail()
        template = adaptation.find(_NS + "SegmentTemplate")
        representation = adaptation.find(_NS + "Representation")
        if (
            template is None
            or len(template)
            or representation is None
            or template.attrib
            != {
                "timescale": "1",
                "duration": str(_SEGMENT_SECONDS),
                "startNumber": "0",
                "initialization": upstream_path + "header",
                "media": upstream_path + "$Number$.m4s",
            }
        ):
            raise _fail()
        mime = representation.get("mimeType")
        codec = representation.get("codecs", "")
        rep = str(_integer(representation.get("id"), 0, 15))
        if mime in representations.values() or rep in representations:
            raise _fail()
        attrs = {
            "id": rep,
            "mimeType": mime,
            "codecs": codec,
            "bandwidth": str(_integer(representation.get("bandwidth"), 1, 200_000_000)),
        }
        if mime == "video/mp4" and re.fullmatch(r"avc1\.[0-9a-fA-F]{6}", codec):
            for key in ("width", "height"):
                attrs[key] = str(_integer(representation.get(key), 1, 8192))
        elif mime == "audio/mp4" and codec == "mp4a.40.2":
            attrs["audioSamplingRate"] = str(
                _integer(representation.get("audioSamplingRate"), 8000, 192000)
            )
        else:
            raise _fail()
        target = ET.SubElement(output_period, "AdaptationSet", {"segmentAlignment": "true"})
        ET.SubElement(
            target,
            "SegmentTemplate",
            {
                "timescale": "1",
                "duration": str(_SEGMENT_SECONDS),
                "startNumber": "0",
                "initialization": local_path + "header",
                "media": local_path + "$Number$.m4s",
            },
        )
        ET.SubElement(target, "Representation", attrs)
        representations[rep] = mime
    return ET.tostring(mpd, encoding="utf-8"), representations, math.ceil(total / _SEGMENT_SECONDS)


def eligible(source, codecs):
    return (
        getattr(source, "domain", "anime") != "anime"
        and source.container in {"mkv", "mp4"}
        and source.video_codec == "h264"
        and {"h264", "aac"}.issubset(codecs)
        and source._single_plex_part
        and source.audio_codec
    )


@dataclass(frozen=True)
class Generation:
    upstream_id: str
    key: str
    manifest: bytes
    representations: dict
    first_segment: int
    segment_count: int


class CompatibleStream:
    def __init__(self, source, transport=None):
        self.source = source
        self.transport = transport
        self.lock = asyncio.Lock()
        self.generation = None
        self.destination = None

    def _headers(self, upstream_id):
        return {
            "Host": self.destination.host_header,
            "X-Plex-Token": self.source.token,
            "X-Plex-Client-Identifier": "omakase-web-playback",
            "X-Plex-Client-Profile-Name": "Generic",
            "X-Plex-Platform": "Chrome",
            "X-Plex-Session-Identifier": upstream_id,
            "Accept-Encoding": "identity",
        }

    def _client(self):
        return httpx.AsyncClient(
            transport=self.transport, follow_redirects=False, trust_env=False, timeout=12
        )

    def _request(self, client, path, upstream_id, params=None):
        return client.build_request(
            "GET",
            self.destination.base_url + _PREFIX + path,
            params=params,
            headers=self._headers(upstream_id),
            extensions={"sni_hostname": self.destination.sni_hostname},
        )

    async def _xml(self, client, path, upstream_id, params):
        response = await client.send(self._request(client, path, upstream_id, params), stream=True)
        try:
            if response.status_code != 200:
                raise _fail()
            data = bytearray()
            async for block in response.aiter_raw():
                if len(data) + len(block) > _XML_LIMIT:
                    raise _fail()
                data.extend(block)
            return bytes(data)
        finally:
            await response.aclose()

    async def _stop(self, upstream_id):
        try:
            async with asyncio.timeout(4), self._client() as client:
                request = self._request(
                    client,
                    "stop",
                    upstream_id,
                    {"session": upstream_id, "transcodeSessionId": upstream_id},
                )
                response = await client.send(request, stream=True)
                await response.aclose()
        except (httpx.HTTPError, TimeoutError):
            # Plex expires abandoned transcoding sessions.
            pass

    async def close(self):
        async with self.lock:
            if self.generation:
                upstream_id = self.generation.upstream_id
                self.generation = None
                await self._stop(upstream_id)

    async def open(self, position, authorize):
        async with self.lock:
            await authorize()
            if self.generation:
                await self._stop(self.generation.upstream_id)
                self.generation = None
            upstream_id = uuid4().hex
            key = uuid4().hex
            # Give the player a segment before the requested keyframe and tolerate
            # a request for the previous nominal segment during manifest startup.
            first = max(0, int(position // _SEGMENT_SECONDS) - 2)
            params = {
                "path": "/library/metadata/" + self.source.metadata_key,
                "mediaIndex": "0",
                "partIndex": "0",
                "protocol": "dash",
                "directPlay": "0",
                "directStream": "1",
                "directStreamAudio": "0",
                "hasMDE": "1",
                "location": "lan",
                "audioChannelCount": "2",
                "videoQuality": "100",
                "maxVideoBitrate": "200000",
                "autoAdjustQuality": "0",
                "session": upstream_id,
                "transcodeSessionId": upstream_id,
                "subtitles": "none",
                "advancedSubtitles": "text",
                "X-Plex-Client-Profile-Extra": _PROFILE,
                "secondsPerSegment": str(_SEGMENT_SECONDS),
                "offset": f"{first * _SEGMENT_SECONDS}.0",
            }
            try:
                async with asyncio.timeout(22):
                    self.destination = await pin_destination(
                        self.source.base_url, transport=self.transport
                    )
                    async with self._client() as client:
                        _require_decision(
                            await self._xml(client, "decision", upstream_id, params), self.source
                        )
                        data = await self._xml(client, "start.mpd", upstream_id, params)
                    manifest, tracks, count = _manifest(
                        data, upstream_id, key, self.source.duration_ms / 1000
                    )
                    await authorize()
                    self.generation = Generation(upstream_id, key, manifest, tracks, first, count)
                    return self.generation
            except (httpx.HTTPError, TimeoutError, OSError, OutboundError):
                raise _fail() from None
            finally:
                if self.generation is None and self.destination is not None:
                    await self._stop(upstream_id)

    async def resource(self, generation, rep, asset, authorize, release):
        if generation is not self.generation or rep not in generation.representations:
            raise _fail("Media resource unavailable.", 404)
        if asset != "header":
            match = re.fullmatch(r"(0|[1-9][0-9]{0,5})\.m4s", asset)
            if (
                not match
                or not generation.first_segment <= int(match[1]) < generation.segment_count
            ):
                raise _fail("Media resource unavailable.", 404)

        client = self._client()
        response = None

        async def close():
            try:
                if response is not None:
                    await response.aclose()
                await client.aclose()
            finally:
                release()

        try:
            path = f"session/{generation.upstream_id}/{rep}/{asset}"
            response = await client.send(
                self._request(client, path, generation.upstream_id), stream=True
            )
            limit = _XML_LIMIT if asset == "header" else _SEGMENT_LIMIT
            declared = response.headers.get("content-length")
            if (
                response.status_code != 200
                or response.headers.get("content-encoding") not in (None, "identity")
                or declared is not None
                and (
                    not declared.isascii()
                    or not declared.isdecimal()
                    or len(declared) > 10
                    or not 0 < int(declared) <= limit
                )
            ):
                raise _fail()
            await authorize()

            async def body():
                received = 0
                async with asyncio.timeout(30):
                    async for block in response.aiter_raw():
                        received += len(block)
                        if received > limit or generation is not self.generation:
                            raise _fail()
                        yield block

            headers = {key: value for key, value in HEADERS.items() if key != "Accept-Ranges"}
            if declared is not None:
                headers["Content-Length"] = declared
            return _ClosingStream(
                body(),
                close=close,
                headers=headers,
                media_type=generation.representations[rep],
            )
        except BaseException as error:
            await close()
            if isinstance(error, (httpx.HTTPError, TimeoutError)):
                raise _fail() from None
            raise
