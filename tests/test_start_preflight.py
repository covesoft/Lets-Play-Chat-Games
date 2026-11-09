"""All predictable startup denials stay ahead of the public reaction probe."""

from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import CAPS, service
from test_live_feedback import interaction
from test_ordinary_flow import rows

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter.discord_io import DiscordTransport
from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.opening_flow import OpeningFlow, OpeningStatus


async def premium(runtime, seconds):
    async with runtime.store.transaction() as unit:
        await unit._conn.execute(
            "INSERT INTO nr_guild_entitlements VALUES (?,?) ON CONFLICT(guild_id) DO UPDATE SET premium_until_utc=excluded.premium_until_utc",
            (101, utc_int(runtime.clock.now() + timedelta(seconds=seconds))),
        )


def bot_for(tmp_path, runtime):
    bot = LetsPlayBot(
        load_settings(base_dir=tmp_path, environ={"CHATGAMES_DEVELOPMENT_MODE": "true"})
    )
    bot.game_runtime = runtime
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("tier,slots", [("free", 1), ("premium", 5), ("expired", 1)])
async def test_full_guild_capacity_declines_before_probe_without_new_reservation(
    tmp_path, monkeypatch, tier, slots
):
    async with service(tmp_path, monkeypatch) as runtime:
        if tier == "premium":
            await premium(runtime, 3600)
        if tier == "expired":
            await premium(runtime, 0)
        for n in range(slots):
            _, status = await runtime.start(101, 202 + n, 303, f"existing-{n}", CAPS)
            assert status is OpeningStatus.ADMITTED
        await settle(runtime)
        runtime.transport.probe = AsyncMock(return_value=CAPS)
        bot = bot_for(tmp_path, runtime)
        async with bot:
            click = interaction()
            click.channel_id = 999
            before = tuple(runtime.lanes)
            await bot.game_adapter.start(click)
            runtime.transport.probe.assert_not_awaited()
            assert click.followup.send.call_args.args == (bot.presentation.text("start.capacity"),)
            assert click.followup.send.call_args.kwargs["ephemeral"] is True
            assert tuple(runtime.lanes) == before
            assert rows(
                tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_admissions WHERE stage!='ended'"
            ) == [(slots,)]
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_active_premium_has_room_and_preflight_is_readonly(tmp_path, monkeypatch):
    async with service(tmp_path, monkeypatch) as runtime:
        await premium(runtime, 3600)
        await runtime.start(101, 202, 303, "existing", CAPS)
        await settle(runtime)
        async with runtime.store.transaction(write=False) as unit:
            before = unit._conn.total_changes
            assert (
                await OpeningFlow(unit).admission_gate(
                    101, 203, now=runtime.clock.now(), source_id="77"
                )
                is None
            )
            assert unit._conn.total_changes == before
        runtime.transport.probe = AsyncMock(return_value=CAPS)
        bot = bot_for(tmp_path, runtime)
        async with bot:
            click = interaction()
            click.channel_id = 203
            await bot.game_adapter.start(click)
            runtime.transport.probe.assert_awaited_once_with(click.channel)
            assert click.followup.send.call_args.args == (bot.presentation.text("start.admitted"),)
            assert rows(
                tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_admissions WHERE stage!='ended'"
            ) == [(2,)]
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_replayed_creation_request_declines_before_probe_even_after_game_ends(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route, _ = await runtime.start(101, 202, 303, "77", CAPS)
        await settle(runtime)
        await runtime.submit(route, "end", actor=303, authorized=True)
        await settle(runtime)
        runtime.transport.probe = AsyncMock(return_value=CAPS)
        bot = bot_for(tmp_path, runtime)
        async with bot:
            click = interaction()
            click.channel_id = 203
            await bot.game_adapter.start(click)
            runtime.transport.probe.assert_not_awaited()
            assert click.followup.send.call_args.args == (bot.presentation.text("start.failed"),)
            assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_admissions") == [(1,)]
            bot.game_runtime = None


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["competing_start", "premium_expiry"])
async def test_admission_rechecks_capacity_after_a_state_change_during_probe(
    tmp_path, monkeypatch, change
):
    async with service(tmp_path, monkeypatch) as runtime:
        if change == "premium_expiry":
            await premium(runtime, 3600)
            await runtime.start(101, 202, 303, "existing", CAPS)
            await settle(runtime)

        async def probe(_):
            if change == "competing_start":
                _, status = await runtime.start(101, 204, 303, "racer", CAPS)
                assert status is OpeningStatus.ADMITTED
            else:
                await premium(runtime, 0)
            return CAPS

        runtime.transport.probe = AsyncMock(side_effect=probe)
        bot = bot_for(tmp_path, runtime)
        async with bot:
            click = interaction()
            click.channel_id = 203
            await bot.game_adapter.start(click)
            runtime.transport.probe.assert_awaited_once()
            assert click.followup.send.call_args.args == (bot.presentation.text("start.capacity"),)
            assert rows(
                tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_admissions WHERE stage!='ended'"
            ) == [(1,)]
            bot.game_runtime = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denial",
    ["archived", "locked", "send_messages", "embed_links", "read_message_history", "marker"],
)
async def test_static_probe_denials_never_send_to_the_channel(denial):
    permissions = discord.Permissions.all()
    channel = NS(
        guild=NS(id=101, me=object()),
        send=AsyncMock(),
        get_partial_message=lambda _: None,
        archived=denial == "archived",
        locked=denial == "locked",
        permissions_for=lambda _: permissions,
    )
    if denial in {"send_messages", "embed_links", "read_message_history"}:
        setattr(permissions, denial, False)
    sdk = DiscordTransport(NS(settings=NS(ding_emoji=None)))
    sdk.marker = NS(id=None if denial == "marker" else 1)
    with pytest.raises(LaneUnavailable):
        await sdk.probe(channel)
    channel.send.assert_not_awaited()
