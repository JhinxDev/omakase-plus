"""Franchise-aware candidate policy for Omakase recommendations."""

from __future__ import annotations

import re
from dataclasses import replace

from omakase.types import MediaItem

LOW_STATUSES = {"DROPPED", "PAUSED"}
SATISFIED_HISTORY_STATUSES = {"COMPLETED", "FINISHED", "REPEATING"}
LOOSE_RELATIONS = {"SIDE_STORY", "SPIN_OFF", "ALTERNATIVE", "SUMMARY", "COMPILATION", "OTHER"}
STRICT_RELATIONS = {"PREQUEL", "SEQUEL", "PARENT", "CONTAINS"}
PREREQUISITE_RELATIONS = {"PREQUEL", "PARENT"}
LANES = {"best_match", "new_seasons", "hidden_gems", "plan_list", "discover"}
_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")
_SUBTITLE_SEPARATORS = (": ", " - ", " – ", " — ")
_SEASON_TAIL = re.compile(
    r"\s+(?:season\s+\d+|s\d+|part\s+\d+|\d+(?:st|nd|rd|th)\s+season|"
    r"the\s+(?:movie|final|animation)|movie|film|ova|special|specials|"
    r"i{2,}|iv|vi+|ix|x|\d+)$",
    re.IGNORECASE,
)
_POLICY_RANK = {"neutral": 0, "boosted": 1, "blocked": 2}


def _title_stem(title: str | None) -> str:
    if not title:
        return ""
    s = title.strip().lower()
    s = _PAREN_TAIL.sub("", s).strip()
    earliest = min(
        (i for i in (s.find(sep) for sep in _SUBTITLE_SEPARATORS) if i != -1),
        default=-1,
    )
    if earliest != -1:
        s = s[:earliest]
    while True:
        new = _SEASON_TAIL.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


def _store_stem_policy(stems: dict[str, str], stem: str, policy: str) -> None:
    if _POLICY_RANK[policy] >= _POLICY_RANK.get(stems.get(stem, "neutral"), 0):
        stems[stem] = policy


def _history_buckets(
    history: list[MediaItem],
) -> tuple[set[int], set[int], set[int], dict[str, str]]:
    loved: set[int] = set()
    low: set[int] = set()
    neutral: set[int] = set()
    stems: dict[str, str] = {}
    for item in history:
        if item.id:
            if item.status in LOW_STATUSES or (item.score is not None and item.score <= 5):
                low.add(item.id)
            elif item.score is not None and item.score >= 8:
                loved.add(item.id)
            elif item.score is not None and 6 <= item.score <= 7:
                neutral.add(item.id)
        for title in (item.title_english, item.title_romaji):
            stem = _title_stem(title)
            if stem:
                if item.status in LOW_STATUSES or (item.score is not None and item.score <= 5):
                    _store_stem_policy(stems, stem, "blocked")
                elif item.score is not None and item.score >= 8:
                    _store_stem_policy(stems, stem, "boosted")
                elif item.score is not None and 6 <= item.score <= 7:
                    _store_stem_policy(stems, stem, "neutral")
    return loved, low, neutral, stems


def _history_item_satisfies_prerequisite(item: MediaItem) -> bool:
    status = (item.status or "").upper()
    return status in SATISFIED_HISTORY_STATUSES


def _satisfied_prerequisite_ids(history: list[MediaItem]) -> set[int]:
    return {item.id for item in history if item.id and _history_item_satisfies_prerequisite(item)}


def _relation_policy(
    candidate: MediaItem, loved: set[int], low: set[int], neutral: set[int]
) -> str:
    related = {rel.media_id for rel in candidate.relations}
    if candidate.id in low or related & low:
        return "blocked"
    if candidate.id in loved or related & loved:
        return "boosted"
    if candidate.id in neutral or related & neutral:
        return "neutral"
    return "neutral"


def _stem_policy(candidate: MediaItem, stems: dict[str, str]) -> str | None:
    policies: list[str] = []
    for title in (candidate.title_english, candidate.title_romaji):
        stem = _title_stem(title)
        if stem and stem in stems:
            policies.append(stems[stem])
    if not policies:
        return None
    return max(policies, key=lambda policy: _POLICY_RANK[policy])


def _sequence_warning(candidate: MediaItem, satisfied_ids: set[int]) -> str:
    strict_prequels = [
        rel
        for rel in candidate.relations
        if rel.relation_type in PREREQUISITE_RELATIONS and rel.media_id not in satisfied_ids
    ]
    if strict_prequels:
        title = strict_prequels[0].title_english or strict_prequels[0].title_romaji
        return f"Sequencing check: verify earlier entry {title} first."
    return ""


def classify_candidate(candidate: MediaItem, history: list[MediaItem]) -> MediaItem:
    loved, low, neutral, stems = _history_buckets(history)
    satisfied_ids = _satisfied_prerequisite_ids(history)
    policy = _relation_policy(candidate, loved, low, neutral)
    stem_policy = _stem_policy(candidate, stems)
    if stem_policy == "blocked":
        policy = "blocked"
    elif stem_policy == "boosted" and policy != "blocked":
        policy = "boosted"

    loose = any(rel.relation_type in LOOSE_RELATIONS for rel in candidate.relations)
    note = ""
    if policy == "blocked":
        note = "Low-rated, dropped, or paused franchise relation."
    elif policy == "boosted":
        note = "Loved franchise continuation."
    return replace(
        candidate,
        franchise_policy=policy,
        franchise_note=note,
        sequence_warning=_sequence_warning(candidate, satisfied_ids),
        loose_order=loose,
    )


def _lane_sort_key(item: MediaItem, lane: str) -> tuple:
    if item.franchise_policy == "blocked":
        return (3,)
    if item.sequence_warning:
        return (2,)
    boosted = item.franchise_policy == "boosted"
    status = (item.status or "").upper()
    year = item.season_year or 0
    score = item.mean_score or 0
    if lane == "new_seasons":
        return (not boosted, status != "RELEASING", -year, -score)
    if lane == "hidden_gems":
        return (boosted, -score, year)
    return (0 if boosted else 1,)


def apply_lane_policy(
    history: list[MediaItem],
    candidates: list[MediaItem],
    lane: str,
) -> list[MediaItem]:
    normalized_lane = lane if lane in LANES else "best_match"
    classified = [classify_candidate(candidate, history) for candidate in candidates]
    if normalized_lane == "discover":
        classified = [
            candidate for candidate in classified if candidate.franchise_policy != "blocked"
        ]
    elif normalized_lane != "plan_list":
        classified = [
            candidate
            for candidate in classified
            if candidate.franchise_policy != "blocked" and not candidate.sequence_warning
        ]
    return sorted(classified, key=lambda item: _lane_sort_key(item, normalized_lane))
