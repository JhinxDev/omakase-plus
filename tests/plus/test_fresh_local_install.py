"""Fresh data-root import-to-library proof using real synthetic media."""

from __future__ import annotations

from omakase.plus import db as plus_db
from omakase.plus.local_import import (
    LocalImportRequest,
    apply_local_import_plan,
    plan_local_import,
)
from omakase.plus.materialize import (
    MaterializationRequest,
    apply_materialization_plan,
    plan_materialization,
)
from omakase.plus.media_store import upsert_monitor
from omakase.plus.storage import configure_root, file_receipt
from tests.helpers.synthetic_media import build_synthetic_mkv


def test_fresh_data_root_imports_verified_multisub_media_and_materializes(tmp_path):
    data = tmp_path / "data"
    imported = tmp_path / "import"
    library = tmp_path / "library"
    imported.mkdir()
    library.mkdir()
    media = build_synthetic_mkv(imported)
    db = plus_db._connect(str(data))
    try:
        user_id = int(
            db.execute(
                "INSERT INTO users (email, password_hash) VALUES (?, ?)",
                ("fresh@example.com", "hash"),
            ).lastrowid
        )
        monitor_id = upsert_monitor(
            db,
            user_id=user_id,
            anilist_id=777,
            title="Fixture Show",
            search_titles=("Fixture Show",),
            format="TV",
            release_status="FINISHED",
            season_number=1,
            expected_episodes=1,
            aired_episodes=1,
        )
        db.commit()
        import_root = configure_root(imported, owner_id=user_id)
        library_root = configure_root(library, owner_id=user_id)
        plan = plan_local_import(
            db,
            user_id=user_id,
            root=import_root,
            requests=(LocalImportRequest(monitor_id, media.name),),
            created_at="2026-07-26T13:00:00Z",
        )
        receipt = apply_local_import_plan(
            db,
            plan=plan,
            expected_plan_hash=plan["plan_hash"],
            root=import_root,
            applied_at="2026-07-26T13:01:00Z",
        )

        assert receipt["usable"] == 1
        tracks = db.execute(
            "SELECT language, switchable FROM media_subtitle_tracks ORDER BY stream_index"
        ).fetchall()
        assert [tuple(row) for row in tracks] == [("eng", 1), ("fra", 1)]

        digest = file_receipt(import_root, media.name).sha256
        publication = plan_materialization(
            source_root=import_root,
            destination_root=library_root,
            requests=(
                MaterializationRequest(
                    source_locator=media.name,
                    destination_locator="anime/Fixture Show/Season 01/Fixture Show S01E01.mkv",
                    content_sha256=digest,
                ),
            ),
        )
        applied = apply_materialization_plan(
            plan=publication,
            expected_plan_hash=publication["plan_hash"],
            source_root=import_root,
            destination_root=library_root,
        )

        assert applied["created_files"][0]["content_sha256"] == digest
        assert (library / "anime/Fixture Show/Season 01/Fixture Show S01E01.mkv").is_file()
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert (
            db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'watch_entries'"
            ).fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM _migrations").fetchone()[0] >= 8
    finally:
        db.close()
