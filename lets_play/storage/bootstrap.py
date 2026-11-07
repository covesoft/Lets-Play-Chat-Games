"""Real on-disk WAL initialization with bounded contention retries and migrations."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

from lets_play.storage.migrations import APPLICATION_ID, MIGRATIONS, SCHEMA_VERSION

BUSY_TIMEOUT_MS = 10_000
WAL_ATTEMPT_TIMEOUT_MS = 100
WAL_STARTUP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class DatabaseInfo:
    schema_version: int
    journal_mode: str
    foreign_keys: bool
    busy_timeout_ms: int
    sqlite_version: str


async def _one(conn: aiosqlite.Connection, sql: str) -> tuple:
    async with conn.execute(sql) as cursor:
        row = await cursor.fetchone()
    if row is None:
        raise sqlite3.DatabaseError("Expected a database metadata row.")
    return row


async def _check_identity(conn: aiosqlite.Connection) -> int:
    version = (await _one(conn, "PRAGMA user_version"))[0]
    app_id = (await _one(conn, "PRAGMA application_id"))[0]
    if version > SCHEMA_VERSION:
        raise sqlite3.DatabaseError("Database schema is newer than this application.")
    if app_id not in {0, APPLICATION_ID}:
        raise sqlite3.DatabaseError("Database belongs to a different application.")
    if app_id == 0:
        tables = (
            await _one(conn, "SELECT COUNT(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'")
        )[0]
        if version != 0 or tables:
            raise sqlite3.DatabaseError("Refusing to adopt an unrecognized database.")
    elif version == 0:
        raise sqlite3.DatabaseError("Application database has no migration version.")
    return version


async def _enable_wal(conn: aiosqlite.Connection, retry_window: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + retry_window
    await conn.execute(f"PRAGMA busy_timeout={WAL_ATTEMPT_TIMEOUT_MS}")
    while True:
        try:
            mode = (await _one(conn, "PRAGMA journal_mode=WAL"))[0]
            if mode != "wal":
                raise sqlite3.DatabaseError("WAL mode was not enabled.")
            break
        except sqlite3.OperationalError as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 0xFF
            if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise sqlite3.OperationalError("Timed out enabling WAL mode.") from exc
            await asyncio.sleep(min(0.05, remaining))
    await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


async def _migrate(conn: aiosqlite.Connection) -> None:
    await conn.execute("BEGIN IMMEDIATE")
    try:
        version = await _check_identity(conn)
        for migration in MIGRATIONS:
            if migration.version <= version:
                continue
            for statement in migration.statements:
                await conn.execute(statement)
            await conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            await conn.execute(f"PRAGMA user_version={migration.version}")
        await conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        async with conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ) as cursor:
            recorded = await cursor.fetchall()
        if recorded != [(m.version, m.name) for m in MIGRATIONS]:
            raise sqlite3.DatabaseError(
                "Database migration metadata does not match this application."
            )
        await conn.commit()
    except BaseException:
        await conn.rollback()
        raise


async def initialize_database(
    path: Path, *, wal_timeout: float = WAL_STARTUP_TIMEOUT_SECONDS
) -> DatabaseInfo:
    """Initialize/upgrade and close; no gameplay repository is opened.

    FK/busy settings describe this connection. Every future gameplay connection
    must explicitly configure those connection-local pragmas as well.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path, isolation_level=None) as conn:
        # Keep the identity reads in one snapshot during concurrent first opens.
        await conn.execute("BEGIN")
        try:
            await _check_identity(conn)
        finally:
            await conn.rollback()
        await _enable_wal(conn, wal_timeout)
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA synchronous=FULL")
        await _migrate(conn)
        return DatabaseInfo(
            schema_version=(await _one(conn, "PRAGMA user_version"))[0],
            journal_mode=(await _one(conn, "PRAGMA journal_mode"))[0],
            foreign_keys=bool((await _one(conn, "PRAGMA foreign_keys"))[0]),
            busy_timeout_ms=(await _one(conn, "PRAGMA busy_timeout"))[0],
            sqlite_version=(await _one(conn, "SELECT sqlite_version()"))[0],
        )
