"""Cloud episode availability has finite work and explicit continuation."""

import asyncio
import re

import pytest

from omakase.plus import debrid_catalog as catalog
from omakase.plus import native_tv_test
from omakase.plus.debrid_library import DebridLibraryError
from tests.plus.test_native_tv_test import (
    LIBRARY_API,
    LIBRARY_PAGE,
    ORIGIN,
    TestClient,
    browser,
)


@pytest.fixture
def system(request):
    return request.getfixturevalue("native_system")


from tests.plus.test_native_tv_test import system as native_system  # noqa: E402, F401


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Library:
    def __init__(self, torrents):
        self.torrents = torrents
        self.active = self.peak = 0
        self.calls = []
        self.fail = set()

    async def inventory(self, token):
        return self.torrents, {}

    async def _torrent(self, token, record):
        self.calls.append(record)
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0)
            if record in self.fail:
                raise DebridLibraryError("fixture", 503)
            item = self.torrents[record]
            names = item.get("files", [item["filename"] + ".mkv"])
            return (
                {index: {"path": name} for index, name in enumerate(names)},
                dict.fromkeys(range(len(names)), "fixture"),
                item["hash"],
            )
        finally:
            self.active -= 1


@pytest.fixture
def binding(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(catalog, "account_token", lambda *_: "fixture-account")
    monkeypatch.setattr(
        catalog,
        "catalog_title",
        lambda db, scope, domain, key, season: catalog.CatalogTitle(
            domain, key, "Fixture Show", 2025, season
        ),
    )
    return SimpleNamespace(kind="personal", viewer_id=1)


@pytest.mark.anyio
async def test_24_individual_episodes_in_requested_season_31(binding):
    library = Library(
        {
            str(number): {
                "filename": f"Fixture Show S31E{number:02}",
                "hash": str(number),
                "status": "downloaded",
            }
            for number in range(1, 25)
        }
    )
    result = await catalog.tv_episode_ids(None, binding, "fixture", library, seasons=(31,))
    assert result.episodes == frozenset((31, number) for number in range(1, 25))
    assert result.partial_seasons == ()
    assert 1 < library.peak <= 4


@pytest.mark.anyio
async def test_large_season_advances_in_bounded_batches_and_deduplicates(binding):
    library = Library(
        {
            str(number): {
                "filename": f"Fixture Show S01E{number:02}",
                "hash": str(number),
                "status": "downloaded",
            }
            for number in range(1, 62)
        }
    )
    library.torrents["pack"] = {
        "filename": "Fixture Show S01",
        "hash": "pack",
        "status": "downloaded",
        "files": ["Fixture Show S01E01.mkv", "Fixture Show S01E62.mkv"],
    }
    first = await catalog.tv_episode_ids(None, binding, "fixture", library, seasons=(1,))
    assert first.partial_seasons == (1,)
    assert 0 < len(library.calls) <= 48
    second = await catalog.tv_episode_ids(None, binding, "fixture", library, seasons=(1,))
    assert second.partial_seasons == ()
    assert second.episodes == frozenset((1, number) for number in range(1, 63))
    assert len(library.calls) == 62
    assert library.peak <= 4


@pytest.mark.anyio
async def test_transient_error_leaves_availability_partial_and_retryable(binding):
    library = Library(
        {
            "one": {"filename": "Fixture Show S01E01", "hash": "one", "status": "downloaded"},
            "two": {"filename": "Fixture Show S01E02", "hash": "two", "status": "downloaded"},
        }
    )
    library.fail.add("two")
    first = await catalog.tv_episode_ids(None, binding, "fixture", library, seasons=(1,))
    assert first.episodes == {(1, 1)}
    assert first.partial_seasons == (1,)
    library.fail.clear()
    second = await catalog.tv_episode_ids(None, binding, "fixture", library, seasons=(1,))
    assert second.episodes == {(1, 1), (1, 2)}
    assert second.partial_seasons == ()
    assert library.calls == ["one", "two", "two"]


def test_episode_route_requests_the_page_season_and_marks_unscanned_items(system, monkeypatch):
    app, db, sessions, _ = system
    monkeypatch.setenv("OMAKASE_NATIVE_TV_LIBRARY", "1")
    monkeypatch.setattr(native_tv_test, "playback_provider", lambda *_: "realdebrid")

    async def availability(db, scope, key, library, *, seasons):
        assert scope.viewer_id == 1 and key == "42"
        assert seasons == (31,)
        return catalog.EpisodeAvailability(frozenset({(31, 1)}), (31,))

    monkeypatch.setattr(native_tv_test, "debrid_tv_episode_ids", availability)
    db.execute(
        """INSERT INTO tv_catalog VALUES
           (1,42,'{"title":"Fixture series","in_collection":true}','fixture')"""
    )
    db.executemany(
        """INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,metadata_json,updated_at)
           VALUES(1,42,?,?,'{}','fixture')""",
        [(1, 1), (31, 1), (31, 2)],
    )
    db.commit()
    with TestClient(app, base_url=ORIGIN) as native:
        owner, csrf = browser(app, sessions[1])
        page = owner.post(LIBRARY_PAGE, data={"csrf_token": csrf, "action": "issue"})
        link = re.search(r"#library-([A-F0-9]{20})", page.text)[1]
        native.headers["Authorization"] = (
            "Bearer " + native.post(LIBRARY_API + "/redeem", json={"code": link}).json()["token"]
        )
        reply = native.get(LIBRARY_API + "/episodes?kind=tv&title_id=42&season=31").json()
    assert reply["availability"] == {"state": "partial", "seasons": [31], "retry_offset": 0}
    playable, unknown = reply["items"]
    assert playable["watch_path"].endswith("/42/31/1")
    assert unknown["watch_path"] is None and unknown["availability"] == "partial"
    assert reply["next_offset"] is None
