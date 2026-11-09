"""Bounded home summaries; the original activity ledger remains unchanged."""

from __future__ import annotations

import json

_REASONS = {
    "SELECTION_DEFERRED": (
        "Alternative not added",
        "The saved check does not record a more specific reason for keeping this alternative out of the library.",
    ),
    "PLEX_ENGLISH_SUBTITLE_MISSING": (
        "Required subtitles missing",
        "The checked release did not provide the required selectable English subtitles.",
    ),
    "PLEX_ASSET_COVERAGE_INVALID": (
        "Episode match needs attention",
        "The checked release did not match its expected episode coverage.",
    ),
    "PLEX_MEDIA_PROBE_FAILED": (
        "File could not be checked",
        "The saved file check failed. The release was not added to Plex.",
    ),
    "PLEX_DESTINATION_CONFLICT": (
        "Library destination conflict",
        "The release would conflict with another item in Plex.",
    ),
    "PLEX_SOURCE_UNAVAILABLE": (
        "Source unavailable",
        "The source folder was unavailable when this release was checked.",
    ),
    "PLEX_POLICY_EXCLUDED": (
        "Source excluded",
        "The configured publication policy excludes this source from Plex.",
    ),
}


def present_home_activity(db, events, *, include_checks=True):
    """Group only identical visible results, retaining every original attempt.

    Failure codes come from the exact run/monitor candidate record, never from
    another title's latest curator receipt. Unknown/missing causes stay unknown.
    """
    grouped = {}
    for event in events[:24]:
        if not include_checks and event["outcome"] in {"Check complete", "Checked existing files"}:
            continue
        codes = set()
        if event["outcome"] == "Publication Withheld":
            for row in db.execute(
                "SELECT reason FROM media_candidate_attempts WHERE sync_run_id=? "
                "AND monitor_id=? AND decision='PUBLICATION_WITHHELD' ORDER BY id LIMIT 16",
                (event["id"], event["monitor_id"]),
            ).fetchall():
                try:
                    if len(row["reason"]) > 2048:
                        continue
                    value = json.loads(row["reason"])
                    if (
                        isinstance(value, dict)
                        and type(value.get("v")) is int
                        and value["v"] == 1
                        and isinstance(value.get("c"), str)
                    ):
                        codes.add(value["c"])
                except (ValueError, TypeError, RecursionError):
                    continue
        label, detail = event["outcome"], event["detail"]
        if (
            not include_checks
            and label == "Publication Withheld"
            and (not codes or codes == {"SELECTION_DEFERRED"})
        ):
            continue
        if len(codes) == 1 and next(iter(codes)) in _REASONS:
            label, detail = _REASONS[next(iter(codes))]
            if not include_checks:
                detail = {
                    "Required subtitles missing": "The copy had no selectable English subtitles.",
                    "Episode match needs attention": "The copy did not match the episodes we expected.",
                    "File could not be checked": "Omakase couldn’t read the file.",
                    "Library destination conflict": "Another copy already uses this location.",
                    "Source unavailable": "Omakase couldn’t reach the source.",
                    "Source excluded": "This source is outside your library settings.",
                }.get(label, detail)
        elif label == "Publication Withheld":
            label = "Alternative not added"
            if detail.startswith("This release was not selected."):
                detail = "The saved check does not record a more specific reason for this result."
        key = (event["monitor_id"], label, detail, tuple(sorted(codes)))
        if key not in grouped:
            grouped[key] = {**event, "outcome": label, "detail": detail, "attempts": []}
        grouped[key]["attempts"].append(dict(event))
    return [{**row, "repeat_count": len(row["attempts"])} for row in grouped.values()]
