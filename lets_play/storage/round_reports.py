"""Round-end delivery receipts over the existing immutable round ledger."""

from dataclasses import dataclass

from lets_play.storage.discord_store import DiscordStore
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.recovery_flow import active_recovery, require_dispatchable
from lets_play.storage.summary_store import SummaryStore

ROUND_PAGE_SIZE = 20


@dataclass(frozen=True, slots=True)
class RoundReport:
    session_id: str
    round_id: str
    round_number: int
    state: str | None


class RoundReports:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def pending(self, session):
        self.unit._check()
        context = await self.unit.get_context(session)
        if (
            context is None
            or context.position.lifecycle == "ended"
            or await active_recovery(self.unit, session)
        ):
            return None
        row = await _row(
            self.conn,
            """SELECT r.round_id,r.round_number,d.state FROM nr_round_resolutions r
            LEFT JOIN nr_round_reports d USING(session_id,round_id)
            WHERE r.session_id=? AND r.kind='activation' AND (d.state IS NULL OR d.state!='delivered')
            ORDER BY r.round_number LIMIT 1""",
            (session,),
        )
        return RoundReport(session, row[0], row[1], row[2]) if row else None

    async def page(self, session, round_number, *, anchor=0, backwards=False):
        self.unit._check()
        if (
            type(round_number) is not int
            or not 0 < round_number < 2**63
            or type(anchor) is not int
            or not 0 <= anchor < 2**63
            or type(backwards) is not bool
        ):
            return None
        row = await _row(
            self.conn,
            "SELECT round_id FROM nr_round_resolutions WHERE session_id=? AND round_number=? AND kind='activation'",
            (session, round_number),
        )
        if row is None:
            return None
        try:
            return await DiscordStore(self.unit)._ranked_scores(
                session, anchor, backwards, round_id=row[0], limit=ROUND_PAGE_SIZE
            )
        except ValueError:
            return None

    @_write
    async def claim(self, report, *, now):
        await require_dispatchable(self.unit, report.session_id)
        current = await self.pending(report.session_id)
        if current is None or current.round_id != report.round_id or current.state is not None:
            return False
        traffic = await _row(
            self.conn,
            "SELECT SUM(human_messages),SUM(human_messages-whispers) FROM nr_rounds WHERE session_id=? AND round_number<=?",
            (report.session_id, report.round_number),
        )
        await SummaryStore(self.unit).initialize(report.session_id)
        return bool(
            await _execute(
                self.conn,
                "INSERT INTO nr_round_reports(session_id,round_id,state,attempted_at_utc,human_messages,nonwhispers) VALUES (?,?,'sending',?,?,?) ON CONFLICT DO NOTHING",
                (report.session_id, report.round_id, utc_int(now), traffic[0], traffic[1]),
            )
        )

    @_write
    async def acknowledge(self, report, *, message_id, now):
        if type(message_id) is not int or not 0 < message_id < 2**63:
            raise ValueError("Invalid round report message identity.")
        row = await _row(
            self.conn,
            "SELECT * FROM nr_round_reports WHERE session_id=? AND round_id=?",
            (report.session_id, report.round_id),
        )
        if row and row["state"] == "delivered":
            if row["message_id"] != message_id:
                raise ValueError("Conflicting round report acknowledgement.")
            return False
        if row is None or row["state"] != "sending" or utc_int(now) < row["attempted_at_utc"]:
            raise ValueError("Round report acknowledgement requires a known send.")
        await _execute(
            self.conn,
            "UPDATE nr_round_reports SET state='delivered',message_id=?,delivered_at_utc=? WHERE session_id=? AND round_id=?",
            (message_id, utc_int(now), report.session_id, report.round_id),
        )
        await SummaryStore(self.unit).public_success(
            report.session_id,
            message_id=message_id,
            now=now,
            human_messages=row["human_messages"],
            nonwhispers=row["nonwhispers"],
            round_id=report.round_id,
            ordinary_posts=0,
        )
        return True

    @_write
    async def uncertain(self, report):
        return bool(
            await _execute(
                self.conn,
                "UPDATE nr_round_reports SET state='uncertain' WHERE session_id=? AND round_id=? AND state='sending'",
                (report.session_id, report.round_id),
            )
        )
