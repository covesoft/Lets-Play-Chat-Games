# Durable later-round ordinary flow

`storage.ordinary_flow.OrdinaryFlow` coordinates a schema-3 unit of work for
already-playing ordinary sessions. It persists frozen offers, publication facts,
selection, announced cutover, phrase usage, and old-round closure boundaries.
It performs no Discord I/O. The live shell still does not invoke gameplay.

This is a later-round foundation: lobby/opening admission, adaptive warnings,
picker eligibility/rotation, full refresh/held/revival, mode flow, startup recovery,
award/credit resolution, cleanup, and delivery workers remain separate work.

## Transaction and receipt contract

```python
from lets_play.storage.ordinary_flow import OrdinaryFlow

# store is a long-lived game store; frame is an authoritative server envelope.
async with store.transaction() as unit:
    result = await OrdinaryFlow(unit).prepare_offer(frame, hand)
# Only here are the result and queued output committed.
```

Every mutation, chat/reaction ingress, and timer uses a positive, contiguous
`EventEnvelope.sequence` in one trusted server dispatcher per session, starting
at 1. `received_at` is the server's receipt time and must be nondecreasing; it is
not a client-selected or historical message timestamp. Same-time frames are
ordered by sequence. The database rejects gaps before applying anything and
recognizes older sequence deliveries as replays. `watermark(session_id)` exposes
the persisted sequence/time for reconciliation. Do not allocate sequence numbers
concurrently by independently reading this watermark.

The future dispatcher must enqueue a timer in the same lane as chat/control
receipts. A timer cannot skip an already-numbered earlier message. `ingest(event)`
advances any due cutover before evaluating that receipt under its owner rules.
Old messages ahead of the timer therefore finish in the old round; a message at
or after an announced boundary receives the new rules. `GameUnitOfWork.apply()`
refuses direct scoring once this session has a flow watermark. Use flow ingress,
including for ignored bot/system traffic admitted to the lane. Retrying a new
receipt of an already-scored source can advance the watermark without scoring it
again; inspect the separate `score_status` in the `INGESTED` result.

These checks enforce the supplied logical order; they do not implement a Gateway
queue, event receipt allocator, or scheduler. A gap/clock regression requires
explicit reconciliation. Never skip/drop a judged event to make a timer run.
Units retain the store's bounded admission, timeout and rollback-only rules.

## Publication and selection

| Operation | Durable result |
| --- | --- |
| `prepare_offer(frame, hand)` | Freeze three proven options/picker, reserve one open offer, retain old rules/round, queue an offer-output intent |
| `publish_offer(frame, offer_id, message_id=...)` | Record an actual successful publication acknowledgement and start the 60-second deadline once |
| `choose(frame, offer_id, incoming_id, outgoing_id=...)` | Reauthorize the picker, validate deadline/snapshot/usage and save partial or complete selection |
| `timeout(frame, offer_id, rng=...)` | At expiry, uniformly choose among still-legal templates, then among its legal targets |
| `publish_activation(frame, offer_id, message_id=...)` | Record acknowledgement of the announcement of complete rules and its stored future effective time |
| `tick(frame)` / `ingest(event)` | Apply the due acknowledged transition once, or request recovery when its promise cannot be honored |

Use the corresponding typed envelope kinds (`OFFER_PREPARE`, `OFFER_PUBLISHED`,
`CHOICE_SUBMIT`, `CHOICE_TIMEOUT`, `ACTIVATION_PUBLISHED`, `TIMER`). Publication
acknowledgements are server-observed delivery facts. Preparing a row, reading an
output intent, or obtaining a mock send result is not actual publication evidence.

`OfferRecord.choice_deadline` is `None` before publication. Its frozen hand still
contains a proposal deadline for the pure hand API; do not schedule that proposal
as a public timeout. On acknowledgement it becomes exactly publication time plus
60 seconds, retaining its token. Repeated acknowledgements cannot change the
message ID or restart the clock. Partial target selection and reopen preserve it.

Once an incoming card has been selected, a later timeout retains it and completes
only a missing target. Target count never adds template tickets. Human choice and
timeout completion provenance are retained for future award predicates; no award
is granted here. A wholly automatic timeout remains distinguishable from a human
incoming-card selection whose target timed out.

If no still-legal saved completion remains, timeout returns `UNAVAILABLE` without
inventing a substitute, consuming a phrase or removing a rule. Complete three-card
refresh with the old deadline and the held fallback are not implemented here;
the future engine must handle this refusal before exposing gameplay.

## Announced cutover and uncertainty

A completed selection prepares one immutable activation with a stored effective
time at least five seconds ahead, rounded up to a whole UTC second. The future
publisher must announce that exact time and the complete incoming rules. This is
an old-rule enforcement interval: there is no exemption/grace period. Publication
lead is an engineering constant, not a gameplay pacing promise.

Activation acknowledgement must be recorded strictly before the boundary. The
same ordered unit that crosses that boundary atomically records one old-round
closure, changes rules/round/ruleset ownership, starts the new round at the
announced time, consumes the selected phrase, and marks the offer/activation done.
Late timer execution does not reset that start time. Selection-period scores stay
in the old round. Replays and competing ordered ticks cannot close it twice.

If acknowledgement is missing/late, ownership is stale, or the selected result is
no longer provable/available, the transition does not silently enforce new rules.
It preserves the old round/configuration, requests a recovery pause for a playing
owner, retires superseded pending outputs, and queues one recovery-notice intent.
An existing pause reason is preserved; an ended owner never reopens or receives
a queued recovery notice. `publish_recovery(...)` records notice acknowledgement,
but does not resume, restart clocks, or apply the abandoned change.

An unknown network outcome can still have produced a visible post. Internal
exactly-once guards do not promise exactly-once Discord delivery. The future
publisher must reconcile known messages and honor capability/recovery policy.
`known_message_id(...)` retrieves saved identities for reuse/edit/reconciliation.
No delivery leases, retry/backoff worker or send-result reconciliation is supplied.

## Data and remaining boundary

Offer JSON is allowlisted/versioned configuration: owner, rule/parameter snapshots,
picker, three incoming choices/allowed targets, deadline and parser/evaluator pins.
It stores no generated witness, raw chat, media or arbitrary output payload.
Witnesses are recomputed by actual validators when loading/revalidating it.
Timestamps are exact UTC integer microseconds; no float conversion is used.

`used_keys(session_id, candidate_keys)` is bounded to 15 normalized candidate keys,
so a bank/offer check need not load all game usage into RAM. Phrase usage commits
only with activation, remains after replacement and is scoped to the session.
Output reads are bounded to 100 identifiers and exclude acknowledged/retired/
ended work. Live rows use foreign-key cleanup; no purge service is implemented.

`nr_round_closures` is a durable pending-awards boundary, not completed badge,
streak, picker, global-game-credit or profile resolution. Score/offer/provenance
evidence stays available. Future resolution must commit idempotently before purge.
No per-game history service is created.

Reopening a database handle is tested. A disruptive bot/Gateway restart is a
different event: the future startup coordinator must enter explicit recovery,
announce it and apply its resume policy before dispatching ordinary receipts.
Loading these records alone does not authorize silent resume or prove observed
mode survival. Opening choices and modes are intentionally outside this facade.

Migration 3 appends these records to schema 2 and preserves old score rows/unknown
start times. Stop the bot and make a verified SQLite backup before upgrading.
