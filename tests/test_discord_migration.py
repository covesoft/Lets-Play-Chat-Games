"""Schema 9 appends only the outbound-attempt guard; released rows survive."""

import sqlite3

import pytest
from test_ordinary_flow import rows
from test_recovery_migration import released_seven

from lets_play.storage import bootstrap
from lets_play.storage.migrations import Migration


async def schema_eight(path, monkeypatch):
    released_seven(path, True)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", bootstrap.MIGRATIONS[:8])
        assert (await bootstrap.initialize_database(path)).schema_version == 8


@pytest.mark.asyncio
async def test_upgrade_preserves_all_schema_eight_rows_and_versions(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    await schema_eight(path, monkeypatch)
    tables = [
        r[0]
        for r in rows(
            path, "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'nr_%'"
        )
    ]
    before = {table: rows(path, f"SELECT * FROM {table}") for table in tables}
    assert (await bootstrap.initialize_database(path)).schema_version == 9
    assert {table: rows(path, f"SELECT * FROM {table}") for table in tables} == before
    assert rows(path, "SELECT COUNT(*) FROM nr_discord_deliveries") == [(0,)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]


@pytest.mark.asyncio
async def test_failed_ninth_migration_preserves_schema_eight_exactly(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    await schema_eight(path, monkeypatch)
    original = bootstrap.MIGRATIONS
    broken = Migration(9, original[-1].name, (*original[-1].statements, "INVALID SQL"))
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", (*original[:-1], broken))
        with pytest.raises(sqlite3.OperationalError):
            await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(8,)]
    assert rows(path, "SELECT name FROM sqlite_schema WHERE name='nr_discord_deliveries'") == []
    assert (await bootstrap.initialize_database(path)).schema_version == 9
