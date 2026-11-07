"""Released schema-6 upgrade evidence, including rewritten tables and rollback."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_ordinary_flow import rows

from lets_play.storage import bootstrap
from lets_play.storage.game_store import open_game_store
from lets_play.storage.migrations import SCHEMA_VERSION, Migration
from lets_play.storage.ordinary_flow import OrdinaryFlow
from lets_play.storage.pacing_flow import RoundPacing


def released_six(path, *, populated=True):
    name = "released_schema6_progress.sql" if populated else "released_schema6.sql"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript((Path(__file__).parent / "fixtures" / name).read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("populated", [False, True])
async def test_schema_six_upgrade_preserves_scores_offer_clocks_and_original_pacing_attempt(
    tmp_path, populated
):
    path = tmp_path / "game.sqlite3"
    released_six(path, populated=populated)
    tables = [
        r[0]
        for r in rows(
            path, "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'nr_%'"
        )
    ]
    columns = {t: ",".join(r[1] for r in rows(path, f"PRAGMA table_info({t})")) for t in tables}
    before = {t: rows(path, f"SELECT {columns[t]} FROM {t}") for t in tables}
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION == 8
    for table, snapshot in before.items():
        if table == "nr_sessions":
            i = columns[table].split(",").index("pacing_version")
            snapshot = [(*r[:i], "ordinary-pacing-2", *r[i + 1 :]) for r in snapshot]
        assert rows(path, f"SELECT {columns[table]} FROM {table}") == snapshot
    assert rows(path, "SELECT COUNT(*) FROM nr_evolution") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_revivals") == [(0,)]
    if populated:
        assert rows(path, "SELECT attempt,next_estimate,revival_id FROM nr_round_pacing") == [
            (1, None, None)
        ]
        async with open_game_store(path) as store:
            async with store.transaction(write=False) as unit:
                assert (await RoundPacing(unit).record("smoke")).attempt == 1
                assert (await OrdinaryFlow(unit).watermark("smoke")).last_sequence > 0
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [6, 10, 18, 25, None])
async def test_failed_migration_seven_restores_rebuilt_tables_columns_ids_and_metadata(
    tmp_path, monkeypatch, after
):
    path = tmp_path / "game.sqlite3"
    released_six(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO nr_pacing_outputs(session_id,round_id,kind,message_id,acknowledged_at_utc) SELECT session_id,round_id,'warning',123,456 FROM nr_round_pacing"
        )
        conn.execute(
            "INSERT INTO nr_picker_candidates(session_id,round_id,actor_id,ordinary_posts,actual_n,actual_d,raw_faults) SELECT session_id,round_id,606,5,'100','1',0 FROM nr_round_pacing"
        )
    before = {
        t: rows(path, f"SELECT * FROM {t}")
        for t in (
            "nr_pacing_outputs",
            "nr_picker_candidates",
            "nr_round_pacing",
            "schema_migrations",
            "nr_sessions",
            "sqlite_sequence",
        )
    }
    original = bootstrap.MIGRATIONS
    statements = original[6].statements
    if after is not None:
        statements = statements[:after]
    failed = Migration(7, original[6].name, (*statements, "INVALID SQL"))
    monkeypatch.setattr(bootstrap, "MIGRATIONS", (*original[:6], failed))
    with pytest.raises(sqlite3.OperationalError):
        await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(6,)]
    for t, snapshot in before.items():
        assert rows(path, f"SELECT * FROM {t}") == snapshot
    assert rows(path, "SELECT name FROM sqlite_schema WHERE name LIKE '%_v5'") == []
    assert rows(path, "SELECT name FROM sqlite_schema WHERE name='nr_evolution'") == []
    monkeypatch.setattr(bootstrap, "MIGRATIONS", original)
    assert (await bootstrap.initialize_database(path)).schema_version == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("keep_row", [False, True])
async def test_migration_preserves_deleted_output_id_highwater_for_delayed_external_ack(
    tmp_path, keep_row
):
    path = tmp_path / "game.sqlite3"
    released_six(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO nr_pacing_outputs(output_id,session_id,round_id,kind) SELECT 99,session_id,round_id,'warning' FROM nr_round_pacing"
        )
        conn.execute("DELETE FROM nr_pacing_outputs WHERE output_id=99")
        if keep_row:
            conn.execute(
                "INSERT INTO nr_pacing_outputs(output_id,session_id,round_id,kind) SELECT 10,session_id,round_id,'warning' FROM nr_round_pacing"
            )
    await bootstrap.initialize_database(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO nr_pacing_outputs(session_id,round_id,kind,attempt) SELECT session_id,round_id,'warning',2 FROM nr_round_pacing"
        )
        assert conn.execute("SELECT MAX(output_id) FROM nr_pacing_outputs").fetchone()[0] == 100
    assert rows(path, "PRAGMA foreign_key_check") == []
