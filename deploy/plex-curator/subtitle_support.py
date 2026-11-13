"""Exact-file English subtitle auditing and sidecar extraction."""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

_ENGLISH_CODES = frozenset({"en", "eng", "english"})
_TEXT_CODECS = frozenset({"ass", "mov_text", "ssa", "subrip", "text", "webvtt"})
_IMAGE_CODECS = frozenset({"dvb_subtitle", "dvd_subtitle", "hdmv_pgs_subtitle"})
_SUBTITLE_CODECS = _TEXT_CODECS | _IMAGE_CODECS
_MAX_CACHE_BYTES = 8 * 1024 * 1024
_MAX_CACHE_ENTRIES = 20_000
_CACHE_SCHEMA = "subtitle-probe-cache-v2"
_MAX_CONTENT_SAMPLE_BYTES = 256 * 1024
_CONTENT_SAMPLE_SECONDS = 15 * 60
_MAX_CONTENT_PROBES = 4
_ENGLISH_WORDS = frozenset(
    {
        "a",
        "about",
        "all",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "come",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "go",
        "had",
        "has",
        "have",
        "he",
        "her",
        "here",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "just",
        "know",
        "like",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "one",
        "or",
        "out",
        "she",
        "so",
        "that",
        "the",
        "their",
        "them",
        "there",
        "they",
        "this",
        "to",
        "up",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
    }
)
_SUBTITLE_TAG_RE = re.compile(r"<[^>\r\n]{0,500}>|\{[^}\r\n]{0,500}\}")
_ENGLISH_TOKEN_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")


class SubtitleAuditError(ValueError):
    """A media file cannot provide one safe exact-file English subtitle."""


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    index: int
    codec: str
    language: str
    title: str = ""
    default: bool = False
    forced: bool = False
    hearing_impaired: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool) or self.index < 0:
            raise SubtitleAuditError("subtitle stream index is invalid")
        if not isinstance(self.codec, str) or self.codec not in _SUBTITLE_CODECS:
            raise SubtitleAuditError("subtitle codec is unsupported")
        if not isinstance(self.language, str) or not self.language or len(self.language) > 32:
            raise SubtitleAuditError("subtitle language is invalid")
        if (
            not isinstance(self.title, str)
            or len(self.title) > 200
            or any(ord(character) < 32 for character in self.title)
        ):
            raise SubtitleAuditError("subtitle title is invalid")
        if any(
            not isinstance(value, bool)
            for value in (self.default, self.forced, self.hearing_impaired)
        ):
            raise SubtitleAuditError("subtitle disposition is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "codec": self.codec,
            "language": self.language,
            "title": self.title,
            "default": self.default,
            "forced": self.forced,
            "hearing_impaired": self.hearing_impaired,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SubtitleTrack:
        if not isinstance(payload, dict) or set(payload) != {
            "index",
            "codec",
            "language",
            "title",
            "default",
            "forced",
            "hearing_impaired",
        }:
            raise SubtitleAuditError("cached subtitle track has an unknown shape")
        return cls(**payload)


@dataclass(frozen=True, slots=True)
class SubtitleProbe:
    duration_ms: int
    tracks: tuple[SubtitleTrack, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms <= 0
            or self.duration_ms > 24 * 60 * 60 * 1000
        ):
            raise SubtitleAuditError("media duration is invalid")
        object.__setattr__(self, "tracks", tuple(self.tracks))
        if tuple(sorted(self.tracks, key=lambda track: track.index)) != self.tracks:
            raise SubtitleAuditError("subtitle tracks are not canonical")
        if len({track.index for track in self.tracks}) != len(self.tracks):
            raise SubtitleAuditError("subtitle stream indexes are duplicated")

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_ms": self.duration_ms,
            "tracks": [track.to_dict() for track in self.tracks],
        }

    @classmethod
    def from_dict(cls, payload: object) -> SubtitleProbe:
        if not isinstance(payload, dict) or set(payload) != {"duration_ms", "tracks"}:
            raise SubtitleAuditError("cached subtitle probe has an unknown shape")
        tracks = payload["tracks"]
        if not isinstance(tracks, list) or len(tracks) > 100:
            raise SubtitleAuditError("cached subtitle tracks are invalid")
        return cls(
            duration_ms=payload["duration_ms"],
            tracks=tuple(SubtitleTrack.from_dict(track) for track in tracks),
        )


@dataclass(frozen=True, slots=True)
class EmbeddedSubtitle:
    """One exact English subtitle retained inside its source media."""

    source: str
    stream_index: int
    source_codec: str
    source_language: str
    source_title: str
    source_default: bool
    source_forced: bool
    source_hearing_impaired: bool
    role: str
    duration_ms: int

    def __post_init__(self) -> None:
        source = _contained_relative_path(self.source, "embedded subtitle source")
        if (
            not isinstance(self.stream_index, int)
            or isinstance(self.stream_index, bool)
            or self.stream_index < 0
        ):
            raise SubtitleAuditError("embedded subtitle stream index is invalid")
        if self.source_codec not in _SUBTITLE_CODECS:
            raise SubtitleAuditError("embedded subtitle codec is invalid")
        if not self.source_language or len(self.source_language) > 32:
            raise SubtitleAuditError("embedded subtitle language is invalid")
        if len(self.source_title) > 200 or any(
            ord(character) < 32 for character in self.source_title
        ):
            raise SubtitleAuditError("embedded subtitle title is invalid")
        if any(
            not isinstance(value, bool)
            for value in (
                self.source_default,
                self.source_forced,
                self.source_hearing_impaired,
            )
        ):
            raise SubtitleAuditError("embedded subtitle disposition is invalid")
        if self.role not in {"standard", "forced", "sdh"}:
            raise SubtitleAuditError("embedded subtitle role is invalid")
        if (
            not isinstance(self.duration_ms, int)
            or isinstance(self.duration_ms, bool)
            or self.duration_ms <= 0
            or self.duration_ms > 24 * 60 * 60 * 1000
        ):
            raise SubtitleAuditError("embedded subtitle duration is invalid")
        object.__setattr__(self, "source", source.as_posix())

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "stream_index": self.stream_index,
            "source_codec": self.source_codec,
            "source_language": self.source_language,
            "source_title": self.source_title,
            "source_default": self.source_default,
            "source_forced": self.source_forced,
            "source_hearing_impaired": self.source_hearing_impaired,
            "role": self.role,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, payload: object) -> EmbeddedSubtitle:
        if not isinstance(payload, dict) or set(payload) != {
            "source",
            "stream_index",
            "source_codec",
            "source_language",
            "source_title",
            "source_default",
            "source_forced",
            "source_hearing_impaired",
            "role",
            "duration_ms",
        }:
            raise SubtitleAuditError("embedded subtitle has an unknown shape")
        return cls(**payload)


def _contained_relative_path(value: object, field: str) -> PurePosixPath:
    if not isinstance(value, str) or "\\" in value:
        raise SubtitleAuditError(f"{field} is not a contained relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SubtitleAuditError(f"{field} is not a contained relative path")
    return path


def _language_key(value: str) -> str:
    return value.strip().casefold().replace("_", "-").split("-", 1)[0]


def _milliseconds(value: object, field: str) -> int:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise SubtitleAuditError(f"{field} is invalid") from exc
    milliseconds = round(seconds * 1000)
    if milliseconds <= 0 or milliseconds > 24 * 60 * 60 * 1000:
        raise SubtitleAuditError(f"{field} is invalid")
    return milliseconds


def parse_ffprobe(payload: object) -> SubtitleProbe:
    """Parse the bounded ffprobe JSON contract used by the curator."""
    if not isinstance(payload, dict) or not {"format", "streams"} <= set(payload):
        raise SubtitleAuditError("ffprobe output has an unknown shape")
    optional = set(payload) - {"format", "streams"}
    if optional - {"programs", "stream_groups"} or any(payload[name] != [] for name in optional):
        raise SubtitleAuditError("ffprobe output has an unknown shape")
    format_value = payload["format"]
    streams = payload["streams"]
    if not isinstance(format_value, dict) or set(format_value) != {"duration"}:
        raise SubtitleAuditError("ffprobe format has an unknown shape")
    if not isinstance(streams, list) or len(streams) > 100:
        raise SubtitleAuditError("ffprobe streams are invalid")
    tracks: list[SubtitleTrack] = []
    for stream in streams:
        if not isinstance(stream, dict):
            raise SubtitleAuditError("ffprobe stream is invalid")
        if stream.get("codec_type") != "subtitle":
            continue
        codec = str(stream.get("codec_name") or "").casefold()
        if codec not in _SUBTITLE_CODECS:
            continue
        tags = stream.get("tags") or {}
        disposition = stream.get("disposition") or {}
        if not isinstance(tags, dict) or not isinstance(disposition, dict):
            raise SubtitleAuditError("ffprobe subtitle metadata is invalid")
        tracks.append(
            SubtitleTrack(
                index=int(stream.get("index")),
                codec=codec,
                language=str(tags.get("language") or "und"),
                title=str(tags.get("title") or ""),
                default=disposition.get("default") in {1, True, "1"},
                forced=disposition.get("forced") in {1, True, "1"},
                hearing_impaired=disposition.get("hearing_impaired") in {1, True, "1"},
            )
        )
    return SubtitleProbe(
        duration_ms=_milliseconds(format_value["duration"], "media duration"),
        tracks=tuple(sorted(tracks, key=lambda track: track.index)),
    )


def _content_is_english(value: bytes) -> bool:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        text = value.decode("utf-8", errors="replace")
    text = _SUBTITLE_TAG_RE.sub(" ", text.casefold())
    tokens = _ENGLISH_TOKEN_RE.findall(text)
    if len(tokens) < 30:
        return False
    hits = [token for token in tokens if token in _ENGLISH_WORDS]
    return len(hits) >= 12 and len(set(hits)) >= 8 and len(hits) / len(tokens) >= 0.12


def _classify_untagged_text_tracks(
    source: Path,
    probe: SubtitleProbe,
    *,
    ffmpeg: str,
    runner: Callable[..., subprocess.CompletedProcess],
) -> SubtitleProbe:
    classified: list[SubtitleTrack] = []
    probes = 0
    for track in probe.tracks:
        if (
            track.language != "und"
            or track.codec not in _TEXT_CODECS
            or probes >= _MAX_CONTENT_PROBES
        ):
            classified.append(track)
            continue
        probes += 1
        try:
            result = runner(
                [
                    ffmpeg,
                    "-v",
                    "error",
                    "-i",
                    str(source),
                    "-map",
                    f"0:{track.index}",
                    "-t",
                    str(_CONTENT_SAMPLE_SECONDS),
                    "-f",
                    "srt",
                    "-fs",
                    str(_MAX_CONTENT_SAMPLE_BYTES),
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            classified.append(track)
            continue
        output = result.stdout
        if isinstance(output, str):
            output = output.encode("utf-8")
        if not isinstance(output, bytes) or len(output) > _MAX_CONTENT_SAMPLE_BYTES:
            classified.append(track)
            continue
        classified.append(
            replace(track, language="eng-content") if _content_is_english(output) else track
        )
    return SubtitleProbe(duration_ms=probe.duration_ms, tracks=tuple(classified))


def probe_subtitles(
    source: Path,
    *,
    ffprobe: str = "/usr/bin/ffprobe",
    ffmpeg: str = "/usr/bin/ffmpeg",
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> SubtitleProbe:
    """Inspect metadata and bounded text content without changing the source."""
    result = runner(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration:"
                "stream=index,codec_type,codec_name:"
                "stream_tags=language,title:"
                "stream_disposition=default,forced,hearing_impaired"
            ),
            "-of",
            "json",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    if len(result.stdout) > 1024 * 1024:
        raise SubtitleAuditError("ffprobe output is too large")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubtitleAuditError("ffprobe output is invalid") from exc
    return _classify_untagged_text_tracks(
        source,
        parse_ffprobe(payload),
        ffmpeg=ffmpeg,
        runner=runner,
    )


class SubtitleProbeCache:
    """Bounded stat-keyed cache that writes only after a successful plan."""

    def __init__(
        self,
        path: Path,
        *,
        probe: Callable[[Path], SubtitleProbe],
    ) -> None:
        self.path = path
        self._probe = probe
        self._entries = self._read()
        self._dirty = False

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            payload_bytes = self.path.read_bytes()
            if len(payload_bytes) > _MAX_CACHE_BYTES:
                return {}
            payload = json.loads(payload_bytes)
            if (
                not isinstance(payload, dict)
                or set(payload) != {"schema_version", "entries"}
                or payload["schema_version"] != _CACHE_SCHEMA
                or not isinstance(payload["entries"], dict)
                or len(payload["entries"]) > _MAX_CACHE_ENTRIES
            ):
                return {}
            entries: dict[str, dict[str, object]] = {}
            for source, entry in payload["entries"].items():
                if (
                    not isinstance(source, str)
                    or not isinstance(entry, dict)
                    or set(entry) != {"size", "mtime_ns", "probe"}
                    or not isinstance(entry["size"], int)
                    or entry["size"] < 0
                    or not isinstance(entry["mtime_ns"], int)
                    or entry["mtime_ns"] < 0
                ):
                    return {}
                SubtitleProbe.from_dict(entry["probe"])
                entries[source] = entry
            return entries
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, SubtitleAuditError):
            return {}

    def probe(self, source: Path) -> SubtitleProbe:
        stat = source.stat()
        key = str(source)
        cached = self._entries.get(key)
        if (
            cached is not None
            and cached["size"] == stat.st_size
            and cached["mtime_ns"] == stat.st_mtime_ns
        ):
            return SubtitleProbe.from_dict(cached["probe"])
        result = self._probe(source)
        self._entries[key] = {
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "probe": result.to_dict(),
        }
        self._dirty = True
        return result

    def flush(self) -> None:
        if not self._dirty:
            return
        entries = dict(
            sorted(self._entries.items(), key=lambda item: item[0])[-_MAX_CACHE_ENTRIES:]
        )
        payload = json.dumps(
            {"schema_version": _CACHE_SCHEMA, "entries": entries},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if len(payload) > _MAX_CACHE_BYTES:
            raise SubtitleAuditError("subtitle probe cache is too large")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.parent / f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            self._dirty = False
        finally:
            temporary.unlink(missing_ok=True)


def _track_role(track: SubtitleTrack) -> str:
    title = track.title.casefold()
    if track.forced or re.search(r"\b(?:forced|signs?(?:\s*&\s*songs?)?)\b", title):
        return "forced"
    if track.hearing_impaired or re.search(r"\b(?:sdh|cc|hearing impaired)\b", title):
        return "sdh"
    return "standard"


def _selected_english_tracks(probe: SubtitleProbe) -> tuple[tuple[str, SubtitleTrack], ...]:
    english = [track for track in probe.tracks if _language_key(track.language) in _ENGLISH_CODES]
    if not english:
        raise SubtitleAuditError("media has no English subtitle")
    by_role: dict[str, list[SubtitleTrack]] = {}
    for track in english:
        by_role.setdefault(_track_role(track), []).append(track)
    if "standard" not in by_role:
        raise SubtitleAuditError("media has no full-dialogue English subtitle")
    selected: list[tuple[str, SubtitleTrack]] = []
    for role in ("standard", "forced", "sdh"):
        candidates = by_role.get(role)
        if not candidates:
            continue
        track = sorted(
            candidates,
            key=lambda item: (
                item.codec not in _TEXT_CODECS,
                not item.default,
                item.index,
            ),
        )[0]
        selected.append((role, track))
    return tuple(selected)


def plan_english_embedded(
    *,
    source: PurePosixPath,
    probe: SubtitleProbe,
) -> tuple[EmbeddedSubtitle, ...]:
    """Bind deterministic, release-matched English tracks without remote extraction."""
    return tuple(
        EmbeddedSubtitle(
            source=source.as_posix(),
            stream_index=track.index,
            source_codec=track.codec,
            source_language=track.language,
            source_title=track.title,
            source_default=track.default,
            source_forced=track.forced,
            source_hearing_impaired=track.hearing_impaired,
            role=role,
            duration_ms=probe.duration_ms,
        )
        for role, track in _selected_english_tracks(probe)
    )


def validate_embedded_subtitle(binding: EmbeddedSubtitle, probe: SubtitleProbe) -> None:
    """Revalidate one approved embedded track immediately before swap."""
    if probe.duration_ms != binding.duration_ms:
        raise SubtitleAuditError("embedded subtitle media duration changed")
    expected = (
        binding.stream_index,
        binding.source_codec,
        binding.source_language,
        binding.source_title,
        binding.source_default,
        binding.source_forced,
        binding.source_hearing_impaired,
        binding.role,
    )
    for track in probe.tracks:
        observed = (
            track.index,
            track.codec,
            track.language,
            track.title,
            track.default,
            track.forced,
            track.hearing_impaired,
            _track_role(track),
        )
        if observed == expected:
            return
    raise SubtitleAuditError("approved embedded subtitle track changed")
