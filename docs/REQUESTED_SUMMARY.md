# Private requested summaries — v0.3.5 / schema 11 unchanged

Once actual play starts, Summary is available on the phase panel and inside
`/rules`. Any human in the exact game channel can inspect five-player pages,
choose Current round or Whole game, and Refresh. Replies and navigation are
private. No new slash command or command sync is required. Preplay `/rules` keeps
its explanation without unusable copies of Play/picker controls.

Pages show clickable non-pinging identities, actual compliance/unrated, adjusted
score, raw faults, opportunities, ordinary posts and reaction faults. Traffic and
whispers are separate from score opportunities. Player order is by ID, matching
the existing basic result layout; ranking order and richer rule/streak breakdowns
remain future increments. Use `/rules` for complete current requirements.
All text/colors use the existing presentation catalog, including reused result
body templates. These private views have no footer.

These are live reads of committed state, not frozen public captures. The shown update
time follows the ordered receipt watermark. Scores can change between page or
refresh requests. Actor keysets keep reads bounded (six rows for a five-player
page) without an all-player cache or an unbounded OFFSET. Round-page navigation
keeps its recorded round number after cutover and identifies a completed round. Current round deliberately opens
the current round; Whole game reads current session totals. Previous recorded
round scores remain inspectable while the session data exists.

Summary controls never allocate a game receipt, advance an overdue timer, score
an event, update activity or resume enforcement. Paused views identify the pause;
ended games remain read-only through old controls until their data is purged.
The saved session's exact guild/channel is checked before reading scores. Unknown,
expired, wrong-surface and oversized controls get a private unavailable reply.
Overlong custom copy is rejected before sending a malformed embed.

`SummaryStore.live_page` is a read-only projection. It never invokes the schema-11
public capture/claim/ACK methods and never changes their traffic/time baseline or
uncertain-send state. Automatic/public requested/round-end summary publishing,
frozen public pagination, proof reconciliation, full restart resume and modes
remain separate work. This UI does not claim M2 complete or alter the existing
quiet inactivity ending/final-result path. Schema remains 11; no migration needed.
