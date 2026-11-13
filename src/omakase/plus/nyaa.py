"""Nyaa.si RSS feed client — search anime torrents and find the best magnet.

Uses the RSS feed at ``https://nyaa.si/?page=rss`` which returns clean XML
with magnet links, seeders, size, and title. No API key required.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from omakase.plus.acquisition_policy import AcquisitionPolicy

NYAA_RSS_URL = "https://nyaa.si/"

# Category codes: 1_2 = Anime - English Translated
DEFAULT_CATEGORY = "1_2"

# Namespaces used in nyaa.si RSS feed
_NS = {
    "nyaa": "https://nyaa.si/xmlns/nyaa",
    "torrent": "http://xmlns.ezrss.it/0.1/",
}


@dataclass
class NyaaTorrent:
    """A single torrent result from nyaa.si."""

    title: str
    magnet: str
    seeders: int
    leechers: int
    size_bytes: int
    size_display: str
    pub_date: datetime
    is_trusted: bool
    is_batch: bool
    torrent_url: str = ""


def _parse_size(size_str: str) -> tuple[int, str]:
    """Parse a nyaa size string like '1.4 GiB' into (bytes, display)."""
    if not size_str:
        return 0, ""
    parts = size_str.strip().split()
    if len(parts) != 2:
        return 0, size_str
    try:
        value = float(parts[0])
    except ValueError:
        return 0, size_str
    unit = parts[1].upper()
    multipliers = {"B": 1, "KIB": 1024, "MIB": 1024**2, "GIB": 1024**3, "TIB": 1024**4}
    return int(value * multipliers.get(unit, 1)), size_str


def _parse_pubdate(date_str: str) -> datetime:
    """Parse RSS pubDate like 'Sun, 28 May 2026 00:00:00 -0000'."""
    try:
        from email.utils import parsedate_to_datetime

        return parsedate_to_datetime(date_str)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


_BATCH_PATTERNS = [
    re.compile(r"\b(complete|batch)\b", re.IGNORECASE),
    re.compile(r"\ball[\s._-]?episodes?\b", re.IGNORECASE),
    re.compile(r"\b\d{2,3}-\d{2,3}\b"),  # episode ranges like 01-12
]

_REJECT_PATTERNS = [
    re.compile(
        r"\b(hdcam|camrip|cam|hdts|telesync|telecine|dvdscr|screener)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bsample\b", re.IGNORECASE),
    re.compile(r"\b(n[\s._-]?c[\s._-]?op|n[\s._-]?c[\s._-]?ed)\b", re.IGNORECASE),
    re.compile(r"\b(pv|trailer|cm|ost)\b", re.IGNORECASE),
    re.compile(r"\b(op|ed)[\s._-]?\d*\b", re.IGNORECASE),
    re.compile(
        r"\b(creditless|clean[\s._-]?(opening|ending)|non[\s._-]?credit)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(music[\s._-]?video|video[\s._-]?extras?)\b", re.IGNORECASE),
]

_SOURCE_SCORES = [
    (
        re.compile(r"\b(blu[\s._-]?ray|bluray|bd[\s._-]?rip|bdrip|bdmv|bdremux)\b", re.I),
        130,
    ),
    (re.compile(r"\b(web[\s._-]?(dl|rip)|webrip)\b", re.I), 120),
    (re.compile(r"\bhdtv\b", re.I), 45),
    (re.compile(r"\bdvd[\s._-]?rip\b", re.I), 20),
]

_KNOWN_HIGH_QUALITY_GROUPS = (
    "subsplease",
    "judas",
    "ember",
    "mtbb",
    "asenshi",
    "commie",
    "deanzel",
    "vodes",
)

_RAW_PATTERN = re.compile(r"\braws?\b", re.IGNORECASE)
_SHORT_TITLE_TOKEN_MAX_LEN = 8
_SHORT_TITLE_ROMAN = r"(?:ii|iii|iv|v|vi|vii|viii|ix|x)"
_ORDINAL_SEASON_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", re.I)
_SEASON_RE = re.compile(r"\bseason[\s._-]*(\d{1,2})\b|\bs[\s._-]*(\d{1,2})\b", re.I)
_PART_RE = re.compile(
    rf"\bpart[\s._-]*(\d{{1,2}}|{_SHORT_TITLE_ROMAN})\b|"
    rf"\bpt[\s._-]*(\d{{1,2}}|{_SHORT_TITLE_ROMAN})\b",
    re.I,
)
_COUR_RE = re.compile(r"\bcour[\s._-]*(\d{1,2})\b", re.I)
_FINAL_SEASON_RE = re.compile(r"\b(?:the\s+)?final\s+season\b", re.I)
_ROMAN_NUMERALS = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
}
_TITLE_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "batch",
    "complete",
    "episode",
    "episodes",
    "movie",
    "of",
    "on",
    "ova",
    "part",
    "season",
    "series",
    "special",
    "the",
    "to",
}
_RELEASE_NOISE_TOKENS = {
    "aac",
    "bd",
    "bdmv",
    "bdrip",
    "bdremux",
    "bluray",
    "dub",
    "dubbed",
    "flac",
    "hevc",
    "multi",
    "remux",
    "sub",
    "subbed",
    "subs",
    "truehd",
    "uhd",
    "web",
    "webrip",
    "x264",
    "x265",
}


def _is_likely_batch(title: str) -> bool:
    """Heuristic: detect batch/complete-series torrents from title patterns."""
    return any(p.search(title) for p in _BATCH_PATTERNS)


def _is_rejected_quality(title: str) -> bool:
    """Return True for releases that should never be auto-downloaded."""
    return any(p.search(title) for p in _REJECT_PATTERNS)


def _is_unreasonable_size(
    torrent: NyaaTorrent,
    policy: AcquisitionPolicy | None = None,
) -> bool:
    policy = policy or AcquisitionPolicy()
    gib = 1024**3
    size = torrent.size_bytes
    title = torrent.title

    if size > policy.max_release_size_bytes:
        return True
    if size >= 120 * gib and re.search(
        r"\b(collection|all[\s._-]?seasons?|franchise|complete[\s._-]?series)\b",
        title,
        re.IGNORECASE,
    ):
        return True
    return False


def _is_rejected_torrent(
    torrent: NyaaTorrent,
    policy: AcquisitionPolicy | None = None,
) -> bool:
    return _is_rejected_quality(torrent.title) or _is_unreasonable_size(torrent, policy)


def _resolution_name(title: str) -> str:
    if re.search(r"\b(2160p|4k|uhd)\b", title, re.IGNORECASE):
        return "2160p"
    if re.search(r"\b1080p\b", title, re.IGNORECASE):
        return "1080p"
    if re.search(r"\b720p\b", title, re.IGNORECASE):
        return "720p"
    return ""


def _resolution_score(title: str, policy: AcquisitionPolicy | None = None) -> int:
    policy = policy or AcquisitionPolicy()
    resolution = _resolution_name(title)
    if resolution in policy.allowed_resolutions:
        scores = (190, 180, 55)
        return scores[policy.allowed_resolutions.index(resolution)]
    if re.search(r"\b(480p|576p)\b", title, re.IGNORECASE):
        return -130
    if re.search(r"\b(360p|240p)\b", title, re.IGNORECASE):
        return -180
    return 0


def _source_score(title: str) -> int:
    for pattern, score in _SOURCE_SCORES:
        if pattern.search(title):
            return score
    return 0


def _seeder_score(seeders: int) -> int:
    if seeders >= 500:
        return 80
    if seeders >= 200:
        return 70
    if seeders >= 100:
        return 60
    if seeders >= 50:
        return 50
    if seeders >= 20:
        return 40
    if seeders >= 10:
        return 30
    if seeders >= 5:
        return 20
    if seeders >= 2:
        return 10
    return 0


def _size_score(torrent: NyaaTorrent) -> int:
    mib = 1024**2
    gib = 1024**3
    size = torrent.size_bytes

    if size <= 0:
        return 0
    if size < 300 * mib:
        return -70
    if size < 700 * mib:
        return -25
    if size >= 80 * gib:
        return -120
    if size >= 50 * gib:
        return -45
    if torrent.is_batch and size >= 20 * gib:
        return 35
    if size >= 4 * gib:
        return 25
    if size >= 1 * gib:
        return 15
    return 0


def _has_known_high_quality_group(title: str) -> bool:
    normalized_title = title.lower()
    for group in _KNOWN_HIGH_QUALITY_GROUPS:
        pattern = rf"(^|[\[\s._-]){re.escape(group)}($|[\]\s._-])"
        if re.search(pattern, normalized_title):
            return True
    return False


def _title_tokens(title: str) -> set[str]:
    stripped = re.sub(r"\[[^\]]+\]", " ", title.lower())
    stripped = re.sub(r"\([^)]*\)", " ", stripped)
    tokens = set(re.findall(r"[a-z0-9]+", stripped))
    return {
        token
        for token in tokens
        if len(token) >= 2
        and not token.isdigit()
        and not re.fullmatch(r"\d{3,4}p", token)
        and not re.fullmatch(r"s\d{1,2}", token)
        and token not in _TITLE_STOPWORDS
        and token not in _RELEASE_NOISE_TOKENS
    }


def _is_exact_short_title_release(candidate_title: str, expected_token: str) -> bool:
    """Return True when a short one-token title is the whole release title."""
    title = candidate_title.lower()
    season_or_part_pattern = rf"\b(?:season|part)[\s._-]*(?:\d{{1,2}}|{_SHORT_TITLE_ROMAN})\b"
    shorthand_season_pattern = rf"\bs[\s._-]*(?:\d{{1,2}}|{_SHORT_TITLE_ROMAN})\b"
    numeric_sequel_pattern = (
        rf"\b{re.escape(expected_token)}\b\s+"
        rf"(?:\d{{1,3}}|{_SHORT_TITLE_ROMAN})\b"
    )
    if re.search(season_or_part_pattern, title) or re.search(shorthand_season_pattern, title):
        return False
    if re.search(numeric_sequel_pattern, title):
        return False
    return _title_tokens(candidate_title) == {expected_token}


def _explicit_season_number(title: str) -> int | None:
    for pattern in (_SEASON_RE, _ORDINAL_SEASON_RE):
        match = pattern.search(title)
        if match:
            for group in match.groups():
                if group:
                    return int(group)
    return None


def _marker_number(title: str, pattern: re.Pattern[str]) -> int | None:
    match = pattern.search(title)
    if not match:
        return None
    for group in match.groups():
        if not group:
            continue
        value = group.lower()
        if value.isdigit():
            return int(value)
        if value in _ROMAN_NUMERALS:
            return _ROMAN_NUMERALS[value]
    return None


def _explicit_release_intent(title: str) -> dict[str, int | bool | None]:
    return {
        "season": _explicit_season_number(title),
        "part": _marker_number(title, _PART_RE),
        "cour": _marker_number(title, _COUR_RE),
        "final_season": bool(_FINAL_SEASON_RE.search(title)),
    }


def _release_intent_matches(candidate_title: str, expected_title: str) -> bool:
    expected = _explicit_release_intent(expected_title)
    if not any(value for value in expected.values()):
        return True

    candidate = _explicit_release_intent(candidate_title)
    for marker in ("season", "part", "cour"):
        expected_value = expected[marker]
        if expected_value is not None and candidate[marker] != expected_value:
            return False
    if expected["final_season"] and not candidate["final_season"]:
        return False
    return True


def _title_match_score(candidate_title: str, expected_title: str | None) -> float:
    if not expected_title:
        return 1.0

    expected_tokens = _title_tokens(expected_title)
    if not expected_tokens:
        return 1.0

    if not _release_intent_matches(candidate_title, expected_title):
        return 0.0

    expected_season = _explicit_season_number(expected_title)
    if expected_season is not None:
        candidate_season = _explicit_season_number(candidate_title)
        if candidate_season != expected_season:
            return 0.0

    if len(expected_tokens) == 1:
        expected_token = next(iter(expected_tokens))
        if expected_season is None and len(expected_token) <= _SHORT_TITLE_TOKEN_MAX_LEN:
            return 1.0 if _is_exact_short_title_release(candidate_title, expected_token) else 0.0

    candidate_tokens = _title_tokens(candidate_title)
    overlap = expected_tokens & candidate_tokens
    if not overlap:
        return 0.0

    candidate_text = " ".join(re.findall(r"[a-z0-9]+", candidate_title.lower()))
    expected_text = " ".join(re.findall(r"[a-z0-9]+", expected_title.lower()))
    if expected_text and expected_text in candidate_text:
        return 1.0

    ratio = len(overlap) / len(expected_tokens)
    if any(len(token) >= 7 for token in overlap):
        return max(ratio, 0.75)
    return ratio


def _quality_score(
    torrent: NyaaTorrent,
    *,
    prefer_trusted: bool,
    prefer_no_batch: bool,
    policy: AcquisitionPolicy | None = None,
) -> int:
    policy = policy or AcquisitionPolicy()
    title = torrent.title
    normalized_title = title.lower()
    score = 0

    if prefer_trusted and torrent.is_trusted:
        score += 70
    if prefer_trusted and _has_known_high_quality_group(normalized_title):
        score += 50

    score += _resolution_score(title, policy)
    score += _source_score(title)
    score += _seeder_score(torrent.seeders)
    if torrent.seeders >= policy.preferred_seeders:
        score += 20
    score += _size_score(torrent)

    if prefer_no_batch:
        score += 80 if not torrent.is_batch else -40
    elif torrent.is_batch:
        score += 90

    if _RAW_PATTERN.search(title):
        score -= 85
    if re.search(r"\b(hevc|x265|10[\s._-]?bit)\b", title, re.IGNORECASE):
        score += 20
    if re.search(r"\bx264\b", title, re.IGNORECASE):
        score += 10

    return score


async def search(
    query: str,
    category: str = DEFAULT_CATEGORY,
    trusted_only: bool = False,
    timeout: float = 10.0,
) -> list[NyaaTorrent]:
    """Search nyaa.si RSS feed for anime torrents.

    Args:
        query: Search term (e.g. anime title).
        category: Nyaa category code (default: 1_2 = Anime English).
        trusted_only: If True, only return trusted uploads.
        timeout: HTTP timeout in seconds.

    Returns:
        List of NyaaTorrent results, sorted by seeders descending.
    """
    params = {
        "page": "rss",
        "q": query,
        "c": category,
        "f": "2" if trusted_only else "0",  # 2 = trusted only
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(NYAA_RSS_URL, params=params)
        resp.raise_for_status()

    # Nyaa RSS sometimes contains invalid XML characters (control chars,
    # bare ampersands in titles). Clean them up before parsing.
    text = re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]", "", resp.text)
    # Fix unescaped & that aren't part of a valid entity
    text = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)", "&amp;", text)

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    torrents: list[NyaaTorrent] = []

    for item in root.findall(".//item"):
        title_el = item.find("title")
        pubdate_el = item.find("pubDate")

        title = title_el.text if title_el is not None and title_el.text else ""
        if not title:
            continue

        # Construct magnet link from the <nyaa:infoHash>
        magnet = ""
        info_hash_el = item.find("nyaa:infoHash", _NS)
        if info_hash_el is not None and info_hash_el.text:
            magnet = f"magnet:?xt=urn:btih:{info_hash_el.text}&dn={title}"

        # Get nyaa-specific fields
        seeders_el = item.find("nyaa:seeders", _NS)
        leechers_el = item.find("nyaa:leechers", _NS)
        size_el = item.find("nyaa:size", _NS)
        trusted_el = item.find("nyaa:trusted", _NS)

        seeders = int(seeders_el.text) if seeders_el is not None and seeders_el.text else 0
        leechers = int(leechers_el.text) if leechers_el is not None and leechers_el.text else 0
        size_display = size_el.text if size_el is not None and size_el.text else ""
        size_bytes, _ = _parse_size(size_display)
        is_trusted = trusted_el is not None and trusted_el.text == "Yes"
        pub_date = _parse_pubdate(
            pubdate_el.text if pubdate_el is not None and pubdate_el.text else ""
        )

        torrents.append(
            NyaaTorrent(
                title=title,
                magnet=magnet,
                seeders=seeders,
                leechers=leechers,
                size_bytes=size_bytes,
                size_display=size_display,
                pub_date=pub_date,
                is_trusted=is_trusted,
                is_batch=_is_likely_batch(title),
                torrent_url=item.findtext("link", ""),
            )
        )

    # Sort by seeders descending
    torrents.sort(key=lambda t: t.seeders, reverse=True)
    return torrents


def rank_best(
    torrents: list[NyaaTorrent],
    *,
    expected_title: str | None = None,
    prefer_trusted: bool | None = None,
    prefer_no_batch: bool | None = None,
    min_seeders: int | None = None,
    policy: AcquisitionPolicy | None = None,
) -> list[NyaaTorrent]:
    """Return viable torrents sorted from best to worst.

    Heuristic:
    - Reject CAM/sample-style releases outright.
    - Reject extras, unreasonable huge dumps, and poor expected-title matches.
    - Prefer high-quality resolutions and sources (2160p/1080p, BluRay/BDRip/Web-DL).
    - Prefer trusted/known high-quality uploaders, adequate seeders, and sane file sizes.
    - Prefer complete batches by default because Plus downloads title-level recommendations.

    Returns an empty list if no torrent meets the minimum seeders threshold.
    """
    if not torrents:
        return []
    policy = policy or AcquisitionPolicy()
    effective_min_seeders = policy.minimum_seeders if min_seeders is None else min_seeders
    effective_prefer_trusted = policy.prefer_trusted if prefer_trusted is None else prefer_trusted
    effective_prefer_no_batch = (
        not policy.prefer_batches if prefer_no_batch is None else prefer_no_batch
    )

    candidates = [
        t
        for t in torrents
        if t.seeders >= effective_min_seeders
        and not _is_rejected_torrent(t, policy)
        and (
            not _resolution_name(t.title) or _resolution_name(t.title) in policy.allowed_resolutions
        )
        and _title_match_score(t.title, expected_title) >= 0.6
    ]
    if not candidates:
        return []

    def _key(t: NyaaTorrent) -> tuple[int, int, int, int, str]:
        quality = _quality_score(
            t,
            prefer_trusted=effective_prefer_trusted,
            prefer_no_batch=effective_prefer_no_batch,
            policy=policy,
        )
        resolution = _resolution_name(t.title)
        resolution_rank = (
            policy.allowed_resolutions.index(resolution)
            if resolution in policy.allowed_resolutions
            else len(policy.allowed_resolutions)
        )
        return (
            resolution_rank,
            -quality,
            -t.seeders,
            -t.size_bytes,
            t.title.lower(),
        )

    candidates.sort(key=_key)
    return candidates


def find_best(
    torrents: list[NyaaTorrent],
    *,
    expected_title: str | None = None,
    prefer_trusted: bool | None = None,
    prefer_no_batch: bool | None = None,
    min_seeders: int | None = None,
    policy: AcquisitionPolicy | None = None,
) -> NyaaTorrent | None:
    """Pick the best torrent from search results."""
    candidates = rank_best(
        torrents,
        expected_title=expected_title,
        prefer_trusted=prefer_trusted,
        prefer_no_batch=prefer_no_batch,
        min_seeders=min_seeders,
        policy=policy,
    )
    return candidates[0] if candidates else None
