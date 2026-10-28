"""Saved member language choices, never observations or availability evidence."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from omakase.plus.households import _transaction, revalidate_scope
from omakase.plus.language_policy import LanguageRule, resolve_effective_language, rule_from_mapping
from omakase.plus.language_store import library_defaults


def member_preference(db, scope, domain):
    revalidate_scope(db, scope)
    if domain not in {"anime", "movie", "tv"}:
        raise ValueError("Choose anime, movie or TV.")
    row = db.execute(
        "SELECT rule_json,revision FROM member_language_preferences WHERE library_id=? AND user_id=? AND domain=?",
        (scope.library_id, scope.viewer_id, domain),
    ).fetchone()
    if row is None:
        return None
    rule = rule_from_mapping(json.loads(row["rule_json"]))
    if row["revision"] != rule.revision:
        raise ValueError("Saved language preference failed revision validation.")
    return rule


def set_member_preference(db, scope, domain, rule):
    if (
        domain not in {"anime", "movie", "tv"}
        or rule is not None
        and not isinstance(rule, LanguageRule)
    ):
        raise ValueError("Choose a valid media type and language rule.")
    with _transaction(db):
        revalidate_scope(db, scope)
        _, allowed, _ = library_defaults(db, scope)
        if scope.role != "owner" and not allowed:
            raise ValueError("The library owner has disabled personal language preferences.")
        key = (scope.library_id, scope.viewer_id, domain)
        if rule is None:
            db.execute(
                "DELETE FROM member_language_preferences WHERE library_id=? AND user_id=? AND domain=?",
                key,
            )
        else:
            db.execute(
                "INSERT INTO member_language_preferences(library_id,user_id,domain,rule_json,revision,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(library_id,user_id,domain) DO UPDATE SET rule_json=excluded.rule_json,revision=excluded.revision,updated_at=excluded.updated_at",
                (
                    *key,
                    json.dumps(rule.to_mapping(), sort_keys=True, separators=(",", ":")),
                    rule.revision,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )


def effective_language(db, scope, domain):
    defaults, allowed, _ = library_defaults(db, scope)
    permitted = scope.role == "owner" or allowed
    return resolve_effective_language(
        defaults.for_domain(domain),
        member_preference=member_preference(db, scope, domain) if permitted else None,
        allow_member=permitted,
    )
