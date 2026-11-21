"""Judgement, truthfulness and persistence for Omakase's own request search (W-06).

These tests pin the behaviour the owner asked for: the request page must be able to say what
a search found and why a candidate failed. They deliberately do not exercise a live indexer:
the judgement rules and the recorded evidence are what the page's truthfulness depends on.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass

import pytest

from omakase.plus import request_candidates as rc


@dataclass
class Candidate:
    title: str
    magnet: str = "magnet:?xt=urn:btih:" + "a" * 40
    seeders: int = 12
    size_bytes: int = 4 * 1024**3
    is_batch: bool = False
    torrent_url: str = ""
    indexer: str = "fixture"


@pytest.fixture()
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        open(
            "src/omakase/plus/migrations/060-request-candidates.sql",
            encoding="utf-8",
        ).read()
    )
    yield connection
    connection.close()


def movie(**overrides):
    return rc.JudgeRequest(title="Chouchou", kind="movie", year=2003, **overrides)


# --- the side effect the old page lacked: it can now say why a candidate failed ---------


def test_each_rejection_carries_one_plain_reason():
    assert (
        rc.judge(Candidate("Chouchou 2003 1080p", magnet="", torrent_url=""), movie()).code
        == "no_download"
    )
    assert rc.judge(Candidate("Chouchou 2003 1080p", seeders=0), movie()).code == "no_seeders"
    assert rc.judge(Candidate("Some Other Film 2003 1080p"), movie()).code == "title_mismatch"
    assert rc.judge(Candidate("Chouchou 1998 1080p"), movie()).code == "year_mismatch"
    verdict = rc.judge(
        Candidate("Chouchou 2003 1080p", size_bytes=40 * 1024**3),
        rc.JudgeRequest(title="Chouchou", year=2003, maximum_bytes=8 * 1024**3),
    )
    assert verdict.code == "too_large"
    for verdict in (
        rc.judge(Candidate("Chouchou 2003", seeders=0), movie()),
        rc.judge(Candidate("Other 2003"), movie()),
    ):
        assert not verdict.accepted
        assert verdict.text  # the page always has words to show
        assert "no reason" not in verdict.text


def test_a_matching_candidate_is_accepted():
    verdict = rc.judge(Candidate("Chouchou.2003.1080p.BluRay.x264-GROUP"), movie())
    assert verdict.accepted and verdict.code == "accepted"


def test_release_tags_and_punctuation_do_not_hide_a_match():
    assert rc.judge(Candidate("[Group] Chouchou (2003) [1080p][HEVC][Dual Subs]"), movie()).accepted


def test_episode_identity_is_enforced_for_series():
    request = rc.JudgeRequest(title="MobLand", kind="tv", season=1, episode=3)
    assert rc.judge(Candidate("MobLand S01E03 1080p WEB-DL"), request).accepted
    assert rc.judge(Candidate("MobLand S01E04 1080p WEB-DL"), request).code == "episode_mismatch"
    assert rc.judge(Candidate("MobLand S02E03 1080p WEB-DL"), request).code == "episode_mismatch"
    assert rc.judge(Candidate("MobLand 1080p WEB-DL"), request).code == "episode_mismatch"
    # A batch is a legitimate answer to a single episode request.
    assert rc.judge(Candidate("MobLand Complete Season 1", is_batch=True), request).accepted


# --- the summary and the sentence the page shows ----------------------------------------


def test_summary_counts_and_orders_reasons_by_frequency():
    verdicts = [
        rc.Verdict(False, "no_seeders", ""),
        rc.Verdict(False, "no_seeders", ""),
        rc.Verdict(False, "title_mismatch", ""),
        rc.Verdict(True, "accepted", ""),
    ]
    summary = rc.summarize(verdicts)
    assert summary["found"] == 4 and summary["accepted"] == 1 and summary["rejected"] == 3
    assert [item["code"] for item in summary["reasons"]] == ["no_seeders", "title_mismatch"]


def test_describe_never_overstates():
    assert "returned no releases" in rc.describe(rc.summarize([]))
    none_usable = rc.describe(rc.summarize([rc.Verdict(False, "no_seeders", "")]))
    assert "None passed the release checks" in none_usable
    usable = rc.describe(
        rc.summarize([rc.Verdict(True, "accepted", ""), rc.Verdict(False, "no_seeders", "")])
    )
    assert "2 releases found. 1 passed the release checks" in usable


# --- the page block: honest when the feature is off ------------------------------------


def test_page_block_says_the_search_is_off_rather_than_implying_a_result(monkeypatch, db):
    monkeypatch.delenv(rc.SEARCH_ENV, raising=False)
    block = rc.page_block(db, 1, "movie", "tt0000001")
    assert block["enabled"] is False
    assert "own search is off" in block["headline"]
    assert block["candidates"] == []


def test_page_block_reports_the_recorded_snapshot(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    rc.record(
        db,
        1,
        "movie",
        "tt0000001",
        [
            (Candidate("Chouchou 2003 1080p"), rc.Verdict(True, "accepted", "")),
            (Candidate("Chouchou 2003 CAM", seeders=0), rc.Verdict(False, "no_seeders", "")),
        ],
    )
    block = rc.page_block(db, 1, "movie", "tt0000001")
    assert block["enabled"] is True
    assert block["summary"]["found"] == 2 and block["summary"]["accepted"] == 1
    assert [row["accepted"] for row in block["candidates"]] == [True, False]
    assert block["candidates"][1]["text"] == "nobody is seeding it"


def test_record_replaces_the_previous_snapshot_instead_of_appending(monkeypatch, db):
    rc.record(
        db,
        1,
        "movie",
        "tt0000002",
        [(Candidate("Old release 2003"), rc.Verdict(False, "title_mismatch", ""))],
    )
    rc.record(
        db,
        1,
        "movie",
        "tt0000002",
        [(Candidate("Chouchou 2003"), rc.Verdict(True, "accepted", ""))],
    )
    rows = rc.snapshot(db, 1, "movie", "tt0000002")
    assert len(rows) == 1 and bool(rows[0]["accepted"]) is True


def test_search_and_acquire_are_separately_switched(monkeypatch):
    monkeypatch.delenv(rc.SEARCH_ENV, raising=False)
    monkeypatch.delenv(rc.ACQUIRE_ENV, raising=False)
    assert rc.search_enabled() is False and rc.acquire_enabled() is False
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    assert rc.search_enabled() is True and rc.acquire_enabled() is False
    monkeypatch.setenv(rc.ACQUIRE_ENV, "true")
    assert rc.acquire_enabled() is True


# --- the search actually runs on its own, bounded and cached ---------------------------


class FakeSource:
    """A tracked indexer stand-in: it records that it was searched and returns candidates."""

    def __init__(self, candidates):
        self.candidates = candidates
        self.calls = 0

    async def search(self, query):
        self.calls += 1
        return tuple(self.candidates)


class BrokenSource:
    async def search(self, query):
        raise RuntimeError("indexer unreachable")


def test_refresh_searches_judges_and_records(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    source = FakeSource(
        [
            Candidate("Chouchou 2003 1080p"),
            Candidate("Chouchou 2003 CAM", seeders=0),
        ]
    )
    block = __import__("asyncio").run(
        rc.refresh(db, 1, "movie", "tt0000003", movie(), catalogs=[source])
    )
    assert source.calls == 1
    assert block["enabled"] is True
    assert block["summary"]["found"] == 2 and block["summary"]["accepted"] == 1
    # Stored, so the next page render can be free.
    assert len(rc.snapshot(db, 1, "movie", "tt0000003")) == 2


def test_refresh_reuses_a_fresh_snapshot_instead_of_researching(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    source = FakeSource([Candidate("Chouchou 2003 1080p")])
    asyncio.run(rc.refresh(db, 1, "movie", "tt0000004", movie(), catalogs=[source]))
    asyncio.run(rc.refresh(db, 1, "movie", "tt0000004", movie(), catalogs=[source]))
    assert source.calls == 1
    asyncio.run(rc.refresh(db, 1, "movie", "tt0000004", movie(), catalogs=[source], force=True))
    assert source.calls == 2


def test_a_failed_search_never_reads_like_an_empty_result(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    block = asyncio.run(rc.refresh(db, 1, "movie", "tt0000005", movie(), catalogs=[BrokenSource()]))
    assert block["error"] is True
    assert "could not confirm a result" in block["headline"]
    assert block["candidates"] == []
    # And the failure is durable, so a later render still says so rather than "nothing exists".
    assert rc.page_block(db, 1, "movie", "tt0000005")["error"] is True


def test_successful_empty_search_is_distinct_and_cached(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    sources = [FakeSource([]), FakeSource([])]
    for _ in range(2):
        block = asyncio.run(rc.refresh(db, 1, "movie", "tt0000007", movie(), catalogs=sources))
        assert "returned no releases" in block["headline"]
        assert "error" not in block and "partial" not in block
        assert block["candidates"] == []
    assert [source.calls for source in sources] == [1, 1]


def test_no_configured_sources_does_not_claim_a_search(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    assert "not recorded" in rc.page_block(db, 1, "movie", "tt0000008")["headline"]
    block = asyncio.run(rc.refresh(db, 1, "movie", "tt0000008", movie(), catalogs=[]))
    assert "No release sources are configured" in block["headline"]
    assert block["candidates"] == []
    assert "error" not in block
    assert rc.page_block(db, 1, "movie", "tt0000008") == block


class SlowSource:
    def __init__(self):
        self.cancelled = False

    async def search(self, query):
        try:
            await asyncio.Future()
        finally:
            self.cancelled = True


def test_partial_timeout_keeps_completed_candidates(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    good = FakeSource([Candidate("Chouchou 2003 1080p")])
    slow = SlowSource()
    block = asyncio.run(
        rc.refresh(db, 1, "movie", "tt0000009", movie(), catalogs=[good, slow], timeout=0.01)
    )
    assert slow.cancelled
    assert block["partial"] is True
    assert "Some sources did not complete" in block["headline"]
    assert block["summary"]["found"] == 1
    assert block["candidates"][0]["accepted"] is True
    assert rc.page_block(db, 1, "movie", "tt0000009") == block


def test_partial_empty_is_not_a_conclusive_no_result(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    block = asyncio.run(
        rc.refresh(db, 1, "movie", "tt0000010", movie(), catalogs=[FakeSource([]), BrokenSource()])
    )
    assert block["partial"] is True
    assert "found no candidates in the sources that responded" in block["headline"]
    assert block["candidates"] == []
    assert "error" not in block


def test_all_sources_time_out_without_a_confirmed_result(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    slow = SlowSource()
    block = asyncio.run(
        rc.refresh(db, 1, "movie", "tt0000011", movie(), catalogs=[slow], timeout=0.01)
    )
    assert slow.cancelled
    assert block["error"] is True
    assert "could not confirm a result" in block["headline"]
    assert block["candidates"] == []


def test_incomplete_search_can_retry_sooner_than_complete_search(monkeypatch, db):
    from types import SimpleNamespace

    time = [100000.0]
    monkeypatch.setenv(rc.SEARCH_ENV, "1")
    monkeypatch.setattr(rc, "time", SimpleNamespace(time=lambda: time[0]))
    failure_then_good = FakeSource([Candidate("Chouchou 2003 1080p")])
    asyncio.run(rc.refresh(db, 1, "movie", "tt0000012", movie(), catalogs=[BrokenSource()]))
    asyncio.run(rc.refresh(db, 1, "movie", "tt0000013", movie(), catalogs=[FakeSource([])]))
    time[0] += 120
    recovered = asyncio.run(
        rc.refresh(db, 1, "movie", "tt0000012", movie(), catalogs=[failure_then_good])
    )
    complete = asyncio.run(
        rc.refresh(db, 1, "movie", "tt0000013", movie(), catalogs=[failure_then_good])
    )
    assert recovered["summary"]["found"] == 1
    assert complete["summary"]["found"] == 0
    assert failure_then_good.calls == 1


def test_simultaneous_views_share_one_search_for_the_same_request(monkeypatch, db):
    monkeypatch.setenv(rc.SEARCH_ENV, "1")

    class Source(FakeSource):
        async def search(self, query):
            await asyncio.sleep(0.01)
            return await super().search(query)

    source = Source([Candidate("Chouchou 2003 1080p")])

    async def checks():
        async def check():
            return await rc.refresh(db, 1, "movie", "tt0000014", movie(), catalogs=[source])

        return await asyncio.gather(check(), check())

    first, second = asyncio.run(checks())
    assert source.calls == 1
    assert first == second
    assert first["summary"]["found"] == 1


def test_refresh_does_nothing_when_the_feature_is_off(monkeypatch, db):
    monkeypatch.delenv(rc.SEARCH_ENV, raising=False)
    source = FakeSource([Candidate("Chouchou 2003 1080p")])
    block = asyncio.run(rc.refresh(db, 1, "movie", "tt0000006", movie(), catalogs=[source]))
    assert source.calls == 0
    assert block["enabled"] is False
    assert rc.snapshot(db, 1, "movie", "tt0000006") == []
