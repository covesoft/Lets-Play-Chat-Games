import discord
import pytest

from lets_play.discord_adapter.wordlist_ui import WordlistConfigView
from lets_play.new_rules.phrase_bank import parse_phrase_bank
from lets_play.storage.game_store import open_game_store
from lets_play.storage.guild_phrases import GuildPhraseBank


@pytest.mark.asyncio
async def test_guild_phrase_bank_mutations(tmp_path):
    db = tmp_path / "test.sqlite3"
    initial = parse_phrase_bank("hello world\nnice duck")
    async with (
        open_game_store(db, guild_phrase_defaults=initial) as store,
        store.transaction() as unit,
    ):
        bank = GuildPhraseBank(unit)
        guild_id = 123456789

        # Test seed
        phrases = await bank.seed(guild_id)
        assert len(phrases) == 2

        # Test add
        phrases = await bank.add(guild_id, "blue moon")
        assert len(phrases) == 3
        assert phrases[2].text == "blue moon"

        # Test remove
        phrases = await bank.remove(guild_id, "1")
        assert len(phrases) == 2
        assert phrases[0].phrase_id == "1"
        assert phrases[0].text == "nice duck"

        # Test reset
        phrases = await bank.reset(guild_id)
        assert len(phrases) == 2
        assert phrases[0].text == "hello world"


def test_wordlist_config_layout_view():
    phrases = parse_phrase_bank("hello world\nnice duck")

    class MockSettings:
        add_emoji = "➕"
        cancel_emoji = "❌"
        bin_emoji = "🗑"

    class MockBot:
        settings = MockSettings()

    view = WordlistConfigView(MockBot(), 123456789, phrases)
    assert len(view.children) > 0
    # Verify sections with remove buttons
    container = view.children[0]
    first_section = container.children[0]
    assert isinstance(first_section, discord.ui.Section)
    assert first_section.accessory.label == "2/15 Slots Used"
    sections = [
        item
        for item in container.children
        if isinstance(item, discord.ui.Section)
        and isinstance(item.accessory, discord.ui.Button)
        and item.accessory.custom_id
        and item.accessory.custom_id.startswith("wordlist:remove:")
    ]
    assert len(sections) == 2
    for sec in sections:
        assert isinstance(sec.accessory, discord.ui.Button)
        assert sec.accessory.label is None
        assert sec.accessory.emoji is not None
        assert sec.accessory.style == discord.ButtonStyle.secondary
        assert sec.accessory.custom_id.startswith("wordlist:remove:")

    # Verify reset button
    # Find action row or buttons in container
    action_rows = [item for item in container.children if isinstance(item, discord.ui.ActionRow)]
    assert len(action_rows) > 0
    buttons = []
    for row in action_rows:
        buttons.extend(row.children)
    reset_btn = next(
        (b for b in buttons if getattr(b, "custom_id", None) == "wordlist:reset"), None
    )
    assert reset_btn is not None
    assert reset_btn.label == "Delete All Custom Phrases"
    assert reset_btn.style == discord.ButtonStyle.danger


def test_wordlist_config_layout_view_with_six_phrases():
    phrases_text = "\n".join([f"phrase number {i}" for i in range(1, 7)])
    phrases = parse_phrase_bank(phrases_text)

    class MockSettings:
        add_emoji = "➕"
        cancel_emoji = "❌"
        bin_emoji = "🗑"

    class MockBot:
        settings = MockSettings()

    view = WordlistConfigView(MockBot(), 123456789, phrases)
    container = view.children[0]
    text_displays = [
        item for item in container.children if isinstance(item, discord.ui.TextDisplay)
    ]
    assert any("disabled for free games" in td.content for td in text_displays)

