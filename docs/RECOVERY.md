# Offline disruption recovery

Schema 8 `RecoveryFlow(unit)` adds a durable barrier for a disruptive restart or
reconnect. It coordinates existing ordinary, held, revival, lobby and opening
state without sending messages or loading channel history. A handle reopen alone
still does not invoke recovery. The v0.3.13 adapter does enumerate live routes and
enter these barriers before normal dispatch/timers on startup/reconnect. Managers
can inspect a read-only recovery/status pane under `/rules`; live recovery
mutations still permit manager ending only. Proof reconciliation,
notice/read-in and resume remain offline APIs, pending adapter integration.

## Private manager status

`RecoveryStatusStore(unit).read(route, generation=None, family=0, anchor=0,
backwards=False)` reads an exact saved session/guild/channel in a read-only unit.
It returns typed phase/clock/blocker facts without decoding the game context or
exposing saved payloads, phrases, version values or configured assets. Unsupported
versions, modes, prior pauses, missing state and legacy revival remain explicit
blockers. Missing timestamps stay unknown. The opaque generation token invalidates
controls after a new disruption; ended admissions return no view.

The `/rules` entry appears only for managers with an active recovery. Each status
interaction rechecks manager authority, saved route and generation. Overview,
family selection and five-output keyset pages edit the same ephemeral pane;
publicly invoked controls can only open a new private reply. No new command or
sync is required. Copy, labels, placeholders and the recovery color are editable
through the presentation catalog/private overrides.

Four engine namespaces include current-generation recovery work and outstanding
outputs, including a missing source row as unknown. Four adapter-delivery
namespaces, summary receipts and round-report receipts show unresolved rows plus
their latest complete ACK where retained. IDs remain namespaced; the same send
may appear in multiple views. Engine found/never-sent evidence never clears an
adapter claim. Adapter ACK sequence alone establishes no publication timestamp.
Prepared summaries are identified separately from uncertain sends. Unknown-family
checks use bounded existence reads independently of the visible page, so a later
unknown is not hidden by a full earlier page. Pages read at most six records,
retaining five; no copied scores, transcript or historical payload is loaded.

This view does **not** allocate a game receipt, advance a timer, claim/ACK output,
clear/retry delivery, score, change public baselines or approve/resume recovery.
Fresh essential capability facts are explicitly unknown. An empty engine list or
an empty page never proves never-sent delivery or readiness to resume. Existing
`/end-game` remains the authorized mutation. Evidence acquisition/reconciliation,
notice claim/publication/read-in and live resume are separate future increments.

## Enter before timers

The adapter must stop normal intake/dispatch, page through `live_routes`
(`after_session`, limit 1–100), allocate the next trusted receipt per route, and
call `begin(frame, recovery_id)` **before** overdue timers or chat. One dispatcher
owns sequence allocation; the returned watermark is not a concurrency allocator.
There is no automatic process-wide fence or worker lease in this module. Do not
start gameplay workers until every live route has entered its barrier.

`begin` uses `RECOVERY_BEGIN`. The stable ID identifies one real disruption.
Repeating it does not restart recovery. A new genuine disruption gets a new ID,
superseding an unfinished earlier generation while retaining original phase and
reconciled delivery evidence. An ended admission remains ended.

Playing owners physically pause with RECOVERY. Opening stages remain intact and
create no fake playing round. The separate barrier fences both paths. Normal
flow mutations raise `RecoveryRequired` before consuming a receipt, and normal
pending outputs are hidden while fenced. Route queued events through `observe`,
with their exact plain message/reaction snapshots; rejected controls may also be
consumed there. No paused event scores or counts toward a picker pool.

## Resolve delivery before promising resume

`work(session, recovery_id)` exposes at most 100 uncertain output identities for
that session. A larger backlog refuses instead of silently truncating. Reconcile
all of them with `RECOVERY_RECONCILE` and `reconcile`.

This engine work list covers opening/ordinary/pacing/evolution outputs. It does
not exhaust adapter outbound state: inspect `nr_discord_deliveries`, unresolved
`nr_summary_outputs` and `nr_round_reports` too. Their claims/actual ACKs must be
reconciled under their own guards before future live resume. Do not treat an
empty engine work list as proof that all outbound delivery is known.

Reconciliation facts:

- Supply both the real `message_id` and actual `published_at` when delivery is
  proven. The timestamp must respect known preparation/activation/attempt bounds and
  precede its recovery receipt. Legacy warning outputs retain only a lower bound
  for preparation, so the publisher must establish their actual delivery time.
- Omit both only when the publisher has conclusive evidence the output was never
  sent. Missing an ACK, not finding a message in a partial scan, or finding that a
  message was deleted is not such evidence.
- Leave an unknown outcome unresolved. It cannot authorize resume or a blind
  replacement send. Reconciled facts are immutable and retries are idempotent.

Found lobby/offer/warning/revival publications establish their original clocks
from actual publication time. A refreshed offer retains an already-running older
clock. Found activation announcements must predate their original cutover to
permit the promised activation. Late announcements remain paused for explicit
handling. A conclusively unsent ordinary activation can be abandoned into held
play without consuming its turn or closing the old round. With no old round, an
unsent opening activation ends the reservation cleanly.

Existing acknowledged facts remain unchanged. This interface accepts trusted
publisher evidence; it does not acquire it, implement send leases, or promise
exactly-once visible Discord delivery.

## Announce, read in, then resume

| Action | Envelope | Effect |
| --- | --- | --- |
| `approve` | `RECOVERY_APPROVE` | Require resolved deliveries and fresh normalized essential capability facts; verify owners/version pins and a safe continuation. |
| `publish_notice` | `RECOVERY_NOTICE_PUBLISHED` | Record actual recovery notice publication and establish its one 30-second read-in deadline. |
| `observe` | Ordered control, timer, message or reaction kind | Process known terminal expiry first; on the first receipt at/after read-in, resume and process due boundaries before its body. |
| `end` | `MANAGER_END` | Recheck trusted manager authorization and finalize/release once without requiring resume. |

`approve` takes `OpeningCapabilities` and an optional frozen implemented-template
snapshot. It prepares a notice intent, exposed by bounded `pending_outputs`.
A ready row is not publication. The real notice must explain the pause, resumed
rules/phase or held fallback, and 30-second read-in. The engineering constant is
`RECOVERY_READ_IN_SECONDS`; normal new-rule activation receives no extra grace.

`publish_notice` normally uses its server receipt time. After a lost ACK it can
accept `published_at=` as actual delivery proof, preserving that earlier read-in
boundary without reposting. Publication cannot precede notice preparation.
Same-message retries/reopen cannot change identity or extend the deadline. An
unknown notice send must be reconciled externally before retrying delivery.

At resume, rebind saved offers, activation ownership, pacing and revival ownership
in one transaction. Preserve all original deadline tokens/timestamps, partial
human choices, frozen phrase/config snapshots, seed/rotation history and scores.
A proven acknowledged activation may be applied late after read-in with its
original effective boundary, once. The old round remains open through recovery
until actual activation or ending. Expired choices remain expired; dispatch the
existing `CHOICE_TIMEOUT` workflow instead of giving another minute.

A known lobby or revival expiry ends even while notice delivery/read-in is pending;
terminal expiry wins at its exact boundary. Capacity release and earned general
finalization commit together. Revival expiry retains its silent existing-panel
output policy. A normal warning cutoff delayed by recovery uses retained observed
scores only; suspended traffic cannot enter the candidate pool.

Recovery itself never fetches/backfills missed chat. ID-only guards retain ignored
human sources. The actual resume receipt time is also persisted; a later-delivered
message/reaction created before that boundary remains ignored and guarded.
This includes a historical event arriving as the resume receipt itself. The first
current event can score under the actual resumed/activated rules. Old recovery
routing after resume refuses chat before consuming it, so the adapter can route
it through normal ingress.

## Refusals and limits

An old capability/dependency/recovery pause is not waived by a healthy startup.
Unsupported mode recovery and legacy revival handoffs without an original durable
publication clock stay paused. They permit explicit authorized ending; specialized
reconciliation/mode recovery policies remain separate work. No mode survival or
unobserved action award is inferred. Unknown version pins and changed owners
refuse; no frozen state is silently reinterpreted.

Recovery rows and proof IDs are live recovery evidence with FK cleanup, not a
permanent match history. Keep them until the existing finalization/retention rules
permit purge. This does not implement TTL cleanup or operator erasure. Back up a
stopped existing database before schema 8. Migration 8 appends only; released
migrations 1–7 and existing scores/clock/phrase/profile rows remain unchanged.

Tests use real temporary WAL files, explicit offline delivery facts, multiple
connections, rollback and cancellation. M1 is closed; the current adapter also
tests startup/reconnect barriers, pending send fences and manager ending offline.
These checks do not certify live resume, Discord publication evidence or host
throughput. The next bite is bounded, authorized read-only manager status through
`/rules`; it cannot change claims or recovery state. Evidence acquisition,
reconciliation, notice publication and explicit resume follow separately. See
[DISCORD_PREVIEW.md](DISCORD_PREVIEW.md) for the current live boundary.
