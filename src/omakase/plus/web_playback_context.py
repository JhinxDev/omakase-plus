"""Read-only player context and the optional trusted segment boundary.

The exact personal Plex metadata check supplies optional marker evidence.
Request bodies, durations and episode counts never manufacture segment evidence.
"""

from __future__ import annotations

import json
import math

from omakase.plus.anime_synopsis import cached_synopsis


def watch_context(db, monitor, scope, episode_number):
    """Only matching local catalog/publication data; never fetch a provider."""
    from omakase.plus.web_playback_navigation import _candidates, _url

    try:
        genres = json.loads(monitor["genres_json"])
        if not isinstance(genres, list):
            genres = []
        genres = [g for g in genres[:6] if isinstance(g, str) and 0 < len(g) <= 60]
    except (ValueError, TypeError, KeyError):
        genres = []
    try:
        candidates = _candidates(db, scope, monitor)
    except (ValueError, KeyError, TypeError, RecursionError):
        candidates = {}
    previous = episode_number - 1
    following = episode_number + 1
    return {
        **cached_synopsis(db, int(monitor["anilist_id"])),
        "genres": genres,
        "season_number": monitor["season_number"],
        "previous_url": _url(scope, monitor["id"], previous) if previous in candidates else None,
        "next_url": _url(scope, monitor["id"], following) if following in candidates else None,
    }


def normalize_skip_segments(source, evidence=None):
    """Fail closed unless an exact-source trusted producer supplies valid ranges.

    The public envelope is bounded to four ordered, non-overlapping intervals.
    Credits end at their marker, not at the file end, preserving post-credit media.
    """
    empty = {
        "media_version": source.media_version,
        "episode_number": source.episode_number,
        "segments": [],
    }
    if evidence is None:
        evidence = getattr(source, "_skip_evidence", None)
        if not isinstance(evidence, dict) or any(
            key not in evidence or evidence[key] != getattr(source, key, None)
            for key in (
                "source_identity",
                "metadata_key",
                "season_number",
                "episode_number",
                "media_version",
            )
        ):
            return empty
    if not isinstance(evidence, dict) or (
        evidence.get("media_version") != source.media_version
        or type(evidence.get("episode_number")) is not int
        or evidence["episode_number"] != source.episode_number
    ):
        return empty
    rows = evidence.get("segments")
    if not isinstance(rows, list) or len(rows) > 4:
        return empty
    result, prior_end = [], 0
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("kind"), str)
            or row["kind"] not in {"intro", "credits"}
        ):
            return empty
        start, end = row.get("start_seconds"), row.get("end_seconds")
        if not all(type(v) in {int, float} and math.isfinite(v) for v in (start, end)):
            return empty
        if not 0 <= prior_end <= start < end <= source.duration_ms / 1000:
            return empty
        result.append({"kind": row["kind"], "start_seconds": start, "end_seconds": end})
        prior_end = end
    return empty | {"segments": result}
