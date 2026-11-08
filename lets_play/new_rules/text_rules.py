"""Representative ordinary text predicates, with no storage or Discord effects."""

from lets_play.new_rules.contracts import Outcome, RuleEvaluation, RuleInstance
from lets_play.new_rules.parsing import MessageView, is_word_gap, parse_message

TEXT_FAILURE_REASONS = {
    "NR007": "lowercase_required",
    "NR008": "uppercase_required",
    "NR010": "required_question_ending",
    "NR011": "required_three_periods",
    "NR016": "formatting_forbidden",
    "NR013": "questions_forbidden",
    "NR017": "maximum_five_words",
    "NR019": "exactly_one_word",
    "NR022": "repeated_word",
    "NR024": "required_yes_prefix",
    "NR025": "sorry_forbidden",
    "NR027": "maybe_forbidden",
    "NR033": "required_please_prefix",
    "NR034": "required_ending_word",
    "NR036": "required_phrase",
}
SUPPORTED_TEXT_RULES = frozenset(TEXT_FAILURE_REASONS)


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
        case "NR007":
            passed = view.logical == view.logical.lower()
        case "NR008":
            passed = view.logical == view.logical.upper()
        case "NR010":
            passed = view.ending.endswith("?")
        case "NR013":
            passed = "?" not in view.logical
        case "NR017":
            passed = len(view.words) <= 5
        case "NR019":
            passed = len(view.words) == 1
        case "NR022":
            passed = len(set(view.word_keys)) == len(view.words)
        case "NR024" | "NR033":
            required = "yes" if instance.card_id == "NR024" else "please"
            passed = view.starts_with_word(required)
        case "NR025" | "NR027":
            forbidden = "sorry" if instance.card_id == "NR025" else "maybe"
            passed = forbidden not in view.word_keys
        case "NR034":
            passed = view.ends_with_word("over")
        case "NR011":
            ending = view.ending
            passed = ending.endswith("...")
        case "NR016":
            passed = not view.formatting
        case "NR036":
            passed = _contains_phrase(view, keys)
    return RuleEvaluation(
        instance.instance_id,
        Outcome.PASS if passed else Outcome.FAIL,
        None if passed else TEXT_FAILURE_REASONS[instance.card_id],
    )
