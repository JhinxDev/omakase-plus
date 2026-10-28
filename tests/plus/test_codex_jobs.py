from __future__ import annotations

import json
import sqlite3

import pytest

from omakase.plus.codex_jobs import (
    CodexJobError,
    enqueue,
    get_job,
    get_worker_status,
    handle_message,
    validate_result,
)
from omakase.plus.db import run_migrations


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    run_migrations(connection)
    connection.execute("INSERT INTO users(id,email,password_hash) VALUES (1,'a@example.test','x')")
    connection.execute("INSERT INTO users(id,email,password_hash) VALUES (2,'b@example.test','x')")
    connection.commit()
    yield connection
    connection.close()


def online(db, now=100):
    return handle_message(db, {"op": "heartbeat", "worker_id": "worker-1"}, now=now)


def claim(db, now=101, worker="worker-1"):
    return handle_message(db, {"op": "claim", "worker_id": worker}, now=now)["job"]


def lease(job, worker="worker-1"):
    return {"worker_id": worker, "job_id": job["id"], "lease_token": job["lease_token"]}


def valid_result():
    return json.dumps(
        {
            "recommendations": [
                {
                    "anilist_id": 123,
                    "title": "Example",
                    "predicted_score": 8.2,
                    "reasoning": "Candidate evidence.",
                    "best_match_from_history": "Past favorite",
                }
            ]
        }
    )


def test_offline_and_expired_heartbeat_fail_closed(db):
    with pytest.raises(CodexJobError, match="offline"):
        enqueue(db, 1, "private prompt", "gpt-6-astra", now=100)
    online(db)
    assert get_worker_status(db, now=144)["online"]
    assert not get_worker_status(db, now=145)["online"]
    with pytest.raises(CodexJobError, match="offline"):
        enqueue(db, 1, "private prompt", "gpt-6-astra", now=145)


def test_user_scope_single_pending_and_global_serialization(db):
    online(db)
    first = enqueue(db, 1, "first", "gpt-6-astra", now=100)
    with pytest.raises(CodexJobError, match="queued"):
        enqueue(db, 1, "duplicate", "gpt-6-astra", now=100)
    enqueue(db, 2, "second", "gpt-6.1-sol", now=101)
    job = claim(db, now=102)
    assert job["id"] == first
    assert claim(db, now=103, worker="other") is None
    assert get_job(db, first, 2, now=103) is None
    assert get_job(db, first, 1, now=103)["state"] == "running"


def test_wrong_worker_token_late_result_and_prompt_erasure(db):
    online(db)
    job_id = enqueue(db, 1, "private prompt", "gpt-6-astra", now=100)
    job = claim(db)
    wrong = handle_message(
        db, {"op": "finish", **lease(job, "other"), "result": valid_result()}, now=102
    )
    assert not wrong["ok"]
    assert not handle_message(
        db,
        {"op": "finish", **lease(job), "lease_token": "wrong", "result": valid_result()},
        now=102,
    )["ok"]
    assert not handle_message(
        db, {"op": "finish", **lease(job), "result": valid_result()}, now=146
    )["ok"]
    row = db.execute("SELECT * FROM codex_discovery_jobs WHERE id=?", (job_id,)).fetchone()
    assert row["state"] == "expired"
    assert row["prompt"] == "" and row["result"] is None and row["lease_token"] is None


def test_heartbeat_extends_lease_but_not_deadline(db):
    online(db)
    enqueue(db, 1, "private prompt", "gpt-6-astra", timeout=60, now=100)
    job = claim(db)
    assert handle_message(db, {"op": "heartbeat", **lease(job)}, now=130)["ok"]
    row = db.execute("SELECT lease_expires_at FROM codex_discovery_jobs").fetchone()
    assert row[0] == 160
    assert not handle_message(
        db, {"op": "finish", **lease(job), "result": valid_result()}, now=160
    )["ok"]


def test_terminal_result_and_failure_clear_prompt_and_reject_replay(db):
    online(db)
    first = enqueue(db, 1, "private prompt", "gpt-6-astra", now=100)
    job = claim(db)
    assert handle_message(db, {"op": "finish", **lease(job), "result": valid_result()}, now=102)[
        "ok"
    ]
    assert get_job(db, first, 1, now=103)["state"] == "completed"
    assert not handle_message(
        db, {"op": "finish", **lease(job), "result": valid_result()}, now=103
    )["ok"]
    enqueue(db, 2, "other prompt", "gpt-6-sol", now=104)
    other = claim(db, now=105)
    assert handle_message(db, {"op": "fail", **lease(other), "error": "timeout"}, now=106)["ok"]
    assert all(row[0] == "" for row in db.execute("SELECT prompt FROM codex_discovery_jobs"))
    assert "No API fallback" in get_job(db, other["id"], 2, now=107)["error"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"op": []},
        {"op": "purge"},
        {"op": "status", "user_id": 1},
        {"op": "claim", "worker_id": ";rm"},
        {"op": "finish", "worker_id": "ok"},
    ],
)
def test_malformed_protocol(db, payload):
    with pytest.raises(CodexJobError):
        handle_message(db, payload, now=100)


@pytest.mark.parametrize(
    "raw", ["{}", "[]", "{bad", '{"recommendations":[{}]}', '{"recommendations":[],"extra":1}']
)
def test_invalid_result(raw):
    with pytest.raises(CodexJobError):
        validate_result(raw)


def test_duplicate_id_boolean_id_nan_and_arbitrary_failure_text_rejected(db):
    base = json.loads(valid_result())
    base["recommendations"].append(base["recommendations"][0])
    with pytest.raises(CodexJobError, match="duplicate"):
        validate_result(json.dumps(base))
    for key, value in [("anilist_id", True), ("predicted_score", float("nan"))]:
        base = json.loads(valid_result())
        base["recommendations"][0][key] = value
        with pytest.raises(CodexJobError):
            validate_result(json.dumps(base))
    online(db)
    enqueue(db, 1, "private", "gpt-6-astra", now=100)
    job = claim(db)
    with pytest.raises(CodexJobError):
        handle_message(db, {"op": "fail", **lease(job), "error": "secret arbitrary text"}, now=102)


def test_bounds_and_queue_deadline(db):
    online(db)
    for model, prompt, timeout in [
        ("unknown", "valid", 60),
        ("gpt-6-astra", "x" * 120001, 60),
        ("gpt-6-astra", "valid", 601),
    ]:
        with pytest.raises(CodexJobError):
            enqueue(db, 1, prompt, model, timeout=timeout, now=100)
    job = enqueue(db, 1, "private", "gpt-6-astra", timeout=1, now=100)
    assert get_job(db, job, 1, now=101)["state"] == "expired"
    assert db.execute("SELECT prompt FROM codex_discovery_jobs").fetchone()[0] == ""
