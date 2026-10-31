"""Offline lifecycle harness contracts; no native worker/media execution."""

import asyncio
import importlib.util
import signal
from pathlib import Path

import pytest


@pytest.fixture
def helper():
    path = Path(__file__).resolve().parents[2] / "scripts/qualify_web_player_lifecycle.py"
    spec = importlib.util.spec_from_file_location("lifecycle_qualification", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_coordinator_signal_requires_exact_owned_identity(helper, monkeypatch):
    calls = []
    monkeypatch.setattr(helper, "identity", lambda pid: ("123", "boot"))
    monkeypatch.setattr(helper.os, "kill", lambda *args: calls.append(args))
    for pid, identity in [
        (200, ("old", "boot")),
        (200, ("123", "other-boot")),
        (1, ("123", "boot")),
    ]:
        with pytest.raises(ValueError):
            helper.signal_owned(pid, identity, signal.SIGTERM)
    assert calls == []
    helper.signal_owned(200, ["123", "boot"], signal.SIGTERM)
    assert calls == [(200, signal.SIGTERM)]


def test_group_inventory_includes_zombie_watchdog_and_excludes_unrelated(helper):
    rows = [
        {"pid": 200, "pgrp": 200, "state": "Z"},
        {"pid": 201, "pgrp": 200, "state": "Z"},
        {"pid": 202, "pgrp": 300, "state": "S"},
    ]
    assert helper.group_rows([{"pid": 200}], rows) == rows[:2]
    with pytest.raises(ValueError):
        asyncio.run(helper.absent([{"pid": 200}], seconds=0.001, read_rows=lambda: rows))


def test_absence_wait_requires_every_process_state_gone(helper):
    snapshots = [
        [{"pid": 201, "pgrp": 200, "state": "S"}],
        [{"pid": 201, "pgrp": 200, "state": "Z"}],
        [],
    ]
    asyncio.run(helper.absent([{"pid": 200}], read_rows=lambda: snapshots.pop(0)))


def test_exit_observation_records_zombie_or_absence_without_signals(helper):
    snapshots = [
        [{"pid": 200, "state": "S"}, {"pid": 300, "state": "Z"}],
        [{"pid": 200, "state": "Z"}],
    ]
    times = asyncio.run(
        helper.observed_exits([{"pid": 200}, {"pid": 300}], read_rows=lambda: snapshots.pop(0))
    )
    assert set(times) == {"200", "300"} and times["200"] >= times["300"]


def test_preserved_file_database_lease_holds_and_sentinel(helper, tmp_path):
    dbpath = tmp_path / "parentdeath.db"
    db = helper.database(dbpath, initialize=True)
    job, version = "a" * 32, "b" * 64
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires) VALUES(?,?,1,'RUNNING',0,1)",
        (job, version),
    )
    db.commit()
    db.close()
    reopened = helper.database(dbpath)
    try:
        assert helper.version_held(reopened, version)
        assert helper.lease_state(reopened) == {
            "rows": 1,
            "ready": 0,
            "running": 1,
            "reserved_bytes": 805306368,
        }
        reopened.execute("UPDATE web_playback_remux SET state='ERROR'")
        reopened.commit()
        assert not helper.version_held(reopened, version)
        assert helper.lease_state(reopened)["reserved_bytes"] == 0
    finally:
        reopened.close()
    sentinel = tmp_path / "unknown-preserve-sentinel"
    sentinel.mkdir()
    path = sentinel / "keep.txt"
    path.write_bytes(helper.SENTINEL)
    assert helper.sentinel_valid(tmp_path)
    path.write_bytes(b"changed")
    assert not helper.sentinel_valid(tmp_path)


def test_child_event_reader_rejects_missing_wrong_and_oversized_event(helper):
    class Reader:
        def __init__(self, data):
            self.data = data

        async def readline(self):
            return self.data

    class Child:
        def __init__(self, data):
            self.stdout = Reader(data)

    for data in (b"", b'{"event":"unexpected_ready"}\n', b" " * 16385 + b"\n"):
        with pytest.raises(ValueError):
            asyncio.run(helper.read_event(Child(data), "stalled"))
    assert asyncio.run(helper.read_event(Child(b'{"event":"stalled"}\n'), "stalled")) == {
        "event": "stalled"
    }


def test_deadline_receipt_requires_sigkill_for_both_media_children(helper):
    good = {
        "error_type": "RemuxError",
        "code": "deadline_exceeded",
        "child_returncodes": [
            {"mode": "probe", "returncode": 0},
            {"mode": "remux", "returncode": -9},
            {"mode": "observe", "returncode": -9},
        ],
    }
    helper.require_deadline_result(good)
    import copy

    for change in ("wrong_error", "missing_child", "wrong_signal", "normal_exit", "duplicate_role"):
        bad = copy.deepcopy(good)
        if change == "wrong_error":
            bad["code"] = "worker_failed"
        elif change == "missing_child":
            bad["child_returncodes"].pop()
        elif change == "duplicate_role":
            bad["child_returncodes"][2]["mode"] = "remux"
        else:
            bad["child_returncodes"][2]["returncode"] = (
                -signal.SIGTERM if change == "wrong_signal" else 0
            )
        with pytest.raises(ValueError):
            helper.require_deadline_result(bad)


def test_lifecycle_signal_precondition_requires_live_exact_media_workers(helper, monkeypatch):
    workers = [{"pid": 200, "identity": ["123", "boot"], "mode": "remux"}]
    monkeypatch.setattr(helper, "identity", lambda pid: ("123", "boot"))
    good = {"pid": 200, "state": "S", "start_ticks": "123"}
    helper.require_live_media(workers, [good])
    for rows in (
        [],
        [dict(good, state="Z")],
        [dict(good, state="X")],
        [dict(good, start_ticks="other")],
    ):
        with pytest.raises(ValueError):
            helper.require_live_media(workers, rows)
    monkeypatch.setattr(helper, "identity", lambda pid: ("123", "other-boot"))
    with pytest.raises(ValueError):
        helper.require_live_media(workers, [good])


def test_parentdeath_requires_substantial_margin_and_absence_before_watchdog(helper):
    helper.require_parentdeath_window(10, 8, 8.2)
    for killed, absent_at in ((9.01, None), (8, 10), (8, 10.01)):
        with pytest.raises(ValueError):
            helper.require_parentdeath_window(10, killed, absent_at)


def test_observability_trace_excludes_error_message_and_frame_locals(helper):
    private_value = "https://private.invalid/?token=never-print-this"
    try:
        raise ValueError(private_value)
    except ValueError as error:
        details = helper.failure_details(error)
    import json

    rendered = json.dumps(details)
    assert private_value not in rendered and "never-print-this" not in rendered
    assert details["failure_type"] == "ValueError"
    assert (
        details["sanitized_traceback"][-1]["function"]
        == "test_observability_trace_excludes_error_message_and_frame_locals"
    )
    assert set(details["sanitized_traceback"][-1]) == {"file", "function", "line"}
    assert "/" not in details["sanitized_traceback"][-1]["file"]
    with pytest.raises(helper.LifecycleFailure) as caught:
        helper.require(False, "parentdeath_missing_running_lease_or_hold")
    assert (
        helper.failure_details(caught.value)["invariant"]
        == "parentdeath_missing_running_lease_or_hold"
    )


def test_selected_cases_omit_qualified_deadline_without_omitting_parentdeath(helper):
    assert helper.selected_cases("parentdeath") == ("parentdeath",)
    assert helper.selected_cases("all") == ("deadline", "parentdeath")
    with pytest.raises(ValueError):
        helper.selected_cases("skip-reconciliation")


def test_reconciliation_diagnostics_are_read_only_and_describe_real_lease_and_scan(
    helper, tmp_path, monkeypatch
):
    from omakase.plus import web_playback_remux as remux

    db = helper.database(tmp_path / "parentdeath.db", initialize=True)
    cache = tmp_path / "cache"
    cache.mkdir()
    sentinel = cache / "unknown-preserve-sentinel"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_bytes(helper.SENTINEL)
    job, version = "a" * 32, "b" * 64
    folder = cache / job
    folder.mkdir()
    (folder / ".omakase-remux").write_text(job)
    (folder / "partial.m4s").write_bytes(b"synthetic partial")
    db.execute(
        "INSERT INTO web_playback_remux(job_id,media_version,actor_user_id,state,created,expires,deadline,pid,coordinator_pid,coordinator_token) VALUES(?,?,1,'RUNNING',0,1,2,100,200,'never-print-token')",
        (job, version),
    )
    db.commit()
    before = db.total_changes
    calls = []
    monkeypatch.setattr(helper, "processes", lambda: [])
    monkeypatch.setattr(remux, "_cwd_scan_absent", lambda path: calls.append(path) or True)
    try:
        value = helper.reconciliation_diagnostic(
            db, cache, {"job_id": job, "media_version": version}, []
        )
        assert db.total_changes == before and not db.in_transaction
        assert value["lease"]["running"] == 1 and value["held_version"]
        assert value["records"] == [
            {
                "state": "RUNNING",
                "phase": "admitted",
                "deadline": 2.0,
                "pid": 100,
                "coordinator_pid": 200,
            }
        ]
        assert value["owned_marker_valid"] and value["actual_cwd_scan_absent"]
        assert value["owned_entries"] == 2 and value["unknown_sentinel_preserved"]
        assert calls == [folder]
        import json

        assert "never-print-token" not in json.dumps(value)
        assert (folder / "partial.m4s").read_bytes() == b"synthetic partial"
    finally:
        db.close()


def test_unexpected_child_diagnostic_survives_parent_reader(helper):
    class Reader:
        async def readline(self):
            return b'{"status":"failed","failure_type":"ValueError","invariant":"child_assert","unrelated_private":"never-emit"}\n'

    class Child:
        stdout = Reader()

    with pytest.raises(helper.LifecycleFailure) as caught:
        asyncio.run(helper.read_event(Child(), "stalled"))
    value = helper.failure_details(caught.value)
    assert value["invariant"] == "unexpected_child_event"
    assert value["child_diagnostic"] == {"failure_type": "ValueError", "invariant": "child_assert"}
