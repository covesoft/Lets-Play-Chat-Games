"""Editable copy/palette behavior and early failure of malformed owner overrides."""

import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest
from test_discord_runtime import choice, opened, service

from lets_play.cli import main
from lets_play.config import load_settings
from lets_play.discord_adapter import score_ui
from lets_play.discord_adapter.discord_io import DiscordTransport, render
from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.discord_adapter.runtime import ORDINARY_CARDS
from lets_play.discord_adapter.score_ui import detailed_player_entry, player_score_line
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.presentation import PresentationError, load_presentation


def override(tmp_path, data):
    path = tmp_path / "copy.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_score_copy_supports_custom_layout_and_accepts_old_overrides(tmp_path, monkeypatch):
    copy = load_presentation(
        override(
            tmp_path,
            {
                "strings": {
                    "results.score": "{points:,} pts",
                    "results.score_unrated": "Pending",
                    "results.player_score": "{score} | {player}",
                    "reports.player_score": "{player} ({score})",
                    "results.ranking": "{percent:.1f}",
                    "reports.score": "Scoring {percent:.1f}",
                }
            },
        )
    )
    unrated = ScoreTotals()
    clean = ScoreTotals(
        human_messages=1990,
        ordinary_posts=1990,
        ordinary_observations=1990,
        ordinary_quality=1990,
    )
    assert detailed_player_entry(copy, 123, unrated).splitlines()[:2] == [
        "<@123>",
        "Actual compliance: Unrated",
    ]
    assert player_score_line(copy, 123, unrated, template="reports.player_score") == "<@123>"
    assert player_score_line(copy, 123, ScoreTotals(reaction_failures=20_000)) == "0 pts | <@123>"
    assert player_score_line(copy, 123, clean) == "1,000 pts | <@123>"
    assert (
        player_score_line(copy, 123, clean, template="reports.player_score") == "<@123> (1,000 pts)"
    )

    monkeypatch.setattr(score_ui, "SHOW_UNRATED_SCORE_PLACEHOLDER", True)
    assert player_score_line(copy, 123, unrated) == "Pending | <@123>"
    assert (
        player_score_line(copy, 123, unrated, template="reports.player_score") == "<@123> (Pending)"
    )


def test_marker_has_no_fallback_and_settings_hide_private_configuration(tmp_path, monkeypatch):
    assert load_settings(environ={}).ding_emoji is None
    (tmp_path / ".env").write_text("CHATGAMES_DING_EMOJI=private-marker-sentinel\n")
    monkeypatch.setenv("CHATGAMES_DING_EMOJI", "")
    assert load_settings(base_dir=tmp_path).ding_emoji is None
    settings = load_settings(
        environ={
            "CHATGAMES_DING_EMOJI": "private-marker-sentinel",
            "CHATGAMES_PRESENTATION_PATH": "private-copy-sentinel",
        }
    )
    assert settings.ding_emoji == "private-marker-sentinel"
    assert "private-marker-sentinel" not in repr(settings)
    assert "private-copy-sentinel" not in repr(settings)


@pytest.mark.asyncio
async def test_unconfigured_marker_is_refused_before_any_public_probe():
    transport = DiscordTransport(NS(settings=load_settings(environ={})))
    channel = NS(guild=NS(me=None), send=AsyncMock())
    with pytest.raises(LaneUnavailable):
        await transport.probe(channel)
    channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_translation_and_colors_change_output_without_changing_controls(
    tmp_path, monkeypatch
):
    path = override(
        tmp_path,
        {
            "locale": "fr-FR",
            "strings": {
                "lobby.description": "Jouons ensemble.",
                "buttons.play": "Jouer",
                "offer.description": "Choisissez, <@{picker}>.",
                "cards.NR034.name": "Terminé",
            },
            "colors": {"lobby": "#123456", "offer": "#ABCDEF"},
        },
    )
    copy = load_presentation(path)
    assert copy.locale == "fr-FR"
    assert copy.text("control.joined") == load_presentation().text("control.joined")
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        state = await runtime.state(route.session_id)
        original_embed, original_view = render(state)
        embed, view = render(state, presentation=copy)
        assert embed.description == "Jouons ensemble."
        assert embed.colour.value == 0x123456
        assert view.children[0].label == "Jouer"
        assert view.children[0].custom_id == original_view.children[0].custom_id
        state = await choice(runtime, route)
        embed, view = render(state, presentation=copy)
        assert embed.description == f"Choisissez, <@{state.admission.picker_id}>."
        assert embed.colour.value == 0xABCDEF
        assert any(button.label == "Terminé" for button in view.children)
        assert state.admission.hand == (await runtime.state(route.session_id)).admission.hand
        assert original_embed.colour.value != embed.colour.value


@pytest.mark.parametrize(
    "data",
    [
        {"strings": {"offer.description": "No picker placeholder"}},
        {"strings": {"offer.description": "{picker.secret}"}},
        {"strings": {"offer.description": "{picker!r}"}},
        {"strings": {"offer.description": "{picker:{missing}}"}},
        {"strings": {"control.joined": "{unknown}"}},
        {"strings": {"control.joined": ""}},
        {"strings": {"unknown": "extra"}},
        {"colors": {"default": "invalid"}},
        {"colors": {"default": True}},
        {"locale": "not a language tag"},
    ],
)
def test_invalid_overrides_fail_before_gameplay(tmp_path, data):
    with pytest.raises(PresentationError):
        load_presentation(override(tmp_path, data))


def test_duplicate_json_keys_and_bad_paths_report_no_private_contents(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text('{"strings":{},"strings":{"private":"secret"}}')
    with pytest.raises(PresentationError, match="valid JSON"):
        load_presentation(path)
    with pytest.raises(PresentationError) as caught:
        load_presentation(tmp_path / "private-path-sentinel")
    assert "private-path-sentinel" not in str(caught.value)


def test_check_config_validates_private_copy_without_echoing_values(tmp_path, monkeypatch, capsys):
    path = override(tmp_path, {"colors": {"default": "private-invalid-value"}})
    monkeypatch.setenv("CHATGAMES_PRESENTATION_PATH", str(path))
    assert main(["--check-config"], base_dir=tmp_path) == 2
    output = capsys.readouterr()
    assert "Presentation color" in output.err
    assert "private-invalid-value" not in output.err
    assert not (tmp_path / "data").exists()


def test_catalog_covers_sent_keys_and_card_labels_without_inline_english():
    copy = load_presentation()
    module = ast.parse(
        (Path(__file__).parents[1] / "lets_play/discord_adapter/discord_io.py").read_text()
    )
    for node in ast.walk(module):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "text" and node.args and isinstance(node.args[0], ast.Constant):
                assert node.args[0].value in copy.strings
            if node.func.attr == "send" and node.args:
                assert not isinstance(node.args[0], ast.Constant), (
                    "Sent copy belongs in the catalog."
                )
            if node.func.attr == "Embed":
                assert not any(
                    k.arg in {"color", "colour"} and isinstance(k.value, ast.Constant)
                    for k in node.keywords
                )
    for card in ORDINARY_CARDS:
        fields = {"phrase": "red duck"} if card == "NR036" else {}
        assert copy.text(f"cards.{card}.name") and copy.text(f"cards.{card}.description", **fields)


def test_startup_logging_preserves_owner_intent_and_handles_ctrl_c(tmp_path, monkeypatch, caplog):
    import lets_play.cli as cli

    monkeypatch.setenv("CHATGAMES_DISCORD_TOKEN", "private-token-sentinel")

    def interrupted(coroutine):
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.asyncio, "run", interrupted)
    with caplog.at_level(logging.INFO):
        assert main([], base_dir=tmp_path) == 0
    assert "Launching Let's Play Chat Games Discord bot" in caplog.text
    assert "Bot has stopped by intervention." in caplog.text
    assert "private-token-sentinel" not in caplog.text
