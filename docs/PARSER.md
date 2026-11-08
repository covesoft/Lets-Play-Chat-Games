# Offline text API

The offline domain package provides plain-data contracts, transient text views,
and fifteen ordinary text predicates. It is not connected to Discord event handlers.
Candidate event evaluation and exact score values are described in
[EVALUATION.md](EVALUATION.md). Schema 2 supplies an offline event unit of work
([STORAGE.md](STORAGE.md)); a session engine, playable commands, and the full
card catalog remain absent. The live shell does not invoke game evaluation.

## Entry points

```python
from lets_play.new_rules.contracts import RuleInstance
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.text_rules import evaluate_text_rule

view = parse_message("**over...**")
result = evaluate_text_rule(view, RuleInstance("example-instance", "NR034"))
```

`RuleEvaluation` returns an instance ID, typed pass/fail/not-applicable outcome,
and a stable reason code on failure. Supported text IDs are NR007, NR008, NR010,
NR011, NR013, NR016, NR017, NR019, NR022, NR024, NR025, NR027, NR033, NR034, and
NR036. The current [ordinary subset](HANDS.md) lists their behavior and metadata.
Other card IDs raise `NotImplementedError`; they never receive a default successful result.
The engine must select the active instances and phase before invoking ordinary
predicates. No ordinary predicate should run as mode enforcement.

`parse_message()` accepts a string or `None`. It returns:

| Field / property | Meaning |
| --- | --- |
| `raw` | Unchanged received text, transient and excluded from repr |
| `logical` | Content after supported structure is parsed; transient and excluded from repr |
| `words`, `word_keys` | Word spans and normalized identities, with no substring matching |
| `formatting` | Recognized style features, independently of content |
| `links` | Destinations and masked/angle-link distinction; never fetched |
| `objects` | Syntactically recognized Discord emoji/mention/timestamp references |
| `ending` | Logical content with trailing whitespace and closing quote/bracket punctuation removed |
| `character_count` | Grapheme clusters in trimmed logical content, not bytes/code points |
| `has_line_break` | Whether the original input includes a CR or LF |
| `is_whisper` | Exact received `-# ` prefix, without first trimming the input |

## Initial parser policy

The parser uses **markdown-it-py 4.2.0** tokenization with Discord-oriented rules.
It consumes tokens directly and never renders HTML. HTML parsing, HTML entity
decoding, image syntax, reference links, setext headings, horizontal rules, and
implicit indented code blocks are disabled. The source remains available even
when syntax is unsupported.

Supported style features include asterisk emphasis/bold, underscore italic and
underline, strikethrough, paired spoilers, inline/backtick-fenced code, headings
of levels 1–3, subtext, blockquotes, lists, and masked/angle links. Leading heading
and subtext markers require a space. Headings do not remove arbitrary hash text
at the end of a line. The initial link subset accepts HTTP(S)/mailto destinations;
relative destinations remain literal text. Raw URL text is not a complete URL/GIF
detector: that capability must be implemented separately before enabling those rules.

Valid formatting wrappers are excluded from logical word/end checks but remain
in the formatting set. Inline/fenced code is treated as literal content inside
its wrapper; nested markup in code is not stripped again. Spoilers expose their
contents to evaluation, and do not create an exemption. Escapes preserve their
literal displayed characters. Unmatched syntax is retained rather than erased.
Angle-link wrappers and masked links count as authored formatting; bare URLs do
not acquire a formatting flag just because Discord may make them clickable.

Custom emoji, mention, channel, and timestamp syntax is represented by the object
replacement character U+FFFC in logical text. This keeps hidden IDs/names out of
word counts and prevents concatenating neighboring words across an object.
An object after an ending word blocks that ending. Inside code, the same syntax
stays literal. Recognition is syntactic and does not establish emoji availability,
mention resolution, or client rendering in a particular guild.

Word runs contain Unicode letters/numbers and combining marks. Apostrophes are
retained only inside a word; supported straight/curly/modifier/fullwidth and
common typographic substitutes share a matching identity. Hyphens/dashes separate
words. Matching uses NFC, apostrophe normalization, and casefolding; displayed
case/text and literal punctuation remain intact. A Unicode ellipsis is not
expanded into three periods. The period predicate requires a terminal run of at least
three consecutive ASCII periods (owner clarification, v0.3.2). Longer runs pass;
a lone Unicode ellipsis or separated dots do not. Word endings may be followed by whitespace/punctuation,
but not another word, a symbol, or a Discord object.

Empty/absent text is not applicable to these text predicates. Present whitespace,
punctuation-only, and heading-only text remains applicable and can fail a required
word/ending. The exact whole-message whisper prefix makes these text predicates
not applicable. The candidate evaluator accounts for whisper traffic in an active
position; complete session routing/activity scheduling remains engine work.

## Frozen phrase input

NR036 requires an immutable `RuleInstance.parameters` entry named `phrase`; an
optional `phrase_revision` records its source revision. For example:

```python
instance = RuleInstance(
    "clause-example",
    "NR036",
    (("phrase", "red duck"), ("phrase_revision", "example-revision")),
)
result = evaluate_text_rule(parse_message("A **RED** duck appeared."), instance)
```

Matching uses contiguous normalized word identities, not substrings. Spacing,
quotes, punctuation, and hyphen variants may separate the words; another word,
visible symbol, emoji, or Discord object interrupts the phrase. Markup wrappers
are handled by the same logical view as the other predicates. Phrase punctuation
is not an additional literal-match requirement. Single words obey the same word
boundaries and apostrophe/case/canonical-Unicode normalization.

Missing, wordless, formatted, object-containing, or unknown parameter inputs raise
`RuleConfigurationError` even when the message has no text. The parser input limit
also applies to phrase input. Error messages do not echo the phrase. This is an
evaluator input contract, not a guild-bank suitability/profanity/length policy or
a phrase-offer/consumption service. No production word list is used or needed.

## Additional predicate policy

NR007/NR008 compare logical text with Python's Unicode lower/uppercase mapping.
Uncased scripts/numbers do not require an alphabetic letter to be present; a
titlecase letter must meet the selected mapping, and lowercase `ß` fails uppercase
while `SS` passes. Markup wrappers/object identifiers are not displayed letters.

NR010 requires the quote-aware logical ending to finish with ASCII `?`; repeated
question marks are allowed. NR013 forbids ASCII `?` anywhere in logical content,
including code/spoilers. Fullwidth `？` is not silently mapped to ASCII.

NR017 counts at most five parser words; NR019 requires exactly one. Numbers count
as words, contractions retain internal apostrophes, hyphens split words, and opaque
Discord objects/emoji are not word credit. Present wordless text can satisfy a
maximum but fails an exact-one requirement. NR022 disallows repeated normalized
word identities; canonical Unicode, case and apostrophe variants compare equally.

NR024/NR033 require the first logical word to be `yes`/`please`, through supported
heading/formatting wrappers. `yesterday`/`pleased` and an earlier different word
do not qualify. NR025/NR027 forbid whole word identities `sorry`/`maybe` without
substring matching or contraction expansion (`sorry's` is its own word).
Leading whitespace/quote/punctuation may precede a required first word; a leading
visible symbol, emoji, or Discord object blocks it. Heading wrappers are already
handled in the logical view.

The original 73 parser fixtures remain; `ordinary_rule_examples.json` adds 100
independently specified boundary/applicability/composition examples for these
eleven additional predicates. This does not implement the remaining catalog.

## Bounds and verification

`PARSER_VERSION` is `discord-text-1`. Inputs above 20,000 code points raise
`ParserLimitError`; the future application must handle that visibly rather than
score a truncated message. This is a parser resource guard, not a new game rule.
The underlying nesting guard is 64; source lines not represented by parsed blocks
remain literal so deep nesting cannot silently erase evaluable text. No exact
Discord-client parity is claimed for unsupported/malformed or deeply nested syntax.

Raw text, word tokens, destinations, and object references are transient. They
must not be persisted, included in an outbox, or logged. Suppressing their repr
helps accidental diagnostics; it is not an automatic transcript-storage safeguard.
There is no persistent parser/message cache.

`tests/fixtures/parser_examples.json` specifies 73 independent expected cases.
Tests additionally cover Unicode normalization/graphemes, typed event identities,
UTC deadlines, deep nesting, and privacy of representations. Tests use harmless
fixtures and no Discord session or production word list.

The current rule suite is deliberately partial. Expanding predicates must preserve
these behaviors and add applicability/boundary/composition examples. Check the
client-specific edges in live integration before claiming complete platform support.

References:

* [Discord formatting](https://support.discord.com/hc/en-us/articles/210298617-Markdown-Text-101-Chat-Formatting-Bold-Italic-Underline)
* [markdown-it-py token API](https://markdown-it-py.readthedocs.io/en/latest/using.html)
* [markdown-it-py parser architecture](https://markdown-it-py.readthedocs.io/en/latest/architecture.html)
* [regex project](https://pypi.org/project/regex/2026.11.7/)
