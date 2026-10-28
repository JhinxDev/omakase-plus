from __future__ import annotations

import asyncio
import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omakase.plus import db as plus_db
from omakase.plus.manifest import build_library_manifest
from omakase.plus.media_adoption import (
    MovieAdoptionError,
    apply_existing_movie_adoption_plan,
    build_existing_movie_adoption_plan,
)
from omakase.plus.realdebrid import RDFile, RDTorrent

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_brief_06_adoption_request_preserves_exact_six_reviewed_assets():
    payload = json.loads(
        (REPO_ROOT / "deploy" / "brief-06-anime-movie-adoption.json").read_text(encoding="utf-8")
    )

    assert payload["schema_version"] == "existing-movie-adoption-request-v1"
    assert {
        (row["anilist_id"], row["torrent_hash"], row["quality"]) for row in payload["requests"]
    } == {
        (8888, "38715477AD3AA3147A27535CEB2EC17B0AFFE51B", "1080p"),
        (15197, "14FB83FF113DCA93A84A2C529EAEA0D77B0FA2FB", "1080p"),
        (99547, "DD00D25859B85DBD285036FFF2DB96D312C12CEE", "1080p"),
        (99547, "6C8B9F8907B9C8FB02CC2ABADE8A8910BF753935", "2160p"),
        (21893, "12CF5483E34AC82BA13C105300B9628B24CF1564", "2160p"),
        (617, "EABAFF719847BFB00908944B5FA3D0CDB5D68F8C", "1080p"),
    }


@pytest.fixture
def db() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(connection)
    user_id = connection.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        ("adoption@example.com", "hash"),
    ).lastrowid
    assert user_id == 1
    yield connection
    connection.close()


def _target(anilist_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        anilist_id=anilist_id,
        title="Fixture Movie",
        search_titles=("Fixture Movie", "Fixture Film"),
        genres=("Action", "Drama"),
        format="MOVIE",
        status="FINISHED",
        episodes=1,
        aired_episodes=1,
        season_number=None,
    )


def _torrent(torrent_hash: str, quality: str, *, rd_id: str) -> RDTorrent:
    filename = f"Fixture Movie {quality}"
    return RDTorrent(
        id=rd_id,
        torrent_hash=torrent_hash,
        filename=filename,
        status="downloaded",
        progress=100,
        bytes_total=100,
        bytes_done=100,
        links=("https://example.invalid/media",),
        files=(RDFile(id=1, path=f"/{filename}.mkv", bytes=100, selected=True),),
    )


def _rd(*torrents: RDTorrent) -> SimpleNamespace:
    by_id = {torrent.id: torrent for torrent in torrents}
    return SimpleNamespace(
        list_torrents=AsyncMock(return_value=list(torrents)),
        get_torrent=AsyncMock(side_effect=lambda rd_id: by_id[rd_id]),
        add_magnet=AsyncMock(),
        select_files=AsyncMock(),
        delete_torrent=AsyncMock(),
    )


def test_existing_movie_adoption_is_hash_bound_nonmutating_then_revalidated_on_apply(
    db: sqlite3.Connection,
) -> None:
    first = _torrent("A" * 40, "1080p", rd_id="rd-1080")
    second = _torrent("B" * 40, "2160p", rd_id="rd-2160")
    unrelated_duplicate_one = _torrent("C" * 40, "1080p", rd_id="rd-unrelated-one")
    unrelated_duplicate_two = _torrent("C" * 40, "1080p", rd_id="rd-unrelated-two")
    rd = _rd(first, second, unrelated_duplicate_one, unrelated_duplicate_two)
    requests = [
        {"anilist_id": 42, "torrent_hash": first.torrent_hash, "quality": "1080p"},
        {"anilist_id": 42, "torrent_hash": second.torrent_hash, "quality": "2160p"},
    ]

    plan = asyncio.run(
        build_existing_movie_adoption_plan(
            db,
            user_id=1,
            requests=requests,
            rd=rd,
            identity_resolver=lambda value, hint: _target(int(value)),
            created_at="2026-07-16T06:30:00Z",
        )
    )
    assert plan["schema_version"] == "existing-movie-adoption-plan-v1"
    assert len(plan["movies"]) == 1
    assert [asset["quality"] for asset in plan["movies"][0]["assets"]] == [
        "2160p",
        "1080p",
    ]
    assert db.execute("SELECT COUNT(*) FROM media_monitors").fetchone()[0] == 0
    serialized = json.dumps(plan).casefold()
    assert "rd-1080" not in serialized
    assert "rd-2160" not in serialized
    assert "example.invalid" not in serialized

    result = asyncio.run(
        apply_existing_movie_adoption_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            rd=rd,
            identity_resolver=lambda value, hint: _target(int(value)),
            applied_at="2026-07-16T06:31:00Z",
        )
    )

    assert result == {"movies": 1, "assets": 2, "plan_hash": plan["plan_hash"]}
    manifest = build_library_manifest(db, generated_at="2026-07-16T06:32:00Z")
    movie = manifest["titles"][0]
    assert movie["format"] == "MOVIE"
    assert [asset["quality"] for asset in movie["assets"]] == ["2160p", "1080p"]
    rd.add_magnet.assert_not_awaited()
    rd.select_files.assert_not_awaited()
    rd.delete_torrent.assert_not_awaited()

    tampered = deepcopy(plan)
    tampered["movies"][0]["canonical_title"] = "Wrong Movie"
    with pytest.raises(MovieAdoptionError, match="hash"):
        asyncio.run(
            apply_existing_movie_adoption_plan(
                db,
                plan=tampered,
                expected_plan_hash=plan["plan_hash"],
                rd=rd,
                identity_resolver=lambda value, hint: _target(int(value)),
                applied_at="2026-07-16T06:33:00Z",
            )
        )


def test_existing_movie_adoption_blocks_non_movie_state_drift(db: sqlite3.Connection) -> None:
    plus_db_row = db.execute(
        "INSERT INTO media_monitors "
        "(user_id, anilist_id, title, format, release_status, expected_episodes, aired_episodes) "
        "VALUES (1, 99, 'Preserved Show', 'TV', 'RELEASING', 12, 3) RETURNING id"
    ).fetchone()
    assert plus_db_row is not None
    torrent = _torrent("D" * 40, "1080p", rd_id="rd-movie")
    rd = _rd(torrent)
    plan = asyncio.run(
        build_existing_movie_adoption_plan(
            db,
            user_id=1,
            requests=[{"anilist_id": 42, "torrent_hash": torrent.torrent_hash, "quality": "1080p"}],
            rd=rd,
            identity_resolver=lambda value, hint: _target(int(value)),
            created_at="2026-07-16T06:30:00Z",
        )
    )
    db.execute("UPDATE media_monitors SET aired_episodes = 4 WHERE anilist_id = 99")

    with pytest.raises(MovieAdoptionError, match="preservation|changed"):
        asyncio.run(
            apply_existing_movie_adoption_plan(
                db,
                plan=plan,
                expected_plan_hash=plan["plan_hash"],
                rd=rd,
                identity_resolver=lambda value, hint: _target(int(value)),
                applied_at="2026-07-16T06:31:00Z",
            )
        )


def test_existing_movie_adoption_rejects_non_movie_identity_collision(
    db: sqlite3.Connection,
) -> None:
    db.execute(
        "INSERT INTO media_monitors "
        "(user_id, anilist_id, title, format, release_status, expected_episodes, aired_episodes) "
        "VALUES (1, 42, 'Existing Series', 'TV', 'FINISHED', 12, 12)"
    )
    torrent = _torrent("E" * 40, "1080p", rd_id="rd-collision")

    with pytest.raises(MovieAdoptionError, match="non-movie"):
        asyncio.run(
            build_existing_movie_adoption_plan(
                db,
                user_id=1,
                requests=[
                    {"anilist_id": 42, "torrent_hash": torrent.torrent_hash, "quality": "1080p"}
                ],
                rd=_rd(torrent),
                identity_resolver=lambda value, hint: _target(int(value)),
                created_at="2026-07-16T06:30:00Z",
            )
        )


def test_existing_movie_adoption_blocks_pending_rd_removal(db: sqlite3.Connection) -> None:
    monitor_id = db.execute(
        "INSERT INTO media_monitors "
        "(user_id, anilist_id, title, format, release_status, expected_episodes, aired_episodes) "
        "VALUES (1, 99, 'Preserved Show', 'TV', 'FINISHED', 1, 1) RETURNING id"
    ).fetchone()[0]
    asset_id = db.execute(
        "INSERT INTO media_assets (monitor_id, torrent_hash, source_title) "
        "VALUES (?, ?, 'Preserved Asset') RETURNING id",
        (monitor_id, "F" * 40),
    ).fetchone()[0]
    run_id = db.execute(
        "INSERT INTO media_sync_runs (monitor_id, outcome) VALUES (?, 'removal_planned') RETURNING id",
        (monitor_id,),
    ).fetchone()[0]
    db.execute(
        "UPDATE media_sync_runs SET completed_at = '2026-07-16T06:28:00Z' WHERE id = ?",
        (run_id,),
    )
    trigger_names = db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'media_asset_removals'"
    ).fetchall()
    for (trigger_name,) in trigger_names:
        db.execute(f'DROP TRIGGER "{trigger_name}"')
    db.execute("PRAGMA ignore_check_constraints = ON")
    db.execute(
        "INSERT INTO media_asset_removals "
        "(operation_key, asset_id, sync_run_id, requested_at, outcome) "
        "VALUES ('pending-test', ?, ?, '2026-07-16T06:29:00Z', 'pending')",
        (asset_id, run_id),
    )
    db.execute("PRAGMA ignore_check_constraints = OFF")
    torrent = _torrent("D" * 40, "1080p", rd_id="rd-movie")

    with pytest.raises(MovieAdoptionError, match="pending Real-Debrid removal"):
        asyncio.run(
            build_existing_movie_adoption_plan(
                db,
                user_id=1,
                requests=[
                    {"anilist_id": 42, "torrent_hash": torrent.torrent_hash, "quality": "1080p"}
                ],
                rd=_rd(torrent),
                identity_resolver=lambda value, hint: _target(int(value)),
                created_at="2026-07-16T06:30:00Z",
            )
        )


def test_existing_movie_adoption_blocks_ambiguous_or_changed_inventory(
    db: sqlite3.Connection,
) -> None:
    torrent = RDTorrent(
        id="rd-ambiguous",
        torrent_hash="C" * 40,
        filename="Fixture Movie",
        status="downloaded",
        progress=100,
        bytes_total=200,
        bytes_done=200,
        links=(),
        files=(
            RDFile(id=1, path="/Fixture Movie.mkv", bytes=100, selected=True),
            RDFile(id=2, path="/Fixture Movie extra.mp4", bytes=100, selected=True),
        ),
    )

    with pytest.raises(MovieAdoptionError, match="exactly one selected media file"):
        asyncio.run(
            build_existing_movie_adoption_plan(
                db,
                user_id=1,
                requests=[
                    {"anilist_id": 42, "torrent_hash": torrent.torrent_hash, "quality": "1080p"}
                ],
                rd=_rd(torrent),
                identity_resolver=lambda value, hint: _target(int(value)),
                created_at="2026-07-16T06:30:00Z",
            )
        )
