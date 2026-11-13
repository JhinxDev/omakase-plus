"""Tests for the LLM response parser."""

from __future__ import annotations

import pytest

from omakase.engine import LLMOutputParseError, _parse_recommendations, _resolve_rec_urls, run
from omakase.types import (
    MediaItem,
    OmakaseConfig,
    Recommendation,
    RecommendationFeedbackSignal,
    SourceData,
)

SAMPLE_JSON = """
{"recommendations": [
  {"title": "Vinland Saga", "predicted_score": 9, "reasoning": "Morally complex protagonist, dense world-building.", "best_match_from_history": "Berserk"},
  {"title": "Mushishi", "predicted_score": 8, "reasoning": "Quiet, atmospheric pacing.", "best_match_from_history": "Natsume's Book of Friends"}
]}
"""


def test_parses_clean_json():
    recs = _parse_recommendations(SAMPLE_JSON)
    assert len(recs) == 2
    assert recs[0].title == "Vinland Saga"
    assert recs[0].predicted_score == 9.0
    assert "Berserk" in recs[0].best_match_from_history


def test_strips_markdown_code_fences():
    wrapped = "```json\n" + SAMPLE_JSON.strip() + "\n```"
    recs = _parse_recommendations(wrapped)
    assert len(recs) == 2


def test_strips_leading_and_trailing_prose():
    noisy = "Here are your picks:\n" + SAMPLE_JSON.strip() + "\nHope you enjoy!"
    recs = _parse_recommendations(noisy)
    assert len(recs) == 2


def test_raises_on_invalid_json():
    """An unparseable response surfaces as an explicit error.

    Previously this returned `[]` silently, so a max-tokens truncation in
    the LLM call landed as a "0 picks" run in the dashboard with no
    indication of what failed. Now the caller sees a clear error.
    """
    with pytest.raises(LLMOutputParseError):
        _parse_recommendations("not json at all")


def test_raises_on_truncated_json():
    """Mid-string truncation (the real-world max-tokens case) raises."""
    truncated = (
        '{"recommendations": [{"title": "Vinland Saga", "predicted_score": 9, '
        '"reasoning": "Morally complex protagonist who'  # cut mid-string
    )
    with pytest.raises(LLMOutputParseError):
        _parse_recommendations(truncated)


def test_raises_clear_error_on_empty_llm_content():
    with pytest.raises(LLMOutputParseError, match="empty final answer"):
        _parse_recommendations("")


def test_handles_missing_fields_gracefully():
    raw = '{"recommendations": [{"title": "Made in Abyss"}]}'
    recs = _parse_recommendations(raw)
    assert len(recs) == 1
    assert recs[0].title == "Made in Abyss"
    assert recs[0].predicted_score == 0.0
    assert recs[0].reasoning == ""


def test_parse_recommendation_optional_intelligence_fields():
    raw = (
        '{"recommendations":[{'
        '"title":"Frieren Season 2",'
        '"predicted_score":9.1,'
        '"reasoning":"because",'
        '"best_match_from_history":"Frieren",'
        '"anilist_id":123,'
        '"airing_status":"RELEASING",'
        '"franchise_note":"Loved franchise continuation",'
        '"sequence_warning":"Start with season 1",'
        '"lane_reason":"new season"'
        "}]}"
    )
    rec = _parse_recommendations(raw)[0]
    assert rec.anilist_id == 123
    assert rec.airing_status == "RELEASING"
    assert rec.franchise_note == "Loved franchise continuation"
    assert rec.sequence_warning == "Start with season 1"
    assert rec.lane_reason == "new season"


def test_run_passes_lane_and_feedback_to_adapter_and_prompt(monkeypatch):
    calls = {}
    history = [MediaItem(id=1, title_romaji="Loved", title_english="Loved", score=9)]
    candidates = [MediaItem(id=2, title_romaji="Candidate", title_english="Candidate")]
    feedback = [
        RecommendationFeedbackSignal(media_id=2, title="Candidate", feedback_type="interested")
    ]

    class FakeAdapter:
        def fetch(self, username, pool_size=100, **kwargs):
            calls["fetch"] = {
                "username": username,
                "pool_size": pool_size,
                "kwargs": kwargs,
            }
            return SourceData(
                username=username,
                history=history,
                candidates=candidates,
                source_name="anilist",
            )

    class FakeLLM:
        def generate(self, prompt, **kwargs):
            calls["generate"] = {"prompt": prompt, "kwargs": kwargs}
            return (
                '{"recommendations":[{"title":"Candidate","predicted_score":8,'
                '"reasoning":"because","best_match_from_history":"Loved"}]}'
            )

    def fake_build_prompt(
        taste_profile,
        prompt_history,
        prompt_candidates,
        n_recs=10,
        lane=None,
        feedback=None,
        **preferences,
    ):
        calls["prompt"] = {
            "taste_profile": taste_profile,
            "history": prompt_history,
            "candidates": prompt_candidates,
            "n_recs": n_recs,
            "lane": lane,
            "feedback": feedback,
            "preferences": preferences,
        }
        return "prompt text"

    monkeypatch.setattr("omakase.engine.get_adapter", lambda source: FakeAdapter())
    monkeypatch.setattr("omakase.engine.get_llm", lambda llm_type, llm_url, model: FakeLLM())
    monkeypatch.setattr("omakase.engine.build_prompt", fake_build_prompt)

    cfg = OmakaseConfig(
        source="anilist",
        username="tester",
        llm_url="http://llm",
        model="model",
        profile_path="",
        num_recommendations=1,
        recommendation_lane="new_seasons",
        feedback=feedback,
    )

    recs = run(cfg)

    assert calls["fetch"]["kwargs"]["recommendation_lane"] == "new_seasons"
    assert calls["prompt"]["lane"] == "new_seasons"
    assert calls["prompt"]["feedback"] is feedback
    assert calls["prompt"]["n_recs"] == 1
    assert calls["prompt"]["preferences"] == {
        "mood": "",
        "max_episodes": 0,
        "completed_only": False,
    }
    assert calls["generate"]["prompt"] == "prompt text"
    assert recs[0].media_id == 2


def test_run_passes_credentials_by_value_without_process_environment(monkeypatch):
    calls = {}

    class FakeAdapter:
        def fetch(self, username, pool_size=100, **kwargs):
            calls["adapter_kwargs"] = kwargs
            return SourceData(
                username=username,
                history=[MediaItem(id=1, title_romaji="Loved", score=9)],
                candidates=[MediaItem(id=2, title_romaji="Fresh")],
                source_name="myanimelist",
            )

    class FakeLLM:
        def generate(self, prompt, **kwargs):
            return (
                '{"recommendations":[{"title":"Fresh","predicted_score":8,'
                '"reasoning":"fit","best_match_from_history":"Loved"}]}'
            )

    def fake_get_llm(llm_type, llm_url, model, api_key=None):
        calls["api_key"] = api_key
        return FakeLLM()

    monkeypatch.setattr("omakase.engine.get_adapter", lambda _source: FakeAdapter())
    monkeypatch.setattr("omakase.engine.get_llm", fake_get_llm)
    monkeypatch.setattr("omakase.engine.build_prompt", lambda *_args, **_kwargs: "prompt")

    run(
        OmakaseConfig(
            source="myanimelist",
            username="tester",
            llm_url="https://models.example",
            model="model",
            profile_path="",
            api_key="fixture-key",
            mal_client_id="request-only-mal-client",
        )
    )

    assert calls["api_key"] == "fixture-key"
    assert calls["adapter_kwargs"]["mal_client_id"] == "request-only-mal-client"


def test_run_deterministically_excludes_watched_and_rejected_feedback(monkeypatch):
    calls = {}
    history = [MediaItem(id=1, title_romaji="Loved", score=9)]
    candidates = [
        MediaItem(id=2, title_romaji="Already Seen", title_english="Already Seen"),
        MediaItem(id=3, title_romaji="Fresh Pick", title_english="Fresh Pick"),
    ]
    feedback = [
        RecommendationFeedbackSignal(
            media_id=2,
            title="Already Seen",
            feedback_type="already_watched",
        )
    ]

    class FakeAdapter:
        def fetch(self, username, pool_size=100, **kwargs):
            return SourceData(
                username=username,
                history=history,
                candidates=candidates,
                source_name="anilist",
            )

    class FakeLLM:
        def generate(self, prompt, **kwargs):
            return (
                '{"recommendations":['
                '{"title":"Already Seen","predicted_score":9,"reasoning":"bad",'
                '"best_match_from_history":"Loved"},'
                '{"title":"Fresh Pick","predicted_score":8,"reasoning":"good",'
                '"best_match_from_history":"Loved"}]}'
            )

    def fake_build_prompt(_profile, _history, prompt_candidates, **_kwargs):
        calls["candidate_ids"] = [item.id for item in prompt_candidates]
        return "prompt"

    monkeypatch.setattr("omakase.engine.get_adapter", lambda _source: FakeAdapter())
    monkeypatch.setattr("omakase.engine.get_llm", lambda *_args: FakeLLM())
    monkeypatch.setattr("omakase.engine.build_prompt", fake_build_prompt)

    recs = run(
        OmakaseConfig(
            source="anilist",
            username="tester",
            llm_url="http://llm",
            model="model",
            profile_path="",
            feedback=feedback,
        )
    )

    assert calls["candidate_ids"] == [3]
    assert [item.title for item in recs] == ["Fresh Pick"]
    assert recs[0].media_id == 3


def _rec(title: str) -> Recommendation:
    return Recommendation(
        title=title,
        predicted_score=8.0,
        reasoning="",
        best_match_from_history="",
    )


def test_resolve_urls_anilist_permalink_on_title_match():
    candidates = [
        MediaItem(id=21, title_romaji="One Piece", title_english="One Piece"),
        MediaItem(id=99, title_romaji="Vinland Saga", title_english="Vinland Saga"),
    ]
    recs = [_rec("Vinland Saga")]
    _resolve_rec_urls(recs, candidates, "anilist")
    assert recs[0].source == "anilist"
    assert recs[0].url == "https://anilist.co/anime/99/"


def test_resolve_urls_candidate_match_overrides_prefilled_bad_ids():
    rec = Recommendation(
        title="Vinland Saga",
        predicted_score=9,
        reasoning="",
        best_match_from_history="",
        anilist_id=999,
        media_id=999,
        airing_status="BAD_STATUS",
        franchise_note="bad note",
        sequence_warning="bad warning",
    )
    candidate = MediaItem(
        id=123,
        title_romaji="Vinland Saga",
        title_english="Vinland Saga",
        status="FINISHED",
        franchise_note="Loved franchise continuation.",
        sequence_warning="Sequencing check",
    )

    _resolve_rec_urls([rec], [candidate], "anilist")

    assert rec.url == "https://anilist.co/anime/123/"
    assert rec.anilist_id == 123
    assert rec.media_id == 123
    assert rec.airing_status == "FINISHED"
    assert rec.franchise_note == "Loved franchise continuation."
    assert rec.sequence_warning == "Sequencing check"


def test_resolve_urls_mal_permalink_on_title_match():
    candidates = [
        MediaItem(
            id=5114,
            title_romaji="Hagane no Renkinjutsushi",
            title_english="Fullmetal Alchemist: Brotherhood",
        ),
    ]
    recs = [_rec("Fullmetal Alchemist: Brotherhood")]
    _resolve_rec_urls(recs, candidates, "myanimelist")
    assert recs[0].source == "myanimelist"
    assert recs[0].url == "https://myanimelist.net/anime/5114/"


def test_resolve_rec_urls_clears_anilist_id_for_mal_match():
    rec = Recommendation(
        title="Vinland Saga",
        predicted_score=9,
        reasoning="",
        best_match_from_history="",
        anilist_id=999,
        media_id=999,
    )
    candidate = MediaItem(
        id=456,
        title_romaji="Vinland Saga",
        title_english="Vinland Saga",
    )

    _resolve_rec_urls([rec], [candidate], "myanimelist")

    assert rec.url == "https://myanimelist.net/anime/456/"
    assert rec.media_id == 456
    assert rec.anilist_id is None


def test_resolve_urls_falls_back_to_search_when_no_match():
    candidates = [MediaItem(id=1, title_romaji="Cowboy Bebop", title_english="Cowboy Bebop")]
    recs = [_rec("Some Obscure Title")]
    _resolve_rec_urls(recs, candidates, "anilist")
    assert recs[0].url is not None
    assert "anilist.co/search/anime" in recs[0].url
    assert "Some+Obscure+Title" in recs[0].url


def test_resolve_rec_urls_clears_unverified_ids_on_search_fallback():
    candidates = [MediaItem(id=1, title_romaji="Cowboy Bebop", title_english="Cowboy Bebop")]
    rec = Recommendation(
        title="Some Obscure Title",
        predicted_score=9,
        reasoning="",
        best_match_from_history="",
        anilist_id=999,
        media_id=999,
        airing_status="BAD_STATUS",
        franchise_note="bad note",
        sequence_warning="bad warning",
    )

    _resolve_rec_urls([rec], candidates, "anilist")

    assert rec.url == "https://anilist.co/search/anime?search=Some+Obscure+Title"
    assert rec.anilist_id is None
    assert rec.media_id is None
    assert rec.airing_status is None
    assert rec.franchise_note is None
    assert rec.sequence_warning is None


def test_resolve_urls_match_is_case_insensitive_and_uses_romaji_too():
    candidates = [
        MediaItem(id=42, title_romaji="Shingeki no Kyojin", title_english="Attack on Titan"),
    ]
    recs_en = [_rec("attack on titan")]
    recs_ro = [_rec("Shingeki No Kyojin")]
    _resolve_rec_urls(recs_en, candidates, "anilist")
    _resolve_rec_urls(recs_ro, candidates, "anilist")
    assert recs_en[0].url == "https://anilist.co/anime/42/"
    assert recs_ro[0].url == "https://anilist.co/anime/42/"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "11", "-1", "true"])
def test_rejects_invalid_prediction_numbers(value):
    with pytest.raises(LLMOutputParseError):
        _parse_recommendations(
            '{"recommendations":[{"title":"Fresh","predicted_score":' + value + "}]}"
        )


@pytest.mark.parametrize("raw", ["[]", '{"recommendations":{}}', '{"recommendations":[null]}'])
def test_rejects_wrong_response_shapes(raw):
    with pytest.raises(LLMOutputParseError):
        _parse_recommendations(raw)


def test_grounding_resolves_unicode_punctuation_and_id_only_aliases():
    candidates = [MediaItem(id=42, title_romaji="Odd Taxi", title_english="Odd: Taxi")]
    recs = [_rec("ＯＤＤ taxi"), _rec("An alternate title")]
    recs[1].media_id = 42
    _resolve_rec_urls(recs, candidates, "anilist")
    assert [item.media_id for item in recs] == [42, 42]
    assert [item.title for item in recs] == ["Odd: Taxi", "Odd: Taxi"]


def test_ambiguous_alias_requires_a_real_candidate_id():
    candidates = [
        MediaItem(id=42, title_romaji="Shared Title"),
        MediaItem(id=43, title_romaji="Shared Title"),
    ]
    recs = [_rec("Shared Title"), _rec("Shared Title")]
    recs[1].media_id = 43
    _resolve_rec_urls(recs, candidates, "anilist")
    assert recs[0].media_id is None
    assert recs[1].media_id == 43


def test_run_hard_filters_grounding_duplicates_and_seen_history(monkeypatch):
    import json

    prompted = {}
    candidates = [
        MediaItem(id=1, title_romaji="Seen", status="FINISHED", episodes=12),
        MediaItem(
            id=2, title_romaji="Fresh", title_english="Fresh: Pick", status="FINISHED", episodes=12
        ),
        MediaItem(id=3, title_romaji="Long", status="FINISHED", episodes=100),
        MediaItem(id=4, title_romaji="Airing", status="RELEASING", episodes=12),
        MediaItem(id=5, title_romaji="Unknown length", status="FINISHED"),
    ]

    class FakeAdapter:
        def fetch(self, username, *_args, **_kwargs):
            return SourceData(
                username, [MediaItem(id=1, title_romaji="Seen", score=9)], candidates, "anilist"
            )

    class FakeLLM:
        def generate(self, prompt, **kwargs):
            return json.dumps(
                {
                    "recommendations": [
                        {"title": title, "media_id": mid, "predicted_score": 8}
                        for title, mid in [
                            ("Seen", 1),
                            ("Fresh Pick", 999),
                            ("Fresh", 2),
                            ("Invented", 999),
                            ("Long", 3),
                            ("Airing", 4),
                            ("Unknown length", 5),
                        ]
                    ]
                }
            )

    def capture_prompt(_profile, _history, pool, **kwargs):
        prompted.update(ids=[item.id for item in pool], preferences=kwargs)
        return "prompt"

    monkeypatch.setattr("omakase.engine.get_adapter", lambda _source: FakeAdapter())
    monkeypatch.setattr("omakase.engine.get_llm", lambda *_args: FakeLLM())
    monkeypatch.setattr("omakase.engine.build_prompt", capture_prompt)
    picks = run(
        OmakaseConfig(
            source="anilist",
            username="test",
            llm_url="http://llm",
            model="model",
            profile_path="",
            max_episodes=13,
            completed_only=True,
            mood="quiet mystery",
        )
    )
    assert prompted["ids"] == [2]
    assert prompted["preferences"]["mood"] == "quiet mystery"
    assert [(item.media_id, item.title) for item in picks] == [(2, "Fresh: Pick")]


def test_run_rejects_entirely_hallucinated_picks(monkeypatch):
    class FakeAdapter:
        def fetch(self, username, *_args, **_kwargs):
            return SourceData(
                username,
                [MediaItem(id=1, title_romaji="Seen")],
                [MediaItem(id=2, title_romaji="Real")],
                "anilist",
            )

    class FakeLLM:
        def generate(self, prompt, **kwargs):
            return '{"recommendations":[{"title":"Invented","media_id":999,"predicted_score":8}]}'

    monkeypatch.setattr("omakase.engine.get_adapter", lambda _source: FakeAdapter())
    monkeypatch.setattr("omakase.engine.get_llm", lambda *_args: FakeLLM())
    with pytest.raises(LLMOutputParseError, match="no valid picks"):
        run(
            OmakaseConfig(
                source="anilist",
                username="test",
                llm_url="http://llm",
                model="model",
                profile_path="",
            )
        )


def test_taste_ranking_accounts_for_dropped_history_and_keeps_breadth():
    from omakase.engine import _rank_candidates

    history = [
        MediaItem(id=1, title_romaji="Loved", score=9, genres=["Mystery"]),
        MediaItem(id=2, title_romaji="Quit", score=0, status="DROPPED", genres=["Action"]),
    ]
    candidates = [
        MediaItem(id=3, title_romaji="Popular Action", genres=["Action"], mean_score=95),
        MediaItem(id=4, title_romaji="Quiet Mystery", genres=["Mystery"], mean_score=75),
        MediaItem(id=5, title_romaji="Explore", genres=["Comedy"], mean_score=80),
    ]
    ranked = _rank_candidates(history, candidates)
    assert ranked[0].id == 4
    assert {item.id for item in ranked} == {3, 4, 5}


def test_plan_lane_preserves_planned_candidates_without_mislabeling_them_as_watched(monkeypatch):
    prompts = []

    class FakeAdapter:
        def fetch(self, username, *_args, **_kwargs):
            return SourceData(
                username,
                [
                    MediaItem(id=1, title_romaji="Loved", score=9, status="COMPLETED"),
                    MediaItem(id=2, title_romaji="Planned", score=0, status="PLANNING"),
                ],
                [MediaItem(id=2, title_romaji="Planned", status="FINISHED", episodes=12)],
                "anilist",
            )

    class FakeLLM:
        def generate(self, prompt, **kwargs):
            prompts.append(prompt)
            return '{"recommendations":[{"title":"Planned","media_id":2,"predicted_score":8}]}'

    monkeypatch.setattr("omakase.engine.get_adapter", lambda _source: FakeAdapter())
    monkeypatch.setattr("omakase.engine.get_llm", lambda *_args: FakeLLM())
    picks = run(
        OmakaseConfig(
            source="anilist",
            username="test",
            llm_url="http://llm",
            model="model",
            profile_path="",
            use_planning=True,
            recommendation_lane="plan_list",
        )
    )
    assert [item.media_id for item in picks] == [2]
    assert "USER RATING HISTORY (1 titles)" in prompts[0]
    assert "CANDIDATE POOL (1 anime" in prompts[0]
