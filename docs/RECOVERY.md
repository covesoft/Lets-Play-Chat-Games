# Offline disruption recovery

`RecoveryFlow(unit)` provides the existing engine barrier and offline recovery
contracts. v0.3.19/schema 13 extends the bounded live automatic path through
`RebootFlow(unit)` and the serial adapter publisher. Ordinary reboot needs no
server-manager approval, Resume button or player message.

## Automatic recovery for published phases

1. Startup drains admitted work and enters every saved route's barrier before
   allowing publishers/timers. Work finishing during initialization cannot
   recreate normal timers or senders.
2. A candidate has unchanged recovery ownership and no unresolved output. Known
   phases include ordinary idle/waiting, acknowledged warnings and choices, announced
   activations, held play, revival, lobby and opening setup. The saved revival is
   the only prior pause this path restores. Phase-specific proof joins the scoped engine
   output to its adapter receipt: same message, real publication time and positive
   ordered ACK. Warning/activation clocks must match that proof. A refreshed offer
   may inherit an earlier choice clock. Independent scoped existence checks cover
   all four engine-output families, adapter delivery claims, summary receipts, round-report
   receipts and older recovery-notice attempts. A complete engine list alone is
   insufficient. Prepared summaries with no send attempt can wait for resume.
3. Outside database write units, verify essential channel permissions and really
   add/remove the configured marker on the existing known panel. No separate
   start-game probe is posted. Published lobby, choice and revival controls require their original
   panel to remain accessible, before notice and again before resume.
   Missing panel/access or marker failure stays paused.
4. Ordered internal approval uses the original RecoveryFlow contracts. Claim the
   recovery notice durably before its HTTP send. Publish one friendly resumption
   notice with the preserved rules and a 30-second read-in. For an already-announced
   activation it shows the saved incoming rules, labeled to take effect on resume;
   no early activation or round closure occurs. Published choices include a link
   back to their original cards and the unchanged absolute deadline. Actual
   successful publication/ACK owns the persisted boundary. No scores or public summary
   baselines change. The known notice becomes the phase panel in ACK order.
5. During read-in, /rules shows a concise countdown and the preserved rules.
   Ingress is guarded without scoring; even a post after the scheduled boundary
   cannot resume on its own. The timer wakes the publisher, fresh channel/marker
   access is checked, and an ordered internal receipt performs continuation.
   Historical-source guards, original round clocks/rules/scores and due-boundary
   ordering remain the engine's responsibility. Overdue pacing runs after resume.
   Published card buttons stay on the original ACK-bound message, including any
   partial outgoing-target selection. Refresh restores that panel and edits the
   notice into a link, without duplicate card buttons or a new choice minute.
   An expired choice runs the normal timeout workflow after read-in; an already
   announced overdue activation commits once with its original effective clock.
   Lobby and revival expiry use an independent timer and take precedence even
   during read-in or blocked recovery. Revival expiry edits an existing panel
   and releases capacity. Held play retains its saved idle clock and round.
   Opening never creates a round before the original activation is due.

Schema 13 appends `notice_attempted_at_utc`, `notice_ack_sequence` and an index to
`nr_recoveries`. Claims survive lost ACKs, timeout, cancellation and another
startup. An unknown older notice prevents another send. A new real disruption
following a known successful notice can create a new generation/notice, preserving
the same underlying round. A known successful send can ACK after manager ending
or supersession without resuming the ended/old generation. Ended remains terminal.

The former manager recovery/status pane and its read-model/UI modules are removed.
Old `nrx` controls receive a private retirement message; no internals are read.
Keep its old catalog keys accepted for private presentation-override compatibility.
Technical guards are internal. If this limited path cannot establish safe recovery,
the known panel and /rules give a brief host-contact explanation and retain
manager ending. Do not automatically retry a failed/uncertain notice in this build.

## Interrupted lobby and opening-card sends

At startup, an unresolved durable lobby or opening-card send can be matched
against one bounded lookup in its exact channel. Inspect the oldest messages
after the saved attempt time, up to 50. Require a single unedited post by this
bot with the exact saved session's Join button or its three opening-card
controls. An empty, ambiguous, inaccessible or capped lookup leaves the claim
unresolved. This lookup uses publication metadata and controls without reading
or retaining chat bodies.

An ordered receipt rechecks the original claim and recovery generation. It
commits the engine proof and adapter acknowledgement together. Discord's
original message creation time starts the original lobby or choice clock.
An expired lobby closes immediately. An expired opening choice completes via
normal timeout after read-in. Confirmed posts keep their original buttons.

## Interrupted warning and revival posts

New warning and revival posts include a reference tied to the exact session,
output and kind. On startup the bot uses the same bounded original-post lookup
and rechecks that the round and attempt still own the unresolved claim. It
commits the original Discord publication time and adapter acknowledgement in
one ordered receipt. A recovered warning retains its original 30-second cutoff
and advances after read-in if already due. A recovered revival retains its
original five-minute deadline and Continue button. If it already expired the
round ends through the normal silent closure path.

A post from before this version may lack the reference. Old, edited, ambiguous
or capped results leave the claim unresolved.

## Interrupted activation announcements

Opening and ordinary activation announcements carry separate output references.
Startup checks the original bot-authored post in the exact channel and rechecks
the saved selection, recovery generation and durable output. It accepts a
creation timestamp strictly before the recorded cutover only. A post created at
or after cutover, an edited post, and a post without proof leave the claim
unresolved. With timely proof, no replacement announcement is sent. The
existing read-in completes before the original activation is applied once at
its saved effective time.

## Remaining integration and upgrading

Modes, unpublished phases and unproven output claims remain fenced.
Proof for other newly created messages and edits is separate work. No unknown send is inferred healthy, missing/late message proof is not
never-sent evidence, and no chat history is backfilled for scoring. No new manager
recovery gate or diagnostic UI is planned. Full mode/retention/erasure/live evidence
remains open.

Apply v0.3.19 to v0.3.18 while stopped, reinstall, run offline checks and start.
Schema stays **13**; no database initialization, migration or command sync is
needed. Released migrations 1–13 are unchanged. For an older installation,
back up and verify the stopped configured database using SQLite's backup API
before a schema upgrade; honor private `load_settings()` path overrides. The
schema-12 to 13 migration appends only notice guards and rolls back on failure.
Downgrading from schema 13 to v0.3.13 requires a verified schema-12 backup;
never downgrade the database file in place.
See [STORAGE.md](STORAGE.md) for backup policy.

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

Tests exercise real temporary WAL files, reopened runtimes, timer callbacks with
injected clocks, no-message resume, preserved scores/deadlines, all-family
uncertainty, notice loss/cancellation/reboot and manager-ending races, migration
rollback and real SDK transport boundaries without login. This is offline evidence;
actual Windows/client/server observations remain separate. See
[DISCORD_PREVIEW.md](DISCORD_PREVIEW.md) for the current human check.
