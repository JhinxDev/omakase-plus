"""Read-only playback navigation candidates; only the Play POST grants playback."""

from __future__ import annotations

import json
import os
from pathlib import PurePosixPath
from urllib.parse import urlencode

from omakase.plus import households, quality_publication
from omakase.plus.web_playback_source import PlaybackSourceError, _publication
from omakase.plus.web_playback_store import watch_barrier


def enabled():
    return all(
        os.getenv(name, "").strip().lower() in {"true", "1", "yes", "on"}
        for name in ("OMAKASE_PLUS_PRIVATE", "OMAKASE_PLUS_WEB_PLAYER_ENABLED")
    )


def _url(scope, monitor_id, episode):
    return f"/plus/watch/{int(monitor_id)}/{int(episode)}?" + urlencode(
        {"library_id": scope.library_id}
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
        "SELECT * FROM media_monitors WHERE id=? AND user_id=? AND anilist_id=? AND season_number=?",
        (monitor_id, scope.operational_owner_id, card["anilist_id"], card["season_number"]),
    ).fetchone()


def _candidates(db, scope, monitor):
    """Strict current single-file selection, including unbound personal legacy rows.

    Legacy candidates do not create resources or bypass the full source resolver.
    Bound media additionally uses the gateway's publication identity unchanged.
    """
    if int(monitor["season_number"] or 0) <= 0 or (
        "format" in monitor.keys() and str(monitor["format"] or "").upper() == "MOVIE"
    ):
        return {}
    row = db.execute("SELECT receipt_json FROM media_curator_receipt_latest WHERE id=1").fetchone()
    if not row or len(row[0]) > 2 * 1024 * 1024:
        return {}
    receipt = json.loads(row[0])
    if not (
        receipt.get("schema_version") == "curator-receipt-v4"
        and receipt.get("mode") == "live"
        and receipt.get("topology_converged") is True
        and receipt.get("selection_applied") is True
        and receipt.get("outcome") in {"success", "degraded"}
    ):
        return {}
    manifest = quality_publication.validate_current_selection(db, receipt)
    titles = [t for t in manifest["titles"] if t["anilist_id"] == monitor["anilist_id"]]
    if len(titles) != 1:
        return {}
    title = titles[0]["canonical_title"]
    if (
        not isinstance(title, str)
        or title in {"", ".", ".."}
        or any(c in "/\\" or ord(c) < 32 for c in title)
    ):
        return {}
    bindings = db.execute(
        "SELECT i.id FROM library_items i JOIN library_resources r ON r.id=i.resource_id WHERE i.library_id=? AND i.state='active' AND i.domain='anime' AND r.owner_user_id=? AND r.legacy_id=? AND i.canonical_id=? AND i.season_number=? AND r.domain=i.domain AND r.canonical_id=i.canonical_id AND r.season_number=i.season_number",
        (
            scope.library_id,
            scope.operational_owner_id,
            str(monitor["id"]),
            str(monitor["anilist_id"]),
            monitor["season_number"],
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
        and f["season_number"] == monitor["season_number"]
    ]
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
        identity = None
        if bindings:
            try:
                _, _, identity = _publication(
                    db, scope, monitor["anilist_id"], monitor["season_number"], episode
                )
            except PlaybackSourceError:
                continue
        candidates[episode] = identity
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
                "SELECT c.intervals_json FROM web_playback_cycle_coverage c JOIN web_playback_resumes r USING(target_key) WHERE r.user_id=? AND r.library_id=? AND r.canonical_id=? AND r.season_number=? AND r.episode_number=? AND r.media_version=? AND c.barrier=?",
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
        if not enabled():
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
            candidates = _candidates(db, scope, monitor)
            card["episode_play_urls"] = {ep: _url(scope, monitor["id"], ep) for ep in candidates}
            episode = card.get("next_episode") or 1
            resumed = False
            for watch in partial:
                ep = watch["episode_number"]
                if ep not in candidates:
                    continue
                # Resume provenance outlives seven-day session cleanup. Missing
                # legacy/unbound proof stays ordinary Play, never guessed Resume.
                proof = db.execute(
                    "SELECT publication_identity FROM web_playback_resumes WHERE user_id=? AND library_id=? AND domain='anime' AND canonical_id=? AND season_number=? AND episode_number=? AND media_version=?",
                    (
                        user_id,
                        scope.library_id,
                        str(card["anilist_id"]),
                        card["season_number"],
                        ep,
                        watch["media_version"],
                    ),
                ).fetchone()
                if candidates[ep] is None or not proof or proof[0] != candidates[ep]:
                    continue
                episode, resumed = ep, True
                break
            if episode in candidates:
                card.update(
                    play_url=_url(scope, monitor["id"], episode),
                    play_label="Resume" if resumed else "Play",
                    playback_navigation_state="",
                )
        except (ValueError, KeyError, TypeError, RecursionError):
            # Corrupt or stale publication remains an overview, never a capability.
            continue
    return cards


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
