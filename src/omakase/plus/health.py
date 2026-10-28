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


def health_guidance(item: dict[str, Any], section: str = "catalog") -> dict[str, str] | None:
    """Explain saved evidence without diagnosing an unrecorded remote cause.

    Actions are fixed existing GET destinations, never provider URLs or mutations.
    """
    code = str(item.get("reason_code") or "").strip().upper()
    kind = str(item.get("kind") or "").lower()
    if code == "READY" and item.get("status") == "healthy":
        return None
    if section == "catalog":
        href, label = "/plus/settings/sources", "Review search settings"
        if kind == "prowlarr":
            href, label = (
                "/plus/settings/connections#connection-prowlarr",
                "Review Prowlarr connection",
            )
        if code in {"CATALOG_UNAVAILABLE", "PARTIAL_CATALOG_ERROR"}:
            cause = (
                "Every search against this source failed in the last recorded run."
                if code == "CATALOG_UNAVAILABLE"
                else "Some searches against this source failed; other searches returned a response."
            )
            next_step = "This can be temporary. Review the saved check below and your search settings before trying a title again."
            if kind == "animetosho":
                href, label = "/plus/settings/sources#animetosho", "Review AnimeTosho setup"
                next_step = "This may be temporary. AnimeTosho is a server-managed metadata source, not a saved API-key connection. Other sources may still work. If failures continue, ask your server administrator to review this optional source."
            elif kind == "prowlarr":
                next_step = "Review the Prowlarr connection and its saved indexer status. Preserve any source cooldown; this page does not reset it."
            return dict(
                cause=cause, next_step=next_step, href=href, label=label, category="Search failure"
            )
        if code == "CATALOG_SETUP_INCOMPLETE":
            return dict(
                cause="Expanded release search does not have all required settings.",
                next_step="Review your Prowlarr address and API-key state, then test the connection explicitly.",
                href="/plus/settings/connections#connection-prowlarr",
                label="Set up Prowlarr",
                category="Configuration needed",
            )
        if code == "CATALOG_NOT_CHECKED":
            return dict(
                cause="No source search result has been saved yet.",
                next_step="A monitored-title search will create the first result. Opening this page does not start a search.",
                href=href,
                label=label,
                category="Waiting for a check",
            )
    elif section == "plex":
        href, label = "/plus/settings/connections#connection-plex", "Review Plex connection"
    elif kind == "realdebrid":
        href, label = (
            "/plus/settings/connections#connection-realdebrid",
            "Review Real-Debrid connection",
        )
    else:
        href, label = "/plus/settings/storage", "Review storage settings"
    causes = {
        "PROVIDER_UNREACHABLE": (
            "The saved check could not reach the media service.",
            "The service may be temporarily unavailable. Review its connection or storage settings before testing again.",
        ),
        "PROVIDER_AUTH_REQUIRED": (
            "The saved check reports that API access needs attention.",
            "Review the saved connection and test it explicitly. Your saved key is never shown here.",
        ),
        "PLEX_UNREACHABLE": (
            "The saved check could not reach Plex.",
            "Review your Plex connection and test access before checking a title's availability again.",
        ),
        "PLEX_SECTION_MISMATCH": (
            "The latest saved check found a Plex library or episode-mapping mismatch.",
            "Review the Plex connection and library setup, then inspect affected titles. Existing watch history is separate.",
        ),
        "CATEGORY_MISSING": (
            "The download client is missing the category Omakase expects.",
            "Your server administrator must review the configured download category. Storage settings explain the expected setup.",
        ),
        "CATEGORY_PATH_MISMATCH": (
            "The download client's saved path does not match the expected media root.",
            "Ask your server administrator to review mounted paths and download-category mapping before another import.",
        ),
        "CAPACITY_LOW": (
            "The saved check reports too little free storage.",
            "Review storage capacity with your server administrator. Do not delete shared or protected media to dismiss this warning.",
        ),
        "FFPROBE_MISSING": (
            "The media-inspection tool is unavailable to the saved check.",
            "Your server administrator must restore the inspection tool before playback verification can succeed.",
        ),
        "VPN_REQUIRED": (
            "The saved check could not verify the required download tunnel.",
            "Ask your server administrator to verify the existing tunnel. Keep download restrictions in place.",
        ),
        "QBITTORRENT_BYPASS_POSSIBLE": (
            "The saved check found a possible route around the required download tunnel.",
            "Keep acquisition restricted until your server administrator verifies routing. Do not bypass the safety check.",
        ),
        "HEALTH_CHECK_FAILED": (
            "The last saved service check did not finish successfully.",
            "Review its details and the relevant settings. The receipt does not identify a more specific cause.",
        ),
    }
    if code in causes:
        cause, next_step = causes[code]
    else:
        cause, next_step = (
            "There is no successful diagnostic result for this saved service state.",
            "Review the saved details and relevant settings. A missing result does not prove a disconnected account or a service outage.",
        )
    category = (
        "Connection needs attention"
        if code == "PROVIDER_AUTH_REQUIRED"
        else "Check needs attention"
    )
    return dict(cause=cause, next_step=next_step, href=href, label=label, category=category)


def affected_health_titles(db, *, user_id: int, limit: int = 8) -> dict[str, Any]:
    """Bounded account-owned failed checks; do not attribute them to one catalog.

    No raw provider error, operational identifier, or cross-account title is exposed.
    """
    limit = max(1, min(int(limit), 20))
    count = db.execute(
        "SELECT COUNT(*) FROM media_monitors WHERE user_id = ? AND consecutive_failures > 0",
        (user_id,),
    ).fetchone()[0]
    rows = db.execute(
        """SELECT id, title, active, last_checked_at FROM media_monitors
           WHERE user_id = ? AND consecutive_failures > 0
           ORDER BY last_checked_at DESC, id DESC LIMIT ?""",
        (user_id, limit),
    ).fetchall()
    return {
        "total": int(count),
        "items": [
            {
                "title": row["title"],
                "href": f"/plus/library/{int(row['id'])}",
                "active": bool(row["active"]),
                "checked_at": row["last_checked_at"],
            }
            for row in rows
        ],
    }
