"""Standalone anime filename normalization used by the CT111 curator."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SEASON_EPISODE_RE = re.compile(
    r"\bS(?P<season>\d{1,2})E(?P<start>\d{1,4})"
    r"(?:\s*[-~]\s*(?:(?:S(?P<end_season>\d{1,2}))?E)?(?P<end>\d{1,4}))?",
    re.IGNORECASE,
)
_LOCAL_SEASON_RE = re.compile(
    r"\bS(?:EASON)?\s*(?P<season>\d{1,2})\s*[-:]\s*"
    r"(?P<start>\d{1,4})(?:\s*[-~]\s*(?P<end>\d{1,4}))?",
    re.IGNORECASE,
)
_SPECIAL_RE = re.compile(r"\b(?:SP|SPECIAL)[\s._-]?(?P<episode>\d{1,4})\b", re.I)
_FRACTIONAL_EPISODE_RE = re.compile(
    r"(?:\bS\d{1,2}E|\bEP(?:ISODE)?[\s._-]*|(?:^|\s)-\s*)"
    r"\d{1,4}\.\d+(?!\d)",
    re.IGNORECASE,
)
_ABSOLUTE_RANGE_RE = re.compile(r"(?:^|\s)-\s*(?P<start>\d{2,3})(?:\s*[-~]\s*(?P<end>\d{2,3}))?\b")
_SIMPLE_RANGE_RE = re.compile(
    r"(?:^|\s)-\s*(?P<start>\d{1,3})(?:\s*[-~]\s*(?P<end>\d{1,3}))?"
    r"(?=\s*(?:END\b\s*)?(?:[\[(.]|$))",
    re.I,
)


class EpisodeParseError(ValueError):
    """A source filename cannot be mapped to one unambiguous season range."""


@dataclass(frozen=True, slots=True)
class EpisodeRef:
    season: int
    start: int
    end: int
    special: bool = False

    @property
    def episodes(self) -> frozenset[int]:
        return frozenset(range(self.start, self.end + 1))


def _episode_ref(
    *,
    season: int,
    start: int,
    end: int | None,
    expected: int | None,
    special: bool = False,
) -> EpisodeRef:
    end = start if end is None else end
    if end < start:
        raise EpisodeParseError("episode range is reversed")
    if season < 0 or start < 1 or end > 10_000:
        raise EpisodeParseError("episode range is out of bounds")
    if expected is not None and not special and end > expected:
        raise EpisodeParseError("episode range exceeds expected season coverage")
    return EpisodeRef(season=season, start=start, end=end, special=special)


BLEACH_COUR4_ANILIST_ID = 185874
BLEACH_COUR4_NUMBERING_OVERRIDE = "bleach-tybw-cour4-absolute-v1"
SLIME_S4_ANILIST_ID = 182205
SLIME_S4_NUMBERING_OVERRIDE = "slime-s4-absolute-v1"
_SEASON_MARKER_RE = re.compile(r"\b(?:SEASON[\s._-]*|S)(?P<season>\d{1,2})\b", re.I)
_LOCAL_SEASON_EPISODE_RE = re.compile(
    r"\bS(?:EASON)?[\s._-]*(?P<season>\d{1,2})\s*[-:]\s*(?P<episode>\d{1,4})\b", re.I
)
_DASH_EPISODE_RE = re.compile(r"(?:^|\s)-\s*(?P<episode>\d{1,4})(?!\s*[-~])\b")
_NAMED_EPISODE_RE = re.compile(r"\bEP(?:ISODE)?[\s._-]*(?P<episode>\d{1,4})\b", re.I)
_EXTRA_RE = re.compile(
    r"\b(?:n[\s._-]?c[\s._-]?(?:op|ed)|pv|trailer|preview|sample|creditless)\b", re.I
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


def _canonical_episodes(
    text: str, season: int, expected: int | None, anilist_id: int | None
) -> frozenset[int] | None:
    """Exact-title contracts; None delegates only unrelated titles to legacy parsing.

    Local and absolute claims must agree. Out-of-contract ranges are rejected,
    never clipped into the target cour. A future episode can be parsed, but the
    acquisition layer still intersects it with aired required coverage.
    """
    if anilist_id not in {BLEACH_COUR4_ANILIST_ID, SLIME_S4_ANILIST_ID}:
        return None
    empty = frozenset()
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
        offset, total = 40, 10
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
        offset, total = 72, 24

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
    return resolved[0]


def parse_file_episode(
    filename: str,
    *,
    season: int,
    expected: int | None,
    numbering_override: str | None = None,
    anilist_id: int | None = None,
) -> EpisodeRef:
    """Parse one file into a local Plex season/episode range or fail closed."""
    if _FRACTIONAL_EPISODE_RE.search(filename):
        raise EpisodeParseError("fractional episode requires explicit special handling")

    canonical_tags = {
        BLEACH_COUR4_NUMBERING_OVERRIDE: BLEACH_COUR4_ANILIST_ID,
        SLIME_S4_NUMBERING_OVERRIDE: SLIME_S4_ANILIST_ID,
    }
    if numbering_override in canonical_tags or anilist_id in canonical_tags.values():
        if canonical_tags.get(numbering_override) != anilist_id:
            raise EpisodeParseError(
                "canonical numbering requires matching manifest ID and contract"
            )
        if _EXTRA_RE.search(filename):
            raise EpisodeParseError("extra filename has no regular episode coverage")
        episodes = _canonical_episodes(filename, season, expected, anilist_id)
        if not episodes:
            raise EpisodeParseError("filename violates the canonical numbering contract")
        return _episode_ref(
            season=season, start=min(episodes), end=max(episodes), expected=expected
        )

    explicit = _SEASON_EPISODE_RE.search(filename)
    if explicit:
        parsed_season = int(explicit.group("season"))
        if parsed_season != season or (
            explicit.group("end_season") and int(explicit.group("end_season")) != season
        ):
            raise EpisodeParseError("explicit season does not match manifest")
        return _episode_ref(
            season=season,
            start=int(explicit.group("start")),
            end=int(explicit.group("end")) if explicit.group("end") else None,
            expected=expected,
        )

    local = _LOCAL_SEASON_RE.search(filename)
    if local:
        parsed_season = int(local.group("season"))
        if parsed_season != season:
            raise EpisodeParseError("local season hint does not match manifest")
        return _episode_ref(
            season=season,
            start=int(local.group("start")),
            end=int(local.group("end")) if local.group("end") else None,
            expected=expected,
        )

    special = _SPECIAL_RE.search(filename)
    if special:
        episode = int(special.group("episode"))
        return _episode_ref(
            season=0,
            start=episode,
            end=episode,
            expected=None,
            special=True,
        )

    if numbering_override == "mha-s4-absolute-v1" and season == 4:
        absolute = _ABSOLUTE_RANGE_RE.search(filename)
        if absolute:
            start = int(absolute.group("start"))
            end = int(absolute.group("end") or start)
            if start < 64 or end > 88:
                raise EpisodeParseError("absolute episode is outside the MHA S4 window")
            return _episode_ref(
                season=4,
                start=start - 63,
                end=end - 63,
                expected=expected,
            )

    simple = _SIMPLE_RANGE_RE.search(filename)
    if simple:
        return _episode_ref(
            season=season,
            start=int(simple.group("start")),
            end=int(simple.group("end")) if simple.group("end") else None,
            expected=expected,
        )

    raise EpisodeParseError("filename has no unambiguous episode identity")
