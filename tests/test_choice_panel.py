"""The real SDK transport edits the warning into the one authoritative choice."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_ordinary_loop import posts, settle
from test_discord_runtime import Transport, activated, opened, service, until
from test_live_feedback import interaction
from test_ordinary_flow import rows

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.discord_adapter.lane import LaneUnavailable


class PanelTransport(Transport):
    def __init__(self):
        super().__init__()
        self.edits = {}
        self.choice_entered = asyncio.Event()
        self.choice_release = None
        self.choice_failure = False
        self.channel = NS(guild=NS(id=101), send=AsyncMock(return_value=NS(id=9999)))
        self.channel.get_partial_message = self.partial
        self.sdk = DiscordTransport(
            NS(get_channel=lambda _: self.channel, settings=NS(ding_emoji=None))
        )

    def partial(self, message_id):
        if message_id not in self.edits:

            async def edit(**payload):
                view = payload.get("view")
                if view and any(
                    ":pick:" in b.custom_id or ":target:" in b.custom_id for b in view.children
                ):
                    self.choice_entered.set()
                    if self.choice_release:
                        await self.choice_release.wait()
                    if self.choice_failure:
                        raise TimeoutError("private edit failure")

            self.edits[message_id] = NS(edit=AsyncMock(side_effect=edit))
        return self.edits[message_id]

    async def publish(self, route, kind, state):
        if kind == "ordinary_offer":
            self.messages.append((route, kind, state))
            return await self.sdk.publish(route, kind, state)
        return await super().publish(route, kind, state)

    async def refresh(self, route, state):
        await self.sdk.refresh(route, state)


async def warning(runtime, route):
    await posts(runtime, route)
    state = await runtime.state(route.session_id)
    runtime.clock.at = state.pacing.started_at + timedelta(seconds=270)
    await runtime.submit(route, "tick")
    await settle(runtime)
    state = await runtime.state(route.session_id)
    assert state.pacing.stage == "warning_published"
    return state


@pytest.mark.asyncio
async def test_warning_edit_ack_binds_the_original_message_and_its_buttons_work(
    tmp_path, monkeypatch
):
    transport = PanelTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await warning(runtime, route)
        original = state.panel_id
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.offer.stage == "published" and state.panel_id == original
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT message_id FROM nr_discord_deliveries WHERE family='ordinary'",
        ) == [(original,)]
        transport.channel.send.assert_not_awaited()
        view = transport.edits[original].edit.call_args.kwargs["view"]
        assert len(view.children) == 3
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=original)
            click.user.id = state.offer.hand.picker_id
            click.data = {"custom_id": view.children[0].custom_id}
            await bot.game_adapter.component(click)
            assert click.followup.send.call_args.args == (
                bot.presentation.text("control.selected"),
            )
            bot.game_runtime = None
        await settle(runtime)
        assert (await runtime.state(route.session_id)).offer.stage == "selected"


@pytest.mark.asyncio
async def test_refresh_cannot_reveal_unpublished_choices_or_start_their_clock(
    tmp_path, monkeypatch
):
    transport = PanelTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await warning(runtime, route)
        original = state.panel_id
        transport.choice_release = asyncio.Event()
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await asyncio.wait_for(transport.choice_entered.wait(), 5)
        state = await runtime.state(route.session_id)
        assert state.offer.stage == "prepared" and not state.offer_display_ready
        assert state.offer.choice_deadline is None
        count = transport.edits[original].edit.await_count
        await transport.sdk.refresh(route, state)
        assert transport.edits[original].edit.await_count == count
        runtime.clock.at += timedelta(seconds=7)
        acknowledged_at = runtime.clock.now()
        transport.choice_release.set()
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.panel_id == original and state.offer_display_ready
        assert state.offer.published_at == acknowledged_at
        assert state.offer.choice_deadline.expires_at == acknowledged_at + timedelta(seconds=60)
        transport.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_known_choice_panel_never_falls_back_to_a_new_message(tmp_path, monkeypatch):
    transport = PanelTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await warning(runtime, route)
        with pytest.raises(LaneUnavailable):
            await transport.sdk.publish(route, "ordinary_offer", replace(state, panel_id=None))
        transport.channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_choice_edit_pauses_and_keeps_one_attempt_without_fallback(
    tmp_path, monkeypatch, caplog
):
    transport = PanelTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await warning(runtime, route)
        original = state.panel_id
        transport.choice_failure = True
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await until(runtime, route, lambda s: bool(s.recovery_id))
        await settle(runtime)
        transport.channel.send.assert_not_awaited()
        assert sum(1 for _, kind, _ in transport.messages if kind == "ordinary_offer") == 1
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT state FROM nr_discord_deliveries WHERE family='ordinary'",
        ) == [("sending",)]
        await runtime.initialize()
        await settle(runtime)
        assert sum(1 for _, kind, _ in transport.messages if kind == "ordinary_offer") == 1
        assert (await runtime.state(route.session_id)).panel_id == original
        assert "private edit failure" not in caplog.text


@pytest.mark.asyncio
async def test_refresh_waits_for_actual_activation_publication_too(tmp_path, monkeypatch):
    transport = PanelTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await warning(runtime, route)
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        original = state.panel_id
        transport.block = asyncio.Event()
        await runtime.submit(
            route, "choose", actor=state.offer.hand.picker_id, message_id=original, index=0
        )
        state = await runtime.state(route.session_id)
        assert state.offer.stage == "selected" and not state.offer_display_ready
        count = transport.edits[original].edit.await_count
        await transport.sdk.refresh(route, state)
        assert transport.edits[original].edit.await_count == count
        transport.block.set()
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.offer_display_ready and state.panel_id != original
