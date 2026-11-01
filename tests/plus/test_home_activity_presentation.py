"""Home grouping is presentation only; original activity and causes remain intact."""

from omakase.plus.home_activity import present_home_activity


class Evidence:
    def __init__(self, values):
        self.values = values

    def execute(self, sql, params):
        assert "sync_run_id=?" in sql and "monitor_id=?" in sql
        return self

    def fetchall(self):
        return self.values


def event(identity=1, **values):
    return dict(
        id=identity,
        monitor_id=4,
        title="Fixture",
        outcome="Publication Withheld",
        category="attention",
        detail="This release was not selected. Existing usable episode coverage and approved language scope were preserved.",
        created_at="2026-11-01T10:00:00Z",
        completed_at="2026-11-01T10:00:00Z",
        **values,
    )


def test_identical_saved_attempts_group_without_erasing_sources_or_inventing_a_reason():
    events = [event(3), event(2), event(1)]
    result = present_home_activity(
        Evidence([{"reason": '{"v":1,"c":"SELECTION_DEFERRED"}'}]), events
    )
    assert len(result) == 1
    assert result[0]["repeat_count"] == 3
    assert [row["id"] for row in result[0]["attempts"]] == [3, 2, 1]
    assert result[0]["outcome"] == "Alternative not added"
    assert "does not record a more specific reason" in result[0]["detail"]
    assert result[0]["attempts"][0]["detail"] == events[0]["detail"]
    assert events[0]["outcome"] == "Publication Withheld"


def test_exact_recorded_failure_reason_is_visible_and_different_outcomes_do_not_group():
    result = present_home_activity(
        Evidence([{"reason": '{"v":1,"c":"PLEX_ENGLISH_SUBTITLE_MISSING"}'}]), [event()]
    )
    assert result[0]["outcome"] == "Required subtitles missing"
    assert "English" in result[0]["detail"]
    other = {**event(2), "outcome": "Check failed", "detail": "Connection unavailable"}
    result = present_home_activity(Evidence([]), [event(), other])
    assert len(result) == 2
    assert result[1]["detail"] == "Connection unavailable"


def test_home_reason_uses_exact_run_and_monitor_and_never_mutates_the_ledger():
    import sqlite3

    from omakase.plus.db import run_migrations
    from omakase.plus.media_store import record_sync_run, upsert_monitor

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    run_migrations(db)
    db.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(1,'fixture@example.invalid','fixture')"
    )
    ids = [
        upsert_monitor(db, user_id=1, anilist_id=i, title=f"Fixture {i}", format="TV")
        for i in [1, 2]
    ]
    run = record_sync_run(
        db, monitor_id=ids[0], outcome="publication_withheld", completed_at="2026-11-01T00:00:00Z"
    )
    other_run = record_sync_run(
        db, monitor_id=ids[1], outcome="publication_withheld", completed_at="2026-11-01T00:00:00Z"
    )
    db.execute(
        "INSERT INTO media_candidate_attempts(monitor_id,sync_run_id,search_alias,decision,reason) VALUES(?,?,'publication','PUBLICATION_WITHHELD',?)",
        (ids[1], other_run, '{"v":1,"c":"PLEX_ENGLISH_SUBTITLE_MISSING"}'),
    )
    db.commit()
    rows = [{**event(run), "monitor_id": ids[0]}]
    before = db.total_changes
    assert present_home_activity(db, rows)[0]["outcome"] == "Alternative not added"
    assert db.total_changes == before
    db.execute(
        "INSERT INTO media_candidate_attempts(monitor_id,sync_run_id,search_alias,decision,reason) VALUES(?,?,'publication','PUBLICATION_WITHHELD',?)",
        (ids[0], run, '{"v":1,"c":"PLEX_ENGLISH_SUBTITLE_MISSING"}'),
    )
    db.commit()
    before = db.total_changes
    assert present_home_activity(db, rows)[0]["outcome"] == "Required subtitles missing"
    assert db.total_changes == before
    assert db.execute("SELECT count(*) FROM media_candidate_attempts").fetchone()[0] == 2
    db.close()
