"""Bounded adapter reads and durable send claims. No SDK objects or network I/O."""

from dataclasses import dataclass

from lets_play.new_rules.contracts import SessionRoute
from lets_play.new_rules.evaluation import EvaluationContext
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.game_codec import decode_score
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.offer_codec import utc_datetime, utc_int
from lets_play.storage.opening_flow import AdmissionRecord, OpeningFlow
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


class DiscordStore:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def state(self, session, *, page=0):
        self.unit._check()
        if type(page) is not int or not 0 <= page <= 1_000_000:
            raise ValueError("Invalid result page.")
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
            "SELECT message_id FROM nr_opening_outputs WHERE session_id=? AND message_id IS NOT NULL ORDER BY output_id DESC LIMIT 1",
            (session,),
        )
        async with self.conn.execute(
            "SELECT * FROM nr_participants WHERE session_id=? ORDER BY actor_id LIMIT 11 OFFSET ?",
            (session, page * 10),
        ) as cursor:
            rows = await cursor.fetchall()
        return DisplayState(
            admission,
            await self.unit.get_context(session),
            (
                recovery["recovery_id"]
                if (recovery := await active_recovery(self.unit, session))
                else None
            ),
            panel[0] if panel else None,
            row[0],
            tuple((r["actor_id"], decode_score(r)) for r in rows[:10]),
            len(rows) > 10,
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
        )

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
        record = await OpeningFlow(self.unit).record(session)
        kind = {
            "lobby_prepared": "lobby",
            "opening_prepared": "offer",
            "selected": "activation",
            "ended": "closed",
        }.get(record.stage if record else None)
        if kind is None:
            return None
        row = await _row(
            self.conn,
            """SELECT output_id FROM nr_opening_outputs WHERE session_id=? AND kind=?
            AND acknowledged_at_utc IS NULL AND retired_at_utc IS NULL""",
            (session, kind),
        )
        return (row[0], kind) if row else None

    async def claim(self, session, output_id, now):
        self.unit._check(write=True)
        pending = await self.pending(session)
        if pending is None or pending[0] != output_id:
            return False
        return bool(
            await _execute(
                self.conn,
                "INSERT INTO nr_discord_deliveries(output_id,state,attempted_at_utc) VALUES (?,'sending',?) ON CONFLICT DO NOTHING",
                (output_id, utc_int(now)),
            )
        )

    async def delivered(self, output_id, message_id):
        self.unit._check(write=True)
        await _execute(
            self.conn,
            "UPDATE nr_discord_deliveries SET state='delivered',message_id=? WHERE output_id=? AND state='sending'",
            (message_id, output_id),
        )

    async def markers(self, session):
        self.unit._check()
        # Never depend on global page position for one session's work.
        async with self.conn.execute(
            "SELECT intent_id,source_id FROM nr_marker_intents WHERE session_id=? ORDER BY intent_id LIMIT 32",
            (session,),
        ) as cursor:
            return tuple(tuple(r) for r in await cursor.fetchall())
