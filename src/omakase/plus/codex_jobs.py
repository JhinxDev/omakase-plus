"""Private, bounded discovery queue. SSH module protocol never exposes credentials."""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import time
import uuid

MODELS = frozenset({"gpt-6-astra", "gpt-6.1-sol", "gpt-6-sol"})
EFFORTS = frozenset({"medium", "high", "xhigh"})
WORKER_TTL = 45
LEASE_TTL = 45
MAX_DEADLINE = 600
MAX_PROMPT_BYTES = 120_000
MAX_RESULT_BYTES = 64_000
MAX_PROTOCOL_BYTES = 200_000
MAX_PENDING = 4
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")

# Matching the engine's existing response contract keeps the bridge provider-neutral.
OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendations": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "anilist_id": {"type": "integer", "minimum": 1},
                    "title": {"type": "string", "maxLength": 500},
                    "predicted_score": {"type": "number", "minimum": 0, "maximum": 10},
                    "reasoning": {"type": "string", "maxLength": 4000},
                    "best_match_from_history": {"type": "string", "maxLength": 500},
                },
                "required": [
                    "anilist_id",
                    "title",
                    "predicted_score",
                    "reasoning",
                    "best_match_from_history",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["recommendations"],
    "additionalProperties": False,
}


class CodexJobError(RuntimeError):
    """A safe, user-displayable queue/worker failure."""


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise CodexJobError("Invalid worker/job identifier.")
    return value


def validate_result(raw: object) -> str:
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_RESULT_BYTES:
        raise CodexJobError("Codex result exceeds the allowed size.")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise CodexJobError("Codex returned invalid JSON.") from exc
    if not isinstance(data, dict) or set(data) != {"recommendations"}:
        raise CodexJobError("Codex returned an invalid recommendation object.")
    recs = data["recommendations"]
    if not isinstance(recs, list) or len(recs) > 20:
        raise CodexJobError("Codex returned too many recommendations.")
    seen = set()
    required = set(OUTPUT_SCHEMA["properties"]["recommendations"]["items"]["required"])
    for rec in recs:
        if not isinstance(rec, dict) or set(rec) != required:
            raise CodexJobError("Codex returned invalid recommendation fields.")
        media_id, score = rec["anilist_id"], rec["predicted_score"]
        if type(media_id) is not int or media_id <= 0 or media_id in seen:
            raise CodexJobError("Codex returned invalid or duplicate anime IDs.")
        seen.add(media_id)
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 10:
            raise CodexJobError("Codex returned an invalid predicted score.")
        for key, limit in [("title", 500), ("reasoning", 4000), ("best_match_from_history", 500)]:
            if not isinstance(rec[key], str) or not rec[key].strip() or len(rec[key]) > limit:
                raise CodexJobError("Codex returned invalid recommendation text.")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _expire(db: sqlite3.Connection, now: float) -> None:
    db.execute(
        "UPDATE codex_discovery_jobs SET state='expired', prompt='', result=NULL, "
        "error='Codex discovery deadline or worker lease expired.', lease_token=NULL "
        "WHERE state IN ('queued','running') AND (deadline_at<=? OR "
        "(state='running' AND lease_expires_at<=?))",
        (now, now),
    )
    # Compact private queue retention; terminal prompts are already erased immediately.
    db.execute(
        "DELETE FROM codex_discovery_jobs WHERE state NOT IN ('queued','running') AND created_at<?",
        (now - 7 * 86400,),
    )
    db.execute("DELETE FROM codex_workers WHERE heartbeat_at<?", (now - 86400,))


def get_worker_status(db: sqlite3.Connection, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    latest = db.execute("SELECT MAX(heartbeat_at) FROM codex_workers").fetchone()[0]
    counts = dict(
        db.execute(
            "SELECT state, COUNT(*) FROM codex_discovery_jobs "
            "WHERE deadline_at>? AND (state='queued' OR "
            "(state='running' AND lease_expires_at>?)) GROUP BY state",
            (now, now),
        )
    )
    return {
        "online": latest is not None and now - WORKER_TTL < latest <= now + 5,
        "last_seen_at": latest,
        "active_jobs": counts.get("running", 0),
        "queued_jobs": counts.get("queued", 0),
    }


def enqueue(
    db: sqlite3.Connection,
    user_id: int,
    prompt: str,
    model: str,
    effort: str = "high",
    *,
    timeout: int = MAX_DEADLINE,
    now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    if type(user_id) is not int or user_id <= 0:
        raise CodexJobError("Codex discovery requires a signed-in Plus user.")
    if model not in MODELS or effort not in EFFORTS:
        raise CodexJobError("Unsupported Codex model or reasoning effort.")
    if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise CodexJobError("Codex discovery prompt exceeds the allowed size.")
    if type(timeout) is not int or not 1 <= timeout <= MAX_DEADLINE:
        raise CodexJobError("Invalid Codex discovery deadline.")
    job_id = uuid.uuid4().hex
    try:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        if not get_worker_status(db, now=now)["online"]:
            raise CodexJobError("Codex workstation worker is offline. Start it before discovering.")
        rows = db.execute(
            "SELECT user_id FROM codex_discovery_jobs WHERE state IN ('queued','running')"
        ).fetchall()
        if len(rows) >= MAX_PENDING or any(row[0] == user_id for row in rows):
            raise CodexJobError("Codex discovery is already queued. Please wait for it to finish.")
        db.execute(
            "INSERT INTO codex_discovery_jobs "
            "(id,user_id,state,model,effort,prompt,created_at,deadline_at) "
            "VALUES (?,?,'queued',?,?,?,?,?)",
            (job_id, user_id, model, effort, prompt, now, now + timeout),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return job_id


def get_job(
    db: sqlite3.Connection, job_id: str, user_id: int, *, now: float | None = None
) -> dict | None:
    with db:
        _expire(db, time.time() if now is None else now)
    row = db.execute(
        "SELECT id,state,result,error,deadline_at FROM codex_discovery_jobs "
        "WHERE id=? AND user_id=?",
        (job_id, user_id),
    ).fetchone()
    return dict(row) if row else None


def cancel_job(db: sqlite3.Connection, job_id: str, user_id: int) -> None:
    with db:
        db.execute(
            "UPDATE codex_discovery_jobs SET state='expired',prompt='',result=NULL,"
            "error='Codex discovery deadline expired.',lease_token=NULL "
            "WHERE id=? AND user_id=? AND state IN ('queued','running')",
            (job_id, user_id),
        )


def handle_message(db: sqlite3.Connection, message: object, *, now: float | None = None) -> dict:
    """Only SSH-authorized operators invoke this protocol, never browser clients."""
    now = time.time() if now is None else now
    if not isinstance(message, dict):
        raise CodexJobError("Expected a protocol object.")
    op = message.get("op")
    fields = {
        "status": {"op"},
        "claim": {"op", "worker_id"},
        "heartbeat": {"op", "worker_id", "job_id", "lease_token"},
        "finish": {"op", "worker_id", "job_id", "lease_token", "result"},
        "fail": {"op", "worker_id", "job_id", "lease_token", "error"},
    }
    if not isinstance(op, str) or op not in fields or set(message) - fields[op]:
        raise CodexJobError("Unsupported protocol operation or fields.")
    if op == "status":
        return {"ok": True, **get_worker_status(db, now=now)}
    worker_id = _identifier(message.get("worker_id"))
    if op in {"finish", "fail"} or (op == "heartbeat" and message.get("job_id") is not None):
        _identifier(message.get("job_id"))
        _identifier(message.get("lease_token"))
    elif op == "heartbeat" and message.get("lease_token") is not None:
        raise CodexJobError("Job ID required with a lease token.")
    try:
        db.execute("BEGIN IMMEDIATE")
        _expire(db, now)
        if op in {"claim", "heartbeat"}:
            db.execute(
                "INSERT INTO codex_workers(id,heartbeat_at) VALUES (?,?) "
                "ON CONFLICT(id) DO UPDATE SET heartbeat_at=excluded.heartbeat_at",
                (worker_id, now),
            )
        if op == "claim":
            # Global serialization avoids spending across multiple workstation processes.
            if db.execute("SELECT 1 FROM codex_discovery_jobs WHERE state='running'").fetchone():
                response = {"ok": True, "job": None}
            else:
                row = db.execute(
                    "SELECT * FROM codex_discovery_jobs WHERE state='queued' "
                    "ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if row is None:
                    response = {"ok": True, "job": None}
                else:
                    token = uuid.uuid4().hex
                    lease = min(now + LEASE_TTL, row["deadline_at"])
                    db.execute(
                        "UPDATE codex_discovery_jobs SET state='running',worker_id=?,"
                        "lease_token=?,lease_expires_at=? WHERE id=?",
                        (worker_id, token, lease, row["id"]),
                    )
                    response = {
                        "ok": True,
                        "job": {
                            key: row[key]
                            for key in ("id", "model", "effort", "prompt", "deadline_at")
                        },
                    }
                    response["job"].update(
                        lease_token=token,
                        lease_expires_at=lease,
                        remaining_seconds=max(0, row["deadline_at"] - now),
                    )
        elif op == "heartbeat" and not message.get("job_id"):
            response = {"ok": True, **get_worker_status(db, now=now)}
        else:
            row = db.execute(
                "SELECT * FROM codex_discovery_jobs WHERE id=? AND state='running' "
                "AND worker_id=? AND lease_token=?",
                (message["job_id"], worker_id, message["lease_token"]),
            ).fetchone()
            if row is None:
                response = {"ok": False, "error": "Job lease is unavailable or expired."}
            elif op == "heartbeat":
                db.execute(
                    "UPDATE codex_discovery_jobs SET lease_expires_at=? WHERE id=?",
                    (min(now + LEASE_TTL, row["deadline_at"]), row["id"]),
                )
                response = {"ok": True, "remaining_seconds": row["deadline_at"] - now}
            else:
                result = validate_result(message.get("result")) if op == "finish" else None
                error = None if op == "finish" else _safe_error(message.get("error"))
                db.execute(
                    "UPDATE codex_discovery_jobs SET state=?,result=?,error=?,prompt='',"
                    "lease_token=NULL WHERE id=?",
                    ("completed" if op == "finish" else "failed", result, error, row["id"]),
                )
                response = {"ok": True}
        db.commit()
        return response
    except Exception:
        db.rollback()
        raise


def _safe_error(error: object) -> str:
    allowed = {
        "timeout",
        "output_limit",
        "invalid_result",
        "cli_failed",
        "lease_lost",
        "worker_error",
        "subscription_auth_required",
    }
    if not isinstance(error, str) or error not in allowed:
        raise CodexJobError("Unsupported worker error code.")
    return f"Codex workstation discovery failed ({error}). No API fallback was used."


def main() -> int:
    from omakase.plus.db import _connect

    try:
        raw = sys.stdin.buffer.read(MAX_PROTOCOL_BYTES + 1)
        if len(raw) > MAX_PROTOCOL_BYTES:
            raise CodexJobError("Protocol input exceeds the allowed size.")
        message = json.loads(raw)
        db = _connect()
        try:
            response = handle_message(db, message)
        finally:
            db.close()
    except (CodexJobError, ValueError, TypeError, KeyError):
        response = {"ok": False, "error": "Invalid or rejected Codex worker request."}
    except Exception:
        response = {"ok": False, "error": "Codex queue unavailable."}
    print(json.dumps(response, ensure_ascii=False))
    return 0 if response.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
