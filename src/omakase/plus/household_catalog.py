"""Checked public catalog capabilities, separate from operational media and history."""

from __future__ import annotations

import json

from omakase.plus import households as core


def catalog_connections(db, scope):
    """Owner explicitly shares Seerr metadata capability; principal stays the viewer."""
    from omakase.plus.connections import movie_connections

    core.revalidate_scope(db, scope)
    core._require(scope.kind == "household", "Choose a shared catalog.", 422)
    declared = db.execute(
        "SELECT 1 FROM library_settings WHERE library_id=? AND integration='seerr' AND owner_user_id=?",
        (scope.library_id, scope.operational_owner_id),
    ).fetchone()
    core._require(
        declared is not None, "The owner has not shared a catalog connection for this library.", 409
    )
    return movie_connections(db, scope.operational_owner_id)


def actor_catalog_metadata(db, *, viewer_id, domain, canonical_id):
    identity = core._identity(domain, canonical_id)
    if domain == "movie":
        row = db.execute(
            "SELECT * FROM movie_catalog WHERE user_id=? AND imdb_id=? AND identity_verified=1",
            (viewer_id, identity),
        ).fetchone()
        core._require(
            row is not None and type(row["tmdb_id"]) is int,
            "Review this exact movie in the catalog before adding it.",
            409,
        )
        return core.public_catalog_metadata(dict(row))
    core._require(domain == "tv", "Choose a movie or TV catalog.", 422)
    row = db.execute(
        "SELECT metadata_json FROM tv_catalog WHERE user_id=? AND tmdb_id=?",
        (viewer_id, int(identity)),
    ).fetchone()
    metadata = json.loads(row[0]) if row else None
    core._require(
        isinstance(metadata, dict) and metadata.get("tmdb_id", metadata.get("id")) == int(identity),
        "Review this exact TV series in the catalog before adding it.",
        409,
    )
    metadata["tmdb_id"] = int(identity)
    return core.public_catalog_metadata(metadata)


def add_catalog_entry(db, scope, *, domain, canonical_id):
    with core._transaction(db):
        core.revalidate_scope(db, scope)
        metadata = actor_catalog_metadata(
            db, viewer_id=scope.viewer_id, domain=domain, canonical_id=canonical_id
        )
        return core.LibraryRepository(db, scope).add(
            domain=domain,
            canonical_id=canonical_id,
            title=metadata.get("title") or f"{domain.upper()} {canonical_id}",
            metadata=metadata,
        )
