# New Chat Rules discord bot conversation game

import asyncio
from datetime import datetime
import logging

import discord
from discord.ext import commands

from config import (
    CHATRULES_VERSION,
    CHATRULES_DISCORD_TOKEN as DISCORD_TOKEN,
)

logging.basicConfig(level=logging.INFO)

DEVELOPMENT_MODE = True

class ChatRulesBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
        )

        #self.db = Database(db_path=DATABASE_PATH)

    async def setup_hook(self):
        #await self.db.init()

        logging.info("Loading Chat Rules %s discord bot game", CHATRULES_VERSION)
        # Load Cogs before registering slash commands with Discord.
        #await self.load_extension("cogs.main_game")
        await self.tree.sync()

    async def on_ready(self):
        logging.info("Logged in as %s (%s)", self.user, self.user.id)
        logging.info("Connected to %d guilds", len(self.guilds))
        if DEVELOPMENT_MODE:
            for guild in self.guilds:
                logging.info(" - %s [%s] members: %d, role: %s%s",
                            guild.id, guild.name, guild.member_count,
                            "owner" if guild.owner_id == self.user.id else "",
                            "admin" if guild.owner_id != self.user.id and guild.me.guild_permissions.administrator else "")
        logging.info("=" * 50)

    async def close(self):
        await super().close()


async def main():
    logging.info("=" * 50)

    bot = ChatRulesBot()

    async with bot:
        await bot.start(str(DISCORD_TOKEN))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by intervention.")
    except asyncio.CancelledError:
        logging.info("Bot stopped by cancellation.")
    finally:
        logging.info("-"*40 + "END" + "-"*7)
### EOF ###