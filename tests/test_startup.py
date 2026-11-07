"""No live login or HTTP requests: exercise real SDK construction and CLI paths."""

import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import discord
import pytest
from better_profanity import Profanity

from lets_play import __version__
from lets_play.bot import LetsPlayBot, build_intents
from lets_play.cli import main
from lets_play.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


def test_pinned_text_dependencies_work_without_default_profanity_policy():
    import emoji

    sequence = "👩🏽‍🚀"
    assert emoji.emoji_list(sequence)[0]["emoji"] == sequence
    checker = Profanity(words=["turnip"])
    assert checker.contains_profanity("a turnip")
    assert not checker.contains_profanity("a carrot")


def test_intents_request_only_guild_message_reaction_content_capabilities():
    expected = discord.Intents.none()
    for name in ["guilds", "guild_messages", "guild_reactions", "message_content"]:
        setattr(expected, name, True)
    assert build_intents().value == expected.value


@pytest.mark.asyncio
async def test_sdk_construction_and_close_need_no_token_or_network(tmp_path):
    bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
    async with bot:
        assert bot._connection.max_messages is None
        assert bot._connection.member_cache_flags.value == 0
        assert not bot.commands
        assert not bot.tree.get_commands()
    assert bot.is_closed()
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sync,has_command,should_sync",
    [
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
async def test_setup_initializes_database_and_only_syncs_with_opt_in_and_commands(
    tmp_path, monkeypatch, sync, has_command, should_sync
):
    settings = load_settings(base_dir=tmp_path, environ={"CHATRULES_SYNC_COMMANDS": str(sync)})
    bot = LetsPlayBot(settings)
    sync_mock = AsyncMock()
    monkeypatch.setattr(bot.tree, "sync", sync_mock)
    if has_command:

        async def fixture_command(interaction: discord.Interaction):
            """Harmless offline command fixture."""

        bot.tree.add_command(
            discord.app_commands.Command(
                name="fixture", description="Fixture", callback=fixture_command
            )
        )
    async with bot:
        await bot.setup_hook()
        assert sync_mock.await_count == int(should_sync)
    assert settings.database_path.exists()


def test_check_config_reports_status_without_disclosing_token_or_creating_db(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("CHATRULES_DISCORD_TOKEN", "fake-token-sentinel")
    assert main(["--check-config"], base_dir=tmp_path) == 0
    output = capsys.readouterr()
    assert "configured" in output.out
    assert "fake-token-sentinel" not in output.out + output.err
    assert not list(tmp_path.iterdir())


def test_live_startup_without_token_fails_before_network_or_db(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CHATRULES_DISCORD_TOKEN", raising=False)
    assert main([], base_dir=tmp_path) == 2
    assert "CHATRULES_DISCORD_TOKEN" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_explicit_init_db_works_without_token(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("CHATRULES_DISCORD_TOKEN", raising=False)
    assert main(["--init-db"], base_dir=tmp_path) == 0
    assert "journal=wal" in capsys.readouterr().out
    assert (tmp_path / "data/lets_play.sqlite3").exists()


def test_import_original_launchers_without_token_and_version_agrees(monkeypatch):
    monkeypatch.delenv("CHATRULES_DISCORD_TOKEN", raising=False)
    shim = importlib.import_module("config")
    importlib.import_module("discordbot_chatgames")
    assert shim.CHATRULES_VERSION == f"v{__version__}"


def test_module_and_original_cli_version_match():
    for args in [["-m", "lets_play"], [str(ROOT / "discordbot_chatgames.py")]]:
        result = subprocess.run(
            [sys.executable, *args, "--version"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        assert result.stdout.strip() == f"Let's Play {__version__}"
