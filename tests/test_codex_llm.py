from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from omakase.llm.codex import _REQUEST, CodexLLM, request_context
from omakase.plus.codex_jobs import CodexJobError, handle_message
from omakase.plus.codex_worker import (
    WorkerError,
    _cli_env,
    _require_subscription,
    remote_call,
    run_codex,
)


@pytest.fixture(autouse=True)
def no_real_login_checks(monkeypatch):
    monkeypatch.setattr("omakase.plus.codex_worker._require_subscription", lambda command: None)


def job():
    return {
        "model": "gpt-6-astra",
        "effort": "high",
        "prompt": "SHELL $(secret) `value`",
        "remaining_seconds": 60,
    }


def result():
    return json.dumps(
        {
            "recommendations": [
                {
                    "anilist_id": 1,
                    "title": "Anime",
                    "predicted_score": 9,
                    "reasoning": "Grounded reason",
                    "best_match_from_history": "History",
                }
            ]
        }
    )


def synthetic_script(raw=None, extra=""):
    raw = result() if raw is None else raw
    return (
        "import sys,json; from pathlib import Path; "
        "prompt=sys.stdin.read(); assert '$(secret)' in prompt; "
        "p=Path(sys.argv[sys.argv.index('--output-last-message')+1]); "
        f'p.write_text({raw!r},encoding="utf-8"); '
        f"{extra}"
        'print(json.dumps({"type":"turn.completed"}),flush=True)'
    )


def test_subscription_backend_ignores_keys_and_requires_user_context():
    llm = CodexLLM("https://malicious.invalid", "gpt-6-astra", "never-use-this-key")
    assert llm.api_key is None and llm.url == ""
    with pytest.raises(CodexJobError, match="signed-in"):
        llm.generate("prompt")
    with request_context(1, "private-data"):
        assert _REQUEST.get() == (1, "private-data")
        with request_context(2):
            assert _REQUEST.get() == (2, None)
        assert _REQUEST.get() == (1, "private-data")
    assert _REQUEST.get() is None


def _user_queue(tmp_path):
    from omakase.plus.db import _connect

    db = _connect(str(tmp_path))
    db.execute("INSERT INTO users (id,email,password_hash) VALUES (1,'private@example.test','x')")
    db.commit()
    handle_message(db, {"op": "heartbeat", "worker_id": "synthetic"})
    return db


def test_provider_waits_for_owned_queue_result_and_preserves_terminal_state(tmp_path):
    db = _user_queue(tmp_path)

    def synthetic_finish(_seconds):
        claimed = handle_message(db, {"op": "claim", "worker_id": "synthetic"})["job"]
        assert claimed["prompt"] == "private candidate prompt"
        handle_message(
            db,
            {
                "op": "finish",
                "worker_id": "synthetic",
                "job_id": claimed["id"],
                "lease_token": claimed["lease_token"],
                "result": result(),
            },
        )

    try:
        with (
            request_context(1, str(tmp_path)),
            patch("omakase.llm.codex.time.sleep", side_effect=synthetic_finish),
        ):
            response = CodexLLM("", "gpt-6-astra").generate("private candidate prompt")
        assert json.loads(response)["recommendations"][0]["anilist_id"] == 1
        row = db.execute("SELECT state,prompt FROM codex_discovery_jobs").fetchone()
        assert tuple(row) == ("completed", "")
    finally:
        db.close()


def test_provider_timeout_cancels_queue_and_erases_prompt(tmp_path):
    db = _user_queue(tmp_path)
    try:
        with (
            request_context(1, str(tmp_path)),
            patch("omakase.llm.codex.time.monotonic", side_effect=[0, 1000]),
        ):
            with pytest.raises(CodexJobError, match="deadline"):
                CodexLLM("", "gpt-6-astra").generate("private candidate prompt")
        assert tuple(db.execute("SELECT state,prompt FROM codex_discovery_jobs").fetchone()) == (
            "expired",
            "",
        )
    finally:
        db.close()


def test_success_uses_stdin_schema_no_shell_empty_cwd_and_scrubbed_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-forward")
    monkeypatch.setenv("CODEX_API_KEY", "must-not-forward")
    monkeypatch.setenv("OMAKASE_API_KEY", "must-not-forward")
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "must-not-forward")
    assert not any("API_KEY" in key or "ACCESS_TOKEN" in key for key in _cli_env())
    original = subprocess.Popen
    observed = []

    def inspect(args, **kwargs):
        observed.append((args, kwargs))
        assert kwargs["shell"] is False
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert "--ignore-user-config" in args and "--ephemeral" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert job()["prompt"] not in args
        assert kwargs["stdin"].read().decode().endswith(job()["prompt"])
        kwargs["stdin"].seek(0)
        assert {path.name for path in Path(kwargs["cwd"]).iterdir()} == {"response.schema.json"}
        return original(args, **kwargs)

    with patch("omakase.plus.codex_worker.subprocess.Popen", side_effect=inspect):
        response = run_codex(job(), [sys.executable, "-c", synthetic_script()], lambda: True)
    assert json.loads(response)["recommendations"][0]["anilist_id"] == 1
    assert len(observed) == 1
    assert not Path(observed[0][1]["cwd"]).exists()


def test_cli_timeout_and_lease_rejection_stop_process():
    sleep = [sys.executable, "-c", "import time; time.sleep(30)"]
    with pytest.raises(WorkerError, match="timeout"):
        run_codex(job(), sleep, lambda: True, timeout=0.1)
    receipts = iter([True, False])
    with pytest.raises(WorkerError, match="lease_lost"):
        run_codex(job(), sleep, lambda: next(receipts), timeout=3)


def test_failed_cli_and_invalid_result_are_not_success():
    with pytest.raises(WorkerError, match="cli_failed"):
        run_codex(job(), [sys.executable, "-c", "raise SystemExit(2)"], lambda: True)
    with pytest.raises(WorkerError, match="invalid_result"):
        run_codex(job(), [sys.executable, "-c", synthetic_script("{}")], lambda: True)


def test_output_limit_rejects_even_zero_exit():
    script = synthetic_script(extra="print('x'*600000,flush=True); ")
    with pytest.raises(WorkerError, match="output_limit"):
        run_codex(job(), [sys.executable, "-c", script], lambda: True)


def test_ssh_protocol_is_fixed_command_and_json_stdin():
    def run(args, **kwargs):
        assert kwargs["shell"] is False
        assert args[-3:] == ["python", "-m", "omakase.plus.codex_jobs"]
        assert json.loads(kwargs["stdin"].read()) == {"op": "status"}
        kwargs["stdout"].write(b'{"ok":true,"online":false}')
        return subprocess.CompletedProcess(args, 0)

    with patch("omakase.plus.codex_worker.subprocess.run", side_effect=run):
        assert not remote_call("approved-alias", {"op": "status"})["online"]
    with pytest.raises(WorkerError, match="SSH alias"):
        remote_call("-oProxyCommand=evil", {"op": "status"})


def test_remote_timeout_sanitizes_error():
    with patch(
        "omakase.plus.codex_worker.subprocess.run",
        side_effect=subprocess.TimeoutExpired("secret command", 15),
    ):
        with pytest.raises(WorkerError, match="unavailable"):
            remote_call("approved-alias", {"op": "status"})


@pytest.mark.parametrize(
    "receipt,code,accepted",
    [
        (b"Logged in using ChatGPT", 0, True),
        (b"Logged in using an API key - masked", 0, False),
        (b"Unknown login mode", 0, False),
        (b"Not logged in", 1, False),
    ],
)
def test_login_type_preflight_rejects_api_and_unknown_without_logging(receipt, code, accepted):
    def run(args, **kwargs):
        assert args[-2:] == ["login", "status"] and kwargs["shell"] is False
        kwargs["stderr"].write(receipt)
        return subprocess.CompletedProcess(args, code)

    # Imported function remains the original; worker module lookup is stubbed
    # for synthetic inference tests above.
    with patch("omakase.plus.codex_worker.subprocess.run", side_effect=run):
        if accepted:
            _require_subscription(["codex"])
        else:
            with pytest.raises(WorkerError, match="subscription_auth_required"):
                _require_subscription(["codex"])


def test_worker_shutdown_leaves_late_queued_request_for_next_child(monkeypatch):
    from omakase.plus import codex_worker as worker

    clock = iter([0.0, 1700.0, 1700.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker, "codex_command", lambda _path: ["synthetic-codex"])
    calls = []
    monkeypatch.setattr(worker, "remote_call", lambda *args, **kwargs: calls.append(args))
    assert worker.main(["--ssh-alias", "synthetic-alias"]) == 0
    # With only 100 seconds of the 30-minute window remaining, the server is
    # never asked to consume a request that needs a 300-second inference budget.
    assert calls == []


def test_worker_admitted_request_receives_its_full_budget(monkeypatch):
    from omakase.plus import codex_worker as worker

    clock = iter([0.0, 1490.0, 1490.0, 1490.0])
    monkeypatch.setattr(worker.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(worker, "codex_command", lambda _path: ["synthetic-codex"])
    messages = []

    def remote(_alias, message, **kwargs):
        messages.append(message)
        if message["op"] == "claim":
            return {"ok": True, "job": {"id": 1, "lease_token": "synthetic"}}
        return {"ok": True}

    def inference(_job, _command, _heartbeat, *, timeout):
        assert timeout == 300
        return result()

    monkeypatch.setattr(worker, "remote_call", remote)
    monkeypatch.setattr(worker, "run_codex", inference)
    assert worker.main(["--ssh-alias", "synthetic-alias", "--once"]) == 0
    assert [message["op"] for message in messages] == ["claim", "finish"]
