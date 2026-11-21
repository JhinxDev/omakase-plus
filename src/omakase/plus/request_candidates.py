"""Omakase's own candidate search for a request, with a verdict per candidate.

Why this exists
---------------
The request page used to stop at "Received by Riven" because the only evidence available was
Riven's projection, and that projection carries no per-candidate detail: Omakase could not
say whether a search ran, what it found, or why a candidate was unusable. The owner's
direction is that Omakase runs its own search, judges what it finds, and reports that truth;

Riven stays one path, not the only source of truth.

This module is the search-and-judge half. It is deliberately *read-only about the world*:
it searches the tracked indexers, records what it found and why each candidate was rejected,
and leaves acquisition to the caller. Search and acquisition are separately gated
(`OMAKASE_REQUEST_SEARCH`, `OMAKASE_REQUEST_ACQUIRE`) because starting real downloads is a
different decision from telling the truth about a search.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

SEARCH_ENV = "OMAKASE_REQUEST_SEARCH"
ACQUIRE_ENV = "OMAKASE_REQUEST_ACQUIRE"
REFRESH_ENV = "OMAKASE_REQUEST_SEARCH_REFRESH_HOURS"
DEFAULT_REFRESH_HOURS = 6.0
SEARCH_TIMEOUT_SECONDS = 25.0
ERROR_CODE = "search_error"
EMPTY_CODE = "search_empty"
NO_SOURCES_CODE = "search_no_sources"
PARTIAL_CODE = "search_partial"
_SNAPSHOT_MARKERS = {ERROR_CODE, EMPTY_CODE, NO_SOURCES_CODE, PARTIAL_CODE}

# Every rejection carries one of these codes so the page never has to invent wording.
REASON_TEXT = {
    "no_download": "no downloadable link was published for it",
    "no_seeders": "nobody is seeding it",
    "title_mismatch": "the release name does not match the title",
    "year_mismatch": "the release year does not match the title",
    "episode_mismatch": "the release is not the episode that was requested",
    "too_large": "it is larger than the size limit for this kind of title",
    "unreleased": "the release is marked as not yet released",
}

_SEPARATORS = re.compile(r"[^a-z0-9]+")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SEASON_EPISODE = re.compile(r"\bs(\d{1,2})[ ._-]?e(\d{1,3})\b", re.IGNORECASE)
_EPISODE_ONLY = re.compile(r"\b(?:e|ep)[ ._-]?(\d{1,3})\b", re.IGNORECASE)
_NOISE = frozenset(
    {
        "1080p",
        "2160p",
        "720p",
        "480p",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "av1",
        "aac",
        "ac3",
        "dts",
        "flac",
        "opus",
        "10bit",
        "8bit",
        "web",
        "webrip",
        "webdl",
        "bluray",
        "bdrip",
        "remux",
        "hdr",
        "dv",
        "proper",
        "repack",
        "multi",
        "dual",
        "subs",
        "sub",
        "dub",
        "uncensored",
        "unc",
        "batch",
        "complete",
        "amzn",
        "cr",
        "nf",
        "hulu",
        "hidive",
        "vk",
        "mkv",
        "mp4",
    }
)


def search_enabled() -> bool:
    """Omakase's own search runs only when the owner has switched it on."""
    return os.getenv(SEARCH_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


def acquire_enabled() -> bool:
    """Acquiring what the search finds is a separate switch, off unless chosen."""
    return os.getenv(ACQUIRE_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


def normalize(value: object) -> str:
    """Lowercase alphanumeric words, so punctuation and release tags cannot hide a match."""
    text = _SEPARATORS.sub(" ", str(value or "").casefold())
    return " ".join(word for word in text.split() if word and word not in _NOISE)


def _identity_tokens(value: object) -> list[str]:
    return [word for word in normalize(value).split() if word not in _NOISE]


@dataclass(frozen=True)
class Verdict:
    """One candidate's fate, in the words the page will show."""

    accepted: bool
    code: str
    detail: str

    @property
    def text(self) -> str:
        return REASON_TEXT.get(self.code, self.detail or "no reason was recorded")


@dataclass(frozen=True)
class JudgeRequest:
    """What the request asked for, reduced to what judgement needs."""

    title: str
    kind: str = "movie"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    maximum_bytes: int = 0

    @property
    def episode_expected(self) -> bool:
        return self.kind in {"tv", "anime"} and self.episode is not None


def judge(candidate: object, request: JudgeRequest) -> Verdict:
    """Decide whether one candidate can serve this request, and say why not.

    Order matters: a missing download link is fatal before anything else is considered,
    then availability, then identity. Each rejection names one reason, which is what makes
    the request page useful instead of vague.
    """
    title = str(getattr(candidate, "title", "") or "")
    magnet = str(getattr(candidate, "magnet", "") or "")
    torrent_url = str(getattr(candidate, "torrent_url", "") or "")
    if not magnet and not torrent_url:
        return Verdict(False, "no_download", "")
    if int(getattr(candidate, "seeders", 0) or 0) <= 0:
        return Verdict(False, "no_seeders", "")

    wanted = _identity_tokens(request.title)
    found = _identity_tokens(title)
    if wanted and not all(token in found for token in wanted):
        return Verdict(False, "title_mismatch", "")

    if request.year:
        years = {int(match) for match in _YEAR.findall(title)}
        if (
            years
            and request.year not in years
            and request.year - 1 not in years
            and request.year + 1 not in years
        ):
            return Verdict(False, "year_mismatch", "")

    if request.episode_expected:
        season_hit = _SEASON_EPISODE.search(title)
        if season_hit:
            season, episode = int(season_hit.group(1)), int(season_hit.group(2))
            if season != int(request.season or 1) or episode != int(request.episode or 0):
                return Verdict(False, "episode_mismatch", "")
        else:
            single = _EPISODE_ONLY.search(title)
            is_batch = bool(getattr(candidate, "is_batch", False))
            if single and int(single.group(1)) != int(request.episode or 0):
                return Verdict(False, "episode_mismatch", "")
            if not single and not is_batch:
                return Verdict(False, "episode_mismatch", "")

    size = int(getattr(candidate, "size_bytes", 0) or 0)
    if request.maximum_bytes and size > request.maximum_bytes:
        return Verdict(False, "too_large", "")
    return Verdict(True, "accepted", "")


def summarize(verdicts: Sequence[Verdict]) -> dict[str, object]:
    """Counts plus one plain sentence set, ready for the page and the status endpoint."""
    accepted = sum(1 for verdict in verdicts if verdict.accepted)
    rejected: dict[str, int] = {}
    for verdict in verdicts:
        if not verdict.accepted:
            rejected[verdict.code] = rejected.get(verdict.code, 0) + 1
    reasons = [
        {"code": code, "count": count, "text": REASON_TEXT.get(code, "no reason was recorded")}
        for code, count in sorted(rejected.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "found": len(verdicts),
        "accepted": accepted,
        "rejected": len(verdicts) - accepted,
        "reasons": reasons,
    }


def describe(summary: dict[str, object]) -> str:
    """The one sentence the request page shows, never overstating what is known."""
    found = int(summary.get("found", 0) or 0)
    accepted = int(summary.get("accepted", 0) or 0)
    if not found:
        return "Omakase searched the tracked indexers and found no candidate for this title."
    if accepted:
        return (
            f"Omakase searched the tracked indexers and found {found} "
            f"{'candidate' if found == 1 else 'candidates'}; {accepted} could be used."
        )
    return (
        f"Omakase searched the tracked indexers and found {found} "
        f"{'candidate' if found == 1 else 'candidates'}, and none of them could be used."
    )


def record(
    db,
    user_id: int,
    kind: str,
    title_id: str,
    verdicts: Iterable[tuple[object, Verdict]],
    *,
    marker: str | None = None,
) -> int:
    """Replace this request's candidate snapshot with the current search result.

    A snapshot, not a log: the page states what the last search found. Rejections are only
    recorded as verified when a candidate was actually seen, which is the evidence the Riven
    projection never supplied.
    """
    rows = list(verdicts)
    db.execute(
        "DELETE FROM request_candidates WHERE user_id=? AND kind=? AND title_id=?",
        (user_id, kind, str(title_id)),
    )
    observed = time.time()
    for candidate, verdict in rows:
        db.execute(
            "INSERT INTO request_candidates(user_id, kind, title_id, release_title, indexer,"
            " size_bytes, seeders, accepted, reason_code, observed_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                user_id,
                kind,
                str(title_id),
                str(getattr(candidate, "title", "") or "")[:400],
                str(getattr(candidate, "indexer", "") or "")[:120],
                int(getattr(candidate, "size_bytes", 0) or 0),
                int(getattr(candidate, "seeders", 0) or 0),
                1 if verdict.accepted else 0,
                verdict.code,
                observed,
            ),
        )
    if marker is not None:
        db.execute(
            "INSERT INTO request_candidates(user_id, kind, title_id, release_title, "
            "reason_code, observed_at) VALUES(?,?,?,?,?,?)",
            (user_id, kind, str(title_id), "", marker, observed),
        )
    db.commit()
    return len(rows)


def snapshot(db, user_id: int, kind: str, title_id: str) -> list[dict[str, object]]:
    """The stored candidates for one request, newest snapshot only."""
    rows = db.execute(
        "SELECT release_title, indexer, size_bytes, seeders, accepted, reason_code, observed_at"
        " FROM request_candidates WHERE user_id=? AND kind=? AND title_id=?"
        " ORDER BY accepted DESC, seeders DESC, id",
        (user_id, kind, str(title_id)),
    ).fetchall()
    return [dict(row) for row in rows]


def page_block(db, user_id: int, kind: str, title_id: str) -> dict[str, object]:
    """Everything the request page needs to state the search honestly, or say it is off."""
    if not search_enabled():
        return {
            "enabled": False,
            "headline": (
                "Omakase's own search is off. This page reports only what the acquisition "
                "service supplies, so it cannot say what a search found."
            ),
            "candidates": [],
        }
    rows = snapshot(db, user_id, kind, title_id)
    markers = {str(row["reason_code"]) for row in rows} & _SNAPSHOT_MARKERS
    rows = [row for row in rows if str(row["reason_code"]) not in _SNAPSHOT_MARKERS]
    verdicts = [Verdict(bool(row["accepted"]), str(row["reason_code"]), "") for row in rows]
    summary = summarize(verdicts)
    candidates = [
        {
            "title": row["release_title"],
            "indexer": row["indexer"],
            "seeders": row["seeders"],
            "size_bytes": row["size_bytes"],
            "accepted": bool(row["accepted"]),
            "text": REASON_TEXT.get(str(row["reason_code"]), "no reason was recorded"),
            "observed_at": row["observed_at"],
        }
        for row in rows
    ]
    block = {
        "enabled": True,
        "headline": describe(summary),
        "summary": summary,
        "candidates": candidates,
    }
    if NO_SOURCES_CODE in markers:
        block["headline"] = "No release sources are configured for Omakase's own request search."
    elif ERROR_CODE in markers:
        # Older versions also recorded this marker for empty successes. Don't
        # claim the indexers were unreachable when the cause isn't known.
        block["headline"] = (
            "Omakase could not confirm a result from its own search. This page cannot "
            "say what exists yet."
        )
        block["error"] = True
    elif PARTIAL_CODE in markers:
        found = int(summary["found"])
        noun = "candidate" if found == 1 else "candidates"
        result = f"found {found} {noun}" if found else "found no candidates"
        block["headline"] = (
            f"Omakase {result} in the sources that responded. "
            "Some sources did not complete the search."
        )
        block["partial"] = True
    elif not rows and EMPTY_CODE not in markers:
        block["headline"] = "Omakase has not recorded a result from its own search yet."
    return block


def _refresh_hours() -> float:
    raw = os.getenv(REFRESH_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_REFRESH_HOURS
    except ValueError:
        return DEFAULT_REFRESH_HOURS
    return value if value >= 0 else DEFAULT_REFRESH_HOURS


def is_fresh(db, user_id: int, kind: str, title_id: str) -> bool:
    """True when the stored snapshot is recent enough that another search is wasteful."""
    rows = snapshot(db, user_id, kind, title_id)
    if not rows:
        return False
    newest = max(float(row["observed_at"] or 0) for row in rows)
    return (time.time() - newest) < _refresh_hours() * 3600


async def refresh(
    db,
    user_id: int,
    kind: str,
    title_id: str,
    request: JudgeRequest,
    *,
    catalogs=None,
    force: bool = False,
    timeout: float = SEARCH_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Search, judge and record, then return the page block for the request page.

    Bounded twice: sources are searched concurrently under one deadline, and a fresh
    snapshot is not re-searched at all. A failed run records that it failed instead of
    leaving an empty snapshot that would read as "nothing exists".
    """
    import asyncio

    if not search_enabled():
        return page_block(db, user_id, kind, title_id)
    if not force and is_fresh(db, user_id, kind, title_id):
        return page_block(db, user_id, kind, title_id)

    if catalogs is None:
        from omakase.plus.catalog_config import configured_catalogs

        catalogs = configured_catalogs(db, user_id, kind=kind).sources

    async def one(source):
        return tuple(await source.search(request.title))

    tasks = [asyncio.create_task(one(source)) for source in catalogs]
    if not tasks:
        record(db, user_id, kind, title_id, [], marker=NO_SOURCES_CODE)
        return page_block(db, user_id, kind, title_id)

    try:
        completed, _ = await asyncio.wait(tasks, timeout=timeout)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)

    candidates = []
    successes = 0
    for task, result in zip(tasks, results, strict=True):
        if task not in completed or isinstance(result, BaseException):
            continue
        successes += 1
        candidates.extend(result)

    marker = None
    if not successes:
        marker = ERROR_CODE
    elif successes != len(tasks):
        marker = PARTIAL_CODE
    elif not candidates:
        marker = EMPTY_CODE
    record(
        db,
        user_id,
        kind,
        title_id,
        [(item, judge(item, request)) for item in candidates],
        marker=marker,
    )
    return page_block(db, user_id, kind, title_id)
