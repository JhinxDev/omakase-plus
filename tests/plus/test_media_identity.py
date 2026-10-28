from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from omakase.plus import direct
from omakase.plus.identity import (
    identity_rejection_reason,
    normalize_release_identity,
    release_season_markers,
)
from omakase.plus.media_types import MediaTarget


@pytest.mark.parametrize(
    "title",
    [
        "[Late] Bleach TYBW41 [Web,1080p]",
        "Bleach Thousand-Year Blood War Cour 4 Episode 2 | TYBW42",
        "Bleach Sennen Kessen-hen Kashin-tan - 07 [1080p]",
        "Bleach Thousand-Year Blood War - The Calamity [08]",
    ],
)
def test_verified_bleach_cour_identity_survives_storage_season_difference(title):
    assert (
        identity_rejection_reason(
            title,
            target_title="BLEACH: Thousand-Year Blood War - The Calamity",
            target_aliases=(),
            season=2,
            anilist_id=185874,
        )
        == ""
    )


@pytest.mark.parametrize(
    "title",
    [
        "Bleach TYBW40",
        "Bleach TYBW51",
        "Bleach Cour 3 Episode 7",
        "Bleach TYBW41 Season 1",
        "Another Anime TYBW41",
        "Bleach TYBW",
    ],
)
def test_canonical_bleach_identity_does_not_relax_other_cours_or_ambiguous_titles(title):
    assert identity_rejection_reason(
        title,
        target_title="BLEACH: Thousand-Year Blood War - The Calamity",
        target_aliases=(),
        season=2,
        anilist_id=185874,
    )


def test_canonical_slime_absolute_episode_requires_exact_target():
    title = "Tensei Shitara Slime Datta Ken Episode 76 [1080p]"
    kwargs = dict(
        target_title="That Time I Got Reincarnated as a Slime Season 4", target_aliases=(), season=4
    )
    assert identity_rejection_reason(title, anilist_id=182205, **kwargs) == ""
    assert identity_rejection_reason(title, anilist_id=999999, **kwargs)


def _media(
    anilist_id: int,
    english: str,
    *,
    romaji: str | None = None,
    native: str | None = None,
    synonyms: list[str] | None = None,
    status: str = "FINISHED",
    episodes: int | None = 12,
    next_airing_episode: dict[str, int] | None = None,
) -> dict:
    return {
        "id": anilist_id,
        "title": {
            "english": english,
            "romaji": romaji or english,
            "native": native or english,
        },
        "synonyms": synonyms or [],
        "format": "TV",
        "status": status,
        "episodes": episodes,
        "season": "SUMMER",
        "seasonYear": 2026,
        "genres": ["Adventure", "Drama", "Fantasy"],
        "nextAiringEpisode": next_airing_episode,
    }


def test_target_includes_airing_and_library_metadata(monkeypatch):
    captured_query = ""

    def fake_post(query: str, _variables: dict) -> dict:
        nonlocal captured_query
        captured_query = query
        return {
            "data": {
                "Media": _media(
                    178789,
                    "Mushoku Tensei: Jobless Reincarnation Season 3",
                    romaji="Mushoku Tensei III",
                    native="無職転生Ⅲ",
                    synonyms=["Jobless Reincarnation 3rd Season"],
                    status="RELEASING",
                    episodes=14,
                    next_airing_episode={"episode": 4, "airingAt": 1783900000},
                )
            }
        }

    monkeypatch.setattr(direct, "_post", fake_post)

    target = direct.resolve_direct_request("178789", "Season 3")

    assert isinstance(target, MediaTarget)
    assert target.season_number == 3
    assert target.aired_episodes == 3
    assert target.genres == ("Adventure", "Drama", "Fantasy")
    assert target.next_airing_at == 1783900000
    assert "Jobless Reincarnation 3rd Season" in target.search_titles
    assert isinstance(target.search_titles, tuple)
    assert "synonyms" in captured_query
    assert "nextAiringEpisode { episode airingAt }" in captured_query


def test_media_target_is_immutable(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {"data": {"Media": _media(21, "Cowboy Bebop", episodes=26)}},
    )
    target = direct.resolve_direct_request("21")

    with pytest.raises(FrozenInstanceError):
        target.title = "Changed"  # type: ignore[misc]


def test_finished_target_counts_declared_episodes_as_aired(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {"data": {"Media": _media(21, "Cowboy Bebop", episodes=26)}},
    )

    target = direct.resolve_direct_request("21")

    assert target.aired_episodes == 26
    assert target.season_number == 1


def test_final_season_derives_number_from_rich_anilist_prequel_identity(monkeypatch):
    media = _media(
        182896,
        "My Hero Academia FINAL SEASON",
        romaji="Boku no Hero Academia FINAL SEASON",
        episodes=11,
    )
    media["relations"] = {
        "edges": [
            {
                "relationType": "PREQUEL",
                "node": {
                    "id": 163139,
                    "format": "TV",
                    "title": {
                        "english": "My Hero Academia Season 7",
                        "romaji": "Boku no Hero Academia 7th Season",
                        "native": "僕のヒーローアカデミア 7th Season",
                    },
                },
            }
        ]
    }
    captured_query = ""

    def fake_post(query: str, _variables: dict) -> dict:
        nonlocal captured_query
        captured_query = query
        return {"data": {"Media": media}}

    monkeypatch.setattr(direct, "_post", fake_post)

    target = direct.resolve_direct_request("182896")

    assert target.season_number == 8
    assert "relations { edges { relationType node" in captured_query


def test_empty_search_results_raise_value_error_not_index_error(monkeypatch):
    monkeypatch.setattr(direct, "_post", lambda *_: {"data": {"Page": {"media": []}}})

    with pytest.raises(ValueError, match="No AniList anime found"):
        direct.resolve_direct_request("not a real anime")


def test_search_returns_at_most_five_targets(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {
            "data": {"Page": {"media": [_media(index, f"Hero {index}") for index in range(1, 7)]}}
        },
    )

    options = direct.search_direct_requests("Hero")

    assert isinstance(options, tuple)
    assert len(options) == 5
    assert all(isinstance(option, MediaTarget) for option in options)


def test_ambiguous_search_raises_with_immutable_options(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {
            "data": {
                "Page": {
                    "media": [
                        _media(1, "Hero Academy"),
                        _media(2, "Hero Chronicle"),
                    ]
                }
            }
        },
    )

    with pytest.raises(direct.AmbiguousMediaError) as raised:
        direct.resolve_direct_request("Hero")

    assert isinstance(raised.value.options, tuple)
    assert [option.anilist_id for option in raised.value.options] == [1, 2]


def test_exact_alias_resolves_one_of_multiple_options(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {
            "data": {
                "Page": {
                    "media": [
                        _media(1, "Naruto Shippuden"),
                        _media(2, "Naruto"),
                        _media(3, "Boruto: Naruto Next Generations"),
                    ]
                }
            }
        },
    )

    assert direct.resolve_direct_request("naruto").anilist_id == 2


def test_season_hint_selects_the_matching_exact_title(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {
            "data": {
                "Page": {
                    "media": [
                        _media(100, "Golden Kamuy"),
                        _media(
                            300,
                            "Golden Kamuy Season 3",
                            romaji="Golden Kamuy 3rd Season",
                        ),
                    ]
                }
            }
        },
    )

    target = direct.resolve_direct_request("Golden Kamuy", season="3")

    assert target.anilist_id == 300
    assert target.season_number == 3


def test_single_wrong_season_search_result_is_rejected(monkeypatch):
    monkeypatch.setattr(
        direct,
        "_post",
        lambda *_: {"data": {"Page": {"media": [_media(100, "Golden Kamuy")]}}},
    )

    with pytest.raises(ValueError, match="with that season"):
        direct.resolve_direct_request("Golden Kamuy", season="3")


SLIME_4_ALIASES = (
    "That Time I Got Reincarnated as a Slime Season 4",
    "Tensei Shitara Slime Datta Ken 4th Season",
    "Tensura 4",
)


def _slime_reason(title: str) -> str:
    return identity_rejection_reason(
        title,
        target_title=SLIME_4_ALIASES[0],
        target_aliases=SLIME_4_ALIASES[1:],
        season=4,
        anilist_id=182205,
    )


@pytest.mark.parametrize(
    "title",
    [
        "[SubsPlease] Tensura Nikki - Tensei Shitara Slime Datta Ken - 01 (1080p)",
        "[SubsPlease] Tensura Nikki - Tensei Shitara Slime Datta Ken - 07 (1080p)",
    ],
)
def test_rejects_spin_off_returned_by_short_alias(title):
    assert _slime_reason(title) == "AMBIGUOUS_TARGET_IDENTITY"


def test_rejects_conflicting_season_claims():
    title = (
        "That Time I Got Reincarnated as a Slime S04E03 [1080p] "
        "(Tensei Shitara Slime Datta Ken 3rd Season)"
    )

    assert _slime_reason(title) == "CONFLICTING_SEASON"


@pytest.mark.parametrize(
    "title",
    [
        "That Time I Got Reincarnated as a Slime S04E16 [1080p] "
        "(Tensei Shitara Slime Datta Ken 4th Season)",
        "[Asakura] Tensei Shitara Slime Datta Ken 4th Season - 04 [1080p] "
        "| That Time I Got Reincarnated as a Slime Season 4 | Episode 76",
    ],
)
def test_accepts_strong_target_and_season_identity(title):
    assert _slime_reason(title) == ""


@pytest.mark.parametrize("episode", range(1, 25))
def test_ordinal_season_does_not_treat_local_episode_as_another_season(episode):
    title = f"Tensei Shitara Slime Datta Ken 4th Season - {episode:02} [1080p]"

    assert release_season_markers(title) == frozenset({4})
    assert _slime_reason(title) == ""


def test_accepts_season_one_alias_without_explicit_marker():
    assert (
        identity_rejection_reason(
            "[Trix] Heavenly Delusion - 01 [1080p]",
            target_title="Tengoku Daimakyo",
            target_aliases=("Heavenly Delusion",),
            season=1,
            anilist_id=155783,
        )
        == ""
    )


@pytest.mark.parametrize(
    ("source", "target", "aliases", "season", "media_format"),
    [
        (
            "[Judas] Boku no Hero Academia (My Hero Academia) (Season 08) [1080p][Dual-Audio]",
            "My Hero Academia FINAL SEASON",
            ("Boku no Hero Academia 8",),
            8,
            "TV",
        ),
        (
            "Code.Geass.Akito.the.Exiled.1.The.Wyvern.Arrives.2012.1080p",
            "Code Geass: Akito the Exiled - The Wyvern Arrives",
            (),
            1,
            "MOVIE",
        ),
    ],
)
def test_accepts_release_counters_between_ordered_identity_tokens(
    source, target, aliases, season, media_format
):
    assert (
        identity_rejection_reason(
            source,
            target_title=target,
            target_aliases=aliases,
            season=season,
            media_format=media_format,
        )
        == ""
    )


def test_normalization_unifies_common_season_spellings():
    values = {
        normalize_release_identity("Show S04E03"),
        normalize_release_identity("Show Season 4 Episode 3"),
    }

    assert values == {"show season 4 episode 3"}
    assert release_season_markers("Show S04E03 (Other 3rd Season)") == frozenset({3, 4})
