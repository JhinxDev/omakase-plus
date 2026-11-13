"""Account-bound, offline control plane shared by the CLI and optional MCP adapter.

The operator's OS access authenticates this local process. No tool selects another
account, exports credentials, accepts SQL, starts subprocesses or contacts a provider.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from omakase.plus.discovery import DiscoveryRequest, save_preferences, saved_preferences
from omakase.plus.update_service import PRODUCT_VERSION

SCHEMA = "omakase-agent-v1"
WRITE_SCOPES = frozenset({"preferences", "monitors"})


class AgentError(ValueError):
    """A stable error with no submitted values or underlying exception text."""

    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(detail)


class PreferencesPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    count: StrictInt | None = Field(default=None, ge=3, le=15)
    lane: Literal["best_match", "new_seasons", "hidden_gems", "plan_list"] | None = None
    max_episodes: StrictInt | None = Field(default=None, ge=0, le=10000)
    completed_only: StrictBool | None = None
    mood: str | None = Field(default=None, max_length=500)


def _bound(value: int, maximum: int, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise AgentError("invalid_input", "A numeric argument is outside its supported bounds.")
    return value


class AgentService:
    """Fixed existing database/account; each call opens and closes its own connection."""

    def __init__(self, data_dir: str | Path, user_id: int, *, write_scopes=()):
        self.path = Path(data_dir).expanduser().resolve() / "omakase-plus.db"
        self.user_id = _bound(user_id, 2**63 - 1, minimum=1)
        self.write_scopes = frozenset(write_scopes)
        if not self.write_scopes <= WRITE_SCOPES:
            raise AgentError("invalid_scope", "An unsupported write scope was requested.")
        # Never create or migrate a database as an incidental tool invocation.
        with self._db():
            pass

    @contextmanager
    def _db(self, scope=None):
        if os.getenv("OMAKASE_PLUS_PRIVATE", "").strip().lower() != "true":
            raise AgentError("private_required", "Enable the private Plus runtime first.")
        if scope and scope not in self.write_scopes:
            raise AgentError("scope_denied", "The operator has not enabled this write scope.")
        if scope and os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            raise AgentError("review_read_only", "Review instances cannot change saved state.")
        conn = None
        try:
            uri = self.path.as_uri() + ("?mode=rw" if scope else "?mode=ro")
            conn = sqlite3.connect(uri, uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            if not scope:
                conn.execute("PRAGMA query_only=ON")
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (self.user_id,)).fetchone():
                raise AgentError("account_unavailable", "The configured account does not exist.")
            if scope:
                if not conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_audit'"
                ).fetchone():
                    raise AgentError(
                        "schema_upgrade_required",
                        "Start the updated app to apply migrations first.",
                    )
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if scope:
                conn.commit()
        except sqlite3.Error:
            raise AgentError(
                "database_unavailable",
                "The existing database is unavailable, busy or needs an app upgrade.",
            ) from None
        finally:
            if conn is not None:
                conn.close()

    def capabilities(self) -> dict:
        with self._db() as db:
            counts = {}
            for label, table in (
                ("anime", "media_monitors"),
                ("movies", "movie_catalog"),
                ("tv", "tv_catalog"),
            ):
                counts[label] = db.execute(
                    f"SELECT count(*) FROM {table} WHERE user_id=?", (self.user_id,)
                ).fetchone()[0]
        return {
            "schema_version": SCHEMA,
            "product_version": PRODUCT_VERSION,
            "account_id": self.user_id,
            "write_scopes": sorted(self.write_scopes),
            "counts": counts,
            "network": "offline",
            "transport": "local-stdio-or-cli",
            "limits": {"page_size": 100, "max_offset": 10000},
            "operator_commands": [
                "plus doctor",
                "plus support-bundle",
                "plus movies --help",
                "plus plan-local-import",
                "plus plan-plex-setup",
            ],
            "limitations": [
                "Cached observations are not current playback proof.",
                "Monitor activation can trigger the existing acquisition scheduler.",
                "No arbitrary shell, secret export, installation, deletion or automatic TV acquisition.",
            ],
        }

    def inventory(self, kind: str, limit: int = 25, offset: int = 0) -> dict:
        _bound(limit, 100, minimum=1)
        _bound(offset, 10000)
        queries = {
            "anime": "SELECT m.id AS monitor_id,m.anilist_id,m.title,m.season_number,m.expected_episodes,m.active,m.next_check_at,m.consecutive_failures,w.status AS watch_status,w.progress_episodes FROM media_monitors m LEFT JOIN watch_entries w ON w.monitor_id=m.id AND w.user_id=m.user_id WHERE m.user_id=? ORDER BY m.id LIMIT ? OFFSET ?",
            "movies": "SELECT c.imdb_id,c.tmdb_id,c.title,c.year,c.identity_verified,h.status AS watch_status,h.rating,r.state AS request_state,v.state AS verification_state,v.verified_at FROM movie_catalog c LEFT JOIN movie_history h ON h.user_id=c.user_id AND h.imdb_id=c.imdb_id LEFT JOIN movie_requests r ON r.user_id=c.user_id AND r.imdb_id=c.imdb_id LEFT JOIN movie_verifications v ON v.user_id=c.user_id AND v.imdb_id=c.imdb_id AND v.fingerprint=(SELECT v2.fingerprint FROM movie_verifications v2 WHERE v2.user_id=c.user_id AND v2.imdb_id=c.imdb_id ORDER BY v2.verified_at DESC,v2.fingerprint DESC LIMIT 1) WHERE c.user_id=? ORDER BY c.imdb_id LIMIT ? OFFSET ?",
            "tv": "SELECT c.tmdb_id,json_extract(c.metadata_json,'$.title') AS title,h.status AS watch_status,h.rating,c.updated_at FROM tv_catalog c LEFT JOIN tv_history h ON h.user_id=c.user_id AND h.tmdb_id=c.tmdb_id WHERE c.user_id=? ORDER BY c.tmdb_id LIMIT ? OFFSET ?",
        }
        if kind not in queries:
            raise AgentError("invalid_input", "Choose anime, movies or tv.")
        with self._db() as db:
            rows = [dict(r) for r in db.execute(queries[kind], (self.user_id, limit + 1, offset))]
        more = len(rows) > limit
        return {
            "kind": kind,
            "items": rows[:limit],
            "next_offset": offset + limit if more else None,
            "evidence": "cached; no network or playback verification",
        }

    def preferences(self) -> dict:
        with self._db() as db:
            result = saved_preferences(db, self.user_id)
        result.pop("username", None)
        return result

    def set_preferences(self, patch: PreferencesPatch) -> dict:
        changes = patch.model_dump(exclude_none=True)
        if not changes:
            raise AgentError("invalid_input", "Provide at least one supported preference.")
        with self._db("preferences") as db:
            combined = {**saved_preferences(db, self.user_id), **changes}
            save_preferences(db, self.user_id, DiscoveryRequest.model_validate(combined))
            self._audit(db, "preferences", "recommendation-defaults", sorted(changes))
        return self.preferences()

    def set_monitor_active(self, monitor_id: int, active: bool) -> dict:
        _bound(monitor_id, 2**63 - 1, minimum=1)
        if type(active) is not bool:
            raise AgentError("invalid_input", "Active must be a boolean.")
        with self._db("monitors") as db:
            row = db.execute(
                "SELECT active FROM media_monitors WHERE id=? AND user_id=?",
                (monitor_id, self.user_id),
            ).fetchone()
            if row is None:
                raise AgentError("not_found", "This account does not own that monitor.")
            changed = bool(row[0]) != active
            if changed:
                db.execute(
                    "UPDATE media_monitors SET active=?,updated_at=datetime('now') WHERE id=? AND user_id=?",
                    (int(active), monitor_id, self.user_id),
                )
                self._audit(db, "monitor_active", str(monitor_id), ["active"])
        return {"monitor_id": monitor_id, "active": active, "changed": changed}

    def audit(self, limit: int = 25, offset: int = 0) -> dict:
        _bound(limit, 100, minimum=1)
        _bound(offset, 10000)
        with self._db() as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_audit'"
            ).fetchone():
                return {"items": [], "schema_upgrade_required": True}
            rows = db.execute(
                "SELECT id,operation,target,changed_fields_json,created_at FROM agent_audit WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (self.user_id, limit, offset),
            ).fetchall()
        return {"items": [dict(r) for r in rows]}

    def _audit(self, db, operation, target, fields):
        db.execute(
            "INSERT INTO agent_audit(user_id,operation,target,changed_fields_json) VALUES(?,?,?,?)",
            (self.user_id, operation, target, json.dumps(fields)),
        )
