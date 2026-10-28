"""Bounded discovery requests and saved, user-scoped viewing preferences."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    source: Literal["anilist", "myanimelist"] = "anilist"
    username: str = Field(default="", max_length=100)
    provider: Literal["configured", "codex"] = "configured"
    mode: Literal["fast", "pro"] = "fast"
    model: str = Field(default="", max_length=128, pattern=r"^[a-zA-Z0-9._/:+\-]*$")
    count: StrictInt = Field(default=8, ge=3, le=15)
    temperature: float = Field(default=0.4, ge=0, le=1)
    lane: Literal["best_match", "new_seasons", "hidden_gems", "plan_list"] = "best_match"
    use_planning: StrictBool = False
    skip_profile: StrictBool = False
    max_episodes: StrictInt = Field(default=0, ge=0, le=10000)
    completed_only: StrictBool = False
    mood: str = Field(default="", max_length=500)


def saved_preferences(db, user_id: int) -> dict:
    row = db.execute(
        "SELECT preferences FROM discovery_preferences WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row:
        try:
            return DiscoveryRequest.model_validate_json(row["preferences"]).model_dump()
        except ValueError:
            pass
    return DiscoveryRequest().model_dump()


def save_preferences(db, user_id: int, request: DiscoveryRequest) -> None:
    # A transient request username is not an account configuration change.
    preferences = request.model_dump(exclude={"username"})
    db.execute(
        """INSERT INTO discovery_preferences (user_id, preferences)
           VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE
           SET preferences = excluded.preferences, updated_at = datetime('now')""",
        (user_id, json.dumps(preferences)),
    )
