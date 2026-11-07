"""Pure ordinary-subset offers and revalidation; no publication or activation."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.compatibility import (
    DEFAULT_WITNESS_BUDGET,
    LegalCompletion,
    WitnessBudget,
    assess_state,
    legal_completions,
)
from lets_play.new_rules.contracts import (
    Deadline,
    DeadlineKind,
    Lifecycle,
    Phase,
    RandomSource,
    RuleInstance,
    SessionPosition,
)
from lets_play.new_rules.evaluation import EVALUATOR_VERSION, EvaluationContext
from lets_play.new_rules.parsing import PARSER_VERSION, parse_message
from lets_play.new_rules.text_rules import RuleConfigurationError, phrase_keys


@dataclass(frozen=True, slots=True)
class PhraseChoice:
    phrase_id: str
    text: str = field(repr=False)
    revision: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.phrase_id, str)
            or not self.phrase_id
            or not isinstance(self.revision, str)
            or not self.revision
        ):
            raise RuleConfigurationError("A phrase identity and revision are required.")
        phrase_keys(self.instantiate("phrase-validation"))

    def instantiate(self, instance_id: str) -> RuleInstance:
        return RuleInstance(
            instance_id, "NR036", (("phrase", self.text), ("phrase_revision", self.revision))
        )

    @property
    def key(self) -> tuple[str, ...]:
        return phrase_keys(self.instantiate("phrase-key"))


@dataclass(frozen=True, slots=True)
class HandOption:
    incoming: RuleInstance
    completions: tuple[LegalCompletion, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.incoming, RuleInstance)
            or not isinstance(self.completions, tuple)
            or not self.completions
        ):
            raise ValueError("An option needs a frozen incoming rule and immutable completions.")
        targets = []
        for item in self.completions:
            if (
                not isinstance(item, LegalCompletion)
                or not isinstance(item.instances, tuple)
                or item.instances.count(self.incoming) != 1
            ):
                raise ValueError("Each completion must contain its frozen incoming instance once.")
            targets.append(item.outgoing_id)
        if len(set(targets)) != len(targets) or (None in targets and len(targets) != 1):
            raise ValueError("An option has unique replacement targets or one addition.")

    @property
    def outgoing_ids(self) -> tuple[str, ...]:
        return tuple(item.outgoing_id for item in self.completions if item.outgoing_id is not None)

    @property
    def is_replacement(self) -> bool:
        return bool(self.outgoing_ids)


@dataclass(frozen=True, slots=True)
class OrdinaryHand:
    offer_id: str
    picker_id: int
    owner: SessionPosition
    active_instances: tuple[RuleInstance, ...]
    deadline: Deadline
    options: tuple[HandOption, HandOption, HandOption]
    evaluator_version: str = EVALUATOR_VERSION
    parser_version: str = PARSER_VERSION

    def __post_init__(self) -> None:
        if not self.offer_id or self.deadline.kind is not DeadlineKind.CHOICE:
            raise ValueError("A hand requires an offer identity and original choice deadline.")
        if type(self.picker_id) is not int or self.picker_id <= 0:
            raise ValueError("A hand requires a positive authorized picker ID.")
        if not isinstance(self.active_instances, tuple) or not all(
            isinstance(i, RuleInstance) for i in self.active_instances
        ):
            raise ValueError("A hand requires an immutable active-instance snapshot.")
        if (
            not isinstance(self.options, tuple)
            or len(self.options) != 3
            or any(
                not isinstance(option, HandOption) or not option.completions
                for option in self.options
            )
        ):
            raise ValueError("A hand requires exactly three completable options.")
        if (
            len({o.incoming.card_id for o in self.options}) != 3
            or len({o.incoming.instance_id for o in self.options}) != 3
        ):
            raise ValueError("Hand templates and incoming instance IDs must be distinct.")


class BlockedReason(StrEnum):
    WRONG_POSITION = "wrong_position"
    CURRENT_STATE = "current_state_unproven"
    TOO_FEW = "fewer_than_three_templates"


@dataclass(frozen=True, slots=True)
class HandBuild:
    hand: OrdinaryHand | None = None
    blocked: BlockedReason | None = None
    eligible_templates: tuple[str, ...] = ()


def _available(templates: frozenset[str] | None) -> frozenset[str]:
    if templates is None:
        return frozenset(CARD_SPECS)
    if not isinstance(templates, frozenset) or not templates <= CARD_SPECS.keys():
        raise RuleConfigurationError(
            "Availability must contain only implemented ordinary templates."
        )
    return templates


def _used(
    keys: frozenset[tuple[str, ...]], active: tuple[RuleInstance, ...]
) -> frozenset[tuple[str, ...]]:
    if not isinstance(keys, frozenset) or any(
        not isinstance(key, tuple)
        or not key
        or any(not isinstance(word, str) or not word for word in key)
        for key in keys
    ):
        raise RuleConfigurationError("Used phrases must be frozen nonempty word-identity tuples.")
    if any(parse_message(" ".join(key)).word_keys != key for key in keys):
        raise RuleConfigurationError("Used phrase identities must use the shared normalized words.")
    return keys | frozenset(phrase_keys(i) for i in active if i.card_id == "NR036")


def build_hand(
    context: EvaluationContext,
    *,
    offer_id: str,
    picker_id: int,
    deadline: Deadline,
    rng: RandomSource,
    phrase_bank: tuple[PhraseChoice, ...] = (),
    phrase_draw_limit: int = 5,
    used_phrases: frozenset[tuple[str, ...]] = frozenset(),
    available_templates: frozenset[str] | None = None,
    budget: WitnessBudget = DEFAULT_WITNESS_BUDGET,
) -> HandBuild:
    """One ticket per template; freeze an eligible phrase only after template draw.

    The caller supplies an already authorized session draw limit (5 or 15).
    This does not implement entitlements, manager suitability or bank persistence.
    """
    if not offer_id or deadline.kind is not DeadlineKind.CHOICE:
        raise ValueError("A stable offer identity and original choice deadline are required.")
    if type(picker_id) is not int or picker_id <= 0:
        raise ValueError("A positive authorized picker ID is required.")
    if context.position.offer_id is not None and context.position.offer_id != offer_id:
        raise ValueError("The offer identity must match its supplied owner.")
    available = _available(available_templates)
    if (
        not isinstance(phrase_bank, tuple)
        or len(phrase_bank) > 15
        or not all(isinstance(p, PhraseChoice) for p in phrase_bank)
    ):
        raise RuleConfigurationError(
            "The ordered phrase-bank snapshot must contain at most 15 choices."
        )
    if type(phrase_draw_limit) is not int or phrase_draw_limit not in (5, 15):
        raise ValueError("A session phrase window is 5 or 15 positions.")
    if len({p.phrase_id for p in phrase_bank}) != len(phrase_bank) or len(
        {p.key for p in phrase_bank}
    ) != len(phrase_bank):
        raise RuleConfigurationError(
            "Phrase identities and normalized requirements must be unique."
        )
    used = _used(used_phrases, context.active_instances)
    p = context.position
    if p.phase is not Phase.ORDINARY or p.lifecycle not in (
        Lifecycle.OPENING_CHOICE,
        Lifecycle.PLAYING,
    ):
        return HandBuild(blocked=BlockedReason.WRONG_POSITION)
    if p.lifecycle is Lifecycle.OPENING_CHOICE and context.active_instances:
        return HandBuild(blocked=BlockedReason.WRONG_POSITION)
    if not assess_state(context.active_instances, budget=budget).legal:
        return HandBuild(blocked=BlockedReason.CURRENT_STATE)
    # Slice BEFORE used/incompatible filtering: never replenish from later entries.
    phrases = tuple(p for p in phrase_bank[:phrase_draw_limit] if p.key not in used)
    pool: list[tuple[str, tuple[HandOption, ...]]] = []
    for card in sorted(available):
        instance_id = f"{offer_id}:{card}"
        incoming = (
            tuple(p.instantiate(instance_id) for p in phrases)
            if card == "NR036"
            else (RuleInstance(instance_id, card),)
        )
        variants = []
        for instance in incoming:
            completions = legal_completions(context.active_instances, instance, budget=budget)
            if completions:
                variants.append(HandOption(instance, completions))
        if variants:
            pool.append((card, tuple(variants)))
    eligible = tuple(card for card, _ in pool)
    if len(pool) < 3:
        return HandBuild(blocked=BlockedReason.TOO_FEW, eligible_templates=eligible)
    selected = []
    for _ in range(3):
        _, variants = pool.pop(rng.randrange(len(pool)))
        # More phrases/targets do not create extra template tickets.
        index = rng.randrange(len(variants)) if len(variants) > 1 else 0
        selected.append(variants[index])
    return HandBuild(
        hand=OrdinaryHand(
            offer_id, picker_id, p, context.active_instances, deadline, tuple(selected)
        ),
        eligible_templates=eligible,
    )


class ChoiceStatus(StrEnum):
    READY = "ready"
    NEEDS_TARGET = "needs_target"
    STALE = "stale_offer"
    EXPIRED = "expired"
    INVALID = "invalid_choice"
    UNAVAILABLE = "unavailable"
    UNPROVEN = "result_unproven"
    UNAUTHORIZED = "unauthorized_picker"


@dataclass(frozen=True, slots=True)
class ChoiceResult:
    status: ChoiceStatus
    completion: LegalCompletion | None = field(default=None, repr=False)


def revalidate_choice(
    hand: OrdinaryHand,
    context: EvaluationContext,
    incoming_id: str,
    *,
    now: datetime,
    actor_id: int,
    outgoing_id: str | None = None,
    used_phrases: frozenset[tuple[str, ...]] = frozenset(),
    available_templates: frozenset[str] | None = None,
    budget: WitnessBudget = DEFAULT_WITNESS_BUDGET,
) -> ChoiceResult:
    """Pure preparation: never changes ownership, consumes a phrase or closes a round."""
    if type(actor_id) is not int or actor_id != hand.picker_id:
        return ChoiceResult(ChoiceStatus.UNAUTHORIZED)
    if (
        hand.evaluator_version != EVALUATOR_VERSION
        or hand.parser_version != PARSER_VERSION
        or hand.owner != context.position
        or hand.active_instances != context.active_instances
        or context.position.phase is not Phase.ORDINARY
        or context.position.lifecycle not in (Lifecycle.OPENING_CHOICE, Lifecycle.PLAYING)
        or (context.position.lifecycle is Lifecycle.OPENING_CHOICE and context.active_instances)
    ):
        return ChoiceResult(ChoiceStatus.STALE)
    if hand.deadline.is_due(now):
        return ChoiceResult(ChoiceStatus.EXPIRED)
    option = next((o for o in hand.options if o.incoming.instance_id == incoming_id), None)
    if option is None:
        return ChoiceResult(ChoiceStatus.INVALID)
    if option.incoming.card_id not in _available(available_templates):
        return ChoiceResult(ChoiceStatus.UNAVAILABLE)
    used = _used(used_phrases, context.active_instances)
    if option.incoming.card_id == "NR036" and phrase_keys(option.incoming) in used:
        return ChoiceResult(ChoiceStatus.UNAVAILABLE)
    if option.is_replacement:
        if outgoing_id is None:
            if len(option.outgoing_ids) > 1:
                return ChoiceResult(ChoiceStatus.NEEDS_TARGET)
            outgoing_id = option.outgoing_ids[0]
        if outgoing_id not in option.outgoing_ids:
            return ChoiceResult(ChoiceStatus.INVALID)
    elif outgoing_id is not None:
        return ChoiceResult(ChoiceStatus.INVALID)
    if not assess_state(context.active_instances, budget=budget).legal:
        return ChoiceResult(ChoiceStatus.UNPROVEN)
    # Recompute from the frozen incoming parameters and latest supplied inputs;
    # an old saved witness or new guild phrase bank is never activation authority.
    completions = legal_completions(context.active_instances, option.incoming, budget=budget)
    completion = next((c for c in completions if c.outgoing_id == outgoing_id), None)
    return (
        ChoiceResult(ChoiceStatus.READY, completion)
        if completion
        else ChoiceResult(ChoiceStatus.UNPROVEN)
    )
