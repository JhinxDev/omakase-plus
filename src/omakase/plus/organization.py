"""Plex viewing shelves derived from verified media and local watch progress."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from omakase.plus.media_store import covered_episodes
from omakase.plus.product_views import library_titles


def fresh_plex_evidence(db, *, now: datetime | None = None) -> bool:
    row = db.execute(
        "SELECT completed_at FROM media_curator_receipt_latest WHERE id = 1"
    ).fetchone()
    if row is None:
        return False
    try:
        observed = datetime.strptime(row["completed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        # The native curator runs every 15 minutes. Allow two cycles plus five
        # minutes of execution time so shelves do not flicker at timer boundaries.
        return -60 <= age <= 2100
    except (ValueError, TypeError):
        return False


def build_plex_organization(db) -> dict:
    """Expose canonical identities and shelves to the existing private curator.

    This shared Plex library aggregates seasons and household users. No user
    identity, provider locator, credential or taste history leaves the app.
    A Ready shelf requires complete current coverage visible in Plex. Watching
    requires saved progress and some verified media currently visible in Plex.
    """
    grouped: dict[int, dict] = {}
    fresh = fresh_plex_evidence(db)
    users = db.execute("SELECT DISTINCT user_id FROM media_monitors ORDER BY user_id").fetchall()
    for user in users:
        for item in library_titles(db, user_id=int(user["user_id"]), lifecycle="all"):
            row = db.execute(
                "SELECT search_titles_json, season_number FROM media_monitors WHERE id = ?",
                (item["id"],),
            ).fetchone()
            identity = int(item["anilist_id"])
            if identity not in grouped:
                try:
                    aliases = json.loads(row["search_titles_json"] or "[]")
                except (ValueError, TypeError):
                    aliases = []
                grouped[identity] = {
                    "anilist_id": identity,
                    "canonical_title": str(item["title"])[:300],
                    "title_aliases": list(
                        dict.fromkeys(
                            alias[:300]
                            for alias in aliases
                            if isinstance(alias, str) and alias.strip()
                        )
                    )[:50],
                    "season_number": row["season_number"],
                    "shelves": set(),
                }
            if not fresh or item["watch_status"] == "completed" or item["lifecycle"] == "completed":
                continue
            required, usable, visible = item["required"], item["usable"], item["plex_visible"]
            if item["progress_episodes"] > 0 and usable > 0 and visible > 0:
                grouped[identity]["shelves"].add("Watching")
            elif (
                required > 0
                and visible >= required
                and set(range(1, required + 1)) <= covered_episodes(db, item["id"])
            ):
                grouped[identity]["shelves"].add("Ready")
    titles = []
    for identity in sorted(grouped):
        item = grouped[identity]
        item["shelves"] = sorted(item["shelves"])
        titles.append(item)
    if len(titles) > 10_000:
        raise ValueError("Plex organization exceeds its bounded title limit")
    return {
        "schema_version": "plex-organization-v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "titles": titles,
    }
