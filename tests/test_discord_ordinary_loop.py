"""The production adapter and real WAL engine, with injected clock and transport."""

import asyncio
from datetime import timedelta

import pytest
from test_discord_runtime import (
    CAPS,
    Transport,
    activated,
    choice,
    drain,
    event,
    opened,
    service,
    until,
)
from test_ordinary_flow import rows

from lets_play.discord_adapter import runtime as runtime_module
from lets_play.discord_adapter.discord_io import render
from lets_play.discord_adapter.runtime import ORDINARY_CARDS


async def settle(runtime):
    # Drain currently queued controls and publisher ACKs, including follow-up work.
    async with asyncio.timeout(10):
        while True:
            for lane in tuple(runtime.lanes.values()):
                await lane.queue.join()
            await drain(runtime)
            if (
                all(lane.queue.empty() for lane in runtime.lanes.values())
                and not runtime.publishers
            ):
                return


async def posts(runtime, route, *, actors=(303, 404, 505), count=5, base=20000):
    for actor in actors:
        for n in range(count):
            await runtime.submit(
                route, "event", event=event(runtime, route, base + actor * 100 + n, "over", actor)
            )
    await settle(runtime)


async def next_offer(runtime, route):
    state = await runtime.state(route.session_id)
    runtime.clock.at = state.pacing.started_at + timedelta(seconds=270)
    runtime._timer(route, "tick")
    await settle(runtime)
    state = await runtime.state(route.session_id)
    assert state.pacing.stage == "warning_published"
    runtime.clock.at = state.pacing.selection_due_at
    await runtime.submit(route, "tick")
    return await until(
        runtime, route, lambda s: s.offer is not None and s.offer.stage == "published"
    )


@pytest.mark.asyncio
async def test_full_warning_choice_cutover_keeps_old_round_scores_and_starts_new_clock(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await activated(runtime, route)
        await posts(runtime, route)
        state = await runtime.state(route.session_id)
        original = state.context.position
        assert state.pacing.stage == "waiting"
        state = await next_offer(runtime, route)
        assert state.context.position.round_id == original.round_id
        assert len(state.offer.hand.options) == 3
        assert all(o.incoming.card_id in ORDINARY_CARDS for o in state.offer.hand.options)
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]
        old_message = state.panel_id
        assert (
            await runtime.submit(route, "choose", actor=999, message_id=old_message, index=0)
            == "unauthorized"
        )
        picker = state.offer.hand.picker_id
        assert (
            await runtime.submit(route, "choose", actor=picker, message_id=old_message, index=0)
            == "selected"
        )
        state = await until(
            runtime,
            route,
            lambda s: s.offer and s.offer.stage == "selected" and s.panel_id != old_message,
        )
        effective = state.offer.effective_at
        runtime.clock.at = effective - timedelta(microseconds=1)
        await runtime.submit(route, "event", event=event(runtime, route, 99901, "missing", 606))
        assert (
            await runtime.state(route.session_id)
        ).context.position.round_id == original.round_id
        runtime.clock.at = effective
        await runtime.submit(route, "event", event=event(runtime, route, 99902, "over", 606))
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.round_number == 2 and state.pacing.started_at == effective
        assert len(state.context.active_instances) == 2
        async with runtime.store.transaction(write=False) as unit:
            old = await unit.get_score(route.session_id, 606, round_id=original.round_id)
            new = await unit.get_score(
                route.session_id, 606, round_id=state.context.position.round_id
            )
        assert old.ordinary_posts == 1 and old.ordinary_faults == 1
        assert new.ordinary_posts == 1
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (1,)
        ]
        assert (
            await runtime.submit(route, "choose", actor=picker, message_id=old_message, index=0)
            == "stale"
        )
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (1,)
        ]


@pytest.mark.asyncio
async def test_delayed_warning_receives_full_thirty_seconds_and_cutoff_fifth_post_is_not_eligible(
    tmp_path, monkeypatch
):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route, actors=(303,), count=5)
        await posts(runtime, route, actors=(404,), count=4, base=30000)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.pacing.started_at + timedelta(seconds=870)
        transport.block = asyncio.Event()
        transport.entered.clear()
        await runtime.submit(route, "tick")
        await transport.entered.wait()
        runtime.clock.at += timedelta(seconds=20)
        assert (await runtime.state(route.session_id)).pacing.selection_due_at is None
        transport.block.set()
        state = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
        assert state.pacing.selection_due_at == runtime.clock.now() + timedelta(seconds=30)
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "event", event=event(runtime, route, 99903, "over", 404))
        state = await until(runtime, route, lambda s: s.offer is not None)
        assert state.offer.hand.picker_id == 303
        # The cutoff body still scores in the old round, but the snapshot is fixed.
        async with runtime.store.transaction(write=False) as unit:
            assert (
                await unit.get_score(
                    route.session_id, 404, round_id=state.context.position.round_id
                )
            ).ordinary_posts == 5


@pytest.mark.asyncio
async def test_no_eligible_picker_revival_has_three_returners_and_preserves_round(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await runtime.state(route.session_id)
        old = state.context.position.round_id
        runtime.clock.at = state.pacing.started_at + timedelta(seconds=870)
        await runtime.submit(route, "tick")
        state = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        state = await until(runtime, route, lambda s: s.revival and s.revival.stage == "published")
        assert state.context.position.lifecycle == "paused"
        assert state.revival.joined == 0
        original = state.revival.deadline
        message = state.panel_id
        assert await runtime.submit(route, "revive", actor=303, message_id=message) == "joined"
        assert (
            await runtime.submit(route, "revive", actor=303, message_id=message) == "already_joined"
        )
        assert (await runtime.state(route.session_id)).revival.deadline == original
        await runtime.submit(route, "event", event=event(runtime, route, 99904, "ignored", 606))
        async with runtime.store.transaction(write=False) as unit:
            assert await unit.get_score(route.session_id, 606) is None
        assert await runtime.submit(route, "revive", actor=404, message_id=message) == "joined"
        assert await runtime.submit(route, "revive", actor=505, message_id=message) == "revived"
        state = await until(runtime, route, lambda s: s.offer and s.offer.stage == "published")
        assert state.context.position.round_id == old
        assert state.context.position.lifecycle == "playing"
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]


@pytest.mark.asyncio
async def test_revival_expiry_uses_only_existing_panel_and_releases_capacity(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.pacing.started_at + timedelta(seconds=870)
        await runtime.submit(route, "tick")
        state = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        state = await until(runtime, route, lambda s: s.revival and s.revival.stage == "published")
        runtime.clock.at = state.revival.deadline
        await runtime.submit(route, "revive", actor=606, message_id=state.panel_id)
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.admission.stage == "ended"
        assert runtime.transport.messages[-1][1] == "evolution_closed_silent"
        assert (101, 202) not in runtime.routes
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (1,)
        ]


@pytest.mark.asyncio
async def test_held_play_continues_scoring_and_uses_whispers_for_idle(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        state = await runtime.state(route.session_id)
        old = state.context.position.round_id
        await runtime.submit(route, "configure", templates=frozenset({"NR034"}))
        await settle(runtime)
        assert (await runtime.state(route.session_id)).context.position.advancement == "held"
        await runtime.submit(route, "event", event=event(runtime, route, 99905, "wrong", 606))
        runtime.clock.at += timedelta(seconds=899)
        await runtime.submit(
            route, "event", event=event(runtime, route, 99906, "-# still here", 606)
        )
        await runtime.submit(route, "tick")
        state = await runtime.state(route.session_id)
        assert (
            state.context.position.round_id == old and state.context.position.lifecycle == "playing"
        )
        async with runtime.store.transaction(write=False) as unit:
            assert (await unit.get_score(route.session_id, 606)).ordinary_faults == 1
        assert state.next_deadline == runtime.clock.now() + timedelta(seconds=900)
        await runtime.submit(route, "configure", templates=ORDINARY_CARDS)
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.context.position.round_id == old
        assert state.pacing.stage == "warning_published"


@pytest.mark.asyncio
async def test_rules_during_selected_transition_show_old_rules_until_cutover(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route)
        state = await next_offer(runtime, route)
        await runtime.submit(
            route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=0
        )
        state = await until(runtime, route, lambda s: s.offer and s.offer.stage == "selected")
        current, _ = render(state, kind="rules")
        scheduled, _ = render(state, kind="ordinary_activation")
        assert len(current.fields) == 1 and len(scheduled.fields) == 2


async def custom_open(runtime, monkeypatch, card):
    monkeypatch.setattr(runtime_module, "OPENING_CARDS", frozenset({card, "NR004", "NR006"}))
    route = await opened(runtime)
    state = await choice(runtime, route)
    index = next(
        i for i, o in enumerate(state.admission.hand.options) if o.incoming.card_id == card
    )
    await runtime.submit(
        route, "choose", actor=state.admission.picker_id, message_id=state.panel_id, index=index
    )
    state = await until(runtime, route, lambda s: s.panel_id == 1003)
    runtime.clock.at = state.admission.effective_at
    await runtime.submit(route, "tick")
    await settle(runtime)
    return route


async def choose_card(runtime, route, state, card):
    index = next(i for i, o in enumerate(state.offer.hand.options) if o.incoming.card_id == card)
    result = await runtime.submit(
        route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=index
    )
    assert result == "selected"
    await settle(runtime)
    state = await runtime.state(route.session_id)
    runtime.clock.at = state.offer.effective_at
    await runtime.submit(route, "tick")
    await settle(runtime)
    return await runtime.state(route.session_id)


@pytest.mark.asyncio
async def test_public_single_target_replacement_retires_only_its_brevity_occupant(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await custom_open(runtime, monkeypatch, "NR017")
        await runtime.submit(route, "configure", templates=frozenset({"NR019", "NR027", "NR007"}))
        await posts(runtime, route)
        state = await next_offer(runtime, route)
        option = next(o for o in state.offer.hand.options if o.incoming.card_id == "NR019")
        assert len(option.outgoing_ids) == 1
        state = await choose_card(runtime, route, state, "NR019")
        assert [i.card_id for i in state.context.active_instances] == ["NR019"]
        assert state.round_number == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("by_timeout", [True, False])
async def test_partial_replacement_retains_incoming_and_original_deadline(
    tmp_path, monkeypatch, by_timeout
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await custom_open(runtime, monkeypatch, "NR025")
        await runtime.submit(route, "configure", templates=frozenset({"NR027", "NR004", "NR006"}))
        await posts(runtime, route)
        state = await next_offer(runtime, route)
        await choose_card(runtime, route, state, "NR027")
        await runtime.submit(route, "configure", templates=frozenset({"NR022", "NR004", "NR006"}))
        await posts(runtime, route, base=40000)
        state = await next_offer(runtime, route)
        deadline = state.offer.choice_deadline
        message = state.panel_id
        index = next(
            i for i, o in enumerate(state.offer.hand.options) if o.incoming.card_id == "NR022"
        )
        picker = state.offer.hand.picker_id
        assert (
            await runtime.submit(route, "choose", actor=picker, message_id=message, index=index)
            == "needs_target"
        )
        await settle(runtime)
        state = await runtime.state(route.session_id)
        embed, view = render(state, kind="ordinary_offer")
        targets = [b for b in view.children if ":target:" in b.custom_id]
        assert len(targets) == 2 and len(embed.fields) == 1
        assert all(len(b.custom_id) <= 100 for b in view.children)
        assert (
            await runtime.submit(
                route, "target", actor=999, message_id=message, index=index, target_index=0
            )
            == "unauthorized"
        )
        assert (await runtime.state(route.session_id)).offer.choice_deadline == deadline
        if by_timeout:
            runtime.clock.at = deadline.expires_at
            runtime._timer(route, "timeout")
        else:
            runtime.clock.at += timedelta(seconds=10)
            assert (
                await runtime.submit(
                    route, "target", actor=picker, message_id=message, index=index, target_index=1
                )
                == "selected"
            )
        state = await until(runtime, route, lambda s: s.offer and s.offer.stage == "selected")
        assert state.offer.hand.options[index].incoming.instance_id == state.offer.selected_incoming
        assert state.offer.completed_by_timeout == by_timeout and state.offer.chosen_by_picker
        await settle(runtime)
        runtime.clock.at = state.offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        cards = {i.card_id for i in state.context.active_instances}
        assert "NR022" in cards and len(cards) == 2 and state.round_number == 3
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (2,)
        ]


@pytest.mark.asyncio
async def test_unknown_later_offer_send_fences_without_resend_or_closure(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.pacing.started_at + timedelta(seconds=270)
        await runtime.submit(route, "tick")
        state = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
        runtime.transport.fail = "ordinary_offer"
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        state = await until(runtime, route, lambda s: bool(s.recovery_id))
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert [m[1] for m in runtime.transport.messages].count("ordinary_offer") == 1
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT state FROM nr_discord_deliveries WHERE family='ordinary'",
        ) == [("sending",)]


@pytest.mark.asyncio
async def test_actual_silent_publisher_edits_known_panel_and_never_posts_a_replacement(
    tmp_path, monkeypatch
):
    from dataclasses import replace
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from lets_play.config import load_settings
    from lets_play.discord_adapter.discord_io import DiscordTransport
    from lets_play.discord_adapter.lane import LaneUnavailable

    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        state = await runtime.state(route.session_id)
        target = NS(edit=AsyncMock())
        channel = NS(guild=NS(id=101), get_partial_message=lambda _: target, send=AsyncMock())
        transport = DiscordTransport(
            NS(settings=load_settings(environ={}), get_channel=lambda _: channel)
        )
        assert await transport.publish(route, "evolution_closed_silent", state) == state.panel_id
        target.edit.assert_awaited_once()
        channel.send.assert_not_awaited()
        with pytest.raises(LaneUnavailable):
            await transport.publish(route, "evolution_closed_silent", replace(state, panel_id=None))
        channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_output_lookup_is_bounded_to_its_route_not_global_first_page(tmp_path):
    from random import Random

    from test_ordinary_flow import NOW

    from lets_play.new_rules.contracts import EventEnvelope, EventKind, SessionRoute
    from lets_play.storage.discord_store import DiscordStore
    from lets_play.storage.game_store import open_game_store
    from lets_play.storage.opening_flow import OpeningFlow, OpeningStatus

    async with open_game_store(tmp_path / "pages.sqlite3") as store:
        async with store.transaction() as unit:
            flow = OpeningFlow(unit)
            for n in range(105):
                route = SessionRoute(1000 + n, 2000 + n, f"session-{n:03}")
                frame = EventEnvelope(
                    route, EventKind.LOBBY_CREATE, f"request-{n}", 303, NOW, NOW, 1
                )
                assert (
                    await flow.admit(
                        frame,
                        capabilities=CAPS,
                        rng=Random(0),
                        available_templates=frozenset({"NR004", "NR006", "NR034"}),
                    )
                    is OpeningStatus.ADMITTED
                )
        async with store.transaction(write=False) as unit:
            pending = await DiscordStore(unit).pending("session-104")
            assert pending and pending.family == "opening"
            assert pending.output_id > 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,suffix,expected,index,target",
    [
        ("target", ":1:0", "target", 1, 0),
        ("revive", "", "revive", 0, -1),
    ],
)
async def test_sdk_controls_keep_replacement_indices_and_revival_action(
    tmp_path, action, suffix, expected, index, target
):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    import discord

    from lets_play.bot import LetsPlayBot
    from lets_play.config import load_settings
    from lets_play.new_rules.contracts import SessionRoute

    bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
    async with bot:
        route = SessionRoute(101, 202, "a" * 32)
        calls = []

        def submit(*args, **kwargs):
            calls.append((args, kwargs))
            future = asyncio.get_running_loop().create_future()
            future.set_result("selected")
            return future

        bot.game_runtime = NS(ready=True, routes={(101, 202): route}, submit=submit)
        request = NS(
            guild_id=101,
            channel_id=202,
            guild=NS(me=object()),
            user=NS(id=303, bot=False),
            channel=NS(
                send=AsyncMock(),
                get_partial_message=lambda _: None,
                permissions_for=lambda _: discord.Permissions.all(),
            ),
            type=discord.InteractionType.component,
            message=NS(id=55),
            id=66,
            data={"custom_id": f"nr:{route.session_id}:{action}{suffix}"},
        )
        future, _ = bot.game_adapter.capture_control(request)
        await future
        assert calls[0][0] == (route, expected)
        assert calls[0][1]["index"] == index and calls[0][1]["target_index"] == target
        bot.game_runtime = None


@pytest.mark.asyncio
async def test_marker_failure_pauses_and_later_paused_chat_does_not_retry_it(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    transport = Transport()
    transport.mark = AsyncMock(side_effect=TimeoutError("private failure payload"))
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 98701, "wrong", 606))
        await until(runtime, route, lambda s: bool(s.recovery_id))
        await settle(runtime)
        await runtime.submit(route, "event", event=event(runtime, route, 98702, "wrong", 606))
        await settle(runtime)
        assert transport.mark.await_count == 1
        async with runtime.store.transaction(write=False) as unit:
            score = await unit.get_score(route.session_id, 606)
            assert score.ordinary_posts == 1 and score.ordinary_faults == 1
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_marker_intents") == [(1,)]


@pytest.mark.asyncio
async def test_later_round_work_does_not_change_another_guilds_lobby(tmp_path, monkeypatch):
    from lets_play.storage.opening_flow import OpeningStatus

    async with service(tmp_path, monkeypatch) as runtime:
        first = await opened(runtime)
        await activated(runtime, first)
        second, result = await runtime.start(102, 203, 707, "second-guild", CAPS)
        assert result is OpeningStatus.ADMITTED
        lobby = await until(runtime, second, lambda s: s.admission.stage == "lobby_published")
        await posts(runtime, first)
        state = await next_offer(runtime, first)
        assert state.offer.hand.picker_id in {303, 404, 505}
        other = await runtime.state(second.session_id)
        assert other.panel_id == lobby.panel_id and other.context is None
        assert other.admission.joined == 1 and other.admission.stage == "lobby_published"
        assert runtime.routes[(102, 203)] == second
        async with runtime.store.transaction(write=False) as unit:
            from lets_play.storage.discord_store import DiscordStore

            assert (
                await DiscordStore(unit).control_message(first.session_id, "choose")
                != other.panel_id
            )
