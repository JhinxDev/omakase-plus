from omakase.franchise import apply_lane_policy
from omakase.types import MediaItem, MediaRelation


def hist(media_id, title, score=None, status="COMPLETED"):
    return MediaItem(
        id=media_id, title_romaji=title, title_english=title, score=score, status=status
    )


def cand(
    media_id,
    title,
    relation_type="SEQUEL",
    related_id=1,
    status="FINISHED",
    season_year=2026,
    relation_status=None,
    relation_title="Base",
):
    return MediaItem(
        id=media_id,
        title_romaji=title,
        title_english=title,
        status=status,
        season_year=season_year,
        mean_score=82,
        relations=[
            MediaRelation(
                relation_type=relation_type,
                media_id=related_id,
                title_romaji=relation_title,
                status=relation_status,
            )
        ],
    )


def test_loved_franchise_continuation_is_boosted():
    result = apply_lane_policy([hist(1, "Base", score=9)], [cand(2, "Base 2")], "best_match")
    assert result[0].franchise_policy == "boosted"
    assert "Loved franchise" in result[0].franchise_note


def test_low_rated_franchise_is_blocked_outside_plan_lane():
    result = apply_lane_policy([hist(1, "Base", score=4)], [cand(2, "Base 2")], "best_match")
    assert result == []


def test_paused_franchise_is_blocked_outside_plan_lane():
    result = apply_lane_policy(
        [hist(1, "Base", status="PAUSED")], [cand(2, "Base 2")], "new_seasons"
    )
    assert result == []


def test_plan_lane_warns_instead_of_dropping_blocked_relation():
    result = apply_lane_policy([hist(1, "Base", score=3)], [cand(2, "Base 2")], "plan_list")
    assert len(result) == 1
    assert result[0].franchise_policy == "blocked"
    assert "Low-rated" in result[0].franchise_note


def test_six_or_seven_relation_is_neutral():
    result = apply_lane_policy([hist(1, "Base", score=7)], [cand(2, "Base 2")], "best_match")
    assert result[0].franchise_policy == "neutral"


def test_new_seasons_orders_boosted_recent_before_unrelated_finished():
    boosted = cand(2, "Base 2", status="RELEASING", season_year=2026)
    unrelated = MediaItem(
        id=9,
        title_romaji="Older Gem",
        title_english="Older Gem",
        status="FINISHED",
        season_year=2012,
        mean_score=91,
    )
    result = apply_lane_policy([hist(1, "Base", score=9)], [unrelated, boosted], "new_seasons")
    assert result[0].id == 2


def test_new_seasons_orders_releasing_recent_neutral_before_finished_old():
    boosted = cand(2, "Base 2", status="FINISHED", season_year=2010)
    finished_old = MediaItem(
        id=3,
        title_romaji="Finished Old",
        title_english="Finished Old",
        status="FINISHED",
        season_year=2012,
        mean_score=95,
    )
    releasing_recent = MediaItem(
        id=4,
        title_romaji="Releasing Recent",
        title_english="Releasing Recent",
        status="RELEASING",
        season_year=2026,
        mean_score=70,
    )

    result = apply_lane_policy(
        [hist(1, "Base", score=9)],
        [finished_old, releasing_recent, boosted],
        "new_seasons",
    )

    assert [item.id for item in result] == [2, 4, 3]


def test_hidden_gems_orders_neutral_candidates_by_score_then_older_year():
    newer_lower_score = MediaItem(
        id=2,
        title_romaji="Newer Lower Score",
        title_english="Newer Lower Score",
        status="FINISHED",
        season_year=2026,
        mean_score=70,
    )
    older_higher_score = MediaItem(
        id=3,
        title_romaji="Older Higher Score",
        title_english="Older Higher Score",
        status="FINISHED",
        season_year=2014,
        mean_score=91,
    )

    result = apply_lane_policy([], [newer_lower_score, older_higher_score], "hidden_gems")

    assert [item.id for item in result] == [3, 2]


def test_discover_boosts_loose_franchise_entries_and_penalizes_missing_context():
    side_story = cand(2, "Base Side Story", relation_type="SIDE_STORY", related_id=1)
    neutral = MediaItem(
        id=3,
        title_romaji="Original One",
        title_english="Original One",
        status="FINISHED",
        mean_score=70,
    )
    missing_prequel = cand(
        4,
        "Unknown Continuation",
        relation_type="PREQUEL",
        related_id=99,
        relation_status="FINISHED",
        relation_title="Unknown Season 1",
    )

    result = apply_lane_policy(
        [hist(1, "Base", score=9)],
        [missing_prequel, neutral, side_story],
        "discover",
    )

    assert [item.id for item in result] == [2, 3, 4]
    assert result[0].franchise_policy == "boosted"
    assert result[0].loose_order is True
    assert "Unknown Season 1" in result[-1].sequence_warning


def test_best_match_blocks_finished_prerequisite_absent_from_history():
    result = apply_lane_policy(
        [hist(10, "Different Show", score=9)],
        [
            cand(
                2,
                "Base 2",
                relation_type="PREQUEL",
                related_id=1,
                relation_status="FINISHED",
            )
        ],
        "best_match",
    )

    assert result == []


def test_plan_list_keeps_neutral_before_finished_but_missing_prerequisite():
    missing_prequel = cand(
        2,
        "Base 2",
        relation_type="PREQUEL",
        related_id=1,
        relation_status="FINISHED",
    )
    unrelated = MediaItem(
        id=3,
        title_romaji="Unrelated",
        title_english="Unrelated",
        status="FINISHED",
        mean_score=40,
    )

    result = apply_lane_policy(
        [hist(10, "Different Show", score=9)],
        [missing_prequel, unrelated],
        "plan_list",
    )

    assert [item.id for item in result] == [3, 2]
    assert "Base" in result[1].sequence_warning


def test_best_match_neutral_candidates_preserve_source_order():
    releasing = MediaItem(
        id=2,
        title_romaji="First Source Candidate",
        title_english="First Source Candidate",
        status="RELEASING",
        season_year=2026,
        mean_score=65,
    )
    finished = MediaItem(
        id=3,
        title_romaji="Second Source Candidate",
        title_english="Second Source Candidate",
        status="FINISHED",
        season_year=2010,
        mean_score=95,
    )

    result = apply_lane_policy([], [releasing, finished], "best_match")

    assert [item.id for item in result] == [2, 3]


def test_blocked_stem_wins_over_loved_stem_regardless_of_history_order():
    low_first = [
        hist(1, "Base", score=4),
        hist(2, "Base Season 2", score=9),
    ]
    loved_first = list(reversed(low_first))
    candidate = MediaItem(id=3, title_romaji="Base Season 3", title_english="Base Season 3")

    assert apply_lane_policy(low_first, [candidate], "plan_list")[0].franchise_policy == "blocked"
    assert apply_lane_policy(loved_first, [candidate], "plan_list")[0].franchise_policy == "blocked"


def test_blocked_stem_wins_across_candidate_english_and_romaji_titles():
    history = [
        hist(1, "Loved Base", score=9),
        hist(2, "Blocked Base", score=4),
    ]
    candidate = MediaItem(
        id=3,
        title_romaji="Blocked Base Season 2",
        title_english="Loved Base Season 2",
    )
    result = apply_lane_policy(history, [candidate], "plan_list")
    assert result[0].franchise_policy == "blocked"


def test_strict_missing_relation_gets_sequence_warning():
    result = apply_lane_policy(
        [hist(1, "Base", score=9)],
        [cand(2, "Base 2", relation_type="PREQUEL", related_id=99, status="FINISHED")],
        "plan_list",
    )
    assert "Sequencing check" in result[0].sequence_warning


def test_strict_relation_in_completed_history_has_no_sequence_warning():
    candidate = cand(2, "Base 2", relation_type="PREQUEL", related_id=1)
    result = apply_lane_policy(
        [hist(1, "Base", score=9, status="COMPLETED")],
        [candidate],
        "best_match",
    )
    assert result[0].sequence_warning == ""


def test_current_history_with_score_does_not_satisfy_strict_prerequisite():
    history = [hist(1, "Base", score=8, status="CURRENT")]

    plan_result = apply_lane_policy(
        history,
        [
            cand(
                2,
                "Base 2",
                relation_type="PREQUEL",
                related_id=1,
                relation_status="FINISHED",
            )
        ],
        "plan_list",
    )
    assert "Sequencing check" in plan_result[0].sequence_warning

    best_match_result = apply_lane_policy(
        history,
        [
            cand(
                2,
                "Base 2",
                relation_type="PREQUEL",
                related_id=1,
                relation_status="FINISHED",
            )
        ],
        "best_match",
    )
    assert best_match_result == []

    discover_result = apply_lane_policy(
        history,
        [
            cand(
                2,
                "Base 2",
                relation_type="PREQUEL",
                related_id=1,
                relation_status="FINISHED",
            )
        ],
        "discover",
    )
    assert "Sequencing check" in discover_result[0].sequence_warning


def test_loose_order_relation_suppresses_sequence_warning():
    candidate = cand(2, "Base OVA", relation_type="SIDE_STORY", related_id=1)
    result = apply_lane_policy([hist(1, "Base", score=9)], [candidate], "best_match")
    assert result[0].loose_order is True
    assert result[0].sequence_warning == ""


def test_loose_relation_does_not_suppress_unfinished_strict_sequence_warning():
    candidate = cand(2, "Base Side Story", relation_type="SIDE_STORY", related_id=1)
    candidate.relations.append(
        MediaRelation(relation_type="PREQUEL", media_id=99, title_romaji="Missing Base")
    )
    result = apply_lane_policy([hist(1, "Base", score=9)], [candidate], "discover")
    assert result[0].loose_order is True
    assert "Sequencing check" in result[0].sequence_warning
