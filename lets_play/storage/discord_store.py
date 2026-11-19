"""Bounded adapter reads and durable send claims. No SDK objects or network I/O."""

from bisect import insort
from dataclasses import dataclass
from datetime import datetime

from lets_play.new_rules.contracts import SessionRoute
from lets_play.new_rules.evaluation import EvaluationContext
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.breaking_flow import BreakingFlow, BreakingMoment
from lets_play.storage.evolution_flow import EvolutionFlow, RevivalRecord
from lets_play.storage.game_codec import IncompatibleState, decode_score
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
    offer_display_ready: bool = True
    recovery_resume_at: datetime | None = None
    offer_panel_id: int | None = None
    recovery_notice_id: int | None = None
    phase_control_id: int | None = None
    mode: BreakingMoment | None = None

    @property
    def recovery_control_id(self):
        return self.offer_panel_id or self.phase_control_id

    @property
    def terminal_recovery_deadline(self):
        if self.admission.stage == "lobby_published":
            return self.admission.lobby_deadline
        if self.revival and self.revival.stage == "published":
            return self.revival.deadline
        return None


@dataclass(frozen=True, slots=True)
class PendingOutput:
    family: str
    output_id: int
    kind: str
    token: str
    round_id: str | None = None
    attempt: int = 1

    @property
    def terminal(self):
        return (self.family, self.kind) in {("opening", "closed"), ("evolution", "closed_silent")}

    @property
    def choice(self):
        return (self.family, self.kind) == ("ordinary", "offer")

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
            """SELECT d.message_id,d.ack_sequence FROM nr_discord_deliveries d
            WHERE d.session_id=? AND d.message_id IS NOT NULL AND NOT EXISTS(
                SELECT 1 FROM nr_evolution_outputs o WHERE d.family='evolution'
                AND o.output_id=d.output_id AND o.kind='mode_ended'
            ) ORDER BY ack_sequence DESC,attempted_at_utc DESC,output_id DESC LIMIT 1""",
            (session,),
        )
        notice = await _row(
            self.conn,
            "SELECT notice_message_id,notice_ack_sequence FROM nr_recoveries WHERE session_id=? "
            "AND notice_message_id IS NOT NULL ORDER BY notice_ack_sequence DESC LIMIT 1",
            (session,),
        )
        if notice and (panel is None or notice[1] > panel[1]):
            panel = notice
        recovery = await active_recovery(self.unit, session)
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
        offer_panel_id = (
            await OrdinaryFlow(self.unit).known_message_id(session, offer.hand.offer_id, "offer")
            if offer and offer.stage == "published"
            else None
        )
        recovery_notice_id = notice[0] if notice and panel and panel[0] == notice[0] else None
        pacing = await RoundPacing(self.unit).record(session) if context else None
        revival = await EvolutionFlow(self.unit).revival(session) if context else None
        phase_control_id = None
        if admission.stage in {"lobby_published", "opening_published"}:
            kind = "lobby" if admission.stage == "lobby_published" else "offer"
            phase_control_id = await OpeningFlow(self.unit).known_message_id(session, kind)
        elif revival and revival.stage == "published" and admission.stage == "active":
            phase_control_id = await self._revival_message(session, revival)
        if not recovery and recovery_notice_id and (control := offer_panel_id or phase_control_id):
            # The notice never takes ownership of previously published buttons.
            panel = (control,)
        round_row = (
            await _row(
                self.conn,
                "SELECT round_number FROM nr_rounds WHERE session_id=? AND round_id=?",
                (session, context.position.round_id),
            )
            if context
            else None
        )
        mode = await BreakingFlow(self.unit).active(context.position) if context else None
        boundaries = []
        if mode and context.position.lifecycle == "playing":
            boundaries.append(mode.ends_at)
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
            recovery["recovery_id"] if recovery else None,
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
            not offer
            or (
                offer.stage != "prepared"
                and (
                    offer.stage != "selected"
                    or await OrdinaryFlow(self.unit).known_message_id(
                        session, offer.hand.offer_id, "activation"
                    )
                    is not None
                )
            ),
            utc_datetime(recovery["resume_at_utc"])
            if recovery and recovery["stage"] == "reading" and recovery["resume_at_utc"] is not None
            else None,
            offer_panel_id,
            recovery_notice_id,
            phase_control_id,
            mode,
        )

    async def _ranked_scores(self, session, anchor, backwards, *, round_id=None, limit=10):
        # Exact Fraction ordering. Read keyset batches and retain only eleven
        # candidates; never load an all-player cache or sort rounded percentages.
        def key(actor, score):
            ranking = score.ranking_percent()
            return ranking is None, -(ranking or 0), actor

        table = "nr_round_scores" if round_id else "nr_participants"
        condition = "session_id=?" + (" AND round_id=?" if round_id else "")
        args = (session, round_id) if round_id else (session,)
        pivot = None
        if anchor:
            row = await _row(
                self.conn,
                f"SELECT * FROM {table} WHERE {condition} AND actor_id=?",
                (*args, anchor),
            )
            if row is None:
                raise ValueError("Unknown result cursor.")
            pivot = key(anchor, decode_score(row))
        best, last = [], 0
        while True:
            async with self.conn.execute(
                f"SELECT * FROM {table} WHERE {condition} AND actor_id>? ORDER BY actor_id LIMIT 64",
                (*args, last),
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
                if len(best) > limit + 1:
                    best.pop(0 if backwards else -1)
            last = batch[-1]["actor_id"]
        more = len(best) > limit
        if backwards and more:
            best.pop(0)
        scores = tuple((actor, score) for _, actor, score in best[:limit])
        return scores, more if backwards else bool(anchor), bool(anchor) if backwards else more

    async def route(self, session):
        self.unit._check()
        row = await _row(
            self.conn,
            "SELECT guild_id,channel_id FROM nr_admissions WHERE session_id=?",
            (session,),
        )
        return SessionRoute(row[0], row[1], session) if row else None

    async def channel_busy(self, guild, channel):
        self.unit._check()
        return bool(
            await _row(
                self.conn,
                "SELECT 1 FROM nr_admissions WHERE guild_id=? AND channel_id=? AND stage!='ended'",
                (guild, channel),
            )
        )

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
        mode_end = await _row(
            self.conn,
            """SELECT o.output_id,o.token FROM nr_evolution_outputs o
               JOIN nr_sessions s USING(session_id) WHERE o.session_id=? AND o.kind='mode_ended'
               AND s.lifecycle<>'ended' AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
               ORDER BY o.output_id LIMIT 1""",
            (session,),
        )
        if mode_end:
            return PendingOutput("evolution", mode_end[0], "mode_ended", mode_end[1])
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
        claimed = await _execute(
            self.conn,
            "INSERT INTO nr_discord_deliveries(session_id,family,output_id,state,attempted_at_utc) VALUES (?,?,?,'sending',?) ON CONFLICT DO NOTHING",
            (session, output.family, output.output_id, utc_int(now)),
        )
        if claimed and (output.choice or output.terminal):
            state = await self.state(session)
            target = (
                state.panel_id
                if (output.choice or output.kind == "closed_silent" or state.context is None)
                else None
            )
            if output.choice:
                if state.offer.published_at is None and await RoundPacing(self.unit).busy_warning(
                    session, state.context.position.round_id
                ):
                    target = None
                elif target is None:
                    raise IncompatibleState("A choice edit needs its original panel.")
            await _execute(
                self.conn,
                "INSERT INTO nr_discord_publication_targets VALUES (?,?,?)",
                (output.family, output.output_id, target),
            )
        return bool(claimed)

    async def publication_target(self, session, output):
        return await _row(
            self.conn,
            """SELECT d.attempted_at_utc,t.target_message_id FROM nr_discord_deliveries d
               JOIN nr_discord_publication_targets t USING(family,output_id)
               WHERE d.session_id=? AND d.family=? AND d.output_id=?
               AND d.state='sending' AND d.message_id IS NULL AND d.ack_sequence=0""",
            (session, output.family, output.output_id),
        )

    async def terminal_routes(self, *, after_session="", limit=100):
        self.unit._check()
        async with self.conn.execute(
            """SELECT a.guild_id,a.channel_id,a.session_id FROM nr_admissions a
               WHERE a.stage='ended' AND a.session_id>? AND (
                   EXISTS (SELECT 1 FROM nr_opening_outputs o WHERE o.session_id=a.session_id
                       AND o.kind='closed' AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL)
                   OR EXISTS (SELECT 1 FROM nr_evolution_outputs o WHERE o.session_id=a.session_id
                       AND o.kind='closed_silent' AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL)
               ) ORDER BY a.session_id LIMIT ?""",
            (after_session, limit),
        ) as cursor:
            return tuple(SessionRoute(*row) for row in await cursor.fetchall())

    async def uncertain_choice_target(self, session):
        row = await _row(
            self.conn,
            """SELECT t.target_message_id FROM nr_discord_deliveries d
               JOIN nr_discord_publication_targets t USING(family,output_id)
               JOIN nr_ordinary_outputs o ON o.session_id=d.session_id AND o.output_id=d.output_id
               WHERE d.session_id=? AND d.family='ordinary' AND d.state='sending'
               AND o.kind='offer' AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
               AND t.target_message_id IS NOT NULL LIMIT 1""",
            (session,),
        )
        return row[0] if row else None

    async def delivered(self, output, message_id, sequence):
        self.unit._check(write=True)
        await _execute(
            self.conn,
            "UPDATE nr_discord_deliveries SET state='delivered',message_id=?,ack_sequence=? WHERE family=? AND output_id=? AND state='sending'",
            (message_id, sequence, output.family, output.output_id),
        )

    async def _revival_message(self, session, revival):
        row = await _row(
            self.conn,
            "SELECT message_id FROM nr_pacing_outputs WHERE session_id=? AND round_id=? AND attempt=? AND kind='revival'",
            (session, revival.round_id, revival.attempt),
        )
        return row[0] if row else None

    async def control_message(self, session, kind):
        self.unit._check()
        state = await self.state(session)
        if kind == "revive" and state.revival:
            return await self._revival_message(session, state.revival)
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
            """SELECT m.intent_id,m.source_id FROM nr_marker_intents m
               JOIN nr_admissions a USING(session_id)
               WHERE m.session_id=? AND a.stage<>'ended' ORDER BY m.intent_id LIMIT 32""",
            (session,),
        ) as cursor:
            return tuple(tuple(r) for r in await cursor.fetchall())
