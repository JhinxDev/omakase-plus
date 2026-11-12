"""Translate the verified Plex HLS contract to a cookie-scoped tvOS stream."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from functools import partial

import httpx

from omakase.plus import native_tv_matroska as matroska
from omakase.plus.native_tv_captions import MAX_TRACKS, rendition_tags
from omakase.plus.outbound import OutboundError
from omakase.plus.web_playback_compatible import (
    _SEGMENT_LIMIT,
    _SEGMENT_SECONDS,
    CompatibleStream,
    _fail,
)
from omakase.plus.web_playback_delivery import DeliveryError

_MANIFEST_LIMIT = 128 * 1024
_MAX_SEGMENTS = 3000
_SEGMENT = re.compile(r"[0-9]{5}\.ts")
_PREFETCH_WINDOW = 8
_CAPTION_ASSET = re.compile(r"captions-([0-9]{1,2})(?:\.m3u8|-([0-9]{5})\.vtt)")
_PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
_TS_PACKET = 188
_TS_PROBE_LIMIT = 65536
_TRANSCODE_VIDEO_CODECS = {"h264", "hevc", "vc1"}


def eligible(source):
    # Unlike web video-copy delivery, this path asks Plex to encode H.264.
    return (
        os.getenv("OMAKASE_NATIVE_TV_PLEX_HLS") == "1"
        and 0 < source.duration_ms <= 14400000
        and source.container in {"mkv", "mp4"}
        and source.video_codec in _TRANSCODE_VIDEO_CODECS
        and getattr(source, "_single_plex_part", False)
        and bool(source.audio_codec)
        and (source.container != "mp4" or source.audio_codec not in {"aac", "ac3", "eac3"})
    )


def _lines(data: bytes) -> list[str]:
    if not data or len(data) > _MANIFEST_LIMIT:
        raise _fail()
    try:
        return data.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise _fail() from None


def _variant(data: bytes, upstream_id: str) -> str:
    lines = _lines(data)
    path = f"session/{upstream_id}/base/index.m3u8"
    if (
        len(lines) != 3
        or lines[0] != "#EXTM3U"
        or not lines[1].startswith("#EXT-X-STREAM-INF:")
        or lines[2] != path
    ):
        raise _fail()
    return path


def _playlist(data: bytes, expected_duration: float, first_segment: int = 0) -> tuple[bytes, int]:
    lines = _lines(data)
    if first_segment:
        offset = f"#EXT-X-START:TIME-OFFSET={first_segment * _SEGMENT_SECONDS:.6f}"
        if lines[2:3] != [offset]:
            raise _fail()
        del lines[2]
    if lines[:4] != [
        "#EXTM3U",
        "#EXT-X-TARGETDURATION:5",
        "#EXT-X-ALLOW-CACHE:NO",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ] or lines[-1:] != ["#EXT-X-ENDLIST"]:
        raise _fail()
    segments = lines[4:-1]
    count = len(segments) // 2
    if len(segments) % 2 or not 1 <= count <= _MAX_SEGMENTS:
        raise _fail()
    result = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:5",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    total = 0.0
    for number in range(count):
        duration, name = segments[2 * number : 2 * number + 2]
        match = re.fullmatch(r"#EXTINF:([0-9](?:\.[0-9]{1,6})?), *(?:nodesc)?", duration)
        if not match or name != f"{number:05d}.ts" or not 0 < float(match[1]) <= 5.5:
            raise _fail()
        total += float(match[1])
        result.extend((f"#EXTINF:{match[1]},", name))
    if abs(total - expected_duration) > 2:
        raise _fail()
    result.extend(("#EXT-X-ENDLIST", ""))
    return "\n".join(result).encode("ascii"), count


def _first_video_pts(data):
    # The verified Plex profile produces a single H.264 video stream in 188-byte TS packets.
    for position in range(0, len(data) - _TS_PACKET + 1, _TS_PACKET):
        packet = data[position : position + _TS_PACKET]
        if packet[0] != 0x47:
            break
        if packet[1] & 0xC0 != 0x40 or packet[3] & 0xD0 != 0x10:
            continue
        offset = 5 + packet[4] if packet[3] & 0x20 else 4
        pes = packet[offset:]
        if len(pes) < 14 or pes[:3] != b"\x00\x00\x01" or not 0xE0 <= pes[3] <= 0xEF:
            continue
        a, b, c, d, e = pes[9:14]
        if pes[7] & 0x80 and pes[8] >= 5 and (a & c & e & 1):
            return ((a & 14) << 29) | (b << 22) | ((c & 254) << 14) | (d << 7) | (e >> 1)
    return None


def _video_windows(manifest):
    elapsed = 0.0
    windows = []
    for line in manifest.splitlines():
        if line.startswith(b"#EXTINF:"):
            duration = line[8:-1].decode("ascii")
            start = round(elapsed)
            elapsed += float(duration) * 1000
            windows.append((duration, start, round(elapsed)))
    return windows


@dataclass(frozen=True, repr=False)
class NativeGeneration:
    upstream_id: str
    manifest: bytes
    first_segment: int
    segment_count: int


class NativeCompatibleStream(CompatibleStream):
    protocol = "hls"
    video_action = "transcode"
    profile = (
        "add-transcode-target(type=videoProfile&context=streaming&protocol=hls&"
        "container=mpegts&videoCodec=h264&audioCodec=aac&replace=true)"
    )

    def __init__(self, source, transport=None):
        super().__init__(source, transport)
        self.manifest = None
        self.frontier = 0
        self.seek_lock = asyncio.Lock()
        self.tracks = ()
        self.caption_clock = None
        self.caption_windows = []
        self.caption_cache = {}

    def _parameters(self, upstream_id, first):
        # A copy stream cuts on the source keyframes and Plex still advertises
        # nominal five-second segments. The encoder places its own keyframes and
        # keeps the seek timeline and bitrate predictable for AVPlayer.
        return super()._parameters(upstream_id, first) | {
            "directStream": "0",
            "videoResolution": "1920x1080",
            "maxVideoBitrate": "12000",
        }

    async def _load_generation(self, client, upstream_id, first, params):
        path = _variant(await self._xml(client, "start.m3u8", upstream_id, params), upstream_id)
        data = await self._xml(client, path, upstream_id, None)
        manifest, count = _playlist(data, self.source.duration_ms / 1000, first)
        if self.manifest is not None and manifest != self.manifest:
            raise _fail()
        return NativeGeneration(upstream_id, manifest, first, count)

    async def ready(self, authorize):
        generation = await self.open(0, authorize)
        self.manifest = generation.manifest
        if (
            os.getenv("OMAKASE_NATIVE_TV_INDEXED_CAPTIONS") == "1"
            and self.source.container == "mkv"
            and any(
                t.kind == "subtitle" and t.codec in {"srt", "subrip"} for t in self.source.streams
            )
        ):
            await self._prepare_captions(generation, authorize)

    async def _prepare_captions(self, generation, authorize):
        read = partial(
            matroska.read_part_range, self.source, authorize=authorize, transport=self.transport
        )
        try:
            async with asyncio.timeout(6):
                tracks = await matroska.indexed_tracks(
                    read, self.source.part_size, self.source.streams
                )
                if not tracks:
                    return
                clock = await self._initial_clock(generation)
                if clock is None:
                    return
        except (ValueError, httpx.HTTPError, OutboundError, TimeoutError, OSError):
            return
        self.tracks = tracks[:MAX_TRACKS]
        self.caption_clock = clock
        self.caption_windows = _video_windows(self.manifest)

    async def _initial_clock(self, generation):
        async with self._client() as client:
            path = f"session/{generation.upstream_id}/base/00000.ts"
            response = await client.send(
                self._request(client, path, generation.upstream_id), stream=True
            )
            try:
                if response.status_code != 200 or response.headers.get("content-encoding") not in (
                    None,
                    "identity",
                ):
                    return None
                sample = bytearray()
                async for block in response.aiter_raw(chunk_size=4096):
                    sample.extend(block[: _TS_PROBE_LIMIT - len(sample)])
                    if (clock := _first_video_pts(sample)) is not None:
                        return clock
                    if len(sample) >= _TS_PROBE_LIMIT:
                        break
            finally:
                await response.aclose()
        return None

    async def playlist_asset(self, asset, authorize):
        if asset == "index.m3u8":
            if not self.tracks:
                return self.manifest, _PLAYLIST_TYPE
            tags = rendition_tags(
                [
                    (track.stream, f"captions-{index}.m3u8")
                    for index, track in enumerate(self.tracks)
                ]
            )
            rows = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                *tags,
                '#EXT-X-STREAM-INF:BANDWIDTH=14500000,SUBTITLES="subtitles"',
                "video.m3u8",
                "",
            ]
            return "\n".join(rows).encode(), _PLAYLIST_TYPE
        if not self.tracks:
            return None
        if asset == "video.m3u8":
            return self.manifest, _PLAYLIST_TYPE
        match = _CAPTION_ASSET.fullmatch(asset)
        if not match or int(match[1]) >= len(self.tracks):
            return None
        track = int(match[1])
        if match[2] is None:
            rows = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:5",
                "#EXT-X-MEDIA-SEQUENCE:0",
                "#EXT-X-PLAYLIST-TYPE:VOD",
            ]
            for number, (duration, _, _) in enumerate(self.caption_windows):
                rows.extend((f"#EXTINF:{duration},", f"captions-{track}-{number:05d}.vtt"))
            return ("\n".join([*rows, "#EXT-X-ENDLIST", ""])).encode(), _PLAYLIST_TYPE
        number = int(match[2])
        if number >= len(self.caption_windows):
            raise _fail("Caption resource unavailable.", 404)
        key = (track, number)
        if key not in self.caption_cache:
            _, start, end = self.caption_windows[number]
            read = partial(
                matroska.read_part_range, self.source, authorize=authorize, transport=self.transport
            )
            try:
                async with asyncio.timeout(8):
                    payload = await matroska.fragment(
                        read,
                        self.source.part_size,
                        self.tracks[track],
                        start,
                        end,
                        self.caption_clock,
                    )
            except DeliveryError:
                raise
            except (ValueError, httpx.HTTPError, OutboundError, TimeoutError, OSError):
                raise _fail("Caption resource unavailable.", 404) from None
            if len(self.caption_cache) >= 24:
                self.caption_cache.pop(next(iter(self.caption_cache)))
            self.caption_cache[key] = payload
        return self.caption_cache[key], "text/vtt"

    async def segment(self, asset, authorize, release, acquire_seek):
        if not _SEGMENT.fullmatch(asset):
            raise _fail("Media resource unavailable.", 404)
        number = int(asset[:5])
        async with self.seek_lock:
            generation = self.generation
            if generation is None or number >= generation.segment_count:
                raise _fail("Media resource unavailable.", 404)
            # Plex encodes near the selected offset. A distant request must open a
            # new window rather than making Plex process the whole intervening file.
            if number < generation.first_segment or abs(number - self.frontier) > _PREFETCH_WINDOW:
                release_seek = acquire_seek()
                try:
                    generation = await self.open(number * _SEGMENT_SECONDS, authorize)
                finally:
                    release_seek()
                self.frontier = number
            else:
                self.frontier = max(self.frontier, number)
        path = f"session/{generation.upstream_id}/base/{asset}"
        return await self._deliver(
            generation, path, _SEGMENT_LIMIT, "video/mp2t", authorize, release
        )
