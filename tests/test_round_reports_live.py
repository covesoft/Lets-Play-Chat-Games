"""Busy/quiet round reporting through real receipts, fake time and injected HTTP."""

import asyncio
from datetime import timedelta
from fractions import Fraction
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_ordinary_loop import next_offer, settle
from test_discord_runtime import activated, event, opened, service
from test_live_feedback import interaction
from test_ordinary_flow import rows
from test_public_summary import PublicTransport, request

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import requested_summary, round_report_ui
from lets_play.presentation import default_presentation
from lets_play.storage.round_reports import RoundReports
from lets_play.storage.summary_store import SummaryStore


class Transport(PublicTransport):
    def __init__(self):
        super().__init__()
        self.round_entered = asyncio.Event()
        self.round_block = None
        self.round_fail = False

    async def publish_round(self, route, report, page):
        self.round_reports.append((route, report, page))
        self.round_entered.set()
        if self.round_block:
            await self.round_block.wait()
        if self.round_fail:
            raise TimeoutError("private network details")
        round_report_ui.render(route.session_id, report.round_number, page, default_presentation())
        return 11000 + len(self.round_reports)


async def messages(runtime, route, count, *, base=900000, text="over", actor=606):
    for n in range(count):
        await runtime.submit(route, "event", event=event(runtime, route, base + n, text, actor))


async def cutover(runtime, route):
    state = await next_offer(runtime, route)
    await runtime.submit(
        route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=0
    )
    await settle(runtime)
    state = await runtime.state(route.session_id)
    assert not runtime.transport.round_reports
    runtime.clock.at = state.offer.effective_at
    await runtime.submit(route, "tick")


@pytest.mark.asyncio
async def test_interim_and_gate_timer_fires_without_next_post_and_resets_on_manual_success(
    tmp_path, monkeypatch
):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        start = runtime.clock.now()
        await messages(runtime, route, 200, text="-# hi")
        runtime.clock.at = start + timedelta(seconds=120)
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert not transport.summaries
        runtime.clock.at = start + timedelta(seconds=121)
        await messages(runtime, route, 24, base=910000)
        await settle(runtime)
        assert not transport.summaries
        await messages(runtime, route, 1, base=920000)
        await settle(runtime)
        assert len(transport.summaries) == 1
        assert transport.summaries[0][1].ordinary_posts == 25
        runtime.clock.at += timedelta(seconds=30)
        await messages(runtime, route, 1, base=930000)
        assert await request(runtime, route, "manual") == "queued"
        await settle(runtime)
        await messages(runtime, route, 25, base=940000)
        await settle(runtime)
        # Due at 271s, but warning at 270s suppresses this entire next window.
        async with runtime.store.transaction(write=False) as unit:
            assert (
                await SummaryStore(unit).next_deadline(route.session_id, now=runtime.clock.now())
                is None
            )
        assert len(transport.summaries) == 2


@pytest.mark.asyncio
async def test_latched_traffic_schedules_120_second_callback_without_another_message(
    tmp_path, monkeypatch
):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        start = runtime.clock.now()
        await messages(runtime, route, 25)
        await settle(runtime)
        runtime.clock.at = start + timedelta(seconds=119)
        await runtime.submit(route, "tick")
        await settle(runtime)
        remaining = runtime.timers[route.session_id].when() - asyncio.get_running_loop().time()
        assert 0 < remaining <= 1
        assert not transport.summaries
        runtime.clock.at += timedelta(seconds=1)
        runtime._timer(route, "tick")
        await settle(runtime)
        assert len(transport.summaries) == 1
        snapshot = transport.summaries[0][1]
        embed, _ = requested_summary.render(
            transport.summaries[0][2], default_presentation(), snapshot=snapshot, public=True
        )
        assert embed.description == "Round 1 is underway, here's how you're doing:"


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds,expected", [(209, 1), (210, 0), (270, 0), (300, 0)])
async def test_warning_buffer_and_selection_suppress_interims(
    tmp_path, monkeypatch, seconds, expected
):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        start = runtime.clock.now()
        await messages(runtime, route, 24)
        runtime.clock.at = start + timedelta(seconds=seconds)
        await messages(runtime, route, 1, base=920000)
        await settle(runtime)
        assert len(transport.summaries) == expected
        assert not transport.round_reports


@pytest.mark.asyncio
async def test_100_players_round_scores_include_selection_posts_and_rank_all_pages(
    tmp_path, monkeypatch
):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        for actor in range(1000, 1100):
            await messages(
                runtime,
                route,
                5,
                base=actor * 100,
                text="over" if actor % 2 else "wrong",
                actor=actor,
            )
        state = await next_offer(runtime, route)
        assert not transport.round_reports
        await messages(runtime, route, 1, base=999000, text="wrong", actor=1001)
        await runtime.submit(
            route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=0
        )
        await settle(runtime)
        assert not transport.round_reports
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert len(transport.round_reports) == 1
        report = transport.round_reports[0][1]
        assert report.round_number == 1
        async with runtime.store.transaction(write=False) as unit:
            store = RoundReports(unit)
            page = await store.page(route.session_id, 1)
            first = page
            all_players = []
            while True:
                all_players.extend(page[0])
                if not page[2]:
                    break
                page = await store.page(route.session_id, 1, anchor=page[0][-1][0])
            second = await store.page(route.session_id, 1, anchor=first[0][-1][0])
            back = await store.page(route.session_id, 1, anchor=second[0][0][0], backwards=True)
        assert back[0] == first[0]
        assert len({a for a, _ in all_players}) == len(all_players) >= 100
        expected = sorted(
            all_players,
            key=lambda p: (
                p[1].ranking_percent() is None,
                -(p[1].ranking_percent() or Fraction()),
                p[0],
            ),
        )
        assert all_players == expected
        assert dict(all_players)[1001].ordinary_posts == 6
        embed, view = round_report_ui.render(route.session_id, 1, first, default_presentation())
        assert embed.description == "Round 1 is over, here's how you did:"
        assert embed.footer.text is None and len(embed.fields) == 20
        assert all(f.name.startswith("Scoring ") and f.value.startswith("<@") for f in embed.fields)
        assert all(len(b.custom_id) <= 100 for b in view.children)
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_scores") == [(0,)]
        async with runtime.store.transaction(write=False) as unit:
            assert (
                await SummaryStore(unit).next_deadline(route.session_id, now=runtime.clock.now())
                is None
            )
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert len(transport.round_reports) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["pause", "end"])
async def test_known_round_send_success_survives_pause_or_end(tmp_path, monkeypatch, closure):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await messages(runtime, route, 15, actor=303)
        transport.round_block = asyncio.Event()
        await cutover(runtime, route)
        await asyncio.wait_for(transport.round_entered.wait(), 5)
        await runtime.submit(route, closure, actor=303, authorized=True)
        transport.round_block.set()
        await settle(runtime)
        assert rows(tmp_path / "game.sqlite3", "SELECT state FROM nr_round_reports") == [
            ("delivered",)
        ]
        state = await runtime.state(route.session_id)
        assert state.recovery_id if closure == "pause" else state.admission.stage == "ended"
        assert len(transport.round_reports) == 1


@pytest.mark.asyncio
async def test_uncertain_round_send_is_not_retried_after_restart(tmp_path, monkeypatch, caplog):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await messages(runtime, route, 15, actor=303)
        transport.round_fail = True
        await cutover(runtime, route)
        await settle(runtime)
        assert rows(tmp_path / "game.sqlite3", "SELECT state FROM nr_round_reports") == [
            ("uncertain",)
        ]
        await runtime.initialize()
        await settle(runtime)
        assert len(transport.round_reports) == 1
        assert "private network details" not in caplog.text


@pytest.mark.asyncio
async def test_start_in_occupied_channel_never_probes_even_without_route_cache(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        bot = LetsPlayBot(
            load_settings(base_dir=tmp_path, environ={"CHATGAMES_DEVELOPMENT_MODE": "true"})
        )
        async with bot:
            bot.game_runtime = runtime
            runtime.routes.clear()
            runtime.transport.probe = AsyncMock()
            click = interaction(message_id=123)
            await bot.game_adapter.start(click)
            runtime.transport.probe.assert_not_awaited()
            assert click.followup.send.call_args.args == (
                bot.presentation.text("start.channel_busy"),
            )
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_round_paging_is_private_scoped_and_edits_existing_menu(tmp_path, monkeypatch):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await messages(runtime, route, 15, actor=303)
        await cutover(runtime, route)
        await settle(runtime)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            for wrong in (False, True):
                click = interaction(message_id=123)
                click.message.flags = NS(ephemeral=True)
                click.edit_original_response = AsyncMock()
                click.data = {"custom_id": f"nrl:{route.session_id}:1:n:0"}
                if wrong:
                    click.channel_id += 1
                await bot.game_adapter.component(click)
                click.followup.send.assert_not_awaited()
                reply = click.edit_original_response.call_args.kwargs
                assert reply["allowed_mentions"].users is False
                assert (
                    ("content" in reply and reply["content"] is not None)
                    if wrong
                    else reply["embed"].footer.text is None
                )
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_prepared_auto_is_discarded_if_send_slot_opens_inside_warning_buffer(
    tmp_path, monkeypatch
):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        start = runtime.clock.now()
        for _ in range(4):
            await runtime.network_slots.acquire()
        await messages(runtime, route, 25)
        runtime.clock.at = start + timedelta(seconds=120)
        await runtime.submit(route, "tick")
        async with runtime.store.transaction(write=False) as unit:
            pending = await SummaryStore(unit).pending(route.session_id)
            assert pending.kind == "automatic" and pending.state == "prepared"
        runtime.clock.at = start + timedelta(seconds=210)
        for _ in range(4):
            runtime.network_slots.release()
        await settle(runtime)
        assert not transport.summaries
        async with runtime.store.transaction(write=False) as unit:
            assert await SummaryStore(unit).pending(route.session_id) is None
            assert (
                await SummaryStore(unit).next_deadline(route.session_id, now=runtime.clock.now())
                is None
            )


@pytest.mark.asyncio
async def test_automatic_unknown_send_preserves_traffic_and_enters_recovery(tmp_path, monkeypatch):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await messages(runtime, route, 25)
        transport.summary_fail = True
        runtime.clock.at += timedelta(seconds=120)
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert (await runtime.state(route.session_id)).recovery_id
        assert rows(tmp_path / "game.sqlite3", "SELECT ack_messages FROM nr_summary_baselines") == [
            (0,)
        ]
        assert rows(tmp_path / "game.sqlite3", "SELECT state FROM nr_summary_outputs") == [
            ("uncertain",)
        ]
        await runtime.initialize()
        assert len(transport.summaries) == 1


@pytest.mark.asyncio
async def test_round_success_preserves_new_round_posts_received_during_http(tmp_path, monkeypatch):
    transport = Transport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await messages(runtime, route, 15, actor=303)
        transport.round_block = asyncio.Event()
        await cutover(runtime, route)
        await asyncio.wait_for(transport.round_entered.wait(), 5)
        await messages(runtime, route, 25, base=970000)
        transport.round_block.set()
        await settle(runtime)
        assert rows(tmp_path / "game.sqlite3", "SELECT ack_messages FROM nr_summary_baselines") == [
            (15,)
        ]
        async with runtime.store.transaction(write=False) as unit:
            due = await SummaryStore(unit).next_deadline(route.session_id, now=runtime.clock.now())
        assert due == runtime.clock.now() + timedelta(seconds=120)
        runtime.clock.at = due
        runtime._timer(route, "tick")
        await settle(runtime)
        assert len(transport.summaries) == 1
        assert transport.summaries[0][1].round_number == 2
