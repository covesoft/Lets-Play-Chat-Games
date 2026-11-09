# Let’s Play development

This repository implements New Rules in the Let’s Play application. Read this
file before changing source. Owner design notes may be provided privately under
ignored `design/`; consult them before changing behavior. Never commit, package,
or copy private planning contents into public documentation. Public `docs/`
contains operational and API documentation only.

## Current implementation

Application v0.3.13 uses schema 12. M1 is accepted and closed at v0.2.9/schema 8;
M2 is in progress. Preserve the existing domain/storage APIs. This is a
development-only ordinary-round preview, not a public-release build.

Implemented: persisted three-human opening, message/raw-reaction scoring and
markers, ordinary warning/picker/choice/replacement/timeout/cutover, held play,
same-round revival, manager ending, private live scores, requested public reports,
gated automatic interims, compact round-end boards and ranked final standings.
Seventeen ordinary predicates exist; opening offers five proven templates and
later offers sixteen non-phrase templates. NR036 is not offered by the adapter.

Startup/reconnect enters durable recovery before dispatch or timers. Managers can
inspect read-only recovery status through /rules. The current live recovery path permits manager ending only. Offline RecoveryFlow supports
proof reconciliation, approval and a published 30-second read-in, but the live
resume workflow is not wired. No modes are integrated. Richer fault/streak views,
settings/premium/profile interfaces, full catalog/theme/mode awards, final-output
retry/absolute 24-hour cleanup and operator erasure remain unfinished.

See `docs/DISCORD_PREVIEW.md` for the current runbook, `docs/RECOVERY.md` for
recovery API boundaries, `docs/REQUESTED_SUMMARY.md` for current-score access and
`docs/ROUND_REPORTS.md` for automatic/closed-round reporting. Earlier versions'
proposed next steps are historical; use current source and owner instructions.

## Privacy, presentation and authority

- Never commit `.env`, production word lists, databases/sidecars, transcripts or
  media. Use harmless synthetic fixtures. Do not log chat, exception payloads,
  secrets or private list contents. Source hygiene uses NUL-framed Git I/O.
- Owner emoji artwork, names/markup and IDs are private configuration. Never put
  actual assets/IDs in public source, defaults, fixtures, examples, logs, docs or
  distributions. The marker has no fallback and must be supplied privately;
  markup/permission bits do not prove it can be used. Admission must really react.
- All public/player-facing copy and embed colors must be owner-editable near the
  file top or in a dedicated presentation file. Current text, card labels,
  buttons, command descriptions and colors use stable keys through Presentation
  in `lets_play/resources/en-US.json`. Preserve owner wording, colors and log
  edits. Keep protocol/command/card IDs independent of translated text. Validate
  placeholders/private `CHATGAMES_PRESENTATION_PATH` overrides and package the
  JSON in wheel/sdist builds. See `docs/PRESENTATION.md`.
- Do not contact/invite collaborators, push the repository, deploy or run a live
  bot merely to verify an offline edit. Keep any supplied token/IDs private.
  Routine implementation is delegated; M2 work is authorized in bounded bites.

## Development and verification

Use Python 3.13. `pyproject.toml` owns dependency pins; update `uv.lock` and both
requirements exports together. Use `CHATGAMES_*` configuration names, with no
legacy aliases. Run `python -m lets_play` or the installed `lets-play` command.
Offline tests require no token or external private list.

For behavior changes, run meaningful scenarios and the appropriate regression:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m lets_play --check-config
```

For packaging/dependency changes, also build, check installed dependencies and
exercise the installed wheel outside the checkout. Verify patch application to
the current owner base. For documentation-only changes, check diffs, links and
source/privacy boundaries; do not claim a fresh full-suite run without one.
Record actual results and limitations in private notes. Linux/mocks/SDK checks
are not Windows, GitHub Actions or live human evidence. Owner source parity alone
is not a new numerical test result or M2 sign-off.

Every code-patch handoff includes copy-paste Windows repository-root commands:
`git apply --check`, `git apply`, editable reinstall when needed, checks and
startup. Back up/verify a stopped existing database before an actual schema
upgrade. Never rewrite released migrations 1–12. An unchanged schema needs no
migration or command sync; documentation-only patches need no reinstall/restart.

## Engine, persistence and delivery invariants

- Keep New Rules independent of Discord/storage. Evaluation returns derived
  proposals, not committed effects. Guards, score aggregates, marker intents,
  credit and relevant ownership updates commit atomically. Owner revisions are
  monotonic and distinct from ruleset versions. Bound reads/queues and surface
  overload; never wait for network I/O inside a write unit.
- Use actual-validator witnesses for complete three-template legal hands. One
  template ticket survives phrase/target variants. Slice the authorized phrase
  window before filtering. Enforce local caps/slots, never a total-rule ceiling.
  Replace only a permitted occupant; unrelated removal and empty/whisper/media
  bypasses cannot repair contradictions. Unsupported cards/modes remain withheld.
- One trusted contiguous server receipt lane owns controls, chat/reactions, ACKs
  and timers. Capture Gateway receipts before asynchronous callbacks/HTTP defers.
  Never skip a gap or let timers overtake queued chat. Raw reaction source IDs
  use pinned SDK Gateway session/sequence, separate from actor/target offense keys.
  Support one bot process/database and one bounded FIFO/publisher per live route.
- Old rules and the old round apply through choice until announced whole-second
  activation. Preparation is not publication. Only actual warning publication
  starts its full 30 seconds. Freeze picker evidence before the cutoff receipt
  body; preserve exact scores/unrated eligibility. Partial selections and refresh
  retain the original choice clock. Rotation/general resolution commit only at
  actual activation. Closure evidence alone is not a resolved award.
- Lobby clocks start at actual publication (180 seconds; first choice 60).
  Starter plus two distinct humans is quorum; joins alone do not credit a game.
  Preplay has no fake playing round. First acknowledged activation creates it,
  consumes its phrase/turn and commits deliberate Rulemaker/pacing together.
  Preplay source guards prevent ignored sources becoming scored on replay.
- Held play keeps the same rules/round and scoring. Restoration uses a fresh
  warning/snapshot; settings changes do not reset held idle. Human messages,
  including whispers, refresh held idle; reactions/controls/bot output do not.
  Revival pauses scoring, needs three distinct explicit returners and retains
  the open round, scores and original publication-owned clock. Quorum itself
  neither closes the round nor grants another game credit.
- Credit/live flag/global increment/Familiar Face commit together. Resolve real
  closures in creation order before final Very Careful evaluation and purge.
  finalize_ended neither authorizes nor ends a game. Missing evidence/version
  pins require recovery, never guessed streaks, awards or legacy clock values.
- Use the shared read-only OpeningFlow.admission_gate before reaction probing;
  reject duplicate creation sources, occupied channels and current capacity.
  Recheck in the admission write unit after probing to cover races/exact premium
  expiry. Existing games survive downgrade. Trusted grants/phrase-window facts
  are server state; manager/owner authorization and full UI remain separate.
- Commit a durable send claim before HTTP. Store actual delivered message ID,
  ordered ACK and flow acknowledgement together. Preserve older namespaced
  claims. Read bounded pending work per session, never filter a global first page.
  Unknown/missing ACK, cancellation or timeout never permits blind resend.
- Later ordinary offers edit the known phase panel (normally the warning).
  Successful edit ACK binds that same message and owns the choice clock. Refresh
  cannot reveal prepared offers/selected activations before publication ACK.
  Partial target changes still edit in place. Missing/uncertain edits require
  recovery, without replacement sends. Opening choice/new-rule disclosure retain
  their own publication paths. /rules displays the actually enforced old set.
- Inactivity ending finalizes normally and edits only the last known panel, with
  no new-message fallback. Manager ending may post final results. Ended is terminal
  and releases capacity even if delivery is pending. Never purge earned-progress
  evidence before finalization; complete retry/TTL cleanup is still future work.
- Initial joins use control.joined; Continue/revival joins use control.continued,
  selected by the captured action. Keep already-counted, opening/revival-quorum
  and rule-choice replies distinct. NR011 accepts at least three consecutive
  terminal ASCII periods; recorded scores are immutable.

## Summary and results invariants

Compose SummaryStore writes after actual engine effects in the same receipt
unit. Initialize from real activation, not a guessed clock. Only actual public
success advances time/counter baselines; HTTP arrivals remain pending. Known
success may ACK during pause/end without resuming. Private reads never claim,
ACK, allocate a receipt or change timers/activity/public baselines.

Private Summary uses five-player bounded actor-keyset pages ordered by ID, exact
session/guild/channel checks and stable old-round identity. Scope/page buttons
refresh by editing the same ephemeral response with the fresh interaction token.
Only public entry opens a new private pane. No Refresh button is rendered; retain
its old catalog key for override compatibility. Summary appears on disclosure
and /rules, not choice/target menus. Preplay /rules exposes no copied mutations.

Requested public reports require BOTH 30 seconds since actual prior success AND
new non-whisper human traffic. Pending requests coalesce; blocked requests do not
consume traffic/time. Check current scores and legacy nrf controls open live
private scores. After ACK discard copied scores/rules and older completed summary
receipts; retain the latest receipt plus unresolved sends. Compact delivered
legacy archives on startup. Request IDs provide a bounded replay watermark, not
a growing request cache or historical score browser.

Automatic interims require BOTH 120 seconds and 25 new ordinary non-whisper posts,
reset per round. Offending/media posts count; modes/whispers/reactions/controls do
not. Suppress within 60 seconds of a known warning, throughout warning/choice,
modes, pause/recovery and ending. Timers work without another post; revalidate
before claim after acquiring a network slot. Automatic layout omits capture/
traffic and scope/order headers; manual/private layouts retain their detail.

Post compact round leaderboards only at actual activation closure, including
selection-period scores. Use actual immutable round scores, not copied archives.
Existing final standings cover ending. Exact adjusted-score descending order,
unrated last and ID ties applies to round/final boards, not Summary. Rank pages
scan bounded actor batches of 64, retaining 21 candidates for 20 round entries
or eleven for ten final entries; never sort rounded floats/cache the full roster.
Further public-board pages open privately, then edit in place. Mentions go in
field values with pings suppressed. Only final scores for a played game have a
footer; preplay/interim/round boards remain footer-free. Old-round delivery never
consumes new-round ordinary traffic.

## Recovery boundary and next work

Enter every startup barrier before normal dispatch/output/timers. Recovery fences
normal flow and markers. Reconcile uncertain sends with actual message/time proof
or conclusive never-sent evidence; missing ACK/deleted messages/partial searches
prove neither. RecoveryFlow.work covers engine outputs; adapter delivery claims,
summary outputs and round-report receipts also need explicit review before any
future live resume. Do not assume the engine work list exhausts outbound state.

Only actual recovery-notice publication starts the 30-second read-in. Preserve
original clocks, partial choices, frozen rules/phrases, rotation, scores and
fulfilled goals. Known lobby/revival expiry wins while paused. Guard historical
sources created before actual resume; never backfill missed chat/mode success.
Old capability/dependency/recovery pauses, unsupported modes and clockless legacy
revival need explicit policy or authorized ending. No healthy-startup shortcut.

Read-only manager recovery/status is implemented under /rules in
storage/recovery_status.py and discord_adapter/recovery_ui.py. It uses exact
saved scope/generation, per-interaction manager authorization and bounded keyset
pages across all four engine output and four adapter claim namespaces, summary
and round-report receipts. It does not allocate receipts or change clocks,
claims, scores, baselines or recovery. Unsupported versions remain inspectable.

Next bounded bite: acquire/reconcile trusted publication/time or conclusive
never-sent evidence across every outbound family. Keep this separate from the
later notice claim/publication/ACK, read-in and explicit resume integration. Keep
manager-ending-only behavior until that integration is implemented and verified. Drawing Conclusions follows; consult the supplied private policy
before implementing mode interruption. Richer details and a small real human
full-loop/mode/restart check remain before M2 exit. Do not rebuild M1, broaden to
all cards/modes or combine these into one large patch.
