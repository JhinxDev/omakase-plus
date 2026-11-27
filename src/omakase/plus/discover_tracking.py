"""Shared Discover and native library tracking persistence and AniList sync."""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from omakase.plus import discover_hub
from omakase.plus.anilist import AniListWriteError, save_media_list_entry, with_valid_token
from omakase.plus.connections import movie_connections
from omakase.plus.feedback import save_feedback
from omakase.plus.movies import MovieError, MovieService
from omakase.plus.secrets import read_secret
from omakase.plus.tracker_sync import (
    BridgeError,
    anilist_credential_fingerprint,
    require_anilist_credential,
    require_writer_lease,
    writer_lease,
)
from omakase.plus.tv_service import TVError, TVService


def coerce_optional_int(value: object, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"{field_name} must be an integer") from None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    return value


def coerce_optional_score(value: object) -> float | None:
    import math

    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError("score must be a number from 1 to 10")
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise ValueError("score must be a number from 1 to 10") from None
    if not math.isfinite(score) or not 1 <= score <= 10:
        raise ValueError("score must be a number from 1 to 10")
    return round(score, 1)


async def save_discover_track(
    db,
    user_id: int,
    body: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Save watch status, episode progress, rating, or dismissal without downloading."""
    kind = str(body.get("kind") or "").strip().lower()
    if kind not in {"anime", "movie", "tv"}:
        return 422, {"status": "error", "detail": "Unsupported title type."}

    title = str(body.get("title") or "").strip()[:300]
    if not title:
        return 422, {"status": "error", "detail": "Title is required."}

    status = str(body.get("status") or "").strip().lower()
    allowed_statuses = {
        "anime": {"watching", "completed", "planned", "paused", "dropped", "not_for_me"},
        "movie": {"watching", "completed", "dropped", "not_for_me"},
        "tv": {"watching", "completed", "dropped", "not_for_me"},
    }[kind]
    if status not in allowed_statuses:
        return 422, {"status": "error", "detail": "Choose a valid watch status."}

    try:
        media_id = coerce_optional_int(body.get("media_id"), "media_id")
        progress = coerce_optional_int(body.get("progress"), "progress")
        episode_total = coerce_optional_int(body.get("episode_total"), "episode_total")
        score = coerce_optional_score(body.get("score"))
    except ValueError as exc:
        return 422, {"status": "error", "detail": str(exc)}

    imdb_id = str(body.get("imdb_id") or "").strip()
    if kind == "movie" and media_id is None and re.fullmatch(r"tt[0-9]{7,10}", imdb_id):
        row = db.execute(
            "SELECT tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?",
            (user_id, imdb_id),
        ).fetchone()
        if row and row["tmdb_id"]:
            media_id = int(row["tmdb_id"])

    if media_id is None or not 0 < media_id <= 2_147_483_647:
        return 422, {"status": "error", "detail": "A valid media ID is required."}
    if progress is not None and not 0 <= progress <= 100000:
        return 422, {"status": "error", "detail": "Progress must be between 0 and 100000."}
    if episode_total is not None and not 0 < episode_total <= 100000:
        return 422, {"status": "error", "detail": "Episode total is invalid."}
    if score is not None and abs(score * 2 - round(score * 2)) > 1e-9:
        return 422, {
            "status": "error",
            "detail": "Rating must be in 0.5 increments between 1 and 10.",
        }

    source = "anilist" if kind == "anime" else kind
    if status == "not_for_me":
        save_feedback(db, user_id, source, media_id, title, "not_for_me", None)
        if kind == "anime":
            discover_hub.invalidate_tracker_cache(user_id)
        else:
            discover_hub.invalidate_screen_cache(user_id)
        return 200, {
            "status": "ok",
            "local_saved": True,
            "watch_status": "not_for_me",
            "remove_from_for_you": True,
            "detail": "Hidden from For You. Future picks will exclude it.",
        }

    if kind == "anime":
        anilist_status = {
            "watching": "CURRENT",
            "completed": "COMPLETED",
            "planned": "PLANNING",
            "paused": "PAUSED",
            "dropped": "DROPPED",
        }[status]
        effective_progress = (
            episode_total
            if (status == "completed" and progress is None and episode_total)
            else progress
        )
        observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        existing_plan = db.execute(
            "SELECT id FROM anilist_plannings WHERE user_id = ? AND anilist_id = ? ORDER BY id DESC LIMIT 1",
            (user_id, media_id),
        ).fetchone()
        if existing_plan:
            db.execute(
                "UPDATE anilist_plannings SET status = ?, title = ? WHERE id = ?",
                (anilist_status, title, existing_plan["id"]),
            )
        else:
            db.execute(
                "INSERT INTO anilist_plannings (user_id, anilist_id, title, status) VALUES (?, ?, ?, ?)",
                (user_id, media_id, title, anilist_status),
            )
        if (
            status in {"watching", "completed", "paused", "dropped"}
            or effective_progress is not None
            or score is not None
        ):
            prog = effective_progress if effective_progress is not None else 0
            entry_status = (
                "completed"
                if status == "completed"
                else ("watching" if (status == "watching" or prog > 0) else "unwatched")
            )
            completed_at = observed_at if entry_status == "completed" else None
            existing_entry = db.execute(
                """SELECT id, episode_total, progress_episodes
                   FROM watch_entries
                   WHERE user_id = ? AND anilist_id = ?
                   ORDER BY (season_number = 0) DESC, (monitor_id IS NOT NULL) DESC, id DESC LIMIT 1""",
                (user_id, media_id),
            ).fetchone()
            final_total = episode_total or (
                existing_entry["episode_total"] if existing_entry else None
            )
            if final_total is not None and prog > final_total:
                final_total = None
            if existing_entry:
                entry_id = existing_entry["id"]
                db.execute(
                    """UPDATE watch_entries
                       SET status = ?, progress_episodes = ?, episode_total = ?,
                           last_source = 'manual', confidence = 'manual',
                           last_observed_at = ?, completed_at = ?, updated_at = datetime('now')
                       WHERE id = ?""",
                    (entry_status, prog, final_total, observed_at, completed_at, entry_id),
                )
            else:
                monitor = db.execute(
                    "SELECT id, season_number FROM media_monitors WHERE user_id = ? AND anilist_id = ? ORDER BY id LIMIT 1",
                    (user_id, media_id),
                ).fetchone()
                monitor_id = int(monitor["id"]) if monitor else None
                season_number = int(monitor["season_number"] or 0) if monitor else 0
                cursor = db.execute(
                    """INSERT INTO watch_entries
                       (user_id, monitor_id, anilist_id, season_number, status, progress_episodes,
                        episode_total, last_source, confidence, last_observed_at, completed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', 'manual', ?, ?)""",
                    (
                        user_id,
                        monitor_id,
                        media_id,
                        season_number,
                        entry_status,
                        prog,
                        final_total,
                        observed_at,
                        completed_at,
                    ),
                )
                entry_id = cursor.lastrowid
            if entry_status == "completed":
                event_score = int(round(score)) if score is not None else None
                db.execute(
                    """INSERT INTO watch_events
                       (watch_entry_id, user_id, source, event_key, kind, episode, confidence,
                        force, disposition, reason, observed_at, score_ten)
                       VALUES (?, ?, 'manual', ?, 'completed', ?, 'manual', 1, 'accepted',
                               'Recorded from For You', ?, ?)""",
                    (
                        entry_id,
                        user_id,
                        f"discover:complete:{media_id}:{observed_at}:{uuid.uuid4().hex[:8]}",
                        prog,
                        observed_at,
                        event_score,
                    ),
                )
            elif prog > 0:
                db.execute(
                    """INSERT INTO watch_events
                       (watch_entry_id, user_id, source, event_key, kind, episode, confidence,
                        force, disposition, reason, observed_at, score_ten)
                       VALUES (?, ?, 'manual', ?, 'progress', ?, 'manual', 1, 'accepted',
                               'Progress recorded from For You', ?, NULL)""",
                    (
                        entry_id,
                        user_id,
                        f"discover:progress:{media_id}:{observed_at}:{uuid.uuid4().hex[:8]}",
                        prog,
                        observed_at,
                    ),
                )
        db.commit()
        if status == "completed":
            save_feedback(db, user_id, "anilist", media_id, title, "already_watched", None)
        elif status == "dropped":
            save_feedback(db, user_id, "anilist", media_id, title, "not_for_me", None)

        has_token = bool(read_secret(db, user_id, "anilist_oauth_token"))
        tracker_status = "not_connected"
        detail = "Saved in Omakase. Connect AniList in Settings to sync automatically."
        if has_token:
            import sys

            routes_mod = sys.modules.get("omakase.plus.routes")
            w_lease = getattr(routes_mod, "writer_lease", writer_lease)
            fingerprint_fn = getattr(
                routes_mod, "anilist_credential_fingerprint", anilist_credential_fingerprint
            )
            req_cred = getattr(routes_mod, "require_anilist_credential", require_anilist_credential)
            req_lease = getattr(routes_mod, "require_writer_lease", require_writer_lease)
            valid_tok = getattr(routes_mod, "with_valid_token", with_valid_token)
            save_entry = getattr(routes_mod, "save_media_list_entry", save_media_list_entry)

            client_id = os.getenv("ANILIST_CLIENT_ID", "")
            client_secret = os.getenv("ANILIST_CLIENT_SECRET", "")
            base_url = os.getenv("OMAKASE_PLUS_URL", "http://localhost:8765")
            redirect_uri = f"{base_url}/plus/integrations/anilist/callback"
            try:
                with w_lease(db, user_id) as lease:
                    credential = fingerprint_fn(db, user_id)
                    req_cred(db, user_id, credential)
                    req_lease(db, user_id, lease)
                    with valid_tok(db, user_id, client_id, client_secret, redirect_uri) as token:
                        req_lease(db, user_id, lease)
                        req_cred(db, user_id, credential)
                        save_entry(
                            token,
                            media_id,
                            status=anilist_status,
                            progress=effective_progress,
                            score_ten=score,
                        )
                tracker_status = "synced"
                detail = f"Saved to AniList ({anilist_status.title()}) and Omakase."
            except (BridgeError, AniListWriteError, httpx.HTTPError, ValueError):
                tracker_status = "local_only"
                detail = "Saved in Omakase; AniList sync did not complete yet."

        discover_hub.invalidate_tracker_cache(user_id)
        return 200, {
            "status": "ok",
            "local_saved": True,
            "tracker_status": tracker_status,
            "watch_status": status,
            "progress": effective_progress,
            "score": score,
            "remove_from_for_you": status in {"completed", "watching", "paused", "dropped"},
            "saved_to_watchlist": status == "planned",
            "detail": detail,
        }

    if kind == "movie":
        if not re.fullmatch(r"tt[0-9]{7,10}", imdb_id):
            return 422, {"status": "error", "detail": "A valid movie IMDb ID is required."}
        from omakase.plus.movie_history import UNSET as MOVIE_UNSET
        from omakase.plus.movie_history import MovieHistoryService

        try:
            await MovieHistoryService(MovieService(db, movie_connections(db, user_id))).save(
                user_id,
                imdb_id=imdb_id,
                tmdb_id=media_id,
                status=status,
                rating=score if score is not None else MOVIE_UNSET,
            )
        except (MovieError, ValueError) as exc:
            detail = exc.detail if isinstance(exc, MovieError) else str(exc)
            return 422, {"status": "error", "detail": detail}
        if status == "completed":
            save_feedback(db, user_id, "movie", media_id, title, "already_watched", None)
        discover_hub.invalidate_screen_cache(user_id, "movies")
        return 200, {
            "status": "ok",
            "local_saved": True,
            "tracker_status": "local_only",
            "watch_status": status,
            "score": score,
            "remove_from_for_you": True,
            "detail": "Saved to your Movies watch history without downloading.",
        }

    from omakase.plus.tv_service import UNSET as TV_UNSET

    try:
        await TVService(db, movie_connections(db, user_id)).save_history(
            user_id,
            media_id,
            status=status,
            rating=score if score is not None else TV_UNSET,
        )
    except (TVError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, TVError) else str(exc)
        return 422, {"status": "error", "detail": detail}
    if status == "completed":
        save_feedback(db, user_id, "tv", media_id, title, "already_watched", None)
    discover_hub.invalidate_screen_cache(user_id, "tv")
    return 200, {
        "status": "ok",
        "local_saved": True,
        "tracker_status": "local_only",
        "watch_status": status,
        "score": score,
        "remove_from_for_you": True,
        "detail": "Saved to your TV Shows watch history without downloading.",
    }
