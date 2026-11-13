"""Tests for nyaa.si RSS search + best-match heuristic."""

from __future__ import annotations

from datetime import datetime, timezone

from omakase.plus.acquisition_policy import AcquisitionPolicy
from omakase.plus.nyaa import NyaaTorrent, _is_likely_batch, _parse_size, find_best

RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa" xmlns:torrent="http://xmlns.ezrss.it/0.1/">
<channel>
<title>Nyaa - &#34;Dungeon Meshi&#34;</title>
<item>
<title>[SubsPlease] Dungeon Meshi - 01 (1080p) [A8712C3D]</title>
<link>https://nyaa.si/view/12345</link>
<guid>magnet:?xt=urn:btih:A8712C3D&amp;dn=Dungeon+Meshi+01</guid>
<pubDate>Sun, 28 May 2026 12:00:00 -0000</pubDate>
<nyaa:seeders>150</nyaa:seeders>
<nyaa:leechers>20</nyaa:leechers>
<nyaa:downloads>500</nyaa:downloads>
<nyaa:size>1.4 GiB</nyaa:size>
<nyaa:trusted>Yes</nyaa:trusted>
<nyaa:category>Anime - English-translated</nyaa:category>
</item>
<item>
<title>Dungeon Meshi - 01-24 Complete 1080p Batch</title>
<link>https://nyaa.si/view/67890</link>
<guid>magnet:?xt=urn:btih:BATCH9999&amp;dn=Dungeon+Meshi+Batch</guid>
<pubDate>Sun, 28 May 2026 10:00:00 -0000</pubDate>
<nyaa:seeders>300</nyaa:seeders>
<nyaa:leechers>5</nyaa:leechers>
<nyaa:downloads>1000</nyaa:downloads>
<nyaa:size>25.0 GiB</nyaa:size>
<nyaa:trusted>No</nyaa:trusted>
<nyaa:category>Anime - English-translated</nyaa:category>
</item>
<item>
<title>[HorribleSubs] Dungeon Meshi - 01 (720p)</title>
<link>https://nyaa.si/view/11111</link>
<guid>magnet:?xt=urn:btih:HS720P01&amp;dn=Dungeon+Meshi+720p</guid>
<pubDate>Sat, 27 May 2026 08:00:00 -0000</pubDate>
<nyaa:seeders>0</nyaa:seeders>
<nyaa:leechers>3</nyaa:leechers>
<nyaa:downloads>50</nyaa:downloads>
<nyaa:size>350.0 MiB</nyaa:size>
<nyaa:trusted>No</nyaa:trusted>
<nyaa:category>Anime - English-translated</nyaa:category>
</item>
</channel>
</rss>"""


class TestNyaaTorrent:
    def test_from_xml_fields(self):
        t = NyaaTorrent(
            title="[SubsPlease] Dungeon Meshi - 01",
            magnet="magnet:?xt=urn:btih:ABC",
            seeders=100,
            leechers=10,
            size_bytes=1503238528,
            size_display="1.4 GiB",
            pub_date=datetime.now(timezone.utc),
            is_trusted=True,
            is_batch=False,
        )
        assert t.seeders == 100
        assert t.is_trusted is True
        assert t.is_batch is False


class TestParseSize:
    def test_gib(self):
        b, _ = _parse_size("1.4 GiB")
        assert b == int(1.4 * 1024**3)

    def test_mib(self):
        b, _ = _parse_size("350.0 MiB")
        assert b == int(350.0 * 1024**2)

    def test_kib(self):
        b, _ = _parse_size("512 KiB")
        assert b == 512 * 1024

    def test_empty(self):
        b, _ = _parse_size("")
        assert b == 0


class TestIsLikelyBatch:
    def test_batch_keyword(self):
        assert _is_likely_batch("Dungeon Meshi Complete 1080p") is True

    def test_all_episodes(self):
        assert _is_likely_batch("One Piece all episodes 1-1000") is True

    def test_episode_range(self):
        assert _is_likely_batch("Dungeon Meshi - 01-24 1080p") is True

    def test_season_keyword(self):
        assert _is_likely_batch("Attack on Titan Season 1 Complete") is True

    def test_season_with_single_episode_marker_is_not_batch(self):
        assert _is_likely_batch("Show Season 2 - 01 [1080p]") is False

    def test_season_code_with_single_episode_marker_is_not_batch(self):
        assert _is_likely_batch("Show S01 - 01 [1080p]") is False

    def test_single_episode_is_not_batch(self):
        assert _is_likely_batch("[SubsPlease] Dungeon Meshi - 01 (1080p)") is False


class TestFindBest:
    def _make_torrent(
        self,
        title,
        seeders,
        trusted=False,
        batch=False,
        size_bytes=1_000_000_000,
        size_display="1 GiB",
    ):
        return NyaaTorrent(
            title=title,
            magnet=f"magnet:?xt=urn:btih:{title}",
            seeders=seeders,
            leechers=5,
            size_bytes=size_bytes,
            size_display=size_display,
            pub_date=datetime.now(timezone.utc),
            is_trusted=trusted,
            is_batch=batch,
        )

    def test_prefers_trusted(self):
        t1 = self._make_torrent("Show 01", 100, trusted=True, batch=False)
        t2 = self._make_torrent("Show 01", 500, trusted=False, batch=False)
        best = find_best([t2, t1])
        assert best is not None
        assert best.is_trusted is True

    def test_default_prefers_good_complete_batch_for_title_level_download(self):
        t1 = self._make_torrent("Show 01", 100, trusted=True, batch=True)
        t2 = self._make_torrent("Show 01", 100, trusted=True, batch=False)
        best = find_best([t1, t2])
        assert best is not None
        assert best.is_batch is True

    def test_can_still_prefer_non_batch_for_episode_level_download(self):
        t1 = self._make_torrent("Show 01-12 Complete", 100, trusted=True, batch=True)
        t2 = self._make_torrent("Show 01", 100, trusted=True, batch=False)
        best = find_best([t1, t2], prefer_no_batch=True)
        assert best is not None
        assert best.is_batch is False

    def test_seeders_breaks_tie(self):
        t1 = self._make_torrent("Show 01", 50, trusted=True, batch=False)
        t2 = self._make_torrent("Show 01", 200, trusted=True, batch=False)
        best = find_best([t1, t2])
        assert best is not None
        assert best.seeders == 200

    def test_rank_best_returns_all_viable_candidates_in_best_first_order(self):
        from omakase.plus import nyaa

        low_res_trusted = self._make_torrent(
            "[Trusted] Show - 01 [480p]",
            500,
            trusted=True,
            size_bytes=220_000_000,
            size_display="209.8 MiB",
        )
        high_quality = self._make_torrent(
            "[GoodGroup] Show - 01 [1080p][WEB-DL]",
            40,
            trusted=False,
            size_bytes=1_400_000_000,
            size_display="1.3 GiB",
        )
        cam = self._make_torrent("[FastSeeder] Show 1080p HDCAM", 300)

        assert hasattr(nyaa, "rank_best")
        ranked = nyaa.rank_best([low_res_trusted, cam, high_quality], expected_title="Show")

        assert ranked == [high_quality, low_res_trusted]

    def test_find_best_delegates_to_ranked_candidates(self):
        from omakase.plus import nyaa

        t1 = self._make_torrent("Show 01", 50, trusted=True, batch=False)
        t2 = self._make_torrent("Show 01", 200, trusted=True, batch=False)

        assert find_best([t1, t2]) is nyaa.rank_best([t1, t2])[0]

    def test_min_seeders_filters(self):
        t1 = self._make_torrent("Show 01", 0, trusted=False, batch=False)
        best = find_best([t1], min_seeders=1)
        assert best is None

    def test_owner_policy_controls_resolution_seeders_and_maximum_size(self):
        from omakase.plus import nyaa

        policy = AcquisitionPolicy(
            allowed_resolutions=("720p", "1080p"),
            minimum_seeders=3,
            preferred_seeders=10,
            max_release_size_gib=10,
            prefer_batches=False,
        )
        preferred = self._make_torrent(
            "Show - 01 [720p][WEB-DL]",
            3,
            size_bytes=800_000_000,
        )
        lower_priority = self._make_torrent(
            "Show - 01 [1080p][WEB-DL]",
            100,
            size_bytes=1_400_000_000,
        )
        disallowed = self._make_torrent("Show - 01 [2160p]", 500)
        oversized = self._make_torrent(
            "Show - 01 [720p]",
            500,
            size_bytes=11 * 1024**3,
        )

        ranked = nyaa.rank_best(
            [lower_priority, disallowed, oversized, preferred],
            expected_title="Show",
            policy=policy,
        )

        assert ranked == [preferred, lower_priority]

    def test_empty_list(self):
        assert find_best([]) is None

    def test_disable_trusted_pref(self):
        t1 = self._make_torrent("Show 01", 100, trusted=True, batch=False)
        t2 = self._make_torrent("Show 01", 500, trusted=False, batch=False)
        best = find_best([t2, t1], prefer_trusted=False)
        assert best is not None
        assert best.seeders == 500

    def test_disable_trusted_pref_also_disables_known_group_bonus(self):
        known_group = self._make_torrent(
            "[SubsPlease] Show 01 [1080p]",
            5,
            trusted=True,
            batch=False,
        )
        popular_release = self._make_torrent(
            "[Other] Show 01 [1080p]",
            100,
            trusted=False,
            batch=False,
        )

        best = find_best([known_group, popular_release], prefer_trusted=False)

        assert best is not None
        assert best.seeders == 100

    def test_prefers_1080p_webdl_over_high_seed_low_quality_raw(self):
        good = self._make_torrent(
            "[SubsPlease] Frieren - 01-28 Complete [1080p][WEB-DL][AAC]",
            80,
            batch=True,
            size_bytes=28_000_000_000,
            size_display="26.1 GiB",
        )
        low_quality = self._make_torrent(
            "[RandomRaw] Frieren CAM RAW 720p",
            900,
            batch=False,
            size_bytes=500_000_000,
            size_display="476.8 MiB",
        )

        best = find_best([low_quality, good])

        assert best is good

    def test_non_cam_bdrip_beats_2160p_cam_with_more_seeders(self):
        good = self._make_torrent(
            "[Judas] The Anime Movie [BDRip][1080p][x265][10bit]",
            25,
            size_bytes=4_500_000_000,
            size_display="4.2 GiB",
        )
        cam = self._make_torrent(
            "[FastSeeder] The Anime Movie 2160p HDCAM",
            300,
            size_bytes=1_200_000_000,
            size_display="1.1 GiB",
        )

        best = find_best([cam, good])

        assert best is good

    def test_quality_beats_trusted_low_resolution(self):
        low_res_trusted = self._make_torrent(
            "[Trusted] Show - 01 [480p]",
            500,
            trusted=True,
            size_bytes=220_000_000,
            size_display="209.8 MiB",
        )
        high_quality = self._make_torrent(
            "[GoodGroup] Show - 01 [1080p][WEB-DL]",
            40,
            trusted=False,
            size_bytes=1_400_000_000,
            size_display="1.3 GiB",
        )

        best = find_best([low_res_trusted, high_quality])

        assert best is high_quality

    def test_uses_seedable_720p_when_no_better_option_exists(self):
        only_seedable = self._make_torrent("[Group] Show - 01 [720p]", 30)

        best = find_best([only_seedable])

        assert best is only_seedable

    def test_rejects_cam_only_result(self):
        cam = self._make_torrent("[FastSeeder] The Anime Movie 1080p HDCAM", 300)

        best = find_best([cam])

        assert best is None

    def test_title_match_beats_wrong_title_quality(self):
        wrong_title = self._make_torrent(
            "[Trusted] Dungeon Meshi - 01-24 Complete [2160p][BluRay]",
            500,
            trusted=True,
            batch=True,
            size_bytes=55_000_000_000,
            size_display="51.2 GiB",
        )
        correct_title = self._make_torrent(
            "[GoodGroup] Frieren - 01 [720p]",
            20,
            size_bytes=800_000_000,
            size_display="762.9 MiB",
        )

        best = find_best([wrong_title, correct_title], expected_title="Frieren")

        assert best is correct_title

    def test_title_gate_returns_none_when_no_result_matches_expected_title(self):
        wrong_title = self._make_torrent(
            "[Trusted] Dungeon Meshi - 01-24 Complete [2160p][BluRay]",
            500,
            trusted=True,
            batch=True,
            size_bytes=55_000_000_000,
            size_display="51.2 GiB",
        )

        best = find_best([wrong_title], expected_title="Frieren")

        assert best is None

    def test_short_one_token_title_rejects_longer_different_title(self):
        wrong_title = self._make_torrent(
            "[EMBER] Berserk of Gluttony (2023) (Boushoku no Berserk) [BD 1080p][HEVC]",
            500,
            trusted=True,
            batch=True,
            size_bytes=24_000_000_000,
            size_display="22.4 GiB",
        )
        correct_title = self._make_torrent(
            "[GoodGroup] Berserk - 01-25 Complete [1080p]",
            25,
            batch=True,
            size_bytes=18_000_000_000,
            size_display="16.8 GiB",
        )

        best = find_best([wrong_title, correct_title], expected_title="Berserk")

        assert best is correct_title

    def test_short_one_token_title_accepts_exact_title_level_release(self):
        exact_title = self._make_torrent(
            "[Group] Berserk (1997) [BD 1080p]",
            30,
            size_bytes=18_000_000_000,
            size_display="16.8 GiB",
        )

        best = find_best([exact_title], expected_title="Berserk")

        assert best is exact_title

    def test_short_one_token_title_returns_none_when_only_longer_different_title_matches(
        self,
    ):
        wrong_title = self._make_torrent(
            "[EMBER] Berserk of Gluttony (2023) (Boushoku no Berserk) [BD 1080p][HEVC]",
            500,
            trusted=True,
            batch=True,
            size_bytes=24_000_000_000,
            size_display="22.4 GiB",
        )

        best = find_best([wrong_title], expected_title="Berserk")

        assert best is None

    def test_short_one_token_title_rejects_season_and_part_fragments(self):
        fragment_titles = [
            "[Group] Berserk Season 2 - 01-12 Complete [1080p]",
            "[Group] Berserk S2 - 01-12 Complete [1080p]",
            "[Group] Berserk S 2 - 01-12 Complete [1080p]",
            "[Group] Berserk S-2 - 01-12 Complete [1080p]",
            "[Group] Berserk S_2 - 01-12 Complete [1080p]",
            "[Group] Berserk S.2 - 01-12 Complete [1080p]",
            "[Group] Berserk Part 2 - 01-12 Complete [1080p]",
            "[Group] Berserk 2 Complete [1080p]",
        ]

        for title in fragment_titles:
            torrent = self._make_torrent(
                title,
                100,
                batch=True,
                size_bytes=8_000_000_000,
                size_display="7.5 GiB",
            )
            assert find_best([torrent], expected_title="Berserk") is None

    def test_explicit_season_number_detects_common_forms(self):
        from omakase.plus import nyaa

        assert nyaa._explicit_season_number("Berserk Season 2") == 2
        assert nyaa._explicit_season_number("Berserk S2") == 2
        assert nyaa._explicit_season_number("Golden Kamuy 3rd Season") == 3
        assert nyaa._explicit_season_number("Berserk") is None

    def test_specific_season_query_accepts_matching_short_title_season(self):
        target = self._make_torrent("[GoodGroup] Berserk Season 2 [1080p][HEVC]", 80)
        wrong = self._make_torrent("[GoodGroup] Berserk (1997) Complete [1080p]", 200)

        best = find_best([wrong, target], expected_title="Berserk Season 2")

        assert best is target

    def test_specific_season_query_rejects_wrong_short_title_season(self):
        wrong = self._make_torrent("[GoodGroup] Berserk Season 1 [1080p][HEVC]", 80)

        assert find_best([wrong], expected_title="Berserk Season 2") is None

    def test_explicit_part_query_rejects_wrong_part(self):
        target = self._make_torrent(
            "[SubsPlease] Attack on Titan The Final Season Part 2 - 01-12 [1080p]",
            50,
        )
        wrong = self._make_torrent(
            "[SubsPlease] Attack on Titan The Final Season Part 1 - 01-16 [1080p]",
            120,
        )

        best = find_best([wrong, target], expected_title="Attack on Titan Final Season Part 2")

        assert best is target

    def test_explicit_cour_query_rejects_wrong_cour(self):
        target = self._make_torrent("[GoodGroup] Spy x Family Cour 2 [1080p]", 80)
        wrong = self._make_torrent("[GoodGroup] Spy x Family Cour 1 [1080p]", 120)

        best = find_best([wrong, target], expected_title="Spy x Family Cour 2")

        assert best is target

    def test_title_gate_keeps_matching_common_title_shapes(self):
        cases = [
            (
                "Attack on Titan Final Season Part 2",
                "[SubsPlease] Attack on Titan The Final Season Part 2 - 01 [1080p]",
            ),
            (
                "BLEACH Thousand-Year Blood War",
                "[Vodes] BLEACH Thousand-Year Blood War - 01 [1080p]",
            ),
            ("Dungeon Meshi", "[SubsPlease] Dungeon Meshi - 01 [1080p]"),
            ("Frieren", "[SubsPlease] Frieren - 01-28 Complete [1080p]"),
        ]

        for expected_title, release_title in cases:
            torrent = self._make_torrent(release_title, 50)
            assert find_best([torrent], expected_title=expected_title) is torrent

    def test_rejects_common_extra_releases(self):
        extra_titles = [
            "[Group] Frieren NCOP [1080p]",
            "[Group] Frieren NCED [1080p]",
            "[Group] Frieren PV [1080p]",
            "[Group] Frieren Trailer [1080p]",
            "[Group] Frieren CM [1080p]",
            "[Group] Frieren OST [FLAC]",
            "[Group] Frieren OP [1080p]",
            "[Group] Frieren ED-only [1080p]",
            "[Group] Frieren Creditless Opening [1080p]",
            "[Group] Frieren Music Video [1080p]",
        ]

        for title in extra_titles:
            torrent = self._make_torrent(title, 100)
            assert find_best([torrent], expected_title="Frieren") is None

    def test_rejects_enormous_remux_dump_when_reasonable_complete_release_exists(self):
        dump = self._make_torrent(
            "[BigDump] Frieren Complete BDMV BDRemux Collection [2160p]",
            300,
            batch=True,
            size_bytes=420 * 1024**3,
            size_display="420.0 GiB",
        )
        reasonable = self._make_torrent(
            "[SubsPlease] Frieren - 01-28 Complete [1080p][WEB-DL]",
            80,
            batch=True,
            size_bytes=28_000_000_000,
            size_display="26.1 GiB",
        )

        best = find_best([dump, reasonable], expected_title="Frieren")

        assert best is reasonable

    def test_rejects_enormous_remux_dump_without_reasonable_alternative(self):
        dump = self._make_torrent(
            "[BigDump] Frieren Complete BDMV BDRemux Collection [2160p]",
            300,
            batch=True,
            size_bytes=420 * 1024**3,
            size_display="420.0 GiB",
        )

        best = find_best([dump], expected_title="Frieren")

        assert best is None


def test_rss_retains_public_metadata_link_without_fetching_the_torrent(monkeypatch):
    import asyncio

    import httpx

    from omakase.plus import nyaa

    calls = []

    def request(req):
        calls.append(req)
        return httpx.Response(
            200,
            text=RSS_XML.replace(
                "https://nyaa.si/view/12345", "https://nyaa.si/download/12345.torrent"
            ),
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        nyaa.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(request), **kwargs),
    )
    rows = asyncio.run(nyaa.search("Dungeon Meshi"))
    assert (
        next(row for row in rows if row.seeders == 150).torrent_url
        == "https://nyaa.si/download/12345.torrent"
    )
    assert len(calls) == 1
