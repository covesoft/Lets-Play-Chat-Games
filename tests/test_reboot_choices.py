"""Known warning/choice/activation recovery through real WAL and SDK boundaries."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_choice_panel import warning
from test_discord_ordinary_loop import choose_card, custom_open, next_offer, posts, settle
from test_discord_runtime import activated, event, opened, service, until
from test_ordinary_flow import rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport, render
from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.discord_adapter.ordinary_ui import resumption_rules
from lets_play.presentation import default_presentation


async def offered(runtime, route):
    await posts(runtime, route)
    await next_offer(runtime, route)
    await settle(runtime)
    return await runtime.state(route.session_id)


async def partial(runtime, monkeypatch):
    route = await custom_open(runtime, monkeypatch, "NR025")
    await runtime.submit(route, "configure", templates=frozenset({"NR027", "NR004", "NR006"}))
    state = await offered(runtime, route)
    await choose_card(runtime, route, state, "NR027")
    await runtime.submit(route, "configure", templates=frozenset({"NR022", "NR004", "NR006"}))
    await posts(runtime, route, base=30000)
    await next_offer(runtime, route)
    await settle(runtime)
    state = await runtime.state(route.session_id)
    index = next(i for i, o in enumerate(state.offer.hand.options) if o.incoming.card_id == "NR022")
    assert (
        await runtime.submit(
            route,
            "choose",
            actor=state.offer.hand.picker_id,
            message_id=state.panel_id,
            index=index,
        )
        == "needs_target"
    )
    await settle(runtime)
    return route, index, await runtime.state(route.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [0, 15])
async def test_acknowledged_warning_keeps_original_cutoff_and_ignores_paused_picker_activity(
    tmp_path, monkeypatch, delay
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        original = await warning(runtime, route)
        cutoff = original.pacing.selection_due_at
        runtime.clock.at += timedelta(seconds=delay)
        await runtime.initialize()
        saved = await reading(runtime, route)
        for n in range(5):
            await runtime.submit(
                route, "event", event=event(runtime, route, 92000 + n, "over", 606)
            )
        assert (await runtime.state(route.session_id)).pacing.selection_due_at == cutoff
        await fire(runtime, route, saved.recovery_resume_at)
        await until(runtime, route, lambda s: s.offer and s.offer.stage == "published")
        await settle(runtime)
        resumed = await runtime.state(route.session_id)
        assert resumed.context.position.round_id == original.context.position.round_id
        assert resumed.pacing.selection_due_at == cutoff
        assert resumed.offer.hand.picker_id in {303, 404, 505}
        assert resumed.offer.choice_deadline.expires_at == saved.recovery_resume_at + timedelta(
            seconds=60
        )
        assert len(transport.recoveries) == 1
        async with runtime.store.transaction(write=False) as unit:
            assert await unit.get_score(route.session_id, 606) is None
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_partial_choice_keeps_selection_original_deadline_and_single_closure(
    tmp_path, monkeypatch, expired
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route, index, original = await partial(runtime, monkeypatch)
        round_id = original.context.position.round_id
        deadline = original.offer.choice_deadline
        card_panel = original.panel_id
        count = rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions")[0][0]
        runtime.clock.at += timedelta(seconds=40 if expired else 10)
        await runtime.initialize()
        saved = await reading(runtime, route)
        assert saved.offer.selected_incoming == original.offer.selected_incoming
        assert saved.offer.choice_deadline == deadline
        assert saved.offer_panel_id == card_panel and saved.panel_id != card_panel
        # All controls remain paused throughout read-in, including rightful ones.
        assert (
            await runtime.submit(
                route,
                "target",
                actor=original.offer.hand.picker_id,
                message_id=card_panel,
                index=index,
                target_index=0,
            )
            == "paused"
        )
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        if expired:
            resumed = await until(runtime, route, lambda s: s.offer and s.offer.stage == "selected")
            assert resumed.offer.completed_by_timeout and resumed.offer.chosen_by_picker
        else:
            assert resumed.panel_id == card_panel
            assert resumed.offer.stage == "published" and resumed.offer.choice_deadline == deadline
            assert (
                await runtime.submit(
                    route, "target", actor=999, message_id=card_panel, index=index, target_index=0
                )
                == "unauthorized"
            )
            assert (
                await runtime.submit(
                    route,
                    "target",
                    actor=original.offer.hand.picker_id,
                    message_id=saved.recovery_notice_id,
                    index=index,
                    target_index=0,
                )
                == "stale"
            )
            assert await runtime.submit(
                route, "event", event=event(runtime, route, 93000, "over", 606)
            )
            assert (
                await runtime.submit(
                    route,
                    "target",
                    actor=original.offer.hand.picker_id,
                    message_id=card_panel,
                    index=index,
                    target_index=1,
                )
                == "selected"
            )
            await settle(runtime)
            resumed = await runtime.state(route.session_id)
        assert resumed.offer.selected_incoming == original.offer.selected_incoming
        assert resumed.offer.choice_deadline == deadline
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (count,)
        ]
        runtime.clock.at = resumed.offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.context.position.round_id != round_id
        assert "NR022" in {i.card_id for i in state.context.active_instances}
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (count + 1,)
        ]
        await runtime.submit(
            route,
            "target",
            actor=original.offer.hand.picker_id,
            message_id=card_panel,
            index=index,
            target_index=1,
        )
        await settle(runtime)
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (count + 1,)
        ]


@pytest.mark.asyncio
async def test_published_three_card_hand_survives_actual_store_reopen(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        original = await offered(runtime, route)
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        saved = await reading(runtime, route)
        runtime.clock.at = original.offer.published_at + timedelta(seconds=30)
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.recovery_id is None and resumed.panel_id == original.panel_id
        assert resumed.offer.choice_deadline == original.offer.choice_deadline
        assert [o.incoming for o in resumed.offer.hand.options] == [
            o.incoming for o in original.offer.hand.options
        ]
        assert (
            await runtime.submit(
                route,
                "choose",
                actor=original.offer.hand.picker_id,
                message_id=original.panel_id,
                index=0,
            )
            == "selected"
        )


@pytest.mark.asyncio
async def test_known_announced_activation_is_shown_in_read_in_and_committed_once_afterward(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await offered(runtime, route)
        old_round = state.context.position.round_id
        await runtime.submit(
            route, "choose", actor=state.offer.hand.picker_id, message_id=state.panel_id, index=0
        )
        await settle(runtime)
        selected = await runtime.state(route.session_id)
        effective = selected.offer.effective_at
        future = resumption_rules(selected)
        assert selected.offer_display_ready
        await runtime.initialize()
        saved = await reading(runtime, route)
        embed, view = render(saved, kind="rules")
        assert "saved rule change" in embed.description
        assert [f.name for f in embed.fields] == [
            default_presentation().text(f"cards.{i.card_id}.name") for i in future
        ]
        assert not any(":pick:" in b.custom_id or ":target:" in b.custom_id for b in view.children)
        assert (
            saved.context.position.round_id == old_round
            and saved.context.active_instances != future
        )
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]
        channel = NS(guild=NS(id=101), send=AsyncMock(return_value=NS(id=30001)))
        sdk = DiscordTransport(
            NS(get_channel=lambda _: channel, settings=load_settings(environ={}))
        )
        await sdk.publish_recovery(route, saved)
        notice = channel.send.call_args.kwargs
        assert "saved rule change" in notice["embed"].description
        assert [f.name for f in notice["embed"].fields] == [f.name for f in embed.fields]
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.context.active_instances == future
        assert resumed.context.position.round_id != old_round
        assert resumed.pacing.started_at == effective
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (1,)
        ]
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (1,)
        ]


@pytest.mark.asyncio
async def test_real_sdk_restores_original_card_panel_and_turns_notice_into_a_link(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route, index, original = await partial(runtime, monkeypatch)
        await runtime.initialize()
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        edits = {}

        def message(mid):
            return edits.setdefault(mid, NS(edit=AsyncMock()))

        channel = NS(
            guild=NS(id=101), get_partial_message=message, send=AsyncMock(return_value=NS(id=30001))
        )
        sdk = DiscordTransport(
            NS(get_channel=lambda _: channel, settings=load_settings(environ={}))
        )
        await sdk.refresh(route, resumed)
        assert set(edits) == {original.panel_id, saved.recovery_notice_id}
        cards = edits[original.panel_id].edit.call_args.kwargs["view"]
        assert len(cards.children) == 2 and all(":target:" in b.custom_id for b in cards.children)
        notice = edits[saved.recovery_notice_id].edit.call_args.kwargs
        assert str(original.panel_id) in notice["embed"].description
        assert not any(
            ":target:" in b.custom_id or ":pick:" in b.custom_id for b in notice["view"].children
        )
        assert notice["allowed_mentions"].everyone is False
        channel.send.assert_not_awaited()
        await sdk.publish_recovery(route, saved)
        payload = channel.send.call_args.kwargs
        assert str(original.panel_id) in payload["embed"].description
        assert "original deadline" in payload["embed"].description


@pytest.mark.asyncio
async def test_required_choice_panel_is_checked_before_resume_and_loss_stays_paused(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        original = await offered(runtime, route)
        await runtime.initialize()
        saved = await reading(runtime, route)
        assert original.panel_id in transport.probes
        base = transport.recovery_capabilities

        async def probe(route, panel):
            if panel == original.panel_id:
                raise LaneUnavailable("missing original panel")
            return await base(route, panel)

        transport.recovery_capabilities = probe
        await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.recovery_id and state.recovery_resume_at is None
        assert state.offer.choice_deadline == original.offer.choice_deadline
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]


@pytest.mark.asyncio
async def test_second_reboot_during_read_in_keeps_partial_cards_and_original_deadline(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route, _, original = await partial(runtime, monkeypatch)
        await runtime.initialize()
        first = await reading(runtime, route)
        runtime.clock.at += timedelta(seconds=15)
        await runtime.initialize()
        second = await reading(runtime, route)
        assert second.recovery_id != first.recovery_id
        assert second.recovery_resume_at == first.recovery_resume_at + timedelta(seconds=15)
        assert second.offer.selected_incoming == original.offer.selected_incoming
        assert second.offer.choice_deadline == original.offer.choice_deadline
        assert second.offer_panel_id == original.panel_id
        await fire(runtime, route, second.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.offer.stage == "published" and resumed.panel_id == original.panel_id
        runtime.clock.at = original.offer.choice_deadline.expires_at
        await runtime.submit(route, "timeout")
        await settle(runtime)
        state = await runtime.state(route.session_id)
        assert state.offer.stage == "selected" and state.offer.completed_by_timeout
        assert len(transport.recoveries) == 2


@pytest.mark.asyncio
async def test_ordered_resume_rechecks_publication_proof_after_network_access_check(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await offered(runtime, route)
        await runtime.initialize()
        saved = await reading(runtime, route)
        base = transport.recovery_capabilities

        async def probe(route, panel):
            facts = await base(route, panel)
            async with runtime.store.transaction() as unit:
                await unit._conn.execute(
                    "UPDATE nr_discord_deliveries SET state='sending' WHERE family='ordinary'"
                )
            return facts

        transport.recovery_capabilities = probe
        await fire(runtime, route, saved.recovery_resume_at)
        blocked = await runtime.state(route.session_id)
        assert blocked.recovery_id and blocked.recovery_resume_at is None
        assert blocked.offer.choice_deadline == saved.offer.choice_deadline
        assert len(transport.recoveries) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem",
    [
        "warning_ack",
        "warning_clock",
        "offer_ack",
        "offer_message",
        "activation_unknown",
        "activation_late",
    ],
)
async def test_missing_or_conflicting_publication_proof_never_authorizes_recovery(
    tmp_path, monkeypatch, problem
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        state = await warning(runtime, route)
        if problem.startswith(("offer", "activation")):
            runtime.clock.at = state.pacing.selection_due_at
            await runtime.submit(route, "tick")
            await until(runtime, route, lambda s: s.offer and s.offer.stage == "published")
            await settle(runtime)
            state = await runtime.state(route.session_id)
        if problem.startswith("activation"):
            await runtime.submit(
                route,
                "choose",
                actor=state.offer.hand.picker_id,
                message_id=state.panel_id,
                index=0,
            )
            await settle(runtime)
        async with runtime.store.transaction() as unit:
            sql = {
                "warning_ack": "UPDATE nr_pacing_outputs SET acknowledged_at_utc=NULL WHERE kind='warning'",
                "warning_clock": "UPDATE nr_round_pacing SET selection_due_at_utc=NULL",
                "offer_ack": "UPDATE nr_discord_deliveries SET ack_sequence=0 WHERE family='ordinary'",
                "offer_message": "UPDATE nr_ordinary_outputs SET message_id=999999 WHERE kind='offer'",
                "activation_unknown": "UPDATE nr_discord_deliveries SET state='sending' WHERE family='ordinary' AND output_id IN (SELECT output_id FROM nr_ordinary_outputs WHERE kind='activation')",
                "activation_late": "UPDATE nr_activations SET announced_at_utc=effective_at_utc",
            }[problem]
            cursor = await unit._conn.execute(sql)
            assert cursor.rowcount > 0, problem
        if problem == "activation_unknown":
            assert rows(
                tmp_path / "game.sqlite3",
                "SELECT state FROM nr_discord_deliveries WHERE family='ordinary' ORDER BY output_id",
            )[-1] == ("sending",)
        await runtime.initialize()
        await settle(runtime)
        assert (await runtime.state(route.session_id)).recovery_id
        assert not transport.recoveries
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_round_resolutions") == [
            (0,)
        ]
