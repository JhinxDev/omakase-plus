import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from omakase.plus import movie_fallback, movies_store
from omakase.plus.db import run_migrations
from omakase.plus.realdebrid import RDFile, RDTorrent
from tests.plus.test_request_candidates import Candidate


@pytest.fixture
def request_db(monkeypatch):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'viewer@fixture.test','x')")
    movies_store.upsert_movie(
        db,
        1,
        {"imdb_id": "tt0000001", "tmdb_id": 77, "title": "Fixture Film", "year": 2025},
        now="2026-01-01T00:00:00Z",
    )
    movies_store.ensure_request(db, 1, "tt0000001", now="2026-01-01T00:00:00Z", edition=None)
    db.execute("UPDATE movie_requests SET code='no_eligible_results'")
    monkeypatch.setenv("OMAKASE_REQUEST_ACQUIRE", "1")
    monkeypatch.setattr(movie_fallback, "account_token", lambda *_: "fixture-token")
    monkeypatch.setattr(
        movie_fallback.playback_providers, "select", lambda *_: SimpleNamespace(cloud_route=True)
    )
    yield db
    db.close()


class Provider:
    def __init__(self, *, ambiguous=False, uncertain=False):
        self.ambiguous = ambiguous
        self.uncertain = uncertain
        self.added = []
        self.selection = []
        self.content_hash = ""

    async def add_magnet(self, magnet):
        self.added.append(magnet)
        self.content_hash = magnet.split(":")[-1]
        if self.uncertain:
            raise TimeoutError
        return "fixture123"

    async def get_torrent(self, item_id, *, require_definite_absence):
        assert item_id == "fixture123"
        assert require_definite_absence
        files = [RDFile(4, "/Fixture Film 2025 2160p.mkv", 1234, bool(self.selection))]
        if self.ambiguous:
            files.append(RDFile(5, "/Fixture Film 2025 1080p.mkv", 1000, False))
        return RDTorrent(
            item_id,
            "Fixture Film 2025",
            "downloaded" if self.selection else "waiting_files_selection",
            100 if self.selection else 0,
            1234,
            0,
            ("fixture-link",) if self.selection else (),
            tuple(files),
            self.content_hash,
        )

    async def select_files(self, item_id, files):
        self.selection.append((item_id, files))
        return True


class Inventory:
    async def fresh_torrents(self, token):
        assert token == "fixture-token"
        return {}


def run(db, provider, candidates=()):
    return asyncio.run(
        movie_fallback.advance(
            db, 1, "tt0000001", candidates, library=Inventory(), provider=provider
        )
    )


def offers():
    return (
        Candidate("Fixture Film 2025 1080p", magnet="magnet:?xt=urn:btih:" + "a" * 40),
        Candidate("Fixture Film 2025 2160p", magnet="magnet:?xt=urn:btih:" + "b" * 40),
    )


def test_uses_single_exact_file_and_advances_existing_provider_item(request_db):
    provider = Provider()
    assert run(request_db, provider, offers()) == "selected"
    assert run(request_db, provider) == "downloaded"
    assert run(request_db, provider, offers()) == "downloaded"
    assert provider.added == ["magnet:?xt=urn:btih:" + "B" * 40]
    assert provider.selection == [("fixture123", "4")]


@pytest.mark.parametrize(("ambiguous", "uncertain"), [(True, False), (False, True)])
def test_ambiguous_side_effect_does_not_send_another_transfer(request_db, ambiguous, uncertain):
    provider = Provider(ambiguous=ambiguous, uncertain=uncertain)
    expected = "file_mismatch" if ambiguous else "submission_unknown"
    assert run(request_db, provider, offers()) == expected
    assert run(request_db, provider, offers()) == expected
    assert len(provider.added) == 1
    assert provider.selection == []


def test_a_resumed_riven_request_prevents_fallback_submission(request_db):
    request_db.execute("UPDATE movie_requests SET code='acquiring'")
    provider = Provider()
    assert run(request_db, provider, offers()) is None
    assert not provider.added
