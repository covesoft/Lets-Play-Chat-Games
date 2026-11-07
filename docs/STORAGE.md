# SQLite initialization and migration strategy

M0 supplies bootstrap/migration infrastructure, not a gameplay repository.
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

## M1 boundary

M1 introduces transaction/unit-of-work and game schema constraints, including
exact-channel admission, session/source/reaction deduplication, once-only round
resolution, and atomic live-credit/global-profile updates. A Python process lock
alone cannot enforce these invariants. No part of the M0 bootstrap demonstrates
those game transactions yet. Do not persist raw Discord objects, message text,
audio, media, or a historical finished-game ledger.

## Backups and failures

Before migrations in deployment, stop the process and make a verified SQLite
backup (prefer SQLite’s backup API), or take a coherent backup with all processes
stopped and WAL state accounted for. Copying only the main file while live may
miss committed WAL data. Restore into a separate path and verify integrity/schema
before replacing production state. Never remove a live `-wal` or `-shm` file as
lock recovery. The M6 operator rehearsal must verify this on the actual host.
