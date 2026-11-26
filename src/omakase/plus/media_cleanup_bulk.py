"""Proof-backed Watched-media bulk cleanup for personal Movies and TV Shows."""

from __future__ import annotations

import json
import re
from typing import Literal

from omakase.plus import movies_store
from omakase.plus.debrid_catalog import (
    CatalogTitle,
    _alternates,
    _matches,
    _movie_matches_parts,
    _title_prefixes,
    _tokens,
)
from omakase.plus.debrid_library import (
    DebridLibraryError,
    account_binding,
    account_token,
    catalog_library,
)
from omakase.plus.household_media import personal_legacy_visible
from omakase.plus.realdebrid import RealDebridClient

_IMDB = re.compile(r"tt[0-9]{7,10}\Z")


class MediaCleanupBulkError(ValueError):
    """Safe validation or ownership failure during bulk cleanup."""


def _personal_guard(db, scope, *, domain: Literal["movie", "tv"], canonical_id: str) -> None:
    if scope.kind != "personal" or scope.viewer_id != scope.operational_owner_id:
        raise MediaCleanupBulkError("Cleanup is only available from your personal library.")
    if not personal_legacy_visible(db, scope, domain=domain, canonical_id=canonical_id):
        raise MediaCleanupBulkError("Shared library titles cannot be removed from here.")


def _normalize_ids(kind: Literal["movie", "tv"], raw_ids: list[str]) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_ids:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        if kind == "movie":
            if not _IMDB.fullmatch(value):
                raise MediaCleanupBulkError("Choose valid movies to clean up.")
        else:
            if not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value) > 2**31 - 1:
                raise MediaCleanupBulkError("Choose valid TV shows to clean up.")
        seen.add(value)
        cleaned.append(value)
    if not cleaned:
        raise MediaCleanupBulkError("Select at least one watched title to clean up.")
    if len(cleaned) > 50:
        raise MediaCleanupBulkError("Select at most 50 titles at once.")
    return cleaned


def _format_bytes(size: int) -> str:
    if size <= 0:
        return ""
    units = ("B", "KB", "MB", "GB", "TB")
    val = float(size)
    idx = 0
    while val >= 1024 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    return f"{val:.1f} {units[idx]}" if idx >= 2 else f"{int(val)} {units[idx]}"


def _match_movie_torrents(torrents: dict, title: CatalogTitle) -> list[dict]:
    prefixes = _title_prefixes(title)
    matches = []
    for torrent_id, data in torrents.items():
        if not isinstance(data, dict) or data.get("status") != "downloaded":
            continue
        filename = str(data.get("filename") or "")
        parts = _tokens(filename)
        if not parts or not prefixes:
            continue
        first_matches = any(parts[: len(p)] == p for p in prefixes) or (
            title.year
            and len(parts) > 1
            and parts[0] == str(title.year)
            and any(parts[1 : 1 + len(p)] == p for p in prefixes)
        )
        if first_matches and _movie_matches_parts(parts, title):
            size = int(data.get("bytes") or 0)
            matches.append(
                {
                    "id": str(torrent_id),
                    "filename": filename,
                    "bytes": size,
                    "size_label": _format_bytes(size),
                }
            )
    return matches


def _match_tv_torrents(
    torrents: dict, title_str: str, tmdb_id: str, year: int | None, seasons: list
) -> list[dict]:
    season_nums = [
        int(row["season_number"])
        for row in seasons
        if isinstance(row, dict)
        and type(row.get("season_number")) is int
        and row["season_number"] >= 0
    ]
    if not season_nums:
        season_nums = list(range(1, 10))
    targets = [CatalogTitle("tv", tmdb_id, title_str, year, s) for s in season_nums]
    matches = []
    seen_ids: set[str] = set()
    for torrent_id, data in torrents.items():
        tid = str(torrent_id)
        if tid in seen_ids or not isinstance(data, dict) or data.get("status") != "downloaded":
            continue
        filename = str(data.get("filename") or "")
        if any(_matches(filename, target) for target in targets):
            seen_ids.add(tid)
            size = int(data.get("bytes") or 0)
            matches.append(
                {
                    "id": tid,
                    "filename": filename,
                    "bytes": size,
                    "size_label": _format_bytes(size),
                }
            )
    return matches


async def preview_movie_cleanup(
    db,
    scope,
    raw_ids: list[str],
    *,
    cloud_playback: bool,
    library=None,
) -> dict:
    lib = catalog_library if library is None else library
    imdb_ids = _normalize_ids("movie", raw_ids)
    torrents: dict = {}
    if cloud_playback:
        token = account_token(db, scope.viewer_id)
        if not token:
            raise MediaCleanupBulkError(
                "Connect Real-Debrid in Settings to review removable copies."
            )
        try:
            torrents = await lib.fresh_torrents(token)
        except DebridLibraryError as exc:
            raise MediaCleanupBulkError(str(exc)) from exc

    eligible = []
    blocked = []
    for imdb_id in imdb_ids:
        try:
            _personal_guard(db, scope, domain="movie", canonical_id=imdb_id)
        except MediaCleanupBulkError as exc:
            blocked.append({"id": imdb_id, "title": imdb_id, "reason": str(exc)})
            continue
        row = db.execute(
            """SELECT c.imdb_id, c.title, c.original_title, c.year, h.status
               FROM movie_catalog c
               JOIN movie_history h ON h.user_id=c.user_id AND h.imdb_id=c.imdb_id
               WHERE c.user_id=? AND c.imdb_id=?""",
            (scope.viewer_id, imdb_id),
        ).fetchone()
        if row is None or row["status"] != "completed":
            blocked.append(
                {
                    "id": imdb_id,
                    "title": row["title"] if row else imdb_id,
                    "reason": "Mark this movie as Watched before cleaning up its copy.",
                }
            )
            continue
        if cloud_playback:
            catalog_title = CatalogTitle(
                "movie",
                imdb_id,
                row["title"],
                row["year"],
                alternate_titles=_alternates(row["title"], row["original_title"]),
            )
            files = _match_movie_torrents(torrents, catalog_title) if row["year"] else []
            if not files:
                blocked.append(
                    {
                        "id": imdb_id,
                        "title": row["title"],
                        "reason": "No matching Real-Debrid copy found to remove.",
                    }
                )
                continue
            eligible.append(
                {
                    "id": imdb_id,
                    "title": row["title"],
                    "year": row["year"],
                    "files": files,
                    "detail_href": f"/plus/movies/{imdb_id}",
                }
            )
        else:
            eligible.append(
                {
                    "id": imdb_id,
                    "title": row["title"],
                    "year": row["year"],
                    "files": [],
                    "detail_href": f"/plus/movies/{imdb_id}#movie-after-watch",
                }
            )

    return {
        "kind": "movie",
        "kind_label": "movies",
        "back_href": "/plus/movies?view=completed",
        "back_label": "Back to watched movies",
        "execute_action": "/plus/movies/completed/cleanup/execute",
        "cloud_playback": cloud_playback,
        "eligible": eligible,
        "eligible_ids": [item["id"] for item in eligible],
        "blocked": blocked,
    }


async def preview_tv_cleanup(
    db,
    scope,
    raw_ids: list[str],
    *,
    cloud_playback: bool,
    library=None,
) -> dict:
    lib = catalog_library if library is None else library
    tmdb_ids = _normalize_ids("tv", raw_ids)
    torrents: dict = {}
    if cloud_playback:
        token = account_token(db, scope.viewer_id)
        if not token:
            raise MediaCleanupBulkError(
                "Connect Real-Debrid in Settings to review removable copies."
            )
        try:
            torrents = await lib.fresh_torrents(token)
        except DebridLibraryError as exc:
            raise MediaCleanupBulkError(str(exc)) from exc

    eligible = []
    blocked = []
    for tmdb_str in tmdb_ids:
        tmdb_id = int(tmdb_str)
        try:
            _personal_guard(db, scope, domain="tv", canonical_id=tmdb_str)
        except MediaCleanupBulkError as exc:
            blocked.append({"id": tmdb_str, "title": tmdb_str, "reason": str(exc)})
            continue
        row = db.execute(
            """SELECT c.tmdb_id, c.metadata_json, h.status
               FROM tv_catalog c
               JOIN tv_history h ON h.user_id=c.user_id AND h.tmdb_id=c.tmdb_id
               WHERE c.user_id=? AND c.tmdb_id=?""",
            (scope.viewer_id, tmdb_id),
        ).fetchone()
        if row is None or row["status"] != "completed":
            blocked.append(
                {
                    "id": tmdb_str,
                    "title": tmdb_str,
                    "reason": "Mark this TV show as Watched before cleaning up its copy.",
                }
            )
            continue
        try:
            meta = json.loads(row["metadata_json"])
        except (ValueError, TypeError):
            meta = {}
        title_str = str(meta.get("title") or meta.get("name") or tmdb_str)
        year = meta.get("year") if type(meta.get("year")) is int else None
        seasons = meta.get("seasons") if isinstance(meta.get("seasons"), list) else []
        if cloud_playback:
            files = _match_tv_torrents(torrents, title_str, tmdb_str, year, seasons)
            if not files:
                blocked.append(
                    {
                        "id": tmdb_str,
                        "title": title_str,
                        "reason": "No matching Real-Debrid copy found to remove.",
                    }
                )
                continue
            eligible.append(
                {
                    "id": tmdb_str,
                    "title": title_str,
                    "year": year,
                    "files": files,
                    "detail_href": f"/plus/tv/{tmdb_id}",
                }
            )
        else:
            eligible.append(
                {
                    "id": tmdb_str,
                    "title": title_str,
                    "year": year,
                    "files": [],
                    "detail_href": f"/plus/tv/{tmdb_id}",
                }
            )

    return {
        "kind": "tv",
        "kind_label": "TV shows",
        "back_href": "/plus/tv?view=completed",
        "back_label": "Back to watched TV shows",
        "execute_action": "/plus/tv/completed/cleanup/execute",
        "cloud_playback": cloud_playback,
        "eligible": eligible,
        "eligible_ids": [item["id"] for item in eligible],
        "blocked": blocked,
    }


async def execute_cloud_cleanup(
    db,
    scope,
    *,
    kind: Literal["movie", "tv"],
    raw_ids: list[str],
    now: str,
    library=None,
    client_factory=None,
) -> dict:
    lib = catalog_library if library is None else library
    factory = RealDebridClient if client_factory is None else client_factory
    preview = (
        await preview_movie_cleanup(db, scope, raw_ids, cloud_playback=True, library=lib)
        if kind == "movie"
        else await preview_tv_cleanup(db, scope, raw_ids, cloud_playback=True, library=lib)
    )
    if not preview["eligible"]:
        raise MediaCleanupBulkError("No eligible Real-Debrid copies were found to remove.")
    token = account_token(db, scope.viewer_id)
    if not token:
        raise MediaCleanupBulkError("Connect Real-Debrid in Settings to remove copies.")
    rd = factory(token)
    removed_titles = 0
    removed_torrents = 0
    failed = 0
    for item in preview["eligible"]:
        item_deleted = 0
        for file_entry in item["files"]:
            try:
                ok = await rd.delete_torrent(file_entry["id"], require_exact_id=True)
            except Exception:
                ok = False
            if ok:
                removed_torrents += 1
                item_deleted += 1
            else:
                failed += 1
        if item_deleted > 0:
            removed_titles += 1
            if kind == "movie":
                existing_req = movies_store.get_request(db, scope.viewer_id, item["id"])
                if existing_req:
                    movies_store.update_request(
                        db,
                        scope.viewer_id,
                        item["id"],
                        now=now,
                        state="removed",
                        code="copy_removed",
                        detail=movies_store.COPY_REMOVED_DETAIL,
                    )
    binding = account_binding(token)
    lib.cache.pop(binding, None)
    lib.last_good.pop(binding, None)
    return {
        "removed_titles": removed_titles,
        "removed_torrents": removed_torrents,
        "failed": failed,
    }
