from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tests.plex_curator.test_runtime import _manifest

CURATOR_ROOT = Path(__file__).resolve().parents[2] / "deploy" / "plex-curator"
sys.path.insert(0, str(CURATOR_ROOT))

from curator_runtime import (  # noqa: E402
    CollectionPlan,
    RuntimeContractError,
    _apply_collection_plan,
    _build_collection_plan,
    _fetch_organization,
    _validate_organization,
)
from plex_client import PlexAnimeItem  # noqa: E402


def _organization():
    return {
        "schema_version": "plex-organization-v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "titles": [
            {
                "anilist_id": 42,
                "canonical_title": "Fixture Anime",
                "title_aliases": [],
                "season_number": 1,
                "shelves": ["Ready"],
            }
        ],
    }


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(email="private@example.com"),
        lambda payload: payload["titles"][0].update(shelves=["Unknown Shelf"]),
        lambda payload: payload["titles"][0].update(shelves=["Ready", "Ready"]),
        lambda payload: payload["titles"][0].update(shelves=[{}]),
        lambda payload: payload["titles"][0].update(season_number=True),
        lambda payload: payload["titles"][0].update(anilist_id=True),
        lambda payload: payload["titles"][0].update(title_aliases=["x" * 301]),
        lambda payload: payload["titles"].append(copy.deepcopy(payload["titles"][0])),
        lambda payload: payload.update(titles=[payload["titles"][0]] * 10_001),
    ],
)
def test_organization_contract_rejects_unknown_or_unbounded_data(mutation):
    payload = _organization()
    mutation(payload)
    with pytest.raises(RuntimeContractError):
        _validate_organization(payload)


@pytest.mark.parametrize("offset", [-901, 61])
def test_organization_requires_recent_nonfuture_evidence(offset):
    payload = _organization()
    payload["generated_at"] = (datetime.now(timezone.utc) + timedelta(seconds=offset)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with pytest.raises(RuntimeContractError, match="stale"):
        _validate_organization(payload)


def test_organization_fetch_enforces_byte_limit_before_json_parse(monkeypatch):
    class Response:
        content = b"x" * 4_000_001

        def raise_for_status(self):
            pass

        def json(self):
            raise AssertionError("oversized payload must not be decoded")

    monkeypatch.setattr("curator_runtime.httpx.get", lambda *_args, **_kwargs: Response())
    config = SimpleNamespace(
        runtime=SimpleNamespace(manifest_url="https://private.invalid/plus/api/library-manifest")
    )
    with pytest.raises(RuntimeContractError):
        _fetch_organization(config, "private-token")


def test_organization_fetch_uses_existing_machine_bearer_header(monkeypatch):
    seen = {}

    def fetch(url, **kwargs):
        seen.update(url=url, **kwargs)
        return httpx.Response(200, json=_organization(), request=httpx.Request("GET", url))

    monkeypatch.setattr("curator_runtime.httpx.get", fetch)
    config = SimpleNamespace(
        runtime=SimpleNamespace(manifest_url="https://private.invalid/plus/api/library-manifest")
    )
    payload = _fetch_organization(config, "private-token")
    assert payload["titles"][0]["shelves"] == ["Ready"]
    assert seen["url"] == "https://private.invalid/plus/api/plex-organization"
    assert seen["headers"] == {"Authorization": "Bearer private-token"}
    assert "private-token" not in seen["url"]


class Client:
    def __init__(self, collections):
        self.collections = collections
        self.mutations = []

    def inventory_anime(self, section):
        assert section == 1
        return (PlexAnimeItem("42", "Fixture Anime", (), self.collections),)

    def set_managed_collection(self, **kwargs):
        self.mutations.append(kwargs)


def test_plan_aggregates_seasons_into_ready_and_watching_and_binds_shelves_in_hash():
    manifest = _manifest()
    season = copy.deepcopy(manifest["titles"][0])
    season.update(
        anilist_id=43,
        canonical_title="Fixture Anime Season 2",
        season_number=2,
        title_aliases=["Fixture Anime"],
    )
    manifest["titles"].append(season)
    organization = _organization()
    organization["titles"].append(
        {
            "anilist_id": 43,
            "canonical_title": "Fixture Anime Season 2",
            "title_aliases": ["Fixture Anime"],
            "season_number": 2,
            "shelves": ["Watching"],
        }
    )
    client = Client(("Favorites", "Comedy & Couch Anime"))
    plan = _build_collection_plan(
        client, manifest, 1, {}, organization=_validate_organization(organization)
    )
    assert plan.decisions[0].desired_shelves == ("Ready", "Watching")
    assert CollectionPlan.from_dict(plan.to_dict()) == plan
    changed = copy.deepcopy(organization)
    changed["titles"][1]["shelves"] = []
    other = _build_collection_plan(client, manifest, 1, {}, organization=changed)
    assert other.plan_hash != plan.plan_hash


def test_legacy_plan_preserves_shelves_without_current_organization():
    client = Client(("Favorites", "Comedy & Couch Anime", "Watching"))
    plan = _build_collection_plan(client, _manifest(), 1, {})
    assert plan.decisions[0].desired_shelves == ("Watching",)


def test_plan_applies_stale_genre_and_shelf_removal_without_losing_user_collection():
    manifest = _manifest()
    manifest["titles"][0]["genres"] = ["Fantasy", "Action"]
    client = Client(("Favorites", "Action & Adventure Anime", "Watching"))
    organization = _organization()
    organization["titles"][0]["shelves"] = []
    plan = _build_collection_plan(client, manifest, 1, {}, organization=organization)
    _apply_collection_plan(client, section_key=1, plan=plan)
    assert len(client.mutations) == 1
    mutation = client.mutations[0]
    assert mutation["existing"] == ("Favorites", "Action & Adventure Anime", "Watching")
    assert mutation["desired"] == ("Fantasy Anime",)
    assert mutation["dry_run"] is False


def test_plan_refuses_concurrent_collection_changes_before_any_apply():
    client = Client(("Favorites", "Ready"))
    plan = _build_collection_plan(client, _manifest(), 1, {}, organization=_organization())
    client.collections = ("Favorites", "Private New Collection", "Ready")
    with pytest.raises(RuntimeContractError, match="state changed"):
        _apply_collection_plan(client, section_key=1, plan=plan)
    assert client.mutations == []
