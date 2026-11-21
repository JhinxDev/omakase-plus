"""Owner-scoped TV submissions and bounded Riven status snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from omakase.plus import tv_fallback
from omakase.plus.acquisition_policy import load_user_acquisition_policy
from omakase.plus.btih import normalize_btih
from omakase.plus.debrid_catalog import (
    PublishedEpisode,
    _published_episodes,
    published_name,
    tv_episode_ids,
)
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
_RIVEN_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
_PUBLICATION_BATCH = 8
_INDEXED_GRACE = timedelta(hours=2)
_FALLBACK_REPEAT = timedelta(hours=12)


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


async def _library_observation(
    db, user_id, tmdb_id, service, cloud_library, expected, published=None
):
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
                published=published,
            )
        except DebridLibraryError:
            return None
        available = observed.episodes
    else:
        available = _plex_availability(db, user_id, tmdb_id, binding)
    return {
        "provider": provider,
        "binding": binding,
        "episodes": sorted(expected & available),
        "partial_seasons": list(observed.partial_seasons) if provider == "Real-Debrid" else [],
    }


def project_library(db, user_id, service, progress, tmdb_id=None):
    """Expose last verified episode matches for the currently selected account."""
    if progress is None or not progress["snapshot"]:
        return progress
    snapshot = dict(progress["snapshot"])
    library = snapshot.pop("library", None)
    snapshot.pop("published_files", None)
    search = snapshot.pop("fallback_checks", {}).get("episode")
    if search:
        snapshot["fallback_episode"] = search
    identity = source_binding(db, user_id, service)
    transfers = (
        tv_fallback.receipts(db, user_id, tmdb_id, identity[1])
        if identity and identity[0] == "Real-Debrid" and tmdb_id
        else {}
    )
    snapshot["seasons"] = [
        {
            **row,
            "episodes": [
                {
                    "episode": episode["episode"],
                    "status": tv_fallback.LABELS.get(
                        transfers.get((row["season"], episode["episode"])), episode["status"]
                    ),
                }
                for episode in row["episodes"]
            ],
        }
        for row in snapshot.get("seasons", ())
    ]
    if library and (library.get("provider"), library.get("binding")) == identity:
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
    serialized = json.dumps(
        {key: value for key, value in snapshot.items() if key != "fallback_checks"}, sort_keys=True
    )
    db.execute(
        """UPDATE tv_provider_requests AS r SET checked_at=?,attempted_at=?,
             changed_at=CASE WHEN json_remove(r.snapshot_json,'$.fallback_checks') IS NOT json(?)
                        THEN ? ELSE changed_at END,
             snapshot_json=json_set(?,'$.fallback_checks',
                                    COALESCE(json_extract(r.snapshot_json,'$.fallback_checks'),json('{}')))
           WHERE user_id=? AND title_id=?""",
        (checked, checked, serialized, checked, serialized, user_id, tmdb_id),
    )
    db.commit()


async def _refresh_cloud_during_outage(db, user_id, tmdb_id, service, library, existing, metadata):
    if library is None or not existing or not existing["snapshot"]:
        return
    prior = existing["snapshot"].get("library") or {}
    identity = source_binding(db, user_id, service)
    if not identity or identity[0] != "Real-Debrid":
        return
    if identity != (prior.get("provider"), prior.get("binding")):
        return
    expected = expected_episodes(metadata, existing["seasons"])
    observation = await _library_observation(db, user_id, tmdb_id, service, library, expected)
    if not observation or source_binding(db, user_id, service) != identity:
        return
    saved = {tuple(episode) for episode in prior["episodes"]} & expected
    matched = set(observation["episodes"])
    if matched <= saved:
        return
    observation["episodes"] = sorted(saved | matched)
    # Refresh the cloud match while keeping the last Riven check and search state.
    db.execute(
        """UPDATE tv_provider_requests
           SET snapshot_json=json_set(snapshot_json,'$.library',json(?)),changed_at=?
           WHERE user_id=? AND title_id=? AND checked_at IS ?
             AND json_extract(snapshot_json,'$.library')=json(?)""",
        (
            json.dumps(observation),
            _stamp(),
            user_id,
            tmdb_id,
            existing["checked_at"],
            json.dumps(prior),
        ),
    )
    db.commit()


def _provider_time(value):
    try:
        date = datetime.fromisoformat(value) if isinstance(value, str) and len(value) < 48 else None
    except ValueError:
        return None
    return date.replace(tzinfo=UTC) if date is not None and date.tzinfo is None else date


def _fallback_eligible(episode, label, now):
    aired = _provider_time(episode.get("aired_at"))
    if aired is None or aired > now:
        return False
    if label in FALLBACK_STATES:
        return True
    searched = _provider_time(episode.get("scraped_at"))
    return bool(
        label == "Looking for episodes"
        and (_number(episode.get("scraped_times"), 10000) or 0) >= 2
        and searched is not None
        and searched <= now - _INDEXED_GRACE
    )


def _publication(episode, season, number):
    identity = episode.get("id")
    folder = published_name(episode.get("folder"))
    filename = published_name(episode.get("file"))
    if (
        episode.get("symlinked") is True
        and episode.get("season_number") == season
        and episode.get("episode_number") == number
        and isinstance(identity, str)
        and _RIVEN_FILE_ID.fullmatch(identity)
        and folder
        and filename
    ):
        return identity, folder, filename
    return None


async def _recover_publications(
    db, user_id, tmdb_id, service, cloud_library, baseline, publications, previous, expected
):
    """Reconcile Riven's exact published file against a release in this Real-Debrid account."""
    token = account_token(db, user_id)
    binding = account_binding(token)
    if not token or not baseline or baseline["provider"] != "Real-Debrid":
        return {}, None
    scope = personal_scope(db, user_id)
    current = _published_episodes(db, scope, str(tmdb_id), binding)
    pairs = Counter((folder, filename) for _, folder, filename in publications.values())
    choices = {
        key: claim
        for key, claim in publications.items()
        if key in expected and pairs[claim[1:]] == 1
    }
    current = {
        key: hint
        for key, hint in current.items()
        if key in choices and choices[key][1:] == (hint.folder, hint.filename)
    }
    ready = {tuple(episode) for episode in baseline["episodes"]}
    if not choices.keys() - ready - current.keys():
        return current, None
    try:
        torrents, _ = await cloud_library.inventory(token)
    except DebridLibraryError:
        return current, None
    hashes = {}
    for item in torrents.values():
        if item.get("status") == "downloaded" and (value := normalize_btih(item.get("hash"))):
            hashes.setdefault(item.get("filename"), set()).add(value)
    waiting = [
        key for key in sorted(choices.keys() - ready - current.keys()) if choices[key][1] in hashes
    ]
    old = previous.get("published_files") or {}
    after = tuple(old.get("cursor") or ()) if old.get("binding") == binding else ()
    batch = sorted(waiting, key=lambda key: (key <= after, key))[:_PUBLICATION_BATCH]

    async def verify(key):
        identity, folder, filename = choices[key]
        try:
            data = await service._json("riven", f"/api/v1/items/{identity}/streams")
        except MovieError:
            return None
        streams = data.get("streams") if isinstance(data, dict) else None
        if not isinstance(streams, list):
            return None
        matched = {
            normalize_btih(row.get("infohash")) for row in streams if isinstance(row, dict)
        } & hashes[folder]
        if len(matched) == 1:
            return PublishedEpisode(matched.pop(), folder, filename)
        return None

    if batch:
        try:
            async with asyncio.timeout(2):
                verified = await asyncio.gather(*(verify(key) for key in batch))
        except TimeoutError:
            return current, after
        current.update({key: hint for key, hint in zip(batch, verified, strict=True) if hint})
    return current, batch[-1] if batch else None


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
    by_season, publications, remaining = {}, {}, MAX_EPISODES
    now = datetime.now(UTC)
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
            episodes[n] = {
                "episode": n,
                "status": label,
                "fallback_eligible": _fallback_eligible(episode, label, now),
            }
            if metadata.get("imdb_id") and (publication := _publication(episode, number, n)):
                publications[number, n] = publication
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
        "publications": publications,
    }


async def refresh(db, user_id, tmdb_id, service, *, submit=False, cloud_library=None):
    """Check the exact show. A write requires explicit intake and a reserved receipt."""
    metadata = service._raw(user_id, tmdb_id)
    if metadata is None:
        metadata = await service._metadata_tv(tmdb_id)
        service._save(user_id, metadata)
    existing = read(db, user_id, tmdb_id)
    seasons = existing["seasons"] if existing else exact_seasons(metadata)
    try:
        snapshot = await inspect_riven(service, tmdb_id, metadata, seasons)
    except MovieError as error:
        if error.code in {"transport_error", "transport_timeout"} or (
            error.code == "backend_unavailable" and (error.http_status or 0) >= 500
        ):
            await _refresh_cloud_during_outage(
                db, user_id, tmdb_id, service, cloud_library, existing, metadata
            )
        raise
    if snapshot is not None:
        if existing is None:
            remember(db, user_id, tmdb_id, seasons, submission="observed")
        expected = expected_episodes(metadata, seasons)
        previous = (existing or {}).get("snapshot") or {}
        publications = snapshot.pop("publications")
        snapshot["expected"] = len(expected)
        snapshot["library"] = await _library_observation(
            db, user_id, tmdb_id, service, cloud_library, expected
        )
        if snapshot["library"] and snapshot["library"]["provider"] == "Real-Debrid":
            published, cursor = await _recover_publications(
                db,
                user_id,
                tmdb_id,
                service,
                cloud_library,
                snapshot["library"],
                publications,
                previous,
                expected,
            )
            prior = previous.get("published_files") or {}
            receipt = {
                "binding": snapshot["library"]["binding"],
                "cursor": cursor,
                "episodes": [
                    (s, e, hint.infohash, hint.folder, hint.filename)
                    for (s, e), hint in sorted(published.items())
                ],
            }
            if json.dumps(receipt["episodes"]) != json.dumps(prior.get("episodes", [])):
                snapshot["library"] = await _library_observation(
                    db, user_id, tmdb_id, service, cloud_library, expected, published
                )
            snapshot["published_files"] = receipt
        _write_snapshot(db, user_id, tmdb_id, snapshot)
        return True
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


async def search_missing_episode(db, user_id, tmdb_id, service, snapshot):
    """Give one aired episode a bounded fallback search after Riven has had a chance."""
    from omakase.plus import request_candidates

    if not request_candidates.search_enabled() or not snapshot:
        return
    metadata = service._raw(user_id, tmdb_id)
    if not metadata:
        return
    library = snapshot.get("library") or {}
    if not library:
        return
    if await tv_fallback.advance_pending(db, user_id, tmdb_id):
        return
    checks = snapshot.get("fallback_checks") or {}
    now = datetime.now(UTC)
    latest = _provider_time(checks.get("latest"))
    if latest is not None and latest > now - timedelta(minutes=10):
        return
    ready = {tuple(episode) for episode in library.get("episodes", ())}
    partial = set(library.get("partial_seasons", ()))
    recent = {
        key: value
        for key, value in checks.get("episodes", {}).items()
        if (checked := _provider_time(value)) and checked > now - _FALLBACK_REPEAT
    }
    waiting = [
        (row["season"], episode["episode"])
        for row in snapshot["seasons"]
        if row["season"] not in partial
        for episode in row["episodes"]
        if episode.get("fallback_eligible")
        and (row["season"], episode["episode"]) not in ready
        and f"{row['season']}:{episode['episode']}" not in recent
    ]
    if not waiting:
        return
    waiting.sort(key=lambda item: (item[1], item[0]))
    last = checks.get("episode")
    cursor = (0, 0)
    if (
        isinstance(last, list)
        and len(last) == 2
        and _number(last[0], 200)
        and _number(last[1], 1000)
    ):
        cursor = (last[1], last[0])
    # An early episode's cooldown can expire before a long request reaches its tail.
    season, episode = next(((s, e) for s, e in waiting if (e, s) > cursor), waiting[0])
    timestamp = now.isoformat()
    reservation = {"latest": timestamp, "episodes": recent, "episode": checks.get("episode")}
    reserved = db.execute(
        """UPDATE tv_provider_requests SET snapshot_json=json_set(snapshot_json,'$.fallback_checks',json(?))
           WHERE user_id=? AND title_id=?
             AND json_extract(snapshot_json,'$.fallback_checks.latest') IS ?""",
        (json.dumps(reservation), user_id, tmdb_id, checks.get("latest")),
    ).rowcount
    db.commit()
    if not reserved:
        return

    async def on_matches(eligible):
        if library["provider"] == "Real-Debrid":
            if (
                request_candidates.acquire_enabled()
                and not db.execute(
                    """SELECT 1 FROM tv_episodes WHERE user_id=? AND tmdb_id=?
                   AND season_number=? AND episode_number=?""",
                    (user_id, tmdb_id, season, episode),
                ).fetchone()
            ):
                from omakase.plus.tv_service import TVError

                try:
                    async with asyncio.timeout(3):
                        await service.season(user_id, tmdb_id, season)
                except (MovieError, TVError, TimeoutError):
                    return
            await tv_fallback.submit(db, user_id, tmdb_id, season, episode, eligible)

    policy = (
        load_user_acquisition_policy(db, user_id) if library["provider"] == "Real-Debrid" else None
    )
    result = await request_candidates.refresh(
        db,
        user_id,
        "tv",
        str(tmdb_id),
        request_candidates.JudgeRequest(
            title=metadata["title"],
            kind="tv",
            season=season,
            episode=episode,
            allowed_resolutions=policy.allowed_resolutions if policy else (),
            minimum_seeders=policy.minimum_seeders if policy else 1,
            maximum_bytes=policy.max_release_size_bytes if policy else 0,
        ),
        force=True,
        timeout=5,
        on_matches=on_matches,
    )
    if result.get("outcome") in {"complete", "partial"}:
        recent[f"{season}:{episode}"] = timestamp
    completed = {"latest": timestamp, "episodes": recent, "episode": [season, episode]}
    db.execute(
        """UPDATE tv_provider_requests SET snapshot_json=json_set(snapshot_json,'$.fallback_checks',json(?))
           WHERE user_id=? AND title_id=?
             AND json_extract(snapshot_json,'$.fallback_checks.latest')=?""",
        (json.dumps(completed), user_id, tmdb_id, timestamp),
    )
    db.commit()


async def refresh_pending(db, *, heartbeat):
    """Read a small batch and advance separately opted-in Real-Debrid transfers."""
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
            async with asyncio.timeout(26):
                if await refresh(db, user_id, tmdb_id, service, cloud_library=library):
                    current = read(db, user_id, tmdb_id)
                    if current is None:
                        continue
                    await search_missing_episode(db, user_id, tmdb_id, service, current["snapshot"])
        except (TVError, MovieError, TimeoutError):
            continue
