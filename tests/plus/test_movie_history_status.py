"""Saved import status must not invent current-connection freshness."""

from dataclasses import replace
from pathlib import Path

import pytest
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader

from omakase.plus.movie_history import MovieHistoryService
from omakase.plus.movies_store import upsert_movie
from tests.plus.test_movies import NativeMovies, service
from tests.plus.test_movies import db as db


def test_saved_status_is_read_only_and_scoped_to_account_and_current_connection(db):
    fake = NativeMovies()
    movies = service(db, fake)
    history = MovieHistoryService(movies)
    stamp = "2026-10-27T07:59:09Z"
    scope = history._scope()
    db.execute("INSERT INTO movie_sync_cursors VALUES (1,?,12,?)", (scope, stamp))
    db.execute("INSERT INTO movie_sync_cursors VALUES (2,?,0,?)", (scope, "2026-10-28T00:00:00Z"))
    db.commit()
    before = db.total_changes
    result = history.sync_status(1)
    assert result["mode"] == "manual" and result["checked_at"] == stamp
    assert result["has_more"] is True and result["completion_known"] is True
    assert "remaining" not in result
    assert history.sync_status(2)["has_more"] is False
    movies.connections = replace(movies.connections, plex_token="other-fixture-token")
    assert history.sync_status(1)["checked_at"] is None
    assert history.sync_status(1)["has_more"] is False
    assert fake.calls == [] and db.total_changes == before


def test_disabled_connection_does_not_reuse_an_older_saved_cursor(db):
    fake = NativeMovies()
    movies = service(db, fake)
    history = MovieHistoryService(movies)
    db.execute(
        "INSERT INTO movie_sync_cursors VALUES (1,?,0,?)",
        (history._scope(), "2026-10-27T07:59:09Z"),
    )
    db.commit()
    movies.connections = replace(movies.connections, enabled=False)
    result = history.sync_status(1)
    assert result["configured"] is False and result["checked_at"] is None
    assert fake.calls == []


def test_old_saved_observations_without_cursor_do_not_claim_never_imported(db):
    history = MovieHistoryService(service(db, NativeMovies()))
    stamp = "2026-10-27T07:59:09Z"
    upsert_movie(
        db, 1, dict(imdb_id="tt0084787", tmdb_id=1091, title="The Thing", year=1982), now=stamp
    )
    db.execute(
        "INSERT INTO movie_plex_history(user_id,source_scope,imdb_id,tmdb_id,plex_key,fingerprint,observed_at) VALUES(1,?,'tt0084787',1091,'1',?,?)",
        (history._scope(), "a" * 64, stamp),
    )
    db.commit()
    result = history.sync_status(1)
    assert result["checked_at"] == stamp and result["completion_known"] is False
    assert result["observed_movies"] == 1


@pytest.mark.parametrize(
    "checked,pending,known,label",
    [
        (None, False, False, "Import Plex history"),
        ("2026-10-27T07:59:09Z", False, True, "Refresh from Plex"),
        ("2026-10-27T07:59:09Z", True, True, "Continue import"),
        ("2026-10-27T07:59:09Z", False, False, "Refresh from Plex"),
    ],
)
def test_history_tools_explain_manual_import_and_saved_batch_state(checked, pending, known, label):
    templates = Path(__file__).resolve().parents[2] / "src/omakase/plus/templates"
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(templates),
            ]
        ),
        autoescape=True,
    )
    html = env.get_template("movies.html").render(
        cards=[],
        history_sync=dict(
            configured=True, checked_at=checked, has_more=pending, completion_known=known
        ),
        csrf_token="fixture-csrf",
    )
    assert "Manual import." in html and "Opening Movies does not refresh it" in html
    assert label in html and "manually saved watched status and ratings are protected" in html
    if checked:
        assert f'datetime="{checked}"' in html
    else:
        assert "No import has been recorded for this account and Plex connection" in html
    if pending:
        assert "saved import batch is unfinished" in html
    if checked and not known:
        assert "completion of the earlier import was not recorded" in html
