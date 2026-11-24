"""Bounded ordinary-subset witnesses; no SAT, draws, storage, or Discord I/O."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from itertools import chain, combinations, permutations

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
    ReferenceKind,
    RuleEvaluation,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.gif_media import is_gif_link
from lets_play.new_rules.parsing import (
    MAX_PARSER_CODEPOINTS,
    ParserLimitError,
    grapheme_count,
    parse_message,
)
from lets_play.new_rules.profanity import ProfanityList
from lets_play.new_rules.text_rules import (
    MIN_CHARACTERS,
    MIN_WORDS,
    REQUIRED_ENDINGS,
    ROYAL_WORDS,
    RuleConfigurationError,
    decree_case,
    phrase_keys,
)


class Rejection(StrEnum):
    UNSUPPORTED = "unsupported_rule"
    INVALID = "invalid_instances"
    DUPLICATE = "duplicate_template"
    CAP = "mechanical_cap"
    SLOT = "exclusive_slot"
    CONFLICT = "direct_conflict"
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
    reference_kind: ReferenceKind = ReferenceKind.NONE
    referenced_message_id: int | None = None


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
_MATCHING_MARK_CONFLICTS = frozenset({("NR010", "NR013"), ("NR012", "NR014")})
_FILLER_WORDS = ("bright", "green", "ducks", "swim", "today", "near", "each", "pond")
_EXTRA_FILLER_WORDS = ("boats", "float", "under", "little", "clouds", "west", "shore", "park")


def _direct_conflict(left: str, right: str) -> bool:
    if left == "NR019":
        return CARD_SPECS[right].deck is Deck.CLAUSES
    if right == "NR019":
        return CARD_SPECS[left].deck is Deck.CLAUSES
    return (left, right) in _MATCHING_MARK_CONFLICTS or (right, left) in _MATCHING_MARK_CONFLICTS


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
    if any(_direct_conflict(left, right) for left, right in combinations(cards, 2)):
        return Rejection.CONFLICT
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


def _required_part_orders(phrases: tuple[tuple[str, ...], ...], *, require_royal: bool):
    needs_word = require_royal and all(ROYAL_WORDS.isdisjoint(phrase) for phrase in phrases)
    for order in permutations(phrases):
        if not needs_word:
            yield order
            continue
        for word in sorted(ROYAL_WORDS):
            for index in range(len(order) + 1):
                yield (*order[:index], (word,), *order[index:])


def _render_witness(words: tuple[str, ...], cards: set[str], punctuation: str) -> str:
    if "NR037" in cards:
        words = tuple(decree_case(word) for word in words)
    if "NR038" in cards:
        words = (words[0] + ",", *words[1:])
    text = " ".join(words) + punctuation
    if "NR008" in cards:
        return text.upper()
    if "NR007" in cards:
        return text.lower()
    return text


def _witness_texts(
    words: tuple[str, ...],
    cards: set[str],
    punctuation: str,
    *,
    has_prefix: bool,
    has_suffix: bool,
    filler_words: tuple[str, ...],
):
    text = _render_witness(words, cards, punctuation)
    missing_words = MIN_WORDS - len(words) if "NR018" in cards else 0
    missing_characters = MIN_CHARACTERS - grapheme_count(text) if "NR020" in cards else 0
    if missing_words <= 0 and missing_characters <= 0:
        yield len(words), text
        return
    # Alternate padding when a configured phrase blocks the first witness.
    # Keep ordinary witness generation unchanged for other inventories.
    rotations = range(len(filler_words)) if "NR032" in cards else range(1)
    for offset in rotations:
        padding = []
        remaining = missing_characters
        for filler in (*filler_words[offset:], *filler_words[:offset]):
            if len(padding) >= missing_words and remaining <= 0:
                break
            if filler not in words:
                padding.append(filler)
                remaining -= len(filler) + 1
        # Try each gap and let the validators reject a split required phrase.
        for index in range(int(has_prefix), len(words) + 1 - int(has_suffix)):
            padded = (*words[:index], *padding, *words[index:])
            yield len(padded), _render_witness(padded, cards, punctuation)


def _with_gif_phrases(text, urls, suffix, punctuation):
    if not urls:
        yield text
        return
    if len(urls) == 1:
        # A single GIF can carry all the needed readable text in its label.
        yield f"[{text}](<{urls[0]}>)"
    body = text.removesuffix(punctuation)
    if suffix:
        before, _, last = body.rpartition(" ")
        parts = (before, *urls, last)
    else:
        parts = (body, *urls)
    yield " ".join(filter(None, parts)) + punctuation


def assess_state(
    instances: tuple[RuleInstance, ...],
    *,
    budget: WitnessBudget = DEFAULT_WITNESS_BUDGET,
    profanity: ProfanityList | None = None,
) -> Assessment:
    rejection = inventory_rejection(instances)
    if rejection:
        return Assessment(rejection=rejection)
    try:
        context = EvaluationContext(_POSITION, instances, profanity=profanity)
        if not context.dependencies_available:
            return Assessment(rejection=Rejection.INVALID)
        phrases = tuple(phrase_keys(i) for i in instances if i.card_id == "NR036")
    except ParserLimitError:
        return Assessment(rejection=Rejection.SEARCH_LIMIT)
    except (ValueError, NotImplementedError):
        return Assessment(rejection=Rejection.INVALID)
    if any(len(phrase) > budget.max_words for phrase in phrases):
        return Assessment(rejection=Rejection.SEARCH_LIMIT)
    urls = tuple(phrase[0] for phrase in phrases if len(phrase) == 1 and is_gif_link(phrase[0]))
    if urls:
        phrases = tuple(phrase for phrase in phrases if phrase[0] not in urls)
    cards = {i.card_id for i in instances}
    # A native reply to the public game panel is available when the rule starts.
    reply_kind = ReferenceKind.REPLY if "NR040" in cards else ReferenceKind.NONE
    reply_target = 1 if reply_kind is ReferenceKind.REPLY else None
    prefix = ("yes",) if "NR024" in cards else ("please",) if "NR033" in cards else ()
    suffix = ("over",) if "NR034" in cards else ()
    punctuation = next((ends[0] for card, ends in REQUIRED_ENDINGS.items() if card in cards), "")
    filler_words = _FILLER_WORDS
    fallback_words = ("hello",)
    if "NR032" in cards:
        filler_words = tuple(
            word
            for word in (*_FILLER_WORDS, *_EXTRA_FILLER_WORDS)
            if not profanity.matches(parse_message(word))
        )
        fallback_words = tuple(
            word for word in ("hello", *filler_words) if not profanity.matches(parse_message(word))
        )
    candidates: set[tuple[str, ...]] = set()
    limited = False
    # At most two phrases and one extra inclusion word. Every stage is bounded;
    # these budgets withhold an unproven state and leave scored messages intact.
    orders = _required_part_orders(phrases, require_royal="NR035" in cards)
    for order_index, order in enumerate(orders):
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
            if words:
                candidates.add(words)
            else:
                candidates.update((word,) for word in fallback_words[: budget.max_candidates])
    captions = (
        trial
        for words in sorted(candidates, key=lambda item: (len(item), item))
        for trial in _witness_texts(
            words,
            cards,
            punctuation,
            has_prefix=bool(prefix),
            has_suffix=bool(suffix),
            filler_words=filler_words,
        )
    )
    texts = chain(
        ((0, urls[0]),) if len(urls) == 1 else (),
        (
            (word_count, trial)
            for word_count, text in captions
            for trial in _with_gif_phrases(text, urls, suffix, punctuation)
        ),
    )
    for index, (word_count, text) in enumerate(texts):
        if index >= budget.max_candidates:
            limited = True
            break
        if word_count > budget.max_words or len(text) > MAX_PARSER_CODEPOINTS:
            limited = True
            continue
        view = parse_message(text)
        if urls and len(view.words) > budget.max_words:
            limited = True
            continue
        if not view.has_text or not view.words or view.is_whisper:
            continue
        envelope = EventEnvelope(_ROUTE, EventKind.MESSAGE_CREATE, "witness", 1, _TIME, _TIME, 0)
        event = MessageEvent(
            envelope, text, reference_kind=reply_kind, referenced_message_id=reply_target
        )
        decision = evaluate_message(event, context)
        if all(
            result.outcome is Outcome.PASS
            or (instance.card_id == "NR006" and result.outcome is Outcome.NOT_APPLICABLE)
            for instance, result in zip(instances, decision.evaluations, strict=True)
        ):
            return Assessment(witness=Witness(text, decision.evaluations, reply_kind, reply_target))
    return Assessment(rejection=Rejection.SEARCH_LIMIT if limited else Rejection.NO_WITNESS)


def replacement_targets(
    active: tuple[RuleInstance, ...], incoming: RuleInstance
) -> tuple[str | None, ...]:
    """Find removals permitted by the caps, slots, and direct conflicts."""
    if incoming.card_id not in CARD_SPECS:
        raise NotImplementedError("This template has no implemented compatibility metadata.")
    if incoming.card_id != "NR036" and any(i.card_id == incoming.card_id for i in active):
        return ()
    spec = CARD_SPECS[incoming.card_id]
    groups = [
        {instance.instance_id}
        for instance in active
        if _direct_conflict(incoming.card_id, instance.card_id)
    ]
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
    witness: Witness | None = field(repr=False)


def legal_completions(
    active: tuple[RuleInstance, ...],
    incoming: RuleInstance,
    *,
    budget: WitnessBudget = DEFAULT_WITNESS_BUDGET,
    profanity: ProfanityList | None = None,
) -> tuple[LegalCompletion, ...]:
    if inventory_rejection(active):
        raise RuleConfigurationError("Current inventory is invalid or unsupported.")
    if any(i.instance_id == incoming.instance_id for i in active):
        raise RuleConfigurationError("Incoming and active instance identities must differ.")
    results = []
    for outgoing in replacement_targets(active, incoming):
        result = tuple(i for i in active if i.instance_id != outgoing) + (incoming,)
        assessment = assess_state(result, budget=budget, profanity=profanity)
        if assessment.legal:
            results.append(LegalCompletion(outgoing, result, assessment.witness))
    return tuple(results)
