"""A bounded Discord-oriented token configuration for markdown-it-py.

Only tokens are consumed; HTML is never rendered. Discord-specific token rules
use the upstream state/delimiter APIs. This is a documented subset with fixtures,
not a claim of exact parity with every Discord client rendering edge case.
"""

import re

from markdown_it import MarkdownIt
from markdown_it.rules_block import blockquote, fence, list_block
from markdown_it.rules_block.state_block import StateBlock
from markdown_it.rules_inline.state_inline import Delimiter, StateInline

_OBJECT = re.compile(
    r"<(?:a?:[A-Za-z0-9_]{2,32}:[0-9]{1,20}|@!?[0-9]{1,20}|@&[0-9]{1,20}"
    r"|#[0-9]{1,20}|t:-?[0-9]{1,20}(?::[tTdDfFR])?)>"
)


def _discord_object(state: StateInline, silent: bool) -> bool:
    match = _OBJECT.match(state.src, state.pos)
    if match is None:
        return False
    if not silent:
        token = state.push("discord_object", "", 0)
        token.content = match[0]
    state.pos = match.end()
    return True


def _spoiler_marker(state: StateInline, silent: bool) -> bool:
    if silent or state.src[state.pos] != "|":
        return False
    stop = state.pos
    while stop < state.posMax and state.src[stop] == "|":
        stop += 1
    token = state.push("text", "", 0)
    token.content = state.src[state.pos : stop]
    if stop - state.pos == 2:
        state.delimiters.append(
            Delimiter(
                marker=ord("|"),
                length=0,
                token=len(state.tokens) - 1,
                end=-1,
                open=True,
                close=True,
            )
        )
    state.pos = stop
    return True


def _spoiler_pairs(state: StateInline) -> None:
    groups = [state.delimiters]
    groups.extend(meta["delimiters"] for meta in state.tokens_meta if meta and "delimiters" in meta)
    for delimiters in groups:
        for delimiter in delimiters:
            if delimiter.marker != ord("|") or delimiter.end < 0:
                continue
            closing = delimiters[delimiter.end]
            for index, nesting, name in [
                (delimiter.token, 1, "spoiler_open"),
                (closing.token, -1, "spoiler_close"),
            ]:
                token = state.tokens[index]
                token.type, token.tag, token.nesting = name, "span", nesting
                token.markup, token.content = "||", ""


def _line(state: StateBlock, number: int) -> str:
    return state.src[state.bMarks[number] + state.tShift[number] : state.eMarks[number]]


def _heading(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    line = _line(state, start)
    match = re.match(r"(#{1,3}|-#) (.*)\Z", line)
    if match is None or state.is_code_block(start):
        return False
    if silent:
        return True
    style = "subtext" if match[1] == "-#" else "heading"
    state.line = start + 1
    token = state.push(f"{style}_open", "", 1)
    token.map, token.markup = [start, start + 1], match[1]
    token = state.push("inline", "", 0)
    token.content, token.map, token.children = match[2], [start, start + 1], []
    state.push(f"{style}_close", "", -1)
    return True


def _fence(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    return _line(state, start).startswith("```") and fence(state, start, end, silent)


def _quote(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    line = _line(state, start)
    return (line.startswith("> ") or line.startswith(">>> ")) and blockquote(
        state, start, end, silent
    )


def _list(state: StateBlock, start: int, end: int, silent: bool) -> bool:
    return bool(re.match(r"(?:[-*]|[0-9]+\.) ", _line(state, start))) and list_block(
        state, start, end, silent
    )


def make_markdown_parser() -> MarkdownIt:
    parser = MarkdownIt("commonmark", {"html": False, "typographer": False, "maxNesting": 64})
    parser.disable(
        ["code", "hr", "lheading", "reference", "html_block", "html_inline", "image", "entity"]
    )
    parser.enable("strikethrough")
    upstream_validate = parser.validateLink
    # Discord masked links use actual destinations, not CommonMark's local-file
    # references. Restrict this initial subset to web and email destinations.
    parser.validateLink = lambda value: (
        bool(re.match(r"(?i)(?:https?://|mailto:)", value)) and upstream_validate(value)
    )
    parser.block.ruler.at("heading", _heading, {"alt": ["paragraph", "blockquote"]})
    parser.block.ruler.at("fence", _fence, {"alt": ["paragraph", "blockquote", "list"]})
    parser.block.ruler.at("blockquote", _quote, {"alt": ["paragraph", "blockquote", "list"]})
    parser.block.ruler.at("list", _list, {"alt": ["paragraph", "blockquote"]})
    parser.inline.ruler.before("text", "discord_object", _discord_object)
    parser.inline.ruler.before("text", "discord_spoiler", _spoiler_marker)
    # Let the upstream text rule stop at our additional delimiter even when it
    # occurs in the middle of a text run, rather than only at its beginning.
    parser.inline.terminator_re = re.compile(f"(?:{parser.inline.terminator_re.pattern})|\\|")
    parser.inline.ruler2.before("emphasis", "discord_spoiler", _spoiler_pairs)
    return parser
