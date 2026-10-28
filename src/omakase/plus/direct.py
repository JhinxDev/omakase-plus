"""AniList resolver for Plus direct download requests."""

from __future__ import annotations

import re

import httpx

from omakase.plus.media_types import MediaTarget

API_URL = "https://graphql.anilist.co"
USER_AGENT = "Omakase/0.1 (+https://github.com/HeyiTzSenpai/omakase)"

_ANILIST_ID_RE = re.compile(r"anilist\.co/anime/(\d+)", re.I)
_ARC_HINT_RE = re.compile(r"\b(arc|cour|final|movie|ova|part|season|special)\b", re.I)
_ORDINAL_SEASON_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+season\b", re.I)
_SEASON_RE = re.compile(r"\bseason[\s._-]*(\d{1,2})\b|\bs[\s._-]*(\d{1,2})\b", re.I)
_TITLE_NORMALIZE_RE = re.compile(r"[^\w]+", re.UNICODE)


DirectDownloadTarget = MediaTarget


class AmbiguousMediaError(ValueError):
    """Raised when an AniList search has no single decisive identity."""

    def __init__(self, options: tuple[MediaTarget, ...]):
        self.options = options
        super().__init__("AniList search is ambiguous; choose the intended title.")


def parse_anilist_id(value: str) -> int | None:
    text = (value or "").strip()
    match = _ANILIST_ID_RE.search(text)
    if match:
        return int(match.group(1))
    if text.isdigit():
        return int(text)
    return None


def _preferred_title(media: dict) -> str:
    title = media.get("title") or {}
    return title.get("english") or title.get("romaji") or title.get("native") or ""


def _season_number(value: str) -> int | None:
    if value.strip().isdigit():
        return int(value.strip())
    for pattern in (_SEASON_RE, _ORDINAL_SEASON_RE):
        match = pattern.search(value)
        if match:
            for group in match.groups():
                if group:
                    return int(group)
    return None


def _season_hint_alias(season_hint: str) -> str:
    hint = (season_hint or "").strip()
    if not hint:
        return ""
    number = _season_number(hint)
    if number is not None and not _ARC_HINT_RE.search(hint):
        return f"Season {number}"
    return hint


def _already_has_hint(value: str, hint: str) -> bool:
    value_lower = value.lower()
    hint_lower = hint.lower()
    if hint_lower in value_lower:
        return True
    hint_season = _season_number(hint)
    return hint_season is not None and _season_number(value) == hint_season


def _canonical_titles(media: dict) -> tuple[str, ...]:
    title = media.get("title") or {}
    values = [
        title.get("english"),
        title.get("romaji"),
        title.get("native"),
        *(media.get("synonyms") or []),
    ]

    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return tuple(result)


def _search_titles(media: dict, season_hint: str = "") -> tuple[str, ...]:
    values = _canonical_titles(media)
    result = list(values)
    if season_hint:
        hint = _season_hint_alias(season_hint)
        for value in values:
            if _already_has_hint(value, hint):
                continue
            season_alias = f"{value} {hint}"
            if season_alias not in result:
                result.append(season_alias)
    return tuple(result)


def _media_season_number(media: dict, season_hint: str, *, trust_hint: bool) -> int | None:
    for title in _canonical_titles(media):
        number = _season_number(title)
        if number is not None:
            return number
    relation_edges = (media.get("relations") or {}).get("edges") or []
    prequels = []
    for edge in relation_edges:
        if not isinstance(edge, dict) or edge.get("relationType") != "PREQUEL":
            continue
        node = edge.get("node") or {}
        if isinstance(node, dict) and node.get("format") in {"TV", "TV_SHORT", "ONA"}:
            prequels.append(node)
    if len(prequels) == 1:
        for title in _canonical_titles(prequels[0]):
            number = _season_number(title)
            if number is not None:
                return number + 1
        return 2
    if trust_hint:
        hinted_number = _season_number(season_hint)
        if hinted_number is not None:
            return hinted_number
    if (media.get("format") or "") in {"TV", "TV_SHORT", "ONA"}:
        return 1
    return None


def _aired_episode_count(media: dict) -> int:
    next_airing = media.get("nextAiringEpisode") or {}
    next_episode = next_airing.get("episode")
    if isinstance(next_episode, int) and next_episode > 0:
        return next_episode - 1
    episodes = media.get("episodes")
    if media.get("status") == "FINISHED" and isinstance(episodes, int) and episodes > 0:
        return episodes
    return 0


def _unique_strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value.strip() and value.strip() not in result:
            result.append(value.strip())
    return tuple(result)


def _target_from_media(
    media: dict,
    season_hint: str = "",
    *,
    trust_season_hint: bool = False,
) -> MediaTarget:
    next_airing = media.get("nextAiringEpisode") or {}
    next_airing_at = next_airing.get("airingAt")
    return MediaTarget(
        anilist_id=int(media["id"]),
        title=_preferred_title(media),
        search_titles=_search_titles(media, season_hint),
        format=media.get("format") or "",
        status=media.get("status") or "",
        episodes=media.get("episodes"),
        aired_episodes=_aired_episode_count(media),
        season_number=_media_season_number(media, season_hint, trust_hint=trust_season_hint),
        season=media.get("season") or "",
        season_year=media.get("seasonYear"),
        genres=_unique_strings(media.get("genres")),
        next_airing_at=next_airing_at if isinstance(next_airing_at, int) else None,
    )


def _post(query: str, variables: dict) -> dict:
    with httpx.Client(timeout=20.0) as client:
        response = client.post(
            API_URL,
            json={"query": query, "variables": variables},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        response.raise_for_status()
        return response.json()


def _normalize_title(value: str) -> str:
    return " ".join(_TITLE_NORMALIZE_RE.sub(" ", value.casefold()).split())


def _exact_options(
    options: tuple[MediaTarget, ...], query_text: str, season_hint: str
) -> tuple[MediaTarget, ...]:
    query = _normalize_title(query_text)
    hint = _season_hint_alias(season_hint)
    combined = _normalize_title(f"{query_text} {hint}".strip()) if hint else query
    wanted_season = _season_number(season_hint)
    exact: list[MediaTarget] = []
    for option in options:
        if wanted_season is not None and option.season_number != wanted_season:
            continue
        aliases = {_normalize_title(alias) for alias in option.search_titles}
        if query in aliases or combined in aliases:
            exact.append(option)
    return tuple(exact)


def search_direct_requests(query_text: str, season: str = "") -> tuple[MediaTarget, ...]:
    text = (query_text or "").strip()
    season_hint = (season or "").strip()
    if not text:
        raise ValueError("Enter an anime title or AniList URL.")

    fields = (
        "id title { romaji english native } synonyms format status episodes season seasonYear "
        "genres nextAiringEpisode { episode airingAt } "
        "relations { edges { relationType node { "
        "id format title { romaji english native } synonyms } } }"
    )
    media_id = parse_anilist_id(text)
    if media_id is not None:
        data = _post(
            f"query ($id: Int) {{ Media(id: $id, type: ANIME) {{ {fields} }} }}",
            {"id": media_id},
        )
        media = (data.get("data") or {}).get("Media")
        media_rows = [media] if media else []
    else:
        search = f"{text} {_season_hint_alias(season_hint)}".strip() if season_hint else text
        data = _post(
            f"""
            query ($search: String!) {{
              Page(page: 1, perPage: 5) {{
                media(search: $search, type: ANIME, isAdult: false) {{
                  {fields}
                }}
              }}
            }}
            """,
            {"search": search},
        )
        media_rows = ((data.get("data") or {}).get("Page") or {}).get("media") or []

    return tuple(
        _target_from_media(
            media,
            season_hint,
            trust_season_hint=media_id is not None,
        )
        for media in media_rows[:5]
        if isinstance(media, dict) and media.get("id") is not None
    )


def resolve_direct_request(query_text: str, season: str = "") -> MediaTarget:
    text = (query_text or "").strip()
    season_hint = (season or "").strip()
    options = search_direct_requests(text, season_hint)

    if not options:
        raise ValueError(f'No AniList anime found for "{text}".')
    if parse_anilist_id(text) is not None:
        return options[0]

    exact = _exact_options(options, text, season_hint)
    if len(exact) == 1:
        return exact[0]
    if len(options) == 1:
        wanted_season = _season_number(season_hint)
        if wanted_season is not None and options[0].season_number != wanted_season:
            raise ValueError(f'No AniList anime found for "{text}" with that season.')
        return options[0]
    raise AmbiguousMediaError(options)
