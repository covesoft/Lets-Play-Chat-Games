"""Transient source, logical-content, and formatting views. No HTTP, logging, or DB."""

from dataclasses import dataclass, field
from enum import StrEnum
from unicodedata import category, normalize

import emoji
import regex
from markdown_it.token import Token

from lets_play.new_rules.markdown import CUSTOM_EMOJI_MARKUP, make_markdown_parser

PARSER_VERSION = "discord-text-1"
MAX_PARSER_CODEPOINTS = 20_000
_APOSTROPHES = "'\u2018\u2019\u201a\u201b\u02bc\u02bb\uff07`\u00b4\u2032\u2035\u055a"
_APOSTROPHE_MAP = str.maketrans({char: "'" for char in _APOSTROPHES})
_ALNUM = rf"[\p{{L}}\p{{N}}--[{_APOSTROPHES}]]"
_BODY = rf"[\p{{L}}\p{{M}}\p{{N}}--[{_APOSTROPHES}]]"
_WORDS = regex.compile(rf"{_ALNUM}{_BODY}*(?:[{_APOSTROPHES}]{_ALNUM}{_BODY}*)*", regex.V1)
_GRAPHEME = regex.compile(r"\X")
_QUOTATION = regex.compile(r"\p{Quotation_Mark}\Z")
_MASS_MENTION = regex.compile(r"(?<![\w@])@(?:here|everyone)(?!\w)")
_BARE_LINK = regex.compile(
    r"""(?ix)(?<![\w@./:\-])(?:
        (?:https?://|www\.|(?:discord\.gg|discord(?:app)?\.com/invite)/)
        (?:[\p{L}\p{N}]|\[[0-9a-f:]+\])
        (?:(?![,;](?:https?://|www\.))[^\s<>`"|\ufffc])*
        |mailto:[^@\s<>`"|\ufffc]+@[\p{L}\p{N}][\p{L}\p{N}.-]*
    )"""
)


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

    @property
    def letter_count(self) -> int:
        return sum(char.isalpha() and char not in _APOSTROPHES for char in self.text)


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
    _sole_explicit_link: Link | None = field(default=None, repr=False)
    has_explicit_mention: bool = False

    @property
    def has_text(self) -> bool:
        return self.raw is not None and self.raw != ""

    @property
    def is_whisper(self) -> bool:
        return self.raw is not None and self.raw.startswith("-# ")

    @property
    def has_emoji(self) -> bool:
        return (
            bool(emoji.emoji_list(self.logical))
            or any(CUSTOM_EMOJI_MARKUP.fullmatch(obj) for obj in self.objects)
            or CUSTOM_EMOJI_MARKUP.search(self.logical) is not None
        )

    @property
    def shared_links(self) -> tuple[Link, ...]:
        """Explicit links and visible bare URLs for rules that inspect destinations."""
        destinations = {link.destination for link in self.links}
        bare = []
        for match in _BARE_LINK.finditer(self.logical):
            destination = match[0].rstrip(".,!?:;’'”")
            # Keep balanced URL parentheses and IPv6 brackets, but drop prose wrappers.
            for left, right in (("(", ")"), ("[", "]"), ("{", "}")):
                excess = max(0, destination.count(right) - destination.count(left))
                if excess:
                    trimmed = destination.rstrip(right)
                    destination = destination[: max(len(trimmed), len(destination) - excess)]
            if destination not in destinations:
                bare.append(Link(destination, masked=False))
                destinations.add(destination)
        return self.links + tuple(bare)

    def single_link(self, shared_links: tuple[Link, ...]) -> Link | None:
        """Match one complete source link, keeping captions and duplicate URLs distinct."""
        explicit = self._sole_explicit_link
        # Angle links have only one destination even if Markdown normalized its URL.
        if explicit is not None:
            return explicit if not explicit.masked or len(shared_links) == 1 else None
        if len(shared_links) != 1:
            return None
        link = shared_links[0]
        source = (self.raw or "").strip()
        if not link.masked and source == link.destination and _BARE_LINK.fullmatch(source):
            return link
        return None

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
        return grapheme_count(self.logical.strip())

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


def grapheme_count(text: str) -> int:
    return sum(1 for _ in _GRAPHEME.finditer(text))


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
) -> tuple[str, bool]:
    pieces = []
    has_mass_mention = False
    for token in tokens:
        match token.type:
            case "text":
                pieces.append(token.content)
                if not has_mass_mention and "@" in token.content:
                    has_mass_mention = bool(
                        _MASS_MENTION.search(_BARE_LINK.sub(" ", token.content))
                    )
            case "text_special":
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
    return "".join(pieces), has_mass_mention


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
    has_mass_mention = False
    source = content or ""
    normalized_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Keep uncovered source lines literal. In particular, the upstream nesting
    # guard may omit an inner block; it must never erase its evaluable content.
    lines = normalized_lines.copy()
    parser = make_markdown_parser()
    tokens = parser.parse(source)
    for token in tokens:
        match token.type:
            case "inline":
                text, inline_mass_mention = _inline(token.children or [], styles, links, objects)
                has_mass_mention |= inline_mass_mention
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
    sole_link = None
    if len(links) == 1 and len(tokens) == 3 and tokens[0].type == "paragraph_open":
        children = tokens[1].children or []
        if children and children[0].type == "link_open" and children[-1].type == "link_close":
            sole_link = links[0]
    return MessageView(
        content,
        logical,
        words,
        frozenset(styles),
        tuple(links),
        tuple(objects),
        _sole_explicit_link=sole_link,
        has_explicit_mention=has_mass_mention or any(obj.startswith("<@") for obj in objects),
    )
