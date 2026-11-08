"""Read-only score controls use the real WAL engine and SDK without public output."""

from dataclasses import replace

import discord
import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import activated, event, opened, service, until
from test_live_feedback import interaction
from test_ordinary_flow import rows

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import requested_summary
from lets_play.discord_adapter.discord_io import render
from lets_play.presentation import default_presentation
from lets_play.storage.summary_store import SummaryStore


async def read(runtime, route, **options):
    async with runtime.store.transaction(write=False) as unit:
        return await SummaryStore(unit).live_page(route.session_id, **options)


@pytest.mark.asyncio
async def test_forward_backward_pages_are_bounded_and_do_not_mutate_state(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        for actor in range(600, 613):
            await runtime.submit(
                route, "event", event=event(runtime, route, 70000 + actor, "wrong", actor)
            )
        await runtime.submit(
            route, "event", event=event(runtime, route, 80000, "-# listening", 606)
        )
        await settle(runtime)
        before = await runtime.state(route.session_id)
        count = len(runtime.transport.messages)
        first = await read(runtime, route)
        assert first.human_messages == 14 and first.whispers == 1
        assert first.as_of == runtime.clock.now()
        actors = []
        page = first
        while True:
            assert len(page.players) <= 5
            actors.extend(a for a, _ in page.players)
            if not page.following:
                break
            page = await read(
                runtime, route, round_number=page.round_number, actor=page.players[-1][0]
            )
        expected = [
            r[0]
            for r in rows(
                tmp_path / "game.sqlite3", "SELECT actor_id FROM nr_round_scores ORDER BY actor_id"
            )
        ]
        assert actors == expected and len(actors) == len(set(actors))
        second = await read(runtime, route, actor=first.players[-1][0])
        back = await read(runtime, route, actor=second.players[0][0], backwards=True)
        assert back.players == first.players
        assert await runtime.state(route.session_id) == before
        assert len(runtime.transport.messages) == count
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_baselines") == [
            (0,)
        ]
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_outputs") == [(0,)]


@pytest.mark.asyncio
async def test_pinned_round_scores_and_current_game_totals_are_distinct(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route)
        old = await read(runtime, route)
        state = await next_offer(runtime, route)
        await runtime.submit(
            route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=0
        )
        state = await until(runtime, route, lambda s: s.offer and s.offer.stage == "selected")
        await settle(runtime)
        runtime.clock.at = state.offer.effective_at
        await runtime.submit(route, "event", event=event(runtime, route, 81000, "over", 303))
        await settle(runtime)
        pinned = await read(runtime, route, round_number=1)
        current = await read(runtime, route)
        game = await read(runtime, route, scope="game")
        assert pinned.players == old.players and pinned.human_messages == 15
        assert pinned.round_closed and not current.round_closed
        assert current.round_number == 2 and current.human_messages == 1
        assert game.human_messages == 16
        assert dict(game.players)[303].ordinary_posts == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_surface", [False, True])
async def test_sdk_summary_is_private_nonpinging_and_exact_surface_scoped(
    tmp_path, monkeypatch, wrong_surface
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 82000, "wrong", 606))
        await settle(runtime)
        state = await runtime.state(route.session_id)
        _, view = render(state, kind="rules")
        control = next(b.custom_id for b in view.children if b.custom_id.startswith("nrs:"))
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(message_id=999999)
            request.type = discord.InteractionType.component
            request.data = {"custom_id": control}
            if wrong_surface:
                request.channel_id += 1
            lanes = dict(runtime.lanes)
            assert bot.game_adapter.capture_control(request) is None
            await bot.game_adapter.component(request)
            request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
            kwargs = request.followup.send.call_args.kwargs
            assert kwargs["ephemeral"] is True
            assert ("embed" in kwargs) != wrong_surface
            if not wrong_surface:
                assert "<@606>" in str(kwargs["embed"].to_dict())
                assert kwargs["allowed_mentions"].users is False
                assert "footer" not in kwargs["embed"].to_dict()
            assert runtime.lanes == lanes
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_rules_callback_exposes_only_readonly_summary_and_preplay_has_no_dead_controls(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction()
            await bot.game_adapter.rules(request)
            assert not request.followup.send.call_args.kwargs["view"].children
            await activated(runtime, route)
            request.followup.send.reset_mock()
            await bot.game_adapter.rules(request)
            controls = request.followup.send.call_args.kwargs["view"].children
            assert len(controls) == 1 and controls[0].custom_id.startswith("nrs:")
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_recovery_and_ended_views_do_not_resume_or_allocate_retired_lanes(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.pause(route)
        paused = await read(runtime, route)
        assert paused.lifecycle == "paused"
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        assert not runtime.lanes
        ended = await read(runtime, route, scope="game")
        assert ended.lifecycle == "ended" and not runtime.lanes
        embed, _ = requested_summary.render(ended, default_presentation())
        assert "footer" not in embed.to_dict()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "options",
    [{"actor": 2**63}, {"round_number": 2**63}, {"round_number": 999}, {"scope": "other"}],
)
async def test_expired_or_oversized_controls_are_refused(tmp_path, monkeypatch, options):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        assert await read(runtime, route, **options) is None


@pytest.mark.asyncio
async def test_overlong_custom_copy_returns_private_unavailable_response(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            strings = dict(bot.presentation.strings)
            strings["results.detail"] = "x" * 2000 + strings["results.detail"]
            bot.presentation = replace(bot.presentation, strings=strings)
            request = interaction()
            request.data = {"custom_id": f"nrs:{route.session_id}:round:0:0:n"}
            await bot.game_adapter.component(request)
            request.followup.send.assert_awaited_once_with(
                bot.presentation.text("summary.unavailable"), ephemeral=True
            )
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_private_reads_preserve_uncertain_public_attempt_and_its_baseline(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        async with runtime.store.transaction() as unit:
            summaries = SummaryStore(unit)
            await summaries.prepare(
                route.session_id, "pending", kind="requested", now=runtime.clock.now()
            )
            await summaries.claim(route.session_id, "pending", now=runtime.clock.now())
            await summaries.mark_uncertain(route.session_id, "pending")
        before = rows(tmp_path / "game.sqlite3", "SELECT * FROM nr_summary_baselines")
        await runtime.submit(route, "event", event=event(runtime, route, 83000, "over", 606))
        page = await read(runtime, route, scope="game")
        assert dict(page.players)[606].ordinary_posts == 1
        assert rows(tmp_path / "game.sqlite3", "SELECT * FROM nr_summary_baselines") == before
        assert rows(tmp_path / "game.sqlite3", "SELECT state FROM nr_summary_outputs") == [
            ("uncertain",)
        ]
