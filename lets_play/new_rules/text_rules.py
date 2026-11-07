"""Representative ordinary text predicates, with no storage or Discord effects."""

from lets_play.new_rules.contracts import Outcome, RuleEvaluation, RuleInstance
from lets_play.new_rules.parsing import MessageView, is_word_gap, parse_message

SUPPORTED_TEXT_RULES = frozenset({"NR011", "NR016", "NR034", "NR036"})
TEXT_FAILURE_REASONS = {
    "NR011": "required_three_periods",
    "NR016": "formatting_forbidden",
    "NR034": "required_ending_word",
    "NR036": "required_phrase",
}


class RuleConfigurationError(ValueError):
    """A frozen instance cannot be evaluated; do not count it as compliance."""


def phrase_keys(instance: RuleInstance) -> tuple[str, ...]:
    """Validate evaluator input, not guild-bank suitability or session legality.

    The exact phrase/revision stays frozen in the instance. No global cache or
    mutable guild setting is consulted, and errors never echo private wording.
    """
    params = dict(instance.parameters)
    if not params.get("phrase") or params.keys() - {"phrase", "phrase_revision"}:
        raise RuleConfigurationError("A frozen phrase and optional revision are required.")
    view = parse_message(params["phrase"])
    gaps = []
    end = 0
    for word in view.words:
        gaps.append(view.logical[end : word.start])
        end = word.end
    gaps.append(view.logical[end:])
    if (
        not view.words
        or view.formatting
        or view.links
        or view.objects
        or not all(is_word_gap(gap) for gap in gaps)
    ):
        raise RuleConfigurationError("The frozen phrase must contain plain words and punctuation.")
    return view.word_keys


def _contains_phrase(view: MessageView, keys: tuple[str, ...]) -> bool:
    size = len(keys)
    message_keys = view.word_keys
    for start in range(len(message_keys) - size + 1):
        if message_keys[start : start + size] != keys:
            continue
        words = view.words[start : start + size]
        if all(
            is_word_gap(view.logical[left.end : right.start])
            for left, right in zip(words, words[1:], strict=False)
        ):
            return True
    return False


def evaluate_text_rule(view: MessageView, instance: RuleInstance) -> RuleEvaluation:
    if instance.card_id not in SUPPORTED_TEXT_RULES:
        raise NotImplementedError("This card has no implemented text predicate.")
    keys = phrase_keys(instance) if instance.card_id == "NR036" else ()
    if view.is_whisper or not view.has_text:
        return RuleEvaluation(instance.instance_id, Outcome.NOT_APPLICABLE)
    match instance.card_id:
        case "NR034":
            passed = view.ends_with_word("over")
        case "NR011":
            ending = view.ending
            passed = ending.endswith("...") and not ending.endswith("....")
        case "NR016":
            passed = not view.formatting
        case "NR036":
            passed = _contains_phrase(view, keys)
    return RuleEvaluation(
        instance.instance_id,
        Outcome.PASS if passed else Outcome.FAIL,
        None if passed else TEXT_FAILURE_REASONS[instance.card_id],
    )
