"""Automatic recovery of published setup, held play, and revival."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import CAPS, activated, choice, opened, service, until
from test_ordinary_flow import rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.config import load_settings
from lets_play.discord_adapter import runtime as runtime_module
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.opening_flow import OpeningStatus


async def revival(runtime, route):
    state = await activated(runtime, route)
    runtime.clock.at = state.pacing.started_at + timedelta(seconds=870)
    await runtime.submit(route, "tick")
    state = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
    runtime.clock.at = state.pacing.selection_due_at
    await runtime.submit(route, "tick")
    return await until(runtime, route, lambda s: s.revival and s.revival.stage == "published")


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["lobby", "opening", "revival"])
async def test_saved_controls_and_deadlines_after_reboot(tmp_path, monkeypatch, phase):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        if phase == "opening":
            await choice(runtime, route)
        if phase == "revival":
            state = await revival(runtime, route)
            await runtime.submit(route, "revive", actor=303, message_id=state.panel_id)
        await settle(runtime)
        original = await runtime.state(route.session_id)
        await runtime.initialize()
        saved = await reading(runtime, route)
        assert saved.recovery_control_id == original.panel_id
        assert saved.recovery_resume_at < original.next_deadline
        await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.recovery_id is None and state.panel_id == original.panel_id
        assert state.admission.stage == original.admission.stage
        assert state.next_deadline == original.next_deadline
        assert set(transport.probes) == {original.panel_id, saved.recovery_notice_id}
        action = {"lobby": "join", "opening": "choose", "revival": "revive"}[phase]
        actor = state.admission.picker_id if phase == "opening" else 404
        data = dict(actor=actor, index=0, capabilities=CAPS)
        assert await runtime.submit(route, action, message_id=saved.panel_id, **data) == "stale"
        expected = {
            "lobby": OpeningStatus.JOINED,
            "opening": OpeningStatus.SELECTED,
            "revival": "joined",
        }
        assert (
            await runtime.submit(route, action, message_id=original.panel_id, **data)
            == expected[phase]
        )
        if phase == "revival":
            after = await runtime.state(route.session_id)
            assert after.revival.joined == 2
            assert after.context.position.round_id == original.context.position.round_id


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["lobby", "revival"])
@pytest.mark.parametrize("remaining", [0, 10, 30])
async def test_terminal_deadline_wins_without_player_traffic(
    tmp_path, monkeypatch, phase, remaining
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        if phase == "revival":
            await revival(runtime, route)
        await settle(runtime)
        original = await runtime.state(route.session_id)
        runtime.clock.at = original.next_deadline - timedelta(seconds=remaining)
        await runtime.initialize()
        if remaining:
            await reading(runtime, route)
            timer = runtime.timers[route.session_id]
            callback = timer._callback
            args = timer._args
            assert args == (route, "tick")
            timer.cancel()
            runtime.clock.at = original.next_deadline
            callback(*args)
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.admission.stage == "ended"
        assert not state.recovery_id
        assert (101, 202) not in runtime.routes
        assert len(transport.recoveries) == bool(remaining)
        assert transport.messages[-1][1] == (
            "evolution_closed_silent" if phase == "revival" else "closed"
        )
        if phase == "revival":
            assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
                (1,)
            ]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["opening", "selected"])
async def test_opening_timeout_or_activation_uses_saved_clock(tmp_path, monkeypatch, phase):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await choice(runtime, route)
        if phase == "selected":
            await runtime.submit(
                route, "choose", actor=state.admission.picker_id, message_id=state.panel_id, index=0
            )
        await settle(runtime)
        original = await runtime.state(route.session_id)
        if phase == "opening":
            runtime.clock.at = original.next_deadline - timedelta(seconds=10)
        await runtime.initialize()
        saved = await reading(runtime, route)
        assert saved.context is None
        assert saved.admission.next_deadline == original.next_deadline
        await fire(runtime, route, saved.recovery_resume_at)
        if phase == "opening":
            actual = await until(runtime, route, lambda s: s.admission.stage == "selected")
            assert actual.admission.choice_deadline == original.admission.choice_deadline
        else:
            state = await runtime.state(route.session_id)
            assert state.admission.stage == "active"
            assert state.pacing.started_at == original.admission.effective_at
            assert len(runtime.transport.messages) == 3
            assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_rounds") == [(1,)]


@pytest.mark.asyncio
async def test_announced_unattached_opener_survives_pool_change_on_reboot(tmp_path, monkeypatch):
    updated_pool = runtime_module.OPENING_CARDS
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await choice(runtime, route)
        index = next(
            i
            for i, option in enumerate(state.admission.hand.options)
            if option.incoming.card_id == "NR004"
        )
        assert (
            await runtime.submit(
                route,
                "choose",
                actor=state.admission.picker_id,
                message_id=state.panel_id,
                index=index,
            )
            is OpeningStatus.SELECTED
        )
        await settle(runtime)
        original = await runtime.state(route.session_id)

        monkeypatch.setattr(runtime_module, "OPENING_CARDS", updated_pool)
        await runtime.initialize()
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.admission.stage == "active"
        assert state.pacing.started_at == original.admission.effective_at
        assert tuple(rule.card_id for rule in state.context.active_instances) == ("NR004",)


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining", [15, 60])
async def test_held_idle_keeps_original_clock(tmp_path, monkeypatch, remaining):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        await runtime.submit(route, "configure", templates=frozenset({"NR034"}))
        await settle(runtime)
        original = await runtime.state(route.session_id)
        runtime.clock.at = original.next_deadline - timedelta(seconds=remaining)
        await runtime.initialize()
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        actual = await runtime.state(route.session_id)
        assert actual.context.position.round_id == original.context.position.round_id
        assert actual.scores == original.scores
        if remaining > 30:
            assert actual.context.position.advancement == "held"
            assert actual.next_deadline == original.next_deadline
        else:
            assert actual.context.position.pause_reason == "revival"
            assert actual.revival.stage == "published"


@pytest.mark.asyncio
async def test_sdk_notice_links_lobby_without_copying_controls(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        original = await runtime.state(route.session_id)
        await runtime.initialize()
        saved = await reading(runtime, route)
        messages = {}

        def message(mid):
            return messages.setdefault(mid, NS(edit=AsyncMock()))

        channel = NS(
            guild=NS(id=101), send=AsyncMock(return_value=NS(id=30001)), get_partial_message=message
        )
        sdk = DiscordTransport(
            NS(get_channel=lambda _: channel, settings=load_settings(environ={}))
        )
        await sdk.publish_recovery(route, saved)
        notice = channel.send.call_args.kwargs
        assert "game setup continues" in notice["embed"].description
        assert f"/{original.panel_id}" in notice["embed"].description
        assert [item.custom_id for item in notice["view"].children] == [
            f"nrq:{route.session_id}:recovery:{saved.recovery_id}"
        ]
        await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        await sdk.refresh(route, state)
        assert messages[original.panel_id].edit.call_args.kwargs["view"].children
        assert not messages[saved.panel_id].edit.call_args.kwargs["view"].children


@pytest.mark.asyncio
async def test_lobby_expiry_survives_failed_recovery_access_probe(tmp_path, monkeypatch):
    from lets_play.discord_adapter.lane import LaneUnavailable

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await settle(runtime)
        original = await runtime.state(route.session_id)

        async def fail_probe(route, panel):
            raise LaneUnavailable("missing saved lobby panel")

        transport.recovery_capabilities = fail_probe
        await runtime.initialize()
        await settle(runtime)
        assert not transport.recoveries
        assert (await runtime.state(route.session_id)).recovery_id
        timer = runtime.timers[route.session_id]
        callback, args = timer._callback, timer._args
        timer.cancel()
        runtime.clock.at = original.admission.lobby_deadline
        callback(*args)
        await settle(runtime)
        assert (await runtime.state(route.session_id)).admission.stage == "ended"


@pytest.mark.asyncio
async def test_held_reopen_does_not_replace_saved_config_with_startup_defaults(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        await runtime.submit(route, "configure", templates=frozenset({"NR034"}))
        await settle(runtime)
        original = await runtime.state(route.session_id)
    async with service(tmp_path, monkeypatch) as runtime:
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.context.position.advancement == "held"
        assert resumed.next_deadline == original.next_deadline
        assert resumed.context.position.round_id == original.context.position.round_id
        assert resumed.scores == original.scores


@pytest.mark.asyncio
async def test_expiry_publishes_silent_closure_when_concurrent_probe_fails(tmp_path, monkeypatch):
    import asyncio

    from lets_play.discord_adapter.lane import LaneUnavailable

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        original = await revival(runtime, route)
        await settle(runtime)
        probing, release = asyncio.Event(), asyncio.Event()

        async def fail_probe(route, panel):
            probing.set()
            await release.wait()
            raise LaneUnavailable("probe failed after expiry")

        transport.recovery_capabilities = fail_probe
        await runtime.initialize()
        await asyncio.wait_for(probing.wait(), timeout=5)
        timer = runtime.timers[route.session_id]
        callback, args = timer._callback, timer._args
        timer.cancel()
        runtime.clock.at = original.revival.deadline
        callback(*args)
        await until(runtime, route, lambda s: s.admission.stage == "ended")
        release.set()
        await settle(runtime)
        assert transport.messages[-1][1] == "evolution_closed_silent"
        assert not transport.recoveries
