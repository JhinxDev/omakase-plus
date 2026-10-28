"""Regression tests for AniList franchise metadata and lane policy integration.

Bug discovered 2026-05-28: the candidate pool returned by AniList included
sequels and spin-offs of dropped or watched shows because AniList's
``id_not_in`` only excludes by exact id. The LLM then dutifully scored those
2/10 with reasoning citing the rule violation, polluting recommendations.

AniList now parses rich relation metadata and delegates keep/boost/block
decisions to the lane policy. Dropped/paused/low-rated franchise links still
block outside plan-list mode, while loved continuations can be boosted.
"""

from __future__ import annotations

from unittest.mock import patch

from omakase.adapters.anilist import (
    AniListAdapter,
    _candidate_is_in_franchise,
    _title_stem,
)
from omakase.types import MediaItem, MediaRelation

# ── _title_stem ─────────────────────────────────────────────────────────


def test_title_stem_strips_season_marker():
    assert _title_stem("Gintama Season 2") == "gintama"
    assert _title_stem("Attack on Titan Season 3 Part 2") == "attack on titan"


def test_title_stem_strips_subtitle_after_colon():
    assert _title_stem("Gintama: THE VERY FINAL") == "gintama"
    assert _title_stem("Code Geass: Lelouch of the Re;surrection") == "code geass"


def test_title_stem_picks_earliest_separator():
    # "Chainsaw Man - The Movie: Reze Arc" has both " - " and ": ".
    # We want the earliest separator (the dash) so the stem is "chainsaw man",
    # not "chainsaw man - the movie".
    assert _title_stem("Chainsaw Man - The Movie: Reze Arc") == "chainsaw man"
    # Unicode en-dash variant (the real AniList title uses U+2013)
    assert _title_stem("Chainsaw Man – The Movie: Reze Arc") == "chainsaw man"


def test_title_stem_strips_roman_numerals():
    assert _title_stem("Macross II") == "macross"
    assert _title_stem("Fate/Zero III") == "fate/zero"


def test_title_stem_strips_movie_film_special():
    assert _title_stem("Chainsaw Man Movie") == "chainsaw man"
    assert _title_stem("One Piece Film") == "one piece"
    assert _title_stem("Bleach Special") == "bleach"


def test_title_stem_strips_parenthetical_tail():
    assert _title_stem("Naruto (TV)") == "naruto"
    assert _title_stem("Hunter x Hunter (2011)") == "hunter x hunter"


def test_title_stem_handles_none_and_empty():
    assert _title_stem(None) == ""
    assert _title_stem("") == ""
    assert _title_stem("   ") == ""


# ── _candidate_is_in_franchise ──────────────────────────────────────────


def test_candidate_blocked_by_exact_id_match():
    cand = MediaItem(id=1, title_romaji="Anything")
    assert _candidate_is_in_franchise(cand, {1, 2}, set()) is True


def test_candidate_blocked_by_relation_to_history_id():
    cand = MediaItem(id=999, title_romaji="The Very Final", related_ids=[42, 7])
    assert _candidate_is_in_franchise(cand, {42}, set()) is True


def test_candidate_blocked_by_title_stem_match():
    cand = MediaItem(id=999, title_romaji="Gintama: THE VERY FINAL")
    assert _candidate_is_in_franchise(cand, set(), {"gintama"}) is True


def test_candidate_passes_when_unrelated():
    cand = MediaItem(
        id=999,
        title_romaji="A Completely Unrelated Show",
        related_ids=[1000, 1001],
    )
    assert _candidate_is_in_franchise(cand, {42, 50}, {"naruto", "bleach"}) is False


def test_candidate_blocked_via_english_title_stem():
    cand = MediaItem(id=999, title_romaji="ロロ", title_english="Naruto: The Last Movie")
    assert _candidate_is_in_franchise(cand, set(), {"naruto"}) is True


# ── AniList metadata parsing ────────────────────────────────────────────


def _candidate_payload(media_id: int, title: str, genres: list[str] | None = None) -> dict:
    return {
        "id": media_id,
        "title": {"romaji": title, "english": title},
        "genres": genres or ["Drama"],
        "tags": [],
        "meanScore": 81,
        "description": "desc",
        "format": "TV",
        "status": "FINISHED",
        "episodes": 12,
        "studios": {"nodes": []},
        "relations": {"edges": []},
    }


def test_anilist_candidates_parse_rich_relation_metadata(monkeypatch):
    adapter = AniListAdapter()

    def fake_graphql(query, variables):
        assert "seasonYear" in query
        assert "nextAiringEpisode" in query
        assert "relations" in query
        media = []
        if variables["page"] == 1:
            media = [
                {
                    "id": 2,
                    "title": {"romaji": "Base 2", "english": "Base 2"},
                    "genres": ["Drama"],
                    "tags": [],
                    "meanScore": 81,
                    "description": "desc",
                    "format": "TV",
                    "status": "RELEASING",
                    "season": "SPRING",
                    "seasonYear": 2026,
                    "startDate": {"year": 2026, "month": 4, "day": 1},
                    "episodes": 12,
                    "nextAiringEpisode": {"episode": 4, "airingAt": 1776200000},
                    "studios": {"nodes": []},
                    "relations": {
                        "edges": [
                            {
                                "relationType": "PREQUEL",
                                "node": {
                                    "id": 1,
                                    "type": "ANIME",
                                    "format": "TV",
                                    "status": "FINISHED",
                                    "episodes": 12,
                                    "season": "WINTER",
                                    "seasonYear": 2025,
                                    "title": {"romaji": "Base", "english": "Base"},
                                },
                            }
                        ]
                    },
                }
            ]
        return {"data": {"Page": {"media": media}}}

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)
    item = adapter._fetch_candidates([], 1)[0]
    assert item.status == "RELEASING"
    assert item.season == "SPRING"
    assert item.season_year == 2026
    assert item.start_date == "2026-04-01"
    assert item.next_airing_episode == 4
    assert item.relations[0].media_id == 1


def test_anilist_global_candidate_fetch_omits_empty_genre_filter(monkeypatch):
    adapter = AniListAdapter()
    seen_genres = []

    def fake_graphql(query, variables):
        seen_genres.append(variables.get("genres"))
        assert "genre_in:" not in query
        assert "$genres" not in query
        return {
            "data": {
                "Page": {
                    "media": [
                        {
                            "id": 2,
                            "title": {"romaji": "Global Pick", "english": "Global Pick"},
                            "genres": ["Drama"],
                            "tags": [],
                            "meanScore": 81,
                            "description": "desc",
                            "format": "TV",
                            "status": "FINISHED",
                            "episodes": 12,
                            "studios": {"nodes": []},
                            "relations": {"edges": []},
                        }
                    ]
                    if variables["page"] == 1
                    else []
                }
            }
        }

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)
    item = adapter._fetch_candidates([], 1)[0]
    assert item.id == 2
    assert seen_genres[0] is None


def test_anilist_global_candidates_continue_after_excluded_page(monkeypatch):
    adapter = AniListAdapter()
    seen_pages = []

    def fake_graphql(query, variables):
        seen_pages.append(variables["page"])
        media_by_page = {
            1: [_candidate_payload(1, "Already Watched")],
            2: [_candidate_payload(2, "Fresh Pick")],
        }
        return {"data": {"Page": {"media": media_by_page.get(variables["page"], [])}}}

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)

    items = adapter._fetch_candidates([1], 1)

    assert [item.id for item in items] == [2]
    assert seen_pages[:2] == [1, 2]


def test_anilist_targeted_candidates_continue_after_duplicate_pages(monkeypatch):
    adapter = AniListAdapter()
    seen_calls = []

    def fake_graphql(query, variables):
        page = variables["page"]
        genres = variables.get("genres")
        seen_calls.append((page, tuple(genres) if genres else None))
        if genres == ["Drama"] and page == 1:
            media = [_candidate_payload(1, "Already Watched", ["Drama"])]
        elif genres == ["Drama"] and page == 2:
            media = [_candidate_payload(2, "Fresh Drama", ["Drama"])]
        elif genres is None and page == 1:
            media = [_candidate_payload(1, "Already Watched", ["Action"])]
        elif genres is None and page == 2:
            media = [_candidate_payload(3, "Fresh Top Up", ["Action"])]
        else:
            media = []
        return {"data": {"Page": {"media": media}}}

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)

    items = adapter._fetch_candidates_targeted([1], 3, ["Drama"])

    assert [item.id for item in items] == [2, 3]
    assert (2, ("Drama",)) in seen_calls
    assert (2, None) in seen_calls


# ── fetch() integration ─────────────────────────────────────────────────


def _hist():
    return [
        MediaItem(id=100, title_romaji="Gintama Season 2", status="DROPPED"),
        MediaItem(id=200, title_romaji="Chainsaw Man", status="COMPLETED"),
    ]


def test_fetch_drops_sequel_via_relations():
    """A sequel whose rich relation points at a history id must be excluded."""
    adapter = AniListAdapter()
    candidates_in = [
        MediaItem(
            id=101,
            title_romaji="The Very Final",
            relations=[
                MediaRelation(
                    relation_type="PREQUEL",
                    media_id=100,
                    title_romaji="Dropped First Season",
                )
            ],
        ),
        MediaItem(id=999, title_romaji="Fresh Original Show", related_ids=[]),
    ]
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_candidates_targeted", return_value=candidates_in),
        patch.object(adapter, "_analyze_genre_affinity", return_value=["Action"]),
    ):
        data = adapter.fetch("anyuser", use_planning=False)
    cand_ids = {c.id for c in data.candidates}
    assert 101 not in cand_ids, "sequel related to history must be dropped"
    assert 999 in cand_ids, "unrelated candidate must pass through"


def test_fetch_internal_lanes_default_to_best_match_filtering():
    """The public adapter must not expose internal lane policies."""
    adapter = AniListAdapter()
    history = [MediaItem(id=1, title_romaji="Watched Base", score=9, status="COMPLETED")]
    missing_prequel = MediaItem(
        id=2,
        title_romaji="Unseen Follow-up",
        relations=[
            MediaRelation(
                relation_type="PREQUEL",
                media_id=999,
                title_romaji="Missing First Entry",
                status="FINISHED",
            )
        ],
    )
    original = MediaItem(id=3, title_romaji="Fresh Original Show")

    with (
        patch.object(adapter, "_fetch_history", return_value=history),
        patch.object(adapter, "_fetch_candidates", return_value=[missing_prequel, original]),
        patch.object(adapter, "_analyze_genre_affinity", return_value=[]),
    ):
        for lane in ("discover", "plan_list", "not-a-public-lane"):
            data = adapter.fetch("anyuser", use_planning=False, recommendation_lane=lane)

            assert [candidate.id for candidate in data.candidates] == [3]


def test_fetch_blocks_dropped_title_stem_but_keeps_unscored_completed_franchise():
    """Title-stem policy blocks disliked franchises without blanket-dropping neutral ones."""
    adapter = AniListAdapter()
    candidates_in = [
        # No related_ids set — simulates incomplete AniList metadata
        MediaItem(id=102, title_romaji="Gintama Season 3", related_ids=[]),
        MediaItem(id=201, title_romaji="Chainsaw Man – The Movie: Reze Arc", related_ids=[]),
        MediaItem(id=999, title_romaji="Mushoku Tensei", related_ids=[]),
    ]
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_candidates_targeted", return_value=candidates_in),
        patch.object(adapter, "_analyze_genre_affinity", return_value=["Action"]),
    ):
        data = adapter.fetch("anyuser", use_planning=False)
    cand_ids = {c.id for c in data.candidates}
    assert 102 not in cand_ids, "Gintama Season 3 must be dropped by title stem"
    assert 201 in cand_ids, "completed but unscored franchise entries are no longer blanket-dropped"
    assert 999 in cand_ids, "Mushoku Tensei is unrelated and must pass"


def test_fetch_planning_mode_unaffected_by_franchise_filter():
    """Planning-mode candidates are user-selected — never franchise-filter them."""
    adapter = AniListAdapter()
    planning = [
        MediaItem(id=101, title_romaji="Gintama: THE VERY FINAL"),
        MediaItem(id=999, title_romaji="Fresh Show"),
    ]
    with (
        patch.object(adapter, "_fetch_history", return_value=_hist()),
        patch.object(adapter, "_fetch_planning", return_value=planning),
    ):
        data = adapter.fetch("anyuser", use_planning=True)
    cand_ids = {c.id for c in data.candidates}
    # Planning items the user explicitly chose are kept even when same-franchise.
    assert cand_ids == {101, 999}


def test_fetch_new_seasons_keeps_loved_continuations(monkeypatch):
    adapter = AniListAdapter()
    history = [
        MediaItem(id=1, title_romaji="Base", title_english="Base", score=9, status="COMPLETED")
    ]
    candidates = [
        MediaItem(
            id=2,
            title_romaji="Base 2",
            title_english="Base 2",
            status="RELEASING",
            season_year=2026,
            relations=[MediaRelation(relation_type="PREQUEL", media_id=1, title_romaji="Base")],
        )
    ]
    monkeypatch.setattr(adapter, "_fetch_history", lambda username: history)
    monkeypatch.setattr(adapter, "_analyze_genre_affinity", lambda history: ["Drama"])
    monkeypatch.setattr(
        adapter, "_fetch_candidates_targeted", lambda exclude_ids, pool_size, genres: candidates
    )
    data = adapter.fetch("me", 10, recommendation_lane="new_seasons")
    assert data.candidates[0].franchise_policy == "boosted"


def test_genre_affinity_uses_dropped_history_without_popularity_volume_bias():
    adapter = AniListAdapter()
    history = [MediaItem(id=1, title_romaji="Love", score=9, genres=["Mystery"])]
    history += [
        MediaItem(id=i, title_romaji="Meh", score=6, genres=["Action"]) for i in range(2, 20)
    ]
    history += [
        MediaItem(id=20, title_romaji="Quit", status="DROPPED", score=0, genres=["Mystery"])
    ]
    assert adapter._analyze_genre_affinity(history) == []
    assert adapter._analyze_genre_affinity(history[:-1]) == ["Mystery"]


def test_hidden_gem_retrieval_is_a_bounded_lower_visibility_source(monkeypatch):
    adapter = AniListAdapter()
    calls = []

    def fake_graphql(query, variables):
        calls.append(variables)
        assert "popularity_lesser: $visibility" in query
        return {"data": {"Page": {"media": [_candidate_payload(len(calls), "Gem")]}}}

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)
    gems = adapter._fetch_candidates_targeted(
        [], 100, ["Mystery", "Drama", "Comedy"], hidden_only=True
    )
    assert len(calls) == 2
    assert all(call["visibility"] == 50000 and call["page"] == 1 for call in calls)
    assert [item.id for item in gems] == [1, 2]


def test_hidden_gem_lane_keeps_eligible_gems_when_baseline_has_higher_scores(monkeypatch):
    adapter = AniListAdapter()
    monkeypatch.setattr(
        adapter, "_fetch_history", lambda _username: [MediaItem(id=1, title_romaji="Seen", score=9)]
    )
    monkeypatch.setattr(adapter, "_analyze_genre_affinity", lambda _history: [])
    monkeypatch.setattr(
        adapter,
        "_fetch_candidates",
        lambda *_args: [MediaItem(id=10, title_romaji="Popular", mean_score=99)],
    )
    monkeypatch.setattr(
        adapter,
        "_fetch_candidates_targeted",
        lambda *_args, **_kwargs: [MediaItem(id=20, title_romaji="Gem", mean_score=75)],
    )
    data = adapter.fetch("test", pool_size=2, recommendation_lane="hidden_gems")
    assert {item.id for item in data.candidates} == {10, 20}


def test_targeted_pool_interleaves_genres_and_general_exploration(monkeypatch):
    adapter = AniListAdapter()

    def fake_graphql(query, variables):
        genre = (variables.get("genres") or ["Comedy"])[0]
        start = {"Drama": 100, "Mystery": 200, "Comedy": 300}[genre]
        media = [_candidate_payload(start + i, f"{genre} {i}", [genre]) for i in range(50)]
        return {"data": {"Page": {"media": media}}}

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)
    candidates = adapter._fetch_candidates_targeted([], 6, ["Drama", "Mystery"])
    assert [item.genres[0] for item in candidates] == ["Drama", "Mystery", "Comedy"] * 2


def test_targeted_normal_queries_omit_absent_visibility_and_exploration_genre(monkeypatch):
    adapter = AniListAdapter()
    calls = []

    def fake_graphql(query, variables):
        calls.append((query, variables))
        assert "popularity_lesser" not in query
        assert "$visibility" not in query
        assert "visibility" not in variables
        if variables.get("genres") is None:
            assert "genre_in:" not in query
            assert "$genres" not in query
        else:
            assert "genre_in: $genres" in query
        return {"data": {"Page": {"media": []}}}

    monkeypatch.setattr(adapter, "_graphql", fake_graphql)
    assert adapter._fetch_candidates_targeted([], 1, ["Action"]) == []
    assert any(variables.get("genres") == ["Action"] for _, variables in calls)
    assert any("genres" not in variables for _, variables in calls)
