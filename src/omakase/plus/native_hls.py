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
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_CUE_CLUSTER_POSITION = 0xF1
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
_SPOOL_TOTAL_LIMIT = 96 * 1024 * 1024
_SPOOL_PLAN_LIMIT = 48 * 1024 * 1024
_MAX_PLANS = 3
_MAX_PREFETCH = 2
_OPEN_WAIT = 25.0
_GENERATE_TIMEOUT = 60.0
_RESERVE_BYTES = 8 * 1024**3
_ADMISSION_MARGIN = 32 * 1024**2
_ABANDON_SECONDS = 240.0
_READ_CHUNK = 256 * 1024
# One window never asks the provider for more than this, whatever the bitrate.
_WINDOW_MAX_BYTES = 128 * 1024 * 1024
# Bytes past the next segment's first cluster, enough for ffmpeg to finish the
# window it was asked for.
_WINDOW_TAIL_BYTES = 4 * 1024 * 1024
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
    touched: float = field(default_factory=time.monotonic)
    diagnostic_id: str = field(default_factory=diagnostic_id)
    warm: float | None = None
    bytes_read: int = 0

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
                )
            )
            ordinal += 1
    if video is not None:
        video_number = video.get("number")
    return video, audios, video_number


def _cue_points(data, start, end, video_number, scale):
    """Every video cue as (seconds, absolute cluster offset from the segment)."""
    points = []
    for point_start, point_end in _children(data, start, end, {_CUE_POINT}).get(_CUE_POINT, []):
        fields = _children(data, point_start, point_end, {_CUE_TIME, _CUE_TRACK_POSITIONS})
        if _CUE_TIME not in fields:
            continue
        tracks = []
        cluster = None
        for track_start, track_end in fields.get(_CUE_TRACK_POSITIONS, []):
            inner = _children(data, track_start, track_end, {_CUE_TRACK, _CUE_CLUSTER_POSITION})
            if _CUE_TRACK in inner:
                tracks.append(_uint(data, *inner[_CUE_TRACK][0]))
            if _CUE_CLUSTER_POSITION in inner:
                cluster = _uint(data, *inner[_CUE_CLUSTER_POSITION][0])
        if video_number is not None and tracks and video_number not in tracks:
            continue
        if cluster is None:
            continue
        try:
            value = _uint(data, *fields[_CUE_TIME][0]) * scale / 1e9
        except (IndexError, ValueError):
            continue
        if value >= 0:
            points.append((value, cluster))
    points.sort()
    return points


def _window_index(points, duration, origin, segment_base):
    """Group cue points into windows: each returned boundary is a real keyframe."""
    usable = [(value, cluster) for value, cluster in points if 0 <= value < duration - 0.2]
    if len(usable) < 2:
        return (), ()
    segments = []
    clusters = []
    start, cluster = usable[0]
    for value, next_cluster in usable[1:]:
        if value - start >= _SEGMENT_SECONDS:
            segments.append((start, value))
            clusters.append(segment_base + cluster)
            start, cluster = value, next_cluster
    if duration - start > 0.2:
        segments.append((start, duration))
        clusters.append(segment_base + cluster)
    if not 2 <= len(segments) <= _MAX_SEGMENTS:
        return (), ()
    relative = tuple((round(a - origin, 3), round(b - origin, 3)) for a, b in segments)
    return relative, tuple(clusters)


async def _range(client, url, start, end):
    if end < start or end - start > _MAX_INDEX_BYTES:
        return None
    try:
        response = await client.get(url, headers={"Range": f"bytes={start}-{end}"})
    except httpx.HTTPError:
        return None
    if response.status_code not in {200, 206}:
        return None
    return response.content


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
        segments, clusters = _window_index(points, float(duration), origin, segment_start)
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
            bandwidth=max(1, int(total * 8 / float(duration))),
        )


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
        for kind2, _start2, body2, size2 in _boxes(init, body, body + size):
            if kind2 != b"trak":
                continue
            for kind3, _start3, body3, size3 in _boxes(init, body2, body2 + size2):
                if kind3 != b"mdia":
                    continue
                for kind4, _start4, body4, _size4 in _boxes(init, body3, body3 + size3):
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
        for kind2, _start2, body2, size2 in _boxes(data, body, body + size):
            if kind2 != b"traf":
                continue
            for kind3, _start3, body3, _size3 in _boxes(data, body2, body2 + size2):
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


class NativeStreams:
    """Registry of native plans, one bounded spool directory each."""

    def __init__(self, root=None, clock=time.monotonic, free_bytes=None):
        self.root = Path(root) if root else None
        self.clock = clock
        self.plans: dict[str, NativePlan] = {}
        self.free_bytes = free_bytes or self._default_free
        self.producers = asyncio.Semaphore(1)

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
        # The reserve is the hard floor; the windows this module may add on top of
        # it are already capped by the spool limit, so demand only that plus a
        # small slack. A larger margin refused playback on a host that had a
        # quarter of a gibibyte above its reserve.
        try:
            free = self.free_bytes()
        except OSError:
            return False
        return free - _RESERVE_BYTES > _SPOOL_TOTAL_LIMIT + _ADMISSION_MARGIN + reserve

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
            _LOG.info(
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
        lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-INDEPENDENT-SEGMENTS"]
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
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            f"#EXT-X-TARGETDURATION:{timeline.target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            "#EXT-X-INDEPENDENT-SEGMENTS",
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
        if self._window_ready(plan, number, subtitles):
            plan.touched = self.clock()
            return
        if not self._admitted():
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

    def _window_commands(self, plan, number, subtitles, video_part, audio_parts, subtitle_paths):
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
        args.extend(
            [
                "-t",
                f"{span:.3f}",
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
            args.extend(
                [
                    "-t",
                    f"{span:.3f}",
                    "-map",
                    f"0:s:{track}",
                    "-c:s",
                    "webvtt",
                    "-vn",
                    "-an",
                    "-f",
                    "webvtt",
                    str(path),
                ]
            )
        return args

    async def _generate(self, plan, number, subtitles=0):
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
            plan, number, subtitles, video_part, audio_parts, subtitle_paths
        )
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            async with self.producers:
                await asyncio.wait_for(self._feed(plan, number, process.stdin), _GENERATE_TIMEOUT)
                try:
                    await process.stdin.drain()
                except (BrokenPipeError, OSError, ConnectionResetError):
                    pass
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError, ConnectionResetError):
                    pass
                await asyncio.wait_for(process.wait(), _GENERATE_TIMEOUT)
        except (TimeoutError, asyncio.TimeoutError):
            _kill(process)
            await process.wait()
            self._discard(video_part, audio_parts)
            raise NativeError("deadline") from None
        except BaseException:
            _kill(process)
            try:
                await process.wait()
            except BaseException:
                pass
            self._discard(video_part, audio_parts)
            raise
        if process.returncode != 0:
            self._discard(video_part, audio_parts)
            warn(_LOG, "native_window", "ffmpeg_failed", "native", plan.diagnostic_id)
            raise NativeError("ffmpeg_failed")
        try:
            await asyncio.to_thread(
                self._commit, plan, number, video_part, audio_parts, subtitle_paths
            )
        except (OSError, ValueError) as error:
            self._discard(video_part, audio_parts)
            warn(_LOG, "native_window", "invalid_output", "native", plan.diagnostic_id)
            raise NativeError("invalid_output") from error

    async def _feed(self, plan, number, stdin):
        """Write the synthetic Matroska window into ffmpeg's standard input."""
        timeline = plan.timeline
        start = timeline.segments[number][0]
        keyframe_ms = int(round((timeline.origin + start) * 1000))
        offset = timeline.clusters[number]
        end = timeline.total
        if number + 1 < len(timeline.clusters):
            end = min(end, timeline.clusters[number + 1] + _WINDOW_TAIL_BYTES)
        budget = min(_WINDOW_MAX_BYTES, max(end - offset, self._estimate_window(timeline, number)))
        reader = _RangeReader(timeline.url, offset, min(timeline.total, offset + budget))
        try:
            await self._write_head(reader, plan, timeline, keyframe_ms, stdin)
            async for chunk in reader.chunks(_READ_CHUNK):
                stdin.write(chunk)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # ffmpeg stops reading as soon as it has the window it was asked for.
            # A closed pipe is the normal end of a bounded feed, not a failure.
            return
        finally:
            await reader.close()

    def _estimate_window(self, timeline, number):
        return 8 * 1024 * 1024

    async def _write_head(self, reader, plan, timeline, keyframe_ms, stdin):
        first = await reader.peek(64)
        if len(first) < 12:
            raise NativeError("short_window")
        cluster_id, cluster_after = vint(first, 0, True)
        if cluster_id != _CLUSTER:
            raise NativeError("no_cluster")
        _size, payload = vint(first, cluster_after, False)
        stdin.write(timeline.header + _encode_id(_SEGMENT) + _UNKNOWN_SIZE)
        stdin.write(timeline.info + timeline.tracks)
        await stdin.drain()
        # The first cluster may start before the keyframe. Rebuild it with an
        # unknown size and only the blocks at or after the keyframe, so the very
        # first frame ffmpeg sees is the boundary itself.
        await reader.consume(payload)
        stdin.write(_encode_id(_CLUSTER) + _UNKNOWN_SIZE)
        timestamp = None
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
                return
            total = body + size
            if total + 16 > _WINDOW_MAX_BYTES:
                raise NativeError("oversized_cluster")
            if element_id == _TIMESTAMP:
                raw = await reader.take(total)
                timestamp = int.from_bytes(raw[body:], "big")
                stdin.write(raw)
                await stdin.drain()
                continue
            if element_id not in {_SIMPLE_BLOCK, _BLOCK_GROUP}:
                await reader.consume(total)
                continue
            raw = await reader.take(total)
            if timestamp is None:
                continue
            payload = raw[body:]
            if element_id == _BLOCK_GROUP:
                inner = _children(raw, body, total, {_BLOCK})
                if _BLOCK not in inner:
                    continue
                start, stop = inner[_BLOCK][0]
                payload = raw[start:stop]
            try:
                relative = _block_time(payload)
            except (IndexError, ValueError):
                continue
            if timestamp + relative >= keyframe_ms - 2:
                stdin.write(raw)
                await stdin.drain()

    def _commit(self, plan, number, video_part, audio_parts, subtitle_paths):
        """Split fragments into init segments and media, and place each clock."""
        timeline = plan.timeline
        start = timeline.segments[number][0]
        video_data = video_part.read_bytes()
        video_init, video_media = split_init(video_data)
        timescale = media_timescale(video_init)
        if timescale is None:
            raise ValueError("video_timescale")
        _write_atomic(plan.root / "init.mp4", video_init)
        _write_atomic(
            plan.video_segment_path(number),
            patch_decode_time(video_media, round(start * timescale)),
        )
        video_part.unlink(missing_ok=True)
        for audio, path in audio_parts:
            data = path.read_bytes()
            init, media = split_init(data)
            audio_timescale = media_timescale(init)
            if audio_timescale is None:
                raise ValueError("audio_timescale")
            _write_atomic(plan.root / audio.init, init)
            _write_atomic(
                plan.audio_segment_path(audio, number),
                patch_decode_time(media, round(start * audio_timescale)),
            )
            path.unlink(missing_ok=True)
        for _track, path in subtitle_paths:
            _normalise_webvtt(path)
        self._trim(plan)

    def _discard(self, video_part, audio_parts):
        for path in [video_part, *[path for _audio, path in audio_parts]]:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue

    # -- bounds -------------------------------------------------------------

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
    """Sequential reader over one HTTP range of the provider file."""

    def __init__(self, url, start, end, transport=None):
        self.url = url
        self.position = start
        self.end = end
        self.buffer = bytearray()
        self.transport = transport
        self.client = None
        self.response = None

    async def _client(self):
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(45, connect=10), follow_redirects=False, trust_env=False
            )
        return self.client

    async def _fill(self, wanted):
        if self.position >= self.end:
            return False
        stop = min(self.end, self.position + max(wanted, _READ_CHUNK)) - 1
        client = await self._client()
        response = await client.get(self.url, headers={"Range": f"bytes={self.position}-{stop}"})
        if response.status_code not in {200, 206}:
            raise NativeError("provider_failed")
        data = response.content
        if not data:
            return False
        self.buffer.extend(data)
        self.position += len(data)
        return True

    async def peek(self, count):
        while len(self.buffer) < count:
            if not await self._fill(count - len(self.buffer)):
                break
        return bytes(self.buffer[:count])

    async def consume(self, count):
        while len(self.buffer) < count:
            if not await self._fill(count - len(self.buffer)):
                break
        del self.buffer[:count]

    async def take(self, count):
        while len(self.buffer) < count:
            if not await self._fill(count - len(self.buffer)):
                break
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
        if self.client is not None:
            await self.client.aclose()
            self.client = None


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


def _observe(task):
    if not task.cancelled():
        task.exception()


def _kill(process):
    try:
        process.kill()
    except (ProcessLookupError, RuntimeError):
        pass
