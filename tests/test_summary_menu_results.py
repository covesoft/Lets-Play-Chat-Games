"""Live menu edits and exact, bounded final-result ranking."""

from fractions import Fraction
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, event, opened, service
from test_live_feedback import interaction

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import requested_summary
from lets_play.discord_adapter.discord_io import render
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.game_codec import SCORE_COLUMNS, encode_score
from lets_play.storage.summary_store import SummaryStore


@pytest.mark.asyncio
@pytest.mark.parametrize("editing", [False, True])
async def test_summary_opens_privately_then_edits_ephemeral_menu_in_place(
    tmp_path, monkeypatch, editing
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(message_id=123)
            request.message.flags = NS(ephemeral=editing)
            request.edit_original_response = AsyncMock()
            request.data = {"custom_id": f"nrs:{route.session_id}:game:0:0:n"}
            await bot.game_adapter.component(request)
            request.response.defer.assert_awaited_once_with(ephemeral=True, thinking=not editing)
            result = request.edit_original_response if editing else request.followup.send
            kwargs = result.call_args.kwargs
            assert "embed" in kwargs and kwargs["allowed_mentions"].users is False
            assert len(kwargs["view"].children) == 2
            assert all(
                b.label != bot.presentation.text("buttons.summary_refresh")
                for b in kwargs["view"].children
            )
            if editing:
                request.followup.send.assert_not_awaited()
            else:
                request.edit_original_response.assert_not_awaited()
                assert kwargs["ephemeral"] is True
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_summary_page_navigation_edits_and_scope_buttons_read_new_scores(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        for actor in range(600, 607):
            await runtime.submit(
                route, "event", event=event(runtime, route, 70000 + actor, "over", actor)
            )
        await settle(runtime)
        public_count = len(runtime.transport.messages)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime

            async def click(control):
                request = interaction(message_id=123)
                request.message.flags = NS(ephemeral=True)
                request.edit_original_response = AsyncMock()
                request.data = {"custom_id": control}
                await bot.game_adapter.component(request)
                request.followup.send.assert_not_awaited()
                return request.edit_original_response.call_args.kwargs

            first = await click(f"nrs:{route.session_id}:game:0:0:n")
            next_button = next(
                b
                for b in first["view"].children
                if b.label == bot.presentation.text("buttons.results_next")
            )
            second = await click(next_button.custom_id)
            assert second["embed"].fields[1].value != first["embed"].fields[1].value
            previous = next(
                b
                for b in second["view"].children
                if b.label == bot.presentation.text("buttons.results_previous")
            )
            back = await click(previous.custom_id)
            assert back["embed"].fields[1].value == first["embed"].fields[1].value
            await runtime.submit(route, "event", event=event(runtime, route, 80000, "over", 303))
            updated = await click(f"nrs:{route.session_id}:round:0:0:n")
            assert updated["embed"].fields[1].value != first["embed"].fields[1].value
            assert len(runtime.transport.messages) == public_count
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_real_manager_results_rank_highest_adjusted_score_first(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 81000, "wrong", 606))
        for n in range(5):
            await runtime.submit(
                route, "event", event=event(runtime, route, 82000 + n, "over", 707)
            )
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        state = runtime.transport.messages[-1][2]
        assert runtime.transport.messages[-1][1] == "closed"
        assert [actor for actor, _ in state.scores] == [707, 606, 303, 404, 505]
        embed, _ = render(state)
        assert "<@707>" in embed.fields[0].value


async def ranked_fixture(runtime, route):
    good = ScoreTotals(
        human_messages=1, ordinary_posts=1, ordinary_observations=1, ordinary_quality=1
    )
    almost = ScoreTotals(
        human_messages=1,
        ordinary_posts=1,
        ordinary_observations=1,
        ordinary_quality=Fraction(10**20 - 1, 10**20),
        ordinary_faults=1,
        ordinary_offending_messages=1,
    )
    assert float(good.ranking_percent()) == float(almost.ranking_percent())
    # Synthetic derived final counters exercise precision/batch boundaries without
    # manufacturing a huge number of Gateway messages or changing engine logic.
    async with runtime.store.transaction() as unit:
        for actor in range(599, 731):
            values = (route.session_id, actor, *encode_score(almost if actor == 599 else good))
            await unit._conn.execute(
                f"INSERT INTO nr_participants(session_id,actor_id,{','.join(SCORE_COLUMNS)}) VALUES ({','.join('?' for _ in values)})",
                values,
            )


@pytest.mark.asyncio
async def test_ranked_pages_cover_all_players_with_exact_ties_and_unrated_last(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        await ranked_fixture(runtime, route)
        first = await runtime.state(route.session_id)
        assert [a for a, _ in first.scores] == list(range(600, 610))
        state = first
        actors = []
        while True:
            actors.extend(a for a, _ in state.scores)
            if not state.more_scores:
                break
            state = await runtime.state(route.session_id, result_anchor=state.scores[-1][0])
        assert actors == [*range(600, 731), 599, 303, 404, 505]
        second = await runtime.state(route.session_id, result_anchor=first.scores[-1][0])
        previous = await runtime.state(
            route.session_id, result_anchor=second.scores[0][0], result_backwards=True
        )
        assert previous.scores == first.scores and not previous.previous_scores
        async with runtime.store.transaction(write=False) as unit:
            summary = await SummaryStore(unit).live_page(route.session_id, scope="game")
        assert [a for a, _ in summary.players] == [303, 404, 505, 599, 600]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_surface", [False, True])
async def test_final_result_paging_edits_private_menu_without_allocating_a_lane(
    tmp_path, monkeypatch, wrong_surface
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        await ranked_fixture(runtime, route)
        state = await runtime.state(route.session_id)
        _, view = render(state)
        next_button = view.children[0]
        assert requested_summary.RESULTS_CONTROL.fullmatch(next_button.custom_id)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(message_id=123)
            request.message.flags = NS(ephemeral=True)
            request.edit_original_response = AsyncMock()
            request.data = {"custom_id": next_button.custom_id}
            if wrong_surface:
                request.guild_id += 1
            await bot.game_adapter.component(request)
            request.followup.send.assert_not_awaited()
            kwargs = request.edit_original_response.call_args.kwargs
            if wrong_surface:
                assert kwargs["embed"] is None
                assert kwargs["content"] == bot.presentation.text("results.unavailable")
            else:
                embed = kwargs["embed"]
                assert "<@610>" in embed.fields[0].value and embed.footer.text
            assert not runtime.lanes
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_old_result_controls_reopen_ranked_first_page_in_same_private_panel(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        await ranked_fixture(runtime, route)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(message_id=123)
            request.message.flags = NS(ephemeral=True)
            request.edit_original_response = AsyncMock()
            request.data = {"custom_id": f"nr:{route.session_id}:results:1"}
            await bot.game_adapter.component(request)
            request.followup.send.assert_not_awaited()
            kwargs = request.edit_original_response.call_args.kwargs
            assert "<@600>" in kwargs["embed"].fields[0].value
            assert all(
                requested_summary.RESULTS_CONTROL.fullmatch(b.custom_id)
                for b in kwargs["view"].children
            )
            bot.game_runtime = None
