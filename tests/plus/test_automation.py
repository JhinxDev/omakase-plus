"""Tests for the Plan → Nyaa → Real-Debrid automation pipeline."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from omakase.plus.nyaa import NyaaTorrent


def test_automation_module_imports():
    """Verify the automation module can be imported."""
    from omakase.plus import automation

    assert hasattr(automation, "search_and_download")


def test_db_fixture_works():
    """Verify test database setup works."""
    tmp = tempfile.mkdtemp()
    db_path = Path(tmp) / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE user_secrets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            key_name TEXT NOT NULL,
            encrypted_value TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(user_id, key_name)
        )"""
    )
    conn.execute(
        "INSERT INTO user_secrets (user_id, key_name, encrypted_value) VALUES (?, ?, ?)",
        (1, "realdebrid_api_key", "encrypted-fake-key"),
    )
    conn.commit()
    rows = conn.execute("SELECT * FROM user_secrets WHERE user_id = 1").fetchall()
    assert len(rows) == 1
    assert rows[0]["key_name"] == "realdebrid_api_key"
    conn.close()


def test_search_and_download_uses_title_level_batch_preference():
    """Plus Download should prefer a complete high-quality batch over one episode."""
    from omakase.plus.automation import search_and_download

    batch = NyaaTorrent(
        title="[SubsPlease] Frieren - 01-28 Complete [1080p][WEB-DL][AAC]",
        magnet="magnet:?xt=urn:btih:BATCH",
        seeders=80,
        leechers=5,
        size_bytes=28_000_000_000,
        size_display="26.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    single_episode = NyaaTorrent(
        title="[GoodGroup] Frieren - 01 [1080p][WEB-DL]",
        magnet="magnet:?xt=urn:btih:SINGLE",
        seeders=100,
        leechers=5,
        size_bytes=1_400_000_000,
        size_display="1.3 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=False,
    )

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            assert magnet == batch.magnet
            return "rd-batch"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            assert torrent_id == "rd-batch"
            assert files == "all"
            return True

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Frieren"
        assert trusted_only is False
        return [single_episode, batch]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Frieren"))

    assert result["status"] == "ok"
    assert result["rd_id"] == "rd-batch"
    assert result["magnet"] == batch.magnet
    assert result["torrent_title"] == batch.title
    assert result["size"] == batch.size_display


def test_search_and_download_tries_aliases_until_torrents_rank():
    """Resolved AniList title aliases should be searched in order before giving up."""
    from omakase.plus.automation import search_and_download

    alias_batch = NyaaTorrent(
        title="[GoodGroup] Golden Kamuy 3rd Season Complete [1080p][HEVC]",
        magnet="magnet:?xt=urn:btih:ALIAS",
        seeders=64,
        leechers=4,
        size_bytes=12_000_000_000,
        size_display="11.2 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"searches": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            assert magnet == alias_batch.magnet
            return "rd-alias"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            assert torrent_id == "rd-alias"
            assert files == "all"
            return True

    async def fake_search(title: str, trusted_only: bool = False):
        calls["searches"].append(title)
        assert trusted_only is False
        if title == "Golden Kamuy Season 3":
            return []
        if title == "Golden Kamuy 3rd Season":
            return [alias_batch]
        raise AssertionError(f"unexpected alias search: {title}")

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(
            search_and_download(
                db=None,
                user_id=1,
                title="Golden Kamuy Season 3",
                search_titles=["Golden Kamuy Season 3", "Golden Kamuy 3rd Season"],
            )
        )

    assert result["status"] == "ok"
    assert result["rd_id"] == "rd-alias"
    assert result["torrent_title"] == alias_batch.title
    assert calls["searches"] == ["Golden Kamuy Season 3", "Golden Kamuy 3rd Season"]


def test_search_and_download_falls_back_when_top_candidate_is_rejected_by_rd():
    """RD can reject one hash; Download should try the next ranked candidate."""
    from omakase.plus.automation import search_and_download

    rejected = NyaaTorrent(
        title="[Judas] Bleach - 001-366 Complete [BD 1080p][x265][10bit]",
        magnet="magnet:?xt=urn:btih:RD451",
        seeders=120,
        leechers=5,
        size_bytes=72_000_000_000,
        size_display="67.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    accepted = NyaaTorrent(
        title="[GoodGroup] Bleach - 001-366 Complete [1080p][WEB-DL]",
        magnet="magnet:?xt=urn:btih:BACKUP",
        seeders=70,
        leechers=5,
        size_bytes=60_000_000_000,
        size_display="55.9 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            if magnet == rejected.magnet:
                return None
            return "rd-backup"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            assert torrent_id == "rd-backup"
            assert files == "all"
            return True

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Bleach"
        assert trusted_only is False
        return [accepted, rejected]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Bleach"))

    assert result["status"] == "ok"
    assert result["rd_id"] == "rd-backup"
    assert result["magnet"] == accepted.magnet
    assert result["torrent_title"] == accepted.title
    assert result["seeders"] == accepted.seeders
    assert result["size"] == accepted.size_display
    assert calls["magnets"] == [rejected.magnet, accepted.magnet]


def test_search_and_download_falls_back_after_provider_block():
    """A provider-blocked RD hash should not stop trying other ranked candidates."""
    from omakase.plus.automation import search_and_download
    from omakase.plus.realdebrid import RealDebridProviderBlock

    blocked = NyaaTorrent(
        title="[Judas] Bleach - 001-366 Complete [BD 1080p][x265][10bit]",
        magnet="magnet:?xt=urn:btih:RD451",
        seeders=120,
        leechers=5,
        size_bytes=72_000_000_000,
        size_display="67.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    accepted = NyaaTorrent(
        title="[GoodGroup] Bleach - 001-366 Complete [1080p][WEB-DL]",
        magnet="magnet:?xt=urn:btih:BACKUP",
        seeders=70,
        leechers=5,
        size_bytes=60_000_000_000,
        size_display="55.9 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            if magnet == blocked.magnet:
                raise RealDebridProviderBlock(
                    http_status=451,
                    error_code="infringing_file",
                    detail="Real-Debrid blocked this torrent hash.",
                )
            return "rd-backup"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            assert torrent_id == "rd-backup"
            return True

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Bleach"
        assert trusted_only is False
        return [accepted, blocked]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Bleach"))

    assert result["status"] == "ok"
    assert result["rd_id"] == "rd-backup"
    assert result["torrent_title"] == accepted.title
    assert calls["magnets"] == [blocked.magnet, accepted.magnet]


def test_search_and_download_falls_back_without_unguarded_rd_cleanup():
    """An unselectable RD item remains until the guarded replacement policy can act."""
    from omakase.plus.automation import search_and_download

    unselectable = NyaaTorrent(
        title="[Judas] Frieren - 01-28 Complete [BD 1080p][x265][10bit]",
        magnet="magnet:?xt=urn:btih:UNSELECTABLE",
        seeders=120,
        leechers=5,
        size_bytes=28_000_000_000,
        size_display="26.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    accepted = NyaaTorrent(
        title="[GoodGroup] Frieren - 01-28 Complete [1080p][WEB-DL]",
        magnet="magnet:?xt=urn:btih:BACKUP",
        seeders=70,
        leechers=5,
        size_bytes=24_000_000_000,
        size_display="22.4 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"deleted": [], "magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            if magnet == unselectable.magnet:
                return "rd-unselectable"
            return "rd-backup"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            assert files == "all"
            return torrent_id == "rd-backup"

        async def delete_torrent(self, torrent_id: str) -> bool:
            calls["deleted"].append(torrent_id)
            return True

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Frieren"
        assert trusted_only is False
        return [accepted, unselectable]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Frieren"))

    assert result["status"] == "ok"
    assert result["rd_id"] == "rd-backup"
    assert result["magnet"] == accepted.magnet
    assert calls["magnets"] == [unselectable.magnet, accepted.magnet]
    assert calls["deleted"] == []


def test_search_and_download_returns_rd_error_when_all_ranked_candidates_are_rejected():
    """If every ranked candidate fails RD, do not claim the download started."""
    from omakase.plus.automation import search_and_download

    first = NyaaTorrent(
        title="[Judas] Bleach - 001-366 Complete [BD 1080p][x265][10bit]",
        magnet="magnet:?xt=urn:btih:RD451A",
        seeders=120,
        leechers=5,
        size_bytes=72_000_000_000,
        size_display="67.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    second = NyaaTorrent(
        title="[GoodGroup] Bleach - 001-366 Complete [1080p][WEB-DL]",
        magnet="magnet:?xt=urn:btih:RD451B",
        seeders=70,
        leechers=5,
        size_bytes=60_000_000_000,
        size_display="55.9 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            return None

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Bleach"
        assert trusted_only is False
        return [second, first]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Bleach"))

    assert result["status"] == "rd_error"
    assert "all candidate torrents" in result["detail"].lower()
    assert calls["magnets"] == [first.magnet, second.magnet]


def test_search_and_download_returns_provider_block_when_all_batches_are_blocked():
    """All-blocked title-level batches should surface provider-blocked status."""
    from omakase.plus.automation import search_and_download
    from omakase.plus.realdebrid import RealDebridProviderBlock

    batch = NyaaTorrent(
        title="[EMBER] Bleach: Thousand-Year Blood War - The Conflict (Batch) [1080p]",
        magnet="magnet:?xt=urn:btih:BATCH451",
        seeders=80,
        leechers=5,
        size_bytes=5_200_000_000,
        size_display="4.9 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    episode = NyaaTorrent(
        title="[EMBER] Bleach: Thousand-Year Blood War - The Conflict - 32 [1080p]",
        magnet="magnet:?xt=urn:btih:EPISODE",
        seeders=70,
        leechers=5,
        size_bytes=390_000_000,
        size_display="371.9 MiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=False,
    )
    calls = {"magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            raise RealDebridProviderBlock(
                http_status=451,
                error_code="infringing_file",
                detail="Real-Debrid blocked this torrent hash.",
            )

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "BLEACH: Thousand-Year Blood War - The Conflict"
        assert trusted_only is False
        return [episode, batch]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(
            search_and_download(
                db=None,
                user_id=1,
                title="BLEACH: Thousand-Year Blood War - The Conflict",
            )
        )

    assert result["status"] == "rd_provider_block"
    assert result["http_status"] == 451
    assert result["error_code"] == "infringing_file"
    assert "provider blocked" in result["detail"].lower()
    assert calls["magnets"] == [batch.magnet]


def test_search_and_download_returns_rd_error_for_mixed_provider_and_generic_failures():
    """Provider-block status is only for candidates that all hit provider blocks."""
    from omakase.plus.automation import search_and_download
    from omakase.plus.realdebrid import RealDebridProviderBlock

    blocked = NyaaTorrent(
        title="[Judas] Bleach - 001-366 Complete [BD 1080p][x265][10bit]",
        magnet="magnet:?xt=urn:btih:RD451",
        seeders=120,
        leechers=5,
        size_bytes=72_000_000_000,
        size_display="67.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    rejected = NyaaTorrent(
        title="[GoodGroup] Bleach - 001-366 Complete [1080p][WEB-DL]",
        magnet="magnet:?xt=urn:btih:GENERIC",
        seeders=70,
        leechers=5,
        size_bytes=60_000_000_000,
        size_display="55.9 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            if magnet == blocked.magnet:
                raise RealDebridProviderBlock(
                    http_status=451,
                    error_code="infringing_file",
                    detail="Real-Debrid blocked this torrent hash.",
                )
            return None

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Bleach"
        assert trusted_only is False
        return [rejected, blocked]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Bleach"))

    assert result["status"] == "rd_error"
    assert "provider blocked" in result["detail"]
    assert calls["magnets"] == [blocked.magnet, rejected.magnet]


def test_search_and_download_does_not_fallback_from_batch_to_single_episode():
    """Title-level downloads should not degrade from a blocked batch to one episode."""
    from omakase.plus.automation import search_and_download

    batch = NyaaTorrent(
        title="[EMBER] Bleach: Thousand-Year Blood War - The Conflict (Batch) [1080p]",
        magnet="magnet:?xt=urn:btih:BATCH451",
        seeders=80,
        leechers=5,
        size_bytes=5_200_000_000,
        size_display="4.9 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )
    episode = NyaaTorrent(
        title="[EMBER] Bleach: Thousand-Year Blood War - The Conflict - 32 [1080p]",
        magnet="magnet:?xt=urn:btih:EPISODE",
        seeders=70,
        leechers=5,
        size_bytes=390_000_000,
        size_display="371.9 MiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=False,
    )
    calls = {"magnets": []}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            calls["magnets"].append(magnet)
            if magnet == batch.magnet:
                return None
            return "rd-episode"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            raise AssertionError("single-episode fallback should not be selected")

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "BLEACH: Thousand-Year Blood War - The Conflict"
        assert trusted_only is False
        return [episode, batch]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(
            search_and_download(
                db=None,
                user_id=1,
                title="BLEACH: Thousand-Year Blood War - The Conflict",
            )
        )

    assert result["status"] == "rd_error"
    assert calls["magnets"] == [batch.magnet]


def test_search_and_download_rejects_wrong_title_result():
    """Wrong-title search results should not be handed to Real-Debrid."""
    from omakase.plus.automation import search_and_download

    wrong_title = NyaaTorrent(
        title="[Trusted] Dungeon Meshi - 01-24 Complete [2160p][BluRay]",
        magnet="magnet:?xt=urn:btih:WRONG",
        seeders=500,
        leechers=5,
        size_bytes=55_000_000_000,
        size_display="51.2 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            raise AssertionError(f"wrong-title magnet should not be added: {magnet}")

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Frieren"
        assert trusted_only is False
        return [wrong_title]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Frieren"))

    assert result["status"] == "not_found"


def test_search_and_download_rejects_longer_title_for_short_generic_query():
    """A short title must not download a different longer title."""
    from omakase.plus.automation import search_and_download

    wrong_title = NyaaTorrent(
        title="[EMBER] Berserk of Gluttony (2023) (Boushoku no Berserk) [BD 1080p][HEVC]",
        magnet="magnet:?xt=urn:btih:GLUTTONY",
        seeders=500,
        leechers=5,
        size_bytes=24_000_000_000,
        size_display="22.4 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            raise AssertionError(f"wrong-title magnet should not be added: {magnet}")

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Berserk"
        assert trusted_only is False
        return [wrong_title]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Berserk"))

    assert result["status"] == "not_found"


def test_search_and_download_reports_select_files_failure_without_unguarded_cleanup():
    """A failed selection is reported without deleting an unproven RD item."""
    from omakase.plus.automation import search_and_download

    torrent = NyaaTorrent(
        title="[SubsPlease] Frieren - 01-28 Complete [1080p][WEB-DL][AAC]",
        magnet="magnet:?xt=urn:btih:BATCH",
        seeders=80,
        leechers=5,
        size_bytes=28_000_000_000,
        size_display="26.1 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=False,
        is_batch=True,
    )
    calls = {"deleted": False}

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            assert magnet == torrent.magnet
            return "rd-unselectable"

        async def select_files(self, torrent_id: str, files: str = "all") -> bool:
            assert torrent_id == "rd-unselectable"
            assert files == "all"
            return False

        async def delete_torrent(self, torrent_id: str) -> bool:
            assert torrent_id == "rd-unselectable"
            calls["deleted"] = True
            return True

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Frieren"
        assert trusted_only is False
        return [torrent]

    with (
        patch("omakase.plus.automation.read_secret", return_value="rd-key"),
        patch("omakase.plus.automation.search", side_effect=fake_search),
        patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
    ):
        result = asyncio.run(search_and_download(db=None, user_id=1, title="Frieren"))

    assert result["status"] == "rd_error"
    assert result["rd_id"] == "rd-unselectable"
    assert "select files" in result["detail"]
    assert calls["deleted"] is False


def test_search_and_download_records_provider_block_attempt_without_magnet():
    """Attempt telemetry keeps RD block evidence without storing full magnets."""
    from omakase.plus.automation import search_and_download
    from omakase.plus.db import _db, get_db
    from omakase.plus.realdebrid import RealDebridProviderBlock

    torrent = NyaaTorrent(
        title="[SubsPlease] Bleach - 451 [1080p][HEVC]",
        magnet="magnet:?xt=urn:btih:ABCDEF1234567890&dn=blocked",
        seeders=44,
        leechers=2,
        size_bytes=1_500_000_000,
        size_display="1.4 GiB",
        pub_date=datetime.now(timezone.utc),
        is_trusted=True,
        is_batch=True,
    )

    class FakeRealDebridClient:
        def __init__(self, api_key: str):
            self.api_key = api_key

        async def add_magnet(self, magnet: str) -> str | None:
            assert magnet == torrent.magnet
            raise RealDebridProviderBlock(
                http_status=451,
                error_code="infringing_file",
                detail="Provider blocked this file",
            )

    async def fake_search(title: str, trusted_only: bool = False):
        assert title == "Bleach"
        assert trusted_only is False
        return [torrent]

    with tempfile.TemporaryDirectory() as tmp:
        conn = get_db(tmp)
        try:
            user_id = conn.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("attempts@example.com", "hash"),
            ).lastrowid
            planning_id = conn.execute(
                """INSERT INTO anilist_plannings
                   (user_id, anilist_id, title, status)
                   VALUES (?, ?, ?, ?)""",
                (user_id, 170732, "Bleach", "PLANNING"),
            ).lastrowid
            conn.commit()

            with (
                patch("omakase.plus.automation.read_secret", return_value="rd-key"),
                patch("omakase.plus.automation.search", side_effect=fake_search),
                patch("omakase.plus.automation.RealDebridClient", new=FakeRealDebridClient),
            ):
                result = asyncio.run(
                    search_and_download(
                        db=conn,
                        user_id=user_id,
                        title="Bleach",
                        planning_id=planning_id,
                    )
                )

            rows = conn.execute(
                """SELECT candidate_rank, total_candidates, torrent_title, torrent_hash,
                          seeders, size_display, is_batch, status, http_status,
                          error_code, detail, rd_torrent_id
                   FROM download_attempts
                   WHERE anilist_planning_id = ?""",
                (planning_id,),
            ).fetchall()
        finally:
            conn.close()
            _db.clear()

    assert result["status"] == "rd_provider_block"
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_rank"] == 1
    assert row["total_candidates"] == 1
    assert row["torrent_title"] == torrent.title
    assert row["torrent_hash"] == "ABCDEF1234567890"
    assert row["seeders"] == 44
    assert row["size_display"] == "1.4 GiB"
    assert row["is_batch"] == 1
    assert row["status"] == "provider_block"
    assert row["http_status"] == 451
    assert row["error_code"] == "infringing_file"
    assert row["detail"] == "Provider blocked this file"
    assert row["rd_torrent_id"] == ""
    assert "magnet:" not in row["torrent_hash"]
