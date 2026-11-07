"""Harmless event fixtures for representative predicates and candidate effects."""

from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction

import pytest

from lets_play.new_rules.contracts import (
    Advancement,
    AttachmentSnapshot,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    Outcome,
    PauseReason,
    Phase,
    ReactionEvent,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message, evaluate_reaction
from lets_play.new_rules.parsing import MAX_PARSER_CODEPOINTS, parse_message
from lets_play.new_rules.text_rules import RuleConfigurationError, evaluate_text_rule

ROUTE = SessionRoute(101, 202, "session-a")
POSITION = SessionPosition(ROUTE, Lifecycle.PLAYING, round_id="round-one", ruleset_version=1)
NOW = datetime(2026, 11, 7, tzinfo=UTC)


def context(*cards, position=POSITION, exclusions=()):
    return EvaluationContext(
        position,
        tuple(RuleInstance(f"instance-{index}", card) for index, card in enumerate(cards)),
        exclusions,
    )


def envelope(kind=EventKind.MESSAGE_CREATE, route=ROUTE, actor=303, source="event-a"):
    return EventEnvelope(route, kind, source, actor, NOW, NOW, 1)


def message(content="over...", **kwargs):
    return MessageEvent(envelope(), content, **kwargs)


def reaction(emoji=None, target=404, **kwargs):
    return ReactionEvent(
        envelope(EventKind.REACTION_ADD), target, emoji or EmojiIdentity(unicode="👍"), **kwargs
    )


def phrase(text, instance="clause-one", revision="revision-one"):
    return RuleInstance(instance, "NR036", (("phrase", text), ("phrase_revision", revision)))


def test_composed_text_outcomes_make_one_fractional_observation_and_one_marker():
    decision = evaluate_message(message("**over...**"), context("NR034", "NR011", "NR016"))
    assert [e.outcome for e in decision.evaluations] == [Outcome.PASS, Outcome.PASS, Outcome.FAIL]
    assert decision.score.ordinary_quality == Fraction(2, 3)
    assert decision.score.raw_faults == 1
    assert decision.needs_marker and decision.enroll
    assert decision.position == POSITION
    assert decision.source_key == ("session-a", EventKind.MESSAGE_CREATE, "event-a")
    assert decision.actor_id == 303


@pytest.mark.parametrize("content", [None, ""])
def test_absent_text_is_not_credited_as_text_compliance(content):
    decision = evaluate_message(message(content), context("NR034", "NR006"))
    assert all(e.outcome is Outcome.NOT_APPLICABLE for e in decision.evaluations)
    assert decision.score.ordinary_posts == 1
    assert decision.score.actual_percent is None
    assert decision.enroll and not decision.needs_marker


@pytest.mark.parametrize("content", [" ", "...", "# "])
def test_present_wordless_text_is_still_applicable(content):
    decision = evaluate_message(message(content), context("NR034"))
    assert decision.evaluations[0].outcome is Outcome.FAIL


@pytest.mark.parametrize("content", [None, "", "https://example.invalid/image.png"])
def test_unattached_is_applicable_without_a_caption_and_previews_are_not_files(content):
    decision = evaluate_message(message(content), context("NR004"))
    assert decision.evaluations[0].outcome is Outcome.PASS
    assert decision.score.ordinary_quality == 1


@pytest.mark.parametrize("voice", [False, True])
def test_attachments_fail_once_per_instance_including_native_voice(voice):
    event = message(
        None,
        attachments=(AttachmentSnapshot(501, "harmless.ogg"), AttachmentSnapshot(502, "test.txt")),
        is_voice_message=voice,
    )
    decision = evaluate_message(event, context("NR004", "NR034"))
    assert decision.score.raw_faults == 1
    assert decision.score.ordinary_quality == 0
    assert decision.evaluations[1].outcome is Outcome.NOT_APPLICABLE
    assert decision.needs_marker


@pytest.mark.parametrize("phase", list(Phase))
def test_whisper_exempts_whole_message_media_and_mode_but_counts_traffic(phase):
    event = message(
        "-# " + "x" * (MAX_PARSER_CODEPOINTS + 1),
        attachments=(AttachmentSnapshot(501, "harmless.txt"),),
        sticker_ids=(601,),
    )
    decision = evaluate_message(
        event, context("NR004", "NR034", position=replace(POSITION, phase=phase))
    )
    assert decision.evaluations == ()
    assert decision.score.whispers == decision.score.human_messages == 1
    assert decision.score.ordinary_posts == decision.score.observations == 0
    assert not decision.needs_marker and not decision.enroll


@pytest.mark.parametrize("prefix", [" -# ", "-#", "-#\n"])
def test_near_whisper_prefix_does_not_exempt_the_message(prefix):
    decision = evaluate_message(message(prefix + "missing"), context("NR034"))
    assert decision.score.whispers == 0
    assert decision.score.raw_faults == 1


@pytest.mark.parametrize("field", ["is_bot", "is_webhook", "is_system"])
def test_nonhuman_messages_are_ignored_before_parsing(field):
    event = message("x" * (MAX_PARSER_CODEPOINTS + 1), **{field: True})
    assert evaluate_message(event, context("NR034")) is None


@pytest.mark.parametrize(
    "route",
    [
        SessionRoute(999, 202, "session-a"),
        SessionRoute(101, 999, "session-a"),
        SessionRoute(101, 202, "new-game"),
    ],
)
def test_exact_guild_channel_thread_and_session_routes_are_required(route):
    event = replace(message(), envelope=replace(envelope(), route=route))
    assert evaluate_message(event, context("NR034")) is None
    event = replace(reaction(), envelope=replace(envelope(EventKind.REACTION_ADD), route=route))
    assert evaluate_reaction(event, context("NR006")) is None


@pytest.mark.parametrize(
    "lifecycle", [Lifecycle.LOBBY, Lifecycle.OPENING_CHOICE, Lifecycle.PAUSED, Lifecycle.ENDED]
)
def test_nonplaying_positions_produce_no_enforcement_candidates(lifecycle):
    position = replace(
        POSITION,
        lifecycle=lifecycle,
        pause_reason=PauseReason.REVIVAL if lifecycle is Lifecycle.PAUSED else None,
    )
    assert evaluate_message(message(), context("NR034", position=position)) is None
    assert evaluate_reaction(reaction(), context("NR006", position=position)) is None


@pytest.mark.parametrize("advancement", list(Advancement))
def test_warning_choice_and_held_events_keep_the_supplied_open_round(advancement):
    position = replace(POSITION, advancement=advancement)
    decision = evaluate_message(message(), context("NR034", position=position))
    assert decision.position.round_id == "round-one"
    assert decision.position.ruleset_version == 1
    assert decision.score.ordinary_posts == 1


def test_modes_never_fall_back_to_ordinary_compliance():
    active = context("NR034", "NR006", position=replace(POSITION, phase=Phase.MODE))
    with pytest.raises(NotImplementedError):
        evaluate_message(message(), active)
    assert evaluate_reaction(reaction(), active) is None


@pytest.mark.parametrize(
    "content",
    [
        "red duck",
        "a RED, duck appeared",
        "red-duck",
        "red−duck",
        "red\nduck",
        "**red** duck",
        "`red duck`",
        "||red duck||",
    ],
)
def test_frozen_phrase_matches_contiguous_logical_word_identities(content):
    result = evaluate_text_rule(parse_message(content), phrase("red duck"))
    assert result.outcome is Outcome.PASS


@pytest.mark.parametrize(
    "content",
    [
        "red duckling",
        "bred duck",
        "red small duck",
        "red 🦆 duck",
        "red <a:oof:123456> duck",
        "red <@123456> duck",
        "red + duck",
        "red ‼ duck",
    ],
)
def test_phrase_does_not_match_substrings_extra_words_or_intervening_objects(content):
    assert evaluate_text_rule(parse_message(content), phrase("red duck")).outcome is Outcome.FAIL


def test_phrase_matching_normalizes_quotes_apostrophes_case_and_canonical_unicode():
    result = evaluate_text_rule(parse_message("“DON’T CAFÉ”"), phrase("don't cafe\u0301"))
    assert result.outcome is Outcome.PASS


@pytest.mark.parametrize(
    "apostrophe", ["'", "‘", "’", "‚", "‛", "ʼ", "ʻ", "＇", "`", "´", "′", "‵", "՚"]
)
def test_phrase_apostrophe_substitutes_work_inside_words(apostrophe):
    content = f"DON{apostrophe}T duck"
    assert evaluate_text_rule(parse_message(content), phrase("don't duck")).outcome is Outcome.PASS


@pytest.mark.parametrize(
    "quote", ["'", "‘", "’", "‚", "‛", "ʼ", "ʻ", "＇", "´", "′", "‵", "՚", '"', "“", "”", "«", "»"]
)
def test_frozen_phrase_and_message_accept_surrounding_quote_substitutes(quote):
    frozen = phrase(f"{quote}don't duck{quote}")
    assert evaluate_text_rule(parse_message(f"DON’T {quote}duck"), frozen).outcome is Outcome.PASS


def test_both_missing_phrase_instances_are_two_faults_but_one_marker():
    active = EvaluationContext(POSITION, (phrase("red duck"), phrase("blue goose", "clause-two")))
    missing = evaluate_message(message("over..."), active)
    assert missing.score.raw_faults == 2
    assert missing.score.offending_messages == 1 and missing.needs_marker
    partial = evaluate_message(message("RED duck"), active)
    assert partial.score.ordinary_quality == Fraction(1, 2)
    assert partial.score.raw_faults == 1
    complete = evaluate_message(message("red duck and blue goose"), active)
    assert complete.score.ordinary_quality == 1
    assert not complete.needs_marker
    assert dict(active.active_instances[0].parameters)["phrase_revision"] == "revision-one"


@pytest.mark.parametrize(
    "text",
    ["", "...", "**red duck**", "red 🦆 duck", "<@123456>", "[duck](https://example.invalid)"],
)
def test_invalid_phrase_instances_are_errors_even_on_absent_text(text):
    with pytest.raises(RuleConfigurationError):
        evaluate_text_rule(parse_message(None), phrase(text))


def test_missing_or_unknown_phrase_parameters_cannot_be_silently_ignored():
    for params in [(), (("phrase", "red duck"), ("typo", "not-used"))]:
        with pytest.raises(RuleConfigurationError):
            EvaluationContext(POSITION, (RuleInstance("bad", "NR036", params),))


def test_active_instance_identity_template_and_phrase_caps_are_validated():
    with pytest.raises(RuleConfigurationError):
        EvaluationContext(POSITION, (RuleInstance("same", "NR004"), RuleInstance("same", "NR006")))
    with pytest.raises(RuleConfigurationError):
        context("NR004", "NR004")
    with pytest.raises(RuleConfigurationError):
        EvaluationContext(POSITION, tuple(phrase(f"duck {i}", str(i)) for i in range(3)))
    with pytest.raises(RuleConfigurationError):
        EvaluationContext(POSITION, (phrase("red duck"), phrase("RED-DUCK", "other")))
    with pytest.raises(NotImplementedError):
        context("NR037")


def test_derived_decisions_never_retain_content_attachment_names_or_frozen_phrase_text():
    active = EvaluationContext(POSITION, (phrase("PRIVATE_SENTINEL"),))
    decision = evaluate_message(message("PRIVATE_SENTINEL"), active)
    assert "PRIVATE_SENTINEL" not in repr(decision) + repr(active)
    assert not hasattr(decision, "content") and not hasattr(decision, "attachments")


def test_expressionless_attributes_failure_to_actor_and_never_marks_target():
    decision = evaluate_reaction(reaction(), context("NR006"))
    assert decision.actor_id == 303
    assert decision.dedup_key == ("session-a", 303, 404)
    assert decision.evaluation.outcome is Outcome.FAIL
    assert decision.score.reaction_failures == 1
    assert decision.score.ordinary_posts == decision.score.human_messages == 0
    assert decision.enroll and not decision.needs_marker
    # No target author/content is supplied or fetched, including old, whisper,
    # bot-authored, or media-only targets.


def test_readds_different_emoji_and_later_rounds_share_one_persistence_key():
    first = evaluate_reaction(reaction(), context("NR006"))
    again = evaluate_reaction(reaction(EmojiIdentity(custom_id=701)), context("NR006"))
    later = evaluate_reaction(
        reaction(),
        context("NR006", position=replace(POSITION, round_id="round-two", ruleset_version=2)),
    )
    assert first.dedup_key == again.dedup_key == later.dedup_key
    # Repeated pure evaluation still returns a CANDIDATE. No store acceptance or
    # exactly-once score application is claimed by this test.
    assert evaluate_reaction(reaction(), context("NR006")) == first


def test_other_actors_targets_and_games_have_independent_reaction_keys():
    original = evaluate_reaction(reaction(), context("NR006"))
    other_actor = replace(
        reaction(), envelope=replace(envelope(EventKind.REACTION_ADD), actor_id=999)
    )
    new_route = replace(ROUTE, session_id="session-b")
    other_game = replace(
        reaction(), envelope=replace(envelope(EventKind.REACTION_ADD), route=new_route)
    )
    keys = {
        original.dedup_key,
        evaluate_reaction(other_actor, context("NR006")).dedup_key,
        evaluate_reaction(reaction(target=999), context("NR006")).dedup_key,
        evaluate_reaction(
            other_game, context("NR006", position=replace(POSITION, route=new_route))
        ).dedup_key,
    }
    assert len(keys) == 4


@pytest.mark.parametrize(
    "identity,excluded",
    [
        (EmojiIdentity(custom_id=701), EmojiIdentity(custom_id=701)),
        (EmojiIdentity(unicode="❤"), EmojiIdentity(unicode="❤️")),
    ],
)
def test_excluded_reactions_consume_no_offense_candidate(identity, excluded):
    event = reaction(identity)
    assert evaluate_reaction(event, context("NR006", exclusions=(excluded,))) is None
    assert evaluate_reaction(event, context("NR006")) is not None


def test_bots_and_pre_rule_actions_consume_no_candidate_and_modifiers_stay_distinct():
    assert evaluate_reaction(reaction(is_bot=True), context("NR006")) is None
    assert evaluate_reaction(reaction(), context("NR034")) is None
    assert evaluate_reaction(reaction(), context("NR006")) is not None
    assert (
        evaluate_reaction(
            reaction(EmojiIdentity(unicode="👍🏽")),
            context("NR006", exclusions=(EmojiIdentity(unicode="👍"),)),
        )
        is not None
    )
