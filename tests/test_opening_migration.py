"""Released schema-5 snapshots preserve real score/rotation facts through admission upgrade."""

import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from test_opening import Lane
from test_ordinary_flow import NOW, rows

from lets_play.new_rules.contracts import EventEnvelope, EventKind, MessageEvent, SessionRoute
from lets_play.storage import bootstrap
from lets_play.storage.game_store import ApplyStatus, open_game_store
from lets_play.storage.migrations import SCHEMA_VERSION, Migration
from lets_play.storage.opening_flow import OpeningFlow, OpeningStatus


def released_five(path):
    fixture = Path(__file__).parent / "fixtures/released_schema5_progress.sql"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(fixture.read_text())


@pytest.mark.asyncio
async def test_schema_five_upgrade_registers_legacy_capacity_without_guessing_start_or_phrase_window(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    released_five(path)
    tables = (
        "nr_sessions",
        "nr_rounds",
        "nr_participants",
        "nr_round_scores",
        "nr_profiles",
        "nr_badges",
        "nr_player_progress",
        "nr_offers",
        "nr_flow_watermarks",
        "nr_round_pacing",
        "nr_rotation",
        "nr_picker_candidates",
    )
    columns = {t: ",".join(r[1] for r in rows(path, f"PRAGMA table_info({t})")) for t in tables}
    before = {t: rows(path, f"SELECT {columns[t]} FROM {t}") for t in tables}
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION == 11
    for table, value in before.items():
        actual = rows(path, f"SELECT {columns[table]} FROM {table}")
        if table == "nr_sessions":
            pin = columns[table].split(",").index("pacing_version")
            value = [(*r[:pin], "ordinary-pacing-2", *r[pin + 1 :]) for r in value]
        assert actual == value
    assert rows(
        path,
        "SELECT session_id,stage,playing_session_id,phrase_draw_limit,prepared_at_utc FROM nr_admissions",
    ) == [("smoke", "legacy", "smoke", None, None)]
    assert rows(path, "SELECT COUNT(*) FROM nr_guild_entitlements") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_lobby_members") == [(0,)]
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    async with open_game_store(path) as store:
        assert await Lane(store, session="new", channel=999).admit(at=400) is OpeningStatus.CAPACITY


@pytest.mark.asyncio
async def test_failed_migration_six_restores_five_metadata_and_populated_state(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    released_five(path)
    before = rows(path, "SELECT * FROM nr_profiles")
    original = bootstrap.MIGRATIONS
    failed = Migration(6, original[5].name, (*original[5].statements, "INVALID SQL"))
    monkeypatch.setattr(bootstrap, "MIGRATIONS", (*original[:5], failed))
    with pytest.raises(sqlite3.OperationalError):
        await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(5,)]
    assert rows(path, "SELECT COUNT(*) FROM schema_migrations") == [(5,)]
    assert rows(path, "SELECT * FROM nr_profiles") == before
    assert rows(path, "SELECT COUNT(*) FROM sqlite_schema WHERE name='nr_admissions'") == [(0,)]
    monkeypatch.setattr(bootstrap, "MIGRATIONS", original)
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION


@pytest.mark.asyncio
async def test_migrated_playing_session_keeps_its_original_flow_watermark(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_five(path)
    async with open_game_store(path) as store:
        old = rows(path, "SELECT last_sequence,observed_at_utc FROM nr_flow_watermarks")[0]
        frame = EventEnvelope(
            SessionRoute(101, 202, "smoke"),
            EventKind.MESSAGE_CREATE,
            "post-upgrade",
            303,
            NOW + timedelta(seconds=400),
            NOW + timedelta(seconds=400),
            old[0] + 1,
        )
        async with store.transaction() as unit:
            result = await OpeningFlow(unit).ingest(MessageEvent(frame, "over"))
        assert result.score_status is ApplyStatus.APPLIED
        assert rows(path, "SELECT last_sequence FROM nr_flow_watermarks") == [(old[0] + 1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_admissions") == [(1,)]


@pytest.mark.asyncio
async def test_migrated_ended_delivery_records_do_not_consume_capacity(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_five(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE nr_sessions SET lifecycle='ended'")
    async with open_game_store(path) as store:
        assert rows(path, "SELECT stage FROM nr_admissions") == [("ended",)]
        assert await Lane(store, session="new", channel=202).admit(at=400) is OpeningStatus.ADMITTED


@pytest.mark.parametrize(
    "change", ["format", "parser", "extra", "phrase_shape", "too_many", "unknown_card"]
)
def test_opening_config_codec_refuses_unknown_or_unbounded_state(change):
    from lets_play.storage.game_codec import IncompatibleState
    from lets_play.storage.opening_codec import decode_config, encode_config

    data = json.loads(encode_config((), frozenset({"NR034", "NR004", "NR006"})))
    if change == "format":
        data["format"] = True
    elif change == "parser":
        data["parser"] = "future"
    elif change == "extra":
        data["raw_message"] = "PRIVATE_RAW_CANARY"
    elif change == "phrase_shape":
        data["phrases"] = [{"id": "x", "text": "duck", "revision": "r"}]
    elif change == "too_many":
        data["phrases"] = [["x", "duck", "r"]] * 16
    else:
        data["templates"] = ["NR999"]
    with pytest.raises(IncompatibleState):
        decode_config(json.dumps(data))
