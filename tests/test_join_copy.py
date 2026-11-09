"""The same joined result has distinct lobby and revival participation copy."""

from datetime import timedelta

import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, opened, service
from test_live_feedback import interaction

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings


async def click(bot, route, message, action, actor):
    request = interaction(message_id=message)
    request.user.id = actor
    request.data = {"custom_id": f"nr:{route.session_id}:{action}"}
    await bot.game_adapter.component(request)
    request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    assert request.followup.send.call_args.kwargs["ephemeral"] is True
    return request.followup.send.call_args.args[0]


@pytest.mark.asyncio
async def test_lobby_joins_keep_initial_confirmation_and_opening_quorum_copy(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await runtime.state(route.session_id)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            assert await click(bot, route, state.panel_id, "join", 404) == bot.presentation.text(
                "control.joined"
            )
            assert await click(bot, route, state.panel_id, "join", 404) == bot.presentation.text(
                "control.already_joined"
            )
            assert await click(bot, route, state.panel_id, "join", 505) == bot.presentation.text(
                "control.opening"
            )
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_continue_uses_owner_copy_and_preserves_repeat_and_quorum_confirmations(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await runtime.state(route.session_id)
        round_id = state.context.position.round_id
        runtime.clock.at = state.pacing.started_at + timedelta(seconds=870)
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.revival.joined == 0
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            message = state.panel_id
            assert await click(bot, route, message, "revive", 303) == bot.presentation.text(
                "control.continued"
            )
            assert await click(bot, route, message, "revive", 303) == bot.presentation.text(
                "control.already_joined"
            )
            assert (await runtime.state(route.session_id)).revival.joined == 1
            assert await click(bot, route, message, "revive", 404) == bot.presentation.text(
                "control.continued"
            )
            assert await click(bot, route, message, "revive", 505) == bot.presentation.text(
                "control.revived"
            )
            await settle(runtime)
            assert (await runtime.state(route.session_id)).context.position.round_id == round_id
            bot.game_runtime = None
