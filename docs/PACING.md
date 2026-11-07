# Ordinary warning and succession API

`storage.pacing_flow.RoundPacing` coordinates one ordinary advancement in a round.
It uses the same unit and server receipt lane as OrdinaryFlow. No network I/O,
live scheduler, Discord publisher, opening lobby, held refresh or full revival
workflow is implemented here. Facade results are provisional until commit.

## Register real start facts

```python
async with store.transaction() as unit:
    status = await RoundPacing(unit).register_round(
        frame,
        estimated_players=3,
        rng=rng,
        started_at=actual_activation_time,
        previous_picker_id=known_successful_opening_picker,
    )
```

Use `PACING_REGISTER`, a new positive contiguous server receipt. A known saved
start must match; an unknown start may be recorded only before any scored/whisper/
reaction receipt in that round. A used legacy round with an unknown start requires
explicit recovery. Registration never guesses a start from its current receipt.
Repeated registration cannot change the estimate, start or saved random seed.
The trusted opening service must supply the actual joined-roster estimate and
successful opening picker, when known; registration is not opening admission.

The target is frozen. Ordinary human posts increment an indexed aggregate once,
including offending/media posts; whispers, modes, reactions and bots do not.
Once the target is met it stays latched. `record(session_id).next_deadline` is the
next scheduler boundary: no warning before start+270s, target-triggered warning
thereafter, or fallback at start+870s. Preparing a warning changes advancement
state and queues one identifier-only output. It starts no publication clock.

`tick(frame)` uses `TIMER`; OrdinaryFlow ingress also checks due pacing before
scoring and observes target progress after accepted scoring. A scheduler can run
these boundaries without waiting for another message. Neither API implements
that live scheduler or permits gaps/backdating in the receipt lane.

## Publication and immutable snapshot

`pending_outputs(limit=100)` exposes at most 100 currently applicable warning,
revival or recovery intents. Stale owners/rounds and ended sessions cannot expose
an old warning as ready. Render warning text from `new_rules.pacing.WARNING_TEXT`.
Only a real successful publication permits `publish_warning(frame, round_id,
message_id=...)` using `WARNING_PUBLISHED`. Its receipt establishes exactly thirty
seconds, even past the normal fifteen-minute selection window. Known same-message
ACK retries do not restart it; an identity conflict fails the unit. Use
`known_warning_message_id` to reuse/reconcile an acknowledged warning.

The first ordered receipt at or after that absolute deadline freezes candidates
**before scoring that receipt**. Thus pre-cutoff receipts affect eligibility;
an exactly-at-cutoff fifth post is still scored in the old round but cannot join
the already-frozen picker pool. A late timer cannot overtake queued earlier chat.
Later messages keep updating final old-round results until announced activation,
without changing the saved candidate measurements or selected picker.

The snapshot uses five ordinary posts and exact actual normalized compliance,
including mode/reaction opportunities, without rounded or adjusted-rank comparisons.
All tied worst measured actors are excluded only when survivors remain and there
are faults. Unrated activity-qualified actors remain candidates. All-zero-fault,
all-unrated, all-tied and sole-player pools are not emptied by the filter.
`candidates(session_id, round_id, after_actor=0, limit=100)` provides keyset pages
of the frozen derived evidence, not a transcript or roster/member cache.

## Rotation and activation

Selection is a lazy shuffled rotation: uniformly sample among currently eligible,
unserved survivors in the current cycle; begin a new cycle only when necessary.
Avoid the previous picker when another legal successor exists. Inactive/excluded
actors retain their service history; newcomers enter the unserved pool. A known
successful opening picker begins as already served. A sole eligible actor can
choose again. A committed injected seed, version-pinned deterministic replay and
bounded-page reservoir sampling avoid reshuffling on duplicate/cancelled timers.

Selection reserves a picker but does not consume their turn. Registered rounds
cannot prepare an arbitrary/pre-warning picker offer. OrdinaryFlow rechecks the
frozen picker and continues to authorize offer/card/target selection. Only a
successful activation records rotation service and the previous picker. Failed
publication, abandoned selection or recovery cannot consume it. This fairness
effect is separate from Rulemaker's deliberate-choice predicate.

Activation composes closure, general resolution, rule/phrase changes, rotation
service and the next ordinary pacing record in one write unit. The new clock is
the announced effective time, including after a late timer. Its normal frozen estimate
is the final old round's five-post count, including players qualifying during
choice after the picker snapshot. No completed-round evidence is discarded.

An empty five-post pool enters a revival pause and queues one invitation intent,
preserving the open round/rules/scores. Schema 7 EvolutionFlow owns the invitation
clock, quorum, expiry and held restoration; see [EVOLUTION.md](EVOLUTION.md). A stale
playing warning owner requests recovery; an existing pause reason is preserved
and an ended owner never reopens. Recovery/capability pauses never resume automatically.

## Persistence and limits

Migration 5 appends to released schema 4, backfills ordinary round counts from
retained scores, and leaves legacy starts/profiles/scores/choice clocks unchanged.
It does not register or resume old sessions. Migration 7 upgrades the known
`ordinary-pacing-1` pin to `ordinary-pacing-2` and adds per-round attempts. Old output
IDs, publication times and candidate evidence become attempt 1. Fresh attempts
archive the retired pacing projection; candidate pages default to the current
attempt, with explicit `attempt=` for retained evidence. `PacingRecord` and pending
outputs expose the exact attempt. `publish_warning(..., attempt=...)` must name it;
its default is **1**, never an alias for the latest attempt. An old ACK therefore
cannot start or extend a restored warning. Successful explicit revival freezes
the next-round estimate at its three returners rather than cumulative old traffic.

Candidate processing uses pages of at most 100 and exact fractions; global/member/
message object caches are not introduced. SQLite still has one writer, and the
atomic snapshot can take longer with a large participant set. Tests establish
correctness, not a guild-count or production throughput guarantee. Existing store
backpressure, cancellation, rollback-only and migration-backup rules remain.

Unknown external send outcomes still require publisher reconciliation; a local
intent/ACK fixture is not a real Discord delivery or exactly-once visible post.
No startup resume policy, retries/leases, summary sender or retention service is
supplied by this module.
