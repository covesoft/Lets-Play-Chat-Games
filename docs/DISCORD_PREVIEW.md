# New Rules Discord preview — v0.3.30 / schema 16

This development build connects the existing ordinary engine to Discord. Run one
bot process against a database. Private/requested scores, automatic interims and
compact round-end reports and acknowledged ordinary-phase reboot recovery are
wired. Drawing Conclusions uses the same announced cutover, and an interrupted
bout restores ordinary play after read-in. Other Breaking Moments and remaining
release work are still open.

## Install and enable

The current schema is **16**, with WAL and foreign keys enabled. For a v0.3.29
installation on schema 16, stop the bot, apply v0.3.30, reinstall, and restart.
For a fresh database, run `python -m lets_play --init-db`. See
[RECOVERY.md](RECOVERY.md) and [STORAGE.md](STORAGE.md).


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
   snapshot, with at least five ordinary posts. The exactly-at-cutoff body is
   scored afterward in the old round. The public warning shows the countdown
   without rule fields. `/rules` still displays the game status and current rules.
4. With fewer than eight observed channel messages after the warning, the warning
   is edited into the complete hand. With eight or more, including whispers and
   bot posts, the chooser gets one new message and the warning stays in chat.
   The acknowledged panel's buttons remain authoritative and actual delivery
   starts the 60-second choice deadline. An uncertain edit or send enters recovery.
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
9. Also reboot during a published warning, a published choice, a partial
   replacement-target choice, and an already-announced rule transition. Each
   receives the same automatic read-in. Confirm original deadlines and selections
   survive. The notice links to the saved cards; their original buttons work after
   resume, while the notice has no duplicate card buttons. If the deadline passes
   during the pause, timeout completes the choice automatically after read-in.
   Announced transitions show the incoming rules during read-in, take effect at
   resume once, and retain their saved round clock.
10. Reboot during held play, a Continue check, a lobby, or opening cards. Check
    that the original panel remains the place to click. If the lobby or Continue
    deadline lands during read-in, it ends on that deadline. Held idle time
    carries over, and an expired opening choice finishes automatically.
11. The manager debug pane is retired. Old diagnostic controls reply privately.
    Unpublished phases and unresolved sends remain paused, with a brief
    host-contact notice.
    Manager ending remains available. Deleted/missing ACK evidence does not allow
    a blind resend. Actual marker/client/channel behavior still needs human checks.

Later pool: NR004, NR006, NR007, NR008, NR010, NR011, NR013, NR016, NR017,
NR019, NR022, NR024, NR025, NR027, NR033 and NR034. NR036 is not offered because
its phrase-bank integration belongs to a later increment. No new validators are
introduced here. Legal hands and replacements still require actual-validator
compatibility witnesses; an incomplete hand is never published.

12. When a later hand offers Drawing Conclusions, look for the *Breaking Moment
    · 2 minutes* label and choose its blurple button. Check that `/rules`
    shows only Drawing Conclusions during the bout. Try three distinct emoji-only
    posts and then an emoji mixed with text. Expect one marker on the mixed post.
    A whisper or reaction should add no compliance observation. After two minutes
    a lime-green notice lists the restored rules in the same round. Its next
    hand is ordinary-only. In a separate bout, restart after the third valid
    post and check the recovery countdown, the edited rules panel when play
    resumes, and the saved scores. Expect no separate green notice after read-in.

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
Drawing Conclusions, settings, owner grants, profile commands, remaining recovery
phases/uncertain-send reconciliation and final-output retry/24-hour cleanup workers
remain unfinished.
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

Acknowledged ordinary, held, revival and opening phases recover automatically.
Startup can verify an interrupted lobby or opening-card send from a bounded
match of the original controls. New warning and revival posts support the same
bounded lookup with a reference bound to the original round attempt. M2 continues
with proof for other output families. Activation announcements also carry a
reference and require creation strictly before the saved cutover. Held and
recovery notices also carry exact references. A verified recovery notice from
a superseded restart clears the old uncertainty. The latest restart gets its
own announcement and read-in. Recovery notices use a read-only Game status
button. Public score posts use Check current scores and remain footer-free. Drawing Conclusions follows recovery. Keep
recovery unattended; technical reconciliation is host/service work. Do not reintroduce a
manager resume gate or expand retired debug UI. See [RECOVERY.md](RECOVERY.md).
