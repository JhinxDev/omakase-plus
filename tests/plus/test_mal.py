from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from omakase.plus.mal import MALClient, TrackerHTTPError, authorize_url, parse_mal_entry
from omakase.plus.tracker_reconciliation import Identity, Provider


def status(**updates):
    return {
        "status": "watching",
        "score": 0,
        "num_episodes_watched": 2,
        "is_rewatching": False,
        "num_times_rewatched": 0,
        "start_date": None,
        "finish_date": None,
        "updated_at": "2026-10-28T12:00:00Z",
        **updates,
    }


def client(handler):
    return MALClient(
        httpx.Client(transport=httpx.MockTransport(handler)), "client", "secret", now=lambda: 1000
    )


def identity():
    return Identity(1, Provider.MAL, 202, 1, verified=True)


def test_plain_pkce_and_exact_form_exchange_rotation():
    verifier = "a" * 64
    query = parse_qs(
        urlsplit(
            authorize_url("client", "https://example.invalid/callback", verifier, "state")
        ).query
    )
    assert query["code_challenge"] == [verifier] and query["code_challenge_method"] == ["plain"]
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url == "https://myanimelist.net/v1/oauth2/token"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "expires_in": 3600,
                "access_token": "synthetic-access",
                "refresh_token": "synthetic-refresh",
            },
        )

    c = client(handler)
    bundle = c.exchange("code", verifier, "https://example.invalid/callback")
    assert bundle.expires_at == 4600
    assert parse_qs(requests[0].content.decode())["code_verifier"] == [verifier]
    assert c.refresh(bundle).refresh_token == "synthetic-refresh"
    assert parse_qs(requests[1].content.decode())["grant_type"] == ["refresh_token"]
    assert "synthetic-access" not in repr(bundle)


def test_verified_viewer_complete_pagination_and_write_spelling():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/users/@me":
            return httpx.Response(200, json={"id": 202, "name": "Synthetic member"})
        if request.method == "PATCH":
            form = parse_qs(request.content.decode())
            assert form == {"num_watched_episodes": ["3"]}
            return httpx.Response(200, json=status(num_episodes_watched=3))
        if request.url.path.endswith("animelist"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"node": {"id": 22, "title": "Synthetic anime"}, "list_status": status()}
                    ],
                    "paging": {},
                },
            )
        return httpx.Response(200, json={"id": 22, "my_list_status": status()})

    c = client(handler)
    assert c.viewer("token") == (202, "Synthetic member")
    inventory = c.inventory("token", identity())
    assert inventory.complete and inventory.entries[0].state.progress == 2
    assert c.patch("token", 22, {"progress": 3}).state.progress == 3
    assert requests[-1].method == "PATCH"
    assert all(r.headers.get("authorization") == "Bearer token" for r in requests)


@pytest.mark.parametrize(
    "bad",
    [
        {"num_episodes_watched": None},
        {"score": True},
        {"is_rewatching": "false"},
        {"updated_at": None},
    ],
)
def test_missing_or_bad_required_fields_are_not_zero_defaults(bad):
    with pytest.raises(TrackerHTTPError):
        parse_mal_entry(22, status(**bad))


def test_documentation_example_alias_never_silently_defaults_progress():
    payload = status()
    del payload["num_episodes_watched"]
    payload["num_watched_episodes"] = 2
    with pytest.raises(TrackerHTTPError):
        parse_mal_entry(22, payload)


def test_unsafe_next_and_incomplete_error_fail_closed():
    c = client(
        lambda r: httpx.Response(
            200, json={"data": [], "paging": {"next": "https://evil.invalid/steal"}}
        )
    )
    with pytest.raises(TrackerHTTPError):
        c.inventory("token", identity())

    def rate(_):
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": "limited"})

    with pytest.raises(TrackerHTTPError) as info:
        client(rate).viewer("token")
    assert info.value.retry_after == 60 and info.value.code == "rate_limited"


@pytest.mark.parametrize(
    "patch", [{"started_at": "2020"}, {"score": None}, {"score": 75}, {"comments": "private"}]
)
def test_unqualified_or_unsupported_patch_fields_never_dispatch(patch):
    c = client(lambda r: pytest.fail("unsupported patch must not reach transport"))
    with pytest.raises(TrackerHTTPError):
        c.patch("token", 22, patch)


def al_row(**updates):
    return {
        "id": 1,
        "mediaId": 11,
        "status": "CURRENT",
        "score": 70.0,
        "progress": 2,
        "repeat": 0,
        "startedAt": {"year": 2020, "month": None, "day": None},
        "completedAt": {"year": None, "month": None, "day": None},
        "updatedAt": 1000,
        "media": {
            "id": 11,
            "idMal": 22,
            "type": "ANIME",
            "title": {"userPreferred": "Synthetic anime"},
        },
        **updates,
    }


def test_anilist_authenticated_inventory_exact_mapping_and_minimal_score_raw_patch():
    from omakase.plus.mal import AniListBridgeClient

    requests = []

    def handler(request):
        payload = __import__("json").loads(request.content)
        requests.append(payload)
        query = payload["query"]
        if "Viewer" in query:
            return httpx.Response(
                200, json={"data": {"Viewer": {"id": 101, "name": "Synthetic member"}}}
            )
        if "SaveMediaListEntry" in query:
            assert payload["variables"] == {"id": 1, "mediaId": 11, "scoreRaw": 80}
            return httpx.Response(200, json={"data": {"SaveMediaListEntry": al_row(score=80.0)}})
        return httpx.Response(
            200,
            json={"data": {"Page": {"pageInfo": {"hasNextPage": False}, "mediaList": [al_row()]}}},
        )

    c = AniListBridgeClient(httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.viewer("token") == (101, "Synthetic member")
    inv, mappings = c.inventory("token", Identity(1, Provider.ANILIST, 101, 1, verified=True))
    assert inv.complete and mappings[0].mal_id == 22
    assert inv.entries[0].state.started_at.year == 2020
    assert c.patch("token", 11, {"score": 80}).state.score == 80
    assert "status" not in requests[-1]["variables"]


def test_anilist_graphql_200_error_is_not_success_or_partial_inventory():
    from omakase.plus.mal import AniListBridgeClient

    c = AniListBridgeClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    json={
                        "data": None,
                        "errors": [{"status": 429, "message": "synthetic limited"}],
                    },
                )
            )
        )
    )
    with pytest.raises(TrackerHTTPError) as info:
        c.viewer("token")
    assert info.value.code == "rate_limited" and info.value.retry_after == 60


def test_mal_page_operation_preserves_canonical_read_and_validates_next_origin():
    calls = []

    def handler(request):
        offset = int(request.url.params.get("offset", "0"))
        calls.append(offset)
        return httpx.Response(
            200,
            json={
                "data": [{"node": {"id": 22 + offset}, "list_status": status()}],
                "paging": {"next": "https://api.myanimelist.net/v2/users/@me/animelist?offset=1"}
                if offset == 0
                else {},
            },
        )

    c = client(handler)
    first, _, next_cursor = c.scan_page("token", identity(), 0)
    second, _, end = c.scan_page("token", identity(), next_cursor)
    assert calls == [0, 1] and end is None and first[0].media_id == 22 and second[0].media_id == 23


def test_multi_day_retry_after_is_not_shortened_to_one_day():
    c = client(
        lambda request: httpx.Response(
            429, headers={"Retry-After": "172800"}, json={"error": "limited"}
        )
    )
    with pytest.raises(TrackerHTTPError) as error:
        c.viewer("token")
    assert error.value.retry_after == 172800
