"""Pure representative evaluation. Decisions are candidates, never committed effects.

The caller supplies the authoritative position and frozen active instances. A
future transactional store must deduplicate and apply derived scores together.
This module does not implement a full event router, session engine, or mode.
"""

from dataclasses import dataclass

from lets_play.new_rules.contracts import (
    EmojiIdentity,
    EventKind,
    Lifecycle,
    MessageEvent,
    Outcome,
    Phase,
    ReactionEvent,
    RuleEvaluation,
    RuleInstance,
    SessionPosition,
)
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.scoring import ScoreTotals, ordinary_score, reaction_score, whisper_score
from lets_play.new_rules.text_rules import (
    SUPPORTED_TEXT_RULES,
    TEXT_FAILURE_REASONS,
    RuleConfigurationError,
    evaluate_text_rule,
    phrase_keys,
)

SUPPORTED_RULES = SUPPORTED_TEXT_RULES | {"NR004", "NR006"}
EVALUATOR_VERSION = "representatives-1"
FAILURE_REASONS = TEXT_FAILURE_REASONS | {
    "NR004": "attachments_forbidden",
    "NR006": "reaction_prohibited",
}


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    position: SessionPosition
    active_instances: tuple[RuleInstance, ...]
    reaction_exclusions: tuple[EmojiIdentity, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.active_instances, tuple) or not all(
            isinstance(instance, RuleInstance) for instance in self.active_instances
        ):
            raise ValueError("Active instances must be an immutable tuple of rules.")
        if not isinstance(self.reaction_exclusions, tuple) or not all(
            isinstance(identity, EmojiIdentity) for identity in self.reaction_exclusions
        ):
            raise ValueError("Reaction exclusions must be an immutable tuple of emoji identities.")
        ids = [instance.instance_id for instance in self.active_instances]
        cards = [instance.card_id for instance in self.active_instances]
        if len(set(ids)) != len(ids):
            raise RuleConfigurationError("Active rule instance IDs must be unique.")
        if cards.count("NR036") > 2 or any(
            cards.count(card) > 1 for card in set(cards) - {"NR036"}
        ):
            raise RuleConfigurationError("Only the phrase card may have two active instances.")
        phrases = []
        for instance in self.active_instances:
            if instance.card_id not in SUPPORTED_RULES:
                raise NotImplementedError("An active card lacks a representative validator.")
            if instance.card_id == "NR036":
                phrases.append(phrase_keys(instance))
            elif instance.parameters:
                raise RuleConfigurationError("This representative card takes no parameters.")
        if len(set(phrases)) != len(phrases):
            raise RuleConfigurationError("Active phrase instances must require distinct wording.")


@dataclass(frozen=True, slots=True)
class MessageDecision:
    """Derived data only: no message content, attachments, or phrase parameters."""

    position: SessionPosition
    source_key: tuple[str, EventKind, str]
    actor_id: int
    evaluations: tuple[RuleEvaluation, ...]
    score: ScoreTotals

    @property
    def enroll(self) -> bool:
        return bool(self.score.ordinary_posts)

    @property
    def needs_marker(self) -> bool:
        return bool(self.score.ordinary_offending_messages)


@dataclass(frozen=True, slots=True)
class ReactionDecision:
    position: SessionPosition
    source_key: tuple[str, EventKind, str]
    actor_id: int
    dedup_key: tuple[str, int, int]
    evaluation: RuleEvaluation
    score: ScoreTotals

    @property
    def enroll(self) -> bool:
        return True

    @property
    def needs_marker(self) -> bool:
        return False


def evaluate_message(event: MessageEvent, context: EvaluationContext) -> MessageDecision | None:
    position = context.position
    if (
        event.envelope.route != position.route
        or position.lifecycle is not Lifecycle.PLAYING
        or not event.is_human
    ):
        return None
    # Exempt the whole received event before parsing, including mode traffic and
    # attachments. Oversize whispers need no unnecessary parser work.
    if event.content is not None and event.content.startswith("-# "):
        evaluations, score = (), whisper_score()
    else:
        if position.phase is not Phase.ORDINARY:
            raise NotImplementedError("Non-whisper mode messages need a mode validator.")
        view = parse_message(event.content)
        results = []
        for instance in context.active_instances:
            match instance.card_id:
                case "NR006":
                    result = RuleEvaluation(instance.instance_id, Outcome.NOT_APPLICABLE)
                case "NR004":
                    # This prohibition is applicable to every ordinary post;
                    # absence of a file is actual compliance, even without text.
                    failed = bool(event.attachments)
                    result = RuleEvaluation(
                        instance.instance_id,
                        Outcome.FAIL if failed else Outcome.PASS,
                        FAILURE_REASONS[instance.card_id] if failed else None,
                    )
                case _:
                    result = evaluate_text_rule(view, instance)
            results.append(result)
        evaluations = tuple(results)
        score = ordinary_score(evaluations)
    return MessageDecision(
        position, event.envelope.source_key, event.envelope.actor_id, evaluations, score
    )


def evaluate_reaction(event: ReactionEvent, context: EvaluationContext) -> ReactionDecision | None:
    position = context.position
    if (
        event.envelope.route != position.route
        or position.lifecycle is not Lifecycle.PLAYING
        or position.phase is not Phase.ORDINARY
        or event.is_bot
    ):
        return None
    if event.emoji.key in {identity.key for identity in context.reaction_exclusions}:
        return None
    instance = next((i for i in context.active_instances if i.card_id == "NR006"), None)
    if instance is None:
        return None
    # No target lookup and no seen-set mutation. Re-adds, different emoji, and
    # later rounds produce the SAME candidate key. Only atomic persistence can
    # accept its first application. Excluded/pre-rule actions consume nothing.
    return ReactionDecision(
        position,
        event.envelope.source_key,
        event.envelope.actor_id,
        event.offense_key,
        RuleEvaluation(instance.instance_id, Outcome.FAIL, FAILURE_REASONS[instance.card_id]),
        reaction_score(),
    )
