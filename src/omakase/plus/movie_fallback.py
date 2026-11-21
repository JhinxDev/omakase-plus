"""Advance one authorized, exact-file fallback transfer after a Riven failure."""

from __future__ import annotations

import asyncio
import re
from pathlib import PurePosixPath

import httpx

from omakase.plus import movies_store, request_candidates
from omakase.plus.acquisition_policy import load_user_acquisition_policy
from omakase.plus.btih import normalize_btih, torrent_hash_from_magnet
from omakase.plus.catalog import quality_from_title
from omakase.plus.debrid_catalog import CatalogTitle, _movie_matches, _tail
from omakase.plus.debrid_library import (
    DebridLibrary,
    DebridLibraryError,
    _video,
    account_binding,
    account_token,
)
from omakase.plus.provider_limits import ProviderRateLimit, cooldown_hooks
from omakase.plus.providers import playback as playback_providers
from omakase.plus.realdebrid import RealDebridClient, RealDebridProviderBlock

_TRANSFER_TIMEOUT = 12
_ID = re.compile(r"[A-Za-z0-9]{3,64}")
_MOVIE_QUALITY = {"2160p": 2, "1080p": 1}
_ACTIVE = frozenset({"submitted", "selecting", "selected"})
_LOOKUP = "user_id=? AND imdb_id=? AND request_created_at=?"
_TRANSIENT = (DebridLibraryError, ProviderRateLimit, httpx.HTTPError, TimeoutError, ValueError)

LABELS = {
    "submitting": "A transfer submission is awaiting confirmation.",
    "submission_unknown": "The provider did not confirm submission. No duplicate was sent.",
    "submitted": "The provider accepted the transfer. The movie file is being checked.",
    "selecting": "The file selection is awaiting confirmation.",
    "selected": "The exact movie file was selected. The provider is preparing it.",
    "downloaded": "The provider reports that the selected movie file is available.",
    "existing_copy": "A matching transfer already exists in your provider account.",
    "file_mismatch": "The provider did not supply one unambiguous movie file.",
    "provider_restricted": "The provider restricted this release.",
    "provider_failed": "The provider reported that the fallback transfer failed.",
    "provider_missing": "The provider no longer has this transfer.",
    "account_changed": "This transfer belongs to a previously connected account.",
}
TERMINAL_RIVEN_CODES = frozenset(
    {"backend_failed", "no_eligible_results", "investigation_exhausted"}
)


def attach_summaries(db, user_id, models):
    by_id = {model["title_id"]: model for model in models if model["kind"] == "movie"}
    if not by_id:
        return
    binding = account_binding(account_token(db, user_id))
    predicates = ",".join("?" for _ in by_id)
    rows = db.execute(
        f"""SELECT t.imdb_id, t.state, t.account_binding FROM movie_fallback_transfers t
        JOIN movie_requests r USING (user_id, imdb_id)
        WHERE t.user_id=? AND t.request_created_at=r.created_at AND t.imdb_id IN ({predicates})""",
        (user_id, *by_id),
    )
    for row in rows:
        state = row["state"] if binding == row["account_binding"] else "account_changed"
        if state in LABELS:
            by_id[row["imdb_id"]]["fallback_transfer"] = {"state": state, "headline": LABELS[state]}


def _row(db, key):
    return db.execute(f"SELECT * FROM movie_fallback_transfers WHERE {_LOOKUP}", key).fetchone()


def _change(db, key, previous, state, *, torrent_id=None):
    count = db.execute(
        f"""UPDATE movie_fallback_transfers SET state=?, torrent_id=COALESCE(?,torrent_id)
            WHERE {_LOOKUP} AND state=?""",
        (state, torrent_id, *key, previous),
    ).rowcount
    db.commit()
    return count == 1


def _choose(candidates, title, policy):
    releases = []
    for item in candidates:
        fields = " ".join(_tail(item.title, title))
        quality = _MOVIE_QUALITY.get(quality_from_title(fields))
        if (
            not quality
            or not _movie_matches(item.title, title)
            or not policy.minimum_seeders <= int(item.seeders)
            or not 0 < int(item.size_bytes) <= policy.max_release_size_bytes
            or re.search(r"\b(?:cam|hdcam|camrip|hdts|telesync|telecine|dvdscr|screener)\b", fields)
        ):
            continue
        digest = torrent_hash_from_magnet(item.magnet)
        if digest:
            releases.append((quality, int(item.seeders), digest))
    return max(releases)[2] if releases else None


async def _inspect(db, key, row, title, provider, authorized, max_bytes):
    state = row["state"]
    try:
        item = await provider.get_torrent(row["torrent_id"], require_definite_absence=True)
    except _TRANSIENT:
        return state
    if item is None:
        next_state = "provider_missing"
    elif item.id != row["torrent_id"] or normalize_btih(item.torrent_hash) != row["torrent_hash"]:
        next_state = "file_mismatch"
    elif item.status in {"dead", "error", "magnet_error", "virus"}:
        next_state = "provider_failed"
    else:
        files = [
            part
            for part in item.files
            if part.id > 0
            and 0 < part.bytes <= max_bytes
            and _video(PurePosixPath(part.path).name)
            and _movie_matches(PurePosixPath(part.path).name, title)
        ]
        selected = [part.id for part in item.files if part.selected]
        if not files and item.status == "magnet_conversion":
            return state
        if len(files) != 1 or (selected and selected != [files[0].id]):
            next_state = "file_mismatch"
        elif selected:
            next_state = "downloaded" if item.status == "downloaded" and item.links else "selected"
        elif (
            item.status == "waiting_files_selection"
            and authorized()
            and _change(db, key, "submitted", "selecting")
        ):
            try:
                accepted = await provider.select_files(item.id, str(files[0].id))
            except _TRANSIENT:
                return "selecting"
            if accepted:
                _change(db, key, "selecting", "selected")
            return _row(db, key)["state"]
        else:
            return _row(db, key)["state"]
    _change(db, key, state, next_state)
    return _row(db, key)["state"]


async def advance(db, user_id, imdb_id, candidates=(), *, library=None, provider=None):
    """Use one scoped receipt and advance it without resubmitting an ambiguous write."""
    if not request_candidates.acquire_enabled():
        return None
    token = account_token(db, user_id)
    request = movies_store.get_request(db, user_id, imdb_id)
    metadata = movies_store.get_movie(db, user_id, imdb_id)
    if not token or not request or request["code"] == "copy_removed":
        return None
    if not metadata or not metadata["identity_verified"] or not metadata["year"]:
        return None
    binding = account_binding(token)
    key = (user_id, imdb_id, request["created_at"])
    title = CatalogTitle("movie", imdb_id, metadata["title"], metadata["year"])

    def authorized():
        current = movies_store.get_request(db, user_id, imdb_id)
        return bool(
            request_candidates.acquire_enabled()
            and playback_providers.select(db, user_id).cloud_route
            and account_token(db, user_id) == token
            and current
            and current["created_at"] == key[2]
            and current["code"] in TERMINAL_RIVEN_CODES
        )

    row = _row(db, key)
    if row and row["account_binding"] != binding:
        return "account_changed"
    if row and row["state"] not in _ACTIVE:
        return row["state"]
    if not row and not candidates:
        return None
    if not authorized():
        return None
    provider = provider or RealDebridClient(token, **cooldown_hooks(db, user_id))
    policy = load_user_acquisition_policy(db, user_id)
    try:
        async with asyncio.timeout(_TRANSFER_TIMEOUT):
            if row is None:
                digest = _choose(candidates, title, policy)
                if not digest:
                    return None
                inventory = await (library or DebridLibrary()).fresh_torrents(token)
                present = any(
                    normalize_btih(item.get("hash")) == digest
                    or (
                        item.get("status") not in {"dead", "error", "magnet_error", "virus"}
                        and _movie_matches(item.get("filename"), title)
                    )
                    for item in inventory.values()
                )
                if not authorized():
                    return None
                state = "existing_copy" if present else "submitting"
                count = db.execute(
                    """INSERT INTO movie_fallback_transfers(user_id,imdb_id,request_created_at,
                       account_binding,torrent_hash,state) VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                    (*key, binding, digest, state),
                ).rowcount
                db.commit()
                if not count or present:
                    winner = _row(db, key)
                    return winner["state"] if winner is not None else "existing_copy"
                try:
                    item_id = await provider.add_magnet(f"magnet:?xt=urn:btih:{digest}")
                except RealDebridProviderBlock:
                    _change(db, key, "submitting", "provider_restricted")
                    return "provider_restricted"
                except _TRANSIENT:
                    item_id = None
                if not isinstance(item_id, str) or not _ID.fullmatch(item_id):
                    _change(db, key, "submitting", "submission_unknown")
                    return "submission_unknown"
                _change(db, key, "submitting", "submitted", torrent_id=item_id)
                row = _row(db, key)
            return await _inspect(
                db, key, row, title, provider, authorized, policy.max_release_size_bytes
            )
    except _TRANSIENT:
        row = _row(db, key)
        return row["state"] if row is not None else None
