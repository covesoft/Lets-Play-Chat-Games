# Requested summaries — v0.3.9 / schema 12

During play, Summary is available on the rules disclosure panel and inside
`/rules`. Choice and replacement-target menus show only their choice controls.
Any human in the exact saved game channel can inspect private five-player pages
and choose Current round or Whole game. A public entry opens a private reply;
scope/page buttons then edit that same private panel. Repeated scope clicks read
fresh committed scores. There is no Refresh button or additional slash command.
Preplay `/rules` omits unusable game controls.

Live pages show clickable, non-pinging identities, actual compliance/unrated,
adjusted scores, raw faults, opportunities, ordinary posts and reaction faults.
Traffic/whispers are distinct from score opportunities. Players stay ID-ordered;
final standings separately use exact adjusted-score order. Interim views have
no footer. All text and colors use the presentation catalog. Use `/rules` for
complete current instructions; richer fault/streak details remain future work.

Actor-keyset navigation reads at most six rows for five visible players. Values
can change between clicks. Round-page navigation retains its round number after
cutover; Current round deliberately follows the current owner. Paused/ended
views identify their state and never resume play. Exact saved session and
guild/channel checks apply, including to old controls. Scope/page reads allocate
no game receipt, advance no timer, and change no public baseline. Overlong copy
fails privately rather than sending an invalid embed.

## Public reports and current scores

**Post summary to channel** in the live private menu explicitly requests a public
report. Any human in that channel may use it. Its private acknowledgement edits
the current menu. Scope/page buttons never publish. There is no manager-only
restriction on score access or reporting.

A first report is allowed without a previous report. Later requests require
**both 30 seconds since the last successful public delivery and at least one new
non-whisper human message beyond that report's captured totals**. The constant
`REQUEST_COALESCE_SECONDS` is near the top of `storage/summary_store.py`. Pending
requests coalesce. Whisper-only traffic, reactions, controls and private reads
do not unlock a repost. A blocked request does not reset the cooldown or consume
traffic. New non-whisper traffic arriving during a send remains eligible after
the cooldown. This is separate from the automatic 120-second/25-ordinary-post
gate, which excludes whispers and mode posts; see [ROUND_REPORTS.md](ROUND_REPORTS.md).

SDK dispatch captures a public request before HTTP defer, in the existing
bounded ordered lane. Due engine boundaries run first, then one consistent
round/session/rule/traffic/score capture is committed. An ended game cannot
prepare a report. The public report shows five ID-ordered round scores, active
rule names and aggregate round/game traffic, with mentions suppressed and no
footer. **Check current scores** opens the normal live private menu, giving
access to all current players and both scopes. Older `nrf` saved-score controls
also redirect to current scores; no historical browsing service remains.

## Delivery state and retention

Only unresolved output retains copied player scores and rule configuration.
The serial publisher commits a unique sending claim before HTTP and ACKs only
an actual returned Discord message ID through the lane. ACK advances the public
baseline through the captured human counter and actual ACK time, preserving
arrivals during delivery. In that same transaction, copied scores/rules are
removed and obsolete completed receipts are discarded. At most one completed
receipt per retained game remains for cooldown, traffic watermark, latest ACK
identity and old-interaction replay checks, alongside any unresolved send.
A bounded SDK interaction cursor in the baseline protects even coalesced or declined requests from replay.
No growing request cache is added. Discord history retains the public report.

Startup also compacts earlier delivered archives, including ended games, in
receipt-keyset pages. It preserves pending/sending/uncertain output and its
proof. Cleanup never changes actual game/round scores, rules, activity,
progression or the acknowledged baseline. Schema 12 preserves that cleanup policy and adds round-local watermarks plus
minimal round-report delivery receipts. Back up before upgrading from schema 11.
Same-message ACK replay of the retained receipt is a no-op; conflicting identity
rejects. Obsolete completed capture keys are not a historical API.

Known success can still commit during a concurrent pause/end without resuming
play. Unknown delivery enters recovery and preserves unsummarized traffic;
shutdown/restart retain the sending fence. No blind retries are introduced.
Managers can currently end paused games; evidence reconciliation and explicit
resume remain later work. Prepared reports are withheld after ending or through
recovery. Inactivity closure edits only its known panel. Automatic/round-end
publishers are described in ROUND_REPORTS.md. Richer details and modes remain
separate work; M2 is incomplete.

SDK interaction edit/defer behavior follows the pinned discord.py API:
https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.Interaction.edit_original_response
