# Offline evaluation and score API

These APIs perform no network calls, database writes, enrollment, marker delivery,
or awards. The Discord shell does not invoke them. A decision is a proposed
effect that a future transactional store must accept before applying anything.
Database schema 1 still contains migration bookkeeping only.

## Representative event evaluation

`EvaluationContext(position, active_instances, reaction_exclusions=())` supplies
an immutable owner position, frozen rules, and current emoji exclusions.
`evaluate_message(event, context)` and `evaluate_reaction(event, context)` return
a derived decision or `None` for an ignored event. They require the exact guild,
channel/thread, and session route, a playing position, and a human actor. They
do not decide whether an incoming event is timely, order arrivals, or transition
the session. Warning/choice/held events retain the supplied round/ruleset owner.

| Card ID | Input and applicability |
| --- | --- |
| NR004 | Every ordinary human message; no attachments passes, one or more attachments fails once. Native voice attachments count; link previews do not. |
| NR006 | Ordinary reaction actions by their actor; message creates return not-applicable for this card. |
| NR011, NR016, NR034 | Present ordinary text, including whitespace/punctuation-only text; absent or empty text is not-applicable. |
| NR036 | Present ordinary text against a frozen phrase; each active instance is evaluated separately. |

Unsupported cards raise `NotImplementedError`. Invalid instance configurations
raise `RuleConfigurationError`. Only NR036 can have two active instances, and
their normalized word requirements must differ. These basic guards do not
implement a deck, collective compatibility, legal hands, or phrase-bank services.

An exact received `-# ` prefix exempts the whole message, including its media,
before parsing. Its decision contains only human-traffic/whisper counters, with
no enrollment, score observation, or marker. This also works during a mode.
Non-whisper mode messages explicitly raise `NotImplementedError`; they are never
scored using ordinary predicates. Ordinary reaction enforcement is suspended in
a mode. `None` outside a playing position means no enforcement candidate from
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
The future store must insert a unique guard and apply the score, participation,
and any derived effects in the same transaction. Do not sum repeated candidates
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
gates, whisper exemption, and frozen phrase composition. Persistent exactly-once
application, concurrency/restart recovery, and live Discord behavior require
their own integration evidence.
