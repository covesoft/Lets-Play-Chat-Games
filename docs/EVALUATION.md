# Offline evaluation and score API

These APIs perform no network calls, database writes, enrollment, marker delivery,
or awards. The Discord shell does not invoke them. A decision is a proposed
effect that a transactional store must accept before applying anything. Schema 2
provides the offline [event unit of work](STORAGE.md); the live shell still does
not invoke it.

## Representative event evaluation

`EvaluationContext(position, active_instances, reaction_exclusions=(), profanity=None)`
supplies an immutable owner position, frozen rules, current emoji exclusions,
and an optional in-memory private gameplay list (keyword only).
`evaluate_message(event, context)` and `evaluate_reaction(event, context)` return
a derived decision or `None` for an ignored event. They require the exact guild,
channel/thread, and session route, a playing position, and a human actor. They
do not decide whether an incoming event is timely, order arrivals, or transition
the session. Warning/choice/held events retain the supplied round/ruleset owner.
`SessionPosition.revision` is a nonnegative monotonic ownership revision, distinct
from the ruleset version. The store increments it for every ownership mutation,
including changes that later return to the same visible state.

| Card ID | Input and applicability |
| --- | --- |
| NR001 | Present ordinary text. Recognized Unicode emoji or custom emoji markup fails once. |
| NR002 | Every ordinary human message. A GIF attachment or recognized GIF link fails once. |
| NR003 | Every ordinary human message. One or more Discord sticker IDs fails once, even without a caption. |
| NR004 | Every ordinary human message; no attachments passes, one or more attachments fails once. Native voice attachments count; link previews do not. |
| NR005 | Present ordinary text. Recognized links fail once unless the whole post is a single GIF link. See [GIF detection boundaries](PARSER.md#gif-and-link-rules). |
| NR006 | Ordinary reaction actions by their actor; message creates return not-applicable for this card. |
| NR007–NR009, NR013, NR037 | Inside Voice, SPEAK UP, No Accounting, No Questions, and Decree Mode use visible text after URL removal. Captions and visible link labels are checked. URL-only posts are not-applicable for these cards and still count as ordinary posts. |
| NR010–NR012, NR014–NR016, NR022, NR024–NR031, NR033–NR035, NR038 | Present ordinary text, including whitespace/punctuation-only text. Absent or empty text is not-applicable. No Doubts still checks visible URL words. |
| NR017–NR021, NR039 | Brevity uses the same URL-filtered text as the five typography cards above. |
| NR023 | Present ordinary text. Any explicit user, bot, role, `@here` or `@everyone` mention fails once. Automatic reply pings are ignored. |
| NR032 | Present ordinary text against the configured gameplay list. One or more matches fail the card once. The instance pins a list revision. |
| NR036 | Present ordinary text against a frozen phrase; each active instance is evaluated separately. |
| NR040 | Every ordinary human message. A native reply reference with a target passes. Other posts, including media-only posts without a reply, fail once. |

Unsupported cards raise `NotImplementedError`. Invalid instance configurations
raise `RuleConfigurationError`. Only NR036 can have two active instances, and
their normalized word requirements must differ. These context guards do not
establish full feasibility. Use the separate [witness and hand API](HANDS.md) for
ordinary rules. The live guild phrase-bank service remains pending. With an
active NR032, evaluation requires the matching list revision.

An exact received `-# ` prefix exempts the whole message, including its media,
before parsing. Its decision contains only human-traffic/whisper counters, with
no enrollment, score observation, or marker. Moment of Silence ignores it entirely.
During a saved bout, the flow supplies the `mode` choice to `evaluate_message`.
Drawing Conclusions accepts recognized Unicode or custom emoji with optional
whitespace. An emoji-only forward also qualifies when its snapshot contains
only emoji and its optional note is emoji-only. Forwarded media or an unavailable
snapshot fails. Only Drawing Conclusions evaluates the forwarded snapshot.
Getting Graphic accepts one recognized GIF
link using the [standalone link rules](PARSER.md#gif-and-link-rules), or one GIF
upload without a caption. Automatic link previews require no extra media check.
Stickers, voice messages, forwards and competing links or uploads fail Getting
Graphic. Why Are We Shouting checks each source line for one to three leading
hash signs, an ASCII space, and heading text. Whitespace-only lines and blank
italic spans such as `_ _` are ignored, including when such a span is the only
content in a heading. Escaped or unmatched underscores, list markers, and blank
code, strikethrough, and underline spans are checked. A post with no applicable
text adds traffic. A valid heading with an invisible character as its body can
qualify.
Moment of Silence faults every other human message once, regardless of content
or media. It ignores Discord's native poll posts and automatic poll results.
Whispers and polls create no traffic, observation, marker, or participation in
this mode. The source guard prevents replay scoring after rules return.
Playing Telephone passes native voice messages with one attachment and no
accompanying text, stickers, or poll. Pure forwarded voice messages add traffic
without a score observation or duration credit. Audio uploads, other forwards,
and messages with text fault. The mode uses Discord's voice flag and attachment
duration metadata. Finite native duration of at least 4.0 seconds yields one
qualifying memo for progression. The usual whisper exemption applies.
An applicable outcome contributes one binary observation and a failed post
requests one marker. Mode messages contribute no ordinary participation. A mode
without a supported choice raises `NotImplementedError` on non-whisper posts.
Ordinary reaction enforcement is suspended during a mode. `None` outside a playing position means no enforcement candidate from
this API, not a complete application's lobby/pause traffic policy.

Message decisions carry the supplied position, stable source key, actor ID,
per-instance outcomes, and additive score values. They exclude content, filenames,
media, link destinations, and phrase parameters. `enroll` and `needs_marker` are
requests for future application, not completed side effects. One offending
message requests one marker, even if multiple instances fail.

## Reaction candidates and persistence boundary

Reaction exclusions use `EmojiIdentity.key`: custom emoji compare by ID;
Unicode identities normalize canonical equivalence and presentation selectors
while preserving skin-tone, ZWJ, and other sequence distinctions. Bot, wrong-route,
wrong-phase, excluded, and pre-rule events produce no candidate.

A prohibited reaction decision attributes one failed opportunity to its actor.
It requests enrollment for a reaction-only participant and never requests a
target marker. No target message fetch or inspection is performed, so target age,
content, authorship, and whisper/media status do not introduce exemptions here.

Its `dedup_key` is `(session_id, actor_id, target_message_id)`. Emoji, round, and
rule instance are deliberately absent. Re-adds and later rounds return the same
candidate key. **The evaluator does not remember or reject duplicate candidates.**
The schema-2 unit of work inserts a unique guard and applies the score,
participation, and marker intents in the same transaction. Do not sum repeated candidates
or send effects before that acceptance. Event `source_key` and reaction
`dedup_key` serve different purposes; a fresh source event can still target an
already-counted actor/message pair.

## Exact score values

`ordinary_score(evaluations)` creates one ordinary-post delta. If there are `r`
applicable instances and `v` failures, its observation quality is `(r-v)/r`.
Not-applicable outcomes do not enter `r`. With `r=0`, it records participation
but no accuracy observation. Duplicate instance results raise `ValueError`.

`whisper_score()` records only human traffic and one whisper.
`mode_score(evaluation)` supplies arithmetic for an externally provided mode
result: pass/fail has quality 1/0, not-applicable has no observation. It implements
no mode validator and adds no ordinary participation.
`reaction_score()` represents one candidate failed reaction opportunity.

`ScoreTotals` keeps human traffic, whispers, ordinary participation/observations,
ordinary quality/faults/offending-message counts, mode observations/quality, and
reaction failures separate. It rejects negative/inconsistent counters and
approximate floating-point quality. Values are immutable and add field by field.
Round/session aggregation must add these values, not average percentages.

| Derived property | Calculation |
| --- | --- |
| `quality` (`Q`) | Ordinary plus mode quality |
| `observations` (`n`) | Ordinary plus mode observations |
| `opportunities` (`D`) | `n + reaction_failures` |
| `actual_percent` | `100 * Q / D`, or `None` when `D=0` |
| `ranking_percent()` | `100 * (Q + strength * baseline) / (D + strength)`, or `None` when `D=0` |
| `raw_faults` | Failed ordinary instances plus failed mode posts plus reaction failures |
| `offending_messages` | Offending ordinary posts plus failed mode posts; excludes reactions |

`RankingModel` defaults to strength 10 and baseline 9/10. All quality, actual,
and ranking calculations use standard-library `Fraction`. Comparisons stay exact;
display rounding belongs at the presentation boundary. Actual and adjusted
ranking percentages are separate values. Raw faults are not subtracted from
quality again. No threshold award, picker selection, or game-credit service is
implemented by this arithmetic module.

Tests cover partial-quality messages, unrated participation, reaction-only
players, additive round aggregation, ranking, exact boundary comparisons, phase
gates, whisper exemption, and frozen phrase composition. Separate storage tests
exercise atomic internal effects, duplicate races, cancellation, and reopen.
Full session recovery and live Discord behavior remain separate work.
