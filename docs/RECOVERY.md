# Offline disruption recovery

Schema 8 `RecoveryFlow(unit)` adds a durable barrier for a disruptive restart or
reconnect. It coordinates existing ordinary, held, revival, lobby and opening
state without sending messages or loading channel history. A handle reopen alone
still does not invoke recovery. The live shell has no game dispatcher yet.

## Enter before timers

The future adapter must stop normal intake/dispatch, page through `live_routes`
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
all of them with `RECOVERY_RECONCILE` and `reconcile`:

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
connections, rollback and cancellation. They do not establish real Discord
publication, automatic startup wiring, host throughput, or M1 completion. The
remaining M1 acceptance/scenario audit precedes live M2 integration.
