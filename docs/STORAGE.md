# SQLite initialization, migration, and event unit of work

The package supplies bootstrap/migration infrastructure and an offline New Rules
event unit of work. It is not wired to live Discord gameplay.
Pure ordinary-subset [hand preparation](HANDS.md) can supply legal result states;
offers, their consumption, publication, and round resolution are not persisted by
that API. Those operations still require a composed transition service.
`initialize_database()` opens one aiosqlite connection, initializes it, reports
safe metadata, and closes it on success or failure. `--init-db` invokes it without
Discord. The live shell invokes it in `setup_hook()` before command sync.

The database is on a local filesystem. Use SQLite WAL with `synchronous=FULL`,
`foreign_keys=ON`, and a 10,000 ms ordinary busy timeout. Foreign-key enforcement,
synchronous mode, and busy timeout are **connection-local** and must be set again
on every M1 gameplay connection. WAL persists in the file. Do not use an in-memory
database or a network filesystem as a substitute for the production WAL setup.

WAL transition runs outside a transaction. A 100 ms per-attempt busy timeout and
five-second monotonic retry window handle SQLITE_BUSY/SQLITE_LOCKED at startup.
Unrelated errors propagate immediately. After successful WAL transition, restore
the ordinary busy timeout. A persistent lock fails startup and closes the
connection; it never silently continues in rollback-journal mode. Tests use real
reader locks, not just fabricated exceptions.

## Versioning and atomicity

* `PRAGMA application_id` identifies Let’s Play with the four-byte `LPLY` value.
* `PRAGMA user_version` is the authoritative integer schema version.
* `schema_migrations(version, name)` records each applied ordered migration.
* Schema **1** adds only that bookkeeping. It has no sessions/profiles/chat tables.
* Schema **2** adds live evaluation owners/round identities, per-player session and
  round scores, traffic counters, source/reaction guards, per-instance fault
  aggregates, and message-marker intents. It requires SQLite 3.37+ for STRICT tables.
* New databases must be empty and unclaimed. Foreign, unrecognized nonempty, and
  newer-version databases fail clearly; there is no automatic downgrade/adoption.

Check ownership/version before changing journal mode. Acquire `BEGIN IMMEDIATE`
before migrations and recheck under that lock so parallel first opens serialize.
Run all pending SQL statements, bookkeeping inserts, and version changes in the
same explicit transaction. Do not use `executescript()` here because its implicit
transaction behavior can defeat rollback. Failed SQL rolls back the entire
pending batch. Cancellation must also roll back and close the connection.

Append immutable numbered `Migration` entries to `storage/migrations.py`. Never
edit an already-released migration or delete/recreate a user’s database to hide
an upgrade failure. With future schema changes, add a real old-schema fixture,
forward-upgrade check, rollback/fault check, and integrity check. Complex data
transformations can use explicit functions inside the same transaction when
required; no migration framework dependency is needed yet.

## Offline event API

```python
from pathlib import Path
from lets_play.storage.game_store import ApplyStatus, open_game_store

# context is an EvaluationContext; decision came from the pure evaluator.
async with open_game_store(Path("data/example.sqlite3")) as store:
    async with store.transaction() as unit:
        await unit.insert_session(context)

    status = await store.apply(decision)  # Returns after COMMIT.
    if status is ApplyStatus.APPLIED:
        pass  # Internal effects committed; delivery remains separate.

    async with store.transaction(write=False) as unit:
        total = await unit.get_score(context.position.route.session_id, decision.actor_id)
        markers = await unit.pending_markers(limit=100)
```

`insert_session` is an internal persistence primitive for a playing evaluation
owner. It does not implement lobby/quorum, capacity/entitlement admission, or
opening-hand checks. A partial unique index reserves one non-ended session per
channel, but complete guild admission still needs an application service.

`GameUnitOfWork.set_owner(expected_position, replacement_context)` compares the
complete persisted owner, including a monotonic `revision`. It returns false
when stale/ended and requires the next revision for a mutation. Rule changes
require a new ruleset version; a new round requires a new playing ruleset and
cannot reuse an existing round ID. Pause/resume may keep the same round. The
revision protects against returning to an earlier visible state. An ended owner
cannot reopen. This method is a storage primitive, not a rule activation or award
resolver: future activation services must compose all their effects in one unit.

`store.apply()` wraps a write unit and returns one of:

| Status | Meaning |
| --- | --- |
| `APPLIED` | Guard, aggregates, enrollment, faults, and marker intent committed together |
| `DUPLICATE_EVENT` | This session/source-kind/source-ID was already committed |
| `DUPLICATE_REACTION` | This session/actor/target already incurred its reaction fault |
| `STALE_OWNER` | An unseen candidate no longer owns the current playing position |
| `UNKNOWN_SESSION` | No stored session; the store does not implicitly create one |

`unit.apply()` has the same statuses, but its return is provisional until the
enclosing unit exits successfully. Never send effects based on a provisional
return. Every write error makes the unit rollback-only, even if caller code
catches the exception. Nested units on one store, reuse after exit, and use from
another task fail explicitly. Read units expose no mutation methods.

## Atomicity and data representation

Write units use `BEGIN IMMEDIATE` and one connection/async lock per store instance.
Database transactions and unique keys protect independent connections/processes;
the lock alone is not the guard. Source keys are unique per session and event kind.
Reaction offense keys contain only session, actor, and target, so emoji/re-adds,
instance changes, and later rounds cannot repeat a fault. Duplicate reaction
attempts do not accumulate extra source rows. Rejected/stale events consume no key.

Each committed participant delta updates both its session total and owner-round
total. Scores retain separate ordinary/mode/reaction fields. Exact quality uses
canonical decimal TEXT numerator/denominator pairs, decoded with `Fraction`;
there is no SQLite REAL arithmetic or integer cast/SUM of quality. Counters use
nonnegative SQLite integers; overflow/corruption fails instead of approximating.
Per-instance/reason fault counters preserve useful breakdowns without storing an
event transcript. Only implemented reason codes can be written by this API.

Whispers update session/round human-traffic and whisper counters plus their source
guard. They do not create/update a participant row. Thus player score traffic
counts enrolled scoring/participation messages, while `get_traffic()` includes
whispers from all human senders. An ordinary post with no applicable predicate
still creates participation but remains unrated. Reaction-only participants have
reaction score fields and no ordinary participation/traffic credit.

The context codec explicitly serializes frozen rule configuration and emoji
exclusions; frozen phrases are intentional settings snapshots. It does not
serialize dataclasses generically. Raw content, filenames, URLs, media, and event
objects never enter the store. Parser/evaluator/score version pins are checked on
read: mismatches raise `IncompatibleState` and require an explicit recovery policy,
rather than silently changing live semantics after an upgrade.
v0.2.3 pins `ordinary-subset-2` as the evaluator version. Offline session fixtures
saved under v0.2.2's `representatives-1` refuse automatic reinterpretation. The
live shell has not created gameplay sessions; schema 2 itself is unchanged.

Marker intents contain identifiers only and commit with the fault. Read at most
100 per page; acknowledge only after successful/terminal handling. Acknowledgement
is idempotent, and monotonically allocated intent IDs prevent a stale
acknowledgement deleting a later intent. Source IDs are still opaque domain
identifiers; the future Discord adapter must supply/resolve the actual message
identity. No sender, lease/retry scheduler, or complete external-delivery outbox is
implemented. Pending intents may be read again. Internal exactly-once effects do
not promise exactly-once external delivery.

## Contention, cancellation, and capacity

`open_game_store` migrates once on open, then configures every runtime connection
with foreign keys, FULL synchronous mode, and a bounded busy timeout (default
1,000 ms, separate from bootstrap's 10,000 ms). A missing file is not recreated
between migration and runtime open. `max_pending` defaults to 128 including the
active unit; excess work raises `StoreOverloaded` immediately. Limits are internal
API parameters, not new environment keys. Both bounds are validated before I/O.
Marker page sizes are bounded, guard/score lookups are indexed, and no session,
message, member, or profile object cache is maintained.

WAL permits reader snapshots alongside writes, but SQLite has one writer at a
time. Keep transactions short; never await Discord/network calls inside a write
unit. Do not open a connection per message. Use one long-lived store per service
instance and ensure the future ingress service surfaces/retries contention and
overload without silently dropping judged traffic. This storage API does not
implement ingress scheduling or certify a guild count/requests-per-second limit.

Cancellation during a unit body rolls back before releasing the connection.
Commit/rollback/close cleanup is awaited even through repeated cancellation.
Cancellation at commit acknowledgement can leave a committed result: retry the
same stable source key to learn its status; never invent a fresh ID. Closing
stops admission and drains the active unit. A failed rollback makes the store
unusable. No unbounded automatic write retry hides lock/disk errors.

## Verification and remaining boundary

Tests use real temporary WAL files, released schema-1 upgrade/rollback fixtures,
separate connection/process duplicate races, ownership compare-and-set races,
fault-trigger rollback, cancellation at begin/body/commit acknowledgement, bounded
contention/admission, reopen, reader snapshots, exact fraction storage, and FK
cascades. They are offline correctness checks, not host throughput measurements.

The live shell still only bootstraps the database and logs in. Complete admission,
deadline/round transitions, round resolution, game credit/profiles/awards,
delivery/recovery workers, and safe ended-session purge remain unimplemented.
Round rows here identify score ownership; they do not certify finalized awards.
Foreign-key cascades provide a cleanup foundation, not an implemented retention
service. Do not delete live evidence before future finalization commits.

## Backups and failures

Before migrations in deployment, stop the process and make a verified SQLite
backup (prefer SQLite’s backup API), or take a coherent backup with all processes
stopped and WAL state accounted for. Copying only the main file while live may
miss committed WAL data. Restore into a separate path and verify integrity/schema
before replacing production state. Never remove a live `-wal` or `-shm` file as
lock recovery. The M6 operator rehearsal must verify this on the actual host.

References: [SQLite WAL](https://sqlite.org/wal.html),
[transactions](https://sqlite.org/lang_transaction.html),
[STRICT tables](https://sqlite.org/stricttables.html), and
[aiosqlite execution model](https://aiosqlite.omnilib.dev/en/stable/).
