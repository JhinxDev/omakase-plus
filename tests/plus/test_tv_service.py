"""Native TV adapters: exact identity, explicit history and complete bounded reads."""

import asyncio
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from omakase.plus.db import run_migrations
from omakase.plus.movies import MovieConnections, MovieError
from omakase.plus.tv_service import TVError, TVService, air_date, number, plain


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    run_migrations(conn)
    conn.executemany(
        "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
        [(1, "one@test.invalid", "x"), (2, "two@test.invalid", "x")],
    )
    conn.commit()
    yield conn
    conn.close()


class Native:
    def __init__(self):
        self.calls = []
        self.tmdb = 42
        self.episode_parent = "7"
        self.short_page = False
        self.total = 1
        self.guid = "tmdb://42"
        self.imdb = "tt1234567"
        self.catalog = {
            "id": 42,
            "name": "TV Title",
            "externalIds": {"imdbId": self.imdb},
            "firstAirDate": "2020-01-01",
            "overview": "<p>Safe &amp; plain.</p><script>bad()</script>",
            "posterPath": "/poster.jpg",
            "seasons": [
                {"seasonNumber": 1, "name": "Season 1", "airDate": "2020-01-01", "episodeCount": 2}
            ],
        }

    def __call__(self, request):
        self.calls.append(request)
        assert request.method == "GET"
        path = request.url.path
        if path == "/api/v1/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"mediaType": "movie", "id": 9},
                        {
                            "mediaType": "tv",
                            "id": 42,
                            "name": "TV Title",
                            "overview": "Search synopsis",
                            "firstAirDate": "2020-01-01",
                        },
                    ]
                },
            )
        if path == "/api/v1/tv/42":
            return httpx.Response(200, json=self.catalog)
        if path == "/api/v1/tv/42/season/1":
            return httpx.Response(
                200,
                json={
                    "seasonNumber": 1,
                    "episodes": [
                        {
                            "seasonNumber": 1,
                            "episodeNumber": 1,
                            "name": "Pilot",
                            "airDate": "2020-01-01",
                        },
                        {
                            "seasonNumber": 1,
                            "episodeNumber": 2,
                            "name": "Future",
                            "airDate": "2099-01-01",
                        },
                    ],
                },
            )
        if path == "/library/sections":
            xml = '<MediaContainer><Directory key="5" title="TV Shows" type="show"/><Directory key="9" title="Anime" type="show"/></MediaContainer>'
        elif path == "/library/sections/5/all":
            xml = f'<MediaContainer totalSize="{self.total}" offset="0" librarySectionID="5"><Directory type="show" ratingKey="7"><Guid id="{self.guid}"/><Guid id="imdb://{self.imdb}"/></Directory></MediaContainer>'
        elif path == "/library/metadata/7/allLeaves":
            xml = f'<MediaContainer totalSize="1" offset="0" librarySectionID="5"><Video type="episode" grandparentRatingKey="{self.episode_parent}" parentIndex="1" index="1" title="Pilot" originallyAvailableAt="2020-01-01" viewCount="1"/></MediaContainer>'
        else:
            raise AssertionError(path)
        return httpx.Response(200, text=xml)


def service(db, native):
    return TVService(
        db,
        MovieConnections(
            enabled=True,
            riven_url="http://riven.test",
            riven_api_key="riven-fixture",
            seerr_url="http://seerr.test",
            seerr_api_key="seerr-fixture",
            plex_url="http://plex.test",
            plex_token="plex-fixture",
        ),
        transport=httpx.MockTransport(native),
        now=lambda: datetime(2026, 10, 27, tzinfo=timezone.utc),
    )


def run(coro):
    return asyncio.run(coro)


def test_search_filters_movies_and_detail_cache_plain_text(db):
    native = Native()
    svc = service(db, native)
    cards = run(svc.search(1, "TV Title"))
    assert len(cards) == 1 and cards[0]["tmdb_id"] == 42
    card = run(svc.detail(1, 42))
    assert card["overview"] == "Safe & plain."
    assert card["status_source"] == "none" and not card["coverage_complete"]
    assert card["poster_url"] == "/plus/tv/42/artwork?variant=poster"
    calls = len(native.calls)
    run(svc.detail(1, 42))
    assert len(native.calls) == calls
    assert not run(svc.list(2))
    assert all("plex" not in r.url.host for r in native.calls)


def test_explicit_plex_import_is_bounded_and_never_playback_proof(db):
    native = Native()
    svc = service(db, native)
    result = run(svc.sync(1))
    assert result == {
        "imported": 1,
        "skipped": 0,
        "remaining": 0,
        "next_offset": 1,
        "total": 1,
        "coverage_complete": True,
    }
    card = run(svc.check(1, 42))
    assert card["in_plex_episodes"] == 1 and card["watched_episodes"] == 1
    assert card["available_episodes"] == 0 and card["status_source"] == "none"
    assert card["seasons"][0]["episodes"][0]["available"] is None
    assert all("/library/parts/" not in r.url.path for r in native.calls)
    assert all(r.method == "GET" for r in native.calls)
    assert all(
        r.headers.get("X-Api-Key") is None for r in native.calls if r.url.host == "plex.test"
    )


def test_episode_manual_wins_and_rating_does_not_set_progress(db):
    native = Native()
    svc = service(db, native)
    run(svc.detail(1, 42))
    run(svc.season(1, 42, 1))
    run(svc.save_episode(1, 42, 1, 1, False))
    card = run(svc.check(1, 42))
    assert card["watched_episodes"] == 0
    assert card["seasons"][0]["episodes"][0]["watch_source"] == "manual"
    card = run(svc.save_history(1, 42, rating=8.5))
    assert (
        card["rating"] == 8.5
        and card["rating_source"] == "manual"
        and card["status_source"] == "none"
    )
    run(svc.season(1, 42, 1))
    assert run(svc.detail(1, 42))["seasons"][0]["episodes"][0]["watch_source"] == "manual"
    with pytest.raises(TVError):
        run(svc.save_episode(1, 42, 1, 2, True))
    assert not run(svc.list(2))


@pytest.mark.parametrize("rating", [True, float("nan"), float("inf"), 0, 11, 8.2, "8"])
def test_invalid_ratings(db, rating):
    svc = service(db, Native())
    with pytest.raises(TVError):
        run(svc.save_history(1, 42, rating=rating))


def test_conflicting_identity_and_topology_fail_before_history(db):
    native = Native()
    svc = service(db, native)
    run(svc.detail(1, 42))
    native.imdb = "tt7654321"
    with pytest.raises(TVError, match="conflict"):
        run(svc.check(1, 42))
    native.imdb = "tt1234567"
    native.episode_parent = "99"
    with pytest.raises(TVError, match="identity"):
        run(svc.check(1, 42))
    assert db.execute("SELECT count(*) FROM tv_episodes").fetchone()[0] == 0


def test_ambiguous_or_title_only_plex_is_skipped(db):
    native = Native()
    native.guid = "tvdb://42"
    svc = service(db, native)
    assert run(svc.sync(1))["skipped"] == 1
    assert run(svc.list(1)) == []
    assert len(native.calls) == 2


@pytest.mark.parametrize("value", ["2026-02-30", "2026-2-01", 12, "tomorrow"])
def test_dates_are_strict(value):
    with pytest.raises(TVError):
        air_date(value)


def test_bounded_input_and_unknown_user(db):
    svc = service(db, Native())
    for query in ["", "x" * 201, "TV\nTitle"]:
        with pytest.raises(TVError):
            run(svc.search(1, query))
    with pytest.raises(TVError):
        run(svc.sync(1, offset=10001))
    with pytest.raises(TVError):
        run(svc.list(999))
    with pytest.raises((TVError, MovieError)):
        run(svc.detail(1, True))
    assert plain("<b>hello</b> &lt;world&gt;<style>secret</style>") == "hello <world>"
    with pytest.raises(TVError):
        number(True, 200)


def test_catalog_search_and_open_do_not_add_collection(db):
    native = Native()
    svc = service(db, native)
    run(svc.search(1, "TV Title"))
    assert run(svc.list(1)) == []
    assert run(svc.search(1, "42"))[0]["tmdb_id"] == 42
    assert run(svc.list(1)) == []
    run(svc.save_history(1, 42, rating=8))
    assert len(run(svc.list(1))) == 1
    assert run(svc.list(2)) == []


def test_short_page_cannot_be_claimed_as_complete(db):
    native = Native()
    native.total = 2
    svc = service(db, native)
    with pytest.raises(TVError, match="paging"):
        run(svc.sync(1))
    assert db.execute("SELECT count(*) FROM tv_catalog").fetchone()[0] == 0


def test_different_plex_account_never_reuses_presence(db):
    native = Native()
    svc = service(db, native)
    run(svc.check(1, 42))
    from dataclasses import replace

    second = TVService(db, replace(svc.connections, plex_token="different-account"))
    card = run(second.detail(1, 42))
    assert card["in_plex_episodes"] == 0
    assert not card["coverage_complete"]
    assert not card["plex_url"]
    assert not card["seasons"][0]["episodes"][0]["in_plex"]
    # Personal history survives reconnection, but the old account is no longer
    # used as evidence that a current library item exists.
    assert card["watched_episodes"] == 1


def test_malformed_or_duplicate_season_metadata_is_rejected(db):
    native = Native()
    native.catalog["seasons"] = ["not a season"]
    with pytest.raises(TVError):
        run(service(db, native).detail(1, 42))
    native.catalog["seasons"] = [{"seasonNumber": True}]
    with pytest.raises(TVError):
        run(service(db, native).detail(1, 42))


def test_cached_future_dates_advance_without_catalog_network(db):
    from datetime import timedelta

    native = Native()
    svc = service(db, native)
    now = datetime(2026, 10, 27, tzinfo=timezone.utc)
    svc._now = lambda: now
    native.catalog["seasons"][0]["airDate"] = "2026-10-28"
    run(svc.detail(1, 42))
    run(svc.season(1, 42, 1))
    with db:
        import json

        episode = {
            "episode_number": 2,
            "title": "Tomorrow",
            "air_date": "2026-10-28",
            "released": False,
        }
        db.execute(
            "UPDATE tv_episodes SET metadata_json=? WHERE episode_number=2", (json.dumps(episode),)
        )
    before = run(svc.detail(1, 42))
    assert not before["seasons"][0]["released"]
    assert not before["seasons"][0]["episodes"][1]["released"]
    with pytest.raises(TVError):
        run(svc.save_episode(1, 42, 1, 2, True))
    calls = len(native.calls)
    now += timedelta(days=1)
    after = run(svc.detail(1, 42))
    assert after["seasons"][0]["released"]
    assert after["seasons"][0]["episodes"][1]["released"]
    assert run(svc.save_episode(1, 42, 1, 2, True))["watched_episodes"] == 1
    assert len(native.calls) == calls


def test_explicit_season_refresh_reads_new_episode_count_and_preserves_history(db):
    native = Native()
    original = native.__call__
    expanded = False

    def handler(request):
        if expanded and request.url.path == "/api/v1/tv/42/season/1":
            native.calls.append(request)
            return httpx.Response(
                200,
                json={
                    "seasonNumber": 1,
                    "episodes": [
                        {
                            "seasonNumber": 1,
                            "episodeNumber": n,
                            "name": f"Episode {n}",
                            "airDate": "2020-01-01",
                        }
                        for n in range(1, 4)
                    ],
                },
            )
        return original(request)

    svc = service(db, handler)
    run(svc.detail(1, 42))
    run(svc.season(1, 42, 1))
    run(svc.save_episode(1, 42, 1, 1, True))
    assert run(svc.detail(1, 42))["seasons"][0]["episodes_loaded"]
    assert not run(svc.refresh(1, 42))["seasons"][0]["episodes_loaded"]
    expanded = True
    card = run(svc.season(1, 42, 1))
    assert card["seasons"][0]["episode_count"] == 3
    assert len(card["seasons"][0]["episodes"]) == 3
    assert card["seasons"][0]["episodes"][0]["watched"]
    assert card["seasons"][0]["episodes"][0]["watch_source"] == "manual"


def test_filtered_collection_precedes_paging_with_explicit_continuation(db):
    import json

    svc = service(db, Native())
    with db:
        for tmdb_id in range(1, 603):
            db.execute(
                "INSERT INTO tv_catalog VALUES(?,?,?,?)",
                (
                    1,
                    tmdb_id,
                    json.dumps(
                        {
                            "tmdb_id": tmdb_id,
                            "title": f"Show {tmdb_id}",
                            "in_collection": True,
                            "seasons": [],
                        }
                    ),
                    f"2026-10-{27 if tmdb_id > 1 else 1:02d}",
                ),
            )
        db.execute(
            "INSERT INTO tv_history(user_id,tmdb_id,status,status_source,updated_at) VALUES(1,1,'watching','manual','2026-10-01')"
        )
    page = run(svc.collection_page(1, view="watching"))
    assert page["total"] == 1 and len(page["cards"]) == 1
    assert page["cards"][0]["tmdb_id"] == 1 and not page["has_more"]
    first = run(svc.collection_page(1))
    assert first["total"] == 602 and len(first["cards"]) == 100
    assert first["next_offset"] == 100 and first["previous_offset"] is None
    seen = set()
    offset = 0
    while True:
        current = run(svc.collection_page(1, offset=offset))
        ids = {row["tmdb_id"] for row in current["cards"]}
        assert not seen.intersection(ids)
        seen.update(ids)
        if current["next_offset"] is None:
            break
        offset = current["next_offset"]
    assert len(seen) == 602
    assert run(svc.collection_page(2))["total"] == 0
    with pytest.raises(TVError):
        run(svc.collection_page(1, offset=-1))


def test_inventory_season_summary_requires_observed_seasons_not_total_counts(db):
    native = Native()
    svc = service(db, native)
    run(svc.detail(1, 42))
    empty = svc.card(1, svc._raw(1, 42))
    assert empty["plex_summary"] == "Availability unknown"
    assert empty["watch_status_label"] == "Not tracked"
    observed = run(svc.check(1, 42))
    assert observed["plex_summary"] == "1 season in Plex"
    assert observed["plex_season_ids"] == [1]
    assert observed["plex_indexed_episodes"] == 1
    assert not observed["season_coverage_known"]
    before = len(native.calls)
    svc.card(1, svc._raw(1, 42))
    assert len(native.calls) == before
    assert svc.card(2, svc._raw(1, 42))["plex_season_ids"] == []
    assert svc.card(2, svc._raw(1, 42))["plex_indexed_episodes"] == 0


def test_complete_released_set_excludes_future_but_never_unknown_dates(db):
    svc = service(db, Native())
    run(svc.check(1, 42))
    card = run(svc.season(1, 42, 1))
    assert card["plex_summary"] == "S1 complete"
    assert card["plex_season_coverage"] == [
        {"season_number": 1, "released_total": 1, "in_plex": 1, "state": "complete"}
    ]
    # When a released expected episode is absent, this becomes partial.
    db.execute(
        "UPDATE tv_episodes SET metadata_json=replace(metadata_json,'2099-01-01','2020-01-02') WHERE episode_number=2"
    )
    card = svc.card(1, svc._raw(1, 42))
    assert card["plex_summary"] == "S1 partial"
    assert card["plex_season_coverage"][0]["released_total"] == 2
    db.execute(
        "UPDATE tv_episodes SET metadata_json=replace(metadata_json,'2020-01-02','') WHERE episode_number=2"
    )
    assert not svc.card(1, svc._raw(1, 42))["season_coverage_known"]


def test_stale_scope_cannot_reuse_season_or_playback_claims(db):
    svc = service(db, Native())
    run(svc.check(1, 42))
    db.execute("UPDATE tv_plex_snapshots SET source_scope='old-scope'")
    card = svc.card(1, svc._raw(1, 42))
    assert card["plex_summary"] == "Last known in Plex"
    assert not card["plex_snapshot_current"]
    assert not card["season_coverage_known"] and card["plex_season_ids"] == []
    assert card["in_plex_episodes"] == 0 and card["plex_url"] is None
    assert card["watched_episodes"] == 1


def test_distinct_observed_seasons_include_specials_without_completion_claim(db):
    svc = service(db, Native())
    run(svc.check(1, 42))
    db.execute(
        "INSERT INTO tv_episodes SELECT user_id,tmdb_id,0,episode_number,metadata_json,watched,watch_source,plex_present,updated_at FROM tv_episodes WHERE season_number=1"
    )
    db.execute(
        "INSERT INTO tv_episodes SELECT user_id,tmdb_id,2,episode_number,metadata_json,watched,watch_source,plex_present,updated_at FROM tv_episodes WHERE season_number=1"
    )
    card = svc.card(1, svc._raw(1, 42))
    assert card["plex_summary"] == "2 seasons + specials in Plex"
    assert card["plex_season_ids"] == [0, 1, 2]
    assert not card["season_coverage_known"]
