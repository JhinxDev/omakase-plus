"""Web playback expands provenance without rewriting established viewing records."""

import json
import shutil
import sqlite3

import pytest

from omakase.plus import db as database
from omakase.plus.viewing_history import viewing_history
from omakase.plus.watch.models import WatchEntry, WatchEvent


@pytest.fixture
def historical(tmp_path, monkeypatch):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in database._MIGRATIONS_DIR.glob("*.sql"):
        if int(path.name.split("-")[0]) < 38:
            shutil.copyfile(path, migrations / path.name)
    original = database._MIGRATIONS_DIR
    monkeypatch.setattr(database, "_MIGRATIONS_DIR", migrations)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    database.run_migrations(conn)
    conn.executescript("""
    INSERT INTO users(id,email,password_hash) VALUES(1,'synthetic@example.invalid','hash');
    INSERT INTO sessions(id,user_id,expires_at) VALUES('retained-session',1,'2030-01-01');
    INSERT INTO watch_entries(id,user_id,anilist_id,season_number,last_source,status,progress_episodes,episode_total,completed_at)
      VALUES(41,1,21,1,'manual','completed',12,12,'2026-10-30T12:00:00Z');
    INSERT INTO watch_events(id,watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,observed_at,score_ten)
      VALUES(51,41,1,'manual','manual:retained','completed',12,'manual','accepted','2026-10-30T12:00:00Z',9);
    INSERT INTO watch_confirmations(id,watch_entry_id,watch_event_id,user_id,proposed_episode,prompt)
      VALUES(61,41,51,1,12,'Retained confirmation');
    INSERT INTO tracker_sync_outbox(id,watch_entry_id,watch_event_id,user_id,action,progress_episodes,score_ten,sync_state,attempt_count,last_error)
      VALUES(71,41,51,1,'complete',12,9,'failed',2,'Retained retry');
    INSERT INTO notification_state(user_id,notification_id,read_at) VALUES(1,'retained','2026-10-30');
    INSERT INTO tracker_bridge_connections(user_id,provider,remote_user_id,display_name,generation,state,encrypted_bundle,expires_at,verified_at)
      VALUES(1,'anilist',123,'Synthetic',1,'verified','encrypted-fixture',2000000000,1800000000);
    INSERT INTO library_items(id,library_id,domain,canonical_id,season_number,title,created_by,created_at)
      SELECT 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',library_id,'anime','21',1,'Retained title',1,'2026-10-30' FROM personal_libraries WHERE user_id=1;
    INSERT INTO watch_library_links(user_id,library_item_id,watch_entry_id) VALUES(1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',41);
    INSERT INTO libraries(id,kind,owner_user_id,name) VALUES('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','household',1,'Retained household');
    INSERT INTO household_memberships(library_id,user_id,role,joined_at) VALUES('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',1,'owner','2026-10-30');
    INSERT INTO library_items(id,library_id,domain,canonical_id,season_number,title,created_by,created_at)
      VALUES('cccccccccccccccccccccccccccccccc','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','anime','21',1,'Shared retained title',1,'2026-10-30');
    INSERT INTO watch_library_links(user_id,library_item_id,watch_entry_id) VALUES(1,'cccccccccccccccccccccccccccccccc',41);
    UPDATE sqlite_sequence SET seq=1000 WHERE name IN ('watch_events','watch_confirmations','tracker_sync_outbox');
    """)
    for index, old_source in enumerate(("plex", "apple_tv", "anilist", "migration"), 100):
        conn.execute(
            "INSERT INTO watch_entries(id,user_id,anilist_id,last_source) VALUES(?,1,?,?)",
            (index, index, old_source),
        )
        conn.execute(
            "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,observed_at) VALUES(?,1,?,'retained:source','progress',1,'exact','accepted','2026-10-30T12:00:00Z')",
            (index, old_source),
        )
    conn.commit()
    yield conn, migrations, original / "038-web-player-watch-source.sql"
    conn.close()


def snapshot(conn):
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name!='_migrations'"
        )
    ]
    return {
        table: sorted(
            [
                dict(row)
                for row in conn.execute(f'SELECT * FROM "{table}"')
                if table != "sqlite_sequence" or row["name"] != "_migrations"
            ],
            key=repr,
        )
        for table in tables
    }


def objects(conn):
    return {
        (r["type"], r["name"], r["tbl_name"]): r["sql"]
        for r in conn.execute("SELECT * FROM sqlite_master WHERE type IN ('index','trigger')")
    }


def apply(conn, directory, source, suffix=""):
    (directory / source.name).write_text(source.read_text() + suffix)
    database.run_migrations(conn)


def test_migration_preserves_all_rows_relationships_schema_guards_and_sequences(historical):
    conn, directory, source = historical
    before, guards = snapshot(conn), objects(conn)
    apply(conn, directory, source)
    assert snapshot(conn) == before
    assert objects(conn) == guards
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.execute("UPDATE watch_entries SET last_source='web_player' WHERE id=41")
    event_id = insert_event(conn, source="web_player")
    assert event_id > 1000
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("UPDATE watch_entries SET last_source='page_open' WHERE id=41")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_event(conn, source="page_open")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE watch_events SET reason='changed' WHERE id=51")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert_event(conn, source="web_player", score=8)


def test_failure_after_rebuild_rolls_back_schema_rows_and_marker(historical):
    conn, directory, source = historical
    before, guards = snapshot(conn), objects(conn)
    apply_error = "\nINSERT INTO nonexistent_web_source_038 VALUES(1);"
    with pytest.raises(sqlite3.OperationalError):
        apply(conn, directory, source, apply_error)
    assert snapshot(conn) == before
    assert objects(conn) == guards
    assert not conn.execute("SELECT 1 FROM _migrations WHERE name=?", (source.name,)).fetchone()
    assert list(conn.execute("PRAGMA foreign_key_check")) == []
    assert not list(conn.execute("SELECT name FROM sqlite_temp_master WHERE name LIKE 'web_%_038'"))
    apply(conn, directory, source)
    assert snapshot(conn) == before


def insert_event(
    conn,
    *,
    source="web_player",
    disposition="accepted",
    confidence="exact",
    kind="progress",
    score=None,
    episode=2,
    key=None,
):
    return conn.execute(
        "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,observed_at,payload_json,score_ten) VALUES(41,1,?,?,?,?,?,?,?,?,?)",
        (
            source,
            key or f"{source}:{episode}:{disposition}:{confidence}:{kind}",
            kind,
            episode,
            confidence,
            disposition,
            "2026-10-31T13:00:00Z",
            json.dumps({"watched_at": "2026-10-31T12:59:00Z"}),
            score,
        ),
    ).lastrowid


def test_web_history_is_deliberate_accepted_exact_viewing_with_honest_source(historical):
    conn, directory, source = historical
    apply(conn, directory, source)
    # The source-enum migration is tested in isolation elsewhere. The current
    # read model also requires the later playback tables, even when empty.
    for migration in source.parent.glob("*.sql"):
        if int(migration.name.split("-", 1)[0]) > 38:
            shutil.copyfile(migration, directory / migration.name)
    database.run_migrations(conn)
    assert (
        WatchEvent.progress(source="web_player", episode=2, event_key="web:session").source
        == "web_player"
    )
    assert (
        WatchEntry(anilist_id=21, season_number=1, last_source="web_player").last_source
        == "web_player"
    )
    insert_event(conn)
    insert_event(conn, episode=3, disposition="ignored")
    insert_event(conn, episode=4, confidence="inferred")
    insert_event(conn, episode=5, disposition="confirmation_required")
    insert_event(conn, episode=6, key="web:bulk:import")
    insert_event(conn, episode=7, key="web:migration:import")
    cards = viewing_history(conn, 1)["items"]
    web = [card for card in cards if card["source_label"] == "Web player"]
    assert len(web) == 1
    assert web[0]["episode"] == 2
    assert web[0]["watched_at"] == "2026-10-31T12:59:00Z"
    assert web[0]["watch_time_known"] is True
    assert web[0]["time_label"] == "Watched"
    assert web[0]["source_detail"] == "From Web player"
    assert any(card["source_label"] == "Manual" and card["rating"] == 9 for card in cards)
