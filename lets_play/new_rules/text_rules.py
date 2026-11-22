"""Representative ordinary text predicates, with no storage or Discord effects."""

from lets_play.new_rules.contracts import Outcome, RuleEvaluation, RuleInstance
from lets_play.new_rules.parsing import MessageView, grapheme_count, is_word_gap, parse_message

TEXT_FAILURE_REASONS = {
    "NR001": "emoji_forbidden",
    "NR005": "links_forbidden",
    "NR007": "lowercase_required",
    "NR008": "uppercase_required",
    "NR009": "digits_forbidden",
    "NR010": "required_question_ending",
    "NR011": "required_three_periods",
    "NR012": "required_exclamation_ending",
    "NR013": "questions_forbidden",
    "NR014": "exclamations_forbidden",
    "NR015": "line_break_forbidden",
    "NR016": "formatting_forbidden",
    "NR017": "maximum_five_words",
    "NR018": "minimum_five_words",
    "NR019": "exactly_one_word",
    "NR020": "minimum_twenty_characters",
    "NR021": "maximum_forty_characters",
    "NR022": "repeated_word",
    "NR024": "required_yes_prefix",
    "NR025": "sorry_forbidden",
    "NR026": "laughter_word_forbidden",
    "NR027": "maybe_forbidden",
    "NR028": "because_forbidden",
    "NR029": "but_forbidden",
    "NR030": "first_person_word_forbidden",
    "NR031": "second_person_word_forbidden",
    "NR033": "required_please_prefix",
    "NR034": "required_ending_word",
    "NR035": "required_royal_word",
    "NR036": "required_phrase",
    "NR037": "title_case_required",
    "NR038": "comma_required",
    "NR039": "maximum_six_letters_per_word",
}
SUPPORTED_TEXT_RULES = frozenset(TEXT_FAILURE_REASONS)
MIN_WORDS = 5
MIN_CHARACTERS = 20
REQUIRED_ENDINGS = {"NR010": "?", "NR011": "...", "NR012": "!"}
ROYAL_WORDS = frozenset({"we", "our", "us"})
_NUMERIC_EMOJI = frozenset("🔢🔟💯")
_NUMERIC_CUSTOM_EMOJI_PREFIXES = ("<:100:", "<:1000:", "<a:100:", "<a:1000:")
_FORBIDDEN_WORDS = {
    "NR025": frozenset({"sorry"}),
    "NR026": frozenset({"lol", "lmao", "haha"}),
    "NR027": frozenset({"maybe"}),
    "NR028": frozenset({"because"}),
    "NR029": frozenset({"but"}),
    "NR030": frozenset({"i", "me", "my", "i'm", "i've", "i'd", "i'll", "i'd've", "i'll've"}),
    "NR031": frozenset(
        {"you", "your", "you're", "you've", "you'll", "you'd", "you'd've", "you'll've"}
    ),
}


class RuleConfigurationError(ValueError):
    """A frozen instance cannot be evaluated; do not count it as compliance."""


def decree_case(word: str) -> str:
    if grapheme_count(word) >= 3:
        for index, char in enumerate(word):
            if char.lower() != char.upper():
                return word[:index] + char.title() + word[index + 1 :].lower()
    return word


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
        case "NR001":
            passed = not view.has_emoji
        case "NR005":
            passed = not view.shared_links
        case "NR007":
            passed = view.logical == view.logical.lower()
        case "NR008":
            passed = view.logical == view.logical.upper()
        case "NR009":
            passed = not (
                any(char.isdigit() or char in _NUMERIC_EMOJI for char in view.logical)
                or any(obj.startswith(_NUMERIC_CUSTOM_EMOJI_PREFIXES) for obj in view.objects)
            )
        case "NR010" | "NR011" | "NR012":
            passed = view.ending.endswith(REQUIRED_ENDINGS[instance.card_id])
        case "NR013":
            passed = "?" not in view.logical
        case "NR014":
            passed = "!" not in view.logical
        case "NR015":
            passed = not view.has_line_break
        case "NR017":
            passed = len(view.words) <= 5
        case "NR018":
            passed = len(view.words) >= MIN_WORDS
        case "NR019":
            passed = len(view.words) == 1
        case "NR020":
            passed = view.character_count >= MIN_CHARACTERS
        case "NR021":
            passed = view.character_count <= 40
        case "NR022":
            passed = len(set(view.word_keys)) == len(view.words)
        case "NR024" | "NR033":
            required = "yes" if instance.card_id == "NR024" else "please"
            passed = view.starts_with_word(required)
        case card if card in _FORBIDDEN_WORDS:
            passed = _FORBIDDEN_WORDS[card].isdisjoint(view.word_keys)
        case "NR034":
            passed = view.ends_with_word("over")
        case "NR035":
            passed = not ROYAL_WORDS.isdisjoint(view.word_keys)
        case "NR016":
            passed = not view.formatting
        case "NR036":
            passed = _contains_phrase(view, keys)
        case "NR037":
            passed = all(word.text == decree_case(word.text) for word in view.words)
        case "NR038":
            passed = "," in view.logical
        case "NR039":
            passed = all(word.letter_count <= 6 for word in view.words)
    return RuleEvaluation(
        instance.instance_id,
        Outcome.PASS if passed else Outcome.FAIL,
        None if passed else TEXT_FAILURE_REASONS[instance.card_id],
    )
