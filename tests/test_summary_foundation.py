"""Offline summaries over the real engine/WAL store; no Discord output is added."""

import asyncio
import sqlite3
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from test_discord_runtime import activated, event, opened, service
from test_ordinary_flow import rows

from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import open_game_store
from lets_play.storage.recovery_flow import RecoveryRequired
from lets_play.storage.summary_store import SummaryStore


@pytest.fixture(autouse=True)
def offline_publisher(monkeypatch):
    # These foundation cases manually claim/ACK. Integration delivery is covered
    # in test_public_summary; the runtime must not race their simulated sender.
    from lets_play.discord_adapter.runtime import OpeningRuntime

    monkeypatch.setattr(OpeningRuntime, "_publish_summary", AsyncMock(return_value=False))
    monkeypatch.setattr(OpeningRuntime, "_maybe_auto", AsyncMock())


async def capture(runtime, route, key="one", kind="requested"):
    async with runtime.store.transaction() as unit:
        return await SummaryStore(unit).prepare(
            route.session_id, key, now=runtime.clock.now(), kind=kind
        )


async def baseline(runtime, route):
    async with runtime.store.transaction(write=False) as unit:
        async with unit._conn.execute(
            "SELECT public_at_utc,ack_messages FROM nr_summary_baselines WHERE session_id=?",
            (route.session_id,),
        ) as cursor:
            return tuple(await cursor.fetchone())


async def post(runtime, route, n, text="over"):
    await runtime.submit(route, "event", event=event(runtime, route, 70000 + n, text, 606))


@pytest.mark.asyncio
async def test_automatic_capture_needs_both_gates_without_new_message_at_deadline(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        start = runtime.clock.now()
        async with runtime.store.transaction() as unit:
            await SummaryStore(unit).initialize(route.session_id)
        runtime.clock.at = start + timedelta(seconds=119)
        for n in range(25):
            await post(runtime, route, n, "-# listening")
        assert await capture(runtime, route, kind="automatic") is None
        for n in range(25, 50):
            await post(runtime, route, n)
        async with runtime.store.transaction(write=False) as unit:
            assert await SummaryStore(unit).next_deadline(route.session_id) == start + timedelta(
                seconds=120
            )
        runtime.clock.at += timedelta(seconds=1)
        snapshot = await capture(runtime, route, kind="automatic")
        assert snapshot.human_messages == 50 and snapshot.whispers == 25
        assert snapshot.round_messages == 50 and snapshot.round_whispers == 25
        assert runtime.transport.messages[-1][1] == "activation"


@pytest.mark.asyncio
async def test_time_alone_and_commands_do_not_fill_traffic_gate(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        runtime.clock.at += timedelta(seconds=121)
        for n in range(24):
            await post(runtime, route, n)
        await runtime.submit(route, "tick")
        assert await capture(runtime, route, kind="automatic") is None
        await post(runtime, route, 24)
        assert await capture(runtime, route, kind="automatic")


@pytest.mark.asyncio
async def test_snapshot_is_frozen_idempotent_and_pages_both_scopes(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await post(runtime, route, 1, "wrong")
        first = await capture(runtime, route)
        await post(runtime, route, 2)
        repeated = await capture(runtime, route)
        assert repeated == first and first.human_messages == 1
        assert [r.card_id for r in first.rules] == ["NR034"]
        async with runtime.store.transaction(write=False) as unit:
            summaries = SummaryStore(unit)
            for scope in ("game", "round"):
                page = await summaries.scores(
                    route.session_id, "one", scope=scope, after_actor=505, limit=1
                )
                actor, score = page[0]
                assert actor == 606 and score.ordinary_posts == score.ordinary_faults == 1
                assert score.actual_percent == 0
            assert len(await summaries.scores(route.session_id, "one", limit=2)) == 2
            assert await summaries.scores("other-session", "one") == ()
        with pytest.raises(ValueError):
            await capture(runtime, route, kind="automatic")


@pytest.mark.asyncio
async def test_public_ack_preserves_arrivals_during_send_and_retry_does_not_reset_time(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await post(runtime, route, 1)
        snapshot = await capture(runtime, route)
        async with runtime.store.transaction() as unit:
            assert await SummaryStore(unit).claim(route.session_id, "one", now=runtime.clock.now())
        for n in range(2, 27):
            await post(runtime, route, n, "over")
        runtime.clock.at += timedelta(seconds=10)
        async with runtime.store.transaction() as unit:
            assert await SummaryStore(unit).acknowledge(
                route.session_id, "one", message_id=999, now=runtime.clock.now()
            )
        acknowledged = await baseline(runtime, route)
        assert acknowledged[1] == snapshot.human_messages == 1
        async with runtime.store.transaction(write=False) as unit:
            assert await SummaryStore(unit).next_deadline(
                route.session_id
            ) == runtime.clock.now() + timedelta(seconds=120)
        runtime.clock.at += timedelta(seconds=5)
        async with runtime.store.transaction() as unit:
            assert not await SummaryStore(unit).acknowledge(
                route.session_id, "one", message_id=999, now=runtime.clock.now()
            )
        assert await baseline(runtime, route) == acknowledged
        with pytest.raises(ValueError):
            async with runtime.store.transaction() as unit:
                await SummaryStore(unit).acknowledge(
                    route.session_id, "one", message_id=1000, now=runtime.clock.now()
                )
        assert await baseline(runtime, route) == acknowledged


@pytest.mark.asyncio
async def test_uncertain_send_preserves_baseline_and_blocks_new_or_replayed_attempts(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await capture(runtime, route)
        before = await baseline(runtime, route)
        async with runtime.store.transaction() as unit:
            summaries = SummaryStore(unit)
            assert await summaries.claim(route.session_id, "one", now=runtime.clock.now())
            assert await summaries.mark_uncertain(route.session_id, "one")
            assert not await summaries.claim(route.session_id, "one", now=runtime.clock.now())
        await post(runtime, route, 1)
        assert await capture(runtime, route, key="fresh-key") is None
        assert await baseline(runtime, route) == before
        async with runtime.store.transaction(write=False) as unit:
            assert await SummaryStore(unit).next_deadline(route.session_id) is None
            assert (await unit.get_score(route.session_id, 606)).ordinary_posts == 1


@pytest.mark.asyncio
async def test_two_connections_can_claim_only_one_send(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await capture(runtime, route)
        async with open_game_store(tmp_path / "game.sqlite3") as other:

            async def claim(store):
                async with store.transaction() as unit:
                    return await SummaryStore(unit).claim(
                        route.session_id, "one", now=runtime.clock.now()
                    )

            assert sorted(await asyncio.gather(claim(runtime.store), claim(other))) == [False, True]


@pytest.mark.asyncio
async def test_failure_during_score_copy_rolls_back_header_baseline_and_rows(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        async with runtime.store.transaction() as unit:
            await unit._conn.execute(
                "CREATE TRIGGER fail_summary BEFORE INSERT ON nr_summary_scores BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            await capture(runtime, route)
        for table in ("nr_summary_baselines", "nr_summary_outputs", "nr_summary_scores"):
            assert rows(tmp_path / "game.sqlite3", f"SELECT COUNT(*) FROM {table}") == [(0,)]


@pytest.mark.asyncio
async def test_known_ack_during_recovery_does_not_resume_enforcement(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await post(runtime, route, 1)
        await capture(runtime, route)
        async with runtime.store.transaction() as unit:
            await SummaryStore(unit).claim(route.session_id, "one", now=runtime.clock.now())
        await runtime.pause(route)
        with pytest.raises(RecoveryRequired):
            await capture(runtime, route, key="blocked")
        async with runtime.store.transaction() as unit:
            assert await SummaryStore(unit).acknowledge(
                route.session_id, "one", message_id=999, now=runtime.clock.now()
            )
        assert (await baseline(runtime, route))[1] == 1
        assert (await runtime.state(route.session_id)).recovery_id


@pytest.mark.asyncio
async def test_reopen_preserves_claim_and_frozen_scores_without_sending(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await post(runtime, route, 1)
        original = await capture(runtime, route)
        async with runtime.store.transaction() as unit:
            await SummaryStore(unit).claim(route.session_id, "one", now=runtime.clock.now())
    async with service(tmp_path, monkeypatch) as restarted:
        async with restarted.store.transaction(write=False) as unit:
            snapshot = await SummaryStore(unit).snapshot(route.session_id, "one")
            assert snapshot.state == "sending" and snapshot.rules == original.rules
            assert (await SummaryStore(unit).scores(route.session_id, "one", after_actor=505))[0][
                1
            ].ordinary_posts == 1
        assert not restarted.transport.messages


@pytest.mark.asyncio
async def test_missing_activation_clock_is_not_guessed(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        async with runtime.store.transaction() as unit:
            await unit._conn.execute(
                "UPDATE nr_rounds SET started_at_utc=NULL WHERE session_id=?", (route.session_id,)
            )
        with pytest.raises(IncompatibleState):
            await capture(runtime, route)


@pytest.mark.asyncio
@pytest.mark.parametrize("scope,actor,limit", [("bad", 0, 20), ("game", -1, 20), ("game", 0, 101)])
async def test_page_bounds_are_checked(tmp_path, monkeypatch, scope, actor, limit):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await capture(runtime, route)
        with pytest.raises(ValueError):
            async with runtime.store.transaction(write=False) as unit:
                await SummaryStore(unit).scores(
                    route.session_id, "one", scope=scope, after_actor=actor, limit=limit
                )
