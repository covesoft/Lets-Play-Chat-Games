"""Versioned frozen offer configuration. No witnesses, chat or arbitrary payloads."""

import json
from datetime import UTC, datetime, timedelta

from lets_play.new_rules.breaking_moments import MODE_VALIDATORS, ModeChoice
from lets_play.new_rules.compatibility import (
    LegalCompletion,
    inventory_rejection,
    replacement_targets,
)
from lets_play.new_rules.contracts import (
    Advancement,
    Deadline,
    DeadlineKind,
    Lifecycle,
    PauseReason,
    Phase,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EVALUATOR_VERSION, EvaluationContext
from lets_play.new_rules.hands import HandOption, OrdinaryHand, choice_completions
from lets_play.new_rules.parsing import PARSER_VERSION
from lets_play.new_rules.profanity import ProfanityList, list_available
from lets_play.storage.game_codec import IncompatibleState

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_int(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware publication/event timestamp is required.")
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def utc_datetime(value: int) -> datetime:
    if type(value) is not int:
        raise ValueError("A persisted UTC timestamp must be an integer microsecond count.")
    return _EPOCH + timedelta(microseconds=value)


def _rule(instance):
    return [instance.instance_id, instance.card_id, list(instance.parameters)]


def _read_rule(data):
    instance, card, params = data
    return RuleInstance(instance, card, tuple(tuple(pair) for pair in params))


def encode_hand(hand: OrdinaryHand) -> str:
    p = hand.owner
    return json.dumps(
        {
            "format": 1,
            "offer": hand.offer_id,
            "picker": hand.picker_id,
            "owner": [
                p.route.guild_id,
                p.route.channel_id,
                p.route.session_id,
                p.lifecycle,
                p.phase,
                p.advancement,
                p.round_id,
                p.ruleset_version,
                p.offer_id,
                p.transition_id,
                p.pause_reason,
                p.revision,
            ],
            "active": [_rule(i) for i in hand.active_instances],
            "deadline": [hand.deadline.token, utc_int(hand.deadline.expires_at)],
            "options": [
                [_rule(o.incoming), [c.outgoing_id for c in o.completions]] for o in hand.options
            ],
            "evaluator": hand.evaluator_version,
            "parser": hand.parser_version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_hand(
    value: str, *, profanity: ProfanityList | None = None, allow_unavailable: bool = False
) -> OrdinaryHand:
    try:
        data = json.loads(value)
        if (
            set(data)
            != {
                "format",
                "offer",
                "picker",
                "owner",
                "active",
                "deadline",
                "options",
                "evaluator",
                "parser",
            }
            or type(data["format"]) is not int
            or data["format"] != 1
            or data["evaluator"] != EVALUATOR_VERSION
            or data["parser"] != PARSER_VERSION
        ):
            raise ValueError
        if (
            not isinstance(data["options"], list)
            or len(data["options"]) != 3
            or not isinstance(data["active"], list)
            or len(data["active"]) > 41
        ):
            raise ValueError
        (
            guild,
            channel,
            session,
            life,
            phase,
            adv,
            round_id,
            version,
            offer,
            transition,
            pause,
            revision,
        ) = data["owner"]
        owner = SessionPosition(
            SessionRoute(guild, channel, session),
            Lifecycle(life),
            Phase(phase),
            Advancement(adv),
            round_id,
            version,
            offer,
            transition,
            PauseReason(pause) if pause else None,
            revision,
        )
        active = tuple(_read_rule(i) for i in data["active"])
        active_list_available = list_available(active, profanity)
        if not allow_unavailable and not active_list_available:
            raise ValueError
        options = []
        for raw, targets in data["options"]:
            incoming = decode_choice(raw)
            incoming_list_available = isinstance(incoming, ModeChoice) or list_available(
                (incoming,), profanity
            )
            if allow_unavailable and not (active_list_available and incoming_list_available):
                # Read saved targets for safe recovery, never as activation proof.
                completions = _unproven_completions(owner, active, incoming, targets)
            else:
                completions = tuple(
                    c
                    for c in choice_completions(active, incoming, profanity=profanity)
                    if c.outgoing_id in targets
                )
            if [c.outgoing_id for c in completions] != targets:
                raise ValueError
            options.append(HandOption(incoming, completions))
        token, expires = data["deadline"]
        return OrdinaryHand(
            data["offer"],
            data["picker"],
            owner,
            active,
            Deadline(token, DeadlineKind.CHOICE, utc_datetime(expires)),
            tuple(options),
            data["evaluator"],
            data["parser"],
        )
    except ValueError, TypeError, KeyError, AttributeError, OverflowError, NotImplementedError:
        raise IncompatibleState(
            "Stored offer needs explicit recovery; it cannot be reinterpreted."
        ) from None


def _unproven_completions(owner, active, incoming, targets):
    EvaluationContext(owner, active)
    if inventory_rejection(active) or any(i.instance_id == incoming.instance_id for i in active):
        raise ValueError
    if isinstance(incoming, ModeChoice):
        return (LegalCompletion(None, active, None),)
    completions = []
    for target in replacement_targets(active, incoming):
        if target in targets:
            result = tuple(i for i in active if i.instance_id != target) + (incoming,)
            EvaluationContext(owner, result)
            if not inventory_rejection(result):
                completions.append(LegalCompletion(target, result, None))
    return tuple(completions)


def decode_choice(data):
    instance, card, params = data
    if card in MODE_VALIDATORS and params == []:
        return ModeChoice(instance, card)
    return _read_rule(data)
