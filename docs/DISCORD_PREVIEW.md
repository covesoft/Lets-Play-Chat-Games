# New Rules ordinary-round preview — v0.3.7 / schema 11

This development build connects the existing ordinary engine to Discord. Run one
bot process against a database. Full summaries, modes and automatic restart
resume remain separate work; M2 is still in progress.

## Install and enable

Stop the bot and make a verified SQLite backup using the procedure in
[STORAGE.md](STORAGE.md). Install the updated package using the README commands,
run offline checks, then `python -m lets_play --init-db`. Expect schema 11, WAL and
foreign keys enabled. Released migrations 1–10 are unchanged. Migration 11 appends offline summary
capture/baseline tables and preserves every old claim, message ID, clock and
gameplay row. The capture/ACK methods remain offline. The adapter now uses its read-only
private score-page API. A failed migration rolls back atomically. To roll
back after a successful upgrade, stop the bot and restore the verified pre-upgrade
backup with the matching old code; never downgrade a schema-11 file in place.

Keep token, owner IDs and actual emoji markup in the private `.env`:

```dotenv
CHATGAMES_DEVELOPMENT_MODE=true
CHATGAMES_SYNC_COMMANDS=false
CHATGAMES_DING_EMOJI=
```

Supply your own custom marker privately before starting a game. No bundled asset
or fallback exists. The three commands remain `/start-game new-rules`, `/rules`
and `/end-game`; no additional command sync is needed for this increment. For a
new installation, explicitly enable sync once, then turn it off. Message Content
Intent must be available. Start normally with `python -m lets_play`.

Admission checks the exact message-bearing guild channel, including thread send
permission, then posts a short probe and really adds/removes the configured
reaction before deleting the probe. A failed reaction prevents admission.
Archived/locked threads and forum/media containers are refused; start inside an
accessible post/thread. Administrator is unnecessary. No private phrase list,
badge map, voice library or additional intent is needed.

## Human development check

1. Start a game and have two other humans press Play. The starter is already
   joined; duplicate clicks cannot increase quorum. A no-quorum invitation expires
   180 seconds after actual publication. All later chat in this exact channel
   participates, including people who did not press Play. `-# ` whispers are exempt.
2. The named opening picker chooses one of three cards or times out after the
   published 60 seconds. Confirm the announced future start and `/rules` after
   activation. Opening hands still use Unattached, Expressionless, Inside Voice,
   Are You Sure?, and Roger Wilco.
3. Chat through the ordinary round. A warning cannot occur before 4:30 after
   activation. The frozen post target can trigger it thereafter; fallback is at
   14:30. Only actual warning publication starts its full 30 seconds. A delayed
   send must not shorten that notice. Picker eligibility uses the saved cutoff
   snapshot, with at least five ordinary posts; the exactly-at-cutoff body is
   scored afterward in the old round.
4. The selected picker receives a new complete three-card hand and the original
   publication-owned 60-second choice deadline. Later hands use sixteen already
   implemented non-phrase templates. Choices can add a rule or replace one legal
   existing occupant. When several outgoing rules are legal, choosing the incoming
   card reveals the outgoing buttons. The original deadline does not restart;
   timeout keeps the chosen incoming card and completes a legal outgoing choice.
5. Confirm old rules remain enforced during the entire choice and announced
   transition. `/rules` shows those current rules, while the public transition
   shows the future set and exact effective time. At cutover the old round resolves
   once, the new rules activate and the new round clock begins. Try unauthorized,
   duplicate and old-message buttons; they must not change the saved choice.
6. If nobody qualifies by the cutoff, scoring pauses and Continue is offered.
   Three distinct humans must click within five minutes of actual publication;
   the starter is not automatically counted. Quorum preserves the same open round
   and scores, then offers a choice. If no complete legal hand exists, held play
   keeps scoring under the same rules; fifteen minutes without a new human message
   enters revival. Whispers count for this held-idle clock. These states are
   naturally rare in the small pool and are also exercised with offline fixtures.
7. On revival expiry, only the existing known panel is edited to game-over results.
   There is no new-message fallback, including when the panel was deleted. A
   manager's explicit ending may post normal final results. Check both behaviors.
8. Restart during an offer/transition/revival. Saved routes enter durable recovery
   before timers or scoring; the known panel and `/rules` show pause. This build
   still provides manager ending only. No downtime scoring or automatic resume is
   inferred. Test animated marker/client rendering and actual channel behavior
   independently from offline evidence.

Later pool: NR004, NR006, NR007, NR008, NR010, NR011, NR013, NR016, NR017,
NR019, NR022, NR024, NR025, NR027, NR033 and NR034. NR036 is not offered because
its phrase-bank integration belongs to a later increment. No new validators are
introduced here. Legal hands and replacements still require actual-validator
compatibility witnesses; an incomplete hand is never published.

## Delivery and ordering

One bounded FIFO per session orders Gateway chat/reactions, controls, ACKs and
timers. SDK dispatch captures receipts before async callbacks/HTTP defers. Raw
reaction identity uses the pinned SDK Gateway session/sequence; actor/target
fault deduplication remains separate. No history scan or message cache is needed.
This bridge is pinned to discord.py 2.7.1 and must be reviewed on SDK upgrades.

Receipts use monotonic elapsed time anchored to UTC and the saved watermark. The
queue holds at most 64 waiting receipts. Overload or processing failure fences
intake and enters recovery. Four network calls can run concurrently, each bounded
to 20 seconds. No HTTP wait occurs inside a SQLite write unit. Content remains
transient, including while the queue is idle.

Every public output commits its unique `(family, output_id)` claim before HTTP.
The actual returned message ID, ordered ACK sequence and domain acknowledgement
then commit together. Warning/revival ACKs carry the exact persisted attempt;
partial choices keep their original publication-owned deadline. Per-session
pending reads are bounded independently of other sessions' output backlog.
Cancellation, timeout, restart or a missing ACK never authorizes blind resend.
Unknown sends and essential feedback loss pause enforcement. Marker work is
fenced during recovery; a deleted marker target does not reverse its score.
Errors log types, not received text or exception payloads.

## Remaining work and editable copy

Manager/final standings use ten-player pages ordered by exact adjusted score,
highest first, with unrated players last and ID breaking exact ties. They retain
clickable non-pinging identities and no score footer on preplay closures.
Public automatic/requested/per-round summaries and their delivery integration,
badge presentation, Drawing Conclusions, settings, owner grants,
evidence-based restart resume and final-output retry/24-hour cleanup workers are
not wired. Failed terminal delivery retains durable results evidence. This is a
development preview, not a public-play release or M2 completion.

All new copy, rule descriptions, button labels and warning/held/revival colors
are in the existing editable catalog. See [PRESENTATION.md](PRESENTATION.md),
including the new `round.description` key for custom round text. Private overrides
keep their existing wording; update any old first-rule-only explanation.

Official platform references rechecked for this increment:

- [Discord embeds and limits](https://docs.discord.com/developers/resources/message#embed-object-embed-limits)
- [discord.py buttons and views](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.ui.Button)
- [Discord reactions](https://docs.discord.com/developers/resources/message#create-reaction)

After play starts, press Summary on the phase panel or inside `/rules` for private
round/game totals and five-player ID-ordered pages. Scope/page buttons edit the
existing private response and read updated scores; there is no Refresh button. Round paging retains
its round number through cutover; Current round opens the new round. Scores are
live reads and can change between requests. These controls neither resume paused
play nor change public summary baselines. See [REQUESTED_SUMMARY.md](REQUESTED_SUMMARY.md).

For a public report, choose **Post summary to channel** in that private menu.
Verify one saved report appears, its saved round/game pages open privately and
keep their captured scores after more chat. Rapid repeat clicks should share a
pending report or receive a private recent-report reply. The live scope buttons
remain quiet. Automated/round-end summaries are not enabled yet. This increment
uses the installed schema 11 and needs no migration or command sync.
