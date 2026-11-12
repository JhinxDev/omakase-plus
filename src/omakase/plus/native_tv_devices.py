"""Device pairing state. Keep device credentials separate from web session cookies."""

from __future__ import annotations

import hashlib
import re
import secrets
import time
import unicodedata
from ipaddress import ip_address, ip_network

DEVICE_TTL = 30 * 86400
PAIRING_TTL = 300
ACTIVITY_INTERVAL = 300
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE = re.compile(r"[23456789ABCDEFGHJKMNPQRSTUVWXYZ]{8}")
DEVICE_SECRET = re.compile(r"[A-Za-z0-9_-]{43}")
DEVICE_LABELS = {"apple-tv": "Apple TV", "iphone": "iPhone"}


def digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def normalized_code(value):
    value = value.strip().upper().replace("-", "")
    return value if _CODE.fullmatch(value) else None


def device_reference(key):
    return digest("omakase:device-display:" + key)[:32]


def _display_name(value):
    visible = "".join(char for char in value if not unicodedata.category(char).startswith("C"))
    return visible.strip()[:40].strip() or None


def begin(db, *, kind="apple-tv"):
    now = int(time.time())
    secret = secrets.token_urlsafe(32)
    code = "".join(secrets.choice(_ALPHABET) for _ in range(8))
    with db:
        db.execute("DELETE FROM native_tv_devices WHERE expires_at<=?", (now,))
        inserted = db.execute(
            """INSERT OR IGNORE INTO native_tv_devices
                  (token_digest,code_digest,expires_at,device_kind,display_name)
               SELECT ?,?,?,?,?
               WHERE (SELECT count(*) FROM native_tv_devices WHERE user_id IS NULL)<32""",
            (digest(secret), digest(code), now + PAIRING_TTL, kind, DEVICE_LABELS[kind]),
        ).rowcount
    if inserted:
        return secret, f"{code[:4]}-{code[4:]}"
    return None


def approve(db, *, code, actor, browser_session, library_id, display_name=""):
    code = normalized_code(code)
    if code is None:
        return None
    now = int(time.time())
    with db:
        updated = db.execute(
            """UPDATE native_tv_devices
               SET code_digest=NULL,user_id=?,browser_session=?,library_id=?,expires_at=?,
                   display_name=COALESCE(?,display_name)
               WHERE code_digest=? AND expires_at>?
                 AND (SELECT count(*) FROM native_tv_devices WHERE user_id=? AND expires_at>?)<5
               RETURNING device_kind""",
            (
                actor,
                browser_session,
                library_id,
                now + DEVICE_TTL,
                _display_name(display_name),
                digest(code),
                now,
                actor,
                now,
            ),
        ).fetchone()
    return updated["device_kind"] if updated else None


def for_account(db, actor):
    return db.execute(
        """SELECT token_digest,user_id,browser_session,library_id,display_name,device_kind,expires_at,
                  last_seen_at,network_hint,shared_household_id
           FROM native_tv_devices WHERE user_id=? AND expires_at>?
           ORDER BY last_seen_at DESC, expires_at DESC, token_digest""",
        (actor, int(time.time())),
    ).fetchall()


def rename(db, actor, key, name):
    with db:
        changed = db.execute(
            """UPDATE native_tv_devices
               SET display_name=COALESCE(?, CASE device_kind WHEN 'iphone' THEN ? ELSE ? END)
               WHERE user_id=? AND token_digest=? AND expires_at>?""",
            (
                _display_name(name),
                DEVICE_LABELS["iphone"],
                DEVICE_LABELS["apple-tv"],
                actor,
                key,
                int(time.time()),
            ),
        ).rowcount
    return changed == 1


def for_household_owner(db, actor):
    return db.execute(
        """SELECT d.user_id,d.browser_session,d.library_id,d.display_name,d.device_kind,d.expires_at,
                  d.last_seen_at,d.network_hint,m.display_name AS member_name
           FROM libraries l
           JOIN native_tv_devices d ON d.shared_household_id=l.id
           JOIN household_memberships m ON m.library_id=l.id AND m.user_id=d.user_id
           WHERE l.owner_user_id=? AND l.kind='household' AND l.state='active'
             AND m.role='member' AND m.state='active' AND d.expires_at>?
           ORDER BY m.display_name COLLATE NOCASE,m.user_id,
                    d.last_seen_at DESC,d.display_name,d.token_digest""",
        (actor, int(time.time())),
    ).fetchall()


def set_household_sharing(db, actor, key, *, share):
    now = int(time.time())
    with db:
        if share:
            changed = db.execute(
                """UPDATE native_tv_devices SET shared_household_id=m.library_id
                   FROM household_memberships m JOIN libraries l ON l.id=m.library_id
                   WHERE native_tv_devices.user_id=? AND token_digest=? AND expires_at>?
                     AND m.user_id=native_tv_devices.user_id AND m.state='active' AND m.role='member'
                     AND l.state='active' AND l.kind='household'""",
                (actor, key, now),
            ).rowcount
        else:
            changed = db.execute(
                """UPDATE native_tv_devices SET shared_household_id=NULL
                   WHERE user_id=? AND token_digest=? AND expires_at>?""",
                (actor, key, now),
            ).rowcount
    return changed == 1


def lookup(db, key):
    now = int(time.time())
    return db.execute(
        """SELECT user_id,browser_session,library_id,last_seen_at,expires_at-? AS remaining
           FROM native_tv_devices WHERE token_digest=? AND expires_at>?""",
        (now, key, now),
    ).fetchone()


def network_hint(host):
    """Record a coarse public prefix from the trusted ASGI peer, never a full address."""
    try:
        address = ip_address(host)
    except ValueError:
        return None
    address = getattr(address, "ipv4_mapped", None) or address
    if not address.is_global or address.is_multicast or address.is_reserved:
        return None
    prefix = 24 if address.version == 4 else 48
    return str(ip_network((address, prefix), strict=False))


def record_activity(db, key, *, seen_at, network):
    with db:
        db.execute(
            """UPDATE native_tv_devices SET last_seen_at=?,network_hint=?
               WHERE token_digest=? AND user_id IS NOT NULL AND expires_at>?
                 AND (last_seen_at IS NULL OR last_seen_at<=?)""",
            (seen_at, network, key, seen_at, seen_at - ACTIVITY_INTERVAL),
        )


def revoke(db, actor, *, key=None):
    with db:
        if key is None:
            db.execute("DELETE FROM native_tv_devices WHERE user_id=?", (actor,))
        else:
            db.execute(
                "DELETE FROM native_tv_devices WHERE user_id=? AND token_digest=?", (actor, key)
            )


def revoke_credential(db, key):
    """Remove the exact credential, including any pending approval for it."""
    with db:
        db.execute("DELETE FROM native_tv_devices WHERE token_digest=?", (key,))
