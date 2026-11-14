"""Bounded on-demand HLS for the Apple TV system player.

AVPlayer presents Apple's own controls but cannot demux Matroska. This module turns
one Real-Debrid original into a complete VOD HLS timeline and generates only the
short windows the player asks for.

A window is produced without downloading the title and without trusting the
demuxer's seek, which measurably lands a keyframe early on Matroska. Instead the
module reads the cue table, starts at the cluster that holds the requested
keyframe, writes the original EBML header, Info and Tracks elements followed by
that cluster with its leading blocks trimmed, and streams the following bytes to
ffmpeg. Video and audio are stream-copied; text subtitles come out of the same
window as WebVTT. Nothing is prepared per title and every buffer is capped.

One timeline origin per pass keeps picture, audio and cues on one clock: the
origin is the source presentation time of the first video keyframe, segment
boundaries are cue-index keyframe times, each fragmented segment's decode time is
patched to its absolute offset, and each WebVTT window is written relative to the
same segment start.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from omakase.plus.debrid_seek import (
    _CUES,
    _EBML,
    _SEGMENT,
    _children,
    _element,
    _timecode_scale,
    _uint,
)
from omakase.plus.playback_diagnostics import diagnostic_id, warn

_LOG = logging.getLogger(__name__)

_TRACKS = 0x1654AE6B
_INFO = 0x1549A966
_SEEKHEAD = 0x114D9B74
_CLUSTER = 0x1F43B675
_TIMESTAMP = 0xE7
_SIMPLE_BLOCK = 0xA3
_BLOCK_GROUP = 0xA0
_BLOCK = 0xA1
_REFERENCE = 0xFB
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_CUE_CLUSTER_POSITION = 0xF1
_CUE_RELATIVE_ID = 0xF0
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_LANGUAGE = 0x22B59C
_TRACK_NAME = 0x536E
_PIXEL_WIDTH = 0xB0
_PIXEL_HEIGHT = 0xBA
_DEFAULT_DURATION = 0x23E383
_UNKNOWN_SIZE = b"\x01\xff\xff\xff\xff\xff\xff\xff"

_VIDEO_CODECS = {
    "V_MPEG4/ISO/AVC": "h264",
    "V_MPEGH/ISO/HEVC": "hevc",
    "V_AV1": "av1",
    "V_MPEG2": "mpeg2",
}
_AUDIO_CODECS = {
    "A_AAC": "aac",
    "A_AC3": "ac3",
    "A_EAC3": "eac3",
    "A_MPEG/L3": "mp3",
    "A_DTS": "dts",
    "A_TRUEHD": "truehd",
    "A_FLAC": "flac",
    "A_OPUS": "opus",
    "A_VORBIS": "vorbis",
    "A_PCM/INT/LIT": "pcm_s16le",
}
_PLAYABLE_VIDEO = {"h264", "hevc"}
# Video is copied. Audio is copied when AVPlayer can decode it from fragmented
# MP4; only the other codecs are converted, and only that track.
_COPY_AUDIO = {"aac", "ac3", "eac3", "mp3"}

_DIRECT = re.compile(r"https?://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.real-debrid\.com/[^\s]{1,3000}")
_SEGMENT_SECONDS = 6.0
_MAX_SEGMENTS = 4000
_MAX_INDEX_BYTES = 12 * 1024 * 1024
_MAX_AUDIO_TRACKS = 4
_MAX_SUBTITLE_TRACKS = 6
# Bounds. The host runs about a quarter of a gibibyte above its 8 GiB reserve, so
# the spool is small on purpose: a few seconds of windows per session, never a
# title.
_SPOOL_TOTAL_LIMIT = 256 * 1024 * 1024
_SPOOL_PLAN_LIMIT = 128 * 1024 * 1024
_MAX_PLANS = 3
_MAX_PREFETCH = 4
# Windows generated before the playlist is answered, so the player starts with
# a buffer instead of asking for a window that does not exist yet.
_OPEN_WINDOWS = 2
_OPEN_WAIT = 45.0
_GENERATE_TIMEOUT = 120.0
_RESERVE_BYTES = 8 * 1024**3
_ADMISSION_MARGIN = 32 * 1024**2
_ABANDON_SECONDS = 240.0
_READ_CHUNK = 8 * 1024 * 1024
# Measured on the live link: one connection delivers about 0.9 MB/s whatever the
# request size, so a window is pulled over a few connections at once.
_READ_PARALLEL = 4
# A provider that keeps returning nothing is retried this many times, then fails.
_READ_ATTEMPTS = 4
# CueRelativePosition window starts are implemented and unit tested but off by
# default: they are not qualified until a live measurement shows saved bytes.
_CUE_RELATIVE_ENABLED = False
_CUE_RELATIVE_TOLERANCE_MS = 120
_CUE_HEAD_BYTES = 64 * 1024
_CUE_PROBE_BYTES = 4096
# One window never asks the provider for more than this, whatever the bitrate.
_WINDOW_MAX_BYTES = 128 * 1024 * 1024
# Bytes past the next segment's first cluster, enough for ffmpeg to finish the
# window it was asked for.
_WINDOW_TAIL_BYTES = 64 * 1024
# Continuous remux (option B). One forward FFmpeg session per playback reads the
# source from the last usable random access point and is never cut at a picture
# boundary, so the source frame sequence is preserved. Only a bounded window of
# fragments is retained: the pump pauses a few windows past the one the player
# asked for and older fragments are evicted, so peak disk is the retention window
# and never the episode.
_CONTINUOUS_FLAG = "OMAKASE_NATIVE_CONTINUOUS"
# One init that describes every session instead of only the first one.
_STABLE_INIT_FLAG = "OMAKASE_CONTINUOUS_STABLE_INIT"
# Windows produced ahead of the requested one before the pump pauses on demand.
_LEAD_WINDOWS = 4
# Windows kept behind the requested one for a small backward seek.
_KEEP_WINDOWS = 2
# A straggling request for a segment from before a seek must not drag the session
# back: a restart this soon after the last one is answered as a retry instead.
_SEEK_COOLDOWN = 4.0
# One fragment pair is buffered whole, so a broken size box cannot be buffered.
_FRAGMENT_MAX_BYTES = 48 * 1024 * 1024
_INIT_BOX_LIMIT = 4 * 1024 * 1024
_INIT_BOXES = {b"ftyp", b"moov", b"styp"}
# Fragmented output for the native player. ``negative_cts_offsets`` lets the muxer
# write signed composition offsets, so a stream whose first pictures are reordered
# needs no edit list and no picture has to be dropped to avoid a negative one.
_MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof+delay_moov+negative_cts_offsets"
# Audio is cut by the muxer before the playlist boundary can be known exactly, so
# it is fragmented at about one AAC frame and partitioned by its own decode time.
# Every fragment keeps an absolute clock, so the partition never moves a sample in
# time, and the boundary error stays inside a frame and a half.
_AUDIO_FRAGMENT_US = 25_000
# How far a fragment's earliest presentation may sit from the start of the window
# it is filed into. A fragment opens on a source keyframe, so the only legitimate
# distance is the reorder lead-in of that random access point; half a second is
# generous for that and far tighter than a window, so a cue table that disagrees
# with the keyframes fails instead of swapping two segments.
_FRAGMENT_SLACK = 0.5
_VIDEO_SEGMENT = re.compile(r"v([0-9]{5})\.m4s")
_AUDIO_SEGMENT = re.compile(r"a([0-9])-([0-9]{5})\.m4s")
_AUDIO_PLAYLIST = re.compile(r"audio([0-9])\.m3u8")
_AUDIO_INIT = re.compile(r"init-a([0-9])\.mp4")
_SUBTITLE_SEGMENT = re.compile(r"s([0-9])-([0-9]{5})\.vtt")
_SUBTITLE_PLAYLIST = re.compile(r"subtitles([0-9]{1,2})\.m3u8")


class NativeError(RuntimeError):
    """A window could not be produced. Never carries a URL or a token."""

    def __init__(self, code="native_failed"):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class NativeAudio:
    """One audio rendition: the demuxer ordinal, not the Matroska track number."""

    ordinal: int
    codec: str
    language: str = ""
    name: str = ""
    # The Matroska track number, kept so a session can anchor this rendition on the
    # source block its first fragment was built from.
    number: int = 0

    @property
    def copyable(self) -> bool:
        return self.codec in _COPY_AUDIO

    @property
    def playlist(self) -> str:
        return f"audio{self.ordinal}.m3u8"

    @property
    def init(self) -> str:
        return f"init-a{self.ordinal}.mp4"

    def segment(self, number: int) -> str:
        return f"a{self.ordinal}-{number:05d}.m4s"


@dataclass(frozen=True)
class NativeTimeline:
    """A complete VOD timeline. Every time is relative to ``origin``."""

    url: str
    duration: float
    origin: float
    segments: tuple[tuple[float, float], ...]
    clusters: tuple[int, ...]
    total: int
    header: bytes
    info: bytes
    tracks: bytes
    video_codec: str
    width: int = 0
    height: int = 0
    frame_rate: float | None = None
    audios: tuple[NativeAudio, ...] = ()
    bandwidth: int = 0
    video_number: int = 0
    # CueRelativePosition per window, when the cue table publishes one.
    relatives: tuple[int | None, ...] = ()

    @property
    def target_duration(self) -> int:
        return max(1, int(max(end - start for start, end in self.segments) + 0.999))


@dataclass
class NativePlan:
    key: str
    url: str
    duration: float
    root: Path
    subtitle_key: str = ""
    timeline: NativeTimeline | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    task: asyncio.Task | None = None
    windows: dict[int, asyncio.Task] = field(default_factory=dict)
    prefetch: asyncio.Task | None = None
    prefetch_start: int | None = None
    touched: float = field(default_factory=time.monotonic)
    diagnostic_id: str = field(default_factory=diagnostic_id)
    warm: float | None = None
    # Bytes reserved for windows that are queued or running, so concurrent
    # producers cannot both pass admission against the same free space.
    reserved_bytes: int = 0
    probed: bool = False
    # Continuous remux (option B): one forward session for this plan.
    continuous: bool = False
    session: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    seek_at: float = 0.0
    backward_at: float = 0.0

    def video_segment_path(self, number: int) -> Path:
        return self.root / f"v{number:05d}.m4s"

    def audio_segment_path(self, audio: NativeAudio, number: int) -> Path:
        return self.root / audio.segment(number)

    def subtitle_segment_path(self, track: int, number: int) -> Path:
        return self.root / f"s{track}-{number:05d}.vtt"


# --- Matroska parsing -------------------------------------------------------


def vint(data, position, keep_marker):
    first = data[position]
    length = 1
    mask = 0x80
    while length <= 8 and not (first & mask):
        mask >>= 1
        length += 1
    if length > 8 or position + length > len(data):
        raise ValueError("bad vint")
    value = first if keep_marker else first & (mask - 1)
    for offset in range(1, length):
        value = (value << 8) | data[position + offset]
    return value, position + length


def _encode_size(size):
    if size < 0x7F:
        return bytes([0x80 | size])
    for length in range(2, 9):
        if size < (1 << (7 * length)) - 1:
            out = bytearray(size.to_bytes(length, "big"))
            out[0] |= 1 << (8 - length)
            return bytes(out)
    raise ValueError("size")


def _encode_id(element_id):
    return element_id.to_bytes((element_id.bit_length() + 7) // 8, "big")


def _encode_element(element_id, payload):
    return _encode_id(element_id) + _encode_size(len(payload)) + payload


def _element_ranges(data, start, end, wanted):
    """Like ``_children`` but keeps each element's own start offset."""
    result = {}
    position = start
    while position < end:
        try:
            element_id, size, body = _element(data, position)
        except (IndexError, ValueError):
            break
        if size is None:
            size = end - body
        stop = body + size
        if stop > end or stop <= position:
            break
        if element_id in wanted:
            result.setdefault(element_id, []).append((position, stop))
        position = stop
    return result


def _block_time(raw):
    _track, after = vint(raw, 0, False)
    if after + 2 > len(raw):
        raise ValueError("block")
    return struct.unpack(">h", raw[after : after + 2])[0]


def _uint_field(data, fields, key):
    if key not in fields:
        return None
    return _uint(data, *fields[key][0])


def _text_field(data, fields, key, limit=64):
    if key not in fields:
        return ""
    start, stop = fields[key][0]
    try:
        return data[start:stop].decode("utf-8", "ignore").strip()[:limit]
    except (IndexError, ValueError):
        return ""


def _track_table(data, track_range):
    """Return the video description, the audio renditions and the video number."""
    if track_range is None:
        return None, [], None
    start, end = track_range
    video = None
    video_number = None
    audios = []
    ordinal = 0
    for entry_start, entry_end in _children(data, start, end, {_TRACK_ENTRY}).get(_TRACK_ENTRY, []):
        fields = _children(
            data,
            entry_start,
            entry_end,
            {
                _TRACK_NUMBER,
                _TRACK_TYPE,
                _CODEC_ID,
                _LANGUAGE,
                _TRACK_NAME,
                _PIXEL_WIDTH,
                _PIXEL_HEIGHT,
                _DEFAULT_DURATION,
            },
        )
        track_type = _uint_field(data, fields, _TRACK_TYPE)
        if track_type is None:
            continue
        codec = _text_field(data, fields, _CODEC_ID)
        if track_type == 1 and video is None:
            duration = _uint_field(data, fields, _DEFAULT_DURATION) or 0
            video = {
                "number": _uint_field(data, fields, _TRACK_NUMBER),
                "codec": _VIDEO_CODECS.get(codec, codec.lower()),
                "width": _uint_field(data, fields, _PIXEL_WIDTH) or 0,
                "height": _uint_field(data, fields, _PIXEL_HEIGHT) or 0,
                "frame_rate": round(1e9 / duration, 3) if duration else None,
            }
        elif track_type == 2:
            audios.append(
                NativeAudio(
                    ordinal=ordinal,
                    codec=_AUDIO_CODECS.get(codec, codec.lower()),
                    language=_text_field(data, fields, _LANGUAGE).lower(),
                    name=_text_field(data, fields, _TRACK_NAME, 40),
                    number=_uint_field(data, fields, _TRACK_NUMBER) or 0,
                )
            )
            ordinal += 1
    if video is not None:
        video_number = video.get("number")
    return video, audios, video_number


def _cue_points(data, start, end, video_number, scale):
    """Every video cue as (seconds, cluster offset, relative offset or None)."""
    points = []
    for point_start, point_end in _children(data, start, end, {_CUE_POINT}).get(_CUE_POINT, []):
        fields = _children(data, point_start, point_end, {_CUE_TIME, _CUE_TRACK_POSITIONS})
        if _CUE_TIME not in fields:
            continue
        tracks = []
        cluster = None
        relative = None
        for track_start, track_end in fields.get(_CUE_TRACK_POSITIONS, []):
            inner = _children(
                data, track_start, track_end, {_CUE_TRACK, _CUE_CLUSTER_POSITION, _CUE_RELATIVE_ID}
            )
            if _CUE_TRACK in inner:
                tracks.append(_uint(data, *inner[_CUE_TRACK][0]))
            if _CUE_CLUSTER_POSITION in inner:
                cluster = _uint(data, *inner[_CUE_CLUSTER_POSITION][0])
            if _CUE_RELATIVE_ID in inner:
                relative = _uint(data, *inner[_CUE_RELATIVE_ID][0])
        if video_number is not None and tracks and video_number not in tracks:
            continue
        if cluster is None:
            continue
        try:
            value = _uint(data, *fields[_CUE_TIME][0]) * scale / 1e9
        except (IndexError, ValueError):
            continue
        if value >= 0:
            points.append((value, cluster, relative))
    points.sort()
    return points


def _window_index(points, duration, origin, segment_base):
    """Group cue points into windows: each returned boundary is a real keyframe."""
    usable = [
        (value, cluster, relative)
        for value, cluster, relative in points
        if 0 <= value < duration - 0.2
    ]
    if len(usable) < 2:
        return (), (), ()
    segments = []
    clusters = []
    relatives = []
    start, cluster, relative = usable[0]
    for value, next_cluster, next_relative in usable[1:]:
        if value - start >= _SEGMENT_SECONDS:
            segments.append((start, value))
            clusters.append(segment_base + cluster)
            relatives.append(relative)
            start, cluster, relative = value, next_cluster, next_relative
    if duration - start > 0.2:
        segments.append((start, duration))
        clusters.append(segment_base + cluster)
        relatives.append(relative)
    if not 2 <= len(segments) <= _MAX_SEGMENTS:
        return (), (), ()
    relative_times = tuple((round(a - origin, 3), round(b - origin, 3)) for a, b in segments)
    return relative_times, tuple(clusters), tuple(relatives)


_CONTENT_RANGE = re.compile(r"\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*")


def _content_range_start(value):
    match = _CONTENT_RANGE.fullmatch(value or "")
    if not match:
        return None
    first, last = int(match[1]), int(match[2])
    return first if first <= last else None


async def _range(client, url, start, end, *, limit=None):
    """Read exactly one byte range, or None.

    A provider that ignores ``Range`` must never make this process buffer a whole
    title, so the response is streamed and refused unless it is a 206 whose
    ``Content-Range`` starts where it was asked to and whose body is no longer
    than the requested window. A truncated body is refused as well: index bytes
    that are silently short would shift every parsed offset.
    """
    limit = _MAX_INDEX_BYTES if limit is None else min(limit, _MAX_INDEX_BYTES)
    if end < start or end - start + 1 > limit:
        return None
    want = end - start + 1
    headers = {"Range": f"bytes={start}-{end}"}
    try:
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code != 206:
                return None
            if _content_range_start(response.headers.get("content-range")) != start:
                return None
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > want:
                        return None
                except ValueError:
                    return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > want:
                    return None
            if len(body) != want:
                return None
            return bytes(body)
    except (httpx.HTTPError, NativeError):
        return None


async def _tail_cues(client, url, total):
    """Locate a Cues element in the last few megabytes of the file."""
    window = min(total, 4 * 1024 * 1024)
    tail = await _range(client, url, total - window, total - 1)
    if not tail:
        return None
    marker = b"\x1c\x53\xbb\x6b"
    position = tail.rfind(marker)
    if position < 0:
        return None
    # The element header is at the marker; make sure it parses as a bounded Cues.
    try:
        element_id, size, _body = _element(tail, position)
    except (IndexError, ValueError):
        return None
    if element_id != _CUES or not size or size > _MAX_INDEX_BYTES:
        return None
    if position + size > window:
        return None
    return total - window + position


async def build_timeline(url, duration):
    """Read the container index and return a complete timeline, or None.

    Only a few range reads happen here: the EBML header, the track list and the
    cue table. Opening a two-hour title costs kilobytes and proves whether the
    source can be remuxed at all.
    """
    if not _DIRECT.fullmatch(url or ""):
        return None
    if not isinstance(duration, (int, float)) or not 1 <= duration <= 43_200:
        return None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False
    ) as client:
        try:
            probe = await client.head(url)
            if probe.status_code != 200:
                return None
            total = int(probe.headers["content-length"])
        except (httpx.HTTPError, KeyError, ValueError):
            return None
        if total < 1024:
            return None
        limit = min(1024 * 1024, total)
        first = await _range(client, url, 0, limit - 1)
        if not first or first[:4] != b"\x1a\x45\xdf\xa3":
            return None
        try:
            element_id, size, body = _element(first, 0)
            if element_id != _EBML or not size:
                return None
            header = first[0 : body + size]
        except (IndexError, ValueError):
            return None
        segment_start = None
        position = len(header)
        while position < len(first):
            try:
                element_id, size, body = _element(first, position)
            except (IndexError, ValueError):
                break
            if element_id == _SEGMENT:
                segment_start = body
                break
            if size is None:
                break
            position = body + size
        if segment_start is None:
            return None
        wanted = _children(first, segment_start, len(first), {_TRACKS, _INFO, _SEEKHEAD})
        tracks_range = wanted.get(_TRACKS, [None])[0]
        info_range = wanted.get(_INFO, [None])[0]
        seekhead_range = wanted.get(_SEEKHEAD, [None])[0]
        if tracks_range is None or info_range is None:
            return None
        elements = _element_ranges(first, segment_start, len(first), {_TRACKS, _INFO})
        tracks_bytes = first[elements[_TRACKS][0][0] : elements[_TRACKS][0][1]]
        info_bytes = first[elements[_INFO][0][0] : elements[_INFO][0][1]]
        video, audios, video_number = _track_table(first, tracks_range)
        if video is None or video["codec"] not in _PLAYABLE_VIDEO or video_number is None:
            return None
        if not audios:
            # A silent episode is not a success: without a readable audio track the
            # system player would show picture and no sound, which is exactly the
            # failure this route must never present as working playback.
            return None
        scale = _timecode_scale(first, info_range) or 1_000_000
        cues_position = None
        if seekhead_range is not None:
            for seek_start, seek_end in _children(
                first, seekhead_range[0], seekhead_range[1], {0x4DBB}
            ).get(0x4DBB, []):
                inner = _children(first, seek_start, seek_end, {0x53AB, 0x53AC})
                if 0x53AB in inner and 0x53AC in inner:
                    seek_id = int.from_bytes(
                        first[inner[0x53AB][0][0] : inner[0x53AB][0][1]], "big"
                    )
                    if seek_id == _CUES:
                        cues_position = segment_start + _uint(first, *inner[0x53AC][0])
        if cues_position is None:
            # Some muxers write the cue table without a seek-head entry. Look in
            # the tail of the file instead of refusing a perfectly good source.
            cues_position = await _tail_cues(client, url, total)
        if cues_position is None:
            return None
        cues_header = await _range(client, url, cues_position, min(cues_position + 31, total - 1))
        if not cues_header:
            return None
        try:
            element_id, size, body = _element(cues_header, 0)
        except (IndexError, ValueError):
            return None
        if element_id != _CUES or not size or size > _MAX_INDEX_BYTES:
            return None
        last = min(cues_position + body + size - 1, total - 1)
        cues = await _range(client, url, cues_position, last)
        if not cues or len(cues) < body + size:
            return None
        points = _cue_points(cues, body, body + size, video_number, scale)
        if len(points) < 2:
            return None
        origin = points[0][0]
        segments, clusters, relatives = _window_index(
            points, float(duration), origin, segment_start
        )
        if not segments:
            return None
        return NativeTimeline(
            url=url,
            duration=float(duration),
            origin=round(origin, 3),
            segments=segments,
            clusters=clusters,
            total=total,
            header=header,
            info=info_bytes,
            tracks=tracks_bytes,
            video_codec=video["codec"],
            width=video["width"],
            height=video["height"],
            frame_rate=video["frame_rate"],
            audios=tuple(audios[:_MAX_AUDIO_TRACKS]),
            video_number=int(video_number),
            relatives=relatives,
            bandwidth=max(1, int(total * 8 / float(duration))),
        )


def _rasl_nals(block):
    """True when a video block is a leading RASL picture (HEVC NAL types 8, 9).

    Matroska stores HEVC length prefixed, so the units are walked by their four
    byte lengths rather than by Annex-B start codes. Open-GOP HEVC marks its six
    second boundaries with CRA pictures, and the RASL pictures that follow them
    reference the previous GOP; a player entering at the CRA must discard them,
    and while they are forwarded both ffmpeg and AVPlayer report missing
    references, which is the visible video stutter.
    """
    index = 0
    while index + 5 <= len(block):
        size = int.from_bytes(block[index : index + 4], "big")
        if size <= 0 or index + 4 + size > len(block):
            return False
        nal = (block[index + 4] >> 1) & 0x3F
        if nal in (8, 9):
            return True
        if nal < 32:
            return False
        index += 4 + size
    return False


def _window_frames(timeline, span):
    """Frames in a window, so video is cut by count and never overlaps a neighbour."""
    rate = float(getattr(timeline, "frame_rate", 0) or 0)
    if rate <= 0:
        return max(1, int(round(span * 24)))
    return max(1, int(round(span * rate)))


def _cluster_header(data):
    """(cluster timestamp ms, payload start) for the Cluster at the front of data."""
    try:
        element_id, size, body = _element(data, 0)
    except (IndexError, ValueError):
        return None, None
    if element_id != _CLUSTER:
        return None, None
    stop = len(data) if size is None else min(len(data), body + size)
    timestamp = None
    try:
        children = _children(data, body, stop, {_TIMESTAMP})
    except (IndexError, ValueError):
        return None, body
    if _TIMESTAMP in children:
        begin, finish = children[_TIMESTAMP][0]
        timestamp = int.from_bytes(data[begin:finish], "big")
    return timestamp, body


def _candidate_offsets(cluster_start, payload_start, relative):
    """Two readings of CueRelativePosition, payload-relative (spec) first."""
    return (payload_start + relative, cluster_start + relative)


def _first_block_timing(data, video_number):
    """(track, relative ms, is_key) for the block at the front of data, or None."""
    try:
        element_id, after = vint(data, 0, True)
        size, body = vint(data, after, False)
    except (IndexError, ValueError):
        return None
    if element_id not in {_SIMPLE_BLOCK, _BLOCK_GROUP} or not size:
        return None
    # A keyframe block is far larger than any probe; only its header is needed,
    # so the declared size is checked for presence, not for being buffered.
    payload = data[body:]
    is_key = True
    if element_id == _BLOCK_GROUP:
        inner = _children(data, body, min(len(data), body + size), {_BLOCK, _REFERENCE})
        if _BLOCK not in inner:
            return None
        begin, finish = inner[_BLOCK][0]
        payload = data[begin:finish]
        is_key = _REFERENCE not in inner
    try:
        track, after_track = vint(payload, 0, False)
        relative = int.from_bytes(payload[after_track : after_track + 2], "big", signed=True)
        flags = payload[after_track + 2] if len(payload) > after_track + 2 else 0
    except (IndexError, ValueError):
        return None
    if element_id == _SIMPLE_BLOCK:
        is_key = bool(flags & 0x80)
    if video_number and track != video_number:
        return None
    return track, relative, is_key


# --- ISO-BMFF helpers -------------------------------------------------------


def _boxes(data, start, end):
    position = start
    while position + 8 <= end:
        size = struct.unpack(">I", data[position : position + 4])[0]
        kind = data[position + 4 : position + 8]
        if size == 1:
            if position + 16 > end:
                return
            size = struct.unpack(">Q", data[position + 8 : position + 16])[0]
            body = position + 16
        elif size == 0:
            size = end - position
            body = position + 8
        else:
            body = position + 8
        if size < 8 or position + size > end:
            return
        yield kind, position, body, size
        position += size


def split_init(data):
    """Return (initialisation segment, first media segment) from a fragment."""
    init = bytearray()
    position = 0
    for kind, start, _body, size in _boxes(data, 0, len(data)):
        if kind in {b"ftyp", b"moov", b"styp"}:
            init.extend(data[start : start + size])
            position = start + size
            continue
        break
    return bytes(init), data[position:]


def media_timescale(init):
    """The track timescale of an initialisation segment, or None."""
    for kind, _start, body, size in _boxes(init, 0, len(init)):
        if kind != b"moov":
            continue
        for kind2, _start2, body2, size2 in _boxes(init, body, _payload_end(init, body, size)):
            if kind2 != b"trak":
                continue
            for kind3, _start3, body3, size3 in _boxes(init, body2, _payload_end(init, body2, size2)):
                if kind3 != b"mdia":
                    continue
                for kind4, _start4, body4, _size4 in _boxes(
                    init, body3, _payload_end(init, body3, size3)
                ):
                    if kind4 != b"mdhd":
                        continue
                    version = init[body4]
                    offset = body4 + 4 + (16 if version == 1 else 8)
                    try:
                        return struct.unpack(">I", init[offset : offset + 4])[0]
                    except struct.error:
                        return None
    return None


def patch_decode_time(data, base):
    """Set every ``tfdt`` decode time in a fragmented segment to ``base``.

    The muxer writes zero for every standalone fragment, so without this patch a
    multi-segment playlist would claim that every window starts at the beginning.
    """
    if base < 0:
        raise ValueError("decode time")
    out = bytearray(data)
    patched = 0
    for kind, _start, body, size in _boxes(data, 0, len(data)):
        if kind != b"moof":
            continue
        for kind2, _start2, body2, size2 in _boxes(data, body, _payload_end(data, body, size)):
            if kind2 != b"traf":
                continue
            for kind3, _start3, body3, _size3 in _boxes(data, body2, _payload_end(data, body2, size2)):
                if kind3 != b"tfdt":
                    continue
                if data[body3] == 1:
                    struct.pack_into(">Q", out, body3 + 4, base)
                else:
                    struct.pack_into(">I", out, body3 + 4, min(base, 0xFFFFFFFF))
                patched += 1
    if not patched:
        raise ValueError("no tfdt")
    return bytes(out)


# --- continuous remux (option B) --------------------------------------------


def continuous_enabled():
    return os.environ.get(_CONTINUOUS_FLAG) == "1"


def stable_init_enabled():
    """One init that describes every session, without rewriting muxer bytes.

    The muxer records where a session sits on the source clock in the
    initialisation segment's edit list, and HLS names one init for the whole pass.
    Patching that list after the fact changes the media-to-presentation mapping
    the muxer chose, which is why this release stopped doing it: an edit list is
    how the muxer compensates for reorder delay, and zeroing it makes the picture
    present late and silently drops the pictures whose composition offsets are
    negative.

    Instead the session feeds every muxer a feed-relative clock and asks for
    signed composition offsets, so the list the muxer writes is the identity and
    comes out the same for every start. This flag selects that configuration.
    ``OMAKASE_CONTINUOUS_STABLE_INIT=0`` is the rollback: the muxer keeps the
    absolute clock, writes its own edit list, and the session applies it.
    """
    return os.environ.get(_STABLE_INIT_FLAG, "1") != "0"


def edit_entries(init):
    """(version, movie timescale, entries) from an initialisation edit list.

    An ISO-BMFF edit list entry is ``segment_duration``, ``media_time`` and a
    16.16 fixed-point ``media_rate``, so entries advance by ``2 * width + 4``
    bytes. Reading only the two width fields is the defect that used to corrupt
    the list this module patched: the next entry started four bytes early, which
    zeroed the previous entry's rate and left the following entry's media time
    untouched.

    Truncated or malformed input returns only the entries that were complete, so
    a caller refuses instead of guessing.
    """
    for kind, _start, body, size in _boxes(init, 0, len(init)):
        if kind != b"moov":
            continue
        movie_ts = 1000
        for kind2, _start2, body2, size2 in _boxes(init, body, _payload_end(init, body, size)):
            if kind2 == b"mvhd":
                movie_ts = _mvhd_timescale(init, body2)
                continue
            if kind2 != b"trak":
                continue
            for kind3, _start3, body3, size3 in _boxes(
                init, body2, _payload_end(init, body2, size2)
            ):
                if kind3 != b"edts":
                    continue
                for kind4, _start4, body4, size4 in _boxes(
                    init, body3, _payload_end(init, body3, size3)
                ):
                    if kind4 != b"elst":
                        continue
                    version = init[body4]
                    width = 8 if version == 1 else 4
                    count = int.from_bytes(init[body4 + 4 : body4 + 8], "big")
                    stride = width * 2 + 4
                    end = _payload_end(init, body4, size4)
                    at = body4 + 8
                    entries = []
                    for _ in range(count):
                        if at + stride > end:
                            return version, movie_ts, entries
                        duration = int.from_bytes(init[at : at + width], "big")
                        media_time = int.from_bytes(
                            init[at + width : at + width * 2], "big", signed=True
                        )
                        rate = int.from_bytes(
                            init[at + width * 2 : at + width * 2 + 2], "big", signed=True
                        )
                        fraction = int.from_bytes(
                            init[at + width * 2 + 2 : at + width * 2 + 4], "big", signed=True
                        )
                        entries.append(
                            {
                                "duration": duration,
                                "media_time": media_time,
                                "media_rate": (rate, fraction),
                            }
                        )
                        at += stride
                    return version, movie_ts, entries
    return 0, 1000, []


def init_edits(init):
    """The edit list an initialisation segment declares, as a plain report."""
    version, movie_ts, entries = edit_entries(init)
    return {"version": version, "movie_timescale": movie_ts, "entries": entries}


def edits_are_identity(init):
    """True when the init maps media time to presentation time unchanged.

    No edit list, one entry with media time 0 and rate 1.0, and leading empty
    edits of zero duration all mean the same thing. Anything else states that
    presentation is deliberately offset from media time, which the one init a
    playlist can name cannot express for a session that started elsewhere.
    """
    _version, _movie_ts, entries = edit_entries(init)
    for entry in entries:
        if entry["media_time"] == -1:
            if entry["duration"]:
                return False
            continue
        if entry["media_time"] != 0:
            return False
        if entry["media_rate"] != (1, 0):
            return False
    return True


async def _read_exact_stream(stream, count):
    data = bytearray()
    while len(data) < count:
        part = await stream.read(count - len(data))
        if not part:
            break
        data.extend(part)
    return bytes(data)


async def _read_box(stream):
    """One whole ISO-BMFF box from a child's stdout, or None at end of stream."""
    header = await _read_exact_stream(stream, 8)
    if len(header) < 8:
        return None
    size = struct.unpack(">I", header[:4])[0]
    kind = header[4:8]
    prefix = header
    offset = 8
    if size == 1:
        large = await _read_exact_stream(stream, 8)
        if len(large) < 8:
            return None
        size = struct.unpack(">Q", large)[0]
        prefix, offset = header + large, 16
    if size < offset or size > _FRAGMENT_MAX_BYTES:
        # A live pipe has no end of file, so size zero is never usable here.
        raise NativeError("box_size")
    body = await _read_exact_stream(stream, size - offset)
    if len(body) < size - offset:
        return None
    return kind, prefix + body


def fragment_bases(data):
    """(payload offset, byte width, decode time) for every tfdt in a fragment."""
    found = []
    for kind, _start, body, size in _boxes(data, 0, len(data)):
        if kind != b"moof":
            continue
        for kind2, _start2, body2, size2 in _boxes(data, body, _payload_end(data, body, size)):
            if kind2 != b"traf":
                continue
            for kind3, _start3, body3, _size3 in _boxes(data, body2, _payload_end(data, body2, size2)):
                if kind3 != b"tfdt":
                    continue
                width = 8 if data[body3] == 1 else 4
                at = body3 + 4
                found.append((at, width, int.from_bytes(data[at : at + width], "big")))
    return found


def _mvhd_timescale(init, body):
    version = init[body]
    at = body + 20 if version == 1 else body + 12
    try:
        return struct.unpack(">I", init[at : at + 4])[0] or 1000
    except struct.error:
        return 1000


def edit_shift(init):
    """Seconds between media time and presentation time, from the edit list.

    The muxer answers a stream whose first packets are reordered with an edit
    list: an empty edit hides the delay the decoder needs and the real edit names
    the media time of the first shown picture. This is the rollback reading, used
    when ``OMAKASE_CONTINUOUS_STABLE_INIT=0`` asks the muxer for an absolute
    clock and the session applies its list.
    """
    if not init:
        return 0.0
    media_ts = media_timescale(init) or 1000
    _version, movie_ts, entries = edit_entries(init)
    empty = 0
    for entry in entries:
        if entry["media_time"] == -1:
            empty += entry["duration"]
            continue
        return empty / movie_ts - entry["media_time"] / media_ts
    return empty / movie_ts


def _payload_end(data, body, size):
    """Where a box's payload really ends.

    ``_boxes`` yields the box's total size, header included, so a payload spans
    ``[body, body + size - 8)``. Using ``body + size`` reads eight bytes past the
    box and, for the last box in a buffer, raises instead of parsing.
    """
    return min(len(data), body + max(0, size - 8))


def _trun_lowest_offset(data, body, size):
    version = data[body]
    flags = int.from_bytes(data[body + 1 : body + 4], "big")
    count = int.from_bytes(data[body + 4 : body + 8], "big")
    at = body + 8
    if flags & 0x000001:
        at += 4
    if flags & 0x000004:
        at += 4
    if not flags & 0x000800:
        return 0
    fixed = 0
    for bit in (0x000100, 0x000200, 0x000400):
        if flags & bit:
            fixed += 4
    limit = _payload_end(data, body, size)
    stride = fixed + 4
    lowest = None
    for index in range(count):
        offset = at + stride * index + fixed
        if offset + 4 > limit:
            break
        value = int.from_bytes(data[offset : offset + 4], "big", signed=version == 1)
        lowest = value if lowest is None else min(lowest, value)
    return lowest if lowest is not None else 0


def minimum_composition_offset(data):
    """Smallest composition offset among a fragment's samples, in its timescale.

    The picture a fragment presents first is not necessarily its first sample:
    open-GOP fragments begin with leading pictures that are shown before the
    keyframe they follow. The smallest offset is where the fragment really
    starts on the presentation timeline, so that is what decides its window.
    """
    for kind, _start, body, size in _boxes(data, 0, len(data)):
        if kind != b"moof":
            continue
        for kind2, _start2, body2, size2 in _boxes(data, body, _payload_end(data, body, size)):
            if kind2 != b"traf":
                continue
            lowest = None
            for kind3, _start3, body3, size3 in _boxes(data, body2, _payload_end(data, body2, size2)):
                if kind3 != b"trun":
                    continue
                value = _trun_lowest_offset(data, body3, size3)
                lowest = value if lowest is None else min(lowest, value)
            if lowest is not None:
                return lowest
    return 0


def first_composition_offset(data):
    """Composition offset of the first sample in a fragment, in its timescale.

    Kept as a measurement tool for the qualification: it says how far the first
    decoded sample runs before the picture the player shows. The session itself
    does not need it, because the muxer writes an edit list for the same offset
    and the measured first presentation time is already the cue plus the source's
    own container start delay.
    """
    for kind, _start, body, size in _boxes(data, 0, len(data)):
        if kind != b"moof":
            continue
        for kind2, _start2, body2, size2 in _boxes(data, body, _payload_end(data, body, size)):
            if kind2 != b"traf":
                continue
            for kind3, _start3, body3, size3 in _boxes(data, body2, _payload_end(data, body2, size2)):
                if kind3 != b"trun":
                    continue
                version = data[body3]
                flags = int.from_bytes(data[body3 + 1 : body3 + 4], "big")
                position = body3 + 4
                if flags & 0x000001:
                    position += 4
                if flags & 0x000004:
                    position += 4
                if flags & 0x000100:
                    position += 4
                if flags & 0x000200:
                    position += 4
                if flags & 0x000400:
                    position += 4
                if not flags & 0x000800:
                    return 0
                if position + 4 > _payload_end(data, body3, size3):
                    return 0
                return int.from_bytes(
                    data[position : position + 4], "big", signed=version == 1
                )
    return 0


def _trun_samples(data, body, size):
    """Per-sample (duration or None, composition offset) for one ``trun``."""
    version = data[body]
    flags = int.from_bytes(data[body + 1 : body + 4], "big")
    count = int.from_bytes(data[body + 4 : body + 8], "big")
    at = body + 8
    if flags & 0x000001:
        at += 4
    if flags & 0x000004:
        at += 4
    fixed = 0
    for bit in (0x000100, 0x000200, 0x000400):
        if flags & bit:
            fixed += 4
    has_offset = bool(flags & 0x000800)
    stride = fixed + (4 if has_offset else 0)
    end = _payload_end(data, body, size)
    samples = []
    for index in range(count):
        position = at + stride * index
        if position + stride > end:
            return None
        duration = None
        if flags & 0x000100:
            duration = int.from_bytes(data[position : position + 4], "big")
            position += 4
        if flags & 0x000200:
            position += 4
        if flags & 0x000400:
            position += 4
        offset = 0
        if has_offset:
            offset = int.from_bytes(
                data[position : position + 4], "big", signed=version == 1
            )
        samples.append((duration, offset))
    return samples


def fragment_presentation(data):
    """(decode time, composition offset) for every sample of a fragment.

    Decode times advance by each sample's own duration, falling back to the track
    fragment header's default duration, so a pair is the sample's real decode time
    rather than an assumption that every sample lasts one frame. Truncated input
    yields the samples that were complete.
    """
    for kind, _start, body, size in _boxes(data, 0, len(data)):
        if kind != b"moof":
            continue
        samples = []
        for kind2, _start2, body2, size2 in _boxes(data, body, _payload_end(data, body, size)):
            if kind2 != b"traf":
                continue
            default = None
            decode_time = None
            truns = []
            for kind3, _start3, body3, size3 in _boxes(
                data, body2, _payload_end(data, body2, size2)
            ):
                if kind3 == b"tfhd":
                    flags = int.from_bytes(data[body3 + 1 : body3 + 4], "big")
                    position = body3 + 8
                    if flags & 0x000001:
                        position += 8
                    if flags & 0x000002:
                        position += 4
                    if flags & 0x000008 and position + 4 <= _payload_end(data, body3, size3):
                        default = int.from_bytes(data[position : position + 4], "big")
                elif kind3 == b"tfdt":
                    width = 8 if data[body3] == 1 else 4
                    decode_time = int.from_bytes(
                        data[body3 + 4 : body3 + 4 + width], "big"
                    )
                elif kind3 == b"trun":
                    truns.append(_trun_samples(data, body3, size3))
            if decode_time is None:
                continue
            dts = decode_time
            for trun in truns:
                if trun is None:
                    return samples
                for duration, offset in trun:
                    step = duration if duration is not None else default
                    if step is None:
                        return samples
                    samples.append((dts, offset))
                    dts += step
        return samples
    return []


def earliest_presentation(data):
    """min(DTS_i + CTS_i) over a fragment, in its timescale units.

    This is the definition of earliest presentation. The smallest composition
    offset added to the fragment's base is a different number: with reordered
    pictures the earliest presented sample is often not the first sample, and on
    this material it is two frames later than the base, so the old approximation
    put every window 0.084 s off its own clock.
    """
    pairs = fragment_presentation(data)
    if not pairs:
        return 0
    return min(decode + offset for decode, offset in pairs)


def shift_decode_times(data, extra):
    """Add ``extra`` to every ``tfdt`` of a fragment, keeping its own spacing.

    One continuous session numbers its output from the cue it started on. Adding
    the playlist time of that cue puts every fragment on the pass timeline, so a
    restart elsewhere does not move any sample.
    """
    bases = fragment_bases(data)
    if not bases:
        raise ValueError("no_tfdt")
    out = bytearray(data)
    for at, width, value in bases:
        moved = value + extra
        if moved < 0 or moved >= 1 << (width * 8):
            raise ValueError("decode_time")
        out[at : at + width] = moved.to_bytes(width, "big")
    return bytes(out)


class _Collector:
    """A stdin-shaped sink that keeps bytes, so one head can feed every consumer."""

    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        return None

    def is_closing(self):
        return False


class _ContinuousTrack:
    """One FFmpeg consumer of a session's shared byte stream."""

    def __init__(self, kind, ordinal=0, audio=None):
        self.kind = kind
        self.ordinal = ordinal
        self.audio = audio
        self.process = None
        self.init = b""
        self.timescale = None
        self.window = -1
        self.target = None
        self.frontier = -1
        self.failed = False
        self.fragments = 0
        self.tail = None
        # Presentation minus media time, from this track's own edit list, and from
        # the init the playlist actually pins (they differ for a later session).
        self.shift = 0.0
        self.pinned_shift = 0.0


class _ContinuousSession:
    """One forward remux, from the last usable random access point.

    ``start`` is the playlist window whose cue keyframe the source is trimmed to,
    so the first block the demuxer sees is the keyframe itself. Nothing after that
    is cut: the muxer emits every following picture in source order, which is what
    keeps the frame sequence intact where per-window cutting lost four frames at
    every CRA. Each fragment is filed into the playlist window its own decode time
    belongs to, and every fragment keeps an absolute clock, so a restart at another
    cue lands on the same timeline the playlist already declares.
    """

    def __init__(self, streams, plan, start):
        self.streams = streams
        self.plan = plan
        self.timeline = plan.timeline
        count = len(self.timeline.segments)
        self.start = max(0, min(int(start), count - 1))
        # The feed begins one cue before the window the session opens on. An
        # open-GOP CRA cannot be entered cold: its leading pictures reference the
        # previous GOP, so a muxer given a stream that starts at the CRA drops
        # them and presents the first picture five frames late. Reading from the
        # cue before it preserves the source relationship and lands the first
        # picture of the window where the playlist says it is.
        self.feed_start = max(0, self.start - 1)
        self.tracks: list[_ContinuousTrack] = []
        # The highest window the player has asked for.
        self.active = self.start
        self.peak_bytes = 0
        self.error = None
        self.closed = False
        self.feed_done = False
        self.readers_done = 0
        self.demand = asyncio.Event()
        self.pump: asyncio.Task | None = None
        self.tasks: list[asyncio.Task] = []
        self.metrics = {"bytes": 0, "read_ms": 0, "write_ms": 0, "windows": 0}
        # Presentation anchoring: where each track's first forwarded block sits on
        # the source timeline, reported by the head writer, then the one constant
        # that puts the muxer's own clock back on that timeline. Shared by every
        # track so a restart lands on the same source time and A/V stays put.
        self.feed_first: dict[int, int] = {}
        # Seconds of common delay the whole session needs, because a decode time
        # cannot be written below zero. Held in seconds, not in any one track's
        # timescale: the video and audio muxers use different timescales, and
        # adding a number of video ticks to audio moved the sound against the
        # picture by 113 ms on the measured pass.
        self.clock_delay = 0.0
        # The pass origin the picture lands on, in seconds, before any track is
        # anchored: sound and subtitles are positioned relative to it.
        self.origin: float | None = None
        self.video_base = None
        self.audio_base = None
        self.clock_ready = asyncio.Event()

    # -- state --------------------------------------------------------------

    @property
    def frontier(self):
        media = [track.frontier for track in self.tracks if track.kind != "subtitle"]
        return min(media) if media else self.start - 1

    def ready(self, number):
        """Every media rendition of this window is on disk."""
        try:
            if self.plan.video_segment_path(number).stat().st_size <= 16:
                return False
            for audio in self.timeline.audios:
                if self.plan.audio_segment_path(audio, number).stat().st_size <= 16:
                    return False
        except OSError:
            return False
        return True

    def can_serve(self, number):
        """True when continuing this session is a better answer than restarting."""
        if self.closed or self.error is not None:
            return False
        if number < self.start:
            return False
        if number > self.active + _LEAD_WINDOWS:
            return False
        # A window already passed that is no longer on disk has to be remade, and
        # the session cannot walk backwards.
        return not (number <= self.frontier and not self.ready(number))

    def fail(self, code):
        if self.error is None:
            self.error = code or "generation_failed"
            warn(_LOG, "native_continuous", self.error, "native", self.plan.diagnostic_id)

    def _ahead(self):
        if any(track.failed for track in self.tracks if track.kind != "subtitle"):
            return True
        if self.frontier < self.start:
            return False
        lead = 1 if self.streams.plan_usage(self.plan) > _SPOOL_PLAN_LIMIT * 3 // 4 else _LEAD_WINDOWS
        return self.frontier >= self.active + lead

    # -- lifecycle ----------------------------------------------------------

    def _command(self, track):
        args = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-probesize",
            "1000000",
            "-analyzeduration",
            "1000000",
            # Keep the source clock. Renormalising a stream whose first packet is
            # reordered pushes every presentation time by a reorder delay and hides
            # it in an edit list; with the source clock kept, decode times stay
            # absolute and the only translation left is subtracting the origin.
            # ``-copyts`` is an input option, so it must precede ``-i``.
            "-copyts",
            "-f",
            "matroska",
            "-i",
            "pipe:0",
            # Every session's media clock starts at zero. A session that opens at
            # a cue otherwise starts at that cue's absolute time, and the muxer
            # records the difference in a leading empty edit, so the one init a
            # playlist names would describe only the session that wrote it. With
            # the clock normalised every start writes the same init, and the
            # session anchors that zero on the source time of its first block.
            "-avoid_negative_ts",
            "make_zero",
        ]
        if track.kind == "audio":
            audio = track.audio
            codec = ["-c:a", "copy"] if audio.copyable else [
                "-c:a", "aac", "-b:a", "192k", "-ac", "2"
            ]
            return [
                *args,
                "-map",
                f"0:a:{audio.ordinal}",
                *codec,
                "-f",
                "mp4",
                "-frag_duration",
                str(_AUDIO_FRAGMENT_US),
                "-movflags",
                _MOVFLAGS,
                "pipe:1",
            ]
        tag = ["-tag:v", "hvc1"] if self.timeline.video_codec == "hevc" else []
        return [
            *args,
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            *tag,
            "-f",
            "mp4",
            "-movflags",
            _MOVFLAGS,
            "pipe:1",
        ]

    async def open(self):
        self.tracks = [_ContinuousTrack("video")]
        for audio in self.timeline.audios:
            self.tracks.append(_ContinuousTrack("audio", audio.ordinal, audio))
        if not self.streams._admitted():
            self.error = "reserve"
            raise NativeError("reserve")
        for track in self.tracks:
            try:
                process = await asyncio.create_subprocess_exec(
                    *self._command(track),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                self.shutdown("process_failed")
                self.fail("process_failed")
                raise NativeError("process_failed") from error
            track.process = process
            track.tail = _Tail()
            self.streams.running.add(process)
            track.stderr_task = asyncio.create_task(_drain(process.stderr, track.tail))
            self.tasks.append(track.stderr_task)
            self.tasks.append(asyncio.create_task(self._read_fragments(track)))
        self.pump = asyncio.create_task(self._pump_run())
        _LOG.warning(
            "native continuous start=%d window=%.3f tracks=%d origin=%.3f",
            self.start,
            self.timeline.segments[self.start][0],
            len(self.tracks),
            self.timeline.origin,
        )

    def shutdown(self, reason="closed"):
        """Stop the session now: used by the synchronous registry close path."""
        if self.closed:
            return
        self.closed = True
        self.demand.set()
        if self.pump is not None:
            self.pump.cancel()
        for task in self.tasks:
            task.cancel()
        for track in self.tracks:
            _kill(track.process)
        for path in list(self.plan.root.glob(".*.part")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    async def close(self, reason="closed"):
        """Stop the session with every wait bounded.

        A restart runs inside this call, so it must never be able to wait on a
        producer that is itself waiting on backpressure, on a pipe, or on a lock
        this caller holds: the children are killed first, and then their tasks and
        processes are collected with a deadline instead of an unbounded await.
        """
        self.shutdown(reason)
        pending = [task for task in [self.pump, *self.tasks] if task is not None]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=10)
        for track in self.tracks:
            process = track.process
            if process is None:
                continue
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except (TimeoutError, asyncio.CancelledError):
                _kill(process)
            self.streams.running.discard(process)
        _LOG.warning(
            "native continuous close=%s start=%d frontier=%d active=%d windows=%d "
            "read=%d read_ms=%d write_ms=%d peak_bytes=%d video_base=%s audio_base=%s",
            reason,
            self.start,
            self.frontier,
            self.active,
            self.metrics["windows"],
            self.metrics["bytes"],
            self.metrics["read_ms"],
            self.metrics["write_ms"],
            self.peak_bytes,
            self.video_base,
            self.audio_base,
        )

    # -- the pump -----------------------------------------------------------

    async def _pump_run(self):
        timeline = self.timeline
        offset = timeline.clusters[self.feed_start]
        keyframe_ms = int(
            round((timeline.origin + timeline.segments[self.feed_start][0]) * 1000)
        )
        client = await self.streams.shared_client()
        reader = _RangeReader(
            timeline.url, offset, timeline.total, client=client, limit=timeline.total
        )
        try:
            collector = _Collector()
            self.feed_first = await self.streams._write_head(
                reader, timeline, keyframe_ms, timeline.video_number, collector
            )
            head = bytes(collector.data)
            if len(head) < 32:
                raise NativeError("short_window")
            await self._broadcast(head)
            while not self.closed:
                if self._ahead():
                    # Nothing is read past the retention window: the provider is
                    # not walked ahead of the viewer, so the spool stays bounded.
                    self.demand.clear()
                    if self._ahead():
                        try:
                            async with asyncio.timeout(_ABANDON_SECONDS):
                                await self.demand.wait()
                        except TimeoutError:
                            self.closed = True
                            break
                    continue
                if reader.buffer:
                    data = bytes(reader.buffer)
                    reader.buffer.clear()
                    await self._broadcast(data)
                    continue
                if reader.position >= reader.end:
                    break
                if not await reader._fill(_READ_CHUNK):
                    break
            await self._drain_tail()
        except asyncio.CancelledError:
            raise
        except (NativeError, OSError, ValueError, httpx.HTTPError) as error:
            self.fail(getattr(error, "code", "pump_failed"))
        finally:
            self.metrics["bytes"] = reader.read_bytes
            self.metrics["read_ms"] = round(reader.read_seconds * 1000)
            self.feed_done = True
            await reader.close()
            _LOG.warning(
                "native continuous feed=done start=%d bytes=%d ms=%d closed=%s error=%s",
                self.start,
                self.metrics["bytes"],
                self.metrics["read_ms"],
                self.closed,
                self.error,
            )

    async def _broadcast(self, data):
        for track in self.tracks:
            process = track.process
            if process is None or track.failed:
                continue
            try:
                process.stdin.write(data)
                started = time.monotonic()
                await process.stdin.drain()
                self.metrics["write_ms"] += round((time.monotonic() - started) * 1000)
            except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
                # A consumer that stopped is a failed session, not a closed pipe:
                # only the end of the feed is allowed to close one.
                track.failed = True
                self.fail("process_failed")

    async def _drain_tail(self):
        """Close every stdin so each muxer flushes its last fragment."""
        for track in self.tracks:
            process = track.process
            if process is None:
                continue
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, RuntimeError):
                continue

    # -- fragments ----------------------------------------------------------

    async def _read_fragments(self, track):
        stream = track.process.stdout
        try:
            while not self.closed:
                box = await _read_box(stream)
                if box is None:
                    break
                kind, data = box
                if kind in _INIT_BOXES:
                    if len(track.init) + len(data) > _INIT_BOX_LIMIT:
                        raise NativeError("init_too_large")
                    track.init += data
                    if kind == b"moov":
                        # The initialisation segment is the track description, not
                        # a window: it is written once, atomically, and never
                        # evicted or replaced by a later session.
                        path = (
                            self.plan.root / "init.mp4"
                            if track.kind == "video"
                            else self.plan.root / track.audio.init
                        )
                        if not path.is_file():
                            await asyncio.to_thread(_write_atomic, path, track.init)
                            pinned = track.init
                        else:
                            # The playlist pins one init for the whole pass, so a
                            # restarted session must present its media through that
                            # init's clock, not the clock its own muxer chose.
                            try:
                                pinned = await asyncio.to_thread(path.read_bytes)
                            except OSError:
                                pinned = track.init
                        if track.timescale is None:
                            track.timescale = media_timescale(track.init)
                        # One init for the whole pass: the playlist names this exact
                        # byte string, and with every session's clock normalised the
                        # muxer writes the same one for each start. Anything else
                        # means the init the player holds does not describe this
                        # session's media, so it is refused rather than served on a
                        # guessed timeline.
                        if stable_init_enabled() and pinned != track.init:
                            raise NativeError("init_mismatch")
                        track.shift = edit_shift(pinned)
                        track.pinned_shift = edit_shift(track.init)
                    continue
                if kind != b"moof":
                    continue
                media = await _read_box(stream)
                if media is None:
                    break
                if media[0] != b"mdat":
                    raise NativeError("fragment")
                await self._place(track, data + media[1])
        except asyncio.CancelledError:
            raise
        except (NativeError, OSError, ValueError) as error:
            track.failed = True
            self.fail(getattr(error, "code", "process_failed"))
        finally:
            self._publish_open(track)
            if track.kind != "subtitle":
                self.readers_done += 1
            # Read the child's last words before reporting them: the stderr drain
            # is a separate task and may still be behind at this point.
            drain = getattr(track, "stderr_task", None)
            if drain is not None:
                try:
                    async with asyncio.timeout(5):
                        await drain
                except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    drain.cancel()
            _LOG.warning(
                "native continuous track=done kind=%s window=%d frontier=%d "
                "fragments=%d rc=%s failed=%s ffmpeg=%s",
                track.kind,
                track.window,
                track.frontier,
                track.fragments,
                None if track.process is None else track.process.returncode,
                track.failed,
                track.tail.text() if track.tail is not None else "",
            )

    def _window_at(self, seconds):
        for number, (start, end) in enumerate(self.timeline.segments):
            if start - 1e-6 <= seconds < end:
                return number
        return len(self.timeline.segments) - 1

    def _segment_path(self, track, number):
        if track.kind == "video":
            return self.plan.video_segment_path(number)
        return self.plan.audio_segment_path(track.audio, number)

    async def _place(self, track, fragment):
        if not track.timescale:
            raise NativeError("init_timescale")
        pairs = fragment_presentation(fragment)
        if not pairs:
            raise NativeError("fragment_clock")
        earliest = min(decode + offset for decode, offset in pairs)
        number = self.feed_start + track.fragments
        anchor = await self._base_for(track, earliest)
        base = self._select_base(track, earliest, anchor, number)
        data = shift_decode_times(fragment, base)
        if track.kind == "video":
            # One fragment per source keyframe and one window per cue point, in the
            # same order. A fragment opens on the keyframe that starts its window and
            # carries that keyframe's leading pictures, which are decoded here and
            # presented just before the cue, so a segment begins a reorder lead before
            # its declared start. That is the price of never cutting the source
            # sequence, and the measured lead is bounded per boundary.
            number = self.feed_start + track.fragments
            shown = self._presentation(track, base, earliest)
            if track.fragments < 3:
                _LOG.warning(
                    "native continuous video n=%d earliest=%.3f shown=%.3f expect=%d ts=%d",
                    track.fragments,
                    earliest / track.timescale,
                    shown,
                    number,
                    track.timescale,
                )
            if number < len(self.timeline.segments):
                window_start = self.timeline.segments[number][0]
                if not window_start - _FRAGMENT_SLACK <= shown < window_start + _FRAGMENT_SLACK:
                    raise NativeError("timeline_mismatch")
            number = min(number, len(self.timeline.segments) - 1)
        else:
            number = self._window_at(self._presentation(track, base, earliest))
        if track.fragments < 3:
            _LOG.warning(
                "native continuous place kind=%s n=%d earliest=%d first_cts=%d "
                "shift=%.3f base=%d shown=%.3f window=%d ts=%d",
                track.kind,
                track.fragments,
                earliest,
                first_composition_offset(fragment) if track.kind == "video" else 0,
                track.shift,
                base,
                self._presentation(track, base, earliest),
                number,
                track.timescale,
            )
        number = min(number, len(self.timeline.segments) - 1)
        if number != track.window:
            self._publish_open(track)
            path = self._segment_path(track, number)
            track.target = path
            track.window = number
            part = path.parent / f".{path.name}.part"
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            track.part = part
        await asyncio.to_thread(self._append, track.part, data)
        track.fragments += 1

    async def _base_for(self, track, earliest):
        """The constant that puts this track's first fragment on the pass timeline.

        The head writer reports where each track's first forwarded block really
        sits on the source timeline, and the earliest *presented* sample of the
        first fragment is what gets anchored there. Each track is anchored on its
        own source time, so picture, sound and subtitles keep the relationship the
        source gave them instead of each being forced onto a cue.

        A decode time cannot be written below zero. Whatever cannot be expressed
        is pushed later for every track by the same amount, so A/V sync is kept and
        the pass simply starts a fraction of a frame late rather than with the
        picture sliding against the sound.
        """
        number = self.timeline.video_number if track.kind == "video" else track.audio.number
        if track.kind == "audio":
            if self.audio_base is not None:
                return self.audio_base
        elif self.video_base is not None:
            return self.video_base
        source_ms = self.feed_first.get(number)
        if source_ms is None:
            # No head report: fall back to the cue, which is the old contract and
            # still correct for a session that opens exactly on a cue.
            base = round(self.timeline.segments[self.feed_start][0] * track.timescale)
            base -= earliest
        else:
            base = round(source_ms * track.timescale / 1000) - earliest
        # The pinned init's edit list is the mapping this media is presented
        # through, so it belongs in the anchor rather than being assumed away.
        base -= round(track.shift * track.timescale)
        if track.kind == "audio":
            if not self.clock_ready.is_set():
                # The video anchor decides the session-wide delay. Audio fragments
                # are small and can arrive first, so wait for it rather than
                # anchoring against a delay that does not exist yet.
                try:
                    async with asyncio.timeout(5):
                        await self.clock_ready.wait()
                except TimeoutError:
                    pass
            base += round(self.clock_delay * track.timescale)
            self.audio_base = base
            return base
        self.clock_delay = max(self.clock_delay, -base / track.timescale)
        base += round(self.clock_delay * track.timescale)
        self.video_base = base
        self.origin = (base + earliest) / track.timescale + track.shift
        self.clock_ready.set()
        return base

    def _select_base(self, track, earliest, anchor, number):
        """Pick the constant this fragment's own clock is actually on.

        A fragmented stream written to a pipe carries either the session's clock
        (the first fragment of a session, and every audio fragment) or the source's
        absolute decode times (later video fragments). Both are real, so the window
        table decides which: a candidate is used only when it lands the fragment
        inside its own window. Nothing is assumed, and if neither candidate lands
        the caller raises instead of serving media at the wrong time.
        """
        window_start = self.timeline.segments[
            min(number, len(self.timeline.segments) - 1)
        ][0]
        floor = window_start - _FRAGMENT_SLACK
        top = window_start + _FRAGMENT_SLACK
        for candidate in (anchor, -round(track.shift * track.timescale)):
            shown = (candidate + earliest) / track.timescale + track.shift
            if floor <= shown < top:
                return candidate
        return anchor

    @staticmethod
    def _presentation(track, base, earliest):
        """Where the fragment's earliest presented sample is shown, in pass seconds."""
        return (base + earliest) / track.timescale + track.shift

    @staticmethod
    def _append(path, data):
        with path.open("ab") as stream:
            stream.write(data)

    def _publish_open(self, track):
        path = getattr(track, "part", None)
        if path is None or track.target is None:
            return
        track.part = None
        try:
            if path.stat().st_size > 16:
                path.replace(track.target)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            return
        self._published(track, track.window)

    def _published(self, track, number):
        track.frontier = max(track.frontier, number)
        self.metrics["windows"] += 1
        usage = self.streams.plan_usage(self.plan)
        self.peak_bytes = max(self.peak_bytes, usage)
        _LOG.warning(
            "native continuous kind=%s window=%d start=%d frontier=%d end=%.3f "
            "spool=%d fragments=%d",
            track.kind,
            number,
            self.start,
            self.frontier,
            self.timeline.segments[number][1],
            usage,
            track.fragments,
        )
        self._retain()

    def _retain(self):
        """Delete windows the player has passed, so peak disk is the retention."""
        keep_from = self.active - _KEEP_WINDOWS
        if keep_from <= self.start:
            return
        for number in range(max(self.start, keep_from - 2), keep_from):
            for path in (
                self.plan.video_segment_path(number),
                *[self.plan.audio_segment_path(audio, number) for audio in self.timeline.audios],
            ):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue

# --- presentation helpers ---------------------------------------------------


def _escape(value):
    return (value or "").replace('"', "'").replace("\n", " ")[:40]


_LANGUAGE_NAMES = {
    "eng": "English",
    "en": "English",
    "fre": "French",
    "fra": "French",
    "ger": "German",
    "deu": "German",
    "spa": "Spanish",
    "es": "Spanish",
    "ita": "Italian",
    "jpn": "Japanese",
    "ja": "Japanese",
    "und": "Unknown",
}


def _language_name(language):
    return _LANGUAGE_NAMES.get(language or "", language.upper() if language else "Audio")


def _probe_enabled():
    return os.environ.get("OMAKASE_NATIVE_PROBE") == "1"


async def _probe_rates(client, url, total):
    """Read-only throughput probe: one big read, serial ranges, parallel ranges."""
    size = min(4 * 1024 * 1024, max(256 * 1024, total // 8))
    quarter = max(64 * 1024, size // 4)

    async def timed(start, length):
        began = time.monotonic()
        body = await _range(client, url, start, start + length - 1)
        return len(body) / max(1e-6, time.monotonic() - began) / 1e6

    try:
        single = await timed(0, size)
        began = time.monotonic()
        for index in range(4):
            await _range(client, url, index * quarter, index * quarter + quarter - 1)
        serial = (4 * quarter) / max(1e-6, time.monotonic() - began) / 1e6
        began = time.monotonic()
        await asyncio.gather(
            *(
                _range(client, url, index * quarter, index * quarter + quarter - 1)
                for index in range(4)
            )
        )
        parallel = (4 * quarter) / max(1e-6, time.monotonic() - began) / 1e6
        _LOG.warning(
            "native probe single_mbps=%.2f serial_mbps=%.2f parallel4_mbps=%.2f",
            single,
            serial,
            parallel,
        )
    except Exception as error:
        _LOG.warning("native probe failed=%s", type(error).__name__)


class NativeStreams:
    """Registry of native plans, one bounded spool directory each."""

    def __init__(self, root=None, clock=time.monotonic, free_bytes=None):
        self.root = Path(root) if root else None
        self.clock = clock
        self.plans: dict[str, NativePlan] = {}
        self.free_bytes = free_bytes or self._default_free
        # Windows are remote-read bound, not CPU bound, so two can overlap safely.
        self.producers = asyncio.Semaphore(2)
        self.running: set = set()
        self.client = None
        self.client_loop = None
        self.admission_lock = asyncio.Lock()

    async def shared_client(self):
        """One pooled provider client: a TLS handshake per window is pure cost.

        The pool belongs to the running event loop, so a new loop (a test, or a
        restarted worker) gets a fresh client instead of borrowing dead sockets.
        """
        loop = asyncio.get_running_loop()
        if self.client is None or self.client_loop is not loop:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(30, connect=10),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
                follow_redirects=False,
                trust_env=False,
            )
            self.client_loop = loop
        return self.client

    @staticmethod
    def _default_free():
        for candidate in ("/", "/home/omakase/data"):
            try:
                return shutil.disk_usage(candidate).free
            except OSError:
                continue
        return 0

    @property
    def enabled(self):
        return self.root is not None

    # -- admission ----------------------------------------------------------

    def _admitted(self, reserve=0):
        """Free space must survive this window and everything already reserved.

        Reserved bytes are not on disk yet, so they are subtracted from free
        space rather than added to usage; that is what stops two concurrent
        producers from each passing admission and then together crossing the
        reserve floor.
        """
        try:
            free = self.free_bytes()
        except OSError:
            return False
        return free - self.reserved() - reserve >= _RESERVE_BYTES + _ADMISSION_MARGIN

    def reserved(self):
        if not self.plans:
            return 0
        return sum(plan.reserved_bytes for plan in self.plans.values())

    def _usage(self, root):
        total = 0
        try:
            for path in root.rglob("*"):
                try:
                    if path.is_file():
                        total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return 0
        return total

    def usage(self):
        return self._usage(self.root) if self.root is not None else 0

    def plan_usage(self, plan):
        return self._usage(plan.root)

    # -- lifecycle ----------------------------------------------------------

    def start(self, key, url, duration, *, subtitle_key="", warm=None):
        if not self.enabled or not key or not _DIRECT.fullmatch(url or ""):
            return None
        existing = self.plans.get(key)
        if existing is not None:
            existing.touched = self.clock()
            return existing
        if len(self.plans) >= _MAX_PLANS:
            oldest = min(self.plans.values(), key=lambda item: item.touched)
            self.close(oldest.key)
        if not self._admitted():
            warn(_LOG, "native_start", "free_space_reserve", "native", None)
            return None
        root = self.root / key
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            warn(_LOG, "native_start", "resource_unavailable", "native", None)
            return None
        plan = NativePlan(key, url, float(duration), root, subtitle_key, warm=warm)
        plan.continuous = continuous_enabled()
        self.plans[key] = plan
        plan.task = asyncio.create_task(self._build(plan))
        return plan

    def get(self, key):
        plan = self.plans.get(key) if key else None
        if plan is not None:
            plan.touched = self.clock()
        return plan

    async def _build(self, plan):
        try:
            timeline = await build_timeline(plan.url, plan.duration)
            if timeline is None:
                plan.error = "unsupported"
                warn(_LOG, "native_index", "unsupported_media", "native", plan.diagnostic_id)
                return
            plan.timeline = timeline
            _LOG.warning(
                "native timeline segments=%d origin=%.3f video=%s audio=%d bytes=%d",
                len(timeline.segments),
                timeline.origin,
                timeline.video_codec,
                len(timeline.audios),
                timeline.total,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError, RuntimeError, KeyError, httpx.HTTPError):
            plan.error = "index_unavailable"
            warn(_LOG, "native_index", "index_unavailable", "native", plan.diagnostic_id)
        except Exception:  # noqa: BLE001 - terminal owner observes unexpected failures
            plan.error = "unexpected_error"
            warn(_LOG, "native_index", plan.error, "native", plan.diagnostic_id)
        finally:
            plan.ready.set()
        if plan.timeline is not None:
            await self._warm(plan)

    async def ready(self, plan, timeout=_OPEN_WAIT):
        if plan is None:
            return None
        if not plan.ready.is_set():
            try:
                async with asyncio.timeout(timeout):
                    await plan.ready.wait()
            except TimeoutError:
                return None
        return plan.timeline

    @staticmethod
    def _position_index(timeline, position):
        position = max(0.0, float(position))
        for number, (start, end) in enumerate(timeline.segments):
            if start <= position < end:
                return number
        return len(timeline.segments) - 1

    async def _warm(self, plan):
        timeline = plan.timeline
        if timeline is None:
            return
        if plan.continuous:
            # A continuous session starts on the first request, when the route
            # already knows how many subtitle renditions the player will see.
            return
        opening = 0
        if plan.warm is not None:
            opening = self._position_index(timeline, plan.warm)
        try:
            await self.ensure(plan, opening, _OPEN_WAIT)
        except (NativeError, asyncio.CancelledError):
            return

    # -- playlists ----------------------------------------------------------

    def master_playlist(self, plan, subtitles=()):
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        # No EXT-X-INDEPENDENT-SEGMENTS: a continuous session cuts at the source's
        # own random access points, and an open-GOP CRA is not a point a decoder
        # can enter cold. Claiming independence the media does not have is worse
        # than leaving the tag out.
        lines = ["#EXTM3U", "#EXT-X-VERSION:7"]
        for number, audio in enumerate(timeline.audios):
            lines.append(
                f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="'
                f'{_escape(audio.name or _language_name(audio.language))}",'
                f'LANGUAGE="{audio.language or "und"}",'
                f'DEFAULT={"YES" if number == 0 else "NO"},AUTOSELECT=YES,URI="{audio.playlist}"'
            )
        for number, track in enumerate(subtitles[:_MAX_SUBTITLE_TRACKS]):
            lines.append(
                f'#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID="subs",NAME="'
                f'{_escape(getattr(track, "name", "") or _language_name(getattr(track, "language", "")))}",'
                f'LANGUAGE="{getattr(track, "language", "") or "und"}",'
                f"DEFAULT={'YES' if number == 0 else 'NO'},AUTOSELECT=YES,FORCED=NO,"
                f'URI="subtitles{number}.m3u8"'
            )
        stream = f"#EXT-X-STREAM-INF:BANDWIDTH={timeline.bandwidth}"
        if timeline.width and timeline.height:
            stream += f",RESOLUTION={timeline.width}x{timeline.height}"
        if timeline.frame_rate:
            stream += f",FRAME-RATE={timeline.frame_rate:.3f}"
        if timeline.audios:
            stream += ',AUDIO="aud"'
        if subtitles:
            stream += ',SUBTITLES="subs"'
        lines.append(stream)
        lines.append("stream.m3u8")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def _segment_lines(self, timeline):
        # Every segment carries an absolute decode time on one pass timeline, in
        # video, audio and subtitles alike, so there is no discontinuity to
        # declare and no independent-segment guarantee to make.
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{timeline.target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
        ]
        return lines

    def video_playlist(self, plan):
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        lines = self._segment_lines(timeline)
        lines.append('#EXT-X-MAP:URI="init.mp4"')
        for number, (start, end) in enumerate(timeline.segments):
            lines.append(f"#EXTINF:{max(0.04, end - start):.3f},")
            lines.append(f"v{number:05d}.m4s")
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def audio_playlist(self, plan, ordinal):
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        audio = next((item for item in timeline.audios if item.ordinal == ordinal), None)
        if audio is None:
            raise NativeError("no_track")
        lines = self._segment_lines(timeline)
        lines.append(f'#EXT-X-MAP:URI="{audio.init}"')
        for number, (start, end) in enumerate(timeline.segments):
            lines.append(f"#EXTINF:{max(0.04, end - start):.3f},")
            lines.append(audio.segment(number))
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode("utf-8")

    def subtitle_playlist(self, plan, track):
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        lines = self._segment_lines(timeline)
        for number, (start, end) in enumerate(timeline.segments):
            lines.append(f"#EXTINF:{max(0.04, end - start):.3f},")
            lines.append(f"s{track}-{number:05d}.vtt")
        lines.append("#EXT-X-ENDLIST")
        return ("\n".join(lines) + "\n").encode("utf-8")

    # -- generation ---------------------------------------------------------

    async def ensure(self, plan, number, timeout=_GENERATE_TIMEOUT, subtitles=0):
        """Produce one window: video, every audio rendition and the subtitles."""
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        if not 0 <= number < len(timeline.segments):
            raise NativeError("no_segment")
        if plan.continuous:
            await self._ensure_continuous(plan, number, timeout, subtitles)
            plan.touched = self.clock()
            return
        if self._window_ready(plan, number, subtitles):
            plan.touched = self.clock()
            return
        estimate = self._reserve_estimate(timeline, number)
        # Sustained playback must not be refused for space that older, already
        # consumed windows are holding: evict those first, then refuse only if
        # the budget still cannot hold the window.
        if self.plan_usage(plan) + estimate > _SPOOL_PLAN_LIMIT:
            self._evict_before(plan, number)
        if self.plan_usage(plan) + estimate > _SPOOL_PLAN_LIMIT:
            warn(_LOG, "native_window", "window_budget", "native", plan.diagnostic_id)
            raise NativeError("window_budget")
        if not self._admitted(reserve=estimate):
            warn(_LOG, "native_window", "free_space_reserve", "native", plan.diagnostic_id)
            raise NativeError("reserve")
        task = plan.windows.get(number)
        if task is None:
            task = asyncio.create_task(self._generate(plan, number, subtitles))
            task.add_done_callback(_observe)
            plan.windows[number] = task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout)
        except (TimeoutError, asyncio.TimeoutError):
            raise NativeError("deadline") from None
        finally:
            if task.done() and plan.windows.get(number) is task:
                plan.windows.pop(number, None)
        if not self._window_ready(plan, number, subtitles):
            raise NativeError("generation_failed")
        plan.touched = self.clock()
        self._prefetch(plan, number + 1, subtitles)

    # -- continuous mode ----------------------------------------------------

    def _media_ready(self, plan, number):
        try:
            if (plan.root / "init.mp4").stat().st_size <= 16:
                return False
            for audio in plan.timeline.audios:
                if (plan.root / audio.init).stat().st_size <= 16:
                    return False
            if plan.video_segment_path(number).stat().st_size <= 16:
                return False
            for audio in plan.timeline.audios:
                if plan.audio_segment_path(audio, number).stat().st_size <= 16:
                    return False
        except OSError:
            return False
        return True

    def _subtitle_ready(self, plan, number, tracks):
        for track in range(min(int(tracks or 0), _MAX_SUBTITLE_TRACKS)):
            try:
                if plan.subtitle_segment_path(track, number).stat().st_size <= 0:
                    return False
            except OSError:
                return False
        return True

    async def _wait_for(self, plan, session, number, timeout, check):
        """Wait for one window, but never for a session that was restarted away."""
        try:
            async with asyncio.timeout(timeout):
                while True:
                    if check():
                        return
                    if plan.session is not session:
                        # Another request restarted the session at another cue.
                        # Answering this one from that timeline would be the
                        # substitution this design removes, so it is a retry.
                        raise NativeError("restarted")
                    if session.error is not None:
                        raise NativeError(session.error)
                    if session.feed_done and session.readers_done >= len(session.tracks):
                        # The source ended and every muxer is finished, so this
                        # window can never be produced. Fail now instead of
                        # waiting out the deadline for something impossible.
                        raise NativeError("incomplete")
                    await asyncio.sleep(0.05)
        except TimeoutError:
            warn(_LOG, "native_window", "deadline", "native", plan.diagnostic_id)
            raise NativeError("deadline") from None

    async def _ensure_continuous(self, plan, number, timeout, subtitles):
        """Serve one window from a single forward session, restarting for a seek."""
        if self._media_ready(plan, number):
            return
        async with plan.lock:
            session = plan.session
            if session is None or not session.can_serve(number):
                backward = session is not None and number < session.start
                # Only a backward restart can thrash: a straggling request for a
                # segment from before a seek asks for one, and honouring a burst
                # of them would keep tearing down the position the viewer chose.
                # Forward requests are served by continuing the same session.
                if backward and self.clock() - plan.backward_at < _SEEK_COOLDOWN:
                    warn(_LOG, "native_window", "stale", "native", plan.diagnostic_id)
                    raise NativeError("stale")
                if session is not None:
                    await session.close("restart")
                session = _ContinuousSession(self, plan, number)
                plan.session = session
                if backward:
                    plan.backward_at = self.clock()
                try:
                    await session.open()
                except NativeError:
                    plan.session = None
                    raise
                warn(_LOG, "native_continuous", "started", "native", plan.diagnostic_id)
            session.active = max(session.active, number)
            session.demand.set()
        await self._wait_for(
            plan, session, number, timeout, lambda: self._media_ready(plan, number)
        )

    async def ensure_subtitles(self, plan, number, tracks):
        """Subtitle segments come from one bounded window read, on demand only.

        Cues are not taken from the continuous feed: a text track reports nothing
        during silence, so a shared cue stream cannot prove when a window is
        complete and a lane that lags the feed loses cues. This path is the one
        already qualified for windows, and it only runs when the player actually
        asks for a subtitle segment, so a title watched without subtitles pays
        nothing for it.
        """
        count = min(int(tracks or 0), _MAX_SUBTITLE_TRACKS)
        if count <= 0:
            raise NativeError("no_track")
        if self._subtitle_ready(plan, number, count):
            plan.touched = self.clock()
            return
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        if not 0 <= number < len(timeline.segments):
            raise NativeError("no_segment")
        await self._generate(plan, number, count, subtitles_only=True)
        if not self._subtitle_ready(plan, number, count):
            raise NativeError("generation_failed")
        plan.touched = self.clock()

    def _window_ready(self, plan, number, subtitles=0):
        timeline = plan.timeline
        if timeline is None:
            return False
        try:
            if plan.video_segment_path(number).stat().st_size <= 16:
                return False
            if (plan.root / "init.mp4").stat().st_size <= 16:
                return False
            for audio in timeline.audios:
                if plan.audio_segment_path(audio, number).stat().st_size <= 16:
                    return False
            for track in range(min(subtitles, _MAX_SUBTITLE_TRACKS)):
                if not plan.subtitle_segment_path(track, number).is_file():
                    return False
        except OSError:
            return False
        return True

    def _prefetch(self, plan, start, subtitles=0):
        if plan.prefetch is not None and not plan.prefetch.done():
            current = plan.prefetch_start
            if current is not None and current <= start <= current + _MAX_PREFETCH:
                return
            plan.prefetch.cancel()
        plan.prefetch_start = start
        if plan.prefetch is not None and not plan.prefetch.done():
            return

        async def run():
            try:
                timeline = plan.timeline
                if timeline is None:
                    return
                for number in range(start, min(start + _MAX_PREFETCH, len(timeline.segments))):
                    if self._window_ready(plan, number, subtitles):
                        continue
                    if self.plan_usage(plan) > _SPOOL_PLAN_LIMIT * 3 // 4:
                        break
                    await self.ensure(plan, number, _GENERATE_TIMEOUT, subtitles)
            except (NativeError, asyncio.CancelledError):
                return

        plan.prefetch = asyncio.create_task(run())

    def _window_commands(
        self, plan, number, subtitles, video_part, audio_parts, subtitle_paths,
        subtitles_only=False,
    ):
        timeline = plan.timeline
        start, end = timeline.segments[number]
        span = max(0.2, end - start)
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "matroska",
            "-i",
            "pipe:0",
        ]
        if subtitles_only:
            # Keep the source clock: a cue that began before this window has to
            # arrive with its real time so it can be clipped into the window
            # instead of being lost at the boundary or after a seek.
            args.extend(["-copyts", "-avoid_negative_ts", "disabled"])
        if not subtitles_only:
            args.extend(
                [
                    "-frames:v",
                    str(_window_frames(timeline, span)),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                ]
            )
            if timeline.video_codec == "hevc":
                args.extend(["-tag:v", "hvc1"])
            args.extend(
                [
                    "-f",
                    "mp4",
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof+delay_moov",
                    str(video_part),
                ]
            )
            for audio, path in audio_parts:
                args.extend(["-t", f"{span:.3f}", "-map", f"0:a:{audio.ordinal}", "-vn"])
                if audio.copyable:
                    args.extend(["-c:a", "copy"])
                else:
                    # Selective audio-only conversion: AVPlayer cannot decode this
                    # codec, so only this rendition is re-encoded. Video is untouched.
                    args.extend(["-c:a", "aac", "-b:a", "192k", "-ac", "2"])
                args.extend(
                    [
                        "-f",
                        "mp4",
                        "-movflags",
                        "+frag_keyframe+empty_moov+default_base_moof+delay_moov",
                        str(path),
                    ]
                )
        for track, path in subtitle_paths:
            args.extend(["-map", f"0:s:{track}", "-c:s", "webvtt", "-vn", "-an"])
            if not subtitles_only:
                args.extend(["-t", f"{span:.3f}"])
            args.extend(["-f", "webvtt", str(path)])
        return args

    async def _generate(self, plan, number, subtitles=0, subtitles_only=False):
        timeline = plan.timeline
        if timeline is None:
            raise NativeError("no_timeline")
        root = plan.root
        video_part = root / f".v{number:05d}.part"
        audio_parts = [
            (audio, root / f".a{audio.ordinal}-{number:05d}.part") for audio in timeline.audios
        ]
        subtitle_paths = [
            (track, plan.subtitle_segment_path(track, number))
            for track in range(min(subtitles, _MAX_SUBTITLE_TRACKS))
        ]
        args = self._window_commands(
            plan, number, subtitles, video_part, audio_parts, subtitle_paths,
            subtitles_only=subtitles_only,
        )
        # The producer slot is taken before any process exists: a window that is
        # merely queued must not hold a child process, a pipe and file handles.
        # The whole operation, queueing included, runs inside one deadline.
        estimate = (
            2 * 1024 * 1024
            if subtitles_only
            else self._reserve_estimate(plan.timeline, number)
        )
        try:
            async with asyncio.timeout(_GENERATE_TIMEOUT):
                async with self.producers:
                    async with self.admission_lock:
                        if not subtitles_only and self.plan_usage(plan) + estimate > _SPOOL_PLAN_LIMIT:
                            self._evict_before(plan, number)
                        if not subtitles_only and self.plan_usage(plan) + estimate > _SPOOL_PLAN_LIMIT:
                            warn(
                                _LOG, "native_window", "window_budget", "native", plan.diagnostic_id
                            )
                            raise NativeError("window_budget")
                        if self.usage() + self.reserved() + estimate > _SPOOL_TOTAL_LIMIT:
                            warn(
                                _LOG, "native_window", "global_budget", "native", plan.diagnostic_id
                            )
                            raise NativeError("global_budget")
                        if not self._admitted(reserve=estimate):
                            warn(
                                _LOG,
                                "native_window",
                                "free_space_reserve",
                                "native",
                                plan.diagnostic_id,
                            )
                            raise NativeError("reserve")
                        plan.reserved_bytes += estimate
                    try:
                        await self._produce(
                            plan, number, args, video_part, audio_parts, subtitle_paths,
                            subtitles_only=subtitles_only,
                        )
                    except (OSError, RuntimeError) as error:
                        raise NativeError("process_failed") from error
                    finally:
                        plan.reserved_bytes = max(0, plan.reserved_bytes - estimate)
        except (TimeoutError, asyncio.TimeoutError):
            self._discard(video_part, audio_parts)
            warn(_LOG, "native_window", "deadline", "native", plan.diagnostic_id)
            raise NativeError("deadline") from None
        except NativeError:
            self._discard(video_part, audio_parts)
            raise

    async def _produce(
        self, plan, number, args, video_part, audio_parts, subtitle_paths,
        subtitles_only=False,
    ):
        started = time.monotonic()
        metrics = {}
        tail = _Tail()
        process = None
        stderr_task = None
        commit_started = None
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            self.running.add(process)
            stderr_task = asyncio.create_task(_drain(process.stderr, tail))
            try:
                metrics = await self._feed(
                    plan,
                    number,
                    process.stdin,
                    max(0, number - 1) if subtitles_only else None,
                )
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ConnectionResetError, RuntimeError):
                    pass
            await process.wait()
        finally:
            if process is not None and process.returncode is None:
                _kill(process)
            if process is not None:
                try:
                    await process.wait()
                except BaseException:
                    pass
                self.running.discard(process)
            if stderr_task is not None:
                try:
                    await stderr_task
                except BaseException:
                    stderr_task.cancel()
        elapsed = round((time.monotonic() - started) * 1000)
        if process is None or process.returncode != 0:
            warn(_LOG, "native_window", "ffmpeg_failed", "native", plan.diagnostic_id)
            _LOG.warning(
                "native window=%d rc=%s ms=%d read_ms=%s read_bytes=%s read_calls=%s "
                "write_ms=%s ffmpeg=%s",
                number,
                None if process is None else process.returncode,
                elapsed,
                metrics.get("read_ms"),
                metrics.get("read_bytes"),
                metrics.get("read_calls"),
                metrics.get("write_ms"),
                tail.text(),
            )
            raise NativeError("ffmpeg_failed")
        commit_started = time.monotonic()
        try:
            if subtitles_only:
                size = await asyncio.to_thread(
                    self._commit_subtitles, plan, number, subtitle_paths
                )
            else:
                size = await asyncio.to_thread(
                    self._commit, plan, number, video_part, audio_parts, subtitle_paths
                )
        except (OSError, ValueError) as error:
            self._discard(video_part, audio_parts)
            warn(_LOG, "native_window", "invalid_output", "native", plan.diagnostic_id)
            raise NativeError("invalid_output") from error
        commit_ms = round((time.monotonic() - commit_started) * 1000)
        start, end = plan.timeline.segments[number]
        if number == 0 and _probe_enabled() and not plan.probed:
            plan.probed = True
            asyncio.create_task(
                _probe_rates(await self.shared_client(), plan.timeline.url, plan.timeline.total)
            )
        _LOG.warning(
            "native window=%d span=%.3f ms=%d read_ms=%s read_bytes=%s read_calls=%s "
            "write_ms=%s commit_ms=%d out=%d spool=%d reserved=%d",
            number,
            end - start,
            elapsed,
            metrics.get("read_ms"),
            metrics.get("read_bytes"),
            metrics.get("read_calls"),
            metrics.get("write_ms"),
            commit_ms,
            size,
            self.plan_usage(plan),
            plan.reserved_bytes,
        )

    async def _relative_start(self, timeline, number, keyframe_ms):
        """(absolute block offset, cluster timestamp) or None when unqualified.

        Two readings of CueRelativePosition exist in the wild, so both are tried
        and the candidate is accepted only when the bytes there really begin with
        this video track's keyframe at the expected time. Anything else falls back
        to the cluster-start trim.
        """
        relatives = getattr(timeline, "relatives", ())
        if not relatives or number >= len(relatives):
            return None
        relative = relatives[number]
        if relative is None:
            return None
        cluster_start = timeline.clusters[number]
        client = await self.shared_client()
        head = await _range(
            client,
            timeline.url,
            cluster_start,
            min(timeline.total - 1, cluster_start + _CUE_HEAD_BYTES - 1),
        )
        if not head:
            return None
        timestamp, payload_start = _cluster_header(head)
        if payload_start is None:
            return None
        for candidate in _candidate_offsets(cluster_start, cluster_start + payload_start, relative):
            probe = await _range(
                client,
                timeline.url,
                candidate,
                min(timeline.total - 1, candidate + _CUE_PROBE_BYTES - 1),
            )
            if not probe:
                continue
            timing = _first_block_timing(probe, timeline.video_number)
            if timing is None:
                continue
            _track, block_ms, is_key = timing
            if not is_key:
                continue
            if (
                timestamp is not None
                and abs(timestamp + block_ms - keyframe_ms) > _CUE_RELATIVE_TOLERANCE_MS
            ):
                continue
            return candidate, timestamp
        return None

    async def _feed(self, plan, number, stdin, from_number=None):
        """Write the synthetic Matroska window into ffmpeg's standard input.

        Returns window-local metrics. ``read_ms`` is only the time spent inside
        HTTP range reads; ``write_ms`` is only the time blocked on ffmpeg's pipe,
        so provider cost and consumer backpressure stay separable.

        ``from_number`` starts the feed one window earlier, which subtitle work
        uses so a cue that began before the window can be clipped into it.
        """
        timeline = plan.timeline
        first = number if from_number is None else max(0, min(from_number, number))
        start = timeline.segments[first][0]
        keyframe_ms = int(round((timeline.origin + start) * 1000))
        offset = timeline.clusters[first]
        end = timeline.total
        if number + 1 < len(timeline.clusters):
            end = min(end, timeline.clusters[number + 1] + _WINDOW_TAIL_BYTES)
        budget = min(_WINDOW_MAX_BYTES, max(end - offset, self._reserve_estimate(timeline, number)))
        resolved = None
        if _CUE_RELATIVE_ENABLED:
            resolved = await self._relative_start(timeline, number, keyframe_ms)
        start_offset = offset if resolved is None else resolved[0]
        reader = _RangeReader(
            timeline.url,
            start_offset,
            min(timeline.total, start_offset + budget),
            client=await self.shared_client(),
            limit=min(_WINDOW_MAX_BYTES, self._reserve_estimate(timeline, number) * 2),
        )
        write_seconds = 0.0
        try:
            if resolved is None:
                await self._write_head(reader, timeline, keyframe_ms, timeline.video_number, stdin)
            else:
                # The cluster header and its Timestamp are written here because
                # the original ones are before the first byte we read; blocks stay
                # verbatim so their relative timestamps keep their meaning.
                cluster_ms = resolved[1] if resolved[1] is not None else keyframe_ms
                stdin.write(timeline.header + _encode_id(_SEGMENT) + _UNKNOWN_SIZE)
                stdin.write(timeline.info + timeline.tracks)
                stdin.write(_encode_id(_CLUSTER) + _UNKNOWN_SIZE)
                stdin.write(_encode_element(_TIMESTAMP, int(cluster_ms).to_bytes(8, "big")))
                await stdin.drain()
            async for chunk in reader.chunks(_READ_CHUNK):
                if stdin.is_closing():
                    break
                stdin.write(chunk)
                write_started = time.monotonic()
                await stdin.drain()
                write_seconds += time.monotonic() - write_started
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
            # ffmpeg stops reading as soon as it has the window it was asked for.
            # A closed pipe is the normal end of a bounded feed, not a failure.
            # uvloop reports a closed transport as RuntimeError, not BrokenPipe.
            pass
        finally:
            await reader.close()
        return {
            "read_ms": round(reader.read_seconds * 1000),
            "read_bytes": reader.read_bytes,
            "read_calls": reader.read_calls,
            "write_ms": round(write_seconds * 1000),
        }

    @staticmethod
    def _reserve_estimate(timeline, number):
        """Bytes one window may put on disk, including its temporary outputs."""
        start, end = timeline.segments[number]
        span = max(0.2, end - start)
        bitrate = max(1_000_000, timeline.bandwidth)
        picture = int(span * bitrate / 8)
        # The synthetic header, audio renditions, subtitle cues and the init boxes
        # are small next to the picture, but they are counted as well.
        return min(_SPOOL_PLAN_LIMIT // 2, picture + 6 * 1024 * 1024)

    async def _write_head(self, reader, timeline, keyframe_ms, video_number, stdin):
        """Write the synthetic Matroska head and report each track's first time.

        The whole feed keeps the source's absolute clock: only the first cluster's
        header is rebuilt, so rebasing that one header alone would make the muxer
        see the clock jump back to the source clock on the next cluster. One clock
        for the whole session is the contract the anchor relies on.

        Returns ``{Matroska track number: absolute presentation ms}`` for the
        earliest block of each track that was forwarded, which is what the session
        anchors the muxer's own presentation clock against.
        """
        head = await reader.peek(64)
        if len(head) < 12:
            raise NativeError("short_window")
        cluster_id, cluster_after = vint(head, 0, True)
        if cluster_id != _CLUSTER:
            raise NativeError("no_cluster")
        _size, payload = vint(head, cluster_after, False)
        stdin.write(timeline.header + _encode_id(_SEGMENT) + _UNKNOWN_SIZE)
        stdin.write(timeline.info + timeline.tracks)
        await stdin.drain()
        # The first cluster may start before the keyframe. Rebuild it with an
        # unknown size and forward only what the player can actually start on.
        await reader.consume(payload)
        stdin.write(_encode_id(_CLUSTER) + _UNKNOWN_SIZE)
        timestamp = None
        shift = 0
        started = video_number == 0
        sources = {}
        while True:
            header = await reader.peek(16)
            if len(header) < 2:
                break
            try:
                element_id, after = vint(header, 0, True)
                size, body = vint(header, after, False)
            except (IndexError, ValueError):
                raise NativeError("bad_cluster") from None
            if element_id == _CLUSTER:
                # The next cluster begins; stop owning this one.
                return sources
            total = body + size
            if total + 16 > _WINDOW_MAX_BYTES:
                raise NativeError("oversized_cluster")
            if element_id == _TIMESTAMP:
                raw = await reader.take(total)
                timestamp = int.from_bytes(raw[body:], "big")
                shift = timestamp - keyframe_ms
                stdin.write(_encode_element(_TIMESTAMP, int(keyframe_ms).to_bytes(8, "big")))
                await stdin.drain()
                continue
            if element_id not in {_SIMPLE_BLOCK, _BLOCK_GROUP}:
                await reader.consume(total)
                continue
            raw = await reader.take(total)
            if timestamp is None:
                continue
            block = raw[body:]
            is_key = True
            if element_id == _BLOCK_GROUP:
                inner = _children(raw, body, total, {_BLOCK, _REFERENCE})
                if _BLOCK not in inner:
                    continue
                start, stop = inner[_BLOCK][0]
                block = raw[start:stop]
                is_key = _REFERENCE not in inner
            try:
                relative = _block_time(block)
                track, after_track = vint(block, 0, False)
                flags = block[after_track + 2] if len(block) > after_track + 2 else 0
            except (IndexError, ValueError):
                continue
            if started and track == video_number and _rasl_nals(block):
                # Drop leading pictures that reference the previous segment.
                continue
            if not started:
                # Start on the video keyframe itself. A timestamp-only cut can
                # drop the keyframe for HEVC and hand the player a fragment it
                # cannot start decoding; the offset is only a sanity window.
                if element_id == _SIMPLE_BLOCK:
                    is_key = bool(flags & 0x80)
                if track == video_number and is_key and timestamp + relative >= keyframe_ms - 50:
                    started = True
                else:
                    continue
            if timestamp + relative < keyframe_ms:
                # Anything shown before the cue belongs to the window this session
                # did not open on, and an open-GOP CRA's leading pictures cannot be
                # decoded without the previous group anyway. Dropping them once,
                # here, is what makes the muxer's decode zero the cue itself; no
                # later picture is ever cut, so the continuous sequence is intact.
                continue
            absolute = timestamp + relative
            if track not in sources or absolute < sources[track]:
                sources[track] = absolute
            if shift:
                # Shift the block's relative time by the same amount the cluster
                # time moved, so absolute timestamps stay exactly where they were.
                position = raw.index(block)
                at = position + after_track
                shifted = (relative + shift).to_bytes(2, "big", signed=True)
                raw = raw[:at] + shifted + raw[at + 2 :]
            stdin.write(raw)
            await stdin.drain()
        return sources

    def _commit(self, plan, number, video_part, audio_parts, subtitle_paths):
        """Split fragments into init segments and media, and place each clock.

        Every file is written once, directly, to its final name. A temporary copy
        plus a rename would double the peak disk one window needs, and this spool
        is capped against the host reserve, not against free space alone.
        """
        timeline = plan.timeline
        start = timeline.segments[number][0]
        written = 0

        def place(target, data):
            nonlocal written
            if len(data) > _SPOOL_PLAN_LIMIT:
                raise ValueError("window_too_large")
            target.write_bytes(data)
            written += len(data)

        video_data = video_part.read_bytes()
        video_init, video_media = split_init(video_data)
        timescale = media_timescale(video_init)
        if timescale is None:
            raise ValueError("video_timescale")
        place(plan.root / "init.mp4", video_init)
        place(
            plan.video_segment_path(number),
            patch_decode_time(video_media, round(start * timescale)),
        )
        video_part.unlink(missing_ok=True)
        for audio, part in audio_parts:
            data = part.read_bytes()
            init, media = split_init(data)
            audio_timescale = media_timescale(init)
            if audio_timescale is None:
                raise ValueError("audio_timescale")
            place(plan.root / audio.init, init)
            place(
                plan.audio_segment_path(audio, number),
                patch_decode_time(media, round(start * audio_timescale)),
            )
            part.unlink(missing_ok=True)
        for _track, subtitle in subtitle_paths:
            _normalise_webvtt(subtitle)
            try:
                written += subtitle.stat().st_size
            except OSError:
                continue
        self._trim(plan)
        return written

    def _commit_subtitles(self, plan, number, subtitle_paths):
        """Clip each cue into this window and put it on the window's clock.

        The window read starts a window early and keeps the source clock, so a
        cue that began before this window (or before a seek landed on it) is
        still part of it. Nothing here can touch a media segment: this pass owns
        no audio or video file and evicts nothing.
        """
        timeline = plan.timeline
        start = timeline.origin + timeline.segments[number][0]
        end = timeline.origin + timeline.segments[number][1]
        written = 0
        for _track, subtitle in subtitle_paths:
            _clip_webvtt(subtitle, start, end)
            try:
                written += subtitle.stat().st_size
            except OSError:
                continue
        return written

    def _discard(self, video_part, audio_parts):
        for path in [video_part, *[path for _audio, path in audio_parts]]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    # -- bounds -------------------------------------------------------------

    def _evict_before(self, plan, number):
        """Delete windows the player has already passed, keeping recent ones."""
        keep_from = max(1, number - 1)
        for path in plan.root.glob("v*.m4s"):
            try:
                index = int(path.stem[1:])
            except ValueError:
                continue
            if index >= keep_from:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        for path in plan.root.glob("a*-*.m4s"):
            try:
                index = int(path.stem.split("-")[1])
            except (IndexError, ValueError):
                continue
            if index >= keep_from:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
        for path in plan.root.glob("s*-*.vtt"):
            try:
                index = int(path.stem.split("-")[1])
            except (IndexError, ValueError):
                continue
            if index >= keep_from:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    def _trim(self, plan):
        try:
            files = sorted(
                (path for path in plan.root.iterdir() if path.suffix == ".m4s" and path.is_file()),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        for path in files:
            if total <= _SPOOL_PLAN_LIMIT:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue
        self._trim_global()

    def _trim_global(self):
        if self.root is None:
            return
        try:
            files = sorted(
                (path for path in self.root.rglob("*.m4s") if path.is_file()),
                key=lambda path: path.stat().st_mtime,
            )
        except OSError:
            return
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        for path in files:
            if total <= _SPOOL_TOTAL_LIMIT:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue

    def close(self, key, reason="closed"):
        plan = self.plans.pop(key, None)
        if plan is None:
            return
        session = plan.session
        plan.session = None
        if session is not None:
            # Kill the forward session before its spool disappears, so no child
            # process can keep writing into a directory that is being removed.
            session.shutdown(reason)
        for task in (plan.task, plan.prefetch, *plan.windows.values()):
            if task is not None:
                task.cancel()
        plan.windows.clear()
        shutil.rmtree(plan.root, ignore_errors=True)

    def sweep(self):
        now = self.clock()
        for key, plan in tuple(self.plans.items()):
            if now - plan.touched > _ABANDON_SECONDS:
                self.close(key)
                warn(_LOG, "native_close", "abandoned", "native", plan.diagnostic_id)

    def reset(self):
        """Remove this module's own spool directories, for restart cleanup."""
        if self.root is None:
            return 0
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return 0
        removed = 0
        for path in entries:
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed += 1
            except OSError:
                continue
        return removed


class _RangeReader:
    """Sequential reader over one bounded HTTP range of the provider file.

    Every request is validated before its bytes are kept: a 206 whose
    ``Content-Range`` starts at the requested offset, a body no longer than the
    bytes still wanted, and a hard ceiling on the total bytes one window may pull
    from the provider. A short body is never concatenated into the stream: the
    missing remainder is re-requested at its own offset, and if the provider stops
    making progress the window fails.

    Time spent inside HTTP reads is accumulated separately from the caller's work,
    so a window's provider cost can be told apart from ffmpeg backpressure.
    """

    def __init__(self, url, start, end, transport=None, limit=_WINDOW_MAX_BYTES, client=None):
        self.url = url
        self.position = start
        self.end = min(end, start + limit)
        self.limit = limit
        self.buffer = bytearray()
        self.transport = transport
        self.client = client
        self.owns_client = client is None
        self.read_bytes = 0
        self.read_calls = 0
        self.read_seconds = 0.0

    async def _client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False
            )
        return self.client

    async def _read_once(self, start, want):
        """One validated range response; may be shorter than asked, never longer."""
        client = await self._client()
        stop = start + want - 1
        time.monotonic()
        try:
            async with client.stream(
                "GET", self.url, headers={"Range": f"bytes={start}-{stop}"}
            ) as response:
                if response.status_code != 206:
                    raise NativeError("provider_range")
                content_range = response.headers.get("content-range")
                if _content_range_start(content_range) != start:
                    raise NativeError("provider_range")
                match = _CONTENT_RANGE.fullmatch(content_range or "")
                if match is not None and int(match[2]) > stop:
                    raise NativeError("provider_oversized")
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        if int(declared) > want:
                            raise NativeError("provider_oversized")
                    except ValueError:
                        raise NativeError("provider_range") from None
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > want:
                            raise NativeError("provider_oversized")
                except httpx.HTTPError as error:
                    # A connection that dies mid-body is a failed read, not a
                    # shorter one: the caller retries it at the same offset.
                    raise NativeError("provider_failed") from error
                return bytes(body)
        finally:
            self.read_calls += 1

    async def _read_exact(self, start, want):
        """Exactly ``want`` bytes from ``start``, retrying each gap at its offset."""
        data = bytearray()
        idle = 0
        while len(data) < want:
            try:
                part = await self._read_once(start + len(data), want - len(data))
            except NativeError as error:
                if error.code != "provider_failed":
                    raise
                part = b""
            if not part:
                idle += 1
                if idle >= _READ_ATTEMPTS:
                    raise NativeError("provider_truncated")
                await asyncio.sleep(0)
                continue
            idle = 0
            data.extend(part)
        return bytes(data)

    async def _fill(self, wanted):
        if self.position >= self.end:
            return False
        want = min(self.end - self.position, max(wanted, _READ_CHUNK))
        if self.read_bytes + want > self.limit:
            raise NativeError("window_too_large")
        read_started = time.monotonic()
        try:
            data = await self._read_parallel(want)
        finally:
            self.read_seconds += time.monotonic() - read_started
        if not data:
            return False
        self.buffer.extend(data)
        self.position += len(data)
        self.read_bytes += len(data)
        return True

    async def _read_parallel(self, want):
        """Read ``want`` bytes over several connections, in order.

        Each part is read exactly or not at all, so parallel reads cannot
        interleave or shorten the stream.
        """
        parts = _READ_PARALLEL if want > 512 * 1024 else 1
        block = want // parts
        ranges = []
        start = self.position
        for index in range(parts):
            length = block if index < parts - 1 else want - block * (parts - 1)
            if length <= 0:
                continue
            ranges.append((start, length))
            start += length
        results = await asyncio.gather(
            *(self._read_exact(offset, length) for offset, length in ranges)
        )
        return b"".join(results)

    async def _need(self, count):
        while len(self.buffer) < count and self.position < self.end:
            if not await self._fill(count - len(self.buffer)):
                break

    async def peek(self, count):
        await self._need(count)
        return bytes(self.buffer[:count])

    async def consume(self, count):
        await self._need(count)
        del self.buffer[:count]

    async def take(self, count):
        await self._need(count)
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    async def chunks(self, size):
        if self.buffer:
            data = bytes(self.buffer)
            self.buffer.clear()
            yield data
        while self.position < self.end:
            if not await self._fill(size):
                break
            if not self.buffer:
                break
            data = bytes(self.buffer)
            self.buffer.clear()
            yield data

    async def close(self):
        if self.owns_client and self.client is not None:
            await self.client.aclose()
        self.client = None


_VTT_TIME = re.compile(r"(\d+(?::\d+)?:\d{2}\.\d{3})\s*-->\s*(\d+(?::\d+)?:\d{2}\.\d{3})")


def _vtt_seconds(text):
    parts = text.strip().split(":")
    try:
        seconds = float(parts[-1])
        minutes = int(parts[-2]) if len(parts) > 1 else 0
        hours = int(parts[-3]) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        return None
    return hours * 3600 + minutes * 60 + seconds


def _vtt_stamp(seconds):
    millis = int(round(max(0.0, seconds) * 1000))
    return (
        f"{millis // 3_600_000:02d}:{millis // 60_000 % 60:02d}:"
        f"{millis // 1000 % 60:02d}.{millis % 1000:03d}"
    )


def _clip_webvtt(path, start, end):
    """Keep only this window's part of every cue, on the window's own clock."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    except OSError:
        return
    cues = ["WEBVTT", ""]
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines or lines[0].startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue
        for index, line in enumerate(lines):
            match = _VTT_TIME.search(line)
            if match is None:
                continue
            low = _vtt_seconds(match[1])
            high = _vtt_seconds(match[2])
            if low is None or high is None or high <= low:
                break
            low, high = max(low, start), min(high, end)
            if high - low <= 0.001:
                break
            cues.append(f"{_vtt_stamp(low - start)} --> {_vtt_stamp(high - start)}")
            payload = "\n".join(lines[index + 1 :])
            if payload:
                cues.append(payload)
            cues.append("")
            break
    text = "\n".join(cues).rstrip() + "\n" if len(cues) > 2 else "WEBVTT\n\n"
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        return


def _write_atomic(path, data):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _normalise_webvtt(path):
    """Make sure a subtitle segment is a valid, short, well formed WebVTT file."""
    try:
        data = path.read_bytes()
    except OSError:
        path.write_text("WEBVTT\n\n", encoding="utf-8")
        return
    text = data.decode("utf-8", "ignore").replace("\r\n", "\n").strip()
    if not text.startswith("WEBVTT"):
        text = "WEBVTT\n\n" + text
    if text.rstrip().endswith("WEBVTT"):
        text = text.rstrip() + "\n\n"
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


class _Tail:
    """Bounded tail of a child process's stderr with provider URLs redacted."""

    def __init__(self, limit=4096):
        self.limit = limit
        self.data = bytearray()

    def add(self, chunk):
        self.data.extend(chunk)
        if len(self.data) > self.limit:
            del self.data[: len(self.data) - self.limit]

    def text(self):
        raw = self.data.decode("utf-8", "ignore").strip().replace("\n", " ")
        return _redact(raw)[:400]


_PROVIDER_URL = re.compile(r"\S*(?:https?://|real-debrid|token=|\.mkv\b|\.mp4\b|\.m3u8\b)\S*")


def _redact(value):
    """Strip provider URLs, including a fragment cut off by the tail bound."""
    return _PROVIDER_URL.sub("<provider>", value)


async def _drain(stream, tail):
    if stream is None:
        return
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            tail.add(chunk)
    except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
        return


def _observe(task):
    if not task.cancelled():
        task.exception()


def _kill(process):
    if process is None:
        return
    try:
        process.kill()
    except (ProcessLookupError, RuntimeError):
        pass
