"""Explicit configuration loading with no token requirement at import time."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from dotenv import dotenv_values

BOT_PERMISSIONS_INT = 274878024768
REPOSITORY_URL = "https://github.com/covesoft/Lets-Play-Chat-Games"
OTHER_BOTS_URL = "https://pivot.quest/dbots/"
THIS_BOT_URL = "https://pivot.quest/dbots/#lets-play"
DONATE_URL = "https://ko-fi.com/lepivo/goal?g=8"


class ConfigError(ValueError):
    """A safe, key-only diagnostic, never an echo of a configured value."""


@dataclass(frozen=True, slots=True)
class Settings:
    token: str | None = field(default=None, repr=False)
    app_id: int | None = None
    admin_ids: frozenset[int] = frozenset()
    database_path: Path = Path("data/lets_play.sqlite3")
    profanity_list_path: Path | None = field(default=None, repr=False)
    phrase_suitability_list_path: Path | None = field(default=None, repr=False)
    badge_emoji_map_path: Path | None = None
    ding_emoji: str | None = None
    development_mode: bool = False
    sync_commands: bool = False
    other_bots_url: str = OTHER_BOTS_URL
    this_bot_url: str = THIS_BOT_URL
    repository_url: str = REPOSITORY_URL
    donate_url: str = DONATE_URL
    support_url: str | None = None
    vote_url: str | None = None
    privacy_contact_url: str | None = None

    def require_token(self) -> str:
        if not self.token:
            raise ConfigError("CHATRULES_DISCORD_TOKEN is required for live startup.")
        return self.token

    @property
    def invite_url(self) -> str | None:
        if self.app_id is None:
            return None
        query = urlencode(
            {
                "client_id": self.app_id,
                "permissions": BOT_PERMISSIONS_INT,
                "integration_type": 0,
                "scope": "applications.commands bot",
            }
        )
        return f"https://discord.com/oauth2/authorize?{query}"


def _value(env: Mapping[str, str | None], key: str) -> str | None:
    value = env.get(key)
    return value.strip() or None if value is not None else None


def _snowflake(value: str | None, key: str) -> int | None:
    if value is None:
        return None
    if not re.fullmatch(r"[0-9]{1,20}", value) or not 0 < int(value) < 2**64:
        raise ConfigError(f"{key} must contain a positive Discord ID.")
    return int(value)


def _boolean(env: Mapping[str, str | None], key: str) -> bool:
    value = _value(env, key)
    if value is None:
        return False
    if value.casefold() not in {"true", "false", "1", "0"}:
        raise ConfigError(f"{key} must be true, false, 1, or 0.")
    return value.casefold() in {"true", "1"}


def _path(base: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _url(env: Mapping[str, str | None], key: str, default: str | None = None) -> str | None:
    value = _value(env, key) or default
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not any(character.isspace() for character in value)
            and parsed.port in {None, 443}
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigError(f"{key} must be an HTTPS URL without credentials.")
    return value


def load_settings(
    *, base_dir: Path | None = None, environ: Mapping[str, str] | None = None
) -> Settings:
    """Read only the selected root's .env; the process environment wins.

    Passing ``environ`` supplies the complete environment and disables .env reads
    for deterministic offline tests. No process environment values are mutated.
    Optional content files are not loaded or checked until their feature exists.
    """
    base = (base_dir or Path.cwd()).resolve()
    if environ is None:
        env: Mapping[str, str | None] = {
            **dotenv_values(base / ".env", interpolate=False),
            **os.environ,
        }
    else:
        env = environ
    owners = set()
    for item in (_value(env, "ADMIN_IDS") or "").split(","):
        if item.strip():
            owners.add(_snowflake(item.strip(), "ADMIN_IDS"))
    database = _value(env, "CHATRULES_DATABASE_PATH") or "data/lets_play.sqlite3"
    if database == ":memory:":
        raise ConfigError("CHATRULES_DATABASE_PATH must name an on-disk database for WAL.")
    support_url = _url(env, "CHATRULES_LINK_SUPPORT")
    support_code = _value(env, "CHATRULES_SUPPORT_INVITE_CODE")
    if support_url is None and support_code is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", support_code):
            raise ConfigError("CHATRULES_SUPPORT_INVITE_CODE must contain an invite code.")
        support_url = f"https://discord.gg/{support_code}"
    return Settings(
        token=_value(env, "CHATRULES_DISCORD_TOKEN"),
        app_id=_snowflake(_value(env, "CHATRULES_APP_ID"), "CHATRULES_APP_ID"),
        admin_ids=frozenset(owners),
        database_path=_path(base, database),
        profanity_list_path=_path(base, _value(env, "CHATRULES_PROFANITY_LIST_PATH")),
        phrase_suitability_list_path=_path(
            base, _value(env, "CHATRULES_PHRASE_SUITABILITY_LIST_PATH")
        ),
        badge_emoji_map_path=_path(base, _value(env, "CHATRULES_BADGE_EMOJI_MAP_PATH")),
        ding_emoji=_value(env, "CHATRULES_DING_EMOJI"),
        development_mode=_boolean(env, "CHATRULES_DEVELOPMENT_MODE"),
        sync_commands=_boolean(env, "CHATRULES_SYNC_COMMANDS"),
        other_bots_url=_url(env, "CHATRULES_LINK_OTHER_BOTS", OTHER_BOTS_URL),
        this_bot_url=_url(env, "CHATRULES_LINK_THIS_BOT", THIS_BOT_URL),
        repository_url=_url(env, "CHATRULES_LINK_GITHUB", REPOSITORY_URL),
        donate_url=_url(env, "CHATRULES_LINK_DONATE", DONATE_URL),
        support_url=support_url,
        vote_url=_url(env, "CHATRULES_LINK_VOTE"),
        privacy_contact_url=_url(env, "CHATRULES_PRIVACY_CONTACT_URL"),
    )
