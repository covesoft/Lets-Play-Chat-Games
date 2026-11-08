# Private requested summaries — v0.3.6 / schema 11 unchanged

Once actual play starts, Summary is available on the phase panel and inside
`/rules`. Any human in the exact game channel can inspect five-player pages,
choose Current round or Whole game. The first click from a public phase panel
opens a private reply. From an ephemeral `/rules` response or summary page, scope
and page buttons edit that same private panel. Each scope click re-reads scores,
including repeated clicks on the selected scope; there is no Refresh button. No new slash command or command sync is required. Preplay `/rules` keeps
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
scope requests. Actor keysets keep reads bounded (six rows for a five-player
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

Final played-game results use a separate display order: exact adjusted score,
highest first, unrated last and actor ID for exact ties. Ten-player result pages
scan keyset batches of 64 while retaining at most eleven candidates in RAM; each
page is O(players), not an indexed-leaderboard or throughput claim. Final scores
are immutable, so rank anchors give stable forward/back navigation. Final-result
paging opens privately from a public result and then edits that private menu.
Old ID-offset buttons reopen ranked page one and replace themselves with rank
cursor controls. This changes display order only, not scores or awards.

SDK interaction edit/defer behavior was rechecked against the official
[discord.py interaction API](https://discordpy.readthedocs.io/en/stable/interactions/api.html#discord.Interaction.edit_original_response).
