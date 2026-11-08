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
from lets_play.presentation import PresentationError, load_presentation

logger = logging.getLogger(__name__)
# Operator-facing CLI/log presentation is kept together for easy owner edits.
CLI_DESCRIPTION = "Let's Play — New Rules (development scaffold)"
CLI_VERSION = "Let's Play is on version {version}!"
LOG_FORMAT = "%(levelname)s:%(name)s:%(message)s"
LOG_SEPARATOR = "=" * 55
LOG_LAUNCH = "Launching Let's Play Chat Games Discord bot (v%s)"
LOG_STOP = "-" * 10 + "Bot stopped by intervention." + "-" * 10


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
    args = parser.parse_args(argv)
    try:
        settings = load_settings(base_dir=base_dir)
        if args.check_config:
            load_presentation(settings.presentation_path)
            print("Configuration valid. No Discord connection or database write performed.")
            print(
                "Live token: configured."
                if settings.token
                else "Live token: missing (offline checks work)."
            )
            print(
                "Invite: available."
                if settings.invite_url
                else "Invite: omitted until CHATGAMES_APP_ID is set."
            )
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
    except (ConfigError, PresentationError) as exc:
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
