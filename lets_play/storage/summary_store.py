"""Summary capture/ACK and bounded projections. No scheduler, SDK or network I/O.

Compose writes after engine effects in the same ordered receipt unit. A snapshot
is provisional until commit; claim before HTTP and ACK only actual public success.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from lets_play.new_rules.contracts import RuleInstance
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.game_codec import SCORE_COLUMNS, IncompatibleState, decode_score
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import utc_datetime, utc_int
from lets_play.storage.recovery_flow import active_recovery, require_dispatchable

AUTO_SECONDS = 60
AUTO_MESSAGES = 25
REQUEST_COALESCE_SECONDS = 30
_COLUMNS = ",".join(SCORE_COLUMNS)


def _key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:._-]{1,128}", value):
        raise ValueError("A summary key must be a bounded server identifier.")


@dataclass(frozen=True, slots=True)
class SummarySnapshot:
    summary_id: int
    session_id: str
    output_key: str
    kind: str
    state: str
    round_id: str
    round_number: int
    captured_at: datetime
    human_messages: int
    whispers: int
    round_messages: int
    round_whispers: int
    rules: tuple[RuleInstance, ...]
    message_id: int | None


@dataclass(frozen=True, slots=True)
class LiveSummaryPage:
    session_id: str
    scope: str
    round_number: int
    lifecycle: str
    round_closed: bool
    as_of: datetime | None
    human_messages: int
    whispers: int
    players: tuple[tuple[int, ScoreTotals], ...]
    previous: bool
    following: bool


class SummaryStore:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def pending(self, session):
        self.unit._check()
        row = await _row(
            self.conn,
            "SELECT output_key FROM nr_summary_outputs WHERE session_id=? AND state IN ('prepared','sending','uncertain')",
            (session,),
        )
        return await self.snapshot(session, row[0]) if row else None

    @_write
    async def request(self, session, output_key, *, now):
        """Coalesce explicit public requests without advancing the delivery baseline."""
        _key(output_key)
        context = await self.unit.get_context(session)
        if context is None or context.position.lifecycle == "ended":
            return "unavailable"
        existing = await self.snapshot(session, output_key) or await self.pending(session)
        if existing:
            return "uncertain" if existing.state == "uncertain" else "shared"
        previous = await _row(
            self.conn,
            "SELECT output_key,delivered_at_utc,human_messages-whispers FROM nr_summary_outputs WHERE session_id=? AND state='delivered' ORDER BY summary_id DESC LIMIT 1",
            (session,),
        )
        if previous:
            # SDK request keys contain increasing interaction IDs. A replay of an
            # older interaction stays harmless after its obsolete receipt is pruned.
            prior_id = previous["output_key"].removeprefix("request:")
            current_id = output_key.removeprefix("request:")
            if prior_id.isdecimal() and current_id.isdecimal() and int(current_id) <= int(prior_id):
                return "shared"
            if utc_int(now - timedelta(seconds=REQUEST_COALESCE_SECONDS)) < previous[1]:
                return "recent"
            traffic = await self.unit.get_traffic(session)
            if traffic.human_messages - traffic.whispers <= previous[2]:
                return "no_new_posts"
        snapshot = await self.prepare(session, output_key, now=now, kind="requested")
        return "queued" if snapshot else "unavailable"

    async def _compact_session(self, session):
        # Only unresolved output keeps copied player scores/rules. One final
        # receipt supplies time, traffic and replay protection for the next send.
        await _execute(
            self.conn,
            "DELETE FROM nr_summary_scores WHERE summary_id IN (SELECT summary_id FROM nr_summary_outputs WHERE session_id=? AND state='delivered')",
            (session,),
        )
        await _execute(
            self.conn,
            "UPDATE nr_summary_outputs SET rules_json='[]' WHERE session_id=? AND state='delivered' AND rules_json!='[]'",
            (session,),
        )
        await _execute(
            self.conn,
            "DELETE FROM nr_summary_outputs WHERE session_id=? AND state='delivered' AND summary_id!=(SELECT MAX(summary_id) FROM nr_summary_outputs WHERE session_id=? AND state='delivered')",
            (session, session),
        )

    @_write
    async def compact_delivered(self, *, after_summary=0, limit=100):
        """Page legacy receipts at startup, including ended games; keep pending proof."""
        if (
            type(after_summary) is not int
            or not 0 <= after_summary < 2**63
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid bounded summary cleanup page.")
        async with self.conn.execute(
            "SELECT summary_id,session_id FROM nr_summary_outputs WHERE state='delivered' AND summary_id>? ORDER BY summary_id LIMIT ?",
            (after_summary, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        for session in {r["session_id"] for r in rows}:
            await self._compact_session(session)
        return rows[-1]["summary_id"] if rows else None

    async def preview(self, snapshot):
        """One consistent public preview while sending; no historical page service."""
        return LiveSummaryPage(
            snapshot.session_id,
            "round",
            snapshot.round_number,
            "captured",
            False,
            snapshot.captured_at,
            snapshot.round_messages,
            snapshot.round_whispers,
            await self.scores(snapshot.session_id, snapshot.output_key, scope="round", limit=5),
            False,
            False,
        )

    @_write
    async def initialize(self, session):
        context = await self.unit.get_context(session)
        if context is None or context.position.lifecycle == "ended":
            return False
        await require_dispatchable(self.unit, session)
        first = await _row(
            self.conn,
            "SELECT started_at_utc FROM nr_rounds WHERE session_id=? ORDER BY round_number LIMIT 1",
            (session,),
        )
        if first is None or first[0] is None:
            raise IncompatibleState("Summary initialization needs the real first activation time.")
        return bool(
            await _execute(
                self.conn,
                "INSERT INTO nr_summary_baselines(session_id,public_at_utc) VALUES (?,?) ON CONFLICT DO NOTHING",
                (session, first[0]),
            )
        )

    async def snapshot(self, session, output_key):
        self.unit._check()
        _key(output_key)
        row = await _row(
            self.conn,
            "SELECT * FROM nr_summary_outputs WHERE session_id=? AND output_key=?",
            (session, output_key),
        )
        if row is None:
            return None
        rules = tuple(
            RuleInstance(i, card, tuple(tuple(p) for p in parameters))
            for i, card, parameters in json.loads(row["rules_json"])
        )
        return SummarySnapshot(
            row["summary_id"],
            session,
            output_key,
            row["kind"],
            row["state"],
            row["round_id"],
            row["round_number"],
            utc_datetime(row["captured_at_utc"]),
            row["human_messages"],
            row["whispers"],
            row["round_messages"],
            row["round_whispers"],
            rules,
            row["message_id"],
        )

    async def next_deadline(self, session):
        self.unit._check()
        if await active_recovery(self.unit, session):
            return None
        row = await _row(
            self.conn,
            """SELECT b.public_at_utc FROM nr_summary_baselines b
            JOIN nr_sessions s USING(session_id) WHERE b.session_id=? AND s.lifecycle='playing'
            AND s.human_messages-b.ack_messages>=?
            AND NOT EXISTS(SELECT 1 FROM nr_summary_outputs o WHERE o.session_id=b.session_id
                AND o.state IN ('prepared','sending','uncertain'))""",
            (session, AUTO_MESSAGES),
        )
        return utc_datetime(row[0]) + timedelta(seconds=AUTO_SECONDS) if row else None

    @_write
    async def prepare(self, session, output_key, *, now, kind="automatic"):
        _key(output_key)
        if kind not in {"automatic", "requested"}:
            raise ValueError("Unsupported summary capture kind.")
        captured = utc_int(now)
        previous = await self.snapshot(session, output_key)
        if previous is not None:
            if previous.kind != kind:
                raise ValueError("A summary key cannot change its meaning.")
            return previous
        context = await self.unit.get_context(session)
        if context is None or context.position.lifecycle == "ended":
            return None
        await require_dispatchable(self.unit, session)
        await self.initialize(session)
        baseline = await _row(
            self.conn,
            "SELECT public_at_utc FROM nr_summary_baselines WHERE session_id=?",
            (session,),
        )
        watermark = await _row(
            self.conn,
            "SELECT observed_at_utc FROM nr_flow_watermarks WHERE session_id=?",
            (session,),
        )
        if captured < baseline[0] or (watermark and captured < watermark[0]):
            raise ValueError("Summary capture time cannot precede observed state.")
        pending = await _row(
            self.conn,
            "SELECT 1 FROM nr_summary_outputs WHERE session_id=? AND state IN ('prepared','sending','uncertain') LIMIT 1",
            (session,),
        )
        if pending:
            return None
        if kind == "automatic":
            deadline = await self.next_deadline(session)
            if deadline is None or now < deadline:
                return None
        p = context.position
        traffic = await self.unit.get_traffic(session)
        rnd = await _row(
            self.conn,
            "SELECT round_number,human_messages,whispers FROM nr_rounds WHERE session_id=? AND round_id=?",
            (session, p.round_id),
        )
        rules = json.dumps(
            [(r.instance_id, r.card_id, r.parameters) for r in context.active_instances]
        )
        cursor = await self.conn.execute(
            """INSERT INTO nr_summary_outputs(session_id,output_key,kind,state,round_id,round_number,captured_at_utc,human_messages,whispers,round_messages,round_whispers,rules_json)
            VALUES (?,?,?,'prepared',?,?,?,?,?,?,?,?)""",
            (
                session,
                output_key,
                kind,
                p.round_id,
                rnd[0],
                captured,
                traffic.human_messages,
                traffic.whispers,
                rnd[1],
                rnd[2],
                rules,
            ),
        )
        summary = cursor.lastrowid
        await cursor.close()
        for scope, table in (("game", "nr_participants"), ("round", "nr_round_scores")):
            condition = "session_id=?" + (" AND round_id=?" if scope == "round" else "")
            values = (session, p.round_id) if scope == "round" else (session,)
            await _execute(
                self.conn,
                f"INSERT INTO nr_summary_scores SELECT ?,?,actor_id,{_COLUMNS} FROM {table} WHERE {condition}",
                (summary, scope, *values),
            )
        return await self.snapshot(session, output_key)

    @_write
    async def claim(self, session, output_key, *, now):
        row = await self.snapshot(session, output_key)
        attempted = utc_int(now)
        if row is None or row.state != "prepared":
            return False
        await require_dispatchable(self.unit, session)
        if now < row.captured_at:
            raise ValueError("A send attempt cannot precede capture.")
        context = await self.unit.get_context(session)
        if context is None or context.position.lifecycle == "ended":
            return False
        return bool(
            await _execute(
                self.conn,
                "UPDATE nr_summary_outputs SET state='sending',attempted_at_utc=? WHERE summary_id=? AND state='prepared'",
                (attempted, row.summary_id),
            )
        )

    @_write
    async def mark_uncertain(self, session, output_key):
        _key(output_key)
        return bool(
            await _execute(
                self.conn,
                "UPDATE nr_summary_outputs SET state='uncertain' WHERE session_id=? AND output_key=? AND state='sending'",
                (session, output_key),
            )
        )

    @_write
    async def acknowledge(self, session, output_key, *, message_id, now):
        if type(message_id) is not int or not 0 < message_id < 2**63:
            raise ValueError("A summary ACK needs a positive SQLite-range message ID.")
        delivered = utc_int(now)
        row = await self.snapshot(session, output_key)
        if row is None:
            raise ValueError("Unknown summary acknowledgement.")
        if row.state == "delivered":
            if row.message_id != message_id:
                raise ValueError("Conflicting summary acknowledgement.")
            return False
        attempt = await _row(
            self.conn,
            "SELECT attempted_at_utc FROM nr_summary_outputs WHERE summary_id=?",
            (row.summary_id,),
        )
        if row.state != "sending" or attempt[0] is None or delivered < attempt[0]:
            raise ValueError("A summary ACK needs its actual ordered send attempt.")
        # Actual public success remains a fact during disruption/ending. This
        # updates no enforcement owner and never resumes scoring.
        await _execute(
            self.conn,
            "UPDATE nr_summary_outputs SET state='delivered',message_id=?,delivered_at_utc=? WHERE summary_id=?",
            (message_id, delivered, row.summary_id),
        )
        await _execute(
            self.conn,
            "UPDATE nr_summary_baselines SET public_at_utc=MAX(public_at_utc,?),ack_messages=MAX(ack_messages,?) WHERE session_id=?",
            (delivered, row.human_messages, session),
        )
        await self._compact_session(session)
        return True

    async def scores(self, session, output_key, *, scope="game", after_actor=0, limit=20):
        self.unit._check()
        if (
            scope not in {"game", "round"}
            or type(after_actor) is not int
            or not 0 <= after_actor < 2**63
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("Invalid bounded summary page.")
        row = await self.snapshot(session, output_key)
        if row is None:
            return ()
        async with self.conn.execute(
            "SELECT * FROM nr_summary_scores WHERE summary_id=? AND scope=? AND actor_id>? ORDER BY actor_id LIMIT ?",
            (row.summary_id, scope, after_actor, limit),
        ) as cursor:
            return tuple((r["actor_id"], decode_score(r)) for r in await cursor.fetchall())

    async def live_page(
        self, session, *, scope="round", round_number=0, actor=0, backwards=False, limit=5
    ):
        """Read-only current totals; round pages stay bound to their round number.

        Unlike public captures, values can change between page requests. Actor
        keysets keep navigation bounded without an all-player cache or new writes.
        """
        self.unit._check()
        if (
            scope not in {"round", "game"}
            or type(round_number) is not int
            or not 0 <= round_number < 2**63
            or type(actor) is not int
            or not 0 <= actor < 2**63
            or type(backwards) is not bool
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            return None
        context = await self.unit.get_context(session)
        if context is None:
            return None
        p = context.position
        rnd = await _row(
            self.conn,
            "SELECT round_id,round_number,human_messages,whispers FROM nr_rounds WHERE session_id=? AND "
            + ("round_number=?" if round_number and scope == "round" else "round_id=?"),
            (session, round_number if round_number and scope == "round" else p.round_id),
        )
        if rnd is None:
            return None
        traffic = await self.unit.get_traffic(session) if scope == "game" else None
        mark = await _row(
            self.conn,
            "SELECT observed_at_utc FROM nr_flow_watermarks WHERE session_id=?",
            (session,),
        )
        condition = "session_id=?" + (" AND round_id=?" if scope == "round" else "")
        values = (session, rnd["round_id"]) if scope == "round" else (session,)
        table = "nr_round_scores" if scope == "round" else "nr_participants"
        comparison, order = ("<", "DESC") if backwards else (">", "ASC")
        async with self.conn.execute(
            f"SELECT * FROM {table} WHERE {condition} AND actor_id{comparison}? ORDER BY actor_id {order} LIMIT ?",
            (*values, actor, limit + 1),
        ) as cursor:
            rows = await cursor.fetchall()
        more = len(rows) > limit
        selected = rows[:limit]
        if backwards:
            selected.reverse()
        return LiveSummaryPage(
            session,
            scope,
            rnd["round_number"],
            p.lifecycle.value,
            scope == "round" and rnd["round_id"] != p.round_id,
            utc_datetime(mark[0]) if mark else None,
            traffic.human_messages if traffic else rnd["human_messages"],
            traffic.whispers if traffic else rnd["whispers"],
            tuple((r["actor_id"], decode_score(r)) for r in selected),
            more if backwards else bool(actor),
            bool(actor) if backwards else more,
        )
