"""Append-only schema-13 notice claims preserve released schema-12 state."""

import sqlite3
from contextlib import closing

import pytest
from test_ordinary_flow import rows
from test_round_report_migration import released_eleven
from test_summary_foundation_migration import snapshot

from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS, Migration


async def released_twelve(path, monkeypatch):
    await released_eleven(path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:12])
        assert (await bootstrap.initialize_database(path)).schema_version == 12
    with closing(sqlite3.connect(path)) as conn, conn:
        session = conn.execute("SELECT session_id FROM nr_admissions LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO nr_recoveries(session_id,recovery_id,stage,original_stage,original_lifecycle,original_round,owner_revision,began_at_utc) VALUES (?,'released','paused','active','playing','old-round',7,100)",
            (session,),
        )
        conn.execute(
            "INSERT INTO nr_recovery_work VALUES (?,'released','ordinary',999,'offer','unknown',NULL,NULL)",
            (session,),
        )


@pytest.mark.asyncio
async def test_schema_twelve_recovery_claims_scores_and_clocks_survive_upgrade(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    await released_twelve(path, monkeypatch)
    before = snapshot(path)
    with closing(sqlite3.connect(path)) as conn:
        columns = {t: [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] for t in before}
    assert (await bootstrap.initialize_database(path)).schema_version == 13
    for table, values in before.items():
        if table != "schema_migrations":
            assert rows(path, f"SELECT {','.join(columns[table])} FROM {table}") == values
    assert rows(path, "SELECT notice_attempted_at_utc,notice_ack_sequence FROM nr_recoveries") == [
        (None, 0)
    ]
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("after", range(1, len(MIGRATIONS[12].statements) + 1))
async def test_each_notice_migration_statement_rolls_back_without_losing_previous_state(
    tmp_path, monkeypatch, after
):
    path = tmp_path / "game.sqlite3"
    await released_twelve(path, monkeypatch)
    before = snapshot(path)
    failed = Migration(13, MIGRATIONS[12].name, (*MIGRATIONS[12].statements[:after], "INVALID SQL"))
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", (*MIGRATIONS[:12], failed))
        with pytest.raises(sqlite3.OperationalError):
            await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(12,)]
    assert snapshot(path) == before
    assert (await bootstrap.initialize_database(path)).schema_version == 13
