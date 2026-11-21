import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from omakase.plus import debrid_catalog, request_candidates, tv_fallback, tv_provider_requests
from omakase.plus.db import run_migrations
from omakase.plus.debrid_catalog import CatalogTitle, PublishedEpisode
from omakase.plus.debrid_library import CloudSource, DebridLibraryError, account_binding
from omakase.plus.realdebrid import RDFile, RDTorrent
from tests.plus.test_request_candidates import Candidate

FIXTURE_ACCOUNT = "synthetic-fixture-account"
HASH = "A" * 40
TITLE = "Fixture Show"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def request_db(monkeypatch):
    with sqlite3.connect(":memory:") as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        run_migrations(db)
        db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'viewer@fixture.test','x')")
        db.execute(
            "INSERT INTO for_you_requests(user_id,kind,title_id,title) VALUES(1,'tv',77,?)",
            (TITLE,),
        )
        tv_provider_requests.remember(db, 1, 77, [3])
        monkeypatch.setenv("OMAKASE_REQUEST_ACQUIRE", "1")
        monkeypatch.setenv("OMAKASE_REQUEST_SEARCH", "1")
        monkeypatch.setattr(tv_fallback, "account_token", lambda *_: FIXTURE_ACCOUNT)
        monkeypatch.setattr(
            tv_fallback.playback, "select", lambda *_: SimpleNamespace(cloud_route=True)
        )
        monkeypatch.setattr(tv_fallback, "personal_scope", lambda *_: None)
        monkeypatch.setattr(
            tv_fallback,
            "catalog_title",
            lambda *args: CatalogTitle("tv", "77", TITLE, 2025, *args[-2:]),
        )
        yield db


def _snapshot(*episodes):
    return {
        "library": {
            "provider": "Real-Debrid",
            "binding": account_binding(FIXTURE_ACCOUNT),
            "episodes": [],
        },
        "seasons": [
            {
                "season": 3,
                "episodes": [
                    {"episode": item, "fallback_eligible": True, "status": "Looking for episodes"}
                    for item in episodes
                ],
            }
        ],
    }


class FixtureInventory:
    async def fresh_torrents(self, token):
        assert token == FIXTURE_ACCOUNT
        return {}


class FixtureProvider:
    def __init__(self, filename, *, uncertain=False):
        self.filename = filename
        self.uncertain = uncertain
        self.added = 0
        self.selected = []

    async def add_magnet(self, magnet):
        assert magnet == f"magnet:?xt=urn:btih:{HASH}"
        self.added += 1
        if self.uncertain:
            raise TimeoutError
        return "fixture-received".replace("-", "")

    async def get_torrent(self, item_id, *, require_definite_absence):
        assert item_id == "fixturereceived" and require_definite_absence
        ready = bool(self.selected)
        return RDTorrent(
            item_id,
            "Fixture Show S03E01 1080p",
            "downloaded" if ready else "waiting_files_selection",
            100 if ready else 0,
            1234,
            0,
            ("fixture",) if ready else (),
            (RDFile(4, self.filename, 1234, ready),),
            HASH,
        )

    async def select_files(self, item_id, files):
        self.selected.append((item_id, files))
        return True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("filename", "uncertain", "expected", "may_select"),
    [
        ("/Fixture Show S03E01 1080p.mkv", False, "downloaded", True),
        ("/Fixture Show S03E02 1080p.mkv", False, "file_mismatch", False),
        ("/Fixture Show S03E01 1080p.mkv", True, "submission_unknown", False),
    ],
)
async def test_enqueue_once_and_select_only_exact_episode(
    request_db, filename, uncertain, expected, may_select
):
    db = request_db
    tv_provider_requests._write_snapshot(db, 1, 77, _snapshot(1))
    provider = FixtureProvider(filename, uncertain=uncertain)
    candidate = Candidate(
        f"{TITLE} S03E01 1080p", magnet=f"magnet:?xt=urn:btih:{HASH}", size_bytes=1234
    )
    for _ in range(3):
        await tv_fallback.submit(
            db, 1, 77, 3, 1, [candidate], library=FixtureInventory(), provider=provider
        )
        await tv_fallback.advance_pending(db, 1, 77, provider=provider)
    assert tv_fallback.receipts(db, 1, 77, account_binding(FIXTURE_ACCOUNT)) == {(3, 1): expected}
    assert not tv_fallback.receipts(db, 1, 77, account_binding("another-account"))
    assert provider.added == 1
    assert provider.selected == ([("fixturereceived", "4")] if may_select else [])


@pytest.mark.anyio
async def test_stalled_search_moves_to_next_missing_episode(request_db, monkeypatch):
    db = request_db
    tv_provider_requests._write_snapshot(db, 1, 77, _snapshot(1, 2))
    first = []

    async def search(_db, _user_id, kind, _key, request, **kwargs):
        assert kind == "tv" and kwargs["force"]
        first.append((request.season, request.episode))
        return {"outcome": "complete"}

    monkeypatch.setattr(request_candidates, "search_enabled", lambda: True)
    monkeypatch.setattr(request_candidates, "refresh", search)
    service = SimpleNamespace(_raw=lambda *_: {"title": TITLE})

    async def run():
        current = tv_provider_requests.read(db, 1, 77)["snapshot"]
        await tv_provider_requests.search_missing_episode(db, 1, 77, service, current)

    await run()
    await run()
    db.execute("""UPDATE tv_provider_requests SET snapshot_json=json_set(
        snapshot_json,'$.fallback_checks.latest','2025-01-01T00:00:00+00:00')""")
    db.commit()
    tv_provider_requests._write_snapshot(db, 1, 77, _snapshot(1, 2))
    await run()
    assert first == [(3, 1), (3, 2)]


def test_only_stale_attempted_aired_episodes_are_eligible():
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    old = {"aired_at": "2026-10-01", "scraped_at": "2026-10-10T09:00:00Z", "scraped_times": 2}
    assert tv_provider_requests._fallback_eligible(old, "Looking for episodes", now)
    assert not tv_provider_requests._fallback_eligible(old, "Transferring", now)
    assert not tv_provider_requests._fallback_eligible(
        {**old, "scraped_times": 1}, "Looking for episodes", now
    )
    assert not tv_provider_requests._fallback_eligible(
        {**old, "aired_at": "2026-10-11"}, "No release found", now
    )


class PublishedLibrary:
    def __init__(self, *, hash_value=HASH, selected=True):
        self.hash_value = hash_value
        self.selected = selected

    async def inventory(self, token):
        assert token == FIXTURE_ACCOUNT
        return {"abc": {"status": "downloaded", "hash": self.hash_value, "filename": "opaque"}}, {}

    async def _torrent(self, token, record):
        assert token == FIXTURE_ACCOUNT and record == "abc"
        files = {4: {"path": "/opaque/unknown.mkv", "bytes": 1234}}
        return files, {4: "synthetic"} if self.selected else {}, self.hash_value

    async def torrent_source(self, token, record, file_id, *, inventory):
        assert (token, record, file_id) == (FIXTURE_ACCOUNT, "abc", 4) and self.selected
        return CloudSource(
            "unknown.mkv",
            "synthetic-identity",
            "synthetic-version",
            20,
            account_binding(FIXTURE_ACCOUNT),
            "",
        )


@pytest.mark.anyio
async def test_fallback_does_not_reacquire_a_watched_episode(request_db):
    db = request_db
    tv_provider_requests._write_snapshot(db, 1, 77, _snapshot(1))
    db.execute("INSERT INTO tv_catalog VALUES(1,77,'{}','2026-01-01T00:00:00Z')")
    db.execute(
        """INSERT INTO tv_episodes(user_id,tmdb_id,season_number,episode_number,
               metadata_json,watched,updated_at) VALUES(1,77,3,1,'{}',1,'2026-01-01T00:00:00Z')"""
    )
    provider = FixtureProvider("/Fixture Show S03E01 1080p.mkv")
    candidate = Candidate(
        f"{TITLE} S03E01 1080p", magnet=f"magnet:?xt=urn:btih:{HASH}", size_bytes=1234
    )
    await tv_fallback.submit(
        db, 1, 77, 3, 1, [candidate], library=FixtureInventory(), provider=provider
    )
    assert provider.added == 0


def test_tv_search_evidence_explains_the_resolution_filter():
    request = request_candidates.JudgeRequest(
        title=TITLE, kind="tv", season=3, episode=1, allowed_resolutions=("1080p",)
    )
    release = Candidate(
        f"{TITLE} S03E01 720p", magnet=f"magnet:?xt=urn:btih:{HASH}", size_bytes=1234
    )
    assert request_candidates.judge(release, request).code == "quality_excluded"


@pytest.mark.anyio
async def test_published_match_requires_saved_account_bound_hash_and_exact_selected_file(
    request_db, monkeypatch
):
    db = request_db
    scope = SimpleNamespace(kind="personal", viewer_id=1, operational_owner_id=1)
    monkeypatch.setattr(debrid_catalog, "account_token", lambda *_: FIXTURE_ACCOUNT)
    monkeypatch.setattr(
        debrid_catalog,
        "catalog_title",
        lambda _db, _scope, domain, key, season, episode=0: CatalogTitle(
            domain, key, TITLE, 2025, season, episode
        ),
    )
    snapshot = {
        **_snapshot(1),
        "published_files": {
            "binding": account_binding(FIXTURE_ACCOUNT),
            "episodes": [[3, 1, HASH, "opaque", "unknown.mkv"]],
        },
    }
    tv_provider_requests._write_snapshot(db, 1, 77, snapshot)
    library = PublishedLibrary()
    availability = await debrid_catalog.tv_episode_ids(db, scope, "77", library, seasons=(3,))
    assert availability.episodes == {(3, 1)}
    source, _ = await debrid_catalog.resolve_catalog(db, scope, "tv", "77", 3, 1, library)
    assert source.locator[1] == PublishedEpisode(HASH, "opaque", "unknown.mkv")
    for invalid in [PublishedLibrary(hash_value="B" * 40), PublishedLibrary(selected=False)]:
        assert not (
            await debrid_catalog.tv_episode_ids(db, scope, "77", invalid, seasons=(3,))
        ).episodes
        assert await debrid_catalog.resolve_catalog(db, scope, "tv", "77", 3, 1, invalid) is None
    snapshot["published_files"]["binding"] = account_binding("different-account")
    db.execute("UPDATE tv_provider_requests SET snapshot_json=?", (json.dumps(snapshot),))
    with pytest.raises(DebridLibraryError, match="no longer available"):
        debrid_catalog.revalidate_catalog(db, source)
