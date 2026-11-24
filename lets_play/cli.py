"""Safe offline checks and live startup share one explicit configuration path."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from lets_play import __version__
from lets_play.config import ConfigError, load_settings
from lets_play.new_rules.phrase_bank import PhraseBankError, load_guild_phrases
from lets_play.new_rules.profanity import load_profanity_list
from lets_play.presentation import PresentationError, load_presentation

logger = logging.getLogger(__name__)
# Operator-facing CLI/log presentation is kept together for easy owner edits.
CLI_DESCRIPTION = "Let's Play — New Rules (development scaffold)"
CLI_VERSION = "Let's Play is on version {version}!"
LOG_FORMAT = "%(levelname)s:%(name)s:%(message)s"
LOG_SEPARATOR = "=" * 55
LOG_LAUNCH = "Launching Let's Play Chat Games Discord bot (v%s)"
LOG_STOP = "-" * 11 + " Bot has stopped by intervention. " + "-" * 11


async def _import_guild_phrases(path: Path, guild_id: int, phrases) -> None:
    from lets_play.storage.game_store import open_game_store
    from lets_play.storage.guild_phrases import GuildPhraseBank

    async with open_game_store(path) as store, store.transaction() as unit:
        await GuildPhraseBank(unit).replace(guild_id, phrases)


def main(argv: list[str] | None = None, *, base_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=CLI_DESCRIPTION)
    parser.add_argument(
        "--version", action="version", version=CLI_VERSION.format(version=__version__)
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check-config", action="store_true", help="Validate without a login or DB write"
    )
    mode.add_argument(
        "--init-db", action="store_true", help="Initialize the local database without Discord"
    )
    mode.add_argument(
        "--import-guild-phrases",
        metavar="GUILD_ID",
        type=int,
        help="Replace the selected guild's Don't Forget bank from the configured file",
    )
    args = parser.parse_args(argv)
    try:
        settings = load_settings(base_dir=base_dir)
        if args.check_config:
            load_presentation(settings.presentation_path)
            print(f"Let's Play v{__version__} - Configuration valid.")
            print("No Discord connection or database write performed.")
            print(
                "Discord token: configured."
                if settings.token
                else "Discord token: missing (offline checks work)."
            )
            print(
                "Bot Invite: available."
                if settings.invite_url
                else "Bot Invite: omitted until CHATGAMES_APP_ID is set."
            )
            print(
                "Support server: configured."
                if settings.support_url
                else "Support server: no invite configured."
            )
            if settings.profanity_list_path is None:
                print("Mind Your Language: disabled (no private gameplay list configured).")
            elif (gameplay_list := load_profanity_list(settings.profanity_list_path)) is None:
                print("Mind Your Language: unavailable. Check CHATGAMES_PROFANITY_LIST_PATH.")
            else:
                print(
                    "Mind Your Language: private gameplay list loaded "
                    f"(unique entries: {gameplay_list.entry_count:,})."
                )
            try:
                phrases = load_guild_phrases(
                    settings.guild_phrases_file_path, settings.phrase_suitability_list_path
                )
            except PhraseBankError as exc:
                print(f"Don't Forget defaults unavailable. {exc}")
            else:
                if phrases is not None:
                    print(f"Don't Forget defaults loaded (phrases: {len(phrases)}).")
            return 0
        if args.import_guild_phrases is not None:
            guild_id = args.import_guild_phrases
            if not 0 < guild_id <= (1 << 63) - 1:
                raise ConfigError("Enter a positive SQLite-compatible guild ID.")
            phrases = load_guild_phrases(
                settings.guild_phrases_file_path, settings.phrase_suitability_list_path
            )
            if phrases is None:
                raise ConfigError("Set CHATGAMES_GUILD_PHRASES_FILE_PATH before importing.")
            asyncio.run(_import_guild_phrases(settings.database_path, guild_id, phrases))
            print(f"Don't Forget guild bank saved (phrases: {len(phrases)}).")
            return 0
        if args.init_db:
            from lets_play.storage import initialize_database

            info = asyncio.run(initialize_database(settings.database_path))
            print(
                f"Database ready: schema={info.schema_version}, journal={info.journal_mode}, foreign_keys=on."
            )
            return 0

        # Run the discord bot
        settings.require_token()
        from lets_play.bot import run_bot

        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
        logger.info("%s", LOG_SEPARATOR)
        logger.info(LOG_LAUNCH, __version__)

        asyncio.run(run_bot(settings))
        return 0
    except (ConfigError, PhraseBankError, PresentationError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(
            f"Startup failed ({type(exc).__name__}); check database access and storage.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        logger.info(LOG_STOP)
        return 0
