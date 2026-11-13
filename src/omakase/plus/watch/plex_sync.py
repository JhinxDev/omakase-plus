"""Strict Plex library snapshots and exact watch-progress ingestion."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from omakase.plus.identity import normalize_release_identity
from omakase.plus.watch.models import WatchEvent
from omakase.plus.watch.service import ingest_watch_event

PLEX_SNAPSHOT_SCHEMA = "plex-library-snapshot-v1"
MAX_PLEX_SNAPSHOT_BYTES = 512_000
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ITEM_FIELDS = {
    "rating_key",
    "section",
    "media_type",
    "title",
    "season_number",
    "episode_total",
    "watched_through",
    "last_viewed_at",
}


class PlexSnapshotError(ValueError):
    """A Plex inventory snapshot is malformed or cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class PlexSnapshotItem:
    rating_key: str
    section: str
    media_type: str
    title: str
    season_number: int
    episode_total: int
    watched_through: int
    last_viewed_at: str | None
    canonical_title: str | None = None
    managed_paths: tuple[tuple[str, int], ...] | None = None
    path_identity_complete: bool | None = None


def _timestamp(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise PlexSnapshotError(f"{name} is invalid")
    try:
        datetime.strptime(value, _UTC_FORMAT)
    except ValueError as exc:
        raise PlexSnapshotError(f"{name} is invalid") from exc
    return value


def _text(value: object, *, name: str, limit: int) -> str:
    if not isinstance(value, str):
        raise PlexSnapshotError(f"{name} is invalid")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > limit or any(ord(char) < 32 for char in normalized):
        raise PlexSnapshotError(f"{name} is invalid")
    return normalized


def _integer(value: object, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise PlexSnapshotError(f"{name} is invalid")
    return value


def _validate_item(value: object) -> PlexSnapshotItem:
    optional = {"canonical_title", "managed_paths", "path_identity_complete"}
    if (
        not isinstance(value, dict)
        or not _ITEM_FIELDS <= set(value)
        or set(value) - _ITEM_FIELDS - optional
    ):
        raise PlexSnapshotError("Plex snapshot item has an unknown shape")
    refs = None
    complete = None
    if ("managed_paths" in value) != ("path_identity_complete" in value):
        raise PlexSnapshotError("Plex path identity evidence is incomplete")
    if "managed_paths" in value:
        raw_refs = value["managed_paths"]
        complete = value["path_identity_complete"]
        if not isinstance(raw_refs, list) or len(raw_refs) > 100 or type(complete) is not bool:
            raise PlexSnapshotError("Plex path identity evidence is invalid")
        parsed = []
        for ref in raw_refs:
            if not isinstance(ref, dict) or set(ref) != {"canonical_title", "season_number"}:
                raise PlexSnapshotError("Plex managed path reference is invalid")
            parsed.append(
                (
                    _text(ref["canonical_title"], name="managed title", limit=300),
                    _integer(ref["season_number"], name="managed season", minimum=0, maximum=99),
                )
            )
        refs = tuple(sorted(parsed))
        if len(refs) != len(set(refs)) or ("canonical_title" in value and not complete):
            raise PlexSnapshotError("Plex managed path references are contradictory")
    rating_key = value["rating_key"]
    if not isinstance(rating_key, str) or not re.fullmatch(r"[1-9][0-9]{0,31}", rating_key):
        raise PlexSnapshotError("Plex rating key is invalid")
    media_type = value["media_type"]
    if media_type not in {"show", "movie"}:
        raise PlexSnapshotError("Plex media type is invalid")
    season_number = _integer(
        value["season_number"], name="Plex season number", minimum=0, maximum=99
    )
    if media_type == "movie" and season_number != 0:
        raise PlexSnapshotError("Plex movies must use season zero")
    if media_type == "show" and season_number == 0:
        raise PlexSnapshotError("Plex shows must identify a season")
    episode_total = _integer(
        value["episode_total"], name="Plex episode total", minimum=1, maximum=5000
    )
    watched_through = _integer(
        value["watched_through"], name="Plex progress", minimum=0, maximum=episode_total
    )
    last_viewed_at = value["last_viewed_at"]
    if last_viewed_at is not None:
        last_viewed_at = _timestamp(last_viewed_at, name="Plex last-viewed timestamp")
    if watched_through > 0 and last_viewed_at is None:
        raise PlexSnapshotError("watched Plex media requires a last-viewed timestamp")
    canonical_title = (
        _text(value["canonical_title"], name="managed canonical title", limit=300)
        if "canonical_title" in value
        else None
    )
    if (
        canonical_title is not None
        and refs is not None
        and refs != ((canonical_title, season_number),)
    ):
        raise PlexSnapshotError("Plex canonical title contradicts managed path evidence")
    return PlexSnapshotItem(
        rating_key=rating_key,
        section=_text(value["section"], name="Plex section", limit=80),
        media_type=media_type,
        title=_text(value["title"], name="Plex title", limit=300),
        season_number=season_number,
        episode_total=episode_total,
        watched_through=watched_through,
        last_viewed_at=last_viewed_at,
        canonical_title=canonical_title,
        managed_paths=refs,
        path_identity_complete=complete,
    )


def validate_plex_library_snapshot(payload: object) -> tuple[str, tuple[PlexSnapshotItem, ...]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "generated_at",
        "items",
    }:
        raise PlexSnapshotError("Plex snapshot has an unknown shape")
    if payload["schema_version"] != PLEX_SNAPSHOT_SCHEMA:
        raise PlexSnapshotError("Plex snapshot schema is unsupported")
    generated_at = _timestamp(payload["generated_at"], name="Plex snapshot timestamp")
    raw_items = payload["items"]
    if not isinstance(raw_items, list) or len(raw_items) > 1000:
        raise PlexSnapshotError("Plex snapshot items are invalid")
    items = tuple(_validate_item(value) for value in raw_items)
    identities = [(item.rating_key, item.season_number) for item in items]
    if len(identities) != len(set(identities)):
        raise PlexSnapshotError("Plex snapshot items contain duplicate identities")
    return generated_at, items


def _base_title(value: str, season_number: int) -> str:
    normalized = normalize_release_identity(value)
    if season_number > 0:
        normalized = re.sub(rf"\bseason\s+{season_number}\b", " ", normalized)
        normalized = re.sub(rf"\b{season_number}\b$", " ", normalized)
    return " ".join(normalized.split())


def _monitor_matches(item: PlexSnapshotItem, monitor: sqlite3.Row) -> bool:
    monitor_season = int(monitor["season_number"] or 0)
    if monitor_season != item.season_number:
        return False
    if item.canonical_title is not None:
        # This full identity came from one exact managed folder and season.
        # A renamed display title cannot override contradictory path evidence.
        return item.canonical_title == monitor["title"]
    if item.path_identity_complete is not None:
        return False
    try:
        aliases = json.loads(monitor["search_titles_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        aliases = []
    if not isinstance(aliases, list):
        aliases = []
    plex_title = normalize_release_identity(item.title)
    plex_base = _base_title(item.title, item.season_number)
    for candidate in (monitor["title"], *aliases):
        if not isinstance(candidate, str):
            continue
        normalized = normalize_release_identity(candidate)
        if normalized == plex_title or _base_title(candidate, monitor_season) == plex_base:
            return True
    return False


def _canonical_payload_hash(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PLEX_SNAPSHOT_BYTES:
        raise PlexSnapshotError("Plex snapshot body is too large")
    return hashlib.sha256(encoded).hexdigest()


def store_plex_library_snapshot(
    db: sqlite3.Connection,
    payload: object,
    *,
    received_at: str,
) -> dict[str, int | bool]:
    """Store one immutable inventory and advance exact matched progress idempotently."""
    received_at = _timestamp(received_at, name="Plex snapshot receipt timestamp")
    generated_at, items = validate_plex_library_snapshot(payload)
    payload_hash = _canonical_payload_hash(payload)
    title_count = len({item.rating_key for item in items})
    prior = db.execute(
        "SELECT id FROM plex_library_snapshots WHERE payload_hash = ?", (payload_hash,)
    ).fetchone()
    if prior is not None:
        return {
            "stored": False,
            "item_count": len(items),
            "title_count": title_count,
            "matched_monitors": 0,
            "progress_updates": 0,
        }

    nested = db.in_transaction
    db.execute("SAVEPOINT plex_snapshot" if nested else "BEGIN IMMEDIATE")
    try:
        cursor = db.execute(
            """INSERT INTO plex_library_snapshots
               (schema_version, generated_at, received_at, payload_hash, item_count, title_count)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                PLEX_SNAPSHOT_SCHEMA,
                generated_at,
                received_at,
                payload_hash,
                len(items),
                title_count,
            ),
        )
        snapshot_id = int(cursor.lastrowid)
        for item in items:
            db.execute(
                """INSERT INTO plex_library_items
                   (snapshot_id, rating_key, section, media_type, title, season_number,
                    episode_total, watched_through, last_viewed_at, canonical_title,
                    managed_paths_json, path_identity_complete)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot_id,
                    item.rating_key,
                    item.section,
                    item.media_type,
                    item.title,
                    item.season_number,
                    item.episode_total,
                    item.watched_through,
                    item.last_viewed_at,
                    item.canonical_title,
                    json.dumps(item.managed_paths) if item.managed_paths is not None else None,
                    int(item.path_identity_complete)
                    if item.path_identity_complete is not None
                    else None,
                ),
            )

        monitors = db.execute(
            """SELECT id, user_id, title, search_titles_json, season_number
               FROM media_monitors"""
        ).fetchall()
        matched: set[int] = set()
        progress_updates = 0
        for item in items:
            candidates = [monitor for monitor in monitors if _monitor_matches(item, monitor)]
            for monitor in candidates:
                monitor_id = int(monitor["id"])
                matched.add(monitor_id)
                if item.watched_through <= 0:
                    continue
                event_key = (
                    f"plex:snapshot:{item.rating_key}:s{item.season_number}:"
                    f"e{item.watched_through}:{item.last_viewed_at}"
                )
                event = WatchEvent.progress(
                    source="plex",
                    episode=item.watched_through,
                    event_key=event_key,
                )
                result = ingest_watch_event(
                    db,
                    user_id=int(monitor["user_id"]),
                    monitor_id=monitor_id,
                    event=event,
                    observed_at=generated_at,
                )
                if result.disposition == "accepted":
                    progress_updates += 1

        # Keep a bounded audit trail while the latest inventory remains queryable.
        stale_ids = [
            int(row["id"])
            for row in db.execute(
                """SELECT id FROM plex_library_snapshots
                   ORDER BY generated_at DESC, id DESC LIMIT -1 OFFSET 10"""
            ).fetchall()
        ]
        if stale_ids:
            db.executemany(
                "DELETE FROM plex_library_snapshots WHERE id = ?",
                ((snapshot_id,) for snapshot_id in stale_ids),
            )
        if nested:
            db.execute("RELEASE SAVEPOINT plex_snapshot")
        else:
            db.commit()
        return {
            "stored": True,
            "item_count": len(items),
            "title_count": title_count,
            "matched_monitors": len(matched),
            "progress_updates": progress_updates,
        }
    except Exception:
        if nested:
            db.execute("ROLLBACK TO SAVEPOINT plex_snapshot")
            db.execute("RELEASE SAVEPOINT plex_snapshot")
        else:
            db.rollback()
        raise


def latest_plex_library_summary(db: sqlite3.Connection) -> dict[str, object]:
    row = db.execute(
        """SELECT generated_at, title_count, item_count
           FROM plex_library_snapshots
           ORDER BY generated_at DESC, id DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        return {"connected": False, "generated_at": "", "title_count": 0, "item_count": 0}
    return {
        "connected": True,
        "generated_at": str(row["generated_at"]),
        "title_count": int(row["title_count"]),
        "item_count": int(row["item_count"]),
    }
