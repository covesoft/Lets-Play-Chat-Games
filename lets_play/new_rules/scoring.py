"""Exact additive score values. No event deduplication, persistence, or awards."""

from __future__ import annotations

from dataclasses import dataclass, fields
from fractions import Fraction

from lets_play.new_rules.contracts import Outcome, RuleEvaluation

SCORE_VERSION = "normalized-1"


def _exact(value: int | Fraction) -> Fraction:
    if type(value) is not int and not isinstance(value, Fraction):
        raise ValueError("Score arithmetic requires integers or Fractions, never floats.")
    return Fraction(value)


@dataclass(frozen=True, slots=True)
class RankingModel:
    strength: Fraction = Fraction(10)
    baseline: Fraction = Fraction(9, 10)

    def __post_init__(self) -> None:
        object.__setattr__(self, "strength", _exact(self.strength))
        object.__setattr__(self, "baseline", _exact(self.baseline))
        if self.strength <= 0 or not 0 <= self.baseline <= 1:
            raise ValueError("Ranking needs positive strength and a baseline in [0, 1].")


DEFAULT_RANKING = RankingModel()


@dataclass(frozen=True, slots=True)
class ScoreTotals:
    """Sum these fields across events/rounds; never average percentages.

    Human traffic includes whispers and applicable/non-applicable mode posts.
    Only ordinary posts contribute ordinary participation. Raw faults and one
    marker per offending message remain separate from observation quality.
    """

    human_messages: int = 0
    whispers: int = 0
    ordinary_posts: int = 0
    ordinary_observations: int = 0
    ordinary_quality: Fraction = Fraction(0)
    ordinary_faults: int = 0
    ordinary_offending_messages: int = 0
    mode_observations: int = 0
    mode_quality: Fraction = Fraction(0)
    reaction_failures: int = 0

    def __post_init__(self) -> None:
        for name in ("ordinary_quality", "mode_quality"):
            object.__setattr__(self, name, _exact(getattr(self, name)))
        for item in fields(self):
            if item.name.endswith("quality"):
                continue
            value = getattr(self, item.name)
            if type(value) is not int or value < 0:
                raise ValueError("Score counters must be nonnegative integers.")
        if not 0 <= self.ordinary_quality <= self.ordinary_observations <= self.ordinary_posts:
            raise ValueError("Ordinary quality/observations must fit ordinary participation.")
        if (
            not 0 <= self.mode_quality <= self.mode_observations
            or self.mode_quality.denominator != 1
        ):
            raise ValueError("Mode observations have binary quality.")
        if self.human_messages < self.whispers + self.ordinary_posts + self.mode_observations:
            raise ValueError("Message categories cannot exceed human traffic.")
        bad = self.ordinary_offending_messages
        if not 0 <= bad <= self.ordinary_observations or self.ordinary_faults < bad:
            raise ValueError("Ordinary faults must account for offending observations.")
        if bool(bad) != bool(self.ordinary_faults):
            raise ValueError("Ordinary faults and offending messages must agree.")
        if self.ordinary_quality < self.ordinary_observations - bad or (
            bool(bad) != (self.ordinary_quality < self.ordinary_observations)
        ):
            raise ValueError("Ordinary quality must agree with offending observations.")

    def __add__(self, other: object) -> ScoreTotals:
        if not isinstance(other, ScoreTotals):
            return NotImplemented
        return ScoreTotals(
            **{
                item.name: getattr(self, item.name) + getattr(other, item.name)
                for item in fields(self)
            }
        )

    @property
    def quality(self) -> Fraction:
        return self.ordinary_quality + self.mode_quality

    @property
    def observations(self) -> int:
        return self.ordinary_observations + self.mode_observations

    @property
    def opportunities(self) -> int:
        return self.observations + self.reaction_failures

    @property
    def mode_faults(self) -> int:
        return self.mode_observations - self.mode_quality.numerator

    @property
    def raw_faults(self) -> int:
        return self.ordinary_faults + self.mode_faults + self.reaction_failures

    @property
    def offending_messages(self) -> int:
        return self.ordinary_offending_messages + self.mode_faults

    @property
    def actual_percent(self) -> Fraction | None:
        if not self.opportunities:
            return None
        return 100 * self.quality / self.opportunities

    def ranking_percent(self, model: RankingModel = DEFAULT_RANKING) -> Fraction | None:
        if not self.opportunities:
            return None
        return (
            100
            * (self.quality + model.strength * model.baseline)
            / (self.opportunities + model.strength)
        )


def ordinary_score(evaluations: tuple[RuleEvaluation, ...]) -> ScoreTotals:
    ids = [evaluation.rule_instance_id for evaluation in evaluations]
    if len(ids) != len(set(ids)):
        raise ValueError("Each active instance may be evaluated only once per message.")
    applicable = sum(e.outcome is not Outcome.NOT_APPLICABLE for e in evaluations)
    failed = sum(e.outcome is Outcome.FAIL for e in evaluations)
    return ScoreTotals(
        human_messages=1,
        ordinary_posts=1,
        ordinary_observations=int(bool(applicable)),
        ordinary_quality=Fraction(applicable - failed, applicable) if applicable else Fraction(0),
        ordinary_faults=failed,
        ordinary_offending_messages=int(bool(failed)),
    )


def whisper_score() -> ScoreTotals:
    return ScoreTotals(human_messages=1, whispers=1)


def mode_score(evaluation: RuleEvaluation) -> ScoreTotals:
    """Arithmetic for a supplied mode result, not an implemented mode validator."""
    return ScoreTotals(
        human_messages=1,
        mode_observations=int(evaluation.outcome is not Outcome.NOT_APPLICABLE),
        mode_quality=Fraction(int(evaluation.outcome is Outcome.PASS)),
    )


def reaction_score() -> ScoreTotals:
    """One candidate failed opportunity; the store must accept its unique key first."""
    return ScoreTotals(reaction_failures=1)
