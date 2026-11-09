"""Process exact-copy removal jobs under the ordinary media sync lease."""

import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import httpx

from omakase.plus.connections import movie_connections
from omakase.plus.movie_cleanup import (
    MovieCleanupProof,
    MovieCleanupUnavailable,
    _matches,
    _owned_acquisition,
    _owner_movie,
    _plex_binding,
    _rd_client,
    _torrent_matches,
    movie_cleanup_enabled,
    movie_cleanup_preference,
    verify_movie_cleanup_copy,
    watched_and_rated_manually,
)
from omakase.plus.movies import MovieError, MovieService, _imdb
from omakase.plus.movies_store import get_submission
from omakase.plus.provider_limits import ProviderRateLimit
from omakase.plus.secrets import read_secret


class _Wait(ValueError):
    """A temporary hold. Retry without advancing the durable stage."""


class _Revoked(ValueError):
    """The viewer revoked the queued intent."""


@dataclass(frozen=True)
class _Receipt:
    copy: MovieCleanupProof
    connections: str
    machine: str
    tmdb_id: int


def _job_key(job):
    return job["user_id"], job["imdb_id"], job["cleanup_tag"]


def _update(db, job, *, stage, proof=None, attempts=0, retry_at=None, reason=None):
    db.execute(
        """UPDATE movie_cleanup_jobs
           SET stage=?, proof_json=COALESCE(?, proof_json), attempts=?, retry_at=?, reason=?
           WHERE user_id=? AND imdb_id=? AND cleanup_tag=? AND state='queued' AND stage=?
             AND intent=? AND selection_digest IS ?""",
        (
            stage,
            proof,
            attempts,
            retry_at,
            reason,
            *_job_key(job),
            job["stage"],
            job["intent"],
            job["selection_digest"],
        ),
    )
    db.commit()


def _connection_stamp(connections, key):
    values = (
        connections.riven_url,
        connections.riven_api_key,
        connections.plex_url,
        connections.plex_token,
        connections.plex_machine_id,
        connections.plex_movies_section_id,
        key,
    )
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()


def _authorize(service, job, *, riven_id=None):
    db = service.db
    user_id, imdb_id, tag = _job_key(job)
    pending = db.execute(
        """SELECT 1 FROM movie_cleanup_jobs
           WHERE user_id=? AND imdb_id=? AND cleanup_tag=? AND state='queued' AND stage=?
             AND intent=? AND selection_digest IS ?""",
        (user_id, imdb_id, tag, job["stage"], job["intent"], job["selection_digest"]),
    ).fetchone()
    history = db.execute(
        """SELECT status, status_source, rating, rating_source FROM movie_history
           WHERE user_id=? AND imdb_id=?""",
        (user_id, imdb_id),
    ).fetchone()
    allowed = (
        history is not None and history["status"] == "completed"
        if job["intent"] == "manual"
        else movie_cleanup_preference(db, user_id) and watched_and_rated_manually(history)
    )
    if pending is None or not movie_cleanup_enabled() or not allowed:
        raise _Revoked()
    movie = _owner_movie(service, user_id, imdb_id)
    if service._scope != job["backend_scope"] or not _owned_acquisition(
        service, user_id, movie, {"acquisition_tag": tag, "riven_id": riven_id}
    ):
        raise MovieCleanupUnavailable("acquisition_changed")
    return movie


def _ensure_unshared(db, copy):
    # A torrent can also be used by Omakase's independent anime and import flows.
    item = db.execute(
        """SELECT 1 FROM media_assets
           WHERE rd_torrent_id=:id OR provider_asset_id=:id OR lower(torrent_hash)=:hash
           UNION ALL SELECT 1 FROM download_attempts
           WHERE rd_torrent_id=:id OR lower(torrent_hash)=:hash
           UNION ALL SELECT 1 FROM anilist_plannings WHERE rd_torrent_id=:id
           LIMIT 1""",
        {"id": copy.torrent_id, "hash": copy.torrent_hash},
    ).fetchone()
    if item:
        raise MovieCleanupUnavailable("copy_shared_with_other_media")


async def _plex_machine(service):
    identity = await service._xml("/identity")
    machine = identity.get("machineIdentifier")
    if not _matches(machine, r"[A-Za-z0-9_-]{1,128}") or (
        service.connections.plex_machine_id and machine != service.connections.plex_machine_id
    ):
        raise MovieCleanupUnavailable("plex_server_changed")
    return machine


def _read_receipt(job):
    try:
        data = json.loads(job["proof_json"])
        copy = data["copy"]
        copy["publication"] = tuple(copy["publication"])
        return _Receipt(
            MovieCleanupProof(**copy), data["connections"], data["machine"], data["tmdb_id"]
        )
    except (TypeError, ValueError, KeyError):
        raise MovieCleanupUnavailable("removal_receipt_invalid") from None


def _riven_paths(job, receipt):
    _, imdb_id, tag = _job_key(job)
    return "/api/v1/items/movie-removal-binding/" + _imdb(imdb_id), {
        "omakase_acquisition_tag": tag,
        "riven_id": receipt.copy.riven_id,
        "tmdb_id": receipt.tmdb_id,
        "torrent_id": receipt.copy.torrent_id,
        "torrent_hash": receipt.copy.torrent_hash,
    }


def _selection_digest(receipt):
    return hashlib.sha256(json.dumps(asdict(receipt), sort_keys=True).encode()).hexdigest()


async def verify_manual_cleanup_selection(service, user_id, imdb_id, copy):
    movie = _owner_movie(service, user_id, imdb_id)
    submission = get_submission(service.db, service._scope, imdb_id)
    tag = submission["cleanup_tag"] if submission else None
    if not copy.owned or not _owned_acquisition(
        service, user_id, movie, {"acquisition_tag": tag, "riven_id": copy.riven_id}
    ):
        raise MovieCleanupUnavailable("acquisition_unverified")
    key = read_secret(service.db, user_id, "realdebrid_api_key")
    if not key:
        raise MovieCleanupUnavailable("real_debrid_disconnected")
    receipt = _Receipt(
        copy,
        _connection_stamp(service.connections, key),
        await _plex_machine(service),
        movie["tmdb_id"],
    )
    path, params = _riven_paths(
        {"user_id": user_id, "imdb_id": imdb_id, "cleanup_tag": tag}, receipt
    )
    if await _withdrawal(service, path, params) != "present":
        raise MovieCleanupUnavailable("copy_changed")
    return tag, _selection_digest(receipt)


async def _withdrawal(service, path, params):
    try:
        receipt = await service._json("riven", path + "/withdrawal", params=params)
    except MovieError as error:
        if error.http_status in (404, 405):
            raise MovieCleanupUnavailable("guarded_removal_unavailable") from error
        raise
    if (
        receipt.get("schema") != "riven-movie-withdrawal-v1"
        or receipt.get("acquisition_tag") != params["omakase_acquisition_tag"]
        or receipt.get("riven_id") != params["riven_id"]
        or type(receipt.get("tmdb_id")) is not int
        or receipt["tmdb_id"] != params["tmdb_id"]
        or receipt.get("torrent_id") != params["torrent_id"]
        or receipt.get("torrent_hash") != params["torrent_hash"]
        or receipt.get("state") not in ("present", "withdrawing", "withdrawn", "released")
    ):
        raise MovieCleanupUnavailable("guarded_removal_receipt_invalid")
    return receipt["state"]


async def _release_withdrawal(service, path, params):
    await service._bytes("riven", "POST", path + "/withdrawal/release", params=params)
    if await _withdrawal(service, path, params) != "released":
        raise _Wait("waiting_for_riven")


async def _check_plex_withdrawal(service, movie, receipt, *, allow_original=False):
    copy = receipt.copy
    section, _ = await service._sections()
    if section != copy.plex_section or await _plex_machine(service) != receipt.machine:
        raise MovieCleanupUnavailable("plex_server_changed")
    original_present = False
    try:
        await service._xml("/library/metadata/" + copy.plex_key)
    except MovieError as error:
        if error.http_status != 404:
            raise
    else:
        if not allow_original:
            raise _Wait("waiting_for_plex")
        current = await _plex_binding(service, movie, {"publication": list(copy.publication)})
        if current != (copy.plex_key, copy.plex_section, copy.file_size):
            raise MovieCleanupUnavailable("copy_changed")
        original_present = True
    for video in await service._videos(section, require_complete=True):
        if original_present and video.get("ratingKey") == copy.plex_key:
            continue
        if (
            service._guids(video, "imdb") & {movie["imdb_id"]}
            or service._guids(video, "tmdb") & {str(movie["tmdb_id"])}
            or any(
                tuple(PurePosixPath(part.get("file", "")).parts[-2:]) == copy.publication
                for part in video.findall(".//Part")
            )
        ):
            raise _Wait("waiting_for_plex")
    sessions = await service._xml("/status/sessions")
    if any(current.get("ratingKey") == copy.plex_key for current in sessions.findall(".//Video")):
        raise _Wait("movie_is_playing")


async def _step(db, job, heartbeat, service_factory):
    user_id, imdb_id, _ = _job_key(job)
    stage = job["stage"]
    service = service_factory(db, user_id)
    movie = _authorize(service, job)
    key = read_secret(db, user_id, "realdebrid_api_key")
    if not key:
        raise MovieCleanupUnavailable("real_debrid_disconnected")
    stamp = _connection_stamp(service.connections, key)

    def confirm_authorized(copy):
        heartbeat()
        current = service_factory(db, user_id)
        current_key = read_secret(db, user_id, "realdebrid_api_key")
        if _connection_stamp(current.connections, current_key) != stamp:
            raise MovieCleanupUnavailable("connections_changed")
        current_movie = _authorize(current, job, riven_id=copy.riven_id)
        if current_movie["tmdb_id"] != movie["tmdb_id"]:
            raise MovieCleanupUnavailable("movie_identity_changed")
        _ensure_unshared(db, copy)

    if stage == "prepare":
        copy = await verify_movie_cleanup_copy(service, user_id, imdb_id)
        if not copy.owned:
            raise MovieCleanupUnavailable("acquisition_unverified")
        receipt = _Receipt(copy, stamp, await _plex_machine(service), movie["tmdb_id"])
        if job["intent"] == "manual" and job["selection_digest"] != _selection_digest(receipt):
            raise MovieCleanupUnavailable("confirmed_copy_changed")
        confirm_authorized(copy)
        _update(db, job, stage="withdrawal_intent", proof=json.dumps(asdict(receipt)))
        return

    receipt = _read_receipt(job)
    if receipt.connections != stamp or receipt.tmdb_id != movie["tmdb_id"]:
        raise MovieCleanupUnavailable("connections_or_identity_changed")
    copy = receipt.copy
    confirm_authorized(copy)
    path, params = _riven_paths(job, receipt)
    withdrawal = await _withdrawal(service, path, params)
    rd = _rd_client(service, user_id, key)
    if withdrawal == "released":
        if await rd.get_torrent(copy.torrent_id, require_definite_absence=True) is not None:
            raise MovieCleanupUnavailable("released_provider_item_reappeared")
        confirm_authorized(copy)
        _update(db, job, stage="complete")
        return
    if stage == "withdrawal_intent":
        if withdrawal == "present":
            current = await verify_movie_cleanup_copy(service, user_id, imdb_id)
            if current != copy or await _plex_machine(service) != receipt.machine:
                raise MovieCleanupUnavailable("copy_changed")
        elif withdrawal == "withdrawing":
            # A committed native intent may already have unpublished the link.
            # Check the original Plex copy or its absence before resuming it.
            await _check_plex_withdrawal(service, movie, receipt, allow_original=True)
            torrent = await rd.get_torrent(copy.torrent_id, require_definite_absence=True)
            if torrent is not None and not _torrent_matches(
                torrent, copy.torrent_id, copy.torrent_hash, copy.file_name, copy.file_size
            ):
                raise MovieCleanupUnavailable("real_debrid_binding_changed")
        if withdrawal != "withdrawn":
            confirm_authorized(copy)
            await service._bytes("riven", "DELETE", path, params=params)
            withdrawal = await _withdrawal(service, path, params)
        if withdrawal != "withdrawn":
            raise _Wait("waiting_for_riven")
        _update(db, job, stage="awaiting_plex")
        return

    if withdrawal != "withdrawn":
        raise MovieCleanupUnavailable("withdrawal_changed")
    await _check_plex_withdrawal(service, movie, receipt)
    torrent = await rd.get_torrent(copy.torrent_id, require_definite_absence=True)
    if torrent is None:
        confirm_authorized(copy)
        await _release_withdrawal(service, path, params)
        _update(db, job, stage="complete")
        return
    if not _torrent_matches(
        torrent, copy.torrent_id, copy.torrent_hash, copy.file_name, copy.file_size
    ):
        raise MovieCleanupUnavailable("real_debrid_binding_changed")
    if stage == "awaiting_plex":
        confirm_authorized(copy)
        _update(db, job, stage="provider_intent")
        return

    # Recheck the native receipt after the Plex and provider lookups. A new
    # publication or a shared Riven reference revokes the withdrawal proof.
    if await _withdrawal(service, path, params) != "withdrawn":
        raise MovieCleanupUnavailable("withdrawal_changed")
    confirm_authorized(copy)
    if not await rd.delete_torrent(copy.torrent_id, require_exact_id=True):
        raise _Wait("provider_removal_unconfirmed")
    if await rd.get_torrent(copy.torrent_id, require_definite_absence=True) is not None:
        raise _Wait("provider_removal_unconfirmed")
    confirm_authorized(copy)
    await _release_withdrawal(service, path, params)
    _update(db, job, stage="complete")


def _service(db, user_id):
    return MovieService(db, movie_connections(db, user_id))


def _retry(db, job, reason, *, not_before=None):
    attempts = job["attempts"] + 1
    if attempts >= 12:
        _update(db, job, stage="blocked", attempts=12, reason=reason)
        return
    deadline = datetime.now(UTC) + timedelta(minutes=min(attempts, 5))
    _update(
        db,
        job,
        stage=job["stage"],
        attempts=attempts,
        retry_at=max(deadline, not_before or deadline).isoformat(),
        reason=reason,
    )


async def process_movie_cleanup_jobs(db, *, heartbeat, service_factory=_service):
    """Advance at most two jobs once per run while holding the sync lease."""
    if not movie_cleanup_enabled():
        return
    jobs = db.execute(
        """SELECT * FROM movie_cleanup_jobs
           WHERE state='queued' AND stage NOT IN ('complete', 'blocked')
             AND (retry_at IS NULL OR retry_at<=?)
           ORDER BY queued_at, user_id, imdb_id LIMIT 2""",
        (datetime.now(UTC).isoformat(),),
    ).fetchall()
    for job in jobs:
        heartbeat()
        try:
            async with asyncio.timeout(60):
                await _step(db, job, heartbeat, service_factory)
        except _Revoked:
            continue
        except MovieCleanupUnavailable as error:
            if str(error) in {"movie_is_playing", "copy_lookup_failed"}:
                _retry(db, job, str(error))
            else:
                _update(db, job, stage="blocked", reason=str(error))
        except ProviderRateLimit as error:
            if error.manual_review_required:
                _update(db, job, stage="blocked", reason="provider_pause")
            else:
                deadline = datetime.now(UTC) + timedelta(seconds=error.retry_after_seconds)
                _retry(db, job, "provider_pause", not_before=deadline)
        except MovieError as error:
            if error.http_status in (409, 422):
                _update(db, job, stage="blocked", reason="guarded_removal_refused")
            else:
                deadline = datetime.fromisoformat(error.retry_at) if error.retry_at else None
                _retry(db, job, "connection_or_lookup_failed", not_before=deadline)
        except _Wait as error:
            _retry(db, job, str(error))
        except (TimeoutError, httpx.HTTPError, ValueError, OverflowError, RecursionError):
            _retry(db, job, "connection_or_lookup_failed")
