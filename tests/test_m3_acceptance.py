"""Full ordinary deck reachability and acceptance witnesses for the theme cores."""

from types import SimpleNamespace

import discord
import pytest
from test_evaluation import NOW, POSITION, message
from test_hands import DEADLINE, RecordedRNG

from lets_play.discord_adapter.discord_io import message_snapshot
from lets_play.discord_adapter.runtime import OPENING_CARDS, ORDINARY_CARDS
from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.compatibility import assess_state
from lets_play.new_rules.contracts import Lifecycle, Outcome, RuleInstance, SessionPosition
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.hands import ChoiceStatus, PhraseChoice, build_hand, revalidate_choice
from lets_play.new_rules.profanity import ProfanityList

PROFANITY = ProfanityList("rutabaga")
PHRASE = PhraseChoice("fixture-pond", "duck pond", "fixture-1")


def deal(context, templates, offer_id, first_index=0):
    return build_hand(
        context,
        offer_id=offer_id,
        picker_id=303,
        deadline=DEADLINE,
        rng=RecordedRNG([first_index]),
        phrase_bank=(PHRASE,),
        available_templates=templates,
    )


def select(context, hand, incoming, templates):
    choice = revalidate_choice(
        hand,
        context,
        incoming.instance_id,
        now=NOW,
        actor_id=303,
        available_templates=templates,
    )
    assert choice.status is ChoiceStatus.READY
    return choice.completion.instances


@pytest.fixture(scope="module")
def later_rounds():
    opening = EvaluationContext(
        SessionPosition(POSITION.route, Lifecycle.OPENING_CHOICE), (), profanity=PROFANITY
    )
    available = deal(opening, OPENING_CARDS, "opening").eligible_templates
    contexts = []
    for opener in ("NR006", "NR007"):
        starting_hand = deal(opening, OPENING_CARDS, "opening", available.index(opener)).hand
        incoming = starting_hand.options[0].incoming
        assert incoming.card_id == opener
        active = select(opening, starting_hand, incoming, OPENING_CARDS)
        context = EvaluationContext(POSITION, active, profanity=PROFANITY)
        eligible = deal(context, ORDINARY_CARDS, "later").eligible_templates
        contexts.append((context, eligible))
    return contexts


@pytest.mark.parametrize("card", sorted(CARD_SPECS))
def test_every_ordinary_card_is_selectable_in_a_complete_later_hand(card, later_rounds):
    candidates = [(context, eligible) for context, eligible in later_rounds if card in eligible]
    assert candidates, f"{card} is unavailable from both legal opening states"
    context, eligible = candidates[0]
    hand = deal(context, ORDINARY_CARDS, "later", eligible.index(card)).hand
    assert hand is not None
    assert len(hand.options) == len({item.incoming.card_id for item in hand.options}) == 3
    assert hand.options[0].incoming.card_id == card
    for item in hand.options:
        active = select(context, hand, item.incoming, ORDINARY_CARDS)
        assert item.incoming in active
        assert assess_state(active, profanity=PROFANITY).legal


THEME_CASES = (
    pytest.param(("NR037", "NR035", "NR032"), "We Sail At Dawn", id="royal-court"),
    pytest.param(("NR034", "NR015", "NR017"), "all clear over", id="radio-discipline"),
    pytest.param(("NR007", "NR013", "NR014"), "the kettle is on", id="quiet-please"),
    pytest.param(("NR001", "NR006", "NR026"), "care to deal", id="poker-night"),
    pytest.param(("NR019", "NR007", "NR016"), "later", id="small-talk"),
    pytest.param(
        ("NR033", "NR036", "NR034"), "please visit the duck pond over", id="terms-and-conditions"
    ),
    pytest.param(("NR001", "NR026", "NR029"), "the gate is still open", id="serious-business"),
    pytest.param(("NR008", "NR012", "NR035"), "WE ARE READY!", id="public-address"),
    pytest.param(("NR011", "NR038", "NR025"), "well, let's see...", id="dramatic-pause"),
    pytest.param(("NR039", "NR009", "NR016"), "young geese nap", id="fine-print"),
    pytest.param(("NR013", "NR028", "NR029"), "let's try again", id="no-further-questions"),
    pytest.param(("NR040", "NR036", "NR034"), "meet at the duck pond over", id="chain-letter"),
    pytest.param(("NR033", "NR010", "NR032"), "please, could we talk?", id="complaint-department"),
    pytest.param(("NR019", "NR013", "NR028"), "perhaps", id="no-comment"),
    pytest.param(
        ("NR001", "NR003", "NR004", "NR005"), "the ducks are up early", id="back-to-basics"
    ),
)


def theme_rule(card):
    if card == "NR032":
        return PROFANITY.instantiate("profanity")
    if card == "NR036":
        return PHRASE.instantiate("phrase")
    return RuleInstance(card, card)


def native_reply(content):
    incoming = SimpleNamespace(
        id=8,
        author=SimpleNamespace(id=303, bot=False),
        content=content,
        created_at=NOW,
        attachments=(),
        stickers=(),
        reference=SimpleNamespace(message_id=9, type=discord.MessageReferenceType.default),
        type=discord.MessageType.reply,
        flags=SimpleNamespace(voice=False),
        webhook_id=None,
        is_system=lambda: False,
    )
    return message_snapshot(incoming, POSITION.route, NOW)


@pytest.mark.parametrize(("cards", "content"), THEME_CASES)
def test_every_theme_core_has_a_legal_witness_and_accepts_natural_chat(cards, content):
    active = tuple(theme_rule(card) for card in cards)
    context = EvaluationContext(POSITION, active, profanity=PROFANITY)
    assert assess_state(active, profanity=PROFANITY).legal

    candidate = message(content)
    if "NR040" in cards:
        plain = evaluate_message(candidate, context)
        assert [
            result.reason_code for result in plain.evaluations if result.outcome is Outcome.FAIL
        ] == ["reply_required"]
        candidate = native_reply(content)
        assert candidate.is_reply

    decision = evaluate_message(candidate, context)
    assert decision.score.raw_faults == 0
    for card, result in zip(cards, decision.evaluations, strict=True):
        expected = Outcome.NOT_APPLICABLE if card == "NR006" else Outcome.PASS
        assert result.outcome is expected
