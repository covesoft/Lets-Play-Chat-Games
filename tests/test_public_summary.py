"""Requested public delivery through the real ordered runtime and WAL store."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import Transport, activated, event, opened, service
from test_live_feedback import interaction
from test_ordinary_flow import rows
from test_summary_foundation import baseline

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import requested_summary
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.presentation import PresentationError, default_presentation
from lets_play.storage.summary_store import SummaryStore


class PublicTransport(Transport):
    def __init__(self):
        super().__init__()
        self.summaries = []
        self.summary_entered = asyncio.Event()
        self.summary_block = None
        self.summary_fail = False

    async def publish_summary(self, route, snapshot, page):
        self.summaries.append((route, snapshot, page))
        self.summary_entered.set()
        if self.summary_block:
            await self.summary_block.wait()
        if self.summary_fail:
            raise TimeoutError("private error text must not be logged")
        requested_summary.render(page, default_presentation(), snapshot=snapshot, public=True)
        return 9000 + len(self.summaries)


async def request(runtime, route, source="public-one"):
    ids = getattr(runtime, "test_request_ids", None)
    if ids is None:
        ids = runtime.test_request_ids = {}
    ids.setdefault(source, str(100000 + len(ids)))
    return await runtime.submit(route, "summary_request", actor=303, source=ids[source])


async def latest(runtime, route):
    async with runtime.store.transaction(write=False) as unit:
        async with unit._conn.execute(
            "SELECT output_key FROM nr_summary_outputs WHERE session_id=? ORDER BY summary_id DESC LIMIT 1",
            (route.session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return await SummaryStore(unit).snapshot(route.session_id, row[0]) if row else None


@pytest.mark.asyncio
async def test_public_request_coalesces_pending_and_recent_success_then_allows_next(
    tmp_path, monkeypatch
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        transport.summary_block = asyncio.Event()
        assert await request(runtime, route) == "queued"
        await asyncio.wait_for(transport.summary_entered.wait(), 5)
        sending = await latest(runtime, route)
        assert sending.state == "sending"
        assert await baseline(runtime, route) == (
            int(runtime.clock.now().timestamp() * 1_000_000),
            0,
        )
        assert set(
            await asyncio.gather(*(request(runtime, route, f"burst-{n}") for n in range(12)))
        ) == {"shared"}
        transport.summary_block.set()
        await settle(runtime)
        snapshot = await latest(runtime, route)
        assert snapshot.state == "delivered" and snapshot.message_id == 9001
        assert await request(runtime, route, "public-two") == "recent"
        runtime.clock.at += timedelta(seconds=29)
        await runtime.submit(route, "event", event=event(runtime, route, 70999, "over"))
        assert await request(runtime, route, "public-two") == "recent"
        runtime.clock.at += timedelta(seconds=1)
        assert await request(runtime, route, "public-two") == "queued"
        await settle(runtime)
        assert len(transport.summaries) == 2
        assert await request(runtime, route, "public-one") == "shared"
        await settle(runtime)
        assert len(transport.summaries) == 2


@pytest.mark.asyncio
async def test_public_snapshot_and_ack_keep_arrivals_during_send_pending(tmp_path, monkeypatch):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 71000, "wrong"))
        await settle(runtime)
        transport.summary_block = asyncio.Event()
        assert await request(runtime, route) == "queued"
        await asyncio.wait_for(transport.summary_entered.wait(), 5)
        frozen = await latest(runtime, route)
        runtime.clock.at += timedelta(seconds=3)
        for n in range(25):
            await runtime.submit(route, "event", event=event(runtime, route, 72000 + n, "-# hi"))
        assert (await latest(runtime, route)).human_messages == 1
        transport.summary_block.set()
        await settle(runtime)
        assert (await baseline(runtime, route))[1] == 1
        async with runtime.store.transaction(write=False) as unit:
            summaries = SummaryStore(unit)
            assert await summaries.next_deadline(
                route.session_id
            ) == runtime.clock.now() + timedelta(seconds=60)
            live = await summaries.live_page(route.session_id, scope="game")
        assert frozen.human_messages == 1 and live.human_messages == 26
        assert dict(transport.summaries[0][2].players)[606].ordinary_faults == 1
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_scores") == [(0,)]
        assert live.whispers == 25
        # This release does not activate the automatic sender, even once due.
        runtime.clock.at += timedelta(seconds=61)
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert len(transport.summaries) == 1


@pytest.mark.asyncio
async def test_unknown_public_send_pauses_without_discard_or_blind_retry(
    tmp_path, monkeypatch, caplog
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 71000, "over"))
        transport.summary_fail = True
        assert await request(runtime, route) == "queued"
        await settle(runtime)
        assert (await latest(runtime, route)).state == "uncertain"
        assert (await baseline(runtime, route))[1] == 0
        assert (await runtime.state(route.session_id)).recovery_id
        assert await request(runtime, route, "again") == "paused"
        await runtime.initialize()
        await settle(runtime)
        assert len(transport.summaries) == 1
        assert "private error text" not in caplog.text
        await runtime.submit(route, "end", authorized=True, actor=303)
        await settle(runtime)
        assert len(transport.summaries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("closure", ["pause", "end"])
async def test_known_public_success_is_recorded_during_pause_or_end(tmp_path, monkeypatch, closure):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 71000, "over"))
        transport.summary_block = asyncio.Event()
        await request(runtime, route)
        await asyncio.wait_for(transport.summary_entered.wait(), 5)
        await runtime.submit(route, closure, authorized=True, actor=303)
        runtime.clock.at += timedelta(seconds=2)
        transport.summary_block.set()
        await settle(runtime)
        assert (await latest(runtime, route)).state == "delivered"
        assert (await baseline(runtime, route))[1] == 1
        state = await runtime.state(route.session_id)
        assert state.recovery_id if closure == "pause" else state.admission.stage == "ended"
        assert len(transport.summaries) == 1


@pytest.mark.asyncio
async def test_shutdown_retains_claim_and_restart_does_not_repost(tmp_path, monkeypatch):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        transport.summary_block = asyncio.Event()
        await request(runtime, route)
        await asyncio.wait_for(transport.summary_entered.wait(), 5)
        await runtime.initialize()
        assert (await latest(runtime, route)).state == "sending"
        assert (await baseline(runtime, route))[1] == 0
        assert (await runtime.state(route.session_id)).recovery_id
        transport.summary_block.set()
        assert await request(runtime, route, "after-restart") == "paused"
        await settle(runtime)
        assert len(transport.summaries) == 1


@pytest.mark.asyncio
async def test_sdk_post_captures_before_defer_and_edits_current_private_panel(
    tmp_path, monkeypatch
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=123)
            click.message.flags = NS(ephemeral=True)
            click.edit_original_response = AsyncMock()
            click.data = {"custom_id": f"nrp:{route.session_id}"}

            async def deferred(**kwargs):
                assert runtime.lanes[route.session_id].queue.qsize() >= 1
                assert kwargs == {"ephemeral": True, "thinking": False}

            click.response.defer.side_effect = deferred
            await bot.game_adapter.component(click)
            await settle(runtime)
            click.followup.send.assert_not_awaited()
            reply = click.edit_original_response.call_args.kwargs
            assert reply["allowed_mentions"].users is False
            assert reply["content"] == bot.presentation.text("summary.post_queued")
            assert len(transport.summaries) == 1
            bot.game_runtime = None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["public", "private", "wrong", "bot", "oversized"])
async def test_legacy_saved_buttons_open_current_private_scores_on_exact_surface(
    tmp_path, monkeypatch, surface
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await request(runtime, route)
        await settle(runtime)
        snapshot = await latest(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 75000, "over", 303))
        await settle(runtime)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=9001)
            click.message.flags = NS(ephemeral=surface == "private")
            click.edit_original_response = AsyncMock()
            click.data = {
                "custom_id": f"nrf:{route.session_id}:{snapshot.summary_id}:game:{2**63 if surface == 'oversized' else 0}:n"
            }
            if surface == "wrong":
                click.channel_id += 1
            if surface == "bot":
                click.user.bot = True
            await bot.game_adapter.component(click)
            reply = (
                click.edit_original_response if surface == "private" else click.followup.send
            ).call_args.kwargs
            assert reply["allowed_mentions"].users is False
            if surface in {"public", "private"}:
                assert reply["embed"].footer.text is None
                assert "Ordinary posts: 1" in str(reply["embed"].to_dict())
                assert any("<@303>" in field.value for field in reply["embed"].fields)
                assert all(not b.custom_id.startswith("nrf:") for b in reply["view"].children)
            else:
                assert reply["content"] == bot.presentation.text(
                    "control.unavailable" if surface == "bot" else "summary.unavailable"
                )
            assert len(transport.summaries) == 1
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_public_transport_uses_saved_rules_no_footer_and_suppresses_mentions(
    tmp_path, monkeypatch
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await request(runtime, route)
        await settle(runtime)
        _, snapshot, page = transport.summaries[0]
        channel = NS(guild=NS(id=101), send=AsyncMock(return_value=NS(id=9876)))
        bot = NS(get_channel=lambda _: channel, settings=NS(ding_emoji=None))
        sdk = DiscordTransport(bot)
        assert await sdk.publish_summary(route, snapshot, page) == 9876
        kwargs = channel.send.call_args.kwargs
        assert kwargs["allowed_mentions"].to_dict() == discord.AllowedMentions.none().to_dict()
        assert kwargs["embed"].footer.text is None
        assert "Roger Wilco" in str(kwargs["embed"].to_dict())
        assert len(kwargs["view"].children) == 1
        assert kwargs["view"].children[0].custom_id == f"nrs:{route.session_id}:round:0:0:n"
        assert kwargs["view"].children[0].label == default_presentation().text(
            "buttons.summary_current"
        )
        copy = default_presentation()
        bad = replace(copy, strings={**copy.strings, "summary.capture_detail": "x" * 1100})
        with pytest.raises(PresentationError):
            requested_summary.render(page, bad, snapshot=snapshot, public=True)


@pytest.mark.asyncio
async def test_preplay_and_retired_controls_cannot_post(tmp_path, monkeypatch):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        assert await request(runtime, route) == "unavailable"
        await runtime.submit(route, "end", authorized=True, actor=303)
        await settle(runtime)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=123)
            click.data = {"custom_id": f"nrp:{route.session_id}"}
            await bot.game_adapter.component(click)
            assert click.followup.send.call_args.args == ()
            assert click.followup.send.call_args.kwargs["content"] == bot.presentation.text(
                "summary.unavailable"
            )
            assert len(transport.summaries) == 0
            assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_outputs") == [
                (0,)
            ]
            bot.game_runtime = None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["channel", "session", "bot"])
async def test_wrong_public_request_controls_do_not_allocate_receipts(
    tmp_path, monkeypatch, invalid
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=123)
            click.type = discord.InteractionType.component
            click.data = {
                "custom_id": f"nrp:{'f' * 32 if invalid == 'session' else route.session_id}"
            }
            if invalid == "channel":
                click.channel_id += 1
            if invalid == "bot":
                click.user.bot = True
            async with runtime.store.transaction(write=False) as unit:
                before = await unit._conn.execute_fetchall("SELECT * FROM nr_flow_watermarks")
            assert bot.game_adapter.capture_control(click) is None
            await bot.game_adapter.component(click)
            async with runtime.store.transaction(write=False) as unit:
                after = await unit._conn.execute_fetchall("SELECT * FROM nr_flow_watermarks")
            assert [tuple(r) for r in before] == [tuple(r) for r in after]
            assert len(transport.summaries) == 0
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_request_at_inactivity_expiry_only_closes_existing_panel(tmp_path, monkeypatch):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.pacing.started_at + timedelta(seconds=870)
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        runtime.clock.at = state.revival.deadline
        assert await request(runtime, route) == "unavailable"
        await settle(runtime)
        assert (await runtime.state(route.session_id)).admission.stage == "ended"
        assert transport.messages[-1][1] == "evolution_closed_silent"
        assert len(transport.summaries) == 0


@pytest.mark.asyncio
async def test_old_public_report_buttons_open_current_totals_after_manager_end(
    tmp_path, monkeypatch
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await request(runtime, route)
        await settle(runtime)
        snapshot = await latest(runtime, route)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=9001)
            click.data = {"custom_id": f"nrf:{route.session_id}:{snapshot.summary_id}:round:0:n"}
            await bot.game_adapter.component(click)
            kwargs = click.followup.send.call_args.kwargs
            assert "embed" in kwargs and kwargs["ephemeral"] is True
            assert len(transport.summaries) == 1
            bot.game_runtime = None
