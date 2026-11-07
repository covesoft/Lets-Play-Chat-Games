"""User-facing configuration behavior, including secret-free diagnostics."""

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import discord
import pytest

from lets_play.config import CHATGAMES_BOT_PERMISSIONS_INT, ConfigError, load_settings


def test_offline_defaults_do_not_need_token_or_create_files(tmp_path):
    settings = load_settings(base_dir=tmp_path, environ={})
    assert settings.token is None
    assert settings.database_path == tmp_path / "data/lets_play.sqlite3"
    assert settings.invite_url is None
    assert settings.vote_url is None
    assert settings.support_url is None
    assert settings.admin_ids == frozenset()
    assert not list(tmp_path.iterdir())
    with pytest.raises(ConfigError, match="CHATGAMES_DISCORD_TOKEN"):
        settings.require_token()


def test_dotenv_is_explicit_and_environment_wins_without_mutation(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "CHATGAMES_APP_ID=123\nCHATGAMES_DISCORD_TOKEN=local-sentinel\n"
        "CHATGAMES_DING_EMOJI=${UNEXPANDED}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CHATGAMES_APP_ID", "456")
    monkeypatch.setenv("CHATGAMES_DISCORD_TOKEN", "")
    settings = load_settings(base_dir=tmp_path)
    assert settings.app_id == 456
    assert settings.token is None
    assert settings.ding_emoji == "${UNEXPANDED}"
    import os

    assert "CHATGAMES_DING_EMOJI" not in os.environ
    # An explicit mapping bypasses even an existing file.
    assert load_settings(base_dir=tmp_path, environ={}).app_id is None


def test_settings_repr_never_discloses_token_or_private_list_paths(tmp_path):
    settings = load_settings(
        base_dir=tmp_path,
        environ={
            "CHATGAMES_DISCORD_TOKEN": "fake-token-sentinel",
            "CHATGAMES_PROFANITY_LIST_PATH": "private-list-sentinel.txt",
            "CHATGAMES_PHRASE_SUITABILITY_LIST_PATH": "suitability-sentinel.txt",
        },
    )
    assert "fake-token-sentinel" not in repr(settings)
    assert "private-list-sentinel" not in repr(settings)
    assert "suitability-sentinel" not in repr(settings)
    assert settings.require_token() == "fake-token-sentinel"


def test_owners_are_deduplicated_and_empty_entries_are_compatible():
    settings = load_settings(environ={"CHATGAMES_ADMIN_IDS": "123, 456,123, ,"})
    assert settings.admin_ids == frozenset({123, 456})


@pytest.mark.parametrize(
    "key,value",
    [
        ("CHATGAMES_ADMIN_IDS", "123,owner-secret"),
        ("CHATGAMES_APP_ID", "-1"),
        ("CHATGAMES_APP_ID", "0"),
        ("CHATGAMES_APP_ID", "１２３"),
        ("CHATGAMES_APP_ID", str(2**64)),
        ("CHATGAMES_SYNC_COMMANDS", "sometimes"),
        ("CHATGAMES_DEVELOPMENT_MODE", "debug"),
        ("CHATGAMES_LINK_VOTE", "http://example.invalid/private"),
        ("CHATGAMES_LINK_VOTE", "https://user:secret@example.invalid/"),
        ("CHATGAMES_LINK_VOTE", "https://[broken"),
        ("CHATGAMES_SUPPORT_INVITE_CODE", "https://discord.gg/secret"),
        ("CHATGAMES_DATABASE_PATH", ":memory:"),
    ],
)
def test_bad_settings_fail_with_key_only_diagnostic(key, value):
    with pytest.raises(ConfigError) as error:
        load_settings(environ={key: value})
    assert key in str(error.value)
    assert value not in str(error.value)


@pytest.mark.parametrize(
    "value,expected", [("true", True), ("1", True), ("FALSE", False), ("0", False)]
)
def test_boolean_contract(value, expected):
    assert load_settings(environ={"CHATGAMES_SYNC_COMMANDS": value}).sync_commands is expected


def test_paths_are_root_relative_without_loading_optional_private_files(tmp_path):
    settings = load_settings(
        base_dir=tmp_path,
        environ={
            "CHATGAMES_DATABASE_PATH": "other/local.sqlite3",
            "CHATGAMES_PROFANITY_LIST_PATH": "missing-private-list.txt",
        },
    )
    assert settings.database_path == tmp_path / "other/local.sqlite3"
    assert settings.profanity_list_path == tmp_path / "missing-private-list.txt"
    assert not list(tmp_path.iterdir())
    absolute = tmp_path / "external.sqlite3"
    assert (
        load_settings(environ={"CHATGAMES_DATABASE_PATH": str(absolute)}).database_path == absolute
    )


def test_invite_contains_only_agreed_permissions_and_guild_scopes():
    settings = load_settings(environ={"CHATGAMES_APP_ID": "123"})
    query = parse_qs(urlsplit(settings.invite_url).query)
    permissions = discord.Permissions.none()
    for name in [
        "view_channel",
        "send_messages",
        "send_messages_in_threads",
        "embed_links",
        "attach_files",
        "read_message_history",
        "add_reactions",
    ]:
        setattr(permissions, name, True)
    assert CHATGAMES_BOT_PERMISSIONS_INT == permissions.value
    assert query == {
        "client_id": ["123"],
        "permissions": [str(permissions.value)],
        "integration_type": ["0"],
        "scope": ["applications.commands bot"],
    }


def test_optional_links_are_only_supplied_when_configured():
    settings = load_settings(
        environ={
            "CHATGAMES_SUPPORT_INVITE_CODE": "test-code",
            "CHATGAMES_LINK_VOTE": "https://example.invalid/vote",
        }
    )
    assert settings.support_url == "https://discord.gg/test-code"
    assert settings.vote_url == "https://example.invalid/vote"
    assert load_settings(environ={"CHATGAMES_APP_ID": "123"}).vote_url is None


def test_committed_example_is_safe_and_satisfies_offline_contract():
    from dotenv import dotenv_values

    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env.example")
    settings = load_settings(environ={key: value or "" for key, value in values.items()})
    assert settings.token is None
    assert not settings.admin_ids
    assert not settings.sync_commands
    assert settings.profanity_list_path is None
