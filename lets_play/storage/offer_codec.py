"""Versioned frozen offer configuration. No witnesses, chat or arbitrary payloads."""

import json
from datetime import UTC, datetime, timedelta

from lets_play.new_rules.compatibility import legal_completions
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
from lets_play.new_rules.evaluation import EVALUATOR_VERSION
from lets_play.new_rules.hands import HandOption, OrdinaryHand
from lets_play.new_rules.parsing import PARSER_VERSION
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


def decode_hand(value: str) -> OrdinaryHand:
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
        options = []
        for raw, targets in data["options"]:
            incoming = _read_rule(raw)
            completions = tuple(
                c for c in legal_completions(active, incoming) if c.outgoing_id in targets
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
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, NotImplementedError):
        raise IncompatibleState(
            "Stored offer needs explicit recovery; it cannot be reinterpreted."
        ) from None
