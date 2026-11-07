"""Real SDK construction and payload translation; no live Discord connection."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_runtime import activated, opened, service
from test_ordinary_flow import NOW

from lets_play.bot import LetsPlayBot
from lets_play.config import CHATGAMES_DING_EMOJI, load_settings
from lets_play.discord_adapter.discord_io import (
    DiscordTransport,
    capabilities,
    message_snapshot,
    reaction_snapshot,
    render,
)
from lets_play.new_rules.contracts import ReferenceKind, SessionRoute
from lets_play.storage.opening_flow import OpeningStatus


@pytest.mark.parametrize(
    "missing",
    ["view_channel", "send_messages", "embed_links", "read_message_history", "add_reactions"],
)
def test_capability_preflight_requires_each_essential_operation(missing):
    permissions = discord.Permissions.all()
    setattr(permissions, missing, False)
    channel = NS(
        send=AsyncMock(), get_partial_message=lambda _: None, permissions_for=lambda _: permissions
    )
    assert not capabilities(channel, object(), marker_verified=True).available


def test_forum_container_and_unverified_marker_are_not_playable():
    assert not capabilities(NS(), object(), marker_verified=True).available
    channel = NS(
        send=AsyncMock(),
        get_partial_message=lambda _: None,
        permissions_for=lambda _: discord.Permissions.all(),
    )
    assert not capabilities(channel, object()).available
    assert capabilities(channel, object(), marker_verified=True).available
    channel.archived = True
    assert not capabilities(channel, object(), marker_verified=True).available


@pytest.mark.asyncio
async def test_marker_probe_uses_exact_animated_asset_and_real_reaction_ack():
    settings = load_settings(environ={})
    assert settings.ding_emoji == CHATGAMES_DING_EMOJI == "<a:NUH_UH_neon:1568499898071126026>"
    user = NS(id=55)
    transport = DiscordTransport(NS(settings=settings, user=user))
    assert transport.marker.id == 1568499898071126026 and transport.marker.animated
    probe = NS(add_reaction=AsyncMock(), remove_reaction=AsyncMock(), delete=AsyncMock())
    channel = NS(
        send=AsyncMock(return_value=probe),
        get_partial_message=lambda _: None,
        guild=NS(me=object()),
        permissions_for=lambda _: discord.Permissions.all(),
    )
    assert (await transport.probe(channel)).available
    probe.add_reaction.assert_awaited_once_with(transport.marker)
    probe.remove_reaction.assert_awaited_once_with(transport.marker, user)
    probe.delete.assert_awaited_once()
    probe.add_reaction.side_effect = RuntimeError("failed reaction")
    with pytest.raises(RuntimeError):
        await transport.probe(channel)
    assert probe.delete.await_count == 2


def test_message_snapshot_retains_transient_metadata_without_history_fetch():
    reference = NS(message_id=9, type=discord.MessageReferenceType.default)
    message = NS(
        id=8,
        author=NS(id=7, bot=False),
        content="**over...**",
        created_at=NOW,
        attachments=[NS(id=12, filename="memo.ogg", content_type="audio/ogg", duration=4.0)],
        stickers=[NS(id=13)],
        reference=reference,
        type=discord.MessageType.reply,
        flags=NS(voice=True),
        webhook_id=None,
        is_system=lambda: False,
    )
    snapshot = message_snapshot(message, SessionRoute(1, 2, "s"), NOW)
    assert snapshot.reference_kind is ReferenceKind.REPLY and snapshot.referenced_message_id == 9
    assert snapshot.is_voice_message and snapshot.attachments[0].duration_seconds == 4.0
    assert snapshot.sticker_ids == (13,) and snapshot.content == "**over...**"
    assert "over" not in repr(snapshot) and "memo.ogg" not in repr(snapshot)
    message.reference.type = discord.MessageReferenceType.forward
    assert (
        message_snapshot(message, snapshot.envelope.route, NOW).reference_kind
        is ReferenceKind.FORWARD
    )


def test_raw_reaction_snapshot_uses_action_receipt_not_target_creation_time():
    payload = NS(user_id=7, member=NS(bot=False), message_id=1, emoji=NS(id=44, name="renamed"))
    route = SessionRoute(1, 2, "s")
    first, second = (
        reaction_snapshot(payload, route, NOW, source_id="gw:one:1"),
        reaction_snapshot(payload, route, NOW, source_id="gw:one:2"),
    )
    assert first.envelope.source_id != second.envelope.source_id
    assert first.envelope.created_at == NOW
    assert first.emoji.key == ("custom", 44)
    assert first.offense_key == second.offense_key


@pytest.mark.asyncio
async def test_manager_authorization_and_exact_channel_component_checks(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        bot = LetsPlayBot(
            load_settings(base_dir=tmp_path, environ={"CHATGAMES_DEVELOPMENT_MODE": "true"})
        )
        async with bot:
            bot.game_runtime = runtime
            interaction = NS(
                guild_id=101,
                channel_id=202,
                guild=NS(me=object()),
                user=NS(id=999, bot=False),
                permissions=discord.Permissions.none(),
                id=77,
                response=NS(defer=AsyncMock()),
                followup=NS(send=AsyncMock()),
            )
            await bot.game_adapter.end(interaction)
            assert (await runtime.state(route.session_id)).admission.stage == "lobby_published"
            interaction.data = {"custom_id": f"nr:{route.session_id}:join"}
            interaction.channel_id = 999
            await bot.game_adapter.component(interaction)
            assert (await runtime.state(route.session_id)).admission.joined == 1
            assert "different or expired" in interaction.followup.send.call_args.args[0]
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_render_scheduled_rule_is_explicit_and_ended_results_are_bounded(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await runtime.state(route.session_id)
        embed, _ = render(state)
        assert "current rules" in embed.title and "Roger Wilco" in str(embed.to_dict())
        embed, _ = render(replace(state, recovery_id="pause"))
        assert "paused" in embed.title
        assert "No rule" not in embed.description


@pytest.mark.asyncio
async def test_deleted_marker_target_is_local_but_unknown_emoji_is_not():
    response = NS(status=404, reason="Not Found")
    target = NS(
        add_reaction=AsyncMock(
            side_effect=discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})
        )
    )
    channel = NS(guild=NS(id=1), get_partial_message=lambda _: target)
    transport = DiscordTransport(
        NS(settings=load_settings(environ={}), get_channel=lambda _: channel)
    )
    await transport.mark(SessionRoute(1, 2, "s"), 12)
    target.add_reaction.side_effect = discord.NotFound(
        response, {"code": 10014, "message": "Unknown Emoji"}
    )
    with pytest.raises(discord.NotFound):
        await transport.mark(SessionRoute(1, 2, "s"), 12)


@pytest.mark.asyncio
async def test_gateway_dispatch_snapshots_reaction_sequence_before_async_callbacks(tmp_path):
    """Two repeated packets retain one stable source, despite async SDK handlers."""
    bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
    async with bot:
        submissions = []
        route = SessionRoute(101, 202, "s")

        def submit(route, action, **data):
            submissions.append((route, action, data))
            future = asyncio.get_running_loop().create_future()
            future.set_result(None)
            return future

        bot.game_runtime = NS(
            ready=True,
            routes={(101, 202): route},
            clock=NS(now=lambda: NOW),
            submit=submit,
            _consume=lambda f: f.exception(),
        )
        bot.ws = NS(session_id="gateway-session", sequence=77)
        payload = NS(
            guild_id=101,
            channel_id=202,
            user_id=606,
            member=NS(bot=False),
            message_id=13,
            emoji=NS(id=None, name="⭐"),
        )
        bot.dispatch("raw_reaction_add", payload)
        bot.dispatch("raw_reaction_add", payload)
        bot.ws.sequence = 78
        bot.dispatch("raw_reaction_add", payload)
        keys = [s[2]["event"].envelope.source_id for s in submissions]
        assert keys[0] == keys[1] and keys[2] != keys[1]
        assert "gateway-session" not in keys[0]
        bot.game_runtime = None
        bot.ws = None


@pytest.mark.asyncio
async def test_control_receipt_is_captured_before_http_defer(tmp_path):
    bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
    async with bot:
        route = SessionRoute(101, 202, "a" * 32)
        submitted = []
        future = asyncio.get_running_loop().create_future()
        future.set_result(OpeningStatus.JOINED)

        def submit(*args, **kwargs):
            submitted.append((args, kwargs))
            return future

        bot.game_runtime = NS(ready=True, routes={(101, 202): route}, submit=submit)
        channel = NS(
            send=AsyncMock(),
            get_partial_message=lambda _: None,
            permissions_for=lambda _: discord.Permissions.all(),
        )
        interaction = NS(
            guild_id=101,
            channel_id=202,
            guild=NS(me=object()),
            channel=channel,
            user=NS(id=404, bot=False),
            message=NS(id=1001),
            id=77,
            extras={},
            type=discord.InteractionType.component,
            data={"custom_id": f"nr:{route.session_id}:join"},
            response=NS(defer=AsyncMock()),
            followup=NS(send=AsyncMock()),
        )
        captured = bot.game_adapter.capture_control(interaction)
        interaction.extras["new_rules_receipt"] = captured
        assert len(submitted) == 1
        interaction.response.defer.assert_not_awaited()
        await bot.game_adapter.component(interaction)
        assert len(submitted) == 1
        interaction.response.defer.assert_awaited_once()
        bot.game_runtime = None
