"""Released send claims survive the schema-10 multi-family guard migration."""

import sqlite3
from contextlib import closing

import pytest
from test_ordinary_flow import rows
from test_recovery_migration import released_seven

from lets_play.storage import bootstrap
from lets_play.storage.migrations import Migration


async def released_nine(path, monkeypatch):
    released_seven(path, True)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", bootstrap.MIGRATIONS[:9])
        assert (await bootstrap.initialize_database(path)).schema_version == 9
    with closing(sqlite3.connect(path)) as conn, conn:
        session = conn.execute("SELECT session_id FROM nr_admissions LIMIT 1").fetchone()[0]
        for kind, state, message in [("lobby", "sending", None), ("offer", "delivered", 123)]:
            conn.execute(
                "INSERT INTO nr_opening_outputs(session_id,kind) VALUES (?,?) ON CONFLICT DO NOTHING",
                (session, kind),
            )
            output = conn.execute(
                "SELECT output_id FROM nr_opening_outputs WHERE session_id=? AND kind=?",
                (session, kind),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO nr_discord_deliveries VALUES (?,?,?,?)",
                (output, state, 123456, message),
            )
    return session


@pytest.mark.asyncio
async def test_nine_to_ten_preserves_claims_and_all_other_live_rows(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    session = await released_nine(path, monkeypatch)
    tables = [
        r[0]
        for r in rows(
            path,
            "SELECT name FROM sqlite_schema WHERE type='table' AND name LIKE 'nr_%' AND name<>'nr_discord_deliveries'",
        )
    ]
    before = {t: rows(path, f"SELECT * FROM {t}") for t in tables}
    claims = rows(path, "SELECT * FROM nr_discord_deliveries ORDER BY output_id")
    assert (await bootstrap.initialize_database(path)).schema_version == 10
    assert {t: rows(path, f"SELECT * FROM {t}") for t in tables} == before
    assert rows(
        path,
        "SELECT session_id,family,output_id,state,attempted_at_utc,message_id,ack_sequence FROM nr_discord_deliveries ORDER BY output_id",
    ) == [(session, "opening", *row, 0) for row in claims]
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    # A different outbox's identity 1 must not collide with opening's identity 1.
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO nr_discord_deliveries(session_id,family,output_id,state,attempted_at_utc) VALUES (?,'ordinary',?,'sending',123456)",
            (session, claims[0][0]),
        )
    assert rows(path, "SELECT COUNT(*) FROM nr_discord_deliveries") == [(len(claims) + 1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("after", [1, 2, 3, 4, 5])
async def test_failed_tenth_migration_restores_original_guard_and_scores(
    tmp_path, monkeypatch, after
):
    path = tmp_path / "game.sqlite3"
    await released_nine(path, monkeypatch)
    tables = [
        r[0]
        for r in rows(
            path,
            "SELECT name FROM sqlite_schema WHERE type='table' AND (name LIKE 'nr_%' OR name='schema_migrations')",
        )
    ]
    before = {t: rows(path, f"SELECT * FROM {t}") for t in tables}
    migrations = bootstrap.MIGRATIONS
    broken = Migration(10, migrations[9].name, (*migrations[9].statements[:after], "INVALID SQL"))
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", (*migrations[:9], broken))
        with pytest.raises(sqlite3.OperationalError):
            await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(9,)]
    assert {t: rows(path, f"SELECT * FROM {t}") for t in tables} == before
    assert rows(path, "SELECT name FROM sqlite_schema WHERE name='nr_discord_deliveries_v9'") == []
    assert (await bootstrap.initialize_database(path)).schema_version == 10
