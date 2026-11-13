"""Personal web playback persistence, using synthetic exact targets only."""

import sqlite3
from dataclasses import replace

import pytest

from omakase.plus.db import run_migrations
from omakase.plus.web_playback_store import (
    PlaybackStoreError,
    ResolvedTarget,
    active_media_holds,
    close_session,
    create_session,
    get_resume,
    get_session,
    record_event,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'viewer@example.test','hash')")
    conn.execute(
        "INSERT INTO libraries(id,kind,owner_user_id,name) VALUES(?,'household',1,'Test')",
        ("d" * 32,),
    )
    conn.commit()
    yield conn
    conn.close()


TARGET = ResolvedTarget(1, "d" * 32, "123", 1, 1, "a" * 64, 1200)
BIND = "b" * 64


def session(db, target=TARGET, now=100):
    return create_session(
        db,
        target=target,
        binding_digest=BIND,
        snapshot={"membership": {}, "policy": {}, "source": {}},
        now=now,
    )


def event(db, s, seq, pos, now, **kwargs):
    return record_event(
        db,
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        seq=seq,
        position_seconds=pos,
        playing=kwargs.pop("playing", True),
        now=now,
        **kwargs,
    )


def test_coverage_replay_seek_and_stale_writer(db):
    s = session(db)
    event(db, s, 1, 0, 101, event="play")
    result = event(db, s, 2, 10, 111)
    assert result["covered_seconds"] == 10
    assert event(db, s, 2, 10, 112) == result
    with pytest.raises(PlaybackStoreError):
        event(db, s, 2, 11, 112)
    event(db, s, 3, 3, 112, event="seek")
    assert get_resume(db, target=TARGET)["position_seconds"] == 3
    assert event(db, s, 4, 4, 113)["covered_seconds"] == 10
    newer = session(db, now=114)
    assert newer["revision"] > s["revision"]
    with pytest.raises(PlaybackStoreError):
        event(db, s, 5, 5, 115)


def test_expiry_bindings_limits_and_holds(db):
    s = session(db)
    for kwargs in (
        {"user_id": 2, "binding_digest": BIND},
        {"user_id": 1, "binding_digest": "c" * 64},
    ):
        with pytest.raises(PlaybackStoreError):
            get_session(db, session_id=s["session_id"], now=101, **kwargs)
    session(db, replace(TARGET, episode_number=2), 101)
    third = session(db, replace(TARGET, episode_number=3), 102)
    assert len(active_media_holds(db, now=103)) == 2
    with pytest.raises(PlaybackStoreError):
        get_session(db, session_id=s["session_id"], user_id=1, binding_digest=BIND, now=103)
    close_session(db, session_id=third["session_id"], user_id=1, binding_digest=BIND, now=104)
    assert len(active_media_holds(db, now=105)) == 1
    assert active_media_holds(db, now=1400) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"position_seconds": float("nan")},
        {"position_seconds": 1201},
        {"playback_rate": 3},
        {"seq": True},
        {"playing": 1},
    ],
)
def test_invalid_events_do_not_move_resume(db, kwargs):
    s = session(db)
    args = dict(
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        seq=1,
        position_seconds=1,
        playing=True,
        now=101,
    )
    args.update(kwargs)
    with pytest.raises(PlaybackStoreError):
        record_event(db, **args)
    assert get_resume(db, target=TARGET)["position_seconds"] == 0


def test_no_seek_pause_end_or_impossible_delta_coverage(db):
    s = session(db)
    event(db, s, 1, 0, 101)
    event(db, s, 2, 100, 102)
    event(db, s, 3, 110, 112, playing=False, event="pause")
    event(db, s, 4, 120, 122, playing=False)
    event(db, s, 5, 1200, 123, event="ended")
    assert get_resume(db, target=TARGET)["covered_seconds"] == 10
    with pytest.raises(PlaybackStoreError):
        event(db, s, 6, 1200, 123.1)


def test_outer_transaction_not_committed_and_old_schema_retained(db):
    db.execute("INSERT INTO users(email,password_hash) VALUES('kept@example.test','hash')")
    session(db)
    assert db.in_transaction
    db.rollback()
    assert (
        db.execute("SELECT COUNT(*) FROM users WHERE email='kept@example.test'").fetchone()[0] == 0
    )
    assert get_resume(db, target=TARGET) is None
    db.execute("INSERT INTO users(email,password_hash) VALUES('kept@example.test','hash')")
    db.commit()
    run_migrations(db)
    assert (
        db.execute("SELECT email FROM users WHERE email='kept@example.test'").fetchone()[0]
        == "kept@example.test"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"canonical_id": "001"},
        {"domain": "movie"},
        {"media_version": "../file"},
        {"duration_seconds": float("inf")},
        {"duration_seconds": 43201},
        {"episode_number": 0},
    ],
)
def test_exact_target_required(changes):
    with pytest.raises(PlaybackStoreError):
        replace(TARGET, **changes)


def test_pause_credits_only_preceding_played_interval(db):
    s = session(db)
    event(db, s, 1, 0, 101, event="play")
    assert event(db, s, 2, 10, 111, event="pause", playing=False)["covered_seconds"] == 10
    assert event(db, s, 3, 20, 121, playing=False)["covered_seconds"] == 10


def test_overlap_union_rate_and_absolute_expiry(db):
    s = session(db, now=0)
    event(db, s, 1, 0, 1, playback_rate=2)
    assert event(db, s, 2, 20, 11, playback_rate=2)["covered_seconds"] == 20
    event(db, s, 3, 5, 12, event="seek")
    event(db, s, 4, 5, 13, event="play")
    assert event(db, s, 5, 15, 23)["covered_seconds"] == 20
    assert event(db, s, 6, 25, 33)["covered_seconds"] == 25
    for seq in range(7, 21):
        event(db, s, seq, 25, 33 + (seq - 6) * 1000, playing=False)
    with pytest.raises(PlaybackStoreError):
        get_session(db, session_id=s["session_id"], user_id=1, binding_digest=BIND, now=14400)


def test_new_migration_preserves_preexisting_rows(tmp_path, monkeypatch):
    import shutil

    from omakase.plus import db as plus_db

    directory = tmp_path / "old"
    directory.mkdir()
    canonical = plus_db._MIGRATIONS_DIR
    for path in canonical.glob("*.sql"):
        if int(path.name.split("-", 1)[0]) < 37:
            shutil.copyfile(path, directory / path.name)
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", directory)
    run_migrations(conn)
    conn.execute("INSERT INTO users(email,password_hash) VALUES('old@example.test','hash')")
    conn.commit()
    before = conn.execute("SELECT * FROM users").fetchall()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", canonical)
    run_migrations(conn)
    assert conn.execute("SELECT * FROM users").fetchall() == before
    assert tables <= {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    conn.close()


def test_process_clock_change_and_wall_clock_jump_do_not_credit(db, monkeypatch):
    from omakase.plus import web_playback_store as store

    clock = {"wall": 100.0, "mono": 10.0}
    monkeypatch.setattr(store.time, "time", lambda: clock["wall"])
    monkeypatch.setattr(store.time, "monotonic", lambda: clock["mono"])
    s = session(db)
    record_event(
        db,
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        seq=1,
        position_seconds=0,
        playing=True,
    )
    clock.update(wall=110.0, mono=11.0)
    result = record_event(
        db,
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        seq=2,
        position_seconds=10,
        playing=True,
    )
    assert result["covered_seconds"] == 0
    monkeypatch.setattr(store, "_CLOCK_ID", "newprocess")
    clock.update(wall=120.0, mono=20.0)
    result = record_event(
        db,
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        seq=3,
        position_seconds=20,
        playing=True,
    )
    assert result["covered_seconds"] == 0


def test_library_deletion_cascades_only_new_playback_rows(db):
    session(db)
    db.execute("DELETE FROM libraries WHERE id=?", (TARGET.library_id,))
    assert db.execute("SELECT COUNT(*) FROM web_playback_resumes").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["get", "event", "close"])
def test_snapshot_comparison_rejects_change_without_mutation(db, operation):
    s = session(db)
    expected = {"source": {}, "policy": {}, "membership": {}}
    assert (
        get_session(
            db,
            session_id=s["session_id"],
            user_id=1,
            binding_digest=BIND,
            expected_snapshot=expected,
            now=101,
        )["session_id"]
        == s["session_id"]
    )
    changed = dict(expected, policy={"revision": 2})
    common = dict(
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        expected_snapshot=changed,
        now=102,
    )
    with pytest.raises(PlaybackStoreError, match="snapshot"):
        if operation == "get":
            get_session(db, **common)
        elif operation == "event":
            record_event(db, seq=1, position_seconds=10, playing=True, **common)
        else:
            close_session(db, **common)
    assert get_resume(db, target=TARGET)["position_seconds"] == 0
    assert get_session(
        db,
        session_id=s["session_id"],
        user_id=1,
        binding_digest=BIND,
        expected_snapshot=expected,
        now=103,
    )


def test_cleanup_cap_preserves_resume_events_and_caller_transaction(db):
    from omakase.plus.web_playback_store import cleanup_sessions

    s = session(db)
    event(db, s, 1, 0, 101)
    saved = event(db, s, 2, 10, 111)
    key = db.execute("SELECT target_key FROM web_playback_resumes").fetchone()[0]
    for index in range(205):
        db.execute(
            "INSERT INTO web_playback_sessions(session_id,target_key,user_id,binding_digest,snapshot_json,revision,created_at,expires_at,last_seen,closed,closed_at) VALUES(?,?,1,?,'{}',1,0,100,100,1,100)",
            (f"old-{index}", key, BIND),
        )
        db.execute(
            "INSERT INTO web_playback_events VALUES(?,1,'fingerprint','{}')", (f"old-{index}",)
        )
    db.commit()
    assert cleanup_sessions(db, now=1_000_000) == 200
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 6
    assert get_resume(db, target=TARGET).items() <= saved.items()
    db.execute("INSERT INTO users(email,password_hash) VALUES('transaction@example.test','hash')")
    assert cleanup_sessions(db, now=1_000_000) == 6
    assert db.in_transaction
    db.rollback()
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 6
    fresh = session(db, now=1_000_000)
    assert get_session(
        db, session_id=fresh["session_id"], user_id=1, binding_digest=BIND, now=1_000_001
    )
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM web_playback_events").fetchone()[0] == 0
    assert get_resume(db, target=TARGET)["covered_seconds"] == saved["covered_seconds"]
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_cleanup_retains_recent_closed_expired_and_live_sessions(db):
    from omakase.plus.web_playback_store import cleanup_sessions

    now = 1_000_000
    key = session(db, now=now)["session_id"]
    target_key = db.execute("SELECT target_key FROM web_playback_resumes").fetchone()[0]
    for sid, closed, closed_at, expires, last_seen in [
        ("recent-closed", 1, now - 100, now + 100, now - 100),
        ("recent-expired", 0, None, now - 100, now - 100),
        ("recent-inactive", 0, None, now + 100, now - 1300),
    ]:
        db.execute(
            "INSERT INTO web_playback_sessions(session_id,target_key,user_id,binding_digest,snapshot_json,revision,created_at,expires_at,last_seen,closed,closed_at) VALUES(?,?,1,?,'{}',1,?,?,?,?,?)",
            (sid, target_key, BIND, now - 1000, expires, last_seen, closed, closed_at),
        )
    db.commit()
    assert cleanup_sessions(db, now=now) == 0
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 4
    assert get_session(db, session_id=key, user_id=1, binding_digest=BIND, now=now)


def test_ended_credits_only_finite_preceding_playing_delta(db):
    s = session(db)
    event(db, s, 1, 1180, 101, event="play")
    result = event(db, s, 2, 1200, 121, event="ended", playing=False)
    assert result["covered_seconds"] == 20
    assert event(db, s, 3, 1200, 131, event="ended", playing=False)["covered_seconds"] == 20
    s = session(db, replace(TARGET, episode_number=2), now=200)
    event(db, s, 1, 0, 201, event="play")
    assert event(db, s, 2, 1200, 202, event="ended", playing=False)["covered_seconds"] == 0


def test_coverage_interval_cap_never_credits_gaps():
    from omakase.plus.web_playback_store import _union

    intervals = [[float(i * 3), float(i * 3 + 1)] for i in range(513)]
    result = _union(intervals, [2000.0, 2010.0])
    assert len(result) == 512
    assert sum(end - start for start, end in result) == 521
    for start, end in result:
        assert [start, end] in intervals or [start, end] == [2000.0, 2010.0]
    assert _union([[0, 5], [10, 15]], [3, 12]) == [[0, 15]]


def test_replay_receipts_bounded_recent_replay_and_old_sequence_rejected(db):
    s = session(db)
    for seq in range(1, 258):
        result = event(db, s, seq, 0, 100 + seq, playing=False)
    assert db.execute("SELECT COUNT(*) FROM web_playback_events").fetchone()[0] == 256
    assert event(db, s, 257, 0, 358, playing=False) == result
    with pytest.raises(PlaybackStoreError, match="replay"):
        event(db, s, 257, 1, 358, playing=False)
    with pytest.raises(PlaybackStoreError, match="sequence"):
        event(db, s, 1, 0, 358, playing=False)
    assert get_resume(db, target=TARGET).items() <= result.items()


def test_typed_playback_targets_keep_separate_resume_positions(db):
    targets = [
        TARGET,
        replace(TARGET, domain="tv"),
        replace(TARGET, domain="anime-film", season_number=0, episode_number=0),
        replace(
            TARGET, domain="movie", canonical_id="tt1234567", season_number=0, episode_number=0
        ),
    ]
    for i, target in enumerate(targets, start=1):
        opened = session(db, target=target, now=100 + i * 10)
        event(db, opened, 1, i, 101 + i * 10, event="seek", playing=False)
    assert [get_resume(db, target=target)["position_seconds"] for target in targets] == [1, 2, 3, 4]


def test_target_migration_preserves_live_session_and_coverage(monkeypatch, tmp_path):
    import shutil

    from omakase.plus import db as migrations

    installed = migrations._MIGRATIONS_DIR
    previous = tmp_path / "migrations"
    previous.mkdir()
    for path in installed.glob("*.sql"):
        if int(path.name.split("-", 1)[0]) < 47:
            shutil.copyfile(path, previous / path.name)
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    monkeypatch.setattr(migrations, "_MIGRATIONS_DIR", previous)
    migrations.run_migrations(conn)
    conn.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'demo@example.invalid','fixture')"
    )
    conn.execute(
        "INSERT INTO libraries(id,kind,owner_user_id,name) VALUES(?,'household',1,'Fixture')",
        ("d" * 32,),
    )
    conn.commit()
    opened = session(conn)
    event(conn, opened, 1, 0, 101, event="play")
    receipt = event(conn, opened, 2, 5, 106)
    monkeypatch.setattr(migrations, "_MIGRATIONS_DIR", installed)
    migrations.run_migrations(conn)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert event(conn, opened, 2, 5, 107) == receipt
    assert get_resume(conn, target=TARGET)["position_seconds"] == 5
    assert event(conn, opened, 3, 6, 107)["watch_covered_seconds"] == 6
    conn.close()
