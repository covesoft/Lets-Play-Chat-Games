"""Complete legal offers, frozen parameters, fair tickets and original deadlines."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from random import Random

import pytest

from lets_play.new_rules.compatibility import WitnessBudget, assess_state
from lets_play.new_rules.contracts import (
    Deadline,
    DeadlineKind,
    Lifecycle,
    Phase,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext
from lets_play.new_rules.hands import (
    BlockedReason,
    ChoiceStatus,
    PhraseChoice,
    build_hand,
    revalidate_choice,
)
from lets_play.new_rules.text_rules import RuleConfigurationError

NOW = datetime(2026, 11, 7, tzinfo=UTC)
DEADLINE = Deadline("original-choice", DeadlineKind.CHOICE, NOW + timedelta(seconds=60))
POSITION = SessionPosition(
    SessionRoute(101, 202, "session"), Lifecycle.PLAYING, round_id="round", ruleset_version=1
)


class RecordedRNG:
    def __init__(self, picks=()):
        self.picks = list(picks)
        self.stops = []

    def randrange(self, stop):
        self.stops.append(stop)
        pick = self.picks.pop(0) if self.picks else 0
        assert 0 <= pick < stop
        return pick

    def shuffle(self, values):
        raise AssertionError("The offer builder does not need shuffle")


def context(*cards, position=POSITION, instances=None):
    return EvaluationContext(
        position,
        tuple(RuleInstance(card, card) for card in cards) if instances is None else instances,
    )


def bank(count):
    return tuple(PhraseChoice(str(i), f"duck {i}", f"revision-{i}") for i in range(count))


def build(ctx=None, *, cards=("NR004", "NR006", "NR036"), **kwargs):
    return build_hand(
        ctx or context(),
        offer_id="offer",
        picker_id=303,
        deadline=DEADLINE,
        rng=kwargs.pop("rng", RecordedRNG()),
        available_templates=frozenset(cards),
        **kwargs,
    )


def option(hand, card):
    return next(item for item in hand.options if item.incoming.card_id == card)


def resolve(hand, ctx, card, **kwargs):
    return revalidate_choice(
        hand,
        ctx,
        option(hand, card).incoming.instance_id,
        now=kwargs.pop("now", NOW),
        actor_id=kwargs.pop("actor_id", 303),
        **kwargs,
    )


def test_opening_offer_has_three_distinct_ordinary_templates_and_real_completions():
    opening = context(
        position=replace(
            POSITION, lifecycle=Lifecycle.OPENING_CHOICE, round_id=None, ruleset_version=0
        )
    )
    result = build(opening, cards=("NR004", "NR011", "NR034"))
    assert result.blocked is None and len(result.hand.options) == 3
    assert len({o.incoming.card_id for o in result.hand.options}) == 3
    assert all(not o.is_replacement for o in result.hand.options)
    assert result.hand.deadline is DEADLINE
    for item in result.hand.options:
        assert item.completions and all(assess_state(c.instances).legal for c in item.completions)
        assert (
            revalidate_choice(
                result.hand, opening, item.incoming.instance_id, now=NOW, actor_id=303
            ).status
            is ChoiceStatus.READY
        )


@pytest.mark.parametrize("seed", range(12))
def test_seeded_draws_are_reproducible_and_every_slot_has_a_legal_completion(seed):
    first = build_hand(
        context("NR011", "NR034"),
        offer_id="offer",
        picker_id=303,
        deadline=DEADLINE,
        rng=Random(seed),
        phrase_bank=bank(5),
    )
    second = build_hand(
        context("NR011", "NR034"),
        offer_id="offer",
        picker_id=303,
        deadline=DEADLINE,
        rng=Random(seed),
        phrase_bank=bank(5),
    )
    assert first == second
    assert first.hand and len({o.incoming.card_id for o in first.hand.options}) == 3
    for item in first.hand.options:
        for completion in item.completions:
            assert assess_state(completion.instances).legal


def test_fewer_than_three_legal_templates_returns_no_partial_hand_or_rng_draw():
    rng = RecordedRNG()
    result = build(cards=("NR004", "NR006"), rng=rng)
    assert result.hand is None and result.blocked is BlockedReason.TOO_FEW
    assert result.eligible_templates == ("NR004", "NR006")
    assert rng.stops == []


def test_many_phrase_variants_remain_one_template_ticket():
    rng = RecordedRNG((2, 14, 0, 0))
    result = build(phrase_bank=bank(15), phrase_draw_limit=15, rng=rng)
    assert rng.stops == [3, 15, 2, 1]
    assert len(result.eligible_templates) == 3
    assert dict(option(result.hand, "NR036").incoming.parameters)["phrase"] == "duck 14"
    assert len({o.incoming.card_id for o in result.hand.options}) == 3


def test_unselected_phrase_template_neither_rolls_nor_consumes_a_phrase():
    rng = RecordedRNG()
    used = frozenset({("already", "used")})
    result = build(
        cards=("NR004", "NR006", "NR007", "NR036"),
        phrase_bank=bank(15),
        phrase_draw_limit=15,
        used_phrases=used,
        rng=rng,
    )
    assert rng.stops == [4, 3, 2]
    assert all(o.incoming.card_id != "NR036" for o in result.hand.options)
    assert used == frozenset({("already", "used")})


def test_multiple_outgoing_targets_do_not_multiply_a_template_ticket():
    ctx = context("NR022", "NR025")
    rng = RecordedRNG()
    hand = build(ctx, cards=("NR004", "NR006", "NR027"), rng=rng).hand
    assert rng.stops == [3, 2, 1]
    assert set(option(hand, "NR027").outgoing_ids) == {"NR022", "NR025"}


def test_multiple_target_choice_keeps_the_original_deadline_and_requires_selection():
    ctx = context("NR022", "NR025")
    hand = build(ctx, cards=("NR004", "NR006", "NR027")).hand
    assert resolve(hand, ctx, "NR027").status is ChoiceStatus.NEEDS_TARGET
    for target in ("NR022", "NR025"):
        result = resolve(hand, ctx, "NR027", outgoing_id=target, now=NOW + timedelta(seconds=59))
        assert result.status is ChoiceStatus.READY and result.completion.outgoing_id == target
        assert len(result.completion.instances) == 2
    assert (
        resolve(hand, ctx, "NR027", outgoing_id="NR022", now=DEADLINE.expires_at).status
        is ChoiceStatus.EXPIRED
    )
    assert hand.deadline is DEADLINE


def test_single_clear_replacement_can_resolve_directly_and_never_removes_unrelated_rule():
    ctx = context("NR017", "NR034")
    hand = build(ctx, cards=("NR004", "NR006", "NR019")).hand
    result = resolve(hand, ctx, "NR019")
    assert result.status is ChoiceStatus.READY
    assert result.completion.outgoing_id == "NR017"
    assert {i.card_id for i in result.completion.instances} == {"NR019", "NR034"}
    assert resolve(hand, ctx, "NR019", outgoing_id="NR034").status is ChoiceStatus.INVALID
    assert resolve(hand, ctx, "NR004", outgoing_id="NR017").status is ChoiceStatus.INVALID


def test_candidate_with_no_supported_removal_is_suppressed_not_repaired_cross_deck():
    clause = PhraseChoice("p", "maybe duck", "r1").instantiate("clause")
    ctx = context(instances=(clause, RuleInstance("apology", "NR025")))
    result = build(ctx, cards=("NR004", "NR006", "NR027"))
    assert result.hand is None and result.blocked is BlockedReason.TOO_FEW
    assert "NR027" not in result.eligible_templates


def test_only_whole_state_legal_phrase_replacement_is_shown():
    active = (
        RuleInstance("brief", "NR017"),
        PhraseChoice("a", "red duck pond", "r1").instantiate("long"),
        PhraseChoice("b", "blue goose", "r1").instantiate("short"),
    )
    ctx = context(instances=active)
    hand = build(ctx, phrase_bank=(PhraseChoice("c", "red duck pond grows", "r2"),)).hand
    assert option(hand, "NR036").outgoing_ids == ("short",)
    result = resolve(hand, ctx, "NR036")
    assert result.status is ChoiceStatus.READY and result.completion.outgoing_id == "short"


def test_used_prefix_holes_do_not_refill_from_position_six():
    phrases = bank(6)
    used = frozenset(p.key for p in phrases[:5])
    free = build(phrase_bank=phrases, used_phrases=used)
    assert free.hand is None and "NR036" not in free.eligible_templates
    premium = build(phrase_bank=phrases, used_phrases=used, phrase_draw_limit=15)
    assert dict(option(premium.hand, "NR036").incoming.parameters)["phrase"] == "duck 5"


def test_incompatible_prefix_holes_also_do_not_refill_from_position_six():
    phrases = tuple(PhraseChoice(str(i), f"maybe duck {i}", "r1") for i in range(5)) + (
        PhraseChoice("six", "red duck", "r1"),
    )
    ctx = context("NR027")
    free = build(ctx, phrase_bank=phrases)
    assert free.hand is None and "NR036" not in free.eligible_templates
    assert build(ctx, phrase_bank=phrases, phrase_draw_limit=15).hand is not None


def test_normalized_remove_readd_cannot_recycle_a_used_phrase_but_new_game_can():
    previous = PhraseChoice("old", "red duck", "r1")
    edited = PhraseChoice("new", "RED-DUCK", "r2")
    assert previous.key == edited.key
    result = build(phrase_bank=(edited,), used_phrases=frozenset({previous.key}))
    assert result.hand is None
    assert build(phrase_bank=(edited,)).hand is not None


def test_active_phrase_is_used_even_when_caller_supplies_no_usage_keys():
    chosen = PhraseChoice("p", "red duck", "r1")
    ctx = context(instances=(chosen.instantiate("active-clause"),))
    assert build(ctx, phrase_bank=(chosen,)).hand is None


def test_existing_offer_is_frozen_while_new_bank_changes_affect_future_deals():
    old = PhraseChoice("p", "Red duck", "r1")
    new = PhraseChoice("p", "Blue goose", "r2")
    ctx = context()
    hand = build(ctx, phrase_bank=(old,)).hand
    later = build(ctx, phrase_bank=(new,)).hand
    assert dict(option(later, "NR036").incoming.parameters)["phrase"] == "Blue goose"
    result = resolve(hand, ctx, "NR036")
    assert result.status is ChoiceStatus.READY
    frozen = result.completion.instances[-1]
    assert dict(frozen.parameters) == {"phrase": "Red duck", "phrase_revision": "r1"}
    # Pure preparation does not consume usage or activate a rule.
    assert resolve(hand, ctx, "NR036") == result and ctx.active_instances == ()


def test_latest_consumption_or_template_capability_can_withhold_a_frozen_choice():
    selected = PhraseChoice("p", "red duck", "r1")
    ctx = context()
    hand = build(ctx, phrase_bank=(selected,)).hand
    assert (
        resolve(hand, ctx, "NR036", used_phrases=frozenset({selected.key})).status
        is ChoiceStatus.UNAVAILABLE
    )
    assert (
        resolve(hand, ctx, "NR036", available_templates=frozenset({"NR004", "NR006"})).status
        is ChoiceStatus.UNAVAILABLE
    )
    assert dict(option(hand, "NR036").incoming.parameters)["phrase"] == "red duck"


@pytest.mark.parametrize(
    "field,value",
    [("revision", 1), ("round_id", "next"), ("ruleset_version", 2), ("phase", Phase.MODE)],
)
def test_changed_owner_makes_the_offer_stale(field, value):
    ctx = context()
    hand = build(ctx, phrase_bank=bank(1)).hand
    changed = replace(ctx, position=replace(POSITION, **{field: value}))
    assert resolve(hand, changed, "NR004").status is ChoiceStatus.STALE


def test_changed_frozen_rules_or_evaluator_version_is_stale_even_with_same_owner():
    ctx = context()
    hand = build(ctx, phrase_bank=bank(1)).hand
    assert resolve(hand, context("NR034"), "NR004").status is ChoiceStatus.STALE
    assert (
        resolve(replace(hand, evaluator_version="future-fixture"), ctx, "NR004").status
        is ChoiceStatus.STALE
    )
    assert (
        resolve(replace(hand, parser_version="future-parser"), ctx, "NR004").status
        is ChoiceStatus.STALE
    )


def test_unknown_choice_or_target_cannot_fall_back_to_a_different_option():
    ctx = context()
    hand = build(ctx, phrase_bank=bank(1)).hand
    assert (
        revalidate_choice(hand, ctx, "invented-choice", now=NOW, actor_id=303).status
        is ChoiceStatus.INVALID
    )
    assert resolve(hand, ctx, "NR004", outgoing_id="invented-target").status is ChoiceStatus.INVALID


def test_saved_witness_cannot_override_latest_search_budget():
    ctx = context()
    hand = build(ctx, phrase_bank=(PhraseChoice("p", "red duck pond", "r1"),)).hand
    assert (
        resolve(hand, ctx, "NR036", budget=WitnessBudget(max_words=2)).status
        is ChoiceStatus.UNPROVEN
    )


@pytest.mark.parametrize("lifecycle", [Lifecycle.LOBBY, Lifecycle.ENDED])
def test_nonchoosing_nonplaying_positions_do_not_build_hands(lifecycle):
    result = build(context(position=replace(POSITION, lifecycle=lifecycle)), phrase_bank=bank(1))
    assert result.hand is None and result.blocked is BlockedReason.WRONG_POSITION


def test_mode_positions_do_not_run_ordinary_dealing_and_invalid_current_state_blocks():
    assert (
        build(context(position=replace(POSITION, phase=Phase.MODE)), phrase_bank=bank(1)).blocked
        is BlockedReason.WRONG_POSITION
    )
    invalid = context("NR010", "NR013")
    rng = RecordedRNG()
    result = build(invalid, phrase_bank=bank(1), rng=rng)
    assert result.blocked is BlockedReason.CURRENT_STATE and rng.stops == []


def test_offer_and_phrase_representations_do_not_reveal_phrase_or_witness_text():
    phrase = PhraseChoice("p", "PRIVATE_CANARY", "r1")
    hand = build(phrase_bank=(phrase,)).hand
    assert "PRIVATE_CANARY" not in repr(phrase) + repr(hand) + repr(hand.options)
    witness_text = option(hand, "NR036").completions[0].witness.text
    assert witness_text not in repr(hand) + repr(hand.options)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"phrase_draw_limit": 4},
        {"phrase_draw_limit": True},
        {"available_templates": frozenset({"NR037"})},
        {"phrase_bank": bank(16)},
        {"used_phrases": frozenset({("NOT_NORMALIZED",)})},
    ],
)
def test_invalid_draw_inputs_fail_without_a_partial_hand(kwargs):
    with pytest.raises(ValueError):
        build_hand(
            context(),
            offer_id="offer",
            picker_id=303,
            deadline=DEADLINE,
            rng=RecordedRNG(),
            **kwargs,
        )


def test_duplicate_bank_keys_and_hand_template_shapes_are_rejected():
    with pytest.raises(RuleConfigurationError):
        build(
            phrase_bank=(PhraseChoice("a", "red duck", "r1"), PhraseChoice("b", "RED-DUCK", "r2"))
        )
    hand = build(phrase_bank=bank(1)).hand
    with pytest.raises(ValueError):
        replace(hand, options=(hand.options[0],) * 3)
    with pytest.raises(ValueError):
        replace(hand, options=hand.options[:2])
    with pytest.raises(ValueError):
        replace(hand.options[0], completions=list(hand.options[0].completions))


def test_picker_authorization_is_rechecked_at_both_choice_steps():
    ctx = context("NR022", "NR025")
    hand = build(ctx, cards=("NR004", "NR006", "NR027")).hand
    assert resolve(hand, ctx, "NR027", actor_id=999).status is ChoiceStatus.UNAUTHORIZED
    assert resolve(hand, ctx, "NR027").status is ChoiceStatus.NEEDS_TARGET
    assert (
        resolve(hand, ctx, "NR027", actor_id=999, outgoing_id="NR022").status
        is ChoiceStatus.UNAUTHORIZED
    )
    assert resolve(hand, ctx, "NR027", outgoing_id="NR022").status is ChoiceStatus.READY
