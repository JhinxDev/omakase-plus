from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone

import pytest

from omakase.plus import catalog
from omakase.plus.acquisition_policy import AcquisitionPolicy
from omakase.plus.media_types import MediaCandidate
from omakase.plus.nyaa import NyaaTorrent


def _torrent(
    title: str,
    torrent_hash: str,
    *,
    seeders: int = 10,
    size_bytes: int = 1024,
    trusted: bool = False,
    batch: bool = False,
) -> NyaaTorrent:
    return NyaaTorrent(
        title=title,
        magnet=f"magnet:?xt=urn:btih:{torrent_hash}&dn=test",
        seeders=seeders,
        leechers=0,
        size_bytes=size_bytes,
        size_display="1 KiB",
        pub_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
        is_trusted=trusted,
        is_batch=batch,
    )


def _candidate(
    title: str,
    *,
    torrent_hash: str = "A" * 40,
    seeders: int = 10,
    quality: str = "1080p",
    score: int = 100,
    coverage: frozenset[int] = frozenset({1}),
) -> MediaCandidate:
    return MediaCandidate(
        torrent_hash=torrent_hash,
        source_title=title,
        magnet=f"magnet:?xt=urn:btih:{torrent_hash}",
        seeders=seeders,
        size_bytes=1024,
        quality=quality,
        score=score,
        coverage=coverage,
    )


def test_searches_every_alias_and_deduplicates_hash(monkeypatch):
    calls: list[str] = []
    shared_hash = "a" * 40

    async def fake_search(alias: str, **_kwargs):
        calls.append(alias)
        quality = "720p" if alias == "English" else "1080p"
        return [
            _torrent(
                f"[Group] Show S03E01 [{quality}] [Multi-Subs]",
                shared_hash,
            )
        ]

    monkeypatch.setattr(catalog, "search", fake_search)

    found = asyncio.run(
        catalog.search_all_aliases(["English", "Romaji", "Native"], season=3, expected=12)
    )

    assert calls == ["English", "Romaji", "Native"]
    assert [item.torrent_hash for item in found] == [shared_hash.upper()]
    assert found[0].quality == "1080p"
    assert found[0].search_alias == "Romaji"


def test_search_skips_results_without_a_hex_btih(monkeypatch):
    async def fake_search(_alias: str, **_kwargs):
        return [_torrent("Show S01E01 [1080p]", "NOT-A-BTIH")]

    monkeypatch.setattr(catalog, "search", fake_search)

    assert asyncio.run(catalog.search_all_aliases(["Show"], season=1, expected=1)) == ()


def test_catalog_accepts_base32_btih_and_stores_canonical_hex(monkeypatch):
    raw_hash = bytes.fromhex("BC" * 20)
    base32_hash = base64.b32encode(raw_hash).decode()

    async def fake_search(_alias: str, **_kwargs):
        return [_torrent("Show S01E01 [1080p] [Multi-Subs]", base32_hash)]

    monkeypatch.setattr(catalog, "search", fake_search)

    found = asyncio.run(catalog.search_all_aliases(["Show"], season=1, expected=1))

    assert found[0].torrent_hash == "BC" * 20


@pytest.mark.parametrize(
    ("title", "season", "reason"),
    [
        ("Show S03E01 [RAW][1080p]", 3, "RAW_RELEASE"),
        ("Show S03E01 [Hardsubs][1080p]", 3, "HARDSUB_RELEASE"),
        ("Show S03E01 [480p][Multi-Subs]", 3, "LOW_RESOLUTION"),
        ("Show S02E01 [1080p][Multi-Subs]", 3, "WRONG_SEASON"),
        ("Show S03E01 [1080p][No Subs]", 3, "NO_SUBTITLES"),
        ("Show S03E01 [CAM][1080p][Subs]", 3, "CAM_SOURCE"),
    ],
)
def test_quality_profile_rejects_ineligible_candidates(title, season, reason):
    candidate = _candidate(title)

    assert not catalog.is_eligible(candidate, season=season)
    assert catalog.candidate_rejection_reason(candidate, season=season) == reason


def test_quality_profile_accepts_zero_seed_candidate_for_later_instant_cache_gate():
    candidate = _candidate("Show S03E01 [1080p][Multi-Subs]", seeders=0)

    assert catalog.is_eligible(candidate, season=3)


def test_quality_profile_accepts_raw_named_group_with_explicit_multisubs():
    candidate = _candidate("[Erai-raws] Show S03E01 [1080p CR WEB-DL][MultiSub]")

    assert catalog.is_eligible(candidate, season=3)
    assert catalog.candidate_rejection_reason(candidate, season=3) == ""


def test_hardsub_release_is_available_only_as_an_explicit_fallback():
    candidate = _candidate("Show S01E01 [1080p] [Hardsubs]")

    assert catalog.candidate_rejection_reason(candidate, season=1) == "HARDSUB_RELEASE"
    assert (
        catalog.candidate_rejection_reason(
            candidate,
            season=1,
            allow_hardsub=True,
        )
        == ""
    )


def test_quality_profile_rejects_candidate_without_parsed_coverage():
    candidate = _candidate(
        "Show [1080p][Multi-Subs]",
        coverage=frozenset(),
    )

    assert catalog.candidate_rejection_reason(candidate, season=3) == "NO_EPISODE_COVERAGE"


def test_target_identity_rejects_short_alias_spin_off():
    candidate = _candidate(
        "[SubsPlease] Tensura Nikki - Tensei Shitara Slime Datta Ken - 04 (1080p)"
    )

    assert (
        catalog.candidate_rejection_reason(
            candidate,
            season=4,
            target_title="That Time I Got Reincarnated as a Slime Season 4",
            target_aliases=("Tensei Shitara Slime Datta Ken 4th Season", "Tensura 4"),
            anilist_id=182205,
        )
        == "AMBIGUOUS_TARGET_IDENTITY"
    )


def test_target_identity_accepts_ordinal_season_without_treating_episode_as_season():
    candidate = _candidate(
        "[Asakura] Tensei Shitara Slime Datta Ken 4th Season - 05 "
        "(WEB 1080p HEVC EAC3) | That Time I Got Reincarnated as a Slime Season 4 | "
        "Episode 77"
    )

    assert (
        catalog.candidate_rejection_reason(
            candidate,
            season=4,
            target_title="That Time I Got Reincarnated as a Slime Season 4",
            target_aliases=("Tensei Shitara Slime Datta Ken 4th Season", "Tensura 4"),
            anilist_id=182205,
        )
        == ""
    )


def test_global_ranking_keeps_every_1080p_candidate_ahead_of_720p():
    candidates = [
        _candidate("720", torrent_hash="7" * 40, quality="720p", seeders=500, score=999),
        _candidate("1080", torrent_hash="1" * 40, quality="1080p", seeders=1, score=1),
    ]

    ranked = catalog.rank_candidates(candidates)

    assert [candidate.quality for candidate in ranked] == ["1080p", "720p"]


def test_default_ranking_prefers_1080p_and_quality_profile_accepts_4k():
    candidates = [
        _candidate("1080", torrent_hash="1" * 40, quality="1080p", score=999),
        _candidate("2160", torrent_hash="2" * 40, quality="2160p", score=1),
    ]

    ranked = catalog.rank_candidates(candidates)

    assert [candidate.quality for candidate in ranked] == ["1080p", "2160p"]
    assert (
        catalog.candidate_rejection_reason(
            _candidate(
                "Show S01E01 [2160p] [Multi-Subs]",
                quality="2160p",
            ),
            season=1,
        )
        == ""
    )


def test_owner_policy_changes_resolution_order_and_safety_limits():
    policy = AcquisitionPolicy(
        allowed_resolutions=("720p", "1080p"),
        minimum_seeders=3,
        preferred_seeders=10,
        max_release_size_gib=80,
    )
    candidates = [
        _candidate("1080", torrent_hash="1" * 40, quality="1080p", seeders=100),
        _candidate("720", torrent_hash="7" * 40, quality="720p", seeders=3),
    ]

    assert [item.quality for item in catalog.rank_candidates(candidates, policy=policy)] == [
        "720p",
        "1080p",
    ]
    assert (
        catalog.candidate_rejection_reason(
            _candidate("2160", quality="2160p"),
            season=1,
            policy=policy,
        )
        == "UNSUPPORTED_RESOLUTION"
    )
    assert (
        catalog.candidate_rejection_reason(
            _candidate("1080", quality="1080p", seeders=2),
            season=1,
            policy=policy,
            enforce_seeders=True,
        )
        == "LOW_AVAILABILITY"
    )


def test_candidate_conversion_normalizes_hash_coverage_and_quality():
    candidate = catalog.candidate_from_torrent(
        _torrent(
            "[Judas] Show Season 3 Complete [1080p][HEVC][Dual Audio][Multi-Subs]",
            "b" * 40,
            seeders=25,
            trusted=True,
            batch=True,
        ),
        search_alias="Show III",
        season=3,
        expected=12,
    )

    assert candidate is not None
    assert candidate.torrent_hash == "B" * 40
    assert candidate.coverage == frozenset(range(1, 13))
    assert candidate.quality == "1080p"
    assert candidate.score > 0


def test_disabling_trusted_preference_also_disables_known_group_bonus():
    policy = AcquisitionPolicy(prefer_trusted=False)
    known_group = catalog.candidate_from_torrent(
        _torrent(
            "[SubsPlease] Show S01E01 [1080p][Multi-Subs]",
            "1" * 40,
            seeders=5,
            trusted=True,
        ),
        search_alias="Show",
        season=1,
        expected=1,
        policy=policy,
    )
    popular_release = catalog.candidate_from_torrent(
        _torrent(
            "[Other] Show S01E01 [1080p][Multi-Subs]",
            "2" * 40,
            seeders=100,
        ),
        search_alias="Show",
        season=1,
        expected=1,
        policy=policy,
    )

    assert known_group is not None
    assert popular_release is not None
    assert popular_release.score > known_group.score


def test_alias_windows_continue_after_one_provider_error(monkeypatch):
    calls: list[str] = []

    async def fake_search(alias: str, **_kwargs):
        calls.append(alias)
        if alias == "English":
            raise RuntimeError("provider unavailable")
        return [_torrent("Show S01E01 [1080p][Multi-Subs]", "C" * 40)]

    monkeypatch.setattr(catalog, "search", fake_search)

    windows = asyncio.run(catalog.search_alias_windows(["English", "Romaji"], season=1, expected=1))

    assert calls == ["English", "Romaji"]
    assert windows[0].alias == "English"
    assert windows[0].provider_complete is False
    assert windows[0].candidates == ()
    assert windows[1].provider_complete is True
    assert [candidate.torrent_hash for candidate in windows[1].candidates] == ["C" * 40]


def test_additional_catalogs_search_two_canonical_aliases_while_nyaa_searches_all(
    monkeypatch,
):
    nyaa_calls: list[str] = []
    additional_calls: list[str] = []

    async def fake_search(alias: str, **_kwargs):
        nyaa_calls.append(alias)
        return []

    class AdditionalCatalog:
        kind = "prowlarr"

        async def search(self, alias: str):
            additional_calls.append(alias)
            return []

    monkeypatch.setattr(catalog, "search", fake_search)

    windows = asyncio.run(
        catalog.search_alias_windows(
            ["English", "Romaji", "Native"],
            season=1,
            expected=12,
            additional_catalogs=(AdditionalCatalog(),),
        )
    )

    assert nyaa_calls == ["English", "Romaji", "Native"]
    assert additional_calls == ["English", "Romaji"]
    assert [receipt.catalog_kind for receipt in windows[2].source_receipts] == ["nyaa"]


def test_gap_search_recovers_older_release_and_keeps_canonical_alias(monkeypatch):
    calls = []

    async def fake_search(query):
        calls.append(query)
        if query == "Show 03":
            return [_torrent("Show S01E03 [1080p] [Multi-Subs]", "B" * 40)]
        return [_torrent("Show S01E12 [1080p] [Multi-Subs]", "A" * 40)]

    monkeypatch.setattr(catalog, "search", fake_search)
    windows = asyncio.run(
        catalog.search_alias_windows(
            ["Show", "Romaji", "Native"],
            season=1,
            expected=12,
            missing_episodes=[3, 4, 5, 6, 12, 13, 0, True],
        )
    )
    assert calls == ["Show", "Romaji", "Native", "Show 03", "Show 12", "Romaji 03", "Romaji 12"]
    assert len(windows) == 3
    assert {item.torrent_hash for item in windows[0].candidates} == {"A" * 40, "B" * 40}
    recovered = next(item for item in windows[0].candidates if item.torrent_hash == "B" * 40)
    assert recovered.coverage == frozenset({3})
    assert recovered.search_alias == "Show"
    assert len(windows[0].source_receipts) == 3


@pytest.mark.parametrize(
    "anilist_id,season,alias,episode,query_number",
    [
        (185874, 2, "Bleach TYBW", 1, "41"),
        (182205, 4, "Tensei Shitara Slime Datta Ken 4th Season", 4, "76"),
        (182205, 1, "Unrelated", 4, "04"),
    ],
)
def test_gap_query_uses_only_reviewed_canonical_numbering(
    monkeypatch, anilist_id, season, alias, episode, query_number
):
    calls = []

    async def fake_search(query):
        calls.append(query)
        return []

    monkeypatch.setattr(catalog, "search", fake_search)
    asyncio.run(
        catalog.search_alias_windows(
            [alias], season=season, expected=24, anilist_id=anilist_id, missing_episodes=[episode]
        )
    )
    gap_alias = "Bleach Sennen Kessen Hen" if anilist_id == 185874 and season in {2, 4} else alias
    assert calls == [alias, f"{gap_alias} {query_number}"]


def test_bleach_gap_query_reaches_generic_release_titles_without_more_requests(monkeypatch):
    calls = []

    async def fake_search(query):
        calls.append(query)
        if query == "Bleach Sennen Kessen Hen 44":
            return [_torrent("[DKB] Bleach - Sennen Kessen-hen - 44 [1080p][Multi-Subs]", "B" * 40)]
        return []

    monkeypatch.setattr(catalog, "search", fake_search)
    windows = asyncio.run(
        catalog.search_alias_windows(
            ["BLEACH Thousand-Year Blood War The Calamity"],
            season=2,
            expected=10,
            anilist_id=185874,
            missing_episodes=[4],
        )
    )
    assert calls == ["BLEACH Thousand-Year Blood War The Calamity", "Bleach Sennen Kessen Hen 44"]
    assert windows[0].candidates[0].coverage == frozenset({4})
    assert windows[0].candidates[0].search_alias == windows[0].alias


def test_gap_search_failure_preserves_broad_and_healthy_source_results(monkeypatch):
    nyaa_calls = []
    source_calls = []

    async def fake_search(query):
        nyaa_calls.append(query)
        if query.endswith("01"):
            raise RuntimeError("catalog unavailable")
        return [_torrent("Show S01E02 [1080p] [Multi-Subs]", "A" * 40)]

    class Source:
        kind = "prowlarr"

        async def search(self, query):
            source_calls.append(query)
            return [_torrent("Show S01E01 [1080p] [Multi-Subs]", "B" * 40)]

    monkeypatch.setattr(catalog, "search", fake_search)
    windows = asyncio.run(
        catalog.search_alias_windows(
            ["Show", "Romaji", "Native"],
            season=1,
            expected=12,
            missing_episodes=[1, 12],
            additional_catalogs=[Source()],
        )
    )
    assert source_calls == ["Show", "Romaji", "Show 01", "Show 12"]
    assert len(nyaa_calls) == 7
    assert not windows[0].provider_complete
    assert windows[0].error_code == "NYAA_ERROR"
    assert {item.torrent_hash for item in catalog.merge_alias_windows(windows)} == {
        "A" * 40,
        "B" * 40,
    }
    assert catalog.candidate_rejection_reason(
        _candidate("Different Anime S01E01 [1080p]"),
        season=1,
        target_title="Show",
    )


@pytest.mark.parametrize(
    "missing,excluded", [((1, 2), frozenset()), ((), frozenset()), ((1, 2), frozenset({"A" * 40}))]
)
def test_metadata_enrichment_is_global_bounded_and_only_for_eligible_gaps(
    monkeypatch, missing, excluded
):
    from dataclasses import replace

    from omakase.plus import torrent_metadata

    rows = [
        replace(
            _torrent("Show Season 1 [1080p][Multi-Subs]", ch * 40),
            torrent_url=f"https://nyaa.si/download/{i}.torrent",
        )
        for i, ch in enumerate("ABC", start=1)
    ]
    rows.extend(
        [
            replace(
                _torrent("Show Season 2 [1080p][Multi-Subs]", "D" * 40),
                torrent_url="https://nyaa.si/download/4.torrent",
            ),
            replace(
                _torrent("Show Season 1 [480p][Multi-Subs]", "E" * 40),
                torrent_url="https://nyaa.si/download/5.torrent",
            ),
        ]
    )

    async def search(_alias, **_kwargs):
        return rows

    probes = []

    async def fetch(url, **kwargs):
        probes.append(kwargs["torrent_hash"])
        return frozenset({1, 2})

    monkeypatch.setattr(catalog, "search", search)
    monkeypatch.setattr(torrent_metadata, "fetch_planned_coverage", fetch)
    windows = asyncio.run(
        catalog.search_alias_windows(
            ("Show Season 1", "Show"),
            season=1,
            expected=2,
            missing_episodes=missing,
            metadata_excluded_hashes=excluded,
        )
    )
    found = catalog.merge_alias_windows(windows)
    assert len(probes) == (2 if missing else 0)
    assert len(set(probes)) == len(probes)
    assert not set(probes) & excluded
    assert sum(item.metadata_backed for item in found) == (2 if missing else 0)
    assert all(window.provider_complete for window in windows)
    assert all(not item.coverage for item in found if item.torrent_hash in {"D" * 40, "E" * 40})
    assert any(
        not item.coverage for item in found if item.torrent_hash in {"A" * 40, "B" * 40, "C" * 40}
    )


def test_invalid_metadata_origin_does_not_consume_public_probe_budget(monkeypatch):
    from dataclasses import replace

    from omakase.plus import torrent_metadata

    rows = [
        replace(_torrent("Show Season 1 [1080p]", ch * 40), torrent_url=url)
        for ch, url in [
            ("A", "https://127.0.0.1/private"),
            ("B", "https://nyaa.si/download/2.torrent"),
            ("C", "https://nyaa.si/download/3.torrent"),
        ]
    ]

    async def search(_alias, **_kwargs):
        return rows

    probes = []

    async def fetch(url, **_kwargs):
        probes.append(url)
        return frozenset()

    monkeypatch.setattr(catalog, "search", search)
    monkeypatch.setattr(torrent_metadata, "fetch_planned_coverage", fetch)
    windows = asyncio.run(
        catalog.search_alias_windows(("Show",), season=1, expected=2, missing_episodes=(1,))
    )
    assert len(probes) == 2
    assert all(not candidate.metadata_backed for candidate in catalog.merge_alias_windows(windows))
    assert all(window.provider_complete for window in windows)
