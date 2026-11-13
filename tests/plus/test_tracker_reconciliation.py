"""Pure reconciliation fixtures; no account, database or provider access."""

from dataclasses import replace

import pytest

from omakase.plus.tracker_reconciliation import (
    Baseline,
    Entry,
    ExactMapping,
    FuzzyDate,
    Identity,
    Inventory,
    Pair,
    Provider,
    ScoreDecision,
    State,
    Status,
    plan_reconciliation,
    preconditions_match,
)


def pair():
    return Pair(
        Identity(1, Provider.ANILIST, 101, 1, verified=True),
        Identity(1, Provider.MAL, 202, 1, verified=True),
    )


def mapping():
    return ExactMapping(11, 22, 22)


def entry(provider, **changes):
    return Entry(11 if provider == Provider.ANILIST else 22, replace(State(), **changes))


def inventories(a=None, m=None, *, owner=None, complete=True):
    owner = owner or pair()
    return (
        Inventory(owner.anilist, (a,) if a else (), complete),
        Inventory(owner.mal, (m,) if m else (), complete),
    )


def run(a, m, old=None, **kwargs):
    left, right = inventories(a, m)
    return plan_reconciliation(pair(), left, right, (mapping(),), (old,) if old else (), **kwargs)


def baseline(**changes):
    return Baseline(
        pair(), mapping(), entry(Provider.ANILIST, **changes), entry(Provider.MAL, **changes)
    )


def test_initial_preview_is_review_only_even_for_missing_counterpart():
    a = entry(Provider.ANILIST, progress=3)
    result = run(a, None)
    assert result.patches == () and len(result.proposals) == 1
    proposal = result.proposals[0]
    assert proposal.before_target is None and proposal.before_source == a
    assert dict(proposal.changes)["progress"] == 3
    assert result.review_required


def test_initial_existing_scores_are_never_selected_automatically():
    result = run(entry(Provider.ANILIST, score=70), entry(Provider.MAL, score=80))
    assert not result.patches and not result.proposals
    assert any(c.field == "score" and c.reason == "initial_difference" for c in result.conflicts)


def test_complete_equal_initial_entries_still_need_baseline_acknowledgement():
    result = run(entry(Provider.ANILIST), entry(Provider.MAL))
    assert result.review_required and not result.patches and not result.conflicts


@pytest.mark.parametrize("side", [Provider.ANILIST, Provider.MAL])
def test_one_sided_progress_change_has_minimal_patch(side):
    old = baseline()
    a, m = old.anilist, old.mal
    if side == Provider.ANILIST:
        a = replace(a, state=replace(a.state, progress=3))
    else:
        m = replace(m, state=replace(m.state, progress=3))
    result = run(a, m, old)
    assert len(result.patches) == 1 and not result.conflicts
    patch = result.patches[0]
    assert dict(patch.changes) == {"progress": 3}
    assert patch.target == (Provider.MAL if side == Provider.ANILIST else Provider.ANILIST)


def test_different_independent_fields_merge_without_touching_other_fields():
    old = baseline()
    result = run(
        replace(old.anilist, state=replace(old.anilist.state, progress=2)),
        replace(old.mal, state=replace(old.mal.state, score=70)),
        old,
    )
    assert {p.target: dict(p.changes) for p in result.patches} == {
        Provider.MAL: {"progress": 2},
        Provider.ANILIST: {"score": 70},
    }


def test_same_update_on_both_sides_is_noop():
    result = run(entry(Provider.ANILIST, progress=2), entry(Provider.MAL, progress=2), baseline())
    assert not result.patches and not result.conflicts


def test_conflicting_rating_changes_require_review():
    result = run(
        entry(Provider.ANILIST, score=70), entry(Provider.MAL, score=80), baseline(score=60)
    )
    assert not result.patches and any(c.field == "score" for c in result.conflicts)


def test_concurrent_different_progression_fields_are_grouped_conflict():
    result = run(
        entry(Provider.ANILIST, status=Status.COMPLETED),
        entry(Provider.MAL, progress=5),
        baseline(),
    )
    assert not result.patches and any(
        c.reason == "concurrent_progression" for c in result.conflicts
    )


def test_lossy_score_needs_explicit_exact_value_decision_and_keeps_echo_stable():
    old = baseline(score=60)
    a, m = entry(Provider.ANILIST, score=75), old.mal
    result = run(a, m, old)
    assert not result.patches and any(c.reason == "lossy_score" for c in result.conflicts)
    decision = ScoreDecision(pair(), mapping(), 75, 80)
    approved = run(a, m, old, score_decisions=(decision,))
    assert dict(approved.patches[0].changes) == {"score": 80}
    acknowledged = Baseline(pair(), mapping(), a, entry(Provider.MAL, score=80))
    echoed = run(acknowledged.anilist, acknowledged.mal, acknowledged)
    assert not echoed.patches and not echoed.conflicts
    changed = run(entry(Provider.ANILIST, score=76), m, old, score_decisions=(decision,))
    assert not changed.patches and any(c.reason == "lossy_score" for c in changed.conflicts)


@pytest.mark.parametrize("side", [Provider.ANILIST, Provider.MAL])
def test_absence_never_plans_delete_or_automatic_resurrection(side):
    old = baseline()
    result = run(
        None if side == Provider.ANILIST else old.anilist,
        None if side == Provider.MAL else old.mal,
        old,
    )
    assert not result.patches and not result.proposals
    assert any(c.reason == "entry_absent" for c in result.conflicts)


def test_incomplete_either_inventory_blocks_every_patch():
    left, right = inventories(entry(Provider.ANILIST), entry(Provider.MAL))
    for a, m in [(replace(left, complete=False), right), (left, replace(right, complete=False))]:
        result = plan_reconciliation(pair(), a, m, (mapping(),), (baseline(),))
        assert (
            result.blocked == ("incomplete_inventory",)
            and not result.patches
            and not result.proposals
        )


def test_relinked_generation_invalidates_baseline_and_preconditions():
    old = baseline()
    newpair = replace(pair(), mal=replace(pair().mal, generation=2))
    a, m = inventories(old.anilist, old.mal, owner=newpair)
    result = plan_reconciliation(newpair, a, m, (mapping(),), (old,))
    assert result.blocked == ("baseline_identity_changed",)
    patch = run(entry(Provider.ANILIST, progress=1), old.mal, old).patches[0]
    assert not preconditions_match(patch, newpair, patch.before_source, patch.before_target)


def test_create_and_update_require_exact_source_and_destination_rereads():
    proposal = run(entry(Provider.ANILIST, progress=2), None).proposals[0]
    assert preconditions_match(proposal, pair(), proposal.before_source, None)
    assert not preconditions_match(proposal, pair(), proposal.before_source, entry(Provider.MAL))
    assert not preconditions_match(
        proposal, pair(), replace(proposal.before_source, revision="changed"), None
    )
    old = baseline()
    patch = run(entry(Provider.ANILIST, progress=2), old.mal, old).patches[0]
    assert preconditions_match(patch, pair(), patch.before_source, patch.before_target)
    assert not preconditions_match(
        patch, pair(), patch.before_source, replace(patch.before_target, revision="changed")
    )


def test_progress_decrease_and_score_clear_require_review_unless_explicit_rewatch():
    old = baseline(progress=10, score=70)
    result = run(entry(Provider.ANILIST, progress=1, score=None), old.mal, old)
    assert not result.patches and {c.reason for c in result.conflicts} >= {
        "progress_decrease",
        "explicit_clear",
    }
    a = entry(Provider.ANILIST, progress=1, score=70, rewatching=True)
    result = run(a, old.mal, old)
    assert dict(result.patches[0].changes) == {"progress": 1, "rewatching": True}


def test_partial_dates_preserve_precision_without_inventing_month_or_day():
    date = FuzzyDate(2020)
    result = run(entry(Provider.ANILIST, started_at=date), entry(Provider.MAL), baseline())
    assert dict(result.patches[0].changes) == {"started_at": date}
    assert date.month is None and date.day is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Identity(True, Provider.MAL, 1, 1),
        lambda: Identity(1, Provider.MAL, 0, 1),
        lambda: Identity(1, Provider.MAL, 1, 0),
        lambda: Pair(pair().anilist, replace(pair().mal, user_id=2)),
        lambda: ExactMapping(11, 22, 23),
        lambda: ExactMapping(11, 22, 22, "MANGA"),
        lambda: State(progress=-1),
        lambda: State(score=0),
        lambda: State(score=True),
        lambda: State(repeat=1001),
        lambda: State(status=Status.COMPLETED, rewatching=True),
        lambda: FuzzyDate(2021, 2, 29),
        lambda: FuzzyDate(None, 2),
        lambda: Inventory(pair().anilist, (entry(Provider.ANILIST), entry(Provider.ANILIST))),
        lambda: Inventory(pair().mal, (entry(Provider.MAL, score=75),)),
        lambda: ScoreDecision(pair(), mapping(), 75, 40),
    ],
)
def test_invalid_typed_inputs_fail_closed(factory):
    with pytest.raises(ValueError):
        factory()


def test_unverified_identity_wrong_inventory_and_mapping_collision_fail_closed():
    p = replace(pair(), mal=replace(pair().mal, verified=False))
    a, m = inventories(entry(Provider.ANILIST), entry(Provider.MAL), owner=p)
    assert plan_reconciliation(p, a, m, (mapping(),)).blocked == ("unverified_identity",)
    a, m = inventories(entry(Provider.ANILIST), entry(Provider.MAL))
    outsider = replace(a, identity=replace(a.identity, user_id=2))
    with pytest.raises(ValueError):
        plan_reconciliation(pair(), outsider, m, (mapping(),))
    with pytest.raises(ValueError):
        plan_reconciliation(pair(), a, m, (mapping(), ExactMapping(11, 33, 33)))


def test_unmapped_entries_are_reported_without_guessing_titles_or_patches():
    a, m = inventories(Entry(99, State()), None)
    result = plan_reconciliation(pair(), a, m, (mapping(),))
    assert (
        result.unmapped == ((Provider.ANILIST, 99),) and not result.patches and not result.proposals
    )


def test_identity_and_inventory_defaults_never_assert_verified_or_complete():
    identity = Identity(1, Provider.ANILIST, 101, 1)
    assert identity.verified is False
    assert Inventory(identity, ()).complete is False


def test_progress_conflict_withholds_whole_progression_patch_but_allows_rating():
    old = baseline(progress=10, repeat=2)
    a = entry(Provider.ANILIST, status=Status.PLANNING, progress=0, repeat=1, score=80)
    result = run(a, old.mal, old)
    assert {c.reason for c in result.conflicts} >= {"progress_decrease", "repeat_decrease"}
    assert len(result.patches) == 1 and dict(result.patches[0].changes) == {"score": 80}


def test_all_unknown_date_is_unset_not_a_fabricated_date_value():
    with pytest.raises(ValueError):
        FuzzyDate()


def test_lossy_initial_create_is_only_a_review_proposal_after_exact_decision():
    a = entry(Provider.ANILIST, score=75)
    result = run(a, None)
    assert not result.patches and not result.proposals
    assert result.conflicts[0].reason == "lossy_score"
    result = run(a, None, score_decisions=(ScoreDecision(pair(), mapping(), 75, 70),))
    assert not result.patches and dict(result.proposals[0].changes)["score"] == 70


def test_unmapped_inventory_requires_review_and_remains_immutable():
    from dataclasses import FrozenInstanceError

    a, m = inventories(Entry(99, State()), None)
    result = plan_reconciliation(pair(), a, m, (mapping(),))
    assert result.review_required
    with pytest.raises(FrozenInstanceError):
        a.complete = False


def test_conflicting_duplicate_score_decisions_are_rejected():
    old = baseline(score=60)
    with pytest.raises(ValueError, match="Ambiguous"):
        run(
            entry(Provider.ANILIST, score=75),
            old.mal,
            old,
            score_decisions=(
                ScoreDecision(pair(), mapping(), 75, 70),
                ScoreDecision(pair(), mapping(), 75, 80),
            ),
        )


def test_mapping_drift_cannot_reuse_a_baseline_or_produce_a_guess():
    old = baseline()
    a, m = inventories(old.anilist, old.mal)
    with pytest.raises(ValueError, match="mapping changed"):
        plan_reconciliation(pair(), a, m, (ExactMapping(11, 33, 33),), (old,))
