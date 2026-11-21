"""Owner-scoped TV submissions and bounded Riven status snapshots."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime

from omakase.plus.debrid_catalog import tv_episode_ids
from omakase.plus.debrid_library import DebridLibraryError, account_binding, account_token
from omakase.plus.movies import MovieError
from omakase.plus.providers import playback
from omakase.plus.web_playback_catalog import personal_scope

MAX_EPISODES = 500

RIVEN_STATES = {
    "requested": "Queued",
    "indexed": "Looking for episodes",
    "scraped": "Checking releases",
    "downloading": "Transferring",
    "downloaded": "Transfer reported",
    "symlinked": "Sent to the library",
    "completed": "Sent to the library",
    "partiallycompleted": "Some episodes sent to the library",
    "failed": "Needs attention",
    "no streams": "No release found",
    "no_streams": "No release found",
    "nostreams": "No release found",
    "not_found": "Waiting for Riven",
    "unknown": "Checking availability",
}

FALLBACK_STATES = {"No release found", "Needs attention"}


def enabled():
    return os.getenv("OMAKASE_TV_REQUEST_RIVEN", "").strip().casefold() in {"true", "1", "yes"}


def _stamp():
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value, limit):
    if type(value) is int and 1 <= value <= limit:
        return value
    return None


def exact_seasons(metadata):
    return sorted(
        {
            number
            for row in metadata.get("seasons", ())
            if row.get("released") and (number := _number(row.get("season_number"), 200))
        }
    )


def expected_episodes(metadata, seasons):
    counts = {
        row["season_number"]: row.get("episode_count")
        for row in metadata.get("seasons", ())
        if row["season_number"] in seasons
    }
    if not counts or counts.keys() != set(seasons):
        return set()
    if any(_number(count, 1000) is None for count in counts.values()):
        return set()
    if sum(counts.values()) > MAX_EPISODES:
        return set()
    return {(season, ep) for season, count in counts.items() for ep in range(1, count + 1)}


def source_binding(db, user_id, service):
    if playback.select(db, user_id).cloud_route:
        token = account_token(db, user_id)
        return ("Real-Debrid", account_binding(token)) if token else None
    return ("Plex", service._plex_scope()) if service.connections.enabled else None


def _plex_availability(db, user_id, tmdb_id, binding):
    return {
        (row[0], row[1])
        for row in db.execute(
            """SELECT e.season_number,e.episode_number FROM tv_episodes e
               JOIN tv_plex_snapshots s USING(user_id,tmdb_id)
               WHERE e.user_id=? AND e.tmdb_id=? AND s.source_scope=? AND e.plex_present=1""",
            (user_id, tmdb_id, binding),
        )
    }


async def _library_observation(db, user_id, tmdb_id, service, cloud_library, expected):
    identity = source_binding(db, user_id, service)
    if not identity or not expected:
        return None
    provider, binding = identity
    if provider == "Real-Debrid":
        if cloud_library is None:
            return None
        try:
            observed = await tv_episode_ids(
                db,
                personal_scope(db, user_id),
                str(tmdb_id),
                cloud_library,
                seasons=sorted({season for season, _ in expected}),
                timeout=4,
            )
        except DebridLibraryError:
            return None
        available = observed.episodes
    else:
        available = _plex_availability(db, user_id, tmdb_id, binding)
    return {"provider": provider, "binding": binding, "episodes": sorted(expected & available)}


def project_library(db, user_id, service, progress):
    """Expose last verified episode matches for the currently selected account."""
    if progress is None or not progress["snapshot"]:
        return progress
    snapshot = dict(progress["snapshot"])
    library = snapshot.pop("library", None)
    if library and (library.get("provider"), library.get("binding")) == source_binding(
        db, user_id, service
    ):
        ready = {tuple(row) for row in library["episodes"]}
        stored = {row["season"]: row["episodes"] for row in snapshot["seasons"]}
        for season, episode in sorted(ready):
            entries = stored.setdefault(season, [])
            if not any(row["episode"] == episode for row in entries):
                entries.append({"episode": episode, "status": "Awaiting Riven update"})
        snapshot["seasons"] = [
            {
                "season": season,
                "ready": sum((season, row["episode"]) in ready for row in rows),
                "episodes": [
                    {**row, "available": (season, row["episode"]) in ready}
                    for row in sorted(rows, key=lambda row: row["episode"])
                ],
            }
            for season, rows in sorted(stored.items())
        ]
        snapshot["ready"] = len(ready)
        snapshot["library_name"] = library["provider"]
    return {**progress, "snapshot": snapshot}


def read(db, user_id, tmdb_id):
    row = db.execute(
        """SELECT origin, submission, seasons_json, checked_at, snapshot_json, changed_at
           FROM tv_provider_requests WHERE user_id=? AND title_id=?""",
        (user_id, tmdb_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "origin": row[0],
        "submission": row[1],
        "seasons": json.loads(row[2]),
        "checked_at": row[3],
        "snapshot": json.loads(row[4]) if row[4] else None,
        "changed_at": row[5],
    }


def remember(db, user_id, tmdb_id, seasons, *, origin="viewer", submission="accepted"):
    inserted = db.execute(
        """INSERT INTO tv_provider_requests (user_id,title_id,origin,submission,seasons_json)
           VALUES (?,?,?,?,?) ON CONFLICT(user_id,title_id) DO NOTHING""",
        (user_id, tmdb_id, origin, submission, json.dumps(seasons)),
    ).rowcount
    db.commit()
    return inserted == 1


def _write_snapshot(db, user_id, tmdb_id, snapshot):
    checked = _stamp()
    serialized = json.dumps(snapshot, sort_keys=True)
    db.execute(
        """UPDATE tv_provider_requests SET checked_at=?,attempted_at=?,
             changed_at=CASE WHEN snapshot_json IS NOT ? THEN ? ELSE changed_at END,
             snapshot_json=?
           WHERE user_id=? AND title_id=?""",
        (checked, checked, serialized, checked, serialized, user_id, tmdb_id),
    )
    db.commit()


async def inspect_riven(service, tmdb_id, metadata, seasons):
    try:
        data = await service._json(
            "riven", f"/api/v1/items/{tmdb_id}", params={"use_tmdb_id": "true"}
        )
    except MovieError as error:
        if error.http_status == 404:
            return None
        raise
    if (
        not isinstance(data, dict)
        or str(data.get("tmdb_id")) != str(tmdb_id)
        or str(data.get("type", "")).casefold() != "show"
        or metadata.get("imdb_id")
        and data.get("imdb_id") != metadata["imdb_id"]
    ):
        raise MovieError("invalid_metadata")
    by_season, remaining = {}, MAX_EPISODES
    for season in data.get("seasons") or ():
        if not isinstance(season, dict):
            continue
        number = _number(season.get("number") or season.get("season_number"), 200)
        if number not in seasons or number in by_season:
            continue
        episodes = {}
        for episode in season.get("episodes") or ():
            if not remaining:
                break
            if not isinstance(episode, dict):
                continue
            n = _number(episode.get("number") or episode.get("episode_number"), 1000)
            if n is None or n in episodes:
                continue
            label = RIVEN_STATES.get(
                str(episode.get("state", "")).strip().casefold(), "Awaiting update"
            )
            episodes[n] = {"episode": n, "status": label}
            remaining -= 1
        by_season[number] = {
            "season": number,
            "episodes": [episodes[key] for key in sorted(episodes)],
        }
    return {
        "status": RIVEN_STATES.get(
            str(data.get("state", "")).strip().casefold(), "Riven is checking"
        ),
        "seasons": [by_season[key] for key in sorted(by_season)],
    }


async def refresh(db, user_id, tmdb_id, service, *, submit=False, cloud_library=None):
    """Check the exact show. A write requires explicit intake and a reserved receipt."""
    metadata = service._raw(user_id, tmdb_id)
    if metadata is None:
        metadata = await service._metadata_tv(tmdb_id)
        service._save(user_id, metadata)
    existing = read(db, user_id, tmdb_id)
    seasons = existing["seasons"] if existing else exact_seasons(metadata)
    snapshot = await inspect_riven(service, tmdb_id, metadata, seasons)
    if snapshot is not None:
        if existing is None:
            remember(db, user_id, tmdb_id, seasons, submission="observed")
        expected = expected_episodes(metadata, seasons)
        snapshot["expected"] = len(expected)
        snapshot["library"] = await _library_observation(
            db, user_id, tmdb_id, service, cloud_library, expected
        )
        _write_snapshot(db, user_id, tmdb_id, snapshot)
        return
    if not submit or existing is not None or not seasons or not metadata.get("imdb_id"):
        return
    if not remember(db, user_id, tmdb_id, seasons, submission="sending"):
        return
    outcome = "unconfirmed"
    try:
        body = await service._bytes(
            "riven", "POST", "/api/v1/items/add", params={"imdb_ids": metadata["imdb_id"]}
        )
        if json.loads(body).get("message") == "Added 1 item(s) to the queue":
            outcome = "accepted"
    except (MovieError, ValueError, AttributeError):
        pass
    db.execute(
        "UPDATE tv_provider_requests SET submission=? WHERE user_id=? AND title_id=? AND submission='sending'",
        (outcome, user_id, tmdb_id),
    )
    db.commit()


async def search_missing_episode(db, user_id, tmdb_id, metadata, snapshot):
    """Search one exact episode when Riven confirms it could not find a release."""
    from omakase.plus import request_candidates

    if not request_candidates.search_enabled() or not metadata or not snapshot:
        return
    ready = {tuple(episode) for episode in (snapshot.get("library") or {}).get("episodes", ())}
    missing = next(
        (
            (row["season"], ep["episode"])
            for row in snapshot["seasons"]
            for ep in row["episodes"]
            if ep["status"] in FALLBACK_STATES and (row["season"], ep["episode"]) not in ready
        ),
        None,
    )
    if missing is None:
        return
    season, episode = missing
    await request_candidates.refresh(
        db,
        user_id,
        "tv",
        str(tmdb_id),
        request_candidates.JudgeRequest(
            title=metadata["title"],
            kind="tv",
            season=season,
            episode=episode,
        ),
    )


async def refresh_pending(db, *, heartbeat):
    """Refresh a small batch per scheduler run using status reads only."""
    if not enabled():
        return
    from omakase.plus.connections import movie_connections
    from omakase.plus.debrid_library import DebridLibrary
    from omakase.plus.tv_service import TVError, TVService

    rows = db.execute(
        """SELECT user_id,title_id FROM tv_provider_requests
           WHERE attempted_at IS NULL
              OR attempted_at<strftime('%Y-%m-%dT%H:%M:%SZ','now','-4 minutes')
           ORDER BY attempted_at,title_id LIMIT 4"""
    ).fetchall()
    library = DebridLibrary()
    for user_id, tmdb_id in rows:
        heartbeat()
        service = TVService(db, movie_connections(db, user_id))
        db.execute(
            "UPDATE tv_provider_requests SET attempted_at=? WHERE user_id=? AND title_id=?",
            (_stamp(), user_id, tmdb_id),
        )
        db.commit()
        try:
            async with asyncio.timeout(12):
                await refresh(db, user_id, tmdb_id, service, cloud_library=library)
                current = read(db, user_id, tmdb_id)
                if current:
                    await search_missing_episode(
                        db, user_id, tmdb_id, service._raw(user_id, tmdb_id), current["snapshot"]
                    )
        except (TVError, MovieError, TimeoutError):
            continue
