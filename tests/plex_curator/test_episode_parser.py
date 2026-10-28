from __future__ import annotations

import sys
from pathlib import Path

import pytest

CURATOR_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from episode_parser import EpisodeParseError, EpisodeRef, parse_file_episode  # noqa: E402


def test_absolute_my_hero_number_maps_to_local_season_episode():
    name = "Boku no Hero - 64 (My Hero Academia S4 - 01).mkv"
    assert parse_file_episode(name, season=4, expected=25) == EpisodeRef(4, 1, 1)


def test_absolute_my_hero_range_requires_the_versioned_override():
    assert parse_file_episode(
        "Boku no Hero - 64-66.mkv",
        season=4,
        expected=25,
        numbering_override="mha-s4-absolute-v1",
    ) == EpisodeRef(4, 1, 3)

    with pytest.raises(EpisodeParseError):
        parse_file_episode("Boku no Hero - 64-66.mkv", season=4, expected=25)


def test_explicit_range_and_specials_are_distinct():
    assert parse_file_episode("Show.S02E03-E05.mkv", season=2, expected=12) == EpisodeRef(2, 3, 5)
    assert parse_file_episode("Show SP02.mkv", season=2, expected=12) == EpisodeRef(
        0, 2, 2, special=True
    )


def test_simple_release_episode_and_range_use_manifest_bounds():
    assert parse_file_episode("[Group] Show - 03 [1080p].mkv", season=1, expected=12) == EpisodeRef(
        1, 3, 3
    )
    assert parse_file_episode(
        "[Group] Show - 03-05 [1080p].mkv", season=1, expected=12
    ) == EpisodeRef(1, 3, 5)
    assert parse_file_episode(
        "[Group] Show - 12 END [1080p].mkv", season=1, expected=12
    ) == EpisodeRef(1, 12, 12)


def test_simple_parser_does_not_treat_resolution_or_year_as_episode():
    with pytest.raises(EpisodeParseError):
        parse_file_episode("Show (2014) [1080p].mkv", season=1, expected=12)


def test_fractional_recap_does_not_collide_with_regular_episode():
    with pytest.raises(EpisodeParseError, match="fractional episode"):
        parse_file_episode(
            "Tensei Shitara Slime Datta Ken - S03E17.5v3.mkv",
            season=3,
            expected=24,
        )


@pytest.mark.parametrize(
    ("filename", "season", "expected", "anilist_id", "tag", "episodes"),
    [
        (
            "Bleach TYBW 41 Cour 4 Episode 1.mkv",
            2,
            10,
            185874,
            "bleach-tybw-cour4-absolute-v1",
            {1},
        ),
        (
            "Bleach TYBW - 41-50 [Batch].mkv",
            2,
            10,
            185874,
            "bleach-tybw-cour4-absolute-v1",
            set(range(1, 11)),
        ),
        (
            "BLEACH Sennen Kessen-hen Kashin-tan [08][1080P].mkv",
            2,
            10,
            185874,
            "bleach-tybw-cour4-absolute-v1",
            {8},
        ),
        (
            "Tensei Shitara Slime Datta Ken - 76 [1080p].mkv",
            4,
            24,
            182205,
            "slime-s4-absolute-v1",
            {4},
        ),
        (
            "Tensei Shitara Slime Datta Ken 4th Season - 04 [1080p] | Episode 76.mkv",
            4,
            24,
            182205,
            "slime-s4-absolute-v1",
            {4},
        ),
        (
            "That Time I Got Reincarnated as a Slime S04E01-S04E03.mkv",
            4,
            24,
            182205,
            "slime-s4-absolute-v1",
            {1, 2, 3},
        ),
    ],
)
def test_canonical_curator_contract_matches_plus_inventory_parser(
    filename, season, expected, anilist_id, tag, episodes
):
    from omakase.plus.coverage import parse_episode_coverage

    plus = parse_episode_coverage(filename, season, expected=expected, anilist_id=anilist_id)
    curator = parse_file_episode(
        filename, season=season, expected=expected, anilist_id=anilist_id, numbering_override=tag
    )
    assert curator.episodes == plus.episodes == frozenset(episodes)
    assert curator.season == season
    assert plus.numbering_override == tag


@pytest.mark.parametrize(
    ("filename", "season", "expected", "anilist_id", "tag"),
    [
        ("Bleach TYBW 40.mkv", 2, 10, 185874, "bleach-tybw-cour4-absolute-v1"),
        ("Bleach TYBW 51.mkv", 2, 10, 185874, "bleach-tybw-cour4-absolute-v1"),
        ("Bleach TYBW 41 Cour 3 Episode 1.mkv", 2, 10, 185874, "bleach-tybw-cour4-absolute-v1"),
        ("Bleach TYBW 41 Cour 4 Episode 2.mkv", 2, 10, 185874, "bleach-tybw-cour4-absolute-v1"),
        ("Bleach TYBW 41.5.mkv", 2, 10, 185874, "bleach-tybw-cour4-absolute-v1"),
        ("Bleach TYBW 41 sample.mkv", 2, 10, 185874, "bleach-tybw-cour4-absolute-v1"),
        ("Tensei Shitara Slime Datta Ken Season 3 - 76.mkv", 4, 24, 182205, "slime-s4-absolute-v1"),
        ("Tensei Shitara Slime Datta Ken - 97.mkv", 4, 24, 182205, "slime-s4-absolute-v1"),
        ("Slime Diaries Season 4 - 76.mkv", 4, 24, 182205, "slime-s4-absolute-v1"),
    ],
)
def test_canonical_curator_and_plus_both_fail_closed(filename, season, expected, anilist_id, tag):
    from omakase.plus.coverage import parse_episode_coverage

    assert not parse_episode_coverage(
        filename, season, expected=expected, anilist_id=anilist_id
    ).episodes
    with pytest.raises(EpisodeParseError):
        parse_file_episode(
            filename,
            season=season,
            expected=expected,
            anilist_id=anilist_id,
            numbering_override=tag,
        )


@pytest.mark.parametrize(
    ("anilist_id", "tag"),
    [
        (None, "bleach-tybw-cour4-absolute-v1"),
        (999, "bleach-tybw-cour4-absolute-v1"),
        (185874, None),
        (185874, "slime-s4-absolute-v1"),
    ],
)
def test_canonical_curator_requires_exact_manifest_id_and_tag(anilist_id, tag):
    with pytest.raises(EpisodeParseError, match="matching manifest ID"):
        parse_file_episode(
            "Bleach TYBW 41.mkv",
            season=2,
            expected=10,
            anilist_id=anilist_id,
            numbering_override=tag,
        )


@pytest.mark.parametrize(
    "filename",
    [
        "Bleach TYBW Cour 4 Episode 1-11.mkv",
        "Bleach TYBW Calamity [01-11].mkv",
        "Bleach TYBW Cour 4 - 1-11 [Batch].mkv",
    ],
)
def test_curator_canonical_local_ranges_reject_out_of_contract_endpoints(filename):
    with pytest.raises(EpisodeParseError):
        parse_file_episode(
            filename,
            season=2,
            expected=10,
            anilist_id=185874,
            numbering_override="bleach-tybw-cour4-absolute-v1",
        )


@pytest.mark.parametrize("episode", range(1, 25))
def test_curator_ordinal_slime_filename_matches_plus_inventory(episode):
    from omakase.plus.coverage import parse_episode_coverage

    filename = f"Tensei Shitara Slime Datta Ken 4th Season - {episode:02} [1080p].mkv"
    plus = parse_episode_coverage(filename, 4, expected=24, anilist_id=182205)
    curator = parse_file_episode(
        filename,
        season=4,
        expected=24,
        anilist_id=182205,
        numbering_override="slime-s4-absolute-v1",
    )
    assert curator.episodes == plus.episodes == frozenset({episode})


@pytest.mark.parametrize(
    "filename",
    [
        "Tensei Shitara Slime Datta Ken 5th Season - 05.mkv",
        "Tensei Shitara Slime Datta Ken 4th Season - 05 | Season 5.mkv",
        "Tensei Shitara Slime Datta Ken 4th Season - 05 | Episode 78.mkv",
    ],
)
def test_curator_ordinal_contract_preserves_wrong_season_and_absolute_conflict(filename):
    with pytest.raises(EpisodeParseError):
        parse_file_episode(
            filename,
            season=4,
            expected=24,
            anilist_id=182205,
            numbering_override="slime-s4-absolute-v1",
        )


@pytest.mark.parametrize(
    "filename,start,end",
    [
        ("Bleach Thousand-Year Blood War S01E44.mkv", 4, 4),
        ("Bleach S17E44 Thousand-Year Blood War.mkv", 4, 4),
        ("Bleach Thousand-Year Blood War S01E41-E44.mkv", 1, 4),
        ("Bleach Sennen Kessen-hen (41-44) [Batch].mkv", 1, 4),
        ("Bleach Thousand-Year Blood War - E44v2.mkv", 4, 4),
        ("Bleach Thousand-Year Blood War S01E44v2.mkv", 4, 4),
    ],
)
def test_curator_accepts_source_verified_bleach_global_conventions(filename, start, end):
    parsed = parse_file_episode(
        filename,
        season=2,
        expected=10,
        anilist_id=185874,
        numbering_override="bleach-tybw-cour4-absolute-v1",
    )
    assert (parsed.start, parsed.end) == (start, end)


@pytest.mark.parametrize(
    "filename",
    [
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
def test_bleach_global_tokens_must_be_consumed_completely(filename):
    from omakase.plus.coverage import parse_single_file_coverage

    assert not parse_single_file_coverage(
        filename, season=2, expected=10, anilist_id=185874
    ).episodes
    with pytest.raises(EpisodeParseError):
        parse_file_episode(
            filename,
            season=2,
            expected=10,
            anilist_id=185874,
            numbering_override="bleach-tybw-cour4-absolute-v1",
        )
