# Personal tracker reconciliation core

`omakase.plus.tracker_reconciliation` is a pure planning module for a user's own
AniList and MyAnimeList identities. It performs no network, database, token,
provider mutation, scheduling or list deletion. It is not a connected tracker
integration. It does not implement provider wire fields, OAuth, refresh/revocation
or omission/clear semantics. The separate [personal tracker adapter](myanimelist-bridge.md)
provides the current connection, preview/apply and bounded-worker workflow.

## Inputs and authority

- `Identity`: local user, typed provider, positive numeric remote identity and
  connection generation. `verified` defaults false. A future authenticated adapter
  must prove numeric ownership; a username, public list or XML upload cannot.
- `Pair`: one AniList identity and one MAL identity belonging to the same local
  user. Household/media permissions confer no tracker permission.
- `ExactMapping`: AniList media ID plus MAL ID and matching observed `idMal` for
  ANIME. The caller must obtain authentic exact mapping evidence. No title matching
  or season/franchise arithmetic is performed.
- `Inventory`: immutable tuple of at most10,000 unique normalized entries.
  `complete` defaults false. A future adapter must prove its bounded traversal
  completed across all relevant statuses/pages; error, private, partial or404 data
  is not an empty complete list. Complete traversal is not an atomic remote snapshot.
- `Entry`: exact media ID, `State` and optional bounded opaque revision. `State`
  contains canonical status, progress, score, repeat count, current rewatch flag and
  partial dates. No raw payload, notes, title or custom lists are required.
- `Baseline`: exact acknowledged normalized entries for both sides, tied to pair
  and mapping. Only the future coordinator may establish/advance it after reviewed
  reconciliation and successful readbacks. This module does not manufacture an
  approval or advance a baseline.

A connection generation or numeric identity change blocks existing baselines;
wrong-account inventories, mapping collisions and changed baseline mappings reject.
Old identities cannot authorize a relinked account's mutations. Omitted/unknown
fields must not be synthesized as known zeros by adapters: normalize only after
retrieving the complete required field set.

## Planning

```python
from omakase.plus.tracker_reconciliation import (
    Entry, ExactMapping, Identity, Inventory, Pair, Provider, State,
    plan_reconciliation,
)

# Only an authenticated identity adapter may assert these verification flags.
pair = Pair(
    Identity(1, Provider.ANILIST, 101, 1, verified=True),
    Identity(1, Provider.MAL, 202, 1, verified=True),
)
mapping = ExactMapping(11, 22, observed_id_mal=22)
left = Inventory(pair.anilist, (Entry(11, State(progress=3)),), complete=True)
right = Inventory(pair.mal, (), complete=True)
preview = plan_reconciliation(pair, left, right, (mapping,))
assert preview.patches == ()  # First sync is always review-only.
assert preview.proposals[0].before_target is None
```

`ReconciliationPlan` separates `patches`, review-only `proposals`, per-field
`conflicts`, `unmapped` IDs and whole-plan `blocked` reasons. It remains immutable.
No plan object itself authorizes or executes a provider mutation.

Without a baseline, a one-sided entry produces a review-only create proposal.
Existing entries with differences produce conflicts; no rating/status winner is
chosen. Equal entries still require initial acknowledgement. Missing mappings are
reported for review and never guessed. Incomplete inventories or unverified
identities produce no proposals or patches.

With a baseline, each field is compared with its own side's acknowledged value:
unchanged is a no-op; a one-sided edit produces a minimal patch to the other side;
the same new value on both sides needs no write; divergent concurrent edits require
review. Independent score/date/progress fields can merge, but concurrent divergent
status/progress/rewatch changes conflict as a coupled group. Ambiguous progress or
repeat-count decreases withhold that direction's entire progression patch. A
progress reset accompanied by a new explicit rewatch cycle can propagate. Scores
and dates clearing to unset require explicit review rather than accidental0/null.

An entry missing after an established baseline always produces `entry_absent`
review, including if both entries disappeared. There is no deletion action and no
automatic resurrection. Other independently safe mapped entries may still plan.

## Score and date precision

Scores are normalized integer1–100 or `None` for unscored; adapters must convert
provider-specific zero/unset semantics explicitly. MAL-normalized inventories must
have scores representable in ten-point increments. An AniList75 score cannot
silently become MAL8. A `ScoreDecision` binds the current pair, exact mapping,
source value and explicit nearest representable target, e.g75→70 or80. A changed
source value or relink invalidates that decision; conflicting duplicate decisions
reject. Initial lossy creates remain proposals even with a decision, never patches.

Baselines retain each side's acknowledged representation: AniList75 and MAL80 can
be an explicitly accepted baseline. The next unchanged observation produces no
reverse patch; this prevents75→80→75 quantization loops. Establishing such a
baseline still requires actual coordinator approval/readback, not just this type.

`FuzzyDate(2020)` preserves year-only precision; it never invents January1.
Missing dates are `None`; all-unknown date objects, impossible dates and invalid
precision are rejected. MAL date capabilities are not assumed: a future adapter
must reject or queue unsupported partial dates instead of silently coercing them.
Rewatch flag/count remain distinct. Canonical rewatching requires current status;
provider-specific status/count/date semantics must be qualified before conversion.

## Exact preconditions and the external race

Every `PlannedPatch` carries both identities/generations, exact mapping, minimal
canonical changed fields, exact observed source entry and exact destination entry.
Creates require destination absence. `preconditions_match(patch, pair, source,
target)` compares fresh exact normalized rereads, including opaque revision when
available. A destination created after preview or either entry changing makes the
precondition false. The future executor must reread before creates AND updates.

A true comparison is neither authorization nor remote atomic compare-and-set.
The coordinator must verify current consent, plan hash, leases and connection
generations, convert only qualified supported fields, and preserve omitted fields.
Without provider conditional writes there remains a read-check-write race. Do not
promise that an external concurrent edit can never be overwritten. After a timeout
or ambiguous outcome, reread before retry; acknowledge only actual desired readback,
otherwise conflict/defer. Local deduplication cannot guarantee remote exactly once.

Existing local-watch writers must eventually share serialization and compare/review
rules with this bridge. Incoming completions must not trigger media removal,
acquisition, score replacement or outgoing echo loops. First-sync approval,
connection/outbox persistence, provider rate/error contracts and actual live
readbacks remain separate unfinished work. AniList refresh tokens are unsupported;
this pure module has no credentials or OAuth code.

## Verification

Run with the project's existing verified interpreter, without provider access:

```text
python -m pytest tests/plus/test_tracker_reconciliation.py -q
python -m ruff check src/omakase/plus/tracker_reconciliation.py tests/plus/test_tracker_reconciliation.py
python -m ruff format --check src/omakase/plus/tracker_reconciliation.py tests/plus/test_tracker_reconciliation.py
```

The dedicated fixtures cover zero-patch initial previews, complete/incomplete
inventories, exact mappings, owner/generation isolation, unchanged and one-sided
updates, concurrent field conflicts, coupled progression/rewatch handling, lossy
score decisions and stable echoes, partial dates, absence without deletion,
create/update/source reread preconditions, immutability and invalid bounded inputs.
They do not prove a working MAL connection, provider wire contract or remote CAS.
