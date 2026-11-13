"""Authorized immutable language requests, physical observations and evaluations."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone

from omakase.plus.acquisition_policy import AcquisitionPolicy, policy_from_json
from omakase.plus.households import LibraryRepository, resolve_scope, revalidate_scope
from omakase.plus.language_policy import (
    DomainLanguagePolicies,
    LanguageEvidence,
    LanguageRequestSnapshot,
    LanguageRule,
    domains_from_mapping,
    evaluate_language,
    evidence_from_mapping,
    evidence_to_mapping,
    request_snapshot_from_mapping,
    rule_from_mapping,
)

CONTRACT_VERSION = "language-policy-v1"


class LanguageContractError(ValueError):
    pass


def _require(ok, message):
    if not ok:
        raise LanguageContractError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _transaction(db):
    nested = db.in_transaction
    name = "language_" + uuid.uuid4().hex
    db.execute("SAVEPOINT " + name if nested else "BEGIN IMMEDIATE")
    try:
        yield
        db.execute("RELEASE SAVEPOINT " + name) if nested else db.commit()
    except BaseException:
        if nested:
            db.execute("ROLLBACK TO SAVEPOINT " + name)
            db.execute("RELEASE SAVEPOINT " + name)
        else:
            db.rollback()
        raise


def library_defaults(db, scope):
    revalidate_scope(db, scope)
    row = db.execute(
        "SELECT * FROM library_language_defaults WHERE library_id=?", (scope.library_id,)
    ).fetchone()
    if row is None:
        return DomainLanguagePolicies.legacy_defaults(), True, True
    policies = domains_from_mapping(json.loads(row["policies_json"]))
    _require(
        _hash(policies.to_mapping()) == row["revision"],
        "Saved language defaults failed revision validation.",
    )
    return policies, bool(row["allow_member_preferences"]), bool(row["allow_request_overrides"])


def set_library_defaults(
    db, scope, policies, *, allow_member_preferences=True, allow_request_overrides=True
):
    _require(isinstance(policies, DomainLanguagePolicies), "Typed language defaults required.")
    _require(
        type(allow_member_preferences) is bool and type(allow_request_overrides) is bool,
        "Language permissions must be booleans.",
    )
    with _transaction(db):
        revalidate_scope(db, scope, owner=True)
        db.execute(
            "INSERT INTO library_language_defaults VALUES(?,?,?,?,?,?) ON CONFLICT(library_id) DO UPDATE SET policies_json=excluded.policies_json,revision=excluded.revision,allow_member_preferences=excluded.allow_member_preferences,allow_request_overrides=excluded.allow_request_overrides,updated_at=excluded.updated_at",
            (
                scope.library_id,
                _json(policies.to_mapping()),
                _hash(policies.to_mapping()),
                int(allow_member_preferences),
                int(allow_request_overrides),
                _now(),
            ),
        )
        db.execute(
            "UPDATE libraries SET policy_revision=policy_revision+1 WHERE id=?", (scope.library_id,)
        )


def _episodes(values):
    _require(
        isinstance(values, (list, tuple, set, frozenset)) and len(values) <= 1000,
        "Bounded exact episode set required.",
    )
    _require(
        all(type(n) is int and 1 <= n <= 5000 for n in values), "Episode identities are invalid."
    )
    return tuple(sorted(set(values)))


def _authorized_item(db, snapshot):
    scope = resolve_scope(db, viewer_id=snapshot.actor_user_id, library_id=snapshot.library_id)
    _require(
        snapshot.library_item_id is not None, "Language request needs an authorized library item."
    )
    item = LibraryRepository(db, scope).item(snapshot.library_item_id)
    parts = snapshot.media_identity.split(":")
    season = int(parts[3]) if len(parts) == 4 else 0
    _require(
        item["domain"] == snapshot.domain
        and item["canonical_id"] == parts[1]
        and item["season_number"] == season,
        "Request title identity does not match the library item.",
    )
    _require(
        snapshot.resource_id is None or item["resource_id"] == snapshot.resource_id,
        "Request resource binding changed.",
    )
    return scope, item


def _validate_request_episode_scope(db, item, domain, episodes):
    if domain != "anime" or item["resource_id"] is None:
        return
    monitor = db.execute(
        "SELECT m.format FROM library_resources r JOIN media_monitors m ON CAST(m.id AS TEXT)=r.legacy_id AND m.user_id=r.owner_user_id WHERE r.id=?",
        (item["resource_id"],),
    ).fetchone()
    if monitor is not None and monitor["format"] == "MOVIE":
        _require(
            tuple(episodes) == (1,),
            "An owned anime movie request requires exact whole-film coverage one.",
        )


def record_request(
    db,
    snapshot,
    *,
    acquisition_policy=None,
    required_episodes=(),
    intent="evaluate",
    household_event_id=None,
):
    _require(isinstance(snapshot, LanguageRequestSnapshot), "Frozen language request required.")
    _require(intent in {"evaluate", "acquire"}, "Choose an explicit request intent.")
    episodes = _episodes(required_episodes)
    _require(snapshot.domain != "movie" or not episodes, "Movies do not have episode requirements.")
    with _transaction(db):
        scope, item = _authorized_item(db, snapshot)
        _validate_request_episode_scope(db, item, snapshot.domain, episodes)
        defaults, member_allowed, override_allowed = library_defaults(db, scope)
        default = defaults.for_domain(snapshot.domain)
        _require(
            snapshot.effective.inherited_revision == default.revision,
            "Library language default changed; resolve a fresh request.",
        )
        if snapshot.effective.source in {"library_default", "legacy_default"}:
            _require(
                snapshot.effective.rule == default, "Default policy does not match the library."
            )
        if snapshot.effective.source == "member_preference":
            _require(
                scope.role == "owner" or member_allowed,
                "Member language preferences are not permitted.",
            )
        if snapshot.effective.source == "request_override":
            _require(
                scope.role == "owner" or override_allowed,
                "Request language override is not permitted.",
            )
        if intent == "acquire":
            _acquisition_permission(scope, snapshot.domain)
        base = acquisition_policy or AcquisitionPolicy()
        _require(isinstance(base, AcquisitionPolicy), "Typed acquisition criteria required.")
        policies = base.languages or defaults
        policies = replace(policies, **{snapshot.domain: snapshot.effective.rule})
        policy = replace(base, languages=policies)
        if household_event_id is not None:
            event = db.execute(
                "SELECT * FROM library_request_events WHERE id=?", (household_event_id,)
            ).fetchone()
            _require(
                event is not None
                and event["actor_user_id"] == snapshot.actor_user_id
                and event["library_id"] == snapshot.library_id
                and event["library_item_id"] == snapshot.library_item_id,
                "Household request event does not match.",
            )
        digest = _hash(
            [
                snapshot.to_mapping(),
                json.loads(policy.to_json()),
                episodes,
                intent,
                household_event_id,
            ]
        )
        existing = db.execute(
            "SELECT request_digest FROM language_request_policies WHERE id=?",
            (snapshot.request_id,),
        ).fetchone()
        if existing:
            _require(
                existing["request_digest"] == digest,
                "Request ID already has a different immutable policy.",
            )
            return snapshot.request_id
        db.execute(
            "INSERT INTO language_request_policies(id,actor_user_id,library_id,library_item_id,resource_id,household_event_id,domain,intent,snapshot_json,acquisition_json,publication_policy_json,publication_policy_revision,required_episodes_json,policy_revision,binding_digest,request_digest,membership_revision,library_policy_revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot.request_id,
                snapshot.actor_user_id,
                snapshot.library_id,
                snapshot.library_item_id,
                snapshot.resource_id,
                household_event_id,
                snapshot.domain,
                intent,
                _json(snapshot.to_mapping()),
                policy.to_json(),
                _json(default.to_mapping()),
                default.revision,
                _json(episodes),
                snapshot.effective.revision,
                snapshot.binding_digest,
                digest,
                scope.membership_revision,
                scope.policy_revision,
                _now(),
            ),
        )
        if snapshot.resource_id:
            db.execute(
                "INSERT INTO language_request_bindings VALUES(?,?,?)",
                (snapshot.request_id, snapshot.resource_id, _now()),
            )
            _capture_request_source(db, snapshot, scope, snapshot.resource_id)
    return snapshot.request_id


def _capture_request_source(db, snapshot, scope, resource_id):
    from omakase.plus.language_source import require_current_source

    resource = db.execute("SELECT * FROM library_resources WHERE id=?", (resource_id,)).fetchone()
    source = require_current_source(db, scope=scope, resource=resource)
    db.execute(
        "INSERT INTO language_request_sources(request_id,source_generation) VALUES(?,?)",
        (snapshot.request_id, source.source_generation),
    )


def current_request_source(db, snapshot, *, asserted_generation=None):
    from omakase.plus.language_source import require_current_source

    scope, item = _authorized_item(db, snapshot)
    binding = db.execute(
        "SELECT resource_id FROM language_request_bindings WHERE request_id=?",
        (snapshot.request_id,),
    ).fetchone()
    _require(binding is not None, "Language request resource is not yet bound.")
    resource = db.execute("SELECT * FROM library_resources WHERE id=?", (binding[0],)).fetchone()
    source = require_current_source(
        db, scope=scope, resource=resource, asserted_generation=asserted_generation
    )
    captured = db.execute(
        "SELECT source_generation FROM language_request_sources WHERE request_id=?",
        (snapshot.request_id,),
    ).fetchone()
    _require(
        captured is not None and captured[0] == source.source_generation,
        "Request source generation changed; create a fresh request.",
    )
    return source


def _acquisition_permission(scope, domain):
    _require(domain != "tv", "Automatic TV acquisition and track verification are unsupported.")
    _require(
        scope.kind != "household" or scope.acquisition_requests,
        "Household acquisition requests are disabled.",
    )
    _require(scope.role == "owner" or scope.members_can_add, "Member acquisition is not permitted.")


def load_request(db, *, actor_user_id, request_id, for_acquisition=False):
    _require(
        type(actor_user_id) is int and actor_user_id > 0, "A valid authenticated actor is required."
    )
    row = db.execute(
        "SELECT * FROM language_request_policies WHERE id=? AND actor_user_id=?",
        (request_id, actor_user_id),
    ).fetchone()
    _require(row is not None, "Language request is not available.")
    snapshot = request_snapshot_from_mapping(json.loads(row["snapshot_json"]))
    _require(
        snapshot.actor_user_id == actor_user_id
        and snapshot.binding_digest == row["binding_digest"]
        and snapshot.effective.revision == row["policy_revision"],
        "Stored request binding failed validation.",
    )
    publication = rule_from_mapping(json.loads(row["publication_policy_json"]))
    _require(
        publication.revision == row["publication_policy_revision"]
        and publication.revision == snapshot.effective.inherited_revision,
        "Stored publication policy failed validation.",
    )
    scope, item = _authorized_item(db, snapshot)
    policy = policy_from_json(row["acquisition_json"])
    episodes = _episodes(json.loads(row["required_episodes_json"]))
    _validate_request_episode_scope(db, item, snapshot.domain, episodes)
    _require(
        _hash(
            [
                snapshot.to_mapping(),
                json.loads(policy.to_json()),
                episodes,
                row["intent"],
                row["household_event_id"],
            ]
        )
        == row["request_digest"],
        "Stored request digest failed validation.",
    )
    if db.execute(
        "SELECT 1 FROM language_request_bindings WHERE request_id=?", (request_id,)
    ).fetchone():
        current_request_source(db, snapshot)
    if for_acquisition:
        _require(row["intent"] == "acquire", "This request does not authorize acquisition.")
        _acquisition_permission(scope, snapshot.domain)
        _require(
            scope.membership_revision == row["membership_revision"]
            and scope.policy_revision == row["library_policy_revision"],
            "Request permissions changed before dispatch.",
        )
        if row["household_event_id"]:
            event = db.execute(
                "SELECT state FROM library_request_events WHERE id=?", (row["household_event_id"],)
            ).fetchone()
            _require(
                event is not None and event["state"] in {"queued", "dispatched"},
                "Household request is no longer dispatchable.",
            )
    return snapshot, policy, episodes


def bind_request_resource(db, *, actor_user_id, request_id, resource_id):
    with _transaction(db):
        snapshot, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
        _, item = _authorized_item(db, snapshot)
        _require(item["resource_id"] == resource_id, "Resource is not bound to the requested item.")
        old = db.execute(
            "SELECT resource_id FROM language_request_bindings WHERE request_id=?", (request_id,)
        ).fetchone()
        _require(
            old is None or old["resource_id"] == resource_id,
            "Immutable request resource is already bound.",
        )
        if old is None:
            db.execute(
                "INSERT INTO language_request_bindings VALUES(?,?,?)",
                (request_id, resource_id, _now()),
            )
            scope, _ = _authorized_item(db, snapshot)
            _capture_request_source(db, snapshot, scope, resource_id)


def record_observation(
    db,
    *,
    observer_user_id,
    resource_id,
    evidence,
    episodes=(),
    source_generation,
    asset_id=None,
    probe_id=None,
    observed_at=None,
    library_id=None,
):
    _require(isinstance(evidence, LanguageEvidence), "Typed exact-file evidence required.")
    _require(
        source_generation is None
        or (
            isinstance(source_generation, str)
            and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", source_generation))
        ),
        "Bounded source generation required.",
    )
    _require(
        evidence.selection_context is None and not any(t.selected for t in evidence.tracks),
        "Physical inventory cannot assert a viewer playback selection.",
    )
    episodes = _episodes(episodes)
    resource = db.execute(
        "SELECT * FROM library_resources WHERE id=? AND owner_user_id=?",
        (resource_id, observer_user_id),
    ).fetchone()
    _require(resource is not None, "Physical observation resource is not owned by this inspector.")
    from omakase.plus.language_source import require_current_source

    if library_id is None:
        candidates = db.execute(
            "SELECT DISTINCT library_id FROM library_items WHERE resource_id=? ORDER BY library_id",
            (resource_id,),
        ).fetchall()
        _require(
            len(candidates) == 1, "Choose the explicit library source for physical inspection."
        )
        library_id = candidates[0][0]
    scope = resolve_scope(db, viewer_id=observer_user_id, library_id=library_id)
    source = require_current_source(
        db, scope=scope, resource=resource, asserted_generation=source_generation
    )
    source_generation = source.source_generation
    inventory_generation = evidence.file_fingerprint
    if resource["domain"] != "anime":
        _require(
            asset_id is None and probe_id is None,
            "Non-anime observations cannot borrow an anime asset or probe.",
        )
        _require(
            resource["domain"] != "movie" or not episodes,
            "Movies do not have episode observations.",
        )
    if resource["domain"] == "anime":
        _require(
            type(asset_id) is int and type(probe_id) is int,
            "Anime observation needs an exact stored asset/file probe.",
        )
        probe = db.execute(
            "SELECT p.*,m.user_id,m.anilist_id,m.season_number,m.expected_episodes,m.format,a.numbering_override,a.planned_coverage_json,a.storage_backend,a.storage_locator,a.inventory_fingerprint AS asset_inventory_generation FROM media_file_probes p JOIN media_assets a ON a.id=p.asset_id JOIN media_monitors m ON m.id=a.monitor_id WHERE p.id=? AND p.asset_id=? AND m.id=? AND m.user_id=?",
            (probe_id, asset_id, resource["legacy_id"], observer_user_id),
        ).fetchone()
        _require(
            probe is not None
            and probe["inventory_fingerprint"] == evidence.file_fingerprint
            and str(probe["anilist_id"]) == resource["canonical_id"]
            and (probe["season_number"] or 0) == resource["season_number"],
            "Physical probe does not match the owned resource/file.",
        )
        _require(
            probe["storage_backend"] != "local"
            or resource["connector_scope"].startswith(("local:", "local-")),
            "Local media needs a qualified current configured-root source contract.",
        )
        inventory_generation = probe["asset_inventory_generation"]
        _require(bool(inventory_generation), "Current inventory generation is unknown.")
        if episodes:
            from omakase.plus.publication_episodes import approved_file_episodes

            marker = json.loads(probe["byte_range_receipt_json"]).get("publication_scope", {})
            planned = set(json.loads(probe["planned_coverage_json"]))
            if probe["format"] == "MOVIE":
                _require(
                    marker == {"format": "MOVIE", "sole_file": True},
                    "Movie language observation needs an exact sole-file publication proof.",
                )
            if probe["storage_backend"] == "local":
                _require(
                    probe["file_locator"] == probe["storage_locator"],
                    "Local observation does not match its exact owned file locator.",
                )
            actual = approved_file_episodes(
                probe["file_locator"],
                media_format=probe["format"],
                season=resource["season_number"],
                expected=probe["expected_episodes"],
                anilist_id=int(resource["canonical_id"]),
                numbering_override=probe["numbering_override"],
                approved_coverage=planned,
                sole_file=marker.get("sole_file") is True,
            )
            _require(
                set(episodes) == actual,
                "Language episodes do not match the exact approved file coverage.",
            )
    stamp = observed_at or _now()
    payload = evidence_to_mapping(evidence)
    digest = _hash(
        [
            resource_id,
            asset_id,
            probe_id,
            payload,
            episodes,
            source_generation,
            inventory_generation,
            stamp,
        ]
    )
    with _transaction(db):
        db.execute(
            "INSERT OR IGNORE INTO asset_language_observations(resource_id,observer_user_id,asset_id,probe_id,file_fingerprint,evidence_json,inventory_generation,episodes_json,source_generation,observation_digest,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                resource_id,
                observer_user_id,
                asset_id,
                probe_id,
                evidence.file_fingerprint,
                _json(payload),
                inventory_generation,
                _json(episodes),
                source_generation,
                digest,
                stamp,
            ),
        )
        return db.execute(
            "SELECT id FROM asset_language_observations WHERE resource_id=? AND observation_digest=?",
            (resource_id, digest),
        ).fetchone()[0]


def evaluate_request(
    db,
    *,
    actor_user_id,
    request_id,
    observation_id=None,
    source_generation=None,
    playback_context=None,
    playback_selection_id=None,
):
    with _transaction(db):
        snapshot, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
        source = current_request_source(db, snapshot, asserted_generation=source_generation)
        if observation_id is None and playback_selection_id is not None:
            selected_file = db.execute(
                "SELECT f.language_observation_id FROM viewer_selection_observations s JOIN viewer_playback_bindings b ON b.id=s.binding_id JOIN movie_file_observations f ON f.id=s.file_observation_id WHERE s.id=? AND b.actor_user_id=?",
                (playback_selection_id, actor_user_id),
            ).fetchone()
            _require(selected_file is not None, "Viewer playback observation is unavailable.")
            observation_id = selected_file[0]
        binding = db.execute(
            "SELECT resource_id FROM language_request_bindings WHERE request_id=?", (request_id,)
        ).fetchone()
        _require(binding is not None, "Request resource has not been bound.")
        observation = db.execute(
            "SELECT * FROM asset_language_observations WHERE id=? AND resource_id=?",
            (observation_id, binding["resource_id"]),
        ).fetchone()
        _require(observation is not None, "Physical observation is not available for this request.")
        _require(
            observation["source_generation"] == source.source_generation,
            "Physical source generation changed; recheck the file.",
        )
        if observation["asset_id"] is not None:
            current = db.execute(
                "SELECT a.inventory_fingerprint,p.inventory_fingerprint AS file_fingerprint FROM media_assets a JOIN media_file_probes p ON p.asset_id=a.id WHERE a.id=? AND p.id=?",
                (observation["asset_id"], observation["probe_id"]),
            ).fetchone()
            _require(
                current is not None
                and current["inventory_fingerprint"] == observation["inventory_generation"]
                and current["file_fingerprint"] == observation["file_fingerprint"],
                "Current file inventory changed; a fresh observation is required.",
            )
        from omakase.plus.language_publication import require_current_file

        require_current_file(db, observation)
        evidence = evidence_from_mapping(json.loads(observation["evidence_json"]))
        selection = None
        if playback_selection_id is not None:
            from omakase.plus.playback_proof import resolve_selection

            selection = resolve_selection(
                db,
                actor_user_id=actor_user_id,
                request_id=request_id,
                observation_id=observation_id,
                selection_id=playback_selection_id,
            )
        context = "selection:" + str(playback_selection_id) if selection else ""
        result = evaluate_language(
            snapshot.effective.rule,
            evidence,
            domain=snapshot.domain,
            selection=selection,
        )
        payload = _json(asdict(result))
        db.execute(
            "INSERT OR IGNORE INTO language_evaluations(request_id,observation_id,policy_revision,file_fingerprint,playback_context,state,physical_compatible,selection_confirmed,evaluation_json,evaluated_at,playback_selection_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                request_id,
                observation_id,
                result.policy_revision,
                result.file_fingerprint,
                context,
                result.state,
                int(result.physical_compatible),
                int(result.selection_confirmed),
                payload,
                _now(),
                playback_selection_id,
            ),
        )
        row = db.execute(
            "SELECT id,evaluation_json FROM language_evaluations WHERE request_id=? AND observation_id=? AND playback_context=?",
            (request_id, observation_id, context),
        ).fetchone()
        _require(
            row["evaluation_json"] == payload,
            "Saved immutable evaluation differs from current evaluator; use a versioned successor.",
        )
        return row["id"], result


def approve_publication_contract(db, *, scope, resource_id, asset_id, rule):
    _require(isinstance(rule, LanguageRule), "Typed publication language rule required.")
    with _transaction(db):
        revalidate_scope(db, scope, owner=True)
        resource = db.execute(
            "SELECT owner_user_id FROM library_resources WHERE id=?", (resource_id,)
        ).fetchone()
        _require(
            resource is not None and resource["owner_user_id"] == scope.viewer_id,
            "Publication resource is not owned.",
        )
        old = db.execute(
            "SELECT * FROM publication_language_contracts WHERE resource_id=? AND asset_id=?",
            (resource_id, asset_id),
        ).fetchone()
        if old:
            _require(
                old["policy_revision"] == rule.revision,
                "Published asset keeps its approved contract; explicit successor publication is required.",
            )
            return old["id"]
        db.execute(
            "INSERT INTO publication_language_contracts(resource_id,asset_id,approved_by,policy_json,policy_revision,contract_version,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                resource_id,
                asset_id,
                scope.viewer_id,
                _json(rule.to_mapping()),
                rule.revision,
                CONTRACT_VERSION,
                _now(),
            ),
        )
        return db.execute(
            "SELECT id FROM publication_language_contracts WHERE resource_id=? AND asset_id=?",
            (resource_id, asset_id),
        ).fetchone()[0]


def persist_request_probe(
    db,
    *,
    actor_user_id,
    request_id,
    asset_id,
    probe_id,
    evidence,
    episodes,
    source_generation,
    observed_at,
):
    snapshot, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    binding = db.execute(
        "SELECT resource_id FROM language_request_bindings WHERE request_id=?", (request_id,)
    ).fetchone()
    _require(binding is not None, "Request resource has not been bound.")
    resource = db.execute(
        "SELECT owner_user_id FROM library_resources WHERE id=?", (binding["resource_id"],)
    ).fetchone()
    observation_id = record_observation(
        db,
        observer_user_id=resource["owner_user_id"],
        resource_id=binding["resource_id"],
        asset_id=asset_id,
        probe_id=probe_id,
        evidence=evidence,
        episodes=episodes,
        source_generation=source_generation,
        observed_at=observed_at,
        library_id=snapshot.library_id,
    )
    return evaluate_request(
        db,
        actor_user_id=actor_user_id,
        request_id=request_id,
        observation_id=observation_id,
        source_generation=source_generation,
    )


def request_language_status(db, *, actor_user_id, request_id, source_generation):
    snapshot, _, required = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    if snapshot.domain == "tv":
        return {"state": "unsupported", "language_coverage_complete": False, "covered_episodes": []}
    from omakase.plus.language_publication import CURRENT_FILE_SQL

    rows = db.execute(
        "SELECT e.state,e.physical_compatible,o.episodes_json,o.id AS observation_id FROM language_evaluations e JOIN asset_language_observations o ON o.id=e.observation_id LEFT JOIN media_assets a ON a.id=o.asset_id LEFT JOIN media_file_probes p ON p.id=o.probe_id WHERE e.request_id=? AND o.source_generation=? AND (o.asset_id IS NULL OR (a.inventory_fingerprint=o.inventory_generation AND p.inventory_fingerprint=o.file_fingerprint AND a.status='downloaded' AND a.downloaded_once=1 AND a.inventory_verified_at IS NOT NULL)) AND "
        + CURRENT_FILE_SQL
        + " ORDER BY e.id DESC",
        (request_id, source_generation),
    ).fetchall()
    covered = set()
    current_rows = []
    for row in rows:
        from omakase.plus.language_publication import require_current_file

        observation = db.execute(
            "SELECT * FROM asset_language_observations WHERE id=?", (row["observation_id"],)
        ).fetchone()
        try:
            require_current_file(db, observation)
        except (ValueError, OSError):
            continue
        current_rows.append(row)
        if row["physical_compatible"]:
            covered.update(json.loads(row["episodes_json"]))
    rows = current_rows
    complete = bool(rows) and (
        set(required) <= covered if required else any(r["physical_compatible"] for r in rows)
    )
    return {
        "state": "language_compatible"
        if complete
        else "pending_verification"
        if rows and rows[0]["physical_compatible"]
        else rows[0]["state"]
        if rows
        else "pending_verification",
        "language_coverage_complete": complete,
        "covered_episodes": sorted(covered),
    }


def evaluate_existing_resource(db, *, actor_user_id, request_id, source_generation):
    snapshot, _, _ = load_request(db, actor_user_id=actor_user_id, request_id=request_id)
    binding = db.execute(
        "SELECT resource_id FROM language_request_bindings WHERE request_id=?", (request_id,)
    ).fetchone()
    _require(binding is not None, "Request resource is not bound.")
    from omakase.plus.language_publication import CURRENT_FILE_SQL

    rows = db.execute(
        "SELECT o.id FROM asset_language_observations o JOIN media_assets a ON a.id=o.asset_id JOIN media_file_probes p ON p.id=o.probe_id WHERE o.resource_id=? AND o.source_generation=? AND a.inventory_fingerprint=o.inventory_generation AND p.inventory_fingerprint=o.file_fingerprint AND a.status='downloaded' AND a.downloaded_once=1 AND a.inventory_verified_at IS NOT NULL AND "
        + CURRENT_FILE_SQL
        + " ORDER BY o.id DESC LIMIT 128",
        (binding["resource_id"], source_generation),
    ).fetchall()
    for row in rows:
        try:
            evaluate_request(
                db,
                actor_user_id=actor_user_id,
                request_id=request_id,
                observation_id=row["id"],
                source_generation=source_generation,
            )
        except (ValueError, OSError):
            continue
    return request_language_status(
        db, actor_user_id=actor_user_id, request_id=request_id, source_generation=source_generation
    )


def bind_acquired_assets(db, *, actor_user_id, request_id, asset_ids):
    snapshot, _, _ = load_request(
        db, actor_user_id=actor_user_id, request_id=request_id, for_acquisition=True
    )
    row = db.execute(
        "SELECT p.publication_policy_json,p.publication_policy_revision,b.resource_id,r.owner_user_id,r.legacy_id FROM language_request_policies p JOIN language_request_bindings b ON b.request_id=p.id JOIN library_resources r ON r.id=b.resource_id WHERE p.id=?",
        (request_id,),
    ).fetchone()
    _require(row is not None, "Acquired resource is not bound.")
    rule = rule_from_mapping(json.loads(row["publication_policy_json"]))
    _require(
        rule.revision == row["publication_policy_revision"],
        "Approved publication policy failed revision validation.",
    )
    owner_scope = resolve_scope(db, viewer_id=row["owner_user_id"], library_id=snapshot.library_id)
    with _transaction(db):
        for asset_id in asset_ids:
            asset = db.execute(
                "SELECT id FROM media_assets WHERE id=? AND monitor_id=?",
                (asset_id, row["legacy_id"]),
            ).fetchone()
            _require(asset is not None, "Acquired asset belongs to another resource.")
            db.execute(
                "INSERT OR IGNORE INTO language_request_assets VALUES(?,?,?)",
                (request_id, asset_id, _now()),
            )
            existing = db.execute(
                "SELECT id FROM publication_language_contracts WHERE resource_id=? AND asset_id=?",
                (row["resource_id"], asset_id),
            ).fetchone()
            if existing is None:
                approve_publication_contract(
                    db,
                    scope=owner_scope,
                    resource_id=row["resource_id"],
                    asset_id=asset_id,
                    rule=rule,
                )
