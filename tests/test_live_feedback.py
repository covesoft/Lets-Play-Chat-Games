"""Owner-reported live regressions: reply routing, result layout and NR011."""

from fractions import Fraction
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_runtime import activated, choice, drain, event, opened, service, until

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import runtime as runtime_module
from lets_play.discord_adapter.discord_io import DiscordTransport, render
from lets_play.new_rules.contracts import Outcome, RuleInstance
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.text_rules import evaluate_text_rule
from lets_play.storage.opening_flow import OpeningStatus


def interaction(*, manager=True, message_id=None):
    permissions = discord.Permissions.none()
    permissions.manage_guild = manager
    channel = NS(
        send=AsyncMock(),
        get_partial_message=lambda _: None,
        permissions_for=lambda _: discord.Permissions.all(),
    )
    return NS(
        guild_id=101,
        channel_id=202,
        guild=NS(me=object()),
        channel=channel,
        user=NS(id=303, bot=False),
        permissions=permissions,
        id=77,
        extras={},
        message=NS(id=message_id),
        response=NS(defer=AsyncMock()),
        followup=NS(send=AsyncMock()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "manager,ready,key",
    [
        (True, True, "end.no_game_here"),
        (False, True, "end.manager_required"),
        (True, False, "control.unavailable"),
    ],
)
async def test_end_without_active_game_uses_correct_reason(tmp_path, manager, ready, key):
    bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
    async with bot:
        bot.game_runtime = NS(ready=ready, routes={})
        request = interaction(manager=manager)
        await bot.game_adapter.end(request)
        request.followup.send.assert_awaited_once_with(bot.presentation.text(key), ephemeral=True)
        bot.game_runtime = None


@pytest.mark.asyncio
async def test_duplicate_join_has_friendly_reply_but_old_lobby_control_stays_stale(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await runtime.state(route.session_id)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(message_id=state.panel_id)
            request.data = {"custom_id": f"nr:{route.session_id}:join"}
            await bot.game_adapter.component(request)
            request.followup.send.assert_awaited_once_with(
                bot.presentation.text("control.already_joined"), ephemeral=True
            )
            assert (await runtime.state(route.session_id)).admission.joined == 1
            await choice(runtime, route)
            request.followup.send.reset_mock()
            await bot.game_adapter.component(request)
            request.followup.send.assert_awaited_once_with(
                bot.presentation.text("control.already_joined"), ephemeral=True
            )
            request.user.id = 999  # A nonmember cannot use the old invitation to join late.
            request.followup.send.reset_mock()
            await bot.game_adapter.component(request)
            request.followup.send.assert_awaited_once_with(
                bot.presentation.text("control.stale"), ephemeral=True
            )
            assert (await runtime.state(route.session_id)).admission.joined == 3
            bot.game_runtime = None


@pytest.mark.asyncio
@pytest.mark.parametrize("opening_choice", [False, True])
async def test_preplay_close_has_no_score_footer(tmp_path, monkeypatch, opening_choice):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        if opening_choice:
            await choice(runtime, route)
        await runtime.submit(route, "end", actor=303, authorized=True)
        state = await runtime.state(route.session_id)
        assert state.context is None
        embed, _ = render(state)
        assert "footer" not in embed.to_dict()
        assert not embed.fields


@pytest.mark.asyncio
async def test_quorum_expiry_has_no_score_footer(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.admission.lobby_deadline
        await runtime.submit(route, "tick")
        state = await runtime.state(route.session_id)
        assert state.admission.stage == "ended" and state.context is None
        assert "footer" not in render(state)[0].to_dict()


@pytest.mark.asyncio
async def test_score_mentions_are_field_values_and_all_pings_stay_disabled(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 6001))
        await runtime.submit(route, "end", actor=303, authorized=True)
        await drain(runtime)
        state = await runtime.state(route.session_id)
        embed, _ = render(state)
        for (actor, _), field in zip(state.scores, embed.fields, strict=True):
            assert "<@" not in field.name
            assert f"<@{actor}>" in field.value
        assert embed.footer.text
        channel = NS(guild=NS(id=101), send=AsyncMock(return_value=NS(id=9001)))
        transport = DiscordTransport(
            NS(settings=load_settings(environ={}), get_channel=lambda _: channel)
        )
        await transport.publish(route, "closed", state)
        mentions = channel.send.call_args.kwargs["allowed_mentions"]
        assert mentions.users is False and mentions.roles is False and mentions.everyone is False


@pytest.mark.parametrize(
    "text,passes",
    [
        ("what is wrong, bot...", True),
        ("WHAT IS WRONG BOT....", True),
        ("What. ...", True),
        ("over.......", True),
        ("**over....**", True),
        ("“over.....”", True),
        ("over....   ", True),
        ("over..", False),
        ("over…", False),
        ("over. . .", False),
        ("over...!", False),
    ],
)
def test_are_you_sure_accepts_three_or_more_consecutive_ascii_periods(text, passes):
    result = evaluate_text_rule(parse_message(text), RuleInstance("periods", "NR011"))
    assert result.outcome is (Outcome.PASS if passes else Outcome.FAIL)


@pytest.mark.asyncio
async def test_are_you_sure_live_lane_scores_examples_once_and_keeps_existing_effects(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        monkeypatch.setattr(runtime_module, "OPENING_CARDS", frozenset({"NR004", "NR006", "NR011"}))
        route = await opened(runtime)
        state = await choice(runtime, route)
        index = next(
            i
            for i, option in enumerate(state.admission.hand.options)
            if option.incoming.card_id == "NR011"
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
        state = await until(runtime, route, lambda s: s.panel_id == 1003)
        runtime.clock.at = state.admission.effective_at
        await runtime.submit(route, "tick")
        for source, text in enumerate(
            ("what is wrong, bot...", "WHAT IS WRONG BOT....", "What. ...", "fails.."), start=7001
        ):
            await runtime.submit(route, "event", event=event(runtime, route, source, text))
        # Changing a previously accepted source payload is not a rescore path.
        await runtime.submit(route, "event", event=event(runtime, route, 7004, "fixed...."))
        await drain(runtime)
        async with runtime.store.transaction(write=False) as unit:
            score = await unit.get_score(route.session_id, 606)
        assert score.ordinary_observations == 4 and score.ordinary_quality == Fraction(3)
        assert score.ordinary_faults == 1
        assert runtime.transport.markers == [7004]
