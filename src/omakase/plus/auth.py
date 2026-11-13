"""Authentication utilities for Omakase Plus.

Uses Argon2 for password hashing and cryptographically-random session
tokens stored in SQLite.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher

_hasher = PasswordHasher()


# ── Password helpers ────────────────────────────────────────


def hash_password(plain: str) -> str:
    """Return an Argon2id hash of *plain*."""
    return _hasher.hash(plain)


def verify_password(plain: str, hash_value: str) -> bool:
    """Return True if *plain* matches *hash_value*.

    Returns False on any Argon2 exception (wrong format, verification
    failure, etc.) instead of propagating the error.
    """
    try:
        return _hasher.verify(hash_value, plain)
    except Exception:
        return False


# ── Session helpers ─────────────────────────────────────────


def session_max_age() -> int:
    """Configured idle lifetime, bounded to 7–365 days (default 90)."""
    try:
        days = int(os.getenv("OMAKASE_SESSION_DAYS", "90"))
    except ValueError:
        days = 90
    return (days if 7 <= days <= 365 else 90) * 86400


def session_absolute_max_age() -> int:
    """Configured absolute session lifetime, bounded to 7–730 days (default 365)."""
    try:
        days = int(os.getenv("OMAKASE_SESSION_ABSOLUTE_DAYS", "365"))
    except ValueError:
        days = 365
    return (days if 7 <= days <= 730 else 365) * 86400


def create_session(db, user_id: int) -> str:
    """Insert a persistent session for *user_id*.

    Returns the raw session id (64 hex chars) that should be stored
    in the ``omakase_session`` cookie.
    """
    session_id = secrets.token_hex(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=session_max_age())).isoformat()
    db.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
        (session_id, user_id, expires_at),
    )
    db.commit()
    return session_id


def validate_session(db, session_id: str) -> int | None:
    """Look up *session_id* and return the associated ``user_id``.

    Expired sessions are silently deleted during lookup and ``None``
    is returned.
    """
    return authenticate_session(db, session_id, renew=False)[0]


def authenticate_session(db, session_id: str, *, renew: bool = True) -> tuple[int | None, bool]:
    """Authenticate and occasionally extend idle expiry using compare-and-set.

    The boolean asks the HTTP layer to renew its persistent cookie. Missing,
    expired, absolutely aged-out and revoked sessions can never be recreated
    by this operation.
    """
    try:
        row = db.execute(
            "SELECT user_id, expires_at, created_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = db.execute(
            "SELECT user_id, expires_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if row is None:
        return None, False
    now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(row["expires_at"])
        invalid = expires.tzinfo is None or expires <= now
    except (TypeError, ValueError):
        invalid = True
    if invalid:
        db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        db.commit()
        return None, False
    created_raw = row["created_at"] if "created_at" in row.keys() else None
    if created_raw is not None:
        try:
            created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            absolutely_expired = now - created >= timedelta(seconds=session_absolute_max_age())
        except (TypeError, ValueError):
            absolutely_expired = False
        if absolutely_expired:
            db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            db.commit()
            return None, False
    renewed = False
    lifetime = session_max_age()
    if renew and expires - now < timedelta(seconds=lifetime / 3):
        cursor = db.execute(
            "UPDATE sessions SET expires_at=? WHERE id=? AND expires_at=?",
            ((now + timedelta(seconds=lifetime)).isoformat(), session_id, row["expires_at"]),
        )
        db.commit()
        renewed = cursor.rowcount == 1
        if (
            not renewed
            and db.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone() is None
        ):
            return None, False
    return row["user_id"], renewed


def delete_session(db, session_id: str) -> None:
    """Remove *session_id* from the database."""
    db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    db.commit()


# ── Account lifecycle helpers ───────────────────────────────


def validate_new_password(password: str) -> str | None:
    """Return a human-readable problem with *password*, or None when valid.

    Length is the only requirement (NIST-style: no forced composition rules).
    """
    if not password:
        return "New password is required."
    if len(password) < 8:
        return "New password must be at least 8 characters."
    if len(password) > 1024:
        return "New password is too long."
    return None


def set_password(db, user_id: int, new_password: str) -> None:
    """Replace a user's password hash and revoke all of their sessions."""
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.commit()


def change_password(
    db,
    user_id: int,
    current_password: str,
    new_password: str,
    *,
    keep_session_id: str | None = None,
) -> bool:
    """Change a password after verifying the current one.

    Returns False when the current password is wrong; on success all sessions
    except *keep_session_id* are revoked so other devices must sign in again.
    """
    row = db.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not verify_password(current_password, row["password_hash"]):
        return False
    problem = validate_new_password(new_password)
    if problem is not None:
        raise ValueError(problem)
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (hash_password(new_password), user_id),
    )
    revoke_user_sessions(db, user_id, except_session_id=keep_session_id)
    db.commit()
    return True


def revoke_user_sessions(db, user_id: int, *, except_session_id: str | None = None) -> int:
    """Delete every stored session for *user_id* (optionally keeping one)."""
    if except_session_id:
        cursor = db.execute(
            "DELETE FROM sessions WHERE user_id = ? AND id <> ?",
            (user_id, except_session_id),
        )
    else:
        cursor = db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    db.commit()
    return int(cursor.rowcount or 0)


def session_count(db, user_id: int, *, except_session_id: str | None = None) -> int:
    """Count stored sessions for *user_id* (optionally excluding one)."""
    if except_session_id:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ? AND id <> ?",
            (user_id, except_session_id),
        ).fetchone()
    else:
        row = db.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE user_id = ?", (user_id,)
        ).fetchone()
    return int(row["n"])
