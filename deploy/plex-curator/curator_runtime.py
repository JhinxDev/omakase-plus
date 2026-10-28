"""Locked CT111 runtime for planning, applying, and reporting Plex curation."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import unicodedata
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import httpx
from plex_client import (
    GENRE_COLLECTIONS,
    SHELF_COLLECTIONS,
    PlexClient,
    PlexInventoryChangedError,
    PlexLibraryNames,
    PlexSection,
    TopologyPlan,
    TopologyPlanError,
    TopologyRemovalProof,
    anime_bucket,
    managed_collection_tags,
    managed_media_title,
    plan_library_topology,
    read_plex_token,
)
from plex_curator import (
    CuratorConfig,
    CuratorValidationError,
    OverlayApplyResult,
    OverlayPlan,
    PlexMatchHint,
    PublishedTitle,
    TrustedAnimeFolder,
    apply_overlay_plan,
    build_overlay_plan,
)
from subtitle_support import (
    EmbeddedSubtitle,
    SubtitleAuditError,
    SubtitleProbe,
    SubtitleProbeCache,
    probe_subtitles,
    validate_embedded_subtitle,
)

_RUN_PLAN_SCHEMA = "curator-run-plan-v2"
_COLLECTION_PLAN_SCHEMA = "anime-collection-plan-v2"
_CONFIG_SCHEMA = "curator-config-v6"
_PLEX_REQUEST_TIMEOUT_SECONDS = 120.0
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HELD_LOCKS: set[str] = set()
_HELD_LOCKS_GUARD = threading.Lock()


class CuratorBusyError(RuntimeError):
    """The global curator lock is already held."""


class PlexScanBusyError(RuntimeError):
    """An active Plex scan temporarily prevents safe planning or publication."""


class RuntimeContractError(ValueError):
    """A runtime config, approved plan, proof, or remote contract is invalid."""


@dataclass(frozen=True, slots=True)
class PlexRuntimeConfig:
    base_url: str
    container_name: str
    overlay_path: str
    preferences_xml: Path
    riven_movies_path: str
    library_names: PlexLibraryNames


@dataclass(frozen=True, slots=True)
class NetworkRuntimeConfig:
    manifest_url: str
    receipt_url: str
    state_root: Path
    lock_path: Path


@dataclass(frozen=True, slots=True)
class SubtitleRuntimeConfig:
    ffprobe_path: str
    require_english_subtitle: bool


@dataclass(frozen=True, slots=True)
class LoadedRuntimeConfig:
    filesystem: CuratorConfig
    plex: PlexRuntimeConfig
    runtime: NetworkRuntimeConfig
    subtitles: SubtitleRuntimeConfig
    collection_overrides: dict[str, str]
    config_digest: str


@dataclass(frozen=True, slots=True)
class CollectionDecision:
    rating_key: str
    title: str
    existing_collections: tuple[str, ...]
    desired_collection: str
    desired_shelves: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "rating_key": self.rating_key,
            "title": self.title,
            "existing_collections": list(self.existing_collections),
            "desired_collection": self.desired_collection,
            "desired_shelves": list(self.desired_shelves),
        }

    @classmethod
    def from_dict(cls, payload: object) -> CollectionDecision:
        value = _object(
            payload,
            {
                "rating_key",
                "title",
                "existing_collections",
                "desired_collection",
                "desired_shelves",
            },
            "collection decision",
        )
        rating_key = value["rating_key"]
        title = value["title"]
        existing = value["existing_collections"]
        desired = value["desired_collection"]
        if not isinstance(rating_key, str) or not rating_key.isdigit() or int(rating_key) < 1:
            raise RuntimeContractError("collection rating key is invalid")
        if not isinstance(title, str) or not title or len(title) > 300:
            raise RuntimeContractError("collection title is invalid")
        if (
            not isinstance(existing, list)
            or len(existing) > 100
            or any(not isinstance(item, str) or not item for item in existing)
            or len(set(existing)) != len(existing)
        ):
            raise RuntimeContractError("existing collections are invalid")
        if not isinstance(desired, str) or desired not in GENRE_COLLECTIONS:
            raise RuntimeContractError("desired collection is unmanaged")
        shelves = value["desired_shelves"]
        if (
            not isinstance(shelves, list)
            or len(shelves) > 2
            or any(
                not isinstance(shelf, str) or shelf not in SHELF_COLLECTIONS for shelf in shelves
            )
            or len(set(shelves)) != len(shelves)
        ):
            raise RuntimeContractError("desired viewing shelves are invalid")
        return cls(rating_key, title, tuple(existing), desired, tuple(shelves))


@dataclass(frozen=True, slots=True)
class CollectionPlan:
    decisions: tuple[CollectionDecision, ...]
    plan_hash: str

    def _unsigned(self) -> dict:
        return {
            "schema_version": _COLLECTION_PLAN_SCHEMA,
            "decisions": [decision.to_dict() for decision in self.decisions],
        }

    def to_dict(self) -> dict:
        return {**self._unsigned(), "plan_hash": self.plan_hash}

    @classmethod
    def create(cls, decisions: tuple[CollectionDecision, ...]) -> CollectionPlan:
        ordered = tuple(sorted(decisions, key=lambda item: int(item.rating_key)))
        provisional = cls(ordered, "")
        return cls.from_dict(
            {
                **provisional._unsigned(),
                "plan_hash": hashlib.sha256(_canonical_bytes(provisional._unsigned())).hexdigest(),
            }
        )

    @classmethod
    def from_dict(cls, payload: object) -> CollectionPlan:
        value = _object(
            payload,
            {"schema_version", "decisions", "plan_hash"},
            "collection plan",
        )
        if value["schema_version"] != _COLLECTION_PLAN_SCHEMA:
            raise RuntimeContractError("collection plan schema is unsupported")
        raw_decisions = value["decisions"]
        if not isinstance(raw_decisions, list) or len(raw_decisions) > 10_000:
            raise RuntimeContractError("collection decisions are invalid")
        decisions = tuple(CollectionDecision.from_dict(item) for item in raw_decisions)
        rating_keys = [item.rating_key for item in decisions]
        if len(set(rating_keys)) != len(rating_keys):
            raise RuntimeContractError("collection rating keys are duplicated")
        plan_hash = _sha256(value["plan_hash"], "collection plan hash")
        plan = cls(decisions, plan_hash)
        calculated = hashlib.sha256(_canonical_bytes(plan._unsigned())).hexdigest()
        if not hmac.compare_digest(calculated, plan_hash):
            raise RuntimeContractError("collection plan hash does not match its content")
        return plan


@dataclass(frozen=True, slots=True)
class ApprovedRunPlan:
    created_at: str
    config_digest: str
    manifest_generated_at: str
    overlay_plan: OverlayPlan
    topology_plan: TopologyPlan
    collection_plan: CollectionPlan
    plan_hash: str

    def _unsigned(self) -> dict:
        return {
            "schema_version": _RUN_PLAN_SCHEMA,
            "created_at": self.created_at,
            "config_digest": self.config_digest,
            "manifest_generated_at": self.manifest_generated_at,
            "overlay_plan": self.overlay_plan.to_dict(),
            "topology_plan": self.topology_plan.to_dict(),
            "collection_plan": self.collection_plan.to_dict(),
        }

    def to_dict(self) -> dict:
        return {**self._unsigned(), "plan_hash": self.plan_hash}

    @classmethod
    def create(
        cls,
        *,
        config_digest: str,
        manifest_generated_at: str,
        overlay_plan: OverlayPlan,
        topology_plan: TopologyPlan,
        collection_plan: CollectionPlan,
        created_at: str | None = None,
    ) -> ApprovedRunPlan:
        provisional = cls(
            created_at=created_at or _utc_now(),
            config_digest=config_digest,
            manifest_generated_at=manifest_generated_at,
            overlay_plan=overlay_plan,
            topology_plan=topology_plan,
            collection_plan=collection_plan,
            plan_hash="",
        )
        plan_hash = hashlib.sha256(_canonical_bytes(provisional._unsigned())).hexdigest()
        return cls.from_dict({**provisional._unsigned(), "plan_hash": plan_hash})

    @classmethod
    def from_dict(cls, payload: dict) -> ApprovedRunPlan:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "created_at",
            "config_digest",
            "manifest_generated_at",
            "overlay_plan",
            "topology_plan",
            "collection_plan",
            "plan_hash",
        }:
            raise RuntimeContractError("approved plan has an unknown shape")
        if payload["schema_version"] != _RUN_PLAN_SCHEMA:
            raise RuntimeContractError("approved plan schema is unsupported")
        created_at = _timestamp(payload["created_at"], "plan creation")
        manifest_generated_at = _timestamp(payload["manifest_generated_at"], "manifest generation")
        config_digest = _sha256(payload["config_digest"], "config digest")
        plan_hash = _sha256(payload["plan_hash"], "plan hash")
        plan = cls(
            created_at=created_at,
            config_digest=config_digest,
            manifest_generated_at=manifest_generated_at,
            overlay_plan=OverlayPlan.from_dict(payload["overlay_plan"]),
            topology_plan=TopologyPlan.from_dict(payload["topology_plan"]),
            collection_plan=CollectionPlan.from_dict(payload["collection_plan"]),
            plan_hash=plan_hash,
        )
        calculated = hashlib.sha256(_canonical_bytes(plan._unsigned())).hexdigest()
        if not hmac.compare_digest(calculated, plan.plan_hash):
            raise RuntimeContractError("approved plan hash does not match its content")
        return plan


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise RuntimeContractError(f"{field} is not UTC RFC 3339 seconds")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise RuntimeContractError(f"{field} is invalid") from exc
    return value


def _sha256(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise RuntimeContractError(f"{field} is not a lowercase SHA-256")
    return value


def _object(value: object, fields: set[str], name: str) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeContractError(f"{name} has an unknown shape")
    return value


def _posix_absolute(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value.endswith("/"):
        raise RuntimeContractError(f"{name} is not a canonical absolute path")
    if "/../" in f"{value}/" or "/./" in f"{value}/":
        raise RuntimeContractError(f"{name} is not a canonical absolute path")
    return value


def _endpoint(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise RuntimeContractError(f"{name} is invalid")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.fragment:
        raise RuntimeContractError(f"{name} is invalid")
    return value


def load_runtime_config(path: Path) -> LoadedRuntimeConfig:
    """Load one exact, secret-free curator-config-v6 document."""
    try:
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("curator config is unavailable or invalid") from exc
    root = _object(
        payload,
        {
            "schema_version",
            "source_root",
            "overlay_root",
            "riven_roots",
            "mount_aliases",
            "excluded_source_folders",
            "trusted_allowlist",
            "plex",
            "runtime",
            "subtitles",
            "collection_overrides",
            "metadata_match_hints",
        },
        "curator config",
    )
    if root["schema_version"] != _CONFIG_SCHEMA:
        raise RuntimeContractError("curator config schema is unsupported")
    source_root = _posix_absolute(root["source_root"], "source root")
    overlay_root = _posix_absolute(root["overlay_root"], "overlay root")
    riven_roots = root["riven_roots"]
    if not isinstance(riven_roots, list):
        raise RuntimeContractError("Riven roots are invalid")
    normalized_riven = tuple(Path(_posix_absolute(value, "Riven root")) for value in riven_roots)
    aliases = root["mount_aliases"]
    if not isinstance(aliases, dict):
        raise RuntimeContractError("mount aliases are invalid")
    normalized_aliases = tuple(
        (
            Path(_posix_absolute(alias, "mount alias")),
            Path(_posix_absolute(target, "mount target")),
        )
        for alias, target in sorted(aliases.items())
    )
    excluded = root["excluded_source_folders"]
    if not isinstance(excluded, list) or any(not isinstance(item, str) for item in excluded):
        raise RuntimeContractError("excluded source folders are invalid")

    allowlist = _object(
        root["trusted_allowlist"], {"schema_version", "entries"}, "trusted allowlist"
    )
    if allowlist["schema_version"] != "trusted-anime-allowlist-v1" or not isinstance(
        allowlist["entries"], list
    ):
        raise RuntimeContractError("trusted allowlist is invalid")
    trusted: list[TrustedAnimeFolder] = []
    for entry in allowlist["entries"]:
        trusted.append(
            TrustedAnimeFolder(
                **_object(
                    entry,
                    {
                        "source_folder",
                        "section",
                        "canonical_title",
                        "season_number",
                        "reason",
                    },
                    "trusted allowlist entry",
                )
            )
        )

    plex = _object(
        root["plex"],
        {
            "base_url",
            "container_name",
            "overlay_path",
            "preferences_xml",
            "riven_movies_path",
            "library_names",
        },
        "Plex config",
    )
    container = plex["container_name"]
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", container):
        raise RuntimeContractError("Plex container name is invalid")
    library_names = _object(
        plex["library_names"],
        {"anime", "anime_movies", "tv", "movies"},
        "Plex library names",
    )
    try:
        parsed_library_names = PlexLibraryNames(**library_names)
    except (TypeError, TopologyPlanError) as exc:
        raise RuntimeContractError("Plex library names are invalid") from exc
    runtime = _object(
        root["runtime"],
        {"manifest_url", "receipt_url", "state_root", "lock_path"},
        "runtime config",
    )
    subtitles = _object(
        root["subtitles"],
        {"ffprobe_path", "require_english_subtitle"},
        "subtitle config",
    )
    if not isinstance(subtitles["require_english_subtitle"], bool):
        raise RuntimeContractError("subtitle requirement is invalid")
    overrides = _object(
        root["collection_overrides"], {"schema_version", "entries"}, "collection overrides"
    )
    if overrides["schema_version"] != "anime-collection-overrides-v1" or not isinstance(
        overrides["entries"], dict
    ):
        raise RuntimeContractError("collection overrides are invalid")
    for identity, bucket in overrides["entries"].items():
        if not isinstance(identity, str) or not identity or bucket not in GENRE_COLLECTIONS:
            raise RuntimeContractError("collection override entry is invalid")
    match_hints = _object(
        root["metadata_match_hints"],
        {"schema_version", "entries"},
        "metadata match hints",
    )
    if match_hints["schema_version"] != "plex-match-hints-v1" or not isinstance(
        match_hints["entries"], list
    ):
        raise RuntimeContractError("metadata match hints are invalid")
    try:
        plex_match_hints = tuple(PlexMatchHint.from_dict(entry) for entry in match_hints["entries"])
    except CuratorValidationError as exc:
        raise RuntimeContractError("metadata match-hint entry is invalid") from exc

    filesystem = CuratorConfig(
        source_root=Path(source_root),
        overlay_root=Path(overlay_root),
        riven_roots=normalized_riven,
        mount_aliases=normalized_aliases,
        trusted_allowlist=tuple(trusted),
        excluded_source_folders=tuple(excluded),
        plex_match_hints=plex_match_hints,
    )
    return LoadedRuntimeConfig(
        filesystem=filesystem,
        plex=PlexRuntimeConfig(
            base_url=_endpoint(plex["base_url"], "Plex base URL"),
            container_name=container,
            overlay_path=_posix_absolute(plex["overlay_path"], "Plex overlay path"),
            preferences_xml=Path(_posix_absolute(plex["preferences_xml"], "Plex Preferences.xml")),
            riven_movies_path=_posix_absolute(plex["riven_movies_path"], "Riven movies path"),
            library_names=parsed_library_names,
        ),
        runtime=NetworkRuntimeConfig(
            manifest_url=_endpoint(runtime["manifest_url"], "manifest URL"),
            receipt_url=_endpoint(runtime["receipt_url"], "receipt URL"),
            state_root=Path(_posix_absolute(runtime["state_root"], "state root")),
            lock_path=Path(_posix_absolute(runtime["lock_path"], "lock path")),
        ),
        subtitles=SubtitleRuntimeConfig(
            ffprobe_path=_posix_absolute(subtitles["ffprobe_path"], "ffprobe path"),
            require_english_subtitle=subtitles["require_english_subtitle"],
        ),
        collection_overrides=dict(overrides["entries"]),
        config_digest=hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
    )


def write_approved_plan(path: Path, plan: ApprovedRunPlan) -> None:
    """Atomically persist an exact 0600 dry-run plan."""
    plan = ApprovedRunPlan.from_dict(plan.to_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(plan.to_dict()))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        if os.name != "nt":
            parent = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_approved_plan(path: Path) -> ApprovedRunPlan:
    try:
        return ApprovedRunPlan.from_dict(json.loads(path.read_bytes()))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("approved plan is unavailable or invalid") from exc


@contextmanager
def curator_lock(path: Path) -> Iterator[None]:
    """Hold one non-blocking process-wide and OS-wide curator lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _HELD_LOCKS_GUARD:
        if key in _HELD_LOCKS:
            raise CuratorBusyError("curator run already active")
        _HELD_LOCKS.add(key)
    stream = path.open("a+b")
    acquired = False
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise CuratorBusyError("curator run already active") from exc
        else:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise CuratorBusyError("curator run already active") from exc
        acquired = True
        yield
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()
        with _HELD_LOCKS_GUARD:
            _HELD_LOCKS.discard(key)


def _machine_token() -> str:
    token = os.getenv("OMAKASE_LIBRARY_MANIFEST_TOKEN", "")
    if not token or len(token) > 512 or any(character.isspace() for character in token):
        raise RuntimeContractError("machine credential is unavailable")
    return token


def _fetch_manifest(config: LoadedRuntimeConfig, token: str) -> dict:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if os.getenv("OMAKASE_PLUS_LANGUAGE_PUBLICATION_QUALIFIED", "false").strip().lower() == "true":
        from omakase.plus.language_publication import CAPABILITY, LANGUAGE_PUBLICATION_QUALIFIED

        if LANGUAGE_PUBLICATION_QUALIFIED:
            headers["X-Omakase-Curator-Capabilities"] = CAPABILITY
    try:
        response = httpx.get(
            config.runtime.manifest_url,
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
        raise RuntimeContractError("manifest fetch failed") from exc
    if not isinstance(payload, dict):
        raise RuntimeContractError("manifest response is invalid")
    return payload


def _fetch_organization(config: LoadedRuntimeConfig, token: str) -> dict:
    endpoint = config.runtime.manifest_url.rsplit("/", 1)[0] + "/plex-organization"
    try:
        response = httpx.get(endpoint, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        response.raise_for_status()
        if len(response.content) > 4_000_000:
            raise RuntimeContractError("Plex organization exceeds its byte limit")
        return _validate_organization(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeContractError(
            "Plex organization unavailable; shelving was not changed"
        ) from exc


def _validate_organization(payload: object) -> dict:
    value = _object(payload, {"schema_version", "generated_at", "titles"}, "Plex organization")
    if value["schema_version"] != "plex-organization-v1":
        raise RuntimeContractError("Plex organization schema is unsupported")
    generated = _timestamp(value["generated_at"], "organization timestamp")
    age = (
        datetime.now(timezone.utc)
        - datetime.strptime(generated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    ).total_seconds()
    if age < -60 or age > 900:
        raise RuntimeContractError("Plex organization is stale")
    titles = value["titles"]
    if not isinstance(titles, list) or len(titles) > 10_000:
        raise RuntimeContractError("organization titles are invalid")
    ids = set()
    for title in titles:
        item = _object(
            title,
            {"anilist_id", "canonical_title", "title_aliases", "season_number", "shelves"},
            "organization title",
        )
        identity = item["anilist_id"]
        if type(identity) is not int or identity <= 0 or identity in ids:
            raise RuntimeContractError("organization identity is invalid")
        ids.add(identity)
        if (
            not isinstance(item["canonical_title"], str)
            or not 1 <= len(item["canonical_title"]) <= 300
        ):
            raise RuntimeContractError("organization title is invalid")
        aliases = item["title_aliases"]
        if (
            not isinstance(aliases, list)
            or len(aliases) > 50
            or any(not isinstance(alias, str) or not 1 <= len(alias) <= 300 for alias in aliases)
        ):
            raise RuntimeContractError("organization aliases are invalid")
        season = item["season_number"]
        if season is not None and (type(season) is not int or not 1 <= season <= 99):
            raise RuntimeContractError("organization season is invalid")
        shelves = item["shelves"]
        if (
            not isinstance(shelves, list)
            or len(shelves) > 2
            or any(
                not isinstance(shelf, str) or shelf not in SHELF_COLLECTIONS for shelf in shelves
            )
            or len(set(shelves)) != len(shelves)
        ):
            raise RuntimeContractError("organization shelf is invalid")
    return value


def _submit_receipt(config: LoadedRuntimeConfig, token: str, receipt: dict) -> None:
    try:
        response = httpx.post(
            config.runtime.receipt_url,
            headers={"Authorization": f"Bearer {token}"},
            json=receipt,
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeContractError("receipt submission failed") from exc


def _sync_library_snapshot(config: LoadedRuntimeConfig, token: str) -> None:
    """Submit a fresh read-only Plex inventory after a successful live run."""
    client = _plex_client(config)
    try:
        snapshot = client.library_snapshot(
            library_names=config.plex.library_names,
            generated_at=_utc_now(),
            overlay_path=config.plex.overlay_path,
        )
    finally:
        client.close()
    snapshot_url = f"{config.runtime.receipt_url.rsplit('/', 1)[0]}/plex-library-snapshot"
    try:
        response = httpx.post(
            snapshot_url,
            headers={"Authorization": f"Bearer {token}"},
            json=snapshot,
            timeout=30,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeContractError("Plex library snapshot submission failed") from exc


def _plex_client(config: LoadedRuntimeConfig) -> PlexClient:
    token = read_plex_token(config.plex.preferences_xml)
    return PlexClient(
        config.plex.base_url,
        token,
        timeout=_PLEX_REQUEST_TIMEOUT_SECONDS,
    )


def _require_idle_plex(client: PlexClient) -> None:
    scans = client.active_library_scans()
    if not scans:
        return
    detail = "; ".join(
        " · ".join(value for value in (scan.title, scan.subtitle, scan.progress) if value)
        for scan in scans
    )
    raise PlexScanBusyError(f"Plex library scan is active ({detail}); retry after it completes")


def _subtitle_probe_cache(config: LoadedRuntimeConfig) -> SubtitleProbeCache:
    return SubtitleProbeCache(
        config.runtime.state_root / "subtitle-probe-cache-v2.json",
        probe=lambda source: probe_subtitles(
            source,
            ffprobe=config.subtitles.ffprobe_path,
        ),
    )


def _flush_probe_cache(probe_cache: SubtitleProbeCache) -> None:
    try:
        probe_cache.flush()
    except SubtitleAuditError as exc:
        raise RuntimeContractError("subtitle probe cache could not be persisted") from exc


def _build_approved_plan(
    config: LoadedRuntimeConfig,
    manifest: dict,
    sections: tuple[PlexSection, ...],
    collection_plan: CollectionPlan,
    *,
    probe_cache: SubtitleProbeCache | None = None,
    persist_probe_cache: bool = True,
) -> ApprovedRunPlan:
    probe_cache = probe_cache if probe_cache is not None else _subtitle_probe_cache(config)
    language_probe = None
    if manifest.get("schema_version") == "manifest-v3":
        from functools import partial

        from language_publication import probe_file

        language_probe = partial(probe_file, ffprobe=config.subtitles.ffprobe_path)
    try:
        overlay = build_overlay_plan(
            config.filesystem,
            manifest,
            subtitle_probe=probe_cache.probe,
            require_english_subtitles=config.subtitles.require_english_subtitle,
            language_probe=language_probe,
        )
    except Exception:
        if persist_probe_cache:
            try:
                probe_cache.flush()
            except SubtitleAuditError:
                pass
        raise
    if persist_probe_cache:
        _flush_probe_cache(probe_cache)
    topology = plan_library_topology(
        sections,
        current_overlay=f"{config.plex.overlay_path}/current",
        riven_movies=config.plex.riven_movies_path,
        library_names=config.plex.library_names,
    )
    return ApprovedRunPlan.create(
        config_digest=config.config_digest,
        manifest_generated_at=manifest["generated_at"],
        overlay_plan=overlay,
        topology_plan=topology,
        collection_plan=collection_plan,
    )


def _probe_plex_paths(config: LoadedRuntimeConfig, paths: tuple[str, ...]) -> tuple[str, ...]:
    readable: list[str] = []
    for path in paths:
        result = subprocess.run(
            ["docker", "exec", config.plex.container_name, "test", "-r", path],
            check=False,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            readable.append(path)
    overlay_paths = (
        f"{config.plex.overlay_path}/current/Anime",
        f"{config.plex.overlay_path}/current/Anime Movies",
    )
    broken = subprocess.run(
        [
            "docker",
            "exec",
            config.plex.container_name,
            "find",
            "-L",
            *overlay_paths,
            "-type",
            "l",
            "-print",
            "-quit",
        ],
        check=False,
        capture_output=True,
        timeout=30,
    )
    if broken.returncode != 0 or broken.stdout.strip():
        readable = [path for path in readable if path not in overlay_paths]
    return tuple(readable)


def _require_readable_plex_paths(
    config: LoadedRuntimeConfig,
    paths: tuple[str, ...],
) -> tuple[str, ...]:
    readable = _probe_plex_paths(config, paths)
    unreadable = tuple(path for path in paths if path not in readable)
    if unreadable:
        raise RuntimeContractError(
            "Plex path preflight found unreadable or broken paths: " + ", ".join(unreadable)
        )
    return readable


def _series_tree_fingerprints(root: Path) -> dict[str, str]:
    if not root.is_dir() or root.is_symlink():
        raise RuntimeContractError("overlay section is not a real directory")
    fingerprints: dict[str, str] = {}
    for series in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if series.is_symlink() or not series.is_dir():
            raise RuntimeContractError("overlay section has an invalid top-level entry")
        records: list[tuple[str, str, str]] = []
        for candidate in sorted(series.rglob("*"), key=lambda path: str(path)):
            relative = candidate.relative_to(series).as_posix()
            if candidate.is_symlink():
                records.append(("link", relative, os.readlink(candidate)))
            elif candidate.is_dir():
                records.append(("directory", relative, ""))
            elif candidate.is_file():
                records.append(
                    ("file", relative, hashlib.sha256(candidate.read_bytes()).hexdigest())
                )
            else:
                raise RuntimeContractError("overlay series has an unsupported filesystem entry")
        fingerprints[series.name] = hashlib.sha256(_canonical_bytes(records)).hexdigest()
    return fingerprints


def _overlay_scan_targets(
    config: LoadedRuntimeConfig,
    result: OverlayApplyResult,
) -> tuple[tuple[str, str | None], ...]:
    """Return the smallest safe Plex scan set for one atomic overlay change."""
    if not result.changed:
        return ()
    previous = result.previous_generation
    targets: list[tuple[str, str | None]] = []
    for section in ("Anime", "Anime Movies"):
        current_root = result.generation / section
        if previous is None or not (previous / section).is_dir():
            targets.append((section, None))
            continue
        prior = _series_tree_fingerprints(previous / section)
        current = _series_tree_fingerprints(current_root)
        if set(prior) - set(current):
            targets.append((section, None))
            continue
        changed = sorted(
            (title for title, digest in current.items() if prior.get(title) != digest),
            key=str.casefold,
        )
        targets.extend(
            (
                section,
                f"{config.plex.overlay_path}/current/{section}/{title}",
            )
            for title in changed
        )
    return tuple(targets)


def _configured_section(
    sections: tuple[PlexSection, ...],
    title: str,
    role: str,
) -> PlexSection:
    matches = [section for section in sections if section.title.casefold() == title.casefold()]
    if len(matches) != 1:
        raise RuntimeContractError(f"configured {role} library is absent or ambiguous")
    return matches[0]


def _section_observation(
    client: PlexClient,
    library_names: PlexLibraryNames,
) -> tuple[tuple[PlexSection, ...], list[dict]]:
    actual_sections = client.inventory_sections()
    canonical_sections: list[PlexSection] = []
    observed: list[dict] = []
    for logical_title, configured_title in library_names.logical_to_actual().items():
        section = _configured_section(actual_sections, configured_title, logical_title)
        canonical_sections.append(PlexSection(section.key, logical_title, section.paths))
        observed.append(
            {
                "key": section.key,
                "title": logical_title,
                "count": client.section_count(section.key),
            }
        )
    return tuple(canonical_sections), observed


def _normalized_identity(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", value).split())


def _manifest_identity_keys(title: dict) -> set[str]:
    season = title["season_number"]
    keys: set[str] = set()
    for value in (title["canonical_title"], *title["title_aliases"]):
        normalized = _normalized_identity(value)
        if not normalized:
            continue
        keys.add(normalized)
        suffixes = [" final season"]
        if isinstance(season, int) and not isinstance(season, bool):
            suffixes.extend((f" season {season}", f" s{season}", f" {season}"))
        for suffix in suffixes:
            if normalized.endswith(suffix):
                base = normalized[: -len(suffix)].strip()
                if base:
                    keys.add(base)
    return keys


def _title_coverage(
    client: PlexClient,
    manifest: dict,
    sections: tuple[PlexSection, ...],
    library_names: PlexLibraryNames = PlexLibraryNames(),
    *,
    overlay_path: str = "/library/symlinks/omakase",
    published_titles: tuple[PublishedTitle, ...] | None = None,
) -> list[dict]:
    """Report Plex visibility, bounded by the exact signed publication coverage."""
    publications: dict[tuple[int, str, int | None], PublishedTitle] = {}
    if published_titles is not None:
        for publication in published_titles:
            key = (
                publication.anilist_id,
                publication.section,
                publication.season_number,
            )
            if key in publications:
                raise RuntimeContractError("signed title publication is duplicated")
            publications[key] = publication

    def publication_for(title: dict, section_name: str) -> PublishedTitle | None:
        if published_titles is None:
            return None
        season_number = title["season_number"]
        publication = publications.get((title["anilist_id"], section_name, season_number))
        if publication is None:
            return None
        if len(publication.usable_episodes) != int(title["coverage"]["usable_count"]):
            raise RuntimeContractError("signed title publication does not match manifest coverage")
        return publication

    needed_sections: set[str] = set()
    for title in manifest["titles"]:
        section_name = "Anime Movies" if title["format"] == "MOVIE" else "Anime"
        publication = publication_for(title, section_name)
        if publication is None or publication.published_episodes:
            needed_sections.add(section_name)
    configured_sections: dict[str, PlexSection] = {}
    if "Anime" in needed_sections:
        configured_sections["Anime"] = _configured_section(
            sections,
            library_names.anime,
            "anime",
        )
    if "Anime Movies" in needed_sections:
        configured_sections["Anime Movies"] = _configured_section(
            sections,
            library_names.anime_movies,
            "anime movies",
        )
    inventories: dict[str, tuple] = {
        section_name: client.inventory_anime(section.key)
        for section_name, section in configured_sections.items()
    }
    movie_path_identities: dict[str, str] = {}
    if "Anime Movies" in inventories:
        prefix = PurePosixPath(overlay_path, "current", "Anime Movies")
        for item in inventories["Anime Movies"]:
            try:
                folder_identities = {
                    PurePosixPath(path).relative_to(prefix).parts[0]
                    for path in client.item_media_paths(item.rating_key)
                }
            except (IndexError, TopologyPlanError, ValueError):
                continue
            if len(folder_identities) == 1:
                movie_path_identities[item.rating_key] = _normalized_identity(
                    folder_identities.pop()
                )
    coverage: list[dict] = []
    for title in manifest["titles"]:
        section_name = "Anime Movies" if title["format"] == "MOVIE" else "Anime"
        usable = int(title["coverage"]["usable_count"])
        publication = publication_for(title, section_name)
        published_count = usable if publication is None else len(publication.published_episodes)
        if section_name == "Anime" and publication is not None:
            season = int(title["season_number"] or 1)
            path_matches = []
            for item in inventories.get(section_name, ()):
                episodes = {
                    episode
                    for episode, path in client.season_episode_paths(item.rating_key, season)
                    if episode in publication.published_episodes
                    and managed_media_title(
                        path,
                        overlay_path=overlay_path,
                        section="Anime",
                        season=season,
                        episode=episode,
                    )
                    == title["canonical_title"]
                }
                if episodes:
                    path_matches.append(episodes)
            if len(path_matches) > 1:
                raise RuntimeContractError("Plex managed season identity is ambiguous")
            coverage.append(
                {
                    "anilist_id": title["anilist_id"],
                    "section": section_name,
                    "season_number": title["season_number"],
                    "usable_count": usable,
                    "plex_visible_count": len(path_matches[0]) if path_matches else 0,
                }
            )
            continue
        matches = []
        if published_count:
            identity_keys = _manifest_identity_keys(title)
            matches = [
                item
                for item in inventories[section_name]
                if _normalized_identity(item.title) in identity_keys
            ]
            if not matches and section_name == "Anime Movies":
                matches = [
                    item
                    for item in inventories[section_name]
                    if movie_path_identities.get(item.rating_key) in identity_keys
                ]
        if len(matches) > 1:
            raise RuntimeContractError("Plex title identity is ambiguous")
        if not matches:
            visible = 0
        elif section_name == "Anime Movies":
            visible = min(published_count, 1)
        else:
            season = int(title["season_number"] or 1)
            visible = min(
                published_count,
                client.season_episode_count(matches[0].rating_key, season),
            )
        coverage.append(
            {
                "anilist_id": title["anilist_id"],
                "section": section_name,
                "season_number": title["season_number"],
                "usable_count": usable,
                "plex_visible_count": visible,
            }
        )
    return coverage


def _held_title_coverage(plan: OverlayPlan) -> list[dict]:
    """Report no current Plex visibility when every safe manifest asset was blocked."""
    return [
        {
            "anilist_id": publication.anilist_id,
            "section": publication.section,
            "season_number": publication.season_number,
            "usable_count": len(publication.usable_episodes),
            "plex_visible_count": 0,
        }
        for publication in plan.title_publications
    ]


def _build_collection_plan(
    client: PlexClient,
    manifest: dict,
    section_key: int,
    overrides: dict[str, str],
    *,
    overlay_path: str = "/library/symlinks/omakase",
    organization: dict | None = None,
) -> CollectionPlan:
    """Resolve every current Anime item once and hash its exact proposed state."""
    for title in manifest["titles"]:
        if title["format"] == "MOVIE":
            continue
        identity_keys = _manifest_identity_keys(title)
        matching_overrides = [
            identity for identity in overrides if _normalized_identity(identity) in identity_keys
        ]
        if len(matching_overrides) > 1:
            raise RuntimeContractError("manifest collection identity is ambiguous")
        try:
            if matching_overrides:
                anime_bucket((), identity=matching_overrides[0], overrides=overrides)
            else:
                anime_bucket(title["genres"])
        except TopologyPlanError as exc:
            raise RuntimeContractError("manifest collection identity is unresolved") from exc
    decisions: list[CollectionDecision] = []
    inventory = client.inventory_anime(section_key)
    for item in inventory:
        if item.title in overrides:
            desired = anime_bucket((), identity=item.title, overrides=overrides)
        else:
            normalized = _normalized_identity(item.title)
            matches = [
                title
                for title in manifest["titles"]
                if title["format"] != "MOVIE" and normalized in _manifest_identity_keys(title)
            ]
            if not matches:
                prefix = PurePosixPath(overlay_path, "current", "Anime")
                try:
                    media_paths = client.series_media_paths(item.rating_key)
                    folder_identities = {
                        PurePosixPath(path).relative_to(prefix).parts[0] for path in media_paths
                    }
                except (IndexError, TopologyPlanError, ValueError) as exc:
                    raise RuntimeContractError(
                        f"Plex collection identity is unresolved for {item.title!r}"
                    ) from exc
                if len(folder_identities) != 1:
                    raise RuntimeContractError(
                        f"Plex collection paths are ambiguous for {item.title!r}"
                    )
                folder_identity = _normalized_identity(folder_identities.pop())
                matches = [
                    title
                    for title in manifest["titles"]
                    if title["format"] != "MOVIE"
                    and folder_identity in _manifest_identity_keys(title)
                ]
                if not matches:
                    managed = tuple(
                        collection
                        for collection in item.collections
                        if collection in GENRE_COLLECTIONS
                    )
                    if len(managed) != 1:
                        raise RuntimeContractError(
                            f"Plex collection path identity is unresolved for {item.title!r}"
                        )
                    # A removed title remains in Plex until the new managed overlay
                    # is swapped and scanned. Bind its exact current state without
                    # mutating the collection during that transition.
                    desired = managed[0]
            if matches:
                desired_buckets = {anime_bucket(tuple(match["genres"])) for match in matches}
                if len(desired_buckets) != 1:
                    ids = ", ".join(str(match["anilist_id"]) for match in matches)
                    raise RuntimeContractError(
                        f"Plex collection identity is ambiguous for {item.title!r} "
                        f"across AniList IDs {ids}"
                    )
                desired = desired_buckets.pop()
        if organization is None:
            # Standalone legacy callers have no status evidence. Preserve that
            # axis until the new private endpoint supplies complete state.
            shelves = tuple(tag for tag in item.collections if tag in SHELF_COLLECTIONS)
        else:
            normalized = _normalized_identity(item.title)
            status_matches = [
                title
                for title in organization["titles"]
                if normalized in _manifest_identity_keys(title)
            ]
            if not status_matches:
                prefix = PurePosixPath(overlay_path, "current", "Anime")
                try:
                    folders = {
                        PurePosixPath(path).relative_to(prefix).parts[0]
                        for path in client.series_media_paths(item.rating_key)
                    }
                except (AttributeError, IndexError, TopologyPlanError, ValueError):
                    folders = set()
                folder_keys = {_normalized_identity(folder) for folder in folders}
                status_matches = [
                    title
                    for title in organization["titles"]
                    if folder_keys & _manifest_identity_keys(title)
                ]
            shelves = tuple(
                sorted({shelf for title in status_matches for shelf in title["shelves"]})
            )
        decisions.append(
            CollectionDecision(
                rating_key=item.rating_key,
                title=item.title,
                existing_collections=item.collections,
                desired_collection=desired,
                desired_shelves=shelves,
            )
        )
    return CollectionPlan.create(tuple(decisions))


def _apply_collection_plan(
    client: PlexClient,
    *,
    section_key: int,
    plan: CollectionPlan,
) -> None:
    """Apply only the exact rating-key decisions approved by dry-run."""
    observed = tuple(
        sorted(
            (
                item.rating_key,
                item.title,
                item.collections,
            )
            for item in client.inventory_anime(section_key)
        )
    )
    expected = tuple(
        sorted(
            (
                decision.rating_key,
                decision.title,
                decision.existing_collections,
            )
            for decision in plan.decisions
        )
    )
    if observed != expected:
        raise RuntimeContractError("Plex collection state changed after approval")
    for decision in plan.decisions:
        desired_tags = managed_collection_tags(
            decision.existing_collections,
            (decision.desired_collection, *decision.desired_shelves),
        )
        if set(desired_tags) == set(decision.existing_collections):
            continue
        client.set_managed_collection(
            section_key=section_key,
            rating_key=decision.rating_key,
            existing=decision.existing_collections,
            desired=(decision.desired_collection, *decision.desired_shelves),
            dry_run=False,
        )


def _receipt(
    *,
    plan: ApprovedRunPlan,
    mode: str,
    started_at: str,
    sections: tuple[PlexSection, ...],
    observed: list[dict],
    title_coverage: list[dict],
    topology_converged: bool | None = None,
) -> dict:
    if topology_converged is not None and not isinstance(topology_converged, bool):
        raise RuntimeContractError("receipt topology convergence is invalid")
    exact = {section.title for section in sections} == set(
        ("Anime", "Anime Movies", "TV Shows", "Movies")
    )
    converged = (
        mode == "live" and exact and len(sections) == 4
        if topology_converged is None
        else topology_converged
    )
    counts = {item["title"]: item["count"] for item in observed} if converged else None
    completed_at = _utc_now()
    blocked_assets = [
        {**asset.to_dict(), "last_observed_at": completed_at}
        for asset in plan.overlay_plan.blocked_assets
    ]
    outcome = "degraded" if blocked_assets or not converged and mode == "live" else "success"
    receipt = {
        "schema_version": "curator-receipt-v2",
        "run_id": f"curator-{plan.plan_hash[:24]}",
        "started_at": started_at,
        "completed_at": completed_at,
        "outcome": outcome,
        "mode": mode,
        "topology_converged": converged,
        "manifest_generated_at": plan.manifest_generated_at,
        "observed_sections": observed,
        "converged_section_counts": counts,
        "title_coverage": title_coverage,
        "blocked_assets": blocked_assets,
        "errors": [],
    }

    if plan.overlay_plan.language_v3:
        receipt["schema_version"] = "curator-receipt-v3"
        receipt["language_files"] = list(plan.overlay_plan.language_files)
    return receipt


def _read_removal_proof(path: Path, expected_plan_hash: str) -> TopologyRemovalProof:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeContractError("topology removal proof is unavailable or invalid") from exc
    fields = {
        "plan_hash",
        "scans_succeeded",
        "first_observed_at",
        "first_counts",
        "second_observed_at",
        "second_counts",
        "playback_http_status",
        "playback_bytes",
        "unmatched_required_media",
        "invincible_tv_seasons",
        "invincible_anime_matches",
    }
    payload = _object(payload, fields, "topology removal proof")
    try:
        proof = TopologyRemovalProof(
            plan_hash=payload["plan_hash"],
            scans_succeeded=payload["scans_succeeded"],
            first_observed_at=datetime.fromisoformat(
                payload["first_observed_at"].replace("Z", "+00:00")
            ),
            first_counts=payload["first_counts"],
            second_observed_at=datetime.fromisoformat(
                payload["second_observed_at"].replace("Z", "+00:00")
            ),
            second_counts=payload["second_counts"],
            playback_http_status=payload["playback_http_status"],
            playback_bytes=payload["playback_bytes"],
            unmatched_required_media=payload["unmatched_required_media"],
            invincible_tv_seasons=tuple(payload["invincible_tv_seasons"]),
            invincible_anime_matches=payload["invincible_anime_matches"],
        )
        proof.validate(expected_plan_hash=expected_plan_hash)
    except (AttributeError, TypeError, ValueError, TopologyPlanError) as exc:
        raise RuntimeContractError("topology removal proof is invalid") from exc
    return proof


@dataclass(frozen=True, slots=True)
class _PreparedDryRun:
    plan: ApprovedRunPlan
    receipt: dict
    probe_cache: SubtitleProbeCache


def _prepare_dry_run(
    config: LoadedRuntimeConfig,
    token: str,
    *,
    probe_cache: SubtitleProbeCache | None = None,
) -> _PreparedDryRun:
    probe_cache = probe_cache if probe_cache is not None else _subtitle_probe_cache(config)
    manifest = _fetch_manifest(config, token)
    organization = _fetch_organization(config, token)
    client = _plex_client(config)
    try:
        _require_idle_plex(client)
        started_at = _utc_now()
        sections = client.inventory_sections()
        anime_section = _configured_section(
            sections,
            config.plex.library_names.anime,
            "anime",
        )
        collection_plan = _build_collection_plan(
            client,
            manifest,
            anime_section.key,
            config.collection_overrides,
            overlay_path=config.plex.overlay_path,
            organization=organization,
        )
        plan = _build_approved_plan(
            config,
            manifest,
            sections,
            collection_plan,
            probe_cache=probe_cache,
            persist_probe_cache=False,
        )
        canonical_sections, observed = _section_observation(
            client,
            config.plex.library_names,
        )
        receipt = _receipt(
            plan=plan,
            mode="dry-run",
            started_at=started_at,
            sections=canonical_sections,
            observed=observed,
            title_coverage=_title_coverage(
                client,
                manifest,
                sections,
                config.plex.library_names,
                overlay_path=config.plex.overlay_path,
                published_titles=plan.overlay_plan.title_publications,
            ),
        )
        return _PreparedDryRun(plan, receipt, probe_cache)
    finally:
        client.close()


def _publish_dry_run(
    config: LoadedRuntimeConfig, plan_path: Path, token: str, prepared: _PreparedDryRun
) -> None:
    _flush_probe_cache(prepared.probe_cache)
    write_approved_plan(plan_path, prepared.plan)
    _submit_receipt(config, token, prepared.receipt)


def _dry_run(config: LoadedRuntimeConfig, plan_path: Path, token: str) -> ApprovedRunPlan:
    probe_cache = _subtitle_probe_cache(config)
    try:
        prepared = _prepare_dry_run(config, token, probe_cache=probe_cache)
    except Exception:
        try:
            probe_cache.flush()
        except SubtitleAuditError:
            pass
        raise
    _publish_dry_run(config, plan_path, token, prepared)
    return prepared.plan


def _live(
    config: LoadedRuntimeConfig,
    plan_path: Path,
    backup_receipt: Path,
    token: str,
    removal_proof_path: Path | None,
    *,
    prepared: _PreparedDryRun | None = None,
) -> OverlayApplyResult:
    approved = read_approved_plan(plan_path) if prepared is None else prepared.plan
    if not hmac.compare_digest(approved.config_digest, config.config_digest):
        raise RuntimeContractError("approved plan config digest differs from live config")
    manifest = _fetch_manifest(config, token)
    organization = _fetch_organization(config, token)
    client = _plex_client(config)
    try:
        _require_idle_plex(client)
        sections = client.inventory_sections()
        anime_section = _configured_section(
            sections,
            config.plex.library_names.anime,
            "anime",
        )
        collection_plan = _build_collection_plan(
            client,
            manifest,
            anime_section.key,
            config.collection_overrides,
            overlay_path=config.plex.overlay_path,
            organization=organization,
        )
        if prepared is None:
            recomputed = _build_approved_plan(config, manifest, sections, collection_plan)
        else:
            recomputed = _build_approved_plan(
                config,
                manifest,
                sections,
                collection_plan,
                probe_cache=prepared.probe_cache,
                persist_probe_cache=False,
            )
        if (
            recomputed.overlay_plan.plan_hash != approved.overlay_plan.plan_hash
            or recomputed.topology_plan.plan_hash != approved.topology_plan.plan_hash
            or recomputed.collection_plan.plan_hash != approved.collection_plan.plan_hash
        ):
            raise RuntimeContractError("live state differs from the approved dry-run plan")
        if prepared is not None:
            _publish_dry_run(config, plan_path, token, prepared)
        started_at = _utc_now()
        if approved.overlay_plan.holds_current_generation:
            held = apply_overlay_plan(
                config.filesystem,
                approved.overlay_plan,
                expected_plan_hash=approved.overlay_plan.plan_hash,
            )
            receipt = _receipt(
                plan=approved,
                mode="live",
                started_at=started_at,
                sections=(),
                observed=[],
                title_coverage=_held_title_coverage(approved.overlay_plan),
                topology_converged=False,
            )
            _submit_receipt(config, token, receipt)
            return held

        def mutate_and_report(overlay_result: OverlayApplyResult) -> None:
            readable = _require_readable_plex_paths(
                config,
                approved.topology_plan.intended_paths,
            )
            topology_mutations = client.apply_topology(
                approved.topology_plan,
                backup_receipt=backup_receipt,
                readable_paths=readable,
                dry_run=False,
            )
            sections = client.inventory_sections()
            by_title = {section.title.casefold(): section for section in sections}
            logical_to_actual = config.plex.library_names.logical_to_actual()
            scan_targets: dict[str, set[str] | None] = {}
            for logical_title, path in _overlay_scan_targets(config, overlay_result):
                title = logical_to_actual[logical_title]
                if path is None:
                    scan_targets[title] = None
                elif title not in scan_targets:
                    scan_targets[title] = {path}
                elif scan_targets[title] is not None:
                    scan_targets[title].add(path)
            by_key = {section.key: section.title for section in sections}
            for mutation in topology_mutations:
                if mutation.key is None:
                    scan_targets[config.plex.library_names.movies] = None
                elif mutation.key in by_key:
                    scan_targets[by_key[mutation.key]] = None
            if removal_proof_path is not None:
                scan_targets.update(dict.fromkeys(config.plex.library_names.actual_titles()))
            _apply_collection_plan(
                client,
                section_key=by_title[config.plex.library_names.anime.casefold()].key,
                plan=approved.collection_plan,
            )
            for title in config.plex.library_names.actual_titles():
                if title not in scan_targets:
                    continue
                section = by_title.get(title.casefold())
                if section is None:
                    raise TopologyPlanError("intended Plex section is absent after mutation")
                paths = scan_targets[title]
                if paths is None:
                    client.scan_section(section.key)
                else:
                    for path in sorted(paths):
                        client.scan_section(section.key, path=path)
            if removal_proof_path is not None:
                proof = _read_removal_proof(removal_proof_path, approved.topology_plan.plan_hash)
                client.remove_deferred_sections(approved.topology_plan, proof, dry_run=False)
            canonical_sections, observed = _section_observation(
                client,
                config.plex.library_names,
            )
            receipt = _receipt(
                plan=approved,
                mode="live",
                started_at=started_at,
                sections=canonical_sections,
                observed=observed,
                title_coverage=_title_coverage(
                    client,
                    manifest,
                    sections,
                    config.plex.library_names,
                    overlay_path=config.plex.overlay_path,
                    published_titles=approved.overlay_plan.title_publications,
                ),
            )
            _submit_receipt(config, token, receipt)

        live_probe_cache = SubtitleProbeCache(
            config.runtime.state_root / "subtitle-probe-cache-v2.json",
            probe=lambda source: probe_subtitles(
                source,
                ffprobe=config.subtitles.ffprobe_path,
            ),
        )
        live_probes: dict[Path, SubtitleProbe] = {}

        def validate_embedded(source: Path, binding: EmbeddedSubtitle) -> None:
            probe = live_probes.get(source)
            if probe is None:
                probe = live_probe_cache.probe(source)
                live_probes[source] = probe
            validate_embedded_subtitle(binding, probe)

        return apply_overlay_plan(
            config.filesystem,
            approved.overlay_plan,
            expected_plan_hash=approved.overlay_plan.plan_hash,
            after_swap=mutate_and_report,
            embedded_subtitle_validator=validate_embedded,
        )
    finally:
        client.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Omakase Plex curator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("dry-run", "live", "auto"):
        command = subparsers.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--plan", type=Path, required=True)
        if name in {"live", "auto"}:
            command.add_argument("--backup-receipt", type=Path, required=True)
            command.add_argument("--removal-proof", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_runtime_config(args.config)
        token = _machine_token()
        with curator_lock(config.runtime.lock_path):
            if args.command == "dry-run":
                plan = _dry_run(config, args.plan, token)
                summary = {"outcome": "success", "mode": "dry-run", "plan_hash": plan.plan_hash}
            elif args.command == "live":
                result = _live(
                    config,
                    args.plan,
                    args.backup_receipt,
                    token,
                    args.removal_proof,
                )
                _sync_library_snapshot(config, token)
                summary = {
                    "outcome": "success",
                    "mode": "live",
                    "plan_hash": result.plan_hash,
                    "changed": result.changed,
                }
            else:
                prepared = _prepare_dry_run(config, token)
                result = _live(
                    config,
                    args.plan,
                    args.backup_receipt,
                    token,
                    args.removal_proof,
                    prepared=prepared,
                )
                _sync_library_snapshot(config, token)
                summary = {
                    "outcome": "success",
                    "mode": "auto",
                    "plan_hash": prepared.plan.plan_hash,
                    "changed": result.changed,
                }
        print(json.dumps(summary, sort_keys=True))
        return 0
    except CuratorBusyError:
        print('{"code":"LOCK_HELD","outcome":"degraded"}', file=sys.stderr)
        return 75
    except PlexScanBusyError:
        print(
            json.dumps(
                {
                    "code": "PLEX_SCAN_ACTIVE",
                    "outcome": "deferred",
                    "mode": args.command,
                    "changed": False,
                    "retryable": True,
                    "detail": "Plex is scanning its library; retry after the scan completes.",
                },
                sort_keys=True,
            )
        )
        return 0
    except PlexInventoryChangedError:
        # This can occur before publication or during the final read afterward.
        # Do not assert changed=False, publish an empty snapshot, or infer absence.
        print(
            json.dumps(
                {
                    "code": "PLEX_INVENTORY_CHANGED",
                    "outcome": "deferred",
                    "mode": args.command,
                    "retryable": True,
                    "detail": "Plex inventory changed during the read; retry after it settles.",
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        CuratorValidationError,
        RuntimeContractError,
        TopologyPlanError,
        httpx.HTTPError,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        detail = " ".join(str(exc).split())[:500]
        failure = {
            "code": "CURATOR_FAILED",
            "outcome": "failed",
            "error_type": type(exc).__name__,
        }
        if detail:
            failure["detail"] = detail
        print(json.dumps(failure, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
