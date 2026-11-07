# New Rules general progression

`storage.progression.Progression` is an offline application facade over an active
`GameUnitOfWork`. It reads derived score/choice/closure evidence, never chat or
Discord objects. Results and notice intents are provisional until the enclosing
transaction commits. It performs no Discord I/O.

## Credit and profiles

`GameUnitOfWork.apply()` composes ordinary participation credit with the accepted
score event. At five session-wide ordinary posts, it increments the actor's
**New Rules games played** and flips their live `game_count_credited` flag in the
same transaction. Whispers, reactions and mode observations do not fill that
ordinary threshold. Zero-applicability ordinary posts and offending posts do.
Qualifying again, reopening, and duplicate event/source receipts cannot add a
second credit. Concurrent independent sessions may each credit the same actor.

`credit_ordinary(session_id, actor_id=None)` reconciles that same saved predicate;
it accepts no caller-supplied success flag. This also recovers qualifying live
schema-3 aggregates at round resolution. Familiar Face membership commits with
the increment reaching ten. Existing badge membership never blocks credit.

`profile(actor_id)` returns `NewRulesProfile`: actor ID, New Rules-only count and
sorted stable general badge keys. An unknown actor reads as zero/empty without
creating a record. Global rows contain no session, guild, earning date or repeat
counter. They survive gameplay foreign-key cleanup. Future games must use their
own aggregates; this count is not an application-wide total.

Schema 4 supports the five general badge keys only. Adding other predicates and
keys requires explicit implementation/migration; the table is not an arbitrary
public award-grant API. Mode-based gameplay credit and the twenty theme/mode badge
predicates are not implemented by this facade.

## Actual round closure

`resolve_round(session_id, round_id)` accepts an old round only when retained
closure/offer/activation evidence proves successful activation. Open, selected,
held or revival/recovery-paused rounds do not close merely because resolution
was requested. The facade returns `RESOLVED`, `ALREADY_DONE`, `NOT_CLOSED` or
`UNKNOWN_SESSION` and rejects out-of-order real closures with
`ResolutionOrderError`. It never fabricates a missing closure.

OrdinaryFlow calls the resolver after preparing its complete cutover, in that
same write unit. Closure, new rules/owner, phrase consumption, general progress
and badges either commit together or roll back together. Rulemaker requires a
successfully activated deliberate incoming-card choice; a human choice with
automatic target completion qualifies, a fully automatic timeout does not.
Picker-only badge membership can exist with zero games played.

Resolution uses immutable old-round counters. Spotless requires five judged
ordinary observations and zero raw ordinary/mode/reaction faults. Personal
activity-qualified rounds and Party Animal use ordinary participation, allowing
unjudged/offending posts without manufacturing clean accuracy. A missed or
nonqualifying round after enrollment resets the current clean run; best remains.
Participants first seen in a later round are untouched by earlier resolutions.
Party Animal requires participation in three qualifying completed rounds with
at least five qualified actors in each.

Set-based indexed SQL applies the whole round atomically without loading its
roster/history into Python. It retains SQLite's single-writer constraint and the
store's bounded admission/timeout, not a certified production throughput ceiling.

## Ending and notices

`finalize_ended(session_id)` requires an already terminal owner and every earlier
closure resolved. Compose it in the same unit as a trusted terminal owner update;
it does not check manager permissions, allocate an ordered receipt or end a game.
The future ending service must do those things. It resolves the final round once,
then evaluates Very Careful against final personal activity-qualified N and the
best clean run. A pause cannot invoke this path. Repeated finalization is inert.
No early Very Careful award is made while N can still grow.

Streak milestones are identifier-only `nr_streak_notices`, keyed by round/actor,
not global badges or repeat counters. A later clean run can reach three again.
`pending_streak_notices(limit=100)` reads at most 100; acknowledge only after the
containing summary/notice actually succeeds. Acknowledgement is idempotent and
monotonic notice IDs protect later work from an old acknowledgement. No summary
publisher, external exactly-once delivery, backoff or TTL worker is supplied.

## Upgrade and recovery

Back up the stopped database before schema 4. Migrations 1–3 are unchanged.
Frozen empty/populated released-schema-3 fixtures test forward upgrade and rollback.
Existing exact score rows, choice provenance, original deadlines, watermarks and
unknown round start times survive. No credit is issued by migration itself.

Legacy participants get their first known round from retained round-score rows.
Absent evidence stays NULL and resolution refuses it; it never guesses that the
actor participated from round one. The `general-1` progression version is pinned
per live session; incompatible state requires explicit recovery.

For legacy unresolved closures, read `pending_rounds(session_id, limit=100)` and
resolve each in returned creation order, within ordinary short write units,
before allowing further cutovers. Repeat the query until empty. A new cutover
will refuse an earlier unresolved gap. This is a reconciliation API, not a full
disruptive-restart coordinator or authorization to silently resume gameplay.

Do not purge live evidence until all required general and future mode/theme
finalization plus final output handling are complete. This increment tests FK
survival of global data; it does not implement the purge, 24-hour final-output
retention or owner-operated erasure workflow. An erasure workflow must also
invalidate pending live award tasks to prevent recreation.
