from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest

from omakase.plus.language_policy import (
    DomainLanguagePolicies,
    LanguageEvidence,
    LanguageRequestSnapshot,
    LanguageRule,
    TrackObservation,
    domains_from_mapping,
    evaluate_language,
    language_matches,
    normalize_language,
    resolve_effective_language,
    rule_from_mapping,
)

FP = "a" * 64


def track(identity, kind, language, **kwargs):
    return TrackObservation(
        identity, kind, FP, language, role=kwargs.pop("role", "full_dialogue"), **kwargs
    )


def evidence(*tracks, **kwargs):
    return LanguageEvidence(
        FP, tracks, inventory_complete=kwargs.pop("inventory_complete", True), **kwargs
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("eng", "en"),
        ("EN-us", "en-US"),
        ("fra", "fr"),
        ("fre", "fr"),
        ("fr-ca", "fr-CA"),
        ("jpn", "ja"),
        ("ger", "de"),
        ("deu", "de"),
        ("zho", "zh"),
        ("chi-Hant", "zh-Hant"),
        ("ZH-hans-cn", "zh-Hans-CN"),
    ],
)
def test_canonical_aliases_and_script_region_preservation(raw, expected):
    assert normalize_language(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "und",
        "mul",
        "",
        "xxx",
        "*",
        "x-private",
        "fr-XX",
        "zh-Latn",
        "http://evil",
        "en_US",
        None,
        True,
        42,
    ],
)
def test_unknown_unregistered_or_unbounded_requests_are_rejected(raw):
    with pytest.raises(ValueError):
        normalize_language(raw)


def test_unknown_observation_does_not_fulfill_language_or_infer_from_title():
    assert normalize_language("und", observation=True) is None
    assert normalize_language("mul", observation=True) is None
    assert normalize_language("Japanese", observation=True) is None
    assert normalize_language("en_US", observation=True) == "en-US"
    assert language_matches("fr", "fr-CA")
    assert not language_matches("fr-CA", "fr")
    assert not language_matches("fr-CA", "fr-FR")
    assert not language_matches("zh-Hant", "zh-Hans")


def test_ordered_choices_deduplicate_aliases_and_preserve_revision():
    a = LanguageRule(audio_preferred=["jpn", "fra", "ja", "en"])
    b = LanguageRule(audio_preferred=("ja", "fr", "en"))
    assert a.audio_preferred == ("ja", "fr", "en") and a.revision == b.revision
    assert (
        LanguageRule(audio_preferred=("en", "ja")).revision
        != LanguageRule(audio_preferred=("ja", "en")).revision
    )
    assert "no required audio language" in a.summary()["audio_language"]
    with pytest.raises(FrozenInstanceError):
        a.audio_required = ("fr",)


@pytest.mark.parametrize(
    "value",
    [
        {"audio_preferred": "en"},
        {"audio_required": True},
        {"audio_preferred": ["en"] * 9},
        {"unknown": True},
        {"subtitles_mode": "required"},
        {"subtitles_mode": "off", "subtitle_required": ["en"]},
        {"audio_required": ["fr"], "audio_fallback": ["fr"]},
        {"audio_required": ["fr", "ja"], "audio_requirement": "all_of", "audio_fallback": ["en"]},
        {"allow_sdh": 1},
    ],
)
def test_closed_bounded_contradiction_free_policy(value):
    with pytest.raises(ValueError):
        rule_from_mapping(value)


def test_explicit_new_defaults_do_not_retroactively_make_japanese_required():
    old = DomainLanguagePolicies.legacy_defaults()
    new = DomainLanguagePolicies.new_defaults()
    assert old.anime.audio_required == () and old.anime.audio_preferred == ()
    assert new.anime.audio_required == () and new.anime.audio_preferred == ("ja",)
    assert old.anime.subtitle_required == new.anime.subtitle_required == ("en",)
    assert old.movie.audio_required == new.movie.audio_required == ("en",)
    assert domains_from_mapping(new.to_mapping()) == new
    with pytest.raises(ValueError):
        domains_from_mapping({"anime": {}})


def test_any_all_and_same_copy_multitrack_reuse_without_replacement():
    observed = evidence(track("a1", "audio", "jpn"), track("a2", "audio", "fra"))
    any_rule = LanguageRule(audio_required=("fr", "en"))
    all_rule = LanguageRule(audio_required=("fr", "ja"), audio_requirement="all_of")
    assert evaluate_language(any_rule, observed, domain="anime").physical_compatible
    result = evaluate_language(all_rule, observed, domain="anime")
    assert result.state == "language_compatible" and set(result.audio_stream_ids) == {"a1", "a2"}
    assert result.replacement_allowed is False
    with pytest.raises(ValueError):
        LanguageEvidence(FP, (TrackObservation("other", "audio", "b" * 64, "en"),), True)


def test_preferences_only_never_claim_required_language_match():
    result = evaluate_language(
        LanguageRule(audio_preferred=("ja",)),
        evidence(track("audio", "audio", "und")),
        domain="anime",
    )
    assert (
        result.physical_compatible
        and result.audio_stream_ids == ()
        and result.missing_required == ()
    )


def test_regional_unknown_and_unavailable_are_distinct():
    rule = LanguageRule(audio_required=("fr-CA",))
    assert (
        evaluate_language(rule, evidence(track("a", "audio", "fr")), domain="anime").state
        == "language_unknown"
    )
    assert (
        evaluate_language(rule, evidence(track("a", "audio", "fr-FR")), domain="anime").state
        == "language_unavailable"
    )
    assert (
        evaluate_language(rule, evidence(track("a", "audio", "fr-CA")), domain="anime").state
        == "language_compatible"
    )
    assert (
        evaluate_language(rule, evidence(track("a", "audio", "und")), domain="anime").state
        == "language_unknown"
    )


def test_explicit_fallback_keeps_original_requirement_and_names_alternative():
    rule = LanguageRule(audio_required=("ja",), audio_fallback=("fr", "en"))
    result = evaluate_language(rule, evidence(track("a", "audio", "eng")), domain="anime")
    assert result.state == "fallback_available" and result.fallback_languages == ("en",)
    assert result.missing_required == ("audio:ja",) and result.replacement_allowed is False


@pytest.mark.parametrize("role", ["forced", "signs", "commentary"])
def test_forced_signs_commentary_never_satisfy_full_dialogue_subtitles(role):
    rule = DomainLanguagePolicies.legacy_defaults().anime
    result = evaluate_language(
        rule,
        evidence(track("a", "audio", "und"), track("s", "subtitle", "en", role=role)),
        domain="anime",
    )
    assert not result.physical_compatible


def test_sdh_external_sync_burned_in_and_unknown_roles_have_explicit_limits():
    rule = LanguageRule(subtitle_required=("fr",), subtitles_mode="required")
    observed = evidence(track("a", "audio", "ja"), track("s", "subtitle", "fr", role="sdh"))
    assert not evaluate_language(rule, observed, domain="anime").physical_compatible
    allowed = LanguageRule(subtitle_required=("fr",), subtitles_mode="required", allow_sdh=True)
    assert evaluate_language(allowed, observed, domain="anime").physical_compatible
    unsynced = evidence(
        track("a", "audio", "ja"),
        track("s", "subtitle", "fr", source="external", sync_verified=False),
    )
    assert not evaluate_language(rule, unsynced, domain="anime").physical_compatible
    burned = LanguageRule(
        subtitle_required=("fr",), subtitles_mode="required", subtitle_kind="burned_in_fallback"
    )
    observed = evidence(
        track("a", "audio", "ja"),
        track("s", "subtitle", "fr", source="burned_in", switchable=False),
    )
    assert evaluate_language(burned, observed, domain="anime").state == "unsupported"


def test_advertised_presence_incomplete_inventory_and_tv_never_claim_verified_ready():
    rule = LanguageRule(audio_required=("fr",))
    tracks = (track("a", "audio", "fr"),)
    for observed in (
        evidence(*tracks, kind="advertised"),
        evidence(*tracks, kind="presence"),
        evidence(*tracks, inventory_complete=False),
    ):
        assert evaluate_language(rule, observed, domain="anime").state == "pending_verification"
    assert evaluate_language(rule, evidence(*tracks), domain="tv").state == "unsupported"
    assert not hasattr(evaluate_language(rule, evidence(*tracks), domain="anime"), "ready")


def test_movie_available_and_selected_tracks_and_viewer_context_stay_separate():
    from omakase.plus.playback_proof import PlaybackSelection

    rule = DomainLanguagePolicies.legacy_defaults().movie
    observed = LanguageEvidence(FP, (track("a", "audio", "en"),), True)
    forged = replace(
        observed,
        tracks=(replace(observed.tracks[0], selected=True),),
        selection_context="plex-user:2:unverified",
    )
    assert (
        evaluate_language(
            rule, forged, domain="movie", playback_context="plex-user:2:unverified"
        ).state
        == "selection_unconfirmed"
    )
    selection = PlaybackSelection(1, 2, FP, "a", None)
    assert evaluate_language(
        rule, observed, domain="movie", selection=selection
    ).selection_confirmed


def test_effective_request_snapshot_is_frozen_and_forbidden_override_is_rejected():
    library = DomainLanguagePolicies.legacy_defaults().anime
    member = LanguageRule(audio_required=("fr",))
    with pytest.raises(ValueError):
        resolve_effective_language(library, member_preference=member)
    effective = resolve_effective_language(library, member_preference=member, allow_member=True)
    snap = LanguageRequestSnapshot(
        "request-1", 2, "5" * 32, "anime", "anilist:21:season:1", effective
    )
    original = json.dumps(snap.to_mapping(), sort_keys=True)
    changed = resolve_effective_language(
        library, request_override=LanguageRule(audio_required=("en",)), allow_override=True
    )
    assert snap.effective.rule == member and original == json.dumps(
        snap.to_mapping(), sort_keys=True
    )
    assert (
        snap.binding_digest
        != LanguageRequestSnapshot(
            "request-1", 2, "5" * 32, "anime", "anilist:21:season:1", changed
        ).binding_digest
    )
    assert snap.effective.source == "member_preference"
    with pytest.raises(ValueError):
        LanguageRequestSnapshot(
            "request-1", True, "5" * 32, "anime", "anilist:21:season:1", effective
        )


def test_request_snapshot_roundtrip_and_tampered_policy_fail_closed():
    from omakase.plus.language_policy import request_snapshot_from_mapping

    rule = LanguageRule(audio_required=("fr-CA",))
    snapshot = LanguageRequestSnapshot(
        "request-1", 2, "5" * 32, "movie", "imdb:tt0000001", resolve_effective_language(rule)
    )
    encoded = json.loads(json.dumps(snapshot.to_mapping()))
    restored = request_snapshot_from_mapping(encoded)
    assert restored == snapshot and restored.binding_digest == snapshot.binding_digest
    encoded["policy"]["audio_required"] = ["en"]
    with pytest.raises(ValueError, match="revision"):
        request_snapshot_from_mapping(encoded)


def test_ordered_preferred_tracks_are_choices_not_remote_selection():
    observed = evidence(
        track("en", "audio", "eng"), track("ja", "audio", "jpn"), track("fr", "audio", "fra")
    )
    rule = LanguageRule(audio_preferred=("fr", "ja", "en"))
    result = evaluate_language(rule, observed, domain="anime")
    assert result.preferred_audio_stream_ids == ("fr", "ja", "en")
    assert result.audio_stream_ids == () and not result.selection_confirmed
    assert not any(t.selected for t in observed.tracks)


def test_external_subtitle_binding_is_never_assumed_from_default_fields():
    observed = evidence(
        track("audio", "audio", "ja"), track("subtitle", "subtitle", "en", source="external")
    )
    rule = DomainLanguagePolicies.legacy_defaults().anime
    assert evaluate_language(rule, observed, domain="anime").state == "language_unknown"
    verified = evidence(
        track("audio", "audio", "ja"),
        track(
            "subtitle", "subtitle", "en", source="external", release_bound=True, sync_verified=True
        ),
    )
    assert evaluate_language(rule, verified, domain="anime").physical_compatible


@pytest.mark.parametrize(
    "value",
    [
        {"audio_requirement": []},
        {"subtitle_requirement": {}},
        {"subtitle_kind": []},
        {"subtitles_mode": []},
    ],
)
def test_malformed_enum_types_are_controlled_validation_errors(value):
    with pytest.raises(ValueError):
        rule_from_mapping(value)


def test_preference_only_and_fallback_do_not_claim_original_language_requirements_verified():
    preferred = evaluate_language(
        LanguageRule(audio_preferred=("ja",)), evidence(track("a", "audio", "en")), domain="anime"
    )
    assert (
        preferred.physical_compatible
        and not preferred.audio_required_confirmed
        and not preferred.subtitle_required_confirmed
    )
    fallback = evaluate_language(
        LanguageRule(audio_required=("ja",), audio_fallback=("en",)),
        evidence(track("a", "audio", "en")),
        domain="anime",
    )
    assert fallback.state == "fallback_available" and not fallback.audio_required_confirmed


@pytest.mark.parametrize(
    "domain,identity",
    [
        ("anime", "anilist:9999999999:season:1"),
        ("anime", "anilist:21:season:999"),
        ("tv", "tmdb:9999999999"),
        ("tv", "tmdb:42:season:201"),
    ],
)
def test_snapshot_media_identity_bounds(domain, identity):
    with pytest.raises(ValueError):
        LanguageRequestSnapshot(
            "request", 1, "1" * 32, domain, identity, resolve_effective_language(LanguageRule())
        )


def test_partial_confirmed_tracks_are_retained_when_other_requirement_fails():
    rule = LanguageRule(
        audio_required=("fr",), subtitle_required=("en",), subtitles_mode="required"
    )
    result = evaluate_language(rule, evidence(track("a", "audio", "fr")), domain="anime")
    assert not result.physical_compatible and result.state == "language_unavailable"
    assert result.audio_stream_ids == ("a",) and result.audio_required_confirmed
    assert not result.subtitle_required_confirmed and result.missing_required == ("subtitle:en",)


def test_malformed_snapshot_source_does_not_escape_as_uncontrolled_type_error():
    from omakase.plus.language_policy import request_snapshot_from_mapping

    encoded = LanguageRequestSnapshot(
        "r", 1, "1" * 32, "movie", "imdb:tt0000001", resolve_effective_language(LanguageRule())
    ).to_mapping()
    encoded["policy_source"] = []
    with pytest.raises(ValueError):
        request_snapshot_from_mapping(encoded)


def test_movie_all_of_inventory_needs_only_one_eligible_selected_subtitle():
    from omakase.plus.playback_proof import PlaybackSelection

    rule = LanguageRule(
        audio_required=("en",),
        subtitle_required=("en", "fr"),
        subtitle_requirement="all_of",
        subtitles_mode="required",
    )
    observed = LanguageEvidence(
        FP,
        (
            track("a", "audio", "en"),
            track("s-en", "subtitle", "en"),
            track("s-fr", "subtitle", "fr"),
            replace(track("forced", "subtitle", "en"), role="forced"),
        ),
        True,
    )

    def evaluate(subtitle):
        return evaluate_language(
            rule, observed, domain="movie", selection=PlaybackSelection(1, 2, FP, "a", subtitle)
        )

    assert evaluate("s-en").selection_confirmed and evaluate("s-en").subtitle_required_confirmed
    assert evaluate("s-fr").selection_confirmed
    assert evaluate(None).state == "selection_required"
    assert evaluate("forced").state == "selection_required"
