"""Safe local-storage resolution and content receipts."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

MEDIA_SUFFIXES = frozenset({".avi", ".m2ts", ".m4v", ".mkv", ".mp4", ".ts"})
SUBTITLE_SUFFIXES = frozenset({".ass", ".srt", ".ssa", ".vtt"})
_HASH_CHUNK_BYTES = 1024 * 1024


class StorageSafetyError(ValueError):
    """A configured storage path or requested locator is unsafe."""


@dataclass(frozen=True, slots=True)
class ConfiguredRoot:
    """One canonical import root owned by one Omakase user."""

    owner_id: int
    path: Path

    def __post_init__(self) -> None:
        if (
            isinstance(self.owner_id, bool)
            or not isinstance(self.owner_id, int)
            or self.owner_id <= 0
        ):
            raise StorageSafetyError("storage root owner must be a positive integer")
        if not self.path.is_absolute():
            raise StorageSafetyError("storage root must be absolute")


@dataclass(frozen=True, slots=True)
class FileReceipt:
    """Hash-bound, stable evidence for one local file."""

    locator: str
    bytes: int
    modified_ns: int
    sha256: str


def configure_root(path: str | os.PathLike[str], *, owner_id: int) -> ConfiguredRoot:
    """Resolve an existing directory into a canonical ownership-scoped root."""
    raw = Path(path)
    if not raw.is_absolute():
        raise StorageSafetyError("storage root must be absolute")
    try:
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageSafetyError("storage root is unavailable") from exc
    if not resolved.is_dir():
        raise StorageSafetyError("storage root must be a directory")
    return ConfiguredRoot(owner_id=owner_id, path=resolved)


def normalize_locator(locator: str) -> str:
    """Normalize a user locator without permitting absolute or parent paths."""
    if not isinstance(locator, str):
        raise TypeError("storage locator must be a string")
    value = locator.strip().replace("\\", "/")
    if not value or "\x00" in value:
        raise StorageSafetyError("storage locator is invalid")
    pure = PurePosixPath(value)
    drive_like = bool(pure.parts and ":" in pure.parts[0])
    if (
        pure.is_absolute()
        or pure.drive
        or drive_like
        or any(part in {"", ".", ".."} for part in pure.parts)
    ):
        raise StorageSafetyError("storage locator must be a contained relative path")
    return pure.as_posix()


def resolve_locator(
    root: ConfiguredRoot,
    locator: str,
    *,
    file_only: bool = True,
) -> Path:
    """Resolve a relative locator and reject traversal or symlink escape."""
    normalized = normalize_locator(locator)
    requested = root.path.joinpath(*PurePosixPath(normalized).parts)
    try:
        resolved = requested.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageSafetyError("storage locator is unavailable") from exc
    if not resolved.is_relative_to(root.path):
        raise StorageSafetyError("storage locator escapes its configured root")
    if file_only and not resolved.is_file():
        raise StorageSafetyError("storage locator must identify a file")
    return resolved


def locator_for_path(root: ConfiguredRoot, path: str | os.PathLike[str]) -> str:
    """Return a stable relative locator for a contained existing path."""
    try:
        resolved = Path(path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise StorageSafetyError("storage path is unavailable") from exc
    if not resolved.is_relative_to(root.path):
        raise StorageSafetyError("storage path escapes its configured root")
    return resolved.relative_to(root.path).as_posix()


def require_media_file(root: ConfiguredRoot, locator: str) -> Path:
    """Resolve one supported, nonempty local media file."""
    resolved = resolve_locator(root, locator)
    if resolved.suffix.casefold() not in MEDIA_SUFFIXES:
        raise StorageSafetyError("storage locator is not a supported media file")
    if resolved.stat().st_size <= 0:
        raise StorageSafetyError("media file must not be empty")
    return resolved


def file_receipt(root: ConfiguredRoot, locator: str) -> FileReceipt:
    """Stream a complete SHA-256 without loading the media file into memory."""
    resolved = require_media_file(root, locator)
    return _receipt_for_resolved(root, locator, resolved)


def content_receipt(root: ConfiguredRoot, locator: str) -> FileReceipt:
    """Hash any nonempty contained file, including an external subtitle."""
    resolved = resolve_locator(root, locator)
    if resolved.stat().st_size <= 0:
        raise StorageSafetyError("storage file must not be empty")
    return _receipt_for_resolved(root, locator, resolved)


def _receipt_for_resolved(
    root: ConfiguredRoot,
    locator: str,
    resolved: Path,
) -> FileReceipt:
    digest = hashlib.sha256()
    with resolved.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    stat = resolved.stat()
    return FileReceipt(
        locator=normalize_locator(locator),
        bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        sha256=digest.hexdigest(),
    )
