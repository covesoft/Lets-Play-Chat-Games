# Offline ordinary-subset hand API

The package can prepare legal ordinary additions/replacements, construct a
complete three-template offer, and revalidate a frozen choice. It performs no
I/O, publication, activation, phrase consumption, round closure, or awards.
The live adapter enables the Drawing Conclusions ticket for eligible later
hands. The full catalog and other Breaking Moments are still pending.

## Implemented catalog

`catalog.CARD_SPECS` is immutable metadata for these 37 implemented validators:

| ID | Card | Check / mechanical group |
| --- | --- | --- |
| NR001 | Poker Face | No Unicode or custom emoji in message text (Expression) |
| NR002 | Moving Pictures Prohibited | No GIF uploads or recognized GIF links (Expression) |
| NR003 | Sticker Shock | No Discord stickers, including posts without captions (Expression) |
| NR004 | Unattached | No files; Expression |
| NR005 | Keep It Here | No shared links except a single GIF link on its own (Expression) |
| NR006 | Expressionless | Prohibited reaction actors; Expression |
| NR007 | Inside Voice | Lowercase mapping; casing slot |
| NR008 | SPEAK UP | Uppercase mapping; casing slot |
| NR009 | No Accounting | No Unicode digits or specified number emoji (Typography) |
| NR010 | Questionable Conduct | ASCII question-mark ending; terminal slot |
| NR011 | Are You Sure? | At least three consecutive terminal ASCII periods; terminal slot |
| NR012 | With Gusto | ASCII exclamation-mark ending; terminal slot |
| NR013 | No Questions | No ASCII question marks; conflicts with NR010 |
| NR014 | Curb The Enthusiasm | No ASCII exclamation marks; conflicts with NR012 |
| NR015 | One Breath | No original CR or LF (Typography) |
| NR016 | Plain Clothes | No supported formatting; independent Typography restriction |
| NR017 | Briefly | At most five words; Brevity |
| NR018 | Use Your Words | At least five words (Brevity) |
| NR019 | One Word Will Do | Exactly one word; Brevity |
| NR020 | Room to Elaborate | At least twenty graphemes (Brevity) |
| NR021 | Small Print | At most forty graphemes (Brevity) |
| NR022 | No Echoes | No repeated normalized word; Vocabulary |
| NR024 | So Agreeable | Distinct first word `yes`; prefix slot |
| NR025 | No Apologies | No whole word `sorry`; Vocabulary |
| NR026 | No Laughing Matter | No `lol`, `lmao`, or `haha`; Vocabulary |
| NR027 | No Doubts | No whole word `maybe`; Vocabulary |
| NR028 | No Excuses | No whole word `because`; Vocabulary |
| NR029 | No Objections | No whole word `but`; Vocabulary |
| NR030 | Selfless Conversation | No `I`, `me`, `my`, or the listed contractions (Vocabulary) |
| NR031 | No Second Person | No `you`, `your`, or listed contractions; Vocabulary |
| NR033 | Please Proceed | Distinct first word `please`; prefix slot |
| NR034 | Roger Wilco | Ending word `over`; separate from terminal punctuation |
| NR035 | The Royal We | Include `we`, `our`, or `us`; Clauses |
| NR036 | Don’t Forget | Frozen phrase; two independent instances |
| NR037 | Decree Mode | Title case words of at least three graphemes (casing slot) |
| NR038 | A Small Pause | Include an ASCII comma (Typography) |
| NR039 | Little Words | Up to six Unicode letters per word (Brevity) |

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
instance/template identity, caps, slots, direct conflicts, parameters, and the
entire resulting state. NR019 conflicts with every Clauses instance, including
one-word phrases. NR010 conflicts with NR013, and NR012 with NR014. The two
bans can coexist, and either required mark can coexist with the other mark's ban.
The witness search composes required prefixes, phrases, inclusion words, and
endings, then applies casing and punctuation. If a required phrase already
contains a Royal We word, the witness can use that same occurrence. Minimum-length rules can add distinct filler words while preserving the required start and ending. The actual evaluator must pass every applicable predicate; only
NR006 is not-applicable to this synthetic message. Empty text, media-only posts,
and whispers cannot establish feasibility.

Pairwise legality is insufficient: two three-word phrases each fit Briefly, but
their six-word combined requirement does not. Conversely, `red duck` and `duck
pond` can share a word and pass No Echoes as `red duck pond`.

`legal_completions(active, incoming)` returns every proven permitted single
removal/addition. At a capped deck, outgoing targets come from that deck; an
exclusive slot targets that slot; a full NR036 allotment targets NR036 instances.
Either card in a direct conflict can replace its counterpart. The same outgoing
instance must satisfy every applicable restriction, and the entire resulting
state must pass. A phrase-specific witness failure grants no removal on its own. Already-active
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
offer. When enabled, Drawing Conclusions contributes a single ticket. It has
one completion that preserves the exact ordinary inventory. Opening hands and
the hand after a mode-led round use ordinary tickets only.

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

`READY` contains a prepared `LegalCompletion`. Repeating pure preparation has no
effects. The schema-3 [ordinary flow](ORDINARY_FLOW.md) can persist later-round
offers and compose selection, publication acknowledgement, replacement, phrase
usage and a once-only old-round closure boundary. Closures retain evidence for
future award resolution. This pure module does not persist or schedule anything.
Mode dealing, complete refresh/held/revival and startup recovery, round/game
credit, and Discord UI remain outside the current implementation.
