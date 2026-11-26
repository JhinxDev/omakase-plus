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


def test_stale_requested_movie_escalates_with_original_title_and_tiered_selection(
    request_db, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from omakase.plus.acquisition_policy import AcquisitionPolicy

    stale_ts = (datetime.now(UTC) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    fresh_ts = (datetime.now(UTC) - timedelta(minutes=20)).isoformat().replace("+00:00", "Z")
    request_db.execute(
        "UPDATE movie_catalog SET original_title='Le Dîner de Cons', year=1998 WHERE imdb_id='tt0000001'"
    )
    request_db.execute(
        "UPDATE movie_requests SET code='requested', created_at=? WHERE imdb_id='tt0000001'",
        (fresh_ts,),
    )
    monkeypatch.setattr(
        movie_fallback,
        "load_user_acquisition_policy",
        lambda *_: AcquisitionPolicy(
            allowed_resolutions=("1080p", "2160p"), minimum_seeders=10, preferred_seeders=20
        ),
    )
    cam = Candidate(
        "1998 Le Diner de Cons 1080p HDCAM", seeders=80, magnet="magnet:?xt=urn:btih:" + "1" * 40
    )
    clean_sd = Candidate(
        "1998 Fixture Film [EngSub]", seeders=2, magnet="magnet:?xt=urn:btih:" + "2" * 40
    )
    alt_hd = Candidate(
        "Le Diner de Cons 1998 1080p", seeders=4, magnet="magnet:?xt=urn:btih:" + "3" * 40
    )

    provider = Provider()
    # Fresh requested movie (< 2h) waits for Riven first.
    assert run(request_db, provider, [alt_hd]) is None
    assert not provider.added

    # Stale (> 2h) escalates using original_title and tiered fallback (low-seeder HD > clean SD, never CAM).
    request_db.execute(
        "UPDATE movie_requests SET created_at=? WHERE imdb_id='tt0000001'", (stale_ts,)
    )
    request_db.commit()

    class AltProvider(Provider):
        async def get_torrent(self, item_id, *, require_definite_absence):
            return RDTorrent(
                item_id,
                "Le.Diner.de.Cons.1998.1080p",
                "waiting_files_selection",
                0,
                1234,
                0,
                (),
                (RDFile(9, "/Le.Diner.de.Cons.1998.1080p.mkv", 1234, False),),
                self.content_hash,
            )

    alt_provider = AltProvider()
    assert run(request_db, alt_provider, [cam, clean_sd, alt_hd]) == "selected"
    assert alt_provider.added == ["magnet:?xt=urn:btih:" + "3" * 40]
    assert alt_provider.selection == [("fixture123", "9")]

    # Tier 3 fallback accepts clean SD when no HD/4K exists, still rejecting CAM.
    title_obj = movie_fallback.CatalogTitle("movie", "tt0000001", "Fixture Film", 1998)
    assert (
        movie_fallback._choose(
            [cam, clean_sd], title_obj, movie_fallback.load_user_acquisition_policy(request_db, 1)
        )
        == "2" * 40
    )


def test_select_movie_file_handles_single_and_main_feature_torrents():
    from omakase.plus import debrid_catalog

    chouchou = movie_fallback.CatalogTitle("movie", "tt0328955", "Chouchou", 2003)
    assert (
        debrid_catalog.select_movie_file(
            chouchou,
            "2003 Chouchou (FRA) [EngSub]",
            [(1, "/chouchou.avi", 730_310_838)],
        )
        == 1
    )
    assert (
        debrid_catalog.select_movie_file(
            chouchou,
            "2003 Chouchou (FRA) [EngSub]",
            [
                (1, "/chouchou_trailer.mkv", 25 * 1024 * 1024),
                (2, "/feature.mkv", 1_400 * 1024 * 1024),
                (3, "/bonus.mkv", 90 * 1024 * 1024),
            ],
        )
        == 2
    )
    assert (
        debrid_catalog.select_movie_file(
            chouchou,
            "2003 Chouchou (FRA) [EngSub]",
            [
                (1, "/disc1.mkv", 700 * 1024 * 1024),
                (2, "/disc2.mkv", 700 * 1024 * 1024),
            ],
        )
        is None
    )


def test_movie_fallback_retries_next_candidate_after_file_mismatch(request_db, monkeypatch):
    from omakase.plus.debrid_library import account_binding
    from omakase.plus.realdebrid import RDFile, RDTorrent

    monkeypatch.setattr(
        movie_fallback.playback_providers, "select", lambda *_: SimpleNamespace(cloud_route=True)
    )
    monkeypatch.setattr(movie_fallback, "account_token", lambda *_: "fixture-token")
    monkeypatch.setattr(movie_fallback.request_candidates, "acquire_enabled", lambda: True)
    library = Inventory()
    candidates = [
        SimpleNamespace(
            title="Fixture Film 2025 1080p BluRay",
            magnet=f"magnet:?xt=urn:btih:{'1' * 40}",
            seeders=12,
            size_bytes=2_000_000_000,
        ),
        SimpleNamespace(
            title="Fixture Film 2025 1080p WEB",
            magnet=f"magnet:?xt=urn:btih:{'2' * 40}",
            seeders=8,
            size_bytes=1_800_000_000,
        ),
    ]

    class SingleProvider:
        def __init__(self, tid: str, torrent: RDTorrent):
            self.tid = tid
            self.torrent = torrent

        async def add_magnet(self, _magnet: str) -> str:
            return self.tid

        async def get_torrent(self, _tid: str, *, require_definite_absence: bool = False):
            return self.torrent

        async def select_files(self, _tid: str, _files: str) -> bool:
            return True

    provider_a = SingleProvider(
        "tid1",
        RDTorrent(
            id="tid1",
            filename="Fixture Film 2025 1080p BluRay",
            status="waiting_files_selection",
            progress=0.0,
            bytes_total=2_000_000_000,
            bytes_done=0,
            links=(),
            files=(
                RDFile(1, "/part1.mkv", 700_000_000, False),
                RDFile(2, "/part2.mkv", 700_000_000, False),
            ),
            torrent_hash="1" * 40,
        ),
    )
    first = asyncio.run(
        movie_fallback.advance(
            request_db,
            1,
            "tt0000001",
            candidates,
            library=library,
            provider=provider_a,
            next_attempt=True,
        )
    )
    assert first == "file_mismatch"

    provider_b = SingleProvider(
        "tid2",
        RDTorrent(
            id="tid2",
            filename="Fixture Film 2025 1080p WEB",
            status="waiting_files_selection",
            progress=0.0,
            bytes_total=1_800_000_000,
            bytes_done=0,
            links=(),
            files=(RDFile(5, "/Fixture Film 2025 1080p WEB.mkv", 1_800_000_000, False),),
            torrent_hash="2" * 40,
        ),
    )
    second = asyncio.run(
        movie_fallback.advance(
            request_db,
            1,
            "tt0000001",
            candidates,
            library=library,
            provider=provider_b,
            next_attempt=True,
        )
    )
    assert second == "selected"
    rows = request_db.execute(
        "SELECT torrent_hash, state FROM movie_fallback_transfers WHERE account_binding=? ORDER BY id",
        (account_binding("fixture-token"),),
    ).fetchall()
    assert [(r["torrent_hash"], r["state"]) for r in rows] == [
        ("1" * 40, "file_mismatch"),
        ("2" * 40, "selected"),
    ]


def test_overdue_movie_fallback_marks_stalled_and_request_status_copy(request_db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from omakase.plus import request_status
    from omakase.plus.debrid_library import account_binding
    from omakase.plus.realdebrid import RDFile, RDTorrent

    monkeypatch.setattr(
        movie_fallback.playback_providers, "select", lambda *_: SimpleNamespace(cloud_route=True)
    )
    monkeypatch.setattr(movie_fallback, "account_token", lambda *_: "fixture-token")
    monkeypatch.setattr(movie_fallback.request_candidates, "acquire_enabled", lambda: True)
    stale_time = (datetime.now(UTC) - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    request_db.execute(
        """INSERT INTO movie_fallback_transfers(
           user_id,imdb_id,request_created_at,account_binding,torrent_hash,torrent_id,state,created_at)
           VALUES(1,'tt0000001','2026-01-01T00:00:00Z',?,?,?,?,?)""",
        (account_binding("fixture-token"), "3" * 40, "tid3", "selected", stale_time),
    )
    request_db.commit()

    class DownloadingProvider:
        async def get_torrent(self, _tid: str, *, require_definite_absence: bool = False):
            return RDTorrent(
                id="tid3",
                filename="Fixture Film 2025 1080p",
                status="downloading",
                progress=12.0,
                bytes_total=1_000_000,
                bytes_done=120_000,
                links=(),
                files=(RDFile(1, "/Fixture Film 2025 1080p.mkv", 1_000_000, True),),
                torrent_hash="3" * 40,
            )

    state = asyncio.run(
        movie_fallback.advance(
            request_db, 1, "tt0000001", library=Inventory(), provider=DownloadingProvider()
        )
    )
    assert state == "stalled"
    assert movie_fallback.is_fallback_eligible(
        {"code": "acknowledgement_unconfirmed", "created_at": "2026-01-01T00:00:00Z"}
    )
    model = {
        "kind": "movie",
        "state": "requested",
        "category": "active",
        "status_label": "Searching",
        "unknown_reason": False,
        "provider_confirmed": True,
        "fallback_search": {"eligible": 2},
        "fallback_transfer": {"state": "file_mismatch", "headline": "Mismatch"},
        "stages": [],
        "reason_text": "",
    }
    request_status.decorate_display(model)
    assert "checking alternatives" in model["display_detail"]
    assert "checking alternatives" in model["display_subline"]
    assert "Queuing cloud transfer" not in model["display_detail"]
