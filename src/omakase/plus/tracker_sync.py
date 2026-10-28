"""Personal bridge persistence/coordinator. All provider work is explicit and bounded.

No job runs merely by importing this module or rendering a status. Apply remains
gated until legacy AniList writers use the SAME writer_lease. No remote atomic CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from functools import wraps

from omakase.plus.mal import AniListBridgeClient, MALClient, TokenBundle, TrackerHTTPError
from omakase.plus.secrets import decrypt, encrypt, read_secret
from omakase.plus.tracker_reconciliation import (
    Baseline,
    Entry,
    ExactMapping,
    FuzzyDate,
    Identity,
    Inventory,
    Pair,
    PlannedPatch,
    Provider,
    ScoreDecision,
    State,
    Status,
    plan_reconciliation,
    preconditions_match,
)


class BridgeError(ValueError):
    pass


def _check(ok, code):
    if not ok:
        raise BridgeError(code)


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _seal(value):
    _check(
        bool(os.getenv("OMAKASE_PLUS_MASTER_KEY")), "persistent_encryption_configuration_required"
    )
    return encrypt(_json(value))


def _open(value):
    try:
        return json.loads(decrypt(value))
    except (ValueError, TypeError, KeyError) as exc:
        raise BridgeError("invalid_saved_bridge_state") from exc


def _identity(data):
    return Identity(**{**data, "provider": Provider(data["provider"])})


def _pair(data):
    return Pair(_identity(data["anilist"]), _identity(data["mal"]))


def _state(data):
    values = {**data, "status": Status(data["status"])}
    for name in ("started_at", "completed_at"):
        if values.get(name) is not None:
            values[name] = FuzzyDate(**values[name])
    return State(**values)


def _entry(data):
    return (
        None
        if data is None
        else Entry(data["media_id"], _state(data["state"]), data.get("revision"))
    )


def _patch(data):
    changes = []
    for name, value in data["changes"]:
        if name == "status":
            value = Status(value)
        elif name in ("started_at", "completed_at") and value is not None:
            value = FuzzyDate(**value)
        changes.append((name, value))
    return PlannedPatch(
        _pair(data["pair"]),
        ExactMapping(**data["mapping"]),
        Provider(data["target"]),
        tuple(changes),
        _entry(data["before_source"]),
        _entry(data["before_target"]),
    )


def _baseline(data):
    return Baseline(
        _pair(data["pair"]),
        ExactMapping(**data["mapping"]),
        _entry(data["anilist"]),
        _entry(data["mal"]),
    )


@contextmanager
def writer_lease(db, user_id, *, now=None, ttl=300):
    """All old/new tracker writers must hold this personal lease across remote work.

    A lease is local serialization, not an atomic transaction with providers.
    Never hold a SQLite transaction across HTTP. Existing callers must integrate
    this before setting the bridge activation gate.
    """
    _check(type(user_id) is int and user_id > 0, "invalid_owner")
    _check(not db.in_transaction, "clean_transaction_required")
    stamp = int(time.time()) if now is None else int(now)
    token = secrets.token_urlsafe(24)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "DELETE FROM tracker_bridge_leases WHERE user_id=? AND expires_at<=?", (user_id, stamp)
        )
        try:
            db.execute(
                "INSERT INTO tracker_bridge_leases VALUES(?,?,?)", (user_id, token, stamp + ttl)
            )
        except sqlite3.IntegrityError as exc:
            raise BridgeError("writer_busy") from exc
        db.commit()
    except BaseException:
        db.rollback()
        raise
    try:
        yield token
    finally:
        with db:
            db.execute(
                "DELETE FROM tracker_bridge_leases WHERE user_id=? AND token=?", (user_id, token)
            )


def anilist_credential_fingerprint(db, user_id):
    """Local-only credential generation guard; never returns the bearer token."""
    value = read_secret(db, user_id, "anilist_oauth_token")
    return _hash(value) if value is not None else None


def require_anilist_credential(db, user_id, expected):
    _check(anilist_credential_fingerprint(db, user_id) == expected, "tracker_connection_changed")
    pause = (
        db.execute(
            "SELECT MAX(retry_at) FROM tracker_bridge_connections WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        or 0
    )
    _check(pause <= int(time.time()), "tracker_rate_deferred")


def require_writer_lease(db, user_id, token, *, now=None):
    """Recheck the exact lease immediately before dispatch and acknowledgement."""
    stamp = int(time.time()) if now is None else int(now)
    _check(
        db.execute(
            "SELECT 1 FROM tracker_bridge_leases WHERE user_id=? AND token=? AND expires_at>?",
            (user_id, token, stamp),
        ).fetchone()
        is not None,
        "writer_lease_expired",
    )


def _provider_guard(method):
    @wraps(method)
    def guarded(self, user, *args, **kwargs):
        try:
            return method(self, user, *args, **kwargs)
        except TrackerHTTPError as error:
            self.record_provider_error(user, error)
            raise

    return guarded


def configured_service(db, http=None, *, now=time.time):
    """Explicit app/worker factory; constructing it never calls a provider."""
    return BridgeService(
        db,
        MALClient(
            http,
            os.getenv("MAL_CLIENT_ID", "").strip(),
            os.getenv("MAL_CLIENT_SECRET", "").strip(),
            now=now,
        )
        if http
        else None,
        AniListBridgeClient(http, now=now) if http else None,
        now=now,
        legacy_writers_ready=os.getenv("OMAKASE_TRACKER_BRIDGE_WRITERS_READY", "").casefold()
        == "true",
        anilist_token_source=lambda uid: read_secret(db, uid, "anilist_oauth_token"),
    )


class BridgeService:
    def __init__(
        self,
        db,
        mal,
        anilist,
        *,
        now=time.time,
        legacy_writers_ready=False,
        anilist_token_source=None,
    ):
        self.db, self.mal, self.anilist, self.now = db, mal, anilist, now
        self.legacy_writers_ready = legacy_writers_ready
        self.anilist_token_source = anilist_token_source

    def record_provider_error(self, user, error):
        if error.code == "reconnect_required":
            provider = getattr(error, "provider", None)
            with self.db:
                self.db.execute(
                    "UPDATE tracker_bridge_connections SET state='reconnect',generation=generation+1,encrypted_bundle=NULL WHERE user_id=? AND (? IS NULL OR provider=?)",
                    (
                        user,
                        provider.value if provider else None,
                        provider.value if provider else None,
                    ),
                )
                self._invalidate(user)
        if error.retry_after:
            with self.db:
                self.db.execute(
                    "UPDATE tracker_bridge_connections SET retry_at=MAX(retry_at,?) WHERE user_id=? AND state=?",
                    (int(self.now()) + error.retry_after, user, "verified"),
                )

    def _owner(self, user):
        _check(
            type(user) is int
            and user > 0
            and self.db.execute("SELECT 1 FROM users WHERE id=?", (user,)).fetchone() is not None,
            "invalid_owner",
        )

    def status(self, user):
        self._owner(user)
        rows = {
            r["provider"]: r
            for r in self.db.execute(
                "SELECT provider,display_name,state,verified_at,expires_at FROM tracker_bridge_connections WHERE user_id=?",
                (user,),
            )
        }
        result = {}
        for provider in Provider:
            row = rows.get(provider.value)
            result[provider.value] = {
                "state": row["state"] if row else "disconnected",
                "name": row["display_name"] if row and row["state"] != "disconnected" else None,
                "verified_at": row["verified_at"] if row else None,
            }
        preference = self.db.execute(
            "SELECT enabled FROM tracker_bridge_preferences WHERE user_id=?", (user,)
        ).fetchone()
        retry_at = (
            self.db.execute(
                "SELECT MAX(retry_at) FROM tracker_bridge_connections WHERE user_id=?", (user,)
            ).fetchone()[0]
            or 0
        )
        latest = self.db.execute(
            "SELECT MAX(created_at) FROM tracker_bridge_plans WHERE user_id=? AND state!='cancelled'",
            (user,),
        ).fetchone()[0]
        worker_active = (
            all(
                os.getenv(key, "").strip().casefold() == "true"
                for key in (
                    "OMAKASE_PLUS_PRIVATE",
                    "OMAKASE_TRACKER_BRIDGE_WORKER_ENABLED",
                    "OMAKASE_TRACKER_BRIDGE_WRITERS_READY",
                )
            )
            and os.getenv("OMAKASE_PLUS_REVIEW_MODE", "").strip().casefold() != "true"
        )
        return {
            "last_complete_check": latest,
            "retry_after": max(0, retry_at - int(self.now())),
            "opted_in": bool(preference and preference[0]),
            "enabled": bool(
                preference
                and preference[0]
                and self.legacy_writers_ready
                and all(v["state"] == "verified" for v in result.values())
                and self.db.execute(
                    "SELECT 1 FROM _migrations WHERE name='035-myanimelist-bridge.sql'"
                ).fetchone()
            ),
            "connections": result,
            "worker_active": worker_active,
            "mode": "automatic" if worker_active and preference and preference[0] else "manual",
            "apply_enabled": bool(self.legacy_writers_ready),
            "pending": self.db.execute(
                "SELECT count(*) FROM tracker_bridge_outbox WHERE user_id=? AND state NOT IN ('acknowledged','cancelled')",
                (user,),
            ).fetchone()[0],
        }

    def _invalidate(self, user):
        self.db.execute(
            "UPDATE tracker_bridge_preferences SET enabled=0,updated_at=? WHERE user_id=?",
            (int(self.now()), user),
        )
        self.db.execute(
            "UPDATE tracker_bridge_plans SET state='cancelled' WHERE user_id=?", (user,)
        )
        self.db.execute(
            "UPDATE tracker_bridge_outbox SET state=CASE WHEN state IN ('leased','ambiguous') THEN 'ambiguous' ELSE 'cancelled' END,last_code='connection_changed' WHERE user_id=? AND state!='acknowledged'",
            (user,),
        )
        self.db.execute("DELETE FROM tracker_bridge_baselines WHERE user_id=?", (user,))
        self.db.execute("DELETE FROM tracker_bridge_scans WHERE user_id=?", (user,))

    def save_connection(self, user, provider, remote_id, name, bundle):
        self._owner(user)
        _check(
            isinstance(provider, Provider)
            and type(remote_id) is int
            and remote_id > 0
            and isinstance(name, str)
            and 1 <= len(name) <= 100
            and isinstance(bundle, TokenBundle),
            "invalid_identity",
        )
        encrypted = _seal(json.loads(bundle.to_json()))
        try:
            with self.db:
                old = self.db.execute(
                    "SELECT generation FROM tracker_bridge_connections WHERE user_id=? AND provider=?",
                    (user, provider.value),
                ).fetchone()
                generation = old[0] + 1 if old else 1
                self.db.execute(
                    """INSERT INTO tracker_bridge_connections(user_id,provider,remote_user_id,display_name,generation,state,encrypted_bundle,expires_at,verified_at) VALUES(?,?,?,?,?,'verified',?,?,?) ON CONFLICT(user_id,provider) DO UPDATE SET remote_user_id=excluded.remote_user_id,display_name=excluded.display_name,generation=excluded.generation,state='verified',encrypted_bundle=excluded.encrypted_bundle,expires_at=excluded.expires_at,verified_at=excluded.verified_at,refresh_lease=NULL,refresh_until=0""",
                    (
                        user,
                        provider.value,
                        remote_id,
                        name,
                        generation,
                        encrypted,
                        bundle.expires_at,
                        int(self.now()),
                    ),
                )
                self._invalidate(user)
        except sqlite3.IntegrityError as exc:
            raise BridgeError("identity_linked_elsewhere") from exc

    def disconnect(self, user, provider):
        self._owner(user)
        _check(isinstance(provider, Provider), "invalid_provider")
        with self.db:
            self.db.execute(
                "UPDATE tracker_bridge_connections SET state='disconnected',generation=generation+1,encrypted_bundle=NULL,refresh_lease=NULL,refresh_until=0 WHERE user_id=? AND provider=?",
                (user, provider.value),
            )
            self._invalidate(user)
        # Local unlink only; no invented remote token revoke endpoint.

    def begin_oauth(self, user, session, config, verifier):
        self._owner(user)
        _check(
            isinstance(session, str) and bool(session) and 43 <= len(verifier) <= 128,
            "invalid_oauth_context",
        )
        state = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM tracker_mal_oauth_states WHERE user_id=?", (user,))
            self.db.execute(
                "INSERT INTO tracker_mal_oauth_states VALUES(?,?,?,?,?,?)",
                (
                    _hash(state),
                    user,
                    _hash(session),
                    _hash(config),
                    _seal(verifier),
                    int(self.now()) + 600,
                ),
            )
        return state

    def consume_oauth(self, user, session, config, state):
        self._owner(user)
        with self.db:
            row = self.db.execute(
                "SELECT encrypted_verifier,expires_at FROM tracker_mal_oauth_states WHERE state_hash=? AND user_id=? AND session_hash=? AND config_hash=?",
                (_hash(state), user, _hash(session), _hash(config)),
            ).fetchone()
            _check(row is not None, "invalid_oauth_state")
            self.db.execute(
                "DELETE FROM tracker_mal_oauth_states WHERE state_hash=?", (_hash(state),)
            )
        _check(row["expires_at"] > int(self.now()), "expired_oauth_state")
        return _open(row["encrypted_verifier"])

    def _connection(self, user, provider):
        row = self.db.execute(
            "SELECT * FROM tracker_bridge_connections WHERE user_id=? AND provider=? AND state='verified'",
            (user, provider.value),
        ).fetchone()
        _check(row is not None and row["encrypted_bundle"], "connection_required")
        return row

    def _token(self, user, provider):
        row = self._connection(user, provider)
        bundle = TokenBundle.from_json(_json(_open(row["encrypted_bundle"])))
        if provider == Provider.ANILIST and self.anilist_token_source is not None:
            current = self.anilist_token_source(user)
            if not isinstance(current, str) or not secrets.compare_digest(
                current, bundle.access_token
            ):
                with self.db:
                    self.db.execute(
                        "UPDATE tracker_bridge_connections SET state='reconnect',generation=generation+1,encrypted_bundle=NULL WHERE user_id=? AND provider='anilist'",
                        (user,),
                    )
                    self._invalidate(user)
                raise BridgeError("connection_changed")
        if provider == Provider.MAL and bundle.expires_at <= int(self.now()) + 60:
            token = secrets.token_urlsafe(24)
            stamp = int(self.now())
            with self.db:
                changed = self.db.execute(
                    "UPDATE tracker_bridge_connections SET refresh_lease=?,refresh_until=? WHERE user_id=? AND provider=? AND generation=? AND state='verified' AND refresh_until<=?",
                    (token, stamp + 60, user, provider.value, row["generation"], stamp),
                ).rowcount
                _check(changed == 1, "refresh_busy")
            try:
                fresh = self.mal.refresh(bundle)
                with self.db:
                    changed = self.db.execute(
                        "UPDATE tracker_bridge_connections SET encrypted_bundle=?,expires_at=?,refresh_lease=NULL,refresh_until=0 WHERE user_id=? AND provider=? AND generation=? AND state='verified' AND refresh_lease=?",
                        (
                            _seal(json.loads(fresh.to_json())),
                            fresh.expires_at,
                            user,
                            provider.value,
                            row["generation"],
                            token,
                        ),
                    ).rowcount
                    _check(changed == 1, "connection_changed")
                bundle = fresh
            except TrackerHTTPError as exc:
                with self.db:
                    self.db.execute(
                        "UPDATE tracker_bridge_connections SET refresh_lease=NULL,refresh_until=0,state=CASE WHEN ?='reconnect_required' THEN 'reconnect' ELSE state END WHERE user_id=? AND provider=? AND refresh_lease=?",
                        (exc.code, user, provider.value, token),
                    )
                raise
        return bundle.access_token

    def _current_pair(self, user):
        self._owner(user)
        rows = {p: self._connection(user, p) for p in Provider}
        pair = Pair(
            *(
                Identity(user, p, rows[p]["remote_user_id"], rows[p]["generation"], verified=True)
                for p in (Provider.ANILIST, Provider.MAL)
            )
        )
        return pair

    def _context(self, user, *, verify=False):
        pair = self._current_pair(user)
        rows = {p: self._connection(user, p) for p in Provider}
        retry = max(row["retry_at"] for row in rows.values()) - int(self.now())
        if retry > 0:
            raise TrackerHTTPError("rate_limited", retry_after=retry)
        tokens = {p: self._token(user, p) for p in Provider}
        if verify:
            for provider, client in ((Provider.ANILIST, self.anilist), (Provider.MAL, self.mal)):
                try:
                    remote, _ = client.viewer(tokens[provider])
                except TrackerHTTPError as error:
                    error.provider = provider
                    raise
                if remote != rows[provider]["remote_user_id"]:
                    with self.db:
                        self.db.execute(
                            "UPDATE tracker_bridge_connections SET state='reconnect',generation=generation+1,encrypted_bundle=NULL WHERE user_id=? AND provider=?",
                            (user, provider.value),
                        )
                        self._invalidate(user)
                    raise BridgeError("provider_identity_changed")
        return pair, tokens

    def _baselines(self, user):
        return tuple(
            _baseline(_open(r[0]))
            for r in self.db.execute(
                "SELECT encrypted_payload FROM tracker_bridge_baselines WHERE user_id=?", (user,)
            )
        )

    def _save_baseline(self, user, pair, mapping, a, m, *, changed_fields=None):
        if changed_fields is not None:
            row = self.db.execute(
                "SELECT encrypted_payload FROM tracker_bridge_baselines WHERE user_id=? AND anilist_id=?",
                (user, mapping.anilist_id),
            ).fetchone()
            if row is None:
                return  # First baseline needs every initial conflict/patch acknowledged.
            if row is not None:
                old = _baseline(_open(row[0]))
                _check(old.pair == pair and old.mapping == mapping, "connection_changed")
                # Preserve prior values of every unresolved/conflicting field.
                a = replace(
                    a,
                    state=replace(
                        old.anilist.state, **{k: getattr(a.state, k) for k in changed_fields}
                    ),
                )
                m = replace(
                    m,
                    state=replace(
                        old.mal.state, **{k: getattr(m.state, k) for k in changed_fields}
                    ),
                )
        baseline = Baseline(pair, mapping, a, m)
        with self.db:
            self.db.execute(
                "INSERT INTO tracker_bridge_baselines VALUES(?,?,?,?,?) ON CONFLICT(user_id,anilist_id) DO UPDATE SET mal_id=excluded.mal_id,encrypted_payload=excluded.encrypted_payload,updated_at=excluded.updated_at",
                (
                    user,
                    mapping.anilist_id,
                    mapping.mal_id,
                    _seal(asdict(baseline)),
                    int(self.now()),
                ),
            )

    def _prune(self, user):
        """Retire only this owner's expired temporary snapshots; never remote lists."""
        self._owner(user)
        stamp = int(self.now())
        expired = self.db.execute(
            "SELECT id FROM tracker_bridge_plans WHERE user_id=? AND expires_at<=? AND state!='cancelled' ORDER BY created_at LIMIT 20",
            (user, stamp),
        ).fetchall()
        with self.db:
            self.db.execute(
                "DELETE FROM tracker_bridge_scans WHERE user_id=? AND created_at<?",
                (user, stamp - 900),
            )
            for row in expired:
                self.db.execute(
                    "UPDATE tracker_bridge_outbox SET state=CASE WHEN state IN ('leased','ambiguous') THEN 'ambiguous' ELSE 'cancelled' END,last_code='preview_expired' WHERE user_id=? AND plan_id=? AND state!='acknowledged'",
                    (user, row["id"]),
                )
                self.db.execute(
                    "UPDATE tracker_bridge_plans SET state='cancelled',encrypted_payload=? WHERE user_id=? AND id=?",
                    (_seal({"retired": True}), user, row["id"]),
                )
            self.db.execute(
                "DELETE FROM tracker_bridge_plans WHERE user_id=? AND id IN (SELECT p.id FROM tracker_bridge_plans p WHERE p.user_id=? AND p.state='cancelled' AND p.expires_at<? AND NOT EXISTS(SELECT 1 FROM tracker_bridge_outbox o WHERE o.plan_id=p.id AND o.user_id=p.user_id AND o.state IN ('ambiguous','leased')) ORDER BY p.created_at LIMIT 20)",
                (user, user, stamp - 7 * 86400),
            )

    def _scan(self, user, provider, identity, token):
        client = self.mal if provider == Provider.MAL else self.anilist
        if not hasattr(client, "scan_page"):
            result = client.inventory(token, identity)
            return (*result, True) if provider == Provider.ANILIST else (result, (), True)
        row = self.db.execute(
            "SELECT * FROM tracker_bridge_scans WHERE user_id=? AND provider=?",
            (user, provider.value),
        ).fetchone()
        if row is not None and (
            row["generation"] != identity.generation or row["created_at"] < int(self.now()) - 900
        ):
            with self.db:
                self.db.execute(
                    "DELETE FROM tracker_bridge_scans WHERE user_id=? AND provider=?",
                    (user, provider.value),
                )
            row = None
        payload = (
            _open(row["encrypted_payload"])
            if row
            else {"entries": [], "mappings": [], "titles": {}}
        )
        entries = [_entry(e) for e in payload["entries"]]
        mappings = [ExactMapping(**m) for m in payload["mappings"]]
        client.titles.update({int(k): v for k, v in payload["titles"].items()})
        cursor = row["cursor"] if row else 0 if provider == Provider.MAL else 1
        complete = bool(row and row["complete"])
        for _ in range(2):
            if complete:
                break
            new, new_mappings, next_cursor = client.scan_page(token, identity, cursor)
            seen = {e.media_id for e in entries}
            _check(not seen.intersection(e.media_id for e in new), "duplicate_inventory_entry")
            entries.extend(new)
            mappings.extend(new_mappings)
            _check(len(entries) <= 10000, "inventory_limit_exceeded")
            complete = next_cursor is None
            cursor = next_cursor
            payload = {
                "entries": [asdict(e) for e in entries],
                "mappings": [asdict(m) for m in mappings],
                "titles": client.titles,
            }
            _check(len(_json(payload).encode()) <= 8 * 1024 * 1024, "inventory_limit_exceeded")
            with self.db:
                self.db.execute(
                    "INSERT INTO tracker_bridge_scans VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,provider) DO UPDATE SET cursor=excluded.cursor,complete=excluded.complete,encrypted_payload=excluded.encrypted_payload",
                    (
                        user,
                        provider.value,
                        identity.generation,
                        cursor,
                        int(complete),
                        _seal(payload),
                        row["created_at"] if row else int(self.now()),
                    ),
                )
        return Inventory(identity, tuple(entries), complete=complete), tuple(mappings), complete

    @_provider_guard
    def preview(self, user, *, score_choices=()):
        with writer_lease(self.db, user, now=self.now()):
            self._prune(user)
            self.anilist.deadline = self.mal.deadline = self._provider_deadline()
            pair, tokens = self._context(user, verify=True)
            a, mappings, done_a = self._scan(
                user, Provider.ANILIST, pair.anilist, tokens[Provider.ANILIST]
            )
            m, _, done_m = self._scan(user, Provider.MAL, pair.mal, tokens[Provider.MAL])
            if not (done_a and done_m):
                return {
                    "status": "reading",
                    "provider_writes": 0,
                    "scans": {
                        "anilist": {"read": len(a.entries), "complete": done_a},
                        "myanimelist": {"read": len(m.entries), "complete": done_m},
                    },
                }
            mappings = list(mappings)
            with self.db:
                for mapping in mappings:
                    self.db.execute(
                        "INSERT INTO tracker_bridge_id_mappings VALUES(?,?,?) ON CONFLICT(anilist_id) DO UPDATE SET mal_id=excluded.mal_id,verified_at=excluded.verified_at",
                        (mapping.anilist_id, mapping.mal_id, int(self.now())),
                    )
            for row in self.db.execute(
                "SELECT anilist_id,mal_id FROM tracker_bridge_id_mappings WHERE verified_at>?",
                (int(self.now()) - 900,),
            ):
                if row["mal_id"] in {e.media_id for e in m.entries} and not any(
                    x.mal_id == row["mal_id"] for x in mappings
                ):
                    mappings.append(ExactMapping(row["anilist_id"], row["mal_id"], row["mal_id"]))
            missing = sorted({e.media_id for e in m.entries} - {x.mal_id for x in mappings})
            # Catalog mapping calls are bounded independently; unresolved IDs remain visible.
            for mal_id in missing[:10]:
                exact = self.anilist.mapping(tokens[Provider.ANILIST], mal_id)
                if exact is not None:
                    mappings.append(exact)
                    with self.db:
                        self.db.execute(
                            "INSERT INTO tracker_bridge_id_mappings VALUES(?,?,?) ON CONFLICT(anilist_id) DO UPDATE SET mal_id=excluded.mal_id,verified_at=excluded.verified_at",
                            (exact.anilist_id, exact.mal_id, int(self.now())),
                        )
            decisions = tuple(
                ScoreDecision(pair, ExactMapping(x[0], x[1], x[1]), x[2], x[3])
                for x in score_choices
            )
            plan = plan_reconciliation(
                pair, a, m, tuple(mappings), self._baselines(user), score_decisions=decisions
            )
            _check(not plan.blocked, "plan_blocked")
            items = []
            for patch in (*plan.proposals, *plan.patches):
                unsupported = [
                    name
                    for name, value in patch.changes
                    if name in ("started_at", "completed_at")
                    or (name == "score" and value is None)
                    or (
                        patch.target == Provider.ANILIST
                        and name == "rewatching"
                        and value is False
                        and "status" not in dict(patch.changes)
                    )
                ]
                items.append(
                    {
                        "key": str(len(items)),
                        "patch": asdict(patch),
                        "unsupported": unsupported,
                        "review_only": any(patch == p for p in plan.proposals),
                        "title": self.anilist.titles.get(patch.mapping.anilist_id)
                        or self.mal.titles.get(patch.mapping.mal_id)
                        or f"AniList {patch.mapping.anilist_id} / MAL {patch.mapping.mal_id}",
                    }
                )
            observed = []
            aa = {e.media_id: e for e in a.entries}
            mm = {e.media_id: e for e in m.entries}
            for mapping in mappings:
                ae, me = aa.get(mapping.anilist_id), mm.get(mapping.mal_id)
                if ae is not None and me is not None and ae.state == me.state:
                    observed.append(asdict(Baseline(pair, mapping, ae, me)))
            payload = {
                "pair": asdict(pair),
                "items": items,
                "conflicts": [asdict(c) for c in plan.conflicts],
                "unmapped": [(p.value, i) for p, i in plan.unmapped],
                "titles": {
                    str(mapping.anilist_id): self.anilist.titles.get(mapping.anilist_id)
                    or self.mal.titles.get(mapping.mal_id)
                    or f"AniList {mapping.anilist_id} / MAL {mapping.mal_id}"
                    for mapping in mappings
                },
                "equal_baselines": observed,
                "observations": [
                    {
                        "mapping": asdict(mapping),
                        "anilist": asdict(aa[mapping.anilist_id])
                        if mapping.anilist_id in aa
                        else None,
                        "mal": asdict(mm[mapping.mal_id]) if mapping.mal_id in mm else None,
                    }
                    for mapping in mappings
                ],
                "initial_ids": [
                    mapping.anilist_id
                    for mapping in mappings
                    if not any(b.mapping == mapping for b in self._baselines(user))
                ],
            }
            raw = _json(payload)
            _check(len(raw.encode()) <= 8 * 1024 * 1024, "plan_too_large")
            identifier = secrets.token_hex(16)
            digest = _hash(raw)
            with self.db:
                self.db.execute(
                    "INSERT INTO tracker_bridge_plans VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier,
                        user,
                        digest,
                        _seal(payload),
                        int(self.now()),
                        int(self.now()) + 900,
                        "preview",
                    ),
                )
            with self.db:
                self.db.execute("DELETE FROM tracker_bridge_scans WHERE user_id=?", (user,))
        return self.plan(user, identifier)

    def resolve_plan(self, user, identifier, digest, choices):
        """Derive a new review plan from explicit field choices; no provider request."""
        row = self._plan_row(user, identifier)
        _check(
            row["plan_hash"] == digest
            and row["state"] == "preview"
            and row["expires_at"] > int(self.now()),
            "plan_changed_or_expired",
        )
        _check(isinstance(choices, list) and len(choices) <= 20, "invalid_choices")
        payload = _open(row["encrypted_payload"])
        pair = self._current_pair(user)
        _check(pair == _pair(payload["pair"]), "connection_changed")
        observations = {o["mapping"]["anilist_id"]: o for o in payload["observations"]}
        grouped = {}
        resolved = set()
        for choice in choices:
            _check(
                isinstance(choice, dict) and set(choice) <= {"conflict", "source", "round_to"},
                "invalid_choices",
            )
            key = choice.get("conflict")
            _check(
                isinstance(key, str)
                and key.isdigit()
                and int(key) < len(payload["conflicts"])
                and key not in resolved,
                "invalid_choices",
            )
            index = int(key)
            conflict = payload["conflicts"][index]
            source = choice.get("source")
            _check(source in ("anilist", "myanimelist", "keep_separate"), "invalid_choices")
            _check(conflict["field"] not in ("entry",), "entry_absence_needs_new_preview")
            resolved.add(key)
            if source == "keep_separate":
                continue
            mapping = ExactMapping(**conflict["mapping"])
            observed = observations[mapping.anilist_id]
            a, m = _entry(observed["anilist"]), _entry(observed["mal"])
            _check(a is not None and m is not None, "entry_absence_needs_new_preview")
            origin, target = (a, Provider.MAL) if source == "anilist" else (m, Provider.ANILIST)
            destination = m if target == Provider.MAL else a
            names = (
                ("status", "progress", "repeat", "rewatching")
                if conflict["field"] == "progression"
                else (conflict["field"],)
            )
            values = {name: getattr(origin.state, name) for name in names}
            _check(
                not any(
                    name in ("started_at", "completed_at") or value is None
                    for name, value in values.items()
                ),
                "unsupported_field_or_clear",
            )
            if target == Provider.MAL and "score" in values and values["score"] % 10:
                decision = ScoreDecision(pair, mapping, values["score"], choice.get("round_to"))
                values["score"] = decision.target_score
            if target == Provider.ANILIST and values.get("rewatching") is False:
                values.setdefault("status", origin.state.status)
            bucket = grouped.setdefault((mapping, target), (origin, destination, {}))
            for name, value in values.items():
                _check(name not in bucket[2] or bucket[2][name] == value, "conflicting_choices")
                if getattr(destination.state, name) != value:
                    bucket[2][name] = value
        items = list(payload["items"])
        for (mapping, target), (origin, destination, values) in grouped.items():
            if not values:
                continue
            existing = next(
                (
                    i
                    for i in items
                    if i["patch"]["mapping"] == asdict(mapping)
                    and i["patch"]["target"] == target.value
                ),
                None,
            )
            if existing:
                existing_patch = _patch(existing["patch"])
                merged = dict(existing_patch.changes)
                _check(
                    all(k not in merged or merged[k] == v for k, v in values.items()),
                    "conflicting_choices",
                )
                merged.update(values)
                existing["patch"] = asdict(
                    PlannedPatch(pair, mapping, target, tuple(merged.items()), origin, destination)
                )
            else:
                patch = PlannedPatch(
                    pair, mapping, target, tuple(values.items()), origin, destination
                )
                items.append(
                    {
                        "key": str(len(items)),
                        "patch": asdict(patch),
                        "unsupported": [],
                        "title": payload.get("titles", {}).get(
                            str(mapping.anilist_id),
                            f"AniList {mapping.anilist_id} / MAL {mapping.mal_id}",
                        ),
                    }
                )
        payload["items"] = items
        payload["conflicts"] = [
            c for i, c in enumerate(payload["conflicts"]) if str(i) not in resolved
        ]
        raw = _json(payload)
        identifier = secrets.token_hex(16)
        with self.db:
            self.db.execute(
                "INSERT INTO tracker_bridge_plans VALUES(?,?,?,?,?,?,?)",
                (
                    identifier,
                    user,
                    _hash(raw),
                    _seal(payload),
                    int(self.now()),
                    row["expires_at"],
                    "preview",
                ),
            )
        return self.plan(user, identifier)

    def _finalize_initial(self, user, pair, payload, identifier, tokens):
        states = {
            r["item_key"]: r["state"]
            for r in self.db.execute(
                "SELECT item_key,state FROM tracker_bridge_outbox WHERE user_id=? AND plan_id=?",
                (user, identifier),
            )
        }
        reads = 0
        for observed in payload.get("observations", []):
            mapping = ExactMapping(**observed["mapping"])
            a, m = _entry(observed["anilist"]), _entry(observed["mal"])
            existing = self.db.execute(
                "SELECT encrypted_payload FROM tracker_bridge_baselines WHERE user_id=? AND anilist_id=?",
                (user, mapping.anilist_id),
            ).fetchone()
            if existing:
                old = _baseline(_open(existing[0]))
                if (
                    a is None
                    or m is None
                    or a.state != m.state
                    or (old.anilist.state == a.state and old.mal.state == m.state)
                ):
                    continue
            elif mapping.anilist_id not in payload.get("initial_ids", []):
                continue
            if any(c["mapping"]["anilist_id"] == mapping.anilist_id for c in payload["conflicts"]):
                continue
            items = [i for i in payload["items"] if i["patch"]["mapping"] == asdict(mapping)]
            if any(states.get(i["key"]) != "acknowledged" for i in items):
                continue
            expected_a = a.state if a else State()
            expected_m = m.state if m else State()
            for item in items:
                patch = _patch(item["patch"])
                if patch.target == Provider.ANILIST:
                    expected_a = replace(expected_a, **dict(patch.changes))
                else:
                    expected_m = replace(expected_m, **dict(patch.changes))
            if reads >= 5:
                break
            reads += 1
            current_a = self.anilist.entry(
                tokens[Provider.ANILIST], mapping.anilist_id, pair.anilist
            )
            current_m = self.mal.entry(tokens[Provider.MAL], mapping.mal_id)
            if (
                current_a is not None
                and current_m is not None
                and current_a.state == expected_a
                and current_m.state == expected_m
            ):
                current_pair = self._current_pair(user)
                _check(current_pair == pair, "connection_changed")
                self._save_baseline(user, pair, mapping, current_a, current_m)

    def set_enabled(self, user, enabled):
        self._owner(user)
        _check(type(enabled) is bool, "invalid_preference")
        if enabled:
            _check(self.legacy_writers_ready, "writer_coordination_required")
            self._current_pair(user)
            _check(
                self.db.execute(
                    "SELECT 1 FROM tracker_bridge_plans WHERE user_id=? AND state='approved'",
                    (user,),
                ).fetchone()
                is not None,
                "first_preview_approval_required",
            )
        with self.db:
            self.db.execute(
                "INSERT INTO tracker_bridge_preferences VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at",
                (user, int(enabled), int(self.now())),
            )

    def sync_tick(self, user, *, guard=None, deadline=None):
        """One opted-in unit; cancellation/worker expiry is rechecked before writes.

        New entries, lossy values and conflicts always stay in review. The caller
        owns fair scheduling; no timer or provider operation starts on import.
        """
        if not self.status(user)["enabled"]:
            return {"state": "paused"}
        if self.db.execute(
            "SELECT 1 FROM tracker_bridge_outbox WHERE user_id=? AND state IN ('ambiguous','leased') LIMIT 1",
            (user,),
        ).fetchone():
            return {"state": "review_required", "reason": "uncertain_previous_write"}
        previous_guard = getattr(self, "_automatic_guard", None)
        previous_deadline = getattr(self, "_job_deadline", None)
        self._automatic_guard, self._job_deadline = guard, deadline
        try:
            if guard is not None:
                guard()
            preview = self.preview(user)
            if preview.get("status") == "reading":
                return preview
            if guard is not None:
                guard()
            selected = [
                item["key"]
                for item in preview["items"]
                if not item.get("review_only", True) and not item["unsupported"]
            ][:10]
            result = self.apply(user, preview["id"], preview["hash"], approved_items=selected)
            return {
                **result,
                "state": "review_required"
                if preview["counts"]["conflicts"]
                or any(i.get("review_only", True) for i in preview["items"])
                else "checked",
                "plan_id": preview["id"],
            }
        finally:
            self._automatic_guard, self._job_deadline = previous_guard, previous_deadline

    def _provider_deadline(self):
        return min(time.monotonic() + 25, getattr(self, "_job_deadline", None) or float("inf"))

    def _plan_row(self, user, identifier):
        self._owner(user)
        _check(isinstance(identifier, str) and len(identifier) == 32, "plan_not_found")
        row = self.db.execute(
            "SELECT * FROM tracker_bridge_plans WHERE user_id=? AND id=?", (user, identifier)
        ).fetchone()
        _check(row is not None and row["state"] != "cancelled", "plan_not_found")
        return row

    def plan(self, user, identifier):
        row = self._plan_row(user, identifier)
        payload = _open(row["encrypted_payload"])
        for conflict in payload["conflicts"]:
            conflict["title"] = payload.get("titles", {}).get(
                str(conflict["mapping"]["anilist_id"]),
                f"AniList {conflict['mapping']['anilist_id']} / MAL {conflict['mapping']['mal_id']}",
            )
        return {
            "id": identifier,
            "hash": row["plan_hash"],
            "state": row["state"],
            "expires_at": row["expires_at"],
            "items": payload["items"],
            "conflicts": payload["conflicts"],
            "unmapped": payload["unmapped"],
            "counts": {
                "proposals": len(payload["items"]),
                "conflicts": len(payload["conflicts"]),
                "unmapped": len(payload["unmapped"]),
                "equal": len(payload["equal_baselines"]),
            },
        }

    def _fresh(self, patch, tokens):
        exact = self.anilist.mapping(tokens[Provider.ANILIST], patch.mapping.mal_id)
        _check(exact == patch.mapping, "mapping_changed")
        a = self.anilist.entry(
            tokens[Provider.ANILIST], patch.mapping.anilist_id, patch.pair.anilist
        )
        m = self.mal.entry(tokens[Provider.MAL], patch.mapping.mal_id)
        return (a, m) if patch.target == Provider.MAL else (m, a)

    def _owned_lease(self, user, token):
        guard = getattr(self, "_automatic_guard", None)
        if guard is not None:
            guard()
        require_writer_lease(self.db, user, token, now=self.now())

    @_provider_guard
    def apply(self, user, identifier, digest, *, approved_items):
        _check(self.legacy_writers_ready, "writer_coordination_required")
        _check(
            isinstance(approved_items, list)
            and len(approved_items) <= 20
            and len(set(approved_items)) == len(approved_items)
            and all(isinstance(i, str) for i in approved_items),
            "invalid_approval",
        )
        with writer_lease(self.db, user, now=self.now()) as lease:
            self.anilist.deadline = self.mal.deadline = self._provider_deadline()
            row = self._plan_row(user, identifier)
            _check(
                row["plan_hash"] == digest and row["expires_at"] > int(self.now()),
                "plan_changed_or_expired",
            )
            payload = _open(row["encrypted_payload"])
            pair, tokens = self._context(user, verify=True)
            _check(pair == _pair(payload["pair"]), "connection_changed")
            indexed = {item["key"]: item for item in payload["items"]}
            _check(
                all(key in indexed and not indexed[key]["unsupported"] for key in approved_items),
                "unsupported_or_unknown_item",
            )
            with self.db:
                self.db.execute(
                    "UPDATE tracker_bridge_plans SET state='approved' WHERE user_id=? AND id=?",
                    (user, identifier),
                )
                for key in approved_items:
                    self.db.execute(
                        "INSERT OR IGNORE INTO tracker_bridge_outbox(plan_id,user_id,item_key,encrypted_patch,state,updated_at) VALUES(?,?,?,?,'pending',?)",
                        (identifier, user, key, _seal(indexed[key]["patch"]), int(self.now())),
                    )
            queued = self.db.execute(
                "SELECT * FROM tracker_bridge_outbox WHERE user_id=? AND plan_id=? AND state IN ('pending','deferred','leased') AND (state='leased' OR not_before<=?) ORDER BY id LIMIT 10",
                (user, identifier, int(self.now())),
            ).fetchall()
            for item in queued:
                state = item["state"]
                if state == "leased":
                    with self.db:
                        self.db.execute(
                            "UPDATE tracker_bridge_outbox SET state='ambiguous',last_code='interrupted_after_dispatch' WHERE id=? AND user_id=?",
                            (item["id"], user),
                        )
                    continue
                if state not in ("pending", "deferred") or item["not_before"] > int(self.now()):
                    continue
                patch = _patch(_open(item["encrypted_patch"]))
                self._owned_lease(user, lease)
                current_pair, _ = self._context(user)
                _check(current_pair == pair, "connection_changed")
                phase = "preflight"
                try:
                    source, target = self._fresh(patch, tokens)
                    if not preconditions_match(patch, pair, source, target):
                        with self.db:
                            self.db.execute(
                                "UPDATE tracker_bridge_outbox SET state='conflict',last_code='precondition_changed',updated_at=? WHERE id=? AND user_id=?",
                                (int(self.now()), item["id"], user),
                            )
                        continue
                    self._owned_lease(user, lease)
                    # Durable dispatch point; an interruption after this never blindly replays.
                    with self.db:
                        self.db.execute(
                            "UPDATE tracker_bridge_outbox SET state='leased',attempt_count=attempt_count+1,updated_at=? WHERE id=? AND user_id=?",
                            (int(self.now()), item["id"], user),
                        )
                    client = self.mal if patch.target == Provider.MAL else self.anilist
                    phase = "write"
                    client.patch(
                        tokens[patch.target],
                        patch.mapping.media_id(patch.target),
                        dict(patch.changes),
                    )
                    phase = "readback"
                    after_source, after_target = self._fresh(patch, tokens)
                    valid = (
                        after_source == source
                        and after_target is not None
                        and all(getattr(after_target.state, k) == v for k, v in patch.changes)
                    )
                    _check(valid, "readback_mismatch")
                    aa, mm = (
                        (after_source, after_target)
                        if patch.target == Provider.MAL
                        else (after_target, after_source)
                    )
                    current_pair, _ = self._context(user)
                    _check(current_pair == pair, "connection_changed")
                    self._owned_lease(user, lease)
                    self._save_baseline(
                        user, pair, patch.mapping, aa, mm, changed_fields=dict(patch.changes)
                    )
                    with self.db:
                        self.db.execute(
                            "UPDATE tracker_bridge_outbox SET state='acknowledged',last_code='',updated_at=? WHERE id=? AND user_id=? AND state='leased'",
                            (int(self.now()), item["id"], user),
                        )
                except TrackerHTTPError as exc:
                    self.record_provider_error(user, exc)
                    target_state = (
                        "ambiguous"
                        if exc.ambiguous
                        or phase == "readback"
                        or (
                            phase == "write"
                            and exc.code
                            not in (
                                "rate_limited",
                                "job_budget_exhausted",
                                "reconnect_required",
                                "provider_rejected",
                            )
                        )
                        else "deferred"
                        if exc.retry_after
                        else "conflict"
                    )
                    with self.db:
                        self.db.execute(
                            "UPDATE tracker_bridge_outbox SET state=?,last_code=?,not_before=?,updated_at=? WHERE id=? AND user_id=? AND state!='cancelled'",
                            (
                                target_state,
                                exc.code,
                                int(self.now()) + (exc.retry_after or 0),
                                int(self.now()),
                                item["id"],
                                user,
                            ),
                        )
                except BridgeError:
                    with self.db:
                        self.db.execute(
                            "UPDATE tracker_bridge_outbox SET state=?,last_code='precondition_or_readback_unconfirmed',updated_at=? WHERE id=? AND user_id=? AND state!='cancelled'",
                            (
                                "conflict" if phase == "preflight" else "ambiguous",
                                int(self.now()),
                                item["id"],
                                user,
                            ),
                        )
            try:
                current_pair, _ = self._context(user)
                if current_pair == pair:
                    self._finalize_initial(user, pair, payload, identifier, tokens)
            except (BridgeError, TrackerHTTPError):
                pass  # Missing readback never manufactures an initial baseline.
        counts = {
            r[0]: r[1]
            for r in self.db.execute(
                "SELECT state,count(*) FROM tracker_bridge_outbox WHERE user_id=? AND plan_id=? GROUP BY state",
                (user, identifier),
            )
        }
        result = {
            k: counts.get(k, 0)
            for k in ("acknowledged", "ambiguous", "deferred", "pending", "cancelled")
        }
        result["conflicts"] = counts.get("conflict", 0)
        return result
