"""Unit tests for scripts/asc_cleanup_ephemeral_dev_certs.py."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "asc_cleanup_ephemeral_dev_certs.py"
)
SPEC = importlib.util.spec_from_file_location("asc_cleanup_ephemeral_dev_certs", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MOD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MOD
SPEC.loader.exec_module(MOD)


def _encode_der_int(val: bytes) -> bytes:
    return b"\x02" + bytes([len(val)]) + val


def _encode_der_sig(r_bytes: bytes, s_bytes: bytes) -> bytes:
    body = _encode_der_int(r_bytes) + _encode_der_int(s_bytes)
    if len(body) < 128:
        return b"\x30" + bytes([len(body)]) + body
    return b"\x30\x81" + bytes([len(body)]) + body


def test_normalize_serial_edge_cases() -> None:
    assert MOD.normalize_serial("serial=000a:1b:2C") == "A1B2C"
    assert MOD.normalize_serial("  00:00:00  ") == "0"
    assert MOD.normalize_serial("0f1e2d") == "F1E2D"
    assert MOD.normalize_serial("") == ""
    assert MOD.normalize_serial("not-hex") == ""


def test_der_signature_to_raw_es256_lengths_and_validation() -> None:
    r32 = bytes(range(1, 33))
    s32 = bytes(range(33, 65))
    assert MOD.der_signature_to_raw_es256(_encode_der_sig(r32, s32)) == r32 + s32

    # 31-byte r (short) and 33-byte 0x00-prefixed s (high bit set)
    r31 = b"\x01" * 31
    s32_msb = b"\x80" + b"\x02" * 31
    raw = MOD.der_signature_to_raw_es256(_encode_der_sig(r31, b"\x00" + s32_msb))
    assert raw == (b"\x00" + r31) + s32_msb

    with pytest.raises(ValueError):
        MOD.der_signature_to_raw_es256(b"\x31\x00")
    with pytest.raises(ValueError):
        MOD.der_signature_to_raw_es256(_encode_der_sig(b"\x01" * 33, s32))


def test_sign_es256_jwt_claims(tmp_path: Path) -> None:
    key_file = tmp_path / "AuthKey_TEST.p8"
    key_file.write_text("placeholder", encoding="utf-8")

    r32 = b"\x11" * 32
    s32 = b"\x22" * 32

    def fake_sign(data: bytes, path: Path) -> bytes:
        assert path == key_file
        assert b"." in data
        return _encode_der_sig(r32, s32)

    token = MOD.sign_es256_jwt(
        key_id="KEY123",
        issuer_id="ISSUER456",
        key_path=key_file,
        now=1700000000,
        ttl_seconds=300,
        sign_fn=fake_sign,
    )
    head_b64, payload_b64, sig_b64 = token.split(".")
    header = json.loads(base64.urlsafe_b64decode(head_b64 + "=="))
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
    raw_sig = base64.urlsafe_b64decode(sig_b64 + "==")

    assert header == {"alg": "ES256", "kid": "KEY123", "typ": "JWT"}
    assert payload == {
        "iss": "ISSUER456",
        "iat": 1700000000,
        "exp": 1700000300,
        "aud": "appstoreconnect-v1",
    }
    assert raw_sig == r32 + s32


def test_extract_serials_from_pem_bundle_dedupes_and_normalizes() -> None:
    bundle = "\n".join(
        [
            "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
            "-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----",
            "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----",
        ]
    )
    mapping = {
        "AAA": "serial=000a1b",
        "BBB": "serial=FF01",
    }

    def fake_serial(cert_pem: str) -> str:
        for key, val in mapping.items():
            if key in cert_pem:
                return val
        return ""

    assert MOD.extract_serials_from_pem_bundle(bundle, serial_fn=fake_serial) == ["A1B", "FF01"]


def test_select_certs_to_revoke_strict_safety_matrix() -> None:
    certs = [
        {
            "id": "api-dev-match",
            "attributes": {
                "certificateType": "DEVELOPMENT",
                "displayName": "Apple Development: Created via API",
                "name": "Created via API",
                "serialNumber": "001122AA",
            },
        },
        {
            "id": "api-dev-other",
            "attributes": {
                "certificateType": "DEVELOPMENT",
                "displayName": "Apple Development: Created via API",
                "name": "Created via API",
                "serialNumber": "998877",
            },
        },
        {
            "id": "personal-ios-dev",
            "attributes": {
                "certificateType": "IOS_DEVELOPMENT",
                "displayName": "YASSINE BARHOUMI",
                "name": "iOS Development: YASSINE BARHOUMI",
                "serialNumber": "1122AA",
            },
        },
        {
            "id": "managed-dist",
            "attributes": {
                "certificateType": "DISTRIBUTION",
                "displayName": "YASSINE BARHOUMI",
                "name": "Distribution Managed: Created via API",
                "serialNumber": "1122AA",
            },
        },
        {
            "id": "ios-dist",
            "attributes": {
                "certificateType": "IOS_DISTRIBUTION",
                "displayName": "Created via API",
                "name": "Created via API",
                "serialNumber": "1122AA",
            },
        },
        {
            "id": "manual-dev-non-api",
            "attributes": {
                "certificateType": "DEVELOPMENT",
                "displayName": "YASSINE BARHOUMI",
                "name": "Apple Development: Mac",
                "serialNumber": "1122AA",
            },
        },
    ]

    exact = MOD.select_certs_to_revoke(certs, target_serials={"1122aa"}, prune_all_api_dev=False)
    assert [c["id"] for c in exact] == ["api-dev-match"]

    pruned = MOD.select_certs_to_revoke(certs, target_serials=set(), prune_all_api_dev=True)
    assert [c["id"] for c in pruned] == ["api-dev-match", "api-dev-other"]


def test_main_dry_run_and_live_revoke(tmp_path: Path) -> None:
    key_file = tmp_path / "AuthKey_TEST.p8"
    key_file.write_text("placeholder", encoding="utf-8")
    summary_dry = tmp_path / "dry.json"
    summary_live = tmp_path / "live.json"

    fake_certs = [
        {
            "id": "cert-1",
            "attributes": {
                "certificateType": "DEVELOPMENT",
                "displayName": "Created via API",
                "serialNumber": "ABCD",
            },
        },
        {
            "id": "cert-keep",
            "attributes": {
                "certificateType": "DISTRIBUTION",
                "displayName": "Distribution Managed",
                "serialNumber": "ABCD",
            },
        },
    ]

    revoked_calls: list[str] = []

    rc_dry = MOD.main(
        [
            "--key-id",
            "KEY",
            "--issuer-id",
            "ISSUER",
            "--key-path",
            str(key_file),
            "--serial",
            "abcd",
            "--dry-run",
            "--json-out",
            str(summary_dry),
        ],
        list_fn=lambda _tok: fake_certs,
        revoke_fn=lambda cid, _tok: revoked_calls.append(cid) or 204,
        jwt_fn=lambda **_kwargs: "fake.jwt.token",
    )
    assert rc_dry == 0
    assert revoked_calls == []
    dry_data = json.loads(summary_dry.read_text(encoding="utf-8"))
    assert dry_data["matched"] == 1
    assert dry_data["revoked"] == 0
    assert dry_data["dry_run"] is True

    rc_live = MOD.main(
        [
            "--key-id",
            "KEY",
            "--issuer-id",
            "ISSUER",
            "--key-path",
            str(key_file),
            "--serial",
            "abcd",
            "--json-out",
            str(summary_live),
        ],
        list_fn=lambda _tok: fake_certs,
        revoke_fn=lambda cid, _tok: revoked_calls.append(cid) or 204,
        jwt_fn=lambda **_kwargs: "fake.jwt.token",
    )
    assert rc_live == 0
    assert revoked_calls == ["cert-1"]
    live_data = json.loads(summary_live.read_text(encoding="utf-8"))
    assert live_data["checked"] == 2
    assert live_data["matched"] == 1
    assert live_data["revoked"] == 1
    assert live_data["failed"] == 0
    assert live_data["revoked_ids"] == ["cert-1"]
