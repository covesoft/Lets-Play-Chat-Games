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
existing engine/storage APIs; v0.3.3 wires the ordinary round loop for sixteen
proven templates. Full summaries, modes and automatic restart resume remain open. Resolve the relevant open contracts at their designated gates.

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
closure evidence; only nr_round_resolutions certifies general progression. The v0.3.3 adapter delivers these ordinary transitions; full summaries and
automatic restart resume remain open. Schema 7 composes ordinary refresh/held/revival.

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
v0.3.3 wires ordinary scheduler/publisher dispatch. Automatic recovery
reconciliation remains open.

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
wired for the ordinary preview in v0.3.3. The startup adapter enters the
schema-8 barrier; automatic resume remains open.
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
modes require specialized policy or explicit authorized ending. The startup adapter enters barriers before dispatch. Automatic
resume, send reconciliation/leases, recovery announcements and cleanup remain open.

Schema 9 appends only the Discord outbound-attempt guard. The development preview
uses one bounded FIFO and one publisher per live session, and one bot process per
database. Capture Gateway receipts before async SDK callbacks/HTTP defers. Raw
reaction source IDs use the pinned SDK Gateway session/sequence, independently of
the actor/target offense guard. Claims commit before HTTP; unknown sends pause and
are never blindly retried. All startup routes enter recovery before output/timers.
Current preview recovery permits manager ending only. Do not infer live emoji
usability from markup or permission bits: the admission probe must really react.
Schema 10 preserves all schema-9 claims and namespaces new guards by output
family/identity. Save the actual delivered message and ordered ACK sequence in
the same transaction as its flow acknowledgement. Each live route reads its own
bounded pending-output page; never filter a global first page in memory.

v0.3.3 wires warnings, picker snapshot, ordinary offers, single/partial replacement,
timeouts, cutover and held/revival using the existing engine. Later hands use the
sixteen existing non-phrase validators; opening remains the five-card pool.
Keep the original published deadline on partial target choice and the exact
warning/revival attempt on ACK. /rules shows old enforced rules until cutover,
even while the public transition announces the future rules. Revival quorum
preserves the same open round. Silent inactivity expiry edits the last known
panel only, with no new-message fallback. Modes, full summary baselines,
automatic resume, final-output retry/24-hour cleanup and public release remain
future increments.

v0.3.4/schema 11 adds only the offline summary capture/ACK foundation. Its capture/ACK writes are not
called by the live adapter. v0.3.5 uses only the read-only live_page method. Use SummaryStore inside the same ordered engine write
unit after actual effects; commit before treating a capture as deliverable.
Initialize timing from the real first activation, never an inferred legacy clock.
Automatic preparation needs both 60 seconds and 25 new human traffic messages.
Only actual public success consumes the frozen counter watermark and advances
time; arrivals during HTTP remain pending. Persist one send claim before HTTP.
Unknown sends block new captures/retries pending future explicit reconciliation.
Same-message ACK replay is a no-op; identity conflicts roll back. Read score pages
by actor key with bounded limits. Private rendering and controls exist in v0.3.5; exact ranking order, public
summary scheduling, round/final summary integration and detailed fault/streak UI
remain separate work.
See docs/SUMMARY_FOUNDATION.md. Keep the existing runtime and public copy intact.

v0.3.5 Summary controls are private, read-only, scoped to the exact saved session
and guild/channel, with no FIFO receipt allocation or state transition. They never
prepare/claim/ACK a public capture or reset its counter/time baseline. Read at most
six score rows for a five-player page by actor key. Round navigation retains its
round number; Current round selects the current owner, and game totals stay live.
Paging/refresh reads new committed values; do not label them frozen snapshots.
Keep all copy/colors editable, mentions non-pinging, and private interim views
footer-free. Preplay /rules must not expose copied mutation buttons. No schema or
slash-command change is part of this increment. See docs/REQUESTED_SUMMARY.md.

v0.3.6 private menu navigation edits the originating ephemeral response after a
message-update defer; only a click from a public panel opens a new private reply.
Use the fresh component interaction token, never edit the public phase panel.
Scope/page buttons re-read scores and refresh in place; no Refresh button is shown.
Keep its old catalog key accepted for private-override compatibility.
Final played-game results sort by exact adjusted score descending, unrated last,
then ID for exact ties. Summary pages remain ID-ordered. Read result rank pages
in actor-keyset batches of 64 with at most eleven candidates for ten displayed
players. Do not sort rounded floats or fetch the full roster. New result controls
carry rank anchors/page labels; legacy ID-offset controls reopen ranked page one.
Result paging also edits an existing private response. Scores, progression,
public-summary baselines and schema remain unchanged.

Patch handoffs must include copy-paste Windows commands from the repository root:
`git apply --check`, `git apply`, the editable reinstall when needed, the relevant
checks, and startup. Include migration/backup commands only when the schema
changes. The owner prefers these commands with every delivered patch.
