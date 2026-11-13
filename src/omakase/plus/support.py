"""Privacy-preserving support bundles with a deliberately narrow payload."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

from omakase.plus.doctor import DoctorReport, report_payload
from omakase.plus.setup import SetupState, setup_progress

SUPPORT_BUNDLE_SCHEMA = "omakase-plus-support-v1"
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_MAGNET_RE = re.compile(r"(?i)magnet:\?[^\s\"']+")
_TRACKER_RE = re.compile(r"(?i)https?://[^\s\"']*(?:announce|tracker)[^\s\"']*")
_MEDIA_FILENAME_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])[^/\\\r\n]{1,180}\.(?:mkv|mp4|m4v|avi|ass|ssa|srt|vtt)"
)
_WINDOWS_PATH_RE = re.compile(r"(?i)\b[A-Z]:\\(?:[^<>:\"|?*\r\n]+\\)*[^<>:\"|?*\r\n]*")
_POSIX_PATH_RE = re.compile(r"(?<![\w:])/(?:[^/\s]+/)+[^/\s]*")


class SupportBundleError(ValueError):
    """Support bundle input or destination is unsafe."""


def create_support_bundle(
    output_path: Path,
    *,
    report: DoctorReport,
    app_version: str,
    created_at: str,
    setup_state: SetupState | None = None,
    log_lines: Iterable[str] = (),
) -> dict[str, str | int]:
    """Write a bounded ZIP that excludes databases, names, history, and secrets."""
    if output_path.exists():
        raise SupportBundleError("support bundle destination already exists")
    if not app_version or len(app_version) > 80:
        raise SupportBundleError("application version is invalid")
    lines = list(log_lines)
    if len(lines) > 2_000:
        lines = lines[-2_000:]
    sanitized_log = "\n".join(_redact_line(line) for line in lines)
    if len(sanitized_log.encode("utf-8")) > 512 * 1024:
        sanitized_log = sanitized_log.encode("utf-8")[-512 * 1024 :].decode(
            "utf-8",
            errors="replace",
        )

    summary: dict[str, object] = {
        "schema_version": SUPPORT_BUNDLE_SCHEMA,
        "created_at": created_at,
        "app_version": app_version,
        "doctor_ready": report.ready,
        "included": ["summary.json", "doctor.json"],
        "excluded": [
            "database",
            "secrets",
            "magnets",
            "tracker_urls",
            "full_filenames",
            "watch_history",
        ],
    }
    if setup_state is not None:
        summary["setup"] = setup_progress(setup_state)
    if sanitized_log:
        summary["included"].append("logs/redacted.log")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=output_path.parent,
            prefix=f".{output_path.name}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
        with zipfile.ZipFile(
            temporary_name,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            _write_json(archive, "summary.json", summary)
            _write_json(archive, "doctor.json", report_payload(report))
            if sanitized_log:
                archive.writestr("logs/redacted.log", sanitized_log + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, output_path)
        os.chmod(output_path, 0o600)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)

    return {
        "schema_version": SUPPORT_BUNDLE_SCHEMA,
        "path": str(output_path.resolve()),
        "bytes": output_path.stat().st_size,
        "sha256": _sha256(output_path),
    }


def inspect_support_bundle(path: Path) -> dict[str, object]:
    """Read the safe summary without extracting arbitrary archive entries."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if (
                len(names) > 4
                or any(
                    name.startswith(("/", "\\")) or ".." in Path(name).parts or not name
                    for name in names
                )
                or "summary.json" not in names
            ):
                raise SupportBundleError("support bundle entries are invalid")
            raw = archive.read("summary.json")
    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        raise SupportBundleError("support bundle is unavailable or invalid") from exc
    if len(raw) > 64 * 1024:
        raise SupportBundleError("support summary exceeded the safety limit")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupportBundleError("support summary is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SUPPORT_BUNDLE_SCHEMA:
        raise SupportBundleError("support summary schema is unsupported")
    return payload


def _redact_line(value: str) -> str:
    line = str(value).replace("\x00", "")[:4_096]
    line = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", line)
    line = _MAGNET_RE.sub("<magnet:redacted>", line)
    line = _TRACKER_RE.sub("<tracker:redacted>", line)
    line = _MEDIA_FILENAME_RE.sub(_hashed_match("media"), line)
    line = _WINDOWS_PATH_RE.sub(_hashed_match("path"), line)
    line = _POSIX_PATH_RE.sub(_hashed_match("path"), line)
    return line


def _hashed_match(kind: str):
    def replace(match: re.Match) -> str:
        digest = hashlib.sha256(match.group(0).encode("utf-8", errors="replace")).hexdigest()[:12]
        return f"<{kind}:{digest}>"

    return replace


def _write_json(archive: zipfile.ZipFile, name: str, payload: object) -> None:
    buffer = io.StringIO()
    json.dump(payload, buffer, ensure_ascii=False, indent=2, sort_keys=True)
    buffer.write("\n")
    archive.writestr(name, buffer.getvalue())


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
