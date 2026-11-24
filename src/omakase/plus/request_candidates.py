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

import asyncio
import html
import os
import re
import time
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from weakref import WeakValueDictionary

from omakase.plus.btih import torrent_hash_from_magnet
from omakase.plus.catalog import quality_from_title

SEARCH_ENV = "OMAKASE_REQUEST_SEARCH"
ACQUIRE_ENV = "OMAKASE_REQUEST_ACQUIRE"
REFRESH_ENV = "OMAKASE_REQUEST_SEARCH_REFRESH_HOURS"
DEFAULT_REFRESH_HOURS = 6.0
INCOMPLETE_REFRESH_SECONDS = 60
_SEARCH_LOCKS: WeakValueDictionary[tuple[str | int, int, str, str], asyncio.Lock] = (
    WeakValueDictionary()
)
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
    "insufficient_seeders": "it has fewer seeders than your minimum",
    "quality_excluded": "the resolution is missing or outside your allowed qualities",
    "title_mismatch": "the release name does not match the title",
    "year_mismatch": "the release year does not match the title",
    "episode_mismatch": "the release is not the episode that was requested",
    "too_large": "it is larger than the size limit for this kind of title",
    "unreleased": "the release is marked as not yet released",
    "bad_quality": "it is a low-quality cam, screener, or sample release",
    "accepted_low_seeders": "accepted on fallback with fewer seeders",
    "accepted_quality_fallback": "accepted on fallback at standard or 720p quality",
}

_SEPARATORS = re.compile(r"[^a-z0-9]+")
_YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
_SEASON_EPISODE = re.compile(r"\bs(\d{1,2})[ ._-]?e(\d{1,3})\b", re.IGNORECASE)
_SEASON_RANGE = re.compile(r"\bs(\d{1,2})\s*(?:-|to|thru)\s*s?(\d{1,2})\b", re.IGNORECASE)
_SEASON_ONLY = re.compile(r"\b(?:s(\d{1,2})|season[ ._-]?(\d{1,2}))\b", re.IGNORECASE)
_EPISODE_ONLY = re.compile(r"\b(?:e|ep)[ ._-]?(\d{1,3})\b", re.IGNORECASE)
_BAD_QUALITY = re.compile(
    r"\b(?:cam|hdcam|camrip|hdts|telesync|telecine|dvdscr|screener|sample|trailer)\b",
    re.IGNORECASE,
)
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


def _fold_text(value: object) -> str:
    raw = html.unescape(str(value or ""))
    return "".join(ch for ch in unicodedata.normalize("NFKD", raw) if not unicodedata.combining(ch))


def normalize(value: object) -> str:
    """Lowercase ASCII-folded alphanumeric words, so punctuation and tags cannot hide a match."""
    text = _SEPARATORS.sub(" ", _fold_text(value).casefold())
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
        if self.code == "accepted_low_seeders":
            return REASON_TEXT[self.code]
        if self.code == "accepted_quality_fallback":
            return REASON_TEXT[self.code]
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
    minimum_seeders: int = 1
    allowed_resolutions: tuple[str, ...] = ()
    alternate_titles: tuple[str, ...] = ()
    season_pack: bool = False
    tiered_fallback: bool = False

    @property
    def episode_expected(self) -> bool:
        return self.kind in {"tv", "anime"} and self.episode is not None and not self.season_pack


def _title_matches(found: Sequence[str], request: JudgeRequest) -> bool:
    candidates = [_identity_tokens(request.title)]
    for alt in request.alternate_titles:
        tokens = _identity_tokens(alt)
        if tokens:
            candidates.append(tokens)
    non_empty = [tokens for tokens in candidates if tokens]
    if not non_empty:
        return True
    return any(all(token in found for token in wanted) for wanted in non_empty)


def _season_pack_matches(folded_title: str, target_season: int) -> bool:
    if _SEASON_EPISODE.search(folded_title):
        return False
    for m_range in _SEASON_RANGE.finditer(folded_title):
        if int(m_range.group(1)) <= target_season <= int(m_range.group(2)):
            return True
    seasons = {int(g1 or g2) for g1, g2 in _SEASON_ONLY.findall(folded_title)}
    if target_season in seasons:
        return True
    return bool(not seasons and re.search(r"\bcomplete\b", folded_title, re.IGNORECASE))


def judge(candidate: object, request: JudgeRequest) -> Verdict:
    """Decide whether one candidate can serve this request, and say why not.

    Order matters: a missing download link is fatal before anything else is considered,
    then availability, then identity. Each rejection names one reason, which is what makes
    the request page useful instead of vague.
    """
    title = _fold_text(getattr(candidate, "title", "") or "")
    magnet = str(getattr(candidate, "magnet", "") or "")
    torrent_url = str(getattr(candidate, "torrent_url", "") or "")
    if not magnet and not torrent_url:
        return Verdict(False, "no_download", "")
    seeders = int(getattr(candidate, "seeders", 0) or 0)
    if seeders <= 0:
        return Verdict(False, "no_seeders", "")
    if seeders < request.minimum_seeders:
        return Verdict(False, "insufficient_seeders", "")
    if _BAD_QUALITY.search(title):
        return Verdict(False, "bad_quality", "")

    found = _identity_tokens(title)
    if not _title_matches(found, request):
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

    if request.season_pack:
        if not _season_pack_matches(title, int(request.season or 1)):
            return Verdict(False, "episode_mismatch", "")
    elif request.episode_expected:
        season_hit = _SEASON_EPISODE.search(title)
        if season_hit:
            season, episode = int(season_hit.group(1)), int(season_hit.group(2))
            if season != int(request.season or 1) or episode != int(request.episode or 0):
                return Verdict(False, "episode_mismatch", "")
        elif request.kind == "tv":
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
    if request.allowed_resolutions and quality_from_title(title) not in request.allowed_resolutions:
        return Verdict(False, "quality_excluded", "")
    return Verdict(True, "accepted", "")


def evaluate_candidates(
    candidates: Sequence[object], request: JudgeRequest
) -> list[tuple[object, Verdict]]:
    """Judge candidates strictly first, then relax seeders and 720p/clean-SD only if empty."""
    tier1 = [(item, judge(item, request)) for item in candidates]
    if not request.tiered_fallback or any(verdict.accepted for _, verdict in tier1):
        return tier1
    if request.minimum_seeders > 1:
        relaxed_seeders = replace(request, minimum_seeders=1)
        tier2 = [
            (
                item,
                Verdict(True, "accepted_low_seeders", "")
                if (verdict := judge(item, relaxed_seeders)).accepted
                else verdict,
            )
            for item in candidates
        ]
        if any(verdict.accepted for _, verdict in tier2):
            return tier2
    if request.allowed_resolutions:
        relaxed_quality = replace(request, minimum_seeders=1, allowed_resolutions=())
        tier3 = [
            (
                item,
                Verdict(True, "accepted_quality_fallback", "")
                if (verdict := judge(item, relaxed_quality)).accepted
                else verdict,
            )
            for item in candidates
        ]
        if any(verdict.accepted for _, verdict in tier3):
            return tier3
    return tier1


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
        return "The configured sources returned no releases for this title."
    if accepted:
        return (
            f"{found} {'release' if found == 1 else 'releases'} found. "
            f"{accepted} passed the release checks."
        )
    return (
        f"{found} {'release' if found == 1 else 'releases'} found. None passed the release checks."
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
    block, rows = _assess_snapshot(snapshot(db, user_id, kind, title_id))
    block["candidates"] = [
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
    return block


def _assess_snapshot(rows):
    markers = {str(row["reason_code"]) for row in rows} & _SNAPSHOT_MARKERS
    checked_at = (
        datetime.fromtimestamp(max(row["observed_at"] for row in rows), UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
        if rows and NO_SOURCES_CODE not in markers
        else None
    )
    rows = [row for row in rows if str(row["reason_code"]) not in _SNAPSHOT_MARKERS]
    verdicts = [Verdict(bool(row["accepted"]), str(row["reason_code"]), "") for row in rows]
    summary = summarize(verdicts)
    block = {
        "enabled": True,
        "headline": describe(summary),
        "summary": summary,
        "outcome": "complete",
        "checked_at": checked_at,
    }
    if NO_SOURCES_CODE in markers:
        block["headline"] = "No release sources are configured for Omakase's own request search."
        block["outcome"] = "unconfigured"
    elif ERROR_CODE in markers:
        # Older versions also recorded this marker for empty successes. Don't
        # claim the indexers were unreachable when the cause isn't known.
        block["headline"] = (
            "Omakase could not confirm a result from its own search. This page cannot "
            "say what exists yet."
        )
        block["error"] = True
        block["outcome"] = "unavailable"
    elif PARTIAL_CODE in markers:
        found = int(summary["found"])
        noun = "candidate" if found == 1 else "candidates"
        result = f"found {found} {noun}" if found else "found no candidates"
        block["headline"] = (
            f"Omakase {result} in the sources that responded. "
            "Some sources did not complete the search."
        )
        block["partial"] = True
        block["outcome"] = "partial"
    elif not rows and EMPTY_CODE not in markers:
        block["headline"] = "Omakase has not recorded a result from its own search yet."
        block["outcome"] = "unchecked"
    return block, rows


def attach_search_summaries(db, user_id, models):
    """Load compact, owner-scoped evidence for the bounded Requests result set."""
    if not models or not search_enabled():
        return
    keys = {(model["kind"], model["title_id"]) for model in models}
    values_sql = ", ".join("(?, ?)" for _ in keys)
    params = [item for key in keys for item in key]
    rows = db.execute(
        f"""WITH requested(kind, title_id) AS (VALUES {values_sql})
        SELECT kind, title_id, accepted, reason_code, observed_at
        FROM request_candidates JOIN requested USING (kind, title_id)
        WHERE user_id=?""",
        (*params, user_id),
    )
    by_request = {}
    for row in rows:
        by_request.setdefault((row["kind"], row["title_id"]), []).append(row)
    for model in models:
        records = by_request.get((model["kind"], model["title_id"]))
        if records:
            block, _ = _assess_snapshot(records)
            model["fallback_search"] = {
                "headline": block["headline"],
                "outcome": block["outcome"],
                "checked_at": block["checked_at"],
                "found": block["summary"]["found"],
                "eligible": block["summary"]["accepted"],
            }


def _refresh_hours() -> float:
    raw = os.getenv(REFRESH_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_REFRESH_HOURS
    except ValueError:
        return DEFAULT_REFRESH_HOURS
    return value if value >= 0 else DEFAULT_REFRESH_HOURS


def is_fresh(db, user_id: int, kind: str, title_id: str, *, max_age=None) -> bool:
    """True when the stored snapshot is recent enough that another search is wasteful."""
    rows = snapshot(db, user_id, kind, title_id)
    if not rows:
        return False
    newest = max(float(row["observed_at"] or 0) for row in rows)
    seconds = _refresh_hours() * 3600
    incomplete = {ERROR_CODE, PARTIAL_CODE, NO_SOURCES_CODE}
    if any(row["reason_code"] in incomplete for row in rows):
        seconds = min(INCOMPLETE_REFRESH_SECONDS, seconds)
    if max_age is not None:
        seconds = min(max_age, seconds)
    return (time.time() - newest) < seconds


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
    on_matches=None,
) -> dict[str, object]:
    """Share one bounded search per active request and reuse a fresh snapshot."""
    if not search_enabled():
        return page_block(db, user_id, kind, title_id)
    database = db.execute("PRAGMA database_list").fetchone()[2] or id(db)
    lock = _SEARCH_LOCKS.setdefault((database, user_id, kind, str(title_id)), asyncio.Lock())
    async with lock:
        max_age = INCOMPLETE_REFRESH_SECONDS if on_matches is not None else None
        if not force and is_fresh(db, user_id, kind, title_id, max_age=max_age):
            return page_block(db, user_id, kind, title_id)
        return await _run_search(
            db, user_id, kind, title_id, request, catalogs, timeout, on_matches
        )


def _primary_queries(request: JudgeRequest) -> list[str]:
    if request.season_pack:
        return [f"{request.title} S{int(request.season or 1):02d}"]
    if request.kind == "tv" and request.episode_expected:
        return [f"{request.title} S{int(request.season or 1):02d}E{request.episode:02d}"]
    queries: list[str] = []
    seen: set[str] = set()
    for raw in (request.title, *(_fold_text(alt) for alt in request.alternate_titles)):
        cleaned = raw.strip()
        key = normalize(cleaned) or cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            queries.append(cleaned)
    return queries or [request.title]


def _dedupe_candidates(candidates: Sequence[object]) -> list[object]:
    best: dict[tuple[str, str, int], object] = {}
    for item in candidates:
        title = getattr(item, "title", "") or ""
        magnet = str(getattr(item, "magnet", "") or "")
        url = str(getattr(item, "torrent_url", "") or "").strip()
        digest = torrent_hash_from_magnet(magnet) or url
        key = (digest, normalize(title), int(getattr(item, "size_bytes", 0) or 0))
        prev = best.get(key)
        if prev is None or (int(getattr(item, "seeders", 0) or 0), bool(magnet)) > (
            int(getattr(prev, "seeders", 0) or 0),
            bool(getattr(prev, "magnet", "")),
        ):
            best[key] = item
    return list(best.values())


async def _gather_queries(
    catalogs, queries: Sequence[str], *, timeout: float
) -> tuple[list[object], int, int]:
    async def one(source, query):
        return tuple(await source.search(query))

    tasks = [asyncio.create_task(one(source, query)) for query in queries for source in catalogs]
    if not tasks:
        return [], 0, 0
    try:
        completed, _ = await asyncio.wait(tasks, timeout=timeout)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    candidates: list[object] = []
    successes = 0
    for task, result in zip(tasks, results, strict=True):
        if task not in completed or isinstance(result, BaseException):
            continue
        successes += 1
        candidates.extend(result)
    return candidates, successes, len(tasks)


async def _run_search(db, user_id, kind, title_id, request, catalogs, timeout, on_matches):
    if catalogs is None:
        from omakase.plus.catalog_config import configured_catalogs

        catalogs = configured_catalogs(db, user_id, kind=kind).sources

    if not catalogs:
        record(db, user_id, kind, title_id, [], marker=NO_SOURCES_CODE)
        return page_block(db, user_id, kind, title_id)

    queries = _primary_queries(request)
    started = time.time()
    raw_candidates, successes, task_count = await _gather_queries(
        catalogs, queries, timeout=timeout
    )
    candidates = _dedupe_candidates(raw_candidates)
    evaluated = evaluate_candidates(candidates, request)

    if (
        request.kind == "movie"
        and request.tiered_fallback
        and request.year
        and successes > 0
        and not any(verdict.accepted for _, verdict in evaluated)
    ):
        remaining = timeout - (time.time() - started)
        if remaining > 0.5:
            year_queries = [f"{query} {request.year}" for query in queries]
            more, more_ok, more_total = await _gather_queries(
                catalogs, year_queries, timeout=remaining
            )
            successes += more_ok
            task_count += more_total
            if more:
                candidates = _dedupe_candidates([*candidates, *more])
                evaluated = evaluate_candidates(candidates, request)

    marker = None
    if not successes:
        marker = ERROR_CODE
    elif successes != task_count:
        marker = PARTIAL_CODE
    elif not candidates:
        marker = EMPTY_CODE
    record(db, user_id, kind, title_id, evaluated, marker=marker)
    if on_matches is not None:
        eligible = [item for item, verdict in evaluated if verdict.accepted]
        if eligible:
            await on_matches(eligible)
    return page_block(db, user_id, kind, title_id)
