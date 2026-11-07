"""Schema-4 upgrade and rollback use released DDL independently of migration 5."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_ordinary_flow import POSITION, rows

from lets_play.new_rules.evaluation import EVALUATOR_VERSION, EvaluationContext
from lets_play.new_rules.parsing import PARSER_VERSION
from lets_play.new_rules.scoring import SCORE_VERSION
from lets_play.storage import bootstrap
from lets_play.storage.game_codec import encode_context
from lets_play.storage.game_store import open_game_store
from lets_play.storage.migrations import SCHEMA_VERSION, Migration
from lets_play.storage.pacing_flow import RoundPacing


def released_four(path):
    fixture = Path(__file__).parent / "fixtures/released_schema4.sql"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(fixture.read_text())
        conn.execute(
            """INSERT INTO nr_sessions(session_id,guild_id,channel_id,lifecycle,phase,
            advancement,round_id,ruleset_version,revision,context_json,parser_version,
            evaluator_version,score_version,human_messages,whispers)
            VALUES ('session',101,202,'playing','ordinary','idle','old-round',1,0,?,?,?,?,7,1)""",
            (
                encode_context(EvaluationContext(POSITION, ())),
                PARSER_VERSION,
                EVALUATOR_VERSION,
                SCORE_VERSION,
            ),
        )
        conn.execute(
            "INSERT INTO nr_rounds(session_id,round_id,human_messages,whispers) VALUES ('session','old-round',7,1)"
        )
        for table, keys in (
            ("nr_participants", "session_id,actor_id"),
            ("nr_round_scores", "session_id,round_id,actor_id"),
        ):
            values = "'session',303" if table == "nr_participants" else "'session','old-round',303"
            conn.execute(f"""INSERT INTO {table}({keys},human_messages,ordinary_posts,
                ordinary_observations,ordinary_quality_n,ordinary_faults,ordinary_offending_messages)
                VALUES ({values},6,6,6,'5',1,1)""")
        conn.execute(
            "INSERT INTO nr_player_progress(session_id,actor_id,first_round_number,game_count_credited,best_clean_streak,personal_rounds_played) VALUES ('session',303,1,1,1,1)"
        )
        conn.execute("INSERT INTO nr_profiles VALUES (303,9)")
        conn.execute("INSERT INTO nr_badges VALUES (303,'spotless')")


@pytest.mark.asyncio
async def test_upgrade_preserves_schema_four_scores_and_globals_without_starting_old_rounds(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    released_four(path)
    tables = (
        "nr_participants",
        "nr_round_scores",
        "nr_profiles",
        "nr_badges",
        "nr_player_progress",
    )
    before = {table: rows(path, f"SELECT * FROM {table}") for table in tables}
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION == 9
    for table, snapshot in before.items():
        assert rows(path, f"SELECT * FROM {table}") == snapshot
    assert rows(path, "SELECT ordinary_posts,started_at_utc FROM nr_rounds") == [(6, None)]
    assert rows(path, "SELECT COUNT(*) FROM nr_round_pacing") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_pacing_outputs") == [(0,)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    async with open_game_store(path) as store:
        async with store.transaction(write=False) as unit:
            assert await RoundPacing(unit).record("session") is None


@pytest.mark.asyncio
async def test_failed_schema_five_upgrade_restores_schema_four_columns_data_and_metadata(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    released_four(path)
    before = rows(path, "SELECT * FROM nr_rounds")
    original = bootstrap.MIGRATIONS
    fail = Migration(5, original[4].name, (*original[4].statements, "INVALID SQL"))
    monkeypatch.setattr(bootstrap, "MIGRATIONS", (*original[:4], fail))
    with pytest.raises(sqlite3.OperationalError):
        await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(4,)]
    assert rows(path, "SELECT COUNT(*) FROM schema_migrations") == [(4,)]
    assert rows(path, "SELECT * FROM nr_rounds") == before
    assert rows(path, "SELECT * FROM nr_profiles") == [(303, 9)]
    assert rows(path, "SELECT COUNT(*) FROM sqlite_schema WHERE name='nr_round_pacing'") == [(0,)]
    assert "ordinary_posts" not in {row[1] for row in rows(path, "PRAGMA table_info(nr_rounds)")}
    monkeypatch.setattr(bootstrap, "MIGRATIONS", original)
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION
