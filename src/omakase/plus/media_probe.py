"""Bounded media stream and subtitle verification."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omakase.plus.media_store import record_media_file_probe, replace_subtitle_tracks
from omakase.plus.storage import (
    SUBTITLE_SUFFIXES,
    ConfiguredRoot,
    StorageSafetyError,
    file_receipt,
    normalize_locator,
    require_media_file,
    resolve_locator,
)

_MAX_PROBE_OUTPUT_BYTES = 2 * 1024 * 1024
_DEFAULT_RANGE_BYTES = 64 * 1024
_MAX_RANGE_BYTES = 1024 * 1024
_ENGLISH_LANGUAGES = frozenset({"en", "eng", "en-gb", "en-us"})


@dataclass(frozen=True, slots=True)
class ExternalSubtitle:
    """Explicit release-binding evidence for one sidecar subtitle."""

    locator: str
    language: str
    release_fingerprint: str
    sync_state: str

    def __post_init__(self) -> None:
        if not self.language.strip():
            raise ValueError("external subtitle language is required")
        if not self.release_fingerprint.strip():
            raise ValueError("external subtitle release fingerprint is required")
        if self.sync_state not in {"verified", "unverified"}:
            raise ValueError("external subtitle sync_state is invalid")


@dataclass(frozen=True, slots=True)
class MediaProbeResult:
    """Secret-safe stream facts ready for persistence."""

    outcome: str
    detail: str
    inventory_fingerprint: str
    byte_range_receipt: dict[str, Any]
    video_streams: tuple[dict[str, Any], ...]
    audio_streams: tuple[dict[str, Any], ...]
    subtitle_tracks: tuple[dict[str, Any], ...]

    @property
    def usable(self) -> bool:
        return self.outcome == "usable"


def probe_media_file(
    root: ConfiguredRoot,
    locator: str,
    *,
    inventory_fingerprint: str,
    external_subtitles: Sequence[ExternalSubtitle] = (),
    ffprobe_path: str = "ffprobe",
    timeout_seconds: float = 20.0,
    range_bytes: int = _DEFAULT_RANGE_BYTES,
    runner: Callable[..., Any] = subprocess.run,
) -> MediaProbeResult:
    """Probe one hash-bound media file without invoking a shell."""
    if timeout_seconds <= 0 or timeout_seconds > 120:
        raise ValueError("probe timeout must be between 0 and 120 seconds")
    if range_bytes <= 0 or range_bytes > _MAX_RANGE_BYTES:
        raise ValueError("probe range must be between 1 byte and 1 MiB")
    path = require_media_file(root, locator)
    receipt = file_receipt(root, locator)
    if receipt.sha256 != inventory_fingerprint:
        raise StorageSafetyError("media content changed after the approved plan")
    byte_receipt = _byte_range_receipt(path, range_bytes=range_bytes)
    command = [
        ffprobe_path,
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return _error_result(
            inventory_fingerprint,
            byte_receipt,
            "ffprobe could not inspect this media file",
        )
    stdout = str(getattr(completed, "stdout", ""))
    if int(getattr(completed, "returncode", 1)) != 0:
        return _error_result(
            inventory_fingerprint,
            byte_receipt,
            "ffprobe rejected this media file",
        )
    if len(stdout.encode("utf-8")) > _MAX_PROBE_OUTPUT_BYTES:
        return _error_result(
            inventory_fingerprint,
            byte_receipt,
            "ffprobe output exceeded the safety limit",
        )
    try:
        payload = json.loads(stdout)
    except (TypeError, json.JSONDecodeError):
        return _error_result(
            inventory_fingerprint,
            byte_receipt,
            "ffprobe returned invalid stream data",
        )
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    if not isinstance(streams, list):
        return _error_result(
            inventory_fingerprint,
            byte_receipt,
            "ffprobe returned invalid stream data",
        )

    videos: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    subtitles: list[dict[str, Any]] = []
    for raw in streams:
        if not isinstance(raw, dict):
            continue
        codec_type = str(raw.get("codec_type", ""))
        index = _nonnegative_int(raw.get("index"))
        codec = _bounded(str(raw.get("codec_name", "")), 100)
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        disposition = raw.get("disposition") if isinstance(raw.get("disposition"), dict) else {}
        language = _language(tags.get("language"))
        if codec_type == "video":
            videos.append(
                {
                    "stream_index": index,
                    "codec": codec,
                    "width": _nonnegative_int(raw.get("width")),
                    "height": _nonnegative_int(raw.get("height")),
                }
            )
        elif codec_type == "audio":
            audios.append(
                {
                    "stream_index": index,
                    "language": language,
                    "codec": codec,
                    "channels": _nonnegative_int(raw.get("channels")),
                    "title": _bounded(str(tags.get("title", "")), 200),
                }
            )
        elif codec_type == "subtitle":
            subtitles.append(
                {
                    "stream_index": index,
                    "language": language,
                    "codec": codec,
                    "switchable": True,
                    "default_track": bool(disposition.get("default", 0)),
                    "forced": bool(disposition.get("forced", 0)),
                    "source_kind": "embedded",
                    "sync_policy": "exact_release",
                }
            )

    for offset, external in enumerate(external_subtitles, start=1):
        external_locator = normalize_locator(external.locator)
        external_path = resolve_locator(root, external_locator)
        if external_path.suffix.casefold() not in SUBTITLE_SUFFIXES:
            raise StorageSafetyError("external subtitle extension is unsupported")
        exact_release = external.release_fingerprint == inventory_fingerprint
        verified = exact_release and external.sync_state == "verified"
        subtitles.append(
            {
                "stream_index": 100_000 + offset,
                "language": _language(external.language),
                "codec": external_path.suffix.casefold().lstrip("."),
                "switchable": True,
                "default_track": False,
                "forced": False,
                "source_kind": "external",
                "sync_policy": "verified_external" if verified else "unverified_external",
            }
        )

    trusted_english = any(
        track["language"] in _ENGLISH_LANGUAGES
        and (track["source_kind"] == "embedded" or track["sync_policy"] == "verified_external")
        for track in subtitles
    )
    reasons: list[str] = []
    if not videos:
        reasons.append("video stream missing")
    if not audios:
        reasons.append("audio stream missing")
    if not trusted_english:
        reasons.append("verified selectable English subtitles missing")
    outcome = "usable" if not reasons else "unusable"
    return MediaProbeResult(
        outcome=outcome,
        detail="; ".join(reasons),
        inventory_fingerprint=inventory_fingerprint,
        byte_range_receipt=byte_receipt,
        video_streams=tuple(videos),
        audio_streams=tuple(audios),
        subtitle_tracks=tuple(subtitles),
    )


def persist_probe_result(
    db,
    *,
    asset_id: int,
    file_locator: str,
    verified_at: str,
    result: MediaProbeResult,
) -> int:
    """Persist one probe and replace its complete subtitle inventory."""
    probe_id = record_media_file_probe(
        db,
        asset_id=asset_id,
        file_locator=normalize_locator(file_locator),
        inventory_fingerprint=result.inventory_fingerprint,
        byte_range_receipt=result.byte_range_receipt,
        video_streams=list(result.video_streams),
        audio_streams=list(result.audio_streams),
        outcome=result.outcome,
        detail=result.detail,
        verified_at=verified_at,
    )
    replace_subtitle_tracks(
        db,
        probe_id=probe_id,
        tracks=list(result.subtitle_tracks),
    )
    return probe_id


def _byte_range_receipt(path: Path, *, range_bytes: int) -> dict[str, Any]:
    size = path.stat().st_size
    count = min(size, range_bytes)
    with path.open("rb") as stream:
        first = stream.read(count)
        stream.seek(max(0, size - count))
        last = stream.read(count)
    return {
        "file_bytes": size,
        "range_bytes": count,
        "first_offset": 0,
        "first_sha256": hashlib.sha256(first).hexdigest(),
        "last_offset": max(0, size - count),
        "last_sha256": hashlib.sha256(last).hexdigest(),
    }


def _error_result(
    inventory_fingerprint: str,
    byte_receipt: dict[str, Any],
    detail: str,
) -> MediaProbeResult:
    return MediaProbeResult(
        outcome="error",
        detail=detail,
        inventory_fingerprint=inventory_fingerprint,
        byte_range_receipt=byte_receipt,
        video_streams=(),
        audio_streams=(),
        subtitle_tracks=(),
    )


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, normalized)


def _language(value: object) -> str:
    language = str(value or "und").strip().casefold().replace("_", "-")
    if len(language) < 2:
        language = "und"
    return _bounded(language or "und", 35)


def _bounded(value: str, limit: int) -> str:
    return value[:limit]
