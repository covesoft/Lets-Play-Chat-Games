"""Interrupted activation sends need evidence strictly before cutover."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, choice, opened, service, until
from test_ordinary_flow import rows
from test_reboot_choices import offered
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.discord_store import PendingOutput


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["opening", "ordinary"])
@pytest.mark.parametrize("offset", [-1, 0, 1, None])
async def test_interrupted_activation_honors_original_cutover(
    tmp_path, monkeypatch, family, offset
):
    transport = RecoveryTransport()
    database = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        if family == "opening":
            state = await choice(runtime, route)
            picker = state.admission.picker_id
            original_round = None
            kind = "activation"
        else:
            await activated(runtime, route)
            state = await offered(runtime, route)
            picker = state.offer.hand.picker_id
            original_round = state.context.position.round_id
            kind = "ordinary_activation"
        transport.fail = kind
        await runtime.submit(route, "choose", actor=picker, message_id=state.panel_id, index=0)
        saved = await until(runtime, route, lambda s: s.recovery_id is not None)
        await settle(runtime)
        effective = saved.offer.effective_at if saved.offer else saved.admission.effective_at
        runtime.clock.at = effective + timedelta(seconds=2)
        calls = []

        async def find(exact_route, claimed_family, output_id, attempted_at):
            calls.append((exact_route, claimed_family, output_id))
            if offset is None:
                return None
            published = effective + timedelta(seconds=offset)
            assert attempted_at <= published
            return 9991, published

        transport.find_activation_publication = find
        await runtime.initialize()
        await settle(runtime)
        assert len(calls) == 1 and calls[0][:2] == (route, family)
        assert sum(k == kind for _, k, _ in transport.messages) == 1
        state = await runtime.state(route.session_id)
        if original_round is None:
            assert state.context is None
        else:
            assert state.context.position.round_id == original_round
        assert rows(database, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        if offset != -1:
            assert state.recovery_id and not transport.recoveries
            assert (
                rows(database, "SELECT state FROM nr_discord_deliveries WHERE message_id=9991")
                == []
            )
            return

        read_in = await reading(runtime, route)
        assert read_in.panel_id != 9991
        await fire(runtime, route, read_in.recovery_resume_at)
        after = await runtime.state(route.session_id)
        assert after.recovery_id is None
        assert after.context.position.round_id != original_round
        assert after.pacing.started_at == effective
        assert rows(database, "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (int(family == "ordinary"),)
        ]
        assert rows(database, "SELECT state FROM nr_discord_deliveries WHERE message_id=9991") == [
            ("delivered",)
        ]


@pytest.mark.asyncio
async def test_activation_reference_distinguishes_output_families(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await choice(runtime, route)
        await runtime.submit(
            route, "choose", actor=state.admission.picker_id, message_id=state.panel_id, index=0
        )
        await settle(runtime)
        selected = await runtime.state(route.session_id)
        sent = AsyncMock(return_value=NS(id=9991))
        attempted_at = runtime.clock.now()

        async def history(**kwargs):
            assert kwargs["limit"] == 50
            yield NS(
                id=9991,
                author=NS(id=17),
                webhook_id=None,
                edited_at=None,
                channel=NS(id=route.channel_id),
                created_at=attempted_at + timedelta(seconds=1),
                embeds=[sent.call_args.kwargs["embed"]],
            )

        channel = NS(guild=NS(id=route.guild_id), send=sent, history=history)
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), user=NS(id=17), get_channel=lambda _: channel)
        )
        await sdk.publish_identified(
            route, PendingOutput("opening", 8, "activation", "activation"), selected
        )
        assert await sdk.find_activation_publication(route, "opening", 8, attempted_at) == (
            9991,
            attempted_at + timedelta(seconds=1),
        )
        assert await sdk.find_activation_publication(route, "ordinary", 8, attempted_at) is None
