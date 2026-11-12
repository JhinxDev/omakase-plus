"""Short-lived native HLS from an exact, already-authorized Matroska copy."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import httpx
from starlette.requests import Request

from omakase.plus import native_tv_captions as captions
from omakase.plus.language_policy import normalize_language
from omakase.plus.web_playback_codecs import hevc_video_range
from omakase.plus.web_playback_delivery import DeliveryError, open_part
from omakase.plus.web_playback_source import PlexStream

_MIB = 1024**2
_VARIANT = r"(?:video|a[0-7])"
_NAME = re.compile(
    rf"index\.m3u8|media-(?:{_VARIANT}|muxed)\.m3u8|"
    rf"init(?:-{_VARIANT})?\.mp4|part-(?:{_VARIANT}-)?[0-9]{{5}}\.m4s"
)
_WORKSPACE_PREFIX = "omakase-tv-hls-"
_WORKSPACE_NAME = re.compile(_WORKSPACE_PREFIX + r"[a-z0-9_]{8}")
_COPY_AUDIO = frozenset({"aac", "ac3", "eac3"})
_AUDIO = _COPY_AUDIO | {"flac", "opus", "dca", "dts", "truehd", "mp3"}
_TEXT_SUBTITLES = frozenset({"srt", "subrip", "ass", "ssa", "webvtt", "mov_text"})
_MASTER_LIMIT = 16 * 1024
_VIDEO_VARIANT = re.compile(
    rb"(?m)^#EXT-X-STREAM-INF:[^\r\n]+(?=\r?\nmedia-(?:video|muxed)\.m3u8\r?$)"
)
_VIDEO_RANGE = re.compile(rb"(?<=[,:])VIDEO-RANGE=[^,\r\n]+")
_CODECS = re.compile(rb"(?<=[,:])CODECS=[^,\r\n]+")
_AUDIO_LINE = re.compile(rb"(?m)^#EXT-X-MEDIA:TYPE=AUDIO[^\r\n]+")
_CODEC_TAGS = {
    "hevc": "hvc1.2.4.L153.B0",
    "h264": "avc1.640028",
    "eac3": "ec-3",
    "ac3": "ac-3",
    "aac": "mp4a.40.2",
    "flac": "fLaC",
    "opus": "opus",
    "dts": "dtsc",
    "dca": "dtsc",
    "truehd": "mlpa",
    "mp3": "mp4a.40.34",
}


def eligible(source):
    audio = [track for track in source.streams if track.kind == "audio"]
    return (
        os.getenv("OMAKASE_NATIVE_TV_HLS") == "1"
        and source.container == "mkv"
        and source.video_codec in {"h264", "hevc"}
        and 0 < source.part_size <= 1536 * _MIB
        and 0 < source.duration_ms <= 14400000
        and sum(track.kind == "video" for track in source.streams) == 1
        and 1 <= len(audio) <= 8
        and all(track.codec in _AUDIO for track in audio)
        and (len(audio) == 1 or all(track.container_index is not None for track in audio))
    )


def _renditions(source) -> tuple[list[PlexStream], list[PlexStream]]:
    audio = [track for track in source.streams if track.kind == "audio"]
    subtitles = [
        track
        for track in source.streams
        if track.kind == "subtitle"
        and track.codec in _TEXT_SUBTITLES
        and track.container_index is not None
    ]
    subtitles.sort(key=lambda track: (track.selected, track.default), reverse=True)
    return audio, subtitles[: captions.MAX_TRACKS]


def _track_options(audio: list[PlexStream]) -> list[str]:
    options = ["-map", "0:v:0"]
    for index, track in enumerate(audio):
        source = f"0:{track.container_index}" if track.container_index is not None else "0:a:0"
        options.extend(("-map", source))
        if track.codec in _COPY_AUDIO:
            options.extend((f"-c:a:{index}", "copy"))
        else:
            options.extend((f"-c:a:{index}", "aac", f"-ac:a:{index}", "2", f"-b:a:{index}", "192k"))
    return options


def _output_options(
    audio: list[PlexStream], renditions: bool, video_playlist: str | None
) -> list[str]:
    if not renditions:
        options = ["-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", "part-%05d.m4s"]
        if video_playlist:
            return [*options, "-master_pl_name", "index.m3u8", video_playlist]
        return [*options, "index.m3u8"]
    default = max(
        range(len(audio)), key=lambda index: (audio[index].selected, audio[index].default)
    )
    variants = []
    for index, track in enumerate(audio):
        language = normalize_language(track.language, observation=True) or "und"
        preferred = ",default:yes" if index == default else ""
        variants.append(f"a:{index},agroup:audio,language:{language},name:a{index}{preferred}")
    return [
        "-hls_fmp4_init_filename",
        "init-%v.mp4",
        "-hls_segment_filename",
        "part-%v-%05d.m4s",
        "-master_pl_name",
        "index.m3u8",
        "-var_stream_map",
        " ".join([*variants, "v:0,agroup:audio,name:video"]),
        "media-%v.m3u8",
    ]


class NativeHLS:
    def __init__(self, source, *, authorize, transport):
        self.source = source
        self.authorize = authorize
        self.transport = transport
        self.workspace = _workspace()
        self.folder = Path(self.workspace.name)
        self.budget = int(source.part_size * 1.25) + 64 * _MIB
        self.audio, self.subtitles = _renditions(source)
        self.renditions = len(self.audio) > 1 or bool(self.subtitles)
        # Even a single HEVC rendition needs a variant tag for HDR signaling.
        self.video_playlist = (
            "media-video.m3u8"
            if self.renditions
            else ("media-muxed.m3u8" if source.video_codec == "hevc" else None)
        )
        self.first_segment = "part-video-00000.m4s" if self.renditions else "part-00000.m4s"
        self.file_limit = 4096 * (1 + len(self.audio) if self.renditions else 1) + len(
            self.subtitles
        )
        self.failed = False
        self.error = None
        self.video_range = None
        self.worker = asyncio.create_task(self._feed())

    def path(self, name):
        if self.failed or not _NAME.fullmatch(name):
            raise DeliveryError("Apple TV stream unavailable.", 404)
        return self.folder / name

    def playlist_asset(self, name):
        if name == "index.m3u8" and (self.subtitles or self.video_range or self.video_playlist):
            playlist = captions.master(self.path(name), self.subtitles)
            playlist = self._with_codecs(playlist)
            if self.video_range and (variant := _VIDEO_VARIANT.search(playlist)):
                attribute = b"VIDEO-RANGE=" + self.video_range
                tag, replaced = _VIDEO_RANGE.subn(attribute, variant[0], count=1)
                if not replaced:
                    tag += b"," + attribute
                playlist = playlist[: variant.start()] + tag + playlist[variant.end() :]
            return playlist, "application/vnd.apple.mpegurl"
        if name.startswith("captions-") and not self.failed:
            return captions.rendition(self.folder, name, len(self.subtitles))
        return None

    def _caption_outputs(self):
        # Segment every track against the video playlist so a seek into a cue
        # still finds it when its start time is in the previous fragment.
        options = []
        for index, track in enumerate(self.subtitles):
            options.extend(
                (
                    "-map",
                    f"0:{track.container_index}",
                    "-c:s",
                    "webvtt",
                    "-flush_packets",
                    "1",
                    "-f",
                    "webvtt",
                    f"text-{index}.vtt",
                )
            )
        return options

    async def ready(self):
        try:
            async with asyncio.timeout(24):
                while True:
                    if self.error is not None:
                        raise self.error
                    if self._index_ready():
                        if self.source.video_codec == "hevc":
                            init = "init-video.mp4" if self.renditions else "init.mp4"
                            self.video_range = hevc_video_range(self.folder, init)
                        return
                    if self.worker.done():
                        raise DeliveryError("Couldn’t prepare this copy.", 415)
                    await asyncio.sleep(0.1)
        except TimeoutError:
            raise DeliveryError("Preparing this copy took too long.", 504) from None

    def _with_codecs(self, playlist):
        """Add deterministic CODECS hints when the local ffmpeg omits them."""
        if not self.renditions:
            lines = playlist.splitlines(keepends=True)
            variant = _VIDEO_VARIANT.search(playlist)
            video = _CODEC_TAGS.get(self.source.video_codec)
            if variant is not None and video and not _CODECS.search(variant[0]):
                label = (video + "," + _CODEC_TAGS.get(self.source.audio_codec, "")).rstrip(",")
                attribute = b'CODECS="' + label.encode("ascii") + b'"'
                tag = variant[0] + b"," + attribute
                start = variant.start()
                return playlist[:start] + tag + playlist[variant.end() :]
            return playlist
        lines = playlist.splitlines(keepends=True)
        audio_index = 0
        for number, line in enumerate(lines):
            if not line.startswith(b"#EXT-X-MEDIA:TYPE=AUDIO"):
                continue
            track = self.audio[audio_index] if audio_index < len(self.audio) else None
            audio_index += 1
            codec = _CODEC_TAGS.get(track.codec) if track is not None else None
            if codec and not _CODECS.search(line):
                lines[number] = (
                    line.rstrip(b"\r\n")
                    + b',CODECS="'
                    + codec.encode()
                    + b'"'
                    + line[len(line.rstrip(b"\r\n")) :]
                )
        playlist = b"".join(lines)
        variant = _VIDEO_VARIANT.search(playlist)
        video = _CODEC_TAGS.get(self.source.video_codec)
        if variant is not None and video and not _CODECS.search(variant[0]):
            default_audio = self.audio[0]
            default_codec = _CODEC_TAGS.get(default_audio.codec, "")
            label = (video + "," + default_codec).rstrip(",")
            attribute = b'CODECS="' + label.encode("ascii") + b'"'
            tag = variant[0] + b"," + attribute
            start = variant.start()
            playlist = playlist[:start] + tag + playlist[variant.end() :]
        return playlist

    def _index_ready(self):
        index = self.folder / "index.m3u8"
        if not index.is_file() or not (self.folder / self.first_segment).is_file():
            return False
        if not self.video_playlist:
            return True
        # FFmpeg can write a master with rendition tags and no variant. AVKit
        # rejects that response without requesting a child playlist or retrying.
        with index.open("rb") as stream:
            lines = stream.read(_MASTER_LIMIT).splitlines()
        variant = self.video_playlist.encode("ascii")
        return any(
            row.startswith(b"#EXT-X-STREAM-INF:") and uri == variant
            for row, uri in zip(lines, lines[1:], strict=False)
        )

    async def close(self):
        self.failed = True
        self.worker.cancel()
        try:
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        finally:
            await asyncio.to_thread(self.workspace.cleanup)

    async def _feed(self):
        response = process = None
        try:
            if shutil.disk_usage(self.folder).free <= self.budget + 1024 * _MIB:
                raise DeliveryError("Stream storage unavailable.", 503)
            request = Request({"type": "http", "method": "GET", "headers": []})
            response = await open_part(
                self.source,
                request,
                authorize=self.authorize,
                transport=self.transport,
                internal_matroska=True,
            )
            if response.status_code != 200:
                raise DeliveryError()
            tag = ["-tag:v", "hvc1"] if self.source.video_codec == "hevc" else []
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-loglevel",
                "error",
                "-protocol_whitelist",
                "pipe,file",
                "-f",
                "matroska",
                "-i",
                "pipe:0",
                *_track_options(self.audio),
                "-c:v",
                "copy",
                *tag,
                "-f",
                "hls",
                "-hls_time",
                "4",
                "-hls_playlist_type",
                "event",
                "-hls_segment_type",
                "fmp4",
                "-hls_flags",
                "independent_segments+temp_file",
                "-hls_list_size",
                "0",
                *_output_options(self.audio, self.renditions, self.video_playlist),
                *self._caption_outputs(),
                cwd=self.folder,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            async with asyncio.timeout(3600):
                received = 0
                async for data in response.body_iterator:
                    process.stdin.write(data)
                    await process.stdin.drain()
                    received += len(data)
                    if received % (8 * _MIB) < len(data):
                        files = list(self.folder.iterdir())
                        used = 0
                        for path in files:
                            # FFmpeg atomically replaces temporary files as it publishes segments.
                            with contextlib.suppress(FileNotFoundError):
                                used += path.stat().st_size
                        if (
                            len(files) > self.file_limit
                            or used > self.budget
                            or shutil.disk_usage(self.folder).free < 1024 * _MIB
                        ):
                            raise DeliveryError("Stream storage unavailable.", 503)
                process.stdin.close()
                if await process.wait() != 0:
                    raise DeliveryError("Couldn’t prepare this copy.", 415)
        except DeliveryError as error:
            self.error = error
            self.failed = True
        except (OSError, TimeoutError, httpx.HTTPError, httpx.StreamError):
            self.error = DeliveryError("Couldn’t prepare this copy.", 415)
            self.failed = True
        finally:
            if response is not None:
                await response.close()
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()


def _workspace():
    data_dir = os.getenv("OMAKASE_DATA_DIR")
    if not data_dir:
        return tempfile.TemporaryDirectory(prefix=_WORKSPACE_PREFIX)
    # The deployed /tmp is a small tmpfs. Use private disk space for the segments.
    root = Path(data_dir).resolve() / ".native-tv-hls"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise DeliveryError("Stream storage unavailable.", 503)
    # Interrupted workers can leave scratch folders. A day exceeds every pass TTL.
    cutoff = time.time() - 24 * 3600
    for folder in root.iterdir():
        if _WORKSPACE_NAME.fullmatch(folder.name) and not folder.is_symlink() and folder.is_dir():
            with contextlib.suppress(FileNotFoundError):
                if folder.stat().st_mtime < cutoff:
                    shutil.rmtree(folder)
    return tempfile.TemporaryDirectory(prefix=_WORKSPACE_PREFIX, dir=root)
