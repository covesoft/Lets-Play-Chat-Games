"""Low-cache Discord shell and development-only New Rules opening preview."""

import logging
from contextlib import AsyncExitStack
from hashlib import blake2s

import discord
from discord.ext import commands

from lets_play import __version__
from lets_play.config import Settings
from lets_play.discord_adapter.discord_io import (
    DiscordGame,
    DiscordTransport,
    message_snapshot,
    reaction_snapshot,
)
from lets_play.discord_adapter.runtime import OpeningRuntime
from lets_play.storage import initialize_database
from lets_play.storage.game_store import open_game_store

logger = logging.getLogger(__name__)


def build_intents() -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.guild_reactions = True
    intents.message_content = True
    return intents


class LetsPlayBot(commands.Bot):
    def __init__(self, settings: Settings):
        self.settings = settings
        super().__init__(
            command_prefix=commands.when_mentioned,
            help_command=None,
            intents=build_intents(),
            application_id=settings.app_id,
            owner_ids=settings.admin_ids,
            max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
            chunk_guilds_at_startup=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.game_runtime = None
        self.game_adapter = DiscordGame(self)
        self._game_resources = AsyncExitStack()

    async def setup_hook(self) -> None:
        info = await initialize_database(self.settings.database_path)
        logger.info("Loading Let's Play v%s; database schema %d", __version__, info.schema_version)
        store = await self._game_resources.enter_async_context(
            open_game_store(self.settings.database_path)
        )
        self.game_runtime = OpeningRuntime(store, DiscordTransport(self))
        if self.settings.sync_commands and self.tree.get_commands():
            await self.tree.sync()
        elif self.settings.sync_commands:
            logger.info("Command sync skipped: no registered application commands.")
        else:
            logger.info("Command sync skipped: disabled in settings.")

    async def on_ready(self) -> None:
        if self.game_runtime and not self.game_runtime.ready:
            await self.game_runtime.initialize()
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else None)
        logger.info("Connected to %d guilds", len(self.guilds))
        if self.settings.development_mode:
            for guild in self.guilds:
                logger.info("Guild %s [%s]", guild.id, guild.name)

    async def on_disconnect(self):
        if self.game_runtime and not self.game_runtime.closed:
            await self.game_runtime.disconnected()

    async def on_resumed(self):
        if self.game_runtime and not self.game_runtime.ready:
            await self.game_runtime.initialize()

    async def on_interaction(self, interaction):
        if interaction.type is discord.InteractionType.component:
            await self.game_adapter.component(interaction)

    def dispatch(self, event, /, *args, **kwargs):
        # SDK dispatch is synchronous. Capture receipts here so an HTTP defer or
        # scheduled coroutine cannot let a later timer overtake Gateway ingress.
        runtime = getattr(self, "game_runtime", None)
        if runtime and event == "disconnect":
            runtime.ready = False
            for timer in runtime.timers.values():
                timer.cancel()
        if runtime and runtime.ready and args:
            if event == "message":
                message = args[0]
                if message.guild and not (
                    message.author.bot or message.webhook_id or message.is_system()
                ):
                    route = runtime.routes.get((message.guild.id, message.channel.id))
                    if route:
                        snapshot = message_snapshot(message, route, runtime.clock.now())
                        runtime.submit(route, "event", event=snapshot).add_done_callback(
                            runtime._consume
                        )
            elif event == "raw_reaction_add":
                payload = args[0]
                route = runtime.routes.get((payload.guild_id, payload.channel_id))
                if route and payload.member and not payload.member.bot:
                    ws = self.ws
                    if ws and ws.session_id and ws.sequence is not None:
                        gateway = blake2s(ws.session_id.encode(), digest_size=12).hexdigest()
                        snapshot = reaction_snapshot(
                            payload,
                            route,
                            runtime.clock.now(),
                            source_id=f"gw:{gateway}:{ws.sequence}",
                        )
                        runtime.submit(route, "event", event=snapshot).add_done_callback(
                            runtime._consume
                        )
                    else:
                        runtime.submit(route, "pause").add_done_callback(runtime._consume)
            elif event == "interaction":
                interaction = args[0]
                captured = self.game_adapter.capture_control(interaction)
                if captured:
                    interaction.extras["new_rules_receipt"] = captured
            elif event == "guild_channel_update" or event == "thread_update":
                channel = args[-1]
                route = runtime.routes.get((channel.guild.id, channel.id))
                if route:
                    from lets_play.discord_adapter.discord_io import capabilities

                    if not capabilities(channel, channel.guild.me, marker_verified=True).available:
                        runtime.submit(route, "pause").add_done_callback(runtime._consume)
        super().dispatch(event, *args, **kwargs)

    async def on_message(self, message):
        # No message-prefix commands; receipt capture occurs at dispatch above.
        pass

    async def close(self):
        if self.game_runtime:
            await self.game_runtime.close()
        await self._game_resources.aclose()
        await super().close()


async def run_bot(settings: Settings) -> None:
    token = settings.require_token()
    async with LetsPlayBot(settings) as bot:
        await bot.start(token)
