"""Positive proof for held and superseded recovery notices."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, opened, service
from test_ordinary_flow import rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.discord_store import PendingOutput


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [False, True])
async def test_held_notice_recovery_preserves_idle_deadline(tmp_path, monkeypatch, found):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        original = await activated(runtime, route)
        await settle(runtime)
        transport.fail = "evolution_held"
        await runtime.submit(route, "configure", templates=frozenset({"NR034"}))
        await settle(runtime)
        cutoff = runtime.clock.at + timedelta(minutes=15)
        runtime.clock.at += timedelta(seconds=3)
        calls = []

        async def find(exact_route, output, attempted):
            calls.append((exact_route, output))
            return (1771, attempted + timedelta(seconds=1)) if found else None

        transport.find_held_publication = find
        await runtime.initialize()
        await settle(runtime)
        assert len(calls) == 1 and calls[0][0] == route
        assert sum(kind == "evolution_held" for _, kind, _ in transport.messages) == 1
        if not found:
            assert (await runtime.state(route.session_id)).recovery_id
            assert not transport.recoveries
            return
        state = await reading(runtime, route)
        await fire(runtime, route, state.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert not state.recovery_id
        assert state.context.position.round_id == original.context.position.round_id
        assert state.context.position.advancement == "held"
        assert state.next_deadline == cutoff


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["found", "missing", "future"])
async def test_old_recovery_notice_proof_never_resumes_superseded_generation(
    tmp_path, monkeypatch, outcome
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        original = await activated(runtime, route)
        await settle(runtime)
        transport.notice_failure = True
        await runtime.initialize()
        await settle(runtime)
        first = await runtime.state(route.session_id)
        transport.notice_failure = False
        runtime.clock.at += timedelta(seconds=3)
        calls = []

        async def find(exact_route, recovery_id, attempted):
            calls.append((exact_route, recovery_id))
            if outcome == "missing":
                return None
            return 1772, attempted + timedelta(seconds=4 if outcome == "future" else 1)

        transport.find_recovery_publication = find
        await runtime.initialize()
        await settle(runtime)
        assert calls == [(route, first.recovery_id)]
        proof = rows(
            tmp_path / "game.sqlite3",
            "SELECT stage,notice_message_id,resume_at_utc FROM nr_recoveries WHERE stage='superseded'",
        )
        if outcome != "found":
            assert proof == [("superseded", None, None)]
            assert not transport.recoveries
            return
        assert proof == [("superseded", 1772, None)]
        new = await reading(runtime, route)
        assert new.recovery_id != first.recovery_id
        assert new.recovery_resume_at == runtime.clock.at + timedelta(seconds=30)
        assert len(transport.recoveries) == 1
        await fire(runtime, route, new.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert not state.recovery_id
        assert state.context.position.round_id == original.context.position.round_id


@pytest.mark.asyncio
async def test_sdk_notice_references_match_only_the_original_output(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await activated(runtime, route)
        attempted = runtime.clock.at
        send = AsyncMock(return_value=NS(id=1773))

        async def history(**kwargs):
            assert kwargs == {"limit": 50, "after": attempted, "oldest_first": True}
            yield NS(
                id=1773,
                author=NS(id=17),
                webhook_id=None,
                edited_at=None,
                channel=NS(id=route.channel_id),
                created_at=attempted + timedelta(seconds=1),
                embeds=[send.call_args.kwargs["embed"]],
                components=[
                    NS(
                        children=[
                            discord.Button(item.to_component_dict())
                            for item in send.call_args.kwargs["view"].children
                        ]
                    )
                ],
            )

        channel = NS(guild=NS(id=route.guild_id), send=send, history=history)
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), user=NS(id=17), get_channel=lambda _: channel)
        )
        await sdk.publish_identified(route, PendingOutput("evolution", 8, "held", "token"), state)
        assert await sdk.find_held_publication(route, 8, attempted)
        assert await sdk.find_held_publication(route, 9, attempted) is None
        await runtime.initialize()
        state = await reading(runtime, route)
        await sdk.publish_recovery(route, state)
        assert send.call_args.kwargs["embed"].footer.text == sdk.copy.text("reboot.recovery_footer")
        assert await sdk.find_recovery_publication(route, state.recovery_id, attempted)
        assert await sdk.find_recovery_publication(route, "other-recovery", attempted) is None
