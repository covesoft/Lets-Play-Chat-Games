"""Discord playtest follow-up through the ordered runtime and SDK presentation."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import activated, event, opened, service, until
from test_drawing_conclusions import scheduled
from test_live_feedback import interaction
from test_ordinary_flow import execute, rows
from test_public_summary import PublicTransport
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import requested_summary
from lets_play.discord_adapter.discord_io import DiscordTransport, render
from lets_play.presentation import default_presentation
from lets_play.storage.summary_store import SummaryStore


async def warning(runtime, route):
    await posts(runtime, route)
    opened = await runtime.state(route.session_id)
    runtime.clock.at = opened.pacing.started_at + timedelta(seconds=270)
    await runtime.submit(route, "tick")
    await settle(runtime)
    return await until(runtime, route, lambda state: state.pacing.stage == "warning_published")


async def select(runtime, route, warned):
    runtime.clock.at = warned.pacing.selection_due_at
    await runtime.submit(route, "tick")
    await settle(runtime)
    return await runtime.state(route.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [7, 8])
async def test_warning_countdown_and_threshold_include_bot_posts_and_whispers(
    tmp_path, monkeypatch, count
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        warned = await warning(runtime, route)
        copy = default_presentation()
        timestamp = int(warned.pacing.selection_due_at.timestamp())
        public, _ = render(warned, kind="pacing_warning")
        private, _ = render(warned, kind="rules")
        assert not public.fields
        assert public.description == copy.text("warning.countdown", timestamp=timestamp)
        assert [f.name for f in private.fields] == [copy.text("cards.NR034.name")]

        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            for i in range(1, count + 1):
                source = 100_000 + i
                if i % 2:
                    bot.dispatch(
                        "message",
                        NS(
                            id=source,
                            guild=NS(id=route.guild_id),
                            channel=NS(id=route.channel_id),
                            author=NS(bot=True),
                            webhook_id=None,
                            is_system=lambda: False,
                        ),
                    )
                else:
                    await runtime.submit(
                        route, "event", event=event(runtime, route, source, "-# quiet")
                    )
                # Replaying an observed ID cannot move seven to eight.
                await runtime.submit(route, "channel_post", source=str(source))
            bot.game_runtime = None
        await runtime.submit(route, "channel_post", source=str(warned.panel_id))
        offer = await select(runtime, route, warned)
        assert offer.offer.stage == "published"
        target = rows(
            tmp_path / "game.sqlite3",
            """SELECT t.target_message_id FROM nr_discord_publication_targets t
               JOIN nr_ordinary_outputs o USING(output_id)
               WHERE t.family='ordinary' AND o.kind='offer'""",
        )
        assert target == [(warned.panel_id if count == 7 else None,)]
        async with runtime.store.transaction(write=False) as unit:
            traffic = await unit.get_traffic(route.session_id)
        assert traffic.whispers == count // 2
        assert traffic.human_messages == 15 + count // 2


@pytest.mark.asyncio
async def test_lost_busy_choice_send_recovers_saved_new_post_without_edit(tmp_path, monkeypatch):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        warned = await warning(runtime, route)
        for i in range(8):
            await runtime.submit(route, "channel_post", source=str(80000 + i))
        transport.fail = "ordinary_offer"
        blocked = await select(runtime, route, warned)
        assert blocked.recovery_id and blocked.offer.stage == "prepared"
        assert [
            state.panel_id for _, kind, state in transport.messages if kind == "ordinary_offer"
        ] == [None]
        runtime.clock.at += timedelta(seconds=5)
        transport.find_choice_edit = AsyncMock(return_value=None)
        transport.find_choice_post = AsyncMock(
            return_value=(1777, warned.pacing.selection_due_at + timedelta(seconds=1))
        )
        await runtime.initialize()
        read_in = await reading(runtime, route)
        transport.find_choice_post.assert_awaited_once()
        transport.find_choice_edit.assert_not_awaited()
        assert read_in.offer_panel_id == 1777
        assert read_in.offer.choice_deadline == read_in.offer.hand.deadline
        assert read_in.offer.hand.deadline.expires_at == warned.pacing.selection_due_at + timedelta(
            seconds=61
        )
        await fire(runtime, route, read_in.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.panel_id == 1777
        assert sum(k == "ordinary_offer" for _, k, _ in transport.messages) == 1


@pytest.mark.asyncio
async def test_whole_game_post_freezes_the_chosen_page_and_keeps_private_scope(
    tmp_path, monkeypatch
):
    transport = PublicTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route, actors=(310, 311, 312, 313, 314, 315, 316), count=5)
        offer = await next_offer(runtime, route)
        await runtime.submit(
            route, "choose", actor=offer.offer.hand.picker_id, message_id=offer.panel_id, index=0
        )
        await settle(runtime)
        runtime.clock.at = (await runtime.state(route.session_id)).offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        runtime.clock.at += timedelta(seconds=30)
        await runtime.submit(route, "event", event=event(runtime, route, 92000, "over", 317))
        copy = default_presentation()
        async with runtime.store.transaction(write=False) as unit:
            page = await SummaryStore(unit).live_page(route.session_id, scope="game", actor=314)
        private, view = requested_summary.render(page, copy)
        assert private.title == copy.text("summary.game_title")
        assert private.fields[0].name == copy.text("summary.game_traffic_heading")
        assert private.fields[0].value == copy.text(
            "summary.traffic", messages=page.human_messages, whispers=page.whispers
        )
        assert private.fields[1].name == copy.text("results.player_heading")
        post = next(
            button.custom_id for button in view.children if button.custom_id.startswith("nrp:")
        )
        assert post.endswith(":game:314:n")

        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            click = interaction(message_id=999)
            click.message.flags = NS(ephemeral=True)
            click.edit_original_response = AsyncMock()
            click.data = {"custom_id": post}
            await bot.game_adapter.component(click)
            await settle(runtime)
            reply = click.edit_original_response.call_args.kwargs
            assert reply["content"] == copy.text("summary.post_queued")
            assert reply["embed"].title == private.title
            assert reply["embed"].description == private.description
            assert reply["embed"].to_dict()["fields"] == private.to_dict()["fields"]
            bot.game_runtime = None

        _, snapshot, captured = transport.summaries[0]
        assert captured.scope == "game"
        assert captured.human_messages == 36 and snapshot.round_messages == 1
        assert captured.players == page.players
        assert [p[0] for p in captured.players] == [315, 316, 317, 404, 505]
        public, _ = requested_summary.render(captured, copy, snapshot=snapshot, public=True)
        assert public.title == private.title
        assert public.footer.text is None and public.timestamp is None
        assert public.description == copy.text(
            "summary.lead",
            as_of=copy.text("summary.as_of", timestamp=int(captured.as_of.timestamp())),
            status=copy.text("summary.captured"),
        )
        assert public.to_dict()["fields"] == private.to_dict()["fields"]


@pytest.mark.asyncio
async def test_breaking_moment_expiry_posts_one_separate_lime_green_notice(tmp_path, monkeypatch):
    transport = RecoveryTransport()
    async with service(
        tmp_path, monkeypatch, transport=transport, include_breaking_moment=True
    ) as runtime:
        route, before = await scheduled(runtime, monkeypatch)
        runtime.clock.at = before.offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        active = await runtime.state(route.session_id)
        copy = default_presentation()
        runtime.clock.at = active.mode.ends_at
        await runtime.submit(route, "tick")
        await settle(runtime)

        notices = [state for _, kind, state in transport.messages if kind == "evolution_mode_ended"]
        assert len(notices) == 1
        embed, _ = render(notices[0], kind="evolution_mode_ended")
        assert embed.colour == discord.Colour(copy.color("mode_ended"))
        assert embed.description == copy.text("mode.ended_description")
        assert [field.name for field in embed.fields] == [copy.text("cards.NR034.name")]
        assert notices[0].mode_end_rules == active.context.active_instances

        sent = NS(id=1780)
        channel = NS(guild=NS(id=route.guild_id), send=AsyncMock(return_value=sent))
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), user=NS(id=55), get_channel=lambda _: channel)
        )
        assert (
            await sdk.publish(route, "evolution_mode_ended", notices[0], publication_id=2)
            == sent.id
        )
        payload = channel.send.await_args.kwargs
        assert "footer" not in payload["embed"].to_dict()
        (marker,) = [item for item in payload["view"].children if item.disabled]
        assert marker.custom_id == sdk._mode_end_control(route, 2)
        attempted = runtime.clock.now()
        message = NS(
            id=sent.id,
            author=NS(id=55),
            channel=NS(id=route.channel_id),
            webhook_id=None,
            created_at=attempted + timedelta(seconds=1),
            edited_at=None,
            components=[
                NS(
                    children=[
                        discord.Button(item.to_component_dict())
                        for item in payload["view"].children
                    ]
                )
            ],
            embeds=[payload["embed"]],
        )

        async def history(**kwargs):
            yield message

        channel.history = history
        assert await sdk.find_mode_end_publication(route, 2, attempted) == (
            sent.id,
            message.created_at,
        )
        assert await sdk.find_mode_end_publication(route, 3, attempted) is None
        # The same lookup can settle a pre-upgrade send with a lost ACK.
        message.components = []
        message.embeds = [
            discord.Embed().set_footer(
                text=sdk._publication_reference(route, "evolution", "mode_ended", 2)
            )
        ]
        assert await sdk.find_mode_end_publication(route, 2, attempted) == (
            sent.id,
            message.created_at,
        )
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert sum(kind == "evolution_mode_ended" for _, kind, _ in transport.messages) == 1
        restored = await runtime.state(route.session_id)
        assert restored.round_number == 2 and restored.mode is None
        assert restored.context.active_instances == active.context.active_instances


@pytest.mark.asyncio
async def test_reboot_restores_rules_and_retires_a_legacy_unsent_interruption_notice(
    tmp_path, monkeypatch
):
    transport = RecoveryTransport()
    db = tmp_path / "game.sqlite3"
    async with service(
        tmp_path, monkeypatch, transport=transport, include_breaking_moment=True
    ) as runtime:
        route, before = await scheduled(runtime, monkeypatch)
        runtime.clock.at = before.offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        active = await runtime.state(route.session_id)
        await runtime.initialize()
        state = await reading(runtime, route)
        copy = default_presentation()
        panel = NS(edit=AsyncMock())
        channel = NS(guild=NS(id=route.guild_id), get_partial_message=lambda _: panel)
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), get_channel=lambda _: channel)
        )
        embed, _ = sdk._recovery_notice(route, state)
        assert embed.description == copy.text(
            "mode.interrupted_countdown", timestamp=int(state.recovery_resume_at.timestamp())
        )
        await fire(runtime, route, state.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.round_number == active.round_number and state.mode is None
        assert state.context.active_instances == active.context.active_instances
        await sdk.refresh(route, state)
        embed = panel.edit.await_args.kwargs["embed"]
        assert embed.colour == discord.Colour(copy.color("active"))
        assert [field.name for field in embed.fields] == [copy.text("cards.NR034.name")]
        assert rows(db, "SELECT output_id FROM nr_evolution_outputs WHERE kind='mode_ended'") == []
        assert not any(kind == "evolution_mode_ended" for _, kind, _ in transport.messages)

        execute(
            db,
            "INSERT INTO nr_evolution_outputs(session_id,token,kind) "
            "SELECT session_id,round_id,'mode_ended' FROM nr_breaking_moments",
        )
        await runtime.initialize()
        state = await reading(runtime, route)
        await fire(runtime, route, state.recovery_resume_at)
        assert rows(
            db,
            "SELECT retired_at_utc IS NOT NULL,message_id FROM nr_evolution_outputs "
            "WHERE kind='mode_ended'",
        ) == [(1, None)]
        assert not any(kind == "evolution_mode_ended" for _, kind, _ in transport.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_interruption", [False, True])
async def test_lost_mode_end_send_is_recovered_as_one_known_post(
    tmp_path, monkeypatch, legacy_interruption
):
    transport = RecoveryTransport()
    async with service(
        tmp_path, monkeypatch, transport=transport, include_breaking_moment=True
    ) as runtime:
        route, before = await scheduled(runtime, monkeypatch)
        runtime.clock.at = before.offer.effective_at
        await runtime.submit(route, "tick")
        await settle(runtime)
        active = await runtime.state(route.session_id)
        runtime.clock.at = active.mode.ends_at
        transport.fail = "evolution_mode_ended"
        await runtime.submit(route, "tick")
        await settle(runtime)
        assert (await runtime.state(route.session_id)).recovery_id
        if legacy_interruption:
            execute(
                tmp_path / "game.sqlite3",
                "UPDATE nr_breaking_moments SET outcome='interrupted' WHERE outcome='completed'",
            )
        runtime.clock.at += timedelta(seconds=5)
        transport.find_mode_end_publication = AsyncMock(
            return_value=(1778, active.mode.ends_at + timedelta(seconds=1))
        )
        await runtime.initialize()
        read_in = await reading(runtime, route)
        await fire(runtime, route, read_in.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.mode is None and state.round_number == 2
        assert state.context.position.lifecycle == "playing"
        transport.find_mode_end_publication.assert_awaited_once()
        assert sum(k == "evolution_mode_ended" for _, k, _ in transport.messages) == 1
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT message_id FROM nr_evolution_outputs WHERE kind='mode_ended'",
        ) == [(1778,)]


@pytest.mark.asyncio
async def test_schema_16_preserves_evolution_output_identity_and_allocator(tmp_path, monkeypatch):
    from test_discord_delivery_migration import released_nine

    from lets_play.storage import bootstrap

    db = tmp_path / "legacy.sqlite3"
    session = await released_nine(db, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", bootstrap.MIGRATIONS[:15])
        assert (await bootstrap.initialize_database(db)).schema_version == 15
    execute(
        db,
        f"INSERT INTO nr_evolution_outputs(output_id,session_id,token,kind) VALUES(70,'{session}','held','held')",
    )
    execute(
        db,
        f"INSERT INTO nr_evolution_outputs(output_id,session_id,token,kind) VALUES(99,'{session}','old','held')",
    )
    execute(db, "DELETE FROM nr_evolution_outputs WHERE output_id=99")
    execute(
        db,
        f"INSERT INTO nr_discord_deliveries(session_id,family,output_id,state,attempted_at_utc) VALUES('{session}','evolution',70,'sending',123)",
    )
    output_query = "SELECT * FROM nr_evolution_outputs ORDER BY output_id"
    claim_query = "SELECT * FROM nr_discord_deliveries WHERE family='evolution'"
    saved_outputs, saved_claims = rows(db, output_query), rows(db, claim_query)
    assert (await bootstrap.initialize_database(db)).schema_version == bootstrap.SCHEMA_VERSION
    assert rows(db, output_query) == saved_outputs
    assert rows(db, claim_query) == saved_claims
    execute(
        db,
        f"INSERT INTO nr_evolution_outputs(session_id,token,kind) VALUES('{session}','new','mode_ended')",
    )
    assert rows(db, "SELECT output_id FROM nr_evolution_outputs WHERE kind='mode_ended'") == [
        (100,)
    ]
    assert rows(db, "PRAGMA foreign_key_check") == []
