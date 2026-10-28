"""AES-256-GCM encryption for per-user secrets.

Stored encrypted at rest in the ``user_secrets`` table. The master key is
derived from ``OMAKASE_PLUS_MASTER_KEY`` via HKDF-SHA256.
"""

from __future__ import annotations

import base64
import os
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def _derive_key() -> bytes:
    """Derive a 32-byte AES-256 key from the master env var via HKDF."""
    raw = os.getenv("OMAKASE_PLUS_MASTER_KEY", "")
    if not raw:
        return secrets.token_bytes(32)  # dev fallback — never in production
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"omakase-plus-secrets",
    ).derive(raw.encode())


_KEY: bytes | None = None


def _get_key() -> bytes:
    global _KEY
    if _KEY is None:
        _KEY = _derive_key()
    return _KEY


def encrypt(plaintext: str) -> str:
    """Encrypt a string with AES-256-GCM.

    Returns a base64-encoded blob containing the nonce + ciphertext.
    """
    key = _get_key()
    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)
    packed = nonce + ciphertext
    return base64.b64encode(packed).decode()


def decrypt(blob: str) -> str:
    """Decrypt a base64-encoded AES-256-GCM blob.

    Raises ``ValueError`` if the ciphertext is malformed or the key is wrong.
    """
    key = _get_key()
    packed = base64.b64decode(blob)
    nonce, ciphertext = packed[:12], packed[12:]
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode()


def store_secret(db, user_id: int, key_name: str, plaintext: str) -> None:
    """Store (or update) an encrypted secret for a user.

    Upserts: replaces the existing row for the same (user_id, key_name).
    """
    encrypted = encrypt(plaintext)
    db.execute(
        """INSERT INTO user_secrets (user_id, key_name, encrypted_value)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id, key_name) DO UPDATE SET
               encrypted_value = excluded.encrypted_value,
               created_at = datetime('now')""",
        (user_id, key_name, encrypted),
    )
    db.commit()


def read_secret(db, user_id: int, key_name: str) -> str | None:
    """Read and decrypt a stored secret. Returns ``None`` if not found."""
    row = db.execute(
        "SELECT encrypted_value FROM user_secrets WHERE user_id = ? AND key_name = ?",
        (user_id, key_name),
    ).fetchone()
    if row is None:
        return None
    try:
        return decrypt(row["encrypted_value"])
    except (ValueError, base64.binascii.Error):
        return None


def delete_secret(db, user_id: int, key_name: str) -> None:
    """Delete a stored secret."""
    db.execute(
        "DELETE FROM user_secrets WHERE user_id = ? AND key_name = ?",
        (user_id, key_name),
    )
    db.commit()
