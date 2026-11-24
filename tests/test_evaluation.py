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
    ReferenceKind,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message, evaluate_reaction
from lets_play.new_rules.parsing import MAX_PARSER_CODEPOINTS, parse_message
from lets_play.new_rules.profanity import ProfanityList
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


@pytest.mark.parametrize("content", [None, "", "plain text"])
def test_sticker_shock_fails_once_on_sticker_metadata_even_without_text(content):
    decision = evaluate_message(message(content, sticker_ids=(601, 602)), context("NR003"))
    assert decision.evaluations[0].reason_code == "stickers_forbidden"
    assert decision.score.raw_faults == decision.score.ordinary_observations == 1
    assert decision.needs_marker


@pytest.mark.parametrize(
    ("destination", "gif"),
    [
        ("https://example.invalid/duck.GIF?download=1#media", True),
        ("https://media.discordapp.net/attachments/501/502/duck%2EGIF?format=webp", True),
        ("https://tenor.com/view/duck-example-123", True),
        ("https://media.tenor.com/example/tenor.mp4", True),
        ("https://www.giphy.com/gifs/duck-example", True),
        ("https://giphy.com/embed/duck-example", True),
        ("https://media1.giphy.com/media/example/200.mp4", True),
        ("https://klipy.com/gifs/duck-example", True),
        ("https://static.klipy.com/ii/example/duck.gif", True),
        ("https://giphy.com/clips/duck-example", False),
        ("https://klipy.com/clips/duck-example", False),
        ("https://klipy.com/gifs", False),
        ("https://tenor.com/search/duck-gifs", False),
        ("https://giphy.com.example.invalid/gifs/duck-example", False),
        ("https://giphy.com@example.invalid/gifs/duck-example", False),
        ("https://example.invalid/duck.mp4", False),
        ("https://example.invalid/duck.gif.png", False),
        ("https://example.invalid/redirect?asset=duck.gif", False),
    ],
)
def test_gif_link_classification_is_shared_by_both_cards(destination, gif):
    decision = evaluate_message(message(destination), context("NR002", "NR005"))
    assert [result.outcome for result in decision.evaluations] == (
        [Outcome.FAIL, Outcome.PASS] if gif else [Outcome.PASS, Outcome.FAIL]
    )
    assert decision.score.ordinary_faults == decision.score.ordinary_offending_messages == 1


@pytest.mark.parametrize(
    "card",
    [
        "NR007",
        "NR008",
        "NR009",
        "NR013",
        "NR017",
        "NR018",
        "NR019",
        "NR020",
        "NR021",
        "NR037",
        "NR039",
    ],
)
@pytest.mark.parametrize(
    "content",
    [
        "https://example.invalid/A(Long-Path)?size=12#preview",
        "  <https://example.invalid/clip>   ",
        "[https://klipy.com/gifs/duck-pond-example](https://klipy.com/gifs/duck-pond-example)",
        "www.example.invalid/start\nhttps://example.invalid/end",
        "`https://example.invalid/start`",
        "<player@example.invalid>",
    ],
)
def test_url_only_posts_are_unqualified_for_url_filtered_cards(content, card):
    decision = evaluate_message(message(content), context(card))
    assert decision.evaluations[0].outcome is Outcome.NOT_APPLICABLE
    assert decision.score.ordinary_posts == 1
    assert decision.score.ordinary_observations == decision.score.raw_faults == 0
    assert not decision.needs_marker


@pytest.mark.parametrize(
    "card,caption,outcome",
    [
        ("NR007", "quiet", Outcome.PASS),
        ("NR007", "Quiet", Outcome.FAIL),
        ("NR008", "LOUD", Outcome.PASS),
        ("NR008", "Loud", Outcome.FAIL),
        ("NR009", "Duck Pond", Outcome.PASS),
        ("NR009", "Duck 2", Outcome.FAIL),
        ("NR009", "Duck 💯", Outcome.FAIL),
        ("NR009", "Duck <a:1000:123456789012345678>", Outcome.FAIL),
        ("NR013", "Duck Pond", Outcome.PASS),
        ("NR013", "Duck Pond?", Outcome.FAIL),
        ("NR037", "Duck Pond", Outcome.PASS),
        ("NR037", "duck pond", Outcome.FAIL),
    ],
)
def test_url_filtered_cards_check_captions_and_visible_link_labels(card, caption, outcome):
    url = "https://example.invalid/a-MixedCase-URL?size=12"
    for text in (f"**{caption}** {url}", f"[{caption}]({url})"):
        decision = evaluate_message(message(text), context(card))
        assert decision.evaluations[0].outcome is outcome


def test_casing_exemption_preserves_single_gif_detection_and_visible_url_profanity():
    profanity = ProfanityList("turnip")
    ordinary = context("NR007", "NR005")
    active = replace(
        ordinary,
        active_instances=(*ordinary.active_instances, profanity.instantiate("list")),
        profanity=profanity,
    )
    url = "https://klipy.com/gifs/duck-TURNIP-AbC"
    decision = evaluate_message(message(f"[{url}]({url})"), active)
    assert [result.outcome for result in decision.evaluations] == [
        Outcome.NOT_APPLICABLE,
        Outcome.PASS,
        Outcome.FAIL,
    ]
    assert decision.score.raw_faults == 1
    assert decision.evaluations[-1].reason_code == "profanity_forbidden"


def test_no_doubts_still_reads_visible_gif_urls():
    url = "https://klipy.com/gifs/duck-maybe-123?size=12"
    active = context("NR037", "NR013", "NR009", "NR027", "NR005")
    decision = evaluate_message(message(f"[{url}]({url})"), active)
    assert [result.outcome for result in decision.evaluations] == [
        Outcome.NOT_APPLICABLE,
        Outcome.NOT_APPLICABLE,
        Outcome.NOT_APPLICABLE,
        Outcome.FAIL,
        Outcome.PASS,
    ]
    assert decision.score.raw_faults == 1
    assert decision.evaluations[3].reason_code == "maybe_forbidden"
    assert decision.needs_marker

    labeled = evaluate_message(message(f"[Duck Pond]({url})"), context("NR027"))
    assert labeled.evaluations[0].outcome is Outcome.PASS


@pytest.mark.parametrize(
    "card,caption,outcome",
    [
        ("NR017", "one two three four five", Outcome.PASS),
        ("NR018", "one two three four", Outcome.FAIL),
        ("NR019", "duck", Outcome.PASS),
        ("NR020", "short", Outcome.FAIL),
        ("NR021", "x" * 40, Outcome.PASS),
        ("NR039", "quacks", Outcome.PASS),
        ("NR039", "quacker", Outcome.FAIL),
    ],
)
def test_brevity_applies_thresholds_to_caption_text(card, caption, outcome):
    url = "https://example.invalid/a-deliberately-long-address-with-several-words"
    for text in (f"**{caption}** {url}", f"[{caption}]({url})"):
        decision = evaluate_message(message(text), context(card))
        assert decision.evaluations[0].outcome is outcome


@pytest.mark.parametrize(
    "content,card,outcome",
    [
        ("[https://example.invalid](https://example.invalid)duck", "NR019", Outcome.PASS),
        ("pre<https://example.invalid>fix", "NR019", Outcome.PASS),
        (
            "[one two <https://example.invalid> three four five](https://other.invalid)",
            "NR018",
            Outcome.PASS,
        ),
        ("(https://example.invalid/a(b))!", "NR019", Outcome.FAIL),
        ("https://example.invalid/a(b) " + "x" * 40 + "!", "NR021", Outcome.FAIL),
    ],
)
def test_brevity_keeps_visible_text_and_punctuation_around_urls(content, card, outcome):
    decision = evaluate_message(message(content), context(card))
    assert decision.evaluations[0].outcome is outcome


def test_sticker_gif_link_and_gif_upload_keep_independent_rule_faults():
    active = context("NR026", "NR017", "NR015", "NR004", "NR008", "NR002")
    url = "https://klipy.com/gifs/duck-pond-example"
    events = (
        message(None, sticker_ids=(601,)),
        message(f"[{url}]({url})"),
        message(None, attachments=(AttachmentSnapshot(501, "duck.gif", "image/gif"),)),
    )
    decisions = [evaluate_message(event, active) for event in events]
    assert [item.score.raw_faults for item in decisions] == [0, 1, 2]
    assert [item.score.ordinary_observations for item in decisions] == [1, 1, 1]
    assert [item.needs_marker for item in decisions] == [False, True, True]
    assert decisions[1].evaluations[1].outcome is Outcome.NOT_APPLICABLE
    assert decisions[1].evaluations[4].outcome is Outcome.NOT_APPLICABLE
    assert decisions[1].evaluations[5].reason_code == "gif_forbidden"


@pytest.mark.parametrize(
    ("filename", "mime", "gif"),
    [
        ("duck.GIF", None, True),
        ("duck.GIF", "application/octet-stream", True),
        ("duck.png", "IMAGE/GIF; charset=binary", True),
        ("duck.gif", "image/png", False),
        ("duck.gif", "video/mp4", False),
        ("duck.gif.png", None, False),
        ("duck.webp", "image/webp", False),
    ],
)
def test_gif_attachment_prefers_specific_mime_and_falls_back_to_extension(filename, mime, gif):
    event = message(None, attachments=(AttachmentSnapshot(501, filename, mime),))
    decision = evaluate_message(event, context("NR002", "NR004", "NR005"))
    assert [result.outcome for result in decision.evaluations] == [
        Outcome.FAIL if gif else Outcome.PASS,
        Outcome.FAIL,
        Outcome.NOT_APPLICABLE,
    ]
    assert decision.score.ordinary_faults == 1 + gif
    assert decision.score.ordinary_offending_messages == 1


@pytest.mark.parametrize(
    "extra_media",
    [
        {"attachments": (AttachmentSnapshot(501, "duck.png", "image/png"),)},
        {"sticker_ids": (601,)},
        {"is_voice_message": True},
    ],
)
def test_added_media_disqualifies_the_single_gif_link_exception(extra_media):
    decision = evaluate_message(
        message("https://klipy.com/gifs/duck-example", **extra_media), context("NR002", "NR005")
    )
    assert [result.outcome for result in decision.evaluations] == [Outcome.FAIL, Outcome.FAIL]
    assert decision.score.ordinary_faults == 2 and decision.score.ordinary_offending_messages == 1


def test_moving_pictures_flags_a_captioned_masked_gif_and_upload_once_and_respects_whispers():
    active = context("NR002", "NR004", "NR005")
    content = "caption [duck](https://klipy.com/gifs/duck-example)"
    attachments = (AttachmentSnapshot(501, "duck.gif", "image/gif"),)
    decision = evaluate_message(message(content, attachments=attachments), active)
    assert [result.outcome for result in decision.evaluations] == [Outcome.FAIL] * 3
    assert decision.score.ordinary_faults == 3 and decision.score.ordinary_offending_messages == 1
    whisper = evaluate_message(message(f"-# {content}", attachments=attachments), active)
    assert whisper.evaluations == () and not whisper.needs_marker


def test_expression_prohibitions_compose_and_keep_the_existing_whisper_exemption():
    active = context("NR001", "NR003", "NR005")
    content = "🦆 [pond](https://example.invalid)"
    decision = evaluate_message(message(content, sticker_ids=(601,)), active)
    assert [result.outcome for result in decision.evaluations] == [Outcome.FAIL] * 3
    assert decision.score.raw_faults == 3
    assert decision.score.ordinary_offending_messages == 1 and decision.needs_marker

    whisper = evaluate_message(message(f"-# {content}", sticker_ids=(601,)), active)
    assert whisper.evaluations == ()
    assert whisper.score.whispers == 1 and not whisper.needs_marker

    media = evaluate_message(
        message(None, attachments=(AttachmentSnapshot(501, "pond.gif"),)), active
    )
    assert [result.outcome for result in media.evaluations] == [
        Outcome.NOT_APPLICABLE,
        Outcome.PASS,
        Outcome.NOT_APPLICABLE,
    ]


@pytest.mark.parametrize(
    ("reference_kind", "target", "expected"),
    [
        (ReferenceKind.REPLY, 501, Outcome.PASS),
        (ReferenceKind.REPLY, None, Outcome.FAIL),
        (ReferenceKind.NONE, None, Outcome.FAIL),
        (ReferenceKind.NONE, 501, Outcome.FAIL),
        (ReferenceKind.FORWARD, 501, Outcome.FAIL),
        (ReferenceKind.THREAD_STARTER, 501, Outcome.FAIL),
    ],
)
def test_pass_it_on_uses_native_reply_reference_even_for_media_only_posts(
    reference_kind, target, expected
):
    post = message(
        None, reference_kind=reference_kind, referenced_message_id=target, sticker_ids=(601,)
    )
    decision = evaluate_message(post, context("NR023", "NR040"))
    assert [item.outcome for item in decision.evaluations] == [Outcome.NOT_APPLICABLE, expected]
    assert decision.score.ordinary_observations == 1
    assert decision.score.ordinary_faults == int(expected is Outcome.FAIL)


@pytest.mark.parametrize("content", ["> quoted chat", "https://discord.com/channels/101/202/501"])
def test_pass_it_on_does_not_accept_a_text_reference(content):
    decision = evaluate_message(message(content), context("NR040"))
    assert decision.evaluations[0].reason_code == "reply_required" and decision.needs_marker


def test_mention_and_reply_rules_score_separately_on_the_same_post():
    active = context("NR023", "NR040")
    content = "@here and <@123456>"
    post = message(content)
    decision = evaluate_message(post, active)
    assert [result.outcome for result in decision.evaluations] == [Outcome.FAIL, Outcome.FAIL]
    assert decision.score.ordinary_faults == 2 and decision.score.ordinary_offending_messages == 1

    reply = replace(post, reference_kind=ReferenceKind.REPLY, referenced_message_id=501)
    decision = evaluate_message(reply, active)
    assert [result.outcome for result in decision.evaluations] == [Outcome.FAIL, Outcome.PASS]
    assert decision.score.ordinary_quality == Fraction(1, 2)
    assert evaluate_message(replace(post, content=f"-# {content}"), active).evaluations == ()


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
        "red <a:emo:123456> duck",
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
    with pytest.raises(ValueError, match="valid frozen list revision"):
        context("NR032")


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
