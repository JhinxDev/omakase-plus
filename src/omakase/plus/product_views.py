"""Consumer-first, secret-free read models for Omakase Plus pages."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from omakase.plus.anime_synopsis import cached_synopsis
from omakase.plus.catalog_config import configured_catalogs
from omakase.plus.health import reason_label
from omakase.plus.manifest import validate_curator_receipt
from omakase.plus.media_store import covered_episodes, verified_asset_coverage

_ANILIST_ID_RE = re.compile(r"https://anilist\.co/anime/(\d+)/")
_LANES = frozenset({"best_match", "new_seasons", "hidden_gems", "plan_list"})
_ATTENTION_ASSET_STATES = frozenset({"error", "quarantined", "stalled"})
_ACTIVE_ASSET_STATES = frozenset({"queued", "downloading", "requested"})
_HANDLED_RECOMMENDATIONS = frozenset({"not_for_me", "wrong_sequel", "already_watched"})
_HARDSUB_RE = re.compile(r"\b(?:hard[\s._-]*subs?|hardsubbed)\b", re.I)
_MAX_DETAIL_CHARS = 240
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([a-z0-9_-]*(?:token|key|secret|password|credential)[a-z0-9_-]*)"
    r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]+=*")
_MAGNET_RE = re.compile(r"(?i)magnet:\?[^\s\"']+")
_LONG_IDENTIFIER_RE = re.compile(r"(?i)\b[0-9a-f]{32,}\b")
_SENSITIVE_QUERY_KEYS = frozenset(
    {"api_key", "apikey", "auth", "key", "password", "secret", "token", "x-plex-token"}
)


class ProductViewError(ValueError):
    """A product view request is invalid or not owned by the user."""


def clean_detail(value: object) -> str:
    detail = re.sub(r"\s+", " ", str(value or "")).strip()
    detail = _MAGNET_RE.sub("<magnet:redacted>", detail)
    detail = _BEARER_RE.sub("Bearer <redacted>", detail)
    detail = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=<redacted>", detail)
    detail = _LONG_IDENTIFIER_RE.sub("<identifier:redacted>", detail)
    detail = _redact_url_queries(detail)
    if len(detail) > _MAX_DETAIL_CHARS:
        return detail[: _MAX_DETAIL_CHARS - 3].rstrip() + "..."
    return detail


def normalize_lane(value: str | None) -> str:
    return value if isinstance(value, str) and value in _LANES else "best_match"


def dashboard_view(db, *, user_id: int, run: str = "") -> dict[str, Any]:
    """Build the legacy dashboard context without HTTP or template concerns."""
    profile = db.execute(
        "SELECT content, updated_at FROM taste_profiles WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    recent_rows = db.execute(
        """SELECT id, source, model, picks, lane, created_at
           FROM run_history WHERE user_id = ?
           ORDER BY id DESC LIMIT 10""",
        (user_id,),
    ).fetchall()
    runs = [
        {
            "id": row["id"],
            "source": row["source"],
            "model": row["model"],
            "lane": row["lane"],
            "picks": _json_list(row["picks"]),
            "created_at": row["created_at"],
        }
        for row in recent_rows
    ]

    current_run_picks: list[dict] = []
    current_run_id = 0
    current_run_source = ""
    current_run_lane = "best_match"
    if run:
        try:
            run_id = int(run)
        except (TypeError, ValueError):
            run_id = 0
        if run_id > 0:
            row = db.execute(
                """SELECT id, source, model, picks, lane
                   FROM run_history WHERE id = ? AND user_id = ?""",
                (run_id, user_id),
            ).fetchone()
            if row is not None:
                current_run_id = int(row["id"])
                current_run_source = str(row["source"])
                current_run_lane = normalize_lane(row["lane"])
                for raw in _json_list(row["picks"]):
                    if not isinstance(raw, dict):
                        continue
                    pick = dict(raw)
                    anilist_id = recommendation_anilist_id(pick, current_run_source)
                    pick["anilist_id"] = anilist_id
                    pick["media_id"] = _positive_int(pick.get("media_id")) or anilist_id
                    current_run_picks.append(pick)

    planning_rows = db.execute(
        """SELECT id, anilist_id, title, added_at, status AS planning_status,
                  download_status, download_info, rd_torrent_id
           FROM anilist_plannings
           WHERE user_id = ?
           ORDER BY added_at DESC
           LIMIT 50""",
        (user_id,),
    ).fetchall()
    plannings = [
        {
            "id": int(row["id"]),
            "anilist_id": int(row["anilist_id"]),
            "title": row["title"],
            "added_at": row["added_at"],
            "planning_status": row["planning_status"],
            "download_status": row["download_status"] or "",
            "download_info": row["download_info"] or "",
            "rd_torrent_id": row["rd_torrent_id"] or "",
            "download_attempts": [],
        }
        for row in planning_rows
    ]
    planned_ids = {item["anilist_id"] for item in plannings}
    _attach_download_attempts(db, user_id=user_id, plannings=plannings)
    monitor_jobs = library_jobs(db, user_id)
    summary = {
        "queue_total": len(plannings),
        "queue_active": sum(item["download_status"] == "requested" for item in plannings),
        "queue_attention": sum(
            item["download_status"] in {"not_found", "error", "rd_provider_block", "no_rd_key"}
            for item in plannings
        ),
        "runs_total": len(runs),
        "profile_saved": bool(profile and str(profile["content"]).strip()),
        "monitor_total": len(monitor_jobs),
        "monitor_active": sum(item["active"] for item in monitor_jobs),
    }
    return {
        "taste_profile": profile["content"] if profile else "",
        "taste_profile_updated": profile["updated_at"] if profile else "",
        "runs": runs,
        "current_run_picks": current_run_picks,
        "current_run_id": current_run_id,
        "current_run_source": current_run_source,
        "current_run_lane": current_run_lane,
        "plannings": plannings,
        "planned_ids": planned_ids,
        "dashboard_summary": summary,
        "monitor_jobs": monitor_jobs,
    }


def home_view(db, *, user_id: int, library_scope=None) -> dict[str, Any]:
    titles = library_titles(db, user_id=user_id, library_scope=library_scope)
    states = {state: sum(item["state"] == state for item in titles) for state in _states()}
    attention = [item for item in titles if item["state"] == "attention"][:5]
    continue_watching = [item for item in titles if item["lifecycle"] == "in_progress"][:10]
    confirmation_needed = [item for item in titles if item["has_pending_confirmation"]][:5]
    ready = [item for item in titles if item["lifecycle"] == "ready_to_start"][:10]
    new_episodes = [
        item for item in titles if item["new_episode_ready"] and item["lifecycle"] == "in_progress"
    ][:10]
    recent = activity_view(db, user_id=user_id, limit=6)["events"]
    return {
        "summary": {
            "titles": len(titles),
            **states,
        },
        "continue_watching": continue_watching,
        "confirmation_needed": confirmation_needed,
        "attention": attention,
        "ready": ready,
        "ready_to_watch": ready,
        "new_episodes": new_episodes,
        "recent_activity": recent,
    }


def library_titles(
    db,
    *,
    user_id: int,
    lifecycle: str = "active",
    state: str = "all",
    provider: str = "all",
    content_type: str = "all",
    query: str = "",
    include_removed: bool = False,
    library_scope=None,
) -> list[dict[str, Any]]:
    """Return stable per-title readiness cards with explicit filters."""
    lifecycle_values = {
        "active",
        "in_progress",
        "ready_to_start",
        "waiting",
        "completed",
        "all",
    }
    if lifecycle not in lifecycle_values:
        raise ProductViewError("library lifecycle filter is unsupported")
    if state not in {"all", *_states()}:
        raise ProductViewError("library state filter is unsupported")
    if provider not in {"all", "realdebrid", "local_import", "qbittorrent"}:
        raise ProductViewError("provider filter is unsupported")
    if content_type not in {"all", "series", "movies"}:
        raise ProductViewError("content type filter is unsupported")
    text = query.strip().casefold()
    if len(text) > 100:
        raise ProductViewError("library query is too long")
    if library_scope is not None:
        from omakase.plus import households
        from omakase.plus.household_views import anime_cards

        households._require(library_scope.viewer_id == user_id, "Library viewer mismatch.", 403)
        households.revalidate_scope(db, library_scope)
        if library_scope.kind == "household":
            cards = anime_cards(
                db, library_scope, viewer_id=user_id, query=query, lifecycle=lifecycle
            )
            return [
                card
                for card in cards
                if (state == "all" or card["state"] == state)
                and provider == "all"
                and content_type != "movies"
            ]
    visible = plex_visible_counts(db)
    watch_rows = db.execute(
        """SELECT entry.*,
                  SUM(CASE WHEN confirmation.status = 'pending' THEN 1 ELSE 0 END)
                      AS pending_confirmation_count
           FROM watch_entries AS entry
           LEFT JOIN watch_confirmations AS confirmation
             ON confirmation.watch_entry_id = entry.id
           WHERE entry.user_id = ?
           GROUP BY entry.id""",
        (user_id,),
    ).fetchall()
    watch_by_monitor = {
        int(row["monitor_id"]): row for row in watch_rows if row["monitor_id"] is not None
    }
    unlinked_watch = {
        (int(row["anilist_id"]), int(row["season_number"])): row
        for row in watch_rows
        if row["monitor_id"] is None
    }
    monitors = db.execute(
        """SELECT * FROM media_monitors
           WHERE user_id = ?
           ORDER BY active DESC, title COLLATE NOCASE, id""",
        (user_id,),
    ).fetchall()
    titles: list[dict[str, Any]] = []
    for monitor in monitors:
        if text and text not in str(monitor["title"]).casefold():
            continue
        is_movie = str(monitor["format"]) == "MOVIE"
        if content_type == "movies" and not is_movie:
            continue
        if content_type == "series" and is_movie:
            continue
        assets = db.execute(
            """SELECT asset.*, COALESCE(provider.kind, 'realdebrid') AS provider_kind,
                      COALESCE(provider.label, 'Real-Debrid') AS provider_label
               FROM media_assets AS asset
               LEFT JOIN media_providers AS provider ON provider.id = asset.provider_id
               WHERE asset.monitor_id = ?
               ORDER BY CASE asset.status
                   WHEN 'downloaded' THEN 0
                   WHEN 'downloading' THEN 1
                   WHEN 'queued' THEN 2
                   WHEN 'quarantined' THEN 3
                   ELSE 4 END,
                   asset.id DESC""",
            (monitor["id"],),
        ).fetchall()
        provider_kinds = {str(asset["provider_kind"]) for asset in assets}
        if provider != "all" and provider not in provider_kinds:
            continue
        required = _required_count(monitor)
        coverage = covered_episodes(db, int(monitor["id"]))
        usable = len(coverage & set(range(1, required + 1))) if required else len(coverage)
        quality_coverage: dict[str, set[int]] = {}
        for asset in assets:
            episodes = verified_asset_coverage(asset)
            if required:
                episodes &= set(range(1, required + 1))
            quality = str(asset["quality"] or "").lower()
            if episodes and quality in {"1080p", "2160p", "720p"}:
                quality_coverage.setdefault(quality, set()).update(episodes)
        qualities = [
            quality for quality in ("1080p", "2160p", "720p") if quality in quality_coverage
        ]
        fallback_episodes = sorted(quality_coverage.get("720p", set()))
        plex = visible.get((int(monitor["anilist_id"]), monitor["season_number"]))
        plex_visible = int(plex[1]) if plex else 0
        curator_usable = int(plex[0]) if plex else 0
        publication_pending = bool(
            required > 0
            and usable >= required
            and curator_usable >= required
            and plex_visible < required
        )
        asset_states = {str(asset["status"]) for asset in assets}
        readiness = _readiness_state(
            required=required,
            usable=usable,
            plex_visible=plex_visible,
            asset_states=asset_states,
            failures=int(monitor["consecutive_failures"]),
        )
        status_label = _readiness_label(
            state=readiness,
            required=required,
            usable=usable,
            plex_visible=plex_visible,
        )
        if publication_pending:
            readiness = "acquiring"
            status_label = f"Publishing to Plex · {plex_visible} of {required} visible"
        removal_row = db.execute(
            """SELECT status, detail, scope
               FROM media_title_removals
               WHERE monitor_id = ?
               ORDER BY id DESC LIMIT 1""",
            (monitor["id"],),
        ).fetchone()
        removal_status = str(removal_row["status"]) if removal_row is not None else ""
        # Full media cleanup retains its existing Activity-only behavior. Plex-only
        # withdrawals remain in Completed so watched history and ratings are usable.
        if (
            removal_status == "complete"
            and removal_row["scope"] != "plex_only"
            and not include_removed
        ):
            continue
        if removal_status:
            publication_pending = False
            readiness = "attention" if removal_status == "provider_failed" else "acquiring"
            status_label = {
                "awaiting_plex": "Removing from Plex",
                "plex_removed": "Removing provider media",
                "provider_failed": "Provider cleanup will retry",
                "complete": "Removed from Plex",
            }[removal_status]
            if removal_status == "complete":
                readiness = "archived"
                status_label = (
                    "Removed from Plex" if removal_row["scope"] == "plex_only" else "Media removed"
                )
                plex_visible = usable = 0
                qualities = []
                fallback_episodes = []
        if state != "all" and state != readiness:
            continue
        watch = watch_by_monitor.get(int(monitor["id"]))
        if watch is None:
            watch = unlinked_watch.get(
                (int(monitor["anilist_id"]), int(monitor["season_number"] or 0))
            )
        watch_status = str(watch["status"]) if watch is not None else "unwatched"
        progress_episodes = int(watch["progress_episodes"]) if watch is not None else 0
        stored_total = (
            int(watch["episode_total"])
            if watch is not None and watch["episode_total"] is not None
            else None
        )
        monitor_total = int(monitor["expected_episodes"] or 0)
        episode_total = stored_total or (monitor_total if monitor_total > 0 else None)
        next_episode = None
        if watch_status != "completed" and (
            episode_total is None or progress_episodes < episode_total
        ):
            next_episode = progress_episodes + 1
        new_episode_ready = bool(next_episode is not None and plex_visible >= next_episode)
        if watch_status == "completed":
            title_lifecycle = "completed"
        elif progress_episodes > 0:
            title_lifecycle = "in_progress"
        elif new_episode_ready or readiness == "ready":
            title_lifecycle = "ready_to_start"
        else:
            title_lifecycle = "waiting"
        missing_media = max(0, required - usable)
        missing_plex = max(0, required - plex_visible)
        if title_lifecycle == "completed":
            lifecycle_label = "Completed"
        elif title_lifecycle == "in_progress":
            lifecycle_label = "In progress"
        elif title_lifecycle == "ready_to_start":
            lifecycle_label = "Ready to start"
        elif removal_status:
            lifecycle_label = status_label
        elif publication_pending:
            lifecycle_label = "Publishing to Plex"
        elif required <= 0:
            lifecycle_label = "Waiting to air"
        elif readiness == "acquiring":
            lifecycle_label = "Finding episodes"
        elif missing_media and not provider_kinds:
            lifecycle_label = "No source"
        elif missing_media:
            lifecycle_label = "Episodes missing"
        elif missing_plex:
            lifecycle_label = "Not in Plex"
        else:
            lifecycle_label = "Needs setup"
        if not removal_status:
            if new_episode_ready and readiness != "ready":
                status_label = f"Episode {next_episode} is ready"
            elif required <= 0:
                status_label = "No episodes have aired yet"
            elif missing_media and not provider_kinds:
                noun = "episode" if missing_media == 1 else "episodes"
                status_label = f"No media source found · {missing_media} {noun} missing"
        providers = sorted({str(asset["provider_label"]) for asset in assets})
        if providers:
            provider_summary = ", ".join(providers)
        elif plex_visible > 0:
            provider_summary = "Plex library"
        else:
            provider_summary = "No provider media found"
        if lifecycle == "active" and title_lifecycle == "completed":
            continue
        if lifecycle not in {"active", "all", title_lifecycle}:
            continue
        if watch_status == "completed":
            progress_percent = 100
        elif episode_total:
            progress_percent = min(100, round((progress_episodes / episode_total) * 100))
        else:
            progress_percent = 0
        last_source = str(watch["last_source"]) if watch is not None else ""
        last_source_label = {
            "plex": "Plex",
            "apple_tv": "Apple TV",
            "anilist": "AniList",
            "manual": "Manual",
            "migration": "Imported history",
        }.get(last_source, "Not tracked yet")
        publication_detail = ""
        if publication_pending:
            noun = "episode" if required == 1 else "episodes"
            publication_detail = (
                f"All {required} {noun} are verified and sent to the managed Plex library. "
                f"Plex is indexing them automatically; {plex_visible} of {required} "
                "are visible so far."
            )
        titles.append(
            {
                "id": int(monitor["id"]),
                "anilist_id": int(monitor["anilist_id"]),
                "title": monitor["title"],
                "format": monitor["format"],
                "season_number": monitor["season_number"],
                "release_status": monitor["release_status"] or "UNKNOWN",
                "release_status_label": release_status_label(monitor["release_status"]),
                "active": bool(monitor["active"]),
                "required": required,
                "release_episode_total": int(monitor["expected_episodes"] or 0),
                "display_episode_count": max(required, int(monitor["expected_episodes"] or 0)),
                "upcoming_episodes": list(
                    range(required + 1, int(monitor["expected_episodes"] or required) + 1)
                )
                if monitor["release_status"] == "RELEASING"
                and monitor["next_airing_at"]
                and int(monitor["next_airing_at"]) > datetime.now(UTC).timestamp()
                else [],
                "next_release_at": datetime.fromtimestamp(
                    int(monitor["next_airing_at"]), UTC
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                if monitor["next_airing_at"]
                else "",
                "usable": usable,
                "plex_visible": plex_visible,
                "state": readiness,
                "status_label": status_label,
                "provider_kinds": sorted(provider_kinds),
                "providers": providers,
                "provider_summary": provider_summary,
                "asset_count": len(assets),
                "release_qualities": qualities,
                "fallback_episodes": fallback_episodes,
                "last_result": clean_detail(
                    removal_row["detail"]
                    if removal_row is not None
                    else monitor["last_result"] or "No sync receipt yet"
                ),
                "next_check_at": monitor["next_check_at"] or "",
                "removal_status": removal_status,
                "watch_entry_id": int(watch["id"]) if watch is not None else None,
                "watch_status": watch_status,
                "lifecycle": title_lifecycle,
                "lifecycle_label": lifecycle_label,
                "progress_episodes": progress_episodes,
                "episode_total": episode_total,
                "next_episode": next_episode,
                "progress_percent": progress_percent,
                "last_source_label": last_source_label,
                "last_observed_at": (
                    str(watch["last_observed_at"] or "") if watch is not None else ""
                ),
                "has_pending_confirmation": bool(
                    watch is not None and int(watch["pending_confirmation_count"] or 0)
                ),
                "new_episode_ready": new_episode_ready,
                "publication_pending": publication_pending,
                "publication_detail": publication_detail,
            }
        )
    if library_scope is not None and library_scope.kind == "personal":
        # Migrated operational rows remain personal unless explicitly promoted.
        # Existing personal bindings intentionally retain a title in both scopes.
        titles = [
            item
            for item in titles
            if not db.execute(
                "SELECT 1 FROM library_resources r JOIN library_items i ON i.resource_id=r.id WHERE r.owner_user_id=? AND r.domain='anime' AND r.legacy_id=? AND i.state='active' AND i.library_id!=? AND NOT EXISTS(SELECT 1 FROM library_items own WHERE own.library_id=? AND own.state='active' AND own.resource_id=r.id) LIMIT 1",
                (user_id, str(item["id"]), library_scope.library_id, library_scope.library_id),
            ).fetchone()
        ]
    return titles


def title_detail(db, *, user_id: int, monitor_id: int) -> dict[str, Any] | None:
    """Return one owner-scoped title with assets, tracks, and evidence timeline."""
    monitor = db.execute(
        "SELECT * FROM media_monitors WHERE id = ? AND user_id = ?",
        (monitor_id, user_id),
    ).fetchone()
    if monitor is None:
        return None
    summary = next(
        (
            item
            for item in library_titles(db, user_id=user_id, lifecycle="all", include_removed=True)
            if item["id"] == monitor_id
        ),
        None,
    )
    if summary is None:
        return None
    asset_rows = db.execute(
        """SELECT asset.*, COALESCE(provider.kind, 'realdebrid') AS provider_kind,
                  COALESCE(provider.label, 'Real-Debrid') AS provider_label
           FROM media_assets AS asset
           LEFT JOIN media_providers AS provider ON provider.id = asset.provider_id
           WHERE asset.monitor_id = ?
           ORDER BY asset.id DESC""",
        (monitor_id,),
    ).fetchall()
    assets: list[dict[str, Any]] = []
    for asset in asset_rows:
        probe = db.execute(
            """SELECT * FROM media_file_probes
               WHERE asset_id = ?
               ORDER BY verified_at DESC, id DESC LIMIT 1""",
            (asset["id"],),
        ).fetchone()
        tracks = []
        if probe is not None:
            tracks = [
                {
                    "language": row["language"],
                    "codec": row["codec"],
                    "switchable": bool(row["switchable"]),
                    "default": bool(row["default_track"]),
                    "forced": bool(row["forced"]),
                    "source": row["source_kind"],
                    "sync_policy": row["sync_policy"],
                }
                for row in db.execute(
                    """SELECT * FROM media_subtitle_tracks
                       WHERE probe_id = ?
                       ORDER BY language, stream_index""",
                    (probe["id"],),
                ).fetchall()
            ]
        source_title = str(asset["source_title"] or "")
        english_switchable = any(
            track["language"] in {"eng", "en", "en-us", "en-gb"} and track["switchable"]
            for track in tracks
        )
        burned_in = bool(_HARDSUB_RE.search(source_title))
        if english_switchable:
            subtitle_state = "selectable"
            subtitle_label = "Selectable English subtitles"
            subtitle_detail = "English is a separate track you can switch on or off."
        elif burned_in:
            subtitle_state = "burned_in"
            subtitle_label = "English subtitles are built into the video"
            subtitle_detail = (
                "This release is watchable, but its subtitles cannot be switched, "
                "changed, or timed separately."
            )
        elif probe is None:
            subtitle_state = "automatic"
            subtitle_label = "Automatic verification"
            subtitle_detail = (
                "No action is required. Omakase checks subtitles and playback "
                "before this release appears in Plex."
            )
        else:
            subtitle_state = "missing"
            subtitle_label = "No separate English subtitle track"
            subtitle_detail = "Choose another release if selectable English subtitles are required."
        assets.append(
            {
                "id": int(asset["id"]),
                "provider_kind": asset["provider_kind"],
                "provider_label": asset["provider_label"],
                "quality": asset["quality"] or "unknown",
                "status": asset["status"],
                "status_label": _asset_status_label(asset["status"]),
                "progress": float(asset["progress"]),
                "planned_coverage": _json_ints(asset["planned_coverage_json"]),
                "usable_coverage": _json_ints(asset["usable_coverage_json"]),
                "storage_backend": asset["storage_backend"] or "zurg",
                "source": clean_detail(source_title),
                "last_error": clean_detail(asset["last_error"]),
                "last_error_label": _asset_error_label(asset["last_error"]),
                "subtitle_state": subtitle_state,
                "subtitle_label": subtitle_label,
                "subtitle_detail": subtitle_detail,
                "probe": (
                    {
                        "outcome": probe["outcome"],
                        "detail": clean_detail(probe["detail"]),
                        "verified_at": probe["verified_at"],
                        "audio": _json_list(probe["audio_streams_json"]),
                        "video": _json_list(probe["video_streams_json"]),
                    }
                    if probe is not None
                    else None
                ),
                "subtitles": tracks,
            }
        )
    sync_runs = [
        {
            "kind": "sync",
            "outcome": row["outcome"],
            "detail": clean_detail(row["detail"]),
            "required": int(row["required_count"]),
            "covered": int(row["covered_count"]),
            "created_at": row["created_at"],
            "completed_at": row["completed_at"],
        }
        for row in db.execute(
            """SELECT * FROM media_sync_runs
               WHERE monitor_id = ? ORDER BY id DESC LIMIT 20""",
            (monitor_id,),
        ).fetchall()
    ]
    current_assets = [
        asset
        for asset in assets
        if (
            (asset["status"] == "downloaded" and asset["usable_coverage"])
            or asset["status"] in _ACTIVE_ASSET_STATES
        )
    ]
    if summary["state"] == "archived":
        current_assets = []
    current_ids = {asset["id"] for asset in current_assets}
    other_assets = [asset for asset in assets if asset["id"] not in current_ids]
    if summary["state"] == "ready":
        for asset in current_assets:
            if asset["probe"] is None:
                asset["subtitle_state"] = "automatic"
                asset["subtitle_label"] = "Verified automatically"
                asset["subtitle_detail"] = (
                    "Omakase completed its subtitle and Plex publication checks. "
                    "No manual check is needed."
                )
    usable_episodes = sorted(
        {episode for asset in current_assets for episode in asset["usable_coverage"]}
    )
    removal_row = db.execute(
        """SELECT status, requested_at, plex_removed_at, completed_at, detail, scope
           FROM media_title_removals
           WHERE monitor_id = ?
           ORDER BY id DESC LIMIT 1""",
        (monitor_id,),
    ).fetchone()
    removal = None
    if removal_row is not None:
        removal = {
            "status": removal_row["status"],
            "status_label": {
                "awaiting_plex": "Removing from Plex",
                "plex_removed": "Removing provider media",
                "provider_failed": "Provider cleanup will retry",
                "complete": (
                    "Removed from Plex" if removal_row["scope"] == "plex_only" else "Media removed"
                ),
            }.get(str(removal_row["status"]), "Removal in progress"),
            "requested_at": removal_row["requested_at"],
            "plex_removed_at": removal_row["plex_removed_at"],
            "completed_at": removal_row["completed_at"],
            "detail": clean_detail(removal_row["detail"]),
            "scope": removal_row["scope"],
        }
    watch_timeline = [
        {
            "id": int(row["id"]),
            "source": row["source"],
            "source_label": {
                "plex": "Plex",
                "apple_tv": "Apple TV",
                "anilist": "AniList",
                "manual": "Manual",
                "migration": "Imported history",
            }.get(str(row["source"]), "Watch source"),
            "kind": row["kind"],
            "episode": row["episode"],
            "disposition": row["disposition"],
            "reason": row["reason"],
            "observed_at": row["observed_at"],
            "created_at": row["created_at"],
        }
        for row in db.execute(
            """SELECT event.*
               FROM watch_events AS event
               JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
               WHERE entry.user_id = ? AND entry.id = ?
               ORDER BY event.observed_at DESC, event.id DESC LIMIT 50""",
            (user_id, summary["watch_entry_id"]),
        ).fetchall()
    ]
    pending_confirmations = [
        {
            "id": int(row["id"]),
            "source": row["source"],
            "source_label": {
                "plex": "Plex",
                "apple_tv": "Apple TV",
                "anilist": "AniList",
                "manual": "Manual",
                "migration": "Imported history",
            }.get(str(row["source"]), "Watch source"),
            "proposed_episode": row["proposed_episode"],
            "prompt": row["prompt"],
            "created_at": row["created_at"],
        }
        for row in db.execute(
            """SELECT confirmation.*, event.source
               FROM watch_confirmations AS confirmation
               JOIN watch_entries AS entry ON entry.id = confirmation.watch_entry_id
               JOIN watch_events AS event ON event.id = confirmation.watch_event_id
               WHERE confirmation.user_id = ? AND entry.user_id = ?
                 AND entry.id = ? AND confirmation.status = 'pending'
               ORDER BY confirmation.created_at DESC, confirmation.id DESC""",
            (user_id, user_id, summary["watch_entry_id"]),
        ).fetchall()
    ]
    cleanup_block = db.execute(
        "SELECT detail FROM completion_plex_cleanup_blocks WHERE monitor_id = ? AND user_id = ?",
        (monitor_id, user_id),
    ).fetchone()
    score = db.execute(
        """SELECT event.score_ten FROM watch_events AS event
           JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
           WHERE entry.id = ? AND entry.user_id = ? AND event.user_id = entry.user_id
             AND event.disposition = 'accepted' AND event.score_ten IS NOT NULL
           ORDER BY event.id DESC LIMIT 1""",
        (summary["watch_entry_id"], user_id),
    ).fetchone()
    return {
        **summary,
        **cached_synopsis(db, int(monitor["anilist_id"])),
        "search_titles": _json_strings(monitor["search_titles_json"]),
        "genres": _json_strings(monitor["genres_json"]),
        "assets": assets,
        "current_assets": current_assets,
        "other_assets": other_assets,
        "usable_episodes": usable_episodes,
        "timeline": sync_runs,
        "watch_timeline": watch_timeline,
        "pending_confirmations": pending_confirmations,
        "removal": removal,
        "cleanup_block": clean_detail(cleanup_block["detail"]) if cleanup_block else "",
        "keep_completed_in_plex": bool(monitor["keep_completed_in_plex"]),
        "score_ten": score["score_ten"] if score else None,
    }


def saved_watchlist(db, *, user_id: int, query: str = "") -> list[dict[str, Any]]:
    """Locally saved picks awaiting enrollment, without invented availability."""
    rows = db.execute(
        """SELECT plan.anilist_id, plan.title, plan.added_at
           FROM anilist_plannings AS plan
           WHERE plan.user_id = ? AND plan.status = 'PLANNING'
             AND NOT EXISTS (SELECT 1 FROM media_monitors AS monitor
                 WHERE monitor.user_id = plan.user_id AND monitor.anilist_id = plan.anilist_id)
             AND NOT EXISTS (SELECT 1 FROM recommendation_feedback AS feedback
                 WHERE feedback.user_id = plan.user_id AND feedback.media_id = plan.anilist_id
                 AND feedback.feedback_type = 'already_watched')
           ORDER BY plan.added_at DESC, plan.id DESC LIMIT 100""",
        (user_id,),
    ).fetchall()
    text = query.strip().casefold()
    return [dict(row) for row in rows if not text or text in str(row["title"]).casefold()]


def discover_view(db, *, user_id: int, run_id: int | None = None) -> dict[str, Any]:
    handled_rows = db.execute(
        """SELECT source, media_id, title
           FROM recommendation_feedback
           WHERE user_id = ? AND feedback_type IN ('not_for_me', 'wrong_sequel', 'already_watched')""",
        (user_id,),
    ).fetchall()
    handled_ids = {
        (str(row["source"] or "anilist"), int(row["media_id"]))
        for row in handled_rows
        if row["media_id"] is not None
    }
    handled_titles = {
        (str(row["source"] or "anilist"), str(row["title"]).strip().casefold())
        for row in handled_rows
    }
    rows = db.execute(
        """SELECT id, source, model, lane, picks, created_at
           FROM run_history WHERE user_id = ?
           ORDER BY id DESC LIMIT 20""",
        (user_id,),
    ).fetchall()
    if run_id is not None and not any(int(row["id"]) == run_id for row in rows):
        selected = db.execute(
            "SELECT id, source, model, lane, picks, created_at FROM run_history "
            "WHERE user_id = ? AND id = ?",
            (user_id, run_id),
        ).fetchone()
        if selected is None:
            raise ProductViewError("Recommendation run was not found.")
        rows = [selected, *rows]
    library = {
        item["anilist_id"]: item for item in library_titles(db, user_id=user_id, lifecycle="all")
    }
    runs = []
    for row in rows:
        source = str(row["source"] or "anilist")
        picks = []
        for raw in _json_list(row["picks"]):
            if not isinstance(raw, dict):
                continue
            pick = dict(raw)
            pick_source = str(pick.get("source") or source)
            media_id = recommendation_anilist_id(pick, source) or _positive_int(
                pick.get("media_id")
            )
            pick["media_id"] = media_id
            pick["anilist_id"] = media_id if pick_source == "anilist" else None
            pick["url"] = (
                f"https://anilist.co/anime/{media_id}/"
                if pick_source == "anilist" and media_id
                else f"https://myanimelist.net/anime/{media_id}/"
                if pick_source == "myanimelist" and media_id
                else None
            )
            if media_id is not None and (pick_source, media_id) in handled_ids:
                continue
            if (pick_source, str(pick.get("title") or "").strip().casefold()) in handled_titles:
                continue
            item = library.get(media_id) if pick_source == "anilist" else None
            pick["library_id"] = item["id"] if item else None
            pick["availability"] = item["status_label"] if item else "Not in your library"
            pick["availability_state"] = item["state"] if item else "unknown"
            pick["verified_episodes"] = item["usable"] if item else 0
            pick["required_episodes"] = item["required"] if item else 0
            pick["release_qualities"] = item["release_qualities"] if item else []
            picks.append(pick)
        runs.append(
            {
                "id": int(row["id"]),
                "source": source,
                "model": row["model"],
                "lane": normalize_lane(row["lane"]),
                "created_at": row["created_at"],
                "picks": picks,
            }
        )
    latest = (
        next((run for run in runs if run["id"] == run_id), None)
        if run_id
        else (runs[0] if runs else None)
    )
    return {"runs": runs, "latest": latest}


def activity_view(
    db,
    *,
    user_id: int,
    outcome: str = "all",
    page: int = 1,
    limit: int = 20,
) -> dict[str, Any]:
    if outcome not in {"all", "success", "attention", "running"}:
        raise ProductViewError("activity outcome filter is unsupported")
    if limit < 1 or limit > 200:
        raise ProductViewError("activity limit is invalid")
    if page < 1 or page > 100_000:
        raise ProductViewError("activity page is invalid")
    success_outcomes = ("complete", "completed", "success", "healthy", "no_change", "refreshed")
    where = ["monitor.user_id = ?"]
    parameters: list[object] = [user_id]
    if outcome == "success":
        where.append(
            "run.completed_at IS NOT NULL "
            f"AND run.outcome IN ({','.join('?' for _ in success_outcomes)})"
        )
        parameters.extend(success_outcomes)
    elif outcome == "running":
        where.append("run.completed_at IS NULL")
    elif outcome == "attention":
        where.append(
            "run.completed_at IS NOT NULL "
            f"AND run.outcome NOT IN ({','.join('?' for _ in success_outcomes)})"
        )
        parameters.extend(success_outcomes)
    where_sql = " AND ".join(where)
    total = int(
        db.execute(
            f"""SELECT COUNT(*)
                FROM media_sync_runs AS run
                JOIN media_monitors AS monitor ON monitor.id = run.monitor_id
                WHERE {where_sql}""",
            parameters,
        ).fetchone()[0]
    )
    total_pages = max(1, (total + limit - 1) // limit)
    if page > total_pages:
        raise ProductViewError("activity page is out of range")
    rows = db.execute(
        f"""SELECT run.id, run.outcome, run.required_count, run.covered_count,
                   run.accepted_count, run.detail, run.created_at, run.completed_at,
                   monitor.id AS monitor_id, monitor.title
            FROM media_sync_runs AS run
            JOIN media_monitors AS monitor ON monitor.id = run.monitor_id
            WHERE {where_sql}
            ORDER BY run.id DESC LIMIT ? OFFSET ?""",
        (*parameters, limit, (page - 1) * limit),
    ).fetchall()
    events = []
    for row in rows:
        category = _activity_category(str(row["outcome"]), row["completed_at"])
        if outcome != "all" and category != outcome:
            continue
        events.append(
            {
                "id": int(row["id"]),
                "monitor_id": int(row["monitor_id"]),
                "title": row["title"],
                "outcome": _activity_outcome_label(
                    str(row["outcome"]),
                    required=int(row["required_count"]),
                    covered=int(row["covered_count"]),
                    completed_at=row["completed_at"],
                ),
                "category": category,
                "required": int(row["required_count"]),
                "covered": int(row["covered_count"]),
                "accepted": int(row["accepted_count"]),
                "detail": _activity_detail_label(
                    str(row["outcome"]),
                    required=int(row["required_count"]),
                    covered=int(row["covered_count"]),
                    detail=row["detail"],
                ),
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
            }
        )
    tracker_actions = []
    tracker_limit = limit if page == 1 else 0
    for row in db.execute(
        """SELECT id, tracker, media_id, title, action, score_ten, sync_state,
                  attempt_count, last_error, last_attempt_at, synced_at, created_at
           FROM tracker_media_actions
           WHERE user_id = ?
           ORDER BY id DESC LIMIT ?""",
        (user_id, tracker_limit),
    ).fetchall():
        category = {
            "synced": "success",
            "pending": "running",
            "failed": "attention",
            "unsupported": "attention",
        }.get(str(row["sync_state"]), "attention")
        if outcome != "all" and category != outcome:
            continue
        tracker_actions.append(
            {
                "id": int(row["id"]),
                "tracker": row["tracker"],
                "media_id": int(row["media_id"]),
                "title": row["title"],
                "action": row["action"],
                "score": (float(row["score_ten"]) if row["score_ten"] is not None else None),
                "state": row["sync_state"],
                "category": category,
                "attempts": int(row["attempt_count"]),
                "detail": clean_detail(row["last_error"]),
                "last_attempt_at": row["last_attempt_at"],
                "synced_at": row["synced_at"],
                "created_at": row["created_at"],
            }
        )
    watch_activity = []
    watch_limit = limit if page == 1 else 0
    from omakase.plus.households import feature_enabled

    retained_title = (
        "(SELECT i.title FROM watch_library_links link JOIN library_items i ON i.id=link.library_item_id WHERE link.user_id=entry.user_id AND link.watch_entry_id=entry.id ORDER BY i.created_at DESC,i.id LIMIT 1)"
        if feature_enabled()
        else "NULL"
    )
    for row in db.execute(
        f"""SELECT event.id, event.source, event.kind, event.episode,
                  event.disposition, event.reason, event.observed_at,
                  entry.id AS personal_entry_id, entry.anilist_id, monitor.id AS monitor_id, COALESCE(monitor.title,{retained_title}) AS title
           FROM watch_events AS event
           JOIN watch_entries AS entry ON entry.id = event.watch_entry_id
           LEFT JOIN media_monitors AS monitor ON monitor.id = entry.monitor_id AND monitor.user_id = entry.user_id
           WHERE event.user_id = ? AND entry.user_id = ?
           ORDER BY event.observed_at DESC, event.id DESC LIMIT ?""",
        (user_id, user_id, watch_limit),
    ).fetchall():
        category = "success" if row["disposition"] == "accepted" else "attention"
        if outcome != "all" and category != outcome:
            continue
        source_label = {
            "plex": "Plex",
            "apple_tv": "Apple TV",
            "anilist": "AniList",
            "manual": "manually",
            "migration": "imported history",
        }.get(str(row["source"]), "another source")
        kind_label = {
            "progress": (
                f"Episode {int(row['episode'])} watched"
                if row["episode"] is not None
                else "Watch progress updated"
            ),
            "completed": "Marked complete",
            "correction": "Watch progress corrected",
            "reopened": "Reopened for watching",
        }.get(str(row["kind"]), "Watch progress updated")
        detail = "Updated manually" if row["source"] == "manual" else f"Updated by {source_label}"
        if row["disposition"] == "confirmation_required":
            detail = "Waiting for your confirmation"
        elif row["disposition"] == "ignored":
            detail = "No progress change was needed"
        watch_activity.append(
            {
                "id": int(row["id"]),
                "monitor_id": int(row["monitor_id"]) if row["monitor_id"] is not None else None,
                "detail_url": f"/plus/library/{row['monitor_id']}"
                if row["monitor_id"] is not None
                else f"/plus/history/anime/{row['personal_entry_id']}",
                "title": row["title"] or f"AniList #{row['anilist_id']}",
                "outcome": kind_label,
                "detail": detail,
                "source_label": source_label,
                "category": category,
                "created_at": row["observed_at"],
            }
        )
    return {
        "events": events,
        "watch_activity": watch_activity,
        "tracker_actions": tracker_actions,
        "filter": outcome,
        "page": page,
        "total": total,
        "total_pages": total_pages,
        "page_numbers": sorted(
            {
                1,
                total_pages,
                *range(max(1, page - 2), min(total_pages, page + 2) + 1),
            }
        ),
    }


def health_view(db, *, user_id: int) -> dict[str, Any]:
    providers = [
        {
            "id": int(row["id"]),
            "kind": row["kind"],
            "label": row["label"],
            "enabled": bool(row["enabled"]),
            "status": row["health_status"],
            "reason_code": row["health_reason_code"],
            "reason_label": reason_label(row["health_reason_code"]),
            "detail": clean_detail(row["health_detail"]),
            "checked_at": row["last_health_at"],
        }
        for row in db.execute(
            """SELECT id, kind, label, enabled, health_status, health_reason_code,
                      health_detail, last_health_at
               FROM media_providers WHERE user_id = ?
               ORDER BY enabled DESC, label COLLATE NOCASE""",
            (user_id,),
        ).fetchall()
    ]
    plex_targets = [
        {
            "id": int(row["id"]),
            "label": row["label"],
            "policy": row["publication_policy"],
            "status": row["health_status"],
            "reason_code": row["health_reason_code"],
            "reason_label": reason_label(row["health_reason_code"]),
            "detail": clean_detail(row["health_detail"]),
            "checked_at": row["last_health_at"],
        }
        for row in db.execute(
            """SELECT id, label, publication_policy, health_status,
                      health_reason_code, health_detail, last_health_at
               FROM plex_targets WHERE user_id = ? ORDER BY label COLLATE NOCASE""",
            (user_id,),
        ).fetchall()
    ]
    if not plex_targets:
        receipt_row = db.execute(
            "SELECT receipt_json, received_at FROM media_curator_receipt_latest WHERE id = 1"
        ).fetchone()
        if receipt_row is not None:
            try:
                receipt = validate_curator_receipt(json.loads(receipt_row["receipt_json"]))
            except (TypeError, ValueError, json.JSONDecodeError):
                receipt = None
            if receipt is not None:
                converged = bool(receipt["topology_converged"])
                plex_targets.append(
                    {
                        "id": 0,
                        "label": "Plex library",
                        "policy": "Managed publication",
                        "status": "healthy" if converged else "degraded",
                        "reason_code": "READY" if converged else "PLEX_SECTION_MISMATCH",
                        "reason_label": "Verified by the latest Plex check"
                        if converged
                        else "Plex library or episode mismatch found",
                        "detail": clean_detail(
                            "Latest Plex scan and episode visibility check passed"
                            if converged
                            else "Latest Plex check found a library or episode visibility mismatch"
                        ),
                        "checked_at": receipt.get("completed_at") or receipt_row["received_at"],
                        "source": "Automatic Plex check",
                    }
                )
    for item in providers:
        item["source"] = "Automatic media-source check"
        item["status_label"] = _component_status_label(item["status"], kind="provider")
    for item in plex_targets:
        item.setdefault("source", "Saved Plex connection")
        item["status_label"] = _component_status_label(item["status"], kind="plex")
    catalogs = [
        {
            "kind": row["catalog_kind"],
            "label": row["label"],
            "status": row["health_status"],
            "reason_code": row["reason_code"],
            "reason_label": reason_label(row["reason_code"]),
            "detail": clean_detail(row["detail"]),
            "candidate_count": int(row["candidate_count"]),
            "checked_at": row["checked_at"],
            "source": "Automatic source search",
        }
        for row in db.execute(
            """SELECT catalog_kind, label, health_status, reason_code, detail,
                      candidate_count, checked_at
               FROM media_catalog_health
               WHERE user_id = ?
               ORDER BY catalog_kind COLLATE NOCASE""",
            (user_id,),
        ).fetchall()
    ]
    present_catalogs = {str(item["kind"]) for item in catalogs}
    if "nyaa" not in present_catalogs:
        catalogs.append(
            {
                "kind": "nyaa",
                "label": "Built-in anime source",
                "status": "unknown",
                "reason_code": "CATALOG_NOT_CHECKED",
                "reason_label": reason_label("CATALOG_NOT_CHECKED"),
                "detail": "The first monitored-title search has not run yet.",
                "candidate_count": 0,
                "checked_at": None,
                "source": "Built-in source",
            }
        )
    catalog_configuration = configured_catalogs(db, user_id)
    if catalog_configuration.state != "optional" and "prowlarr" not in present_catalogs:
        configured = catalog_configuration.state == "ready"
        catalogs.append(
            {
                "kind": "prowlarr",
                "label": "Prowlarr expanded search",
                "status": "unknown" if configured else "degraded",
                "reason_code": (
                    "CATALOG_NOT_CHECKED" if configured else "CATALOG_SETUP_INCOMPLETE"
                ),
                "reason_label": reason_label(
                    "CATALOG_NOT_CHECKED" if configured else "CATALOG_SETUP_INCOMPLETE"
                ),
                "detail": catalog_configuration.detail,
                "candidate_count": 0,
                "checked_at": None,
                "source": "Saved source settings",
            }
        )
    for item in catalogs:
        item["status_label"] = _component_status_label(
            item["status"],
            kind="catalog",
            has_results=int(item["candidate_count"]) > 0,
        )
    lease = db.execute("SELECT * FROM media_sync_lease WHERE id = 1").fetchone()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lease_is_live = bool(
        lease is not None and lease["entrypoint"] != "released" and str(lease["expires_at"]) > now
    )
    failures = db.execute(
        """SELECT COUNT(*) FROM media_monitors
           WHERE user_id = ? AND consecutive_failures > 0""",
        (user_id,),
    ).fetchone()[0]
    components = providers + plex_targets
    catalog_failures = any(item["status"] in {"error", "degraded"} for item in catalogs)
    overall = (
        "attention"
        if failures
        or catalog_failures
        or any(item["status"] in {"error", "degraded", "unknown"} for item in components)
        else "healthy"
    )
    if not components and not catalog_failures:
        overall = "setup"
    return {
        "overall": overall,
        "providers": providers,
        "plex_targets": plex_targets,
        "catalogs": catalogs,
        "monitor_failures": int(failures),
        "scheduler": (
            {
                "state": "running",
                "entrypoint": lease["entrypoint"],
                "acquired_at": lease["acquired_at"],
                "heartbeat_at": lease["heartbeat_at"],
                "expires_at": lease["expires_at"],
            }
            if lease_is_live
            else {"state": "idle"}
        ),
    }


def setup_view(db, *, user_id: int) -> dict[str, Any]:
    """Derive setup progress from the same live receipts used by the product."""
    health = health_view(db, user_id=user_id)
    provider_ready = any(
        item["enabled"] and item["status"] in {"healthy", "unknown"} for item in health["providers"]
    )
    plex_ready = any(item["status"] == "healthy" for item in health["plex_targets"])
    monitor_count = int(
        db.execute("SELECT COUNT(*) FROM media_monitors WHERE user_id = ?", (user_id,)).fetchone()[
            0
        ]
    )
    asset_count = int(
        db.execute(
            """SELECT COUNT(*) FROM media_assets AS asset
               JOIN media_monitors AS monitor ON monitor.id = asset.monitor_id
               WHERE monitor.user_id = ?""",
            (user_id,),
        ).fetchone()[0]
    )
    identity_ready = (
        db.execute(
            "SELECT 1 FROM user_secrets WHERE user_id = ? AND key_name = 'anilist_oauth_token'",
            (user_id,),
        ).fetchone()
        is not None
    )
    steps = {
        "deployment": {
            "status": "ready" if provider_ready or monitor_count else "pending",
            "detail": "Omakase Plus is running and has a media mode."
            if provider_ready or monitor_count
            else "Choose Real-Debrid or local storage.",
        },
        "storage": {
            "status": "ready" if asset_count else "pending",
            "detail": f"{asset_count} release{'s' if asset_count != 1 else ''} tracked."
            if asset_count
            else "No acquired or imported releases are tracked yet.",
        },
        "plex": {
            "status": "ready" if plex_ready else "blocked",
            "detail": "The latest Plex scan and episode check passed."
            if plex_ready
            else "Connect Plex and complete one verified library scan.",
        },
        "acquisition": {
            "status": "ready" if provider_ready else "blocked",
            "detail": "A download or import method is configured."
            if provider_ready
            else "Connect Real-Debrid or configure local acquisition.",
        },
        "identity": {
            "status": "ready" if identity_ready else "pending",
            "detail": "AniList is connected."
            if identity_ready
            else "Connect AniList for watched status, scores, and exact title matching.",
        },
        "readiness": {
            "status": "ready" if health["overall"] == "healthy" else "blocked",
            "detail": "Downloads, Plex, automatic checks, and monitored titles agree."
            if health["overall"] == "healthy"
            else "Open Health to see the remaining checks in plain language.",
        },
    }
    complete = sum(item["status"] == "ready" for item in steps.values())
    return {
        "steps": steps,
        "progress": {
            "complete": complete,
            "total": len(steps),
            "blocked": sum(item["status"] == "blocked" for item in steps.values()),
            "ready": complete == len(steps),
        },
    }


def _asset_status_label(value: object) -> str:
    return {
        "downloaded": "Downloaded",
        "downloading": "Downloading",
        "queued": "Waiting to download",
        "requested": "Requested",
        "error": "File check failed",
        "quarantined": "Not being used",
        "stalled": "Download stalled",
    }.get(str(value), str(value).replace("_", " ").title())


_PLEX_BLOCK_LABELS = {
    "PLEX_ASSET_COVERAGE_INVALID": (
        "Plex found a coverage mismatch in this release; searching for another"
    ),
    "PLEX_ASSET_VALIDATION_FAILED": "Plex rejected this release; searching for another",
    "PLEX_DESTINATION_CONFLICT": "This release collides with another Plex item",
    "PLEX_ENGLISH_SUBTITLE_MISSING": (
        "Plex needs a selectable English subtitle track; searching for another release"
    ),
    "PLEX_MEDIA_PROBE_FAILED": (
        "The provider file was locked or unreadable when Plex checked it; "
        "searching for another release"
    ),
    "PLEX_POLICY_EXCLUDED": "This source is excluded from Plex by policy",
    "PLEX_SOURCE_UNAVAILABLE": (
        "The provider folder is no longer available; searching for another release"
    ),
}


def _asset_error_label(value: object) -> str:
    normalized = str(value or "").strip()
    if normalized in _PLEX_BLOCK_LABELS:
        return _PLEX_BLOCK_LABELS[normalized]
    labels = {
        "PROVIDER_DOWNLOAD_BLOCKED": "Real-Debrid lists this as downloaded but restricts file access; another release is needed",
        "DOWNLOAD_ACCESS_UNVERIFIED": "The provider download link could not be verified; waiting for a retry",
        "NO_SWITCHABLE_ENGLISH_SUBTITLE": "No separate English subtitle track",
        "info_failed": "Couldn't read the download details",
        "INFO_FAILED": "Couldn't read the download details",
    }
    return labels.get(normalized, clean_detail(normalized.replace("_", " ").capitalize()))


def library_jobs(db, user_id: int) -> list[dict[str, Any]]:
    visible_counts = plex_visible_counts(db)
    rows = db.execute(
        """SELECT * FROM media_monitors
           WHERE user_id = ?
           ORDER BY active DESC,
                    CASE WHEN next_check_at IS NULL THEN 1 ELSE 0 END,
                    next_check_at,
                    id""",
        (user_id,),
    ).fetchall()
    jobs: list[dict[str, Any]] = []
    for row in rows:
        required = _required_count(row)
        usable = len(covered_episodes(db, int(row["id"])))
        assets = db.execute(
            """SELECT quality, source_folder, source_title, status
               FROM media_assets WHERE monitor_id = ?
               ORDER BY CASE status
                   WHEN 'downloaded' THEN 0
                   WHEN 'downloading' THEN 1
                   WHEN 'queued' THEN 2
                   ELSE 3 END,
                   id DESC""",
            (row["id"],),
        ).fetchall()
        quality = next(
            (
                value
                for value in ("2160p", "1080p", "720p")
                if any(asset["quality"] == value for asset in assets)
            ),
            "Not selected",
        )
        source = "Awaiting source"
        if assets:
            source = clean_detail(assets[0]["source_folder"] or assets[0]["source_title"])
        provider_state = str(assets[0]["status"]) if assets else "planned"
        plex = visible_counts.get((int(row["anilist_id"]), row["season_number"]))
        plex_state = f"{plex[1]} / {plex[0]} visible" if plex else "Awaiting curator"
        jobs.append(
            {
                "id": int(row["id"]),
                "anilist_id": int(row["anilist_id"]),
                "title": row["title"],
                "release_status": row["release_status"] or "UNKNOWN",
                "release_status_label": release_status_label(row["release_status"]),
                "season_number": row["season_number"],
                "active": bool(row["active"]),
                "covered": usable,
                "required": required,
                "progress_max": max(1, required),
                "quality": quality,
                "source": source,
                "rd_state": provider_state,
                "provider_state": provider_state,
                "plex_state": plex_state,
                "next_check_at": row["next_check_at"] or "Not scheduled",
                "last_result": row["last_result"] or "No sync receipt yet",
            }
        )
    return jobs


def plex_visible_counts(db) -> dict[tuple[int, int | None], tuple[int, int]]:
    row = db.execute(
        "SELECT receipt_json FROM media_curator_receipt_latest WHERE id = 1"
    ).fetchone()
    if row is None:
        return {}
    try:
        receipt = validate_curator_receipt(json.loads(row["receipt_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if receipt["mode"] != "live" or not receipt["topology_converged"]:
        return {}
    return {
        (item["anilist_id"], item["season_number"]): (
            item["usable_count"],
            item["plex_visible_count"],
        )
        for item in receipt["title_coverage"]
    }


def recommendation_anilist_id(pick: dict, source: str) -> int | None:
    stored_id = _positive_int(pick.get("anilist_id"))
    if stored_id is not None:
        return stored_id
    if (pick.get("source") or source) == "anilist":
        media_id = _positive_int(pick.get("media_id"))
        if media_id is not None:
            return media_id
    url = pick.get("url")
    if not isinstance(url, str):
        return None
    match = _ANILIST_ID_RE.search(url)
    return int(match.group(1)) if match else None


def _attach_download_attempts(db, *, user_id: int, plannings: list[dict]) -> None:
    planning_ids = [item["id"] for item in plannings]
    if not planning_ids:
        return
    placeholders = ",".join("?" for _ in planning_ids)
    rows = db.execute(
        f"""SELECT anilist_planning_id, request_id, candidate_rank, total_candidates,
                   torrent_title, torrent_hash, seeders, size_display, is_batch,
                   status, http_status, error_code, detail, rd_torrent_id, created_at
            FROM download_attempts
            WHERE user_id = ? AND anilist_planning_id IN ({placeholders})
            ORDER BY anilist_planning_id, created_at DESC, id DESC""",
        (user_id, *planning_ids),
    ).fetchall()
    attempts: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        items = attempts[int(row["anilist_planning_id"])]
        if len(items) >= 5:
            continue
        items.append(
            {
                "request_id": row["request_id"],
                "candidate_rank": row["candidate_rank"],
                "total_candidates": row["total_candidates"],
                "torrent_title": row["torrent_title"],
                "torrent_hash": row["torrent_hash"],
                "seeders": row["seeders"],
                "size_display": row["size_display"],
                "is_batch": bool(row["is_batch"]),
                "status": row["status"],
                "status_label": _attempt_status_label(row["status"]),
                "badge_class": _attempt_badge(row["status"]),
                "response_label": _attempt_response(row),
                "detail": row["detail"],
                "rd_torrent_id": row["rd_torrent_id"],
                "created_at": row["created_at"],
            }
        )
    for planning in plannings:
        planning["download_attempts"] = attempts[planning["id"]]


def _attempt_status_label(status: str) -> str:
    return {
        "provider_block": "Provider blocked",
        "rd_add_failed": "Add failed",
        "select_failed": "Select failed",
        "selected": "Selected",
    }.get(status, status.replace("_", " ").title())


def _attempt_badge(status: str) -> str:
    return {
        "provider_block": "badge-yellow",
        "rd_add_failed": "badge-red",
        "select_failed": "badge-red",
        "selected": "badge-green",
    }.get(status, "badge-gray")


def _attempt_response(row) -> str:
    parts = []
    if row["http_status"] is not None:
        parts.append(str(row["http_status"]))
    if row["error_code"]:
        parts.append(str(row["error_code"]))
    return " ".join(parts)


def _readiness_state(
    *,
    required: int,
    usable: int,
    plex_visible: int,
    asset_states: set[str],
    failures: int,
) -> str:
    if required > 0 and usable >= required and plex_visible >= required:
        return "ready"
    if asset_states & _ACTIVE_ASSET_STATES:
        return "acquiring"
    if (
        required > 0
        and (usable < required or plex_visible < required)
        or failures > 0
        or asset_states & _ATTENTION_ASSET_STATES
    ):
        return "attention"
    return "planned"


def _readiness_label(*, state: str, required: int, usable: int, plex_visible: int) -> str:
    if state == "ready":
        return "Ready"
    missing_media = max(0, required - usable)
    if missing_media:
        noun = "episode" if missing_media == 1 else "episodes"
        if state == "acquiring":
            return f"Finding {missing_media} missing {noun}"
        return f"{missing_media} {noun} missing"
    missing_plex = max(0, required - plex_visible)
    if missing_plex:
        if plex_visible == 0:
            return "Not in Plex"
        noun = "episode" if missing_plex == 1 else "episodes"
        return f"{missing_plex} {noun} not in Plex"
    if required <= 0:
        return "Waiting for aired episodes"
    return "Needs setup"


def release_status_label(value: object) -> str:
    """Translate AniList's machine status into viewer-facing copy."""
    normalized = str(value or "").strip().upper()
    return {
        "FINISHED": "Finished airing",
        "RELEASING": "Currently airing",
        "NOT_YET_RELEASED": "Not yet airing",
        "CANCELLED": "Cancelled",
        "CANCELED": "Cancelled",
        "HIATUS": "On hiatus",
        "UNKNOWN": "Status unknown",
        "": "Status unknown",
    }.get(normalized, normalized.replace("_", " ").title())


def _states() -> tuple[str, ...]:
    return ("ready", "acquiring", "attention", "planned")


def _required_count(row) -> int:
    if row["release_status"] == "FINISHED":
        return int(row["expected_episodes"] or 0)
    return int(row["aired_episodes"] or 0)


def _activity_category(outcome: str, completed_at: object) -> str:
    if completed_at is None:
        return "running"
    if outcome in {"complete", "completed", "success", "healthy", "no_change", "refreshed"}:
        return "success"
    return "attention"


def _component_status_label(
    status: object,
    *,
    kind: str,
    has_results: bool = False,
) -> str:
    normalized = str(status or "").strip().casefold()
    if normalized == "healthy":
        return "Ready"
    if normalized == "unknown":
        return "Not checked"
    if kind == "catalog":
        if normalized == "degraded" and has_results:
            return "Some sources unavailable"
        return "Search unavailable"
    if kind == "plex":
        return "Needs attention" if normalized == "degraded" else "Unavailable"
    return "Limited" if normalized == "degraded" else "Unavailable"


def _activity_outcome_label(
    outcome: str,
    *,
    required: int,
    covered: int,
    completed_at: object,
) -> str:
    if completed_at is None:
        return "Checking now"
    normalized = outcome.strip().casefold()
    if normalized == "refreshed":
        return "Checked existing files"
    if normalized == "degraded":
        return "Episodes still missing"
    if normalized in {"error", "failed", "failure"}:
        return "Check failed"
    if normalized in {"complete", "completed", "success", "healthy", "no_change"}:
        return "Ready" if required > 0 and covered >= required else "Check complete"
    return normalized.replace("_", " ").title() or "Check complete"


def _activity_detail_label(
    outcome: str,
    *,
    required: int,
    covered: int,
    detail: object,
) -> str:
    normalized = outcome.strip().casefold()
    missing = max(0, required - covered)
    if normalized == "degraded" and missing:
        noun = "episode is" if missing == 1 else "episodes are"
        return f"Checked more sources. {missing} {noun} still missing."
    if normalized == "refreshed":
        return "Rechecked the files already linked to this title."
    if normalized == "no_change":
        return "Checked again; nothing changed."
    if normalized in {"complete", "completed", "success", "healthy"} and missing == 0:
        return "All required episodes are ready."
    return clean_detail(detail) or "No additional details."


def _positive_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _json_list(value: object) -> list:
    try:
        payload = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _json_ints(value: object) -> list[int]:
    return [
        int(item)
        for item in _json_list(value)
        if isinstance(item, int) and not isinstance(item, bool) and item > 0
    ]


def _json_strings(value: object) -> list[str]:
    return [item for item in _json_list(value) if isinstance(item, str)]


def _redact_url_queries(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname or ""
            port = f":{parsed.port}" if parsed.port else ""
        except ValueError:
            return "<url:redacted>"
        netloc = hostname + port
        query = urlencode(
            [
                (key, "<redacted>" if key.casefold() in _SENSITIVE_QUERY_KEYS else item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))

    return re.sub(r"https?://[^\s\"'<>]+", replace, value)
