# Offline summary foundation — v0.3.4 / schema 11

This increment supplies persisted summary capture and delivery bookkeeping only.
The live Discord adapter does not invoke its capture/ACK writes. v0.3.5 adds a
read-only `live_page` method and private buttons/rendering described in
[REQUESTED_SUMMARY.md](REQUESTED_SUMMARY.md). Public/automatic delivery remains
unwired. No new command is registered.

`storage.summary_store.SummaryStore` uses an existing game unit of work. Compose
writes after actual engine effects in the same authoritative receipt lane; these
methods neither allocate a receipt nor advance scoring, timers or game ownership.
Their return values are provisional until the enclosing write transaction commits.
No method performs network I/O.

- `initialize(session)` sets a baseline once from the saved real first activation.
  Unknown legacy start times require recovery rather than being guessed.
- `next_deadline(session)` is available only when at least 25 new engine human
  messages are above the acknowledged counter. Its boundary is 60 seconds after
  the public baseline. Active recovery/paused enforcement or pending/uncertain
  output withholds automatic readiness. A later scheduler must observe that
  deadline without depending on another message.
- `prepare(session, output_key, now=..., kind=...)` freezes the current round's
  identity/rules/traffic and exact round/game scores. Automatic captures require
  both gates; requested captures bypass them. A bounded server key is immutable
  and idempotent. One prepared/sending/uncertain output per session prevents
  overlapping routine sends. Requested UI/coalescing remains future work.
- `claim(..., now=...)` commits a unique sending attempt before HTTP. A second
  connection cannot claim the same output again. The caller may send only after
  a successful claim transaction commits.
- `acknowledge(..., message_id=..., now=...)` records known public success, moves
  the public baseline to the captured counter and real ACK time, and preserves
  all arrivals during the send. Same-message retry is a no-op; conflicting ACKs
  reject/roll back. A known-success fact can commit during disruption or ending
  without resuming enforcement.
- `mark_uncertain(...)` preserves the baseline and fences new captures/retries.
  A timeout or lost ACK is not proof that nothing was posted. Explicit evidence
  reconciliation is separate work; do not reset this state or invent a new key
  to force another send. Ordinary scoring is not modified by this bookkeeping.
- `snapshot(...)` and `scores(..., scope='game'|'round', after_actor=..., limit=...)`
  expose frozen values. Pages use indexed actor keysets, accept 1–100 rows and
  never collect the complete roster in RAM. This API orders by ID, not ranking;
  future ranking/presentation must preserve exact Fraction comparisons.

Engine human-traffic counters include ordinary/media/whisper messages, excluding
reactions, nonhumans and controls. Future mode integration must feed that same
ledger. The service stores derived IDs/counters and intentional active-rule
configuration only, never message bodies, media or SDK/member objects. No runtime
cache is introduced. New rows cascade with live session deletion; the complete
ended-data retry/absolute 24-hour cleanup service is still unfinished.

Migration 11 only appends three tables and an in-flight uniqueness index. Released
migrations 1–10 and existing scores, offers, clocks, progression and delivery guards
are unchanged. Stop the bot and verify a SQLite backup before `--init-db`; expect
schema 11, WAL and foreign keys on. Every migration step has a rollback fixture.

Remaining integration: ordered adapter calls, safe publisher/scheduler handling,
private/requested controls, score/fault/rule/streak presentation, round/final
summary capture and delivery proof reconciliation. No complete summary service,
mode, automatic restart resume or M2 completion is claimed by this foundation.
