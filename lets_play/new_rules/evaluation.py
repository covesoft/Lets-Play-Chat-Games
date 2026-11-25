"""Pure representative evaluation. Decisions are candidates, never committed effects.

The caller supplies the authoritative position and frozen active instances. A
future transactional store must deduplicate and apply derived scores together.
The flow supplies the saved Breaking Moment choice when one owns enforcement.
"""

from dataclasses import dataclass, field

from lets_play.new_rules.breaking_moments import (
    MODE_VALIDATORS,
    MOMENT_OF_SILENCE,
    PLAYING_TELEPHONE,
    ModeChoice,
    counts_for_very_vocal,
)
from lets_play.new_rules.catalog import CHATGAMES_MAX_ACTIVE_DONT_FORGET
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
from lets_play.new_rules.gif_media import is_gif_attachment, is_gif_link
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.profanity import ProfanityList, list_available, list_revision
from lets_play.new_rules.scoring import (
    ScoreTotals,
    mode_score,
    ordinary_score,
    reaction_score,
    whisper_score,
)
from lets_play.new_rules.text_rules import (
    SUPPORTED_TEXT_RULES,
    TEXT_FAILURE_REASONS,
    RuleConfigurationError,
    evaluate_text_rule,
    phrase_keys,
)

SUPPORTED_RULES = SUPPORTED_TEXT_RULES | {"NR002", "NR003", "NR004", "NR006", "NR040"}
EVALUATOR_VERSION = "ordinary-subset-2"
FAILURE_REASONS = TEXT_FAILURE_REASONS | {
    "NR002": "gif_forbidden",
    "NR003": "stickers_forbidden",
    "NR004": "attachments_forbidden",
    "NR006": "reaction_prohibited",
    "NR040": "reply_required",
}


@dataclass(frozen=True, slots=True)
class EvaluationContext:
    position: SessionPosition
    active_instances: tuple[RuleInstance, ...]
    reaction_exclusions: tuple[EmojiIdentity, ...] = ()
    profanity: ProfanityList | None = field(default=None, repr=False, compare=False, kw_only=True)

    @property
    def dependencies_available(self) -> bool:
        return list_available(self.active_instances, self.profanity)

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
        if cards.count("NR036") > CHATGAMES_MAX_ACTIVE_DONT_FORGET or any(
            cards.count(card) > 1 for card in set(cards) - {"NR036"}
        ):
            raise RuleConfigurationError("Only the phrase card may have two active instances.")
        phrases = []
        for instance in self.active_instances:
            if instance.card_id not in SUPPORTED_RULES:
                raise NotImplementedError("An active card lacks a representative validator.")
            if instance.card_id == "NR036":
                phrases.append(phrase_keys(instance))
            elif instance.card_id == "NR032":
                if list_revision(instance) is None:
                    raise RuleConfigurationError(
                        "Mind Your Language needs a valid frozen list revision."
                    )
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
    qualifying_voice_memo: bool = field(default=False, kw_only=True)

    @property
    def enroll(self) -> bool:
        return bool(self.score.ordinary_posts or self.score.mode_observations)

    @property
    def needs_marker(self) -> bool:
        return bool(self.score.offending_messages)


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


def evaluate_message(
    event: MessageEvent, context: EvaluationContext, *, mode: ModeChoice | None = None
) -> MessageDecision | None:
    position = context.position
    if (
        event.envelope.route != position.route
        or position.lifecycle is not Lifecycle.PLAYING
        or not event.is_human
    ):
        return None
    if not context.dependencies_available:
        raise RuleConfigurationError("The frozen private gameplay list is unavailable.")
    is_whisper = event.content is not None and event.content.startswith("-# ")
    qualifying_voice_memo = False
    if (
        position.phase is Phase.MODE
        and mode is not None
        and mode.card_id == MOMENT_OF_SILENCE
        and (is_whisper or event.is_native_poll)
    ):
        return None
    # Exempt the whole received event before parsing, including mode traffic and
    # attachments. Oversize whispers need no unnecessary parser work.
    if is_whisper:
        evaluations, score = (), whisper_score()
    elif position.phase is Phase.MODE:
        if mode is None:
            raise NotImplementedError("Non-whisper mode messages need a mode validator.")
        evaluation = MODE_VALIDATORS[mode.card_id](event, mode)
        evaluations, score = (evaluation,), mode_score(evaluation)
        if mode.card_id == PLAYING_TELEPHONE:
            qualifying_voice_memo = counts_for_very_vocal(event, evaluation)
    else:
        view = parse_message(event.content)
        results = []
        for instance in context.active_instances:
            match instance.card_id:
                case "NR006":
                    result = RuleEvaluation(instance.instance_id, Outcome.NOT_APPLICABLE)
                case "NR002" | "NR003" | "NR004" | "NR040":
                    # These cards apply to every ordinary post, including posts without text.
                    match instance.card_id:
                        case "NR002":
                            failed = any(
                                is_gif_attachment(item) for item in event.attachments
                            ) or any(is_gif_link(link.destination) for link in view.shared_links)
                        case "NR003":
                            failed = bool(event.sticker_ids)
                        case "NR004":
                            failed = bool(event.attachments)
                        case _:
                            failed = not event.is_reply
                    result = RuleEvaluation(
                        instance.instance_id,
                        Outcome.FAIL if failed else Outcome.PASS,
                        FAILURE_REASONS[instance.card_id] if failed else None,
                    )
                case _:
                    result = evaluate_text_rule(
                        view,
                        instance,
                        profanity=context.profanity,
                        allow_single_gif=not (
                            event.attachments or event.sticker_ids or event.is_voice_message
                        ),
                    )
            results.append(result)
        evaluations = tuple(results)
        score = ordinary_score(evaluations)
    return MessageDecision(
        position,
        event.envelope.source_key,
        event.envelope.actor_id,
        evaluations,
        score,
        qualifying_voice_memo=qualifying_voice_memo,
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
    if not context.dependencies_available:
        raise RuleConfigurationError("The frozen private gameplay list is unavailable.")
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
