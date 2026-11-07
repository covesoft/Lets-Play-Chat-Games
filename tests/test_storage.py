"""Real SQLite evidence for bootstrap, rollback, and WAL lock handling."""

import asyncio
import sqlite3
from contextlib import closing

import pytest

from lets_play.storage import bootstrap as storage
from lets_play.storage import initialize_database
from lets_play.storage.migrations import APPLICATION_ID, SCHEMA_VERSION, Migration


def scalar(path, sql):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql).fetchone()[0]


@pytest.mark.asyncio
async def test_fresh_and_repeated_startup_preserve_wal_schema_and_integrity(tmp_path):
    path = tmp_path / "data/game.sqlite3"
    first = await initialize_database(path)
    second = await initialize_database(path)
    assert first == second
    assert first.schema_version == SCHEMA_VERSION == 1
    assert first.journal_mode == "wal"
    assert first.foreign_keys
    assert first.busy_timeout_ms == 10000
    assert scalar(path, "PRAGMA application_id") == APPLICATION_ID
    assert scalar(path, "PRAGMA integrity_check") == "ok"
    assert scalar(path, "SELECT COUNT(*) FROM schema_migrations") == 1


@pytest.mark.asyncio
async def test_parallel_first_opens_apply_each_migration_once(tmp_path):
    path = tmp_path / "parallel.sqlite3"
    results = await asyncio.gather(*(initialize_database(path) for _ in range(4)))
    assert all(result.schema_version == 1 for result in results)
    assert scalar(path, "SELECT COUNT(*) FROM schema_migrations") == 1
    assert scalar(path, "PRAGMA integrity_check") == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["newer", "foreign", "unclaimed"])
async def test_unrecognized_or_newer_database_is_not_adopted_or_rejournaled(tmp_path, kind):
    path = tmp_path / "existing.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE sentinel (value TEXT)")
        conn.execute("INSERT INTO sentinel VALUES ('preserve')")
        if kind == "newer":
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            conn.execute(f"PRAGMA application_id={APPLICATION_ID}")
        elif kind == "foreign":
            conn.execute("PRAGMA application_id=42")
    with pytest.raises(sqlite3.DatabaseError):
        await initialize_database(path)
    assert scalar(path, "PRAGMA journal_mode") == "delete"
    assert scalar(path, "SELECT value FROM sentinel") == "preserve"


@pytest.mark.asyncio
async def test_failed_upgrade_rolls_back_ddl_metadata_and_version(tmp_path, monkeypatch):
    path = tmp_path / "upgrade.sqlite3"
    await initialize_database(path)
    failed = Migration(
        2, "fault_fixture", ("CREATE TABLE should_rollback (id INTEGER)", "INVALID SQL")
    )
    monkeypatch.setattr(storage, "MIGRATIONS", (*storage.MIGRATIONS, failed))
    monkeypatch.setattr(storage, "SCHEMA_VERSION", 2)
    with pytest.raises(sqlite3.OperationalError):
        await initialize_database(path)
    assert scalar(path, "PRAGMA user_version") == 1
    assert scalar(path, "SELECT COUNT(*) FROM schema_migrations") == 1
    assert scalar(path, "SELECT COUNT(*) FROM sqlite_schema WHERE name='should_rollback'") == 0
    # Failed startup closed its handle; replacing the bad migration can proceed.
    successful = Migration(2, "upgrade_fixture", ("CREATE TABLE upgrade_ok (id INTEGER)",))
    monkeypatch.setattr(storage, "MIGRATIONS", (*storage.MIGRATIONS[:1], successful))
    assert (await initialize_database(path)).schema_version == 2
    assert scalar(path, "PRAGMA integrity_check") == "ok"


def locked_empty_database(path):
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM sqlite_schema").fetchone()
    return reader


@pytest.mark.asyncio
async def test_real_reader_lock_during_wal_transition_retries_then_succeeds(tmp_path, monkeypatch):
    path = tmp_path / "reader.sqlite3"
    reader = locked_empty_database(path)
    attempts = 0
    real_one = storage._one

    async def release_after_contention(conn, sql):
        nonlocal attempts
        if sql == "PRAGMA journal_mode=WAL":
            attempts += 1
            if attempts == 3:
                reader.rollback()
        return await real_one(conn, sql)

    monkeypatch.setattr(storage, "_one", release_after_contention)
    try:
        result = await initialize_database(path)
        assert attempts == 3
        assert result.journal_mode == "wal"
        assert result.busy_timeout_ms == 10000
    finally:
        reader.close()


@pytest.mark.asyncio
async def test_permanent_wal_lock_fails_closes_and_can_be_retried(tmp_path):
    path = tmp_path / "locked.sqlite3"
    reader = locked_empty_database(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="Timed out enabling WAL"):
            await initialize_database(path, wal_timeout=0.01)
        assert scalar(path, "PRAGMA user_version") == 0
    finally:
        reader.rollback()
        reader.close()
    assert (await initialize_database(path)).journal_mode == "wal"


@pytest.mark.asyncio
async def test_unrelated_wal_error_is_not_retried(tmp_path, monkeypatch):
    real_one = storage._one
    attempts = 0

    async def disk_failure(conn, sql):
        nonlocal attempts
        if sql == "PRAGMA journal_mode=WAL":
            attempts += 1
            raise sqlite3.OperationalError("harmless fault fixture")
        return await real_one(conn, sql)

    monkeypatch.setattr(storage, "_one", disk_failure)
    with pytest.raises(sqlite3.OperationalError, match="harmless fault fixture"):
        await initialize_database(tmp_path / "fault.sqlite3")
    assert attempts == 1
