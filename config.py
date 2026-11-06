# New Chat Rules discord bot conversation game
# Configuration
# file: config.py
# Started 2026-11-05

import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

load_dotenv()

CHATRULES_VERSION = 'v0.1.0'


# ADMINISTRATIVE SETTINGS
CHATRULES_DISCORD_TOKEN = os.getenv("CHATRULES_DISCORD_TOKEN")

BASE_DIR = Path(__file__).resolve().parent

ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()]

APP_ID = os.getenv("CHATRULES_APP_ID")

BOT_PERMISSIONS_INT = 274877958144
# Send Messages, .. in Threads, Embed Links, Attach Files, that's it.
# needs add reactions

# URLS
CHATRULES_LINK_OTHER_BOTS = "https://pivot.quest/dbots/"

CHATRULES_LINK_DONATE = "https://ko-fi.com/lepivo/goal?g=8"

CHATRULES_LINK_VOTE = f"https://top.gg/bot/{APP_ID}/vote"

_CHATRULES_LINK_SUPPORT = "https://discord.gg/"

SUPPORT_SERVER_INVITE = _CHATRULES_LINK_SUPPORT + os.getenv("CHATRULES_SUPPORT_INVITE_CODE", "support")

CHATRULES_LINK_INVITE = (
    "https://discord.com/oauth2/authorize"
    f"?client_id={APP_ID}"
    f"&permissions={BOT_PERMISSIONS_INT}"
    "&integration_type=0"
    "&scope=applications.commands+bot"
)

### Runtime check
if not CHATRULES_DISCORD_TOKEN:
    raise RuntimeError("Missing CHATRULES_DISCORD_TOKEN in .env")
### EOF ###