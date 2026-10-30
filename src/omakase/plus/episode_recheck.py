"""One requested title's upgrade targets, separate from playable coverage."""

from __future__ import annotations

import json
from dataclasses import dataclass

from omakase.plus.episode_quality import episode_qualities
from omakase.plus.media_store import covered_episodes
from omakase.plus.media_types import MediaRecheckIntent


@dataclass(frozen=True)
class RecheckTargets:
    missing: frozenset[int]
    upgrades: frozenset[int]
    pending: frozenset[int]
    downloading: frozenset[int]
    preferred_downloading: frozenset[int]
    upgrade_allowed: bool

    @property
    def search(self):
        return (self.missing - self.downloading) | (
            (self.upgrades - self.pending) if self.upgrade_allowed else frozenset()
        )


def targets(db, monitor, required, policy, intent):
    if (
        not isinstance(intent, MediaRecheckIntent)
        or intent.monitor_id != monitor["id"]
        or intent.actor_user_id != monitor["user_id"]
    ):
        raise ValueError("Episode recheck scope does not match the requesting owner.")
    assets = db.execute(
        "SELECT * FROM media_assets WHERE monitor_id=? ORDER BY id", (monitor["id"],)
    ).fetchall()
    quality = episode_qualities(db, assets)
    missing = frozenset(required - covered_episodes(db, monitor["id"]))
    upgrades = (
        frozenset(
            episode
            for episode, value in quality.items()
            if episode in required and value["quality"] == "720p"
        )
        if intent.upgrade_to_1080
        else frozenset()
    )
    from omakase.plus.quality_publication import current_failed_hashes

    failed = current_failed_hashes(db, monitor["id"])
    pending = set()
    downloading = set()
    preferred_downloading = set()
    for asset in assets:
        if asset["status"] in {"requested", "queued", "waiting_files", "downloading"}:
            downloading.update(json.loads(asset["planned_coverage_json"]))
        if (
            asset["quality"] not in {"1080p", "2160p"}
            or str(asset["torrent_hash"]).upper() in failed
        ):
            continue
        if asset["status"] in {"requested", "queued", "waiting_files", "downloading"}:
            pending.update(json.loads(asset["planned_coverage_json"]))
            preferred_downloading.update(json.loads(asset["planned_coverage_json"]))
        elif asset["status"] == "downloaded" and asset["inventory_verified_at"]:
            pending.update(json.loads(asset["usable_coverage_json"]))
    return RecheckTargets(
        missing,
        upgrades,
        frozenset(pending) & upgrades,
        frozenset(downloading) & required,
        frozenset(preferred_downloading) & upgrades,
        "1080p" in policy.allowed_resolutions,
    )
