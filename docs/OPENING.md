# Admission, lobby and first activation

`storage.opening_flow.OpeningFlow` is an offline application facade over a
GameUnitOfWork. It uses plain route/actor/capability facts and performs no Discord
I/O. Returned states and queued intents become authoritative only after commit.

## Reserve before publication

`admit(frame, capabilities=..., rng=..., phrase_bank=(), available_templates=...)`
uses a stable sequence-one `LOBBY_CREATE` receipt. The actor is auto-joined.
`OpeningCapabilities` contains trusted normalized adapter facts, including real
marker usability. These are not platform checks or a client permission claim.
The future adapter must verify exact guild/thread access and required operations.
Bot/nonhuman requests are ignored. Preflight a complete ordinary hand; no partial
hand or unavailable capability may reserve a new lobby.

Within the same BEGIN IMMEDIATE unit, admission checks one live exact-channel
reservation and guild non-ended count against saved entitlement expiry. Free
capacity is one, currently active premium is five, and exact expiry is inactive.
Lobby, opening, recovery, playing, paused and held reservations count. Ended
delivery work does not. Existing admitted games survive expiry; no periodic
downgrade enforcement is introduced. The unique channel/request constraints also
protect retries and separate processes. A start request cannot replace an old
session or become a new request by changing its session identity.

`nr_guild_entitlements` is a minimal authoritative input managed by a future
authorized grant service. There is no client capacity parameter. This increment
does not expose owner grant controls, billing or implement all live promotion
behavior. New free/premium phrase allowances are five/fifteen and never shrink
for an admitted lobby when entitlement expires. An upgraded lobby can widen at
quorum. Full immediate promotion of ongoing games/settings remains M5 work.

The default configuration snapshot is admission-time availability and <=15
ordered phrase configurations. At quorum a caller may supply fresh trusted
configuration; the resulting public hand freezes those actual wording/revisions.
The pure hand API still slices the authorized prefix before filtering. These
intentional phrase snapshots contain no received chat or generated witnesses.

## Lobby and first choice

Lobbies live in `nr_admissions`/`nr_lobby_members`, separate from `nr_sessions` and
`nr_rounds`. There is no artificial round to score or resolve. Identifier-only
`pending_outputs(limit=100)` returns currently applicable work. A real successful
`publish(frame, 'lobby', message_id=...)` ACK using `LOBBY_PUBLISHED` starts the
original 180 seconds. Preparation alone starts no clock. A known unpublished
capability failure can terminate the reservation; a sender must surface failure
rather than silently leaving unreachable controls.

`join(frame)` uses `LOBBY_JOIN` and requires a human actor. Starter/repeated clicks
cannot inflate quorum. The third distinct human immediately freezes a random
picker from the three, preflights/deals a complete ordinary opening hand and queues
its public offer. There are no active rules yet, no five-post opening eligibility,
no mode card and no join-based played-game credit. Later joins cannot redraw it.
Lack of a complete opening hand fails clearly and releases the slot, without a
ruleless game or held phase. At exact lobby expiry, expiry precedes that receipt's
join. Expiry queues a closing-panel intent; it does not wait for delivery to free
capacity.

`publish(frame, 'offer', ...)` uses `OFFER_PUBLISHED` and starts exactly 60 seconds.
`record().choice_deadline` remains None before this ACK, regardless of the pure
proposal deadline inside the prepared hand. Publication retries and
reopen cannot restart a clock or change known message identity. `choose(frame,
incoming_id)` reauthorizes the frozen picker; at expiry `timeout(frame, rng=...)`
uses `CHOICE_TIMEOUT` and uniformly completes a still-legal template. There is no
outgoing replacement in an empty opening state. No full refresh service exists
yet; total unavailability ends an unstarted session without inventing a rule.

## Announced first activation and handover

A valid selection prepares complete first-rule output with an immutable future
effective time, using the existing five-second/whole-UTC-second announcement
contract. `publish(frame, 'activation', ...)` uses `ACTIVATION_PUBLISHED` strictly
before that boundary. Missing/late ACK or unavailable promised completion enters
opening recovery with no active rule/round and no Rulemaker. A recovery notice
ACK never resumes or resets clocks. Unknown actual network sends still require
external reconciliation; local ACK fixtures are not Discord delivery evidence.

The first ordered receipt at/after the acknowledged boundary atomically:

* Creates the real playing session and round one with that announced start.
* Enrolls the three joined humans with zero observations/posts and no game credit.
* Stores activated offer/provenance/message identities and consumes a selected phrase.
* Grants Rulemaker only for a deliberate valid choice, initializes rotation service
  and pacing with the three joined humans, and links the capacity reservation.
* Hands the current receipt to OrdinaryFlow at its existing sequence. An exactly
  boundary message is evaluated once under round one; it is not dropped as a replay.

No old-round closure/resolution is fabricated. A fully automatic choice can
activate without Rulemaker. A late timer does not restart the announced clock.
After handover the OrdinaryFlow watermark is authoritative. Old opening buttons,
repeated ticks and commit-ack retries cannot create another first round.

`ingest(event)` records only compact ignored preplay source keys. A redelivered
pre-rule message/reaction after activation remains ignored. A new post or new
reaction action may be judged normally. Pre-rule reactions do not consume the
session's prohibited actor/target key. Raw content/media is never retained.

## Ending, migration and remaining work

`cancel(frame, authorized=...)` uses `MANAGER_END`. Its boolean must come from a
fresh trusted manager authorization check; starter identity grants no authority.
For an active session it composes terminal ownership and general finalization,
then releases the reservation and queues closing output. At a cutover timestamp,
due activation runs first, then the authorized ending body ends the resulting
round. A failed finalization rolls back the ending. This preserves evidence for
future mode/theme resolution; it does not implement all those awards or cleanup.

`capability_changed(frame, facts)` uses `CAPABILITY_CHANGED`. Known prepublication
failure can end a lobby; later preplay capability loss requests recovery. Active
game capability-loss handling remains the dedicated adapter/recovery service's
responsibility. `tick(frame)` uses `TIMER`; `record().next_deadline` tells the future
scheduler the current original lobby/choice/activation boundary. Dispatch choice
expiry through `CHOICE_TIMEOUT`/`timeout`, and lobby/activation through `TIMER`/`tick`.
This facade does
not itself run a live scheduler, send retries or a disruptive-startup coordinator.

Migration 6 appends tables only. Released migrations 1–5 are unchanged. Legacy
playing/paused/held sessions reserve their slots; ended sessions do not. Existing
scores/profiles/round/pacing/choice/watermark facts survive. Unknown legacy phrase
allowances and lobby timestamps stay NULL rather than guessing a downgrade or
restarting deadlines. Reconcile those facts before future legacy phrase-bank use.
The internal playing-session seed also registers a legacy slot and cannot steal
an actual lobby reservation; it is not entitlement-authorized public admission.

Active-slot linkage uses FK cleanup, leaving global profiles and guild entitlement
facts independent. This is not a purge/24-hour final-output-retention or user
erasure service; those must cover the new lobby/guard/output rows as well. Schema 7 hands ordinary held/refresh/revival to [EvolutionFlow](EVOLUTION.md),
using the actual frozen opening configuration and session phrase window. Real
commands/permissions, delivery and disruptive-startup recovery remain unfinished. M1 has not become a playable Discord loop merely by adding
these offline transactions.
