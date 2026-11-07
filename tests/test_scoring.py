"""Exact arithmetic examples, independent of validators or a persistent ledger."""

from fractions import Fraction

import pytest

from lets_play.new_rules.contracts import Outcome, RuleEvaluation
from lets_play.new_rules.scoring import (
    RankingModel,
    ScoreTotals,
    mode_score,
    ordinary_score,
    reaction_score,
    whisper_score,
)


def result(index, outcome):
    return RuleEvaluation(
        str(index), outcome, "example_failure" if outcome is Outcome.FAIL else None
    )


def ordinary(*outcomes):
    return ordinary_score(tuple(result(index, outcome) for index, outcome in enumerate(outcomes)))


def repeated(value, count):
    total = ScoreTotals()
    for _ in range(count):
        total += value
    return total


def test_two_of_five_failures_are_one_fractional_observation_and_two_raw_faults():
    score = ordinary(Outcome.PASS, Outcome.FAIL, Outcome.PASS, Outcome.FAIL, Outcome.PASS)
    assert score.ordinary_posts == score.observations == score.opportunities == 1
    assert score.ordinary_quality == Fraction(3, 5)
    assert score.raw_faults == 2
    assert score.offending_messages == 1
    assert score.actual_percent == 60  # Faults are not subtracted again.
    assert score.ranking_percent() == Fraction(960, 11)


@pytest.mark.parametrize("outcomes", [(), (Outcome.NOT_APPLICABLE,)])
def test_no_applicable_rule_counts_participation_but_has_no_accuracy(outcomes):
    score = ordinary(*outcomes)
    assert score.ordinary_posts == score.human_messages == 1
    assert score.observations == score.opportunities == score.raw_faults == 0
    assert score.actual_percent is None
    assert score.ranking_percent() is None


def test_not_applicable_rule_does_not_dilute_a_failure():
    score = ordinary(Outcome.NOT_APPLICABLE, Outcome.FAIL)
    assert score.ordinary_quality == 0
    assert score.actual_percent == 0


def test_whisper_is_only_human_traffic():
    score = whisper_score()
    assert score.human_messages == score.whispers == 1
    assert score.ordinary_posts == score.observations == score.raw_faults == 0
    assert score.actual_percent is None


def test_reaction_only_player_is_rated_with_no_ordinary_participation_or_markers():
    score = repeated(reaction_score(), 3)
    assert score.reaction_failures == score.raw_faults == score.opportunities == 3
    assert score.actual_percent == 0
    assert score.ranking_percent() == Fraction(900, 13)
    assert score.ordinary_posts == score.human_messages == score.offending_messages == 0


def test_five_perfect_messages_and_one_reaction_are_six_opportunities():
    score = repeated(ordinary(Outcome.PASS), 5) + reaction_score()
    assert score.actual_percent == Fraction(250, 3)
    assert score.ranking_percent() == Fraction(175, 2)
    assert score.ordinary_posts == 5
    assert score.reaction_failures == score.raw_faults == 1


def test_high_volume_nearly_perfect_player_outranks_five_perfect_messages():
    large = repeated(ordinary(Outcome.PASS), 98)
    large += repeated(ordinary(Outcome.FAIL), 2)
    large += repeated(reaction_score(), 2)
    small = repeated(ordinary(Outcome.PASS), 5)
    assert large.actual_percent == Fraction(4900, 51)
    assert large.ranking_percent() == Fraction(2675, 28)
    assert small.ranking_percent() == Fraction(280, 3)
    assert large.ranking_percent() > small.ranking_percent()


def test_modes_share_accuracy_but_never_create_ordinary_participation():
    score = repeated(mode_score(result("mode", Outcome.PASS)), 20)
    score += repeated(ordinary(Outcome.PASS), 5)
    assert score.observations == score.opportunities == 25
    assert score.mode_observations == 20
    assert score.ordinary_posts == 5
    assert score.actual_percent == 100
    failed = mode_score(result("mode", Outcome.FAIL))
    assert failed.mode_faults == failed.raw_faults == failed.offending_messages == 1
    assert failed.ordinary_posts == 0
    absent = mode_score(result("mode", Outcome.NOT_APPLICABLE))
    assert absent.human_messages == 1
    assert absent.opportunities == 0


def test_rounds_combine_by_summed_quality_and_opportunities_not_mean_percentage():
    first = ordinary(Outcome.FAIL)
    second = repeated(ordinary(Outcome.PASS), 9)
    assert (first + second).actual_percent == 90
    assert (first + second).actual_percent != (first.actual_percent + second.actual_percent) / 2
    assert (first + second) + reaction_score() == first + (second + reaction_score())


def test_threshold_comparison_uses_actual_exact_accuracy():
    boundary = repeated(ordinary(Outcome.PASS), 19) + ordinary(Outcome.FAIL)
    assert boundary.actual_percent == 95
    assert boundary.ranking_percent() < 95
    assert (boundary + reaction_score()).actual_percent < 95


def test_ranking_model_is_exact_and_unrated_players_stay_unrated():
    model = RankingModel(strength=2, baseline=Fraction(1, 2))
    assert ordinary(Outcome.PASS).ranking_percent(model) == Fraction(200, 3)
    assert ScoreTotals().ranking_percent(model) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"strength": 0},
        {"strength": -1},
        {"baseline": -1},
        {"baseline": 2},
        {"baseline": 0.9},
        {"strength": True},
    ],
)
def test_invalid_or_approximate_ranking_inputs_rejected(kwargs):
    with pytest.raises(ValueError):
        RankingModel(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"ordinary_quality": 0.5},
        {"reaction_failures": True},
        {"ordinary_posts": -1},
        {"ordinary_observations": 1},
        {"whispers": 1},
        {"ordinary_faults": 1},
        {"human_messages": 1, "ordinary_posts": 1, "ordinary_observations": 1},
        {"human_messages": 1, "mode_observations": 1, "mode_quality": Fraction(1, 2)},
    ],
)
def test_inconsistent_or_approximate_totals_are_rejected(kwargs):
    with pytest.raises(ValueError):
        ScoreTotals(**kwargs)


def test_duplicate_instance_result_cannot_inflate_an_observation():
    entry = result("same-instance", Outcome.PASS)
    with pytest.raises(ValueError):
        ordinary_score((entry, entry))
