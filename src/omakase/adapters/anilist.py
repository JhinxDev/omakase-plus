"""AniList adapter — fetches anime watch history and candidate pool.

Based on the Phase 0 work done for the Homelab Anime Recommender.
AniList blocks Python's default User-Agent, so we set a custom one.
"""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from omakase.adapters.base import SourceAdapter, register
from omakase.franchise import apply_lane_policy
from omakase.types import MediaItem, MediaRelation, SourceData

API_URL = "https://graphql.anilist.co"
USER_AGENT = "Omakase/0.1 (homelab; +https://github.com/HeyiTzSenpai/omakase)"
PUBLIC_RECOMMENDATION_LANES = frozenset({"best_match", "new_seasons", "hidden_gems"})
CATALOG_TIMEOUT_SECONDS = 20


class AniListCatalogError(Exception):
    """Safe catalog failure, without provider URLs, bodies or user variables."""

    def __init__(self, category: str, status_code: int | None = None):
        self.category = category
        self.status_code = status_code
        super().__init__("AniList catalog request failed")


def _candidate_request(query: str, variables: dict) -> tuple[str, dict]:
    """Absent optional filters must be omitted, rather than sent as null.

    AniList rejects popularity_lesser:null with HTTP400 even though its
    GraphQL argument is nullable. Preserve non-null hidden-gem constraints.
    """
    variables = dict(variables)
    for variable, declaration, argument in (
        ("genres", ", $genres: [String]", "genre_in: $genres"),
        ("visibility", ", $visibility: Int", "popularity_lesser: $visibility"),
    ):
        if variable in variables and variables[variable] is None:
            del variables[variable]
            query = query.replace(declaration, "")
            query = "\n".join(line for line in query.split("\n") if argument not in line)
    return query, variables


# AniList relation types that mean "same franchise". These feed both the
# richer relation metadata and the legacy title/relation helpers below; fetch()
# now delegates keep/boost/block decisions to omakase.franchise.apply_lane_policy.
FRANCHISE_RELATION_TYPES = frozenset(
    {
        "PREQUEL",
        "SEQUEL",
        "PARENT",
        "SIDE_STORY",
        "SUMMARY",
        "ALTERNATIVE",
        "SPIN_OFF",
        "COMPILATION",
        "CONTAINS",
    }
)

_PAREN_TAIL = re.compile(r"\s*\([^)]*\)\s*$")
_SUBTITLE_SEPARATORS = (": ", " - ", " – ", " — ")
_SEASON_TAIL = re.compile(
    r"\s+(?:season\s+\d+|s\d+|part\s+\d+|\d+(?:st|nd|rd|th)\s+season|"
    r"the\s+(?:movie|final|animation)|movie|film|ova|special|specials|"
    r"i{2,}|iv|vi+|ix|x|\d+)$",
    re.IGNORECASE,
)


def _title_stem(title: str | None) -> str:
    """Normalize an anime title to its franchise stem for prefix matching.

    Used as a belt to AniList's relations graph: if AniList's metadata is
    incomplete, two titles sharing a normalized stem (e.g. "gintama" from
    both "Gintama Season 2" and "Gintama: THE VERY FINAL") get treated as
    the same franchise.
    """
    if not title:
        return ""
    s = title.strip().lower()
    s = _PAREN_TAIL.sub("", s).strip()
    # Split at the EARLIEST subtitle separator, not the first one in tuple
    # order — a title like "Chainsaw Man – The Movie: Reze Arc" has both
    # " – " and ": " and we want the dash, not the colon.
    earliest = min(
        (i for i in (s.find(sep) for sep in _SUBTITLE_SEPARATORS) if i != -1),
        default=-1,
    )
    if earliest != -1:
        s = s[:earliest]
    # Strip iteratively in case multiple markers stack (e.g. "X Season 2 Part 2").
    while True:
        new = _SEASON_TAIL.sub("", s).strip()
        if new == s:
            break
        s = new
    return s


def _build_franchise_block(history: list[MediaItem]) -> tuple[set[int], set[str]]:
    """Build the legacy franchise match set from history.

    Returns (history_ids, history_stems). A candidate is "in-franchise" iff
    its id, any of its related_ids, or its title stem hits one of these sets.
    """
    ids = {m.id for m in history if m.id}
    stems = set()
    for m in history:
        for t in (m.title_english, m.title_romaji):
            stem = _title_stem(t)
            if stem:
                stems.add(stem)
    return ids, stems


def _candidate_is_in_franchise(
    candidate: MediaItem, history_ids: set[int], history_stems: set[str]
) -> bool:
    """Return True if this candidate belongs to the same franchise as any history entry."""
    if candidate.id in history_ids:
        return True
    if any(rid in history_ids for rid in candidate.related_ids):
        return True
    for t in (candidate.title_english, candidate.title_romaji):
        stem = _title_stem(t)
        if stem and stem in history_stems:
            return True
    return False


def _date_string(value: dict | None) -> str | None:
    if not value or not value.get("year"):
        return None
    month = value.get("month") or 1
    day = value.get("day") or 1
    return f"{value['year']:04d}-{month:02d}-{day:02d}"


def _normalize_public_lane(lane: object) -> str:
    if isinstance(lane, str) and lane in PUBLIC_RECOMMENDATION_LANES:
        return lane
    return "best_match"


def _parse_relations(media: dict) -> tuple[list[int], list[MediaRelation]]:
    related_ids: list[int] = []
    relations: list[MediaRelation] = []
    for edge in media.get("relations", {}).get("edges", []) or []:
        rtype = edge.get("relationType")
        node = edge.get("node") or {}
        if not rtype or node.get("type") != "ANIME" or not node.get("id"):
            continue
        relation = MediaRelation(
            relation_type=rtype,
            media_id=node["id"],
            title_romaji=node.get("title", {}).get("romaji", ""),
            title_english=node.get("title", {}).get("english"),
            format=node.get("format"),
            status=node.get("status"),
            episodes=node.get("episodes"),
            season=node.get("season"),
            season_year=node.get("seasonYear"),
        )
        relations.append(relation)
        if rtype in FRANCHISE_RELATION_TYPES:
            related_ids.append(node["id"])
    return related_ids, relations


@register("anilist")
class AniListAdapter(SourceAdapter):
    name = "anilist"

    def _graphql(self, query: str, variables: dict | None = None) -> dict:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = Request(
            API_URL,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(req, timeout=CATALOG_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode())
        except HTTPError as error:
            status_code = error.code
            error.close()
            category = (
                "rate_limit"
                if error.code == 429
                else "access"
                if error.code in {401, 403}
                else "upstream"
                if error.code >= 500
                else "request"
            )
            raise AniListCatalogError(category, status_code) from None
        except (TimeoutError, URLError):
            raise AniListCatalogError("transport") from None
        except (ValueError, UnicodeError):
            raise AniListCatalogError("invalid_response") from None
        if not isinstance(data, dict) or data.get("errors"):
            raise AniListCatalogError("invalid_response")
        return data

    def _fetch_history(self, username: str) -> list[MediaItem]:
        query = """
        query ($username: String) {
          MediaListCollection(userName: $username, type: ANIME) {
            lists {
              entries {
                score(format: POINT_10)
                status
                media {
                  id
                  title { romaji english }
                  genres
                  tags { name rank }
                  meanScore
                  status
                  season
                  seasonYear
                  startDate { year month day }
                  nextAiringEpisode { episode airingAt }
                  format
                  episodes
                  studios(isMain: true) { nodes { name } }
                  relations {
                    edges {
                      relationType
                      node {
                        id
                        type
                        format
                        status
                        episodes
                        season
                        seasonYear
                        title { romaji english }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"username": username})
        lists = data.get("data", {}).get("MediaListCollection", {}).get("lists", [])
        seen_ids: set[int] = set()
        items: list[MediaItem] = []
        for lst in lists:
            for entry in lst.get("entries", []):
                media = entry.get("media", {})
                mid = media.get("id")
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)
                tags = [t["name"] for t in media.get("tags", []) if t.get("rank", 0) >= 80]
                studio = None
                studios = media.get("studios", {}).get("nodes", [])
                if studios:
                    studio = studios[0].get("name")
                related_ids, relations = _parse_relations(media)
                next_airing = media.get("nextAiringEpisode") or {}
                items.append(
                    MediaItem(
                        id=mid,
                        title_romaji=media.get("title", {}).get("romaji", ""),
                        title_english=media.get("title", {}).get("english"),
                        genres=media.get("genres", []),
                        tags=tags,
                        score=entry.get("score"),
                        status=entry.get("status"),
                        format=media.get("format"),
                        episodes=media.get("episodes"),
                        studio=studio,
                        mean_score=media.get("meanScore"),
                        related_ids=related_ids,
                        season=media.get("season"),
                        season_year=media.get("seasonYear"),
                        start_date=_date_string(media.get("startDate")),
                        next_airing_episode=next_airing.get("episode"),
                        next_airing_at=next_airing.get("airingAt"),
                        relations=relations,
                    )
                )
        return items

    def _analyze_genre_affinity(self, history: list[MediaItem]) -> list[str]:
        """Calculate which genres the user actually likes based on scored history.

        Returns up to 5 genres with the strongest positive affinity, sorted best-first.
        """
        # Shrink sparse evidence toward neutral; volume alone is not affinity.
        genre_scores: dict[str, list[float]] = {}
        for item in history:
            score = item.score if item.score and item.score > 0 else None
            if item.status == "DROPPED":
                score = min(score or 4, 4)
            elif item.status in {"PAUSED", "PLANNING"}:
                continue
            if score is None:
                continue
            for genre in item.genres:
                genre_scores.setdefault(genre, []).append(score - 6.5)
        affinity = [
            (genre, sum(scores) / (len(scores) + 2))
            for genre, scores in genre_scores.items()
            if sum(scores) > 0
        ]
        affinity.sort(key=lambda item: item[1], reverse=True)
        return [genre for genre, _ in affinity[:5]]

    def _fetch_candidates(self, exclude_ids: list[int], pool_size: int) -> list[MediaItem]:
        """Fetch candidates using genre-targeted search when history is available.

        Falls back to global SCORE_DESC when called without history context.
        """
        # Base query used for both targeted and global fetches
        base_query = """
        query ($excludeIds: [Int], $page: Int, $genres: [String]) {
          Page(perPage: 50, page: $page) {
            media(
              type: ANIME
              sort: [SCORE_DESC]
              genre_in: $genres
              status_in: [RELEASING, FINISHED]
              id_not_in: $excludeIds
            ) {
              id
              title { romaji english }
              genres
              tags { name rank }
              meanScore
              description(asHtml: false)
              format
              status
              season
              seasonYear
              startDate { year month day }
              episodes
              nextAiringEpisode { episode airingAt }
              studios(isMain: true) { nodes { name } }
              relations {
                edges {
                  relationType
                  node {
                    id
                    type
                    format
                    status
                    episodes
                    season
                    seasonYear
                    title { romaji english }
                  }
                }
              }
            }
          }
        }
        """

        def _parse_media(media_list: list[dict]) -> list[MediaItem]:
            items: list[MediaItem] = []
            for m in media_list:
                tags = [t["name"] for t in m.get("tags", []) if t.get("rank", 0) >= 80]
                studio = None
                studios = m.get("studios", {}).get("nodes", [])
                if studios:
                    studio = studios[0].get("name")
                desc = m.get("description", "")
                if desc and len(desc) > 200:
                    desc = desc[:200] + "..."
                related_ids, relations = _parse_relations(m)
                next_airing = m.get("nextAiringEpisode") or {}
                items.append(
                    MediaItem(
                        id=m["id"],
                        title_romaji=m.get("title", {}).get("romaji", ""),
                        title_english=m.get("title", {}).get("english"),
                        genres=m.get("genres", []),
                        tags=tags,
                        format=m.get("format"),
                        status=m.get("status"),
                        episodes=m.get("episodes"),
                        studio=studio,
                        description=desc,
                        mean_score=m.get("meanScore"),
                        related_ids=related_ids,
                        season=m.get("season"),
                        season_year=m.get("seasonYear"),
                        start_date=_date_string(m.get("startDate")),
                        next_airing_episode=next_airing.get("episode"),
                        next_airing_at=next_airing.get("airingAt"),
                        relations=relations,
                    )
                )
            return items

        def _fetch_page(page: int, genres: list[str] | None = None) -> list[dict]:
            query, variables = _candidate_request(
                base_query,
                {"excludeIds": exclude_ids, "page": page, "genres": genres},
            )
            data = self._graphql(query, variables)
            return data.get("data", {}).get("Page", {}).get("media", [])

        seen_ids: set[int] = set(exclude_ids)
        all_candidates: list[MediaItem] = []

        # Fetch a baseline of 100 top-scored anime (no genre filter)
        page = 1
        while len(all_candidates) < pool_size and page <= max(2, (pool_size + 49) // 50 + 2):
            media_list = _fetch_page(page)
            if not media_list:
                break
            for item in _parse_media(media_list):
                if item.id not in seen_ids:
                    seen_ids.add(item.id)
                    all_candidates.append(item)
            page += 1

        return all_candidates[:pool_size]

    def _fetch_candidates_targeted(
        self,
        exclude_ids: list[int],
        pool_size: int,
        preferred_genres: list[str],
        *,
        hidden_only: bool = False,
    ) -> list[MediaItem]:
        """Fetch candidates targeted by the user's preferred genres.

        Interleaves bounded genre pages and a general exploration source.
        Hidden-only retrieval uses at most two lower-visibility pages.
        """
        base_query = """
        query ($excludeIds: [Int], $page: Int, $genres: [String], $visibility: Int) {
          Page(perPage: 50, page: $page) {
            media(
              type: ANIME
              sort: [SCORE_DESC]
              genre_in: $genres
              popularity_lesser: $visibility
              status_in: [RELEASING, FINISHED]
              id_not_in: $excludeIds
            ) {
              id
              title { romaji english }
              genres
              tags { name rank }
              meanScore
              description(asHtml: false)
              format
              status
              season
              seasonYear
              startDate { year month day }
              episodes
              nextAiringEpisode { episode airingAt }
              studios(isMain: true) { nodes { name } }
              relations {
                edges {
                  relationType
                  node {
                    id
                    type
                    format
                    status
                    episodes
                    season
                    seasonYear
                    title { romaji english }
                  }
                }
              }
            }
          }
        }
        """

        def _parse_media(media_list: list[dict]) -> list[MediaItem]:
            items: list[MediaItem] = []
            for m in media_list:
                tags = [t["name"] for t in m.get("tags", []) if t.get("rank", 0) >= 80]
                studio = None
                studios = m.get("studios", {}).get("nodes", [])
                if studios:
                    studio = studios[0].get("name")
                desc = m.get("description", "")
                if desc and len(desc) > 200:
                    desc = desc[:200] + "..."
                related_ids, relations = _parse_relations(m)
                next_airing = m.get("nextAiringEpisode") or {}
                items.append(
                    MediaItem(
                        id=m["id"],
                        title_romaji=m.get("title", {}).get("romaji", ""),
                        title_english=m.get("title", {}).get("english"),
                        genres=m.get("genres", []),
                        tags=tags,
                        format=m.get("format"),
                        status=m.get("status"),
                        episodes=m.get("episodes"),
                        studio=studio,
                        description=desc,
                        mean_score=m.get("meanScore"),
                        related_ids=related_ids,
                        season=m.get("season"),
                        season_year=m.get("seasonYear"),
                        start_date=_date_string(m.get("startDate")),
                        next_airing_episode=next_airing.get("episode"),
                        next_airing_at=next_airing.get("airingAt"),
                        relations=relations,
                    )
                )
            return items

        def _fetch_page(page: int, genres: list[str] | None) -> list[dict]:
            query, variables = _candidate_request(
                base_query,
                {
                    "excludeIds": exclude_ids,
                    "page": page,
                    "genres": genres,
                    "visibility": 50000 if hidden_only else None,
                },
            )
            data = self._graphql(query, variables)
            return data.get("data", {}).get("Page", {}).get("media", [])

        seen_ids: set[int] = set(exclude_ids)
        batches: list[list[MediaItem]] = []
        per_genre = min(60, max(1, pool_size // max(1, len(preferred_genres))))
        search_genres = (preferred_genres[:2] or [None]) if hidden_only else preferred_genres
        for genre in search_genres:
            batch: list[MediaItem] = []
            for page in (1,) if hidden_only else (1, 2):
                media_list = _fetch_page(page, [genre] if genre else None)
                if not media_list:
                    break
                for item in _parse_media(media_list):
                    if item.id not in seen_ids:
                        seen_ids.add(item.id)
                        batch.append(item)
                if len(batch) >= per_genre:
                    break
            batches.append(batch)

        if not hidden_only:
            # Always retrieve exploration outside favorite genres. Round-robin
            # prevents an early genre's 50-item page from crowding out later ones.
            general: list[MediaItem] = []
            max_pages = max(2, (pool_size + 49) // 50 + 2)
            for page in range(1, max_pages + 1):
                media_list = _fetch_page(page, None)
                if not media_list:
                    break
                for item in _parse_media(media_list):
                    if item.id not in seen_ids:
                        seen_ids.add(item.id)
                        general.append(item)
                if general and sum(len(batch) for batch in batches) + len(general) >= pool_size:
                    break
            batches.append(general)

        all_candidates: list[MediaItem] = []
        for index in range(max((len(batch) for batch in batches), default=0)):
            for batch in batches:
                if index < len(batch):
                    all_candidates.append(batch[index])
        return all_candidates[:pool_size]

    def _fetch_planning(self, username: str) -> list[MediaItem]:
        """Fetch the user's Planning (plan-to-watch) entries as candidates."""
        query = """
        query ($username: String) {
          MediaListCollection(userName: $username, type: ANIME) {
            lists {
              entries {
                status
                media {
                  id
                  title { romaji english }
                  genres
                  tags { name rank }
                  meanScore
                  description(asHtml: false)
                  format
                  status
                  season
                  seasonYear
                  startDate { year month day }
                  episodes
                  nextAiringEpisode { episode airingAt }
                  studios(isMain: true) { nodes { name } }
                  relations {
                    edges {
                      relationType
                      node {
                        id
                        type
                        format
                        status
                        episodes
                        season
                        seasonYear
                        title { romaji english }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        data = self._graphql(query, {"username": username})
        lists = data.get("data", {}).get("MediaListCollection", {}).get("lists", [])
        items: list[MediaItem] = []
        for lst in lists:
            for entry in lst.get("entries", []):
                if entry.get("status") != "PLANNING":
                    continue
                media = entry.get("media", {})
                tags = [t["name"] for t in media.get("tags", []) if t.get("rank", 0) >= 80]
                studio = None
                studios = media.get("studios", {}).get("nodes", [])
                if studios:
                    studio = studios[0].get("name")
                desc = media.get("description", "")
                if desc and len(desc) > 200:
                    desc = desc[:200] + "..."
                related_ids, relations = _parse_relations(media)
                next_airing = media.get("nextAiringEpisode") or {}
                items.append(
                    MediaItem(
                        id=media["id"],
                        title_romaji=media.get("title", {}).get("romaji", ""),
                        title_english=media.get("title", {}).get("english"),
                        genres=media.get("genres", []),
                        tags=tags,
                        format=media.get("format"),
                        status=media.get("status"),
                        episodes=media.get("episodes"),
                        studio=studio,
                        description=desc,
                        mean_score=media.get("meanScore"),
                        related_ids=related_ids,
                        season=media.get("season"),
                        season_year=media.get("seasonYear"),
                        start_date=_date_string(media.get("startDate")),
                        next_airing_episode=next_airing.get("episode"),
                        next_airing_at=next_airing.get("airingAt"),
                        relations=relations,
                    )
                )
        return items

    def fetch(self, username: str, pool_size: int = 100, **kwargs) -> SourceData:
        history = self._fetch_history(username)
        exclude_ids = [m.id for m in history if m.id]

        use_planning = kwargs.get("use_planning", False)
        lane = _normalize_public_lane(kwargs.get("recommendation_lane", "best_match"))
        if use_planning:
            candidates = self._fetch_planning(username)
            watched_ids = {
                m.id for m in history if m.status in {"CURRENT", "COMPLETED", "DROPPED", "PAUSED"}
            }
            candidates = [c for c in candidates if c.id not in watched_ids]
            candidates = apply_lane_policy(history, candidates, "plan_list")
        else:
            # Analyze genre affinity from scored history
            preferred_genres = self._analyze_genre_affinity(history)
            if preferred_genres:
                candidates = self._fetch_candidates_targeted(
                    exclude_ids, pool_size, preferred_genres
                )
            else:
                candidates = self._fetch_candidates(exclude_ids, pool_size)
            if lane == "hidden_gems":
                # At most two extra pages, from a genuinely different visibility
                # stratum. Baseline still supplies breadth when metadata is sparse.
                gems = self._fetch_candidates_targeted(
                    exclude_ids,
                    min(pool_size, 100),
                    preferred_genres[:2],
                    hidden_only=True,
                )
                gems = apply_lane_policy(history, gems, lane)
                candidates = apply_lane_policy(history, candidates, lane)
                # Reserve up to half the pool for lower-visibility retrieval;
                # sorting everything by global score would erase this lane.
                merged = {item.id: item for item in gems[: max(1, pool_size // 2)]}
                for item in candidates:
                    merged.setdefault(item.id, item)
                candidates = list(merged.values())
            candidates = [c for c in candidates if c.id not in exclude_ids]
            if lane != "hidden_gems":
                candidates = apply_lane_policy(history, candidates, lane)
            candidates = candidates[:pool_size]
        return SourceData(
            username=username,
            history=history,
            candidates=candidates,
            source_name="anilist",
        )
