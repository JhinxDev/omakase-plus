"""Stable product-health vocabulary and secret-free runtime receipts."""

from __future__ import annotations

from typing import Any

RUNTIME_HEALTH_SCHEMA = "omakase-plus-runtime-health-v1"

_REASON_COPY = {
    "READY": "Ready",
    "PROVIDER_UNREACHABLE": "Provider unavailable",
    "CATEGORY_MISSING": "Omakase category missing",
    "CATEGORY_PATH_MISMATCH": "Download root mismatch",
    "PLEX_UNREACHABLE": "Plex unavailable",
    "PLEX_SECTION_MISMATCH": "Plex library mapping changed",
    "FFPROBE_MISSING": "Media inspection unavailable",
    "CAPACITY_LOW": "Storage capacity low",
    "VPN_REQUIRED": "Torrent tunnel not verified",
    "QBITTORRENT_BYPASS_POSSIBLE": "Torrent traffic can bypass the tunnel",
    "HEALTH_CHECK_FAILED": "Provider check failed",
    "PROVIDER_AUTH_REQUIRED": "Provider connection needs attention",
    "CATALOG_UNAVAILABLE": "Source search unavailable",
    "PARTIAL_CATALOG_ERROR": "Some source searches failed",
    "CATALOG_NOT_CHECKED": "Waiting for the first search",
    "CATALOG_SETUP_INCOMPLETE": "Expanded search needs setup",
}


def reason_label(reason_code: str) -> str:
    """Map internal reason codes to compact user-facing copy."""
    normalized = str(reason_code or "").strip().upper()
    if not normalized:
        return "No diagnostic receipt"
    return _REASON_COPY.get(normalized, normalized.replace("_", " ").title())


def runtime_health_receipt(
    *,
    revision: str,
    manifest_schema: str,
    database: str = "available",
) -> dict[str, Any]:
    """Build the public-to-private-router, secret-free runtime receipt."""
    safe_revision = str(revision or "unknown")
    if len(safe_revision) > 100:
        safe_revision = "invalid"
    if database not in {"available", "degraded", "unavailable"}:
        database = "unavailable"
    return {
        "schema_version": RUNTIME_HEALTH_SCHEMA,
        "status": "ok" if database == "available" else "degraded",
        "revision": safe_revision,
        "manifest_schema": manifest_schema,
        "database": database,
    }
