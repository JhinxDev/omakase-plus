"""Convert embedded text subtitles from Real-Debrid files for HLS playback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from omakase.plus.playback_diagnostics import diagnostic_id, warn

_LOG = logging.getLogger(__name__)

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
# A SubRip cue timing line accepts any hour value. The earlier parser required
# "00:" and silently dropped every cue past the first hour of a film.
_SRT_TIMING = re.compile(r"\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}")


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
    diagnostic_id: str = field(default_factory=diagnostic_id)
    tracks: list[SubtitleTrack] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    task: asyncio.Task | None = None
    # Sources whose WebVTT output is already on disk finish in seconds, so the
    # first index request can wait for the tracks instead of opening without
    # a SUBTITLES group.
    warm: bool = False
    # True when this source only needs embedded text tracks copied to WebVTT.
    # That job costs seconds and may run during a pass; OCR and ASR do not, so
    # their jobs stay False and never hold a player request open.
    text_only: bool | None = None


class SubtitleCache:
    """One background probe and conversion job per source version.

    The playback gate decides whether expensive OCR or ASR work may run. It is
    derived from the passes that are actually being served, not from a single
    shared boolean: a close for a media id that has already been replaced used
    to clear the flag while the replacement was still streaming, which let
    background work compete with a live viewer. Opening a pass holds the gate
    for the length of that attempt only, so a failed start cannot release a
    different device's playback either.
    """

    def __init__(self, root, clock=time.monotonic, *, embedded_only=False):
        self.root = Path(root) if root else None
        self.clock = clock
        self.jobs: dict[str, SubtitleJob] = {}
        self.embedded_only = embedded_only
        self.disk_budget = (64 if embedded_only else 512) * 1024 * 1024
        self.gate = asyncio.Semaphore(1)
        self.processes = set()
        # Scoped holds: pass-open attempts and the older boolean surface.
        self._holds: dict[str, int] = {}
        self._playback_probe = None

    def bind_playback_probe(self, probe):
        """Answer "is a pass being served" from the live pass registries."""
        self._playback_probe = probe

    @property
    def playing(self):
        if self._holds:
            return True
        probe = self._playback_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:  # a broken probe must never wedge subtitle work
            return False

    @playing.setter
    def playing(self, playing):
        # Compatibility for the earlier boolean surface. It can hold and release
        # only its own lease, so a late clear cannot unpark a live pass.
        if playing:
            self.begin_start("__compat__")
        else:
            self.end_start("__compat__")

    def begin_start(self, attempt):
        """Hold expensive work while one pass is being opened."""
        label = "start:" + str(attempt or "")
        self._holds[label] = self._holds.get(label, 0) + 1
        self._kill_running()

    def end_start(self, attempt):
        """Release only this attempt's hold, never another pass's."""
        label = "start:" + str(attempt or "")
        depth = self._holds.get(label, 0)
        if depth > 1:
            self._holds[label] = depth - 1
        else:
            self._holds.pop(label, None)

    def _kill_running(self):
        """End running children without letting one refusal keep the gate.

        This runs while a hold is being taken. If it raised, the caller would still
        be holding the gate and the route's release would never run, which is how an
        injected PermissionError stranded a start hold.
        """
        for process in tuple(self.processes):
            try:
                process.kill()
            except ProcessLookupError:
                continue
            except Exception:  # a platform refusal such as PermissionError
                continue

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

    async def wait_decision(self, job, timeout):
        """Wait until the source's subtitle kinds are known.

        The first index has to decide in a few seconds whether waiting is worth
        it: a text copy finishes in seconds, while an OCR or ASR job can run for
        minutes. ``text_only`` is what distinguishes them and it is set as soon
        as ffprobe returns, so this waits for the decision rather than for data.
        """
        if job is None or timeout <= 0:
            return False
        deadline = asyncio.get_running_loop().time() + timeout
        while job.text_only is None:
            if job.done.is_set():
                return False
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.05, remaining))
        return True

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
            job.tracks = tracks[: 4 if self.embedded_only else _MAX_TRACKS]
            # Record which kind of work this is before any await, so a player
            # request that is waiting on the decision learns it immediately: a
            # text copy finishes in seconds, while OCR and ASR can run for
            # minutes and must never hold the opening request open.
            job.text_only = bool(job.tracks)
            if job.tracks:
                # Only an embedded text copy is cheap enough to run during a
                # pass. A first anime play used to deadlock on the idle wait
                # below, because the wait can never end while the pass it belongs
                # to is open, so the tracks stayed empty, the index carried no
                # SUBTITLES group, and the TV showed no subtitle menu at all.
                # A source whose WebVTT files are already on disk needs no work
                # at all: reading them costs no CPU, so it is safe while a pass is
                # open.
                if self._load_cached(job, workdir):
                    job.ready.set()
                    return
                async with self.gate:
                    job.ready.set()
                    await self._convert(job, direct, workdir)
                return
            if self.embedded_only:
                job.ready.set()
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
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as error:
            job.error = type(error).__name__
            code = "resource_unavailable" if isinstance(error, OSError) else "conversion_failed"
            warn(_LOG, "subtitle_job", code, "provider", job.diagnostic_id)
            job.ready.set()
        except Exception:  # noqa: BLE001 - terminal background owner observes unexpected failures
            job.error = "unexpected_error"
            warn(_LOG, "subtitle_job", job.error, "provider", job.diagnostic_id)
            job.ready.set()
        finally:
            if self.embedded_only:
                try:
                    self._prune(workdir)
                except OSError:
                    pass
            job.done.set()

    @staticmethod
    def _srt_vtt(data: bytes) -> bytes:
        text = data.decode("utf-8", "replace").replace("\r\n", "\n")
        blocks = []
        for block in text.split("\n\n"):
            lines = [line.strip("\ufeff") for line in block.splitlines()]
            # Any hour value is valid: a film over an hour produces timestamps
            # that start 01: and up, and requiring 00: silently dropped every
            # subtitle after the first hour.
            if len(lines) < 3 or not _SRT_TIMING.fullmatch(lines[1].strip()):
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
        except OSError:
            return False
        self.processes.add(process)
        try:
            # A viewer can acquire the gate while this spawn is suspended, so the
            # registration alone is not enough. Check again now that the child
            # exists, and end it before it does any work for a paused stream.
            if self.playing:
                await self._terminate(process)
                return False
            try:
                await asyncio.wait_for(process.communicate(), timeout)
            except TimeoutError:
                await self._terminate(process)
                return False
        finally:
            self.processes.discard(process)
        return process.returncode == 0

    @staticmethod
    async def _terminate(process):
        """Best-effort child shutdown.

        A signal that is refused must never leave a caller holding the gate or a
        start hold, so every step here swallows its own failure.
        """
        try:
            process.kill()
        except ProcessLookupError:
            pass
        except Exception:  # a platform refusal such as PermissionError
            try:
                process.terminate()
            except Exception:
                pass
        try:
            await process.wait()
        except Exception:
            pass

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
            # The viewer can start between the idle wait and this permit. Rechecking
            # here is what keeps an OCR job from competing with a live pass.
            if self.playing:
                return
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
            # Round the whole timestamp in milliseconds first. The earlier form
            # computed milliseconds from the fractional part, so 1.9996 became
            # 00:00:01.1000, an invalid four digit stamp.
            total_ms = int(round(max(0.0, seconds) * 1000))
            hours, rest = divmod(total_ms, 3_600_000)
            minutes, rest = divmod(rest, 60_000)
            whole, millis = divmod(rest, 1000)
            return f"{hours:02d}:{minutes:02d}:{whole:02d}.{millis:03d}"

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
            # Same race as OCR: a pass that started while this waited for the permit
            # keeps its CPU, and the transcription waits for the next idle window.
            if self.playing:
                return
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
            warn(_LOG, "subtitle_asr", "audio_extraction_failed", "provider", job.diagnostic_id)
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
            except TimeoutError:
                warn(_LOG, "subtitle_asr", "deadline", "provider", job.diagnostic_id)
                return
            except (ImportError, OSError, RuntimeError) as error:
                code = "asr_failed" if isinstance(error, RuntimeError) else "resource_unavailable"
                warn(_LOG, "subtitle_asr", code, "provider", job.diagnostic_id)
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

    @staticmethod
    def _load_cached(job, workdir):
        """Fill tracks from cached WebVTT files. True when every track is ready.

        Uses the same size guards as the conversion loop so a partial or corrupt
        file cannot be offered to the player as a real track.
        """
        if not job.tracks:
            return False
        for number, track in enumerate(job.tracks):
            path = workdir / f"{number}.vtt"
            try:
                if 0 < path.stat().st_size <= 8 * 1024 * 1024:
                    track.data = path.read_bytes()
                    continue
            except OSError:
                pass
            return False
        return True

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
                    *(["-fs", str(8 * 1024 * 1024)] if self.embedded_only else []),
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
                await asyncio.wait_for(
                    process.communicate(), 60 if self.embedded_only else _EXTRACT_TIMEOUT
                )
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
