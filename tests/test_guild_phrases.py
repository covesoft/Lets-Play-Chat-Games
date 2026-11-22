"""Configured defaults, durable guild settings, and live Don't Forget deals."""

import pytest
from test_discord_ordinary_loop import choose_card, next_offer, posts, settle
from test_discord_runtime import activated, event, opened, service
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.cli import main
from lets_play.discord_adapter.discord_io import render
from lets_play.discord_adapter.runtime import OPENING_CARDS
from lets_play.new_rules.phrase_bank import PhraseBankError, load_guild_phrases, parse_phrase_bank
from lets_play.new_rules.profanity import ProfanityList
from lets_play.storage import bootstrap
from lets_play.storage.game_store import open_game_store
from lets_play.storage.guild_phrases import GuildPhraseBank

STARTER_TEXT = "gimme\nfor real\nnice hat\nnever mind\nhave a good\n"
TEMPLATES = frozenset({"NR004", "NR006", "NR036"})


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("nice hat\nNICE-HAT", "duplicates"),
        ("red green blue orange gold plum", "one to five"),
        ("g" * 81, "plain phrase"),
        ("**nice hat**", "plain phrase"),
        ("-# nice hat", "plain phrase"),
        ("@everyone", "plain phrase"),
        ("https://example.invalid", "plain phrase"),
        ("1. named list item", "one to five"),
        ("nice🏷 hat", "plain phrase"),
        ("\n".join(f"red duck {i}" for i in range(16)), "up to 15"),
    ],
)
def test_suitability_rejects_unusable_bank_without_echoing_input(text, error):
    with pytest.raises(PhraseBankError, match=error) as exc:
        parse_phrase_bank(text)
    assert text not in str(exc.value)


def test_operator_suitability_is_independent_and_reports_line_only():
    policy = ProfanityList("turnip")
    assert len(parse_phrase_bank(STARTER_TEXT, suitability=policy)) == 5
    with pytest.raises(PhraseBankError, match="Line 2 does not pass"):
        parse_phrase_bank("green duck\nA TURNIP", suitability=policy)


def test_check_config_and_import_share_validation(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    data.mkdir()
    phrase_file = data / "custom_phrases.txt"
    phrase_file.write_text(STARTER_TEXT, encoding="utf-8-sig")
    (tmp_path / ".env").write_text(
        "CHATGAMES_GUILD_PHRASES_FILE_PATH=data/custom_phrases.txt\n", encoding="utf-8"
    )
    monkeypatch.delenv("CHATGAMES_GUILD_PHRASES_FILE_PATH", raising=False)
    assert main(["--check-config"], base_dir=tmp_path) == 0
    output = capsys.readouterr().out
    assert "defaults loaded (phrases: 5)" in output
    assert "nice hat" not in output and "custom_phrases.txt" not in output
    assert not (data / "lets_play.sqlite3").exists()
    assert main(["--import-guild-phrases", "101"], base_dir=tmp_path) == 0
    assert "guild bank saved (phrases: 5)" in capsys.readouterr().out
    phrase_file.write_text("nice hat\nNICE-HAT", encoding="utf-8")
    assert main(["--import-guild-phrases", "101"], base_dir=tmp_path) == 2
    assert "Line 2 duplicates" in capsys.readouterr().err


def test_configured_unreadable_suitability_prevents_loading_defaults(tmp_path):
    phrase_file = tmp_path / "phrases.txt"
    phrase_file.write_text(STARTER_TEXT, encoding="utf-8")
    with pytest.raises(PhraseBankError, match="CHATGAMES_PHRASE_SUITABILITY_LIST_PATH"):
        load_guild_phrases(phrase_file, tmp_path / "missing.txt")


@pytest.mark.asyncio
async def test_schema_16_upgrade_and_seeds_survive_default_edits_and_explicit_empty(
    tmp_path, monkeypatch
):
    db = tmp_path / "game.sqlite3"
    with monkeypatch.context() as old:
        old.setattr(bootstrap, "MIGRATIONS", bootstrap.MIGRATIONS[:16])
        assert (await bootstrap.initialize_database(db)).schema_version == 16
    assert (await bootstrap.initialize_database(db)).schema_version == 17
    initial = parse_phrase_bank(STARTER_TEXT)
    async with (
        open_game_store(db, guild_phrase_defaults=initial) as store,
        store.transaction() as unit,
    ):
        bank = GuildPhraseBank(unit)
        assert await bank.seed(101) == initial
        await bank.replace(202, ())
    revised = parse_phrase_bank("blue moon")
    async with (
        open_game_store(db, guild_phrase_defaults=revised) as store,
        store.transaction() as unit,
    ):
        bank = GuildPhraseBank(unit)
        assert await bank.seed(101) == initial
        assert await bank.seed(202) == ()
        assert await bank.seed(303) == revised


def phrase_option(state):
    return next(o.incoming for o in state.offer.hand.options if o.incoming.card_id == "NR036")


@pytest.mark.asyncio
async def test_frozen_published_phrase_survives_bank_update_restart_and_scores(
    tmp_path, monkeypatch
):
    defaults = parse_phrase_bank(STARTER_TEXT)
    async with service(tmp_path, monkeypatch, guild_phrase_defaults=defaults) as runtime:
        route = await opened(runtime)
        assert "NR036" not in OPENING_CARDS
        await activated(runtime, route)
        await settle(runtime)
        await runtime.submit(route, "configure", templates=TEMPLATES)
        await posts(runtime, route)
        offer = await next_offer(runtime, route)
        incoming = phrase_option(offer)
        phrase = dict(incoming.parameters)["phrase"]
        assert phrase in STARTER_TEXT.splitlines()
        assert any(phrase in field.value for field in render(offer)[0].fields)
        async with runtime.store.transaction() as unit:
            bank = GuildPhraseBank(unit)
            assert await bank.get(route.guild_id) == defaults
            await bank.replace(route.guild_id, parse_phrase_bank(f"{phrase.upper()}\nBlue moon"))
    async with service(tmp_path, monkeypatch, transport=RecoveryTransport()) as runtime:
        saved = await reading(runtime, route)
        await fire(runtime, route, saved.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert phrase_option(resumed) == incoming
        played = await choose_card(runtime, route, resumed, "NR036")
        assert incoming in played.context.active_instances
        await runtime.submit(route, "event", event=event(runtime, route, 96001, f"{phrase} over"))
        await runtime.submit(route, "event", event=event(runtime, route, 96002, "different over"))
        await settle(runtime)
        assert runtime.transport.markers == [96002]
        await runtime.submit(route, "configure", templates=TEMPLATES)
        await posts_for_phrase(runtime, route, phrase)
        new_offer = await next_offer(runtime, route)
        assert dict(phrase_option(new_offer).parameters)["phrase"] == "Blue moon"


async def posts_for_phrase(runtime, route, phrase):
    for actor in (303, 404, 505):
        for n in range(5):
            item = event(runtime, route, 500000 + actor * 100 + n, f"{phrase} over", actor)
            await runtime.submit(route, "event", event=item)
    await settle(runtime)
