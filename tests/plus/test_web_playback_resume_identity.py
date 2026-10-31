"""Durable exact-publication resume proof, independent of session retention."""

import shutil
import sqlite3
from dataclasses import replace
from types import SimpleNamespace

import pytest

from omakase.plus import db as plus_db
from omakase.plus import households as hh
from omakase.plus import web_playback_navigation as nav
from omakase.plus import web_playback_store as store
from omakase.plus.product_views import home_view
from omakase.plus.web_playback_watch import observe_playback
from tests.plus.test_viewing_history import db as db
from tests.plus.test_viewing_history import entry
from tests.plus.test_viewing_history import event as history_event

VERSION = "a" * 64
IDENTITY = "b" * 64
BINDING = "f" * 64


def _snapshot(target, identity=IDENTITY):
    return {
        "membership": {"library_id": target.library_id, "viewer_id": target.user_id},
        "policy": {},
        "source": {"identity": identity, "media_version": target.media_version},
    }


def _partial(db, *, snapshot=True):
    library = hh.ensure_personal_library(db, user_id=1)
    monitor, watch = entry(db)
    db.execute("UPDATE watch_entries SET progress_episodes=0 WHERE id=?", (watch,))
    db.commit()
    scope = hh.resolve_scope(db, viewer_id=1, library_id=library)
    source = SimpleNamespace(
        actor_user_id=1,
        library_id=library,
        anilist_id=21,
        season_number=1,
        episode_number=2,
        media_version=VERSION,
        _scope=scope,
    )
    target = store.ResolvedTarget(1, library, "21", 1, 2, VERSION, 100)
    resolved = _snapshot(target) if snapshot else {"membership": {}, "policy": {}, "source": {}}
    session = store.create_session(
        db, target=target, binding_digest=BINDING, snapshot=resolved, now=1000
    )
    for seq, position in enumerate((0, 30), start=1):
        with hh._transaction(db):
            accepted = store.record_event(
                db,
                session_id=session["session_id"],
                user_id=1,
                binding_digest=BINDING,
                expected_snapshot=resolved,
                seq=seq,
                position_seconds=position,
                playing=True,
                event="play" if seq == 1 else "progress",
                now=1000 + position,
            )
            observe_playback(db, source=source, state=accepted, now=1000 + position)
    return target, monitor, scope


def _enable(monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")


@pytest.mark.parametrize(
    "candidate,episode,label", [(IDENTITY, 2, "Resume"), ("c" * 64, 1, "Play"), (None, 1, "Play")]
)
def test_actual_resume_publication_survives_eight_day_session_cleanup(
    db, monkeypatch, candidate, episode, label
):
    _enable(monkeypatch)
    target, monitor, _ = _partial(db)
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: candidate, 2: candidate})
    protected = (
        "web_playback_resumes",
        "web_playback_watch",
        "web_playback_cycle_coverage",
        "watch_entries",
        "watch_events",
    )
    before = {
        table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in protected
    }
    assert store.cleanup_sessions(db, now=1000 + 8 * 86400) == 1
    assert db.execute("SELECT COUNT(*) FROM web_playback_sessions").fetchone()[0] == 0
    assert {
        table: [tuple(row) for row in db.execute("SELECT * FROM " + table)] for table in protected
    } == before
    changes = db.total_changes
    card = home_view(db, user_id=1)["continue_watching"][0]
    assert card["play_label"] == label
    assert card["play_url"] == f"/plus/watch/{monitor}/{episode}?library_id={target.library_id}"
    assert card["partial_playback"] and card["progress_episodes"] == 0
    assert db.total_changes == changes
    if candidate is not None:
        current = target if candidate == IDENTITY else replace(target, media_version="d" * 64)
        state = store.create_session(
            db,
            target=current,
            binding_digest=BINDING,
            snapshot=_snapshot(current, candidate),
            now=1000 + 8 * 86400 + 1,
        )
        assert state["position_seconds"] == (30 if candidate == IDENTITY else 0)
        assert "publication_identity" not in state


@pytest.mark.parametrize("other", ["actor", "library", "version"])
def test_other_resume_identity_never_qualifies_own_legacy_partial(db, monkeypatch, other):
    _enable(monkeypatch)
    target, monitor, _ = _partial(db, snapshot=False)
    if other == "actor":
        current = replace(target, user_id=2, library_id=hh.ensure_personal_library(db, user_id=2))
    elif other == "library":
        db.execute(
            "INSERT INTO libraries(id,kind,owner_user_id,name) VALUES(?,'household',1,'Other')",
            ("e" * 32,),
        )
        current = replace(target, library_id="e" * 32)
    else:
        current = replace(target, media_version="d" * 64)
    store.create_session(
        db, target=current, binding_digest=BINDING, snapshot=_snapshot(current), now=1000
    )
    store.cleanup_sessions(db, now=1000 + 8 * 86400)
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: IDENTITY, 2: IDENTITY})
    changes = db.total_changes
    card = home_view(db, user_id=1)["continue_watching"][0]
    assert card["play_label"] == "Play"
    assert card["play_url"] == f"/plus/watch/{monitor}/1?library_id={target.library_id}"
    assert db.total_changes == changes


@pytest.mark.parametrize(
    "source",
    [
        {},
        {"identity": "short", "media_version": VERSION},
        {"identity": IDENTITY, "media_version": "d" * 64},
    ],
)
def test_unproven_snapshot_cannot_create_durable_identity(db, source):
    library = hh.ensure_personal_library(db, user_id=1)
    target = store.ResolvedTarget(1, library, "21", 1, 2, VERSION, 100)
    store.create_session(
        db,
        target=target,
        binding_digest=BINDING,
        snapshot={"membership": {}, "policy": {}, "source": source},
        now=1000,
    )
    assert db.execute("SELECT publication_identity FROM web_playback_resumes").fetchone()[0] is None


def test_legacy_session_snapshot_is_not_a_durable_proof_and_next_stays_read_only(db, monkeypatch):
    _enable(monkeypatch)
    target, monitor, scope = _partial(db)
    db.execute("UPDATE web_playback_resumes SET publication_identity=NULL")
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: IDENTITY, 2: IDENTITY, 3: IDENTITY})
    changes = db.total_changes
    card = home_view(db, user_id=1)["continue_watching"][0]
    assert card["play_label"] == "Play"
    source = SimpleNamespace(
        actor_user_id=1,
        library_id=target.library_id,
        anilist_id=21,
        season_number=1,
        episode_number=2,
        _scope=scope,
    )
    assert (
        nav.exact_next_url(db, source=source, monitor_id=monitor)
        == f"/plus/watch/{monitor}/3?library_id={target.library_id}"
    )
    assert db.total_changes == changes
    # A fresh trusted resolution qualifies the existing exact-version resume,
    # without resetting position, coverage or personal watch history.
    before = store.get_resume(db, target=target)
    state = store.create_session(
        db, target=target, binding_digest=BINDING, snapshot=_snapshot(target), now=1100
    )
    assert state["position_seconds"] == before["position_seconds"] == 30
    assert state["covered_seconds"] == before["covered_seconds"] == 30
    assert home_view(db, user_id=1)["continue_watching"][0]["play_label"] == "Resume"


def test_unknown_new_resolution_declines_old_proof_without_resetting_resume(db, monkeypatch):
    _enable(monkeypatch)
    target, _, _ = _partial(db)
    before = store.get_resume(db, target=target)
    state = store.create_session(
        db,
        target=target,
        binding_digest=BINDING,
        snapshot={"membership": {}, "policy": {}, "source": {}},
        now=1100,
    )
    assert state["position_seconds"] == before["position_seconds"] == 30
    assert state["covered_seconds"] == before["covered_seconds"] == 30
    assert db.execute("SELECT publication_identity FROM web_playback_resumes").fetchone()[0] is None
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: IDENTITY, 2: IDENTITY})
    assert home_view(db, user_id=1)["continue_watching"][0]["play_label"] == "Play"


def test_042_adds_only_nullable_identity_and_preserves_legacy_data(tmp_path, monkeypatch):
    original = plus_db._MIGRATIONS_DIR
    old = tmp_path / "pre-042"
    old.mkdir()
    for migration in original.glob("*.sql"):
        if int(migration.name.split("-", 1)[0]) < 42:
            shutil.copyfile(migration, old / migration.name)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", old)
        plus_db.run_migrations(conn)
        conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(1,'legacy@example.invalid','fixture')"
        )
        library = hh.ensure_personal_library(conn, user_id=1)
        target = store.ResolvedTarget(1, library, "21", 1, 2, VERSION, 100)
        _, watch = entry(conn)
        history_event(conn, watch, source="manual", key="legacy-manual-history")
        conn.execute(
            "INSERT INTO web_playback_resumes(target_key,user_id,library_id,domain,canonical_id,season_number,episode_number,media_version,duration_seconds,position_seconds,intervals_json,updated_at) VALUES(?,1,?,'anime','21',1,2,?,100,30,'[[0,30]]',1000)",
            (store._key(target), library, VERSION),
        )
        conn.execute(
            "INSERT INTO web_playback_cycle_coverage(target_key,barrier,intervals_json) VALUES(?,0,'[[0,30]]')",
            (store._key(target),),
        )
        conn.execute(
            "INSERT INTO web_playback_watch(user_id,library_id,anilist_id,season_number,episode_number,media_version,covered_seconds,duration_seconds,revision,last_played_at) VALUES(1,?,21,1,2,?,30,100,1,'2026-10-31T00:00:00Z')",
            (library, VERSION),
        )
        conn.commit()
        tables = (
            "web_playback_resumes",
            "web_playback_cycle_coverage",
            "web_playback_watch",
            "watch_entries",
            "watch_events",
        )
        columns = {
            table: [row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")]
            for table in tables
        }
        before = {
            table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
            for table in tables
        }
        monkeypatch.setattr(plus_db, "_MIGRATIONS_DIR", original)
        plus_db.run_migrations(conn)
        assert {
            table: [
                tuple(row)
                for row in conn.execute("SELECT " + ",".join(columns[table]) + " FROM " + table)
            ]
            for table in tables
        } == before
        assert (
            conn.execute("SELECT publication_identity FROM web_playback_resumes").fetchone()[0]
            is None
        )
        assert store.get_resume(conn, target=target)["position_seconds"] == 30
        assert "publication_identity" not in store.get_resume(conn, target=target)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE web_playback_resumes SET publication_identity='not-a-digest'")
    finally:
        conn.close()
