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


def main(argv: list[str] | None = None, *, base_dir: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Let's Play — New Rules (M0 foundation)")
    parser.add_argument("--version", action="version", version=f"Let's Play {__version__}")
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
            print("Configuration valid. No Discord connection or database write performed.")
            print(
                "Live token: configured."
                if settings.token
                else "Live token: missing (offline checks work)."
            )
            print(
                "Invite: available."
                if settings.invite_url
                else "Invite: omitted until CHATRULES_APP_ID is set."
            )
            return 0
        if args.init_db:
            from lets_play.storage import initialize_database

            info = asyncio.run(initialize_database(settings.database_path))
            print(
                f"Database ready: schema={info.schema_version}, journal={info.journal_mode}, foreign_keys=on."
            )
            return 0
        settings.require_token()
        from lets_play.bot import run_bot

        logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
        asyncio.run(run_bot(settings))
        return 0
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error) as exc:
        print(
            f"Startup failed ({type(exc).__name__}); check database access and storage.",
            file=sys.stderr,
        )
        return 1
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("Bot stopped by intervention.")
        return 0
