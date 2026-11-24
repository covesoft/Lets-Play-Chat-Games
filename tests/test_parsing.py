"""Executable parser policy examples, with no live messages/private word lists."""

import json
from pathlib import Path

import pytest

from lets_play.new_rules.contracts import Outcome, RuleInstance
from lets_play.new_rules.parsing import (
    MAX_PARSER_CODEPOINTS,
    Formatting,
    ParserLimitError,
    parse_message,
)
from lets_play.new_rules.text_rules import evaluate_text_rule

EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures/parser_examples.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda e: e["id"])
def test_parser_example_pack(example):
    view = parse_message(example["content"])
    assert view.word_keys == tuple(example["words"])
    assert {style.value for style in view.formatting} == set(example["formatting"])
    assert view.is_whisper is example.get("whisper", False)
    for card, expected in example["outcomes"].items():
        result = evaluate_text_rule(view, RuleInstance(f"instance-{card}", card))
        assert result.outcome is Outcome(expected)
        assert result.rule_instance_id == f"instance-{card}"


@pytest.mark.parametrize(
    "apostrophe", ["'", "‘", "’", "‚", "‛", "ʼ", "ʻ", "＇", "`", "´", "′", "‵", "՚"]
)
def test_apostrophe_variants_preserve_contractions_and_equate_word_identity(apostrophe):
    view = parse_message(f"don{apostrophe}t 'don't' over...")
    assert view.word_keys == ("don't", "don't", "over")


@pytest.mark.parametrize("dash", ["-", "‐", "‑", "‒", "–", "—", "−", "－"])
def test_hyphens_and_dashes_separate_words_without_concatenation(dash):
    assert parse_message(f"well{dash}known over...").word_keys == ("well", "known", "over")


def test_canonical_equivalence_and_graphemes_do_not_change_literal_ellipsis():
    view = parse_message("café cafe\u0301 👩🏽‍🚀")
    assert view.word_keys == ("café", "café")
    assert parse_message("👩🏽‍🚀").character_count == 1
    assert parse_message("e\u0301").character_count == 1
    assert parse_message("…").ending == "…"
    assert parse_message("…").character_count == 1


def test_link_destinations_and_discord_object_ids_do_not_become_words():
    view = parse_message("[over...](https://example.invalid/hidden-word) <a:emoji:123456>")
    assert view.word_keys == ("over",)
    assert view.links[0].destination == "https://example.invalid/hidden-word"
    assert view.objects == ("<a:emoji:123456>",)
    assert not view.ends_with_word("over")  # The visible emoji follows the ending.


def test_shared_links_keep_destinations_without_changing_legacy_formatting_or_word_views():
    view = parse_message(
        "[pond](https://example.invalid/hidden) (https://example.invalid/wiki/Duck_(bird)), "
        "<https://example.invalid/angle> www.example.invalid/path"
    )
    assert [(link.destination, link.masked) for link in view.shared_links] == [
        ("https://example.invalid/hidden", True),
        ("https://example.invalid/angle", False),
        ("https://example.invalid/wiki/Duck_(bird)", False),
        ("www.example.invalid/path", False),
    ]
    bare = parse_message("https://example.invalid/path")
    assert not bare.links and not bare.formatting
    assert bare.word_keys == ("https", "example", "invalid", "path")
    assert bare.shared_links and "example.invalid" not in repr(bare.shared_links)


def test_parsing_preserves_raw_and_line_breaks_without_a_code_or_spoiler_escape():
    source = "```text\n**turnover...**\n```\n||over...||"
    view = parse_message(source)
    assert view.raw == source
    assert view.has_line_break
    assert view.word_keys == ("turnover", "over")
    assert Formatting.CODE_BLOCK in view.formatting
    assert Formatting.SPOILER in view.formatting
    assert "**turnover...**" in view.logical  # Code content remains literal.


@pytest.mark.parametrize(
    ("source", "logical"),
    [
        (">>> " + "a" * 40, "a" * 40),
        (">>> **ducks**\r\n\r\npond", "ducks\n\npond"),
        (">>> \npond", "\npond"),
        ("> duck\n>>> pond\nswim", "duck\npond\nswim"),
        (">>> ```\n>>> literal\n```", "\n>>> literal\n"),
    ],
)
def test_multiline_quotes_drop_one_marker_and_preserve_the_quoted_content(source, logical):
    view = parse_message(source)
    assert view.logical == logical and view.raw == source
    assert Formatting.BLOCKQUOTE in view.formatting
    assert evaluate_text_rule(view, RuleInstance("small", "NR021")).outcome is Outcome.PASS
    assert evaluate_text_rule(view, RuleInstance("plain", "NR016")).outcome is Outcome.FAIL
    assert evaluate_text_rule(view, RuleInstance("breath", "NR015")).outcome is (
        Outcome.FAIL if "\n" in source else Outcome.PASS
    )


@pytest.mark.parametrize(
    ("terminal", "question", "exclamation"),
    [
        ("?", True, False),
        ("？", True, False),
        ("⁉", True, False),
        ("⁉️", True, False),
        ("❓", True, False),
        ("❔", True, False),
        (":interrobang:", True, False),
        (":question:", True, False),
        (":grey_question:", True, False),
        ("!", False, True),
        ("！", False, True),
        ("‼", False, True),
        ("‼️", False, True),
        (":bangbang:", False, True),
        ("?!", False, True),
        ("!?", True, False),
        ("⁉️ 🦆", False, False),
        (":question: later", False, False),
    ],
)
def test_question_and_exclamation_cards_recognize_distinct_terminal_forms(
    terminal, question, exclamation
):
    view = parse_message(f"“**Ducks {terminal}**”")
    for card, passed in (("NR010", question), ("NR012", exclamation)):
        assert evaluate_text_rule(view, RuleInstance(card, card)).outcome is (
            Outcome.PASS if passed else Outcome.FAIL
        )


def test_parser_rejects_oversize_instead_of_silently_truncating_scored_text():
    with pytest.raises(ParserLimitError):
        parse_message("x" * (MAX_PARSER_CODEPOINTS + 1))
    assert parse_message("x" * MAX_PARSER_CODEPOINTS).word_keys == ("x" * MAX_PARSER_CODEPOINTS,)


def test_repr_does_not_log_transient_message_tokens_links_or_objects():
    view = parse_message("[PRIVATE_SENTINEL](https://example.invalid/PRIVATE_SENTINEL)")
    assert "PRIVATE_SENTINEL" not in repr(view) + repr(view.words) + repr(view.links)


def test_no_default_pass_for_an_unimplemented_card():
    with pytest.raises(NotImplementedError):
        evaluate_text_rule(parse_message("over..."), RuleInstance("next-rule", "NR040"))


@pytest.mark.parametrize("prefix", ["> " * 100, "- " * 100])
def test_upstream_nesting_guard_cannot_silently_remove_evaluable_words(prefix):
    view = parse_message(prefix + "sentinel over...")
    assert "sentinel" in view.word_keys
    assert view.ends_with_word("over")
    assert view.ending.endswith("...")


def test_relative_link_syntax_is_kept_literal_instead_of_erasing_destination_text():
    view = parse_message("[over...](relative/path)")
    assert view.logical == "[over...](relative/path)"
    assert not view.links and not view.formatting
    assert view.word_keys == ("over", "relative", "path")


def test_separate_parse_calls_do_not_share_message_or_formatting_state():
    first = parse_message("||**over...**||")
    second = parse_message("plain over...")
    assert second.formatting == frozenset()
    assert second.word_keys == ("plain", "over")
    assert first.logical == "over..."
