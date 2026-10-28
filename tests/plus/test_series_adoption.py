from __future__ import annotations

import asyncio
import sqlite3
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omakase.plus import db as plus_db
from omakase.plus.manifest import build_library_manifest
from omakase.plus.realdebrid import RDFile, RDTorrent
from omakase.plus.series_adoption import (
    SeriesAdoptionError,
    apply_existing_series_adoption_plan,
    build_existing_series_adoption_plan,
)


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(connection)
    connection.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("series-adoption@example.com", "hash"),
    )
    connection.execute(
        """INSERT INTO media_monitors
           (id, user_id, anilist_id, title, search_titles_json, genres_json,
            format, release_status, season_number, expected_episodes, aired_episodes,
            active)
           VALUES (104, 1, 166613, ?, ?, '["Action"]', 'TV', 'FINISHED', 2, 12, 12, 1)""",
        (
            "Hell's Paradise Season 2",
            '["Jigokuraku 2nd Season", "Jigokuraku Season 2"]',
        ),
    )
    yield connection
    connection.close()


def _torrent(*, changed: bool = False, duplicate_episode: bool = False) -> RDTorrent:
    files = []
    for episode in range(1, 13):
        shown_episode = 1 if duplicate_episode and episode == 2 else episode
        size = 101 if changed and episode == 12 else 100
        files.append(
            RDFile(
                id=episode,
                path=f"/[New-raws] Jigokuraku S2 - {shown_episode:02d} [1080p] [AMZN].mkv",
                bytes=size,
                selected=True,
            )
        )
    return RDTorrent(
        id="rd-hells-s2",
        torrent_hash="A" * 40,
        filename="[New-raws] Jigokuraku S2 - 01-12 [1080p] [AMZN]",
        status="downloaded",
        progress=100,
        bytes_total=sum(item.bytes for item in files),
        bytes_done=sum(item.bytes for item in files),
        links=(),
        files=tuple(files),
    )


def _rd(torrent: RDTorrent) -> SimpleNamespace:
    return SimpleNamespace(
        list_torrents=AsyncMock(return_value=[torrent]),
        get_torrent=AsyncMock(return_value=torrent),
        add_magnet=AsyncMock(),
        select_files=AsyncMock(),
        delete_torrent=AsyncMock(),
    )


def _requests() -> list[dict[str, object]]:
    return [{"monitor_id": 104, "torrent_hash": "A" * 40, "quality": "1080p"}]


def test_existing_series_adoption_is_hash_bound_nonmutating_and_revalidated(
    db: sqlite3.Connection,
) -> None:
    rd = _rd(_torrent())
    plan = asyncio.run(
        build_existing_series_adoption_plan(
            db,
            user_id=1,
            requests=_requests(),
            rd=rd,
            created_at="2026-07-26T01:30:00Z",
        )
    )

    assert plan["schema_version"] == "existing-series-adoption-plan-v2"
    assert plan["series"][0]["assets"][0]["episode_coverage"] == list(range(1, 13))
    assert db.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    assert "rd-hells-s2" not in str(plan)

    result = asyncio.run(
        apply_existing_series_adoption_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            rd=rd,
            applied_at="2026-07-26T01:31:00Z",
        )
    )

    assert result == {
        "series": 1,
        "assets": 1,
        "episodes": 12,
        "plan_hash": plan["plan_hash"],
    }
    asset = db.execute("SELECT * FROM media_assets").fetchone()
    assert (asset["ownership"], asset["provenance"], asset["status"]) == (
        "external",
        "adopted",
        "downloaded",
    )
    assert asset["usable_coverage_json"] == "[1,2,3,4,5,6,7,8,9,10,11,12]"
    manifest = build_library_manifest(db, generated_at="2026-07-26T01:32:00Z")
    assert manifest["titles"][0]["coverage"]["usable_count"] == 12
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    rd.delete_torrent.assert_not_awaited()

    tampered = deepcopy(plan)
    tampered["series"][0]["canonical_title"] = "Wrong Series"
    with pytest.raises(SeriesAdoptionError, match="hash"):
        asyncio.run(
            apply_existing_series_adoption_plan(
                db,
                plan=tampered,
                expected_plan_hash=plan["plan_hash"],
                rd=rd,
                applied_at="2026-07-26T01:33:00Z",
            )
        )


def test_existing_series_adoption_blocks_duplicate_episode_inventory(
    db: sqlite3.Connection,
) -> None:
    with pytest.raises(SeriesAdoptionError, match="duplicate episodes"):
        asyncio.run(
            build_existing_series_adoption_plan(
                db,
                user_id=1,
                requests=_requests(),
                rd=_rd(_torrent(duplicate_episode=True)),
                created_at="2026-07-26T01:30:00Z",
            )
        )


def test_existing_series_adoption_blocks_state_or_inventory_drift(
    db: sqlite3.Connection,
) -> None:
    original = _torrent()
    rd = _rd(original)
    plan = asyncio.run(
        build_existing_series_adoption_plan(
            db,
            user_id=1,
            requests=_requests(),
            rd=rd,
            created_at="2026-07-26T01:30:00Z",
        )
    )
    db.execute("UPDATE media_monitors SET aired_episodes = 11 WHERE id = 104")

    with pytest.raises(SeriesAdoptionError, match="changed"):
        asyncio.run(
            apply_existing_series_adoption_plan(
                db,
                plan=plan,
                expected_plan_hash=plan["plan_hash"],
                rd=rd,
                applied_at="2026-07-26T01:31:00Z",
            )
        )
    db.execute("UPDATE media_monitors SET aired_episodes = 12 WHERE id = 104")
    changed_rd = _rd(_torrent(changed=True))
    with pytest.raises(SeriesAdoptionError, match="changed"):
        asyncio.run(
            apply_existing_series_adoption_plan(
                db,
                plan=plan,
                expected_plan_hash=plan["plan_hash"],
                rd=changed_rd,
                applied_at="2026-07-26T01:31:00Z",
            )
        )


def test_existing_series_adoption_uses_successful_exact_hash_title_history(
    db: sqlite3.Connection,
) -> None:
    planning_id = db.execute(
        """INSERT INTO anilist_plannings (user_id, anilist_id, title, status)
           VALUES (1, 166613, 'Hell''s Paradise Season 2', 'CURRENT')"""
    ).lastrowid
    db.execute(
        """INSERT INTO download_attempts
           (user_id, anilist_planning_id, request_id, candidate_rank,
            torrent_title, torrent_hash, status)
           VALUES (1, ?, 'history-bound', 1, ?, ?, 'selected')""",
        (
            planning_id,
            "[Judas] Jigokuraku 2nd Season - 09 [1080p]",
            "A" * 40,
        ),
    )
    torrent = RDTorrent(
        id="rd-shortened-title",
        torrent_hash="A" * 40,
        filename="[Judas] HP - S02E09",
        status="downloaded",
        progress=100,
        bytes_total=100,
        bytes_done=100,
        links=(),
        files=(
            RDFile(
                id=9,
                path="/[Judas] Jigokuraku S2 - 09.mkv",
                bytes=100,
                selected=True,
            ),
        ),
    )
    rd = _rd(torrent)

    plan = asyncio.run(
        build_existing_series_adoption_plan(
            db,
            user_id=1,
            requests=_requests(),
            rd=rd,
            created_at="2026-07-30T02:00:00Z",
        )
    )

    asset = plan["series"][0]["assets"][0]
    assert plan["schema_version"] == "existing-series-adoption-plan-v2"
    assert asset["source_title"] == "[Judas] HP - S02E09"
    assert asset["identity_evidence"] == "successful_exact_hash_history"
    assert asset["identity_title"] == "[Judas] Jigokuraku 2nd Season - 09 [1080p]"

    result = asyncio.run(
        apply_existing_series_adoption_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            rd=rd,
            applied_at="2026-07-30T02:01:00Z",
        )
    )

    assert result["episodes"] == 1
    assert (
        db.execute(
            "SELECT usable_coverage_json FROM media_assets WHERE torrent_hash = ?",
            ("A" * 40,),
        ).fetchone()[0]
        == "[9]"
    )
    manifest = build_library_manifest(db, generated_at="2026-07-30T02:02:00Z")
    hells_paradise = next(title for title in manifest["titles"] if title["anilist_id"] == 166613)
    assert hells_paradise["coverage"]["usable_episodes"] == [9]
    assert [asset["torrent_hash"] for asset in hells_paradise["assets"]] == ["A" * 40]
    db.execute(
        "UPDATE media_assets SET source_title = ? WHERE torrent_hash = ?",
        ("[Judas] Jigokuraku S03E09", "A" * 40),
    )
    wrong_season_manifest = build_library_manifest(
        db,
        generated_at="2026-07-30T02:03:00Z",
    )
    assert all(title["anilist_id"] != 166613 for title in wrong_season_manifest["titles"])
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    rd.delete_torrent.assert_not_awaited()
