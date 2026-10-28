"""Server-resolved connector identity; never returns credentials or raw roots."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

from omakase.plus.households import revalidate_scope


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class CurrentLanguageSource:
    library_id: str
    resource_id: str
    owner_user_id: int
    integration: str
    source_generation: str
    library_generation: int


def local_connector_scope(root):
    from omakase.plus.local_import import _root_fingerprint

    return "local-" + _root_fingerprint(root)


def configured_local_root(db, resource):
    """Use the existing maintainer-configured import root, never a request path."""
    from omakase.plus.storage import configure_root

    configured = os.getenv("OMAKASE_IMPORT_ROOT", "")
    if not configured:
        return None
    try:
        root = configure_root(configured, owner_id=resource["owner_user_id"])
        if local_connector_scope(root) != resource["connector_scope"]:
            return None
        return root
    except (ValueError, OSError):
        return None


def configured_local_root_identity(db, resource):
    root = configured_local_root(db, resource)
    if root is None:
        return None
    try:
        stat = root.path.stat()
    except OSError:
        return None
    return digest([local_connector_scope(root), stat.st_dev, stat.st_ino])


def resolve_current_language_source(db, *, scope, resource):
    from omakase.plus import connections

    revalidate_scope(db, scope)
    if resource["owner_user_id"] != scope.operational_owner_id:
        return None
    owner = resource["owner_user_id"]
    connector = resource["connector_scope"]
    integration = "plex" if resource["domain"] in {"movie", "tv"} else "realdebrid"
    if resource["domain"] == "anime" and connector.startswith(("local:", "local-")):
        integration = "local"
        identity = configured_local_root_identity(db, resource)
        if not identity:
            return None
    else:
        url, token = connections._values(db, owner, connections._spec(integration))
        if not url or not token:
            return None
        # Both are consumed only inside the digest, never returned or persisted.
        try:
            url = connections.normalize_connection_url(url)
        except ValueError:
            return None
        server_binding = None
        if integration == "plex":
            machine = connections._read(db, owner, "plex_machine_id")
            section = connections._read(db, owner, "plex_movies_section_id")
            if not machine or not section:
                return None
            server_binding = [machine, section]
        identity = digest([url, token, server_binding])
    settings = db.execute(
        "SELECT library_id,generation FROM library_settings WHERE owner_user_id=? AND integration=? ORDER BY library_id",
        (owner, integration),
    ).fetchall()
    selected = next((row[1] for row in settings if row[0] == scope.library_id), None)
    if scope.kind == "household" and selected is None and integration != "local":
        return None
    generation = digest([owner, connector, integration, identity, [tuple(r) for r in settings]])
    return CurrentLanguageSource(
        scope.library_id, resource["id"], owner, integration, generation, selected or 0
    )


def refresh_source_projection(db, *, scope, resource):
    """Explicit verifier/connection-writer mutation; resolver itself is read-only."""
    source = resolve_current_language_source(db, scope=scope, resource=resource)
    if source is None:
        raise ValueError("Current configured language source is unconfirmed.")
    db.execute(
        "INSERT INTO language_current_sources(library_id,resource_id,owner_user_id,integration,source_generation,library_generation) VALUES(?,?,?,?,?,?) ON CONFLICT(library_id,resource_id) DO UPDATE SET owner_user_id=excluded.owner_user_id,integration=excluded.integration,source_generation=excluded.source_generation,library_generation=excluded.library_generation",
        (
            source.library_id,
            source.resource_id,
            source.owner_user_id,
            source.integration,
            source.source_generation,
            source.library_generation,
        ),
    )
    return source


def require_current_source(db, *, scope, resource, asserted_generation=None):
    source = resolve_current_language_source(db, scope=scope, resource=resource)
    if source is None or (
        asserted_generation is not None and asserted_generation != source.source_generation
    ):
        raise ValueError("Current physical source generation is unconfirmed or changed.")
    return source
