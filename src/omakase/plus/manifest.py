"""Strict, sanitized contracts exchanged with the private Plex curator."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from omakase.plus.identity import identity_rejection_reason
from omakase.plus.identity_history import successful_exact_hash_identity_title

MANIFEST_SCHEMA = "manifest-v2"
CURATOR_RECEIPT_SCHEMA = "curator-receipt-v2"
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_MANIFEST_TITLES = 2_000
MAX_MANIFEST_ASSETS = 4_000
MAX_EPISODE_ENTRIES = 50_000
MAX_RECEIPT_BLOCKED_ASSETS = 2_000

_FORMATS = frozenset({"TV", "TV_SHORT", "ONA", "OVA", "MOVIE", "SPECIAL"})
_QUALITIES = frozenset({"2160p", "1080p", "720p"})
_QUALITY_RANK = {"2160p": 3, "1080p": 2, "720p": 1}
_NUMBERING_OVERRIDES = frozenset(
    {"mha-s4-absolute-v1", "bleach-tybw-cour4-absolute-v1", "slime-s4-absolute-v1"}
)
_SECTION_NAMES = ("Anime", "Anime Movies", "TV Shows", "Movies")
_ANIME_SECTIONS = frozenset({"Anime", "Anime Movies"})
_ERROR_CODES = frozenset(
    {
        "AMBIGUOUS_MATCH",
        "BROKEN_TARGET",
        "DUPLICATE_DESTINATION",
        "INCOMPLETE_COVERAGE",
        "INTERNAL_ERROR",
        "MANIFEST_FETCH_FAILED",
        "MANIFEST_INVALID",
        "OVERLAY_FAILED",
        "OVERLAY_ROLLBACK_FAILED",
        "OVERLAY_SWAP_FAILED",
        "OVERLAY_VALIDATION_FAILED",
        "PATH_ESCAPE",
        "PLAYBACK_FAILED",
        "PLEX_COUNT_UNSTABLE",
        "PLEX_SCAN_FAILED",
        "PLEX_SECTION_MISMATCH",
        "RECEIPT_SUBMIT_FAILED",
        "RIVEN_EXCLUSION_FAILED",
        "TOPOLOGY_REFUSED",
    }
)
# Mirrors deploy/plex-curator/plex_curator.py::_BLOCK_CODES. A single verified
# provider asset was withheld from this generation for one of these reasons;
# the rest of the manifest still published normally.
_BLOCKED_ASSET_CODES = frozenset(
    {
        "ASSET_COVERAGE_INVALID",
        "ASSET_VALIDATION_FAILED",
        "DESTINATION_CONFLICT",
        "ENGLISH_SUBTITLE_MISSING",
        "MEDIA_PROBE_FAILED",
        "LANGUAGE_POLICY_UNCONFIRMED",
        "POLICY_EXCLUDED",
        "SOURCE_UNAVAILABLE",
    }
)
_NONRETRYABLE_BLOCKED_ASSET_CODES = frozenset(
    {"DESTINATION_CONFLICT", "POLICY_EXCLUDED", "LANGUAGE_POLICY_UNCONFIRMED"}
)
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTENT_ID_RE = re.compile(r"^(?:[0-9A-F]{40}|[0-9A-F]{64})$")
_INVENTORY_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,80}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SENSITIVE_DETAIL_RE = re.compile(
    r"(?:magnet:|https?://|[A-Za-z]:[\\/]|(?:^|\s)/(?:[^\s/]+/)+|"
    r"\\\\|\b(?:api[ _-]?key|email|session|secret|token|username|user[ _-]?id|"
    r"rd[ _-]?(?:id|torrent)|provider payload)\b|"
    r"\b[0-9A-Fa-f]{16,}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})",
    re.IGNORECASE,
)


class ManifestContractError(ValueError):
    """An external or persisted payload violates a closed media contract."""


class ReceiptConflictError(ManifestContractError):
    """A curator run ID or completion ordering conflicts with stored evidence."""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fail(field: str) -> None:
    raise ManifestContractError(f"invalid {field}")


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        _fail(field)
    return value


def _string(value: Any, field: str, *, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        _fail(field)
    return value


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail(field)
    return value


def _timestamp(value: Any, field: str) -> str:
    value = _string(value, field, minimum=20, maximum=20)
    if not _UTC_RE.fullmatch(value):
        _fail(field)
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        _fail(field)
    return value


def _unique_strings(
    value: Any,
    field: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum_items:
        _fail(field)
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        item = _string(item, field, minimum=1, maximum=maximum_length)
        if _CONTROL_RE.search(item):
            _fail(field)
        key = item.casefold()
        if key in seen:
            _fail(field)
        seen.add(key)
        result.append(item)
    return result


def _episodes(value: Any, field: str) -> list[int]:
    if not isinstance(value, list) or len(value) > 10_000:
        _fail(field)
    normalized = [_integer(item, field, minimum=1, maximum=10_000) for item in value]
    if normalized != sorted(set(normalized)):
        _fail(field)
    return normalized


def _optional_count(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field, minimum=0, maximum=10_000)


def _optional_season(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field, minimum=1, maximum=99)


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _validate_library_manifest_v2(payload: Any) -> dict[str, Any]:
    """Validate the exact manifest-v2 schema, bounds, counts, and coverage invariants."""
    manifest = _object(payload, {"schema_version", "generated_at", "titles"}, "manifest")
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        _fail("schema_version")
    _timestamp(manifest["generated_at"], "generated_at")
    titles = manifest["titles"]
    if not isinstance(titles, list) or len(titles) > MAX_MANIFEST_TITLES:
        _fail("titles")

    title_ids: set[int] = set()
    asset_total = 0
    episode_entries = 0
    for title in titles:
        title = _object(
            title,
            {
                "anilist_id",
                "canonical_title",
                "title_aliases",
                "genres",
                "format",
                "season_number",
                "expected_episode_count",
                "aired_episode_count",
                "coverage",
                "assets",
            },
            "title",
        )
        anilist_id = _integer(title["anilist_id"], "anilist_id", minimum=1, maximum=2_147_483_647)
        if anilist_id in title_ids:
            _fail("duplicate anilist_id")
        title_ids.add(anilist_id)
        canonical = _string(title["canonical_title"], "canonical_title", minimum=1, maximum=200)
        if _CONTROL_RE.search(canonical):
            _fail("canonical_title")
        aliases = _unique_strings(
            title["title_aliases"],
            "title_aliases",
            maximum_items=10,
            maximum_length=200,
        )
        if canonical.casefold() in {item.casefold() for item in aliases}:
            _fail("title_aliases")
        _unique_strings(title["genres"], "genres", maximum_items=20, maximum_length=64)
        if title["format"] not in _FORMATS:
            _fail("format")
        _optional_season(title["season_number"], "season_number")
        _optional_count(title["expected_episode_count"], "expected_episode_count")
        _integer(title["aired_episode_count"], "aired_episode_count", minimum=0, maximum=10_000)
        if title["format"] == "MOVIE" and (
            title["season_number"] is not None
            or title["expected_episode_count"] != 1
            or title["aired_episode_count"] != 1
        ):
            _fail("movie identity")

        coverage = _object(
            title["coverage"],
            {
                "required_episodes",
                "usable_episodes",
                "plex_visible_episodes",
                "required_count",
                "usable_count",
                "plex_visible_count",
            },
            "coverage",
        )
        required = _episodes(coverage["required_episodes"], "required_episodes")
        usable = _episodes(coverage["usable_episodes"], "usable_episodes")
        visible = _episodes(coverage["plex_visible_episodes"], "plex_visible_episodes")
        for name, values in (
            ("required_count", required),
            ("usable_count", usable),
            ("plex_visible_count", visible),
        ):
            if _integer(coverage[name], name, minimum=0, maximum=10_000) != len(values):
                _fail(name)
        if not set(visible) <= set(usable):
            _fail("plex_visible_episodes")
        if title["format"] == "MOVIE" and (
            set(required) != {1} or set(usable) != {1} or not set(visible) <= {1}
        ):
            _fail("movie coverage")
        episode_entries += len(required) + len(usable) + len(visible)

        assets = title["assets"]
        if not isinstance(assets, list) or not 1 <= len(assets) <= 200:
            _fail("assets")
        asset_total += len(assets)
        asset_hashes: set[str] = set()
        asset_union: set[int] = set()
        for asset in assets:
            if not isinstance(asset, dict):
                _fail("asset")
            expected_fields = {
                "torrent_hash",
                "source_folder",
                "storage_backend",
                "storage_locator",
                "source_title",
                "quality",
                "episode_coverage",
            }
            if "numbering_override" in asset:
                expected_fields.add("numbering_override")
            asset = _object(asset, expected_fields, "asset")
            torrent_hash = _string(asset["torrent_hash"], "torrent_hash", minimum=40, maximum=64)
            if not _CONTENT_ID_RE.fullmatch(torrent_hash) or torrent_hash in asset_hashes:
                _fail("torrent_hash")
            asset_hashes.add(torrent_hash)
            folder = _string(asset["source_folder"], "source_folder", minimum=1, maximum=240)
            if (
                folder in {".", ".."}
                or "/" in folder
                or "\\" in folder
                or _CONTROL_RE.search(folder)
            ):
                _fail("source_folder")
            if asset["storage_backend"] not in {"zurg", "local"}:
                _fail("storage_backend")
            locator = _string(
                asset["storage_locator"],
                "storage_locator",
                minimum=1,
                maximum=4096,
            ).replace("\\", "/")
            locator_parts = locator.split("/")
            if (
                locator.startswith("/")
                or any(part in {"", ".", ".."} for part in locator_parts)
                or _CONTROL_RE.search(locator)
            ):
                _fail("storage_locator")
            source_title = _string(asset["source_title"], "source_title", minimum=1, maximum=300)
            if _CONTROL_RE.search(source_title):
                _fail("source_title")
            if asset["quality"] not in _QUALITIES:
                _fail("quality")
            asset_coverage = _episodes(asset["episode_coverage"], "episode_coverage")
            if not asset_coverage or not set(asset_coverage) <= set(usable):
                _fail("episode_coverage")
            if title["format"] == "MOVIE" and asset_coverage != [1]:
                _fail("movie asset coverage")
            asset_union.update(asset_coverage)
            episode_entries += len(asset_coverage)
            if "numbering_override" in asset:
                override = _string(
                    asset["numbering_override"], "numbering_override", minimum=1, maximum=64
                )
                if override not in _NUMBERING_OVERRIDES:
                    _fail("numbering_override")
        if asset_union != set(usable):
            _fail("usable_episodes")

    if asset_total > MAX_MANIFEST_ASSETS:
        _fail("asset total")
    if episode_entries > MAX_EPISODE_ENTRIES:
        _fail("episode entry total")
    if len(_canonical_bytes(manifest)) > MAX_MANIFEST_BYTES:
        _fail("manifest size")
    return manifest


def _parse_string_array(raw: Any, field: str, maximum_items: int, maximum_length: int) -> list[str]:
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail(field)
    return _unique_strings(
        values, field, maximum_items=maximum_items, maximum_length=maximum_length
    )


def _parse_coverage(raw: Any) -> list[int]:
    try:
        values = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("usable coverage")
    return _episodes(values, "usable coverage")


def _latest_visible_counts(db: sqlite3.Connection) -> dict[tuple[int, int | None], tuple[int, int]]:
    row = db.execute(
        "SELECT receipt_json FROM media_curator_receipt_latest WHERE id = 1"
    ).fetchone()
    if row is None:
        return {}
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("stored curator receipt")
    if not isinstance(payload, dict) or payload.get("schema_version") != CURATOR_RECEIPT_SCHEMA:
        # A receipt stored under a prior curator-receipt schema predates this
        # contract version. Treat it as no Plex-visibility evidence yet
        # instead of failing the manifest closed; the next live curator run
        # overwrites it with a current-schema receipt.
        return {}
    try:
        receipt = validate_curator_receipt(payload)
    except ManifestContractError:
        _fail("stored curator receipt")
    if receipt["mode"] != "live" or not receipt["topology_converged"]:
        return {}
    return {
        (item["anilist_id"], item["season_number"]): (
            item["usable_count"],
            item["plex_visible_count"],
        )
        for item in receipt["title_coverage"]
    }


def _build_library_manifest_v2(
    db: sqlite3.Connection,
    *,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build one cross-user-deduplicated manifest from usable RD file evidence."""
    generated_at = _timestamp(generated_at or utc_now(), "generated_at")
    anilist_rows = db.execute(
        """SELECT DISTINCT monitor.anilist_id
           FROM media_monitors AS monitor
           JOIN media_assets AS asset ON asset.monitor_id = monitor.id
           WHERE asset.status = 'downloaded'
             AND asset.inventory_verified_at IS NOT NULL
             AND asset.usable_coverage_json <> '[]'
             AND NOT EXISTS (SELECT 1 FROM media_title_removals AS removal
                 WHERE removal.monitor_id = monitor.id)
           ORDER BY monitor.anilist_id"""
    ).fetchall()
    visible_counts = _latest_visible_counts(db)
    titles: list[dict[str, Any]] = []

    for id_row in anilist_rows:
        anilist_id = int(id_row[0])
        monitors = db.execute(
            """SELECT * FROM media_monitors AS monitor WHERE anilist_id = ?
               AND NOT EXISTS (SELECT 1 FROM media_title_removals AS removal
                   WHERE removal.monitor_id = monitor.id) ORDER BY id""",
            (anilist_id,),
        ).fetchall()
        if not monitors:
            continue
        canonical = str(monitors[0]["title"]).strip()
        aliases: list[str] = []
        alias_keys = {canonical.casefold()}
        genres: list[str] = []
        genre_keys: set[str] = set()
        formats = {str(row["format"]) for row in monitors}
        seasons = {row["season_number"] for row in monitors}
        if len(formats) != 1 or len(seasons) != 1:
            _fail("cross-user media identity")
        format_value = next(iter(formats))
        for monitor in monitors:
            candidates = [str(monitor["title"]).strip()]
            candidates.extend(
                _parse_string_array(monitor["search_titles_json"], "search titles", 100, 200)
            )
            for alias in candidates:
                key = alias.casefold()
                if key not in alias_keys and len(aliases) < 10:
                    alias_keys.add(key)
                    aliases.append(alias)
            for genre in _parse_string_array(monitor["genres_json"], "genres", 20, 64):
                key = genre.casefold()
                if key not in genre_keys and len(genres) < 20:
                    genre_keys.add(key)
                    genres.append(genre)

        asset_rows = db.execute(
            """SELECT asset.*
               FROM media_assets AS asset
               JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
               WHERE monitor.anilist_id = ?
                 AND asset.status = 'downloaded'
                 AND asset.inventory_verified_at IS NOT NULL
                 AND asset.usable_coverage_json <> '[]'
                 AND NOT EXISTS (SELECT 1 FROM media_title_removals AS removal
                     WHERE removal.monitor_id = monitor.id)
               ORDER BY asset.torrent_hash, asset.id""",
            (anilist_id,),
        ).fetchall()
        monitor_by_id = {int(row["id"]): row for row in monitors}
        by_hash: dict[str, dict[str, Any]] = {}
        for row in asset_rows:
            monitor = monitor_by_id[int(row["monitor_id"])]
            aliases_for_identity = _parse_string_array(
                monitor["search_titles_json"],
                "search titles",
                100,
                200,
            )
            identity_reason = identity_rejection_reason(
                str(row["source_title"]),
                target_title=str(monitor["title"]),
                target_aliases=aliases_for_identity,
                season=int(monitor["season_number"] or 1),
                anilist_id=int(monitor["anilist_id"]),
                media_format=str(monitor["format"]),
            )
            if identity_reason:
                if identity_reason not in {
                    "AMBIGUOUS_TARGET_IDENTITY",
                    "TARGET_TITLE_MISMATCH",
                }:
                    continue
                historical_title = successful_exact_hash_identity_title(
                    db,
                    user_id=int(monitor["user_id"]),
                    anilist_id=int(monitor["anilist_id"]),
                    torrent_hash=str(row["torrent_hash"]),
                    target_title=str(monitor["title"]),
                    target_aliases=aliases_for_identity,
                    season=int(monitor["season_number"] or 1),
                    media_format=str(monitor["format"]),
                )
                if historical_title is None:
                    continue
            _timestamp(row["inventory_verified_at"], "inventory_verified_at")
            fingerprint = str(row["inventory_fingerprint"])
            if not _INVENTORY_FINGERPRINT_RE.fullmatch(fingerprint):
                _fail("inventory_fingerprint")
            coverage = _parse_coverage(row["usable_coverage_json"])
            asset: dict[str, Any] = {
                "torrent_hash": str(row["torrent_hash"]).upper(),
                "source_folder": str(row["source_folder"]),
                "storage_backend": str(row["storage_backend"] or "zurg"),
                "storage_locator": str(row["storage_locator"] or row["source_folder"]),
                "source_title": str(row["source_title"]),
                "quality": str(row["quality"]),
                "episode_coverage": coverage,
            }
            if row["numbering_override"] is not None:
                asset["numbering_override"] = str(row["numbering_override"])
            existing = by_hash.get(asset["torrent_hash"])
            if existing is None:
                asset["_inventory_fingerprint"] = fingerprint
                by_hash[asset["torrent_hash"]] = asset
                continue
            if existing["_inventory_fingerprint"] != fingerprint:
                _fail("cross-user asset inventory")
            comparable = {
                key: value
                for key, value in asset.items()
                if key not in {"episode_coverage", "_inventory_fingerprint"}
            }
            existing_comparable = {
                key: value
                for key, value in existing.items()
                if key not in {"episode_coverage", "_inventory_fingerprint"}
            }
            if comparable != existing_comparable:
                _fail("cross-user asset identity")
            existing["episode_coverage"] = sorted(set(existing["episode_coverage"]) | set(coverage))
        if format_value == "MOVIE":
            asset_sort_key = lambda asset: (  # noqa: E731
                -_QUALITY_RANK.get(str(asset["quality"]), 0),
                str(asset["torrent_hash"]),
            )
        else:
            asset_sort_key = lambda asset: str(asset["torrent_hash"])  # noqa: E731
        ordered_assets = sorted(by_hash.values(), key=asset_sort_key)
        assets = []
        claimed_coverage: set[int] = set()
        for asset in ordered_assets:
            asset.pop("_inventory_fingerprint")
            if format_value == "MOVIE":
                if asset["episode_coverage"] != [1]:
                    _fail("movie asset coverage")
                assets.append(asset)
                claimed_coverage.add(1)
                continue
            novel_coverage = sorted(set(asset["episode_coverage"]) - claimed_coverage)
            if not novel_coverage:
                continue
            asset["episode_coverage"] = novel_coverage
            claimed_coverage.update(novel_coverage)
            assets.append(asset)
        if not assets:
            continue
        usable = sorted({episode for asset in assets for episode in asset["episode_coverage"]})
        required: set[int] = set()
        for monitor in monitors:
            expected = monitor["expected_episodes"]
            aired = int(monitor["aired_episodes"] or 0)
            count = int(expected or 0) if monitor["release_status"] == "FINISHED" else aired
            required.update(range(1, count + 1))
        season_number = next(iter(seasons))
        counts = visible_counts.get((anilist_id, season_number))
        visible = usable if counts == (len(usable), len(usable)) else []
        expected_values = [
            int(row["expected_episodes"])
            for row in monitors
            if row["expected_episodes"] is not None
        ]
        coverage = {
            "required_episodes": sorted(required),
            "usable_episodes": usable,
            "plex_visible_episodes": visible,
            "required_count": len(required),
            "usable_count": len(usable),
            "plex_visible_count": len(visible),
        }
        titles.append(
            {
                "anilist_id": anilist_id,
                "canonical_title": canonical,
                "title_aliases": aliases,
                "genres": genres,
                "format": format_value,
                "season_number": season_number,
                "expected_episode_count": max(expected_values) if expected_values else None,
                "aired_episode_count": max(int(row["aired_episodes"] or 0) for row in monitors),
                "coverage": coverage,
                "assets": assets,
            }
        )

    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "generated_at": generated_at,
        "titles": titles,
    }
    return validate_library_manifest(manifest)


def _validate_error_detail(value: Any) -> str:
    detail = _string(value, "error detail", minimum=0, maximum=300)
    if _CONTROL_RE.search(detail) or _SENSITIVE_DETAIL_RE.search(detail):
        _fail("error detail")
    return detail


def _validate_blocked_asset(item: Any) -> dict[str, Any]:
    item = _object(
        item,
        {
            "anilist_id",
            "section",
            "season_number",
            "torrent_hash",
            "affected_episodes",
            "code",
            "detail",
            "retryable",
            "last_observed_at",
        },
        "blocked asset",
    )
    _integer(item["anilist_id"], "blocked anilist_id", minimum=1, maximum=2_147_483_647)
    if item["section"] not in _ANIME_SECTIONS:
        _fail("blocked asset section")
    _optional_season(item["season_number"], "blocked season_number")
    torrent_hash = _string(item["torrent_hash"], "blocked torrent_hash", minimum=40, maximum=64)
    if not _CONTENT_ID_RE.fullmatch(torrent_hash):
        _fail("blocked torrent_hash")
    episodes = item["affected_episodes"]
    if not isinstance(episodes, list) or not episodes or len(episodes) > 10_000:
        _fail("blocked affected_episodes")
    normalized_episodes = [
        _integer(episode, "blocked affected_episodes", minimum=1, maximum=10_000)
        for episode in episodes
    ]
    if normalized_episodes != sorted(set(normalized_episodes)):
        _fail("blocked affected_episodes")
    code = _string(item["code"], "blocked code", minimum=1, maximum=48)
    if code not in _BLOCKED_ASSET_CODES:
        _fail("blocked code")
    detail = _string(item["detail"], "blocked detail", minimum=1, maximum=300)
    if _CONTROL_RE.search(detail) or _SENSITIVE_DETAIL_RE.search(detail):
        _fail("blocked detail")
    if not isinstance(item["retryable"], bool) or item["retryable"] != (
        code not in _NONRETRYABLE_BLOCKED_ASSET_CODES
    ):
        _fail("blocked retryable")
    _timestamp(item["last_observed_at"], "blocked last_observed_at")
    return item


def _validate_curator_receipt_v2(payload: Any) -> dict[str, Any]:
    """Validate the exact curator-receipt-v2 schema without accepting unknown data."""
    receipt = _object(
        payload,
        {
            "schema_version",
            "run_id",
            "started_at",
            "completed_at",
            "outcome",
            "mode",
            "topology_converged",
            "manifest_generated_at",
            "observed_sections",
            "converged_section_counts",
            "title_coverage",
            "blocked_assets",
            "errors",
        },
        "curator receipt",
    )
    if receipt["schema_version"] != CURATOR_RECEIPT_SCHEMA:
        _fail("schema_version")
    run_id = _string(receipt["run_id"], "run_id", minimum=8, maximum=80)
    if not _RUN_ID_RE.fullmatch(run_id):
        _fail("run_id")
    started_at = _timestamp(receipt["started_at"], "started_at")
    completed_at = _timestamp(receipt["completed_at"], "completed_at")
    if completed_at < started_at:
        _fail("completed_at")
    _timestamp(receipt["manifest_generated_at"], "manifest_generated_at")
    if receipt["outcome"] not in {"success", "degraded", "failed"}:
        _fail("outcome")
    if receipt["mode"] not in {"dry-run", "live"}:
        _fail("mode")
    if not isinstance(receipt["topology_converged"], bool):
        _fail("topology_converged")

    observed = receipt["observed_sections"]
    if not isinstance(observed, list) or len(observed) > 20:
        _fail("observed_sections")
    observed_keys: set[int] = set()
    observed_counts: dict[str, int] = {}
    for section in observed:
        section = _object(section, {"key", "title", "count"}, "observed section")
        key = _integer(section["key"], "section key", minimum=1, maximum=2_147_483_647)
        if key in observed_keys:
            _fail("section key")
        observed_keys.add(key)
        title = _string(section["title"], "section title", minimum=1, maximum=80)
        if _CONTROL_RE.search(title):
            _fail("section title")
        count = _integer(section["count"], "section count", minimum=0, maximum=1_000_000)
        if title in observed_counts:
            _fail("section title")
        observed_counts[title] = count

    converged_counts = receipt["converged_section_counts"]
    if converged_counts is not None:
        converged_counts = _object(
            converged_counts, set(_SECTION_NAMES), "converged_section_counts"
        )
        for name in _SECTION_NAMES:
            _integer(converged_counts[name], name, minimum=0, maximum=1_000_000)
    converged = receipt["topology_converged"]
    if converged:
        if receipt["mode"] != "live" or receipt["outcome"] == "failed":
            _fail("topology_converged")
        if converged_counts is None or observed_counts != converged_counts:
            _fail("converged_section_counts")
    elif converged_counts is not None:
        _fail("converged_section_counts")
    if receipt["mode"] == "dry-run" and (converged or converged_counts is not None):
        _fail("dry-run convergence")
    if receipt["mode"] == "live" and receipt["outcome"] == "success" and not converged:
        _fail("live success")

    blocked_assets = receipt["blocked_assets"]
    if not isinstance(blocked_assets, list) or len(blocked_assets) > MAX_RECEIPT_BLOCKED_ASSETS:
        _fail("blocked_assets")
    blocked_keys: set[tuple[int, str, int | None, str]] = set()
    for item in blocked_assets:
        item = _validate_blocked_asset(item)
        key = (
            item["anilist_id"],
            item["section"],
            item["season_number"],
            item["torrent_hash"],
        )
        if key in blocked_keys:
            _fail("duplicate blocked asset")
        blocked_keys.add(key)
    if blocked_assets and receipt["outcome"] == "success":
        _fail("outcome")

    title_coverage = receipt["title_coverage"]
    if not isinstance(title_coverage, list) or len(title_coverage) > 2_000:
        _fail("title_coverage")
    title_keys: set[tuple[int, int | None]] = set()
    for item in title_coverage:
        item = _object(
            item,
            {
                "anilist_id",
                "section",
                "season_number",
                "usable_count",
                "plex_visible_count",
            },
            "title coverage",
        )
        anilist_id = _integer(item["anilist_id"], "anilist_id", minimum=1, maximum=2_147_483_647)
        if item["section"] not in _ANIME_SECTIONS:
            _fail("title section")
        season_number = _optional_season(item["season_number"], "season_number")
        key = (anilist_id, season_number)
        if key in title_keys:
            _fail("duplicate title coverage")
        title_keys.add(key)
        usable_count = _integer(item["usable_count"], "usable_count", minimum=0, maximum=10_000)
        visible_count = _integer(
            item["plex_visible_count"], "plex_visible_count", minimum=0, maximum=10_000
        )
        if visible_count > usable_count:
            _fail("plex_visible_count")

    errors = receipt["errors"]
    if not isinstance(errors, list) or len(errors) > 50:
        _fail("errors")
    for error in errors:
        error = _object(error, {"code", "anilist_id", "detail"}, "error")
        code = _string(error["code"], "error code", minimum=1, maximum=48)
        if code not in _ERROR_CODES:
            _fail("error code")
        if error["anilist_id"] is not None:
            _integer(
                error["anilist_id"],
                "error anilist_id",
                minimum=1,
                maximum=2_147_483_647,
            )
        _validate_error_detail(error["detail"])
    return receipt


def store_curator_receipt(
    db: sqlite3.Connection,
    payload: Any,
    *,
    received_at: str | None = None,
    language_qualification: bool = False,
) -> dict[str, Any]:
    """Validate and transactionally replace only later converged live evidence."""
    receipt = validate_curator_receipt(payload)
    if receipt["schema_version"] == "curator-receipt-v3":
        from omakase.plus.language_publication import LANGUAGE_PUBLICATION_QUALIFIED

        if not (LANGUAGE_PUBLICATION_QUALIFIED or language_qualification is True):
            raise ManifestContractError("Language receipt import is not qualified.")
    received_at = _timestamp(received_at or utc_now(), "received_at")
    canonical = _canonical_bytes(receipt)
    receipt_hash = hashlib.sha256(canonical).hexdigest()
    eligible = (
        receipt["mode"] == "live"
        and receipt["topology_converged"] is True
        and receipt["outcome"] in {"success", "degraded"}
    )
    db.execute("BEGIN IMMEDIATE")
    try:
        current = db.execute(
            """SELECT receipt_hash, curator_run_id, completed_at
               FROM media_curator_receipt_latest WHERE id = 1"""
        ).fetchone()
        if current is not None:
            _timestamp(current["completed_at"], "stored curator completion")
        if current is not None and current["curator_run_id"] == receipt["run_id"]:
            if current["receipt_hash"] != receipt_hash:
                raise ReceiptConflictError("curator run ID already has different evidence")
            db.commit()
            return {
                "accepted": True,
                "stored": True,
                "idempotent": True,
                "run_id": receipt["run_id"],
                "receipt_hash": receipt_hash,
            }
        if not eligible:
            db.commit()
            return {
                "accepted": True,
                "stored": False,
                "idempotent": False,
                "run_id": receipt["run_id"],
                "receipt_hash": receipt_hash,
            }
        if current is not None and receipt["completed_at"] <= current["completed_at"]:
            raise ReceiptConflictError("curator completion is not later than stored evidence")
        if receipt["schema_version"] == "curator-receipt-v3":
            from omakase.plus.language_publication import import_files

            import_files(db, receipt)
        db.execute(
            """INSERT INTO media_curator_receipt_latest
               (id, receipt_json, receipt_hash, curator_run_id, outcome,
                received_at, completed_at)
               VALUES (1, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   receipt_json = excluded.receipt_json,
                   receipt_hash = excluded.receipt_hash,
                   curator_run_id = excluded.curator_run_id,
                   outcome = excluded.outcome,
                   received_at = excluded.received_at,
                   completed_at = excluded.completed_at""",
            (
                canonical.decode("utf-8"),
                receipt_hash,
                receipt["run_id"],
                receipt["outcome"],
                received_at,
                receipt["completed_at"],
            ),
        )
        db.commit()
    except BaseException as error:
        db.rollback()
        if isinstance(error, ValueError) and not isinstance(error, ManifestContractError):
            raise ManifestContractError(
                "Language publication import failed its current binding checks."
            ) from None
        raise
    return {
        "accepted": True,
        "stored": True,
        "idempotent": False,
        "run_id": receipt["run_id"],
        "receipt_hash": receipt_hash,
    }


def validate_library_manifest(payload):
    if isinstance(payload, dict) and payload.get("schema_version") == "manifest-v3":
        from omakase.plus.language_publication import validate_manifest

        try:
            return validate_manifest(payload)
        except ValueError:
            raise ManifestContractError("Invalid manifest-v3 language contract.") from None
    return _validate_library_manifest_v2(payload)


def _build_library_manifest_versioned(
    db, *, generated_at=None, peer_capabilities=(), qualification=False
):
    from omakase.plus.language_publication import (
        CAPABILITY,
        LANGUAGE_PUBLICATION_QUALIFIED,
        export_contract,
    )

    if (
        not isinstance(peer_capabilities, (tuple, list))
        or len(peer_capabilities) > 8
        or any(c != CAPABILITY for c in peer_capabilities)
        or type(qualification) is not bool
    ):
        raise ManifestContractError("Unknown curator publication capability.")
    has_table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='publication_language_contracts' AND type='table'"
    ).fetchone()
    contracted = (
        db.execute(
            "SELECT 1 FROM publication_language_contracts c JOIN media_assets a ON a.id=c.asset_id WHERE a.status='downloaded' AND a.inventory_verified_at IS NOT NULL LIMIT 1"
        ).fetchone()
        if has_table
        else None
    )
    capable = CAPABILITY in peer_capabilities and (
        LANGUAGE_PUBLICATION_QUALIFIED or qualification is True
    )
    if contracted and not capable:
        raise ManifestContractError(
            "Language publication requires a qualified manifest-v3 peer; current generation retained."
        )
    manifest = _build_library_manifest_v2(db, generated_at=generated_at)
    if not capable:
        return manifest
    manifest["schema_version"] = "manifest-v3"
    for title in manifest["titles"]:
        for asset in title["assets"]:
            rows = db.execute(
                "SELECT c.asset_id FROM publication_language_contracts c JOIN media_assets a ON a.id=c.asset_id JOIN media_monitors m ON m.id=a.monitor_id WHERE m.anilist_id=? AND lower(a.torrent_hash)=lower(?)",
                (title["anilist_id"], asset["torrent_hash"]),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise ManifestContractError("Cross-owner publication contract is ambiguous.")
                asset["language_contract"] = export_contract(db, asset_id=rows[0][0])
    return validate_library_manifest(manifest)


def validate_curator_receipt(payload):
    if isinstance(payload, dict) and payload.get("schema_version") == "curator-receipt-v3":
        from omakase.plus.language_publication import validate_receipt

        try:
            return validate_receipt(payload)
        except ValueError:
            raise ManifestContractError("Invalid curator-receipt-v3 language contract.") from None
    return _validate_curator_receipt_v2(payload)


def build_library_manifest(db, *, generated_at=None, peer_capabilities=(), qualification=False):
    try:
        return _build_library_manifest_versioned(
            db,
            generated_at=generated_at,
            peer_capabilities=peer_capabilities,
            qualification=qualification,
        )
    except ManifestContractError:
        raise
    except ValueError:
        raise ManifestContractError(
            "Publication source or language contract is unavailable."
        ) from None
