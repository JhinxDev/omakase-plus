"""Additive, offline household core. Existing playback/acquisition adapters are not enabled."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


def feature_enabled() -> bool:
    """Opt-in deployment gate. Never enabled by membership or a request parameter."""
    return all(
        os.getenv(name, "").strip().casefold() in {"1", "true", "yes", "on"}
        for name in ("OMAKASE_PLUS_PRIVATE", "OMAKASE_PLUS_HOUSEHOLDS_ENABLED")
    )


class HouseholdError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _require(ok: bool, message: str, status: int = 409) -> None:
    if not ok:
        raise HouseholdError(message, status)


def _id() -> str:
    return uuid.uuid4().hex


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@contextmanager
def _transaction(db):
    nested = db.in_transaction
    savepoint = "household_" + _id()
    db.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
    try:
        yield
        db.execute("RELEASE SAVEPOINT " + savepoint) if nested else db.commit()
    except BaseException:
        if nested:
            db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
            db.execute("RELEASE SAVEPOINT " + savepoint)
        else:
            db.rollback()
        raise


@dataclass(frozen=True)
class LibraryScope:
    library_id: str
    kind: str
    viewer_id: int
    role: str
    operational_owner_id: int
    membership_revision: int
    policy_revision: int
    members_can_add: bool
    acquisition_requests: bool


def resolve_scope(db, *, viewer_id: int, library_id: str) -> LibraryScope:
    row = db.execute(
        "SELECT * FROM libraries WHERE id=? AND state='active'", (library_id,)
    ).fetchone()
    _require(row is not None, "Library not available.", 404)
    role = "owner"
    if row["kind"] == "personal":
        _require(row["owner_user_id"] == viewer_id, "Library not available.", 404)
    else:
        member = db.execute(
            "SELECT role FROM household_memberships WHERE library_id=? AND user_id=? AND state='active'",
            (library_id, viewer_id),
        ).fetchone()
        _require(member is not None, "Library not available.", 404)
        role = member["role"]
    return LibraryScope(
        library_id,
        row["kind"],
        viewer_id,
        role,
        row["owner_user_id"],
        row["membership_revision"],
        row["policy_revision"],
        bool(row["members_can_add"]),
        bool(row["acquisition_requests"]),
    )


def revalidate_scope(db, scope: LibraryScope, *, owner: bool = False) -> LibraryScope:
    current = resolve_scope(db, viewer_id=scope.viewer_id, library_id=scope.library_id)
    _require(current == scope, "Library permissions changed. Reload before continuing.")
    _require(not owner or current.role == "owner", "Only the library owner can do this.", 403)
    return current


def ensure_personal_library(db, *, user_id: int) -> str:
    _require(
        db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone() is not None,
        "Account not available.",
        404,
    )
    row = db.execute(
        "SELECT library_id FROM personal_libraries WHERE user_id=?", (user_id,)
    ).fetchone()
    if row:
        return row[0]
    library_id = _id()
    db.execute(
        "INSERT INTO libraries(id,kind,owner_user_id,name) VALUES(?,'personal',?,'Personal library')",
        (library_id, user_id),
    )
    db.execute(
        "INSERT INTO personal_libraries(user_id,library_id) VALUES(?,?)", (user_id, library_id)
    )
    db.execute(
        "INSERT OR IGNORE INTO library_selections(user_id,library_id) VALUES(?,?)",
        (user_id, library_id),
    )
    return library_id


def select_library(db, scope: LibraryScope) -> None:
    with _transaction(db):
        revalidate_scope(db, scope)
        db.execute(
            "INSERT INTO library_selections(user_id,library_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET library_id=excluded.library_id",
            (scope.viewer_id, scope.library_id),
        )


def selected_scope(db, *, viewer_id: int) -> LibraryScope:
    row = db.execute(
        "SELECT library_id FROM library_selections WHERE user_id=?", (viewer_id,)
    ).fetchone()
    if row is not None:
        try:
            return resolve_scope(db, viewer_id=viewer_id, library_id=row[0])
        except HouseholdError:
            pass
    personal = db.execute(
        "SELECT library_id FROM personal_libraries WHERE user_id=?", (viewer_id,)
    ).fetchone()
    _require(personal is not None, "Personal library has not been initialized.", 404)
    return resolve_scope(db, viewer_id=viewer_id, library_id=personal[0])


def overview(db, *, viewer_id: int) -> dict[str, Any]:
    row = db.execute(
        "SELECT l.id,l.name,m.role,l.members_can_add,l.acquisition_requests FROM household_memberships m JOIN libraries l ON l.id=m.library_id WHERE m.user_id=? AND m.state='active' AND l.state='active'",
        (viewer_id,),
    ).fetchone()
    if row is None:
        return {"household": None}
    members = db.execute(
        "SELECT m.user_id,m.role,m.display_name,m.joined_at FROM household_memberships m WHERE m.library_id=? AND m.state='active' ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END,joined_at",
        (row["id"],),
    ).fetchall()
    # No emails, names, private progress or notifications from other accounts.
    return {
        "household": dict(row),
        "members": [
            {
                "member_id": m["user_id"],
                "label": "You"
                if m["user_id"] == viewer_id
                else "Library owner"
                if m["role"] == "owner"
                else m["display_name"],
                "role": m["role"],
                "joined_at": m["joined_at"],
            }
            for m in members
        ],
    }


def promotion_preview(db, *, user_id: int) -> dict[str, Any]:
    """Actual operational inventory/requests, never history-only catalog rows."""
    items = []
    for row in db.execute(
        "SELECT i.id,i.domain,i.canonical_id,i.season_number,i.title FROM library_items i JOIN personal_libraries p ON p.library_id=i.library_id WHERE p.user_id=? AND i.state='active' ORDER BY i.id LIMIT 1001",
        (user_id,),
    ):
        items.append(
            dict(
                domain=row["domain"],
                canonical_id=row["canonical_id"],
                season_number=row["season_number"],
                title=row["title"],
                legacy_id=None,
                evidence="Explicit personal catalog membership",
                existing_item=row["id"],
            )
        )
    for row in db.execute(
        """SELECT m.id,m.anilist_id,m.season_number,m.title FROM media_monitors m WHERE m.user_id=? AND (m.active=1 OR EXISTS(SELECT 1 FROM media_assets a WHERE a.monitor_id=m.id AND a.status!='removed')) ORDER BY m.id LIMIT 1001""",
        (user_id,),
    ):
        items.append(
            dict(
                domain="anime",
                canonical_id=str(row["anilist_id"]),
                season_number=row["season_number"],
                title=row["title"],
                legacy_id=str(row["id"]),
                evidence="Monitored title or saved media",
            )
        )
    for row in db.execute(
        """SELECT c.imdb_id,c.title FROM movie_catalog c WHERE c.user_id=? AND (EXISTS(SELECT 1 FROM movie_requests r WHERE r.user_id=c.user_id AND r.imdb_id=c.imdb_id) OR EXISTS(SELECT 1 FROM movie_verifications v WHERE v.user_id=c.user_id AND v.imdb_id=c.imdb_id AND length(v.plex_key)>0)) ORDER BY c.imdb_id LIMIT 1001""",
        (user_id,),
    ):
        items.append(
            dict(
                domain="movie",
                canonical_id=row["imdb_id"],
                season_number=0,
                title=row["title"],
                legacy_id=row["imdb_id"],
                evidence="Saved media request or observation",
            )
        )
    for row in db.execute(
        """SELECT c.tmdb_id,c.metadata_json FROM tv_catalog c WHERE c.user_id=? AND (json_extract(c.metadata_json,'$.in_collection')=1 OR EXISTS(SELECT 1 FROM tv_episodes e WHERE e.user_id=c.user_id AND e.tmdb_id=c.tmdb_id AND e.plex_present=1)) ORDER BY c.tmdb_id LIMIT 1001""",
        (user_id,),
    ):
        metadata = json.loads(row["metadata_json"])
        title = (
            (metadata.get("name") or metadata.get("title")) if isinstance(metadata, dict) else None
        )
        title = title or "TV series"
        items.append(
            dict(
                domain="tv",
                canonical_id=str(row["tmdb_id"]),
                season_number=0,
                title=str(title)[:300],
                legacy_id=str(row["tmdb_id"]),
                evidence="Saved Plex episode presence",
            )
        )
    unique = {}
    for item in items:
        unique.setdefault((item["domain"], item["canonical_id"], item["season_number"]), item)
    items = list(unique.values())
    _require(len(items) <= 1000, "This inventory needs a larger paginated review before promotion.")
    digest = hashlib.sha256(
        json.dumps(
            {"owner": user_id, "items": items}, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return {"items": items, "count": len(items), "digest": digest, "history_included": False}


def _identity(domain, canonical_id, season=0):
    value = str(canonical_id)
    _require(domain in {"anime", "movie", "tv"}, "Choose a valid media type.", 422)
    _require(
        bool(re.fullmatch(r"tt[0-9]{7,10}", value))
        if domain == "movie"
        else bool(re.fullmatch(r"[1-9][0-9]{0,9}", value)) and int(value) <= 2147483647,
        "Choose an exact catalog identity.",
        422,
    )
    _require(
        type(season) is int and 0 <= season <= 200 and (domain == "anime" or season == 0),
        "Choose a valid season.",
        422,
    )
    return value


def _bind_item(
    db,
    scope,
    *,
    domain,
    canonical_id,
    title,
    season=0,
    legacy_id=None,
    connector_scope="legacy",
    authorized_event_id=None,
    metadata=None,
):
    canonical_id = _identity(domain, canonical_id, season)
    title = str(title).strip()
    _require(
        0 < len(title) <= 300 and not any(ord(c) < 32 for c in title),
        "Choose a title of up to 300 characters.",
        422,
    )
    resource_id = None
    if legacy_id is not None:
        event = None
        if authorized_event_id is not None:
            event = db.execute(
                "SELECT 1 FROM library_request_events e JOIN library_items i ON i.id=e.library_item_id WHERE e.id=? AND e.library_id=? AND e.actor_user_id=? AND e.state IN ('queued','dispatched') AND e.membership_revision=? AND e.policy_revision=? AND i.domain=? AND i.canonical_id=? AND i.season_number=?",
                (
                    authorized_event_id,
                    scope.library_id,
                    scope.viewer_id,
                    scope.membership_revision,
                    scope.policy_revision,
                    domain,
                    canonical_id,
                    season,
                ),
            ).fetchone()
        _require(
            scope.role == "owner" or event is not None,
            "Only the owner or an authorized acquisition can bind a managed resource.",
            403,
        )
        _require(
            bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(legacy_id)))
            and bool(re.fullmatch(r"[A-Za-z0-9_-]{1,80}", connector_scope)),
            "Invalid resource reference.",
            422,
        )
        # Validate operational ownership; never accept an arbitrary existing record pointer.
        if domain == "anime":
            found = db.execute(
                "SELECT 1 FROM media_monitors WHERE id=? AND user_id=? AND anilist_id=? AND COALESCE(season_number,0)=?",
                (legacy_id, scope.operational_owner_id, int(canonical_id), season),
            ).fetchone()
        else:
            table, key = (
                ("movie_catalog", "imdb_id") if domain == "movie" else ("tv_catalog", "tmdb_id")
            )
            found = db.execute(
                f"SELECT 1 FROM {table} WHERE user_id=? AND {key}=?",
                (scope.operational_owner_id, canonical_id),
            ).fetchone()
            _require(str(legacy_id) == canonical_id, "Resource identity mismatch.", 422)
        _require(found is not None, "Resource not available.", 404)
        if domain == "anime":
            _require(
                not db.execute(
                    "SELECT 1 FROM media_title_removals WHERE monitor_id=? AND status!='complete'",
                    (legacy_id,),
                ).fetchone(),
                "Removal is in progress. This media cannot be shared now.",
                409,
            )

        db.execute(
            "INSERT OR IGNORE INTO library_resources(id,owner_user_id,domain,canonical_id,season_number,connector_scope,legacy_id) VALUES(?,?,?,?,?,?,?)",
            (
                _id(),
                scope.operational_owner_id,
                domain,
                canonical_id,
                season,
                connector_scope,
                str(legacy_id),
            ),
        )
        resource = db.execute(
            "SELECT id,legacy_id FROM library_resources WHERE owner_user_id=? AND domain=? AND canonical_id=? AND season_number=? AND connector_scope=?",
            (scope.operational_owner_id, domain, canonical_id, season, connector_scope),
        ).fetchone()
        _require(
            resource["legacy_id"] == str(legacy_id),
            "A different managed resource is already bound.",
        )
        resource_id = resource["id"]
    db.execute(
        "INSERT OR IGNORE INTO library_items(id,library_id,domain,canonical_id,season_number,resource_id,title,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            _id(),
            scope.library_id,
            domain,
            canonical_id,
            season,
            resource_id,
            title,
            scope.viewer_id,
            _stamp(),
        ),
    )
    item = db.execute(
        "SELECT * FROM library_items WHERE library_id=? AND domain=? AND canonical_id=? AND season_number=?",
        (scope.library_id, domain, canonical_id, season),
    ).fetchone()
    _require(
        not resource_id or item["resource_id"] in {None, resource_id},
        "This catalog entry already uses another resource. A separately supported version is needed.",
    )
    if resource_id and item["resource_id"] is None:
        db.execute("UPDATE library_items SET resource_id=? WHERE id=?", (resource_id, item["id"]))
    if metadata is not None:
        db.execute(
            "UPDATE library_items SET metadata_json=? WHERE id=?",
            (
                json.dumps(
                    public_catalog_metadata(metadata), ensure_ascii=False, separators=(",", ":")
                ),
                item["id"],
            ),
        )
    if item["resource_id"]:
        _require(
            not db.execute(
                "SELECT 1 FROM library_resources r JOIN media_title_removals d ON d.monitor_id=r.legacy_id WHERE r.id=? AND r.domain='anime' AND d.status!='complete'",
                (item["resource_id"],),
            ).fetchone(),
            "Removal is in progress. This media cannot be shared now.",
            409,
        )
    if item["state"] == "removed":
        db.execute(
            "UPDATE library_items SET state='active',title=? WHERE id=?", (title, item["id"])
        )
    return item["id"]


def create_household(
    db,
    *,
    user_id: int,
    name: str,
    promote: bool = False,
    preview_digest: str = "",
    members_can_add: bool = True,
    acquisition_requests: bool = False,
) -> str:
    name = str(name).strip()
    _require(
        0 < len(name) <= 80 and not any(ord(c) < 32 for c in name),
        "Choose a household name of up to 80 characters.",
        422,
    )
    with _transaction(db):
        _require(
            db.execute(
                "SELECT 1 FROM household_memberships WHERE user_id=? AND state='active'", (user_id,)
            ).fetchone()
            is None,
            "You already belong to a household.",
        )
        personal = ensure_personal_library(db, user_id=user_id)
        if promote:
            preview = promotion_preview(db, user_id=user_id)
            _require(
                secrets.compare_digest(preview["digest"], preview_digest),
                "Library inventory changed. Review the preview again.",
            )
            library_id = personal
            db.execute("DELETE FROM personal_libraries WHERE user_id=?", (user_id,))
            db.execute(
                "UPDATE libraries SET kind='household',name=?,members_can_add=?,acquisition_requests=? WHERE id=?",
                (name, int(members_can_add), int(acquisition_requests), library_id),
            )
            ensure_personal_library(db, user_id=user_id)
        else:
            preview = {"items": []}
            library_id = _id()
            db.execute(
                "INSERT INTO libraries(id,kind,owner_user_id,name,members_can_add,acquisition_requests) VALUES(?,'household',?,?,?,?)",
                (library_id, user_id, name, int(members_can_add), int(acquisition_requests)),
            )
        db.execute(
            "INSERT INTO household_memberships(library_id,user_id,role,joined_at) VALUES(?,?,'owner',?)",
            (library_id, user_id, _stamp()),
        )
        scope = resolve_scope(db, viewer_id=user_id, library_id=library_id)
        for item in preview["items"]:
            if item.get("existing_item"):
                continue
            _bind_item(
                db,
                scope,
                domain=item["domain"],
                canonical_id=item["canonical_id"],
                title=item["title"],
                season=item["season_number"],
                legacy_id=item["legacy_id"],
            )
        select_library(db, resolve_scope(db, viewer_id=user_id, library_id=library_id))
        return library_id


def create_invite(
    db, scope: LibraryScope, *, now: int | None = None, ttl: int = 86400
) -> dict[str, Any]:
    now = int(time.time()) if now is None else now
    _require(
        60 <= ttl <= 7 * 86400, "Invitation expiry must be between one minute and seven days.", 422
    )
    with _transaction(db):
        revalidate_scope(db, scope, owner=True)
        _require(scope.kind == "household", "Choose a household.", 422)
        count = db.execute(
            "SELECT COUNT(*) FROM household_invites WHERE library_id=? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>?",
            (scope.library_id, now),
        ).fetchone()[0]
        _require(count < 10, "Revoke an unused invitation before creating another.")
        token = secrets.token_urlsafe(32)
        invite_id = _id()
        db.execute(
            "INSERT INTO household_invites(id,library_id,token_digest,creator_user_id,expires_at,created_at) VALUES(?,?,?,?,?,?)",
            (
                invite_id,
                scope.library_id,
                hashlib.sha256(token.encode()).hexdigest(),
                scope.viewer_id,
                now + ttl,
                now,
            ),
        )
        return {"id": invite_id, "token": token, "expires_at": now + ttl}


def inspect_invite(db, *, token: str, now: int | None = None) -> dict[str, Any]:
    _require(
        isinstance(token, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", token)),
        "Invitation is invalid or no longer available.",
        404,
    )
    now = int(time.time()) if now is None else now
    row = db.execute(
        "SELECT l.id,l.name,i.expires_at FROM household_invites i JOIN libraries l ON l.id=i.library_id WHERE i.token_digest=? AND i.consumed_at IS NULL AND i.revoked_at IS NULL AND i.expires_at>? AND l.state='active'",
        (hashlib.sha256(token.encode()).hexdigest(), now),
    ).fetchone()
    _require(row is not None, "Invitation is invalid or no longer available.", 404)
    return {"library_id": row["id"], "name": row["name"], "expires_at": row["expires_at"]}


def accept_invite(
    db,
    *,
    viewer_id: int,
    token: str,
    now: int | None = None,
    display_name: str = "Household member",
) -> str:
    _require(
        isinstance(token, str) and bool(re.fullmatch(r"[A-Za-z0-9_-]{43}", token)),
        "Invitation is invalid or no longer available.",
        404,
    )
    display_name = str(display_name).strip() or "Household member"
    _require(
        len(display_name) <= 80 and not any(ord(c) < 32 for c in display_name),
        "Choose a household name of up to 80 characters.",
        422,
    )
    now = int(time.time()) if now is None else now
    with _transaction(db):
        _require(
            db.execute("SELECT 1 FROM users WHERE id=?", (viewer_id,)).fetchone() is not None,
            "Account not available.",
            404,
        )
        _require(
            db.execute(
                "SELECT 1 FROM household_memberships WHERE user_id=? AND state='active'",
                (viewer_id,),
            ).fetchone()
            is None,
            "You already belong to a household.",
        )
        row = db.execute(
            "SELECT i.id,i.library_id FROM household_invites i JOIN libraries l ON l.id=i.library_id WHERE token_digest=? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>? AND l.state='active'",
            (hashlib.sha256(token.encode()).hexdigest(), now),
        ).fetchone()
        _require(row is not None, "Invitation is invalid or no longer available.", 404)
        changed = db.execute(
            "UPDATE household_invites SET consumed_at=?,consumed_by=? WHERE id=? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>?",
            (now, viewer_id, row["id"], now),
        ).rowcount
        _require(changed == 1, "Invitation is invalid or no longer available.", 404)
        ensure_personal_library(db, user_id=viewer_id)
        db.execute(
            "INSERT INTO household_memberships(library_id,user_id,role,joined_at,display_name) VALUES(?,?,'member',?,?) ON CONFLICT(library_id,user_id) DO UPDATE SET state='active',joined_at=excluded.joined_at,revoked_at=NULL,display_name=excluded.display_name",
            (row["library_id"], viewer_id, _stamp(), display_name),
        )
        db.execute(
            "UPDATE libraries SET membership_revision=membership_revision+1 WHERE id=?",
            (row["library_id"],),
        )
        select_library(db, resolve_scope(db, viewer_id=viewer_id, library_id=row["library_id"]))
        return row["library_id"]


def revoke_invite(db, scope: LibraryScope, invite_id: str) -> None:
    with _transaction(db):
        revalidate_scope(db, scope, owner=True)
        changed = db.execute(
            "UPDATE household_invites SET revoked_at=? WHERE id=? AND library_id=? AND consumed_at IS NULL AND revoked_at IS NULL",
            (int(time.time()), invite_id, scope.library_id),
        ).rowcount
        _require(changed == 1, "Invitation not available.", 404)


def leave_or_revoke(db, scope: LibraryScope, *, member_id: int | None = None) -> None:
    with _transaction(db):
        revalidate_scope(db, scope, owner=member_id is not None)
        target = scope.viewer_id if member_id is None else member_id
        row = db.execute(
            "SELECT role FROM household_memberships WHERE library_id=? AND user_id=? AND state='active'",
            (scope.library_id, target),
        ).fetchone()
        _require(row is not None, "Membership not available.", 404)
        _require(row["role"] != "owner", "The owner cannot leave an active household.")
        db.execute(
            "UPDATE household_memberships SET state='revoked',revoked_at=? WHERE library_id=? AND user_id=?",
            (_stamp(), scope.library_id, target),
        )
        db.execute(
            "UPDATE libraries SET membership_revision=membership_revision+1 WHERE id=?",
            (scope.library_id,),
        )
        # Dispatched work is retained with its honest outcome; no provider cancellation claim.
        db.execute(
            "UPDATE library_request_events SET state='cancelled',updated_at=? WHERE library_id=? AND actor_user_id=? AND state='queued'",
            (_stamp(), scope.library_id, target),
        )

        personal = db.execute(
            "SELECT library_id FROM personal_libraries WHERE user_id=?", (target,)
        ).fetchone()
        if personal:
            db.execute(
                "UPDATE library_selections SET library_id=? WHERE user_id=? AND library_id=?",
                (personal[0], target, scope.library_id),
            )


def set_policy(
    db, scope: LibraryScope, *, members_can_add: bool, acquisition_requests: bool
) -> None:
    with _transaction(db):
        revalidate_scope(db, scope, owner=True)
        db.execute(
            "UPDATE libraries SET members_can_add=?,acquisition_requests=?,policy_revision=policy_revision+1 WHERE id=?",
            (int(members_can_add), int(acquisition_requests), scope.library_id),
        )
        db.execute(
            "UPDATE library_request_events SET state='cancelled',updated_at=? WHERE library_id=? AND state='queued'",
            (_stamp(), scope.library_id),
        )


class LibraryRepository:
    def __init__(self, db, scope: LibraryScope):
        self.db, self.scope = db, scope

    def items(self) -> list[dict[str, Any]]:
        revalidate_scope(self.db, self.scope)
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id,domain,canonical_id,season_number,title FROM library_items WHERE library_id=? AND state='active' ORDER BY title,id LIMIT 1000",
                (self.scope.library_id,),
            )
        ]

    def add(
        self,
        *,
        domain,
        canonical_id,
        title,
        season=0,
        legacy_id=None,
        connector_scope="legacy",
        metadata=None,
    ) -> str:
        with _transaction(self.db):
            revalidate_scope(self.db, self.scope)
            _require(
                self.scope.role == "owner" or self.scope.members_can_add,
                "Only the library owner can add titles.",
                403,
            )
            return _bind_item(
                self.db,
                self.scope,
                domain=domain,
                canonical_id=canonical_id,
                title=title,
                season=season,
                legacy_id=legacy_id,
                connector_scope=connector_scope,
                metadata=metadata,
            )

    def item(self, item_id, *, include_removed=False):
        revalidate_scope(self.db, self.scope)
        row = self.db.execute(
            "SELECT * FROM library_items WHERE library_id=? AND id=?",
            (self.scope.library_id, item_id),
        ).fetchone()
        _require(
            row is not None and (include_removed or row["state"] == "active"),
            "Library title not available.",
            404,
        )
        return row

    def set_interest(self, item_id, *, keep: bool, active_use: bool = False):
        with _transaction(self.db):
            item = self.item(item_id)
            if keep or active_use:
                pending = self.db.execute(
                    "SELECT 1 FROM library_resources r JOIN media_title_removals d ON d.monitor_id=r.legacy_id WHERE r.id=? AND r.domain='anime' AND d.status!='complete'",
                    (item["resource_id"],),
                ).fetchone()
                _require(
                    not pending, "Removal is in progress. New media claims are unavailable.", 409
                )
            self.db.execute(
                "INSERT INTO library_item_interest(library_item_id,user_id,keep_available,active_use,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(library_item_id,user_id) DO UPDATE SET keep_available=excluded.keep_available,active_use=excluded.active_use,updated_at=excluded.updated_at",
                (item_id, self.scope.viewer_id, int(keep), int(active_use), _stamp()),
            )

    def removal_blocked(self, item_id, *, include_removed=False) -> bool:
        revalidate_scope(self.db, self.scope, owner=True)
        item = self.item(item_id, include_removed=include_removed)
        # Coarse result only: another active member's history is never disclosed.
        interest = self.db.execute(
            """SELECT 1 FROM library_item_interest i JOIN household_memberships m ON m.user_id=i.user_id AND m.library_id=? AND m.state='active' WHERE i.library_item_id=? AND i.user_id!=? AND (i.keep_available=1 OR i.active_use=1) LIMIT 1""",
            (self.scope.library_id, item_id, self.scope.viewer_id),
        ).fetchone()
        if item["domain"] == "anime":
            in_progress = self.db.execute(
                "SELECT 1 FROM watch_entries w JOIN household_memberships m ON m.user_id=w.user_id AND m.library_id=? AND m.state='active' WHERE w.user_id!=? AND w.anilist_id=? AND w.season_number=? AND w.status='watching' LIMIT 1",
                (
                    self.scope.library_id,
                    self.scope.viewer_id,
                    int(item["canonical_id"]),
                    item["season_number"],
                ),
            ).fetchone()
        else:
            identity = "imdb_id" if item["domain"] == "movie" else "tmdb_id"
            in_progress = self.db.execute(
                f"SELECT 1 FROM {item['domain']}_history h JOIN household_memberships m ON m.user_id=h.user_id AND m.library_id=? AND m.state='active' WHERE h.user_id!=? AND h.{identity}=? AND h.status='watching' LIMIT 1",
                (self.scope.library_id, self.scope.viewer_id, item["canonical_id"]),
            ).fetchone()
        references = (
            item["resource_id"]
            and self.db.execute(
                "SELECT 1 FROM library_items WHERE resource_id=? AND state='active' AND id!=? LIMIT 1",
                (item["resource_id"], item_id),
            ).fetchone()
        )
        return bool(interest or in_progress or references)

    def remove_membership(self, item_id):
        with _transaction(self.db):
            revalidate_scope(self.db, self.scope, owner=True)
            self.item(item_id)
            self.db.execute("UPDATE library_items SET state='removed' WHERE id=?", (item_id,))
            self.db.execute(
                "UPDATE library_request_events SET state='cancelled',updated_at=? WHERE library_item_id=? AND state='queued'",
                (_stamp(), item_id),
            )

    def withdraw_interest(self, item_id):
        with _transaction(self.db):
            self.item(item_id, include_removed=True)
            self.db.execute(
                "UPDATE library_item_interest SET keep_available=0,active_use=0,updated_at=? WHERE library_item_id=? AND user_id=?",
                (_stamp(), item_id, self.scope.viewer_id),
            )

    def queue_request(self, item_id, *, idempotency_key: str) -> str:
        with _transaction(self.db):
            item = self.item(item_id)
            _require(item["domain"] != "tv", "Automatic TV acquisition is not supported.", 422)
            _require(
                self.scope.acquisition_requests,
                "Acquisition requests are off for this library.",
                403,
            )
            _require(
                self.scope.role == "owner" or self.scope.members_can_add,
                "Only the owner can add titles.",
                403,
            )
            _require(
                bool(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", idempotency_key)),
                "Invalid request key.",
                422,
            )
            self.db.execute(
                "INSERT OR IGNORE INTO library_request_events(id,library_id,library_item_id,actor_user_id,idempotency_key,state,membership_revision,policy_revision,created_at,updated_at) VALUES(?,?,?,?,?,'queued',?,?,?,?)",
                (
                    _id(),
                    self.scope.library_id,
                    item_id,
                    self.scope.viewer_id,
                    idempotency_key,
                    self.scope.membership_revision,
                    self.scope.policy_revision,
                    _stamp(),
                    _stamp(),
                ),
            )
            event = self.db.execute(
                "SELECT id,library_item_id FROM library_request_events WHERE library_id=? AND actor_user_id=? AND idempotency_key=?",
                (self.scope.library_id, self.scope.viewer_id, idempotency_key),
            ).fetchone()
            _require(
                event["library_item_id"] == item_id, "Request key already identifies another title."
            )
            return event["id"]


def authorize_dispatch(db, *, event_id: str) -> dict[str, Any]:
    """Explicit local dispatch point only; caller must retain backend-wide deduplication.

    No provider operation is performed. Membership after this point cannot undo a
    remote write; adapters must record uncertain outcomes instead of retrying blindly.
    """
    with _transaction(db):
        row = db.execute("SELECT * FROM library_request_events WHERE id=?", (event_id,)).fetchone()
        _require(row is not None and row["state"] == "queued", "Request no longer queued.")
        scope = resolve_scope(db, viewer_id=row["actor_user_id"], library_id=row["library_id"])
        _require(
            scope.membership_revision == row["membership_revision"]
            and scope.policy_revision == row["policy_revision"]
            and scope.acquisition_requests,
            "Request permissions changed.",
        )
        item = LibraryRepository(db, scope).item(row["library_item_id"])
        _require(item["domain"] != "tv", "Automatic TV acquisition is not supported.", 422)
        _require(
            scope.role == "owner" or scope.members_can_add, "Only the owner can add titles.", 403
        )
        db.execute(
            "UPDATE library_request_events SET state='dispatched',updated_at=? WHERE id=?",
            (_stamp(), event_id),
        )
        return {
            "event_id": event_id,
            "library_id": scope.library_id,
            "actor_id": scope.viewer_id,
            "operational_owner_id": scope.operational_owner_id,
            "library_item_id": row["library_item_id"],
        }


def connector_status(db, scope: LibraryScope) -> list[dict[str, str]]:
    revalidate_scope(db, scope)
    results = []
    for row in db.execute(
        "SELECT s.integration,h.health_json FROM library_settings s LEFT JOIN user_connection_health h ON h.user_id=s.owner_user_id AND h.integration=s.integration WHERE s.library_id=?",
        (scope.library_id,),
    ):
        try:
            saved = json.loads(row["health_json"] or "{}")
            state = saved.get("state") if isinstance(saved, dict) else None
        except (ValueError, TypeError):
            state = None
        if state not in {
            "connected",
            "not_configured",
            "incomplete",
            "degraded",
            "error",
            "rate_limited",
            "not_checked",
        }:
            state = "not_checked"
        results.append({"integration": row["integration"], "state": state})
    return results


def bind_connector(db, scope: LibraryScope, *, integration: str) -> None:
    _require(
        integration in {"plex", "realdebrid", "prowlarr", "riven", "seerr"},
        "Choose a valid connection.",
        422,
    )
    with _transaction(db):
        revalidate_scope(db, scope, owner=True)
        db.execute(
            "INSERT INTO library_settings(library_id,integration,owner_user_id) VALUES(?,?,?) ON CONFLICT(library_id,integration) DO UPDATE SET generation=generation+1",
            (scope.library_id, integration, scope.operational_owner_id),
        )
        db.execute(
            "UPDATE libraries SET policy_revision=policy_revision+1 WHERE id=?", (scope.library_id,)
        )
        db.execute(
            "UPDATE library_request_events SET state='cancelled',updated_at=? WHERE library_id=? AND state='queued'",
            (_stamp(), scope.library_id),
        )


def start_anime_tracking(db, scope: LibraryScope, *, item_id: str, include_removed=False) -> int:
    with _transaction(db):
        item = LibraryRepository(db, scope).item(item_id, include_removed=include_removed)
        _require(item["domain"] == "anime", "Choose an anime title.", 422)
        db.execute(
            "INSERT OR IGNORE INTO watch_entries(user_id,anilist_id,season_number) VALUES(?,?,?)",
            (scope.viewer_id, int(item["canonical_id"]), item["season_number"]),
        )
        entry = db.execute(
            "SELECT id FROM watch_entries WHERE user_id=? AND anilist_id=? AND season_number=?",
            (scope.viewer_id, int(item["canonical_id"]), item["season_number"]),
        ).fetchone()
        db.execute(
            "INSERT OR IGNORE INTO watch_library_links(user_id,library_item_id,watch_entry_id) VALUES(?,?,?)",
            (scope.viewer_id, item_id, entry["id"]),
        )
        return entry["id"]


def personal_anime_history(db, *, viewer_id: int) -> list[dict[str, Any]]:
    # Historical rows remain accessible to their viewer even after membership ends.
    return [
        dict(row)
        for row in db.execute(
            "SELECT w.id,w.anilist_id,w.season_number,w.status,w.progress_episodes,(SELECT e.score_ten FROM watch_events e WHERE e.watch_entry_id=w.id AND e.user_id=w.user_id AND e.disposition='accepted' AND e.score_ten IS NOT NULL ORDER BY e.id DESC LIMIT 1) AS rating FROM watch_entries w WHERE w.user_id=? ORDER BY w.id",
            (viewer_id,),
        )
    ]


def _public_artwork(value: object) -> str | None:
    return (
        value
        if isinstance(value, str)
        and re.fullmatch(
            r"https://image\.tmdb\.org/t/p/(?:w[0-9]{2,4}|original)/[A-Za-z0-9_-]{1,160}\.(?:jpg|png)",
            value,
        )
        else None
    )


class PersonalTrackingRepository:
    """Viewer-only history overlay. Catalog copies contain public metadata only."""

    def __init__(self, db, *, viewer_id: int):
        self.db, self.viewer_id = db, viewer_id

    def start(self, scope: LibraryScope, *, item_id: str) -> dict[str, Any]:
        _require(scope.viewer_id == self.viewer_id, "Tracking account mismatch.", 403)
        with _transaction(self.db):
            item = LibraryRepository(self.db, scope).item(item_id)
            if item["domain"] == "anime":
                entry = start_anime_tracking(self.db, scope, item_id=item_id)
                return {"domain": "anime", "watch_entry_id": entry}
            if item["domain"] == "movie":
                source = self.db.execute(
                    "SELECT imdb_id,tmdb_id,title,year,poster_url,backdrop_url,overview,identity_verified,metadata_checked_at FROM movie_catalog WHERE user_id=? AND imdb_id=?",
                    (scope.operational_owner_id, item["canonical_id"]),
                ).fetchone()
                if source is None and item["resource_id"] is None:
                    public = public_catalog_metadata(json.loads(item["metadata_json"]))
                    if public.get("tmdb_id"):
                        source = {
                            **public,
                            "imdb_id": item["canonical_id"],
                            "tmdb_id": public["tmdb_id"],
                            "title": item["title"],
                            "year": public.get("year"),
                            "overview": public.get("overview"),
                            "poster_url": public.get("poster_url"),
                            "backdrop_url": public.get("backdrop_url"),
                            "identity_verified": 1,
                            "metadata_checked_at": None,
                        }
                _require(
                    source is not None and source["tmdb_id"] is not None,
                    "Public movie metadata must be checked before tracking this title.",
                    422,
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO movie_catalog(user_id,imdb_id,tmdb_id,title,year,poster_url,backdrop_url,overview,identity_verified,metadata_checked_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self.viewer_id,
                        source["imdb_id"],
                        source["tmdb_id"],
                        source["title"],
                        source["year"],
                        _public_artwork(source["poster_url"]),
                        _public_artwork(source["backdrop_url"]),
                        source["overview"],
                        source["identity_verified"],
                        source["metadata_checked_at"],
                        _stamp(),
                    ),
                )
                # Preserve the viewer's existing catalog identity and all manual history.
                own = self.db.execute(
                    "SELECT tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?",
                    (self.viewer_id, item["canonical_id"]),
                ).fetchone()
                _require(
                    own is not None and own["tmdb_id"] == source["tmdb_id"],
                    "Your saved movie identity needs review.",
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO movie_history(user_id,imdb_id,tmdb_id,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (self.viewer_id, item["canonical_id"], source["tmdb_id"], _stamp(), _stamp()),
                )
            else:
                row = self.db.execute(
                    "SELECT metadata_json FROM tv_catalog WHERE user_id=? AND tmdb_id=?",
                    (scope.operational_owner_id, int(item["canonical_id"])),
                ).fetchone()
                saved = json.loads(row[0]) if row else json.loads(item["metadata_json"])
                allowed = {
                    "id",
                    "tmdb_id",
                    "name",
                    "title",
                    "overview",
                    "first_air_date",
                    "year",
                    "number_of_seasons",
                    "number_of_episodes",
                    "catalog_subset",
                }
                metadata = (
                    {
                        key: value
                        for key, value in saved.items()
                        if key in allowed and isinstance(value, (str, int, float, bool, type(None)))
                    }
                    if isinstance(saved, dict)
                    else {}
                )
                metadata.update(
                    id=int(item["canonical_id"]),
                    tmdb_id=int(item["canonical_id"]),
                    in_collection=True,
                )
                metadata.setdefault("name", item["title"])
                metadata.setdefault("title", item["title"])
                if isinstance(saved, dict):
                    metadata["poster_url"] = _public_artwork(saved.get("poster_url"))
                    metadata["backdrop_url"] = _public_artwork(saved.get("backdrop_url"))
                    seasons = saved.get("seasons")
                    if isinstance(seasons, list):
                        metadata["seasons"] = [
                            {
                                key: value
                                for key, value in season.items()
                                if key in {"season_number", "name", "air_date", "episode_count"}
                                and isinstance(value, (str, int, type(None)))
                            }
                            for season in seasons[:201]
                            if isinstance(season, dict)
                        ]
                self.db.execute(
                    "INSERT OR IGNORE INTO tv_catalog(user_id,tmdb_id,metadata_json,updated_at) VALUES(?,?,?,?)",
                    (
                        self.viewer_id,
                        int(item["canonical_id"]),
                        json.dumps(metadata, separators=(",", ":")),
                        _stamp(),
                    ),
                )
                self.db.execute(
                    "INSERT OR IGNORE INTO tv_history(user_id,tmdb_id,updated_at) VALUES(?,?,?)",
                    (self.viewer_id, int(item["canonical_id"]), _stamp()),
                )
            return {"domain": item["domain"], "canonical_id": item["canonical_id"]}

    def history(self, domain: str) -> list[dict[str, Any]]:
        if domain == "anime":
            return personal_anime_history(self.db, viewer_id=self.viewer_id)
        _require(domain in {"movie", "tv"}, "Choose a valid media type.", 422)
        identity = "imdb_id" if domain == "movie" else "tmdb_id"
        return [
            dict(row)
            for row in self.db.execute(
                f"SELECT {identity},status,rating FROM {domain}_history WHERE user_id=? ORDER BY {identity}",
                (self.viewer_id,),
            )
        ]


def connection_generation_changed(db, *, owner_user_id: int, integration: str) -> list[str]:
    """Hook for the owning connection writer, not a member-facing endpoint.

    Invalidates core queued authority only. Existing verification/curator adapters
    must invalidate their connector-bound evidence at the same integration point.
    """
    _require(
        integration in {"plex", "realdebrid", "prowlarr", "riven", "seerr"},
        "Choose a valid connection.",
        422,
    )
    with _transaction(db):
        affected = [
            row[0]
            for row in db.execute(
                "SELECT library_id FROM library_settings WHERE owner_user_id=? AND integration=?",
                (owner_user_id, integration),
            )
        ]
        for library_id in affected:
            db.execute(
                "UPDATE library_settings SET generation=generation+1 WHERE library_id=? AND integration=?",
                (library_id, integration),
            )
            db.execute(
                "UPDATE libraries SET policy_revision=policy_revision+1 WHERE id=?", (library_id,)
            )
            db.execute(
                "UPDATE library_request_events SET state='cancelled',updated_at=? WHERE library_id=? AND state='queued'",
                (_stamp(), library_id),
            )
        return affected


def owner_connection_scopes(db, *, owner_user_id: int, integration: str) -> list[dict[str, str]]:
    """Owner-only settings copy helper; never expose it in a member projection."""
    _require(
        integration in {"plex", "realdebrid", "prowlarr", "riven", "seerr"},
        "Choose a valid connection.",
        422,
    )
    return [
        dict(row)
        for row in db.execute(
            "SELECT l.id,l.name,l.kind FROM libraries l WHERE l.owner_user_id=? AND l.state='active' AND (l.kind='personal' OR EXISTS(SELECT 1 FROM library_settings s WHERE s.library_id=l.id AND s.integration=?)) ORDER BY l.kind,l.name",
            (owner_user_id, integration),
        )
    ]


def navigation_context(db, *, viewer_id: int) -> dict[str, Any]:
    """Bounded personal/one-household switch DTO; no history, secrets or GET writes."""
    selected = selected_scope(db, viewer_id=viewer_id)
    rows = db.execute(
        "SELECT l.id,l.name,l.kind FROM libraries l WHERE l.state='active' AND ((l.kind='personal' AND l.owner_user_id=?) OR EXISTS(SELECT 1 FROM household_memberships m WHERE m.library_id=l.id AND m.user_id=? AND m.state='active')) ORDER BY CASE l.kind WHEN 'personal' THEN 0 ELSE 1 END LIMIT 2",
        (viewer_id, viewer_id),
    ).fetchall()
    return {
        "available": True,
        "selected_id": selected.library_id,
        "selected_kind": selected.kind,
        "can_add": selected.role == "owner" or selected.members_can_add,
        "can_request": selected.acquisition_requests
        and (selected.role == "owner" or selected.members_can_add),
        "libraries": [dict(row) for row in rows],
    }


def public_catalog_metadata(value):
    """Finite public fields only; never arbitrary cached service/provider payloads."""
    _require(isinstance(value, dict), "Public catalog metadata is required.", 422)
    result = {}
    for key, maximum in (("title", 300), ("overview", 10000)):
        text = value.get(key)
        if isinstance(text, str):
            result[key] = " ".join(text.split())[:maximum]
    year = value.get("year")
    if type(year) is int and 1870 <= year <= 2200:
        result["year"] = year
    tmdb = value.get("tmdb_id")
    if type(tmdb) is int and 0 < tmdb <= 2147483647:
        result["tmdb_id"] = tmdb
    for key in ("poster_url", "backdrop_url"):
        result[key] = _public_artwork(value.get(key))
    return result
