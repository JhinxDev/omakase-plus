#!/usr/bin/env python3
"""Revoke ephemeral ``Created via API`` Apple Development certificates created by CI."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ASC_CERTIFICATES_URL = (
    "https://api.appstoreconnect.apple.com/v1/certificates"
    "?filter[certificateType]=DEVELOPMENT&limit=200"
)
ASC_DELETE_CERTIFICATE_URL = "https://api.appstoreconnect.apple.com/v1/certificates/{cert_id}"
PEM_CERT_RE = re.compile(
    r"-----BEGIN CERTIFICATE-----\s+.*?\s+-----END CERTIFICATE-----",
    re.DOTALL,
)
HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_serial(raw: str) -> str:
    """Normalize an X.509 certificate serial string into canonical uppercase hex."""
    cleaned = raw.strip()
    if cleaned.lower().startswith("serial="):
        cleaned = cleaned[7:].strip()
    cleaned = cleaned.replace(":", "").replace(" ", "").upper()
    if not cleaned or not HEX_RE.fullmatch(cleaned):
        return ""
    stripped = cleaned.lstrip("0")
    return stripped or "0"


def _read_der_length(buf: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(buf):
        raise ValueError("Truncated DER length")
    first = buf[offset]
    offset += 1
    if first < 0x80:
        return first, offset
    num_bytes = first & 0x7F
    if num_bytes == 0 or offset + num_bytes > len(buf):
        raise ValueError("Unsupported or truncated DER long length")
    length = int.from_bytes(buf[offset : offset + num_bytes], "big")
    return length, offset + num_bytes


def der_signature_to_raw_es256(der: bytes) -> bytes:
    """Convert an ASN.1 DER ECDSA P-256 signature to 64-byte raw ``r || s``."""
    if not der or der[0] != 0x30:
        raise ValueError("Expected ASN.1 DER SEQUENCE for ECDSA signature")
    seq_len, offset = _read_der_length(der, 1)
    if offset + seq_len != len(der):
        raise ValueError("ASN.1 DER SEQUENCE length mismatch")

    parts: list[bytes] = []
    for _ in range(2):
        if offset >= len(der) or der[offset] != 0x02:
            raise ValueError("Expected ASN.1 DER INTEGER component")
        int_len, offset = _read_der_length(der, offset + 1)
        if int_len <= 0 or offset + int_len > len(der):
            raise ValueError("Invalid ASN.1 DER INTEGER length")
        val = der[offset : offset + int_len]
        offset += int_len
        if val[0] & 0x80:
            raise ValueError("ASN.1 DER signature component cannot be negative")
        val = val.lstrip(b"\x00")
        if len(val) > 32:
            raise ValueError("ES256 signature integer exceeds 32 bytes")
        parts.append(val.rjust(32, b"\x00"))

    if offset != len(der):
        raise ValueError("Unexpected trailing bytes after ES256 signature integers")
    return parts[0] + parts[1]


def _default_openssl_sign(signing_input: bytes, key_path: Path) -> bytes:
    proc = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(key_path)],
        input=signing_input,
        capture_output=True,
        check=True,
    )
    return proc.stdout


def sign_es256_jwt(
    *,
    key_id: str,
    issuer_id: str,
    key_path: Path,
    now: int | None = None,
    ttl_seconds: int = 300,
    sign_fn: Callable[[bytes, Path], bytes] | None = None,
) -> str:
    """Create a short-lived App Store Connect ES256 JWT without third-party libraries."""
    issued_at = int(time.time()) if now is None else int(now)
    header = {"alg": "ES256", "kid": key_id, "typ": "JWT"}
    payload = {
        "iss": issuer_id,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "aud": "appstoreconnect-v1",
    }
    encoded_header = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("ascii")

    signer = sign_fn or _default_openssl_sign
    der_sig = signer(signing_input, Path(key_path))
    raw_sig = der_signature_to_raw_es256(der_sig)
    return f"{encoded_header}.{encoded_payload}.{_b64url(raw_sig)}"


def _default_openssl_serial(cert_pem: str) -> str:
    proc = subprocess.run(
        ["openssl", "x509", "-noout", "-serial"],
        input=cert_pem.encode("utf-8"),
        capture_output=True,
        check=True,
    )
    return proc.stdout.decode("utf-8", errors="replace")


def extract_serials_from_pem_bundle(
    pem_text: str,
    *,
    serial_fn: Callable[[str], str] | None = None,
) -> list[str]:
    """Extract sorted, deduplicated, normalized certificate serials from PEM input."""
    reader = serial_fn or _default_openssl_serial
    serials: set[str] = set()
    for match in PEM_CERT_RE.finditer(pem_text):
        normalized = normalize_serial(reader(match.group(0)))
        if normalized:
            serials.add(normalized)
    return sorted(serials)


def is_ephemeral_api_dev_cert(cert: dict[str, Any]) -> bool:
    """Return True strictly for generic ``DEVELOPMENT`` certs labeled ``Created via API``."""
    attrs = cert.get("attributes")
    if not isinstance(attrs, dict):
        return False
    cert_type = str(attrs.get("certificateType") or "").strip().upper()
    if cert_type != "DEVELOPMENT":
        return False
    display_name = str(attrs.get("displayName") or "").casefold()
    name = str(attrs.get("name") or "").casefold()
    return "created via api" in display_name or "created via api" in name


def select_certs_to_revoke(
    certs: list[dict[str, Any]],
    *,
    target_serials: set[str],
    prune_all_api_dev: bool = False,
) -> list[dict[str, Any]]:
    """Select only safe ephemeral API Development certs matching target serials or prune mode."""
    normalized_targets = {s for s in (normalize_serial(raw) for raw in target_serials) if s}
    selected: list[dict[str, Any]] = []
    for cert in certs:
        if not is_ephemeral_api_dev_cert(cert):
            continue
        if prune_all_api_dev:
            selected.append(cert)
            continue
        attrs = cert.get("attributes") or {}
        serial = normalize_serial(str(attrs.get("serialNumber") or ""))
        if serial and serial in normalized_targets:
            selected.append(cert)
    return selected


def list_development_certificates(
    token: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Fetch ``DEVELOPMENT`` certificates from App Store Connect (bounded pagination)."""
    open_url = opener or urllib.request.urlopen
    next_url: str | None = ASC_CERTIFICATES_URL
    collected: list[dict[str, Any]] = []
    for _ in range(10):
        if not next_url:
            break
        req = urllib.request.Request(
            next_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with open_url(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        data = body.get("data") or []
        if isinstance(data, list):
            collected.extend(item for item in data if isinstance(item, dict))
        links = body.get("links") or {}
        raw_next = links.get("next") if isinstance(links, dict) else None
        next_url = str(raw_next) if raw_next else None
    return collected


def revoke_certificate(
    cert_id: str,
    token: str,
    *,
    opener: Callable[..., Any] | None = None,
) -> int:
    """Revoke one App Store Connect certificate by ID; 200/204/404 are treated cleanly."""
    open_url = opener or urllib.request.urlopen
    url = ASC_DELETE_CERTIFICATE_URL.format(cert_id=urllib.parse.quote(cert_id, safe=""))
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="DELETE",
    )
    try:
        with open_url(req, timeout=30) as resp:
            return int(getattr(resp, "status", 204) or 204)
    except urllib.error.HTTPError as exc:
        if exc.code in (200, 204, 404):
            return int(exc.code)
        raise


def _write_summary(summary: dict[str, Any], json_out: Path | None) -> None:
    rendered = json.dumps(summary, indent=2) + "\n"
    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extract-serials-stdin",
        action="store_true",
        help="Read PEM certificates from stdin and print normalized serial numbers.",
    )
    parser.add_argument("--key-id", default="")
    parser.add_argument("--issuer-id", default="")
    parser.add_argument("--key-path", type=Path, default=None)
    parser.add_argument(
        "--serial",
        action="append",
        default=[],
        help="Certificate serial number created during this run (repeatable).",
    )
    parser.add_argument(
        "--prune-stale-api-dev-certs",
        action="store_true",
        help="Also revoke any older generic DEVELOPMENT certificates labeled Created via API.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json-out", type=Path, default=None)
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    list_fn: Callable[[str], list[dict[str, Any]]] | None = None,
    revoke_fn: Callable[[str, str], int] | None = None,
    jwt_fn: Callable[..., str] | None = None,
) -> int:
    args = parse_args(argv)
    if args.extract_serials_stdin:
        for serial in extract_serials_from_pem_bundle(sys.stdin.read()):
            sys.stdout.write(serial + "\n")
        return 0

    if not args.key_id or not args.issuer_id or args.key_path is None:
        sys.stderr.write("--key-id, --issuer-id, and --key-path are required.\n")
        return 2
    if not args.key_path.is_file():
        sys.stderr.write(f"ASC private key file not found: {args.key_path}\n")
        return 2

    target_serials = {s for s in (normalize_serial(raw) for raw in args.serial) if s}
    if not target_serials and not args.prune_stale_api_dev_certs:
        summary: dict[str, Any] = {
            "checked": 0,
            "matched": 0,
            "revoked": 0,
            "failed": 0,
            "revoked_ids": [],
            "dry_run": bool(args.dry_run),
        }
        _write_summary(summary, args.json_out)
        return 0

    make_jwt = jwt_fn or sign_es256_jwt
    token = make_jwt(key_id=args.key_id, issuer_id=args.issuer_id, key_path=args.key_path)

    lister = list_fn or list_development_certificates
    revoker = revoke_fn or revoke_certificate

    certs = lister(token)
    matched = select_certs_to_revoke(
        certs,
        target_serials=target_serials,
        prune_all_api_dev=bool(args.prune_stale_api_dev_certs),
    )

    revoked_ids: list[str] = []
    failed = 0
    for cert in matched:
        cert_id = str(cert.get("id") or "").strip()
        if not cert_id:
            continue
        if args.dry_run:
            revoked_ids.append(cert_id)
            continue
        try:
            revoker(cert_id, token)
            revoked_ids.append(cert_id)
        except Exception as exc:
            failed += 1
            sys.stderr.write(f"Failed to revoke certificate {cert_id}: {exc}\n")

    summary = {
        "checked": len(certs),
        "matched": len(matched),
        "revoked": 0 if args.dry_run else len(revoked_ids),
        "failed": failed,
        "revoked_ids": revoked_ids,
        "dry_run": bool(args.dry_run),
    }
    _write_summary(summary, args.json_out)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
