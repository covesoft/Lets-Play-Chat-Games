"""Compatibility names for the original starter; new code accepts Settings.

Importing this shim needs no token, opens no database, and makes no API calls.
"""

from pathlib import Path

from lets_play import __version__
from lets_play.config import BOT_PERMISSIONS_INT, load_settings

BASE_DIR = Path(__file__).resolve().parent
settings = load_settings(base_dir=BASE_DIR)
CHATRULES_VERSION = f"v{__version__}"
CHATRULES_DISCORD_TOKEN = settings.token
ADMIN_IDS = sorted(settings.admin_ids)
APP_ID = settings.app_id
CHATRULES_LINK_OTHER_BOTS = settings.other_bots_url
CHATRULES_LINK_THIS_BOT = settings.this_bot_url
CHATRULES_LINK_GITHUB = settings.repository_url
CHATRULES_LINK_DONATE = settings.donate_url
CHATRULES_LINK_VOTE = settings.vote_url
SUPPORT_SERVER_INVITE = settings.support_url
CHATRULES_LINK_INVITE = settings.invite_url

__all__ = [
    "ADMIN_IDS",
    "APP_ID",
    "BASE_DIR",
    "BOT_PERMISSIONS_INT",
    "CHATRULES_DISCORD_TOKEN",
    "CHATRULES_LINK_DONATE",
    "CHATRULES_LINK_GITHUB",
    "CHATRULES_LINK_INVITE",
    "CHATRULES_LINK_OTHER_BOTS",
    "CHATRULES_LINK_THIS_BOT",
    "CHATRULES_LINK_VOTE",
    "CHATRULES_VERSION",
    "SUPPORT_SERVER_INVITE",
    "settings",
]
