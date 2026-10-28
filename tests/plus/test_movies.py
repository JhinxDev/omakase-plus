"""Private exact-ID movie contracts, exercised through native HTTP fixtures."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

import httpx
import pytest

from omakase.plus import db as plus_db

IMDB = "tt0084787"
TMDB = 1091
NOW = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)


def backend():
    assert importlib.util.find_spec("omakase.plus.movies") is not None, "Movie backend is missing"
    return importlib.import_module("omakase.plus.movies")


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    plus_db.run_migrations(conn)
    conn.executemany(
        "INSERT INTO users (id, email, password_hash) VALUES (?, ?, ?)",
        [(1, "movie-one@example.invalid", "fixture"), (2, "movie-two@example.invalid", "fixture")],
    )
    conn.commit()
    yield conn
    conn.close()


class NativeMovies:
    def __init__(self):
        self.calls = []
        self.items = []
        self.persist = True
        self.search_rows = [{"id": TMDB, "mediaType": "movie", "title": "The Thing"}]
        self.detail = {
            "id": TMDB,
            "title": "The Thing",
            "releaseDate": "1982-06-25",
            "imdbId": IMDB,
            "externalIds": {"imdbId": IMDB},
            "posterPath": "/poster.jpg",
            "backdropPath": "/backdrop.jpg",
            "overview": "An Antarctic investigation.",
        }
        self.inventory_status = 200
        self.post_status = 200
        self.post_error = None
        self.plex_guid = IMDB
        self.plex_library = "Movies"
        self.plex_section = "6"
        self.selected_language = "eng"
        self.english_selected = True
        self.part_key = "/library/parts/11/file.mkv"
        self.part_file = "/library/symlinks/movies/The Thing (1982)/movie.mkv"
        self.part_size = 1_000_000
        self.probe_status = 206
        self.probe_bytes = 65_536
        self.content_range = "bytes 0-65535/1000000"
        self.probe_error = None
        self.retry_after = "120"
        self.metadata_status = 200
        self.xml_override = None
        self.width = 1920
        self.height = 1080
        self.edition = "Theatrical"

    def movie(self):
        return {
            "id": 7,
            "type": "movie",
            "imdb_id": IMDB,
            "tmdb_id": TMDB,
            "title": "The Thing",
            "year": 1982,
            "state": "Completed",
        }

    @property
    def posts(self):
        return [r for r in self.calls if r.method == "POST"]

    @property
    def probes(self):
        return [r for r in self.calls if r.url.path.startswith("/library/parts/")]

    def video_xml(self):
        root = ElementTree.Element(
            "MediaContainer",
            {
                "librarySectionID": self.plex_section,
                "librarySectionTitle": self.plex_library,
                "size": "1",
                "totalSize": "1",
                "machineIdentifier": "fixture-machine",
            },
        )
        video = ElementTree.SubElement(
            root,
            "Video",
            {
                "ratingKey": "42",
                "key": "/library/metadata/42",
                "type": "movie",
                "title": "The Thing",
                "year": "1982",
                "librarySectionID": self.plex_section,
                "librarySectionTitle": self.plex_library,
                "updatedAt": "1793100000",
                "editionTitle": self.edition,
            },
        )
        ElementTree.SubElement(video, "Guid", {"id": f"imdb://{self.plex_guid}"})
        ElementTree.SubElement(video, "Guid", {"id": f"tmdb://{TMDB}"})
        media = ElementTree.SubElement(
            video,
            "Media",
            {
                "id": "8",
                "width": str(self.width),
                "height": str(self.height),
                "videoResolution": "4k" if self.height == 2160 else "1080",
                "videoCodec": "h264",
            },
        )
        part = ElementTree.SubElement(
            media,
            "Part",
            {
                "id": "11",
                "key": self.part_key,
                "file": self.part_file,
                "size": str(self.part_size),
                "duration": "6000000",
            },
        )
        ElementTree.SubElement(
            part,
            "Stream",
            {
                "id": "10",
                "streamType": "1",
                "codec": "h264",
                "width": str(self.width),
                "height": str(self.height),
            },
        )
        ElementTree.SubElement(
            part,
            "Stream",
            {
                "id": "12",
                "streamType": "2",
                "codec": "dts",
                "channels": "6",
                "languageCode": self.selected_language,
                "selected": "1" if self.english_selected else "0",
                "default": "1",
            },
        )
        return ElementTree.tostring(root)

    def __call__(self, request):
        self.calls.append(request)
        host, path = request.url.host, request.url.path
        assert not any(
            secret in str(request.url) for secret in ("riven-secret", "seerr-secret", "plex-secret")
        )
        if host == "seerr.invalid":
            assert request.headers["X-Api-Key"] == "seerr-secret"
            if path == "/api/v1/search":
                return httpx.Response(200, json={"results": self.search_rows})
            if path == f"/api/v1/movie/{TMDB}":
                return httpx.Response(200, json=self.detail)
        if host == "riven.invalid":
            assert request.headers["X-Api-Key"] == "riven-secret"
            if path == "/api/v1/items" and request.method == "GET":
                assert request.url.params["type"] == "movie"
                assert int(request.url.params["limit"]) <= 100
                return httpx.Response(
                    self.inventory_status,
                    json={"items": self.items},
                    headers={"Retry-After": self.retry_after},
                )
            if path == "/api/v1/items/add" and request.method == "POST":
                assert request.url.params["imdb_ids"] == IMDB
                if self.post_error:
                    raise self.post_error("private backend URL and riven-secret", request=request)
                if self.persist and self.post_status == 200:
                    self.items = [self.movie()]
                return httpx.Response(
                    self.post_status,
                    json={"message": "riven-secret https://signed.invalid/token"},
                    headers={"Retry-After": self.retry_after},
                )
            if path == "/api/v1/items/7":
                return httpx.Response(200, json=self.movie())
        if host == "plex.invalid":
            assert request.headers["X-Plex-Token"] == "plex-secret"
            if path == "/identity":
                return httpx.Response(
                    200, content=b'<MediaContainer machineIdentifier="fixture-machine"/>'
                )
            if path == "/library/sections":
                return httpx.Response(
                    200,
                    content=b'<MediaContainer><Directory key="6" title="Movies" type="movie"/><Directory key="5" title="Anime Movies" type="movie"/></MediaContainer>',
                )
            if path in ("/library/sections/6/all", "/library/metadata/42"):
                return httpx.Response(
                    self.metadata_status,
                    content=self.xml_override or self.video_xml(),
                    headers={"Retry-After": self.retry_after},
                )
            if path == "/library/sections/5/all":
                return httpx.Response(200, content=b'<MediaContainer size="0"/>')
            if path.startswith("/library/parts/"):
                assert request.headers["Range"] == "bytes=0-65535"
                if self.probe_error:
                    raise self.probe_error("plex-secret and private path", request=request)
                return httpx.Response(
                    self.probe_status,
                    content=b"x" * self.probe_bytes,
                    headers={"Content-Range": self.content_range, "Retry-After": self.retry_after},
                )
        raise AssertionError(f"Unexpected fixture request {request.method} {host}{path}")


def service(db, fake, *, now=None, enabled=True, **kwargs):
    module = backend()
    connections = module.MovieConnections(
        enabled=enabled,
        riven_url="http://riven.invalid",
        riven_api_key="riven-secret",
        seerr_url="http://seerr.invalid",
        seerr_api_key="seerr-secret",
        plex_url="http://plex.invalid:32400",
        plex_token="plex-secret",
        plex_movies_section_id=6,
        plex_machine_id="fixture-machine",
    )
    return module.MovieService(
        db, connections, transport=httpx.MockTransport(fake), now=now or (lambda: NOW), **kwargs
    )


class Riven0236Movies(NativeMovies):
    """Safe field projection observed from the pinned native 0.23.6 API."""

    total_override = None

    def movie(self):
        row = super().movie()
        row.update(id="movie_tmdb_1091", type="Movie", tmdb_id="1091")
        row.pop("year")
        row["aired_at"] = "1982-06-25T00:00:00"
        return row

    def __call__(self, request):
        if request.url.host == "riven.invalid" and request.method == "GET":
            if request.url.path == "/api/v1/items":
                self.calls.append(request)
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "items": self.items,
                        "page": 1,
                        "limit": 100,
                        "total_items": len(self.items)
                        if self.total_override is None
                        else self.total_override,
                        "total_pages": 1,
                    },
                )
            if request.url.path == "/api/v1/items/movie_tmdb_1091":
                self.calls.append(request)
                return httpx.Response(200, json=self.movie())
        return super().__call__(request)


@pytest.mark.parametrize("already_present", [False, True])
def test_native_riven_0236_identity_framing_and_submission_deduplication(db, already_present):
    fake = Riven0236Movies()
    if already_present:
        fake.items = [fake.movie()]
    svc = service(db, fake)
    card = run(svc.request(1, imdb_id=IMDB, tmdb_id=TMDB))
    assert card["code"] == "verifying"
    assert (
        db.execute(
            "SELECT backend_id FROM movie_requests WHERE user_id=1 AND imdb_id=?", (IMDB,)
        ).fetchone()[0]
        == "movie_tmdb_1091"
    )
    assert len(fake.posts) == (0 if already_present else 1)
    run(svc.request(1, imdb_id=IMDB, tmdb_id=TMDB))
    assert len(fake.posts) == (0 if already_present else 1)


@pytest.mark.parametrize("total", [2, 101, -1, False])
def test_native_riven_total_items_must_prove_a_complete_inventory(db, total):
    fake = Riven0236Movies()
    fake.items = [fake.movie()]
    fake.total_override = total
    card = run(service(db, fake).request(1, imdb_id=IMDB, tmdb_id=TMDB))
    assert card["code"] == "inventory_incomplete"
    assert fake.posts == []


def test_native_plex_external_imdb_guid_is_not_a_primary_guid_filter(db):
    fake = NativeMovies()

    def route(request):
        if request.url.host == "plex.invalid" and request.url.path.endswith("/all"):
            if "guid" in request.url.params:
                # Current Plex agents use plex:// as the primary guid. Their
                # external IMDb Guid is present only in the bounded inventory.
                return httpx.Response(200, content=b'<MediaContainer size="0" totalSize="0"/>')
        return fake(request)

    card = run(service(db, route).verify(1, IMDB))
    assert card["state"] == "ready"
    assert len(fake.probes) == 1
    assert fake.probes[0].headers["Range"] == "bytes=0-65535"
    assert fake.posts == []


def test_exact_inventory_match_still_requires_matching_direct_metadata(db):
    fake = NativeMovies()

    def route(request):
        if request.url.host == "plex.invalid" and request.url.path == "/library/metadata/42":
            body = fake.video_xml().replace(b"imdb://tt0084787", b"imdb://tt9999999")
            return httpx.Response(200, content=body)
        return fake(request)

    card = run(service(db, route).verify(1, IMDB))
    assert card["code"] == "plex_identity_mismatch"
    assert fake.probes == []


def test_duplicate_external_imdb_inventory_matches_never_probe(db):
    fake = NativeMovies()

    def route(request):
        if request.url.host == "plex.invalid" and request.url.path == "/library/sections/6/all":
            body = fake.video_xml()
            video = ElementTree.fromstring(body).find("Video")
            duplicate = ElementTree.fromstring(body)
            duplicate.append(video)
            return httpx.Response(200, content=ElementTree.tostring(duplicate))
        return fake(request)

    card = run(service(db, route).verify(1, IMDB))
    assert card["code"] == "plex_identity_mismatch"
    assert fake.probes == []


@pytest.mark.parametrize(
    "query,wire_value",
    [("The Thing", b"The%20Thing"), ("Rock & Roll + ?", b"Rock%20%26%20Roll%20%2B%20%3F")],
)
def test_seerr_search_uses_percent_encoding_without_double_encoding(db, query, wire_value):
    def route(request):
        assert request.url.path == "/api/v1/search"
        assert request.url.params["query"] == query
        assert request.url.query == b"query=" + wire_value + b"&page=1"
        return httpx.Response(200, json={"results": []})

    assert run(service(db, route).search(1, query)) == []


def test_recognized_backend_progress_does_not_exhaust_failed_investigation_budget(db):
    class ProgressMovies(NativeMovies):
        state = "Indexed"

        def movie(self):
            return {**super().movie(), "state": self.state}

    fake = ProgressMovies()
    svc = service(db, fake)
    run(svc.request(1, imdb_id=IMDB, tmdb_id=TMDB))
    for state, code in [("Indexed", "searching"), ("Downloading", "acquiring")]:
        fake.state = state
        for _ in range(5):
            assert run(svc.refresh(1, IMDB))["code"] == code
    fake.state = "Completed"
    assert run(svc.refresh(1, IMDB))["code"] == "verifying"
    assert db.execute("SELECT investigations FROM movie_requests").fetchone()[0] == 0
    assert len(fake.posts) == 1


def test_partial_plex_listing_does_not_erase_verified_audio_or_read_more_media(db):
    fake = NativeMovies()

    def route(request):
        result = fake(request)
        if request.url.host == "plex.invalid" and request.url.path == "/library/sections/6/all":
            root = ElementTree.fromstring(result.content)
            for part in root.findall("./Video/Media/Part"):
                for stream in list(part):
                    part.remove(stream)
            return httpx.Response(200, content=ElementTree.tostring(root))
        return result

    svc = service(db, route)
    assert run(svc.verify(1, IMDB))["state"] == "ready"
    cards = run(svc.list(1))
    assert cards[0]["state"] == "ready"
    assert cards[0]["selected_audio"] == "English"
    assert len(fake.probes) == 1


def test_rich_plex_metadata_batches_bounded_reads_and_preserves_input_order(db):
    calls = []
    rows = [ElementTree.Element("Video", {"ratingKey": str(key)}) for key in range(1, 46)]

    def route(request):
        ids = request.url.path.rsplit("/", 1)[1].split(",")
        calls.append(ids)
        root = ElementTree.Element("MediaContainer")
        for key in reversed(ids):
            ElementTree.SubElement(root, "Video", {"ratingKey": key})
        return httpx.Response(200, content=ElementTree.tostring(root))

    result = run(service(db, route)._rich_videos(rows))
    assert [row.get("ratingKey") for row in result] == [str(key) for key in range(1, 46)]
    assert sorted(map(len, calls)) == [5, 20, 20]


@pytest.mark.parametrize("returned", [["2"], ["1", "1"], []])
def test_rich_plex_metadata_cannot_substitute_or_omit_requested_rows(db, returned):
    def route(request):
        root = ElementTree.Element("MediaContainer")
        for key in returned:
            ElementTree.SubElement(root, "Video", {"ratingKey": key})
        return httpx.Response(200, content=ElementTree.tostring(root))

    with pytest.raises(backend().MovieError):
        run(service(db, route)._rich_videos([ElementTree.Element("Video", {"ratingKey": "1"})]))


@pytest.mark.parametrize(
    "child,root,expected", [("6", "5", "6"), (None, "5", "5"), (None, None, "6")]
)
def test_rich_plex_section_precedence_never_hides_explicit_response_fields(
    db, child, root, expected
):
    def route(request):
        container = ElementTree.Element("MediaContainer")
        if root is not None:
            container.set("librarySectionID", root)
            container.set("librarySectionTitle", "Anime Movies")
        video = ElementTree.SubElement(container, "Video", {"ratingKey": "1"})
        if child is not None:
            video.set("librarySectionID", child)
        return httpx.Response(200, content=ElementTree.tostring(container))

    original = ElementTree.Element(
        "Video", {"ratingKey": "1", "librarySectionID": "6", "librarySectionTitle": "Movies"}
    )
    result = run(service(db, route)._rich_videos([original]))[0]
    assert result.get("librarySectionID") == expected
    assert result.get("librarySectionTitle") == ("Anime Movies" if root else "Movies")


@pytest.mark.parametrize(
    "plex_year,secondary_guid,expected",
    [
        ("1981", True, "ready"),
        ("1981", False, "plex_identity_mismatch"),
        ("1970", True, "plex_identity_mismatch"),
        ("", True, "plex_identity_mismatch"),
    ],
)
def test_release_year_variation_requires_both_exact_catalog_identities(
    db, plex_year, secondary_guid, expected
):
    fake = NativeMovies()

    def route(request):
        response = fake(request)
        if request.url.host == "plex.invalid" and request.url.path == "/library/metadata/42":
            root = ElementTree.fromstring(response.content)
            video = root.find("Video")
            video.set("year", plex_year)
            if not secondary_guid:
                for guid in list(video.findall("Guid")):
                    if guid.get("id", "").startswith("tmdb://"):
                        video.remove(guid)
            return httpx.Response(200, content=ElementTree.tostring(root))
        return response

    card = run(service(db, route).verify(1, IMDB))
    assert card["code"] == expected
    assert len(fake.probes) == (1 if expected == "ready" else 0)


def test_known_plex_vc1_video_can_be_verified_without_claiming_direct_play(db):
    fake = NativeMovies()

    def route(request):
        response = fake(request)
        if request.url.host == "plex.invalid" and request.url.path == "/library/metadata/42":
            return httpx.Response(
                200, content=response.content.replace(b'videoCodec="h264"', b'videoCodec="vc1"')
            )
        return response

    assert run(service(db, route).verify(1, IMDB))["state"] == "ready"
    assert len(fake.probes) == 1


def test_exact_imdb_lookup_uses_seerrs_external_id_provider(db):
    fake = NativeMovies()

    def route(request):
        if request.url.host == "seerr.invalid" and request.url.path == "/api/v1/search":
            if request.url.params["query"] != "imdb:" + IMDB:
                return httpx.Response(200, json={"results": []})
        return fake(request)

    svc = service(db, route)
    assert run(svc.search(1, IMDB))[0]["imdb_id"] == IMDB
    assert run(svc.verify(2, IMDB))["state"] == "ready"
    assert fake.posts == []


def test_search_cache_stays_out_of_collection_but_exact_detail_still_opens(db):
    fake = NativeMovies()

    def route(request):
        if request.url.host == "plex.invalid" and request.url.path.endswith("/all"):
            return httpx.Response(200, content=b'<MediaContainer size="0" totalSize="0"/>')
        return fake(request)

    svc = service(db, route)
    assert run(svc.search(1, "The Thing"))[0]["imdb_id"] == IMDB
    assert run(svc.list(1)) == []
    before = len(fake.calls)
    assert run(svc.detail(1, IMDB))["title"] == "The Thing"
    assert len(fake.calls) == before
    assert fake.probes == fake.posts == []


@pytest.mark.parametrize("identities", [("bad",), ("bad", "tt1234567")])
def test_malformed_plex_identity_does_not_break_valid_collection(db, identities):
    fake = NativeMovies()
    root = ElementTree.fromstring(fake.video_xml())
    root.set("size", "2")
    root.set("totalSize", "2")
    bad = ElementTree.SubElement(root, "Video", {"ratingKey": "99", "title": "Invalid"})
    for identity in identities:
        ElementTree.SubElement(bad, "Guid", {"id": "imdb://" + identity})

    def route(request):
        if request.url.host == "plex.invalid" and request.url.path == "/library/sections/6/all":
            return httpx.Response(200, content=ElementTree.tostring(root))
        return fake(request)

    cards = run(service(db, route).list(1))
    assert [card["imdb_id"] for card in cards] == [IMDB]
    assert fake.probes == fake.posts == []


def run(awaitable):
    return asyncio.run(awaitable)


def request_movie(svc, user_id=1, **kwargs):
    return run(svc.request(user_id, imdb_id=IMDB, tmdb_id=TMDB, **kwargs))


def test_connections_disabled_and_secret_repr(db):
    module = backend()
    assert module.MovieConnections().enabled is False
    fake = NativeMovies()
    svc = service(db, fake, enabled=False)
    assert "secret" not in repr(svc.connections)
    with pytest.raises(module.MovieError, match="disabled"):
        run(svc.search(1, "The Thing"))
    assert fake.calls == []


@pytest.mark.parametrize(
    "imdb,tmdb",
    [("tt1", TMDB), ("https://imdb.invalid", TMDB), (IMDB, True), (IMDB, 0), (IMDB + "/../", TMDB)],
)
def test_rejects_bad_ids_before_http_or_storage(db, imdb, tmdb):
    fake = NativeMovies()
    svc = service(db, fake)
    with pytest.raises(ValueError):
        run(svc.request(1, imdb_id=imdb, tmdb_id=tmdb))
    assert fake.calls == []
    assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 0


def test_search_resolves_details_and_whitelists_cards(db):
    fake = NativeMovies()
    fake.search_rows += [{"id": 99, "mediaType": "tv"}, fake.search_rows[0]]
    fake.detail.update({"mediaUrl": "http://private.invalid/secret", "apiKey": "seerr-secret"})
    cards = run(service(db, fake).search(1, "The Thing"))
    assert len(cards) == 1
    card = cards[0]
    assert (card["imdb_id"], card["tmdb_id"], card["title"], card["year"]) == (
        IMDB,
        TMDB,
        "The Thing",
        1982,
    )
    assert card["poster_url"] == "https://image.tmdb.org/t/p/w342/poster.jpg"
    assert card["backdrop_url"] == "https://image.tmdb.org/t/p/w1280/backdrop.jpg"
    assert card["state"] != "ready"
    assert card["plex_url"] is None
    assert not any(word in json.dumps(cards) for word in ("secret", "private.invalid", "apiKey"))
    assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 0


@pytest.mark.parametrize(
    "mutation", ["wrong_imdb", "conflicting_imdb", "wrong_tmdb", "missing_imdb"]
)
def test_mapping_must_agree_before_mutation(db, mutation):
    fake = NativeMovies()
    if mutation == "wrong_imdb":
        fake.detail["imdbId"] = fake.detail["externalIds"]["imdbId"] = "tt0000001"
    elif mutation == "conflicting_imdb":
        fake.detail["externalIds"]["imdbId"] = "tt0000001"
    elif mutation == "wrong_tmdb":
        fake.detail["id"] = TMDB + 1
    else:
        fake.detail.pop("imdbId")
        fake.detail["externalIds"] = {}
    svc = service(db, fake)
    with pytest.raises(ValueError):
        request_movie(svc)
    assert fake.posts == []
    assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 0


def test_request_acknowledgement_is_not_ready_and_is_idempotent_across_users(db):
    fake = NativeMovies()
    svc = service(db, fake)
    first = request_movie(svc)
    second = request_movie(svc)
    other = request_movie(svc, user_id=2)
    assert all(card["state"] != "ready" for card in (first, second, other))
    assert len(fake.posts) == 1
    assert db.execute("SELECT count(*) FROM movie_requests").fetchone()[0] == 2
    assert fake.probes == []
    assert db.execute("SELECT count(*) FROM anilist_plannings").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM media_monitors").fetchone()[0] == 0


def test_existing_riven_movie_does_not_submit(db):
    fake = NativeMovies()
    fake.items = [fake.movie()]
    card = request_movie(service(db, fake))
    assert card["state"] == "verifying"
    assert fake.posts == []


def test_acknowledgement_without_persistence_is_deferred_without_retry(db):
    fake = NativeMovies()
    fake.persist = False
    svc = service(db, fake)
    card = request_movie(svc)
    assert card["state"] == "deferred"
    assert card["code"] == "acknowledgement_unconfirmed"
    request_movie(svc, user_id=2)
    request_movie(svc)
    assert len(fake.posts) == 1


@pytest.mark.parametrize(
    "status,code,state",
    [
        (429, "rate_limited", "deferred"),
        (451, "provider_restricted", "unavailable"),
        (503, "backend_unavailable", "deferred"),
        (403, "connection_rejected", "deferred"),
    ],
)
def test_native_post_failures_have_safe_finite_results(db, status, code, state):
    fake = NativeMovies()
    fake.post_status = status
    card = request_movie(service(db, fake))
    assert card["state"] == state
    assert card["code"] == code
    assert "secret" not in json.dumps(card)
    if status == 429:
        assert card["retry_after"] == "120"
    request_movie(service(db, fake), user_id=2)
    assert len(fake.posts) == (2 if status == 403 else 1)


def test_uncertain_write_timeout_is_never_automatically_resubmitted(db):
    fake = NativeMovies()
    fake.post_error = httpx.ReadTimeout
    svc = service(db, fake)
    card = request_movie(svc)
    assert card["state"] == "deferred"
    assert card["code"] == "transport_timeout"
    request_movie(svc, user_id=2)
    assert len(fake.posts) == 1
    assert "secret" not in json.dumps(card)


def test_ready_requires_current_exact_movie_english_and_byte_evidence(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["state"] == "ready"
    assert card["http_status"] == 206
    assert card["bytes_read"] == 65536
    assert card["selected_audio"] == "English"
    assert card["resolution"] == "1080p"
    assert card["verified_at"] == "2026-10-27T15:00:00Z"
    assert card["plex_key"] == "42"
    assert card["plex_url"].startswith("https://app.plex.tv/desktop/")
    assert "plex-secret" not in json.dumps(card)
    assert "symlinks" not in json.dumps(card)
    run(svc.verify(1, IMDB))
    assert len(fake.probes) == 1
    assert len(run(svc.list(1, view="ready"))) == 1
    assert len(fake.probes) == 1


@pytest.mark.parametrize(
    "status,size,content_range,code",
    [
        (200, 65536, "", "range_not_supported"),
        (404, 0, "", "file_unavailable"),
        (451, 0, "", "provider_restricted"),
        (206, 65000, "bytes 0-65535/1000000", "incomplete_range"),
        (206, 65537, "bytes 0-65535/1000000", "incomplete_range"),
        (206, 65536, "bytes 1-65536/1000000", "invalid_content_range"),
    ],
)
def test_bad_byte_proofs_never_become_ready(db, status, size, content_range, code):
    fake = NativeMovies()
    fake.probe_status, fake.probe_bytes, fake.content_range = status, size, content_range
    svc = service(db, fake)
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["state"] != "ready"
    assert card["code"] == code
    assert card["plex_url"] is None


def test_probe_timeout_is_safe_and_not_ready(db):
    fake = NativeMovies()
    fake.probe_error = httpx.ReadTimeout
    svc = service(db, fake)
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["state"] == "deferred"
    assert card["code"] == "transport_timeout"
    assert "secret" not in json.dumps(card)


@pytest.mark.parametrize(
    "change,code",
    [
        ("imdb", "plex_not_found"),
        ("library", "plex_library_mismatch"),
        ("audio", "english_audio_not_selected"),
        ("no_selection", "english_audio_not_selected"),
        ("edition", "edition_mismatch"),
    ],
)
def test_wrong_identity_library_audio_or_edition_does_not_probe(db, change, code):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc, edition="Theatrical")
    if change == "imdb":
        fake.plex_guid = "tt0000001"
    elif change == "library":
        fake.plex_library = "Anime Movies"
    elif change == "audio":
        fake.selected_language = "spa"
    elif change == "no_selection":
        fake.english_selected = False
    else:
        fake.edition = "Director's Cut"
    card = run(svc.verify(1, IMDB))
    assert card["state"] != "ready"
    assert card["code"] == code
    assert fake.probes == []


def test_actual_file_fingerprint_invalidates_verification(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    assert run(svc.verify(1, IMDB))["state"] == "ready"
    fake.part_file = "/replacement/file.mkv"
    fake.part_size += 10
    fake.probe_status = 404
    cards = run(svc.list(1, view="all"))
    assert all(card["state"] != "ready" for card in cards)
    assert len(fake.probes) == 1
    assert run(svc.verify(1, IMDB))["code"] == "file_unavailable"
    assert len(fake.probes) == 2


def test_user_verification_cache_cannot_be_reused_by_another_user(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    run(svc.verify(1, IMDB))
    request_movie(svc, user_id=2)
    cards = run(svc.list(2, view="requests"))
    assert cards[0]["state"] != "ready"
    run(svc.verify(2, IMDB))
    assert len(fake.probes) == 2


def test_expired_verification_is_not_returned_ready(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    run(svc.verify(1, IMDB))
    later = service(db, fake, now=lambda: NOW + timedelta(hours=1))
    card = run(later.list(1, view="requests"))[0]
    assert card["state"] != "ready"
    assert card["code"] == "verification_expired"
    assert card["last_checked_at"] == NOW.isoformat().replace("+00:00", "Z")
    assert card["plex_url"] is None
    assert len(fake.probes) == 1  # Viewing history does not start a new byte check.
    run(later.verify(1, IMDB))
    assert len(fake.probes) == 2


@pytest.mark.parametrize(
    "key",
    [
        "https://attacker.invalid/token",
        "//attacker.invalid/file",
        "/library/parts/../secret",
        "/library/parts/11/file?X-Plex-Token=other",
    ],
)
def test_plex_part_paths_cannot_be_credential_proxies(db, key):
    fake = NativeMovies()
    fake.part_key = key
    svc = service(db, fake)
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["state"] != "ready"
    assert card["code"] == "invalid_plex_metadata"
    assert fake.probes == []


def test_restriction_and_retry_after_remain_effective_even_when_forced(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    fake.probe_status = 451
    run(svc.verify(1, IMDB))
    run(svc.verify(1, IMDB, force=True))
    assert len(fake.probes) == 1
    fake.part_size += 1
    fake.probe_status = 429
    card = run(svc.verify(1, IMDB))
    assert card["retry_after"] == "120"
    run(svc.verify(1, IMDB, force=True))
    assert len(fake.probes) == 2


def test_refresh_has_a_finite_investigation_budget(db):
    fake = NativeMovies()
    fake.persist = False
    svc = service(db, fake, max_investigations=2)
    request_movie(svc)
    run(svc.refresh(1, IMDB))
    run(svc.refresh(1, IMDB))
    calls = len(fake.calls)
    card = run(svc.refresh(1, IMDB))
    assert card["state"] == "unavailable"
    assert card["code"] == "investigation_exhausted"
    assert len(fake.calls) == calls
    assert len(fake.posts) == 1


def test_unknown_user_is_rejected_before_upstream_access(db):
    fake = NativeMovies()
    svc = service(db, fake)
    with pytest.raises(ValueError):
        run(svc.search(999, "The Thing"))
    assert fake.calls == []


def test_bad_search_shapes_or_external_artwork_are_not_forwarded(db):
    fake = NativeMovies()
    fake.detail["posterPath"] = "//attacker.invalid/token"
    fake.detail["backdropPath"] = "/image.jpg?token=seerr-secret"
    cards = run(service(db, fake).search(1, "The Thing"))
    assert cards[0]["poster_url"] is None
    assert cards[0]["backdrop_url"] is None
    assert "secret" not in json.dumps(cards)


def test_submission_guards_are_unique_per_backend_not_global_across_backends(db):
    from dataclasses import replace

    fake = NativeMovies()
    fake.persist = False
    first = service(db, fake)
    request_movie(first)
    module = backend()
    config = replace(first.connections, riven_url="http://second-riven.invalid")

    def route(request):
        if request.url.host == "second-riven.invalid":
            request = httpx.Request(
                request.method,
                str(request.url).replace("second-riven.invalid", "riven.invalid"),
                headers=request.headers,
            )
        return fake(request)

    other = module.MovieService(db, config, transport=httpx.MockTransport(route), now=lambda: NOW)
    request_movie(other, user_id=2)
    assert len(fake.posts) == 2
    assert db.execute("SELECT count(*) FROM movie_submissions").fetchone()[0] == 2
    rows = [dict(row) for row in db.execute("SELECT * FROM movie_submissions")]
    assert all(len(row["backend_scope"]) == 64 for row in rows)
    assert not any(word in json.dumps(rows) for word in ("http", "secret", "invalid"))


def test_native_riven_ids_and_numeric_string_tmdb_ids(db):
    fake = NativeMovies()
    native = fake.movie()
    native.update(id="movie_tmdb_1091", tmdb_id="1091")
    fake.items = [native]

    def route(request):
        if request.url.path == "/api/v1/items/movie_tmdb_1091":
            fake.calls.append(request)
            return httpx.Response(200, json=native)
        return fake(request)

    module = backend()
    svc = service(db, fake)
    svc = module.MovieService(
        db, svc.connections, transport=httpx.MockTransport(route), now=lambda: NOW
    )
    assert request_movie(svc)["state"] == "verifying"
    assert fake.posts == []


def test_permanent_file_restriction_survives_metadata_outage_and_file_replacement(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    fake.probe_status = 451
    assert run(svc.verify(1, IMDB))["code"] == "provider_restricted"
    fake.metadata_status = 503
    run(svc.list(1))
    fake.metadata_status = 200
    fake.part_size += 1
    fake.probe_status = 206
    assert run(svc.verify(1, IMDB))["state"] == "ready"
    fake.part_size -= 1
    assert run(svc.verify(1, IMDB, force=True))["code"] == "provider_restricted"
    assert len(fake.probes) == 2


def test_verification_discovers_a_safe_plex_link_when_machine_is_not_preconfigured(db):
    from dataclasses import replace

    fake = NativeMovies()
    module = backend()
    svc = service(db, fake)
    svc = module.MovieService(
        db,
        replace(svc.connections, plex_machine_id=""),
        transport=httpx.MockTransport(fake),
        now=lambda: NOW,
    )
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["state"] == "ready"
    assert "/fixture-machine/" in card["plex_url"]


@pytest.mark.parametrize("bad_response", ["wrong-id", "duplicate", "oversized", "wrong-type"])
def test_malformed_riven_inventory_never_triggers_acquisition(db, bad_response):
    fake = NativeMovies()
    row = fake.movie()
    if bad_response == "wrong-id":
        row["id"] = "../settings?token=secret"
        fake.items = [row]
    elif bad_response == "duplicate":
        fake.items = [row, row]
    elif bad_response == "oversized":
        fake.items = [row] * 101
    else:
        row["type"] = "show"
        fake.items = [row]
    card = request_movie(service(db, fake))
    assert card["state"] == "deferred"
    assert card["code"] in {"invalid_riven_metadata", "inventory_incomplete"}
    assert fake.posts == []


@pytest.mark.parametrize("status", [429, 451, 503])
def test_inventory_errors_do_not_acquire_or_hide_reason(db, status):
    fake = NativeMovies()
    fake.inventory_status = status
    card = request_movie(service(db, fake))
    assert card["state"] != "ready"
    assert fake.posts == []
    if status == 429:
        assert card["retry_after"] == "120"


def test_foreign_selected_audio_invalidates_ready_receipt(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    assert run(svc.verify(1, IMDB))["state"] == "ready"
    fake.selected_language = "spa"
    card = run(svc.list(1, view="requests"))[0]
    assert card["code"] == "english_audio_not_selected"
    assert card["selected_audio"] == "Spanish"
    assert card["plex_url"] is None
    assert len(fake.probes) == 1


def test_probe_rejects_redirect_without_leaking_token(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    fake.probe_status = 302
    card = run(svc.verify(1, IMDB))
    assert card["code"] == "redirect_rejected"
    assert len(fake.probes) == 1
    assert {r.url.host for r in fake.calls} <= {"riven.invalid", "seerr.invalid", "plex.invalid"}


def test_xml_entities_are_rejected_without_probe(db):
    fake = NativeMovies()
    fake.xml_override = b'<!DOCTYPE foo [<!ENTITY private "secret">]><MediaContainer><Video title="&private;"/></MediaContainer>'
    svc = service(db, fake)
    request_movie(svc)
    assert run(svc.verify(1, IMDB))["code"] == "invalid_plex_metadata"
    assert fake.probes == []


def test_encoded_byte_response_does_not_prove_original_file_range(db):
    import gzip

    fake = NativeMovies()
    svc = service(db, fake)

    def route(request):
        if request.url.path.startswith("/library/parts/"):
            fake.calls.append(request)
            return httpx.Response(
                206,
                content=gzip.compress(b"x" * 65536),
                headers={"Content-Encoding": "gzip", "Content-Range": fake.content_range},
            )
        return fake(request)

    module = backend()
    svc = module.MovieService(
        db, svc.connections, transport=httpx.MockTransport(route), now=lambda: NOW
    )
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["state"] != "ready"


def test_long_valid_retry_after_is_honoured_even_when_forced(db):
    fake = NativeMovies()
    fake.probe_status = 429
    fake.retry_after = "1209600"
    svc = service(db, fake)
    request_movie(svc)
    card = run(svc.verify(1, IMDB))
    assert card["retry_after"] == "1209600"
    assert card["retry_at"] == "2026-11-10T15:00:00Z"
    run(svc.verify(1, IMDB, force=True))
    assert len(fake.probes) == 1


@pytest.mark.parametrize("bad_id", [None, True, {}, []])
def test_non_scalar_riven_identity_is_rejected_before_submission(db, bad_id):
    fake = NativeMovies()
    row = fake.movie()
    row["id"] = bad_id
    fake.items = [row]
    card = request_movie(service(db, fake))
    assert card["code"] == "invalid_riven_metadata"
    assert not any(
        "/items/None" in str(call.url) or "/items/True" in str(call.url) for call in fake.calls
    )
    assert fake.posts == []


def test_utf16_xml_entities_are_rejected_before_parsing(db):
    fake = NativeMovies()
    fake.xml_override = '<?xml version="1.0" encoding="utf-16"?><!DOCTYPE foo [<!ENTITY private "secret">]><MediaContainer><Video title="&private;"/></MediaContainer>'.encode(
        "utf-16"
    )
    svc = service(db, fake)
    request_movie(svc)
    assert run(svc.verify(1, IMDB))["code"] == "invalid_plex_metadata"
    assert fake.probes == []


def test_total_probe_deadline_cancels_a_slow_stream(db, monkeypatch):
    module = backend()
    monkeypatch.setattr(module, "_OPERATION_TIMEOUT", 0.001, raising=False)
    fake = NativeMovies()

    class SlowFile(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(0.05)
            yield b"x" * 65536

    def route(request):
        if request.url.path.startswith("/library/parts/"):
            fake.calls.append(request)
            return httpx.Response(
                206, stream=SlowFile(), headers={"Content-Range": fake.content_range}
            )
        return fake(request)

    svc = service(db, fake)
    svc = module.MovieService(
        db, svc.connections, transport=httpx.MockTransport(route), now=lambda: NOW
    )
    request_movie(svc)
    assert run(svc.verify(1, IMDB))["code"] == "transport_timeout"


def test_list_hydrates_at_most_twelve_metadata_items_at_concurrency_three_and_caches_failures(db):
    fake = NativeMovies()
    fake.items = [
        {
            "id": f"movie_tmdb_{index}",
            "type": "movie",
            "imdb_id": f"tt{1_000_000 + index}",
            "tmdb_id": str(index),
            "title": f"Movie {index}",
            "state": "Completed",
        }
        for index in range(2001, 2016)
    ]
    active, peak, details = 0, 0, []

    async def route(request):
        nonlocal active, peak
        if request.url.host == "seerr.invalid" and request.url.path.startswith("/api/v1/movie/"):
            index = int(request.url.path.rsplit("/", 1)[1])
            details.append(index)
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0)
            active -= 1
            if index == 2004:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "id": index,
                    "imdbId": f"tt{1_000_000 + index}",
                    "title": f"Movie {index}",
                    "releaseDate": "1982-06-25",
                    "posterPath": "/poster.jpg",
                    "backdropPath": "/backdrop.jpg",
                },
            )
        if request.url.path in {"/library/sections/6/all", "/library/sections/5/all"}:
            fake.calls.append(request)
            return httpx.Response(200, content=b'<MediaContainer size="0"/>')
        return fake(request)

    module = backend()
    svc = service(db, fake)
    svc = module.MovieService(
        db, svc.connections, transport=httpx.MockTransport(route), now=lambda: NOW
    )
    cards = run(svc.list(1))
    assert len(details) == 12
    assert peak == 3
    assert len(cards) == 15
    assert sum(card["poster_url"] is not None for card in cards) == 11
    assert all(card["state"] != "ready" for card in cards)
    cards = run(svc.list(1))
    assert len(details) == 15
    assert details.count(2004) == 1
    assert sum(card["poster_url"] is not None for card in cards) == 14
    assert fake.probes == []
    assert fake.posts == []


def test_sqlite_refuses_ready_receipt_without_exact_byte_proof(db):
    fake = NativeMovies()
    svc = service(db, fake)
    request_movie(svc)
    run(svc.verify(1, IMDB))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE movie_verifications SET http_status=200 WHERE user_id=1")
    db.rollback()


@pytest.mark.parametrize("denial", [401, 403])
def test_explicit_request_recovers_after_definitive_auth_rejection_and_fresh_inventory_proof(
    db, denial
):
    fake = NativeMovies()
    fake.post_status = denial
    svc = service(db, fake)
    assert request_movie(svc)["code"] == "connection_rejected"
    assert len(fake.posts) == 1
    fake.inventory_status = 401
    fake.post_status = 200
    assert request_movie(svc)["code"] == "connection_rejected"
    assert len(fake.posts) == 1
    fake.inventory_status = 200
    assert request_movie(svc)["state"] == "verifying"
    assert len(fake.posts) == 2
    request_movie(svc, user_id=2)
    assert len(fake.posts) == 2
    stored = json.dumps([dict(row) for row in db.execute("SELECT * FROM movie_submissions")])
    assert not any(word in stored for word in ("secret", "token", "api_key", "http"))


def test_acknowledgement_followed_by_auth_read_failure_stays_protected(db):
    fake = NativeMovies()
    fake.persist = False
    phase = {"acknowledged": False, "read_denied": True}

    def route(request):
        if request.url.path == "/api/v1/items" and phase["acknowledged"] and phase["read_denied"]:
            fake.calls.append(request)
            return httpx.Response(403)
        response = fake(request)
        if request.method == "POST":
            phase["acknowledged"] = True
        return response

    module = backend()
    svc = service(db, fake)
    svc = module.MovieService(
        db, svc.connections, transport=httpx.MockTransport(route), now=lambda: NOW
    )
    assert request_movie(svc)["code"] == "connection_rejected"
    phase["read_denied"] = False
    card = request_movie(svc, user_id=2)
    assert card["code"] == "acknowledgement_unconfirmed"
    assert len(fake.posts) == 1


def test_refresh_does_not_resubmit_a_definitive_auth_rejection(db):
    fake = NativeMovies()
    fake.post_status = 403
    svc = service(db, fake)
    request_movie(svc)
    fake.post_status = 200
    run(svc.refresh(1, IMDB))
    assert len(fake.posts) == 1


def test_acknowledgement_preserves_persistence_read_retry_after(db):
    fake = NativeMovies()
    fake.persist = False
    phase = {"acknowledged": False}

    def route(request):
        if request.url.path == "/api/v1/items" and phase["acknowledged"]:
            fake.calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "120"})
        response = fake(request)
        if request.method == "POST":
            phase["acknowledged"] = True
        return response

    module = backend()
    svc = service(db, fake)
    svc = module.MovieService(
        db, svc.connections, transport=httpx.MockTransport(route), now=lambda: NOW
    )
    assert request_movie(svc)["retry_after"] == "120"
    riven_calls = len([call for call in fake.calls if call.url.host == "riven.invalid"])
    card = request_movie(svc, user_id=2)
    assert card["retry_after"] == "120"
    assert len([call for call in fake.calls if call.url.host == "riven.invalid"]) == riven_calls
    assert len(fake.posts) == 1
