"""Advance one exact, owner-authorized TV episode transfer through Real-Debrid."""

from __future__ import annotations

import asyncio
import json
import os
import re

import httpx

from omakase.plus import movie_fallback, request_candidates
from omakase.plus.acquisition_policy import load_user_acquisition_policy
from omakase.plus.btih import normalize_btih, torrent_hash_from_magnet
from omakase.plus.catalog import quality_from_title
from omakase.plus.debrid_catalog import _leaf, _tail, _tv_marker, catalog_title
from omakase.plus.debrid_library import (
    DebridLibrary,
    DebridLibraryError,
    _video,
    account_binding,
    account_token,
)
from omakase.plus.provider_limits import ProviderRateLimit, cooldown_hooks
from omakase.plus.providers import playback
from omakase.plus.realdebrid import RealDebridClient, RealDebridProviderBlock
from omakase.plus.web_playback_catalog import personal_scope

_ACTIVE = ("submitting", "submitted", "selecting", "selected")
_ACTIVE_SQL = ",".join("?" for _ in _ACTIVE)
_ID = re.compile(r"[A-Za-z0-9]{3,64}")
_TRANSIENT = (DebridLibraryError, ProviderRateLimit, httpx.HTTPError, TimeoutError, ValueError)
_BAD_QUALITY = re.compile(
    r"\b(?:cam|hdcam|camrip|hdts|telesync|telecine|dvdscr|screener|sample|trailer)\b",
    re.IGNORECASE,
)
_SEASON_EPISODE = re.compile(r"\bs[0-9]{1,2}[ ._-]?e[0-9]{1,3}\b", re.IGNORECASE)
_SEASON_RANGE = re.compile(r"\bs([0-9]{1,2})\s*(?:-|to|thru)\s*s?([0-9]{1,2})\b", re.IGNORECASE)
_ATTEMPTS = 3
_PACK_ATTEMPTS = 2
_FAILED = {"dead", "error", "magnet_error", "virus"}
_UNCERTAIN = {"submitting", "submission_unknown", "stalled", "downloaded"}

LABELS = {
    **movie_fallback.LABELS,
    "submitted": "Real-Debrid accepted the transfer. Checking the episode file.",
    "selected": "The episode file was selected. Real-Debrid is preparing it.",
    "downloaded": "Real-Debrid reports that the episode file is ready.",
    "file_mismatch": "The release did not contain one unambiguous episode file.",
    "stalled": "Real-Debrid has not confirmed this transfer. It will not be submitted again.",
}


def _account(db, user_id):
    if (
        request_candidates.acquire_enabled()
        and os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").strip().casefold() != "true"
        and playback.select(db, user_id).cloud_route
    ):
        return account_token(db, user_id)
    return None


def _active_row(db, user_id, title_id, binding):
    return db.execute(
        f"""SELECT *, created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-8 hours') AS overdue
            FROM tv_fallback_transfers
            WHERE user_id=? AND title_id=? AND account_binding=? AND state IN ({_ACTIVE_SQL})
            ORDER BY id DESC LIMIT 1""",
        (user_id, title_id, binding, *_ACTIVE),
    ).fetchone()


def _active_pack_row(db, user_id, title_id, binding):
    return db.execute(
        f"""SELECT *, created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-8 hours') AS overdue
            FROM tv_season_pack_transfers
            WHERE user_id=? AND title_id=? AND account_binding=? AND state IN ({_ACTIVE_SQL})
            ORDER BY id DESC LIMIT 1""",
        (user_id, title_id, binding, *_ACTIVE),
    ).fetchone()


def receipts(db, user_id, title_id, binding):
    if not binding:
        return {}
    states = {}
    for row in db.execute(
        """SELECT season,state,selected_episodes_json FROM tv_season_pack_transfers
           WHERE user_id=? AND title_id=? AND account_binding=? ORDER BY id""",
        (user_id, title_id, binding),
    ):
        try:
            episodes = json.loads(row["selected_episodes_json"] or "[]")
        except ValueError:
            episodes = []
        for episode in episodes:
            if type(episode) is int and episode > 0:
                states[(row["season"], episode)] = row["state"]
    for row in db.execute(
        """SELECT season,episode,state FROM tv_fallback_transfers
           WHERE user_id=? AND title_id=? AND account_binding=? ORDER BY id""",
        (user_id, title_id, binding),
    ):
        states[(row["season"], row["episode"])] = row["state"]
    return states


def _set_state(db, row_id, previous, next_state, *, torrent_id=None):
    changed = db.execute(
        """UPDATE tv_fallback_transfers SET state=?,torrent_id=COALESCE(?,torrent_id)
           WHERE id=? AND state=?""",
        (next_state, torrent_id, row_id, previous),
    ).rowcount
    db.commit()
    return changed == 1


def _set_pack_state(db, row_id, previous, next_state, *, torrent_id=None, selected_episodes=None):
    episodes_json = json.dumps(list(selected_episodes)) if selected_episodes is not None else None
    changed = db.execute(
        """UPDATE tv_season_pack_transfers
           SET state=?,
               torrent_id=COALESCE(?,torrent_id),
               selected_episodes_json=COALESCE(?,selected_episodes_json)
           WHERE id=? AND state=?""",
        (next_state, torrent_id, episodes_json, row_id, previous),
    ).rowcount
    db.commit()
    return changed == 1


def _approved(db, user_id, title_id, season, episode, account, title):
    if _account(db, user_id) != account:
        return False
    current = catalog_title(db, personal_scope(db, user_id), "tv", str(title_id), season, episode)
    if title is None or title != current:
        return False
    return (
        db.execute(
            """SELECT 1 FROM tv_provider_requests r, json_each(r.snapshot_json,'$.seasons') s,
                  json_each(s.value,'$.episodes') e
           WHERE r.user_id=? AND r.title_id=?
             AND json_extract(r.snapshot_json,'$.library.provider')='Real-Debrid'
             AND json_extract(r.snapshot_json,'$.library.binding')=?
             AND json_extract(s.value,'$.season')=? AND json_extract(e.value,'$.episode')=?
             AND json_extract(e.value,'$.fallback_eligible')=1
             AND NOT EXISTS (
                 SELECT 1 FROM tv_history h WHERE h.user_id=r.user_id AND h.tmdb_id=r.title_id
                 AND h.status='completed'
             )
             AND NOT EXISTS (
                 SELECT 1 FROM tv_episodes watched WHERE watched.user_id=r.user_id
                 AND watched.tmdb_id=r.title_id AND watched.season_number=?
                 AND watched.episode_number=? AND watched.watched=1
             )
             AND NOT EXISTS (
                 SELECT 1 FROM json_each(r.snapshot_json,'$.library.partial_seasons') partial
                 WHERE partial.value=?
             )
             AND NOT EXISTS (
                 SELECT 1 FROM json_each(r.snapshot_json,'$.library.episodes') ready
                 WHERE json_extract(ready.value,'$[0]')=? AND json_extract(ready.value,'$[1]')=?
             )""",
            (
                user_id,
                title_id,
                account_binding(account),
                season,
                episode,
                season,
                episode,
                season,
                season,
                episode,
            ),
        ).fetchone()
        is not None
    )


def _rank_by_tier(candidates, title, policy, *, predicate):
    allowed_rank = {
        name: len(policy.allowed_resolutions) - index
        for index, name in enumerate(policy.allowed_resolutions)
    }
    fallback_rank = {"720p": 2, "": 1}
    ranked = []
    for item in candidates:
        remaining = " ".join(_tail(item.title, title))
        if not remaining or _BAD_QUALITY.search(remaining):
            continue
        seeders = int(getattr(item, "seeders", 0) or 0)
        size_bytes = int(getattr(item, "size_bytes", 0) or 0)
        if (
            seeders <= 0
            or not 0 < size_bytes <= policy.max_release_size_bytes
            or not predicate(item)
            or not (infohash := torrent_hash_from_magnet(item.magnet))
        ):
            continue
        resolution = quality_from_title(remaining)
        if resolution in allowed_rank and seeders >= policy.minimum_seeders:
            tier, q_rank = 3, allowed_rank[resolution]
        elif resolution in allowed_rank:
            tier, q_rank = 2, allowed_rank[resolution]
        elif resolution in fallback_rank:
            tier, q_rank = 1, fallback_rank[resolution]
        else:
            continue
        ranked.append((tier, q_rank, seeders, infohash))
    return [entry[3] for entry in sorted(ranked, reverse=True)]


def _rank(candidates, title, policy):
    return _rank_by_tier(
        candidates,
        title,
        policy,
        predicate=lambda item: _tv_marker(item.title, title) == (title.season, title.episode),
    )


def _is_season_pack_title(name, title, season):
    tail = _tail(name, title)
    if not tail or {"sample", "trailer"} & set(tail):
        return False
    if isinstance(name, str) and _SEASON_EPISODE.search(name):
        return False
    if _tv_marker(name, title) == (season, 0):
        return True
    if isinstance(name, str) and (rng := _SEASON_RANGE.search(name)):
        return int(rng[1]) <= season <= int(rng[2])
    return False


def _rank_season_pack(candidates, title, season, policy):
    return _rank_by_tier(
        candidates,
        title,
        policy,
        predicate=lambda item: _is_season_pack_title(item.title, title, season),
    )


async def _inspect(db, row, title, provider, authorized, max_bytes):
    row_id, state = row["id"], row["state"]
    if state == "submitting":
        if row["overdue"]:
            _set_state(db, row_id, state, "submission_unknown")
            return False
        return True
    item = await provider.get_torrent(row["torrent_id"], require_definite_absence=True)
    if item is None:
        _set_state(db, row_id, state, "provider_missing")
        return False
    if item.id != row["torrent_id"] or normalize_btih(item.torrent_hash) != row["torrent_hash"]:
        _set_state(db, row_id, state, "file_mismatch")
        return False
    if item.status in _FAILED:
        _set_state(db, row_id, state, "provider_failed")
        return False
    expected = (row["season"], row["episode"])
    videos = [
        part.id
        for part in item.files
        if part.id > 0
        and 0 < part.bytes <= max_bytes
        and _video(_leaf(part.path))
        and _tv_marker(_leaf(part.path), title, from_pack=True) == expected
    ]
    selected = [part.id for part in item.files if part.selected]
    if item.status != "magnet_conversion" and (
        len(videos) != 1 or (selected and selected != videos)
    ):
        _set_state(db, row_id, state, "file_mismatch")
        return False
    if selected and selected == videos and item.status == "downloaded" and item.links:
        _set_state(db, row_id, state, "downloaded")
        return False
    if row["overdue"]:
        _set_state(db, row_id, state, "stalled")
        return False
    if item.status == "waiting_files_selection" and state == "submitted" and authorized():
        if _set_state(db, row_id, state, "selecting"):
            try:
                selected_now = await provider.select_files(item.id, str(videos[0]))
            except RealDebridProviderBlock:
                _set_state(db, row_id, "selecting", "provider_restricted")
                return False
            if selected_now:
                _set_state(db, row_id, "selecting", "selected")
    return True


def _wanted_season_episodes(db, user_id, title_id, season, account):
    row = db.execute(
        "SELECT snapshot_json FROM tv_provider_requests WHERE user_id=? AND title_id=?",
        (user_id, title_id),
    ).fetchone()
    if row is None or not row["snapshot_json"]:
        return set()
    try:
        snapshot = json.loads(row["snapshot_json"])
    except ValueError:
        return set()
    scope = personal_scope(db, user_id)
    ready = {tuple(item) for item in (snapshot.get("library") or {}).get("episodes", ())}
    wanted = set()
    for season_row in snapshot.get("seasons", ()):
        if season_row.get("season") != season:
            continue
        for ep in season_row.get("episodes", ()):
            number = ep.get("episode")
            if (
                type(number) is int
                and number > 0
                and ep.get("fallback_eligible")
                and (season, number) not in ready
            ):
                ep_title = catalog_title(db, scope, "tv", str(title_id), season, number)
                if ep_title is not None and _approved(
                    db, user_id, title_id, season, number, account, ep_title
                ):
                    wanted.add(number)
    return wanted


async def _inspect_pack(db, row, title, provider, wanted_fn, max_bytes):
    row_id, state, season = row["id"], row["state"], row["season"]
    if state == "submitting":
        if row["overdue"]:
            _set_pack_state(db, row_id, state, "submission_unknown")
            return False
        return True
    item = await provider.get_torrent(row["torrent_id"], require_definite_absence=True)
    if item is None:
        _set_pack_state(db, row_id, state, "provider_missing")
        return False
    if item.id != row["torrent_id"] or normalize_btih(item.torrent_hash) != row["torrent_hash"]:
        _set_pack_state(db, row_id, state, "file_mismatch")
        return False
    if item.status in _FAILED:
        _set_pack_state(db, row_id, state, "provider_failed")
        return False
    try:
        saved_episodes = {
            ep
            for ep in json.loads(row["selected_episodes_json"] or "[]")
            if type(ep) is int and ep > 0
        }
    except ValueError:
        saved_episodes = set()
    wanted = saved_episodes or wanted_fn()
    best_by_ep: dict[int, tuple[int, int]] = {}
    for part in item.files:
        if part.id <= 0 or not 0 < part.bytes <= max_bytes:
            continue
        leaf = _leaf(part.path)
        if not _video(leaf):
            continue
        marker = _tv_marker(leaf, title, from_pack=True)
        if not marker or marker[0] != season or marker[1] <= 0:
            continue
        ep = marker[1]
        if wanted and ep not in wanted:
            continue
        prev = best_by_ep.get(ep)
        if prev is None or part.bytes > prev[0]:
            best_by_ep[ep] = (part.bytes, part.id)
    selected_ids = {part.id for part in item.files if part.selected}
    if item.status != "magnet_conversion" and not best_by_ep:
        _set_pack_state(db, row_id, state, "file_mismatch")
        return False
    if (
        state in {"selecting", "selected"}
        and selected_ids
        and item.status == "downloaded"
        and item.links
    ):
        _set_pack_state(db, row_id, state, "downloaded")
        return False
    chosen_ids = {pair[1] for pair in best_by_ep.values()}
    if (
        chosen_ids
        and selected_ids
        and chosen_ids <= selected_ids
        and item.status == "downloaded"
        and item.links
    ):
        _set_pack_state(db, row_id, state, "downloaded", selected_episodes=sorted(best_by_ep))
        return False
    if row["overdue"]:
        _set_pack_state(db, row_id, state, "stalled")
        return False
    if (
        item.status == "waiting_files_selection"
        and state == "submitted"
        and best_by_ep
        and wanted_fn()
    ):
        file_list = ",".join(str(best_by_ep[ep][1]) for ep in sorted(best_by_ep))
        if _set_pack_state(db, row_id, state, "selecting"):
            try:
                selected_now = await provider.select_files(item.id, file_list)
            except RealDebridProviderBlock:
                _set_pack_state(db, row_id, "selecting", "provider_restricted")
                return False
            if selected_now:
                _set_pack_state(
                    db,
                    row_id,
                    "selecting",
                    "selected",
                    selected_episodes=sorted(best_by_ep),
                )
    return True


async def _advance_pack_pending(db, user_id, title_id, token, *, provider=None):
    row = _active_pack_row(db, user_id, title_id, account_binding(token))
    if row is None:
        return False
    season = row["season"]
    title = catalog_title(db, personal_scope(db, user_id), "tv", str(title_id), season, 0)
    if title is None:
        if row["overdue"]:
            _set_pack_state(db, row["id"], row["state"], "stalled")
            return False
        return True

    def wanted_fn():
        if _account(db, user_id) != token:
            return set()
        return _wanted_season_episodes(db, user_id, title_id, season, token)

    provider = provider or RealDebridClient(token, **cooldown_hooks(db, user_id))
    policy = load_user_acquisition_policy(db, user_id)
    try:
        async with asyncio.timeout(5):
            return await _inspect_pack(
                db, row, title, provider, wanted_fn, policy.max_release_size_bytes
            )
    except RealDebridProviderBlock:
        _set_pack_state(db, row["id"], row["state"], "provider_restricted")
        return False
    except _TRANSIENT:
        return True


async def advance_pending(db, user_id, title_id, *, provider=None):
    """Poll the existing receipt before opening a new transfer for this request."""
    token = _account(db, user_id)
    if not token:
        return False
    if await _advance_pack_pending(db, user_id, title_id, token, provider=provider):
        return True
    row = _active_row(db, user_id, title_id, account_binding(token))
    if row is None:
        return False
    season, episode = row["season"], row["episode"]
    title = catalog_title(db, personal_scope(db, user_id), "tv", str(title_id), season, episode)
    if title is None:
        if row["overdue"]:
            _set_state(db, row["id"], row["state"], "stalled")
            return False
        return True

    def authorized():
        return _approved(db, user_id, title_id, season, episode, token, title)

    provider = provider or RealDebridClient(token, **cooldown_hooks(db, user_id))
    policy = load_user_acquisition_policy(db, user_id)
    try:
        async with asyncio.timeout(5):
            return await _inspect(
                db, row, title, provider, authorized, policy.max_release_size_bytes
            )
    except RealDebridProviderBlock:
        _set_state(db, row["id"], row["state"], "provider_restricted")
        return False
    except _TRANSIENT:
        return True


async def submit(
    db, user_id, title_id, season, episode, candidates, *, library=None, provider=None
):
    """Reserve the release before submitting its hash. Never retry an ambiguous write."""
    token = _account(db, user_id)
    if not token:
        return
    title = catalog_title(db, personal_scope(db, user_id), "tv", str(title_id), season, episode)
    if not _approved(db, user_id, title_id, season, episode, token, title):
        return
    binding = account_binding(token)
    if _active_row(db, user_id, title_id, binding) or _active_pack_row(
        db, user_id, title_id, binding
    ):
        return
    previous = db.execute(
        """SELECT state FROM tv_fallback_transfers
           WHERE user_id=? AND title_id=? AND season=? AND episode=? AND account_binding=?""",
        (user_id, title_id, season, episode, binding),
    ).fetchall()
    if len(previous) >= _ATTEMPTS or any(row["state"] in _UNCERTAIN for row in previous):
        return
    policy = load_user_acquisition_policy(db, user_id)
    ranked = _rank(candidates, title, policy)
    if not ranked:
        return
    try:
        async with asyncio.timeout(6):
            inventory = await (library or DebridLibrary()).fresh_torrents(token)
            if any(
                _tv_marker(item.get("filename"), title) == (season, episode)
                and item.get("status") not in _FAILED
                for item in inventory.values()
            ):
                return
            known = {normalize_btih(item.get("hash")) for item in inventory.values()}
            seen = {
                row[0]
                for row in db.execute(
                    """SELECT torrent_hash FROM tv_fallback_transfers WHERE account_binding=?
                       UNION SELECT torrent_hash FROM tv_season_pack_transfers WHERE account_binding=?
                       UNION SELECT torrent_hash FROM movie_fallback_transfers WHERE account_binding=?""",
                    (binding, binding, binding),
                )
            }
            chosen = next(
                (value for value in ranked if value not in known and value not in seen), None
            )
            if not chosen or not _approved(db, user_id, title_id, season, episode, token, title):
                return
            record = db.execute(
                """INSERT INTO tv_fallback_transfers(user_id,title_id,season,episode,account_binding,
                   torrent_hash,state) VALUES(?,?,?,?,?,?,'submitting')
                   ON CONFLICT DO NOTHING RETURNING id""",
                (user_id, title_id, season, episode, binding, chosen),
            ).fetchone()
            db.commit()
            if record is None:
                return
            row_id = record[0]
            provider = provider or RealDebridClient(token, **cooldown_hooks(db, user_id))
            try:
                code = await provider.add_magnet(f"magnet:?xt=urn:btih:{chosen}")
            except RealDebridProviderBlock:
                _set_state(db, row_id, "submitting", "provider_restricted")
                return
            except _TRANSIENT:
                code = None
            if isinstance(code, str) and _ID.fullmatch(code):
                _set_state(db, row_id, "submitting", "submitted", torrent_id=code)
            else:
                _set_state(db, row_id, "submitting", "submission_unknown")
    except _TRANSIENT:
        return


async def submit_season_pack(
    db,
    user_id,
    title_id,
    season,
    missing_episodes,
    *,
    catalogs=None,
    library=None,
    provider=None,
) -> tuple[bool, bool]:
    """Search and reserve one season pack covering >= 2 missing episodes."""
    from omakase.plus.catalog_config import configured_catalogs

    token = _account(db, user_id)
    if not token or len(missing_episodes) < 2:
        return False, False
    policy = load_user_acquisition_policy(db, user_id)
    if not policy.prefer_batches:
        return False, False
    binding = account_binding(token)
    if _active_row(db, user_id, title_id, binding) or _active_pack_row(
        db, user_id, title_id, binding
    ):
        return False, False
    previous = db.execute(
        """SELECT state FROM tv_season_pack_transfers
           WHERE user_id=? AND title_id=? AND season=? AND account_binding=?""",
        (user_id, title_id, season, binding),
    ).fetchall()
    if len(previous) >= _PACK_ATTEMPTS or any(row["state"] in _UNCERTAIN for row in previous):
        return False, False
    title = catalog_title(db, personal_scope(db, user_id), "tv", str(title_id), season, 0)
    if title is None:
        return False, False
    sources = (
        tuple(catalogs)
        if catalogs is not None
        else configured_catalogs(db, user_id, kind="tv").sources
    )
    if not sources:
        return False, False

    query = f"{title.title} S{season:02d}"
    raw_candidates, successes, _ = await request_candidates._gather_queries(
        sources, (query,), timeout=5.0
    )
    if not successes or not raw_candidates:
        return True, False
    deduped = request_candidates._dedupe_candidates(raw_candidates)
    judge_req = request_candidates.JudgeRequest(
        title=title.title,
        kind="tv",
        season=season,
        season_pack=True,
        tiered_fallback=True,
        allowed_resolutions=policy.allowed_resolutions,
        minimum_seeders=policy.minimum_seeders,
        maximum_bytes=policy.max_release_size_bytes,
    )
    evaluated = request_candidates.evaluate_candidates(deduped, judge_req)
    request_candidates.record(db, user_id, "tv", str(title_id), evaluated)
    eligible = [item for item, verdict in evaluated if verdict.accepted]
    ranked = _rank_season_pack(eligible, title, season, policy)
    if not ranked:
        return True, False
    try:
        async with asyncio.timeout(8):
            inventory = await (library or DebridLibrary()).fresh_torrents(token)
            known = {normalize_btih(item.get("hash")) for item in inventory.values()}
            seen = {
                row[0]
                for row in db.execute(
                    """SELECT torrent_hash FROM tv_fallback_transfers WHERE account_binding=?
                       UNION SELECT torrent_hash FROM tv_season_pack_transfers WHERE account_binding=?
                       UNION SELECT torrent_hash FROM movie_fallback_transfers WHERE account_binding=?""",
                    (binding, binding, binding),
                )
            }
            chosen = next(
                (value for value in ranked if value not in known and value not in seen), None
            )
            if not chosen:
                return True, False
            wanted = _wanted_season_episodes(db, user_id, title_id, season, token)
            if len(wanted) < 2:
                return True, False
            record = db.execute(
                """INSERT INTO tv_season_pack_transfers(user_id,title_id,season,account_binding,
                   torrent_hash,state) VALUES(?,?,?,?,?,'submitting')
                   ON CONFLICT DO NOTHING RETURNING id""",
                (user_id, title_id, season, binding, chosen),
            ).fetchone()
            db.commit()
            if record is None:
                return True, False
            row_id = record[0]
            client = provider or RealDebridClient(token, **cooldown_hooks(db, user_id))
            try:
                code = await client.add_magnet(f"magnet:?xt=urn:btih:{chosen}")
            except RealDebridProviderBlock:
                _set_pack_state(db, row_id, "submitting", "provider_restricted")
                return True, False
            except _TRANSIENT:
                code = None
            if not isinstance(code, str) or not _ID.fullmatch(code):
                _set_pack_state(db, row_id, "submitting", "submission_unknown")
                return True, True
            _set_pack_state(db, row_id, "submitting", "submitted", torrent_id=code)
            inserted_row = db.execute(
                """SELECT *, created_at < strftime('%Y-%m-%dT%H:%M:%SZ','now','-8 hours') AS overdue
                   FROM tv_season_pack_transfers WHERE id=?""",
                (row_id,),
            ).fetchone()
            if inserted_row is not None:
                await _inspect_pack(
                    db,
                    inserted_row,
                    title,
                    client,
                    lambda: _wanted_season_episodes(db, user_id, title_id, season, token),
                    policy.max_release_size_bytes,
                )
            return True, True
    except RealDebridProviderBlock:
        return True, False
    except _TRANSIENT:
        return True, False
