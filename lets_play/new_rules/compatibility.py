"""Bounded ordinary-subset witnesses; no SAT, draws, storage, or Discord I/O."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import permutations

from lets_play.new_rules.catalog import (
    CARD_SPECS,
    CHATGAMES_MAX_ACTIVE_BREVITY_RULES,
    CHATGAMES_MAX_ACTIVE_DONT_FORGET,
    CHATGAMES_MAX_ACTIVE_VOCABULARY_RULES,
    Deck,
)
from lets_play.new_rules.contracts import (
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    Outcome,
    RuleEvaluation,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.parsing import MAX_PARSER_CODEPOINTS, ParserLimitError, parse_message
from lets_play.new_rules.text_rules import RuleConfigurationError, phrase_keys


class Rejection(StrEnum):
    UNSUPPORTED = "unsupported_rule"
    INVALID = "invalid_instances"
    DUPLICATE = "duplicate_template"
    CAP = "mechanical_cap"
    SLOT = "exclusive_slot"
    NO_WITNESS = "no_witness"
    SEARCH_LIMIT = "witness_search_limit"


@dataclass(frozen=True, slots=True)
class WitnessBudget:
    max_words: int = 256
    max_candidates: int = 128

    def __post_init__(self) -> None:
        if type(self.max_words) is not int or not 1 <= self.max_words <= 1024:
            raise ValueError("Witness word budget must be in [1, 1024].")
        if type(self.max_candidates) is not int or not 1 <= self.max_candidates <= 512:
            raise ValueError("Witness candidate budget must be in [1, 512].")


DEFAULT_WITNESS_BUDGET = WitnessBudget()


@dataclass(frozen=True, slots=True)
class Witness:
    text: str = field(repr=False)
    evaluations: tuple[RuleEvaluation, ...]


@dataclass(frozen=True, slots=True)
class Assessment:
    witness: Witness | None = field(default=None, repr=False)
    rejection: Rejection | None = None

    @property
    def legal(self) -> bool:
        return self.witness is not None and self.rejection is None


_ROUTE = SessionRoute(1, 1, "witness")
_POSITION = SessionPosition(_ROUTE, Lifecycle.PLAYING, round_id="witness")
_TIME = datetime(2000, 1, 1, tzinfo=UTC)


def inventory_rejection(instances: tuple[RuleInstance, ...]) -> Rejection | None:
    if not isinstance(instances, tuple) or not all(isinstance(i, RuleInstance) for i in instances):
        return Rejection.INVALID
    if any(i.card_id not in CARD_SPECS for i in instances):
        return Rejection.UNSUPPORTED
    if len({i.instance_id for i in instances}) != len(instances):
        return Rejection.INVALID
    cards = [i.card_id for i in instances]
    if any(cards.count(card) > 1 for card in set(cards) - {"NR036"}):
        return Rejection.DUPLICATE
    if cards.count("NR036") > CHATGAMES_MAX_ACTIVE_DONT_FORGET:
        return Rejection.CAP
    for deck, cap in (
        (Deck.BREVITY, CHATGAMES_MAX_ACTIVE_BREVITY_RULES),
        (Deck.VOCABULARY, CHATGAMES_MAX_ACTIVE_VOCABULARY_RULES),
    ):
        if sum(CARD_SPECS[i.card_id].deck is deck for i in instances) > cap:
            return Rejection.CAP
    slots = [CARD_SPECS[i.card_id].slot for i in instances if CARD_SPECS[i.card_id].slot]
    if len(set(slots)) != len(slots):
        return Rejection.SLOT
    return None


def _extensions(left: tuple[str, ...], right: tuple[str, ...]):
    # Include containment, overlap, and concatenation. A longer alternative can
    # matter when a required prefix/ending would otherwise sit in the middle.
    if any(
        left[start : start + len(right)] == right for start in range(len(left) - len(right) + 1)
    ):
        yield left
    for size in range(min(len(left), len(right)), -1, -1):
        if size == 0 or left[-size:] == right[:size]:
            yield left + right[size:]


def assess_state(
    instances: tuple[RuleInstance, ...], *, budget: WitnessBudget = DEFAULT_WITNESS_BUDGET
) -> Assessment:
    rejection = inventory_rejection(instances)
    if rejection:
        return Assessment(rejection=rejection)
    try:
        context = EvaluationContext(_POSITION, instances)
        phrases = tuple(phrase_keys(i) for i in instances if i.card_id == "NR036")
    except ParserLimitError:
        return Assessment(rejection=Rejection.SEARCH_LIMIT)
    except (ValueError, NotImplementedError):
        return Assessment(rejection=Rejection.INVALID)
    if any(len(phrase) > budget.max_words for phrase in phrases):
        return Assessment(rejection=Rejection.SEARCH_LIMIT)
    cards = {i.card_id for i in instances}
    prefix = ("yes",) if "NR024" in cards else ("please",) if "NR033" in cards else ()
    suffix = ("over",) if "NR034" in cards else ()
    punctuation = "..." if "NR011" in cards else "?" if "NR010" in cards else ""
    candidates: set[tuple[str, ...]] = set()
    limited = False
    # At most two phrase instances by the mechanical cap. Every stage is bounded;
    # these budgets withhold an unproven state, never truncate a scored message.
    for order_index, order in enumerate(permutations(phrases)):
        if order_index >= budget.max_candidates:
            limited = True
            break
        current = {prefix}
        for part in (*order, suffix) if suffix else order:
            following = set()
            for left in sorted(current, key=lambda item: (len(item), item)):
                for merged in _extensions(left, part):
                    if len(merged) > budget.max_words or len(following) >= budget.max_candidates:
                        limited = True
                        continue
                    following.add(merged)
            current = following
        for words in sorted(current, key=lambda item: (len(item), item)):
            if len(candidates) >= budget.max_candidates:
                limited = True
                break
            candidates.add(words or ("hello",))
    for words in sorted(candidates, key=lambda item: (len(item), item)):
        text = " ".join(words) + punctuation
        if "NR008" in cards:
            text = text.upper()
        elif "NR007" in cards:
            text = text.lower()
        if len(text) > MAX_PARSER_CODEPOINTS:
            limited = True
            continue
        view = parse_message(text)
        if not view.has_text or not view.words or view.is_whisper:
            continue
        envelope = EventEnvelope(_ROUTE, EventKind.MESSAGE_CREATE, "witness", 1, _TIME, _TIME, 0)
        decision = evaluate_message(MessageEvent(envelope, text), context)
        if all(
            result.outcome is Outcome.PASS
            or (instance.card_id == "NR006" and result.outcome is Outcome.NOT_APPLICABLE)
            for instance, result in zip(instances, decision.evaluations, strict=True)
        ):
            return Assessment(witness=Witness(text, decision.evaluations))
    return Assessment(rejection=Rejection.SEARCH_LIMIT if limited else Rejection.NO_WITNESS)


def replacement_targets(
    active: tuple[RuleInstance, ...], incoming: RuleInstance
) -> tuple[str | None, ...]:
    """Only capped-deck/exclusive-slot removals; no unrelated retirement escape."""
    if incoming.card_id not in CARD_SPECS:
        raise NotImplementedError("This template has no implemented compatibility metadata.")
    if incoming.card_id != "NR036" and any(i.card_id == incoming.card_id for i in active):
        return ()
    spec = CARD_SPECS[incoming.card_id]
    groups = []
    if spec.slot:
        occupants = {i.instance_id for i in active if CARD_SPECS[i.card_id].slot == spec.slot}
        if occupants:
            groups.append(occupants)
    cap = {
        Deck.BREVITY: CHATGAMES_MAX_ACTIVE_BREVITY_RULES,
        Deck.VOCABULARY: CHATGAMES_MAX_ACTIVE_VOCABULARY_RULES,
    }.get(spec.deck)
    if cap is not None:
        occupants = {i.instance_id for i in active if CARD_SPECS[i.card_id].deck == spec.deck}
        if len(occupants) >= cap:
            groups.append(occupants)
    if incoming.card_id == "NR036":
        occupants = {i.instance_id for i in active if i.card_id == "NR036"}
        if len(occupants) >= CHATGAMES_MAX_ACTIVE_DONT_FORGET:
            groups.append(occupants)
    if not groups:
        return (None,)
    permitted = set.intersection(*groups)
    return tuple(i.instance_id for i in active if i.instance_id in permitted)


@dataclass(frozen=True, slots=True)
class LegalCompletion:
    outgoing_id: str | None
    instances: tuple[RuleInstance, ...]
    witness: Witness = field(repr=False)


def legal_completions(
    active: tuple[RuleInstance, ...],
    incoming: RuleInstance,
    *,
    budget: WitnessBudget = DEFAULT_WITNESS_BUDGET,
) -> tuple[LegalCompletion, ...]:
    if inventory_rejection(active):
        raise RuleConfigurationError("Current inventory is invalid or unsupported.")
    if any(i.instance_id == incoming.instance_id for i in active):
        raise RuleConfigurationError("Incoming and active instance identities must differ.")
    results = []
    for outgoing in replacement_targets(active, incoming):
        result = tuple(i for i in active if i.instance_id != outgoing) + (incoming,)
        assessment = assess_state(result, budget=budget)
        if assessment.legal:
            results.append(LegalCompletion(outgoing, result, assessment.witness))
    return tuple(results)
