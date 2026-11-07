"""Allowlisted live-state codecs. Never serialize a message/event object."""

import json
import sqlite3
from dataclasses import fields
from fractions import Fraction

from lets_play.new_rules.contracts import (
    Advancement,
    EmojiIdentity,
    Lifecycle,
    PauseReason,
    Phase,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EVALUATOR_VERSION, EvaluationContext
from lets_play.new_rules.parsing import PARSER_VERSION
from lets_play.new_rules.scoring import SCORE_VERSION, ScoreTotals

MAX_SQLITE_INTEGER = (1 << 63) - 1
SCORE_COLUMNS = tuple(
    column
    for item in fields(ScoreTotals)
    for column in (
        (item.name + "_n", item.name + "_d") if item.name.endswith("quality") else (item.name,)
    )
)


class IncompatibleState(sqlite3.DatabaseError):
    """Live state needs an explicit recovery policy, not silent reinterpretation."""


def encode_context(context: EvaluationContext) -> str:
    # Frozen phrases are intentional configuration snapshots, never chat text.
    return json.dumps(
        {
            "rules": [
                [item.instance_id, item.card_id, list(item.parameters)]
                for item in context.active_instances
            ],
            "exclusions": [[item.custom_id, item.unicode] for item in context.reaction_exclusions],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_context(row: sqlite3.Row) -> EvaluationContext:
    if (
        row["parser_version"] != PARSER_VERSION
        or row["evaluator_version"] != EVALUATOR_VERSION
        or row["score_version"] != SCORE_VERSION
    ):
        raise IncompatibleState("Live parser/evaluator/score version needs explicit recovery.")
    try:
        data = json.loads(row["context_json"])
        if set(data) != {"rules", "exclusions"}:
            raise ValueError
        position = SessionPosition(
            route=SessionRoute(row["guild_id"], row["channel_id"], row["session_id"]),
            lifecycle=Lifecycle(row["lifecycle"]),
            phase=Phase(row["phase"]),
            advancement=Advancement(row["advancement"]),
            round_id=row["round_id"],
            ruleset_version=row["ruleset_version"],
            offer_id=row["offer_id"],
            transition_id=row["transition_id"],
            pause_reason=PauseReason(row["pause_reason"]) if row["pause_reason"] else None,
            revision=row["revision"],
        )
        return EvaluationContext(
            position,
            tuple(
                RuleInstance(instance_id, card, tuple(tuple(pair) for pair in params))
                for instance_id, card, params in data["rules"]
            ),
            tuple(EmojiIdentity(custom, unicode) for custom, unicode in data["exclusions"]),
        )
    except (ValueError, TypeError, KeyError, AttributeError, NotImplementedError):
        raise IncompatibleState("Live context is invalid; explicit recovery is required.") from None


def encode_score(score: ScoreTotals) -> tuple[int | str, ...]:
    values = []
    for item in fields(score):
        value = getattr(score, item.name)
        if isinstance(value, Fraction):
            # Decimal TEXT preserves arbitrary exact numerators/denominators.
            # SQLite REAL and integer SUM/CAST are never used for quality.
            values.extend((str(value.numerator), str(value.denominator)))
        else:
            if value > MAX_SQLITE_INTEGER:
                raise OverflowError("A score counter exceeds SQLite's integer range.")
            values.append(value)
    return tuple(values)


def decode_score(row: sqlite3.Row) -> ScoreTotals:
    try:
        values = {}
        for item in fields(ScoreTotals):
            if item.name.endswith("quality"):
                n, d = row[item.name + "_n"], row[item.name + "_d"]
                value = Fraction(int(n), int(d))
                if str(value.numerator) != n or str(value.denominator) != d:
                    raise ValueError
                values[item.name] = value
            else:
                values[item.name] = row[item.name]
        return ScoreTotals(**values)
    except (ValueError, TypeError, ZeroDivisionError):
        raise IncompatibleState("Stored score is invalid; explicit recovery is required.") from None
