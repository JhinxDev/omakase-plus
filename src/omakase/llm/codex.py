"""Subscription-backed private discovery, dispatched to the owner's workstation."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar

from omakase.llm.base import BaseLLM
from omakase.plus.codex_jobs import (
    MAX_DEADLINE,
    CodexJobError,
    cancel_job,
    enqueue,
    get_job,
)

_REQUEST: ContextVar[tuple[int, str | None] | None] = ContextVar("codex_request", default=None)


@contextmanager
def request_context(user_id: int, data_dir: str | None = None):
    """Bind a Plus user inside the actual discovery worker thread, never process env."""
    if type(user_id) is not int or user_id <= 0:
        raise CodexJobError("Codex discovery requires a signed-in Plus user.")
    token = _REQUEST.set((user_id, data_dir))
    try:
        yield
    finally:
        _REQUEST.reset(token)


class CodexLLM(BaseLLM):
    """Queue-only provider. No API credentials and no direct server-side inference."""

    timeout_seconds = MAX_DEADLINE
    poll_seconds = 1.0

    def __init__(self, url: str, model: str, api_key: str | None = None):
        super().__init__("", model, None)

    def generate(
        self,
        prompt: str,
        temperature: float = 0.4,
        num_ctx: int = 16384,
        supports_json: bool = True,
    ) -> str:
        from omakase.plus.db import _connect

        context = _REQUEST.get()
        if context is None:
            raise CodexJobError("Codex discovery is available only to a signed-in Plus user.")
        user_id, data_dir = context
        db = _connect(data_dir)
        job_id = None
        try:
            job_id = enqueue(db, user_id, prompt, self.model, "high", timeout=self.timeout_seconds)
            end = time.monotonic() + self.timeout_seconds
            while time.monotonic() < end:
                job = get_job(db, job_id, user_id)
                if job is None:
                    raise CodexJobError("Codex discovery job is unavailable.")
                if job["state"] == "completed":
                    return job["result"]
                if job["state"] in {"failed", "expired"}:
                    raise CodexJobError(job["error"] or "Codex discovery failed.")
                time.sleep(self.poll_seconds)
            raise CodexJobError("Codex discovery deadline expired. No API fallback was used.")
        finally:
            if job_id is not None:
                cancel_job(db, job_id, user_id)
            db.close()
