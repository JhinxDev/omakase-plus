from __future__ import annotations

import json

import pytest

from omakase.plus.acquisition_policy import (
    AcquisitionPolicy,
    load_user_acquisition_policy,
    policy_from_form,
    policy_from_json,
)
from omakase.plus.db import get_db
from omakase.plus.secrets import store_secret


def test_default_policy_has_stable_secret_free_settings_view() -> None:
    policy = AcquisitionPolicy()

    assert policy.allowed_resolutions == ("1080p", "2160p", "720p")
    assert len(policy.revision) == 64
    view = policy.settings_view()
    assert view["resolution_order"] == "1080p → 2160p → 720p"
    assert "At least 1 seeder" in view["availability"]
    assert "secret" not in json.dumps(view).casefold()


def test_settings_view_uses_plural_seeder_wording() -> None:
    view = AcquisitionPolicy(minimum_seeders=2).settings_view()

    assert view["availability"] == "At least 2 seeders; 5+ preferred"


def test_owner_form_is_validated_and_round_trips_through_encrypted_storage(tmp_path) -> None:
    db = get_db(tmp_path)
    user_id = db.execute(
        "INSERT INTO users (email, password_hash) VALUES ('owner@example.test', 'hash')"
    ).lastrowid
    policy = policy_from_form(
        {
            "resolution_1": "1080p",
            "resolution_2": "720p",
            "resolution_3": "",
            "minimum_seeders": "3",
            "preferred_seeders": "10",
            "max_release_size_gib": "80",
            "prefer_trusted": "on",
        }
    )
    store_secret(db, int(user_id), "acquisition_policy", policy.to_json())

    loaded = load_user_acquisition_policy(db, int(user_id))

    assert loaded == policy
    assert loaded.allowed_resolutions == ("1080p", "720p")
    assert loaded.allow_hardsub_fallback is False
    stored = db.execute(
        "SELECT encrypted_value FROM user_secrets WHERE key_name = 'acquisition_policy'"
    ).fetchone()[0]
    assert "1080p" not in stored


@pytest.mark.parametrize(
    "payload",
    [
        {"minimum_seeders": 0},
        {"allowed_resolutions": ["480p"]},
        {"allowed_resolutions": ["1080p", "1080p"]},
        {"max_release_size_gib": 301},
        {"minimum_seeders": True},
        {"preferred_seeders": 5.5},
        {"prefer_trusted": "false"},
        {"unknown_setting": True},
    ],
)
def test_unsafe_saved_policy_is_rejected(payload) -> None:
    with pytest.raises(ValueError, match="criteria|resolution|Resolution"):
        policy_from_json(json.dumps(payload))


def test_saved_policy_is_not_overwritten_by_new_default_order(tmp_path):
    db = get_db(tmp_path)
    user_id = db.execute(
        "INSERT INTO users (email,password_hash) VALUES ('policy@example.test','hash')"
    ).lastrowid
    saved = AcquisitionPolicy(
        allowed_resolutions=("2160p", "1080p"),
        minimum_seeders=3,
        preferred_seeders=12,
        max_release_size_gib=80,
        allow_hardsub_fallback=False,
        prefer_batches=False,
        prefer_trusted=False,
    )
    store_secret(db, user_id, "acquisition_policy", saved.to_json())
    assert load_user_acquisition_policy(db, user_id) == saved


def test_schema1_serialization_and_revision_match_pre_language_golden_exactly():
    policy = AcquisitionPolicy()
    assert (
        policy.to_json()
        == '{"allow_hardsub_fallback":true,"allowed_resolutions":["1080p","2160p","720p"],"max_release_size_gib":150,"minimum_seeders":1,"prefer_batches":true,"prefer_trusted":true,"preferred_seeders":5,"schema_version":1}'
    )
    assert policy.revision == "7fc5a9edc00a00e1cad28c8d5b867244560f870528214ab53da6e550ecbbfa9c"
    assert policy.language_for("anime").audio_required == ()
    assert policy.language_for("anime").subtitle_required == ("en",)
    assert policy.language_for("movie").audio_required == ("en",)


def test_explicit_schema2_languages_roundtrip_without_changing_quality_or_old_defaults(tmp_path):
    from omakase.plus.language_policy import DomainLanguagePolicies

    db = get_db(tmp_path)
    user = db.execute(
        "INSERT INTO users(email,password_hash) VALUES('languages@fixture.invalid','x')"
    ).lastrowid
    policy = AcquisitionPolicy(
        allowed_resolutions=("720p",),
        minimum_seeders=2,
        languages=DomainLanguagePolicies.new_defaults(),
    )
    assert policy.schema_version == 2
    store_secret(db, user, "acquisition_policy", policy.to_json())
    loaded = load_user_acquisition_policy(db, user)
    assert loaded == policy and loaded.allowed_resolutions == ("720p",)
    assert loaded.language_for("anime").audio_preferred == ("ja",)
    assert loaded.language_for("anime").audio_required == ()
    assert policy_from_json(AcquisitionPolicy().to_json()).schema_version == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"schema_version": 1, "languages": {}},
        {"schema_version": 2, "languages": {"anime": {}}},
        {"schema_version": 2, "languages": False},
    ],
)
def test_language_schema_does_not_accept_implicit_defaults_or_unknown_shapes(payload):
    with pytest.raises(ValueError):
        policy_from_json(json.dumps(payload))
