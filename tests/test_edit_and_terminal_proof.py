"""Recover the exact edit target and terminal delivery after a lost ACK."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import posts, settle
from test_discord_runtime import CAPS, activated, opened, service, until
from test_ordinary_flow import rows
from test_reboot_lifecycle import revival
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.discord_store import PendingOutput
from lets_play.storage.offer_codec import utc_int


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["valid", "wrong_target", "missing"])
async def test_choice_edit_proof_uses_claimed_target_and_original_clock(
    tmp_path, monkeypatch, outcome
):
    transport = RecoveryTransport()
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await posts(runtime, route)
        runtime.clock.at += timedelta(minutes=5)
        await runtime.submit(route, "tick")
        warned = await until(runtime, route, lambda s: s.pacing.stage == "warning_published")
        await settle(runtime)
        transport.fail = "ordinary_offer"
        runtime.clock.at = warned.pacing.selection_due_at
        await runtime.submit(route, "tick")
        await until(runtime, route, lambda s: s.recovery_id is not None)
        await settle(runtime)
        assert transport.pauses == []
        runtime.clock.at += timedelta(seconds=5)
        calls = []

        async def find(exact_route, output_id, target, attempted):
            calls.append((exact_route, output_id, target))
            if outcome == "missing":
                return None
            return target + (outcome == "wrong_target"), attempted + timedelta(seconds=1)

        transport.find_choice_edit = find
        await runtime.initialize()
        await settle(runtime)
        assert len(calls) == 1 and calls[0][0] == route and calls[0][2] == warned.panel_id
        assert sum(k == "ordinary_offer" for _, k, _ in transport.messages) == 1
        if outcome != "valid":
            assert (await runtime.state(route.session_id)).offer.stage == "prepared"
            assert not transport.recoveries
            return
        saved = await reading(runtime, route)
        assert saved.offer.published_at == warned.pacing.selection_due_at + timedelta(seconds=1)
        assert saved.offer.hand.deadline.expires_at == saved.offer.published_at + timedelta(
            seconds=60
        )
        assert saved.offer_panel_id == warned.panel_id
        await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        assert state.recovery_id is None and state.panel_id == warned.panel_id
        assert rows(db, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["played", "preplay", "silent"])
@pytest.mark.parametrize("found", [False, True])
async def test_terminal_delivery_reconciles_without_reopening(tmp_path, monkeypatch, kind, found):
    transport = RecoveryTransport()
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        if kind == "played":
            await activated(runtime, route)
        elif kind == "silent":
            await revival(runtime, route)
        await settle(runtime)
        original = await runtime.state(route.session_id)
        key = "evolution_closed_silent" if kind == "silent" else "closed"
        transport.fail = key
        if kind == "silent":
            runtime.clock.at = original.revival.deadline
            await runtime.submit(route, "tick")
        else:
            await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        assert (await runtime.state(route.session_id)).admission.stage == "ended"
        runtime.clock.at += timedelta(seconds=4)
        calls = []
        published = None

        async def find(exact_route, output, target, attempted):
            nonlocal published
            calls.append((exact_route, output.ui_kind, target))
            published = attempted + timedelta(seconds=1)
            return ((target or 1776), published) if found else None

        transport.find_terminal_publication = find
        if kind == "played":
            replacement, status = await runtime.start(101, 202, 303, "new-lobby", CAPS)
            assert status == "admitted"
            await settle(runtime)
        await runtime.initialize()
        await settle(runtime)
        target = None if kind == "played" else original.panel_id
        assert calls == [(route, key, target)]
        assert sum(k == key for _, k, _ in transport.messages) == 1
        if kind == "played":
            assert runtime.routes[101, 202] == replacement
            assert len(transport.recoveries) == 1
        else:
            assert (101, 202) not in runtime.routes
            assert not transport.recoveries
        assert (await runtime.state(route.session_id)).admission.stage == "ended"
        table = "nr_evolution_outputs" if kind == "silent" else "nr_opening_outputs"
        out_kind = "closed_silent" if kind == "silent" else "closed"
        assert rows(
            db, f"SELECT message_id,acknowledged_at_utc FROM {table} WHERE kind='{out_kind}'"
        ) == [((target or 1776), utc_int(published)) if found else (None, None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["valid", "no_edit_time", "old_edit", "wrong_marker"])
async def test_sdk_choice_edit_proof_requires_matching_edit_timestamp(
    tmp_path, monkeypatch, condition
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        now = runtime.clock.at
        send = AsyncMock()
        target = 1777
        channel = NS(guild=NS(id=route.guild_id), send=send, fetch_message=AsyncMock())
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), user=NS(id=17), get_channel=lambda _: channel)
        )
        embed = NS(footer=NS(text=sdk._publication_reference(route, "ordinary", "offer", 8)))
        channel.fetch_message.return_value = NS(
            id=target,
            author=NS(id=17),
            webhook_id=None,
            channel=NS(id=route.channel_id),
            created_at=now - timedelta(minutes=1),
            edited_at=None
            if condition == "no_edit_time"
            else now + timedelta(seconds=-1 if condition == "old_edit" else 1),
            embeds=[embed],
        )
        proof = await sdk.find_choice_edit(
            route, 9 if condition == "wrong_marker" else 8, target, now
        )
        assert proof == ((target, now + timedelta(seconds=1)) if condition == "valid" else None)
        channel.fetch_message.assert_awaited_once_with(target)
        send.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_terminal_send_after_restart_uses_existing_saved_end(tmp_path, monkeypatch):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        runtime.ready = False
        await runtime.submit(route, "end", actor=303, authorized=True)
        assert (await runtime.state(route.session_id)).admission.stage == "ended"
        assert not any(kind == "closed" for _, kind, _ in transport.messages)
        await runtime.initialize()
        await settle(runtime)
        assert sum(kind == "closed" for _, kind, _ in transport.messages) == 1
        assert (101, 202) not in runtime.routes
        assert not transport.recoveries


@pytest.mark.asyncio
async def test_sdk_terminal_proof_uses_new_post_or_exact_edit(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await settle(runtime)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        state = await runtime.state(route.session_id)
        now = runtime.clock.at
        send = AsyncMock(return_value=NS(id=1777))
        channel = NS(guild=NS(id=101), send=send, fetch_message=AsyncMock())
        sdk = DiscordTransport(
            NS(settings=load_settings(environ={}), user=NS(id=17), get_channel=lambda _: channel)
        )
        output = PendingOutput("opening", 8, "closed", "closed")
        await sdk.publish_identified(route, output, state)
        payload = send.call_args.kwargs
        components = [
            NS(
                children=[
                    discord.Button(item.to_component_dict()) for item in payload["view"].children
                ]
            )
        ]
        created = now + timedelta(seconds=1)
        edited = now + timedelta(seconds=2)
        base = dict(
            id=1777,
            author=NS(id=17),
            webhook_id=None,
            channel=NS(id=route.channel_id),
            components=components,
            embeds=[payload["embed"]],
        )

        async def history(**kwargs):
            assert kwargs == {"limit": 50, "after": now, "oldest_first": True}
            yield NS(**base, created_at=created, edited_at=None)

        channel.history = history
        assert await sdk.find_terminal_publication(route, output, None, now) == (1777, created)
        channel.fetch_message.return_value = NS(
            **base,
            created_at=now - timedelta(minutes=1),
            edited_at=edited,
        )
        assert await sdk.find_terminal_publication(route, output, 1777, now) == (1777, edited)
        assert await sdk.find_terminal_publication(route, output, 1778, now) is None
