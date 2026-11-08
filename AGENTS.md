# Let’s Play development

This repository implements the New Rules chat game in the Let’s Play application.
Owner-provided design notes may be available privately under `design/`. Consult
them before changing game behavior. Never commit, package, or copy their contents
into public documentation. The public `docs/` directory contains operational
documentation only.

Use Python 3.13. `pyproject.toml` owns dependency pins; update `uv.lock` and both
requirements exports together. Run the application with `python -m lets_play`
or the installed `lets-play` command. Use `CHATGAMES_*` configuration names; there
are no legacy launcher/configuration aliases. No token or external private list
is required for offline tests.

Keep the New Rules domain independent of Discord and storage. M1 is accepted and closed at v0.2.9/schema 8. M2 is in progress. Preserve the
existing engine/storage APIs; the v0.3.0 development preview wires only the opening
path. Do not claim later-round play, modes or full recovery from that preview. Resolve the relevant open contracts at their designated gates.

Never commit `.env`, production word lists, databases/sidecars, transcripts, or
media. Use harmless fixtures. Do not contact/invite collaborators or deploy the
bot without instructions. Do not run the live bot just to verify an offline edit.

Owner emoji assets (including names/markup and IDs) are private configuration.
Never put actual owner assets/IDs into public source, defaults, fixtures, examples,
logs, docs or distributions. Use synthetic fixtures only. An absent marker has no
fallback; it must be supplied privately before admitting a game.

All player/public-facing copy and embed colors must be owner-editable near the
file top or in a dedicated presentation file. Current sent copy, card display
labels, buttons, command descriptions and phase colors live in
`lets_play/resources/en-US.json`; use stable keys through `Presentation`. Preserve
owner copy edits when refactoring. Keep protocol IDs/command identifiers separate
from translated text. New strings/colors must follow this boundary. Private
`CHATGAMES_PRESENTATION_PATH` overrides are validated at startup/config check;
keep template placeholders stable and include the catalog in wheel/sdist builds.
See `docs/PRESENTATION.md`.

Run meaningful checks for each change:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m lets_play --check-config
```

For packaging/dependency changes, also run `python -m build` and `python -m pip
check` (or `uv pip check`). Record actual results and remaining limits in the
private implementation notes when available. Never report a mock/offline check
as live Discord or Windows evidence. M2 is authorized in bounded increments.
The owner verified the earlier v0.1.3 shell live, schema-7/WAL/FK bootstrap, and v0.2.8
tests on Windows 11 (944 passed in 70.72s). Source hygiene uses NUL-framed Git I/O.
Later increments still need their own Windows/CI evidence.

Schema 2 is an offline evaluation unit of work, not a transition/admission or
award service. Keep every guard, aggregate, and marker intent in one transaction;
ownership updates require monotonic revisions. Never interpret a candidate or
an uncommitted unit return value as an applied effect. Use bounded store/page
limits and surface overload/contention explicitly. No network waits inside a
write unit. Back up an existing database before schema upgrades.

The ordinary-subset hand API prepares immutable proposals with actual-validator
witnesses; READY does not activate, consume, publish, resolve a round, or commit.
Keep a single template ticket regardless of phrase/target variants. Slice the
authorized phrase window before filtering. Never repair a conflict by removing an
unrelated rule or use empty/whisper/media bypass as a witness. Bounds may withhold
unproven states. Unsupported cards/modes remain outside the subset until verified.

Schema 3 OrdinaryFlow is a later-round offline coordinator. Use one trusted,
contiguous server receipt sequence for every flow action, chat/reaction, and timer.
Never skip a sequence gap or backdate a timer over queued chat. Publication ACKs
are external facts, not output preparation. Old rules apply until the announced
whole-second cutover; missing/late ACK requests explicit recovery. Closures retain
closure evidence; only nr_round_resolutions certifies general progression. Actual delivery and startup
adapter remain unfinished; schema 7 composes ordinary refresh/held/revival.

Schema 4 progression is New Rules only. Score qualification, live credit flag,
global increment and Familiar Face commit together. Resolve actual closures in
round creation order; never resolve an open/revival round. OrdinaryFlow composes
general resolution with cutover. Compose finalize_ended with a trusted terminal
owner update after earlier closures resolve; it neither authorizes nor ends a game.
Do not purge evidence before general finalization and future mode/theme awards.
Reconcile legacy pending closures explicitly before further activation. Unknown
enrollment evidence/version pins require recovery, never guessed streaks/awards.

Schema 5 RoundPacing shares the OrdinaryFlow receipt lane. Register real activation
time/frozen estimate; never guess a used legacy unknown start. Warning preparation
does not start its 30 seconds: only a successful publication ACK does. Snapshot
before the first at/after-cutoff receipt is scored; later events remain old-round
until activation. Preserve exact-score candidate evidence and unrated eligibility.
Rotation service commits only at valid activation, together with general resolution
and next-round pacing. Compose ordinary held/refresh/revival through schema 7;
scheduler/publisher/reconciliation and startup recovery remain.

Schema 6 OpeningFlow reserves real guild/channel capacity before publishing a lobby.
Lobbies/opening/recovery have no nr_sessions/nr_rounds or score effects. Actual
publication starts original 180s/60s clocks. Quorum is starter plus two distinct
humans; joins do not credit a game. First acknowledged activation creates round
one, consumes its phrase/turn, grants deliberate Rulemaker and initializes pacing
in one unit. Its receipt hands over to OrdinaryFlow without dropping its body.
Preplay source guards prevent replayed ignored events becoming scored. Ending
releases the reservation with general finalization; final delivery remains separate.
Persisted entitlement facts are trusted server state, not client-supplied capacity.
Grant authorization/full promotion/settings UI remain M5 work. Legacy slots retain
unknown phrase allowance rather than guessing a downgrade; frozen offers survive.

Schema 7 EvolutionFlow composes held play, complete refresh and revival over that
same receipt lane. Configure only trusted phrase-window/template facts. Recheck
relevant changes before dispatching subsequent choices. Fresh warning/revival
attempts preserve prior snapshots/ACKs; pass the exact attempt to publish_warning.
Refreshed offers inherit a running original deadline. Held scoring continues;
revival pauses scoring and needs three distinct returners, never automatic starter
credit. Quorum preserves the open round; actual activation or ending resolves it.
Held idle counts only new human messages (including whispers); settings-only
restoration does not reset it. Silent expiry releases capacity/general finalization
atomically and requests only an existing-panel update. O-03 final delivery remains
M2. Reopening handles is tested; the explicit schema-8 barrier still needs a live startup adapter.
Do not infer missing legacy revival publication times or waive old failure pauses.

Schema 8 RecoveryFlow is an explicit offline disruption coordinator. Stop dispatch,
page live routes and enter each barrier BEFORE running overdue timers. Normal flow
APIs and output reads fence active recoveries. Resolve uncertain publication using
actual message/time proof or a conclusive never-sent fact; a missing ACK is neither.
Only a successfully published recovery notice starts the 30-second read-in. Resume
rebinds ownership without resetting original clocks, partial choices or scores.
Known lobby/revival expiry still wins while paused. Ignore/guard historical sources
created before actual resumption; do not backfill missing chat or mode success.
Old capability/dependency/recovery pauses, legacy clockless revival and unsupported
modes require specialized policy or explicit authorized ending. Startup adapter,
send reconciliation/leases, real announcements and cleanup remain M2/later work.

Schema 9 appends only the Discord outbound-attempt guard. The development preview
uses one bounded FIFO and one publisher per live session, and one bot process per
database. Capture Gateway receipts before async SDK callbacks/HTTP defers. Raw
reaction source IDs use the pinned SDK Gateway session/sequence, independently of
the actor/target offense guard. Claims commit before HTTP; unknown sends pause and
are never blindly retried. All startup routes enter recovery before output/timers.
Current preview recovery permits manager ending only. Do not infer live emoji
usability from markup or permission bits: the admission probe must really react.
Later-round pacing, modes, full summary baselines, automatic resume, final-output
retry/24-hour cleanup and public release remain future increments.

Patch handoffs must include copy-paste Windows commands from the repository root:
`git apply --check`, `git apply`, the editable reinstall when needed, the relevant
checks, and startup. Include migration/backup commands only when the schema
changes. The owner prefers these commands with every delivered patch.
