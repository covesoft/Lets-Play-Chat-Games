"""Released schema-10 data survives all-or-nothing summary foundation upgrades."""

import sqlite3

import pytest
from test_discord_delivery_migration import released_nine
from test_ordinary_flow import rows

from lets_play.storage import bootstrap
from lets_play.storage.migrations import Migration


async def released_ten(path, monkeypatch):
    await released_nine(path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", bootstrap.MIGRATIONS[:10])
        assert (await bootstrap.initialize_database(path)).schema_version == 10


def snapshot(path):
    tables = [
        r[0]
        for r in rows(
            path,
            "SELECT name FROM sqlite_schema WHERE type='table' AND (name LIKE 'nr_%' OR name='schema_migrations')",
        )
    ]
    return {t: rows(path, f"SELECT * FROM {t}") for t in tables}


@pytest.mark.asyncio
async def test_upgrade_preserves_scores_clocks_and_existing_send_claims(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    await released_ten(path, monkeypatch)
    before = snapshot(path)
    assert (await bootstrap.initialize_database(path)).schema_version == 13
    after = snapshot(path)
    assert {t: after[t] for t in before if t != "schema_migrations"} == {
        t: r for t, r in before.items() if t != "schema_migrations"
    }
    assert all(not r for t, r in after.items() if t.startswith("nr_summary_"))
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("after", range(1, 5))
async def test_each_failed_migration_step_restores_exact_schema_ten(tmp_path, monkeypatch, after):
    path = tmp_path / "game.sqlite3"
    await released_ten(path, monkeypatch)
    before = snapshot(path)
    migrations = bootstrap.MIGRATIONS
    broken = Migration(11, migrations[10].name, (*migrations[10].statements[:after], "INVALID SQL"))
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", (*migrations[:10], broken))
        with pytest.raises(sqlite3.OperationalError):
            await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(10,)]
    assert snapshot(path) == before
    assert (await bootstrap.initialize_database(path)).schema_version == 13
