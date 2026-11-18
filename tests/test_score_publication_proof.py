"""Verify a lost public score acknowledgement without retrying the post."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, opened, service
from test_ordinary_flow import rows
from test_public_summary import request
from test_reboot_recovery import fire, reading
from test_round_reports_live import Transport, cutover, messages

from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.offer_codec import utc_int


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["requested", "automatic", "round"])
@pytest.mark.parametrize("found", [True, False])
async def test_score_recovery_uses_original_publication_and_watermark(
    tmp_path, monkeypatch, kind, found
):
    transport = Transport()
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        active = await activated(runtime, route)
        await settle(runtime)
        await messages(runtime, route, 25, actor=303)
        transport.summary_fail = transport.round_fail = True
        if kind == "round":
            await cutover(runtime, route)
        elif kind == "automatic":
            runtime.clock.at = active.pacing.started_at + timedelta(seconds=120)
            await runtime.submit(route, "tick")
        else:
            assert await request(runtime, route) == "queued"
        await settle(runtime)
        output, page = (transport.round_reports if kind == "round" else transport.summaries)[0][1:]
        family = "round" if kind == "round" else "summary"
        output_id = output.round_number if kind == "round" else output.summary_id
        if family == "summary":
            assert output.kind == kind
        table = "nr_round_reports" if family == "round" else "nr_summary_outputs"
        assert rows(db, f"SELECT state FROM {table}") == [("uncertain",)]
        runtime.clock.at += timedelta(seconds=4)
        calls = []
        published = None

        async def find(exact_route, exact_family, exact_id, attempted_at):
            nonlocal published
            calls.append((exact_route, exact_family, exact_id))
            published = attempted_at + timedelta(seconds=1)
            return (1774, published) if found else None

        transport.find_score_publication = find
        await runtime.initialize()
        await settle(runtime)
        assert calls == [(route, family, output_id)]
        assert len(transport.round_reports if family == "round" else transport.summaries) == 1
        if not found:
            assert (await runtime.state(route.session_id)).recovery_id
            assert rows(db, f"SELECT state FROM {table}") == [("uncertain",)]
            return

        state = await reading(runtime, route)
        assert rows(db, f"SELECT state,message_id,delivered_at_utc FROM {table}") == [
            ("delivered", 1774, utc_int(published))
        ]
        watermark = page.human_messages if family == "summary" else 25
        assert rows(db, "SELECT ack_nonwhispers,public_at_utc FROM nr_summary_baselines") == [
            (watermark, utc_int(published))
        ]
        await fire(runtime, route, state.recovery_resume_at)
        assert not (await runtime.state(route.session_id)).recovery_id
        assert len(transport.round_reports if family == "round" else transport.summaries) == 1

        attempted = published - timedelta(seconds=1)
        send = AsyncMock(return_value=NS(id=1775))

        async def history(**kwargs):
            assert kwargs == {"limit": 50, "after": attempted, "oldest_first": True}
            yield NS(
                id=1775,
                author=NS(id=17),
                webhook_id=None,
                edited_at=None,
                channel=NS(id=route.channel_id),
                created_at=published,
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
        publish = sdk.publish_round if family == "round" else sdk.publish_summary
        await publish(route, output, page)
        assert not send.call_args.kwargs["embed"].footer.text
        assert await sdk.find_score_publication(route, family, output_id, attempted) == (
            1775,
            published,
        )
        assert await sdk.find_score_publication(route, family, output_id + 1, attempted) is None
