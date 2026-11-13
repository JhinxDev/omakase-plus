from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from omakase.plus import db as plus_db
from omakase.plus.catalog import candidate_from_torrent, rank_candidates
from omakase.plus.manifest import build_library_manifest
from omakase.plus.media_store import upsert_monitor
from omakase.plus.media_sync import reconcile_candidates
from omakase.plus.media_types import MediaTarget
from omakase.plus.nyaa import NyaaTorrent
from omakase.plus.realdebrid import RDFile, RDTorrent, RealDebridProviderBlock

REPO_ROOT = Path(__file__).resolve().parents[2]
CURATOR_ROOT = REPO_ROOT / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from plex_client import PlexSection, plan_library_topology  # noqa: E402
from plex_curator import stage_manifest_overlays  # noqa: E402


def _torrent(title: str, suffix: str, *, seeders: int = 20) -> NyaaTorrent:
    torrent_hash = (suffix * 40)[:40]
    return NyaaTorrent(
        title=title,
        magnet=f"magnet:?xt=urn:btih:{torrent_hash}",
        seeders=seeders,
        leechers=0,
        size_bytes=1024,
        size_display="1 KiB",
        pub_date=datetime(2026, 7, 15, tzinfo=timezone.utc),
        is_trusted=True,
        is_batch=True,
    )


def test_mha_s4_vertical_contract_reaches_staged_overlay_and_plex_dry_run(tmp_path):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(db)
    user_id = db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("must-not-leak@example.com", "hash"),
    ).lastrowid
    target = MediaTarget(
        anilist_id=104276,
        title="My Hero Academia",
        search_titles=("Boku no Hero Academia", "My Hero Academia"),
        format="TV",
        status="FINISHED",
        episodes=3,
        aired_episodes=3,
        season_number=4,
        season="FALL",
        season_year=2019,
        genres=("Action", "Adventure"),
        next_airing_at=None,
    )
    monitor_id = upsert_monitor(
        db,
        user_id=int(user_id),
        anilist_id=target.anilist_id,
        title=target.title,
        search_titles=target.search_titles,
        genres=target.genres,
        format=target.format,
        release_status=target.status,
        season_number=target.season_number,
        expected_episodes=target.episodes,
        aired_episodes=target.aired_episodes,
    )
    monitor = db.execute("SELECT * FROM media_monitors WHERE id = ?", (monitor_id,)).fetchone()
    torrents = (
        _torrent("My Hero Academia S4 - 64-66 RAW 1080p", "a"),
        _torrent("My Hero Academia S4 - 64-66 480p Subs", "b"),
        _torrent("My Hero Academia S4 - 64-66 1080p Dual Audio", "c"),
        _torrent("My Hero Academia S4 - 64-66 720p Dual Audio", "d"),
    )
    candidates = rank_candidates(
        tuple(
            candidate_from_torrent(
                torrent,
                search_alias="Boku no Hero Academia",
                season=4,
                expected=3,
                anilist_id=104276,
            )
            for torrent in torrents
        )
    )
    assert all(candidate is not None for candidate in candidates)

    rd = SimpleNamespace(
        instant_available=AsyncMock(return_value=True),
        add_magnet=AsyncMock(
            side_effect=[
                RealDebridProviderBlock(
                    http_status=451,
                    error_code="infringing_file",
                    detail="provider rejected hash",
                ),
                "rd-sensitive-id",
            ]
        ),
        select_files=AsyncMock(return_value=True),
        get_torrent=AsyncMock(
            return_value=RDTorrent(
                id="rd-sensitive-id",
                filename="My Hero Academia Season 4",
                status="downloaded",
                progress=100,
                bytes_total=200,
                bytes_done=200,
                links=("https://sensitive.invalid/media",),
                files=(
                    RDFile(id=1, path="Boku no Hero - 64.mkv", bytes=100, selected=True),
                    RDFile(id=2, path="Boku no Hero - 65-66.mkv", bytes=100, selected=True),
                ),
            )
        ),
    )
    result = asyncio.run(
        reconcile_candidates(
            db,
            monitor,
            candidates,
            rd=rd,
            searched_aliases=target.search_titles,
            now="2026-07-15T18:22:11Z",
            provider_complete=True,
        )
    )
    assert result.covered == frozenset({1, 2, 3})
    attempts = db.execute(
        "SELECT quality, decision, reason FROM media_candidate_attempts ORDER BY id"
    ).fetchall()
    assert ("", "INELIGIBLE", "LOW_RESOLUTION") in map(tuple, attempts)
    assert ("1080p", "INELIGIBLE", "RAW_RELEASE") in map(tuple, attempts)
    assert ("1080p", "PROVIDER_BLOCK", "PROVIDER_BLOCK") in map(tuple, attempts)
    assert ("720p", "ACCEPTED", "FALLBACK_1080P_EXHAUSTED") in map(tuple, attempts)

    manifest = build_library_manifest(db, generated_at="2026-07-15T18:22:11Z")
    serialized = json.dumps(manifest)
    asset = manifest["titles"][0]["assets"][0]
    assert asset["torrent_hash"] == "D" * 40
    assert asset["numbering_override"] == "mha-s4-absolute-v1"
    assert "must-not-leak" not in serialized
    assert "rd-sensitive-id" not in serialized
    assert "sensitive.invalid" not in serialized

    source_folder = tmp_path / "zurg" / asset["source_folder"]
    source_folder.mkdir(parents=True)
    (source_folder / "Boku no Hero - 64.mkv").write_bytes(b"episode-1")
    (source_folder / "Boku no Hero - 65-66.mkv").write_bytes(b"episodes-2-3")
    staged = stage_manifest_overlays(
        manifest,
        source_root=tmp_path / "zurg",
        staging_root=tmp_path / "overlay-generation",
    )
    destinations = {path.relative_to(staged.root).as_posix() for path in staged.links}
    assert destinations == {
        "Anime/My Hero Academia/Season 04/My Hero Academia - S04E01.mkv",
        "Anime/My Hero Academia/Season 04/My Hero Academia - S04E02-E03.mkv",
    }
    assert all(path.is_symlink() and path.resolve().is_file() for path in staged.links)

    topology = plan_library_topology(
        (
            PlexSection(1, "Anime", ("/old/anime",)),
            PlexSection(2, "Anime Movies", ("/old/anime-movies",)),
            PlexSection(3, "Real-Debrid", ("/library/symlinks",)),
            PlexSection(5, "Riven TV", ("/library/symlinks/riven",)),
        ),
        current_overlay="/library/symlinks/omakase/current",
    )
    assert topology.path_updates == {
        1: "/library/symlinks/omakase/current/Anime",
        2: "/library/symlinks/omakase/current/Anime Movies",
    }
    assert topology.renames == {5: "TV Shows"}
    assert topology.create_sections == ("Movies",)
    assert topology.deferred_removals == (3,)
    assert all(action.kind != "delete_media" for action in topology.actions)
    assert len(staged.plan_hash) == len(topology.plan_hash) == 64
    db.close()
