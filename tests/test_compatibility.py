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
from lets_play.new_rules.text_rules import evaluate_text_rule

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
        if instance.card_id not in {"NR004", "NR006"}:
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
        assert result.rejection in {Rejection.CAP, Rejection.SLOT, Rejection.NO_WITNESS}


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
        (rule("NR010"), rule("NR013")),
        (rule("NR025"), phrase("sorry duck")),
        (rule("NR027"), phrase("maybe duck")),
        (rule("NR022"), phrase("red red duck")),
        (rule("NR019"), rule("NR033"), rule("NR034")),
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
        ((rule("NR011"), rule("NR011", "another")), Rejection.DUPLICATE),
        ((rule("NR037"),), Rejection.UNSUPPORTED),
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
    active = (rule("NR017"), rule("NR034"), rule("NR004"))
    results = legal_completions(active, rule("NR019", "incoming"))
    assert len(results) == 1 and results[0].outgoing_id == "NR017"
    assert {i.card_id for i in results[0].instances} == {"NR019", "NR034", "NR004"}
    verify_witness(results[0].instances, assess_state(results[0].instances))


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


def test_each_phrase_replacement_checks_the_whole_result():
    active = (rule("NR017"), phrase("red duck pond", "long"), phrase("blue goose", "short"))
    assert assess_state(active).legal
    results = legal_completions(active, phrase("red duck pond grows", "incoming"))
    assert [r.outgoing_id for r in results] == ["short"]
    assert results[0].witness.text == "red duck pond grows"


def test_no_supported_single_removal_can_repair_two_required_phrase_conflicts():
    active = (rule("NR019"), phrase("duck", "clause"))
    assert assess_state(active).legal
    assert legal_completions(active, phrase("red duck", "incoming")) == ()


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
