"""Transient source, logical-content, and formatting views. No HTTP, logging, or DB."""

from dataclasses import dataclass, field
from enum import StrEnum
from unicodedata import category, normalize

import emoji
import regex
from markdown_it.token import Token

from lets_play.new_rules.markdown import make_markdown_parser

PARSER_VERSION = "discord-text-1"
MAX_PARSER_CODEPOINTS = 20_000
_APOSTROPHES = "'\u2018\u2019\u201a\u201b\u02bc\u02bb\uff07`\u00b4\u2032\u2035\u055a"
_APOSTROPHE_MAP = str.maketrans({char: "'" for char in _APOSTROPHES})
_ALNUM = rf"[\p{{L}}\p{{N}}--[{_APOSTROPHES}]]"
_BODY = rf"[\p{{L}}\p{{M}}\p{{N}}--[{_APOSTROPHES}]]"
_WORDS = regex.compile(rf"{_ALNUM}{_BODY}*(?:[{_APOSTROPHES}]{_ALNUM}{_BODY}*)*", regex.V1)
_GRAPHEME = regex.compile(r"\X")
_QUOTATION = regex.compile(r"\p{Quotation_Mark}\Z")


class ParserLimitError(ValueError):
    """The caller must handle this visibly rather than score truncated content."""


class Formatting(StrEnum):
    BOLD = "bold"
    ITALIC = "italic"
    UNDERLINE = "underline"
    STRIKETHROUGH = "strikethrough"
    SPOILER = "spoiler"
    INLINE_CODE = "inline_code"
    CODE_BLOCK = "code_block"
    HEADING = "heading"
    SUBTEXT = "subtext"
    BLOCKQUOTE = "blockquote"
    LIST = "list"
    MASKED_LINK = "masked_link"
    ANGLE_LINK = "angle_link"


@dataclass(frozen=True, slots=True)
class Word:
    text: str = field(repr=False)
    key: str = field(repr=False)
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Link:
    destination: str = field(repr=False)
    masked: bool


@dataclass(frozen=True, slots=True)
class MessageView:
    raw: str | None = field(repr=False)
    logical: str = field(repr=False)
    words: tuple[Word, ...] = field(repr=False)
    formatting: frozenset[Formatting]
    links: tuple[Link, ...] = field(default=(), repr=False)
    objects: tuple[str, ...] = field(default=(), repr=False)
    parser_version: str = PARSER_VERSION

    @property
    def has_text(self) -> bool:
        return self.raw is not None and self.raw != ""

    @property
    def is_whisper(self) -> bool:
        return self.raw is not None and self.raw.startswith("-# ")

    @property
    def word_keys(self) -> tuple[str, ...]:
        return tuple(word.key for word in self.words)

    @property
    def ending(self) -> str:
        result = self.logical.rstrip()
        while result and (
            result[-1] in _APOSTROPHES
            or _QUOTATION.fullmatch(result[-1])
            or category(result[-1]) == "Pe"
        ):
            result = result[:-1].rstrip()
        return result

    @property
    def character_count(self) -> int:
        """User-perceived graphemes in trimmed logical text; not bytes/code points."""
        return sum(1 for _ in _GRAPHEME.finditer(self.logical.strip()))

    @property
    def has_line_break(self) -> bool:
        return self.raw is not None and ("\n" in self.raw or "\r" in self.raw)

    def ends_with_word(self, word: str) -> bool:
        if not self.words or self.words[-1].key != word_key(word):
            return False
        tail = self.logical[self.words[-1].end :]
        return all(
            char.isspace() or char in _APOSTROPHES or category(char).startswith("P")
            for char in tail
        )

    def starts_with_word(self, word: str) -> bool:
        if not self.words or self.words[0].key != word_key(word):
            return False
        return is_word_gap(self.logical[: self.words[0].start])


def word_key(word: str) -> str:
    # NFC leaves literal punctuation intact: NFKC would turn … into ... .
    return normalize("NFC", word).translate(_APOSTROPHE_MAP).casefold()


def is_word_gap(text: str) -> bool:
    """Punctuation/spacing between phrase words, including apostrophe substitutes.

    Visible symbols and Discord object placeholders interrupt a phrase. U+2212
    is accepted as a common hyphen substitute, as in ordinary word tokenization.
    """
    # Some standalone emoji (for example ‼) have Unicode punctuation categories.
    if emoji.emoji_list(text):
        return False
    return all(
        char.isspace() or char in _APOSTROPHES or category(char).startswith("P") or char == "−"
        for char in text
    )


def _inline(
    tokens: list[Token], styles: set[Formatting], links: list[Link], objects: list[str]
) -> str:
    pieces = []
    for token in tokens:
        match token.type:
            case "text" | "text_special":
                pieces.append(token.content)
            case "softbreak" | "hardbreak":
                pieces.append("\n")
            case "code_inline":
                styles.add(Formatting.INLINE_CODE)
                pieces.append(token.content)
            case "discord_object":
                objects.append(token.content)
                pieces.append("\ufffc")
            case "strong_open":
                styles.add(Formatting.UNDERLINE if token.markup == "__" else Formatting.BOLD)
            case "em_open":
                styles.add(Formatting.ITALIC)
            case "s_open":
                styles.add(Formatting.STRIKETHROUGH)
            case "spoiler_open":
                styles.add(Formatting.SPOILER)
            case "link_open":
                masked = token.markup != "autolink"
                links.append(Link(token.attrGet("href") or "", masked))
                if masked:
                    styles.add(Formatting.MASKED_LINK)
                else:
                    styles.add(Formatting.ANGLE_LINK)
    return "".join(pieces)


def parse_message(content: str | None) -> MessageView:
    if content is not None and not isinstance(content, str):
        raise TypeError("Message content must be text or None.")
    if content is not None and len(content) > MAX_PARSER_CODEPOINTS:
        raise ParserLimitError(
            "Message exceeds the parser input limit; do not score truncated text."
        )
    styles: set[Formatting] = set()
    links: list[Link] = []
    objects: list[str] = []
    source = content or ""
    normalized_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Keep uncovered source lines literal. In particular, the upstream nesting
    # guard may omit an inner block; it must never erase its evaluable content.
    lines = normalized_lines.copy()
    parser = make_markdown_parser()
    for token in parser.parse(source):
        match token.type:
            case "inline":
                text = _inline(token.children or [], styles, links, objects)
                start, stop = token.map
                parts = text.split("\n")
                # Source maps preserve blank-line boundaries without adding
                # whitespace between adjacent inline formatting fragments.
                lines[start:stop] = parts + [""] * max(0, stop - start - len(parts))
            case "fence":
                styles.add(Formatting.CODE_BLOCK)
                start, stop = token.map
                parts = [""] + token.content.rstrip("\n").split("\n")
                lines[start:stop] = (parts + [""] * (stop - start))[: stop - start]
            case "heading_open":
                styles.add(Formatting.HEADING)
            case "subtext_open":
                styles.add(Formatting.SUBTEXT)
            case "blockquote_open":
                styles.add(Formatting.BLOCKQUOTE)
            case "bullet_list_open" | "ordered_list_open":
                styles.add(Formatting.LIST)
    logical = "\n".join(lines)
    words = tuple(
        Word(match[0], word_key(match[0]), match.start(), match.end())
        for match in _WORDS.finditer(logical)
    )
    return MessageView(content, logical, words, frozenset(styles), tuple(links), tuple(objects))
