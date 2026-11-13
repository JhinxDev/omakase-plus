"""Reversible personal anime membership; tracker/history/media stay untouched."""

from omakase.plus import households

# The legacy monitor can still serve another library. Its excluded personal
# membership stops work only when every other active resource reference is gone.
MONITOR_EXCLUDED_SQL = """EXISTS (
 SELECT 1 FROM library_items own JOIN personal_libraries p ON p.library_id=own.library_id
 WHERE p.user_id=media_monitors.user_id AND own.domain='anime'
 AND own.canonical_id=CAST(media_monitors.anilist_id AS TEXT)
 AND own.season_number=COALESCE(media_monitors.season_number,0) AND own.state='removed'
) AND NOT EXISTS (
 SELECT 1 FROM library_items i JOIN library_resources r ON r.id=i.resource_id
 JOIN libraries l ON l.id=i.library_id
 WHERE i.state='active' AND l.state='active' AND r.domain='anime'
 AND r.owner_user_id=media_monitors.user_id AND r.legacy_id=CAST(media_monitors.id AS TEXT)
)"""


def local_removed(db, *, user_id, anilist_id, season=0):
    return (
        db.execute(
            "SELECT 1 FROM library_items i JOIN personal_libraries p ON p.library_id=i.library_id "
            "WHERE p.user_id=? AND i.domain='anime' AND i.canonical_id=? AND i.season_number=? "
            "AND i.state='removed'",
            (user_id, str(anilist_id), season or 0),
        ).fetchone()
        is not None
    )


def monitoring_excluded(db, monitor_id):
    return (
        db.execute(
            f"SELECT 1 FROM media_monitors WHERE id=? AND ({MONITOR_EXCLUDED_SQL})", (monitor_id,)
        ).fetchone()
        is not None
    )


def set_local_removed(db, *, user_id, monitor_id, removed, library_id=None):
    """Explicit callers only; retain original monitoring flags for accurate undo."""
    with households._transaction(db):
        personal = households.ensure_personal_library(db, user_id=user_id)
        scope = households.resolve_scope(db, viewer_id=user_id, library_id=library_id or personal)
        households.revalidate_scope(db, scope, owner=True)
        households._require(
            scope.kind == "personal", "Use this household's membership action.", 422
        )
        monitor = db.execute(
            "SELECT * FROM media_monitors WHERE id=? AND user_id=?", (monitor_id, user_id)
        ).fetchone()
        households._require(monitor is not None, "Title unavailable.", 404)
        item_id = households.LibraryRepository(db, scope).add(
            domain="anime",
            canonical_id=str(monitor["anilist_id"]),
            season=monitor["season_number"] or 0,
            title=monitor["title"],
            legacy_id=monitor_id,
        )
        if (
            removed
            and households.LibraryRepository(db, scope).item(item_id, include_removed=True)["state"]
            == "active"
        ):
            households.LibraryRepository(db, scope).remove_membership(item_id)
        elif not removed:
            db.execute(
                "UPDATE library_items SET state='active' WHERE id=? AND library_id=?",
                (item_id, scope.library_id),
            )
        return scope.library_id
