"""Typed SQLite persistence helpers for media-orchestrator state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime

from omakase.plus.media_types import SyncLeaseReceipt

_OWNERSHIP_VALUES = frozenset({"monitor_owned", "external"})
_PROVENANCE_VALUES = frozenset({"omakase_created", "adopted", "pre_existing"})
_NUMBERING_OVERRIDES = frozenset(
    {"mha-s4-absolute-v1", "bleach-tybw-cour4-absolute-v1", "slime-s4-absolute-v1"}
)
_OBSERVATION_STATUSES = frozenset({"queued", "downloading", "downloaded", "error"})
_REMOVAL_OUTCOMES = frozenset({"pending", "deleted", "failed", "refused"})
_PROVIDER_KINDS = frozenset({"realdebrid", "local_import", "qbittorrent"})
_PROVIDER_HEALTH_STATUSES = frozenset({"unknown", "healthy", "degraded", "error"})
_STORAGE_BACKENDS = frozenset({"", "zurg", "local"})
_PROBE_OUTCOMES = frozenset({"usable", "unusable", "error"})
_SUBTITLE_SOURCE_KINDS = frozenset({"embedded", "external"})
_SUBTITLE_SYNC_POLICIES = frozenset({"exact_release", "verified_external", "unverified_external"})
_PUBLICATION_POLICIES = frozenset({"plan_only", "managed"})
_DETAIL_LIMIT = 500
_UTC_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_UTC_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class _Unset:
    """Marker distinguishing an omitted upsert field from an explicit value."""

    __slots__ = ()


_UNSET = _Unset()


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a nonnegative integer")
    if value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _optional_nonnegative_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _nonnegative_int(name, value)


def _optional_positive_int(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    return _positive_int(name, value)


def _nonempty_string(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _string(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _optional_string(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _nonempty_string(name, value)


def _timestamp(name: str, value: str) -> str:
    value = _string(name, value).strip()
    if not _UTC_TIMESTAMP_PATTERN.fullmatch(value):
        raise ValueError(f"{name} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.strptime(value, _UTC_TIMESTAMP_FORMAT)
    except ValueError as exc:
        raise ValueError(f"{name} must be canonical UTC YYYY-MM-DDTHH:MM:SSZ") from exc
    return value


def _optional_timestamp(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    return _timestamp(name, value)


def _boolean(name: str, value: bool) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean")
    return value


def _bounded_detail(name: str, value: str) -> str:
    return _string(name, value)[:_DETAIL_LIMIT]


def _json_dump(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _json_object(name: str, value: object) -> str:
    if not isinstance(value, dict):
        raise TypeError(f"{name} must be an object")
    return _json_dump(value)


def _json_array(name: str, value: object) -> str:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return _json_dump(value)


def _string_values(name: str, values: Iterable[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of strings")
    normalized = list(values)
    if any(not isinstance(value, str) for value in normalized):
        raise TypeError(f"{name} must contain only strings")
    return normalized


def _coverage_values(name: str, values: Iterable[int]) -> list[int]:
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} coverage must be an iterable of positive integers")
    normalized: set[int] = set()
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} coverage must contain only positive integers")
        if value <= 0:
            raise ValueError(f"{name} coverage must contain only positive integers")
        normalized.add(value)
    return sorted(normalized)


def _progress(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("progress must be a number between 0 and 100")
    normalized = float(value)
    if not 0 <= normalized <= 100:
        raise ValueError("progress must be a number between 0 and 100")
    return normalized


def _inserted_id(cursor: sqlite3.Cursor) -> int:
    row_id = cursor.lastrowid
    if row_id is None:
        raise RuntimeError("SQLite did not return an inserted row id")
    return int(row_id)


def ensure_media_provider(
    db: sqlite3.Connection,
    *,
    user_id: int,
    kind: str,
    label: str,
    config: dict,
    capabilities: dict,
    enabled: bool = True,
) -> int:
    """Create or update a secret-free provider configuration."""
    user_id = _positive_int("user_id", user_id)
    kind = _nonempty_string("kind", kind).strip()
    if kind not in _PROVIDER_KINDS:
        raise ValueError(f"kind must be one of {sorted(_PROVIDER_KINDS)}")
    label = _nonempty_string("label", label).strip()
    if len(label) > 100:
        raise ValueError("label is too long")
    enabled = _boolean("enabled", enabled)
    config_json = _json_object("config", config)
    capabilities_json = _json_object("capabilities", capabilities)
    db.execute(
        """INSERT INTO media_providers
           (user_id, kind, label, enabled, config_json, capabilities_json)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, kind, label) DO UPDATE SET
               enabled = excluded.enabled,
               config_json = excluded.config_json,
               capabilities_json = excluded.capabilities_json,
               updated_at = datetime('now')""",
        (
            user_id,
            kind,
            label,
            int(enabled),
            config_json,
            capabilities_json,
        ),
    )
    row = db.execute(
        """SELECT id FROM media_providers
           WHERE user_id = ? AND kind = ? AND label = ?""",
        (user_id, kind, label),
    ).fetchone()
    if row is None:
        raise RuntimeError("provider upsert did not produce a row")
    return int(row["id"])


def update_provider_health(
    db: sqlite3.Connection,
    *,
    provider_id: int,
    status: str,
    reason_code: str,
    detail: str,
    checked_at: str,
) -> None:
    """Persist a bounded, secret-free provider readiness receipt."""
    provider_id = _positive_int("provider_id", provider_id)
    if status not in _PROVIDER_HEALTH_STATUSES - {"unknown"}:
        raise ValueError("provider health status is invalid")
    reason_code = _string("reason_code", reason_code)
    if len(reason_code) > 100:
        raise ValueError("reason_code is too long")
    detail = _bounded_detail("detail", detail)
    checked_at = _timestamp("checked_at", checked_at)
    cursor = db.execute(
        """UPDATE media_providers
           SET health_status = ?, health_reason_code = ?, health_detail = ?,
               last_health_at = ?, updated_at = datetime('now')
           WHERE id = ?""",
        (status, reason_code, detail, checked_at, provider_id),
    )
    if cursor.rowcount != 1:
        raise ValueError("provider does not exist")


def update_catalog_health(
    db: sqlite3.Connection,
    *,
    user_id: int,
    catalog_kind: str,
    label: str,
    status: str,
    reason_code: str,
    detail: str,
    candidate_count: int,
    checked_at: str,
) -> None:
    """Persist the latest bounded, secret-free catalog search receipt."""
    user_id = _positive_int("user_id", user_id)
    catalog_kind = _nonempty_string("catalog_kind", catalog_kind).strip().casefold()
    label = _nonempty_string("label", label).strip()
    if len(catalog_kind) > 100 or len(label) > 200:
        raise ValueError("catalog identity is too long")
    if status not in {"healthy", "degraded", "error", "unknown"}:
        raise ValueError("catalog health status is invalid")
    reason_code = _string("reason_code", reason_code)
    if len(reason_code) > 100:
        raise ValueError("reason_code is too long")
    detail = _bounded_detail("detail", detail)
    candidate_count = _nonnegative_int("candidate_count", candidate_count)
    checked_at = _timestamp("checked_at", checked_at)
    db.execute(
        """INSERT INTO media_catalog_health
           (user_id, catalog_kind, label, health_status, reason_code,
            detail, candidate_count, checked_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, catalog_kind) DO UPDATE SET
               label = excluded.label,
               health_status = excluded.health_status,
               reason_code = excluded.reason_code,
               detail = excluded.detail,
               candidate_count = excluded.candidate_count,
               checked_at = excluded.checked_at""",
        (
            user_id,
            catalog_kind,
            label,
            status,
            reason_code,
            detail,
            candidate_count,
            checked_at,
        ),
    )


def record_media_file_probe(
    db: sqlite3.Connection,
    *,
    asset_id: int,
    file_locator: str,
    inventory_fingerprint: str,
    byte_range_receipt: dict,
    video_streams: list,
    audio_streams: list,
    outcome: str,
    detail: str,
    verified_at: str,
) -> int:
    """Upsert one fingerprint-bound media playback/stream receipt."""
    asset_id = _positive_int("asset_id", asset_id)
    file_locator = _nonempty_string("file_locator", file_locator)
    if len(file_locator) > 4096:
        raise ValueError("file_locator is too long")
    inventory_fingerprint = _nonempty_string("inventory_fingerprint", inventory_fingerprint)
    if len(inventory_fingerprint) > 128:
        raise ValueError("inventory_fingerprint is too long")
    if outcome not in _PROBE_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_PROBE_OUTCOMES)}")
    detail = _bounded_detail("detail", detail)
    verified_at = _timestamp("verified_at", verified_at)
    db.execute(
        """INSERT INTO media_file_probes
           (asset_id, file_locator, inventory_fingerprint,
            byte_range_receipt_json, video_streams_json, audio_streams_json,
            outcome, detail, verified_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(asset_id, file_locator, inventory_fingerprint) DO UPDATE SET
               byte_range_receipt_json = excluded.byte_range_receipt_json,
               video_streams_json = excluded.video_streams_json,
               audio_streams_json = excluded.audio_streams_json,
               outcome = excluded.outcome,
               detail = excluded.detail,
               verified_at = excluded.verified_at""",
        (
            asset_id,
            file_locator,
            inventory_fingerprint,
            _json_object("byte_range_receipt", byte_range_receipt),
            _json_array("video_streams", video_streams),
            _json_array("audio_streams", audio_streams),
            outcome,
            detail,
            verified_at,
        ),
    )
    row = db.execute(
        """SELECT id FROM media_file_probes
           WHERE asset_id = ? AND file_locator = ? AND inventory_fingerprint = ?""",
        (asset_id, file_locator, inventory_fingerprint),
    ).fetchone()
    if row is None:
        raise RuntimeError("media probe upsert did not produce a row")
    return int(row["id"])


def replace_subtitle_tracks(
    db: sqlite3.Connection,
    *,
    probe_id: int,
    tracks: Iterable[dict[str, object]],
) -> None:
    """Replace the normalized subtitle stream set for one immutable probe."""
    probe_id = _positive_int("probe_id", probe_id)
    normalized = list(tracks)
    values: list[tuple[object, ...]] = []
    seen: set[tuple[int, str]] = set()
    for track in normalized:
        if not isinstance(track, dict):
            raise TypeError("subtitle tracks must be objects")
        stream_index = _nonnegative_int("stream_index", track.get("stream_index"))
        language = _nonempty_string("language", track.get("language", "und")).strip()
        codec = _string("codec", track.get("codec", "")).strip()
        source_kind = _nonempty_string("source_kind", track.get("source_kind", "")).strip()
        sync_policy = _nonempty_string("sync_policy", track.get("sync_policy", "")).strip()
        if source_kind not in _SUBTITLE_SOURCE_KINDS:
            raise ValueError("subtitle source_kind is invalid")
        if sync_policy not in _SUBTITLE_SYNC_POLICIES:
            raise ValueError("subtitle sync_policy is invalid")
        key = (stream_index, source_kind)
        if key in seen:
            raise ValueError("duplicate subtitle stream")
        seen.add(key)
        values.append(
            (
                probe_id,
                stream_index,
                language,
                codec,
                int(_boolean("switchable", track.get("switchable", True))),
                int(_boolean("default_track", track.get("default_track", False))),
                int(_boolean("forced", track.get("forced", False))),
                source_kind,
                sync_policy,
            )
        )
    if db.execute("SELECT 1 FROM media_file_probes WHERE id = ?", (probe_id,)).fetchone() is None:
        raise ValueError("media probe does not exist")
    db.execute("DELETE FROM media_subtitle_tracks WHERE probe_id = ?", (probe_id,))
    db.executemany(
        """INSERT INTO media_subtitle_tracks
           (probe_id, stream_index, language, codec, switchable, default_track,
            forced, source_kind, sync_policy)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )


def upsert_plex_target(
    db: sqlite3.Connection,
    *,
    user_id: int,
    label: str,
    base_url: str,
    token_secret_name: str,
    library_map: dict,
    publication_policy: str = "plan_only",
) -> int:
    """Persist a Plex target while keeping its token in the encrypted secret store."""
    user_id = _positive_int("user_id", user_id)
    label = _nonempty_string("label", label).strip()
    base_url = _nonempty_string("base_url", base_url).strip()
    token_secret_name = _nonempty_string("token_secret_name", token_secret_name).strip()
    if publication_policy not in _PUBLICATION_POLICIES:
        raise ValueError("publication_policy is invalid")
    db.execute(
        """INSERT INTO plex_targets
           (user_id, label, base_url, token_secret_name, library_map_json,
            publication_policy)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, label) DO UPDATE SET
               base_url = excluded.base_url,
               token_secret_name = excluded.token_secret_name,
               library_map_json = excluded.library_map_json,
               publication_policy = excluded.publication_policy,
               updated_at = datetime('now')""",
        (
            user_id,
            label,
            base_url,
            token_secret_name,
            _json_object("library_map", library_map),
            publication_policy,
        ),
    )
    row = db.execute(
        "SELECT id FROM plex_targets WHERE user_id = ? AND label = ?",
        (user_id, label),
    ).fetchone()
    if row is None:
        raise RuntimeError("Plex target upsert did not produce a row")
    return int(row["id"])


def upsert_monitor(
    db: sqlite3.Connection,
    *,
    user_id: int,
    anilist_id: int,
    title: str,
    search_titles: Iterable[str] | _Unset = _UNSET,
    genres: Iterable[str] | _Unset = _UNSET,
    anilist_planning_id: int | None | _Unset = _UNSET,
    format: str | _Unset = _UNSET,
    release_status: str | _Unset = _UNSET,
    season_number: int | None | _Unset = _UNSET,
    expected_episodes: int | None | _Unset = _UNSET,
    aired_episodes: int | _Unset = _UNSET,
    next_airing_at: int | None | _Unset = _UNSET,
    active: bool | _Unset = _UNSET,
    last_checked_at: str | None | _Unset = _UNSET,
    last_success_at: str | None | _Unset = _UNSET,
    next_check_at: str | None | _Unset = _UNSET,
    consecutive_failures: int | _Unset = _UNSET,
    last_result: str | _Unset = _UNSET,
) -> int:
    """Insert a monitor or partially refresh an existing monitor atomically."""
    user_id = _positive_int("user_id", user_id)
    anilist_id = _positive_int("anilist_id", anilist_id)
    title = _nonempty_string("title", title)

    planning_set = anilist_planning_id is not _UNSET
    planning_value = (
        None
        if not planning_set
        else _optional_positive_int("anilist_planning_id", anilist_planning_id)
    )
    search_titles_set = search_titles is not _UNSET
    search_titles_json = _json_dump(
        [] if not search_titles_set else _string_values("search_titles", search_titles)
    )
    genres_set = genres is not _UNSET
    genres_json = _json_dump([] if not genres_set else _string_values("genres", genres))
    format_set = format is not _UNSET
    format_value = "TV" if not format_set else _nonempty_string("format", format)
    release_status_set = release_status is not _UNSET
    release_status_value = (
        "" if not release_status_set else _string("release_status", release_status)
    )
    season_number_set = season_number is not _UNSET
    season_number_value = (
        None if not season_number_set else _optional_nonnegative_int("season_number", season_number)
    )
    expected_episodes_set = expected_episodes is not _UNSET
    expected_episodes_value = (
        None
        if not expected_episodes_set
        else _optional_nonnegative_int("expected_episodes", expected_episodes)
    )
    aired_episodes_set = aired_episodes is not _UNSET
    aired_episodes_value = (
        0 if not aired_episodes_set else _nonnegative_int("aired_episodes", aired_episodes)
    )
    next_airing_at_set = next_airing_at is not _UNSET
    next_airing_at_value = (
        None
        if not next_airing_at_set or next_airing_at is None
        else _positive_int("next_airing_at", next_airing_at)
    )
    active_set = active is not _UNSET
    active_value = True if not active_set else _boolean("active", active)
    last_checked_at_set = last_checked_at is not _UNSET
    last_checked_at_value = (
        None if not last_checked_at_set else _optional_timestamp("last_checked_at", last_checked_at)
    )
    last_success_at_set = last_success_at is not _UNSET
    last_success_at_value = (
        None if not last_success_at_set else _optional_timestamp("last_success_at", last_success_at)
    )
    next_check_at_set = next_check_at is not _UNSET
    next_check_at_value = (
        None if not next_check_at_set else _optional_timestamp("next_check_at", next_check_at)
    )
    if (
        last_checked_at_set
        and last_success_at_set
        and last_checked_at_value is not None
        and last_success_at_value is not None
        and last_success_at_value > last_checked_at_value
    ):
        raise ValueError("last_success_at must not be after last_checked_at")
    failures_set = consecutive_failures is not _UNSET
    failures_value = (
        0 if not failures_set else _nonnegative_int("consecutive_failures", consecutive_failures)
    )
    last_result_set = last_result is not _UNSET
    last_result_value = "" if not last_result_set else _bounded_detail("last_result", last_result)

    db.execute(
        """INSERT INTO media_monitors
           (user_id, anilist_planning_id, anilist_id, title, search_titles_json,
            genres_json, format, release_status, season_number, expected_episodes,
            aired_episodes, next_airing_at, active, last_checked_at, last_success_at,
            next_check_at, consecutive_failures, last_result)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(user_id, anilist_id) DO UPDATE SET
               anilist_planning_id = CASE WHEN ?
                   THEN excluded.anilist_planning_id ELSE media_monitors.anilist_planning_id END,
               title = excluded.title,
               search_titles_json = CASE WHEN ?
                   THEN excluded.search_titles_json ELSE media_monitors.search_titles_json END,
               genres_json = CASE WHEN ?
                   THEN excluded.genres_json ELSE media_monitors.genres_json END,
               format = CASE WHEN ?
                   THEN excluded.format ELSE media_monitors.format END,
               release_status = CASE WHEN ?
                   THEN excluded.release_status ELSE media_monitors.release_status END,
               season_number = CASE WHEN ?
                   THEN excluded.season_number ELSE media_monitors.season_number END,
               expected_episodes = CASE WHEN ?
                   THEN excluded.expected_episodes ELSE media_monitors.expected_episodes END,
               aired_episodes = CASE WHEN ?
                   THEN excluded.aired_episodes ELSE media_monitors.aired_episodes END,
               next_airing_at = CASE WHEN ?
                   THEN excluded.next_airing_at ELSE media_monitors.next_airing_at END,
               active = CASE WHEN ?
                   THEN excluded.active ELSE media_monitors.active END,
               last_checked_at = CASE WHEN ?
                   THEN excluded.last_checked_at ELSE media_monitors.last_checked_at END,
               last_success_at = CASE WHEN ?
                   THEN excluded.last_success_at ELSE media_monitors.last_success_at END,
               next_check_at = CASE WHEN ?
                   THEN excluded.next_check_at ELSE media_monitors.next_check_at END,
               consecutive_failures = CASE WHEN ?
                   THEN excluded.consecutive_failures ELSE media_monitors.consecutive_failures END,
               last_result = CASE WHEN ?
                   THEN excluded.last_result ELSE media_monitors.last_result END,
               updated_at = datetime('now')""",
        (
            user_id,
            planning_value,
            anilist_id,
            title,
            search_titles_json,
            genres_json,
            format_value,
            release_status_value,
            season_number_value,
            expected_episodes_value,
            aired_episodes_value,
            next_airing_at_value,
            int(active_value),
            last_checked_at_value,
            last_success_at_value,
            next_check_at_value,
            failures_value,
            last_result_value,
            int(planning_set),
            int(search_titles_set),
            int(genres_set),
            int(format_set),
            int(release_status_set),
            int(season_number_set),
            int(expected_episodes_set),
            int(aired_episodes_set),
            int(next_airing_at_set),
            int(active_set),
            int(last_checked_at_set),
            int(last_success_at_set),
            int(next_check_at_set),
            int(failures_set),
            int(last_result_set),
        ),
    )
    row = db.execute(
        "SELECT id FROM media_monitors WHERE user_id = ? AND anilist_id = ?",
        (user_id, anilist_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("monitor upsert did not produce a row")
    return int(row[0])


def list_due_monitors(
    db: sqlite3.Connection,
    *,
    now: str,
    limit: int = 100,
) -> list[sqlite3.Row]:
    """Return active monitors scheduled no later than ``now`` in stable order."""
    now = _timestamp("now", now)
    limit = _positive_int("limit", limit)
    return list(
        db.execute(
            """SELECT *
               FROM media_monitors
               WHERE active = 1
                 AND (
                     next_check_at IS NULL
                     OR (
                         length(next_check_at) = 20
                         AND next_check_at GLOB
                             '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T'
                             || '[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z'
                         AND substr(next_check_at, 1, 4) BETWEEN '0001' AND '9999'
                         AND substr(next_check_at, 12, 2) BETWEEN '00' AND '23'
                         AND strftime(
                             '%Y-%m-%dT%H:%M:%SZ', next_check_at
                         ) = next_check_at
                         AND next_check_at <= ?
                     )
                 )
               ORDER BY
                   CASE WHEN next_check_at IS NULL THEN 0 ELSE 1 END,
                   next_check_at ASC,
                   id ASC
               LIMIT ?""",
            (now, limit),
        ).fetchall()
    )


def upsert_asset(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    torrent_hash: str,
    source_title: str,
    rd_torrent_id: str | _Unset = _UNSET,
    provider_id: int | None | _Unset = _UNSET,
    provider_asset_id: str | _Unset = _UNSET,
    storage_backend: str | _Unset = _UNSET,
    storage_locator: str | _Unset = _UNSET,
    source_folder: str | _Unset = _UNSET,
    quality: str | _Unset = _UNSET,
    seeders: int | _Unset = _UNSET,
    ownership: str | _Unset = _UNSET,
    provenance: str | _Unset = _UNSET,
    created_by_run_id: int | None | _Unset = _UNSET,
    shared_reference_count: int | _Unset = _UNSET,
    downloaded_once: bool | None | _Unset = _UNSET,
    replaces_asset_id: int | None | _Unset = _UNSET,
    replaced_by_asset_id: int | None | _Unset = _UNSET,
    planned_coverage: Iterable[int] | _Unset = _UNSET,
    usable_coverage: Iterable[int] | _Unset = _UNSET,
    numbering_override: str | None | _Unset = _UNSET,
    inventory_fingerprint: str | _Unset = _UNSET,
    inventory_verified_at: str | None | _Unset = _UNSET,
    status: str | _Unset = _UNSET,
    progress: float | _Unset = _UNSET,
    stagnant_checks: int | _Unset = _UNSET,
    last_error: str | _Unset = _UNSET,
) -> int:
    """Insert an asset or partially refresh an existing asset atomically."""
    monitor_id = _positive_int("monitor_id", monitor_id)
    torrent_hash = _nonempty_string("torrent_hash", torrent_hash).strip().upper()
    source_title = _nonempty_string("source_title", source_title)

    rd_torrent_id_set = rd_torrent_id is not _UNSET
    rd_torrent_id_value = "" if not rd_torrent_id_set else _string("rd_torrent_id", rd_torrent_id)
    provider_id_set = provider_id is not _UNSET
    provider_id_value = (
        None if not provider_id_set else _optional_positive_int("provider_id", provider_id)
    )
    provider_asset_id_set = provider_asset_id is not _UNSET or rd_torrent_id_set
    provider_asset_id_value = (
        _string("provider_asset_id", provider_asset_id)
        if provider_asset_id is not _UNSET
        else rd_torrent_id_value
    )
    if len(provider_asset_id_value) > 500:
        raise ValueError("provider_asset_id is too long")
    source_folder_set = source_folder is not _UNSET
    source_folder_value = "" if not source_folder_set else _string("source_folder", source_folder)
    storage_backend_set = storage_backend is not _UNSET or source_folder_set
    storage_backend_value = (
        _string("storage_backend", storage_backend)
        if storage_backend is not _UNSET
        else ("zurg" if source_folder_value else "")
    )
    if storage_backend_value not in _STORAGE_BACKENDS:
        raise ValueError(f"storage_backend must be one of {sorted(_STORAGE_BACKENDS)}")
    storage_locator_set = storage_locator is not _UNSET or source_folder_set
    storage_locator_value = (
        _string("storage_locator", storage_locator)
        if storage_locator is not _UNSET
        else source_folder_value
    )
    if len(storage_locator_value) > 4096:
        raise ValueError("storage_locator is too long")
    quality_set = quality is not _UNSET
    quality_value = "" if not quality_set else _string("quality", quality)
    seeders_set = seeders is not _UNSET
    seeders_value = 0 if not seeders_set else _nonnegative_int("seeders", seeders)
    ownership_set = ownership is not _UNSET
    ownership_value = "external" if not ownership_set else ownership
    if ownership_value not in _OWNERSHIP_VALUES:
        raise ValueError(f"ownership must be one of {sorted(_OWNERSHIP_VALUES)}")
    provenance_set = provenance is not _UNSET
    provenance_value = "pre_existing" if not provenance_set else provenance
    if provenance_value not in _PROVENANCE_VALUES:
        raise ValueError(f"provenance must be one of {sorted(_PROVENANCE_VALUES)}")
    created_by_run_id_set = created_by_run_id is not _UNSET
    created_by_run_id_value = (
        None
        if not created_by_run_id_set
        else _optional_positive_int("created_by_run_id", created_by_run_id)
    )
    shared_reference_count_set = shared_reference_count is not _UNSET
    shared_reference_count_value = (
        0
        if not shared_reference_count_set
        else _nonnegative_int("shared_reference_count", shared_reference_count)
    )
    status_set = status is not _UNSET
    status_value = "candidate" if not status_set else _nonempty_string("status", status)
    downloaded_once_set = downloaded_once is not _UNSET
    if downloaded_once is _UNSET or downloaded_once is None:
        downloaded_once_value = False
    else:
        downloaded_once_value = _boolean("downloaded_once", downloaded_once)
    if status_set and status_value == "downloaded":
        if downloaded_once is False:
            raise ValueError("downloaded status requires downloaded_once")
        downloaded_once_value = True
        downloaded_once_set = True
    replaces_asset_id_set = replaces_asset_id is not _UNSET
    replaces_asset_id_value = (
        None
        if not replaces_asset_id_set
        else _optional_positive_int("replaces_asset_id", replaces_asset_id)
    )
    replaced_by_asset_id_set = replaced_by_asset_id is not _UNSET
    replaced_by_asset_id_value = (
        None
        if not replaced_by_asset_id_set
        else _optional_positive_int("replaced_by_asset_id", replaced_by_asset_id)
    )
    planned_coverage_set = planned_coverage is not _UNSET
    planned_coverage_json = _json_dump(
        [] if not planned_coverage_set else _coverage_values("planned", planned_coverage)
    )
    usable_coverage_set = usable_coverage is not _UNSET
    usable_coverage_json = _json_dump(
        [] if not usable_coverage_set else _coverage_values("usable", usable_coverage)
    )
    numbering_override_set = numbering_override is not _UNSET
    numbering_override_value = (
        None
        if not numbering_override_set or numbering_override is None
        else _nonempty_string("numbering_override", numbering_override)
    )
    if (
        numbering_override_value is not None
        and numbering_override_value not in _NUMBERING_OVERRIDES
    ):
        raise ValueError("numbering_override is not allowlisted")
    inventory_fingerprint_set = inventory_fingerprint is not _UNSET
    inventory_fingerprint_value = (
        ""
        if not inventory_fingerprint_set
        else _string("inventory_fingerprint", inventory_fingerprint)
    )
    inventory_verified_at_set = inventory_verified_at is not _UNSET
    inventory_verified_at_value = (
        None
        if not inventory_verified_at_set
        else _optional_timestamp("inventory_verified_at", inventory_verified_at)
    )
    progress_set = progress is not _UNSET
    progress_value = 0.0 if not progress_set else _progress(progress)
    stagnant_checks_set = stagnant_checks is not _UNSET
    stagnant_checks_value = (
        0 if not stagnant_checks_set else _nonnegative_int("stagnant_checks", stagnant_checks)
    )
    last_error_set = last_error is not _UNSET
    last_error_value = "" if not last_error_set else _bounded_detail("last_error", last_error)

    try:
        db.execute(
            """INSERT INTO media_assets
           (monitor_id, torrent_hash, rd_torrent_id, provider_id, provider_asset_id,
            storage_backend, storage_locator, source_title, source_folder,
            quality, seeders, ownership, provenance, created_by_run_id,
            shared_reference_count, downloaded_once, replaces_asset_id,
            replaced_by_asset_id, planned_coverage_json, usable_coverage_json,
            numbering_override, inventory_fingerprint, inventory_verified_at, status, progress,
            stagnant_checks, last_error)
           VALUES (
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
               CASE WHEN ? THEN ? ELSE (
                   SELECT existing.created_by_run_id
                   FROM media_assets AS existing
                   WHERE existing.monitor_id = ? AND existing.torrent_hash = ?
               ) END,
               ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
           )
           ON CONFLICT(monitor_id, torrent_hash) DO UPDATE SET
               rd_torrent_id = CASE WHEN ?
                   THEN excluded.rd_torrent_id ELSE media_assets.rd_torrent_id END,
               provider_id = CASE WHEN ?
                   THEN excluded.provider_id ELSE media_assets.provider_id END,
               provider_asset_id = CASE WHEN ?
                   THEN excluded.provider_asset_id ELSE media_assets.provider_asset_id END,
               storage_backend = CASE WHEN ?
                   THEN excluded.storage_backend ELSE media_assets.storage_backend END,
               storage_locator = CASE WHEN ?
                   THEN excluded.storage_locator ELSE media_assets.storage_locator END,
               source_title = excluded.source_title,
               source_folder = CASE WHEN ?
                   THEN excluded.source_folder ELSE media_assets.source_folder END,
               quality = CASE WHEN ?
                   THEN excluded.quality ELSE media_assets.quality END,
               seeders = CASE WHEN ?
                   THEN excluded.seeders ELSE media_assets.seeders END,
               ownership = CASE WHEN ?
                   THEN excluded.ownership ELSE media_assets.ownership END,
               provenance = CASE WHEN ?
                   THEN excluded.provenance ELSE media_assets.provenance END,
               created_by_run_id = CASE WHEN ?
                   THEN excluded.created_by_run_id ELSE media_assets.created_by_run_id END,
               shared_reference_count = CASE WHEN ?
                   THEN excluded.shared_reference_count ELSE media_assets.shared_reference_count END,
               downloaded_once = CASE
                   WHEN media_assets.downloaded_once = 1 THEN 1
                   WHEN ? THEN excluded.downloaded_once
                   ELSE media_assets.downloaded_once
               END,
               replaces_asset_id = CASE WHEN ?
                   THEN excluded.replaces_asset_id ELSE media_assets.replaces_asset_id END,
               replaced_by_asset_id = CASE WHEN ?
                   THEN excluded.replaced_by_asset_id ELSE media_assets.replaced_by_asset_id END,
               planned_coverage_json = CASE WHEN ?
                   THEN excluded.planned_coverage_json ELSE media_assets.planned_coverage_json END,
               usable_coverage_json = CASE WHEN ?
                   THEN excluded.usable_coverage_json ELSE media_assets.usable_coverage_json END,
               numbering_override = CASE WHEN ?
                   THEN excluded.numbering_override ELSE media_assets.numbering_override END,
               inventory_fingerprint = CASE WHEN ?
                   THEN excluded.inventory_fingerprint ELSE media_assets.inventory_fingerprint END,
               inventory_verified_at = CASE WHEN ?
                   THEN excluded.inventory_verified_at ELSE media_assets.inventory_verified_at END,
               status = CASE WHEN ?
                   THEN excluded.status ELSE media_assets.status END,
               progress = CASE WHEN ?
                   THEN excluded.progress ELSE media_assets.progress END,
               stagnant_checks = CASE WHEN ?
                   THEN excluded.stagnant_checks ELSE media_assets.stagnant_checks END,
               last_error = CASE WHEN ?
                   THEN excluded.last_error ELSE media_assets.last_error END,
               updated_at = datetime('now')""",
            (
                monitor_id,
                torrent_hash,
                rd_torrent_id_value,
                provider_id_value,
                provider_asset_id_value,
                storage_backend_value,
                storage_locator_value,
                source_title,
                source_folder_value,
                quality_value,
                seeders_value,
                ownership_value,
                provenance_value,
                int(created_by_run_id_set),
                created_by_run_id_value,
                monitor_id,
                torrent_hash,
                shared_reference_count_value,
                int(downloaded_once_value),
                replaces_asset_id_value,
                replaced_by_asset_id_value,
                planned_coverage_json,
                usable_coverage_json,
                numbering_override_value,
                inventory_fingerprint_value,
                inventory_verified_at_value,
                status_value,
                progress_value,
                stagnant_checks_value,
                last_error_value,
                int(rd_torrent_id_set),
                int(provider_id_set),
                int(provider_asset_id_set),
                int(storage_backend_set),
                int(storage_locator_set),
                int(source_folder_set),
                int(quality_set),
                int(seeders_set),
                int(ownership_set),
                int(provenance_set),
                int(created_by_run_id_set),
                int(shared_reference_count_set),
                int(downloaded_once_set),
                int(replaces_asset_id_set),
                int(replaced_by_asset_id_set),
                int(planned_coverage_set),
                int(usable_coverage_set),
                int(numbering_override_set),
                int(inventory_fingerprint_set),
                int(inventory_verified_at_set),
                int(status_set),
                int(progress_set),
                int(stagnant_checks_set),
                int(last_error_set),
            ),
        )
    except sqlite3.IntegrityError as exc:
        if (
            provenance_set
            and provenance_value == "omakase_created"
            and created_by_run_id_value is None
            and "provenance" in str(exc)
        ):
            raise ValueError("omakase_created provenance requires created_by_run_id") from exc
        raise
    row = db.execute(
        "SELECT id FROM media_assets WHERE monitor_id = ? AND torrent_hash = ?",
        (monitor_id, torrent_hash),
    ).fetchone()
    if row is None:
        raise RuntimeError("asset upsert did not produce a row")
    return int(row[0])


def exclude_asset_episodes(
    db: sqlite3.Connection,
    *,
    asset_id: int,
    episodes: Iterable[int],
    reason: str,
) -> dict[str, object]:
    """Persist playback failures so RD refreshes cannot re-admit bad episode files."""
    asset_id = _positive_int("asset_id", asset_id)
    excluded = set(_coverage_values("unusable", episodes))
    if not excluded:
        raise ValueError("unusable coverage must not be empty")
    reason = _nonempty_string("reason", reason)
    row = db.execute(
        """SELECT usable_coverage_json, unusable_coverage_json
           FROM media_assets WHERE id = ?""",
        (asset_id,),
    ).fetchone()
    if row is None:
        raise ValueError("media asset not found")
    try:
        usable = set(_coverage_values("usable", json.loads(row["usable_coverage_json"])))
        prior = set(_coverage_values("unusable", json.loads(row["unusable_coverage_json"])))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("media asset coverage is invalid") from exc
    combined = prior | excluded
    remaining = usable - combined
    db.execute(
        """UPDATE media_assets
           SET usable_coverage_json = ?,
               unusable_coverage_json = ?,
               last_error = ?,
               updated_at = datetime('now')
           WHERE id = ?""",
        (
            _json_dump(sorted(remaining)),
            _json_dump(sorted(combined)),
            _bounded_detail("reason", reason),
            asset_id,
        ),
    )
    return {
        "asset_id": asset_id,
        "excluded_episodes": tuple(sorted(combined)),
        "usable_episodes": tuple(sorted(remaining)),
        "reason": _bounded_detail("reason", reason),
    }


def record_sync_run(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    outcome: str,
    required_count: int = 0,
    covered_count: int = 0,
    accepted_count: int = 0,
    detail: str = "",
    completed_at: str | None = None,
) -> int:
    """Record one bounded synchronization summary and return its row ID."""
    monitor_id = _positive_int("monitor_id", monitor_id)
    outcome = _nonempty_string("outcome", outcome)
    required_count = _nonnegative_int("required_count", required_count)
    covered_count = _nonnegative_int("covered_count", covered_count)
    accepted_count = _nonnegative_int("accepted_count", accepted_count)
    detail = _bounded_detail("detail", detail)
    completed_at = _optional_timestamp("completed_at", completed_at)
    cursor = db.execute(
        """INSERT INTO media_sync_runs
           (monitor_id, outcome, required_count, covered_count, accepted_count,
            detail, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            outcome,
            required_count,
            covered_count,
            accepted_count,
            detail,
            completed_at,
        ),
    )
    return _inserted_id(cursor)


def complete_sync_run(
    db: sqlite3.Connection,
    *,
    sync_run_id: int,
    outcome: str,
    required_count: int,
    covered_count: int,
    accepted_count: int,
    detail: str,
    completed_at: str,
) -> None:
    """Terminalize one previously incomplete synchronization receipt."""
    sync_run_id = _positive_int("sync_run_id", sync_run_id)
    outcome = _nonempty_string("outcome", outcome)
    required_count = _nonnegative_int("required_count", required_count)
    covered_count = _nonnegative_int("covered_count", covered_count)
    accepted_count = _nonnegative_int("accepted_count", accepted_count)
    detail = _bounded_detail("detail", detail)
    completed_at = _timestamp("completed_at", completed_at)
    cursor = db.execute(
        """UPDATE media_sync_runs
           SET outcome = ?, required_count = ?, covered_count = ?,
               accepted_count = ?, detail = ?, completed_at = ?
           WHERE id = ? AND completed_at IS NULL""",
        (
            outcome,
            required_count,
            covered_count,
            accepted_count,
            detail,
            completed_at,
            sync_run_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ValueError("sync run is missing or already complete")


def record_candidate_attempt(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    sync_run_id: int,
    search_alias: str,
    source_title: str,
    torrent_hash: str,
    candidate_rank: int,
    quality: str,
    seeders: int,
    proposed_coverage: Iterable[int],
    decision: str,
    reason: str = "",
    rd_outcome: str = "",
    provider_outcome: str | None = None,
    provider_kind: str = "realdebrid",
    catalog_kind: str = "nyaa",
    instant_checked: bool = False,
    instant_available: bool = False,
    terminal_at: str | None = None,
    asset_id: int | None = None,
) -> int:
    """Append one sanitized candidate decision or RD-attempt receipt."""
    monitor_id = _positive_int("monitor_id", monitor_id)
    sync_run_id = _positive_int("sync_run_id", sync_run_id)
    asset_id = _optional_positive_int("asset_id", asset_id)
    search_alias = _nonempty_string("search_alias", search_alias)
    source_title = _string("source_title", source_title)
    if len(source_title) > 300:
        raise ValueError("source_title is too long")
    torrent_hash = _string("torrent_hash", torrent_hash).strip().upper()
    candidate_rank = _nonnegative_int("candidate_rank", candidate_rank)
    quality = _string("quality", quality)
    seeders = _nonnegative_int("seeders", seeders)
    coverage_json = _json_dump(_coverage_values("proposed", proposed_coverage))
    decision = _nonempty_string("decision", decision)
    reason = _bounded_detail("reason", reason)
    rd_outcome = _bounded_detail("rd_outcome", rd_outcome)
    provider_outcome = _bounded_detail(
        "provider_outcome",
        rd_outcome if provider_outcome is None else provider_outcome,
    )
    provider_kind = _nonempty_string("provider_kind", provider_kind)
    catalog_kind = _nonempty_string("catalog_kind", catalog_kind)
    if len(provider_kind) > 100 or len(catalog_kind) > 100:
        raise ValueError("candidate provenance is too long")
    instant_checked = _boolean("instant_checked", instant_checked)
    instant_available = _boolean("instant_available", instant_available)
    terminal_at = _optional_timestamp("terminal_at", terminal_at)
    cursor = db.execute(
        """INSERT INTO media_candidate_attempts
           (monitor_id, sync_run_id, asset_id, search_alias, source_title, torrent_hash,
            candidate_rank, quality, seeders, proposed_coverage_json, decision,
            reason, rd_outcome, provider_outcome, provider_kind, catalog_kind,
            instant_checked, instant_available, terminal_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            sync_run_id,
            asset_id,
            search_alias,
            source_title,
            torrent_hash,
            candidate_rank,
            quality,
            seeders,
            coverage_json,
            decision,
            reason,
            rd_outcome,
            provider_outcome,
            provider_kind,
            catalog_kind,
            int(instant_checked),
            int(instant_available),
            terminal_at,
        ),
    )
    return _inserted_id(cursor)


def record_alias_search_receipt(
    db: sqlite3.Connection,
    *,
    monitor_id: int,
    sync_run_id: int,
    query_version: str,
    search_alias: str,
    started_at: str,
    completed_at: str,
    candidate_count: int,
    eligible_1080p_count: int,
    attempted_1080p_count: int,
    cursor: str,
    provider_complete: bool,
    exhaustion_outcome: str,
) -> int:
    """Persist deterministic per-alias search-window evidence."""
    monitor_id = _positive_int("monitor_id", monitor_id)
    sync_run_id = _positive_int("sync_run_id", sync_run_id)
    query_version = _nonempty_string("query_version", query_version)
    search_alias = _nonempty_string("search_alias", search_alias)
    started_at = _timestamp("started_at", started_at)
    completed_at = _timestamp("completed_at", completed_at)
    if completed_at < started_at:
        raise ValueError("completed_at must not be before started_at")
    candidate_count = _nonnegative_int("candidate_count", candidate_count)
    eligible_1080p_count = _nonnegative_int("eligible_1080p_count", eligible_1080p_count)
    attempted_1080p_count = _nonnegative_int("attempted_1080p_count", attempted_1080p_count)
    cursor = _string("cursor", cursor)
    provider_complete = _boolean("provider_complete", provider_complete)
    exhaustion_outcome = _bounded_detail("exhaustion_outcome", exhaustion_outcome)
    row = db.execute(
        """INSERT INTO media_alias_search_receipts
           (monitor_id, sync_run_id, query_version, search_alias, started_at,
            completed_at, candidate_count, eligible_1080p_count,
            attempted_1080p_count, cursor, provider_complete, exhaustion_outcome)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            monitor_id,
            sync_run_id,
            query_version,
            search_alias,
            started_at,
            completed_at,
            candidate_count,
            eligible_1080p_count,
            attempted_1080p_count,
            cursor,
            int(provider_complete),
            exhaustion_outcome,
        ),
    )
    return _inserted_id(row)


def acquire_sync_lease(
    db: sqlite3.Connection,
    *,
    owner: str,
    entrypoint: str,
    acquired_at: str,
    expires_at: str,
) -> SyncLeaseReceipt:
    """Atomically acquire or recover the singleton media synchronization lease."""
    owner = _nonempty_string("owner", owner)
    entrypoint = _nonempty_string("entrypoint", entrypoint)
    acquired_at = _timestamp("acquired_at", acquired_at)
    expires_at = _timestamp("expires_at", expires_at)
    if expires_at <= acquired_at:
        raise ValueError("expires_at must be after acquired_at")
    prior = db.execute(
        "SELECT owner, expires_at, prior_owner_hash, prior_expires_at "
        "FROM media_sync_lease WHERE id = 1"
    ).fetchone()
    recovered_owner_hash = ""
    prior_expires_at = ""
    if prior is not None and prior["owner"] != owner and prior["expires_at"] <= acquired_at:
        recovered_owner_hash = hashlib.sha256(str(prior["owner"]).encode()).hexdigest()
        prior_expires_at = str(prior["expires_at"])
    preserved_hash = str(prior["prior_owner_hash"]) if prior is not None else ""
    preserved_expiry = str(prior["prior_expires_at"]) if prior is not None else ""
    db.execute(
        """INSERT INTO media_sync_lease
           (id, owner, acquired_at, heartbeat_at, expires_at, entrypoint,
            prior_owner_hash, prior_expires_at)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO UPDATE SET
               owner = excluded.owner,
               acquired_at = excluded.acquired_at,
               heartbeat_at = excluded.heartbeat_at,
               expires_at = excluded.expires_at,
               entrypoint = excluded.entrypoint,
               prior_owner_hash = excluded.prior_owner_hash,
               prior_expires_at = excluded.prior_expires_at
           WHERE media_sync_lease.owner = excluded.owner
              OR media_sync_lease.expires_at <= excluded.acquired_at""",
        (
            owner,
            acquired_at,
            acquired_at,
            expires_at,
            entrypoint,
            recovered_owner_hash or preserved_hash,
            prior_expires_at or preserved_expiry,
        ),
    )
    row = db.execute(
        "SELECT owner, expires_at, prior_owner_hash FROM media_sync_lease WHERE id = 1"
    ).fetchone()
    acquired = bool(row and row["owner"] == owner)
    return SyncLeaseReceipt(
        acquired=acquired,
        outcome="acquired" if acquired else "already_running",
        expires_at=str(row["expires_at"]) if row is not None else expires_at,
        recovered_owner_hash=(
            str(row["prior_owner_hash"]) if acquired and recovered_owner_hash else ""
        ),
    )


def heartbeat_sync_lease(
    db: sqlite3.Connection,
    *,
    owner: str,
    heartbeat_at: str,
    expires_at: str,
) -> bool:
    """Extend a live lease only for its opaque owner."""
    owner = _nonempty_string("owner", owner)
    heartbeat_at = _timestamp("heartbeat_at", heartbeat_at)
    expires_at = _timestamp("expires_at", expires_at)
    if expires_at <= heartbeat_at:
        raise ValueError("expires_at must be after heartbeat_at")
    cursor = db.execute(
        """UPDATE media_sync_lease
           SET heartbeat_at = ?, expires_at = ?
           WHERE id = 1 AND owner = ? AND expires_at >= ?""",
        (heartbeat_at, expires_at, owner, heartbeat_at),
    )
    return cursor.rowcount == 1


def release_sync_lease(
    db: sqlite3.Connection,
    *,
    owner: str,
    released_at: str,
) -> bool:
    """Expire a lease without exposing or deleting its recovery evidence."""
    owner = _nonempty_string("owner", owner)
    released_at = _timestamp("released_at", released_at)
    cursor = db.execute(
        """UPDATE media_sync_lease
           SET heartbeat_at = ?, expires_at = ?, entrypoint = 'released'
           WHERE id = 1 AND owner = ?""",
        (released_at, released_at, owner),
    )
    return cursor.rowcount == 1


def record_asset_observation(
    db: sqlite3.Connection,
    *,
    asset_id: int,
    sync_run_id: int,
    observed_at: str,
    rd_status: str,
    provider_status: str | None = None,
    bytes_done: int = 0,
    progress: float = 0,
) -> int:
    """Append a completed-run RD observation and return its row ID."""
    asset_id = _positive_int("asset_id", asset_id)
    sync_run_id = _positive_int("sync_run_id", sync_run_id)
    observed_at = _timestamp("observed_at", observed_at)
    if rd_status not in _OBSERVATION_STATUSES:
        raise ValueError(f"rd_status must be one of {sorted(_OBSERVATION_STATUSES)}")
    provider_status = rd_status if provider_status is None else provider_status
    if provider_status not in _OBSERVATION_STATUSES:
        raise ValueError(f"provider_status must be one of {sorted(_OBSERVATION_STATUSES)}")
    bytes_done = _nonnegative_int("bytes_done", bytes_done)
    progress = _progress(progress)
    cursor = db.execute(
        """INSERT INTO media_asset_observations
           (asset_id, sync_run_id, observed_at, rd_status, provider_status,
            bytes_done, progress)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id,
            sync_run_id,
            observed_at,
            rd_status,
            provider_status,
            bytes_done,
            progress,
        ),
    )
    return _inserted_id(cursor)


def _existing_removal_id(
    db: sqlite3.Connection,
    *,
    operation_key: str,
    asset_id: int,
    replacement_asset_id: int | None,
    candidate_attempt_id: int | None,
    sync_run_id: int,
    support_observation_oldest_id: int | None,
    support_observation_newest_id: int | None,
    requested_at: str,
) -> int | None:
    row = db.execute(
        """SELECT id, asset_id, replacement_asset_id, candidate_attempt_id,
                  sync_run_id, support_observation_oldest_id,
                  support_observation_newest_id, requested_at
           FROM media_asset_removals
           WHERE operation_key = ?""",
        (operation_key,),
    ).fetchone()
    if row is None:
        return None
    expected = (
        asset_id,
        replacement_asset_id,
        candidate_attempt_id,
        sync_run_id,
        support_observation_oldest_id,
        support_observation_newest_id,
        requested_at,
    )
    if tuple(row)[1:] != expected:
        raise ValueError("operation_key already exists with a different immutable payload")
    return int(row[0])


def record_asset_removal(
    db: sqlite3.Connection,
    *,
    operation_key: str,
    asset_id: int,
    sync_run_id: int,
    requested_at: str,
    replacement_asset_id: int | None = None,
    candidate_attempt_id: int | None = None,
    support_observation_oldest_id: int | None = None,
    support_observation_newest_id: int | None = None,
    completed_at: str | None = None,
    outcome: str = "pending",
    detail: str = "",
) -> int:
    """Record a guarded removal without accepting caller-supplied proof JSON."""
    operation_key = _nonempty_string("operation_key", operation_key)
    asset_id = _positive_int("asset_id", asset_id)
    replacement_asset_id = _optional_positive_int("replacement_asset_id", replacement_asset_id)
    candidate_attempt_id = _optional_positive_int("candidate_attempt_id", candidate_attempt_id)
    sync_run_id = _positive_int("sync_run_id", sync_run_id)
    support_observation_oldest_id = _optional_positive_int(
        "support_observation_oldest_id", support_observation_oldest_id
    )
    support_observation_newest_id = _optional_positive_int(
        "support_observation_newest_id", support_observation_newest_id
    )
    requested_at = _timestamp("requested_at", requested_at)
    completed_at = _optional_timestamp("completed_at", completed_at)
    if outcome not in _REMOVAL_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_REMOVAL_OUTCOMES)}")
    if outcome == "pending" and completed_at is not None:
        raise ValueError("pending removal must not have completed_at")
    if outcome != "pending" and completed_at is None:
        raise ValueError("terminal removal requires completed_at")
    if completed_at is not None and completed_at < requested_at:
        raise ValueError("completed_at must not be before requested_at")
    if outcome == "refused":
        if support_observation_oldest_id is not None or support_observation_newest_id is not None:
            raise ValueError("refused removal must not include support observations")
    else:
        required_links = (
            replacement_asset_id,
            candidate_attempt_id,
            support_observation_oldest_id,
            support_observation_newest_id,
        )
        if any(value is None for value in required_links):
            raise ValueError("non-refused removal requires replacement, attempt, and support links")
        if support_observation_oldest_id == support_observation_newest_id:
            raise ValueError("removal support observations must be distinct")
    detail = _bounded_detail("detail", detail)

    existing_id = _existing_removal_id(
        db,
        operation_key=operation_key,
        asset_id=asset_id,
        replacement_asset_id=replacement_asset_id,
        candidate_attempt_id=candidate_attempt_id,
        sync_run_id=sync_run_id,
        support_observation_oldest_id=support_observation_oldest_id,
        support_observation_newest_id=support_observation_newest_id,
        requested_at=requested_at,
    )
    if existing_id is not None:
        return existing_id

    try:
        cursor = db.execute(
            """INSERT INTO media_asset_removals
               (operation_key, asset_id, replacement_asset_id, candidate_attempt_id,
                sync_run_id, support_observation_oldest_id,
                support_observation_newest_id, requested_at, completed_at, outcome,
                detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                operation_key,
                asset_id,
                replacement_asset_id,
                candidate_attempt_id,
                sync_run_id,
                support_observation_oldest_id,
                support_observation_newest_id,
                requested_at,
                completed_at,
                outcome,
                detail,
            ),
        )
    except sqlite3.IntegrityError:
        existing_id = _existing_removal_id(
            db,
            operation_key=operation_key,
            asset_id=asset_id,
            replacement_asset_id=replacement_asset_id,
            candidate_attempt_id=candidate_attempt_id,
            sync_run_id=sync_run_id,
            support_observation_oldest_id=support_observation_oldest_id,
            support_observation_newest_id=support_observation_newest_id,
            requested_at=requested_at,
        )
        if existing_id is None:
            raise
        return existing_id
    return _inserted_id(cursor)


def verified_asset_coverage(asset) -> set[int]:
    """Read the same inventory proof for readiness and release-quality labels."""
    if asset["status"] != "downloaded":
        return set()
    try:
        normalized = _timestamp("inventory_verified_at", asset["inventory_verified_at"])
    except (TypeError, ValueError):
        return set()
    if normalized is None or normalized != asset["inventory_verified_at"]:
        return set()
    values = json.loads(asset["usable_coverage_json"])
    if not isinstance(values, list):
        raise ValueError("usable coverage must be a JSON array")
    return set(_coverage_values("usable", values))


def covered_episodes(db: sqlite3.Connection, monitor_id: int) -> set[int]:
    """Return usable coverage from downloaded, inventory-verified assets only."""
    monitor_id = _positive_int("monitor_id", monitor_id)
    covered: set[int] = set()
    rows = db.execute(
        """SELECT status, usable_coverage_json, inventory_verified_at
           FROM media_assets
           WHERE monitor_id = ?
             AND status = 'downloaded'
             AND inventory_verified_at IS NOT NULL""",
        (monitor_id,),
    )
    for row in rows:
        covered.update(
            verified_asset_coverage(
                {"status": row[0], "usable_coverage_json": row[1], "inventory_verified_at": row[2]}
            )
        )
    return covered
