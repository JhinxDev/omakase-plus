"""Exact coverage, contiguous progress, personal ownership and cleanup holds."""

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from omakase.plus import completion_cleanup as cleanup
from omakase.plus import households as hh
from omakase.plus import web_playback_store as store
from omakase.plus.db import run_migrations
from omakase.plus.household_media import save_anime_personal_entry
from omakase.plus.secrets import store_secret
from omakase.plus.viewing_history import anime_viewing_recency, viewing_history
from omakase.plus.watch.models import WatchEvent
from omakase.plus.web_playback_watch import observe_playback


@pytest.fixture
def fixture():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    run_migrations(db)
    for user in (1, 2):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (user, f"v{user}@example.invalid", "synthetic"),
        )
    db.commit()
    for user in (1, 2):
        store_secret(db, user, "plex_token", f"synthetic-plex-{user}")
        db.execute(
            "INSERT INTO sessions(id,user_id,expires_at) VALUES(?,?,?)",
            (f"synthetic-cookie-{user}", user, "2099-01-01T00:00:00+00:00"),
        )
    db.commit()
    library = hh.create_household(db, user_id=1, name="Shared")
    owner = hh.resolve_scope(db, viewer_id=1, library_id=library)
    invite = hh.create_invite(db, owner, now=100)
    hh.accept_invite(db, viewer_id=2, token=invite["token"], now=101)
    owner = hh.resolve_scope(db, viewer_id=1, library_id=library)
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,season_number,expected_episodes) VALUES(1,100,'Synthetic',1,5)"
    ).lastrowid
    db.commit()
    hh.LibraryRepository(db, owner).add(
        domain="anime", canonical_id="100", season=1, title="Synthetic", legacy_id=str(monitor)
    )

    def source(episode, user=2, version="a" * 64):
        return SimpleNamespace(
            actor_user_id=user,
            library_id=library,
            anilist_id=100,
            season_number=1,
            episode_number=episode,
            media_version=version,
            _scope=hh.resolve_scope(db, viewer_id=user, library_id=library),
        )

    yield db, source, monitor
    db.close()


def observe(db, source, coverage=85, revision=1, now=200):
    previous = db.execute(
        "SELECT covered_seconds FROM web_playback_watch WHERE user_id=? AND anilist_id=? AND season_number=? AND episode_number=? AND media_version=?",
        (
            source.actor_user_id,
            source.anilist_id,
            source.season_number,
            source.episode_number,
            source.media_version,
        ),
    ).fetchone()
    return observe_playback(
        db,
        source=source,
        state=dict(
            covered_seconds=coverage,
            duration_seconds=100,
            revision=revision,
            position_seconds=100,
            credited_seconds=max(0, coverage - (previous[0] if previous else 0)),
            watch_covered_seconds=coverage,
            watch_barrier=0,
            accepted_at=now,
        ),
        now=now,
    )


def test_running_copy_keeps_source_until_verified_worker_release(fixture):
    db, source, monitor = fixture
    target = store.ResolvedTarget(2, source(1).library_id, "100", 1, 1, "a" * 64, 100)
    state = store.create_session(
        db,
        target=target,
        binding_digest="b" * 64,
        snapshot={"membership": {}, "policy": {}, "source": {}},
        now=200,
    )
    db.execute(
        "UPDATE web_playback_sessions SET closed=1 WHERE session_id=?", (state["session_id"],)
    )
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,deadline) VALUES(?,?,?,'RUNNING',?,?,?)",
        ("f" * 32, "a" * 64, 2, 200, 230, 230),
    )
    assert cleanup.has_active_playback_hold(db, user_id=1, monitor_id=monitor, now=500)
    db.execute("UPDATE web_playback_remux SET state='ERROR'")
    assert not cleanup.has_active_playback_hold(db, user_id=1, monitor_id=monitor, now=500)


def test_exact_five_then_contiguous_and_personal(fixture):
    db, source, monitor = fixture
    assert observe(db, source(5))["newly_completed"]
    assert (
        db.execute("SELECT progress_episodes FROM watch_entries WHERE user_id=2").fetchone()[0] == 0
    )
    assert [c["episode"] for c in viewing_history(db, 2)["items"]] == [5]
    assert db.execute("SELECT COUNT(*) FROM watch_entries WHERE user_id=1").fetchone()[0] == 0
    for ep in range(1, 5):
        observe(db, source(ep), now=200 + ep)
    assert db.execute(
        "SELECT progress_episodes,monitor_id FROM watch_entries WHERE user_id=2"
    ).fetchone()[:] == (5, None)
    assert {c["episode"] for c in viewing_history(db, 2)["items"]} == {1, 2, 3, 4, 5}
    assert {r[0] for r in db.execute("SELECT user_id FROM tracker_sync_outbox")} == {2}
    assert db.execute("SELECT COUNT(*) FROM media_title_removals").fetchone()[0] == 0


def test_partial_recency_only_increases_on_coverage(fixture):
    db, source, _ = fixture
    observe(db, source(1), coverage=0)
    assert anime_viewing_recency(db, 2) == {}
    observe(db, source(1), coverage=10)
    first = anime_viewing_recency(db, 2)
    assert first == {(100, 1): "1970-01-01T00:03:20Z"}
    observe(db, source(1), coverage=10, revision=2, now=300)
    assert anime_viewing_recency(db, 2) == first
    observe(db, source(1), coverage=11, revision=3, now=301)
    assert anime_viewing_recency(db, 2)[100, 1] == "1970-01-01T00:05:01Z"
    assert viewing_history(db, 2)["items"] == []
    assert db.execute("SELECT COUNT(*) FROM watch_events").fetchone()[0] == 0


def test_replayed_completed_evidence_never_undoes_manual_reopen(fixture):
    db, source, _ = fixture
    for ep in range(1, 6):
        observe(db, source(ep), now=200 + ep)
    entry = db.execute("SELECT id FROM watch_entries WHERE user_id=2").fetchone()[0]
    save_anime_personal_entry(
        db,
        viewer_id=2,
        entry_id=entry,
        event=WatchEvent.reopen(episode=0, event_key="reopen", force=True),
    )
    before = db.execute("SELECT COUNT(*) FROM tracker_sync_outbox").fetchone()[0]
    assert not observe(db, source(5), coverage=99, revision=2, now=300)["newly_completed"]
    assert not observe(db, source(5, version="b" * 64), now=301)["newly_completed"]
    assert (
        db.execute("SELECT progress_episodes FROM watch_entries WHERE user_id=2").fetchone()[0] == 0
    )
    assert db.execute("SELECT COUNT(*) FROM tracker_sync_outbox").fetchone()[0] == before


def playing_session(db, source, now=1000):
    target = store.ResolvedTarget(
        source.actor_user_id,
        source.library_id,
        str(source.anilist_id),
        source.season_number,
        source.episode_number,
        source.media_version,
        100,
    )
    state = store.create_session(
        db,
        target=target,
        binding_digest="b" * 64,
        snapshot={
            "membership": {},
            "policy": {},
            "source": {"identity": "b" * 64, "media_version": source.media_version},
        },
        now=now,
    )
    return state["session_id"]


def playing_event(db, source, sid, seq, pos, now, event="progress", playing=True):
    state = store.record_event(
        db,
        session_id=sid,
        user_id=source.actor_user_id,
        binding_digest="b" * 64,
        seq=seq,
        position_seconds=pos,
        playing=playing,
        event=event,
        now=now,
    )
    result = observe_playback(db, source=source, state=state, now=now)
    return state, result


def test_replaying_covered_interval_updates_recency_but_retry_and_seek_do_not(fixture):
    db, source, _ = fixture
    value = source(1)
    sid = playing_session(db, value)
    playing_event(db, value, sid, 1, 0, 1000, event="play")
    first, _ = playing_event(db, value, sid, 2, 20, 1020)
    playing_event(db, value, sid, 3, 0, 1021, event="seek")
    playing_event(db, value, sid, 4, 0, 1022, event="play")
    replay, _ = playing_event(db, value, sid, 5, 10, 1032)
    assert first["covered_seconds"] == replay["covered_seconds"] == 20
    assert replay["credited_seconds"] == 10
    recency = anime_viewing_recency(db, 2)[100, 1]
    assert recency == "1970-01-01T00:17:12Z"
    observe_playback(db, source=value, state=replay, now=2000)
    playing_event(db, value, sid, 6, 90, 1033, event="seek")
    assert anime_viewing_recency(db, 2)[100, 1] == recency
    assert not db.execute("SELECT 1 FROM web_playback_completions").fetchone()


@pytest.mark.parametrize("version", ["a" * 64, "b" * 64])
def test_fresh_explicit_session_can_complete_after_manual_reopen(fixture, version, monkeypatch):
    monkeypatch.setattr(hh, "_stamp", lambda: "1970-01-01T00:18:14+00:00")
    db, source, _ = fixture
    value = source(1)
    sid = playing_session(db, value)
    playing_event(db, value, sid, 1, 0, 1000, event="play")
    for seq, pos in enumerate((30, 60, 90), 2):
        _, result = playing_event(db, value, sid, seq, pos, 1000 + pos)
    assert result["newly_completed"]
    entry = db.execute("SELECT id FROM watch_entries WHERE user_id=2").fetchone()[0]
    save_anime_personal_entry(
        db,
        viewer_id=2,
        entry_id=entry,
        event=WatchEvent.reopen(episode=0, event_key="reopen", force=True),
    )
    # The still-authorized old session cannot apply its old completion union.
    _, result = playing_event(db, value, sid, 5, 95, 1095)
    assert not result["newly_completed"]
    assert (
        db.execute("SELECT progress_episodes FROM watch_entries WHERE id=?", (entry,)).fetchone()[0]
        == 0
    )
    value = source(1, version=version)
    sid = playing_session(db, value, now=1100)
    playing_event(db, value, sid, 1, 0, 1100, event="play")
    for seq, pos in enumerate((30, 60, 90), 2):
        state, result = playing_event(db, value, sid, seq, pos, 1100 + pos)
    assert state["watch_covered_seconds"] == 90
    assert result["newly_completed"] and result["progress_episodes"] == 1
    assert db.execute("SELECT COUNT(*) FROM web_playback_completions").fetchone()[0] == 2
    assert not observe_playback(db, source=value, state=state, now=2000)["newly_completed"]
    assert (
        db.execute(
            "SELECT COUNT(DISTINCT event_key) FROM watch_events WHERE source='web_player'"
        ).fetchone()[0]
        == 2
    )


def test_reopened_same_version_partial_cycle_returns_to_continue_and_resume(fixture, monkeypatch):
    from omakase.plus import web_playback_navigation as nav

    db, source, _ = fixture
    value = source(1)
    sid = playing_session(db, value)
    playing_event(db, value, sid, 1, 0, 1000, event="play")
    for seq, pos in enumerate((30, 60, 90), 2):
        playing_event(db, value, sid, seq, pos, 1000 + pos)
    entry = db.execute("SELECT id FROM watch_entries WHERE user_id=2").fetchone()[0]
    monkeypatch.setattr(hh, "_stamp", lambda: "1970-01-01T00:18:14+00:00")
    save_anime_personal_entry(
        db,
        viewer_id=2,
        entry_id=entry,
        event=WatchEvent.reopen(episode=0, event_key="reopen-nav", force=True),
    )
    item = db.execute(
        "SELECT id FROM library_items WHERE library_id=?", (value.library_id,)
    ).fetchone()[0]
    card = {
        "id": item,
        "anilist_id": 100,
        "season_number": 1,
        "watch_status": "unwatched",
        "next_episode": 1,
    }
    monkeypatch.delenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", raising=False)
    assert not nav.attach_navigation(db, [dict(card)], user_id=2, library_scope=value._scope)[0][
        "partial_playback"
    ]
    sid = playing_session(db, value, now=1100)
    playing_event(db, value, sid, 1, 0, 1100, event="play")
    playing_event(db, value, sid, 2, 30, 1130)
    assert db.execute("SELECT completed_at FROM web_playback_watch").fetchone()[0] is not None
    before = db.total_changes
    assert nav.attach_navigation(db, [dict(card)], user_id=2, library_scope=value._scope)[0][
        "partial_playback"
    ]
    monkeypatch.setenv("OMAKASE_PLUS_PRIVATE", "true")
    monkeypatch.setenv("OMAKASE_PLUS_WEB_PLAYER_ENABLED", "true")
    monkeypatch.setattr(nav, "_candidates", lambda *args: {1: "b" * 64})
    result = nav.attach_navigation(db, [dict(card)], user_id=2, library_scope=value._scope)[0]
    assert result["partial_playback"] and result["play_label"] == "Resume"
    assert "/watch/1/1?" in result["play_url"]
    assert db.total_changes == before


def test_cleanup_waits_for_both_viewers_then_retries(fixture, monkeypatch):
    db, source, monitor = fixture
    for ep in range(1, 6):
        observe(db, source(ep, user=1), now=200 + ep)
    cleanup.set_completion_cleanup_preference(db, user_id=1, remove_from_plex=True)
    sessions = []
    for user in (1, 2):
        target = store.ResolvedTarget(
            user_id=user,
            library_id=source(1).library_id,
            canonical_id="100",
            season_number=1,
            episode_number=1,
            media_version="a" * 64,
            duration_seconds=100,
        )
        sessions.append(
            store.create_session(
                db,
                target=target,
                binding_digest=hashlib.sha256(f"synthetic-cookie-{user}".encode()).hexdigest(),
                snapshot={
                    "membership": {
                        "library_id": source(1, user=user).library_id,
                        "viewer_id": user,
                        "revision": source(1, user=user)._scope.membership_revision,
                    },
                    "policy": {"revision": source(1, user=user)._scope.policy_revision},
                    "source": {
                        "credential_generation": hashlib.sha256(
                            f"synthetic-plex-{user}".encode()
                        ).hexdigest()
                    },
                },
                now=300,
            )
        )
    called = []
    monkeypatch.setattr(
        cleanup, "request_title_removal", lambda *a, **k: called.append(k) or {"status": "queued"}
    )
    assert (
        cleanup.queue_completion_cleanup(
            db, user_id=1, monitor_id=monitor, requested_at="1970-01-01T00:05:00Z", now=300
        )["status"]
        == "deferred"
    )
    assert not called
    assert db.execute("SELECT active FROM media_monitors WHERE id=?", (monitor,)).fetchone()[0] == 1
    assert cleanup.process_deferred_completion_cleanup(db, now=301) == []
    db.execute("UPDATE web_playback_sessions SET closed=1 WHERE user_id=1")
    assert cleanup.process_deferred_completion_cleanup(db, now=302) == []
    # Expiry of the last member also releases the conservative cross-viewer hold.
    assert cleanup.process_deferred_completion_cleanup(db, now=3000) == [monitor]
    assert len(called) == 1


def test_completion_transition_does_not_withdraw_active_playback(fixture, monkeypatch):
    import time

    db, source, monitor = fixture
    now = time.time()
    for episode in range(1, 5):
        observe(db, source(episode, user=1), now=now)
    cleanup.set_completion_cleanup_preference(db, user_id=1, remove_from_plex=True)
    for user in (1, 2):
        target = store.ResolvedTarget(
            user_id=user,
            library_id=source(1).library_id,
            canonical_id="100",
            season_number=1,
            episode_number=5,
            media_version="a" * 64,
            duration_seconds=100,
        )
        store.create_session(
            db,
            target=target,
            binding_digest=hashlib.sha256(f"synthetic-cookie-{user}".encode()).hexdigest(),
            snapshot={
                "membership": {
                    "library_id": source(1, user=user).library_id,
                    "viewer_id": user,
                    "revision": source(1, user=user)._scope.membership_revision,
                },
                "policy": {"revision": source(1, user=user)._scope.policy_revision},
                "source": {
                    "credential_generation": hashlib.sha256(
                        f"synthetic-plex-{user}".encode()
                    ).hexdigest()
                },
            },
            now=now,
        )
    calls = []
    monkeypatch.setattr(
        cleanup, "request_title_removal", lambda *a, **k: calls.append(k) or {"status": "queued"}
    )
    observed = observe(db, source(5, user=1), now=now)
    assert observed["progress_episodes"] == 5
    assert (
        db.execute("SELECT status FROM watch_entries WHERE user_id=1").fetchone()[0] == "completed"
    )
    assert db.execute("SELECT COUNT(*) FROM deferred_completion_cleanup").fetchone()[0] == 1
    assert not calls
    assert db.execute("SELECT active FROM media_monitors WHERE id=?", (monitor,)).fetchone()[0] == 1
    db.execute("UPDATE web_playback_sessions SET closed=1")
    assert cleanup.process_deferred_completion_cleanup(db, now=now + 1) == [monitor]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "invalidation",
    [
        "logout",
        "expired",
        "naive_expiry",
        "malformed_expiry",
        "removed_token",
        "rotated_token",
        "wrong_binding",
        "missing_generation",
    ],
)
def test_invalid_auth_or_credential_releases_deferred_hold(fixture, monkeypatch, invalidation):
    db, source, monitor = fixture
    for episode in range(1, 6):
        observe(db, source(episode, user=1), now=200 + episode)
    cleanup.set_completion_cleanup_preference(db, user_id=1, remove_from_plex=True)
    scope = source(1)._scope
    target = store.ResolvedTarget(
        user_id=2,
        library_id=scope.library_id,
        canonical_id="100",
        season_number=1,
        episode_number=1,
        media_version="a" * 64,
        duration_seconds=100,
    )
    store.create_session(
        db,
        target=target,
        binding_digest=hashlib.sha256(b"synthetic-cookie-2").hexdigest(),
        snapshot={
            "membership": {
                "library_id": scope.library_id,
                "viewer_id": 2,
                "revision": scope.membership_revision,
            },
            "policy": {"revision": scope.policy_revision},
            "source": {"credential_generation": hashlib.sha256(b"synthetic-plex-2").hexdigest()},
        },
        now=300,
    )
    assert cleanup.has_active_playback_hold(db, user_id=1, monitor_id=monitor, now=301)
    calls = []
    monkeypatch.setattr(
        cleanup, "request_title_removal", lambda *a, **k: calls.append(k) or {"status": "queued"}
    )
    assert (
        cleanup.queue_completion_cleanup(
            db, user_id=1, monitor_id=monitor, requested_at="1970-01-01T00:05:00Z", now=301
        )["status"]
        == "deferred"
    )
    if invalidation == "logout":
        db.execute("DELETE FROM sessions WHERE user_id=2")
    elif invalidation in {"expired", "naive_expiry", "malformed_expiry"}:
        expiry = {
            "expired": "1970-01-01T00:05:01+00:00",
            "naive_expiry": "2099-01-01T00:00:00",
            "malformed_expiry": "invalid",
        }[invalidation]
        db.execute("UPDATE sessions SET expires_at=? WHERE user_id=2", (expiry,))
    elif invalidation == "removed_token":
        db.execute("DELETE FROM user_secrets WHERE user_id=2 AND key_name='plex_token'")
    elif invalidation == "rotated_token":
        store_secret(db, 2, "plex_token", "new-synthetic-token")
    elif invalidation == "wrong_binding":
        db.execute("UPDATE web_playback_sessions SET binding_digest=? WHERE user_id=2", ("c" * 64,))
    else:
        db.execute(
            "UPDATE web_playback_sessions SET snapshot_json=json_remove(snapshot_json,'$.source.credential_generation') WHERE user_id=2"
        )
    # Guard reads neither renew nor delete auth records or encrypted credentials.
    changed = db.total_changes
    assert not cleanup.has_active_playback_hold(db, user_id=1, monitor_id=monitor, now=302)
    assert db.total_changes == changed
    assert cleanup.process_deferred_completion_cleanup(db, now=302) == [monitor]
    assert len(calls) == 1
