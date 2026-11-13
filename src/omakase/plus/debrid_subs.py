"""Convert embedded text subtitles from Real-Debrid files for HLS playback."""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

_PGS_CODEC = "hdmv_pgs_subtitle"
_OCR_TIMEOUT = 1800
_OCR_LANGUAGES = {
    "eng": "en",
    "en": "en",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "es": "es",
    "ita": "it",
    "por": "pt",
    "rus": "ru",
    "ara": "ar",
}

_TEXT_CODECS = {
    "ass",
    "ssa",
    "subrip",
    "srt",
    "webvtt",
    "mov_text",
    "text",
    "hdmv_text",
}
_LANGUAGE_ORDER = (
    "eng",
    "en",
    "fre",
    "fra",
    "fr",
    "ger",
    "deu",
    "de",
    "spa",
    "es",
    "ita",
    "it",
    "por",
    "pt",
    "rus",
    "ru",
    "ara",
    "ar",
)
_LANGUAGE_NAMES = {
    "eng": "English",
    "en": "English",
    "fre": "French",
    "fra": "French",
    "fr": "French",
    "ger": "German",
    "deu": "German",
    "de": "German",
    "spa": "Spanish",
    "es": "Spanish",
    "ita": "Italian",
    "it": "Italian",
    "por": "Portuguese",
    "pt": "Portuguese",
    "rus": "Russian",
    "ru": "Russian",
    "ara": "Arabic",
    "ar": "Arabic",
}
_MAX_TRACKS = 8
_PROBE_TIMEOUT = 45
_EXTRACT_TIMEOUT = 900
_LANGUAGE = re.compile(r"[a-z]{2,3}")


@dataclass
class SubtitleTrack:
    index: int
    language: str
    name: str
    data: bytes = b""


@dataclass
class SubtitleJob:
    key: str
    tracks: list[SubtitleTrack] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    task: asyncio.Task | None = None


class SubtitleCache:
    """One background probe and conversion job per source version."""

    def __init__(self, root, clock=time.monotonic):
        self.root = Path(root) if root else None
        self.clock = clock
        self.jobs: dict[str, SubtitleJob] = {}
        self.disk_budget = 512 * 1024 * 1024

    def start(self, key, direct, duration):
        if not self.root or not direct or not key or duration > 14400:
            return None
        job = self.jobs.get(key)
        if job is not None and not job.done.is_set():
            return job
        job = SubtitleJob(key)
        self.jobs[key] = job
        job.task = asyncio.create_task(self._run(job, direct))
        while len(self.jobs) > 24:
            oldest = next(iter(self.jobs))
            if oldest == key:
                break
            self.jobs.pop(oldest, None)
        return job

    def get(self, key):
        if not key:
            return None
        return self.jobs.get(key)

    async def _run(self, job, direct):
        workdir = self.root / job.key
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            streams = await self._streams(direct)
            tracks = []
            pgs = []
            for stream in streams:
                if stream.get("codec_name") == _PGS_CODEC:
                    pgs.append(stream)
                if stream.get("codec_name") not in _TEXT_CODECS:
                    continue
                tags = stream.get("tags") or {}
                language = str(tags.get("language") or "").strip().lower()
                language = language if _LANGUAGE.fullmatch(language) else ""
                title = str(tags.get("title") or "").strip()[:40]
                if title:
                    name = title
                elif language:
                    name = _LANGUAGE_NAMES.get(language, language.upper())
                else:
                    name = f"Track {len(tracks) + 1}"
                tracks.append(SubtitleTrack(int(stream["index"]), language, name))
            tracks.sort(
                key=lambda track: (
                    _LANGUAGE_ORDER.index(track.language)
                    if track.language in _LANGUAGE_ORDER
                    else 99,
                    track.index,
                )
            )
            job.tracks = tracks[:_MAX_TRACKS]
            if job.tracks:
                job.ready.set()
                await self._convert(job, direct, workdir)
                return
            if pgs:
                await self._ocr(job, direct, workdir, pgs)
                if not job.ready.is_set():
                    job.ready.set()
                return
            job.ready.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a source simply has no usable subtitles
            job.error = type(error).__name__
            job.ready.set()
        finally:
            job.done.set()

    @staticmethod
    def _srt_vtt(data: bytes) -> bytes:
        text = data.decode("utf-8", "replace").replace("\r\n", "\n")
        blocks = []
        for block in text.split("\n\n"):
            lines = [line.strip("\ufeff") for line in block.splitlines()]
            if len(lines) < 3 or not lines[1].startswith("00:"):
                continue
            timing = lines[1].replace(",", ".")
            blocks.append(timing + "\n" + "\n".join(lines[2:]))
        if not blocks:
            return b""
        return ("WEBVTT\n\n" + "\n\n".join(blocks) + "\n").encode("utf-8")

    async def _run_ocr(self, command, timeout):
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.communicate(), timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                return False
        except OSError:
            return False
        return process.returncode == 0

    async def _ocr(self, job, direct, workdir, streams):
        candidates = [
            stream
            for stream in streams
            if stream.get("codec_name") == _PGS_CODEC and stream.get("index") is not None
        ]
        if not candidates:
            return
        ok = 0
        for number, stream in enumerate(candidates[:2]):
            tags = stream.get("tags") or {}
            raw = str(tags.get("language") or "").strip().lower()
            language = _OCR_LANGUAGES.get(raw, "en")
            name = str(tags.get("title") or "").strip()[:40] or (
                _LANGUAGE_NAMES.get(language, language.upper()) + " (OCR)"
            )
            track = job.tracks[number] if number < len(job.tracks) else None
            if track is None:
                track = SubtitleTrack(int(stream["index"]), language, name)
                job.tracks.append(track)
            sup = workdir / f"ocr-{number}.sup"
            mkv = workdir / f"ocr-{number}.mkv"
            srt = workdir / f"ocr-{number}.srt"
            extract = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                direct,
                "-map",
                f"0:{stream['index']}",
                "-c:s",
                "copy",
                "-f",
                "sup",
                str(sup),
            ]
            if not await self._run_ocr(extract, _EXTRACT_TIMEOUT):
                continue
            if not await self._run_ocr(
                ["mkvmerge", "--language", f"0:{language}", "-o", str(mkv), str(sup)],
                _EXTRACT_TIMEOUT,
            ):
                continue
            if not await self._run_ocr(["pgsrip", "rip", "-l", language, str(mkv)], _OCR_TIMEOUT):
                continue
            vtt = workdir / f"ocr-{number}.vtt"
            if vtt.is_file() and 0 < vtt.stat().st_size <= 8 * 1024 * 1024:
                track.data = vtt.read_bytes()
                ok += 1
                continue
            produced = sorted(workdir.glob(f"ocr-{number}.*.srt"))
            if not produced:
                continue
            data = self._srt_vtt(produced[0].read_bytes())
            if data:
                vtt.write_bytes(data)
            if not data or len(data) > 8 * 1024 * 1024:
                continue
            track.language = language
            track.name = name
            track.data = data
            ok += 1
        if ok:
            job.ready.set()
            self._prune(workdir)

    async def _streams(self, direct):
        try:
            process = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "s",
                "-show_streams",
                "-of",
                "json",
                direct,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), _PROBE_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
                return []
        except (OSError, TimeoutError):
            return []
        if process.returncode != 0 or not output:
            return []
        try:
            value = json.loads(output)
        except (TypeError, ValueError):
            return []
        streams = value.get("streams") if isinstance(value, dict) else None
        return streams if isinstance(streams, list) else []

    async def _convert(self, job, direct, workdir):
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", direct]
        for number, track in enumerate(job.tracks):
            args.extend(
                [
                    "-map",
                    f"0:{track.index}",
                    "-f",
                    "webvtt",
                    str(workdir / f"{number}.vtt"),
                ]
            )
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.communicate(), _EXTRACT_TIMEOUT)
            except TimeoutError:
                process.kill()
                await process.wait()
                job.error = "timeout"
                return
        except OSError:
            job.error = "ffmpeg_unavailable"
            return
        if process.returncode != 0:
            job.error = "ffmpeg_failed"
            return
        for number, track in enumerate(job.tracks):
            path = workdir / f"{number}.vtt"
            try:
                if 0 < path.stat().st_size <= 8 * 1024 * 1024:
                    track.data = path.read_bytes()
            except OSError:
                continue
        self._prune(workdir)

    def _prune(self, keep):
        self.clock()
        total = 0
        entries = []
        for path in self.root.iterdir() if self.root.is_dir() else ():
            if not path.is_dir() or path == keep:
                continue
            try:
                size = sum(item.stat().st_size for item in path.iterdir() if item.is_file())
            except OSError:
                continue
            entries.append((size, path))
        total = sum(size for size, _ in entries)
        limit = self.disk_budget - sum(
            item.stat().st_size for item in keep.iterdir() if item.is_file()
        )
        for size, path in sorted(entries, reverse=True):
            if total <= limit:
                break
            try:
                for item in path.iterdir():
                    item.unlink(missing_ok=True)
                path.rmdir()
            except OSError:
                continue
            total -= size
