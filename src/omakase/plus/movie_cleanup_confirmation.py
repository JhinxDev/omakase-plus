"""Short-lived confirmation for one verified, owner-managed movie copy."""

import hashlib
import time
from datetime import UTC, datetime
from secrets import token_hex

from omakase.plus.movie_cleanup import (
    MovieCleanupUnavailable,
    _matches,
    _owner_movie,
    movie_cleanup_enabled,
)
from omakase.plus.movies_store import get_submission


def cleanup_status(db, user_id, imdb_id):
    job = db.execute(
        """SELECT state, stage, reason FROM movie_cleanup_jobs
           WHERE user_id=? AND imdb_id=? ORDER BY queued_at DESC LIMIT 1""",
        (user_id, imdb_id),
    ).fetchone()
    if not job:
        return None
    if job["state"] == "cancelled":
        return "cancelled" if job["stage"] == "prepare" else "paused"
    if job["stage"] == "complete":
        return "removed"
    if job["stage"] == "blocked":
        return "changed" if job["reason"] == "confirmed_copy_changed" else "attention"
    if job["reason"] == "movie_is_playing":
        return "playing"
    if job["stage"] == "awaiting_plex":
        return "waiting"
    return "queued"


def _nonce_digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue_movie_confirmation(db, *, user_id, imdb_id, scope, tag, selection_digest):
    nonce = token_hex(32)
    now = time.time()
    with db:
        db.execute("DELETE FROM movie_cleanup_confirmations WHERE expires_at<=?", (now,))
        saved = db.execute(
            """INSERT INTO movie_cleanup_confirmations
               (user_id, imdb_id, nonce_digest, selection_digest, cleanup_tag, backend_scope, expires_at)
               SELECT user_id, imdb_id, ?, ?, ?, ?, ?
               FROM movie_history WHERE user_id=? AND imdb_id=? AND status='completed'
               ON CONFLICT(user_id, imdb_id) DO UPDATE SET
                 nonce_digest=excluded.nonce_digest, selection_digest=excluded.selection_digest,
                 cleanup_tag=excluded.cleanup_tag, backend_scope=excluded.backend_scope,
                 expires_at=excluded.expires_at""",
            (_nonce_digest(nonce), selection_digest, tag, scope, now + 300, user_id, imdb_id),
        )
    if saved.rowcount != 1:
        raise MovieCleanupUnavailable("watched_movie_required")
    return nonce


def confirm_movie_cleanup(service, user_id, imdb_id, nonce):
    """Consume the checked copy and enqueue it in one transaction."""
    if not movie_cleanup_enabled() or not _matches(nonce, r"[0-9a-f]{64}"):
        raise MovieCleanupUnavailable("confirmation_unavailable")
    db = service.db
    _owner_movie(service, user_id, imdb_id)
    with db:
        preview = db.execute(
            """DELETE FROM movie_cleanup_confirmations
               WHERE user_id=? AND imdb_id=? AND nonce_digest=? AND backend_scope=? AND expires_at>?
               RETURNING cleanup_tag, selection_digest""",
            (user_id, imdb_id, _nonce_digest(nonce), service._scope, time.time()),
        ).fetchone()
        if preview is None:
            raise MovieCleanupUnavailable("confirmation_unavailable")
        submission = get_submission(db, service._scope, imdb_id)
        if (
            submission is None
            or submission["cleanup_owner_id"] != user_id
            or submission["cleanup_tag"] != preview["cleanup_tag"]
        ):
            raise MovieCleanupUnavailable("acquisition_changed")
        saved = db.execute(
            """INSERT INTO movie_cleanup_jobs
                 (user_id, imdb_id, cleanup_tag, backend_scope, queued_at, intent, selection_digest)
               SELECT user_id, imdb_id, ?, ?, ?, 'manual', ?
               FROM movie_history WHERE user_id=? AND imdb_id=? AND status='completed'
               ON CONFLICT (user_id, imdb_id, cleanup_tag) DO UPDATE SET
                 state='queued', intent='manual', selection_digest=excluded.selection_digest,
                 stage='prepare', queued_at=excluded.queued_at, attempts=0, retry_at=NULL, reason=NULL
               WHERE movie_cleanup_jobs.stage='prepare'
                  OR (movie_cleanup_jobs.stage='blocked'
                      AND movie_cleanup_jobs.reason='confirmed_copy_changed'
                      AND movie_cleanup_jobs.proof_json IS NULL)""",
            (
                preview["cleanup_tag"],
                service._scope,
                datetime.now(UTC).isoformat(),
                preview["selection_digest"],
                user_id,
                imdb_id,
            ),
        )
        if saved.rowcount != 1:
            raise MovieCleanupUnavailable("removal_already_reviewed")
