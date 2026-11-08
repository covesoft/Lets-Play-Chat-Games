# New Rules opening preview — v0.3.2 / schema 9

This development build connects the existing engine to Discord. It is not the
complete game. Run one bot process against a database. No deployment or command
sync is performed by installing a patch.

## Install and enable

Stop the old bot, make a verified SQLite backup, then install the updated package
using the normal README commands. Run `python -m pytest` and `python -m lets_play
--init-db`. The expected schema is 9, with WAL and foreign keys enabled.
Migrations 1–8 are unchanged; migration 9 appends one output-attempt table.

Keep the token and owner IDs in the existing local `.env`. Set:

```dotenv
CHATGAMES_DEVELOPMENT_MODE=true
CHATGAMES_SYNC_COMMANDS=true
CHATGAMES_DING_EMOJI=
```

Set `CHATGAMES_DING_EMOJI` privately to your own custom emoji markup before
starting a game. Real assets and IDs do not belong in examples, fixtures or Git.
There is no public default or fallback marker.

Command sync registers `/start-game new-rules`, `/rules`, and `/end-game` globally.
Turn command sync off again after registration. Ensure Message Content intent is
available to the application. Start normally with `python -m lets_play`.

The start command checks the exact message-bearing guild channel, including
thread-specific send permission. Archived/locked threads and forum/media containers
are refused; start within an accessible post/thread. It briefly posts a test
message, adds the configured marker, removes its own reaction, and deletes the
probe. A failed real reaction prevents admission. No Administrator permission is
needed. Application-owned emoji do not require Use External Emojis; actual
reaction usability is still established by the probe.

## Exercise the opening path

1. Start in the development channel. Confirm one public Play invitation, the
   starter already joined, and an explanation of channel-wide participation and
   `-# ` whispers. A no-quorum invitation expires 180 seconds after publication.
2. Have two other humans press Play. Duplicate clicks cannot increase quorum.
   A public three-card offer names one picker; other humans cannot choose.
3. Choose a card or wait 60 seconds for automatic selection. The next message
   shows the selected rule and a future effective time. No scoring starts before
   the announced time. Confirm `/rules` shows the active first rule afterward.
4. Try passing and failing messages, media-only posts, a whole-message whisper,
   and reactions when Expressionless is active. Only infracting message creates
   receive a marker. Expressionless attributes faults to reacting humans and
   does not mark the target. Bot/probe/marker traffic is ignored.
5. Have a non-manager try `/end-game`, then let a server manager end the game.
   Ending finalizes existing general progression and releases capacity before
   delivery. Final results show compliance, adjusted score, faults and basic
   counters, with ten-player pages ordered by ID.
6. Restart during a lobby, choice, or active first round. All saved routes enter
   durable recovery before timers or scoring. The known panel is edited to
   paused when accessible; `/rules` also shows the pause. This build provides
   manager ending, not automatic resume. Never infer scoring during downtime.

The five offered templates are Unattached, Expressionless, Inside Voice,
Are You Sure?, and Roger Wilco. An opening hand contains three distinct templates.
No production phrase list, bank, badge image map, voice library or additional
intent is needed for this preview.

## Delivery and ordering

One FIFO per admitted session orders chat, reaction actions, controls,
publication acknowledgements and timers. SDK dispatch captures message/reaction
receipts and active control receipts synchronously, before HTTP acknowledgement.
Raw-reaction identity uses a hash of the pinned SDK Gateway session plus its
sequence; it never uses target-message age. The separate persistent actor/target
key still owns the once-per-game reaction fault. No message cache/history scan is
needed. This Gateway bridge is pinned to discord.py 2.7.1 and must be rechecked
when upgrading the SDK.

Receipts use monotonic elapsed time anchored to UTC and never regress below a
saved watermark. The bounded queue accepts 64 waiting receipts; overload or a
processing failure fences intake and enters recovery. Four network operations
can run concurrently; each is bounded to 20 seconds. No HTTP call occurs in a
SQLite write unit. Content remains transient, including when a queue is idle.

Before each new public output, a unique persisted send claim commits. Its returned
message ID and engine acknowledgement then commit together. Cancellation, timeout,
restart or a missing ACK never authorizes a blind resend. Unknown sends remain
paused for manager ending. Essential delivery loss also pauses; an already-deleted
marker target is local and does not reverse its score. Errors are logged by type,
not by received text or exception payload.

## Deliberate limits

This is the opening path only. The first ordinary rule remains until manager
ending; no later warning/succession, replacement, held/revival loop or mode is
exposed. Full gated/requested summaries, ranking presentation, badge presentation,
manager settings, owner grants, automatic evidence-based resume and final-output
retry/24-hour purge workers are not wired. Failed final output leaves durable
results evidence; ending remains terminal. Do not use this build for public play
or claim milestone M2 complete. The agreed first mode is implemented separately.

Offline transport/SDK/WAL tests exercise the wired path and failures; actual emoji
rendering, client/channel behavior, Windows execution and a three-human live trial
remain owner/live checks. Record results independently from simulated evidence.

Official references checked for this adapter:

- [Discord reaction endpoint](https://docs.discord.com/developers/resources/message#create-reaction)
- [Application-owned emoji](https://docs.discord.com/developers/resources/emoji#application-owned-emoji)
- [discord.py raw reaction events](https://discordpy.readthedocs.io/en/stable/api.html#discord.on_raw_reaction_add)
- [Interaction responses](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.InteractionResponse.defer)

Sent copy and embed colors are editable independently of this preview’s game
logic. See [PRESENTATION.md](PRESENTATION.md) for the catalog and private overrides.

The v0.3.2 live-feedback fixes place result mentions in field values (with
notifications suppressed), use separate no-game/manager/duplicate-join replies,
and omit score footers from preplay closures. Are You Sure accepts three or more
consecutive ASCII periods; stored effects are not retrospectively re-evaluated.
