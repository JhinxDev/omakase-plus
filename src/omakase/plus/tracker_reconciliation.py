"""Pure personal tracker reconciliation, with no provider, storage or execution code.

Inputs are normalized adapter observations, not unqualified MAL wire fields. Every
patch is only a plan: exact rereads are prerequisites, not an external atomic CAS.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import date
from enum import Enum

MAX_ENTRIES = 10_000


class Provider(str, Enum):
    ANILIST = "anilist"
    MAL = "myanimelist"


class Status(str, Enum):
    PLANNING = "planning"
    CURRENT = "current"
    COMPLETED = "completed"
    PAUSED = "paused"
    DROPPED = "dropped"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _integer(value: object, minimum: int, maximum: int = 2**63 - 1) -> bool:
    return type(value) is int and minimum <= value <= maximum


@dataclass(frozen=True, slots=True)
class Identity:
    user_id: int
    provider: Provider
    remote_user_id: int
    generation: int
    verified: bool = False

    def __post_init__(self) -> None:
        _require(
            all(_integer(v, 1) for v in (self.user_id, self.remote_user_id, self.generation)),
            "Invalid personal identity",
        )
        _require(
            isinstance(self.provider, Provider) and type(self.verified) is bool,
            "Invalid identity verification",
        )


@dataclass(frozen=True, slots=True)
class Pair:
    anilist: Identity
    mal: Identity

    def __post_init__(self) -> None:
        _require(
            isinstance(self.anilist, Identity) and isinstance(self.mal, Identity),
            "Typed identities required",
        )
        _require(
            self.anilist.provider == Provider.ANILIST
            and self.mal.provider == Provider.MAL
            and self.anilist.user_id == self.mal.user_id,
            "Personal identities must belong to one local user",
        )


@dataclass(frozen=True, slots=True)
class ExactMapping:
    anilist_id: int
    mal_id: int
    observed_id_mal: int
    media_kind: str = "ANIME"

    def __post_init__(self) -> None:
        _require(
            all(_integer(v, 1) for v in (self.anilist_id, self.mal_id, self.observed_id_mal))
            and self.mal_id == self.observed_id_mal
            and self.media_kind == "ANIME",
            "Exact AniList idMal anime mapping required",
        )

    def media_id(self, provider: Provider) -> int:
        _require(isinstance(provider, Provider), "Typed provider required")
        return self.anilist_id if provider == Provider.ANILIST else self.mal_id


@dataclass(frozen=True, slots=True)
class FuzzyDate:
    year: int | None = None
    month: int | None = None
    day: int | None = None

    def __post_init__(self) -> None:
        _require(_integer(self.year, 1, 9999), "A known date year is required; use None for unset")
        _require(
            self.month is None or (self.year is not None and _integer(self.month, 1, 12)),
            "Invalid date precision/month",
        )
        _require(
            self.day is None or (self.month is not None and _integer(self.day, 1, 31)),
            "Invalid date precision/day",
        )
        if self.day is not None:
            date(self.year, self.month, self.day)


@dataclass(frozen=True, slots=True)
class State:
    status: Status = Status.CURRENT
    progress: int = 0
    score: int | None = None  # normalized 1–100; None is explicitly unscored
    repeat: int = 0
    rewatching: bool = False
    started_at: FuzzyDate | None = None
    completed_at: FuzzyDate | None = None

    def __post_init__(self) -> None:
        _require(isinstance(self.status, Status), "Typed status required")
        _require(
            _integer(self.progress, 0, 1_000_000) and _integer(self.repeat, 0, 1000),
            "Invalid progress/repeat",
        )
        _require(self.score is None or _integer(self.score, 1, 100), "Invalid normalized score")
        _require(
            type(self.rewatching) is bool
            and (not self.rewatching or self.status == Status.CURRENT),
            "Rewatching requires current status",
        )
        _require(
            all(
                v is None or isinstance(v, FuzzyDate) for v in (self.started_at, self.completed_at)
            ),
            "Typed dates required",
        )


FIELD_NAMES = tuple(f.name for f in fields(State))
PROGRESSION = ("status", "progress", "repeat", "rewatching")
Value = Status | int | bool | FuzzyDate | None


@dataclass(frozen=True, slots=True)
class Entry:
    media_id: int
    state: State
    revision: str | None = None  # optional opaque adapter revision, not a timestamp winner

    def __post_init__(self) -> None:
        _require(
            _integer(self.media_id, 1) and isinstance(self.state, State), "Invalid normalized entry"
        )
        _require(
            self.revision is None
            or (
                isinstance(self.revision, str)
                and 1 <= len(self.revision) <= 200
                and not any(ord(c) < 32 for c in self.revision)
            ),
            "Invalid opaque entry revision",
        )


@dataclass(frozen=True, slots=True)
class Inventory:
    identity: Identity
    entries: tuple[Entry, ...]
    complete: bool = False

    def __post_init__(self) -> None:
        _require(
            isinstance(self.identity, Identity) and type(self.complete) is bool,
            "Invalid inventory identity/completeness",
        )
        _require(
            isinstance(self.entries, tuple)
            and len(self.entries) <= MAX_ENTRIES
            and all(isinstance(e, Entry) for e in self.entries),
            "Bounded typed entry tuple required",
        )
        _require(
            len({e.media_id for e in self.entries}) == len(self.entries),
            "Duplicate inventory identity",
        )
        if self.identity.provider == Provider.MAL:
            _require(
                all(e.state.score is None or e.state.score % 10 == 0 for e in self.entries),
                "MAL canonical score must be representable",
            )


@dataclass(frozen=True, slots=True)
class Baseline:
    pair: Pair
    mapping: ExactMapping
    anilist: Entry
    mal: Entry

    def __post_init__(self) -> None:
        _require(
            isinstance(self.pair, Pair) and isinstance(self.mapping, ExactMapping),
            "Invalid baseline identity",
        )
        _require(
            isinstance(self.anilist, Entry)
            and isinstance(self.mal, Entry)
            and self.anilist.media_id == self.mapping.anilist_id
            and self.mal.media_id == self.mapping.mal_id,
            "Baseline mapping mismatch",
        )
        _require(
            self.mal.state.score is None or self.mal.state.score % 10 == 0,
            "Invalid MAL baseline score",
        )


@dataclass(frozen=True, slots=True)
class ScoreDecision:
    pair: Pair
    mapping: ExactMapping
    source_score: int
    target_score: int

    def __post_init__(self) -> None:
        _require(
            isinstance(self.pair, Pair) and isinstance(self.mapping, ExactMapping),
            "Invalid score decision identity",
        )
        _require(
            _integer(self.source_score, 1, 100)
            and self.source_score % 10 != 0
            and _integer(self.target_score, 10, 100)
            and self.target_score % 10 == 0
            and self.target_score
            in {(self.source_score // 10) * 10, ((self.source_score + 9) // 10) * 10},
            "Explicit nearest score quantization required",
        )


@dataclass(frozen=True, slots=True)
class PlannedPatch:
    pair: Pair
    mapping: ExactMapping
    target: Provider
    changes: tuple[tuple[str, Value], ...]
    before_source: Entry
    before_target: Entry | None

    def __post_init__(self) -> None:
        _require(
            isinstance(self.pair, Pair)
            and isinstance(self.mapping, ExactMapping)
            and isinstance(self.target, Provider),
            "Invalid patch identity",
        )
        _require(
            isinstance(self.changes, tuple)
            and bool(self.changes)
            and all(
                isinstance(c, tuple) and len(c) == 2 and c[0] in FIELD_NAMES for c in self.changes
            ),
            "Closed nonempty field patch required",
        )
        _require(len(dict(self.changes)) == len(self.changes), "Duplicate patch field")
        source = Provider.ANILIST if self.target == Provider.MAL else Provider.MAL
        _require(
            isinstance(self.before_source, Entry)
            and self.before_source.media_id == self.mapping.media_id(source),
            "Exact source precondition required",
        )
        _require(
            self.before_target is None
            or (
                isinstance(self.before_target, Entry)
                and self.before_target.media_id == self.mapping.media_id(self.target)
            ),
            "Exact destination precondition required",
        )
        result = replace(
            self.before_target.state if self.before_target else State(), **dict(self.changes)
        )
        _require(
            self.target != Provider.MAL or result.score is None or result.score % 10 == 0,
            "Unrepresentable target score",
        )


@dataclass(frozen=True, slots=True)
class Conflict:
    mapping: ExactMapping
    field: str
    reason: str
    anilist: Value = None
    mal: Value = None


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    patches: tuple[PlannedPatch, ...] = ()
    proposals: tuple[PlannedPatch, ...] = ()
    conflicts: tuple[Conflict, ...] = ()
    unmapped: tuple[tuple[Provider, int], ...] = ()
    blocked: tuple[str, ...] = ()
    review_required: bool = False


def preconditions_match(
    patch: PlannedPatch, pair: Pair, source: Entry | None, target: Entry | None
) -> bool:
    """Compare exact freshly reread entries, including absence for a planned create.

    True is NOT provider authorization or atomic external compare-and-set. A future
    executor must recheck approval/leases/generations and disclose the residual race.
    """
    return (
        isinstance(patch, PlannedPatch)
        and pair == patch.pair
        and pair.anilist.verified
        and pair.mal.verified
        and source == patch.before_source
        and target == patch.before_target
    )


def _converted_score(
    value: Value, pair: Pair, mapping: ExactMapping, decisions: tuple[ScoreDecision, ...]
) -> Value:
    if value is None or value % 10 == 0:
        return value
    matches = [
        d for d in decisions if d.pair == pair and d.mapping == mapping and d.source_score == value
    ]
    return matches[0].target_score if matches else "lossy"


def plan_reconciliation(
    pair: Pair,
    anilist: Inventory,
    mal: Inventory,
    mappings: tuple[ExactMapping, ...],
    baselines: tuple[Baseline, ...] = (),
    *,
    score_decisions: tuple[ScoreDecision, ...] = (),
) -> ReconciliationPlan:
    """Return a zero-write preview or three-way patch plan, never advance baselines.

    Inventory.complete means a full bounded traversal, NOT an atomic remote snapshot.
    A Baseline must be created externally only after user review and acknowledged
    readbacks; this function cannot manufacture that approval or execute a patch.
    """
    _require(
        isinstance(pair, Pair) and isinstance(anilist, Inventory) and isinstance(mal, Inventory),
        "Typed pair and inventories required",
    )
    _require(
        anilist.identity == pair.anilist and mal.identity == pair.mal,
        "Inventory owner/connection mismatch",
    )
    _require(
        isinstance(mappings, tuple)
        and len(mappings) <= MAX_ENTRIES
        and all(isinstance(m, ExactMapping) for m in mappings),
        "Bounded exact mappings required",
    )
    _require(
        len({m.anilist_id for m in mappings}) == len(mappings) == len({m.mal_id for m in mappings}),
        "Mapping collision",
    )
    _require(
        isinstance(baselines, tuple)
        and len(baselines) <= MAX_ENTRIES
        and all(isinstance(b, Baseline) for b in baselines),
        "Bounded typed baselines required",
    )
    _require(len({b.mapping for b in baselines}) == len(baselines), "Duplicate baseline mapping")
    _require(
        isinstance(score_decisions, tuple)
        and len(score_decisions) <= MAX_ENTRIES
        and all(isinstance(d, ScoreDecision) for d in score_decisions),
        "Bounded score decisions required",
    )
    _require(
        len({(d.pair, d.mapping, d.source_score) for d in score_decisions}) == len(score_decisions),
        "Ambiguous score decisions",
    )
    if not pair.anilist.verified or not pair.mal.verified:
        return ReconciliationPlan(blocked=("unverified_identity",))
    if not anilist.complete or not mal.complete:
        return ReconciliationPlan(blocked=("incomplete_inventory",))
    if any(b.pair != pair for b in baselines):
        return ReconciliationPlan(blocked=("baseline_identity_changed",))
    _require(all(b.mapping in mappings for b in baselines), "Baseline mapping changed or omitted")
    _require(
        all(d.pair == pair and d.mapping in mappings for d in score_decisions),
        "Score decision identity/mapping changed",
    )
    by_a, by_m = ({e.media_id: e for e in inventory.entries} for inventory in (anilist, mal))
    old = {b.mapping: b for b in baselines}
    patches, proposals, conflicts = [], [], []
    review = False
    mapped_a, mapped_m = {m.anilist_id for m in mappings}, {m.mal_id for m in mappings}
    unmapped = tuple(
        [(Provider.ANILIST, i) for i in sorted(by_a.keys() - mapped_a)]
        + [(Provider.MAL, i) for i in sorted(by_m.keys() - mapped_m)]
    )
    for mapping in mappings:
        a, m, b = by_a.get(mapping.anilist_id), by_m.get(mapping.mal_id), old.get(mapping)
        if b is None:
            if a is None and m is None:
                continue
            review = True
            if a is not None and m is not None:
                conflicts.extend(
                    Conflict(
                        mapping,
                        name,
                        "initial_difference",
                        getattr(a.state, name),
                        getattr(m.state, name),
                    )
                    for name in FIELD_NAMES
                    if getattr(a.state, name) != getattr(m.state, name)
                )
                continue
            source, target = (a, Provider.MAL) if a is not None else (m, Provider.ANILIST)
            changes = {
                name: getattr(source.state, name)
                for name in FIELD_NAMES
                if getattr(source.state, name) is not None
            }
            if target == Provider.MAL and "score" in changes:
                converted = _converted_score(changes["score"], pair, mapping, score_decisions)
                if converted == "lossy":
                    conflicts.append(
                        Conflict(mapping, "score", "lossy_score", source.state.score, None)
                    )
                    continue
                changes["score"] = converted
            proposals.append(
                PlannedPatch(pair, mapping, target, tuple(changes.items()), source, None)
            )
            continue
        if a is None or m is None:
            conflicts.append(Conflict(mapping, "entry", "entry_absent"))
            continue
        changed_a = {
            name for name in FIELD_NAMES if getattr(a.state, name) != getattr(b.anilist.state, name)
        }
        changed_m = {
            name for name in FIELD_NAMES if getattr(m.state, name) != getattr(b.mal.state, name)
        }
        progression_conflict = bool(
            changed_a.intersection(PROGRESSION)
            and changed_m.intersection(PROGRESSION)
            and any(getattr(a.state, name) != getattr(m.state, name) for name in PROGRESSION)
        )
        if progression_conflict:
            conflicts.append(Conflict(mapping, "progression", "concurrent_progression"))
        changes = {Provider.ANILIST: {}, Provider.MAL: {}}
        blocked_progression = set()
        for name in FIELD_NAMES:
            av, mv = getattr(a.state, name), getattr(m.state, name)
            ca, cm = name in changed_a, name in changed_m
            if not ca and not cm or av == mv or (progression_conflict and name in PROGRESSION):
                continue
            if ca and cm:
                conflicts.append(Conflict(mapping, name, "both_changed", av, mv))
                continue
            value, previous, source, target = (
                (av, getattr(b.anilist.state, name), a, Provider.MAL)
                if ca
                else (mv, getattr(b.mal.state, name), m, Provider.ANILIST)
            )
            reason = None
            if value is None:
                reason = "explicit_clear"
            elif name == "progress" and value < previous:
                prior = b.anilist if ca else b.mal
                if not (
                    (source.state.rewatching and not prior.state.rewatching)
                    or source.state.repeat > prior.state.repeat
                ):
                    reason = "progress_decrease"
            elif name == "repeat" and value < previous:
                reason = "repeat_decrease"
            elif name == "score" and target == Provider.MAL:
                value = _converted_score(value, pair, mapping, score_decisions)
                if value == "lossy":
                    reason = "lossy_score"
            if reason:
                conflicts.append(Conflict(mapping, name, reason, av, mv))
                if name in PROGRESSION:
                    blocked_progression.add(target)
            else:
                changes[target][name] = value
        for target, values in changes.items():
            if target in blocked_progression:
                values = {name: value for name, value in values.items() if name not in PROGRESSION}
            if values:
                source, destination = (a, m) if target == Provider.MAL else (m, a)
                try:
                    patch = PlannedPatch(
                        pair, mapping, target, tuple(values.items()), source, destination
                    )
                except ValueError:
                    conflicts.append(Conflict(mapping, "progression", "incompatible_merged_state"))
                else:
                    patches.append(patch)
    return ReconciliationPlan(
        tuple(patches),
        tuple(proposals),
        tuple(conflicts),
        unmapped,
        review_required=review or bool(conflicts) or bool(unmapped),
    )
