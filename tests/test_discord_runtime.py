"""Real WAL/engine integration through the live adapter's injected transport."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from test_ordinary_flow import NOW, rows

from lets_play.discord_adapter import runtime as runtime_module
from lets_play.discord_adapter.discord_io import render
from lets_play.discord_adapter.lane import Lane, LaneUnavailable
from lets_play.discord_adapter.runtime import OpeningRuntime
from lets_play.new_rules.contracts import EventEnvelope, EventKind, MessageEvent
from lets_play.storage.game_store import open_game_store
from lets_play.storage.opening_flow import OpeningCapabilities, OpeningStatus

CAPS = OpeningCapabilities(True, True, True)


class Clock:
    def __init__(self):
        self.at = NOW

    def now(self):
        return self.at


class Transport:
    def __init__(self):
        self.messages = []
        self.markers = []
        self.pauses = []
        self.block = None
        self.fail = None
        self.entered = asyncio.Event()

    async def publish(self, route, kind, state):
        self.messages.append((route, kind, state))
        self.entered.set()
        if self.block:
            await self.block.wait()
        if self.fail == kind:
            raise TimeoutError("content-bearing error must not be logged")
        embed, view = render(state, kind=kind)
        assert len(embed) <= 6000
        assert all(len(c.custom_id) <= 100 for c in view.children)
        return 1000 + len(self.messages)

    async def mark(self, route, source):
        self.markers.append(source)

    async def refresh(self, route, state):
        render(state)

    async def paused(self, route, state):
        self.pauses.append(route)


@asynccontextmanager
async def service(tmp_path, monkeypatch, *, transport=None):
    monkeypatch.setattr(runtime_module, "OPENING_CARDS", frozenset({"NR004", "NR006", "NR034"}))
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        runtime = OpeningRuntime(store, transport or Transport(), clock=Clock())
        await runtime.initialize()
        try:
            yield runtime
        finally:
            await runtime.close()


async def until(runtime, route, predicate):
    async with asyncio.timeout(5):
        while True:
            state = await runtime.state(route.session_id)
            if predicate(state):
                return state
            await asyncio.sleep(0.001)


async def drain(runtime):
    while runtime.publishers:
        await asyncio.gather(*tuple(runtime.publishers.values()))


async def opened(runtime):
    route, status = await runtime.start(101, 202, 303, "request", CAPS)
    assert status is OpeningStatus.ADMITTED
    await until(runtime, route, lambda s: s.admission.stage == "lobby_published")
    return route


async def choice(runtime, route):
    state = await runtime.state(route.session_id)
    message = state.panel_id
    for actor in (404, 505):
        await runtime.submit(route, "join", actor=actor, message_id=message, capabilities=CAPS)
    return await until(runtime, route, lambda s: s.admission.stage == "opening_published")


async def activated(runtime, route):
    state = await choice(runtime, route)
    index = next(
        i for i, o in enumerate(state.admission.hand.options) if o.incoming.card_id == "NR034"
    )
    result = await runtime.submit(
        route, "choose", actor=state.admission.picker_id, message_id=state.panel_id, index=index
    )
    assert result is OpeningStatus.SELECTED
    state = await until(
        runtime, route, lambda s: len(runtime.transport.messages) == 3 and s.panel_id == 1003
    )
    runtime.clock.at = state.admission.effective_at
    await runtime.submit(route, "tick")
    return await until(runtime, route, lambda s: s.admission.stage == "active")


def event(runtime, route, source, content="over", actor=606):
    now = runtime.clock.now()
    return MessageEvent(
        EventEnvelope(route, EventKind.MESSAGE_CREATE, str(source), actor, now, now, 0), content
    )


@pytest.mark.asyncio
async def test_opening_quorum_authorization_scoring_marker_dedup_and_end(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await runtime.state(route.session_id)
        for _ in range(2):
            assert (
                await runtime.submit(
                    route, "join", actor=303, message_id=state.panel_id, capabilities=CAPS
                )
                is OpeningStatus.ALREADY_DONE
            )
        assert (await runtime.state(route.session_id)).admission.joined == 1
        state = await choice(runtime, route)
        assert (
            await runtime.submit(route, "choose", actor=999, message_id=state.panel_id, index=0)
            is OpeningStatus.UNAUTHORIZED
        )
        assert (await runtime.state(route.session_id)).admission.stage == "opening_published"
        # Wrong-message controls cannot mutate even if the actor is the picker.
        assert (
            await runtime.submit(
                route, "choose", actor=state.admission.picker_id, message_id=99, index=0
            )
            == "stale"
        )
        index = next(
            i for i, o in enumerate(state.admission.hand.options) if o.incoming.card_id == "NR034"
        )
        await runtime.submit(
            route, "choose", actor=state.admission.picker_id, message_id=state.panel_id, index=index
        )
        state = await until(runtime, route, lambda s: s.panel_id == 1003)
        assert state.selected_card == "NR034"
        # A body exactly on the cutover activates AND scores once.
        runtime.clock.at = state.admission.effective_at
        for post in (
            event(runtime, route, 9001, "hello"),
            event(runtime, route, 9001, "hello"),
            event(runtime, route, 9002, "-# anything"),
            event(runtime, route, 9003, "over"),
        ):
            await runtime.submit(route, "event", event=post)
        await drain(runtime)
        assert runtime.transport.markers == [9001]
        async with runtime.store.transaction(write=False) as unit:
            score = await unit.get_score(route.session_id, 606)
        assert (score.ordinary_posts, score.ordinary_faults) == (2, 1)
        async with runtime.store.transaction(write=False) as unit:
            assert (await unit.get_traffic(route.session_id)).whispers == 1
        assert (
            await runtime.submit(route, "end", actor=606, authorized=False)
            is OpeningStatus.UNAUTHORIZED
        )
        await runtime.submit(route, "end", actor=303, authorized=True)
        await until(runtime, route, lambda s: s.admission.stage == "ended")
        await drain(runtime)
        assert route.session_id not in runtime.lanes
        assert (101, 202) not in runtime.routes
        assert [m[1] for m in runtime.transport.messages] == [
            "lobby",
            "offer",
            "activation",
            "closed",
        ]
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (1,)
        ]


@pytest.mark.asyncio
async def test_unsent_lobby_has_no_clock_and_exact_expiry_releases_capacity(tmp_path, monkeypatch):
    transport = Transport()
    transport.block = asyncio.Event()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route, _ = await runtime.start(101, 202, 303, "request", CAPS)
        await transport.entered.wait()
        runtime.clock.at += timedelta(seconds=400)
        assert (await runtime.state(route.session_id)).admission.lobby_deadline is None
        transport.block.set()
        state = await until(runtime, route, lambda s: s.admission.stage == "lobby_published")
        assert state.admission.lobby_deadline == runtime.clock.now() + timedelta(seconds=180)
        runtime.clock.at = state.admission.lobby_deadline
        result = await runtime.submit(
            route, "join", actor=404, message_id=state.panel_id, capabilities=CAPS
        )
        assert result is OpeningStatus.ENDED
        assert (await runtime.state(route.session_id)).admission.joined == 1


@pytest.mark.asyncio
async def test_choice_timeout_runs_without_chat_and_keeps_original_clock(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await choice(runtime, route)
        original = state.admission.choice_deadline
        runtime.clock.at += timedelta(seconds=15)
        await runtime.submit(route, "choose", actor=999, message_id=state.panel_id, index=0)
        assert (await runtime.state(route.session_id)).admission.choice_deadline == original
        runtime.clock.at = original.expires_at
        await runtime.submit(route, "timeout")
        state = await until(runtime, route, lambda s: s.panel_id == 1003)
        runtime.clock.at = state.admission.effective_at
        await runtime.submit(route, "tick")
        assert (await runtime.state(route.session_id)).admission.stage == "active"
        assert rows(
            tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_badges WHERE badge_key='rulemaker'"
        ) == [(0,)]


@pytest.mark.asyncio
async def test_uncertain_send_pauses_without_blind_retry_or_scores(tmp_path, monkeypatch, caplog):
    transport = Transport()
    transport.fail = "lobby"
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route, _ = await runtime.start(101, 202, 303, "request", CAPS)
        await until(runtime, route, lambda s: bool(s.recovery_id))
        await runtime.submit(route, "event", event=event(runtime, route, 9090))
        assert (await runtime.state(route.session_id)).context is None
        assert len(transport.messages) == 1
        assert "content-bearing" not in caplog.text
        transport.fail = None
        await runtime.submit(route, "end", actor=303, authorized=True)
        await until(runtime, route, lambda s: s.admission.stage == "ended")
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT state FROM nr_discord_deliveries ORDER BY output_id LIMIT 1",
        ) == [("sending",)]


@pytest.mark.asyncio
async def test_reopen_enters_durable_pause_before_any_output_or_scoring(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 9100))
    async with service(tmp_path, monkeypatch) as restarted:
        state = await restarted.state(route.session_id)
        assert state.recovery_id
        assert state.context.position.lifecycle == "paused"
        await restarted.submit(route, "event", event=event(restarted, route, 9101))
        async with restarted.store.transaction(write=False) as unit:
            score = await unit.get_score(route.session_id, 606)
        assert score.ordinary_posts == 1
        assert not restarted.transport.messages
        assert restarted.transport.pauses == [route]
        await restarted.submit(route, "end", actor=303, authorized=True)
        assert (await restarted.state(route.session_id)).admission.stage == "ended"


@pytest.mark.asyncio
async def test_bounded_lane_preserves_fifo_and_fences_overflow():
    clock = Clock()
    observed = []
    failures = []

    async def handle(request):
        observed.append(request.data["n"])

    async def fault(reason):
        failures.append(reason)

    lane = Lane(handle, fault, clock, capacity=2)
    try:
        first = lane.submit("test", n=1)
        second = lane.submit("test", n=2)
        with pytest.raises(LaneUnavailable):
            await lane.submit("test", n=3)
        await first
        with pytest.raises(LaneUnavailable):
            await second
        await lane.task
        assert observed == [1]
        assert failures
    finally:
        await lane.close()


@pytest.mark.asyncio
async def test_new_disruption_supersedes_old_recovery_without_restarting_choice(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await choice(runtime, route)
        original = state.admission.choice_deadline
        await runtime.pause(route)
        old = (await runtime.state(route.session_id)).recovery_id
        runtime.clock.at += timedelta(seconds=25)
        await runtime.initialize()
        state = await runtime.state(route.session_id)
        assert state.recovery_id != old
        assert state.admission.choice_deadline == original
        assert [m[1] for m in runtime.transport.messages] == ["lobby", "offer"]
        assert rows(
            tmp_path / "game.sqlite3", "SELECT stage FROM nr_recoveries ORDER BY began_at_utc"
        ) == [("superseded",), ("paused",)]
