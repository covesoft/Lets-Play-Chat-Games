"""Owner-requested quiet repost policy, current views and bounded delivery state."""

import asyncio
from datetime import timedelta

import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import activated, event, opened, service, until
from test_ordinary_flow import rows
from test_public_summary import PublicTransport, latest, request
from test_summary_foundation import baseline

from lets_play.discord_adapter.discord_io import render
from lets_play.storage.game_codec import SCORE_COLUMNS
from lets_play.storage.summary_store import SummaryStore


@pytest.mark.asyncio
@pytest.mark.parametrize("traffic", [None, "-# a whisper"])
async def test_no_new_nonwhisper_posts_blocks_repeat_after_cooldown(tmp_path, monkeypatch, traffic):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await request(runtime, route)
        await settle(runtime)
        before = await baseline(runtime, route)
        runtime.clock.at += timedelta(seconds=30)
        if traffic:
            await runtime.submit(route, "event", event=event(runtime, route, 78000, traffic))
        for n in range(3):
            assert await request(runtime, route, f"quiet-{n}") == "no_new_posts"
        await settle(runtime)
        assert len(transport.summaries) == 1
        assert await baseline(runtime, route) == before
        await runtime.submit(route, "event", event=event(runtime, route, 78001, "over"))
        assert await request(runtime, route, "after-chat") == "queued"
        await settle(runtime)
        assert len(transport.summaries) == 2


@pytest.mark.asyncio
async def test_nonwhisper_arriving_during_send_allows_later_report(tmp_path, monkeypatch):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        transport.summary_block = asyncio.Event()
        await request(runtime, route)
        await asyncio.wait_for(transport.summary_entered.wait(), 5)
        await runtime.submit(route, "event", event=event(runtime, route, 79000, "over"))
        transport.summary_block.set()
        await settle(runtime)
        runtime.clock.at += timedelta(seconds=30)
        assert await request(runtime, route, "after-arrival") == "queued"
        await settle(runtime)
        assert len(transport.summaries) == 2
        assert transport.summaries[-1][1].human_messages == 1


@pytest.mark.asyncio
async def test_delivered_reports_keep_one_receipt_no_copied_scores_and_reject_old_replay(
    tmp_path, monkeypatch
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        for n in range(5):
            await runtime.submit(route, "event", event=event(runtime, route, 80000 + n, "over"))
            assert await request(runtime, route, f"report-{n}") == "queued"
            await settle(runtime)
            assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_outputs") == [
                (1,)
            ]
            assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_summary_scores") == [
                (0,)
            ]
            assert (await latest(runtime, route)).rules == ()
            runtime.clock.at += timedelta(seconds=30)
        await runtime.submit(route, "event", event=event(runtime, route, 81000, "over"))
        assert await request(runtime, route, "report-0") == "shared"
        await settle(runtime)
        assert len(transport.summaries) == 5
        assert await request(runtime, route, "fresh") == "queued"
        await settle(runtime)
        assert len(transport.summaries) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("ended", [False, True])
async def test_startup_discards_legacy_archives_in_live_and_ended_games_but_keeps_pending(
    tmp_path, monkeypatch, ended
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await request(runtime, route)
        await settle(runtime)
        if ended:
            await runtime.submit(route, "end", actor=303, authorized=True)
            await settle(runtime)
        async with runtime.store.transaction() as unit:
            conn = unit._conn
            async with conn.execute("SELECT * FROM nr_summary_outputs") as cursor:
                template = dict(await cursor.fetchone())
            del template["summary_id"]
            template["rules_json"] = '[["legacy-rule","NR034",[]]]'
            for n in range(3):
                template["output_key"] = f"legacy-{n}"
                cursor = await conn.execute(
                    f"INSERT INTO nr_summary_outputs({','.join(template)}) VALUES ({','.join('?' for _ in template)})",
                    tuple(template.values()),
                )
                legacy_id = cursor.lastrowid
                await cursor.close()
                await conn.execute(
                    f"INSERT INTO nr_summary_scores SELECT ?,'round',actor_id,{','.join(SCORE_COLUMNS)} FROM nr_participants WHERE session_id=?",
                    (legacy_id, route.session_id),
                )
            if not ended:
                summaries = SummaryStore(unit)
                pending = await summaries.prepare(
                    route.session_id, "uncertain", now=runtime.clock.now(), kind="requested"
                )
                assert await summaries.claim(route.session_id, "uncertain", now=runtime.clock.now())
                assert await summaries.mark_uncertain(route.session_id, "uncertain")
        before = await baseline(runtime, route)
        await runtime.initialize()
        await settle(runtime)
        assert await baseline(runtime, route) == before
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT COUNT(*) FROM nr_summary_outputs WHERE state='delivered'",
        ) == [(1,)]
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT COUNT(*) FROM nr_summary_scores s JOIN nr_summary_outputs o USING(summary_id) WHERE o.state='delivered'",
        ) == [(0,)]
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT rules_json FROM nr_summary_outputs WHERE state='delivered'",
        ) == [("[]",)]
        if not ended:
            async with runtime.store.transaction(write=False) as unit:
                kept = await SummaryStore(unit).pending(route.session_id)
                assert kept.summary_id == pending.summary_id and kept.state == "uncertain"
                assert kept.rules == pending.rules
                assert await SummaryStore(unit).scores(route.session_id, "uncertain")
        assert len(transport.summaries) == 1


@pytest.mark.asyncio
async def test_ack_cleanup_rolls_back_with_baseline_and_send_state(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch, transport=PublicTransport()) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        async with runtime.store.transaction() as unit:
            summaries = SummaryStore(unit)
            await summaries.prepare(
                route.session_id, "rollback", now=runtime.clock.now(), kind="requested"
            )
            await summaries.claim(route.session_id, "rollback", now=runtime.clock.now())
        before = await baseline(runtime, route)
        with pytest.raises(RuntimeError, match="rollback"):
            async with runtime.store.transaction() as unit:
                await SummaryStore(unit).acknowledge(
                    route.session_id, "rollback", now=runtime.clock.now(), message_id=9876
                )
                raise RuntimeError("rollback")
        assert await baseline(runtime, route) == before
        async with runtime.store.transaction(write=False) as unit:
            summaries = SummaryStore(unit)
            assert (await summaries.snapshot(route.session_id, "rollback")).state == "sending"
            assert await summaries.scores(route.session_id, "rollback")


@pytest.mark.asyncio
async def test_summary_omitted_from_choices_but_kept_in_rules_and_disclosure(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route)
        state = await next_offer(runtime, route)
        _, choices = render(state, kind="ordinary_offer")
        assert len(choices.children) == 3
        assert all(":pick:" in b.custom_id for b in choices.children)
        _, rules = render(state, kind="rules")
        assert any(b.custom_id.startswith("nrs:") for b in rules.children)
        await runtime.submit(
            route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=0
        )
        state = await until(runtime, route, lambda s: s.offer and s.offer.stage == "selected")
        _, disclosure = render(state, kind="ordinary_activation")
        assert any(b.custom_id.startswith("nrs:") for b in disclosure.children)
