# Round reporting — v0.3.9 / schema 12

Mid-round reports require both 120 seconds and 25 new **ordinary non-whisper human
posts** since the latest successful public score report. Offending and media
posts count. Whispers, mode posts, reactions, edits, nonhumans and controls do
not. Use the actual round start for a new round's time floor and a fresh count;
unused traffic from the previous round does not spill into the next one.

`AUTO_SECONDS`, `AUTO_MESSAGES` and `AUTO_WARNING_BUFFER_SECONDS` live near the
top of `storage/summary_store.py`. Successful manual reports restart the same
automatic gates, while retaining their independent 30-second/new-non-whisper
request rule. Only actual returned public message IDs advance delivery baselines.
Traffic arriving during HTTP remains pending. The lane timer can post after
traffic stops, without needing another message to trigger it.

No automatic report is prepared within 60 seconds of a known scheduled warning,
during the warning or choice/target selection, during a Breaking Moment, recovery
or paused enforcement, or after ending. Held ordinary play can still report
while enforcement continues. Recheck suppression after a send slot is acquired,
before committing the send claim; obsolete never-started automatic output is
discarded safely. Future traffic cannot be predicted, so use known warning state
and deadlines rather than guessing message arrival rates. A minimum-paced busy
round normally has one interim at about two minutes, then its warning at 4:30,
picker at 5:00, and eventual cutover after choice. Other public requests can
reduce automatic output further.

Automatic reports use the editable lead-in:
“Round X is underway, here's how you're doing:”. Existing manual/private Summary
panels retain their presentation and remain ID-ordered. Public interim reports
keep the current-score button, suppressed mentions and no footer.

## Completed-round leaderboard

After actual activation closes the preceding round, post one compact leaderboard
with “Round X is over, here's how you did:”. Warning, picker selection and rule
disclosure preparation do not close a round. Selection-period posts remain in
the old round's final results. Held/revival transitions do not invent a closure.
Game ending uses existing final game standings instead of another round report;
inactivity ending continues to edit only its known panel.

Each field contains only `Scoring 82.35` and the player's clickable mention.
Order by the round's exact adjusted score descending, unrated last, actor ID for
exact ties. No raw faults, opportunities, ordinary-post detail or footer is
added. Up to 20 players appear on a page. Additional page buttons open a private
view, then edit it in place; they do not change the public message or publish
another report. Read the actual immutable `nr_round_scores` ledger in bounded
actor batches of 64, keeping at most 21 candidates. No historical snapshot archive
or all-player cache is introduced. Exact ordering is shared with final standings;
their existing layout and ten-player page size are unchanged.

`nr_round_reports` stores only send state, original attempt time, actual message
identity, delivery time and captured traffic watermarks. It references an actual
`nr_round_resolutions` activation closure. Claim before HTTP; ACK through the
ordered lane; unknown delivery pauses and is never blindly retried. Known success
can commit during concurrent pause/ending without resuming play. These receipts
cascade with session/round retention. A round report takes precedence over a
routine interim. Delivery of the old-round board does not consume new-round posts.

## Upgrade and checks

Migration 12 appends baseline/capture counters and the round delivery receipt
table. Released migrations 1–11, engine scores, rules, clocks, progression and
existing send claims are unchanged. Backfill only known delivered traffic/message
identities and retained request IDs; do not infer legacy captured ordinary counts.
Startup still enters recovery for saved games before timers/sends. Current
recovery permits manager ending only, pending explicit resume integration.

Stop the bot and make a verified SQLite backup before applying/reinstalling and
running `python -m lets_play --init-db`. Expect schema 12, WAL and foreign keys on.
No command sync is required. Older code cannot open schema 12: restore a verified
backup with matching old code if rollback is required.

Offline tests simulate 100 players and 500+ posts, cutoff/suppression boundaries,
timer-only delivery, HTTP arrivals, exact pagination, pause/end races, uncertain
sends and migration rollback at every new statement. These are not live Discord
load or Windows certification. Live checks can focus on layout, permissions and
a small actual round. Re-running start-game in an occupied channel now checks the
durable admission before probing reaction access; authoritative admission still
handles races after that read.
