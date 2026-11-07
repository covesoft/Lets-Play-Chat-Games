"""Discord shell only; game events and commands are a later milestone."""

import logging

import discord
from discord.ext import commands

from lets_play import __version__
from lets_play.config import Settings
from lets_play.storage import initialize_database

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

    async def setup_hook(self) -> None:
        info = await initialize_database(self.settings.database_path)
        logger.info("Loading Let's Play v%s; database schema %d", __version__, info.schema_version)
        if self.settings.sync_commands and self.tree.get_commands():
            await self.tree.sync()
        elif self.settings.sync_commands:
            logger.info("Command sync skipped: the M0 scaffold has no registered commands.")

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else None)
        logger.info("Connected to %d guilds", len(self.guilds))
        if self.settings.development_mode:
            for guild in self.guilds:
                logger.info("Guild %s [%s]", guild.id, guild.name)


async def run_bot(settings: Settings) -> None:
    token = settings.require_token()
    async with LetsPlayBot(settings) as bot:
        await bot.start(token)
