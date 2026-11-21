"""One owner-scoped request status model shared by the web app and Apple TV.

Every request the private app records (movie, anime, TV) is projected through this
module so the web Requests dashboard and the native client read the same fields.

Model shape (all JSON-safe)::

    identity            owner-scoped id, e.g. ``movie:tt0328955``
    kind / media_kind   ``movie`` | ``anime`` | ``tv``
    title_id / title    bounded catalog identity and display title
    season              season identity when the workflow has one, else ``None``
    supported_stages    exactly the stages this request's workflow can perform
    stage / stage_label the furthest stage the request reached
    stages              stage timeline: ``[{"stage","label","state","occurred_at"}]``
    state               legacy display state retained by existing web surfaces
    label               legacy status label retained by existing web surfaces
    status_label        the shared dashboard/native status label
    detail              honest sentence for the current state
    category            ``active`` | ``attention`` | ``history``
    manual              True when the workflow is manual, never automatic
    manual_note         honest manual-workflow sentence, or None
    stopped_at          the stage that stopped, or None
    reason              verified reason for the stop, or None
    unknown_reason      True when no verified reason is available
    reason_text         the verified reason or the explicit unknown statement
    last_checked_at     when this status was last checked, or None
    last_confirmed_at   when the acquisition service last confirmed an update
    next_action         ``{"kind","label","enabled","reason"}`` or None
    can_check           explicit read-only status check is available
    can_retry           explicit idempotent retry is available
    resolved / removed  verified library match / confirmed removal
    timeline            retained provider-event timeline for existing surfaces

Stage states are ``done``, ``current``, ``pending``, ``stopped`` and ``unknown``.
A stage a workflow cannot perform is absent from ``supported_stages`` and never
appears as pending. A stopped request marks later stages ``unknown`` rather than
promising work that will not happen. When the provider did not supply a reason the
model says so explicitly (``unknown_reason``) instead of inventing one.
"""

from __future__ import annotations

import re
from collections import OrderedDict

STAGES = ("saved", "received", "searched", "acquired", "library")
STAGE_STATES = ("done", "current", "pending", "stopped", "unknown")
CATEGORIES = ("active", "attention", "history")

STAGE_LABELS = {
    "saved": "Request saved",
    "received": "Received by the acquisition service",
    "searched": "Searched",
    "acquired": "Copy acquired",
    "library": "In your library",
}
CURRENT_LABELS = {
    "saved": "Saving the request",
    "received": "Waiting for the acquisition service",
    "searched": "Searching",
    "acquired": "Acquiring a copy",
    "library": "Checking your library",
}
STOPPED_LABELS = {
    "saved": "Request was not saved",
    "received": "Not confirmed received",
    "searched": "Search stopped",
    "acquired": "Acquisition failed",
    "library": "Library check failed",
}

LABELS = {
    "requested": "Requested",
    "preparing": "Preparing",
    "ready": "Ready to Play",
    "in_library": "In your library",
    "needs_attention": "Needs Attention",
    "removed": "Removed",
}

# Supported stages per workflow. TV requests are manual: no automatic anime or
# movie lane exists for them, so searched/acquired are not part of the workflow.
SUPPORTED_STAGES = {
    "movie": STAGES,
    "anime": ("saved", "searched", "acquired", "library"),
    "tv": ("saved", "library"),
}

# Provider milestones the movie timeline can display. Copied from the Riven
# projection so existing web surfaces keep their exact labels.
MOVIE_PROGRESS_LABELS = {
    "requested": "Riven confirmed the request",
    "searching": "Riven started looking for a copy",
    "search_complete": "Riven finished searching",
    "acquiring": "Riven reported a transfer",
    "verifying": "Checking library availability",
}

# The acquisition service reported a stop but supplied no reason we can verify.
UNKNOWN_REASON_TEXT = "The acquisition service did not report a verified reason for this update."

OBSERVATION_CODES = frozenset(
    {
        "requested",
        "searching",
        "search_complete",
        "acquiring",
        "verifying",
        "ready",
        "in_library",
        "plex_not_found",
        "backend_failed",
        "provider_restricted",
        "no_eligible_results",
        "investigation_exhausted",
        "candidate_rejected",
        "removed",
    }
)

_MAX_OBSERVATIONS_PER_REQUEST = 8
_MAX_TRACKED_REQUESTS = 256
_OBSERVATIONS: OrderedDict[tuple[int, str, str, str], tuple[tuple[str, str], ...]] = OrderedDict()

_IDENTITY = re.compile(r"(?:anime|movie|tv):[A-Za-z0-9]{1,20}")
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?"
)


class RequestStatusError(ValueError):
    """A bounded projection input was invalid."""


def identity(kind, title_id):
    """Return the owner-scoped request identity used by both clients."""
    if kind not in SUPPORTED_STAGES:
        raise RequestStatusError("Unknown request kind.")
    text = str(title_id)
    value = f"{kind}:{text}"
    if not _IDENTITY.fullmatch(value):
        raise RequestStatusError("Invalid request identity.")
    return value


def normalize_time(value):
    """Return a bounded ISO-8601 timestamp, or None for anything unrecognised."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 40 or not _TIME.fullmatch(text):
        return None
    if " " in text:
        text = text.replace(" ", "T") + "Z"
    return text


def _latest(*values):
    normalized = [time for time in (normalize_time(value) for value in values) if time]
    return max(normalized) if normalized else None


# ---------------------------------------------------------------------------
# Bounded provider observation ledger
#
# Intermediate provider milestones are observed during an explicit status check.
# They are intentionally process-local and keyed by the request lifecycle
# (created_at) so a new acquisition of the same title never inherits old
# milestones. ``candidate_rejected`` is recorded only when a projection supplies
# a verified per-candidate rejection; the current Riven projection does not expose
# per-candidate data, so live requests normally carry no candidate count.
# ---------------------------------------------------------------------------


def record_observation(user_id, kind, title_id, *, lifecycle, code, occurred_at):
    """Record one verified provider milestone for a request lifecycle."""
    request_id = identity(kind, title_id)
    if type(user_id) is not int or user_id <= 0:
        raise RequestStatusError("Invalid observation owner.")
    if code not in OBSERVATION_CODES:
        raise RequestStatusError("Invalid provider milestone.")
    stamp = normalize_time(occurred_at)
    if stamp is None:
        raise RequestStatusError("Invalid milestone time.")
    cycle = "" if lifecycle in (None, "") else str(lifecycle)
    if len(cycle) > 40 or any(ord(character) < 33 for character in cycle):
        raise RequestStatusError("Invalid request lifecycle.")
    key = (user_id, request_id, cycle)
    # Repeated verified candidate rejections are distinct events; other codes
    # record the latest confirmation of that stage only.
    if code == "candidate_rejected":
        current = list(_OBSERVATIONS.get(key, ()))
    else:
        current = [entry for entry in _OBSERVATIONS.get(key, ()) if entry[0] != code]
    current.append((code, stamp))
    current.sort(key=lambda entry: entry[1])
    _OBSERVATIONS[key] = tuple(current[-_MAX_OBSERVATIONS_PER_REQUEST:])
    _OBSERVATIONS.move_to_end(key)
    while len(_OBSERVATIONS) > _MAX_TRACKED_REQUESTS:
        _OBSERVATIONS.popitem(last=False)


def observations(user_id, kind, title_id, *, lifecycle=""):
    """Return bounded verified milestones for one request lifecycle."""
    request_id = identity(kind, title_id)
    cycle = "" if lifecycle in (None, "") else str(lifecycle)
    return _OBSERVATIONS.get((user_id, request_id, cycle), ())


def clear_observations():
    """Drop the process-local ledger (tests and the labelled fixture server)."""
    _OBSERVATIONS.clear()


# ---------------------------------------------------------------------------
# Model assembly
# ---------------------------------------------------------------------------


def _stage_entries(supported, done, *, stopped=None, stop_time=None, current=None, terminal=False):
    entries = []
    for stage in supported:
        occurred = normalize_time(done.get(stage)) if stage in done else None
        if occurred:
            state, label = "done", STAGE_LABELS[stage]
        elif stage == stopped:
            state, label = "stopped", STOPPED_LABELS[stage]
            occurred = normalize_time(stop_time)
        elif stage == current and not terminal:
            state, label = "current", CURRENT_LABELS[stage]
        elif terminal:
            state, label = "unknown", STAGE_LABELS[stage]
        else:
            state, label = "pending", STAGE_LABELS[stage]
        entries.append({"stage": stage, "label": label, "state": state, "occurred_at": occurred})
    return entries


def _next_action(kind, label, reason=None, enabled=True):
    return {"kind": kind, "label": label, "enabled": bool(enabled), "reason": reason}


def build(
    *,
    kind,
    title_id,
    title,
    media_kind=None,
    season=None,
    state,
    label,
    status_label,
    detail,
    category,
    done,
    supported=None,
    stopped=None,
    stop_time=None,
    current=None,
    reason=None,
    unknown_reason=False,
    reason_text=None,
    last_checked_at=None,
    last_confirmed_at=None,
    next_action=None,
    can_check=False,
    can_retry=False,
    resolved=False,
    removed=False,
    manual=False,
    manual_note=None,
    candidates_rejected=None,
    candidates_considered=None,
    href=None,
    detail_href=None,
    legacy_timeline=(),
):
    """Assemble one validated model instance. Bounded, JSON-safe, no I/O."""
    if kind not in SUPPORTED_STAGES:
        raise RequestStatusError("Unknown request kind.")
    if state not in LABELS:
        raise RequestStatusError("Unknown request state.")
    if category not in CATEGORIES:
        raise RequestStatusError("Unknown request category.")
    stages_supported = list(supported if supported is not None else SUPPORTED_STAGES[kind])
    if not set(stages_supported) <= set(STAGES):
        raise RequestStatusError("Unsupported workflow stage.")
    if stopped is not None and stopped not in stages_supported:
        raise RequestStatusError("A stop must belong to the supported workflow.")
    terminal = category == "history" or stopped is not None
    stage_entries = _stage_entries(
        stages_supported,
        done,
        stopped=stopped,
        stop_time=stop_time,
        current=current,
        terminal=terminal,
    )
    reached = [entry["stage"] for entry in stage_entries if entry["state"] == "done"]
    stage = stopped or (reached[-1] if reached else stages_supported[0])
    if stopped is not None:
        stage_label = STOPPED_LABELS[stopped]
    elif current is not None:
        stage_label = CURRENT_LABELS[current]
    else:
        stage_label = STAGE_LABELS[stage]
    checked = normalize_time(last_checked_at)
    confirmed = normalize_time(last_confirmed_at)
    rejected = candidates_rejected if type(candidates_rejected) is int else None
    considered = candidates_considered if type(candidates_considered) is int else None
    return {
        "identity": identity(kind, title_id),
        "kind": kind,
        "media_kind": media_kind or kind,
        "title_id": str(title_id),
        "title": title,
        "season": season if type(season) is int else None,
        "supported_stages": stages_supported,
        "stage": stage,
        "stage_label": stage_label,
        "stages": stage_entries,
        "state": state,
        "label": label,
        "status_label": status_label,
        "detail": detail,
        "category": category,
        "manual": bool(manual),
        "manual_note": manual_note,
        "stopped_at": stopped,
        "reason": reason,
        "unknown_reason": bool(unknown_reason),
        "reason_text": reason_text
        if reason_text
        else (reason or (UNKNOWN_REASON_TEXT if unknown_reason else None)),
        "last_checked_at": checked,
        "last_confirmed_at": confirmed,
        "next_action": next_action,
        "can_check": bool(can_check),
        "can_retry": bool(can_retry),
        "resolved": bool(resolved),
        "removed": bool(removed),
        "candidates_rejected": rejected,
        "candidates_considered": considered,
        "href": href or f"/plus/requests/{kind}/{title_id}",
        "detail_href": detail_href or f"/plus/requests/{kind}/{title_id}",
        "timeline": list(legacy_timeline),
    }


# ---------------------------------------------------------------------------
# Movie workflow: saved -> received -> searched -> acquired -> library
# ---------------------------------------------------------------------------

_MOVIE_WAITING = frozenset({"submission_pending", "acknowledgement_unconfirmed", "unreleased"})
_MOVIE_PREPARING = frozenset(
    {
        "searching",
        "search_complete",
        "acquiring",
        "verifying",
        "plex_not_found",
        "verification_expired",
        "file_changed",
    }
)
# Confirmed provider failures: no verified provider reason is in the projection.
_MOVIE_SERVICE_STOPPED = frozenset(
    {
        "backend_failed",
        "provider_restricted",
        "no_eligible_results",
        "investigation_exhausted",
        "backend_item_missing",
        "backend_state_unknown",
        "unknown_backend_state",
        "connection_rejected",
        "backend_unavailable",
        "transport_timeout",
        "transport_error",
        "redirect_rejected",
        "rate_limited",
    }
)
# Verified library-verification outcomes: the provider reason is already recorded.
_MOVIE_LIBRARY_STOPPED = frozenset(
    {
        "plex_not_found",
        "plex_identity_mismatch",
        "plex_library_mismatch",
        "invalid_plex_metadata",
        "english_audio_not_selected",
        "edition_mismatch",
        "resolution_unknown",
        "video_codec_unknown",
        "range_not_supported",
        "file_unavailable",
        "incomplete_range",
        "invalid_content_range",
        "verification_expired",
        "file_changed",
    }
)
_MOVIE_SEARCHED = (
    frozenset(
        {
            "search_complete",
            "acquiring",
            "verifying",
            "backend_failed",
            "provider_restricted",
            "no_eligible_results",
            "investigation_exhausted",
            "backend_item_missing",
        }
    )
    | _MOVIE_LIBRARY_STOPPED
)
_RETRYABLE = frozenset(
    {
        "backend_failed",
        "no_eligible_results",
        "investigation_exhausted",
        "backend_item_missing",
        "backend_state_unknown",
        "unknown_backend_state",
        "connection_rejected",
        "transport_timeout",
        "transport_error",
    }
)


def _observed(observations_list, code):
    for observed_code, occurred_at in observations_list:
        if observed_code == code:
            return normalize_time(occurred_at)
    return None


def _received_at(observations_list, fallback):
    """Receipt evidence: the observed provider acknowledgement, else the record."""
    return _observed(observations_list, "requested") or fallback


def _searched_at(observations_list, fallback):
    return _observed(observations_list, "search_complete") or fallback


def _movie_timeline(row, observations_list, *, ready_at, library_at, provider_record_confirmed):
    """Reproduce the retained provider-event timeline used by existing surfaces."""
    steps = [
        {
            "key": "requested",
            "label": "Request saved",
            "occurred_at": normalize_time(row["created_at"]),
        }
    ]
    code = row["code"]
    updated = normalize_time(row["updated_at"])
    if code == "acknowledgement_unconfirmed":
        steps.append(
            {"key": "received", "label": "Riven acknowledged the request", "occurred_at": updated}
        )
    phases = {}
    if (
        code == "requested"
        and provider_record_confirmed
        and not _observed(observations_list, "requested")
    ):
        phases["requested"] = updated
    if code in MOVIE_PROGRESS_LABELS and code != "requested":
        phases[code] = updated
    for observed_code, observed_at in observations_list:
        if observed_code in MOVIE_PROGRESS_LABELS:
            phases.setdefault(observed_code, normalize_time(observed_at))
    for phase, at in sorted(phases.items(), key=lambda entry: entry[1] or ""):
        if phase == "requested":
            if steps[-1]["key"] != "received":
                steps.append(
                    {
                        "key": "received",
                        "label": MOVIE_PROGRESS_LABELS[phase],
                        "occurred_at": at,
                    }
                )
        else:
            steps.append({"key": phase, "label": MOVIE_PROGRESS_LABELS[phase], "occurred_at": at})
    if ready_at is not None:
        steps.append({"key": "ready", "label": LABELS["ready"], "occurred_at": ready_at})
    elif library_at is not None:
        steps.append(
            {"key": "in_library", "label": LABELS["in_library"], "occurred_at": library_at}
        )
    return steps


def build_movie(
    row,
    *,
    title,
    library_at=None,
    in_library=False,
    ready_at=None,
    observations_list=(),
    candidates_rejected=None,
    candidates_considered=None,
    provider_record_confirmed=False,
):
    """Project one movie request row into the shared model."""
    code = row["code"]
    movie_href = f"/plus/movies/{row['imdb_id']}"
    entry_href = f"/plus/requests/movie/{row['imdb_id']}"
    timeline = _movie_timeline(
        row,
        observations_list,
        ready_at=normalize_time(ready_at),
        library_at=normalize_time(library_at),
        provider_record_confirmed=provider_record_confirmed,
    )
    created = normalize_time(row["created_at"])
    updated = normalize_time(row["updated_at"])
    checked = _latest(*(occurred_at for _, occurred_at in observations_list))
    progress_order = ("requested", "searching", "search_complete", "acquiring", "verifying")
    if code in progress_order or code in {"submission_pending", "acknowledgement_unconfirmed"}:
        rank = progress_order.index(code) if code in progress_order else -1
        for observed_code, observed_at in observations_list:
            if observed_code in progress_order and progress_order.index(observed_code) > rank:
                rank = progress_order.index(observed_code)
                code = observed_code
                updated = normalize_time(observed_at) or updated
    confirmed = _latest(updated, ready_at, library_at)
    reason = None
    reason_text = None
    unknown_reason = False
    stopped = None
    stop_time = None
    current = None
    retry_reason = None
    removed = False

    if code == "copy_removed":
        removed_at = _latest(updated, created)
        done = {"saved": created, "library": removed_at}
        return build(
            kind="movie",
            title_id=row["imdb_id"],
            title=title,
            state="removed",
            label=LABELS["removed"],
            status_label=LABELS["removed"],
            detail=row["detail"],
            category="history",
            done=done,
            resolved=True,
            removed=True,
            last_checked_at=checked,
            last_confirmed_at=removed_at,
            candidates_rejected=candidates_rejected,
            candidates_considered=candidates_considered,
            href=movie_href,
            detail_href=entry_href,
            legacy_timeline=timeline,
        )

    if ready_at is not None:
        state, detail = "ready", "The matching copy passed its playback check."
        done = {
            "saved": created,
            "received": updated,
            "searched": updated,
            "acquired": updated,
            "library": normalize_time(ready_at),
        }
        category, resolved, status_label = "history", True, LABELS["ready"]
    elif library_at is not None or in_library:
        state, detail = "in_library", "A matching copy is in your Movies library."
        confirmed_library = normalize_time(library_at) or updated
        done = {
            "saved": created,
            "received": updated,
            "searched": updated,
            "acquired": confirmed_library,
            "library": confirmed_library,
        }
        category, resolved, status_label = "history", True, LABELS["in_library"]
    elif code in _MOVIE_LIBRARY_STOPPED:
        state = "preparing"
        detail = "Waiting for a matching copy to appear in your library."
        done = {
            "saved": created,
            "received": _received_at(observations_list, updated),
            "searched": _searched_at(observations_list, updated),
            "acquired": updated,
        }
        stopped = "library"
        stop_time = updated
        reason = row["detail"]
        category, resolved, status_label = "attention", False, LABELS["needs_attention"]
    elif code in _MOVIE_SERVICE_STOPPED:
        state = "needs_attention"
        if code in {"no_eligible_results", "backend_failed", "investigation_exhausted"}:
            detail = "A matching copy could not be confirmed. Review the request."
        else:
            detail = "The latest status could not be confirmed. Check the request for an update."
        timeline.append(
            {"key": "needs_attention", "label": LABELS["needs_attention"], "occurred_at": updated}
        )
        done = {"saved": created}
        if code != "submission_pending":
            done["received"] = _received_at(observations_list, updated)
        if code in _MOVIE_SEARCHED or _observed(observations_list, "search_complete"):
            done["searched"] = _searched_at(observations_list, updated)
        stopped = "acquired" if "searched" in done else "searched"
        stop_time = updated
        unknown_reason = True
        reason_text = (
            f"{row['detail']} The exact provider reason for this stop was not supplied."
            if row["detail"]
            else UNKNOWN_REASON_TEXT
        )
        category, resolved, status_label = "attention", False, LABELS["needs_attention"]
        if code in _RETRYABLE:
            retry_reason = "A retry rechecks the existing request; it cannot create a second one."
    elif code in _MOVIE_PREPARING:
        state, detail = "preparing", "Waiting for a matching copy to appear in your library."
        done = {"saved": created, "received": _received_at(observations_list, updated)}
        if code in _MOVIE_SEARCHED:
            done["searched"] = _searched_at(observations_list, updated)
        if code == "verifying":
            done["acquired"] = updated
        if code == "searching":
            current = "searched"
        elif code == "acquiring":
            current = "acquired"
        elif code in {"verifying"}:
            current = "library"
        category, resolved, status_label = "active", False, LABELS["preparing"]
    elif code == "unreleased":
        state, detail = "requested", "Waiting for the movie to be released."
        done = {"saved": created}
        category, resolved, status_label = "active", False, LABELS["requested"]
    elif code == "acknowledgement_unconfirmed" and not _observed(observations_list, "requested"):
        state = "requested"
        detail = "Riven accepted the submission. A saved request in Riven is still unconfirmed."
        done = {"saved": created}
        category, resolved, status_label = "active", False, "Awaiting confirmation"
    elif code == "requested" or code in _MOVIE_WAITING:
        state = "requested"
        done = {"saved": created}
        observed_received = _observed(observations_list, "requested") or (
            updated if code == "requested" and provider_record_confirmed else None
        )
        if observed_received:
            done["received"] = observed_received
            status_label = "Received by Riven"
            detail = (
                "Riven has the request. A search result and matching copy are still unconfirmed."
            )
        else:
            status_label = "Request saved"
            detail = "Omakase saved the request. Riven has yet to confirm receipt."
        category, resolved = "active", False
    else:
        state = "needs_attention"
        detail = "The latest status could not be confirmed. Check the request for an update."
        timeline.append(
            {"key": "needs_attention", "label": LABELS["needs_attention"], "occurred_at": updated}
        )
        done = {"saved": created, "received": _received_at(observations_list, updated)}
        stopped = "acquired" if "searched" in done else "searched"
        stop_time = updated
        unknown_reason = True
        category, resolved, status_label = "attention", False, LABELS["needs_attention"]

    rejected = candidates_rejected
    if rejected is None:
        rejected = sum(1 for entry in observations_list if entry[0] == "candidate_rejected")
        rejected = rejected or None
    next_action = None
    if resolved or removed:
        next_action = None
    elif stopped is not None:
        if retry_reason is not None:
            next_action = _next_action("retry", "Retry request", retry_reason)
    else:
        next_action = _next_action("check", "Check status")
    return build(
        kind="movie",
        title_id=row["imdb_id"],
        title=title,
        state=state,
        label=LABELS[state],
        status_label=status_label,
        detail=detail,
        category=category,
        done=done,
        stopped=stopped,
        stop_time=stop_time,
        current=current,
        reason=reason,
        unknown_reason=unknown_reason,
        reason_text=reason_text,
        last_checked_at=checked,
        last_confirmed_at=confirmed,
        next_action=next_action,
        can_check=not resolved and not removed,
        can_retry=retry_reason is not None,
        resolved=resolved,
        removed=removed,
        candidates_rejected=rejected,
        candidates_considered=candidates_considered,
        href=movie_href,
        detail_href=entry_href,
        legacy_timeline=timeline,
    )


# ---------------------------------------------------------------------------
# Anime workflow: saved -> searched -> acquired -> library
# ---------------------------------------------------------------------------


def build_anime(
    *,
    anilist_id,
    title,
    created_at,
    monitor,
    last_run=None,
    playable=False,
    withdrawn=False,
    provider_connected=True,
):
    """Project one anime request and its monitor into the shared model."""
    created = normalize_time(created_at)
    last_checked = normalize_time(monitor["last_checked_at"]) if monitor is not None else None
    confirmed = created
    if monitor is not None:
        confirmed = (
            _latest(
                last_checked,
                monitor["last_success_at"],
                monitor["updated_at"],
                created,
            )
            or created
        )
    done = {"saved": created}
    stopped = None
    stop_time = None
    current = None
    reason = None
    unknown_reason = False
    reason_text = None
    status = "requested"
    detail = "Waiting for a playable release or the next media check."
    category = "active"
    resolved = False
    accepted = accepted_count(last_run)
    if monitor is not None:
        if last_checked:
            done["searched"] = last_checked
        if accepted:
            done["acquired"] = _latest(last_checked, created)
    if playable:
        done["library"] = _latest(last_checked, created)
        status = "ready"
        detail = "The first playable file has a verified match."
        category, resolved = "history", True
    elif withdrawn:
        status = "needs_attention"
        detail = "Acquisition stopped for this title."
        category, stopped, stop_time = "attention", "searched", last_checked or created
        reason = detail
    elif monitor is not None and not monitor["active"]:
        status = "needs_attention"
        detail = "No currently playable opening was verified."
        category, stopped, stop_time = "attention", "searched", last_checked or created
        reason = detail
    elif not provider_connected:
        status = "needs_attention"
        detail = "Connect Real-Debrid in Settings to prepare this request."
        category, stopped, stop_time = "attention", "searched", created
        reason = detail
    elif monitor is None:
        status = "needs_attention"
        detail = "The anime monitor is unavailable."
        category, stopped, stop_time = "attention", "searched", created
        reason = detail
    elif monitor["last_result"] == "SYNC_FAILED":
        status = "needs_attention"
        detail = "The last anime media check failed."
        category, stopped, stop_time = "attention", "searched", last_checked or created
        reason = detail
    elif last_run is not None and last_run["outcome"] == "degraded":
        status = "needs_attention"
        detail = "The most recent anime media check found no confirmed playable opening."
        category, stopped, stop_time = "attention", "searched", last_checked or created
        reason = detail
    elif accepted:
        status = "preparing"
        detail = "The anime pipeline found matching media. Playback is still being checked."
    elif not last_checked:
        status = "requested"
        detail = "Queued for the next scheduled anime media check."
    if category == "active" and resolved is False:
        if "acquired" in done:
            current = "library"
        elif "searched" in done:
            current = "acquired"
    next_action = None
    if not resolved:
        next_action = _next_action("check", "Check status")
    return build(
        kind="anime",
        title_id=anilist_id,
        title=title,
        state=status,
        label=LABELS[status],
        status_label=LABELS[status],
        detail=detail,
        category=category,
        done={key: value for key, value in done.items() if value},
        stopped=stopped,
        stop_time=stop_time,
        current=current,
        reason=reason,
        unknown_reason=unknown_reason,
        reason_text=reason_text,
        last_checked_at=last_checked,
        last_confirmed_at=confirmed,
        next_action=next_action,
        can_check=not resolved,
        can_retry=False,
        resolved=resolved,
        season=monitor["season_number"] if monitor is not None else None,
        href=f"/plus/library/{monitor['id']}"
        if monitor is not None
        else f"/plus/requests/anime/{anilist_id}",
        detail_href=f"/plus/requests/anime/{anilist_id}",
    )


def accepted_count(last_run):
    """Sum the anime media-sync evidence counters that prove media was found."""
    if last_run is None:
        return 0
    total = 0
    for name in ("accepted_count", "covered_count"):
        try:
            total += int(last_run[name] or 0)
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return total


# ---------------------------------------------------------------------------
# TV workflow: manual saved -> verified library copy
# ---------------------------------------------------------------------------


def build_tv(
    *,
    tmdb_id,
    title,
    created_at,
    library_at=None,
    library_source=None,
    provider=None,
    can_check=False,
):
    """Project confirmed TV request evidence and selected-library availability."""
    created = normalize_time(created_at)
    if provider is not None:
        snapshot = provider.get("snapshot") or {}
        label = (
            snapshot.get("status")
            or {
                "accepted": "Queued in Riven",
                "sending": "Sending to Riven",
                "unconfirmed": "Submission unconfirmed",
                "observed": "Received by Riven",
            }[provider["submission"]]
        )
        checked = provider.get("checked_at")
        ready = snapshot.get("ready", 0)
        complete = bool(ready and ready == snapshot.get("expected"))
        detail = "Riven accepted this request. Check status for episode progress."
        if snapshot:
            detail = "See the latest episode progress from Riven."
        elif provider["submission"] == "unconfirmed":
            detail = "Check Riven before sending this request again."
        elif provider["submission"] == "sending":
            detail = "Waiting for Riven to confirm receipt."
        if ready:
            article = "episode" if ready == 1 else "episodes"
            detail = f"{ready} {article} confirmed in {snapshot['library_name']} at the last check."
        if complete:
            label = "Ready to watch"
        pending_review = label in {"Needs attention", "No release found", "Submission unconfirmed"}
        model = build(
            kind="tv",
            title_id=tmdb_id,
            title=title,
            state="in_library" if complete else "preparing",
            label=label,
            status_label=label,
            detail=detail,
            category="history" if complete else ("attention" if pending_review else "active"),
            done={"saved": created, "received": checked, "library": checked if complete else None},
            supported=("saved", "received", "library"),
            last_checked_at=checked,
            last_confirmed_at=provider.get("changed_at") or created,
            can_check=can_check,
            resolved=complete,
            next_action=_next_action("check", "Check status") if can_check else None,
            href=f"/plus/tv/{tmdb_id}",
            detail_href=f"/plus/requests/tv/{tmdb_id}",
        )
        model["tv_provider"] = provider
        return model
    if library_at is not None and not can_check:
        done = {"saved": created, "library": normalize_time(library_at)}
        return build(
            kind="tv",
            title_id=tmdb_id,
            title=title,
            state="in_library",
            label=LABELS["in_library"],
            status_label=LABELS["in_library"],
            detail="A verified copy of this show is in your TV library.",
            category="history",
            done=done,
            resolved=True,
            last_checked_at=normalize_time(library_at),
            last_confirmed_at=normalize_time(library_at),
            manual=True,
            manual_note=None,
            href=f"/plus/tv/{tmdb_id}",
            detail_href=f"/plus/requests/tv/{tmdb_id}",
        )
    return build(
        kind="tv",
        title_id=tmdb_id,
        title=title,
        state="requested",
        label=LABELS["requested"],
        status_label=LABELS["requested"],
        detail="Saved for review."
        if not can_check
        else "This request is saved and waiting to be sent to Riven.",
        category="active",
        done={"saved": created},
        last_confirmed_at=created,
        manual=not can_check,
        can_check=can_check,
        can_retry=can_check,
        next_action=_next_action("retry", "Send to Riven") if can_check else None,
        href=f"/plus/tv/{tmdb_id}",
        detail_href=f"/plus/requests/tv/{tmdb_id}",
    )


# ---------------------------------------------------------------------------
# Dashboard grouping
# ---------------------------------------------------------------------------


def group(models):
    """Group model instances into the three dashboard categories."""
    grouped = {category: [] for category in CATEGORIES}
    for model in models:
        if model is None:
            continue
        grouped.setdefault(model["category"], []).append(model)
    for category in CATEGORIES:
        grouped[category].sort(
            key=lambda model: (
                model.get("last_confirmed_at") or "",
                model.get("title") or "",
            ),
            reverse=True,
        )
    return {
        "categories": [
            {
                "key": category,
                "title": {"active": "Active", "attention": "Needs attention", "history": "History"}[
                    category
                ],
                "requests": grouped[category],
            }
            for category in CATEGORIES
        ],
        "counts": {category: len(grouped[category]) for category in CATEGORIES},
    }


def empty_reason(model):
    """True when a stopped request has no verified reason to show."""
    return bool(model.get("stopped_at")) and bool(model.get("unknown_reason"))
