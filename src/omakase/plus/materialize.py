"""Exact plan/apply/rollback for publishing local files into a Plex source root."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from omakase.plus.storage import (
    ConfiguredRoot,
    content_receipt,
    normalize_locator,
    resolve_locator,
)

MATERIALIZATION_PLAN_SCHEMA = "materialization-plan-v1"
MATERIALIZATION_RECEIPT_SCHEMA = "materialization-receipt-v1"
_SHA256 = frozenset("0123456789abcdef")
_STRATEGIES = frozenset({"auto", "hardlink", "reflink", "copy", "symlink"})
_COPY_RESERVE_BYTES = 256 * 1024 * 1024


class MaterializationError(ValueError):
    """A publication plan or filesystem mutation is unsafe."""


@dataclass(frozen=True, slots=True)
class MaterializationRequest:
    source_locator: str
    destination_locator: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_locator", normalize_locator(self.source_locator))
        object.__setattr__(
            self,
            "destination_locator",
            normalize_locator(self.destination_locator),
        )
        digest = self.content_sha256.casefold()
        if len(digest) != 64 or any(character not in _SHA256 for character in digest):
            raise MaterializationError("content_sha256 is invalid")
        object.__setattr__(self, "content_sha256", digest)


def plan_materialization(
    *,
    source_root: ConfiguredRoot,
    destination_root: ConfiguredRoot,
    requests: Sequence[MaterializationRequest],
    strategy: str = "auto",
) -> dict[str, Any]:
    """Return a read-only, hash-bound publication plan."""
    if source_root.owner_id != destination_root.owner_id:
        raise MaterializationError("source and destination roots have different owners")
    if strategy not in _STRATEGIES:
        raise MaterializationError("materialization strategy is unsupported")
    if not requests:
        raise MaterializationError("at least one materialization request is required")
    normalized = tuple(requests)
    destinations = [item.destination_locator.casefold() for item in normalized]
    if len(set(destinations)) != len(destinations):
        raise MaterializationError("materialization destinations must be unique")

    items: list[dict[str, Any]] = []
    copy_bytes = 0
    for request in sorted(normalized, key=lambda item: item.destination_locator):
        receipt = content_receipt(source_root, request.source_locator)
        if receipt.sha256 != request.content_sha256:
            raise MaterializationError("source content does not match its requested hash")
        _require_destination_absent(destination_root, request.destination_locator)
        selected = _select_strategy(
            strategy,
            source=resolve_locator(source_root, request.source_locator),
            destination_root=destination_root.path,
        )
        if selected == "copy":
            copy_bytes += receipt.bytes
        items.append(
            {
                "source_locator": request.source_locator,
                "destination_locator": request.destination_locator,
                "content_sha256": request.content_sha256,
                "bytes": receipt.bytes,
                "strategy": selected,
            }
        )
    if copy_bytes:
        free = shutil.disk_usage(destination_root.path).free
        if copy_bytes + _COPY_RESERVE_BYTES > free:
            raise MaterializationError("destination has insufficient safe copy capacity")

    unsigned = {
        "schema_version": MATERIALIZATION_PLAN_SCHEMA,
        "owner_id": source_root.owner_id,
        "source_root_fingerprint": _root_fingerprint(source_root),
        "destination_root_fingerprint": _root_fingerprint(destination_root),
        "copy_bytes": copy_bytes,
        "items": items,
    }
    return {**unsigned, "plan_hash": _hash(unsigned)}


def apply_materialization_plan(
    *,
    plan: object,
    expected_plan_hash: str,
    source_root: ConfiguredRoot,
    destination_root: ConfiguredRoot,
    reflink_runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Apply an approved plan without overwriting any existing destination."""
    validated = _validated_plan(plan)
    if not hmac.compare_digest(str(validated["plan_hash"]), expected_plan_hash.casefold()):
        raise MaterializationError("approved materialization plan hash does not match")
    _validate_roots(validated, source_root=source_root, destination_root=destination_root)

    created_files: list[dict[str, Any]] = []
    created_directories: list[str] = []
    try:
        for item in validated["items"]:
            source = resolve_locator(source_root, str(item["source_locator"]))
            source_receipt = content_receipt(source_root, str(item["source_locator"]))
            if (
                source_receipt.sha256 != item["content_sha256"]
                or source_receipt.bytes != item["bytes"]
            ):
                raise MaterializationError("materialization source changed after planning")
            destination = _destination_path(
                destination_root,
                str(item["destination_locator"]),
            )
            created_directories.extend(
                _ensure_contained_parents(
                    destination_root,
                    str(item["destination_locator"]),
                )
            )
            if destination.exists() or destination.is_symlink():
                raise MaterializationError("materialization destination already exists")
            _materialize_one(
                source,
                destination,
                strategy=str(item["strategy"]),
                reflink_runner=reflink_runner,
            )
            created_files.append(dict(item))
            if _file_sha256(destination) != item["content_sha256"]:
                raise MaterializationError("materialized content failed hash verification")
    except Exception:
        _rollback_created(
            destination_root,
            created_files=created_files,
            created_directories=created_directories,
        )
        raise

    receipt_unsigned = {
        "schema_version": MATERIALIZATION_RECEIPT_SCHEMA,
        "plan_hash": validated["plan_hash"],
        "owner_id": validated["owner_id"],
        "destination_root_fingerprint": validated["destination_root_fingerprint"],
        "created_files": created_files,
        "created_directories": list(dict.fromkeys(created_directories)),
    }
    return {**receipt_unsigned, "receipt_hash": _hash(receipt_unsigned)}


def rollback_materialization(
    *,
    receipt: object,
    destination_root: ConfiguredRoot,
) -> int:
    """Remove only exact, still-hash-matching files named by a valid receipt."""
    validated = _validated_receipt(receipt)
    if destination_root.owner_id != validated["owner_id"]:
        raise MaterializationError("rollback root owner does not match")
    if _root_fingerprint(destination_root) != validated["destination_root_fingerprint"]:
        raise MaterializationError("rollback destination root does not match")
    for item in validated["created_files"]:
        locator = str(item["destination_locator"])
        path = _destination_path(destination_root, locator)
        if not path.exists() and not path.is_symlink():
            continue
        if _file_sha256(path) != item["content_sha256"]:
            raise MaterializationError("rollback destination content changed")
    removed = _rollback_created(
        destination_root,
        created_files=list(validated["created_files"]),
        created_directories=list(validated["created_directories"]),
    )
    return removed


def _select_strategy(strategy: str, *, source: Path, destination_root: Path) -> str:
    if strategy != "auto":
        if strategy == "reflink" and shutil.which("cp") is None:
            raise MaterializationError("reflink strategy is unavailable")
        return strategy
    try:
        same_device = source.stat().st_dev == destination_root.stat().st_dev
    except OSError as exc:
        raise MaterializationError("materialization devices could not be inspected") from exc
    return "hardlink" if same_device else "copy"


def _materialize_one(
    source: Path,
    destination: Path,
    *,
    strategy: str,
    reflink_runner: Callable[..., Any],
) -> None:
    try:
        if strategy == "hardlink":
            os.link(source, destination)
        elif strategy == "symlink":
            relative_source = os.path.relpath(source, start=destination.parent)
            os.symlink(relative_source, destination)
        elif strategy == "copy":
            with source.open("rb") as input_stream, destination.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            shutil.copystat(source, destination, follow_symlinks=False)
        elif strategy == "reflink":
            completed = reflink_runner(
                [
                    "cp",
                    "--reflink=always",
                    "--no-clobber",
                    "--preserve=mode,timestamps",
                    str(source),
                    str(destination),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                check=False,
            )
            if int(getattr(completed, "returncode", 1)) != 0:
                raise MaterializationError("reflink creation failed")
        else:
            raise MaterializationError("materialization strategy is unsupported")
    except FileExistsError as exc:
        raise MaterializationError("materialization destination already exists") from exc
    except OSError as exc:
        raise MaterializationError("materialization filesystem operation failed") from exc


def _ensure_contained_parents(root: ConfiguredRoot, locator: str) -> list[str]:
    parts = PurePosixPath(normalize_locator(locator)).parts[:-1]
    current = root.path
    created: list[str] = []
    for part in parts:
        current = current / part
        relative = current.relative_to(root.path).as_posix()
        if current.exists() or current.is_symlink():
            try:
                resolved = current.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise MaterializationError("destination parent is unavailable") from exc
            if not resolved.is_relative_to(root.path) or not resolved.is_dir():
                raise MaterializationError("destination parent escapes its configured root")
            continue
        try:
            current.mkdir()
        except OSError as exc:
            raise MaterializationError("destination parent could not be created") from exc
        created.append(relative)
    return created


def _require_destination_absent(root: ConfiguredRoot, locator: str) -> None:
    path = _destination_path(root, locator)
    parent = path.parent
    while not parent.exists() and parent != root.path:
        parent = parent.parent
    try:
        resolved_parent = parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MaterializationError("destination parent is unavailable") from exc
    if not resolved_parent.is_relative_to(root.path):
        raise MaterializationError("destination escapes its configured root")
    if path.exists() or path.is_symlink():
        raise MaterializationError("materialization destination already exists")


def _destination_path(root: ConfiguredRoot, locator: str) -> Path:
    normalized = normalize_locator(locator)
    return root.path.joinpath(*PurePosixPath(normalized).parts)


def _rollback_created(
    root: ConfiguredRoot,
    *,
    created_files: list[dict[str, Any]],
    created_directories: list[str],
) -> int:
    removed = 0
    for item in reversed(created_files):
        path = _destination_path(root, str(item["destination_locator"]))
        if path.exists() or path.is_symlink():
            path.unlink()
            removed += 1
    for locator in reversed(list(dict.fromkeys(created_directories))):
        directory = _destination_path(root, locator)
        try:
            directory.rmdir()
        except OSError:
            continue
    return removed


def _validate_roots(
    plan: dict[str, Any],
    *,
    source_root: ConfiguredRoot,
    destination_root: ConfiguredRoot,
) -> None:
    if source_root.owner_id != plan["owner_id"] or destination_root.owner_id != plan["owner_id"]:
        raise MaterializationError("materialization root owner does not match")
    if _root_fingerprint(source_root) != plan["source_root_fingerprint"]:
        raise MaterializationError("materialization source root does not match")
    if _root_fingerprint(destination_root) != plan["destination_root_fingerprint"]:
        raise MaterializationError("materialization destination root does not match")


def _validated_plan(plan: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "owner_id",
        "source_root_fingerprint",
        "destination_root_fingerprint",
        "copy_bytes",
        "items",
        "plan_hash",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != expected
        or plan.get("schema_version") != MATERIALIZATION_PLAN_SCHEMA
    ):
        raise MaterializationError("materialization plan is invalid")
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if not _valid_hash(plan.get("plan_hash")) or not hmac.compare_digest(
        str(plan["plan_hash"]),
        _hash(unsigned),
    ):
        raise MaterializationError("materialization plan hash is invalid")
    if not isinstance(plan.get("items"), list) or not plan["items"]:
        raise MaterializationError("materialization plan items are invalid")
    return plan


def _validated_receipt(receipt: object) -> dict[str, Any]:
    expected = {
        "schema_version",
        "plan_hash",
        "owner_id",
        "destination_root_fingerprint",
        "created_files",
        "created_directories",
        "receipt_hash",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected
        or receipt.get("schema_version") != MATERIALIZATION_RECEIPT_SCHEMA
    ):
        raise MaterializationError("materialization receipt is invalid")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash"}
    if not _valid_hash(receipt.get("receipt_hash")) or not hmac.compare_digest(
        str(receipt["receipt_hash"]),
        _hash(unsigned),
    ):
        raise MaterializationError("materialization receipt hash is invalid")
    if not isinstance(receipt.get("created_files"), list) or not isinstance(
        receipt.get("created_directories"), list
    ):
        raise MaterializationError("materialization receipt entries are invalid")
    return receipt


def _root_fingerprint(root: ConfiguredRoot) -> str:
    return hashlib.sha256(f"{root.owner_id}:{root.path}".encode()).hexdigest()


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256 for character in value)
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MaterializationError("materialized file could not be verified") from exc
    return digest.hexdigest()
