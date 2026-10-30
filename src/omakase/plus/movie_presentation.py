"""Concise read-only movie display; saved reports never claim live activity."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_REPORT_TTL = timedelta(minutes=30)
_ACTIVITY_CODES = {
    "requested",
    "searching",
    "search_complete",
    "acquiring",
    "unknown_backend_state",
    "backend_state_unknown",
}
_STATUS = {
    "ready": (
        "Ready in Plex",
        "ready",
        "Movie identity, English audio and file access are verified.",
    ),
    "requested": ("Requested", "neutral", "Waiting for a confirmed result."),
    "searching": ("Searching", "pending", "Last reported by Riven. Delivery is not confirmed."),
    "search_complete": (
        "Search reported complete",
        "neutral",
        "A usable copy has not been confirmed.",
    ),
    "acquiring": ("Transfer reported", "pending", "Check Plex after delivery is confirmed."),
    "unknown_backend_state": (
        "Status unknown",
        "neutral",
        "Riven has not confirmed the request's current activity.",
    ),
    "backend_state_unknown": (
        "Status unknown",
        "neutral",
        "Riven has not confirmed the request's current activity.",
    ),
    "provider_restricted": (
        "Provider restricted",
        "blocked",
        "The saved result reports a provider restriction.",
    ),
    "no_eligible_results": (
        "No eligible copy",
        "attention",
        "No matching copy was reported by the connected source.",
    ),
    "backend_failed": (
        "Request failed",
        "blocked",
        "Riven reported a failure. No duplicate request was sent.",
    ),
    "investigation_exhausted": (
        "Needs review",
        "attention",
        "The bounded checks are complete. Review the connected source.",
    ),
    "rate_limited": ("Try later", "attention", "The service asked for a later check."),
    "backend_unavailable": (
        "Service unavailable",
        "attention",
        "Check the configured connection, then try again.",
    ),
    "connection_rejected": (
        "Connection needs attention",
        "blocked",
        "The service rejected the saved connection.",
    ),
    "transport_timeout": (
        "Check timed out",
        "attention",
        "The result is unknown. No duplicate request was sent.",
    ),
    "transport_error": ("Connection unavailable", "attention", "The service could not be reached."),
    "backend_item_missing": (
        "Not found in Riven",
        "attention",
        "The existing request was not found in the bounded inventory.",
    ),
    "acknowledgement_unconfirmed": (
        "Request unconfirmed",
        "attention",
        "The request was acknowledged, but persistence is unconfirmed.",
    ),
    "submission_pending": (
        "Request pending",
        "pending",
        "An existing submission is being checked.",
    ),
    "paused": ("Paused", "attention", "Riven last reported this request as paused."),
    "unreleased": (
        "Not released",
        "neutral",
        "Riven last reported that this title was not released.",
    ),
    "verifying": (
        "Not checked in Plex",
        "neutral",
        "Check Plex when you want to confirm availability.",
    ),
    "verification_expired": (
        "Needs a Plex check",
        "attention",
        "The previous file-access check is out of date.",
    ),
    "plex_not_found": (
        "Not found in Plex",
        "neutral",
        "No exact movie match was found in the Movies library.",
    ),
    "english_audio_not_selected": (
        "Check audio in Plex",
        "attention",
        "Choose English audio in Plex, then check again.",
    ),
}


def _stamp(value, now):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed > now + timedelta(minutes=2):
        return None
    return parsed.astimezone(timezone.utc)


def present_movie(card, *, now=None):
    """Add display vocabulary without changing history, records or connections."""
    now = now or datetime.now(timezone.utc)
    code = card.get("code", "verifying")
    label, tone, summary = _STATUS.get(
        code,
        ("Needs attention", "attention", "Review the saved check details before trying again."),
    )
    report = card.get("request_report") or {}
    recorded_at = _stamp(report.get("updated_at"), now)
    observation = card.get("request_observation") or {}
    observed_at = _stamp(observation.get("observed_at"), now)
    if observation.get("code") != code:
        observed_at = None
    reported_at = observed_at or (recorded_at if report.get("code") == code else None)
    request_label = _STATUS.get(report.get("code"), ("Status unknown", "neutral", ""))[0]
    if code in _ACTIVITY_CODES:
        if reported_at is None:
            label, tone, summary = (
                "Status unknown",
                "neutral",
                "Check the existing request for a current report.",
            )
        elif now - reported_at > _REPORT_TTL:
            label, tone, summary = (
                "Status out of date",
                "attention",
                f"Last saved as {request_label.lower()}. Check for an updated report.",
            )
    history = card.get("history") or {}
    watch_label = (
        {
            "completed": "Watched",
            "watching": "Watching",
            "dropped": "Stopped watching",
        }.get(history.get("status"), "Not watched")
        if history.get("status_source") not in {None, "", "none"}
        else "Not watched"
    )
    display = {
        "status_label": label,
        "status_tone": tone,
        "summary": summary,
        "watch_label": watch_label,
        "request_label": request_label,
        "request_recorded_at": recorded_at.isoformat().replace("+00:00", "Z")
        if recorded_at
        else None,
        "request_observed_at": observed_at.isoformat().replace("+00:00", "Z")
        if observed_at
        else None,
        "request_stale": bool(recorded_at and now - recorded_at > _REPORT_TTL),
    }
    return {**card, "display": display}
