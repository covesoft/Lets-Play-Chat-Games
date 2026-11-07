"""Pure routing, event identity, privacy, and boundary contracts; no SDK or DB."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from lets_play.new_rules.contracts import (
    Advancement,
    AttachmentSnapshot,
    Deadline,
    DeadlineKind,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    Outcome,
    PauseReason,
    Phase,
    ReactionEvent,
    ReferenceKind,
    RuleEvaluation,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)


def envelope(kind=EventKind.MESSAGE_CREATE, *, actor=7, source="source", sequence=0):
    return EventEnvelope(SessionRoute(1, 2, "session-a"), kind, source, actor, NOW, NOW, sequence)


def test_event_identity_is_exact_session_and_source_not_wall_clock():
    event = envelope()
    later = replace(event, received_at=NOW + timedelta(days=1), sequence=3)
    assert event.source_key == later.source_key
    assert event.source_key != replace(event, route=SessionRoute(1, 3, "session-b")).source_key
    assert event.source_key != replace(event, kind=EventKind.REACTION_ADD).source_key
    # Arrivals have a separate order even if platform creation timestamps tie.
    assert sorted([later, event], key=lambda e: e.sequence) == [event, later]


def test_reaction_identity_does_not_repeat_for_different_emoji_or_event_delivery():
    first = ReactionEvent(
        envelope(EventKind.REACTION_ADD, source="one"), 99, EmojiIdentity(custom_id=11)
    )
    another = ReactionEvent(
        envelope(EventKind.REACTION_ADD, source="two"), 99, EmojiIdentity(unicode="⭐")
    )
    assert first.offense_key == another.offense_key == ("session-a", 7, 99)
    assert (
        first.offense_key
        != replace(first, envelope=envelope(EventKind.REACTION_ADD, actor=8)).offense_key
    )
    assert first.offense_key != replace(first, target_message_id=100).offense_key


def test_emoji_exclusion_identity_preserves_joiners_and_modifiers():
    assert EmojiIdentity(unicode="⭐").key == EmojiIdentity(unicode="⭐️").key
    assert EmojiIdentity(unicode="👩‍🚀").key != EmojiIdentity(unicode="👩🏽‍🚀").key
    assert EmojiIdentity(unicode="👩‍🚀").key != EmojiIdentity(unicode="👩🚀").key
    assert EmojiIdentity(custom_id=11).key == ("custom", 11)


def test_message_reference_and_human_metadata_do_not_infer_reply_from_a_forward():
    message = MessageEvent(envelope(), reference_kind=ReferenceKind.REPLY, referenced_message_id=90)
    assert message.is_human and message.is_reply
    assert not replace(message, reference_kind=ReferenceKind.FORWARD).is_reply
    assert not replace(message, is_webhook=True).is_human
    assert not replace(message, is_system=True).is_human
    assert not replace(message, is_bot=True).is_human


def test_transient_content_and_frozen_phrase_parameters_are_not_in_reprs():
    content = "PRIVATE_SENTINEL"
    message = MessageEvent(
        envelope(), content, attachments=(AttachmentSnapshot(8, content, content),)
    )
    rule = RuleInstance("clause-a", "NR036", (("phrase", content),))
    assert content not in repr(message) + repr(rule)
    assert message.content == content  # Excluding repr must not discard evaluator input.
    assert rule != replace(rule, instance_id="clause-b")


def test_phase_position_keeps_paused_owner_round_and_ruleset():
    active = SessionPosition(
        SessionRoute(1, 2, "s"),
        Lifecycle.PLAYING,
        advancement=Advancement.HELD,
        round_id="round-five",
        ruleset_version=5,
    )
    paused = replace(active, lifecycle=Lifecycle.PAUSED, pause_reason=PauseReason.REVIVAL)
    assert paused.round_id == active.round_id and paused.ruleset_version == 5
    assert replace(active, phase=Phase.MODE).round_id == active.round_id
    # These values express the approved policy; an actual transition engine is future work.


def test_deadline_retains_identity_and_uses_inclusive_utc_boundary():
    offset = timezone(timedelta(hours=-7))
    deadline = Deadline("original-offer", DeadlineKind.CHOICE, NOW.astimezone(offset))
    assert deadline.expires_at.tzinfo is UTC
    assert not deadline.is_due(NOW - timedelta(microseconds=1))
    assert deadline.is_due(NOW)
    assert deadline.is_due(NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="aware"):
        deadline.is_due(NOW.replace(tzinfo=None))


@pytest.mark.parametrize("revision", [-1, True, 0.5])
def test_ownership_revision_requires_a_nonnegative_integer(revision):
    with pytest.raises(ValueError, match="revision"):
        SessionPosition(
            SessionRoute(1, 2, "session"), Lifecycle.PLAYING, round_id="round", revision=revision
        )


@pytest.mark.parametrize(
    "outcome,reason",
    [
        (Outcome.FAIL, None),
        (Outcome.PASS, "fault"),
        (Outcome.NOT_APPLICABLE, "fault"),
    ],
)
def test_result_cannot_claim_success_and_a_failure_reason(outcome, reason):
    with pytest.raises(ValueError):
        RuleEvaluation("rule-a", outcome, reason)


def test_invalid_contracts_are_rejected_before_effects():
    with pytest.raises(ValueError):
        RuleInstance("one", "NR041")
    with pytest.raises(ValueError):
        RuleInstance("one", "NR036", (("phrase", "one"), ("phrase", "two")))
    with pytest.raises(ValueError):
        replace(envelope(), sequence=-1)
    with pytest.raises(ValueError):
        SessionPosition(SessionRoute(1, 2, "s"), Lifecycle.PLAYING)
    with pytest.raises(ValueError):
        MessageEvent(envelope(EventKind.REACTION_ADD))
    with pytest.raises(ValueError):
        EmojiIdentity(custom_id=1, unicode="⭐")
