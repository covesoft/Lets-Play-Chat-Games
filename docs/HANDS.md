# Offline ordinary-subset hand API

The package can prepare legal ordinary additions/replacements, construct a
complete three-template offer, and revalidate a frozen choice. It performs no
I/O, publication, activation, phrase consumption, round closure, or awards.
The live shell does not invoke this API. The full catalog and mode-aware deck
are not implemented; every option currently produced is ordinary.

## Implemented catalog

`catalog.CARD_SPECS` is immutable metadata for these 17 implemented validators:

| ID | Card | Check / mechanical group |
| --- | --- | --- |
| NR004 | Unattached | No files; Expression |
| NR006 | Expressionless | Prohibited reaction actors; Expression |
| NR007 | Inside Voice | Lowercase mapping; casing slot |
| NR008 | SPEAK UP | Uppercase mapping; casing slot |
| NR010 | Questionable Conduct | ASCII question-mark ending; terminal slot |
| NR011 | Are You Sure? | Exactly three terminal ASCII periods; terminal slot |
| NR013 | No Questions | No ASCII question marks; independent Typography restriction |
| NR016 | Plain Clothes | No supported formatting; independent Typography restriction |
| NR017 | Briefly | At most five words; Brevity |
| NR019 | One Word Will Do | Exactly one word; Brevity |
| NR022 | No Echoes | No repeated normalized word; Vocabulary |
| NR024 | So Agreeable | Distinct first word `yes`; prefix slot |
| NR025 | No Apologies | No whole word `sorry`; Vocabulary |
| NR027 | Indecisiveness Banned | No whole word `maybe`; Vocabulary |
| NR033 | Please Proceed | Distinct first word `please`; prefix slot |
| NR034 | Roger Wilco | Ending word `over`; separate from terminal punctuation |
| NR036 | Don’t Forget | Frozen phrase; two independent instances |

Mechanical settings are code constants in `catalog.py`:
`CHATGAMES_MAX_ACTIVE_BREVITY_RULES=1`,
`CHATGAMES_MAX_ACTIVE_VOCABULARY_RULES=2`, and
`CHATGAMES_MAX_ACTIVE_DONT_FORGET=2`. They do not depend on premium. Each exclusive
slot has one occupant. There is no total active-rule limit or blanket Typography
cap. Other card IDs are explicitly unsupported, even if a future card would use
the same deck/slot. Predicate/Unicode details are in [PARSER.md](PARSER.md).

## Witnesses and completions

`compatibility.assess_state(instances)` returns an `Assessment`: a `Witness` when
feasibility is established, otherwise a stable rejection reason. It checks
instance/template identity, caps/slots, parameters, and the entire resulting
state. It constructs nonempty ordinary text from required prefix/phrase/ending
words, trying containment/overlap/order alternatives, then applies case/terminal
requirements. The actual evaluator must pass every applicable predicate; only
NR006 is not-applicable to this synthetic message. Empty text, media-only posts,
and whispers cannot establish feasibility.

Pairwise legality is insufficient: two three-word phrases each fit Briefly, but
their six-word combined requirement does not. Conversely, `red duck` and `duck
pond` can share a word and pass No Echoes as `red duck pond`.

`legal_completions(active, incoming)` returns every proven permitted single
removal/addition. At a capped deck, outgoing targets come from that deck; an
exclusive slot targets that slot; a full NR036 allotment targets NR036 instances.
Each result is rechecked in full. A cross-deck conflict cannot retire an unrelated
rule, and a candidate requiring more removals is withheld. Already-active
non-phrase templates do not become redundant draw tickets. Callers must begin
with a valid current state; the hand builder verifies it explicitly.

`WitnessBudget` defaults to 256 words and 128 candidates/order explorations, with
hard bounds on configuration. Parser input remains limited to 20,000 code points.
These are engineering search guards, not player-facing phrase/word limits. An
unproven result may be conservatively withheld with `witness_search_limit` or
`no_witness`; no scored input is truncated and no general satisfiability claim
is made. Witness text can contain private configuration words: it is transient,
excluded from repr, and must not enter logs or generic persistence payloads.

## Offers

```python
from datetime import UTC, datetime, timedelta
from random import Random
from lets_play.new_rules.contracts import Deadline, DeadlineKind
from lets_play.new_rules.hands import PhraseChoice, build_hand

# context is an authoritative EvaluationContext, ordinary/playing or an empty
# opening-choice owner. The future engine supplies its actual original deadline.
now = datetime.now(UTC)
deadline = Deadline("choice-token", DeadlineKind.CHOICE, now + timedelta(seconds=60))
result = build_hand(
    context,
    offer_id="offer-token",
    picker_id=303,
    deadline=deadline,
    rng=Random(7),
    phrase_bank=(PhraseChoice("phrase-id", "red duck", "revision-1"),),
    phrase_draw_limit=5,
)
```

The result contains a complete hand or a blocked reason, never one/two public
options. Every displayed incoming template has at least one proven completion.
The builder samples distinct templates without replacement. NR036 has one ticket
regardless of phrase count; it freezes an eligible phrase only after selecting
that template. Replacement targets likewise do not create extra tickets.
Injected RNG is the sole source of randomness; fixed inputs/seeds reproduce an
offer. No automatic fallback, guaranteed mode, or spontaneous mode timer exists.

The ordered `PhraseChoice` snapshot accepts at most 15 entries. The supplied
session window is 5 or 15: slice **before** filtering used/incompatible wording,
so position six never fills a free-window hole. `used_phrases` is a frozen set of
normalized word-identity tuples (`PhraseChoice.key`); active clauses are included
automatically. Removed/re-added equivalent wording stays used when its identity
is supplied. New games have an independent scope. Draw/preparation never consumes
usage, and empty/exhausted/incompatible windows withhold NR036.

This API does not authorize entitlements, persist usage/settings, validate a full
manager suitability/profanity policy, or load private lists. The future service
must query its durable usage scope for candidate keys and supply the authorized
window; the module maintains no game-history cache.

## Revalidation and remaining integration

`revalidate_choice(hand, context, incoming_id, actor_id=..., now=..., outgoing_id=...)`
rechecks the frozen authorized picker at both steps, then checks saved parser/
evaluator versions, complete owner/snapshot, original absolute deadline,
choice membership, latest supplied template availability/usage, and full result.
It uses the frozen incoming phrase/revision, not a newly edited guild bank or an
old witness as authority. `available_templates` is an internal capability/deck
filter; changing a guild bank affects future deals, not frozen wording.

Multiple legal outgoing targets return `NEEDS_TARGET` until one is selected. A
single clearly displayed target can resolve directly. Both steps retain the
same `Deadline`; the exact expiry is inclusive. An arbitrary target, stale owner,
expired offer, consumed phrase or newly unavailable template cannot silently
select a substitute. Search-budget changes require a fresh proof.
Picker eligibility/rotation is not calculated here; the future engine must supply
the authorized picker ID. The pure helper does not bypass application authorization.

`READY` contains a prepared `LegalCompletion`. Repeating preparation has no
effects. A future transaction service must atomically check/consume the offer,
apply replacement/phrase usage/version changes, resolve the old round once and
coordinate publication before activation. Offers/deadlines are not persisted or
scheduled here. Mode dealing, held/revival timing, complete recovery, round/game
credit, and Discord UI remain outside this increment. Schema stays 2.
