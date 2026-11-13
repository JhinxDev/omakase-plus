"""Member title suggestions and owner review, without provider writes."""

from __future__ import annotations

import json

from omakase.plus import households as core

SETTINGS = "/plus/settings/household"


def request_title(db, scope, *, domain, canonical_id, metadata, season=0):
    identity = core._identity(domain, canonical_id, season)
    catalog = core.public_catalog_metadata(metadata)
    title = catalog.get("title") or f"{domain.upper()} {identity}"
    with core._transaction(db):
        core.revalidate_scope(db, scope)
        core._require(scope.can_request_titles, "Title requests are off for this member.", 403)
        key = (scope.library_id, domain, identity, season)
        core._require(
            db.execute(
                """SELECT 1 FROM library_items WHERE library_id=? AND domain=?
                   AND canonical_id=? AND season_number=? AND state='active'""",
                key,
            ).fetchone()
            is None,
            "This title is already in the household library.",
        )
        previous = db.execute(
            """SELECT id FROM household_title_requests WHERE library_id=? AND domain=?
               AND canonical_id=? AND season_number=? AND actor_user_id=? AND state!='cancelled'
               ORDER BY created_at DESC,id DESC LIMIT 1""",
            (*key, scope.viewer_id),
        ).fetchone()
        if previous:
            return previous["id"]
        pending = db.execute(
            """SELECT count(*) FROM household_title_requests
               WHERE library_id=? AND state='pending'""",
            (scope.library_id,),
        ).fetchone()[0]
        core._require(
            pending < 50, "The household request list is full. Try again after review.", 409
        )
        request_id = core._id()
        db.execute(
            """INSERT INTO household_title_requests
               (id,library_id,domain,canonical_id,season_number,actor_user_id,title,metadata_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (request_id, *key, scope.viewer_id, title, json.dumps(catalog), core._stamp()),
        )
        return request_id


def decide(db, scope, *, request_id, decision):
    states = {"approve": "approved", "decline": "declined"}
    core._require(decision in states, "Choose whether to approve or decline.", 422)
    state = states[decision]
    with core._transaction(db):
        core.revalidate_scope(db, scope, owner=True)
        request = db.execute(
            "SELECT * FROM household_title_requests WHERE library_id=? AND id=?",
            (scope.library_id, request_id),
        ).fetchone()
        core._require(request is not None, "Request not available.", 404)
        if request["state"] == state:
            return
        core._require(request["state"] == "pending", "This request has already been closed.")
        member = core.resolve_scope(
            db, viewer_id=request["actor_user_id"], library_id=scope.library_id
        )
        core._require(member.can_request_titles, "This member can no longer request titles.", 403)
        if state == "approved":
            item = db.execute(
                """SELECT 1 FROM library_items WHERE library_id=? AND domain=?
                   AND canonical_id=? AND season_number=? AND state='active'""",
                (
                    scope.library_id,
                    request["domain"],
                    request["canonical_id"],
                    request["season_number"],
                ),
            ).fetchone()
            if item is None:
                core.LibraryRepository(db, scope).add(
                    domain=request["domain"],
                    canonical_id=request["canonical_id"],
                    season=request["season_number"],
                    title=request["title"],
                    metadata=json.loads(request["metadata_json"]),
                )
        db.execute(
            "UPDATE household_title_requests SET state=?,decided_at=? WHERE library_id=? AND id=?",
            (state, core._stamp(), scope.library_id, request_id),
        )


def for_settings(db, scope):
    core.revalidate_scope(db, scope)
    where = "r.library_id=?"
    params = (scope.library_id,)
    if scope.role != "owner":
        where += " AND r.actor_user_id=?"
        params += (scope.viewer_id,)
    return db.execute(
        f"""SELECT r.id,r.domain,r.title,r.season_number,r.state,m.display_name AS requester
            FROM household_title_requests r JOIN household_memberships m
              ON m.library_id=r.library_id AND m.user_id=r.actor_user_id
            WHERE {where}
            ORDER BY (r.state='pending') DESC,r.created_at DESC,r.id DESC LIMIT 70""",
        params,
    ).fetchall()
