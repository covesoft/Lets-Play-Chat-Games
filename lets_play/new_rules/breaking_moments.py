"""Supported Breaking Moment choices and their pure message predicates."""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar

import emoji

from lets_play.new_rules.contracts import MessageEvent, Outcome, ReferenceKind, RuleEvaluation
from lets_play.new_rules.gif_media import is_gif_attachment, is_gif_link
from lets_play.new_rules.parsing import MAX_PARSER_CODEPOINTS, ParserLimitError, parse_message

DRAWING_CONCLUSIONS = "drawing_conclusions"
GETTING_GRAPHIC = "getting_graphic"
WHY_ARE_WE_SHOUTING = "why_are_we_shouting"
BREAKING_MOMENT_SECONDS = 120
EMOJI_BADGE_MIN_MESSAGES = 3
EMOJI_GAMEPLAY_MIN_MESSAGES = 3
GIF_BADGE_MIN_MESSAGES = 5
GIF_GAMEPLAY_MIN_MESSAGES = 2
HEADING_MIN_MESSAGES = 3
EMOJI_REQUIRED = "emoji_only_required"
GIF_REQUIRED = "single_gif_required"
HEADING_REQUIRED = "heading_on_every_line_required"
MODE_FAILURE_REASONS = {
    DRAWING_CONCLUSIONS: EMOJI_REQUIRED,
    GETTING_GRAPHIC: GIF_REQUIRED,
    WHY_ARE_WE_SHOUTING: HEADING_REQUIRED,
}
_CUSTOM_EMOJI = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:[1-9][0-9]{12,19}>")


@dataclass(frozen=True, slots=True)
class ModeChoice:
    instance_id: str
    card_id: str = DRAWING_CONCLUSIONS
    parameters: ClassVar[tuple] = ()

    def __post_init__(self):
        if not isinstance(self.instance_id, str) or not self.instance_id:
            raise ValueError("A Breaking Moment needs an instance identity.")
        if self.card_id not in MODE_FAILURE_REASONS:
            raise ValueError("Unsupported Breaking Moment.")


def mode_ends_at(started_at: datetime) -> datetime:
    return started_at + timedelta(seconds=BREAKING_MOMENT_SECONDS)


def drawing_conclusions(event: MessageEvent, choice: ModeChoice) -> RuleEvaluation:
    text = event.content or ""
    if len(text) > MAX_PARSER_CODEPOINTS:
        raise ParserLimitError(
            "Message exceeds the parser input limit; do not score truncated text."
        )
    valid = (
        not event.attachments
        and not event.sticker_ids
        and not event.is_voice_message
        and _emoji_only(text)
    )
    return RuleEvaluation(
        choice.instance_id,
        Outcome.PASS if valid else Outcome.FAIL,
        None if valid else EMOJI_REQUIRED,
    )


def getting_graphic(event: MessageEvent, choice: ModeChoice) -> RuleEvaluation:
    view = parse_message(event.content)
    valid = False
    if not (
        event.sticker_ids or event.is_voice_message or event.reference_kind is ReferenceKind.FORWARD
    ):
        if event.attachments:
            valid = (
                len(event.attachments) == 1
                and not (event.content or "").strip()
                and is_gif_attachment(event.attachments[0])
            )
        else:
            link = view.single_link(view.shared_links)
            valid = link is not None and is_gif_link(link.destination)
    return RuleEvaluation(
        choice.instance_id,
        Outcome.PASS if valid else Outcome.FAIL,
        None if valid else GIF_REQUIRED,
    )


def why_are_we_shouting(event: MessageEvent, choice: ModeChoice) -> RuleEvaluation:
    text = event.content or ""
    if len(text) > MAX_PARSER_CODEPOINTS:
        raise ParserLimitError(
            "Message exceeds the parser input limit; do not score truncated text."
        )
    outcome = Outcome.NOT_APPLICABLE
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # Standalone underscores and escaped underscores are blank-line fillers.
        if all(word in {"_", r"\_"} for word in line.split()):
            continue
        prefix, _, heading = line.partition(" ")
        if prefix not in {"#", "##", "###"} or not heading.strip():
            return RuleEvaluation(choice.instance_id, Outcome.FAIL, HEADING_REQUIRED)
        outcome = Outcome.PASS
    return RuleEvaluation(choice.instance_id, outcome)


MODE_VALIDATORS = {
    DRAWING_CONCLUSIONS: drawing_conclusions,
    GETTING_GRAPHIC: getting_graphic,
    WHY_ARE_WE_SHOUTING: why_are_we_shouting,
}


def _emoji_only(text: str) -> bool:
    # Preserve boundaries when removing custom emoji so a joiner, flag half, or
    # other unmatched code point cannot combine across a Discord emoji token.
    count = 0
    for part in _CUSTOM_EMOJI.split(text):
        end = 0
        for token in emoji.emoji_list(part):
            if part[end : token["match_start"]].strip():
                return False
            end = token["match_end"]
            count += 1
        if part[end:].strip():
            return False
    return count > 0 or _CUSTOM_EMOJI.search(text) is not None
