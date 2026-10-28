import json
from dataclasses import replace

import pytest

from omakase.plus.db import get_db
from omakase.plus.mal import TokenBundle, TrackerHTTPError
from omakase.plus.tracker_reconciliation import Entry, ExactMapping, Inventory, Provider, State
from omakase.plus.tracker_sync import BridgeError, BridgeService, writer_lease


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAKASE_PLUS_MASTER_KEY", "synthetic-bridge-master")
    from omakase.plus import secrets

    monkeypatch.setattr(secrets, "_KEY", None)
    db = get_db(tmp_path)
    for n in range(1, 4):
        db.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)",
            (n, f"member{n}@example.test", "hash"),
        )
    db.commit()
    return db


class FakeClient:
    def __init__(self, provider):
        self.provider = provider
        self.rows = {}
        self.writes = []
        self.titles = {}
        self.calls = 0

    def viewer(self, token):
        self.calls += 1
        return (101 if self.provider == Provider.ANILIST else 202, "Synthetic member")

    def inventory(self, token, identity):
        self.calls += 1
        inv = Inventory(identity, tuple(self.rows.values()), complete=True)
        return (inv, (ExactMapping(11, 22, 22),)) if self.provider == Provider.ANILIST else inv

    def mapping(self, token, mal_id):
        return ExactMapping(11, 22, 22)

    def entry(self, token, media_id, identity=None):
        self.calls += 1
        return self.rows.get(media_id)

    def patch(self, token, media_id, changes):
        self.writes.append((media_id, changes))
        prior = self.rows.get(media_id, Entry(media_id, State()))
        row = replace(prior, state=replace(prior.state, **changes), revision="after")
        self.rows[media_id] = row
        return row


def service(db):
    a, m = FakeClient(Provider.ANILIST), FakeClient(Provider.MAL)
    s = BridgeService(db, m, a, now=lambda: 1000, legacy_writers_ready=True)
    for provider, remote in [(Provider.ANILIST, 101), (Provider.MAL, 202)]:
        s.save_connection(
            1,
            provider,
            remote,
            "Synthetic member",
            TokenBundle("synthetic-access", "synthetic-refresh", 99999),
        )
    return s, a, m


def test_encrypted_connections_duplicate_identity_and_three_account_isolation(db):
    s, a, m = service(db)
    saved = db.execute("SELECT encrypted_bundle FROM tracker_bridge_connections").fetchall()
    assert all("synthetic-access" not in r[0] for r in saved)
    with pytest.raises(BridgeError, match="identity_linked_elsewhere"):
        s.save_connection(
            2, Provider.MAL, 202, "Other", TokenBundle("other-access", "other-refresh", 99999)
        )
    assert s.status(2)["connections"]["myanimelist"]["state"] == "disconnected"
    assert s.status(3)["connections"]["anilist"]["state"] == "disconnected"
    assert a.calls == m.calls == 0


def test_oauth_state_encrypted_one_use_same_session_config_and_owner(db):
    s, _, _ = service(db)
    state = s.begin_oauth(1, "session1", "config", "a" * 64)
    row = db.execute("SELECT * FROM tracker_mal_oauth_states").fetchone()
    assert state not in row["state_hash"] and "a" * 64 not in row["encrypted_verifier"]
    with pytest.raises(BridgeError):
        s.consume_oauth(2, "session1", "config", state)
    with pytest.raises(BridgeError):
        s.consume_oauth(1, "different-session", "config", state)
    with pytest.raises(BridgeError):
        s.consume_oauth(1, "session1", "otherconfig", state)
    assert s.consume_oauth(1, "session1", "config", state) == "a" * 64
    with pytest.raises(BridgeError):
        s.consume_oauth(1, "session1", "config", state)


def test_preview_zero_writes_then_exact_approved_create_and_baseline(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State(progress=3))
    preview = s.preview(1)
    assert preview["counts"]["proposals"] == 1 and not a.writes and not m.writes
    with pytest.raises(BridgeError):
        s.plan(2, preview["id"])
    result = s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert result["acknowledged"] == 1 and m.writes[0][1]["progress"] == 3
    assert (
        db.execute("SELECT count(*) FROM tracker_bridge_baselines WHERE user_id=1").fetchone()[0]
        == 1
    )
    again = s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert again["acknowledged"] == 1 and len(m.writes) == 1


def test_create_rechecks_destination_and_preserves_new_remote_entry(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State(progress=3))
    preview = s.preview(1)
    m.rows[22] = Entry(22, State(progress=9))
    result = s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert result["conflicts"] == 1 and not m.writes


def test_unapproved_rating_conflict_and_bad_plan_hash_never_write(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State(score=70))
    m.rows[22] = Entry(22, State(score=80))
    preview = s.preview(1)
    assert preview["counts"]["conflicts"] == 1
    with pytest.raises(BridgeError):
        s.apply(1, preview["id"], "bad", approved_items=[])
    result = s.apply(1, preview["id"], preview["hash"], approved_items=[])
    assert not a.writes and not m.writes and result["acknowledged"] == 0


def test_disconnect_generation_invalidates_pending_plan_no_cross_account_change(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    preview = s.preview(1)
    s.disconnect(1, Provider.MAL)
    with pytest.raises(BridgeError):
        s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert not m.writes


def test_single_writer_lease_and_activation_gate(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    with writer_lease(db, 1, now=1000):
        with pytest.raises(BridgeError, match="writer_busy"):
            s.preview(1)
    preview = s.preview(1)
    s.legacy_writers_ready = False
    with pytest.raises(BridgeError, match="writer_coordination_required"):
        s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert not m.writes


def test_ambiguous_write_never_automatically_replays(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    preview = s.preview(1)

    def uncertain(*args):
        raise TrackerHTTPError("ambiguous_write", ambiguous=True)

    m.patch = uncertain
    result = s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert result["ambiguous"] == 1
    result = s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert result["ambiguous"] == 1 and not m.writes


def test_acknowledging_safe_progress_does_not_acknowledge_conflicting_score(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State(score=60))
    m.rows[22] = Entry(22, State(score=60))
    initial = s.preview(1)
    s.apply(1, initial["id"], initial["hash"], approved_items=[])
    a.rows[11] = Entry(11, State(progress=3, score=70))
    m.rows[22] = Entry(22, State(score=80))
    preview = s.preview(1)
    assert preview["counts"]["conflicts"] == 1
    s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    next_preview = s.preview(1)
    assert next_preview["counts"]["conflicts"] == 1
    assert not next_preview["items"]


def test_disconnect_during_dispatched_write_never_resurrects_baseline_or_job(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    preview = s.preview(1)
    original = m.patch

    def disconnected(*args):
        result = original(*args)
        s.disconnect(1, Provider.MAL)
        return result

    m.patch = disconnected
    result = s.apply(1, preview["id"], preview["hash"], approved_items=["0"])
    assert result["ambiguous"] == 1
    assert db.execute("SELECT count(*) FROM tracker_bridge_baselines").fetchone()[0] == 0


def test_initial_rating_conflict_requires_exact_field_choice_then_minimal_patch(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State(score=70))
    m.rows[22] = Entry(22, State(score=80))
    preview = s.preview(1)
    resolved = s.resolve_plan(
        1, preview["id"], preview["hash"], [{"conflict": "0", "source": "anilist"}]
    )
    assert resolved["counts"]["conflicts"] == 0
    assert dict(resolved["items"][0]["patch"]["changes"]) == {"score": 70}
    result = s.apply(1, resolved["id"], resolved["hash"], approved_items=["0"])
    assert result["acknowledged"] == 1 and m.writes == [(22, {"score": 70})]


def test_resolving_only_one_initial_field_leaves_other_conflict_unacknowledged(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State(progress=3, score=70))
    m.rows[22] = Entry(22, State(score=80))
    preview = s.preview(1)
    index = next(str(i) for i, c in enumerate(preview["conflicts"]) if c["field"] == "progress")
    resolved = s.resolve_plan(
        1, preview["id"], preview["hash"], [{"conflict": index, "source": "anilist"}]
    )
    s.apply(1, resolved["id"], resolved["hash"], approved_items=["0"])
    assert db.execute("SELECT count(*) FROM tracker_bridge_baselines").fetchone()[0] == 0
    assert s.preview(1)["counts"]["conflicts"] == 1


def test_large_inventory_scan_resumes_saved_pages_and_never_plans_partial(db):
    s, a, m = service(db)
    cursors = []

    def page(token, identity, cursor):
        cursors.append(cursor)
        # Three pages, with two inspected in each bounded manual job.
        e = Entry(cursor, State())
        return (e,), (), cursor + 1 if cursor < 3 else None

    a.scan_page = page
    first = s.preview(1)
    assert first["status"] == "reading" and first["provider_writes"] == 0
    assert cursors == [1, 2]
    assert db.execute("SELECT count(*) FROM tracker_bridge_plans").fetchone()[0] == 0
    second = s.preview(1)
    assert cursors == [1, 2, 3] and second["counts"]["unmapped"] == 3
    assert not a.writes and not m.writes


def test_rate_deferred_outbox_respects_retry_after_without_automatic_replay(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    plan = s.preview(1)
    calls = []

    def limited(*args):
        calls.append(1)
        raise TrackerHTTPError("rate_limited", retry_after=60)

    m.patch = limited
    assert s.apply(1, plan["id"], plan["hash"], approved_items=["0"])["deferred"] == 1
    with pytest.raises(TrackerHTTPError):
        s.apply(1, plan["id"], plan["hash"], approved_items=["0"])
    assert calls == [1]


def test_explicit_enable_runs_only_established_safe_fields_and_preserves_new_entry_preview(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    m.rows[22] = Entry(22, State())
    preview = s.preview(1)
    s.apply(1, preview["id"], preview["hash"], approved_items=[])
    s.set_enabled(1, True)
    a.rows[11] = Entry(11, State(progress=4))
    result = s.sync_tick(1)
    assert result["acknowledged"] == 1 and m.rows[22].state.progress == 4
    s.disconnect(1, Provider.MAL)
    assert s.sync_tick(1)["state"] == "paused"


def test_changed_existing_anilist_token_stops_bridge_without_using_saved_copy(db):
    s, a, m = service(db)
    s.anilist_token_source = lambda user: None
    with pytest.raises(BridgeError, match="connection_changed"):
        s.preview(1)
    assert a.calls == m.calls == 0
    assert s.status(1)["connections"]["anilist"]["state"] == "reconnect"


def test_after_write_readback_rate_error_is_ambiguous_not_a_replayable_defer(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    plan = s.preview(1)
    original = m.entry

    def read(*args):
        if m.writes:
            raise TrackerHTTPError("rate_limited", retry_after=60)
        return original(*args)

    m.entry = read
    result = s.apply(1, plan["id"], plan["hash"], approved_items=["0"])
    assert result["ambiguous"] == 1 and result["deferred"] == 0
    assert len(m.writes) == 1


def test_mapping_change_before_dispatch_is_conflict_not_repeated_pending(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    plan = s.preview(1)
    a.mapping = lambda token, mid: ExactMapping(33, 22, 22)
    result = s.apply(1, plan["id"], plan["hash"], approved_items=["0"])
    assert result["conflicts"] == 1 and not m.writes


def test_provider_retry_after_pauses_all_calls_for_that_personal_pair(db):
    s, a, m = service(db)
    calls = []

    def limited(token):
        calls.append(1)
        raise TrackerHTTPError("rate_limited", retry_after=120)

    a.viewer = limited
    for _ in range(2):
        with pytest.raises(TrackerHTTPError):
            s.preview(1)
    assert calls == [1]
    assert s.status(1)["retry_after"] == 120


def test_refresh_rotation_is_atomic_and_duplicate_refresh_lease_blocks(db):
    s, a, m = service(db)
    s.save_connection(
        1, Provider.MAL, 202, "Synthetic member", TokenBundle("old-access", "old-refresh", 900)
    )
    calls = []

    def refresh(bundle):
        calls.append(bundle.refresh_token)
        return TokenBundle("new-access", "new-refresh", 9000)

    m.refresh = refresh
    assert s._token(1, Provider.MAL) == "new-access"
    assert s._token(1, Provider.MAL) == "new-access" and calls == ["old-refresh"]
    row = db.execute(
        "SELECT refresh_lease,refresh_until FROM tracker_bridge_connections WHERE user_id=1 AND provider='myanimelist'"
    ).fetchone()
    assert row[0] is None and row[1] == 0


def test_approved_large_batch_moves_past_acknowledged_prefix_without_replaying(db):
    s, a, m = service(db)
    mappings = tuple(ExactMapping(100 + i, 200 + i, 200 + i) for i in range(20))
    a.rows = {x.anilist_id: Entry(x.anilist_id, State()) for x in mappings}
    a.inventory = lambda token, identity: (
        Inventory(identity, tuple(a.rows.values()), complete=True),
        mappings,
    )
    a.mapping = lambda token, mid: next(x for x in mappings if x.mal_id == mid)
    plan = s.preview(1)
    selected = [str(i) for i in range(20)]
    first = s.apply(1, plan["id"], plan["hash"], approved_items=selected)
    second = s.apply(1, plan["id"], plan["hash"], approved_items=[])
    assert first["acknowledged"] == 10 and first["pending"] == 10
    assert second["acknowledged"] == 20 and second["pending"] == 0
    assert len(m.writes) == 20 and len({mid for mid, _ in m.writes}) == 20


def test_revoked_token_pauses_connection_and_enabled_work_not_fake_connected(db):
    s, a, m = service(db)

    def revoked(token):
        raise TrackerHTTPError("reconnect_required")

    a.viewer = revoked
    with pytest.raises(TrackerHTTPError):
        s.preview(1)
    assert s.status(1)["connections"]["anilist"]["state"] == "reconnect"
    assert s.status(1)["enabled"] is False


def test_changed_viewer_numeric_identity_invalidates_connection_generation(db):
    s, a, m = service(db)
    a.viewer = lambda token: (999, "Different identity")
    with pytest.raises(BridgeError, match="provider_identity_changed"):
        s.preview(1)
    assert s.status(1)["connections"]["anilist"]["state"] == "reconnect"
    assert not a.writes and not m.writes


def test_expired_preview_retention_is_owner_scoped_and_preserves_ambiguous_intents(db):
    s, a, m = service(db)
    a.rows[11] = Entry(11, State())
    plan = s.preview(1)
    db.execute(
        "INSERT INTO tracker_bridge_outbox(plan_id,user_id,item_key,encrypted_patch,state,updated_at) VALUES(?,?,?,?,'leased',?)",
        (plan["id"], 1, "0", "synthetic-patch", 1000),
    )
    db.execute(
        "INSERT INTO tracker_bridge_plans VALUES(?,?,?,?,?,?,?)",
        ("c" * 32, 2, "d" * 64, "other-account-unchanged", 1, 2, "preview"),
    )
    db.commit()
    s.now = lambda: 2000
    s._prune(1)
    row = db.execute(
        "SELECT state,encrypted_payload FROM tracker_bridge_plans WHERE id=?", (plan["id"],)
    ).fetchone()
    from omakase.plus.secrets import decrypt

    assert row["state"] == "cancelled" and json.loads(decrypt(row["encrypted_payload"])) == {
        "retired": True
    }
    assert (
        db.execute("SELECT state FROM tracker_bridge_outbox WHERE user_id=1").fetchone()[0]
        == "ambiguous"
    )
    assert (
        db.execute("SELECT encrypted_payload FROM tracker_bridge_plans WHERE user_id=2").fetchone()[
            0
        ]
        == "other-account-unchanged"
    )
    assert not a.writes and not m.writes


def test_public_lease_recheck_refuses_expiry_before_dispatch_or_acknowledgement(db):
    from omakase.plus.tracker_sync import require_writer_lease

    with writer_lease(db, 1, now=1000) as token:
        require_writer_lease(db, 1, token, now=1001)
        with pytest.raises(BridgeError, match="writer_lease_expired"):
            require_writer_lease(db, 1, token, now=1300)
        with pytest.raises(BridgeError, match="writer_lease_expired"):
            require_writer_lease(db, 2, token, now=1001)
