from __future__ import annotations

import pytest

from omakase.plus.coverage import parse_episode_coverage, select_episode_cover
from omakase.plus.media_types import MediaCandidate


def _candidate(
    torrent_hash: str,
    episodes: set[int],
    *,
    score: int,
    seeders: int = 10,
    size_bytes: int = 1024,
) -> MediaCandidate:
    return MediaCandidate(
        torrent_hash=torrent_hash,
        source_title=torrent_hash,
        magnet=f"magnet:?xt=urn:btih:{torrent_hash}",
        seeders=seeders,
        size_bytes=size_bytes,
        quality="1080p",
        score=score,
        coverage=frozenset(episodes),
    )


@pytest.mark.parametrize(
    ("title", "season", "expected", "episodes"),
    [
        ("Show - S03E04 [1080p]", 3, 14, {4}),
        ("Show S03E01-S03E03 [1080p]", 3, 14, {1, 2, 3}),
        ("Show S03E01-E03 [1080p]", 3, 14, {1, 2, 3}),
        ("Show Season 3 (01-12) [Batch]", 3, 14, set(range(1, 13))),
        ("Show S3 - 04 [1080p]", 3, 14, {4}),
    ],
)
def test_parse_episode_coverage(title, season, expected, episodes):
    parsed = parse_episode_coverage(title, season, expected=expected)

    assert parsed.episodes == frozenset(episodes)


def test_my_hero_absolute_numbering_requires_exact_identity_and_override():
    parsed = parse_episode_coverage(
        "Boku no Hero Academia - 64 [1080p]",
        4,
        expected=25,
        anilist_id=104276,
    )

    assert parsed.episodes == frozenset({1})
    assert parsed.numbering_override == "mha-s4-absolute-v1"
    assert (
        parse_episode_coverage("Another Hero - 64 [1080p]", 4, expected=25, anilist_id=999).episodes
        == frozenset()
    )


def test_my_hero_absolute_range_is_clamped_to_the_frozen_override():
    parsed = parse_episode_coverage(
        "Boku no Hero Academia Season 4 - 63-89 [Batch]",
        4,
        expected=25,
        anilist_id=104276,
    )

    assert parsed.episodes == frozenset(range(1, 26))
    assert parsed.numbering_override == "mha-s4-absolute-v1"


@pytest.mark.parametrize(
    "title",
    [
        "Show S02E01 [1080p]",
        "Show S03 NCOP [1080p]",
        "Show S03 NCED [1080p]",
        "Show S03 PV [1080p]",
        "Show S03E01 sample.mkv",
    ],
)
def test_wrong_season_and_non_episode_extras_do_not_count(title):
    assert parse_episode_coverage(title, 3, expected=12).episodes == frozenset()


def test_specials_are_separate_from_numbered_coverage():
    parsed = parse_episode_coverage("Show S00E02 Special [1080p]", 3, expected=12)

    assert parsed.episodes == frozenset()
    assert parsed.specials == frozenset({2})


def test_ranges_are_capped_at_the_expected_episode_count():
    parsed = parse_episode_coverage("Show Season 3 (01-99) [Batch]", 3, expected=12)

    assert parsed.episodes == frozenset(range(1, 13))


@pytest.mark.parametrize(
    "title",
    ["Show Season 3 Complete [1080p]", "Show S03 [Batch] [1080p]"],
)
def test_complete_season_markers_cover_the_declared_episode_count(title):
    assert parse_episode_coverage(title, 3, expected=12).episodes == frozenset(range(1, 13))


@pytest.mark.parametrize(
    "title",
    ["Show - 04 [1080p]", "Show Episode 04 [1080p]", "Show Ep.04 [1080p]"],
)
def test_plain_numbered_episode_releases_are_parsed(title):
    assert parse_episode_coverage(title, 3, expected=12).episodes == frozenset({4})


def test_local_episode_precedes_absolute_episode_annotation():
    parsed = parse_episode_coverage(
        "Tensei Shitara Slime Datta Ken 4th Season - 04 [1080p] | Episode 76",
        4,
        expected=None,
    )

    assert parsed.episodes == frozenset({4})


def test_fractional_recap_does_not_claim_the_regular_episode():
    assert (
        parse_episode_coverage(
            "Tensei Shitara Slime Datta Ken - S03E17.5v3.mkv",
            3,
            expected=24,
        ).episodes
        == frozenset()
    )


def test_select_minimal_non_overlapping_cover():
    chosen = select_episode_cover(
        required={1, 2, 3},
        candidates=[
            _candidate("BATCH", {1, 2}, score=100),
            _candidate("EP2", {2}, score=500),
            _candidate("EP3", {3}, score=400),
        ],
    )

    assert [item.torrent_hash for item in chosen] == ["BATCH", "EP3"]


def test_cover_selection_uses_stable_quality_seed_size_and_hash_ties():
    chosen = select_episode_cover(
        required={1},
        candidates=[
            _candidate("AAA", {1}, score=100, seeders=20, size_bytes=2000),
            _candidate("BBB", {1}, score=100, seeders=20, size_bytes=1000),
            _candidate("CCC", {1}, score=90, seeders=100, size_bytes=1),
        ],
    )

    assert tuple(item.torrent_hash for item in chosen) == ("BBB",)


def test_cover_selection_does_not_mutate_inputs():
    required = {1, 2}
    candidates = [_candidate("AAA", {1, 2}, score=100)]

    chosen = select_episode_cover(required, candidates)

    assert required == {1, 2}
    assert len(candidates) == 1
    assert isinstance(chosen, tuple)


@pytest.mark.parametrize(
    ("title", "episodes"),
    [
        ("[Late] Bleach TYBW 41 [Web, 1080p, x264, EAC3, ASS]", {1}),
        ("[Late] Bleach TYBW41 [1080p].mkv", {1}),
        ("[Later] Bleach TYBW 42 [1080p] | Thousand-Year Blood War Cour 4 Episode 2", {2}),
        ("Bleach TYBW - 41-50 [Batch]", set(range(1, 11))),
        ("Bleach TYBW 49 [1080p]", {9}),
        ("BLEACH Sennen Kessen-hen - Kashin-tan [07][1080P]", {7}),
        ("Bleach Thousand-Year Blood War The Calamity [08][1080P]", {8}),
        ("Bleach Thousand-Year Blood War Cour 4 Episode 1.mkv", {1}),
        ("Bleach Thousand-Year Blood War S01E44 [1080p]", {4}),
        ("Bleach S17E44 Thousand-Year Blood War [1080p]", {4}),
        ("Bleach Thousand-Year Blood War S01E41-E44 [Batch]", {1, 2, 3, 4}),
        ("[SubsPlease] Bleach Sennen Kessen-hen (41-44) (1080p) [Batch]", {1, 2, 3, 4}),
        ("Bleach Thousand-Year Blood War - E44v2 [Eng-Subs]", {4}),
        ("Bleach Thousand-Year Blood War S01E44v2 [1080p].mkv", {4}),
    ],
)
def test_bleach_cour4_canonical_local_and_absolute_contract(title, episodes):
    parsed = parse_episode_coverage(title, 2, expected=10, anilist_id=185874)
    assert parsed.episodes == frozenset(episodes)
    assert parsed.numbering_override == "bleach-tybw-cour4-absolute-v1"


@pytest.mark.parametrize(
    "title",
    [
        "Bleach TYBW 40 [1080p]",
        "Bleach TYBW 51 [1080p]",
        "Bleach TYBW - 40-42 [Batch]",
        "Bleach TYBW - 49-51 [Batch]",
        "Bleach TYBW 41 Cour 3 Episode 1",
        "Bleach TYBW 41 Season 3 Episode 1",
        "Bleach TYBW 41 Cour 4 Episode 2",
        "Bleach TYBW Episode 1 [1080p]",
        "Bleach TYBW [07][1080p]",
        "Different Show Cour 4 Episode 1",
        "Bleach TYBW 41 preview [1080p]",
        "Bleach TYBW Episode 41.5 [1080p]",
        "Bleach TYBW 41.5 [1080p]",
        "Bleach TYBW 41 OVA [1080p]",
        "Bleach Thousand-Year Blood War S01E04 [1080p]",
        "Bleach Thousand-Year Blood War S17E04 [1080p]",
        "Bleach Thousand-Year Blood War S03E44 [1080p]",
        "Bleach Thousand-Year Blood War S01E44 S17E45 [1080p]",
        "Bleach Sennen Kessen-hen Soukoku-tan - 44 [1080p]",
        "Bleach Thousand-Year Blood War - E44.5 [1080p]",
        "Bleach Sennen Kessen-hen (40-44) (1080p) [Batch]",
        "Bleach Thousand-Year Blood War E44-45.5 [1080p].mkv",
        "Bleach Thousand-Year Blood War E44-9999 [1080p].mkv",
        "Bleach Thousand-Year Blood War S01E41-E44.5 [1080p].mkv",
        "Bleach Thousand-Year Blood War S17E44E45 [1080p].mkv",
        "Bleach TYBW 44 E44.5 [1080p].mkv",
        "Bleach Thousand-Year Blood War S01E44v2E45 [1080p].mkv",
        "Bleach Thousand-Year Blood War S01E44v2-9999 [1080p].mkv",
        "Bleach Thousand-Year Blood War S01E44v2.5 [1080p].mkv",
    ],
)
def test_bleach_cour4_rejects_wrong_cour_out_of_range_and_ambiguous_claims(title):
    assert not parse_episode_coverage(title, 2, expected=10, anilist_id=185874).episodes


def test_canonical_contract_requires_id_and_supported_target_season():
    title = "Bleach TYBW 41 [1080p]"
    assert not parse_episode_coverage(title, 2, expected=10, anilist_id=999).episodes
    assert not parse_episode_coverage(title, 3, expected=10, anilist_id=185874).episodes
    assert not parse_episode_coverage("Bleach TYBW 42", 2, expected=1, anilist_id=185874).episodes


@pytest.mark.parametrize(
    ("title", "episodes"),
    [
        ("Tensei Shitara Slime Datta Ken - 73 [1080p]", {1}),
        ("That Time I Got Reincarnated as a Slime Episode 96 [1080p]", {24}),
        ("Tensei Shitara Slime Datta Ken 4th Season - 04 [1080p] | Episode 76", {4}),
        ("That Time I Got Reincarnated as a Slime S04E04.mkv", {4}),
        ("Tensei Shitara Slime Datta Ken Season 4 - 73-96 [Batch]", set(range(1, 25))),
    ],
)
def test_slime_s4_canonical_absolute_and_local_annotations_agree(title, episodes):
    parsed = parse_episode_coverage(title, 4, expected=24, anilist_id=182205)
    assert parsed.episodes == frozenset(episodes)
    assert parsed.numbering_override == "slime-s4-absolute-v1"


@pytest.mark.parametrize(
    "title",
    [
        "Tensei Shitara Slime Datta Ken - 72 [1080p]",
        "Tensei Shitara Slime Datta Ken - 97 [1080p]",
        "Tensei Shitara Slime Datta Ken Season 3 - 76 [1080p]",
        "Tensei Shitara Slime Datta Ken - 04 [1080p]",
        "Tensei Shitara Slime Datta Ken Season 4 - 04 | Episode 77",
        "Slime Diaries Season 4 - 76 [1080p]",
    ],
)
def test_slime_s4_rejects_wrong_identity_season_or_conflicting_claims(title):
    assert not parse_episode_coverage(title, 4, expected=24, anilist_id=182205).episodes


@pytest.mark.parametrize(
    "title",
    [
        "Bleach TYBW Cour 4 Episode 1-11",
        "Bleach TYBW Calamity [01-11]",
        "Bleach TYBW Cour 4 - 1-11 [Batch]",
    ],
)
def test_canonical_local_ranges_cannot_hide_out_of_contract_endpoints(title):
    assert not parse_episode_coverage(title, 2, expected=10, anilist_id=185874).episodes


@pytest.mark.parametrize("episode", range(1, 25))
@pytest.mark.parametrize("absolute_annotation", [False, True])
def test_slime_ordinal_season_inventory_keeps_local_episode(episode, absolute_annotation):
    title = f"Tensei Shitara Slime Datta Ken 4th Season - {episode:02} [1080p]"
    if absolute_annotation:
        title += f" | Episode {episode + 72}"
    parsed = parse_episode_coverage(title, 4, expected=24, anilist_id=182205)
    assert parsed.episodes == frozenset({episode})
    assert parsed.numbering_override == "slime-s4-absolute-v1"


@pytest.mark.parametrize(
    "title",
    [
        "Tensei Shitara Slime Datta Ken 5th Season - 05 [1080p]",
        "Tensei Shitara Slime Datta Ken 4th Season - 05 | Season 5",
        "Tensei Shitara Slime Datta Ken 4th Season - 05 | Episode 78",
        "Bleach TYBW 4th Season - 41 | Cour 3",
    ],
)
def test_ordinal_overlap_fix_preserves_actual_wrong_season_and_claim_rejections(title):
    anilist_id = 185874 if title.startswith("Bleach") else 182205
    assert not parse_episode_coverage(title, 4, expected=24, anilist_id=anilist_id).episodes


@pytest.mark.parametrize(
    "title",
    [
        "Show S01E02-E03",
        "Show S01E01 EP02",
        "Show - 02-03",
        "Show EP02~03",
        "Show S01E03",
        "Show S01E01E02",
    ],
)
def test_strict_single_file_coverage_rejects_unclipped_ranges_and_conflicting_annotations(title):
    from omakase.plus.coverage import parse_single_file_coverage

    assert not parse_single_file_coverage(title, 1, expected=2).episodes


def test_strict_single_file_coverage_preserves_consistent_legacy_and_canonical_claims():
    from omakase.plus.coverage import parse_single_file_coverage

    assert parse_single_file_coverage("Show S01E02 EP02", 1, expected=2).episodes == frozenset({2})
    assert parse_single_file_coverage(
        "Bleach TYBW41 Cour4 Episode1", 2, expected=10, anilist_id=185874
    ).episodes == frozenset({1})
    assert parse_single_file_coverage(
        "Tensei Shitara Slime Datta Ken 4th Season - 04 Episode76",
        4,
        expected=24,
        anilist_id=182205,
    ).episodes == frozenset({4})
