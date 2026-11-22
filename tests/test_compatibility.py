"""Actual-validator witnesses, whole-state contradictions and legal removals."""

import json
from itertools import combinations
from pathlib import Path

import pytest

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.compatibility import (
    Rejection,
    WitnessBudget,
    assess_state,
    legal_completions,
)
from lets_play.new_rules.contracts import Outcome, RuleInstance
from lets_play.new_rules.evaluation import SUPPORTED_RULES
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.text_rules import SUPPORTED_TEXT_RULES, evaluate_text_rule

EXAMPLES = json.loads(
    (Path(__file__).parent / "fixtures/ordinary_rule_examples.json").read_text(encoding="utf-8")
)


def rule(card, instance_id=None):
    return RuleInstance(instance_id or card, card)


def phrase(text, instance_id="clause"):
    return RuleInstance(instance_id, "NR036", (("phrase", text), ("phrase_revision", "fixture-r1")))


@pytest.mark.parametrize("example", EXAMPLES, ids=lambda item: item["id"])
def test_explicit_ordinary_rule_examples(example):
    outcome = evaluate_text_rule(parse_message(example["content"]), rule(example["card"]))
    assert outcome.outcome is Outcome(example["outcome"])


def verify_witness(instances, assessment):
    assert assessment.legal and assessment.rejection is None
    view = parse_message(assessment.witness.text)
    assert view.has_text and view.words and not view.is_whisper
    for instance, result in zip(instances, assessment.witness.evaluations, strict=True):
        assert result.rule_instance_id == instance.instance_id
        assert result.outcome is (
            Outcome.NOT_APPLICABLE if instance.card_id == "NR006" else Outcome.PASS
        )
        if instance.card_id in SUPPORTED_TEXT_RULES:
            assert evaluate_text_rule(view, instance).outcome is Outcome.PASS


@pytest.mark.parametrize("card", sorted(CARD_SPECS))
def test_every_advertised_template_has_a_nonempty_passing_witness(card):
    assert frozenset(CARD_SPECS) == SUPPORTED_RULES
    instance = phrase("red duck") if card == "NR036" else rule(card)
    verify_witness((instance,), assess_state((instance,)))


@pytest.mark.parametrize(
    "cards", list(combinations(sorted(CARD_SPECS), 2)), ids=lambda cards: "-".join(cards)
)
def test_pair_assessments_never_certify_a_failing_witness(cards):
    instances = tuple(phrase("red duck") if card == "NR036" else rule(card) for card in cards)
    result = assess_state(instances)
    if result.legal:
        verify_witness(instances, result)
    else:
        assert result.witness is None
        assert result.rejection in {Rejection.CAP, Rejection.SLOT, Rejection.CONFLICT}


def test_pairwise_legal_phrases_collectively_exceed_briefly():
    active = (rule("NR017"), phrase("red duck pond", "a"), phrase("blue goose lake", "b"))
    for pair in combinations(active, 2):
        verify_witness(pair, assess_state(pair))
    result = assess_state(active)
    assert not result.legal and result.rejection is Rejection.NO_WITNESS


def test_phrase_overlap_establishes_a_short_nonrepeating_witness():
    active = (rule("NR017"), rule("NR022"), phrase("red duck", "a"), phrase("duck pond", "b"))
    result = assess_state(active)
    verify_witness(active, result)
    assert result.witness.text == "red duck pond"


def test_prefix_suffix_and_contained_phrases_share_words_when_possible():
    active = (
        rule("NR024"),
        rule("NR034"),
        rule("NR017"),
        rule("NR022"),
        phrase("yes red duck", "a"),
        phrase("duck over", "b"),
    )
    result = assess_state(active)
    verify_witness(active, result)
    assert result.witness.text == "yes red duck over"


def test_case_transform_preserves_phrase_matching_and_terminal_composition():
    active = (rule("NR008"), rule("NR033"), rule("NR034"), rule("NR011"), phrase("red duck"))
    result = assess_state(active)
    verify_witness(active, result)
    assert result.witness.text == "PLEASE RED DUCK OVER..."


@pytest.mark.parametrize(
    "active",
    [
        (rule("NR025"), phrase("sorry duck")),
        (rule("NR027"), phrase("maybe duck")),
        (rule("NR022"), phrase("red red duck")),
        (rule("NR022"), rule("NR034"), phrase("over here")),
    ],
)
def test_contradictions_cannot_be_certified_by_empty_media_or_whisper(active):
    assert assess_state(active).rejection is Rejection.NO_WITNESS


def test_no_questions_and_literal_ellipsis_are_compatible():
    active = (rule("NR011"), rule("NR013"), rule("NR034"))
    verify_witness(active, assess_state(active))


@pytest.mark.parametrize(
    "active,rejection",
    [
        ((rule("NR017"), rule("NR019")), Rejection.CAP),
        ((rule("NR022"), rule("NR025"), rule("NR027")), Rejection.CAP),
        ((rule("NR007"), rule("NR008")), Rejection.SLOT),
        ((rule("NR024"), rule("NR033")), Rejection.SLOT),
        ((rule("NR010"), rule("NR011")), Rejection.SLOT),
        ((rule("NR010"), rule("NR012")), Rejection.SLOT),
        ((rule("NR011"), rule("NR012")), Rejection.SLOT),
        ((rule("NR011"), rule("NR011", "another")), Rejection.DUPLICATE),
        ((rule("NR040"),), Rejection.UNSUPPORTED),
        ((phrase("a", "one"), phrase("b", "two"), phrase("c", "three")), Rejection.CAP),
    ],
)
def test_mechanical_and_unsupported_states_are_rejected_before_witnesses(active, rejection):
    assert assess_state(active).rejection is rejection


def test_no_total_active_limit_or_blanket_typography_cap_is_added():
    active = tuple(
        rule(card)
        for card in (
            "NR004",
            "NR006",
            "NR007",
            "NR011",
            "NR013",
            "NR016",
            "NR017",
            "NR022",
            "NR025",
            "NR033",
            "NR034",
        )
    )
    verify_witness(active, assess_state(active))


def test_brevity_replacement_targets_only_the_brevity_instance():
    active = (rule("NR017"), rule("NR004"))
    results = legal_completions(active, rule("NR019", "incoming"))
    assert len(results) == 1 and results[0].outgoing_id == "NR017"
    assert {i.card_id for i in results[0].instances} == {"NR019", "NR004"}
    verify_witness(results[0].instances, assess_state(results[0].instances))


@pytest.mark.parametrize(
    "left,right,passing_text",
    [
        (rule("NR019"), rule("NR024"), "yes"),
        (rule("NR019"), rule("NR033"), "please"),
        (rule("NR019"), rule("NR034"), "over"),
        (rule("NR019"), rule("NR035"), "we"),
        (rule("NR019"), phrase("duck"), "duck"),
        (rule("NR010"), rule("NR013"), None),
        (rule("NR012"), rule("NR014"), None),
    ],
)
def test_reciprocal_direct_conflicts_replace_only_the_counterpart(left, right, passing_text):
    keep = rule("NR004")
    if passing_text:
        view = parse_message(passing_text)
        assert all(evaluate_text_rule(view, card).outcome is Outcome.PASS for card in (left, right))
    for current, incoming in ((left, right), (right, left)):
        assert assess_state((current, incoming)).rejection is Rejection.CONFLICT
        (completion,) = legal_completions((current, keep), incoming)
        assert completion.outgoing_id == current.instance_id
        assert completion.instances == (keep, incoming)


@pytest.mark.parametrize(
    "cards,text",
    [
        (("NR007", "NR013", "NR014"), "hello"),
        (("NR010", "NR014", "NR034"), "over?"),
        (("NR012", "NR013", "NR034"), "over!"),
    ],
)
def test_nonmatching_punctuation_rules_can_stack(cards, text):
    active = tuple(rule(card) for card in cards)
    result = assess_state(active)
    verify_witness(active, result)
    assert result.witness.text == text


def test_terminal_slot_and_direct_conflict_cannot_remove_two_cards():
    active = (rule("NR011"), rule("NR014"))
    assert assess_state(active).legal
    assert legal_completions(active, rule("NR012")) == ()


def test_slot_replacement_removes_the_required_prefix_not_unrelated_clauses():
    active = (rule("NR024"), phrase("red duck"), rule("NR034"))
    results = legal_completions(active, rule("NR033", "incoming"))
    assert [r.outgoing_id for r in results] == ["NR024"]
    assert results[0].witness.text == "please red duck over"


def test_full_vocabulary_deck_offers_each_legal_outgoing_without_extra_tickets():
    active = (rule("NR022"), rule("NR025"), rule("NR034"))
    results = legal_completions(active, rule("NR027", "incoming"))
    assert {r.outgoing_id for r in results} == {"NR022", "NR025"}
    assert all("NR034" in {i.instance_id for i in r.instances} for r in results)


def test_cross_deck_conflict_cannot_retire_an_unrelated_clause():
    active = (phrase("maybe duck"), rule("NR025"))
    assert legal_completions(active, rule("NR027", "incoming")) == ()


@pytest.mark.parametrize(
    "frozen,text",
    [
        ("please our ducks swim over", "please our ducks swim over"),
        ("red duck over", "please our red duck over"),
    ],
)
def test_royal_witness_reuses_frozen_words_and_preserves_the_ending(frozen, text):
    active = tuple(rule(card) for card in ("NR017", "NR022", "NR033", "NR034", "NR035"))
    active += (phrase(frozen),)
    result = assess_state(active)
    verify_witness(active, result)
    assert result.witness.text == text


@pytest.mark.parametrize(
    "cards",
    [
        ("NR008", "NR012", "NR035"),  # Public Address
        ("NR013", "NR028", "NR029"),  # No Further Questions
        ("NR013", "NR019", "NR028"),  # No Comment
    ],
)
def test_newly_reachable_theme_cores_have_actual_witnesses(cards):
    active = tuple(rule(card) for card in cards)
    verify_witness(active, assess_state(active))


def test_second_person_contraction_can_block_a_frozen_phrase():
    active = (phrase("you’re welcome"), rule("NR033"))
    assert assess_state(active).legal
    assert assess_state((*active, rule("NR031"))).rejection is Rejection.NO_WITNESS
    assert legal_completions(active, rule("NR031")) == ()


@pytest.mark.parametrize("card", ["NR018", "NR020"])
def test_minimum_brevity_witness_extends_chat_without_splitting_frozen_ending(card):
    active = tuple(rule(item) for item in (card, "NR022", "NR033", "NR034", "NR037", "NR038"))
    active += (phrase("duck over"),)
    result = assess_state(active)
    verify_witness(active, result)
    assert result.witness.text.startswith("Please, ")
    assert result.witness.text.endswith("Duck Over")


def test_small_print_counts_terminal_mark_and_comma_in_whole_state():
    forced = phrase("under the quiet bridge by the green pond")
    active = (rule("NR021"), forced)
    verify_witness(active, assess_state(active))
    for punctuation_card in ("NR010", "NR038"):
        assert assess_state((*active, rule(punctuation_card))).rejection is Rejection.NO_WITNESS
        assert legal_completions(active, rule(punctuation_card)) == ()


@pytest.mark.parametrize(("restriction", "text"), [("NR009", "duck ７"), ("NR039", "duckling")])
def test_brevity_or_typography_restriction_cannot_remove_an_unrelated_clause(restriction, text):
    active = (phrase(text),)
    assert assess_state(active).legal
    assert legal_completions(active, rule(restriction)) == ()


@pytest.mark.parametrize("current", ["NR007", "NR008"])
def test_decree_mode_reciprocally_replaces_the_other_casing_card(current):
    keep = rule("NR033")
    for old, new in ((current, "NR037"), ("NR037", current)):
        (completion,) = legal_completions((rule(old), keep), rule(new))
        assert completion.outgoing_id == old
        assert completion.instances == (keep, rule(new))


@pytest.mark.parametrize(
    "cards",
    [
        ("NR015", "NR017", "NR034"),  # Radio Discipline
        ("NR011", "NR025", "NR038"),  # Dramatic Pause
        ("NR009", "NR016", "NR039"),  # Fine Print
    ],
)
def test_new_typography_and_brevity_theme_cores_have_actual_witnesses(cards):
    active = tuple(rule(card) for card in cards)
    verify_witness(active, assess_state(active))


def test_each_phrase_replacement_checks_the_whole_result():
    active = (rule("NR017"), phrase("red duck pond", "long"), phrase("blue goose", "short"))
    assert assess_state(active).legal
    results = legal_completions(active, phrase("red duck pond grows", "incoming"))
    assert [r.outgoing_id for r in results] == ["short"]
    assert results[0].witness.text == "red duck pond grows"


@pytest.mark.parametrize(
    "active",
    [
        (phrase("red", "first"), phrase("duck", "second")),
        (rule("NR017"), rule("NR034"), rule("NR004")),
    ],
)
def test_no_supported_single_removal_can_clear_multiple_blockers(active):
    assert assess_state(active).legal
    assert legal_completions(active, rule("NR019", "incoming")) == ()


def test_search_budget_is_not_a_new_player_rule_or_a_truncated_proof():
    active = (phrase("one two three"),)
    limited = assess_state(active, budget=WitnessBudget(max_words=2))
    assert limited.rejection is Rejection.SEARCH_LIMIT and limited.witness is None
    verify_witness(active, assess_state(active))


def test_witness_and_frozen_parameters_are_not_in_representations():
    active = (phrase("PRIVATE_CANARY"),)
    result = assess_state(active)
    assert result.legal
    assert "PRIVATE_CANARY" not in repr(active) + repr(result) + repr(result.witness)
    assert result.witness.text not in repr(result) + repr(result.witness)


@pytest.mark.parametrize("kwargs", [{"max_words": 0}, {"max_words": True}, {"max_candidates": 513}])
def test_witness_bounds_are_validated(kwargs):
    with pytest.raises(ValueError):
        WitnessBudget(**kwargs)
