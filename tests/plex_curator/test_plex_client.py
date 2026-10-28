from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

CURATOR_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from plex_client import (  # noqa: E402
    PlexAnimeItem,
    PlexClient,
    PlexLibraryNames,
    PlexLibraryScan,
    PlexSection,
    TopologyPlanError,
    TopologyRemovalProof,
    anime_bucket,
    managed_collection_tags,
    plan_library_topology,
    read_plex_token,
)


def _sections() -> tuple[PlexSection, ...]:
    return (
        PlexSection(1, "Anime", ("/old/anime",)),
        PlexSection(2, "Anime Movies", ("/old/anime-movies",)),
        PlexSection(3, "Real-Debrid", ("/zurg/__all__",)),
        PlexSection(5, "Riven TV", ("/library/symlinks/shows",)),
    )


def _backup_receipt(tmp_path: Path) -> Path:
    artifacts = {}
    for name in (
        "curator-config",
        "overlay-state",
        "plex-database",
        "plex-preferences",
        "section-inventory",
        "systemd-units",
    ):
        artifact = tmp_path / f"{name}.tar"
        artifact.write_bytes(name.encode())
        artifacts[name] = {
            "path": str(artifact.resolve()),
            "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        }
    receipt = tmp_path / "plex-backup.receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "schema_version": "plex-backup-receipt-v1",
                "created_at": "2026-07-15T18:22:11Z",
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )
    return receipt


def test_bucket_rule_is_exactly_one_collection():
    assert anime_bucket(["Action", "Comedy"]) == "Action & Adventure Anime"
    assert anime_bucket(["Comedy", "Slice of Life"]) == "Comedy & Couch Anime"
    assert anime_bucket(["Drama"]) == "Drama & Suspense Anime"
    assert anime_bucket(["Mystery", "Thriller"]) == "Drama & Suspense Anime"
    assert anime_bucket(["Comedy", "Drama"]) == "Drama & Suspense Anime"
    assert (
        anime_bucket(
            ["Drama", "Mecha"],
            identity="Mobile Suit Gundam",
            overrides={"Mobile Suit Gundam": "Action & Adventure Anime"},
        )
        == "Action & Adventure Anime"
    )
    with pytest.raises(TopologyPlanError, match="unresolved"):
        anime_bucket([])
    assert anime_bucket(["Fantasy"]) == "Fantasy Anime"


def test_collection_update_preserves_unmanaged_tags_and_dry_run_is_nonmutating():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    desired = managed_collection_tags(
        ["Favorites", "Comedy & Couch Anime"], "Action & Adventure Anime"
    )

    assert desired == ("Favorites", "Action & Adventure Anime")
    client.set_managed_collection(
        section_key=1,
        rating_key="42",
        existing=["Favorites", "Comedy & Couch Anime"],
        desired="Action & Adventure Anime",
        dry_run=True,
    )
    assert requests == []

    client.set_managed_collection(
        section_key=1,
        rating_key="42",
        existing=["Favorites", "Comedy & Couch Anime"],
        desired="Action & Adventure Anime",
    )
    assert len(requests) == 1
    assert "secret" not in str(requests[0].url)
    request = requests[0]
    assert request.method == "PUT"
    assert request.url.path == "/library/sections/1/all"
    assert request.url.params.multi_items() == [
        ("type", "2"),
        ("id", "42"),
        ("collection.locked", "1"),
        ("collection[].tag.tag-", "Comedy & Couch Anime"),
        ("collection[0].tag.tag", "Action & Adventure Anime"),
    ]
    assert request.content == b""


def test_collection_update_removes_multiple_stale_managed_tags_in_one_plex_parameter():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    client.set_managed_collection(
        section_key=1,
        rating_key="42",
        existing=[
            "Favorites",
            "Comedy & Couch Anime",
            "Drama & Suspense Anime",
        ],
        desired="Action & Adventure Anime",
    )

    assert requests[0].url.params.multi_items()[-2:] == [
        ("collection[].tag.tag-", "Comedy & Couch Anime,Drama & Suspense Anime"),
        ("collection[0].tag.tag", "Action & Adventure Anime"),
    ]
    assert requests[0].content == b""


def test_preferences_token_is_read_without_being_embedded_in_repr(tmp_path: Path):
    preferences = tmp_path / "Preferences.xml"
    preferences.write_text(
        '<Preferences MachineIdentifier="machine" PlexOnlineToken="private-token" />',
        encoding="utf-8",
    )

    assert read_plex_token(preferences) == "private-token"
    assert "private-token" not in repr(PlexClient("http://plex.invalid", "private-token"))


def test_section_and_anime_metadata_inventory_are_parsed_without_mutation():
    requests: list[httpx.Request] = []
    payloads = {
        "/library/sections": """
            <MediaContainer>
              <Directory key="1" title="Anime"><Location path="/anime" /></Directory>
            </MediaContainer>
        """,
        "/library/sections/1/all": """
            <MediaContainer>
              <Directory ratingKey="42" title="Fixture Anime">
                <Genre tag="Action" /><Genre tag="Comedy" />
                <Collection tag="Favorites" />
                <Collection tag="Comedy &amp; Couch Anime" />
              </Directory>
            </MediaContainer>
        """,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=payloads[request.url.path])

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.inventory_sections() == (PlexSection(1, "Anime", ("/anime",)),)
    anime = client.inventory_anime(1)
    assert anime[0].rating_key == "42"
    assert anime[0].genres == ("Action", "Comedy")
    assert anime[0].collections == ("Favorites", "Comedy & Couch Anime")
    assert requests[-1].url.params.multi_items() == []


def test_movie_inventory_uses_the_section_content_type_instead_of_forcing_tv():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            text='<MediaContainer><Video ratingKey="7" title="Ninja Scroll" /></MediaContainer>',
        )

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    movies = client.inventory_anime(2)

    assert movies == (PlexAnimeItem("7", "Ninja Scroll", (), ()),)
    assert requests[0].url.path == "/library/sections/2/all"
    assert requests[0].url.params.multi_items() == []


def test_library_snapshot_collects_season_progress_and_movies_without_mutation():
    requests: list[httpx.Request] = []
    payloads = {
        "/library/sections": """
            <MediaContainer>
              <Directory key="1" title="Anime"><Location path="/anime" /></Directory>
              <Directory key="2" title="Anime Movies"><Location path="/movies" /></Directory>
            </MediaContainer>
        """,
        "/library/sections/1/all": """
            <MediaContainer><Directory ratingKey="42" title="Hell's Paradise" /></MediaContainer>
        """,
        "/library/metadata/42/allLeaves": """
            <MediaContainer>
              <Video ratingKey="101" parentIndex="2" index="1" viewCount="1"
                     lastViewedAt="1787842200" />
              <Video ratingKey="109" parentIndex="2" index="9" viewCount="1"
                     lastViewedAt="1787842500" />
              <Video ratingKey="112" parentIndex="2" index="12" />
            </MediaContainer>
        """,
        "/library/sections/2/all": """
            <MediaContainer><Video ratingKey="77" title="Ninja Scroll" viewCount="1"
              lastViewedAt="1787842600" /></MediaContainer>
        """,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=payloads[request.url.path])

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    snapshot = client.library_snapshot(
        library_names=PlexLibraryNames(),
        generated_at="2026-08-27T15:00:00Z",
    )

    assert snapshot == {
        "schema_version": "plex-library-snapshot-v1",
        "generated_at": "2026-08-27T15:00:00Z",
        "items": [
            {
                "rating_key": "42",
                "section": "Anime",
                "media_type": "show",
                "title": "Hell's Paradise",
                "season_number": 2,
                "episode_total": 12,
                "watched_through": 9,
                "last_viewed_at": "2026-08-27T14:55:00Z",
            },
            {
                "rating_key": "77",
                "section": "Anime Movies",
                "media_type": "movie",
                "title": "Ninja Scroll",
                "season_number": 0,
                "episode_total": 1,
                "watched_through": 1,
                "last_viewed_at": "2026-08-27T14:56:40Z",
            },
        ],
    }
    assert all(request.method == "GET" for request in requests)
    assert all("secret" not in str(request.url) for request in requests)


def test_season_episode_count_uses_plex_parent_index():
    payload = """
        <MediaContainer size="4">
          <Video ratingKey="1" parentIndex="4" index="1" />
          <Video ratingKey="2" parentIndex="4" index="2" />
          <Video ratingKey="3" parentIndex="5" index="1" />
          <Video ratingKey="4" parentIndex="0" index="1" />
        </MediaContainer>
    """
    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
    )

    assert client.season_episode_count("42", 4) == 2
    assert client.season_episode_count("42", 5) == 1


@pytest.mark.parametrize(
    "foreign_path",
    [
        None,
        "/unmanaged/other.mkv",
        "/library/symlinks/omakase/current/Anime/Other/Season 02/Other - S02E02.mkv",
        "/library/symlinks/omakase/current/Anime/Calamity/../Other/Season 02/a.mkv",
    ],
)
def test_snapshot_binds_a_renamed_show_only_to_one_exact_managed_season(foreign_path):
    first = "/library/symlinks/omakase/current/Anime/Calamity/Season 02/Calamity - S02E01.mkv"
    second = foreign_path or first.replace("E01", "E02")
    payloads = {
        "/library/sections": '<MediaContainer><Directory key="1" title="Anime"/><Directory key="2" title="Anime Movies"/></MediaContainer>',
        "/library/sections/1/all": '<MediaContainer><Directory ratingKey="42" title="Bleach"/></MediaContainer>',
        "/library/sections/2/all": "<MediaContainer/>",
        "/library/metadata/42/allLeaves": f'<MediaContainer><Video parentIndex="2" index="1" viewCount="1" lastViewedAt="1787842200"><Media><Part file="{first}"/></Media></Video><Video parentIndex="2" index="2"><Media><Part file="{second}"/></Media></Video></MediaContainer>',
    }
    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=payloads[request.url.path])
        ),
    )
    snapshot = client.library_snapshot(
        library_names=PlexLibraryNames(),
        generated_at="2026-08-27T15:00:00Z",
        overlay_path="/library/symlinks/omakase",
    )
    assert snapshot["items"][0].get("canonical_title") == (
        "Calamity" if foreign_path is None else None
    )
    item = snapshot["items"][0]
    assert {"canonical_title": "Calamity", "season_number": 2} in item["managed_paths"]
    assert item["path_identity_complete"] is (not (foreign_path and "/../" in foreign_path))


def test_series_media_paths_are_exact_sorted_and_deduplicated():
    payload = """
        <MediaContainer size="2">
          <Video ratingKey="1">
            <Media><Part file="/library/current/Anime/Fixture/Season 01/Episode 02.mkv" /></Media>
          </Video>
          <Video ratingKey="2">
            <Media>
              <Part file="/library/current/Anime/Fixture/Season 01/Episode 01.mkv" />
              <Part file="/library/current/Anime/Fixture/Season 01/Episode 01.mkv" />
            </Media>
          </Video>
        </MediaContainer>
    """
    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text=payload)),
    )

    assert client.series_media_paths("42") == (
        "/library/current/Anime/Fixture/Season 01/Episode 01.mkv",
        "/library/current/Anime/Fixture/Season 01/Episode 02.mkv",
    )


@pytest.mark.parametrize("mode", ["partial", "misindexed_special"])
def test_path_aware_snapshot_rejects_incomplete_or_hidden_regular_inventory(mode):
    path = "/library/symlinks/omakase/current/Anime/Calamity/Season 02/Calamity - S02E01.mkv"
    payloads = {
        "/library/sections": '<MediaContainer><Directory key="1" title="Anime"/><Directory key="2" title="Anime Movies"/></MediaContainer>',
        "/library/sections/1/all": '<MediaContainer><Directory ratingKey="42" title="Bleach"/></MediaContainer>',
        "/library/sections/2/all": "<MediaContainer/>",
        "/library/metadata/42/allLeaves": f'<MediaContainer size="1" totalSize="{2 if mode == "partial" else 1}"><Video parentIndex="{2 if mode == "partial" else 0}" index="1"><Media><Part file="{path}"/></Media></Video></MediaContainer>',
    }
    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=payloads[request.url.path])
        ),
    )
    with pytest.raises(TopologyPlanError):
        client.library_snapshot(
            library_names=PlexLibraryNames(),
            generated_at="2026-08-27T15:00:00Z",
            overlay_path="/library/symlinks/omakase",
        )


def test_item_media_paths_are_exact_sorted_and_deduplicated():
    payload = """
        <MediaContainer size="1">
          <Video ratingKey="7">
            <Media>
              <Part file="/library/current/Anime Movies/Fixture/Fixture.mkv" />
              <Part file="/library/current/Anime Movies/Fixture/Fixture.mkv" />
            </Media>
          </Video>
        </MediaContainer>
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=payload)

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.item_media_paths("7") == ("/library/current/Anime Movies/Fixture/Fixture.mkv",)
    assert requests[0].url.path == "/library/metadata/7"


def test_library_mutations_use_the_installed_plex_query_contract():
    requests: list[httpx.Request] = []
    inventory = """
        <MediaContainer>
          <Directory key="1" title="Anime" type="show"
                     agent="tv.plex.agents.series" scanner="Plex TV Series"
                     language="en-US">
            <Location path="/old/anime" />
          </Directory>
          <Directory key="5" title="Riven TV" type="show"
                     agent="tv.plex.agents.series" scanner="Plex TV Series"
                     language="en-US">
            <Location path="/library/symlinks/shows" />
          </Directory>
        </MediaContainer>
    """

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/library/sections":
            return httpx.Response(200, text=inventory)
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    client.update_section_path(1, "/library/symlinks/omakase/current/Anime")
    client.rename_section(5, "TV Shows")
    client.create_section("Movies", "/library/symlinks/movies")
    client.scan_section(1)

    mutations = [
        request
        for request in requests
        if not (request.method == "GET" and request.url.path == "/library/sections")
    ]
    assert [(request.method, request.url.path) for request in mutations] == [
        ("PUT", "/library/sections/1"),
        ("PUT", "/library/sections/5"),
        ("POST", "/library/sections"),
        ("POST", "/library/sections/1/refresh"),
    ]
    update_params = mutations[0].url.params.multi_items()
    assert update_params == [
        ("name", "Anime"),
        ("agent", "tv.plex.agents.series"),
        ("scanner", "Plex TV Series"),
        ("language", "en-US"),
        ("location", "/library/symlinks/omakase/current/Anime"),
    ]
    rename_params = mutations[1].url.params.multi_items()
    assert rename_params[-1] == ("location", "/library/symlinks/shows")
    assert ("name", "TV Shows") in rename_params
    assert mutations[2].url.params.multi_items() == [
        ("name", "Movies"),
        ("type", "movie"),
        ("agent", "tv.plex.agents.movie"),
        ("scanner", "Plex Movie"),
        ("language", "en-US"),
        ("location", "/library/symlinks/movies"),
    ]
    assert (requests[-1].method, requests[-1].url.path) == (
        "POST",
        "/library/sections/1/refresh",
    )
    assert requests[-1].url.params.multi_items() == [("force", "1")]
    assert all("secret" not in str(request.url) for request in requests)


def test_partial_scan_sends_one_encoded_subdirectory_path():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    mutation = client.scan_section(
        1,
        path="/library/symlinks/omakase/current/Anime/Hell’s Paradise Season 2",
    )

    assert mutation.proposed.endswith("Hell’s Paradise Season 2")
    assert requests[0].url.params.multi_items() == [
        ("force", "1"),
        (
            "path",
            "/library/symlinks/omakase/current/Anime/Hell’s Paradise Season 2",
        ),
    ]
    assert "secret" not in str(requests[0].url)


def test_active_library_scans_exposes_only_section_scan_activity():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/activities"
        return httpx.Response(
            200,
            text="""
                <MediaContainer>
                  <Activity type="library.update.section"
                            title="Scanning Anime"
                            subtitle="Hell's Paradise - Season 02"
                            progress="47" />
                  <Activity type="library.update.item.metadata"
                            title="Updating Metadata"
                            subtitle="Episode 1"
                            progress="12" />
                </MediaContainer>
            """,
        )

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )

    assert client.active_library_scans() == (
        PlexLibraryScan(
            title="Scanning Anime",
            subtitle="Hell's Paradise - Season 02",
            progress="47",
        ),
    )


def test_collection_convergence_applies_versioned_override_and_exactly_one_managed_tag():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                text="""
                    <MediaContainer>
                      <Directory ratingKey="42" title="Fixture Anime">
                        <Genre tag="Action" />
                        <Collection tag="Favorites" />
                        <Collection tag="Action &amp; Adventure Anime" />
                      </Directory>
                    </MediaContainer>
                """,
            )
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    mutations = client.converge_collections(
        1,
        overrides={"Fixture Anime": "Comedy & Couch Anime"},
        dry_run=False,
    )

    assert len(mutations) == 1
    assert requests[-1].method == "PUT"
    values = requests[-1].url.params.multi_items()
    assert ("collection[].tag.tag-", "Action & Adventure Anime") in values
    assert ("collection[0].tag.tag", "Comedy & Couch Anime") in values
    assert all(value != "Favorites" for _, value in values)


def test_collection_convergence_uses_rich_identity_instead_of_sparse_plex_genres():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                text="""
                    <MediaContainer>
                      <Directory ratingKey="42" title="My Hero Academia">
                        <Genre tag="Drama" />
                        <Collection tag="Comedy &amp; Couch Anime" />
                      </Directory>
                    </MediaContainer>
                """,
            )
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(handler),
    )
    mutations = client.converge_collections(
        1,
        rich_genres={"My Hero Academia": ("Action", "Adventure", "Comedy")},
        dry_run=False,
    )

    assert [mutation.proposed for mutation in mutations] == ["Action & Adventure Anime"]
    values = requests[-1].url.params.multi_items()
    assert ("collection[].tag.tag-", "Comedy & Couch Anime") in values
    assert ("collection[0].tag.tag", "Action & Adventure Anime") in values


def test_topology_plan_preserves_keys_and_contains_no_delete_media_action():
    plan = plan_library_topology(
        _sections(),
        current_overlay="/library/symlinks/omakase/current",
        riven_movies="/library/symlinks/movies",
    )

    assert plan.path_updates == {
        1: "/library/symlinks/omakase/current/Anime",
        2: "/library/symlinks/omakase/current/Anime Movies",
    }
    assert plan.renames == {5: "TV Shows"}
    assert plan.create_sections == ("Movies",)
    assert plan.deferred_removals == (3,)
    assert all(action.kind != "delete_media" for action in plan.actions)
    assert type(plan).from_dict(plan.to_dict()) == plan

    tampered = plan.to_dict()
    tampered["renames"][0][1] = "Anime"
    with pytest.raises(TopologyPlanError, match="hash"):
        type(plan).from_dict(tampered)


def test_topology_discovers_custom_library_names_without_assuming_section_keys():
    names = PlexLibraryNames(
        anime="Animation",
        anime_movies="Animated Films",
        tv="Series",
        movies="Films",
    )
    sections = (
        PlexSection(11, "Animation", ("/old/anime",)),
        PlexSection(17, "Animated Films", ("/old/anime-movies",)),
        PlexSection(23, "Series", ("/library/symlinks/shows",)),
        PlexSection(29, "Films", ("/library/symlinks/movies",)),
    )

    plan = plan_library_topology(
        sections,
        current_overlay="/library/symlinks/omakase/current",
        library_names=names,
    )

    assert plan.path_updates == {
        11: "/library/symlinks/omakase/current/Anime",
        17: "/library/symlinks/omakase/current/Anime Movies",
    }
    assert plan.renames == {}
    assert plan.create_sections == ()


@pytest.mark.parametrize(
    "sections",
    [
        _sections()[1:],
        tuple(
            PlexSection(item.key, "Wrong" if item.key == 2 else item.title, item.paths)
            for item in _sections()
        ),
    ],
)
def test_topology_refuses_missing_or_mismatched_immutable_sections(sections):
    with pytest.raises(TopologyPlanError):
        plan_library_topology(
            sections,
            current_overlay="/library/symlinks/omakase/current",
            riven_movies="/library/symlinks/movies",
        )


def test_topology_mutation_requires_backup_and_all_readable_replacement_paths(tmp_path: Path):
    client = PlexClient(
        "http://plex.invalid",
        token="secret",
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    )
    plan = plan_library_topology(
        _sections(),
        current_overlay="/library/symlinks/omakase/current",
        riven_movies="/library/symlinks/movies",
    )
    backup = tmp_path / "missing-backup.receipt.json"

    with pytest.raises(TopologyPlanError, match="backup"):
        client.apply_topology(plan, backup_receipt=backup, readable_paths=(), dry_run=True)

    backup = _backup_receipt(tmp_path)
    with pytest.raises(TopologyPlanError, match="readable"):
        client.apply_topology(
            plan,
            backup_receipt=backup,
            readable_paths=("/library/symlinks/omakase/current/Anime",),
            dry_run=True,
        )


def test_real_debrid_removal_requires_stable_counts_playback_matrix_and_invincible():
    now = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
    counts = {"Anime": 10, "Anime Movies": 2, "TV Shows": 96, "Movies": 8}
    valid = TopologyRemovalProof(
        plan_hash="a" * 64,
        scans_succeeded=True,
        first_observed_at=now,
        first_counts=counts,
        second_observed_at=now + timedelta(minutes=5),
        second_counts=counts,
        playback_http_status=206,
        playback_bytes=1024,
        unmatched_required_media=0,
        invincible_tv_seasons=(1, 2, 3, 4),
        invincible_anime_matches=0,
    )
    valid.validate()

    with pytest.raises(TopologyPlanError, match="Invincible"):
        TopologyRemovalProof(**{**valid.to_dict(), "invincible_anime_matches": 1}).validate()

    with pytest.raises(TopologyPlanError, match="five minutes"):
        TopologyRemovalProof(
            **{**valid.to_dict(), "second_observed_at": now + timedelta(minutes=4, seconds=59)}
        ).validate()


@pytest.mark.parametrize(
    ("genres", "expected"),
    [
        (["Action", "Fantasy", "Comedy"], "Fantasy Anime"),
        (["Action", "Mecha", "Drama"], "Sci-Fi & Mecha Anime"),
        (["Fantasy", "Sci-Fi", "Adventure"], "Sci-Fi & Mecha Anime"),
        (["Drama", "Comedy"], "Drama & Suspense Anime"),
    ],
)
def test_genre_precedence_is_deterministic_and_specific(genres, expected):
    assert anime_bucket(genres) == expected
    assert anime_bucket(reversed(genres)) == expected
    assert (
        anime_bucket(
            genres, identity="Owner Choice", overrides={"Owner Choice": "Comedy & Couch Anime"}
        )
        == "Comedy & Couch Anime"
    )


def test_animation_metadata_does_not_establish_anime_collection_identity():
    with pytest.raises(TopologyPlanError, match="unresolved"):
        anime_bucket(["Animation"])
    with pytest.raises(TopologyPlanError, match="managed bucket"):
        anime_bucket(["Action"], identity="Fixture", overrides={"Fixture": "Ready"})
    sections = tuple(
        PlexSection(item.key, "Animation" if item.title == "Anime" else item.title, item.paths)
        for item in _sections()
    )
    with pytest.raises(TopologyPlanError, match="anime library is absent"):
        plan_library_topology(sections, current_overlay="/library/symlinks/omakase/current")


def test_managed_tags_preserve_user_order_deduplicate_and_allow_both_shelves():
    actual = managed_collection_tags(
        ["Favorites", "Action & Adventure Anime", "Ready", "Private Shelf", "Favorites"],
        ["Fantasy Anime", "Ready", "Watching", "Ready"],
    )
    assert actual == ("Favorites", "Private Shelf", "Fantasy Anime", "Ready", "Watching")
    assert managed_collection_tags(actual, ["Fantasy Anime", "Ready", "Watching"]) == actual


def test_legacy_genre_update_preserves_shelves_and_legacy_shelf_update_preserves_genre():
    existing = ("Favorites", "Action & Adventure Anime", "Ready")
    assert managed_collection_tags(existing, "Fantasy Anime") == (
        "Favorites",
        "Fantasy Anime",
        "Ready",
    )
    assert managed_collection_tags(existing, "Watching") == (
        "Favorites",
        "Action & Adventure Anime",
        "Watching",
    )
    assert managed_collection_tags(existing, ("Fantasy Anime",)) == ("Favorites", "Fantasy Anime")
    assert managed_collection_tags(existing, ()) == ("Favorites",)


def test_collection_shelf_transition_only_mutates_stale_managed_tags():
    requests = []
    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request),
                httpx.Response(200, text="<MediaContainer />"),
            )[1]
        ),
    )
    client.set_managed_collection(
        section_key=1,
        rating_key="42",
        existing=("Favorites", "Fantasy Anime", "Ready"),
        desired=("Fantasy Anime", "Watching"),
    )
    values = requests[-1].url.params.multi_items()
    assert ("collection[].tag.tag-", "Ready") in values
    assert ("collection[0].tag.tag", "Watching") in values
    assert not any("Fantasy Anime" in value or "Favorites" in value for _, value in values)
    client.set_managed_collection(
        section_key=1,
        rating_key="42",
        existing=("Favorites", "Fantasy Anime", "Watching"),
        desired=("Fantasy Anime",),
    )
    values = requests[-1].url.params.multi_items()
    assert ("collection[].tag.tag-", "Watching") in values
    assert not any(
        key.startswith("collection[") and key != "collection[].tag.tag-" for key, _ in values
    )
    count = len(requests)
    client.set_managed_collection(
        section_key=1,
        rating_key="42",
        existing=("Favorites", "Fantasy Anime"),
        desired=("Fantasy Anime",),
    )
    assert len(requests) == count


def test_multi_tag_mutation_adds_each_missing_tag_once_and_dry_run_sends_nothing():
    requests = []
    client = PlexClient(
        "http://plex.invalid:32400",
        token="secret",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request),
                httpx.Response(200, text="<MediaContainer />"),
            )[1]
        ),
    )
    kwargs = dict(
        section_key=1,
        rating_key="42",
        existing=("Favorites", "Action & Adventure Anime"),
        desired=("Sci-Fi & Mecha Anime", "Ready", "Watching", "Ready"),
    )
    client.set_managed_collection(**kwargs, dry_run=True)
    assert requests == []
    client.set_managed_collection(**kwargs)
    additions = [
        (key, value)
        for key, value in requests[-1].url.params.multi_items()
        if key.startswith("collection[") and key != "collection[].tag.tag-"
    ]
    assert additions == [
        ("collection[0].tag.tag", "Sci-Fi & Mecha Anime"),
        ("collection[1].tag.tag", "Ready"),
        ("collection[2].tag.tag", "Watching"),
    ]
    with pytest.raises(TopologyPlanError, match="ambiguous"):
        client.set_managed_collection(
            section_key=1,
            rating_key="42",
            existing=(),
            desired=("Fantasy Anime", "Action & Adventure Anime"),
        )
    with pytest.raises(TopologyPlanError, match="not managed"):
        client.set_managed_collection(
            section_key=1, rating_key="42", existing=(), desired=("Fantasy Anime", "Unknown Shelf")
        )
    assert len(requests) == 1


def test_convergence_shelves_are_explicit_and_missing_state_preserves_existing_shelf():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<MediaContainer><Directory ratingKey="42" title="Fixture"><Collection tag="Favorites"/><Collection tag="Fantasy Anime"/><Collection tag="Ready"/></Directory></MediaContainer>',
            )
        return httpx.Response(200, text="<MediaContainer />")

    client = PlexClient(
        "http://plex.invalid:32400", token="secret", transport=httpx.MockTransport(handler)
    )
    assert (
        client.converge_collections(
            1, rich_genres={"Fixture": ("Fantasy",)}, shelves={}, dry_run=False
        )
        == ()
    )
    mutations = client.converge_collections(
        1, rich_genres={"Fixture": ("Fantasy",)}, shelves={"Fixture": ()}, dry_run=False
    )
    assert len(mutations) == 1
    assert ("collection[].tag.tag-", "Ready") in requests[-1].url.params.multi_items()
    assert all(value != "Favorites" for _, value in requests[-1].url.params.multi_items())


def test_collection_convergence_does_not_rewrite_equivalent_plex_tag_order():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            text='<MediaContainer><Directory ratingKey="42" title="Fixture"><Collection tag="Ready"/><Collection tag="Fantasy Anime"/><Collection tag="Favorites"/></Directory></MediaContainer>',
        )

    client = PlexClient(
        "http://plex.invalid:32400", token="secret", transport=httpx.MockTransport(handler)
    )
    assert (
        client.converge_collections(
            1, rich_genres={"Fixture": ("Fantasy",)}, shelves={"Fixture": ("Ready",)}, dry_run=False
        )
        == ()
    )
    assert [request.method for request in requests] == ["GET"]
