"""Unattended ordinary reboot recovery, durable notice fences and real SDK I/O."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import CAPS, Transport, activated, event, opened, service, until
from test_live_feedback import interaction
from test_ordinary_flow import NOW, rows

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport, render
from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.storage.offer_codec import utc_int


class RecoveryTransport(Transport):
    def __init__(self):
        super().__init__()
        self.probes = []
        self.notice_entered = asyncio.Event()
        self.notice_block = None
        self.access = CAPS
        self.notice_failure = False

    async def recovery_capabilities(self, route, panel_id):
        self.probes.append(panel_id)
        return self.access

    async def publish_recovery(self, route, state):
        self.notice_entered.set()
        if self.notice_block:
            await self.notice_block.wait()
        if self.notice_failure:
            raise TimeoutError("PRIVATE_RECOVERY_FAILURE")
        return await super().publish_recovery(route, state)


async def reading(runtime, route):
    await until(runtime, route, lambda s: s.recovery_resume_at is not None)
    await settle(runtime)
    return await runtime.state(route.session_id)


async def fire(runtime, route, at):
    # Invoke the registered timer callback with an injected elapsed clock. There
    # is no human ingress, no manager control and no thirty-second wall sleep.
    assert route.session_id in runtime.timers
    runtime.timers[route.session_id].cancel()
    runtime.clock.at = at
    runtime._timer(route, "recovery_resume")
    await settle(runtime)


@pytest.mark.asyncio
async def test_real_reopen_announces_then_resumes_on_its_timer_preserving_scores_and_clocks(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        for n in range(5):
            await runtime.submit(route, "event", event=event(runtime, route, 80000 + n))
        await settle(runtime)
        original = await runtime.state(route.session_id)
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        saved = await reading(runtime, route)
        assert saved.context.position.lifecycle == "paused"
        assert len(transport.recoveries) == 1 and not transport.messages
        assert saved.panel_id == 20001
        assert saved.recovery_resume_at == original.admission.effective_at + timedelta(seconds=30)
        # Chat throughout the read-in is guarded and cannot score or trigger resume.
        await runtime.submit(route, "event", event=event(runtime, route, 81000))
        await fire(runtime, route, saved.recovery_resume_at - timedelta(microseconds=1))
        assert (await runtime.state(route.session_id)).recovery_id
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.recovery_id is None and resumed.context.position.lifecycle == "playing"
        assert resumed.context.position.round_id == original.context.position.round_id
        assert resumed.context.active_instances == original.context.active_instances
        assert resumed.scores == original.scores
        assert resumed.pacing.started_at == original.pacing.started_at
        assert len(transport.recoveries) == 1 and len(transport.probes) == 2
        async with runtime.store.transaction(write=False) as unit:
            assert (await unit.get_score(route.session_id, 606)).ordinary_posts == 5
        # Delivery after resume does not backfill an earlier-created event.
        historical = event(runtime, route, 81001)
        from dataclasses import replace

        historical = replace(historical, envelope=replace(historical.envelope, created_at=NOW))
        await runtime.submit(route, "event", event=historical)
        await runtime.submit(route, "event", event=event(runtime, route, 81002))
        async with runtime.store.transaction(write=False) as unit:
            assert (await unit.get_score(route.session_id, 606)).ordinary_posts == 6


@pytest.mark.asyncio
async def test_overdue_pacing_runs_after_read_in_without_resetting_the_round(tmp_path, monkeypatch):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        original = await activated(runtime, route)
        await settle(runtime)
        runtime.clock.at += timedelta(hours=1)
        await runtime.initialize()
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        state = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
        assert state.context.position.round_id == original.context.position.round_id
        assert state.pacing.started_at == original.pacing.started_at
        assert state.pacing.selection_due_at == saved.recovery_resume_at + timedelta(seconds=30)
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]


@pytest.mark.asyncio
async def test_a_human_post_after_read_in_does_not_bypass_the_scheduled_fresh_access_check(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        saved = await reading(runtime, route)
        runtime.clock.at = saved.recovery_resume_at
        await runtime.submit(route, "event", event=event(runtime, route, 82000))
        assert (await runtime.state(route.session_id)).recovery_id
        async with runtime.store.transaction(write=False) as unit:
            assert await unit.get_score(route.session_id, 606) is None
        await fire(runtime, route, saved.recovery_resume_at)
        assert (await runtime.state(route.session_id)).recovery_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("family", ["engine", "adapter", "summary", "round", "notice"])
async def test_unknown_sends_in_any_outbound_family_prevent_notice_and_automatic_resume(
    tmp_path, monkeypatch, family
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        session = route.session_id
        async with runtime.store.transaction() as unit:
            if family == "adapter":
                for n in range(100):
                    await unit._conn.execute(
                        "INSERT INTO nr_discord_deliveries VALUES (?,'ordinary',?,'delivered',?,900,10)",
                        (session, 1000 + n, utc_int(NOW)),
                    )
                await unit._conn.execute(
                    "INSERT INTO nr_discord_deliveries VALUES (?,'ordinary',2000,'sending',?,NULL,0)",
                    (session, utc_int(NOW)),
                )
            elif family == "summary":
                await unit._conn.execute(
                    "INSERT INTO nr_summary_outputs(session_id,output_key,kind,state,round_id,round_number,captured_at_utc,human_messages,whispers,round_messages,round_whispers,rules_json) SELECT session_id,'unknown','requested','sending',round_id,1,?,0,0,0,0,'[]' FROM nr_sessions",
                    (utc_int(NOW),),
                )
            elif family == "round":
                await unit._conn.execute(
                    "INSERT INTO nr_round_resolutions SELECT session_id,round_id,1,'activation' FROM nr_rounds"
                )
                await unit._conn.execute(
                    "INSERT INTO nr_round_reports SELECT session_id,round_id,'uncertain',?,NULL,NULL,0,0 FROM nr_rounds",
                    (utc_int(NOW),),
                )
            elif family == "engine":
                await unit._conn.execute(
                    "UPDATE nr_opening_outputs SET acknowledged_at_utc=NULL WHERE kind='activation'"
                )
            else:
                await unit._conn.execute(
                    "INSERT INTO nr_recoveries(session_id,recovery_id,stage,original_stage,owner_revision,began_at_utc,notice_attempted_at_utc) VALUES (?,'unknown-notice','superseded','active',0,?,?)",
                    (session, utc_int(NOW), utc_int(NOW)),
                )
        await runtime.initialize()
        await settle(runtime)
        assert (await runtime.state(session)).recovery_id
        assert not transport.recoveries and not transport.probes
        assert session not in runtime.timers


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["before_notice", "before_resume"])
async def test_lost_essential_capability_stays_paused(tmp_path, monkeypatch, when):
    from lets_play.storage.opening_flow import OpeningCapabilities

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        if when == "before_notice":
            transport.access = OpeningCapabilities(False, True, True)
        await runtime.initialize()
        await settle(runtime)
        if when == "before_resume":
            saved = await reading(runtime, route)
            transport.access = OpeningCapabilities(False, True, True)
            await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.recovery_id and state.context.position.lifecycle == "paused"
        assert len(transport.recoveries) == (when == "before_resume")
        assert state.recovery_resume_at is None
        embed, _ = render(state, kind="rules")
        assert "couldn’t finish recovery safely" in embed.description


@pytest.mark.asyncio
async def test_notice_timeout_retains_claim_across_another_reboot_and_never_blindly_resends(
    tmp_path, monkeypatch, caplog
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        transport.notice_failure = True
        await runtime.initialize()
        await settle(runtime)
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT COUNT(*) FROM nr_recoveries WHERE notice_attempted_at_utc IS NOT NULL AND notice_message_id IS NULL",
        ) == [(1,)]
        assert "PRIVATE_RECOVERY_FAILURE" not in caplog.text
        transport.notice_failure = False
        await runtime.initialize()
        await settle(runtime)
        assert not transport.recoveries and (await runtime.state(route.session_id)).recovery_id


@pytest.mark.asyncio
async def test_end_during_notice_http_keeps_ack_and_posts_final_results_without_resuming(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        transport.notice_block = asyncio.Event()
        await runtime.initialize()
        await asyncio.wait_for(transport.notice_entered.wait(), 5)
        await runtime.submit(route, "end", actor=303, authorized=True)
        transport.notice_block.set()
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.admission.stage == "ended"
        assert any(kind == "closed" for _, kind, _ in transport.messages)
        assert rows(
            tmp_path / "game.sqlite3", "SELECT stage,notice_message_id FROM nr_recoveries"
        ) == [("closed", 20001)]
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_game_finalizations") == [
            (1,)
        ]


@pytest.mark.asyncio
async def test_ready_notice_claim_is_single_use_and_rolls_back_with_its_receipt(
    tmp_path, monkeypatch
):
    # A real failure after the claim write must roll back both claim and watermark.
    from lets_play.storage import reboot_flow

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        original = reboot_flow._execute

        async def fail(conn, sql, args=()):
            result = await original(conn, sql, args)
            if "SET notice_attempted_at_utc" in sql:
                raise RuntimeError("injected")
            return result

        monkeypatch.setattr(reboot_flow, "_execute", fail)
        await runtime.initialize()
        await settle(runtime)
        assert not transport.recoveries
        assert rows(
            tmp_path / "game.sqlite3", "SELECT notice_attempted_at_utc FROM nr_recoveries"
        ) == [(None,)]
        assert rows(tmp_path / "game.sqlite3", "SELECT stage FROM nr_recoveries") == [("ready",)]


@pytest.mark.asyncio
async def test_sdk_recovery_uses_existing_panel_to_verify_marker_and_publishes_one_friendly_notice(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        state = await reading(runtime, route)
        panel = NS(add_reaction=AsyncMock(), remove_reaction=AsyncMock())
        message = NS(id=777)
        channel = NS(
            guild=NS(id=101, me=object()),
            send=AsyncMock(return_value=message),
            get_partial_message=lambda _: panel,
            permissions_for=lambda _: discord.Permissions.all(),
        )
        bot = NS(
            settings=load_settings(
                environ={"CHATGAMES_DING_EMOJI": "<a:example:123456789012345678>"}
            ),
            user=NS(id=55),
            get_channel=lambda _: channel,
        )
        transport = DiscordTransport(bot)
        assert (await transport.recovery_capabilities(route, state.panel_id)).available
        panel.add_reaction.assert_awaited_once_with(transport.marker)
        panel.remove_reaction.assert_awaited_once_with(transport.marker, bot.user)
        channel.send.assert_not_awaited()
        assert await transport.publish_recovery(route, state) == 777
        payload = channel.send.call_args.kwargs
        assert "30 seconds" in payload["embed"].description
        assert payload["allowed_mentions"].everyone is False
        assert payload["embed"].fields
        assert "Unknown" not in str(payload["embed"].to_dict())
        channel.permissions_for = lambda _: discord.Permissions.none()
        with pytest.raises(LaneUnavailable):
            await transport.recovery_capabilities(route, state.panel_id)


@pytest.mark.asyncio
async def test_rules_has_no_debug_entry_and_retired_controls_reply_privately(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        await reading(runtime, route)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction()
            await bot.game_adapter.rules(request)
            payload = request.followup.send.call_args.kwargs
            assert not any(c.custom_id.startswith("nrx:") for c in payload["view"].children)
            assert "Unknown" not in str(payload["embed"].to_dict())
            request.message.flags = NS(ephemeral=True)
            request.edit_original_response = AsyncMock()
            request.data = {"custom_id": f"nrx:{route.session_id}:retired"}
            request.followup.send.reset_mock()
            await bot.game_adapter.component(request)
            request.followup.send.assert_not_awaited()
            assert "retired" in request.edit_original_response.call_args.kwargs["content"]
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_cancelled_notice_cannot_be_retried_by_the_next_startup(tmp_path, monkeypatch):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        transport.notice_block = asyncio.Event()
        await runtime.initialize()
        await asyncio.wait_for(transport.notice_entered.wait(), 5)
        await runtime.initialize()  # Cancels the in-flight HTTP; outcome is unknown.
        await settle(runtime)
        assert not transport.recoveries
        assert route.session_id in runtime.blocked_recoveries
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT COUNT(*) FROM nr_recoveries WHERE notice_attempted_at_utc IS NOT NULL",
        ) == [(1,)]


@pytest.mark.asyncio
async def test_another_reboot_during_known_read_in_gets_one_fresh_notice_and_keeps_the_round(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        original = await activated(runtime, route)
        await runtime.initialize()
        first = await reading(runtime, route)
        runtime.clock.at += timedelta(seconds=10)
        await runtime.initialize()
        second = await reading(runtime, route)
        assert second.recovery_id != first.recovery_id
        assert second.recovery_resume_at == first.recovery_resume_at + timedelta(seconds=10)
        assert len(transport.recoveries) == 2
        await fire(runtime, route, second.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.recovery_id is None
        assert resumed.context.position.round_id == original.context.position.round_id


@pytest.mark.asyncio
async def test_all_routes_are_fenced_before_probe_and_another_routes_uncertain_send_is_isolated(
    tmp_path, monkeypatch
):
    from test_opening import Lane

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        other = Lane(runtime.store, session="another-session", guild=999, channel=888)
        await other.admit()
        async with runtime.store.transaction() as unit:
            await unit._conn.execute(
                "INSERT INTO nr_discord_deliveries VALUES ('another-session','opening',900,'sending',1,NULL,0)"
            )
        original_probe = transport.recovery_capabilities

        async def probe(route, panel):
            assert rows(
                tmp_path / "game.sqlite3",
                "SELECT COUNT(*) FROM nr_recoveries WHERE stage IN ('paused','ready','reading')",
            ) == [(2,)]
            return await original_probe(route, panel)

        transport.recovery_capabilities = probe
        await runtime.initialize()
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        assert (await runtime.state(route.session_id)).recovery_id is None
        assert (await runtime.state(other.route.session_id)).recovery_id
        assert len(transport.recoveries) == 1


@pytest.mark.asyncio
async def test_resume_probe_http_failure_removes_the_countdown_but_preserves_notice_proof(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        saved = await reading(runtime, route)
        transport.recovery_capabilities = AsyncMock(side_effect=TimeoutError("private"))
        await fire(runtime, route, saved.recovery_resume_at)
        blocked = await runtime.state(route.session_id)
        assert blocked.recovery_id and blocked.recovery_resume_at is None
        assert rows(
            tmp_path / "game.sqlite3", "SELECT stage,notice_message_id FROM nr_recoveries"
        ) == [("paused", 20001)]
