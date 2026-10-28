"""Episode parsing and deterministic coverage selection for anime releases."""

from __future__ import annotations

import re
from collections.abc import Sequence

from omakase.plus.media_types import EpisodeCoverage, MediaCandidate

BLEACH_COUR4_ANILIST_ID = 185874
BLEACH_COUR4_NUMBERING_OVERRIDE = "bleach-tybw-cour4-absolute-v1"
SLIME_S4_ANILIST_ID = 182205
SLIME_S4_NUMBERING_OVERRIDE = "slime-s4-absolute-v1"

MHA_S4_ANILIST_ID = 104276
MHA_S4_NUMBERING_OVERRIDE = "mha-s4-absolute-v1"

_EXTRA_RE = re.compile(
    r"\b(?:n[\s._-]?c[\s._-]?(?:op|ed)|pv|trailer|preview|sample|creditless)\b",
    re.IGNORECASE,
)
_FRACTIONAL_EPISODE_RE = re.compile(
    r"(?:\bS\d{1,2}E|\bEP(?:ISODE)?[\s._-]*|(?:^|\s)-\s*)"
    r"\d{1,4}\.\d+(?!\d)",
    re.IGNORECASE,
)
_SEASON_EPISODE_RE = re.compile(
    r"\bS(?P<season>\d{1,2})E(?P<start>\d{1,4})"
    r"(?:\s*[-~]\s*(?:(?:S(?P<end_season>\d{1,2}))?E)?(?P<end>\d{1,4}))?",
    re.IGNORECASE,
)
_LOCAL_SEASON_EPISODE_RE = re.compile(
    r"\bS(?:EASON)?[\s._-]*(?P<season>\d{1,2})\s*[-:]\s*(?P<episode>\d{1,4})\b",
    re.IGNORECASE,
)
_SEASON_BATCH_RE = re.compile(
    r"\bSEASON[\s._-]*(?P<season>\d{1,2}).*?"
    r"[\[(]\s*(?P<start>\d{1,4})\s*[-~]\s*(?P<end>\d{1,4})\s*[\])]",
    re.IGNORECASE,
)
_SPECIAL_RE = re.compile(r"\b(?:SP|SPECIAL)[\s._-]?(?P<episode>\d{1,4})\b", re.I)
_NAMED_EPISODE_RE = re.compile(r"\bEP(?:ISODE)?[\s._-]*(?P<episode>\d{1,4})\b", re.IGNORECASE)
_BATCH_RE = re.compile(r"\b(?:BATCH|COMPLETE|ALL[\s._-]?EPISODES?)\b", re.I)
_SEASON_MARKER_RE = re.compile(r"\b(?:SEASON[\s._-]*|S)(?P<season>\d{1,2})\b", re.IGNORECASE)
_NUMERIC_RANGE_RE = re.compile(r"\b(?P<start>\d{1,3})\s*[-~]\s*(?P<end>\d{1,3})\b")
_DASH_EPISODE_RE = re.compile(r"(?:^|\s)-\s*(?P<episode>\d{1,4})(?!\s*[-~])\b")
_MHA_ABSOLUTE_RE = re.compile(r"(?:^|\s)-\s*(?P<start>\d{2,3})(?:\s*[-~]\s*(?P<end>\d{2,3}))?\b")


def _bounded_episodes(start: int, end: int, expected: int | None) -> frozenset[int]:
    if end < start:
        start, end = end, start
    upper_bound = min(10000, expected) if expected is not None and expected >= 0 else 10000
    lower = max(1, start)
    upper = min(end, upper_bound)
    if upper < lower:
        return frozenset()
    return frozenset(range(lower, upper + 1))


def _mha_s4_override(title: str, season: int, anilist_id: int | None) -> EpisodeCoverage | None:
    if anilist_id != MHA_S4_ANILIST_ID or season != 4:
        return None
    match = _MHA_ABSOLUTE_RE.search(title)
    if not match:
        return None
    start = int(match.group("start"))
    end = int(match.group("end") or start)
    absolute = range(max(64, min(start, end)), min(88, max(start, end)) + 1)
    episodes = frozenset(value - 63 for value in absolute)
    if not episodes:
        return None
    return EpisodeCoverage(
        episodes=episodes,
        numbering_override=MHA_S4_NUMBERING_OVERRIDE,
    )


_COUR_MARKER_RE = re.compile(r"\b(?:cour|part)[\s._-]*(\d{1,2})\b", re.I)
_ORDINAL_SEASON_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)[\s._-]+season\b", re.I)
_BRACKET_EPISODE_RE = re.compile(r"\[\s*(\d{1,2})(?:v\d+)?\s*\]", re.I)
_TYBW_EPISODE_RE = re.compile(r"\bTYBW[\s._-]*(\d{1,3})(?:\s*[-~]\s*(\d{1,3}))?\b", re.I)
_CANONICAL_DASH_RANGE_RE = re.compile(
    r"(?:^|\s)-\s*(?P<start>\d{1,3})(?:\s*[-~]\s*(?P<end>\d{1,3}))?\b"
)
_CANONICAL_NAMED_RE = re.compile(
    r"\b(?:EP(?:ISODE)?|E)[\s._-]*(?P<start>\d{1,3})"
    r"(?:\s*[-~]\s*(?P<end>\d{1,3}))?(?:v\d+)?(?!\d)(?!\.\d)\b",
    re.I,
)
_CANONICAL_PAREN_RANGE_RE = re.compile(r"\(\s*(?P<start>\d{1,3})\s*[-~]\s*(?P<end>\d{1,3})\s*\)")
_CANONICAL_BRACKET_RE = re.compile(
    r"\[\s*(?P<start>\d{1,2})(?:\s*[-~]\s*(?P<end>\d{1,2}))?(?:v\d+)?\s*\]", re.I
)


def _unconsumed_episode_suffix(text: str, match: re.Match[str]) -> bool:
    suffix = text[match.end() :]
    revision = re.match(r"v\d+", suffix, re.I)
    if revision:
        suffix = suffix[revision.end() :]
    return bool(re.match(r"(?:[a-z0-9]|\.\d|[\s._]*E\d|\s*[-~]\s*(?:S\d{1,2})?E?\d)", suffix, re.I))


def _canonical_numbering(
    text: str, season: int, expected: int | None, anilist_id: int | None
) -> EpisodeCoverage | None:
    """Exact-title contracts; None delegates only unrelated titles to legacy parsing.

    Local and absolute claims must agree. Out-of-contract ranges are rejected,
    never clipped into the target cour. A future episode can be parsed, but the
    acquisition layer still intersects it with aired required coverage.
    """
    if anilist_id not in {BLEACH_COUR4_ANILIST_ID, SLIME_S4_ANILIST_ID}:
        return None
    empty = EpisodeCoverage()
    if re.search(r"\bE[\s._-]*\d+(?:v\d+)?\.\d+", text, re.I):
        return empty
    if re.search(r"\b(?:OVA|OAD|SPECIAL|DIARIES|SPIN[\s._-]*OFF)\b", text, re.I):
        return empty
    if re.search(r"\bTYBW[\s._-]*\d+\.\d+", text, re.I):
        return empty
    normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
    ordinal_seasons = list(_ORDINAL_SEASON_RE.finditer(text))
    # "4th Season - 05" names season 4 and episode 5. The overlapping
    # "Season - 05" substring must not invent a second season claim.
    season_markers = {
        int(match.group("season"))
        for match in _SEASON_MARKER_RE.finditer(text)
        if not any(ordinal.start() <= match.start() < ordinal.end() for ordinal in ordinal_seasons)
    }
    season_markers.update(int(match.group(1)) for match in ordinal_seasons)
    explicit = list(_SEASON_EPISODE_RE.finditer(text))
    season_markers.update(int(match.group("season")) for match in explicit)
    season_markers.update(
        int(match.group("end_season")) for match in explicit if match.group("end_season")
    )
    if anilist_id == BLEACH_COUR4_ANILIST_ID:
        if season not in {2, 4}:
            return empty
        if "bleach" not in normalized.split() or not (
            re.search(r"\btybw(?=[\s._\d-])", text, re.I)
            or "thousand year blood war" in normalized
            or "sennen kessen" in normalized
        ):
            return empty
        cours = {int(match.group(1)) for match in _COUR_MARKER_RE.finditer(text)}
        if cours - {4}:
            return empty
        explicit_seasons = {int(match.group("season")) for match in explicit}
        if season_markers - {1, 4, 17} or (season_markers & {1, 17}) - explicit_seasons:
            return empty
        if "soukoku tan" in normalized or "ketsubetsu tan" in normalized:
            return empty
        local_identity = bool(
            cours == {4} or "calamity" in normalized or "kashin tan" in normalized
        )
        offset, total, tag = 40, 10, BLEACH_COUR4_NUMBERING_OVERRIDE
    else:
        if season_markers - {4}:
            return empty
        if season != 4 or not (
            "tensei shitara slime datta ken" in normalized
            or "that time i got reincarnated as a slime" in normalized
        ):
            return empty
        # Main-series absolute numbers establish S4; local numbers need a
        # source-title S4 claim as well as the canonical monitor identity.
        local_identity = season_markers == {4}
        offset, total, tag = 72, 24, SLIME_S4_NUMBERING_OVERRIDE

    claims: list[tuple[int, int, bool]] = []
    for match in explicit:
        if _unconsumed_episode_suffix(text, match):
            return empty
        start, end = int(match.group("start")), int(match.group("end") or match.group("start"))
        global_claim = anilist_id == BLEACH_COUR4_ANILIST_ID and int(match.group("season")) in {
            1,
            17,
        }
        if global_claim and not offset < start <= end <= offset + total:
            return empty
        claims.append((start, end, not global_claim))
    absolute_dash_matches = list(_CANONICAL_DASH_RANGE_RE.finditer(text))
    for match in _LOCAL_SEASON_EPISODE_RE.finditer(text):
        if int(match.group("season")) != 4:
            return empty
        if any(
            item.group("end") and item.start() <= match.start("episode") < item.end()
            for item in absolute_dash_matches
        ):
            continue
        claims.append((int(match.group("episode")), int(match.group("episode")), False))
    for match in _TYBW_EPISODE_RE.finditer(text):
        claims.append((int(match.group(1)), int(match.group(2) or match.group(1)), False))
    # Absolute dash ranges are recognized without the generic batch clamping.
    for match in absolute_dash_matches:
        if _unconsumed_episode_suffix(text, match):
            return empty
        claims.append(
            (int(match.group("start")), int(match.group("end") or match.group("start")), False)
        )
    for pattern in (_CANONICAL_NAMED_RE, _CANONICAL_BRACKET_RE, _CANONICAL_PAREN_RANGE_RE):
        for match in pattern.finditer(text):
            if any(item.start() <= match.start() < item.end() for item in explicit):
                continue
            if _unconsumed_episode_suffix(text, match):
                return empty
            start = int(match.group("start"))
            claims.append((start, int(match.group("end") or start), False))
    if not claims:
        return empty
    resolved: list[frozenset[int]] = []
    for start, end, explicit_local in claims:
        if end < start:
            return empty
        if not explicit_local and offset < start <= end <= offset + total:
            values = frozenset(range(start - offset, end - offset + 1))
        elif local_identity and 1 <= start <= end <= total:
            values = frozenset(range(start, end + 1))
        else:
            return empty
        if expected is not None and any(value > expected for value in values):
            return empty
        resolved.append(values)
    # Duplicate annotations are allowed, contradictory local/global claims are not.
    if any(values != resolved[0] for values in resolved[1:]):
        return empty
    return EpisodeCoverage(episodes=resolved[0], numbering_override=tag)


def parse_episode_coverage(
    title: str,
    season: int,
    *,
    expected: int | None,
    anilist_id: int | None = None,
) -> EpisodeCoverage:
    """Parse safe target-season episode claims from a release title or filename."""
    text = title or ""
    if _EXTRA_RE.search(text) or _FRACTIONAL_EPISODE_RE.search(text):
        return EpisodeCoverage()

    canonical = _canonical_numbering(text, season, expected, anilist_id)
    if canonical is not None:
        return canonical

    override = _mha_s4_override(text, season, anilist_id)
    if override is not None:
        return override

    explicit_matches = list(_SEASON_EPISODE_RE.finditer(text))
    if explicit_matches:
        explicit_seasons = {int(match.group("season")) for match in explicit_matches}
        explicit_seasons.update(
            int(match.group("end_season"))
            for match in explicit_matches
            if match.group("end_season") is not None
        )
        if explicit_seasons == {0}:
            specials: set[int] = set()
            for match in explicit_matches:
                start = int(match.group("start"))
                end = int(match.group("end") or start)
                specials.update(_bounded_episodes(start, end, 10000))
            return EpisodeCoverage(specials=frozenset(specials))
        if explicit_seasons != {season}:
            return EpisodeCoverage()
        episodes: set[int] = set()
        for match in explicit_matches:
            start = int(match.group("start"))
            end = int(match.group("end") or start)
            episodes.update(_bounded_episodes(start, end, expected))
        return EpisodeCoverage(episodes=frozenset(episodes))

    batch_match = _SEASON_BATCH_RE.search(text)
    if batch_match:
        if int(batch_match.group("season")) != season:
            return EpisodeCoverage()
        return EpisodeCoverage(
            episodes=_bounded_episodes(
                int(batch_match.group("start")),
                int(batch_match.group("end")),
                expected,
            )
        )

    local_match = _LOCAL_SEASON_EPISODE_RE.search(text)
    if local_match:
        if int(local_match.group("season")) != season:
            return EpisodeCoverage()
        episode = int(local_match.group("episode"))
        return EpisodeCoverage(episodes=_bounded_episodes(episode, episode, expected))

    special_match = _SPECIAL_RE.search(text)
    if special_match:
        return EpisodeCoverage(specials=frozenset({int(special_match.group("episode"))}))

    if _BATCH_RE.search(text):
        for range_match in _NUMERIC_RANGE_RE.finditer(text):
            episodes = _bounded_episodes(
                int(range_match.group("start")),
                int(range_match.group("end")),
                expected,
            )
            if episodes:
                return EpisodeCoverage(episodes=episodes)
        season_match = _SEASON_MARKER_RE.search(text)
        if season_match:
            if int(season_match.group("season")) != season:
                return EpisodeCoverage()
            if expected is not None:
                return EpisodeCoverage(episodes=_bounded_episodes(1, expected, expected))

    dash_match = _DASH_EPISODE_RE.search(text)
    if dash_match:
        episode = int(dash_match.group("episode"))
        return EpisodeCoverage(episodes=_bounded_episodes(episode, episode, expected))

    named_match = _NAMED_EPISODE_RE.search(text)
    if named_match:
        episode = int(named_match.group("episode"))
        return EpisodeCoverage(episodes=_bounded_episodes(episode, episode, expected))

    return EpisodeCoverage()


def parse_single_file_coverage(
    title: str,
    season: int,
    *,
    expected: int | None,
    anilist_id: int | None = None,
) -> EpisodeCoverage:
    """Require one unclipped, consistent episode claim for strict file evidence."""
    empty = EpisodeCoverage()
    if re.search(r"\bS\d{1,2}E\d{1,4}(?:[._\s]*E\d{1,4})+", title, re.I):
        return empty
    for pattern in (
        _SEASON_EPISODE_RE,
        _CANONICAL_DASH_RANGE_RE,
        _CANONICAL_NAMED_RE,
        _CANONICAL_BRACKET_RE,
    ):
        if any(match.group("end") is not None for match in pattern.finditer(title)):
            return empty
    if any(
        match.group(2) is not None for match in _TYBW_EPISODE_RE.finditer(title)
    ) or _NUMERIC_RANGE_RE.search(title):
        return empty
    parsed = parse_episode_coverage(title, season, expected=None, anilist_id=anilist_id)
    if len(parsed.episodes) != 1 or parsed.specials:
        return empty
    if expected is not None and max(parsed.episodes) > expected:
        return empty
    # Exact canonical contracts already reconcile every local/global annotation.
    if anilist_id in {BLEACH_COUR4_ANILIST_ID, SLIME_S4_ANILIST_ID}:
        return parsed
    claims = [int(match.group("start")) for match in _SEASON_EPISODE_RE.finditer(title)]
    for pattern in (_LOCAL_SEASON_EPISODE_RE, _NAMED_EPISODE_RE, _DASH_EPISODE_RE):
        claims.extend(int(match.group("episode")) for match in pattern.finditer(title))
    claims.extend(int(match.group(1)) for match in _BRACKET_EPISODE_RE.finditer(title))
    if anilist_id == MHA_S4_ANILIST_ID and season == 4:
        claims = [value - 63 if 64 <= value <= 88 else value for value in claims]
    if not claims or any(value not in parsed.episodes for value in claims):
        return empty
    return parsed


def select_episode_cover(
    required: set[int] | frozenset[int], candidates: Sequence[MediaCandidate]
) -> tuple[MediaCandidate, ...]:
    """Choose a deterministic greedy set of candidates without mutating inputs."""
    missing = set(required)
    pool = list(candidates)
    chosen: list[MediaCandidate] = []

    while missing:
        ranked = sorted(
            (
                (
                    len(item.coverage & missing),
                    item.score,
                    item.seeders,
                    -item.size_bytes,
                    item.torrent_hash,
                    item,
                )
                for item in pool
            ),
            key=lambda row: row[:-1],
            reverse=True,
        )
        if not ranked or ranked[0][0] == 0:
            break
        item = ranked[0][-1]
        chosen.append(item)
        missing -= item.coverage
        pool.remove(item)

    return tuple(chosen)
