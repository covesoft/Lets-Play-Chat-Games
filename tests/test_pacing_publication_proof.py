"""Publication proof restores the exact pacing attempt and its clock."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, opened, service, until
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.discord_store import PendingOutput


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["warning", "revival"])
@pytest.mark.parametrize("outcome", ["missing", "found", "expired"])
async def test_interrupted_pacing_publication(tmp_path, monkeypatch, kind, outcome):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        active = await activated(runtime, route)
        await settle(runtime)
        transport.fail = f"pacing_{kind}"
        runtime.clock.at = active.pacing.started_at + timedelta(seconds=870)
        await runtime.submit(route, "tick")
        if kind == "revival":
            warning = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
            runtime.clock.at = warning.pacing.selection_due_at
            await runtime.submit(route, "tick")
        await until(runtime, route, lambda s: s.recovery_id is not None)
        await settle(runtime)
        runtime.clock.at += timedelta(seconds=301 if outcome == "expired" else 2)
        attempts = []
        published = None

        async def find(exact_route, exact_kind, output_id, attempted):
            nonlocal published
            attempts.append((exact_route, exact_kind, output_id))
            published = attempted + timedelta(seconds=1)
            return (9001, published) if outcome != "missing" else None

        transport.find_pacing_publication = find
        await runtime.initialize()
        await settle(runtime)
        assert len(attempts) == 1 and attempts[0][:2] == (route, kind)
        assert sum(k == f"pacing_{kind}" for _, k, _ in transport.messages) == 1
        if outcome == "missing":
            assert (await runtime.state(route.session_id)).recovery_id
            assert not transport.recoveries
            return
        if outcome == "expired" and kind == "revival":
            assert (await runtime.state(route.session_id)).admission.stage == "ended"
            assert not transport.recoveries
            assert transport.messages[-1][1] == "evolution_closed_silent"
            return
        status = await reading(runtime, route)
        if kind == "warning":
            assert status.pacing.warning_published_at == published
            assert status.pacing.selection_due_at == published + timedelta(seconds=30)
        else:
            assert status.revival.published_at == published
            assert status.revival.deadline == published + timedelta(seconds=300)
            assert status.phase_control_id == 9001
        await fire(runtime, route, status.recovery_resume_at)
        after = await runtime.state(route.session_id)
        assert after.recovery_id is None
        assert after.context.position.round_id == active.context.position.round_id
        if kind == "revival":
            assert after.panel_id == 9001
            assert await runtime.submit(route, "revive", actor=404, message_id=9001) == "joined"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["warning", "revival"])
async def test_sdk_pacing_reference_round_trips_exact_output(tmp_path, monkeypatch, kind):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await activated(runtime, route)
        attempted = state.pacing.started_at
        output = PendingOutput("pacing", 41, kind, "token")
        send = AsyncMock(return_value=NS(id=9001))

        async def history(**kwargs):
            assert kwargs == {"limit": 50, "after": attempted, "oldest_first": True}
            original = send.call_args.kwargs["embed"]
            unrelated = discord.Embed().set_footer(text=original.footer.text + "0")
            for mid, embed in ((9000, unrelated), (9001, original)):
                yield NS(
                    id=mid,
                    author=NS(id=17),
                    webhook_id=None,
                    edited_at=None,
                    channel=NS(id=route.channel_id),
                    created_at=attempted + timedelta(seconds=1),
                    embeds=[embed],
                )

        channel = NS(guild=NS(id=route.guild_id), send=send, history=history)
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), user=NS(id=17), get_channel=lambda _: channel)
        )
        assert await sdk.publish_identified(route, output, state) == 9001
        assert await sdk.find_pacing_publication(route, kind, output.output_id, attempted) == (
            9001,
            attempted + timedelta(seconds=1),
        )
        assert (
            await sdk.find_pacing_publication(route, kind, output.output_id + 1, attempted) is None
        )
