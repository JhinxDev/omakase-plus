"""Seekable HLS built from Real-Debrid direct files.

The provider transcode only produces a short window ahead, so the native player
cannot jump forward. A direct link supports byte ranges, which is enough to read
the container's keyframe index quickly. This module turns that index into an
exact HLS timeline and generates only the requested segments with ffmpeg.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_EBML = 0x1A45DFA3
_SEGMENT = 0x18538067
_SEEKHEAD = 0x114D9B74
_INFO = 0x1549A966
_TRACKS = 0x1654AE6B
_CUES = 0x1C53BB6B
_SEEK = 0x4DBB
_SEEK_ID = 0x53AB
_SEEK_POSITION = 0x53AC
_TIMECODE_SCALE = 0x2AD7B1
_TRACK_ENTRY = 0xAE
_TRACK_NUMBER = 0xD7
_TRACK_TYPE = 0x83
_CODEC_ID = 0x86
_CUE_POINT = 0xBB
_CUE_TIME = 0xB3
_CUE_TRACK_POSITIONS = 0xB7
_CUE_TRACK = 0xF7
_DIRECT = re.compile(r"https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.real-debrid\.com/[^\s]{1,3000}")
_VIDEO_CODECS = {
    "V_MPEG4/ISO/AVC": "h264",
    "V_MPEGH/ISO/HEVC": "hevc",
    "V_AV1": "av1",
    "V_MPEG2": "mpeg2",
}
_COPYABLE_AUDIO = {"aac", "ac3", "eac3", "mp3"}
_AUDIO_CODECS = {
    "A_AAC": "aac",
    "A_AC3": "ac3",
    "A_EAC3": "eac3",
    "A_DTS": "dts",
    "A_TRUEHD": "truehd",
    "A_FLAC": "flac",
    "A_OPUS": "opus",
    "A_VORBIS": "vorbis",
}
# Segment length must exceed the cost of building one. A stream-copy
# segment costs about 5.4 s regardless of size, because the fixed cost is
# ffmpeg seeking the remote Matroska file, so a 6 s segment gains about
# 0.8 s per segment against playback while a 3 s segment would starve.
_SEGMENT_SECONDS = 6.0
_MAX_PLANS = 64
_MAX_SEGMENTS = 4000
_MAX_INDEX_BYTES = 12 * 1024 * 1024
_MAX_PREFETCH = 6
# This host runs at about 96 percent disk usage, so a single plan may not claim
# 1.5 GB. Six prefetched six-second segments are roughly 150 MB; the rest is
# headroom for the segment in flight and the next two.
_PLAN_DISK_LIMIT = 512 * 1024 * 1024


@dataclass
class SeekIndex:
    url: str
    duration: float
    video_codec: str
    audio_codec: str
    keyframes: tuple[float, ...]
    segments: tuple[tuple[float, float], ...]
    container: str = "ts"
    transcode: bool = False
    start_offset: float = 0.0
    audio_index: int | None = None

    @property
    def suffix(self):
        return "m4s" if self.container == "fmp4" else "ts"

    @property
    def playlist(self):
        fmp4 = self.container == "fmp4"
        lines = ["#EXTM3U", f"#EXT-X-VERSION:{7 if fmp4 else 3}"]
        longest = max(end - start for start, end in self.segments)
        target = max(1, int(longest + 0.999))
        if self.transcode:
            # A generated six-second H.264 segment can be a few frames longer
            # than its nominal duration. HLS requires TARGETDURATION to be at
            # least as long as every segment, or AVPlayer can reject the stream.
            target = max(target, int(longest + 1.999))
        lines.extend(
            [
                f"#EXT-X-TARGETDURATION:{target}",
                "#EXT-X-MEDIA-SEQUENCE:0",
                "#EXT-X-PLAYLIST-TYPE:VOD",
            ]
        )
        if fmp4:
            lines.append('#EXT-X-MAP:URI="init.mp4"')
        for number, (start, end) in enumerate(self.segments):
            lines.append(f"#EXTINF:{max(0.04, end - start):.3f},")
            lines.append(f"{number:05d}.{self.suffix}")
        lines.extend(["#EXT-X-ENDLIST", ""])
        return "\n".join(lines).encode("ascii")


@dataclass
class SeekPlan:
    key: str
    url: str
    duration: float
    root: Path
    index: SeekIndex | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    mode: str = ""
    error: str | None = None
    task: asyncio.Task | None = None
    segment_tasks: dict[int, asyncio.Task] = field(default_factory=dict)
    prefetch: asyncio.Task | None = None
    init_task: asyncio.Task | None = None
    touched: float = field(default_factory=time.monotonic)
    # Segment the player will open on. Seeded from the saved resume position so
    # the prefetch starts there instead of always walking forward from zero.
    warm_index: int | None = None
    warm_position: float | None = None


class SeekStreams:
    def __init__(self, root=None, clock=time.monotonic):
        self.root = Path(root) if root else None
        self.clock = clock
        self.plans: dict[str, SeekPlan] = {}

    @property
    def enabled(self):
        return self.root is not None

    def start(self, key, url, duration, title=""):
        if not self.enabled or not isinstance(url, str) or not _DIRECT.fullmatch(url):
            return None
        if not isinstance(key, str) or not key:
            return None
        if not isinstance(duration, (int, float)) or not 1 <= duration <= 43200:
            return None
        if not str(title).lower().endswith((".mkv", ".mka")):
            return None
        plan = self.plans.get(key)
        if plan is not None:
            return plan
        # Version the plan directory by segment length so a segmentation
        # change can never serve segments generated by the old timeline.
        directory = self.root / f"{key}-{int(_SEGMENT_SECONDS * 100)}"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        plan = SeekPlan(key, url, float(duration), directory)
        self.plans[key] = plan
        plan.task = asyncio.create_task(self._build(plan))
        # A viewing session seeks several times and each position can open its
        # own plan, so a small first-in-first-out window evicted the plan the
        # player was still reading and every later segment answered 404.
        while len(self.plans) > _MAX_PLANS:
            candidates = [item for item in self.plans if item != key]
            if not candidates:
                break
            oldest = min(candidates, key=lambda item: self.plans[item].touched)
            dropped = self.plans.pop(oldest, None)
            if dropped is not None and dropped.task is not None:
                dropped.task.cancel()
        return plan

    def get(self, key):
        plan = self.plans.get(key) if key else None
        if plan is not None:
            plan.touched = time.monotonic()
        return plan

    async def _build(self, plan):
        try:
            index = await build_index(plan.url, plan.duration)
            if index is None or len(index.segments) < 2:
                plan.error = "no_index"
                return
            plan.index = index
            plan.ready.set()
            await self._warm(plan)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - fall back to the transcode path
            plan.error = type(error).__name__
        finally:
            plan.ready.set()

    async def _warm(self, plan):
        try:
            if plan.index.container == "fmp4":
                await self.init(plan)
            if plan.warm_position is not None:
                # A resume only needs the segment it opens on; generating the
                # opening of the file first would waste the player's patience.
                plan.warm_index = self._position_index(plan, plan.warm_position)
                self._prefetch(plan, max(0, plan.warm_index - 1))
                return
            await self.segment(plan, 0)
        except Exception:  # noqa: BLE001
            return

    async def init(self, plan):
        path = plan.root / "init.mp4"
        try:
            if path.stat().st_size > 8:
                return path.read_bytes()
        except OSError:
            pass
        if plan.init_task is None:
            plan.init_task = asyncio.create_task(self._generate_init(plan))
        await asyncio.shield(plan.init_task)
        if not path.is_file():
            raise LookupError("init generation")
        return path.read_bytes()

    async def _generate_init(self, plan):
        temporary = plan.root / ".init.part"
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0",
            "-i",
            plan.url,
            "-t",
            "0.2",
            "-map",
            "0:v:0",
            "-map",
            f"0:a:{plan.index.audio_index}" if plan.index.audio_index is not None else "0:a:0?",
            "-c:v",
            "copy",
        ]
        args.extend(_audio_args(plan.index.audio_codec))
        args.extend(
            [
                "-sn",
                "-tag:v",
                "hvc1",
                "-f",
                "mp4",
                "-movflags",
                "+frag_keyframe+empty_moov+default_base_moof",
                str(temporary),
            ]
        )
        await _run_ffmpeg(args)
        init, _media = _split_boxes(temporary.read_bytes())
        if len(init) < 8:
            raise RuntimeError("init_failed")
        temporary.unlink(missing_ok=True)
        (plan.root / "init.mp4").write_bytes(init)

    def _position_index(self, plan, position):
        if plan.index is None or not plan.index.segments:
            return 0
        position = max(0.0, float(position))
        for number, (start, end) in enumerate(plan.index.segments):
            if start <= position < end:
                return number
        return len(plan.index.segments) - 1

    def warm(self, plan, position):
        """Start prefetching at the segment the player is about to open."""
        if plan is None:
            return
        try:
            plan.warm_position = max(0.0, float(position))
        except (TypeError, ValueError):
            return
        if plan.index is None:
            # The index is still building; _warm applies this when it lands.
            return
        plan.warm_index = self._position_index(plan, plan.warm_position)
        self._prefetch(plan, max(0, plan.warm_index - 1))

    async def ensure(self, plan, number, timeout):
        """Make one segment available, bounded, so the player never waits on it."""
        if plan is None or plan.index is None or number is None:
            return False
        if not 0 <= number < len(plan.index.segments):
            return False
        path = plan.root / f"{number:05d}.{plan.index.suffix}"
        try:
            if path.stat().st_size > 188:
                return True
        except OSError:
            pass
        try:
            async with asyncio.timeout(timeout):
                await self.segment(plan, number)
            return True
        except (TimeoutError, LookupError, RuntimeError, OSError, asyncio.CancelledError):
            return False

    async def segment(self, plan, number):
        if plan.index is None or not 0 <= number < len(plan.index.segments):
            raise LookupError("segment")
        plan.touched = self.clock()
        path = plan.root / f"{number:05d}.{plan.index.suffix}"
        try:
            if path.stat().st_size > 188:
                self._prefetch(plan, number + 1)
                return path.read_bytes()
        except OSError:
            pass
        task = plan.segment_tasks.get(number)
        if task is None:
            task = asyncio.create_task(self._generate(plan, number))
            plan.segment_tasks[number] = task
        try:
            await asyncio.shield(task)
        except Exception:  # noqa: BLE001
            plan.segment_tasks.pop(number, None)
            raise
        plan.segment_tasks.pop(number, None)
        if not path.is_file() or path.stat().st_size <= 188:
            raise LookupError("segment generation")
        self._prefetch(plan, number + 1)
        return path.read_bytes()

    async def _generate(self, plan, number):
        start, end = plan.index.segments[number]
        output = plan.root / f"{number:05d}.{plan.index.suffix}"
        temporary = plan.root / f".{number:05d}.part"
        args = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.3f}",
            "-i",
            plan.url,
            "-t",
            f"{max(0.1, end - start):.3f}",
            "-map",
            "0:v:0",
            "-map",
            f"0:a:{plan.index.audio_index}" if plan.index.audio_index is not None else "0:a:0?",
        ]
        if plan.index.transcode:
            args.extend(
                [
                    "-c:v",
                    "libx264",
                    "-preset",
                    "ultrafast",
                    "-crf",
                    "26",
                    "-vf",
                    "scale=-2:720",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "160k",
                    "-threads",
                    "2",
                    "-output_ts_offset",
                    f"{start:.3f}",
                    "-muxdelay",
                    "0",
                    "-muxpreload",
                    "0",
                ]
            )
        else:
            args.extend(
                [
                    # The player derives its clock from the playlist's EXTINF
                    # accumulation, so each segment must carry exactly that
                    # position. The source time left every segment offset by the
                    # first keyframe, and AVPlayer failed the item with CoreMedia
                    # -12971 immediately after playback started.
                    "-output_ts_offset",
                    f"{start - plan.index.segments[0][0]:.3f}",
                    "-muxdelay",
                    "0",
                    "-muxpreload",
                    "0",
                    "-c:v",
                    "copy",
                ]
            )
            args.extend(_audio_args(plan.index.audio_codec))
        if plan.index.container == "fmp4":
            args.extend(
                [
                    "-sn",
                    "-tag:v",
                    "hvc1",
                    "-f",
                    "mp4",
                    "-movflags",
                    "+frag_keyframe+empty_moov+default_base_moof",
                    str(temporary),
                ]
            )
        else:
            args.extend(["-sn", "-f", "mpegts", str(temporary)])
        await _run_ffmpeg(args)
        if not temporary.is_file() or temporary.stat().st_size <= 188:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("segment_failed")
        if plan.index.container == "fmp4":
            _init, media = _split_boxes(temporary.read_bytes())
            if len(media) <= 16:
                temporary.unlink(missing_ok=True)
                raise RuntimeError("segment_failed")
            output.write_bytes(media)
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(output)
        self._trim(plan)

    def _prefetch(self, plan, start):
        if plan.prefetch is not None and not plan.prefetch.done():
            return
        if plan.prefetch is not None and plan.prefetch.done():
            plan.prefetch = None

        async def run():
            try:
                limit = 2 if plan.index is not None and plan.index.transcode else _MAX_PREFETCH
                numbers = []
                for offset in range(limit):
                    number = start + offset
                    if plan.index is None or number >= len(plan.index.segments):
                        break
                    numbers.append(number)
                # Two generators at once: each one pays a fixed seek cost against
                # the remote file, so pairing them roughly halves the wait while
                # the provider link still has headroom at this bitrate.
                for position in range(0, len(numbers), 2):
                    pending = []
                    for number in numbers[position : position + 2]:
                        path = plan.root / f"{number:05d}.{plan.index.suffix}"
                        try:
                            if path.stat().st_size > 188:
                                continue
                        except OSError:
                            pass
                        task = plan.segment_tasks.get(number)
                        if task is None:
                            task = asyncio.create_task(self._generate(plan, number))
                            plan.segment_tasks[number] = task
                        pending.append((number, task))
                    for number, task in pending:
                        try:
                            await asyncio.shield(task)
                        finally:
                            plan.segment_tasks.pop(number, None)
            except (RuntimeError, LookupError, OSError, asyncio.CancelledError):
                return

        plan.prefetch = asyncio.create_task(run())

    def _trim(self, plan):
        try:
            files = sorted(
                (path for path in plan.root.iterdir() if path.suffix in {".ts", ".m4s"}),
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
            if total <= _PLAN_DISK_LIMIT:
                break
            try:
                size = path.stat().st_size
                path.unlink()
                total -= size
            except OSError:
                continue

    def close(self, key):
        plan = self.plans.pop(key, None)
        if plan is None:
            return
        if plan.prefetch is not None:
            plan.prefetch.cancel()
        for task in plan.segment_tasks.values():
            task.cancel()
        shutil.rmtree(plan.root, ignore_errors=True)


async def build_index(url, duration):
    if not _DIRECT.fullmatch(url):
        return None
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=10), follow_redirects=False, trust_env=False
    ) as client:
        try:
            head = await client.head(url)
            if head.status_code != 200:
                return None
            total = int(head.headers["content-length"])
        except (httpx.HTTPError, KeyError, ValueError):
            return None
        if total < 1024:
            return None
        first = await _range(client, url, 0, min(1024 * 1024, total) - 1)
        if not first or first[:4] != _EBML.to_bytes(4, "big"):
            return None
        header = _matroska_header(first)
        if header is None:
            return None
        segment_start, tracks, info, seekhead = header
        video_number, video_codec, audio_codec, audio_index = _track_info(first, tracks)
        probed = _copyable_audio(url)
        if probed is not None:
            audio_index, audio_codec = probed
        if video_number is None:
            return None
        if video_codec == "hevc":
            # HEVC stream-copy fragments duplicate the leading GOP. Fixed
            # six-second H.264 segments give an exact seek timeline and keep
            # far-forward seeks working from the direct file.
            segments = tuple(
                (start, min(start + _SEGMENT_SECONDS, float(duration)))
                for start in (
                    index * _SEGMENT_SECONDS
                    for index in range(int(float(duration) // _SEGMENT_SECONDS) + 1)
                )
                if start < float(duration) - 0.05
            )
            if len(segments) < 2 or len(segments) > _MAX_SEGMENTS:
                return None
            return SeekIndex(
                url=url,
                duration=float(duration),
                video_codec=video_codec,
                audio_codec=audio_codec,
                audio_index=audio_index,
                keyframes=(),
                segments=segments,
                container="ts",
                transcode=True,
            )
        if video_codec != "h264":
            return None
        container = "ts"
        scale = _timecode_scale(first, info) or 1_000_000
        cues_position = _cues_position(first, seekhead, segment_start)
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
        keyframes = _cue_times(cues, body, body + size, video_number, scale)
        if len(keyframes) < 2:
            return None
        segments = _segments(keyframes, float(duration))
        if len(segments) < 2 or len(segments) > _MAX_SEGMENTS:
            return None
        return SeekIndex(
            url=url,
            duration=float(duration),
            video_codec=video_codec,
            audio_codec=audio_codec,
            audio_index=audio_index,
            keyframes=tuple(keyframes),
            segments=tuple(segments),
            container=container,
            start_offset=keyframes[0] if keyframes and keyframes[0] > 0.5 else 0.0,
        )


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


def _copyable_audio(url):
    """Ask the demuxer which audio ordinal muxes into MPEG-TS unchanged.

    Matroska track order in the header does not always match the order of
    streams the demuxer exposes, and mapping the wrong ordinal copied a TrueHD
    track into the stream. ffprobe is authority for its own mapping, and it runs
    once per plan.
    """
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index,codec_name",
                "-of",
                "json",
                url,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for ordinal, stream in enumerate(streams):
        if stream.get("codec_name") in _COPYABLE_AUDIO:
            return ordinal, stream["codec_name"]
    return None


def _audio_args(audio):
    # AVPlayer accepts AAC in MPEG-TS universally. Dolby and lossless sources
    # were being transcoded to E-AC3 5.1, which the Apple TV started and then
    # rejected with CoreMedia -12971 after the first frame. Stereo AAC is the
    # compatible choice; surround can come back once playback is stable.
    if audio in _COPYABLE_AUDIO:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]


async def _run_ffmpeg(args):
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        raise RuntimeError("ffmpeg_unavailable") from None
    try:
        await asyncio.wait_for(process.wait(), 40)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError("segment_timeout") from None
    if process.returncode != 0:
        raise RuntimeError("segment_failed")


def _split_boxes(data):
    position = 0
    init = bytearray()
    while position + 8 <= len(data):
        size = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        if size == 1 and position + 16 <= len(data):
            size = int.from_bytes(data[position + 8 : position + 16], "big")
        if size < 8 or position + size > len(data):
            break
        if kind in {b"ftyp", b"moov"}:
            init.extend(data[position : position + size])
        else:
            break
        position += size
    return bytes(init), data[position:]


def _segments(keyframes, duration):
    points = [value for value in keyframes if 0 <= value < duration - 0.2]
    if not points:
        return []
    segments = []
    start = 0.0 if points[0] <= 0.5 else points[0]
    for value in points[1:]:
        if value - start >= _SEGMENT_SECONDS:
            segments.append((start, value))
            start = value
    if duration - start > 0.2:
        segments.append((start, duration))
    return segments


def _vint(data, position, keep_marker):
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


def _element(data, position):
    element_id, after_id = _vint(data, position, True)
    size, body = _vint(data, after_id, False)
    if size == (1 << (7 * (body - after_id))) - 1:
        size = None
    return element_id, size, body


def _children(data, start, end, wanted):
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
            result.setdefault(element_id, []).append((body, stop))
        position = stop
    return result


def _uint(data, start, stop):
    return int.from_bytes(data[start:stop], "big")


def _matroska_header(data):
    try:
        element_id, size, body = _element(data, 0)
    except (IndexError, ValueError):
        return None
    if element_id != _EBML:
        return None
    position = body + (size or 0)
    segment = None
    while position < len(data):
        try:
            element_id, size, body = _element(data, position)
        except (IndexError, ValueError):
            break
        if element_id == _SEGMENT:
            segment = body
            break
        if size is None:
            break
        position = body + size
    if segment is None:
        return None
    wanted = _children(data, segment, len(data), {_TRACKS, _INFO, _SEEKHEAD})
    tracks = wanted.get(_TRACKS, [None])[0]
    info = wanted.get(_INFO, [None])[0]
    seekhead = wanted.get(_SEEKHEAD, [None])[0]
    return segment, tracks, info, seekhead


def _track_info(data, track_range):
    if track_range is None:
        return None, "", ""
    start, end = track_range
    video_number = None
    video_codec = ""
    audio_codec = ""
    audio_index = None
    audio_seen = 0
    first_audio = None
    for entry_start, entry_end in _children(data, start, end, {_TRACK_ENTRY}).get(_TRACK_ENTRY, []):
        fields = _children(data, entry_start, entry_end, {_TRACK_NUMBER, _TRACK_TYPE, _CODEC_ID})
        if _TRACK_NUMBER not in fields or _TRACK_TYPE not in fields:
            continue
        number = _uint(data, *fields[_TRACK_NUMBER][0])
        track_type = _uint(data, *fields[_TRACK_TYPE][0])
        codec = ""
        if _CODEC_ID in fields:
            try:
                codec = data[fields[_CODEC_ID][0][0] : fields[_CODEC_ID][0][1]].decode(
                    "ascii", "ignore"
                )
            except (IndexError, ValueError):
                codec = ""
        if track_type == 1 and video_number is None:
            video_number = number
            video_codec = _VIDEO_CODECS.get(codec, codec.lower())
        elif track_type == 2:
            name = _AUDIO_CODECS.get(codec, codec.lower())
            # -map selects the ordinal among audio streams, not the container
            # track number, so keep our own count.
            ordinal = audio_seen
            audio_seen += 1
            if first_audio is None:
                first_audio = (ordinal, name)
            # Copying a track keeps the source's own audio offset, so a
            # transcoded track that starts at the seek point drifts about a
            # second away from the copied video. Prefer a codec that muxes
            # into MPEG-TS unchanged.
            if audio_index is None and name in _COPYABLE_AUDIO:
                audio_index = ordinal
                audio_codec = name
    if audio_index is None and first_audio is not None:
        audio_index, audio_codec = first_audio
    return video_number, video_codec, audio_codec, audio_index


def _timecode_scale(data, info_range):
    if info_range is None:
        return None
    start, end = info_range
    fields = _children(data, start, end, {_TIMECODE_SCALE})
    if _TIMECODE_SCALE not in fields:
        return None
    try:
        return _uint(data, *fields[_TIMECODE_SCALE][0])
    except (IndexError, ValueError):
        return None


def _cues_position(data, seekhead_range, segment_start):
    if seekhead_range is None:
        return None
    start, end = seekhead_range
    for seek_start, seek_end in _children(data, start, end, {_SEEK}).get(_SEEK, []):
        fields = _children(data, seek_start, seek_end, {_SEEK_ID, _SEEK_POSITION})
        if _SEEK_ID not in fields or _SEEK_POSITION not in fields:
            continue
        id_start, id_end = fields[_SEEK_ID][0]
        try:
            seek_id = int.from_bytes(data[id_start:id_end], "big")
            position = _uint(data, *fields[_SEEK_POSITION][0])
        except (IndexError, ValueError):
            continue
        if seek_id == _CUES:
            return segment_start + position
    return None


def _cue_times(data, start, end, video_number, scale):
    keyframes = []
    for point_start, point_end in _children(data, start, end, {_CUE_POINT}).get(_CUE_POINT, []):
        fields = _children(data, point_start, point_end, {_CUE_TIME, _CUE_TRACK_POSITIONS})
        if _CUE_TIME not in fields:
            continue
        tracks = []
        for track_start, track_end in fields.get(_CUE_TRACK_POSITIONS, []):
            track_fields = _children(data, track_start, track_end, {_CUE_TRACK})
            if _CUE_TRACK in track_fields:
                tracks.append(_uint(data, *track_fields[_CUE_TRACK][0]))
        if tracks and video_number not in tracks:
            continue
        try:
            value = _uint(data, *fields[_CUE_TIME][0]) * scale / 1e9
        except (IndexError, ValueError):
            continue
        if value >= 0:
            keyframes.append(value)
    keyframes.sort()
    return keyframes
