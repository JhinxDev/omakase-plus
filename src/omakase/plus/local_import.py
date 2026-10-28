"""Hash-bound plan/apply workflow for local anime files."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from omakase.plus.coverage import parse_episode_coverage
from omakase.plus.identity import identity_rejection_reason
from omakase.plus.media_probe import (
    ExternalSubtitle,
    MediaProbeResult,
    persist_probe_result,
    probe_media_file,
)
from omakase.plus.media_store import ensure_media_provider, upsert_asset
from omakase.plus.providers.local import LocalImportProvider, root_label
from omakase.plus.storage import (
    SUBTITLE_SUFFIXES,
    ConfiguredRoot,
    StorageSafetyError,
    content_receipt,
    file_receipt,
    normalize_locator,
    require_media_file,
)

LOCAL_IMPORT_PLAN_SCHEMA = "local-import-plan-v1"
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_QUALITIES = frozenset({"2160p", "1080p", "720p"})


class LocalImportError(ValueError):
    """A local import cannot be planned or safely applied."""


@dataclass(frozen=True, slots=True)
class LocalImportRequest:
    """One requested monitor-to-file association."""

    monitor_id: int
    locator: str
    quality: str = "1080p"
    external_subtitles: tuple[ExternalSubtitle, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.monitor_id, bool) or self.monitor_id <= 0:
            raise LocalImportError("monitor_id must be a positive integer")
        if self.quality not in _QUALITIES:
            raise LocalImportError("quality must be 2160p, 1080p, or 720p")
        object.__setattr__(self, "locator", normalize_locator(self.locator))
        object.__setattr__(self, "external_subtitles", tuple(self.external_subtitles))


def plan_local_import(
    db: sqlite3.Connection,
    *,
    user_id: int,
    root: ConfiguredRoot,
    requests: Sequence[LocalImportRequest],
    created_at: str,
) -> dict[str, Any]:
    """Observe local files and return a non-mutating, content-bound plan."""
    _validate_user_and_root(db, user_id=user_id, root=root)
    _timestamp(created_at)
    if not requests:
        raise LocalImportError("at least one local import request is required")
    normalized = tuple(requests)
    if len({item.locator for item in normalized}) != len(normalized):
        raise LocalImportError("local import locators must be unique")

    files: list[dict[str, Any]] = []
    observed_hashes: set[str] = set()
    for request in sorted(normalized, key=lambda item: (item.monitor_id, item.locator)):
        monitor = db.execute(
            "SELECT * FROM media_monitors WHERE id = ? AND user_id = ?",
            (request.monitor_id, user_id),
        ).fetchone()
        if monitor is None:
            raise LocalImportError("requested monitor does not exist for this user")
        path = require_media_file(root, request.locator)
        receipt = file_receipt(root, request.locator)
        if receipt.sha256 in observed_hashes:
            raise LocalImportError("the same media content was requested more than once")
        observed_hashes.add(receipt.sha256)
        coverage, numbering_override = _coverage_for_path(monitor, path.name)
        collision = _collision_state(
            db,
            monitor_id=request.monitor_id,
            content_hash=receipt.sha256,
            locator=receipt.locator,
        )
        sidecars: list[dict[str, Any]] = []
        for subtitle in request.external_subtitles:
            subtitle_locator = normalize_locator(subtitle.locator)
            subtitle_receipt = content_receipt(root, subtitle_locator)
            suffix = PurePosixPath(subtitle_locator).suffix.casefold()
            if suffix not in SUBTITLE_SUFFIXES:
                raise StorageSafetyError("external subtitle extension is unsupported")
            sidecars.append(
                {
                    "locator": subtitle_locator,
                    "language": subtitle.language,
                    "release_fingerprint": subtitle.release_fingerprint,
                    "sync_state": subtitle.sync_state,
                    "content_sha256": subtitle_receipt.sha256,
                    "bytes": subtitle_receipt.bytes,
                }
            )
        files.append(
            {
                "monitor_id": request.monitor_id,
                "anilist_id": int(monitor["anilist_id"]),
                "locator": receipt.locator,
                "source_title": path.stem,
                "source_folder": path.stem,
                "quality": request.quality,
                "bytes": receipt.bytes,
                "modified_ns": receipt.modified_ns,
                "content_sha256": receipt.sha256,
                "episode_coverage": sorted(coverage),
                "numbering_override": numbering_override,
                "collision": collision,
                "external_subtitles": sorted(sidecars, key=lambda item: item["locator"]),
            }
        )

    unsigned = {
        "schema_version": LOCAL_IMPORT_PLAN_SCHEMA,
        "created_at": created_at,
        "user_id": user_id,
        "root_fingerprint": _root_fingerprint(root),
        "root_label": root_label(root.path),
        "files": files,
    }
    return {**unsigned, "plan_hash": _plan_hash(unsigned)}


def apply_local_import_plan(
    db: sqlite3.Connection,
    *,
    plan: object,
    expected_plan_hash: str,
    root: ConfiguredRoot,
    applied_at: str,
    probe: Callable[..., MediaProbeResult] = probe_media_file,
    _on_import: Callable | None = None,
) -> dict[str, Any]:
    """Re-observe, probe, and transactionally apply one approved import plan."""
    validated = _validated_plan(plan)
    if not hmac.compare_digest(str(validated["plan_hash"]), expected_plan_hash.casefold()):
        raise LocalImportError("approved plan hash does not match")
    _timestamp(applied_at)
    user_id = int(validated["user_id"])
    _validate_user_and_root(db, user_id=user_id, root=root)
    requests = tuple(
        LocalImportRequest(
            monitor_id=int(item["monitor_id"]),
            locator=str(item["locator"]),
            quality=str(item["quality"]),
            external_subtitles=tuple(
                ExternalSubtitle(
                    locator=str(subtitle["locator"]),
                    language=str(subtitle["language"]),
                    release_fingerprint=str(subtitle["release_fingerprint"]),
                    sync_state=str(subtitle["sync_state"]),
                )
                for subtitle in item["external_subtitles"]
            ),
        )
        for item in validated["files"]
    )
    observed = plan_local_import(
        db,
        user_id=user_id,
        root=root,
        requests=requests,
        created_at=str(validated["created_at"]),
    )
    if not _plans_match_for_apply(validated, observed):
        raise LocalImportError("plan-bound local inventory changed before apply")
    observed_by_locator = {str(item["locator"]): item for item in observed["files"]}
    collisions = [item for item in observed["files"] if item["collision"] == "collision"]
    if collisions:
        raise LocalImportError("local import plan contains a content collision")
    already_imported = {
        locator
        for locator, item in observed_by_locator.items()
        if item["collision"] == "already_imported"
    }

    probe_results: dict[str, MediaProbeResult] = {}
    for item, request in zip(validated["files"], requests, strict=True):
        if str(item["locator"]) in already_imported:
            continue
        probe_results[str(item["locator"])] = probe(
            root,
            str(item["locator"]),
            inventory_fingerprint=str(item["content_sha256"]),
            external_subtitles=request.external_subtitles,
        )

    provider = LocalImportProvider(root)
    imported = 0
    usable = 0
    quarantined = 0
    already_present = 0
    db.execute("SAVEPOINT local_import_apply")
    try:
        provider_id = ensure_media_provider(
            db,
            user_id=user_id,
            kind=provider.capabilities.kind,
            label=f"Local: {root_label(root.path)}",
            config={
                "root_fingerprint": _root_fingerprint(root),
                "root_label": root_label(root.path),
            },
            capabilities=asdict(provider.capabilities),
        )
        for item in validated["files"]:
            if str(item["locator"]) in already_imported:
                already_present += 1
                continue
            locator = str(item["locator"])
            coverage = tuple(int(value) for value in item["episode_coverage"])
            result = probe_results[locator]
            asset_id = upsert_asset(
                db,
                monitor_id=int(item["monitor_id"]),
                torrent_hash=str(item["content_sha256"]),
                rd_torrent_id="",
                provider_id=provider_id,
                provider_asset_id=locator,
                storage_backend="local",
                storage_locator=locator,
                source_title=str(item["source_title"]),
                source_folder=str(item["source_folder"]),
                quality=str(item["quality"]),
                ownership="external",
                provenance="adopted",
                downloaded_once=result.usable,
                planned_coverage=coverage,
                usable_coverage=coverage if result.usable else (),
                numbering_override=item["numbering_override"],
                inventory_fingerprint=str(item["content_sha256"]),
                inventory_verified_at=applied_at if result.usable else None,
                status="downloaded" if result.usable else "quarantined",
                progress=100,
                last_error="" if result.usable else result.detail,
            )
            persist_probe_result(
                db,
                asset_id=asset_id,
                file_locator=locator,
                verified_at=applied_at,
                result=result,
            )
            if _on_import is not None:
                _on_import(asset_id, result, item)
            imported += 1
            usable += int(result.usable)
            quarantined += int(not result.usable)
        db.execute("RELEASE SAVEPOINT local_import_apply")
        db.commit()
    except BaseException:
        db.execute("ROLLBACK TO SAVEPOINT local_import_apply")
        db.execute("RELEASE SAVEPOINT local_import_apply")
        raise
    return {
        "plan_hash": validated["plan_hash"],
        "imported": imported,
        "usable": usable,
        "quarantined": quarantined,
        "already_present": already_present,
    }


def _coverage_for_path(monitor: sqlite3.Row, filename: str) -> tuple[frozenset[int], str | None]:
    aliases = _json_strings(monitor["search_titles_json"])
    reason = identity_rejection_reason(
        filename,
        target_title=str(monitor["title"]),
        target_aliases=aliases,
        season=int(monitor["season_number"] or 1),
        anilist_id=int(monitor["anilist_id"]),
        media_format=str(monitor["format"]),
    )
    if reason:
        raise LocalImportError(f"local media identity validation failed: {reason}")
    if str(monitor["format"]) == "MOVIE":
        return frozenset({1}), None
    if monitor["season_number"] is None:
        raise LocalImportError("episodic local media requires a season number")
    parsed = parse_episode_coverage(
        filename,
        int(monitor["season_number"]),
        expected=(
            int(monitor["expected_episodes"]) if monitor["expected_episodes"] is not None else None
        ),
        anilist_id=int(monitor["anilist_id"]),
    )
    if not parsed.episodes:
        raise LocalImportError("local media filename has no unambiguous episode coverage")
    return parsed.episodes, parsed.numbering_override


def _collision_state(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    content_hash: str,
    locator: str,
) -> str:
    rows = db.execute(
        """SELECT monitor_id, provider_asset_id, storage_backend
           FROM media_assets WHERE torrent_hash = ? ORDER BY id""",
        (content_hash.upper(),),
    ).fetchall()
    if not rows:
        return "none"
    if all(
        int(row["monitor_id"]) == monitor_id
        and str(row["provider_asset_id"]) == locator
        and str(row["storage_backend"]) == "local"
        for row in rows
    ):
        return "already_imported"
    return "collision"


def _validated_plan(plan: object) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise LocalImportError("local import plan is invalid")
    expected = {
        "schema_version",
        "created_at",
        "user_id",
        "root_fingerprint",
        "root_label",
        "files",
        "plan_hash",
    }
    if set(plan) != expected or plan.get("schema_version") != LOCAL_IMPORT_PLAN_SCHEMA:
        raise LocalImportError("local import plan is invalid")
    supplied = plan.get("plan_hash")
    unsigned = {key: value for key, value in plan.items() if key != "plan_hash"}
    if (
        not isinstance(supplied, str)
        or not _SHA256_RE.fullmatch(supplied)
        or not hmac.compare_digest(supplied, _plan_hash(unsigned))
    ):
        raise LocalImportError("local import plan hash is invalid")
    _timestamp(str(plan.get("created_at", "")))
    if (
        isinstance(plan.get("user_id"), bool)
        or not isinstance(plan.get("user_id"), int)
        or int(plan["user_id"]) <= 0
    ):
        raise LocalImportError("local import plan user is invalid")
    files = plan.get("files")
    if not isinstance(files, list) or not files:
        raise LocalImportError("local import plan files are invalid")
    return plan


def _validate_user_and_root(
    db: sqlite3.Connection,
    *,
    user_id: int,
    root: ConfiguredRoot,
) -> None:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise LocalImportError("user_id must be a positive integer")
    if root.owner_id != user_id:
        raise LocalImportError("configured import root belongs to a different user")
    if db.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
        raise LocalImportError("user_id does not exist")


def _timestamp(value: str) -> str:
    if not _UTC_RE.fullmatch(value):
        raise LocalImportError("timestamp must be canonical UTC")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise LocalImportError("timestamp must be canonical UTC") from exc
    return value


def _plans_match_for_apply(expected: dict[str, Any], observed: dict[str, Any]) -> bool:
    """Allow only the exact none-to-already-imported idempotence transition."""
    expected_unsigned = {key: value for key, value in expected.items() if key != "plan_hash"}
    observed_unsigned = {key: value for key, value in observed.items() if key != "plan_hash"}
    if len(expected_unsigned["files"]) != len(observed_unsigned["files"]):
        return False
    normalized_observed_files: list[dict[str, Any]] = []
    for expected_item, observed_item in zip(
        expected_unsigned["files"],
        observed_unsigned["files"],
        strict=True,
    ):
        normalized = dict(observed_item)
        if (
            expected_item["collision"] == "none"
            and observed_item["collision"] == "already_imported"
        ):
            normalized["collision"] = "none"
        normalized_observed_files.append(normalized)
    observed_unsigned["files"] = normalized_observed_files
    return observed_unsigned == expected_unsigned


def _root_fingerprint(root: ConfiguredRoot) -> str:
    return hashlib.sha256(f"{root.owner_id}:{root.path}".encode()).hexdigest()


def _plan_hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _json_strings(raw: object) -> tuple[str, ...]:
    try:
        values = json.loads(str(raw))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ()
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values if isinstance(value, str))


def apply_language_local_import_plan(
    db,
    *,
    plan,
    expected_plan_hash,
    root,
    applied_at,
    actor_user_id,
    request_id,
    source_generation=None,
    probe=probe_media_file,
    qualification=False,
):
    """Explicit typed, hash-bound local adoption. Default English API is unchanged.

    Existing plan ownership grants the physical import; the real viewer retains
    their request/evaluation. This performs no acquisition, selection or deletion.
    """
    from dataclasses import replace

    from omakase.plus import households
    from omakase.plus.language_policy import evaluate_language, rule_from_mapping
    from omakase.plus.language_publication import LANGUAGE_PUBLICATION_QUALIFIED
    from omakase.plus.language_source import local_connector_scope
    from omakase.plus.language_store import (
        _transaction,
        approve_publication_contract,
        current_request_source,
        load_request,
        persist_request_probe,
    )
    from omakase.plus.publication_episodes import approved_file_episodes

    if db.in_transaction:
        raise LocalImportError("Commit request preparation before typed local import.")
    if type(qualification) is not bool or not (LANGUAGE_PUBLICATION_QUALIFIED or qualification):
        raise LocalImportError("Typed local language import is not qualified.")
    validated = _validated_plan(plan)
    if len(validated["files"]) != 1:
        raise LocalImportError("Typed local import requires one exact request/file plan.")
    user_id = int(validated["user_id"])
    _validate_user_and_root(db, user_id=user_id, root=root)
    snapshot, _, required = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    if snapshot.domain != "anime":
        raise LocalImportError("Typed local import requires an exact anime resource.")
    binding = db.execute(
        "SELECT r.* FROM language_request_bindings b JOIN library_resources r ON r.id=b.resource_id WHERE b.request_id=?",
        (request_id,),
    ).fetchone()
    item = validated["files"][0]
    if (
        binding is None
        or binding["owner_user_id"] != user_id
        or binding["legacy_id"] != str(item["monitor_id"])
        or binding["connector_scope"] != local_connector_scope(root)
    ):
        raise LocalImportError("Local plan is not bound to this exact owned request resource/root.")
    source = current_request_source(db, snapshot, asserted_generation=source_generation)
    row = db.execute("SELECT * FROM language_request_policies WHERE id=?", (request_id,)).fetchone()
    rule = rule_from_mapping(json.loads(row["publication_policy_json"]))
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE id=? AND user_id=?", (item["monitor_id"], user_id)
    ).fetchone()
    if monitor is None or int(monitor["anilist_id"]) != int(item["anilist_id"]):
        raise LocalImportError("Local plan canonical identity changed.")
    actual = approved_file_episodes(
        item["locator"],
        media_format=monitor["format"],
        season=monitor["season_number"] or 0,
        expected=monitor["expected_episodes"],
        anilist_id=monitor["anilist_id"],
        numbering_override=item["numbering_override"],
        approved_coverage=item["episode_coverage"],
        sole_file=True,
    )
    if required and not actual <= set(required):
        raise LocalImportError("Local file escapes the immutable request episode scope.")
    evaluation = []

    def revalidate():
        fresh, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
        scope = households.resolve_scope(db, viewer_id=actor_user_id, library_id=fresh.library_id)
        if (
            scope.membership_revision != row["membership_revision"]
            or scope.policy_revision != row["library_policy_revision"]
            or current_request_source(db, fresh) != source
        ):
            raise LocalImportError("Local request authorization or configured root changed.")
        return scope

    def policy_probe(*args, **kwargs):
        revalidate()
        if probe is probe_media_file:
            from omakase.plus.publication_probe import local_only_probe_runner

            result = probe(*args, **kwargs, runner=local_only_probe_runner)
        else:
            result = probe(*args, **kwargs)
        usable_video = any(
            v.get("codec") in {"h264", "hevc", "h265", "av1", "vp9", "mpeg4", "mpeg2video", "vc1"}
            and v.get("width", 0) > 0
            and v.get("height", 0) > 0
            for v in result.video_streams
        )
        if (
            result.outcome == "error"
            or not usable_video
            or result.language_evidence is None
            or result.inventory_fingerprint != item["content_sha256"]
        ):
            raise LocalImportError("Local file lacks a current exact structural/language probe.")
        approved = evaluate_language(rule, result.language_evidence, domain="anime")
        if not approved.physical_compatible:
            raise LocalImportError(
                "Local file does not satisfy the frozen owner publication policy."
            )
        stat = require_media_file(root, item["locator"]).stat()
        return replace(
            result,
            outcome="usable",
            detail="Frozen language policy satisfied by exact local file probe.",
            byte_range_receipt={
                **result.byte_range_receipt,
                "publication_stat": {
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                },
                "publication_scope": {"format": monitor["format"], "sole_file": True},
            },
        )

    def bind(asset_id, result, planned, *, approve=True):
        scope = revalidate()
        if approve:
            owner = households.resolve_scope(db, viewer_id=user_id, library_id=scope.library_id)
            approve_publication_contract(
                db, scope=owner, resource_id=binding["id"], asset_id=asset_id, rule=rule
            )
        probe_id = persist_probe_result(
            db,
            asset_id=asset_id,
            file_locator=planned["locator"],
            verified_at=applied_at,
            result=result,
        )
        evaluation.append(
            persist_request_probe(
                db,
                actor_user_id=actor_user_id,
                request_id=request_id,
                asset_id=asset_id,
                probe_id=probe_id,
                evidence=result.language_evidence,
                episodes=tuple(sorted(actual)),
                source_generation=source.source_generation,
                observed_at=applied_at,
            )
        )

    result = apply_local_import_plan(
        db,
        plan=validated,
        expected_plan_hash=expected_plan_hash,
        root=root,
        applied_at=applied_at,
        probe=policy_probe,
        _on_import=bind,
    )
    if not evaluation and result["already_present"]:
        observed = policy_probe(root, item["locator"], inventory_fingerprint=item["content_sha256"])
        asset = db.execute(
            "SELECT id FROM media_assets WHERE monitor_id=? AND storage_backend='local' AND provider_asset_id=? AND lower(torrent_hash)=?",
            (item["monitor_id"], item["locator"], item["content_sha256"]),
        ).fetchone()
        if asset is None:
            raise LocalImportError("Existing local asset identity changed.")
        with _transaction(db):
            bind(asset[0], observed, item, approve=False)
    return {
        **result,
        "language_request_id": request_id,
        "language_evaluation_id": evaluation[0][0],
        "language_state": evaluation[0][1].state,
        "physical_compatible": evaluation[0][1].physical_compatible,
    }
