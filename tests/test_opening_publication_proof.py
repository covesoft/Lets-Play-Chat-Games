"""Collect positive Discord creation proof and commit it with the opening clock."""

from datetime import timedelta
from types import SimpleNamespace as NS

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import CAPS, opened, service, until
from test_ordinary_flow import rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.storage.opening_flow import OpeningStatus


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["lobby", "offer"])
@pytest.mark.parametrize("outcome", ["missing", "found", "expired", "future"])
async def test_lost_opening_ack_reconciles_only_on_positive_proof(
    tmp_path, monkeypatch, kind, outcome
):
    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        if kind == "offer":
            route = await opened(runtime)
            lobby = await runtime.state(route.session_id)
            await runtime.submit(
                route, "join", actor=404, message_id=lobby.panel_id, capabilities=CAPS
            )
        transport.fail = kind
        if kind == "lobby":
            route, _ = await runtime.start(101, 202, 303, "request", CAPS)
        else:
            await runtime.submit(
                route, "join", actor=505, message_id=lobby.panel_id, capabilities=CAPS
            )
        await until(runtime, route, lambda s: s.recovery_id is not None)
        await settle(runtime)
        runtime.clock.at += timedelta(seconds=190 if outcome == "expired" else 8)
        lookups = []

        async def find(exact_route, exact_kind, attempt):
            lookups.append((exact_route, exact_kind))
            if outcome == "missing":
                return None
            return (1899, attempt + timedelta(seconds=20 if outcome == "future" else 1))

        transport.find_opening_publication = find
        await runtime.initialize()
        await settle(runtime)
        assert lookups == [(route, kind)]
        assert sum(k == kind for _, k, _ in transport.messages) == 1
        if outcome in {"missing", "future"}:
            assert (await runtime.state(route.session_id)).recovery_id
            assert not transport.recoveries
            return
        if outcome == "expired" and kind == "lobby":
            assert (await runtime.state(route.session_id)).admission.stage == "ended"
            assert not transport.recoveries
            assert transport.messages[-1][1] == "closed"
            return
        saved = await reading(runtime, route)
        assert saved.phase_control_id == 1899
        assert saved.context is None
        assert saved.next_deadline == runtime.clock.at - timedelta(
            seconds=189 if outcome == "expired" else 7
        ) + timedelta(seconds=180 if kind == "lobby" else 60)
        await fire(runtime, route, saved.recovery_resume_at)
        state = await runtime.state(route.session_id)
        if outcome == "expired":
            assert state.admission.stage == "selected"
            assert state.selected_card
            return
        assert state.panel_id == 1899 and state.recovery_id is None
        if kind == "lobby":
            assert (
                await runtime.submit(route, "join", actor=404, message_id=1899, capabilities=CAPS)
                == OpeningStatus.JOINED
            )
        else:
            assert (
                await runtime.submit(
                    route, "choose", actor=state.admission.picker_id, message_id=1899, index=0
                )
                == OpeningStatus.SELECTED
            )
        assert rows(
            tmp_path / "game.sqlite3",
            "SELECT state,message_id,ack_sequence>0 FROM nr_discord_deliveries WHERE message_id=1899",
        ) == [("delivered", 1899, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem", [None, "duplicate", "full", "author", "edit", "route", "old", "webhook", "control"]
)
async def test_discord_history_requires_one_complete_original_match(problem):
    from datetime import UTC, datetime

    attempt = datetime(2026, 1, 1, tzinfo=UTC)
    route = NS(session_id="a" * 32, channel_id=202, guild_id=101)
    custom_id = f"nr:{route.session_id}:join" if problem != "control" else "nr:other:join"
    button = discord.Button({"type": 2, "style": 2, "custom_id": custom_id})
    message = NS(
        id=9000,
        author=NS(id=18 if problem == "author" else 17),
        webhook_id=24 if problem == "webhook" else None,
        edited_at=attempt if problem == "edit" else None,
        created_at=attempt + timedelta(seconds=-1 if problem == "old" else 1),
        channel=NS(id=204 if problem == "route" else route.channel_id),
        components=[NS(children=[button])],
    )
    unrelated = NS(author=NS(id=18))

    async def history(**kw):
        assert kw == dict(limit=50, after=attempt, oldest_first=True)
        yield message
        if problem == "duplicate":
            yield message
        if problem == "full":
            for _ in range(49):
                yield unrelated

    sdk = object.__new__(DiscordTransport)
    channel = NS(guild=NS(id=101), history=history)
    sdk.bot = NS(user=NS(id=17), get_channel=lambda _: channel)
    proof = await sdk.find_opening_publication(route, "lobby", attempt)
    assert proof == ((9000, message.created_at) if problem is None else None)
