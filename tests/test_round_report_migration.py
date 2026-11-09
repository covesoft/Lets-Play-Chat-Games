"""Released schema-11 receipts survive each atomic schema-12 migration step."""

import sqlite3
from contextlib import closing

import pytest
from test_ordinary_flow import rows
from test_summary_foundation_migration import released_ten, snapshot

from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS, Migration


async def released_eleven(path, monkeypatch):
    await released_ten(path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:11])
        assert (await bootstrap.initialize_database(path)).schema_version == 11
    with closing(sqlite3.connect(path)) as conn, conn:
        session, round_id, number = conn.execute(
            "SELECT session_id,round_id,round_number FROM nr_rounds LIMIT 1"
        ).fetchone()
        conn.execute("INSERT INTO nr_summary_baselines VALUES (?,?,?)", (session, 10, 7))
        for key, state, message in [
            ("request:123", "delivered", 999),
            ("request:124", "sending", None),
        ]:
            conn.execute(
                """INSERT INTO nr_summary_outputs(session_id,output_key,kind,state,round_id,round_number,captured_at_utc,human_messages,whispers,round_messages,round_whispers,rules_json,attempted_at_utc,message_id,delivered_at_utc)
                VALUES (?,?,'requested',?,?,?,?,7,2,7,2,'[]',8,?,?)""",
                (session, key, state, round_id, number, 5, message, 10 if message else None),
            )


@pytest.mark.asyncio
async def test_upgrade_keeps_legacy_rows_and_backfills_only_known_delivery_watermarks(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    await released_eleven(path, monkeypatch)
    with closing(sqlite3.connect(path)) as conn:
        columns = {
            t: [c[1] for c in conn.execute(f"PRAGMA table_info({t})")] for t in snapshot(path)
        }
    before = snapshot(path)
    assert (await bootstrap.initialize_database(path)).schema_version == 13
    for table, values in before.items():
        if table == "schema_migrations":
            continue
        assert rows(path, f"SELECT {','.join(columns[table])} FROM {table}") == values
    assert rows(
        path,
        "SELECT ack_nonwhispers,request_cursor,public_message_id,auto_round_id,auto_round_posts FROM nr_summary_baselines",
    ) == [(5, 124, 999, None, 0)]
    assert rows(path, "SELECT ordinary_posts FROM nr_summary_outputs") == [(0,), (0,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_round_reports") == [(0,)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("after", range(1, len(MIGRATIONS[11].statements) + 1))
async def test_every_failed_upgrade_statement_rolls_back_to_exact_schema_eleven(
    tmp_path, monkeypatch, after
):
    path = tmp_path / "game.sqlite3"
    await released_eleven(path, monkeypatch)
    before = snapshot(path)
    failed = Migration(12, MIGRATIONS[11].name, (*MIGRATIONS[11].statements[:after], "INVALID SQL"))
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", (*MIGRATIONS[:11], failed))
        with pytest.raises(sqlite3.OperationalError):
            await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(11,)]
    assert snapshot(path) == before
    assert (await bootstrap.initialize_database(path)).schema_version == 13
