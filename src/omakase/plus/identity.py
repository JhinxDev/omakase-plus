"""Fail-closed release-title identity checks for media acquisition."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from omakase.plus.coverage import (
    BLEACH_COUR4_ANILIST_ID,
    BLEACH_COUR4_NUMBERING_OVERRIDE,
    SLIME_S4_ANILIST_ID,
    SLIME_S4_NUMBERING_OVERRIDE,
    parse_episode_coverage,
)

_SPACE_RE = re.compile(r"\s+")
_SEASON_EPISODE_RE = re.compile(
    r"\bS(?:EASON)?[\s._-]*0*(?P<season>\d{1,2})"
    r"(?:[\s._-]*E(?:P(?:ISODE)?)?[\s._-]*0*(?P<episode>\d{1,4}))?",
    re.IGNORECASE,
)
_SEASON_PREFIX_RE = re.compile(
    r"\bSEASON[\s._-]*0*(?P<season>\d{1,2})\b",
    re.IGNORECASE,
)
_SEASON_SUFFIX_RE = re.compile(
    r"\b0*(?P<season>\d{1,2})(?:ST|ND|RD|TH)[\s._-]+SEASON\b",
    re.IGNORECASE,
)
_JAPANESE_SEASON_RE = re.compile(r"第\s*0*(?P<season>\d{1,2})\s*期")
_TRAILING_SEASON_RE_TEMPLATE = r"(?:\bSEASON\s*{season}\b|\b{season}\b)$"
_TECHNICAL_BRACKET_RE = re.compile(
    r"\[(?:"
    r"\d{3,4}p|"
    r"[A-F0-9]{8}|"
    r"(?:HEVC|AV1|H\.?26[45]|X26[45]|10BIT|WEB(?:-DL|RIP)?|BLU-?RAY|"
    r"DUAL(?:-AUDIO)?|MULTI(?:-SUBS?)?|AAC|EAC3|FLAC|OPUS)"
    r"(?:[^\]]*)"
    r")\]",
    re.IGNORECASE,
)
_PUNCTUATION_RE = re.compile(r"[^\w]+", re.UNICODE)


def _season_phrase(value: str) -> str:
    value = _SEASON_SUFFIX_RE.sub(
        lambda match: f" season {int(match.group('season'))} ",
        value,
    )
    value = _JAPANESE_SEASON_RE.sub(
        lambda match: f" season {int(match.group('season'))} ",
        value,
    )
    value = _SEASON_EPISODE_RE.sub(
        lambda match: (
            f" season {int(match.group('season'))} "
            + (
                f" episode {int(match.group('episode'))} "
                if match.group("episode") is not None
                else ""
            )
        ),
        value,
    )
    value = _SEASON_PREFIX_RE.sub(
        lambda match: f" season {int(match.group('season'))} ",
        value,
    )
    return value


def normalize_release_identity(value: str) -> str:
    """Normalize title punctuation and equivalent season spellings."""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    text = text.replace("’", "'").replace("`", "'")
    text = _TECHNICAL_BRACKET_RE.sub(" ", text)
    text = _season_phrase(text)
    text = _PUNCTUATION_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def release_season_markers(value: str) -> frozenset[int]:
    """Return explicit numeric season claims found anywhere in a release title."""
    text = unicodedata.normalize("NFKC", value or "")
    seasons: set[int] = set()
    for pattern in (
        _SEASON_SUFFIX_RE,
        _JAPANESE_SEASON_RE,
        _SEASON_EPISODE_RE,
        _SEASON_PREFIX_RE,
    ):
        for match in pattern.finditer(text):
            if pattern in {_SEASON_EPISODE_RE, _SEASON_PREFIX_RE} and re.search(
                r"\b\d{1,2}(?:ST|ND|RD|TH)\s*$",
                text[: match.start()],
                re.IGNORECASE,
            ):
                continue
            seasons.add(int(match.group("season")))
    return frozenset(season for season in seasons if 1 <= season <= 99)


def _base_alias(alias: str, season: int) -> str:
    normalized = normalize_release_identity(alias)
    normalized = re.sub(rf"\bseason\s+{season}\b", " ", normalized)
    normalized = re.sub(
        _TRAILING_SEASON_RE_TEMPLATE.format(season=season),
        " ",
        normalized,
    )
    return _SPACE_RE.sub(" ", normalized).strip()


def _ordered_alias_match(alias: str, source: str) -> bool:
    """Allow harmless release counters between every ordered target token."""
    target_tokens = alias.split()
    source_tokens = source.split()
    if len(target_tokens) < 2:
        return False
    cursor = 0
    for token in target_tokens:
        try:
            cursor = source_tokens.index(token, cursor) + 1
        except ValueError:
            return False
    return True


def identity_rejection_reason(
    source_title: str,
    *,
    target_title: str,
    target_aliases: Sequence[str],
    season: int,
    anilist_id: int | None = None,
    media_format: str = "TV",
) -> str:
    """Return a stable reason when a release cannot be bound to one target."""
    source = normalize_release_identity(source_title)
    if not source:
        return "TARGET_TITLE_MISMATCH"

    # These source-verified contracts bind series + cour + episode, including
    # absolute release numbers when storage uses a different season number.
    # A canonical ID alone never makes an unrelated or ambiguous release valid.
    tag = (
        BLEACH_COUR4_NUMBERING_OVERRIDE
        if anilist_id == BLEACH_COUR4_ANILIST_ID and season in {2, 4}
        else SLIME_S4_NUMBERING_OVERRIDE
        if anilist_id == SLIME_S4_ANILIST_ID and season == 4
        else None
    )
    if tag is not None and media_format == "TV":
        canonical = parse_episode_coverage(
            source_title, season=season, expected=None, anilist_id=anilist_id
        )
        if canonical.episodes and canonical.numbering_override == tag:
            return ""

    markers = release_season_markers(source_title)
    wrong_markers = markers - {season}
    if wrong_markers:
        return "CONFLICTING_SEASON" if season in markers or len(markers) > 1 else "WRONG_SEASON"

    aliases = tuple(
        dict.fromkeys(
            normalized
            for value in (target_title, *target_aliases)
            if (normalized := normalize_release_identity(value))
        )
    )
    if any(alias in source or _ordered_alias_match(alias, source) for alias in aliases):
        return ""

    season_optional = season == 1 or media_format == "MOVIE" or anilist_id == 104276
    if season_optional:
        bases = tuple(
            dict.fromkeys(
                base
                for value in (target_title, *target_aliases)
                if len(base := _base_alias(value, season)) >= 5
            )
        )
        if any(base in source for base in bases):
            return ""

    if season > 1 and not markers:
        return "AMBIGUOUS_TARGET_IDENTITY"
    return "TARGET_TITLE_MISMATCH"
