"""Released schema-7 data survives append-only recovery migration and rollback."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_ordinary_flow import rows

from lets_play.storage import bootstrap
from lets_play.storage.migrations import SCHEMA_VERSION, Migration


def released_seven(path, populated):
    name = "released_schema7_progress.sql" if populated else "released_schema7.sql"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript((Path(__file__).parent / "fixtures" / name).read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("populated", [False, True])
async def test_schema_seven_upgrade_preserves_every_existing_row_without_starting_recovery(
    tmp_path, populated
):
    path = tmp_path / "game.sqlite3"
    released_seven(path, populated)
    tables = [
        r[0]
        for r in rows(
            path, "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'nr_%'"
        )
    ]
    before = {t: rows(path, f"SELECT * FROM {t}") for t in tables}
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION == 10
    for t, value in before.items():
        assert rows(path, f"SELECT * FROM {t}") == value
    assert rows(path, "SELECT COUNT(*) FROM nr_recoveries") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_recovery_work") == [(0,)]
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [1, 2, 3, 4])
async def test_failed_migration_eight_rolls_back_all_ddl_and_preserves_released_scores_clocks(
    tmp_path, monkeypatch, after
):
    path = tmp_path / "game.sqlite3"
    released_seven(path, True)
    before = {
        t: rows(path, f"SELECT * FROM {t}")
        for t in (
            "nr_sessions",
            "nr_offers",
            "nr_revivals",
            "nr_round_pacing",
            "nr_profiles",
            "schema_migrations",
        )
    }
    original = bootstrap.MIGRATIONS
    failed = Migration(8, original[7].name, (*original[7].statements[:after], "INVALID SQL"))
    monkeypatch.setattr(bootstrap, "MIGRATIONS", (*original[:7], failed))
    with pytest.raises(sqlite3.OperationalError):
        await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(7,)]
    for t, value in before.items():
        assert rows(path, f"SELECT * FROM {t}") == value
    assert rows(path, "SELECT name FROM sqlite_schema WHERE name='nr_recoveries'") == []
    monkeypatch.setattr(bootstrap, "MIGRATIONS", original)
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION
