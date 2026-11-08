"""Bounded adapter reads and durable send claims. No SDK objects or network I/O."""

from bisect import insort
from dataclasses import dataclass
from datetime import datetime

from lets_play.new_rules.contracts import SessionRoute
from lets_play.new_rules.evaluation import EvaluationContext
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.evolution_flow import EvolutionFlow, RevivalRecord
from lets_play.storage.game_codec import decode_score
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.offer_codec import utc_datetime, utc_int
from lets_play.storage.opening_flow import AdmissionRecord, OpeningFlow
from lets_play.storage.ordinary_flow import OfferRecord, OrdinaryFlow
from lets_play.storage.pacing_flow import PacingRecord, RoundPacing
from lets_play.storage.recovery_flow import active_recovery


@dataclass(frozen=True, slots=True)
class DisplayState:
    admission: AdmissionRecord
    context: EvaluationContext | None
    recovery_id: str | None
    panel_id: int | None
    end_reason: str | None
    scores: tuple[tuple[int, ScoreTotals], ...]
    more_scores: bool
    selected_card: str | None
    offer: OfferRecord | None = None
    pacing: PacingRecord | None = None
    revival: RevivalRecord | None = None
    round_number: int | None = None
    next_deadline: datetime | None = None
    timeout_due: bool = False
    previous_scores: bool = False


@dataclass(frozen=True, slots=True)
class PendingOutput:
    family: str
    output_id: int
    kind: str
    token: str
    round_id: str | None = None
    attempt: int = 1

    @property
    def ui_kind(self):
        return self.kind if self.family == "opening" else f"{self.family}_{self.kind}"


class DiscordStore:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def state(self, session, *, page=0, result_anchor=0, result_backwards=False):
        self.unit._check()
        if type(page) is not int or not 0 <= page <= 1_000_000:
            raise ValueError("Invalid result page.")
        if (
            type(result_anchor) is not int
            or not 0 <= result_anchor < 2**63
            or type(result_backwards) is not bool
        ):
            raise ValueError("Invalid result cursor.")
        admission = await OpeningFlow(self.unit).record(session)
        if admission is None:
            return None
        row = await _row(
            self.conn,
            "SELECT end_reason,selected_incoming FROM nr_admissions WHERE session_id=?",
            (session,),
        )
        panel = await _row(
            self.conn,
            """SELECT message_id FROM nr_discord_deliveries WHERE session_id=? AND message_id IS NOT NULL
            ORDER BY ack_sequence DESC,attempted_at_utc DESC,output_id DESC LIMIT 1""",
            (session,),
        )
        if panel is None:
            panel = await _row(
                self.conn,
                "SELECT message_id FROM nr_opening_outputs WHERE session_id=? AND message_id IS NOT NULL ORDER BY output_id DESC LIMIT 1",
                (session,),
            )
        context = await self.unit.get_context(session)
        offer = (
            await OrdinaryFlow(self.unit).get_offer(session, context.position.offer_id)
            if context and context.position.offer_id
            else None
        )
        pacing = await RoundPacing(self.unit).record(session) if context else None
        revival = await EvolutionFlow(self.unit).revival(session) if context else None
        round_row = (
            await _row(
                self.conn,
                "SELECT round_number FROM nr_rounds WHERE session_id=? AND round_id=?",
                (session, context.position.round_id),
            )
            if context
            else None
        )
        boundaries = []
        if context is None and admission.next_deadline:
            boundaries.append(admission.next_deadline)
        if offer and offer.stage == "published" and offer.choice_deadline:
            boundaries.append(offer.choice_deadline.expires_at)
        if offer and offer.stage == "selected" and offer.effective_at:
            boundaries.append(offer.effective_at)
        if pacing and pacing.next_deadline:
            boundaries.append(pacing.next_deadline)
        if context and (deadline := await EvolutionFlow(self.unit).next_deadline(session)):
            boundaries.append(deadline)
        next_deadline = min(boundaries) if boundaries else None
        previous_scores = False
        if admission.stage == "ended" and context is not None:
            scores, previous_scores, more_scores = await self._ranked_scores(
                session, result_anchor, result_backwards
            )
        else:
            async with self.conn.execute(
                "SELECT * FROM nr_participants WHERE session_id=? ORDER BY actor_id LIMIT 11 OFFSET ?",
                (session, page * 10),
            ) as cursor:
                rows = await cursor.fetchall()
            scores = tuple((r["actor_id"], decode_score(r)) for r in rows[:10])
            more_scores = len(rows) > 10
        return DisplayState(
            admission,
            context,
            (
                recovery["recovery_id"]
                if (recovery := await active_recovery(self.unit, session))
                else None
            ),
            panel[0] if panel else None,
            row[0],
            scores,
            more_scores,
            next(
                (
                    o.incoming.card_id
                    for o in admission.hand.options
                    if o.incoming.instance_id == row[1]
                ),
                None,
            )
            if admission.hand
            else None,
            offer,
            pacing,
            revival,
            round_row[0] if round_row else None,
            next_deadline,
            bool(
                offer
                and offer.stage == "published"
                and offer.choice_deadline
                and next_deadline == offer.choice_deadline.expires_at
            ),
            previous_scores,
        )

    async def _ranked_scores(self, session, anchor, backwards):
        # Exact Fraction ordering. Read keyset batches and retain only eleven
        # candidates; never load an all-player cache or sort rounded percentages.
        def key(actor, score):
            ranking = score.ranking_percent()
            return ranking is None, -(ranking or 0), actor

        pivot = None
        if anchor:
            row = await _row(
                self.conn,
                "SELECT * FROM nr_participants WHERE session_id=? AND actor_id=?",
                (session, anchor),
            )
            if row is None:
                raise ValueError("Unknown result cursor.")
            pivot = key(anchor, decode_score(row))
        best, last = [], 0
        while True:
            async with self.conn.execute(
                "SELECT * FROM nr_participants WHERE session_id=? AND actor_id>? ORDER BY actor_id LIMIT 64",
                (session, last),
            ) as cursor:
                batch = await cursor.fetchall()
            if not batch:
                break
            for row in batch:
                actor, score = row["actor_id"], decode_score(row)
                order = key(actor, score)
                if pivot is not None and (order >= pivot if backwards else order <= pivot):
                    continue
                insort(best, (order, actor, score))
                if len(best) > 11:
                    best.pop(0 if backwards else -1)
            last = batch[-1]["actor_id"]
        more = len(best) > 10
        if backwards and more:
            best.pop(0)
        scores = tuple((actor, score) for _, actor, score in best[:10])
        return scores, more if backwards else bool(anchor), bool(anchor) if backwards else more

    async def route(self, session):
        self.unit._check()
        row = await _row(
            self.conn,
            "SELECT guild_id,channel_id FROM nr_admissions WHERE session_id=?",
            (session,),
        )
        return SessionRoute(row[0], row[1], session) if row else None

    async def is_lobby_member(self, session, actor):
        self.unit._check()
        return bool(
            await _row(
                self.conn,
                """SELECT 1 FROM nr_lobby_members m JOIN nr_admissions a USING(session_id)
            WHERE m.session_id=? AND m.actor_id=? AND a.stage<>'ended'""",
                (session, actor),
            )
        )

    async def receipt_position(self, session):
        self.unit._check()
        row = await _row(
            self.conn,
            """SELECT COALESCE(w.last_sequence,a.last_sequence),
            COALESCE(w.observed_at_utc,a.observed_at_utc) FROM nr_admissions a
            LEFT JOIN nr_flow_watermarks w USING(session_id) WHERE a.session_id=?""",
            (session,),
        )
        return (row[0], utc_datetime(row[1]) if row[1] is not None else None) if row else (0, None)

    async def pending(self, session):
        self.unit._check()
        if await active_recovery(self.unit, session):
            return None
        for family, flow in (
            ("opening", OpeningFlow(self.unit)),
            ("ordinary", OrdinaryFlow(self.unit)),
            ("pacing", RoundPacing(self.unit)),
            ("evolution", EvolutionFlow(self.unit)),
        ):
            outputs = await flow.pending_outputs(limit=1, session_id=session)
            if not outputs:
                continue
            output = outputs[0]
            token = (
                output.kind
                if family == "opening"
                else output.offer_id
                if family == "ordinary"
                else output.round_id
                if family == "pacing"
                else output.token
            )
            if family == "pacing" and output.kind == "revival":
                revival = await EvolutionFlow(self.unit).revival(session)
                token = revival.revival_id
            return PendingOutput(
                family,
                output.output_id,
                output.kind,
                token,
                getattr(output, "round_id", None),
                getattr(output, "attempt", 1),
            )
        return None

    async def claim(self, session, output, now):
        self.unit._check(write=True)
        if await self.pending(session) != output:
            return False
        return bool(
            await _execute(
                self.conn,
                "INSERT INTO nr_discord_deliveries(session_id,family,output_id,state,attempted_at_utc) VALUES (?,?,?,'sending',?) ON CONFLICT DO NOTHING",
                (session, output.family, output.output_id, utc_int(now)),
            )
        )

    async def delivered(self, output, message_id, sequence):
        self.unit._check(write=True)
        await _execute(
            self.conn,
            "UPDATE nr_discord_deliveries SET state='delivered',message_id=?,ack_sequence=? WHERE family=? AND output_id=? AND state='sending'",
            (message_id, sequence, output.family, output.output_id),
        )

    async def control_message(self, session, kind):
        self.unit._check()
        state = await self.state(session)
        if kind == "revive" and state.revival:
            row = await _row(
                self.conn,
                "SELECT message_id FROM nr_pacing_outputs WHERE session_id=? AND round_id=? AND attempt=? AND kind='revival'",
                (session, state.revival.round_id, state.revival.attempt),
            )
            return row[0] if row else None
        if kind in {"choose", "target"} and state.offer:
            return await OrdinaryFlow(self.unit).known_message_id(
                session, state.offer.hand.offer_id, "offer"
            )
        return await OpeningFlow(self.unit).known_message_id(
            session, "lobby" if kind == "join" else "offer"
        )

    async def markers(self, session):
        self.unit._check()
        if await active_recovery(self.unit, session):
            return ()
        # Never depend on global page position for one session's work.
        async with self.conn.execute(
            "SELECT intent_id,source_id FROM nr_marker_intents WHERE session_id=? ORDER BY intent_id LIMIT 32",
            (session,),
        ) as cursor:
            return tuple(tuple(r) for r in await cursor.fetchall())
