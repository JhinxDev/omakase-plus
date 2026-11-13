"""Convert embedded text subtitles from Real-Debrid files for HLS playback."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

_PGS_CODEC = "hdmv_pgs_subtitle"
_OCR_TIMEOUT = 1800
_ASR_TIMEOUT = 1800
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
_OCR_LANGUAGE_NAMES = {
    "english": "en",
    "french": "fr",
    "german": "de",
    "spanish": "es",
    "italian": "it",
    "portuguese": "pt",
    "russian": "ru",
    "arabic": "ar",
    "danish": "da",
    "norwegian": "nb",
    "swedish": "sv",
    "finnish": "fi",
    "dutch": "nl",
    "polish": "pl",
    "turkish": "tr",
    "japanese": "ja",
}

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


def _local_model_path(root, model_name):
    """Return a cached model directory when one exists, else the model name.

    faster-whisper accepts either a size name or a path. A path skips the
    huggingface_hub lookup completely, which is what keeps transcription working
    even when the Hub client and its transfer layer disagree on versions.
    """
    if not root or not model_name:
        return model_name
    cache = Path(root).parent / "whisper"
    pattern = f"models--Systran--faster-whisper-{model_name}/snapshots/*"
    try:
        snapshots = sorted(cache.glob(pattern))
    except OSError:
        return model_name
    for snapshot in snapshots:
        if (snapshot / "model.bin").is_file():
            return str(snapshot)
    return model_name


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
    # Sources whose WebVTT output is already on disk finish in seconds, so the
    # first index request can wait for the tracks instead of opening without
    # a SUBTITLES group.
    warm: bool = False


class SubtitleCache:
    """One background probe and conversion job per source version."""

    def __init__(self, root, clock=time.monotonic):
        self.root = Path(root) if root else None
        self.clock = clock
        self.jobs: dict[str, SubtitleJob] = {}
        self.disk_budget = 512 * 1024 * 1024
        self.playing = False
        self.gate = asyncio.Semaphore(1)
        self.processes = set()

    def set_playing(self, playing):
        self.playing = bool(playing)
        if self.playing:
            for process in tuple(self.processes):
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    async def _wait_for_idle(self):
        while self.playing:
            await asyncio.sleep(0.5)

    @staticmethod
    def _cached_outputs(workdir):
        """Count usable WebVTT files already on disk for this source version."""
        try:
            return sum(
                1 for path in workdir.glob("*.vtt") if 0 < path.stat().st_size <= 8 * 1024 * 1024
            )
        except OSError:
            return 0

    async def wait_tracks(self, job, timeout):
        """Wait until every discovered subtitle track carries WebVTT data."""
        if job is None or timeout <= 0:
            return
        deadline = asyncio.get_running_loop().time() + timeout
        while not (job.tracks and all(track.data for track in job.tracks)):
            if job.done.is_set():
                return
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.05, remaining))

    def start(self, key, direct, duration):
        if not self.root or not direct or not key or duration > 14400:
            return None
        job = self.jobs.get(key)
        if job is not None and not job.done.is_set():
            return job
        job = SubtitleJob(key, warm=self._cached_outputs(self.root / key) > 0)
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
                await self._wait_for_idle()
                async with self.gate:
                    job.ready.set()
                    await self._convert(job, direct, workdir)
                return
            if pgs:
                await self._ocr(job, direct, workdir, pgs)
                if not job.ready.is_set():
                    await self._asr(job, direct, workdir)
                if not job.ready.is_set():
                    job.ready.set()
                return
            await self._asr(job, direct, workdir)
            if not job.ready.is_set():
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
        prefix = []
        if shutil.which("nice"):
            prefix += ["nice", "-n", "19"]
        if shutil.which("taskset"):
            prefix += ["taskset", "-c", "0"]
        try:
            process = await asyncio.create_subprocess_exec(
                *prefix,
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.processes.add(process)
            try:
                await asyncio.wait_for(process.communicate(), timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
                return False
            finally:
                self.processes.discard(process)
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
        missing = [
            number
            for number in range(len(candidates[:2]))
            if not (workdir / f"ocr-{number}.vtt").is_file()
        ]
        if missing:
            await self._wait_for_idle()
        async with self.gate:
            await self._ocr_locked(job, direct, workdir, candidates)

    async def _ocr_locked(self, job, direct, workdir, candidates):
        ok = 0
        for number, stream in enumerate(candidates[:2]):
            tags = stream.get("tags") or {}
            raw = str(tags.get("language") or "").strip().lower()
            name = str(tags.get("title") or "").strip()[:40]
            language = _OCR_LANGUAGES.get(raw, "")
            if not language:
                lowered = name.lower()
                language = next(
                    (code for label, code in _OCR_LANGUAGE_NAMES.items() if label in lowered),
                    "en",
                )
            name = name or (_LANGUAGE_NAMES.get(language, language.upper()) + " (OCR)")
            track = job.tracks[number] if number < len(job.tracks) else None
            if track is None:
                track = SubtitleTrack(int(stream["index"]), language, name)
                job.tracks.append(track)
            vtt = workdir / f"ocr-{number}.vtt"
            if vtt.is_file() and 0 < vtt.stat().st_size <= 8 * 1024 * 1024:
                track.data = vtt.read_bytes()
                ok += 1
                continue
            sup = workdir / f"ocr-{number}.sup"
            mkv = workdir / f"ocr-{number}.mkv"
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

    @staticmethod
    def _vtt_from_segments(rows):
        def stamp(seconds):
            total = max(0, int(seconds))
            return f"{total // 3600:02d}:{(total // 60) % 60:02d}:{total % 60:02d}.{int(round((seconds - total) * 1000)):03d}"

        blocks = [
            f"{stamp(start)} --> {stamp(end)}\n{text}"
            for start, end, text in rows
            if text and end > start
        ]
        return ("WEBVTT\n\n" + "\n\n".join(blocks) + "\n").encode("utf-8") if blocks else b""

    def _asr_sync(self, audio_path):
        import wave

        import numpy as np
        from faster_whisper import WhisperModel

        with wave.open(audio_path, "rb") as source:
            frames = source.readframes(source.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        # Load the cached model directory directly. Going through the Hub asks
        # huggingface_hub to resolve the repo on every start, and a version skew
        # inside the pinned subtitle stack made that raise before a single cue
        # was produced. A local snapshot needs no network and no Hub API.
        model = WhisperModel(
            _local_model_path(self.root, os.getenv("OMAKASE_ASR_MODEL", "tiny")),
            device="cpu",
            compute_type="int8",
            download_root=str(self.root.parent / "whisper") if self.root else None,
        )
        segments, info = model.transcribe(audio, beam_size=1, vad_filter=True)
        rows = [(segment.start, segment.end, segment.text.strip()) for segment in segments]
        return info.language, rows

    async def _asr(self, job, direct, workdir):
        vtt = workdir / "asr.vtt"
        if not vtt.is_file():
            await self._wait_for_idle()
        async with self.gate:
            await self._asr_locked(job, direct, workdir)

    async def _asr_locked(self, job, direct, workdir):
        audio = workdir / "asr.wav"
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            direct,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ]
        if not await self._run_ocr(command, _EXTRACT_TIMEOUT):
            return
        vtt = workdir / "asr.vtt"
        language = "en"
        if vtt.is_file() and 0 < vtt.stat().st_size <= 8 * 1024 * 1024:
            data = vtt.read_bytes()
        else:
            try:
                language, rows = await asyncio.wait_for(
                    asyncio.to_thread(self._asr_sync, str(audio)), _ASR_TIMEOUT
                )
            except (TimeoutError, ImportError, OSError, RuntimeError):
                return
            data = self._vtt_from_segments(rows)
            if not data:
                return
            vtt.write_bytes(data)
        code = _OCR_LANGUAGES.get(str(language).lower(), str(language).lower()[:2] or "en")
        job.tracks = [
            SubtitleTrack(0, code, _LANGUAGE_NAMES.get(code, code.upper()) + " (ASR)", data)
        ]
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
        pending = []
        for number, track in enumerate(job.tracks):
            path = workdir / f"{number}.vtt"
            try:
                if 0 < path.stat().st_size <= 8 * 1024 * 1024:
                    track.data = path.read_bytes()
                    continue
            except OSError:
                pass
            pending.append(number)
        if not pending:
            # Every track was already converted for this source version.
            job.ready.set()
            return
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", direct]
        for number in pending:
            args.extend(
                [
                    "-map",
                    f"0:{job.tracks[number].index}",
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
