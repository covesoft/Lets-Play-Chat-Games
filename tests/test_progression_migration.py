"""Frozen released-schema upgrades and unresolved-closure reconciliation."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack, closing
from pathlib import Path

import pytest
from test_ordinary_flow import execute, rows
from test_progression import profile, progress

from lets_play.storage import bootstrap
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import open_game_store
from lets_play.storage.migrations import Migration
from lets_play.storage.progression import Progression, ResolutionOrderError, ResolutionStatus


def released_three(path, *, populated=True):
    filename = "released_schema3_progress.sql" if populated else "released_schema3.sql"
    fixture = Path(__file__).parent / "fixtures" / filename
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(fixture.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_schema_three_upgrade_preserves_scores_offers_watermark_and_unknown_times(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_three(path)
    snapshots = {
        table: rows(path, f"SELECT * FROM {table}")
        for table in (
            "nr_participants",
            "nr_round_scores",
            "nr_offers",
            "nr_activations",
            "nr_round_closures",
            "nr_flow_watermarks",
        )
    }
    assert (await bootstrap.initialize_database(path)).schema_version == bootstrap.SCHEMA_VERSION
    for table, before in snapshots.items():
        assert rows(path, f"SELECT * FROM {table}") == before
    assert rows(path, "SELECT round_id,round_number FROM nr_rounds ORDER BY round_number") == [
        ("z-first", 1),
        ("offer-one:round", 2),
        ("a-second-offer:round", 3),
    ]
    assert rows(
        path, "SELECT actor_id,first_round_number FROM nr_player_progress ORDER BY actor_id"
    ) == [(303, 1), (404, 2)]
    assert rows(path, "SELECT started_at_utc FROM nr_rounds WHERE round_number=1") == [(None,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_profiles") == [(0,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_badges") == [(0,)]
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_migration_four_failure_restores_schema_three_ddl_metadata_and_real_scores(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    released_three(path)
    before = rows(path, "SELECT * FROM nr_participants")
    original = bootstrap.MIGRATIONS
    failed = Migration(4, original[3].name, (*original[3].statements, "INVALID SQL"))
    monkeypatch.setattr(bootstrap, "MIGRATIONS", (*original[:3], failed))
    with pytest.raises(sqlite3.OperationalError):
        await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(3,)]
    assert rows(path, "SELECT COUNT(*) FROM schema_migrations") == [(3,)]
    assert rows(path, "SELECT * FROM nr_participants") == before
    assert rows(path, "SELECT COUNT(*) FROM sqlite_schema WHERE name='nr_profiles'") == [(0,)]
    assert "round_number" not in {r[1] for r in rows(path, "PRAGMA table_info(nr_rounds)")}
    assert "progression_version" not in {r[1] for r in rows(path, "PRAGMA table_info(nr_sessions)")}
    monkeypatch.setattr(bootstrap, "MIGRATIONS", original)
    assert (await bootstrap.initialize_database(path)).schema_version == bootstrap.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_reconcile_released_pending_closures_in_creation_order_and_only_once(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_three(path)
    async with open_game_store(path) as store:
        async with store.transaction(write=False) as unit:
            assert await Progression(unit).pending_rounds("session", limit=1) == ("z-first",)
        with pytest.raises(ResolutionOrderError):
            async with store.transaction() as unit:
                await Progression(unit).resolve_round("session", "offer-one:round")
        async with store.transaction() as unit:
            assert (
                await Progression(unit).resolve_round("session", "z-first")
                is ResolutionStatus.RESOLVED
            )
        assert (await progress(store)).current_clean_streak == 1
        assert (await progress(store, 404)).personal_rounds_played == 0
        assert (await progress(store, 404)).current_clean_streak == 0
        assert (await profile(store)).games_played == 1
        assert (await profile(store, 404)).games_played == 1
        async with store.transaction() as unit:
            assert (
                await Progression(unit).resolve_round("session", "offer-one:round")
                is ResolutionStatus.RESOLVED
            )
        p = await progress(store)
        assert (p.current_clean_streak, p.best_clean_streak, p.personal_rounds_played) == (0, 1, 1)
        late = await progress(store, 404)
        assert (late.current_clean_streak, late.personal_rounds_played) == (1, 1)
        assert set((await profile(store)).badges) == {"rulemaker", "spotless"}
        assert (await profile(store, 404)).badges == ("spotless",)
    async with open_game_store(path) as store:
        async with store.transaction() as unit:
            api = Progression(unit)
            assert await api.pending_rounds("session") == ()
            assert await api.resolve_round("session", "z-first") is ResolutionStatus.ALREADY_DONE
        assert await progress(store) == p
        assert (await profile(store)).games_played == 1


@pytest.mark.asyncio
async def test_two_connections_resolve_same_pending_round_once(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_three(path)
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]

        async def resolve(store):
            async with store.transaction() as unit:
                return await Progression(unit).resolve_round("session", "z-first")

        statuses = await asyncio.gather(*(resolve(s) for s in stores))
        assert sorted(statuses) == sorted(
            [ResolutionStatus.RESOLVED, ResolutionStatus.ALREADY_DONE]
        )
        assert (await profile(stores[0])).games_played == 1
        assert (await progress(stores[0])).current_clean_streak == 1


@pytest.mark.asyncio
async def test_legacy_participant_without_round_evidence_is_not_given_invented_chronology(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_three(path)
    execute(path, "DELETE FROM nr_round_scores WHERE actor_id=303")
    async with open_game_store(path) as store:
        assert rows(
            path, "SELECT first_round_number FROM nr_player_progress WHERE actor_id=303"
        ) == [(None,)]
        with pytest.raises(IncompatibleState, match="enrollment"):
            async with store.transaction() as unit:
                await Progression(unit).resolve_round("session", "z-first")
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert (await profile(store)).games_played == 0


@pytest.mark.asyncio
async def test_reconciliation_late_failure_rolls_back_round_guards_credit_and_badges(tmp_path):
    path = tmp_path / "game.sqlite3"
    released_three(path)
    async with open_game_store(path) as store:
        execute(
            path,
            "CREATE TRIGGER fail_progress BEFORE UPDATE ON nr_player_progress WHEN NEW.current_clean_streak>0 BEGIN SELECT RAISE(ABORT,'late progress failure'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            async with store.transaction() as unit:
                await Progression(unit).resolve_round("session", "z-first")
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_profiles") == [(0,)]
        assert rows(path, "SELECT SUM(game_count_credited) FROM nr_player_progress") == [(0,)]
        execute(path, "DROP TRIGGER fail_progress")
        async with store.transaction() as unit:
            assert (
                await Progression(unit).resolve_round("session", "z-first")
                is ResolutionStatus.RESOLVED
            )
