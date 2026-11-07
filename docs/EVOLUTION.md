# Offline ordinary evolution

Schema 7 `EvolutionFlow(unit)` handles held play, complete-hand refresh and revival
for ordinary rounds. It composes existing owners, scoring, pacing, rotation and
general progression in the caller's write transaction. Returned effects are
provisional until commit. It does not publish, authorize manager/grant actions,
run timers, or implement the disruptive-startup recovery policy.

## Ordered actions

Every action uses OrdinaryFlow's exact session route, contiguous server receipt
sequence and nondecreasing receipt time. Due activation, pacing and evolution
boundaries run before the action body. A message at a held-idle cutoff therefore
observes the pause; a third revival click at expiry observes the terminal session.
Sequence gaps, late SQL failures and cancellation roll back the entire unit.

| Method | Envelope kind | Purpose |
| --- | --- | --- |
| `recheck` | `ADVANCEMENT_RECHECK` | Persist relevant trusted template/phrase facts; retain a valid offer, refresh an unusable offer, hold, or restore advancement. |
| `prepare_or_hold` | `OFFER_PREPARE` | Build a complete three-template hand for the frozen pacing picker or enter held play. |
| `refresh_offer` | `OFFER_REFRESH` | Revalidate a specific frozen offer using saved facts; a completed refresh returns its stable replacement ID on retry. |
| `publish_revival` | `REVIVAL_PUBLISHED` | Record actual invitation publication and start its original 300 seconds. |
| `join_revival` | `REVIVAL_JOIN` | Count a distinct human returner against the exact invitation ID. |
| `publish_notice` | `HELD_PUBLISHED` / `SILENT_END_PUBLISHED` | Record an actual existing-panel update against its exact token. |

`recheck` takes an optional ordered tuple of at most 15 `PhraseChoice` values,
a frozen implemented-template set, and an authorized session phrase window (5/15).
These are trusted adapter facts, never values taken from button payloads. Slice the
window before filtering used/incompatible phrases. An unknown legacy window stays
unknown; using a nonempty bank requires explicit reconciliation. New first
activations initialize evolution with their actual opening configuration/window.

The adapter must serialize relevant configuration/capability changes through
`recheck` before subsequent choices, timeout or announcement handling. It must use
`prepare_or_hold` after pacing selects a picker and inspect the committed owner
for a replacement offer ID after refresh. OrdinaryFlow continues to own offer
publication, human selection, timeout, activation ACK and scoring ingress.
A selected/announced promise cannot be silently refreshed: an invalid promised
result enters the existing explicit recovery path. Recovery/capability pauses do
not automatically resume when new facts arrive.

## Held and restored advancement

With fewer than three proven distinct templates, hold the same ordinary round,
rules, score ledger and phrase-use scope. Retire unusable controls. There is no
partial hand, new round, rotation service or unfulfilled-choice award. Human
messages continue to score; one held-panel intent is queued per entry. Repeated
scarce configuration updates coalesce. Rechecks are event-driven, not a polling
loop that repeatedly constructs hands.

A restored full hand begins a fresh warning attempt, with a new picker snapshot
at its cutoff. It still respects the original round's 270-second minimum. Its
30 seconds start only with actual warning publication. Pass the exact attempt
from `PacingRecord`/`PacingOutput` to `RoundPacing.publish_warning`; the default
attempt remains 1 for old callers. Old ACKs cannot acknowledge a new attempt.
Retired pacing projections and candidate snapshots retain their attempt identity.
Restoration consumes no round/phrase and does not reset idle history.

A frozen valid hand survives phrase-bank edits and is never rerolled for variety.
An unusable offer is replaced only by a complete three-template hand for the same
picker. If its clock already started, the replacement inherits the original
publication time, deadline token and expiry, including through reopen and delayed
replacement ACK. Its new output records its own message identity. Partial human
selection on a cancelled offer grants no fulfilled turn; the replacement starts
with no chosen incoming. If no clock ever started, the replacement's first actual
publication starts the normal 60 seconds. No full replacement means held play.

## Idle activity and revival

`next_deadline(session_id)` returns the known held-idle or revival timer boundary,
or `None`. It does not infer a deadline from a prepared invitation. Held idle is
900 seconds from the later of the episode's anchor and last newly accepted human
message. Whispers count. Duplicate sources, reactions, bots/webhooks/system
messages, notices and settings do not. Settings-only restore/rehold preserves the
anchor. This detector runs only in ordinary held play, never a deliberate mode.

No five-post successor at a normal cutoff, or held idle, pauses enforcement and
prepares one invitation with a fresh durable ID and seed. Its 300 seconds start
from actual publication, and retry/reopen cannot reset them. Three distinct human
clicks are required; no starter is included automatically. The picker is uniform
among those three returners, an explicit exception to the five-post/worst-result
filter. Clicks create no scores or played-game credit.

Quorum resumes the same open round, prepares a full hand, and freezes the next
round's player estimate at three. No hand means held play in that same round.
Successful explicit revival resets the idle observation anchor without inventing
a message. Paused human message/reaction IDs are guarded against redelivery after
resume; raw message content, attachment names and reaction text are not stored.
Only actual subsequent activation consumes the picker turn and closes/resolves
the round. Manager ending or expiry can instead finalize it once.

At invitation expiry, ending, capacity release and earned general finalization
commit together. `closed_silent` requests an edit/disable of the existing panel,
not a new channel announcement or promotion. O-03's final-result delivery surface
remains an M2 decision. Pending outputs are bounded to 100 and require real
publication ACKs; unknown network outcomes still need publisher reconciliation.

## Migration and remaining work

Stop the bot and make a verified SQLite backup before upgrading. Migration 7
preserves released schema-6 scores, clocks, output IDs and deleted-ID high-water
marks. Existing pacing output/candidate rows become attempt 1. Old migrations
1–6 are unchanged; no dependencies or environment keys are added.

The known pacing pin upgrades to `ordinary-pacing-2`; unknown pins still refuse.
No missing legacy publication time is invented. A schema-6 revival handoff has
no original durable invitation deadline and needs explicit startup reconciliation;
this migration does not guess one or resume it. Reopening a database handle is
not evidence of safe bot/Gateway restart. Full startup recovery, real publishers,
schedulers, mode/theme resolution, retention and erasure services remain separate
work. Foreign-key cascades are a cleanup foundation, not a purge service. The
suite tests offline correctness, not production capacity or live Discord delivery.
