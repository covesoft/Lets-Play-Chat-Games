"""First three ordinary text predicates; no scoring, storage, or Discord effects.

The engine will select active instances/phases. These representatives verify the
parser contract offline; they are not registered as live game commands.
"""

from lets_play.new_rules.contracts import Outcome, RuleEvaluation, RuleInstance
from lets_play.new_rules.parsing import MessageView

SUPPORTED_TEXT_RULES = frozenset({"NR011", "NR016", "NR034"})


def evaluate_text_rule(view: MessageView, instance: RuleInstance) -> RuleEvaluation:
    if instance.card_id not in SUPPORTED_TEXT_RULES:
        raise NotImplementedError("This card has no predicate in the first parser increment.")
    if view.is_whisper or not view.has_text:
        return RuleEvaluation(instance.instance_id, Outcome.NOT_APPLICABLE)
    match instance.card_id:
        case "NR034":
            passed, reason = view.ends_with_word("over"), "required_ending_word"
        case "NR011":
            ending = view.ending
            passed = ending.endswith("...") and not ending.endswith("....")
            reason = "required_three_periods"
        case "NR016":
            passed, reason = not view.formatting, "formatting_forbidden"
    return RuleEvaluation(
        instance.instance_id, Outcome.PASS if passed else Outcome.FAIL, None if passed else reason
    )
