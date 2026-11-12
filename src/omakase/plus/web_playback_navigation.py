"""Read-only playback navigation candidates; only the Play POST grants playback."""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlencode

from omakase.plus import households, quality_publication
from omakase.plus.web_playback_source import publication_identity, qualified_publication
from omakase.plus.web_playback_store import watch_barrier


def enabled():
    return all(
        os.getenv(name, "").strip().lower() in {"true", "1", "yes", "on"}
        for name in ("OMAKASE_PLUS_PRIVATE", "OMAKASE_PLUS_WEB_PLAYER_ENABLED")
    )


def _url(scope, monitor_id, episode):
    return f"/plus/watch/{int(monitor_id)}/{int(episode)}?" + urlencode(
        {"library_id": scope.library_id, "play": "1"}
    )


def _monitor(db, scope, card):
    households.revalidate_scope(db, scope)
    if scope.kind == "household":
        from omakase.plus.household_media import binding

        media = binding(db, scope, item_id=card["id"])
        if media.domain != "anime" or not media.legacy_id:
            return None
        monitor_id = media.legacy_id
    else:
        monitor_id = card["id"]
    return db.execute(
        "SELECT * FROM media_monitors WHERE id=? AND user_id=? AND anilist_id=? AND COALESCE(season_number,0)=?",
        (monitor_id, scope.operational_owner_id, card["anilist_id"], card["season_number"] or 0),
    ).fetchone()


def _candidates(db, scope, monitor, publication=None):
    """Strict current single-file selection, including unbound personal legacy rows.

    Legacy candidates do not create resources or bypass the full source resolver.
    Bound media additionally uses the gateway's publication identity unchanged.
    """
    season = monitor["season_number"] or 0
    film = "format" in monitor.keys() and monitor["format"] == "MOVIE"
    if film != (season == 0):
        return {}
    try:
        receipt, manifest = publication() if publication is not None else qualified_publication(db)
    except (ValueError, KeyError, TypeError, RecursionError):
        return {}
    titles = [t for t in manifest["titles"] if t["anilist_id"] == monitor["anilist_id"]]
    if len(titles) != 1 or ((titles[0].get("format") == "MOVIE") != film):
        return {}
    title = titles[0]["canonical_title"]
    if (
        not isinstance(title, str)
        or title in {"", ".", ".."}
        or any(c in "/\\" or ord(c) < 32 for c in title)
    ):
        return {}
    bindings = db.execute(
        "SELECT i.id,i.resource_id,r.legacy_id FROM library_items i JOIN library_resources r ON r.id=i.resource_id WHERE i.library_id=? AND i.state='active' AND i.domain='anime' AND r.owner_user_id=? AND r.legacy_id=? AND i.canonical_id=? AND i.season_number=? AND r.domain=i.domain AND r.canonical_id=i.canonical_id AND r.season_number=i.season_number",
        (
            scope.library_id,
            scope.operational_owner_id,
            str(monitor["id"]),
            str(monitor["anilist_id"]),
            season,
        ),
    ).fetchall()
    if len(bindings) > 1 or (scope.kind == "household" and len(bindings) != 1):
        return {}
    # A promoted resource is never implicitly rebound into the personal catalog.
    if (
        not bindings
        and db.execute(
            "SELECT 1 FROM library_resources WHERE owner_user_id=? AND domain='anime' AND legacy_id=?",
            (scope.operational_owner_id, str(monitor["id"])),
        ).fetchone()
    ):
        return {}
    files = [
        f
        for f in receipt["publication_files"]
        if f["selected"]
        and f["anilist_id"] == monitor["anilist_id"]
        and f["season_number"] == (1 if film else season)
    ]
    if film and (len(files) != 1 or files[0]["episodes"] != [1]):
        return {}
    candidates = {}
    for file in files:
        episodes = file["episodes"]
        if len(episodes) != 1 or type(episodes[0]) is not int or episodes[0] < 1:
            continue
        episode = episodes[0]
        if sum(episode in f["episodes"] for f in files) != 1:
            continue
        assets = db.execute(
            "SELECT * FROM media_assets WHERE monitor_id=? AND upper(torrent_hash)=? AND status='downloaded' AND inventory_verified_at IS NOT NULL",
            (monitor["id"], file["torrent_hash"]),
        ).fetchall()
        published = [a for a in titles[0]["assets"] if a["torrent_hash"] == file["torrent_hash"]]
        if len(assets) != 1 or len(published) != 1:
            continue
        asset = assets[0]
        if (
            asset["inventory_fingerprint"] != file["inventory_generation"]
            or quality_publication.source_generation(db, asset["id"]) != file["source_generation"]
        ):
            continue
        if PurePosixPath(file["source"]).suffix.lower() not in {
            ".mkv",
            ".mp4",
            ".avi",
            ".m4v",
            ".mov",
            ".webm",
            ".ts",
        }:
            continue
        identity = (
            publication_identity(
                bindings[0], asset["id"], file, published[0].get("language_contract")
            )
            if bindings
            else None
        )
        candidates[0 if film else episode] = identity
    return candidates


def attach_navigation(db, cards, *, user_id, library_scope=None):
    """Attach actor-owned partial activity even when the player flag is off."""
    try:
        scope = library_scope or households.selected_scope(db, viewer_id=user_id)
        if scope.viewer_id != user_id:
            return cards
        households.revalidate_scope(db, scope)
    except households.HouseholdError:
        return cards
    # Share the qualified publication only within one SQLite read snapshot.
    # Actual playback still resolves and revalidates independently on each request.
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute("BEGIN")
    try:
        return _attach_navigation(db, cards, user_id, scope)
    finally:
        if own_snapshot:
            db.rollback()


def _attach_navigation(db, cards, user_id, scope):
    from functools import lru_cache

    from omakase.plus.debrid_anime import anime_episodes
    from omakase.plus.debrid_library import account_token, playback_provider

    @lru_cache(maxsize=1)
    def publication():
        try:
            return qualified_publication(db)
        except (ValueError, KeyError, TypeError, RecursionError):
            return None

    player_enabled = enabled()
    debrid_only = scope.kind == "personal" and playback_provider(db, user_id) == "realdebrid"
    debrid_connected = debrid_only and bool(account_token(db, user_id))
    for card in cards:
        activity = db.execute(
            "SELECT * FROM web_playback_watch WHERE user_id=? AND library_id=? AND anilist_id=? AND season_number=? AND covered_seconds>0 ORDER BY last_played_at DESC,id DESC",
            (user_id, scope.library_id, card["anilist_id"], card["season_number"]),
        ).fetchall()
        barrier = watch_barrier(
            db, user_id=user_id, anilist_id=card["anilist_id"], season_number=card["season_number"]
        )
        completed = {
            row[0]
            for row in db.execute(
                "SELECT episode_number FROM web_playback_completions WHERE user_id=? AND anilist_id=? AND season_number=? AND barrier=?",
                (user_id, card["anilist_id"], card["season_number"], barrier),
            )
        }
        partial = []
        for watch in activity:
            if watch["episode_number"] in completed:
                continue
            cycle = db.execute(
                "SELECT c.intervals_json FROM web_playback_cycle_coverage c JOIN web_playback_resumes r USING(target_key) WHERE r.user_id=? AND r.library_id=? AND r.domain='anime' AND r.canonical_id=? AND r.season_number=? AND r.episode_number=? AND r.media_version=? AND c.barrier=?",
                (
                    user_id,
                    scope.library_id,
                    str(card["anilist_id"]),
                    card["season_number"],
                    watch["episode_number"],
                    watch["media_version"],
                    barrier,
                ),
            ).fetchone()
            fresh = bool(cycle and any(end > start for start, end in json.loads(cycle[0])))
            if fresh or (barrier == 0 and watch["completed_at"] is None):
                partial.append(watch)
        card["partial_playback"] = bool(partial) and card.get("watch_status") != "completed"
        if card["partial_playback"]:
            card["lifecycle"] = "in_progress"
        if not player_enabled:
            continue
        card.update(
            play_url=None,
            episode_play_urls={},
            playback_navigation_state="Current published episode unavailable",
        )
        try:
            monitor = _monitor(db, scope, card)
            if monitor is None or card.get("state") == "archived":
                continue
            if debrid_only:
                candidates = (
                    dict.fromkeys(anime_episodes(db, scope, monitor["id"]))
                    if debrid_connected
                    else {}
                )
            else:
                candidates = _candidates(db, scope, monitor, publication)
            if 0 in candidates:
                card.update(
                    play_url=_url(scope, monitor["id"], 0),
                    play_label="Play",
                    play_episode=None,
                    playback_navigation_state="",
                )
                continue
            card["episode_play_urls"] = {ep: _url(scope, monitor["id"], ep) for ep in candidates}
            episode = card.get("next_episode") or 1
            resumed = False
            resume_updated = 0
            for watch in partial:
                ep = watch["episode_number"]
                if ep not in candidates:
                    continue
                # Resume provenance outlives seven-day session cleanup. Missing
                # legacy/unbound proof stays ordinary Play, never guessed Resume.
                proof = db.execute(
                    "SELECT publication_identity,duration_seconds,position_seconds,updated_at,n.watch_barrier "
                    "FROM web_playback_resumes r LEFT JOIN web_playback_native_resumes n USING(target_key) "
                    "WHERE user_id=? AND library_id=? AND domain='anime' AND canonical_id=? AND season_number=? AND episode_number=? AND media_version=?",
                    (
                        user_id,
                        scope.library_id,
                        str(card["anilist_id"]),
                        card["season_number"],
                        ep,
                        watch["media_version"],
                    ),
                ).fetchone()
                if (
                    candidates[ep] is None
                    or not proof
                    or proof[0] != candidates[ep]
                    or proof[4] not in (None, barrier)
                ):
                    continue
                episode, resumed = ep, True
                card["episode_resume"] = episode_resume(proof[2], proof[1])
                resume_updated = proof[3]
                break
            if card.get("watch_status") != "completed" and candidates:
                native_resumes = db.execute(
                    """SELECT episode_number,publication_identity,position_seconds,duration_seconds,updated_at
                       FROM web_playback_resumes r JOIN web_playback_native_resumes n USING(target_key)
                       WHERE user_id=? AND library_id=? AND domain='anime'
                         AND canonical_id=? AND season_number=? AND n.watch_barrier=?
                         AND episode_number>? AND position_seconds>0 AND position_seconds<duration_seconds
                         AND updated_at>=? ORDER BY updated_at DESC LIMIT 100""",
                    (
                        user_id,
                        scope.library_id,
                        str(card["anilist_id"]),
                        card["season_number"],
                        barrier,
                        card.get("progress_episodes") or 0,
                        resume_updated,
                    ),
                )
                for ep, identity, position, duration, updated_at in native_resumes:
                    if ep in completed or not identity or candidates.get(ep) != identity:
                        continue
                    episode, resumed = ep, True
                    card.update(
                        episode_resume=episode_resume(position, duration),
                        native_resume_at=datetime.fromtimestamp(updated_at, UTC).isoformat(),
                        lifecycle="in_progress",
                        lifecycle_label="In progress",
                        partial_playback=True,
                    )
                    break
            if episode in candidates:
                card.update(
                    play_url=_url(scope, monitor["id"], episode),
                    play_label="Resume" if resumed else "Play",
                    play_episode=episode,
                    playback_navigation_state="",
                )
        except (ValueError, KeyError, TypeError, RecursionError):
            # Corrupt or stale publication remains an overview, never a capability.
            continue
    return cards


def episode_resume(position, duration):
    """A saved playhead can describe resume, never completion or coverage."""
    if not 0 < position < duration:
        return None
    return {
        "percent": min(99, max(1, round(100 * position / duration))),
        "minutes_left": math.ceil((duration - position) / 60),
    }


def exact_next_url(db, *, source, monitor_id):
    """Explicit next click only; no acquisition, cross-season or alternate title."""
    if not enabled():
        return None
    try:
        scope = source._scope
        households.revalidate_scope(db, scope)
        if source.actor_user_id != scope.viewer_id or source.library_id != scope.library_id:
            return None
        monitor = db.execute(
            "SELECT * FROM media_monitors WHERE id=? AND user_id=? AND anilist_id=? AND season_number=?",
            (monitor_id, scope.operational_owner_id, source.anilist_id, source.season_number),
        ).fetchone()
        if monitor is None:
            return None
        episode = source.episode_number + 1
        candidates = _candidates(db, scope, monitor)
        return _url(scope, monitor_id, episode) if episode in candidates else None
    except (ValueError, KeyError, TypeError, RecursionError):
        return None
