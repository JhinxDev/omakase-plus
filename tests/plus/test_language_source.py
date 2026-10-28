"""Current saved connector generation, not historical caller assertions."""

from dataclasses import asdict

import pytest

from omakase.plus import households as hh
from omakase.plus.language_source import resolve_current_language_source
from omakase.plus.secrets import store_secret
from tests.plus import test_language_store as helpers


@pytest.fixture
def data(tmp_path):
    yield from helpers.setup.__wrapped__(tmp_path)


def test_resolver_is_read_only_and_actual_rd_configuration_or_hook_changes_generation(data):
    db, lib, _, resource, *_ = data
    scope = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    row = db.execute("SELECT * FROM library_resources WHERE id=?", (resource,)).fetchone()
    changes = db.total_changes
    first = resolve_current_language_source(db, scope=scope, resource=row)
    assert first is not None and db.total_changes == changes
    assert "synthetic" not in str(asdict(first))
    hh.connection_generation_changed(db, owner_user_id=1, integration="realdebrid")
    scope = hh.resolve_scope(db, viewer_id=2, library_id=lib)
    second = resolve_current_language_source(db, scope=scope, resource=row)
    assert first.source_generation != second.source_generation
    store_secret(db, 1, "realdebrid_api_key", "rotated-synthetic")
    third = resolve_current_language_source(db, scope=scope, resource=row)
    assert second.source_generation != third.source_generation
    store_secret(db, 1, "realdebrid_api_key", "")
    assert resolve_current_language_source(db, scope=scope, resource=row) is None


def test_unknown_local_root_is_explicitly_unqualified(data):
    db, lib, _, resource, *_ = data
    scope = hh.resolve_scope(db, viewer_id=1, library_id=lib)
    row = dict(db.execute("SELECT * FROM library_resources WHERE id=?", (resource,)).fetchone())
    row["connector_scope"] = "local:unqualified"
    assert resolve_current_language_source(db, scope=scope, resource=row) is None
