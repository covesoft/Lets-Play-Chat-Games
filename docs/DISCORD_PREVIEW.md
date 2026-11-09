# New Rules ordinary-round preview — v0.3.14 / schema 13

This development build connects the existing ordinary engine to Discord. Run one
bot process against a database. Private/requested scores, automatic interims and
compact round-end reports are wired. Modes, explicit evidence-based restart
resume and richer score details remain separate work; M2 is still in progress.

## Install and enable

The current schema is **13**, with WAL and foreign keys enabled. Apply both
v0.3.14 patches in order to v0.3.13: retirement/copy/transport preparation, then
automatic recovery. Stop the bot and make/verify a SQLite backup of its actual
configured database before the schema-12 upgrade. Install, run offline checks,
then `python -m lets_play --init-db` (schema 13) and start. No new command sync.
Schema 13 appends only recovery-notice send/ACK guards and their index; released
migrations 1–12 and existing game scores/clocks/claims remain unchanged. Failed
migration steps roll back. To roll back an upgraded database, restore the verified
schema-12 backup with v0.3.13 source. Never downgrade the schema-13 file in place.
See [RECOVERY.md](RECOVERY.md) and [STORAGE.md](STORAGE.md).

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

Admission first checks development/readiness/human context, static permissions,
marker configuration, archived/locked state and the shared read-only eligibility
gate (duplicate creation source, occupied channel and free/premium capacity).
Passing preflight does not reserve a slot. It then posts a short probe and really
adds/removes the configured reaction before deleting the probe. A failed reaction
prevents admission. The write-side admission gate rechecks capacity/expiry and channel ownership after
the probe to cover concurrent changes; existing games survive premium expiry.
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
4. The warning is edited into the selected picker's complete three-card hand;
   no duplicate choice message is sent. Those original-panel buttons remain
   authoritative. The successful edit ACK starts its publication-owned 60-second
   choice deadline. Missing/uncertain edits enter recovery without a fallback send.
   Later hands use sixteen already implemented non-phrase templates. Choices can
   add a rule or replace one legal existing occupant. When several outgoing rules are legal, choosing the incoming
   card reveals the outgoing buttons. The original deadline does not restart;
   timeout keeps the chosen incoming card and completes a legal outgoing choice.
5. Confirm old rules remain enforced during the entire choice and announced
   transition. `/rules` shows those current rules, while the public transition
   shows the future set and exact effective time. At cutover the old round resolves
   once, the new rules activate and the new round clock begins. Try unauthorized,
   duplicate and old-message buttons; they must not change the saved choice.
6. If nobody qualifies by the cutoff, scoring pauses and Continue is offered.
   Three distinct humans must click within five minutes of actual publication;
   the starter is not automatically counted. Successful initial lobby joins use
   `control.joined`; successful revival returners use `control.continued`. Repeat
   clicks and opening/revival quorum retain their specific replies. Quorum preserves
   the same open round and scores, then offers a choice. If no complete legal hand
   exists, held play keeps scoring under the same rules; fifteen minutes without a new human message
   enters revival. Whispers count for this held-idle clock. These states are
   naturally rare in the small pool and are also exercised with offline fixtures.
7. On revival expiry, only the existing known panel is edited to game-over results.
   There is no new-message fallback, including when the panel was deleted. A
   manager's explicit ending may post normal final results. Check both behaviors.
8. During an ordinary round before warning/choice, stop and restart the bot.
   The channel receives one resumption notice with current rules and 30 seconds
   to read in; /rules shows the countdown. Wait without posting or clicking: the
   game resumes automatically. Check preserved scores/rules/round and verify that
   read-in posts received no points. The known notice becomes the current panel.
   An overdue round can proceed to its saved pacing boundary after read-in.
9. The manager debug pane is retired. Old diagnostic controls reply privately.
   Interrupted warnings/choices, held/revival/opening/modes and uncertain sends
   remain paused in this first automatic path, with a brief host-contact notice.
   Manager ending remains available. Deleted/missing ACK evidence does not allow
   a blind resend. Actual marker/client/channel behavior still needs human checks.

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
Private live scores, requested public reports, automatic interims and actual
round-end boards are wired. Richer fault/rule/streak and badge presentation,
Drawing Conclusions, settings, owner grants, profile commands, evidence-based
restart resume and final-output retry/24-hour cleanup workers remain unfinished.
Failed terminal delivery retains durable results evidence. This is a development
preview, not a public-play release or M2 completion.

All new copy, rule descriptions, button labels and warning/held/revival colors
are in the existing editable catalog. See [PRESENTATION.md](PRESENTATION.md),
including the new `round.description` key for custom round text. Private overrides
keep their existing wording; update any old first-rule-only explanation.

Official references used during earlier adapter work (this documentation-only
review makes no fresh platform-certification claim):

- [Discord embeds and limits](https://docs.discord.com/developers/resources/message#embed-object-embed-limits)
- [discord.py buttons and views](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.ui.Button)
- [Discord reactions](https://docs.discord.com/developers/resources/message#create-reaction)

After play starts, press Summary on the rules disclosure panel or inside `/rules` for private
round/game totals and five-player ID-ordered pages. Scope/page buttons edit the
existing private response and read updated scores; there is no Refresh button. Round paging retains
its round number through cutover; Current round opens the new round. Scores are
live reads and can change between requests. These controls neither resume paused
play nor change public summary baselines. See [REQUESTED_SUMMARY.md](REQUESTED_SUMMARY.md).

For a public report, choose **Post summary to channel** in that private menu.
Verify one report appears and **Check current scores** opens private live totals.
After 30 seconds with only whispers or no new chat, a repeat request should stay
private and decline to post. Add a non-whisper post, then request again. Scope
and page clicks must remain quiet. Choice/target menus should contain only their
choice controls; the disclosure panel and `/rules` retain Summary. Earlier saved
report buttons now open current scores. Delivered copies are removed at ACK and
older delivered archives are compacted on startup; unresolved send evidence is
preserved.

Automatic interims require both 120 seconds and 25 new ordinary non-whisper posts
in the current round. Timers can deliver after chatter stops. They are
suppressed within 60 seconds of a known warning, throughout warning/choice,
modes, pause/recovery and ending. Their payload retains the lead-in, active rules
and player entries while omitting capture/traffic and scope/order headers.
Manual/private Summary layouts retain their details.

Actual activation closure posts a compact leaderboard for the completed round,
ordered by exact adjusted score, with only score headings and mentions. Selection
traffic is included. Game ending uses its final standings instead; inactivity
stays quiet. Additional round-board pages open privately, then edit in place.
See [ROUND_REPORTS.md](ROUND_REPORTS.md) for delivery guards and timing tests.

## Continuity and next bite

Automatic settled-round reboot recovery is implemented; M2 remains in progress.
Next extend it to acknowledged ordinary warnings and choices, keeping original
publication deadlines and partial selections. Cover genuinely uncertain sends
only after each outbound family's proof/guard contract is implemented. Held,
revival/opening continuation and Drawing Conclusions follow. Keep recovery
unattended; technical reconciliation is host/service work. Do not reintroduce a
manager resume gate or expand retired debug UI. See [RECOVERY.md](RECOVERY.md).
