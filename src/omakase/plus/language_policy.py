"""Pure language policy contracts; no provider, storage or media mutations."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

# Deliberately finite, checked-in catalog. Unknown tags are observations, not
# fulfilled requirements. Region/script variants are never collapsed to base.
LANGUAGES = {
    "en": "English",
    "ja": "Japanese",
    "fr": "French",
    "es": "Spanish",
    "de": "German",
    "it": "Italian",
    "pt": "Portuguese",
    "ko": "Korean",
    "zh": "Chinese",
    "ru": "Russian",
    "ar": "Arabic",
    "hi": "Hindi",
    "pl": "Polish",
    "nl": "Dutch",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "cs": "Czech",
    "hu": "Hungarian",
    "ro": "Romanian",
    "el": "Greek",
    "he": "Hebrew",
    "id": "Indonesian",
    "vi": "Vietnamese",
    "th": "Thai",
    "ms": "Malay",
    "fa": "Persian",
    "ta": "Tamil",
    "te": "Telugu",
    "bn": "Bengali",
    "ur": "Urdu",
    "fil": "Filipino",
    "ca": "Catalan",
    "en-US": "English (United States)",
    "en-GB": "English (United Kingdom)",
    "en-CA": "English (Canada)",
    "en-AU": "English (Australia)",
    "fr-CA": "French (Canada)",
    "fr-FR": "French (France)",
    "es-ES": "Spanish (Spain)",
    "es-MX": "Spanish (Mexico)",
    "es-419": "Spanish (Latin America)",
    "pt-BR": "Portuguese (Brazil)",
    "pt-PT": "Portuguese (Portugal)",
    "zh-Hans": "Chinese (Simplified)",
    "zh-Hant": "Chinese (Traditional)",
    "zh-CN": "Chinese (China)",
    "zh-TW": "Chinese (Taiwan)",
    "zh-Hans-CN": "Chinese (Simplified, China)",
    "zh-Hant-TW": "Chinese (Traditional, Taiwan)",
    "ja-JP": "Japanese (Japan)",
    "ko-KR": "Korean (Korea)",
}
_ALIASES = dict(
    zip(
        "eng jpn fra fre spa deu ger ita por kor zho chi rus ara hin pol nld dut tur ukr swe nor dan fin ces cze hun ron rum ell gre heb ind vie tha msa may fas per tam tel ben urd cat".split(),
        "en ja fr fr es de de it pt ko zh zh ru ar hi pl nl nl tr uk sv no da fi cs cs hu ro ro el el he id vi th ms ms fa fa ta te bn ur ca".split(),
        strict=True,
    )
)
_CANONICAL = {tag.casefold(): tag for tag in LANGUAGES}
_DOMAINS = ("anime", "movie", "tv")
_ROLES = {"full_dialogue", "forced", "signs", "sdh", "commentary", "unknown"}


def _require(condition, message="Language policy is invalid."):
    if not condition:
        raise ValueError(message)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def normalize_language(value: object, *, observation=False) -> str | None:
    if not isinstance(value, str) or len(value) > 35:
        if observation:
            return None
        raise ValueError("Choose a supported language.")
    text = value.strip().replace("_", "-" if observation else "_").casefold()
    parts = text.split("-")
    parts[0] = _ALIASES.get(parts[0], parts[0])
    text = "-".join(parts)
    result = _CANONICAL.get(text)
    if result is None and not observation:
        raise ValueError("Choose a supported language.")
    return result


def language_matches(requested: str, observed: str | None) -> bool:
    requested = normalize_language(requested)
    observed = normalize_language(observed, observation=True)
    return bool(observed and (observed == requested or observed.startswith(requested + "-")))


def _choices(values):
    _require(
        isinstance(values, (tuple, list)) and len(values) <= 8,
        "Choose at most eight languages per list.",
    )
    return tuple(dict.fromkeys(normalize_language(value) for value in values))


@dataclass(frozen=True, slots=True)
class LanguageRule:
    audio_preferred: tuple[str, ...] = ()
    audio_required: tuple[str, ...] = ()
    audio_requirement: str = "any_of"
    audio_fallback: tuple[str, ...] = ()
    subtitle_preferred: tuple[str, ...] = ()
    subtitle_required: tuple[str, ...] = ()
    subtitle_requirement: str = "any_of"
    subtitle_fallback: tuple[str, ...] = ()
    subtitles_mode: str = "off"
    subtitle_kind: str = "selectable_required"
    allow_sdh: bool = False
    require_audio_stream: bool = True

    def __post_init__(self):
        for name in (
            "audio_preferred",
            "audio_required",
            "audio_fallback",
            "subtitle_preferred",
            "subtitle_required",
            "subtitle_fallback",
        ):
            object.__setattr__(self, name, _choices(getattr(self, name)))
        _require(
            isinstance(self.audio_requirement, str)
            and self.audio_requirement in {"any_of", "all_of"}
            and isinstance(self.subtitle_requirement, str)
            and self.subtitle_requirement in {"any_of", "all_of"}
        )
        _require(
            isinstance(self.subtitles_mode, str)
            and self.subtitles_mode in {"required", "preferred", "off"}
        )
        _require(
            isinstance(self.subtitle_kind, str)
            and self.subtitle_kind
            in {"selectable_required", "selectable_preferred", "burned_in_fallback"}
        )
        _require(type(self.allow_sdh) is bool and type(self.require_audio_stream) is bool)
        _require(
            self.subtitles_mode != "required" or bool(self.subtitle_required),
            "Required subtitles need a language.",
        )
        _require(
            self.subtitles_mode == "required" or not self.subtitle_required,
            "Subtitle requirements contradict the mode.",
        )
        _require(
            self.subtitles_mode != "off" or not (self.subtitle_preferred or self.subtitle_fallback),
            "Off subtitles cannot have language choices.",
        )
        for required, fallback, mode in (
            (self.audio_required, self.audio_fallback, self.audio_requirement),
            (self.subtitle_required, self.subtitle_fallback, self.subtitle_requirement),
        ):
            _require(
                not fallback or (required and mode == "any_of"),
                "Fallbacks require a require-one rule.",
            )
            _require(
                not set(required).intersection(fallback),
                "Fallback languages must differ from required languages.",
            )

    @property
    def revision(self):
        return _digest(self.to_mapping())

    def to_mapping(self):
        return asdict(self)

    def summary(self):
        def names(tags):
            return ", ".join(LANGUAGES[t] for t in tags)

        audio = (
            (
                "Require "
                + ("every: " if self.audio_requirement == "all_of" else "one of: ")
                + names(self.audio_required)
            )
            if self.audio_required
            else (
                "Prefer " + names(self.audio_preferred) + "; no required audio language"
                if self.audio_preferred
                else "Any audio language"
            )
        )
        subs = (
            "Subtitles not required"
            if self.subtitles_mode == "off"
            else (
                "Require "
                + ("every: " if self.subtitle_requirement == "all_of" else "one of: ")
                + names(self.subtitle_required)
                + "; full-dialogue selectable subtitles"
                if self.subtitle_required
                else "Prefer " + names(self.subtitle_preferred) + " subtitles"
            )
        )
        return {
            "audio_language": audio,
            "subtitle_language": subs,
            "audio_fallback": names(self.audio_fallback) or "None",
            "subtitle_fallback": names(self.subtitle_fallback) or "None",
        }


def rule_from_mapping(value):
    _require(isinstance(value, Mapping), "Language settings must be an object.")
    _require(set(value).issubset(LanguageRule.__dataclass_fields__), "Unknown language setting.")
    try:
        return LanguageRule(**dict(value))
    except TypeError:
        raise ValueError("Language settings are invalid.") from None


@dataclass(frozen=True, slots=True)
class DomainLanguagePolicies:
    anime: LanguageRule
    movie: LanguageRule
    tv: LanguageRule

    def __post_init__(self):
        _require(all(isinstance(getattr(self, d), LanguageRule) for d in _DOMAINS))

    @classmethod
    def legacy_defaults(cls):
        return cls(
            LanguageRule(subtitle_required=("en",), subtitles_mode="required"),
            LanguageRule(audio_required=("en",), audio_preferred=("en",)),
            LanguageRule(),
        )

    @classmethod
    def new_defaults(cls):
        return cls(
            LanguageRule(
                audio_preferred=("ja",), subtitle_required=("en",), subtitles_mode="required"
            ),
            LanguageRule(audio_required=("en",), audio_preferred=("en",)),
            LanguageRule(audio_preferred=("en",)),
        )

    def for_domain(self, domain):
        _require(domain in _DOMAINS, "Choose anime, movie or TV.")
        return getattr(self, domain)

    def to_mapping(self):
        return {d: getattr(self, d).to_mapping() for d in _DOMAINS}


def domains_from_mapping(value):
    _require(
        isinstance(value, Mapping) and set(value) == set(_DOMAINS),
        "Language policies need anime, movie and TV rules.",
    )
    return DomainLanguagePolicies(**{d: rule_from_mapping(value[d]) for d in _DOMAINS})


@dataclass(frozen=True, slots=True)
class TrackObservation:
    stream_id: str
    kind: str
    file_fingerprint: str
    raw_language: str | None = None
    role: str = "unknown"
    source: str = "embedded"
    switchable: bool = True
    selected: bool = False
    release_bound: bool = False
    sync_verified: bool = False
    evidence: str = "tagged"
    language: str | None = field(init=False)

    def __post_init__(self):
        _require(
            isinstance(self.stream_id, str)
            and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", self.stream_id))
        )
        _require(
            isinstance(self.kind, str)
            and self.kind in {"audio", "subtitle"}
            and isinstance(self.role, str)
            and self.role in _ROLES
        )
        _require(
            isinstance(self.file_fingerprint, str)
            and bool(re.fullmatch("[0-9a-f]{64}", self.file_fingerprint))
        )
        _require(
            self.raw_language is None
            or (
                isinstance(self.raw_language, str)
                and bool(re.fullmatch(r"[A-Za-z0-9_-]{0,35}", self.raw_language))
            )
        )
        _require(
            isinstance(self.source, str)
            and self.source in {"embedded", "external", "burned_in"}
            and isinstance(self.evidence, str)
            and self.evidence in {"tagged", "content_verified", "unknown"}
        )
        _require(
            all(
                type(v) is bool
                for v in (self.switchable, self.selected, self.release_bound, self.sync_verified)
            )
        )
        _require(self.source != "burned_in" or (self.kind == "subtitle" and not self.switchable))
        object.__setattr__(
            self,
            "language",
            normalize_language(self.raw_language, observation=True)
            if self.evidence != "unknown"
            else None,
        )


@dataclass(frozen=True, slots=True)
class LanguageEvidence:
    file_fingerprint: str
    tracks: tuple[TrackObservation, ...]
    inventory_complete: bool = False
    kind: str = "exact_file"
    selection_context: str | None = None

    def __post_init__(self):
        _require(
            isinstance(self.file_fingerprint, str)
            and bool(re.fullmatch("[0-9a-f]{64}", self.file_fingerprint))
        )
        _require(isinstance(self.tracks, (tuple, list)) and len(self.tracks) <= 128)
        object.__setattr__(self, "tracks", tuple(self.tracks))
        _require(
            all(
                isinstance(t, TrackObservation) and t.file_fingerprint == self.file_fingerprint
                for t in self.tracks
            ),
            "Tracks must belong to one exact file.",
        )
        _require(
            len({t.stream_id for t in self.tracks}) == len(self.tracks),
            "Track identities must be unique.",
        )
        _require(
            type(self.inventory_complete) is bool
            and isinstance(self.kind, str)
            and self.kind in {"exact_file", "advertised", "presence"}
        )
        _require(
            self.selection_context is None
            or (
                isinstance(self.selection_context, str)
                and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.selection_context))
            )
        )


@dataclass(frozen=True, slots=True)
class LanguageEvaluation:
    policy_revision: str
    file_fingerprint: str
    state: str
    physical_compatible: bool
    audio_stream_ids: tuple[str, ...] = ()
    subtitle_stream_ids: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    fallback_languages: tuple[str, ...] = ()
    selection_confirmed: bool = False
    preferred_audio_stream_ids: tuple[str, ...] = ()
    preferred_subtitle_stream_ids: tuple[str, ...] = ()
    audio_required_confirmed: bool = False
    subtitle_required_confirmed: bool = False
    selected_audio_confirmed: bool = False
    selected_subtitle_confirmed: bool = False

    @property
    def replacement_allowed(self):
        return False


def _eligible(track, rule):
    if track.source == "burned_in":
        return False  # No qualified burned-in publication proof adapter yet.
    if track.source == "external" and not (track.release_bound and track.sync_verified):
        return False
    if track.kind == "subtitle" and not track.switchable:
        return False
    return track.role == "full_dialogue" or (
        track.kind == "subtitle" and rule.allow_sdh and track.role == "sdh"
    )


def _match(required, mode, fallback, tracks, rule):
    if not required:
        return True, (), (), (), False
    eligible = [t for t in tracks if _eligible(t, rule)]
    found = {
        tag: tuple(t.stream_id for t in eligible if language_matches(tag, t.language))
        for tag in required
    }
    missing = tuple(tag for tag in required if not found[tag])
    success = not missing if mode == "all_of" else len(missing) < len(required)
    ids = tuple(dict.fromkeys(s for streams in found.values() for s in streams))
    if success:
        return True, ids, (), (), False
    for tag in fallback:
        ids = tuple(t.stream_id for t in eligible if language_matches(tag, t.language))
        if ids:
            return True, ids, missing, (tag,), False
    unknown = any(
        t.language is None
        or t.role == "unknown"
        or (t.source == "external" and not (t.release_bound and t.sync_verified))
        or any(tag.startswith(t.language + "-") for tag in required)
        for t in tracks
    )
    return False, (), missing, (), unknown


def preferred_tracks(rule, evidence, *, kind):
    """Ordered exact-file choices only; never changes player selection."""
    _require(kind in {"audio", "subtitle"})
    if evidence.kind != "exact_file" or not evidence.inventory_complete:
        return ()
    tags = rule.audio_preferred if kind == "audio" else rule.subtitle_preferred
    return tuple(
        dict.fromkeys(
            track.stream_id
            for tag in tags
            for track in evidence.tracks
            if track.kind == kind
            and _eligible(track, rule)
            and language_matches(tag, track.language)
        )
    )


def evaluate_language(rule, evidence, *, domain, playback_context=None, selection=None):
    _require(isinstance(rule, LanguageRule) and isinstance(evidence, LanguageEvidence))
    _require(domain in _DOMAINS)
    if selection is not None:
        from omakase.plus.playback_proof import PlaybackSelection

        _require(isinstance(selection, PlaybackSelection))

    def result(
        state, compatible=False, audio=(), subtitles=(), missing=(), fallback=(), selected=False
    ):
        return LanguageEvaluation(
            rule.revision,
            evidence.file_fingerprint,
            state,
            compatible,
            audio,
            subtitles,
            missing,
            fallback,
            selected,
            preferred_tracks(rule, evidence, kind="audio"),
            preferred_tracks(rule, evidence, kind="subtitle"),
            bool(
                rule.audio_required and audio and not any(m.startswith("audio:") for m in missing)
            ),
            bool(
                rule.subtitle_required
                and subtitles
                and not any(m.startswith("subtitle:") for m in missing)
            ),
            bool(
                selection
                and selection.file_fingerprint == evidence.file_fingerprint
                and evidence.kind == "exact_file"
                and evidence.inventory_complete
                and selection.selected_audio_id
                in (
                    set(audio)
                    if rule.audio_required
                    else {
                        t.stream_id
                        for t in evidence.tracks
                        if t.kind == "audio" and _eligible(t, rule)
                    }
                )
            ),
            bool(
                selection
                and selection.file_fingerprint == evidence.file_fingerprint
                and evidence.kind == "exact_file"
                and evidence.inventory_complete
                and selection.selected_subtitle_id
                in (
                    set(subtitles)
                    if rule.subtitle_required
                    else {
                        t.stream_id
                        for t in evidence.tracks
                        if t.kind == "subtitle" and _eligible(t, rule)
                    }
                )
            ),
        )

    if domain == "tv":
        return result("unsupported")
    if evidence.kind != "exact_file" or not evidence.inventory_complete:
        return result("pending_verification")
    audio = [t for t in evidence.tracks if t.kind == "audio"]
    subtitles = [t for t in evidence.tracks if t.kind == "subtitle"]
    a, aids, amissing, afallback, aunknown = _match(
        rule.audio_required, rule.audio_requirement, rule.audio_fallback, audio, rule
    )
    s, sids, smissing, sfallback, sunknown = _match(
        rule.subtitle_required, rule.subtitle_requirement, rule.subtitle_fallback, subtitles, rule
    )
    if rule.require_audio_stream and not audio:
        a = False
        amissing = amissing or ("audio_stream",)
    missing = tuple("audio:" + x for x in amissing) + tuple("subtitle:" + x for x in smissing)
    if not (a and s):
        if rule.subtitle_kind == "burned_in_fallback" and any(
            t.source == "burned_in" for t in subtitles
        ):
            return result("unsupported", missing=missing)
        return result(
            "language_unknown" if aunknown or sunknown else "language_unavailable",
            audio=aids,
            subtitles=sids,
            missing=missing,
        )
    fallback = afallback + sfallback
    if domain == "movie":
        # Legacy physical selected/context fields cannot establish viewer identity.
        if (
            selection is None
            or not isinstance(selection, PlaybackSelection)
            or selection.file_fingerprint != evidence.file_fingerprint
        ):
            return result("selection_unconfirmed", True, aids, sids, missing, fallback)
        accepted_audio = (
            set(aids) if rule.audio_required else {t.stream_id for t in audio if _eligible(t, rule)}
        )
        accepted_subtitles = set(sids)
        audio_selected = selection.selected_audio_id in accepted_audio
        subtitle_selected = (
            not rule.subtitle_required or selection.selected_subtitle_id in accepted_subtitles
        )
        if not audio_selected or not subtitle_selected:
            return result("selection_required", True, aids, sids, missing, fallback)
        return result(
            "fallback_available" if fallback else "language_compatible",
            True,
            aids,
            sids,
            missing,
            fallback,
            True,
        )
    return result(
        "fallback_available" if fallback else "language_compatible",
        True,
        aids,
        sids,
        missing,
        fallback,
    )


@dataclass(frozen=True, slots=True)
class EffectiveLanguagePolicy:
    rule: LanguageRule
    source: str
    inherited_revision: str

    def __post_init__(self):
        _require(isinstance(self.rule, LanguageRule))
        _require(
            isinstance(self.source, str)
            and self.source
            in {"library_default", "member_preference", "request_override", "legacy_default"}
        )
        _require(
            isinstance(self.inherited_revision, str)
            and bool(re.fullmatch("[0-9a-f]{64}", self.inherited_revision))
        )

    @property
    def revision(self):
        return self.rule.revision


def resolve_effective_language(
    library_default,
    *,
    member_preference=None,
    request_override=None,
    allow_member=False,
    allow_override=False,
):
    _require(isinstance(library_default, LanguageRule))
    _require(type(allow_member) is bool and type(allow_override) is bool)
    rule = library_default
    source = "library_default"
    if member_preference is not None:
        _require(
            allow_member and isinstance(member_preference, LanguageRule),
            "Member language override is not permitted.",
        )
        rule = member_preference
        source = "member_preference"
    if request_override is not None:
        _require(
            allow_override and isinstance(request_override, LanguageRule),
            "Request language override is not permitted.",
        )
        rule = request_override
        source = "request_override"
    return EffectiveLanguagePolicy(rule, source, library_default.revision)


@dataclass(frozen=True, slots=True)
class LanguageRequestSnapshot:
    request_id: str
    actor_user_id: int
    library_id: str
    domain: str
    media_identity: str
    effective: EffectiveLanguagePolicy
    library_item_id: str | None = None
    resource_id: str | None = None

    def __post_init__(self):
        _require(
            isinstance(self.request_id, str)
            and bool(re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", self.request_id))
        )
        _require(
            type(self.actor_user_id) is int
            and self.actor_user_id > 0
            and isinstance(self.library_id, str)
            and bool(re.fullmatch("[0-9a-f]{32}", self.library_id))
        )
        _require(self.domain in _DOMAINS and isinstance(self.effective, EffectiveLanguagePolicy))
        for value in (self.library_item_id, self.resource_id):
            _require(
                value is None
                or (isinstance(value, str) and bool(re.fullmatch("[0-9a-f]{32}", value)))
            )
        patterns = {
            "anime": r"anilist:[1-9][0-9]{0,9}:season:[0-9]{1,3}",
            "movie": r"imdb:tt[0-9]{7,10}",
            "tv": r"tmdb:[1-9][0-9]{0,9}(?::season:[0-9]{1,3})?",
        }
        _require(
            isinstance(self.media_identity, str)
            and bool(re.fullmatch(patterns[self.domain], self.media_identity))
        )
        parts = self.media_identity.split(":")
        if self.domain != "movie":
            _require(int(parts[1]) <= 2_147_483_647, "Media identity is outside supported bounds.")
            if len(parts) == 4:
                _require(
                    int(parts[3]) <= (99 if self.domain == "anime" else 200),
                    "Season is outside supported bounds.",
                )

    def to_mapping(self):
        return {
            "schema_version": 1,
            "request_id": self.request_id,
            "actor_user_id": self.actor_user_id,
            "library_id": self.library_id,
            "library_item_id": self.library_item_id,
            "resource_id": self.resource_id,
            "domain": self.domain,
            "media_identity": self.media_identity,
            "policy": self.effective.rule.to_mapping(),
            "policy_revision": self.effective.revision,
            "policy_source": self.effective.source,
            "inherited_revision": self.effective.inherited_revision,
        }

    @property
    def binding_digest(self):
        return _digest(self.to_mapping())


def request_snapshot_from_mapping(value):
    """Validate a durable policy snapshot before a worker uses it, without defaults."""
    fields = {
        "schema_version",
        "request_id",
        "actor_user_id",
        "library_id",
        "library_item_id",
        "resource_id",
        "domain",
        "media_identity",
        "policy",
        "policy_revision",
        "policy_source",
        "inherited_revision",
    }
    _require(
        isinstance(value, Mapping) and set(value) == fields, "Request snapshot shape is invalid."
    )
    _require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "Request snapshot version is unsupported.",
    )
    rule = rule_from_mapping(value["policy"])
    _require(
        value["policy_revision"] == rule.revision,
        "Request policy revision does not match its snapshot.",
    )
    effective = EffectiveLanguagePolicy(rule, value["policy_source"], value["inherited_revision"])
    return LanguageRequestSnapshot(
        value["request_id"],
        value["actor_user_id"],
        value["library_id"],
        value["domain"],
        value["media_identity"],
        effective,
        value["library_item_id"],
        value["resource_id"],
    )


def evidence_to_mapping(evidence):
    _require(isinstance(evidence, LanguageEvidence))
    return asdict(evidence)


def evidence_from_mapping(value):
    fields = {"file_fingerprint", "tracks", "inventory_complete", "kind", "selection_context"}
    _require(
        isinstance(value, Mapping) and set(value) == fields,
        "Physical language evidence shape is invalid.",
    )
    _require(isinstance(value["tracks"], list) and len(value["tracks"]) <= 128)
    tracks = []
    for raw in value["tracks"]:
        _require(
            isinstance(raw, Mapping) and set(raw) == set(TrackObservation.__dataclass_fields__),
            "Track evidence shape is invalid.",
        )
        kwargs = dict(raw)
        claimed = kwargs.pop("language")
        track = TrackObservation(**kwargs)
        _require(
            track.language == claimed,
            "Normalized track language conflicts with its raw observation.",
        )
        tracks.append(track)
    return LanguageEvidence(
        value["file_fingerprint"],
        tuple(tracks),
        value["inventory_complete"],
        value["kind"],
        value["selection_context"],
    )
