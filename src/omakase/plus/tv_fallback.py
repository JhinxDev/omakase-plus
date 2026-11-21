"""Advance one exact, owner-authorized TV episode transfer through Real-Debrid."""

from __future__ import annotations

import asyncio
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
_BAD_QUALITY = re.compile(r"\b(?:cam|hdcam|camrip|hdts|telesync|telecine|dvdscr|screener)\b")
_ATTEMPTS = 3
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


def receipts(db, user_id, title_id, binding):
    if not binding:
        return {}
    return {
        (row["season"], row["episode"]): row["state"]
        for row in db.execute(
            """SELECT season,episode,state FROM tv_fallback_transfers
               WHERE user_id=? AND title_id=? AND account_binding=? ORDER BY id""",
            (user_id, title_id, binding),
        )
    }


def _set_state(db, row_id, previous, next_state, *, torrent_id=None):
    changed = db.execute(
        """UPDATE tv_fallback_transfers SET state=?,torrent_id=COALESCE(?,torrent_id)
           WHERE id=? AND state=?""",
        (next_state, torrent_id, row_id, previous),
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


def _rank(candidates, title, policy):
    quality = {name: -index for index, name in enumerate(policy.allowed_resolutions)}
    ranked = []
    for item in candidates:
        remaining = " ".join(_tail(item.title, title))
        resolution = quality_from_title(remaining)
        if (
            resolution in quality
            and _tv_marker(item.title, title) == (title.season, title.episode)
            and not _BAD_QUALITY.search(remaining)
            and int(item.seeders) >= policy.minimum_seeders
            and 0 < int(item.size_bytes) <= policy.max_release_size_bytes
            and (infohash := torrent_hash_from_magnet(item.magnet))
        ):
            ranked.append((quality[resolution], int(item.seeders), infohash))
    return [entry[2] for entry in sorted(ranked, reverse=True)]


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


async def advance_pending(db, user_id, title_id, *, provider=None):
    """Poll the existing receipt before opening a new transfer for this request."""
    token = _account(db, user_id)
    if not token:
        return False
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
    if _active_row(db, user_id, title_id, binding):
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
                       UNION SELECT torrent_hash FROM movie_fallback_transfers WHERE account_binding=?""",
                    (binding, binding),
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
