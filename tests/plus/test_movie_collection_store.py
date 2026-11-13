"""Collection membership comes from native inventory or personal ownership, never cache."""

from __future__ import annotations

import sqlite3

import pytest

from omakase.plus import db as plus_db
from omakase.plus import movies_store as store

NOW = "2026-10-27T08:30:00Z"


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def execute(self, sql, parameters=(), /):
        self.calls.append((sql, tuple(parameters)))
        return super().execute(sql, parameters)


@pytest.fixture
def conn():
    db = sqlite3.connect(":memory:", factory=TrackedConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    plus_db.run_migrations(db)
    db.executemany(
        "INSERT INTO users (id,email,password_hash) VALUES (?,?,?)",
        [(1, "one@example.invalid", "fixture"), (2, "two@example.invalid", "fixture")],
    )
    db.commit()
    yield db
    db.close()


def imdb(number):
    return f"tt{number:07d}"


def movie(conn, number, title, *, user_id=1):
    identity = imdb(number)
    conn.execute(
        "INSERT INTO movie_catalog (user_id,imdb_id,tmdb_id,title,updated_at) VALUES (?,?,?,?,?)",
        (user_id, identity, number, title, NOW),
    )
    return identity


def request(conn, identity, *, user_id=1):
    conn.execute(
        "INSERT INTO movie_requests (user_id,imdb_id,detail,created_at,updated_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, identity, "Explicit request", NOW, NOW),
    )


def history(
    conn,
    identity,
    *,
    user_id=1,
    status="unwatched",
    status_source="none",
    rating=None,
    rating_source="none",
):
    tmdb_id = conn.execute(
        "SELECT tmdb_id FROM movie_catalog WHERE user_id=? AND imdb_id=?", (user_id, identity)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO movie_history "
        "(user_id,imdb_id,tmdb_id,status,status_source,rating,rating_source,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, identity, tmdb_id, status, status_source, rating, rating_source, NOW, NOW),
    )


def identities(rows):
    assert all(isinstance(row, dict) for row in rows)
    return [row["imdb_id"] for row in rows]


def test_search_and_discovery_cache_do_not_become_collection(conn):
    identity = movie(conn, 1, "Cached search result")
    history(conn, identity)
    assert store.collection_movies(conn, 1, native_ids=set()) == []
    assert identities(store.list_movies(conn, 1)) == [identity]


def test_exact_native_riven_and_plex_ids_include_only_owner_catalog_rows(conn):
    riven = movie(conn, 1, "Riven movie")
    plex = movie(conn, 2, "Plex movie")
    movie(conn, 3, "Public cache")
    foreign = movie(conn, 4, "Other user's native movie", user_id=2)
    rows = store.collection_movies(conn, 1, native_ids={riven, plex, foreign})
    assert identities(rows) == [plex, riven]
    assert all(row["user_id"] == 1 for row in rows)


@pytest.mark.parametrize(
    "values",
    [
        {"status_source": "manual"},
        {"status": "completed", "status_source": "plex"},
        {"rating": 8.5, "rating_source": "manual"},
        {"rating": None, "rating_source": "manual"},
        {"rating": 7, "rating_source": "plex"},
    ],
)
def test_explicit_status_or_rating_provenance_includes_history(conn, values):
    identity = movie(conn, 1, "Personal history")
    history(conn, identity, **values)
    assert identities(store.collection_movies(conn, 1, native_ids=set())) == [identity]


def test_history_values_without_personal_provenance_are_not_ownership(conn):
    identity = movie(conn, 1, "No provenance")
    history(conn, identity, status="completed", rating=8.5)
    assert store.collection_movies(conn, 1, native_ids=set()) == []


def test_requests_and_history_are_scoped_to_the_same_user(conn):
    identity = movie(conn, 1, "Cached for owner")
    movie(conn, 1, "Another user's title", user_id=2)
    request(conn, identity, user_id=2)
    history(conn, identity, user_id=2, status_source="manual")
    owned = movie(conn, 2, "Owner's explicit request")
    request(conn, owned)
    assert identities(store.collection_movies(conn, 1, native_ids=set())) == [owned]
    assert identities(store.collection_movies(conn, 2, native_ids=set())) == [identity]


def test_requested_only_excludes_native_and_history_membership(conn):
    requested = movie(conn, 1, "Requested")
    native = movie(conn, 2, "Native only")
    watched = movie(conn, 3, "History only")
    request(conn, requested)
    history(conn, watched, status_source="manual")
    assert identities(
        store.collection_movies(conn, 1, native_ids={requested, native}, requested_only=True)
    ) == [requested]
    assert len(store.collection_movies(conn, 1, native_ids={requested, native})) == 3


def test_many_public_cached_rows_cannot_displace_real_members(conn):
    for number in range(1, 151):
        movie(conn, number, f"A cached {number:03d}")
    native = movie(conn, 1001, "Z native")
    requested = movie(conn, 1002, "Z request")
    watched = movie(conn, 1003, "Z watched")
    request(conn, requested)
    history(conn, watched, status_source="plex")
    assert identities(store.collection_movies(conn, 1, native_ids={native})) == [
        native,
        requested,
        watched,
    ]
    assert len(store.list_movies(conn, 1)) == 100


def test_casefold_order_and_id_ties_are_stable(conn):
    titles = ["zebra", "Straße", "STRASSE", "alpha", "éclair", "Éclair"]
    native = {movie(conn, number, title) for number, title in enumerate(titles, 1)}
    rows = store.collection_movies(conn, 1, native_ids=native)
    assert [(row["title"], row["imdb_id"]) for row in rows] == sorted(
        [(title, imdb(number)) for number, title in enumerate(titles, 1)],
        key=lambda row: (row[0].casefold(), row[1]),
    )


def test_sql_candidate_limit_uses_the_same_casefold_order_as_final_limit(conn):
    native = {movie(conn, number, "zulu") for number in range(1, 101)}
    first = movie(conn, 101, "ß")  # casefold='ss', before zulu, unlike SQLite NOCASE.
    native.add(first)
    request(conn, first)  # Also ensure membership sources are deduplicated.
    rows = store.collection_movies(conn, 1, native_ids=native)
    assert len(rows) == 100
    assert identities(rows)[0] == first
    assert len(set(identities(rows))) == 100


def test_global_top_100_merges_late_chunks_with_personal_candidates(conn):
    native = {movie(conn, number, "zulu") for number in range(1, 1100)}
    late = movie(conn, 1100, "A late native chunk")
    native.add(late)
    requested = []
    for number in range(1500, 1550):
        identity = movie(conn, number, f"B request {number}")
        request(conn, identity)
        requested.append(identity)
    rows = store.collection_movies(conn, 1, native_ids=native)
    assert identities(rows) == [late, *requested, *(imdb(number) for number in range(1, 50))]


def test_large_native_set_is_chunked_under_sqlite_variable_limit(conn):
    native = {imdb(number) for number in range(1, 1101)}
    expected = []
    for number in [1, 400, 401, 800, 801, 1100]:
        expected.append(movie(conn, number, f"Movie {number:04d}"))
    conn.commit()
    changes = conn.total_changes
    conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 401)
    conn.calls.clear()
    rows = store.collection_movies(conn, 1, native_ids=native)
    assert identities(rows) == expected
    assert conn.total_changes == changes
    native_queries = [(sql, params) for sql, params in conn.calls if " IN (" in sql]
    assert len(native_queries) == 3
    assert all(len(params) <= 401 for _, params in native_queries)
    candidates = [(sql, params) for sql, params in conn.calls if "movie_catalog" in sql]
    assert candidates and all("LIMIT 100" in sql for sql, _ in candidates)
    assert all(params[0] == 1 for _, params in candidates)


@pytest.mark.parametrize(
    "bad",
    [
        "tt123456",
        "tt12345678901",
        "TT1234567",
        " tt1234567",
        "tt1234567\n",
        "tt１２３４５６７",
        "tt0000001') OR 1=1 --",
        "tt0000001\x00",
        None,
        1,
        True,
    ],
)
def test_invalid_native_ids_fail_closed_without_sql_injection(conn, bad):
    movie(conn, 1, "Owner")
    with pytest.raises(ValueError):
        store.collection_movies(conn, 1, native_ids={bad})
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM movie_catalog").fetchone()[0] == 1


@pytest.mark.parametrize("native", [None, [], ["tt0000001"], "tt0000001", {"id": "tt0000001"}])
def test_native_id_container_must_be_a_bounded_set(conn, native):
    with pytest.raises(ValueError):
        store.collection_movies(conn, 1, native_ids=native)


def test_more_than_1100_native_ids_is_rejected(conn):
    with pytest.raises(ValueError):
        store.collection_movies(conn, 1, native_ids={imdb(number) for number in range(1, 1102)})


@pytest.mark.parametrize("user_id", [0, True, "1", 3])
def test_invalid_or_unknown_user_is_rejected(conn, user_id):
    with pytest.raises(ValueError):
        store.collection_movies(conn, user_id, native_ids=set())
