"""Retention decisions must protect the live compose chain and keep rollbacks."""

from __future__ import annotations

from ops.release import retention


def test_protected_directories_covers_every_compose_parent() -> None:
    files = [
        "/opt/stacks/omakase-plus/compose-plus.yaml",
        "/opt/stacks/omakase-plus/backups/release-a/candidate.json",
        "/opt/stacks/omakase-plus/receipts/release-b/candidate.json",
    ]
    protected = retention.protected_directories(files, "/opt/stacks/omakase-plus")
    assert "/opt/stacks/omakase-plus" in protected
    assert "/opt/stacks/omakase-plus/backups/release-a" in protected
    assert "/opt/stacks/omakase-plus/receipts/release-b" in protected
    assert "/opt/stacks/omakase-plus/backups/release-old" not in protected


def test_select_directory_victims_keeps_protected_and_recent() -> None:
    entries = [
        {"path": "/stack/backups/live-a", "mtime": 50.0, "bytes": 10},
        {"path": "/stack/backups/recent-b", "mtime": 40.0, "bytes": 20},
        {"path": "/stack/backups/recent-c", "mtime": 30.0, "bytes": 30},
        {"path": "/stack/backups/recent-d", "mtime": 20.0, "bytes": 40},
        {"path": "/stack/backups/old-e", "mtime": 10.0, "bytes": 50},
    ]
    protected = {"/stack/backups/live-a"}
    delete, keep = retention.select_directory_victims(entries, protected, keep_recent=2)
    kept_paths = {item["path"] for item in keep}
    deleted_paths = {item["path"] for item in delete}
    assert kept_paths == {
        "/stack/backups/live-a",
        "/stack/backups/recent-b",
        "/stack/backups/recent-c",
    }
    assert deleted_paths == {"/stack/backups/recent-d", "/stack/backups/old-e"}
    assert any(item["why"] == "compose input" for item in keep)
    assert all(item["why"] for item in delete)


def test_plan_totals_add_directories_and_artifacts() -> None:
    plan = retention.Plan(
        delete_dirs=[{"bytes": 100}, {"bytes": 25}],
        delete_artifacts=[{"bytes": 5}],
    )
    assert plan.reclaim_bytes == 130
