"""Versioned, resumable setup state with secret-free evidence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

SETUP_STATE_SCHEMA = "omakase-plus-setup-v1"
SETUP_STEPS = (
    "deployment",
    "storage",
    "plex",
    "acquisition",
    "identity",
    "readiness",
)
DEPLOYMENT_MODES = frozenset({"local", "real-debrid"})
STEP_STATUSES = frozenset({"pending", "ready", "blocked", "skipped"})
_UTC_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_FORBIDDEN_EVIDENCE_KEY = re.compile(
    r"(?:secret|token|password|credential|magnet|tracker|watch[_-]?history|filename)",
    re.IGNORECASE,
)
_IDENTITY_BEARING_VALUE = re.compile(
    r"(?i)(?:[/\\]|(?:^|\s)[^\s]+\.(?:mkv|mp4|m4v|avi|ass|ssa|srt|vtt)(?:\s|$))"
)


class SetupStateError(ValueError):
    """Setup state is invalid, stale, or contains unsafe evidence."""


@dataclass(frozen=True, slots=True)
class SetupStep:
    status: str = "pending"
    updated_at: str | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SetupState:
    schema_version: str
    revision: int
    deployment_mode: str
    started_at: str
    updated_at: str
    steps: dict[str, SetupStep]


def new_setup_state(*, deployment_mode: str, now: str) -> SetupState:
    """Create an in-memory setup state without touching the filesystem."""
    _timestamp(now)
    if deployment_mode not in DEPLOYMENT_MODES:
        raise SetupStateError("deployment mode must be local or real-debrid")
    return SetupState(
        schema_version=SETUP_STATE_SCHEMA,
        revision=1,
        deployment_mode=deployment_mode,
        started_at=now,
        updated_at=now,
        steps={step: SetupStep() for step in SETUP_STEPS},
    )


def update_setup_step(
    state: SetupState,
    *,
    step: str,
    status: str,
    now: str,
    evidence: tuple[str, ...] = (),
) -> SetupState:
    """Return a new revision for one setup step."""
    _validate_state(state)
    _timestamp(now)
    if step not in SETUP_STEPS:
        raise SetupStateError("setup step is unsupported")
    if status not in STEP_STATUSES:
        raise SetupStateError("setup step status is unsupported")
    cleaned = _evidence(evidence)
    steps = dict(state.steps)
    steps[step] = SetupStep(status=status, updated_at=now, evidence=cleaned)
    return replace(
        state,
        revision=state.revision + 1,
        updated_at=now,
        steps=steps,
    )


def load_setup_state(path: Path) -> SetupState:
    """Load and validate one setup state document."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SetupStateError("setup state is unavailable or invalid") from exc
    if not isinstance(payload, dict):
        raise SetupStateError("setup state must be a JSON object")
    try:
        raw_steps = payload["steps"]
        if not isinstance(raw_steps, dict):
            raise TypeError
        steps = {
            name: SetupStep(
                status=value["status"],
                updated_at=value.get("updated_at"),
                evidence=tuple(value.get("evidence", ())),
            )
            for name, value in raw_steps.items()
            if isinstance(value, dict)
        }
        state = SetupState(
            schema_version=payload["schema_version"],
            revision=payload["revision"],
            deployment_mode=payload["deployment_mode"],
            started_at=payload["started_at"],
            updated_at=payload["updated_at"],
            steps=steps,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SetupStateError("setup state fields are invalid") from exc
    _validate_state(state)
    return state


def save_setup_state(
    path: Path,
    state: SetupState,
    *,
    expected_revision: int | None = None,
) -> None:
    """Atomically persist state, optionally enforcing optimistic concurrency."""
    _validate_state(state)
    path = path.resolve()
    if path.exists() and expected_revision is not None:
        current = load_setup_state(path)
        if current.revision != expected_revision:
            raise SetupStateError("setup state changed since it was loaded")
    elif not path.exists() and expected_revision not in {None, 0}:
        raise SetupStateError("setup state revision does not exist")

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (
        json.dumps(_state_payload(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as output:
            temporary_name = output.name
            os.chmod(temporary_name, 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def setup_progress(state: SetupState) -> dict[str, int | bool]:
    """Return compact progress without exposing evidence."""
    _validate_state(state)
    ready = sum(item.status in {"ready", "skipped"} for item in state.steps.values())
    blocked = sum(item.status == "blocked" for item in state.steps.values())
    return {
        "complete": ready,
        "total": len(SETUP_STEPS),
        "blocked": blocked,
        "ready": ready == len(SETUP_STEPS) and blocked == 0,
    }


def _state_payload(state: SetupState) -> dict[str, Any]:
    payload = asdict(state)
    payload["steps"] = {name: asdict(state.steps[name]) for name in SETUP_STEPS}
    return payload


def _validate_state(state: SetupState) -> None:
    if state.schema_version != SETUP_STATE_SCHEMA:
        raise SetupStateError("setup state schema is unsupported")
    if isinstance(state.revision, bool) or state.revision <= 0:
        raise SetupStateError("setup state revision is invalid")
    if state.deployment_mode not in DEPLOYMENT_MODES:
        raise SetupStateError("setup state deployment mode is invalid")
    _timestamp(state.started_at)
    _timestamp(state.updated_at)
    if set(state.steps) != set(SETUP_STEPS):
        raise SetupStateError("setup state steps are incomplete")
    for step in SETUP_STEPS:
        value = state.steps[step]
        if value.status not in STEP_STATUSES:
            raise SetupStateError("setup state step status is invalid")
        if value.updated_at is not None:
            _timestamp(value.updated_at)
        _evidence(value.evidence)


def _evidence(values: tuple[str, ...]) -> tuple[str, ...]:
    if len(values) > 20:
        raise SetupStateError("setup evidence exceeded the safety limit")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise SetupStateError("setup evidence must be text")
        item = value.strip()
        if (
            not item
            or len(item) > 200
            or _FORBIDDEN_EVIDENCE_KEY.search(item)
            or _IDENTITY_BEARING_VALUE.search(item)
        ):
            raise SetupStateError("setup evidence is unsafe")
        if "magnet:?" in item.casefold() or "://" in item:
            raise SetupStateError("setup evidence cannot contain URLs")
        cleaned.append(item)
    return tuple(cleaned)


def _timestamp(value: str) -> None:
    if not isinstance(value, str) or not _UTC_RE.fullmatch(value):
        raise SetupStateError("setup timestamp must be canonical UTC")
