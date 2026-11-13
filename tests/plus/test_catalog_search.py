import asyncio
import sqlite3

import httpx
import pytest

from omakase.plus.catalog_search import AnimeCatalogSearch, CatalogSearchError
from omakase.plus.db import run_migrations
from omakase.plus.secrets import store_secret


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    run_migrations(conn)
    conn.execute("INSERT INTO users (id,email,password_hash) VALUES (1,'one@example.test','hash')")
    conn.execute("INSERT INTO users (id,email,password_hash) VALUES (2,'two@example.test','hash')")
    yield conn
    conn.close()


def test_search_fixed_origin_and_no_side_effect(db):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            {
                                "id": 21,
                                "title": {"english": "One Piece"},
                                "episodes": 1100,
                                "mediaListEntry": {
                                    "status": "COMPLETED",
                                    "progress": 100,
                                    "score": 9,
                                },
                            }
                        ]
                    }
                }
            },
        )

    cards = asyncio.run(
        AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(handler)).search("One")
    )
    assert cards[0]["title"] == "One Piece"
    assert cards[0]["url"] == "/plus/catalog/anime/21"
    assert calls[0].url == httpx.URL("https://graphql.anilist.co")
    assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 0


@pytest.mark.parametrize("q", ["", "a", "a" * 121])
def test_invalid_queries_never_call_network(db, q):
    def handler(request):
        raise AssertionError("invalid query reached network")

    with pytest.raises(CatalogSearchError):
        asyncio.run(AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(handler)).search(q))


def test_safe_plaintext_details_and_exact_id(db):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Media": {
                        "id": 21,
                        "title": {"english": "One Piece"},
                        "description": "<b>Adventure</b><script>bad</script>",
                        "genres": ["Adventure"],
                        "episodes": 1100,
                    }
                }
            },
        )

    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(handler)).detail(21))
    assert "<" not in card["description"]
    assert card["watch_status"] == "unknown"


def test_mismatched_detail_identity_rejected(db):
    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200, json={"data": {"Media": {"id": 99, "title": {"english": "Wrong"}}}}
        )
    )
    with pytest.raises(CatalogSearchError):
        asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).detail(21))


def test_rate_limit_preserved_without_retry(db):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"Retry-After": "123"})

    with pytest.raises(CatalogSearchError) as error:
        asyncio.run(AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(handler)).search("one"))
    assert error.value.retry_after == 123
    assert len(calls) == 1


def _media(media_id=21, **extra):
    return {
        "id": media_id,
        "type": "ANIME",
        "title": {"english": "One Piece"},
        "episodes": 1100,
        "seasonYear": 1999,
        "format": "TV",
        **extra,
    }


def test_anonymous_remote_list_fields_are_ignored_and_query_is_read_only(db):
    calls = []

    def native(request):
        calls.append(request)
        payload = __import__("json").loads(request.content)
        assert payload["variables"]["search"] == "One Piece"
        assert "mutation" not in payload["query"]
        assert "mediaListEntry" not in payload["query"]
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "data": {
                    "Page": {
                        "media": [
                            _media(
                                mediaListEntry={"status": "COMPLETED", "score": 9, "progress": 1100}
                            )
                        ]
                    }
                }
            },
        )

    card = asyncio.run(
        AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(native)).search("One Piece")
    )[0]
    assert card["watch_status"] == "unknown" and card["rating"] is None and card["progress"] is None
    assert card["poster_url"] == "/plus/media/21/poster"
    assert len(calls) == 1


def test_oauth_watch_fields_require_exact_media_and_authenticated_viewer_binding(db):
    store_secret(db, 1, "anilist_oauth_token", "owner-oauth-secret")
    calls = []

    def native(request):
        calls.append(request)
        payload = __import__("json").loads(request.content)
        assert request.headers["Authorization"] == "Bearer owner-oauth-secret"
        assert "POINT_10_DECIMAL" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "Viewer": {"id": 900},
                    "Media": _media(
                        mediaListEntry={
                            "mediaId": 21,
                            "userId": 900,
                            "status": "CURRENT",
                            "progress": 100,
                            "score": 8.5,
                        }
                    ),
                }
            },
        )

    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(native)).detail(21))
    assert card["watch_status"] == "watching" and card["progress"] == 100 and card["rating"] == 8.5
    assert "owner-oauth-secret" not in str(card)


@pytest.mark.parametrize("entry", [{"mediaId": 99, "userId": 900}, {"mediaId": 21, "userId": 901}])
def test_private_identity_mismatch_is_not_presented_as_watch_history(db, entry):
    store_secret(db, 1, "anilist_oauth_token", "owner-oauth-secret")
    entry.update(status="COMPLETED", progress=1100, score=10)
    native = httpx.MockTransport(
        lambda request: httpx.Response(
            200, json={"data": {"Viewer": {"id": 900}, "Media": _media(mediaListEntry=entry)}}
        )
    )
    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=native).detail(21))
    assert card["watch_status"] == "unknown" and card["rating"] is None


def test_remote_private_state_is_not_cached_across_users(db):
    store_secret(db, 1, "anilist_oauth_token", "first-user-secret")

    def native(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "Viewer": {"id": 900},
                    "Page": {
                        "media": [
                            _media(
                                mediaListEntry={
                                    "mediaId": 21,
                                    "userId": 900,
                                    "status": "COMPLETED",
                                    "progress": 1100,
                                    "score": 10,
                                }
                            )
                        ]
                    },
                }
            },
        )

    transport = httpx.MockTransport(native)
    first = asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).search("one"))[0]
    other = asyncio.run(AnimeCatalogSearch(db, 2, transport=transport).search("one"))[0]
    assert first["watch_status"] == "completed"
    assert other["watch_status"] == "unknown" and other["rating"] is None


def test_local_watch_projection_and_latest_score_override_remote_unknown(db):
    monitor = db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,expected_episodes) VALUES (1,21,'One Piece',1100)"
    ).lastrowid
    entry = db.execute(
        "INSERT INTO watch_entries(user_id,monitor_id,anilist_id,status,progress_episodes,episode_total,last_source,revision) VALUES (1,?,21,'watching',10,1100,'manual',1)",
        (monitor,),
    ).lastrowid
    db.execute(
        "INSERT INTO watch_events(watch_entry_id,user_id,source,event_key,kind,episode,confidence,disposition,observed_at,score_ten) VALUES (?,1,'manual','manual:scored','completed',10,'manual','accepted','2026-10-27T18:00:00Z',8)",
        (entry,),
    )
    native = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"Media": _media()}})
    )
    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=native).detail(21))
    assert card["watch_status"] == "watching" and card["progress"] == 10 and card["rating"] == 8
    assert card["library_id"] == monitor and card["library_status"] == "tracking"


def test_delivery_only_monitor_does_not_infer_any_watch_state(db):
    db.execute(
        "INSERT INTO media_monitors(user_id,anilist_id,title,last_result) VALUES (1,21,'One Piece','completed')"
    )
    native = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"Media": _media()}})
    )
    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=native).detail(21))
    assert card["library_id"] is not None
    assert card["watch_status"] == "unknown"


@pytest.mark.parametrize("body", [b"x" * 524289, b"not-json"], ids=["oversized", "malformed-json"])
def test_response_boundedness_and_malformed_json_are_safe(db, body):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    with pytest.raises(CatalogSearchError) as error:
        asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).search("one"))
    assert "not-json" not in str(error.value)


def test_suggestions_are_bounded_and_duplicate_identities_are_not_repeated(db):
    rows = [_media(), _media()]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"Page": {"media": rows}}})
    )
    assert len(asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).search("one"))) == 1
    rows[:] = [_media(index) for index in range(1, 10)]
    with pytest.raises(CatalogSearchError):
        asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).search("one"))


def test_description_drops_executable_tags_and_keeps_plain_text(db):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "data": {
                    "Media": _media(
                        description="<p>Good <b>story</b>.</p><script>private()</script><style>bad</style>"
                    )
                }
            },
        )
    )
    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).detail(21))
    assert "Good story." in card["description"]
    assert not any(value in card["description"] for value in ("<", ">", "private()", "bad"))


@pytest.mark.parametrize("start_date", ["not-a-date", [], 7])
def test_malformed_nested_metadata_returns_a_safe_error(db, start_date):
    row = _media(startDate=start_date)
    row.pop("seasonYear", None)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": {"Media": row}})
    )
    with pytest.raises(CatalogSearchError):
        asyncio.run(AnimeCatalogSearch(db, 1, transport=transport).detail(21))


def test_partial_private_history_failure_keeps_public_metadata_with_unknown_history(db):
    store_secret(db, 1, "anilist_oauth_token", "private-user-secret")
    calls = []

    def native(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "data": {"Media": _media(), "Viewer": None},
                "errors": [
                    {
                        "message": "private-user-secret must not be returned",
                        "status": 401,
                        "path": ["Viewer"],
                    }
                ],
            },
        )

    card = asyncio.run(AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(native)).detail(21))
    assert card["title"] == "One Piece" and card["watch_status"] == "unknown"
    assert len(calls) == 1 and "private-user-secret" not in str(card)


def test_graphql_rate_limit_preserves_retry_after_without_loop(db):
    calls = []

    def native(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={"data": None, "errors": [{"status": 429, "message": "private details"}]},
            headers={"Retry-After": "150"},
        )

    with pytest.raises(CatalogSearchError) as error:
        asyncio.run(AnimeCatalogSearch(db, 1, transport=httpx.MockTransport(native)).search("one"))
    assert error.value.code == "rate_limited" and error.value.retry_after == 150
    assert len(calls) == 1
