"""Automatic interims omit administrative metadata; detailed views stay intact."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, event, opened, service

from lets_play.discord_adapter import requested_summary
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.presentation import default_presentation
from lets_play.storage.summary_store import SummaryStore


@pytest.mark.asyncio
async def test_automatic_layout_keeps_rules_and_player_entries_without_report_or_order_blocks(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        for n in range(25):
            await runtime.submit(route, "event", event=event(runtime, route, 800000 + n, "over"))
        runtime.clock.at += timedelta(seconds=120)
        await runtime.submit(route, "tick")
        await settle(runtime)
        _, snapshot, page = runtime.transport.summaries[0]
        copy = default_presentation()
        automatic, view = requested_summary.render(page, copy, snapshot=snapshot, public=True)
        manual, _ = requested_summary.render(
            page, copy, snapshot=replace(snapshot, kind="requested"), public=True
        )
        assert automatic.title == copy.text("reports.mid_title")
        assert automatic.description == copy.text("reports.mid_description", round=1)
        assert automatic.fields[0].name == copy.text("summary.rules_heading")
        assert len(automatic.fields) == 1 + len(page.players)
        assert [f.value for f in automatic.fields[1:]] == [
            f.value for f in manual.fields[-len(page.players) :]
        ]
        assert all(f.name == copy.text("results.player_heading") for f in automatic.fields[1:])
        assert automatic.footer.text is None
        assert view.children[0].label == copy.text("buttons.summary_current")
        assert any(f.name == copy.text("summary.capture_heading") for f in manual.fields)
        assert any(f.name == copy.text("summary.scope_round") for f in manual.fields)
        async with runtime.store.transaction(write=False) as unit:
            live = await SummaryStore(unit).live_page(route.session_id)
        detailed, _ = requested_summary.render(live, copy)
        assert detailed.fields[0].name == copy.text("summary.scope_round")
        assert detailed.fields[0].value == copy.text("summary.order")
        channel = NS(guild=NS(id=101), send=AsyncMock(return_value=NS(id=555)))
        sdk = DiscordTransport(NS(settings=NS(ding_emoji=None), get_channel=lambda _: channel))
        await sdk.publish_summary(route, snapshot, page)
        payload = channel.send.call_args.kwargs
        assert payload["embed"].to_dict() == automatic.to_dict()
        assert payload["allowed_mentions"].users is False
