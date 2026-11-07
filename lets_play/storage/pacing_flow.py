"""Durable ordinary warning and succession over the shared ordered receipt lane."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from fractions import Fraction
from hashlib import sha256
from random import Random

from lets_play.new_rules.contracts import Advancement, EventKind, Lifecycle, PauseReason, Phase
from lets_play.new_rules.pacing import (
    FALLBACK_WARNING_SECONDS,
    MIN_WARNING_SECONDS,
    PACING_VERSION,
    WARNING_SECONDS,
    WorstResults,
    message_target,
)
from lets_play.storage.game_codec import IncompatibleState, decode_score
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import utc_datetime, utc_int


@dataclass(frozen=True, slots=True)
class PacingRecord:
    round_id: str
    stage: str
    started_at: datetime
    estimated_players: int
    message_target: int
    target_reached_at: datetime | None
    warning_published_at: datetime | None
    selection_due_at: datetime | None
    picker_id: int | None

    @property
    def next_deadline(self) -> datetime | None:
        if self.stage == "waiting":
            fallback = self.started_at + timedelta(seconds=FALLBACK_WARNING_SECONDS)
            reached = self.target_reached_at or fallback
            return max(
                self.started_at + timedelta(seconds=MIN_WARNING_SECONDS), min(reached, fallback)
            )
        if self.stage == "warning_published":
            return self.selection_due_at
        return None


@dataclass(frozen=True, slots=True)
class PacingOutput:
    output_id: int
    session_id: str
    round_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class PickerCandidate:
    actor_id: int
    ordinary_posts: int
    actual_percent: Fraction | None
    raw_faults: int
    excluded: bool


def _page(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Page limit must be an integer in [1, 100].")


class RoundPacing:
    """Facade results remain provisional until the caller's unit commits."""

    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def record(self, session_id: str, round_id: str | None = None) -> PacingRecord | None:
        self.unit._check()
        if round_id is None:
            context = await self.unit.get_context(session_id)
            if context is None:
                return None
            round_id = context.position.round_id
        row = await self._record(session_id, round_id)
        return self._decode(row) if row is not None else None

    async def _record(self, session, round_id):
        row = await _row(
            self.conn,
            "SELECT * FROM nr_round_pacing WHERE session_id=? AND round_id=?",
            (session, round_id),
        )
        if row is not None:
            version = await _row(
                self.conn, "SELECT pacing_version FROM nr_sessions WHERE session_id=?", (session,)
            )
            if version[0] != PACING_VERSION:
                raise IncompatibleState("Live pacing version needs explicit recovery.")
        return row

    @staticmethod
    def _decode(row):
        def date(column):
            return utc_datetime(row[column]) if row[column] is not None else None

        return PacingRecord(
            row["round_id"],
            row["stage"],
            date("started_at_utc"),
            row["estimated_players"],
            row["message_target"],
            date("target_reached_at_utc"),
            date("warning_published_at_utc"),
            date("selection_due_at_utc"),
            row["picker_id"],
        )

    async def pending_outputs(self, *, limit=100) -> tuple[PacingOutput, ...]:
        self.unit._check()
        _page(limit)
        async with self.conn.execute(
            """SELECT o.output_id,o.session_id,o.round_id,o.kind FROM nr_pacing_outputs AS o
            JOIN nr_sessions AS s USING(session_id)
            JOIN nr_round_pacing AS p ON p.session_id=o.session_id AND p.round_id=o.round_id
            WHERE o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
            AND s.round_id=o.round_id AND (
                (o.kind='warning' AND p.stage='warning_queued' AND s.lifecycle='playing'
                    AND s.phase='ordinary' AND s.advancement='warning'
                    AND s.revision=p.owner_revision AND s.ruleset_version=p.ruleset_version)
                OR (o.kind='revival' AND p.stage='revival' AND s.lifecycle='paused' AND s.pause_reason='revival')
                OR (o.kind='recovery' AND p.stage='cancelled' AND s.lifecycle='paused' AND s.pause_reason='recovery')
            ) ORDER BY o.output_id LIMIT ?""",
            (limit,),
        ) as cursor:
            return tuple(PacingOutput(*r) for r in await cursor.fetchall())

    async def known_warning_message_id(self, session_id: str, round_id: str) -> int | None:
        self.unit._check()
        row = await _row(
            self.conn,
            "SELECT message_id FROM nr_pacing_outputs WHERE session_id=? AND round_id=? AND kind='warning'",
            (session_id, round_id),
        )
        return row[0] if row is not None else None

    async def candidates(
        self, session_id, round_id, *, after_actor=0, limit=100
    ) -> tuple[PickerCandidate, ...]:
        self.unit._check()
        _page(limit)
        if type(after_actor) is not int or not 0 <= after_actor < (1 << 63):
            raise ValueError("A nonnegative SQLite actor cursor is required.")
        async with self.conn.execute(
            """SELECT actor_id,ordinary_posts,actual_n,actual_d,raw_faults,excluded
            FROM nr_picker_candidates WHERE session_id=? AND round_id=? AND actor_id>?
            ORDER BY actor_id LIMIT ?""",
            (session_id, round_id, after_actor, limit),
        ) as cursor:
            return tuple(
                PickerCandidate(
                    r[0],
                    r[1],
                    Fraction(int(r[2]), int(r[3])) if r[2] is not None else None,
                    r[4],
                    bool(r[5]),
                )
                for r in await cursor.fetchall()
            )

    async def _output(self, session, round_id, kind):
        await _execute(
            self.conn,
            "INSERT INTO nr_pacing_outputs(session_id,round_id,kind) VALUES (?,?,?) ON CONFLICT(session_id,round_id,kind) DO NOTHING",
            (session, round_id, kind),
        )

    @_write
    async def register_round(
        self, frame, *, estimated_players, rng, started_at=None, previous_picker_id=None
    ):
        """Trusted start facts only; registering a used legacy unknown start refuses."""
        from lets_play.storage.ordinary_flow import OrdinaryFlow

        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.PACING_REGISTER, None):
            return "replay"
        target = message_target(estimated_players)
        context = await self.unit.get_context(frame.route.session_id)
        p = context.position
        row = await self._record(p.route.session_id, p.round_id)
        if row is not None:
            if row["estimated_players"] != estimated_players or (
                started_at is not None and utc_int(started_at) != row["started_at_utc"]
            ):
                return "invalid"
            return "already_done"
        version = await _row(
            self.conn,
            "SELECT pacing_version FROM nr_sessions WHERE session_id=?",
            (p.route.session_id,),
        )
        if version[0] != PACING_VERSION:
            raise IncompatibleState("Live pacing version needs explicit recovery.")
        if (
            p.lifecycle is not Lifecycle.PLAYING
            or p.phase is not Phase.ORDINARY
            or p.advancement is not Advancement.IDLE
            or p.offer_id is not None
            or p.transition_id is not None
        ):
            return "stale"
        round_row = await _row(
            self.conn,
            "SELECT started_at_utc FROM nr_rounds WHERE session_id=? AND round_id=?",
            (p.route.session_id, p.round_id),
        )
        start = round_row[0]
        if start is None:
            traffic = await _row(
                self.conn,
                "SELECT 1 FROM nr_processed_events WHERE session_id=? AND round_id=? LIMIT 1",
                (p.route.session_id, p.round_id),
            )
            if started_at is None or traffic is not None:
                raise IncompatibleState(
                    "A known activation time is required; recover legacy live state explicitly."
                )
            start = utc_int(started_at)
        elif started_at is not None and utc_int(started_at) != start:
            raise ValueError("Registration cannot change an actual round start.")
        if start > utc_int(frame.received_at):
            raise ValueError("A round cannot start after its registration receipt.")
        if previous_picker_id is not None and (
            type(previous_picker_id) is not int or not 1 <= previous_picker_id < (1 << 63)
        ):
            raise ValueError("A known human picker ID is required.")
        seed = rng.randrange(1 << 63)
        if type(seed) is not int or not 0 <= seed < (1 << 63):
            raise ValueError("The injected random source returned an invalid seed.")
        await _execute(
            self.conn,
            "UPDATE nr_rounds SET started_at_utc=? WHERE session_id=? AND round_id=?",
            (start, p.route.session_id, p.round_id),
        )
        created = await _execute(
            self.conn,
            "INSERT INTO nr_rotation(session_id,previous_picker) VALUES (?,?) ON CONFLICT(session_id) DO NOTHING",
            (p.route.session_id, previous_picker_id),
        )
        if created and previous_picker_id is not None:
            await _execute(
                self.conn,
                "INSERT INTO nr_rotation_members(session_id,actor_id,served_cycle) VALUES (?,?,0)",
                (p.route.session_id, previous_picker_id),
            )
        await self._insert(context, start, estimated_players, target, seed)
        await self._advance_due(frame)
        return "registered"

    async def _insert(self, context, start, estimate, target, seed):
        p = context.position
        await _execute(
            self.conn,
            """INSERT INTO nr_round_pacing(session_id,round_id,ruleset_version,
            owner_revision,started_at_utc,estimated_players,message_target,random_seed,stage)
            VALUES (?,?,?,?,?,?,?,?,'waiting')""",
            (
                p.route.session_id,
                p.round_id,
                p.ruleset_version,
                p.revision,
                start,
                estimate,
                target,
                seed,
            ),
        )

    @_write
    async def tick(self, frame):
        from lets_play.storage.ordinary_flow import OrdinaryFlow

        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.TIMER, None):
            return "replay"
        record = await self.record(frame.route.session_id)
        return record.stage if record else "unregistered"

    @_write
    async def publish_warning(self, frame, round_id, *, message_id):
        from lets_play.storage.ordinary_flow import OrdinaryFlow

        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.WARNING_PUBLISHED, None):
            return "replay"
        if type(message_id) is not int or not 1 <= message_id < (1 << 63):
            raise ValueError("A positive known warning message ID is required.")
        row = await self._record(frame.route.session_id, round_id)
        output = await _row(
            self.conn,
            "SELECT * FROM nr_pacing_outputs WHERE session_id=? AND round_id=? AND kind='warning'",
            (frame.route.session_id, round_id),
        )
        if row is None or output is None:
            return "stale"
        if output["message_id"] is not None:
            if output["message_id"] != message_id:
                raise ValueError("A warning acknowledgement cannot change message identity.")
            return "already_done"
        if row["stage"] != "warning_queued" or output["retired_at_utc"] is not None:
            return "stale"
        observed = utc_int(frame.received_at)
        await _execute(
            self.conn,
            "UPDATE nr_pacing_outputs SET message_id=?,acknowledged_at_utc=? WHERE output_id=?",
            (message_id, observed, output["output_id"]),
        )
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET warning_published_at_utc=?,selection_due_at_utc=?,stage='warning_published' WHERE session_id=? AND round_id=?",
            (
                observed,
                utc_int(frame.received_at + timedelta(seconds=WARNING_SECONDS)),
                frame.route.session_id,
                round_id,
            ),
        )
        return "published"

    async def _advance_due(self, frame):
        context = await self.unit.get_context(frame.route.session_id)
        p = context.position
        row = await self._record(frame.route.session_id, p.round_id)
        if row is None or row["stage"] not in ("waiting", "warning_queued", "warning_published"):
            return
        expected_advance = Advancement.IDLE if row["stage"] == "waiting" else Advancement.WARNING
        if (
            p.lifecycle is not Lifecycle.PLAYING
            or p.phase is not Phase.ORDINARY
            or p.ruleset_version != row["ruleset_version"]
            or p.revision != row["owner_revision"]
            or p.advancement is not expected_advance
        ):
            await self._cancel(frame, row, context)
            return
        if row["stage"] == "waiting":
            count = await _row(
                self.conn,
                "SELECT ordinary_posts FROM nr_rounds WHERE session_id=? AND round_id=?",
                (p.route.session_id, p.round_id),
            )
            if row["target_reached_at_utc"] is None and count[0] >= row["message_target"]:
                await _execute(
                    self.conn,
                    "UPDATE nr_round_pacing SET target_reached_at_utc=? WHERE session_id=? AND round_id=?",
                    (utc_int(frame.received_at), p.route.session_id, p.round_id),
                )
                row = await self._record(p.route.session_id, p.round_id)
            if frame.received_at >= self._decode(row).next_deadline:
                new = replace(p, advancement=Advancement.WARNING, revision=p.revision + 1)
                await self.unit.set_owner(p, replace(context, position=new))
                await _execute(
                    self.conn,
                    "UPDATE nr_round_pacing SET stage='warning_queued',owner_revision=? WHERE session_id=? AND round_id=?",
                    (new.revision, p.route.session_id, p.round_id),
                )
                await self._output(p.route.session_id, p.round_id, "warning")
        elif (
            row["stage"] == "warning_published"
            and utc_int(frame.received_at) >= row["selection_due_at_utc"]
        ):
            await self._snapshot(row, context)

    async def _cancel(self, frame, row, context):
        p = context.position
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET stage='cancelled' WHERE session_id=? AND round_id=?",
            (p.route.session_id, row["round_id"]),
        )
        await _execute(
            self.conn,
            "UPDATE nr_pacing_outputs SET retired_at_utc=? WHERE session_id=? AND round_id=? AND acknowledged_at_utc IS NULL AND retired_at_utc IS NULL",
            (utc_int(frame.received_at), p.route.session_id, row["round_id"]),
        )
        if p.lifecycle is Lifecycle.PLAYING:
            await self.unit.set_owner(
                p,
                replace(
                    context,
                    position=replace(
                        p,
                        lifecycle=Lifecycle.PAUSED,
                        pause_reason=PauseReason.RECOVERY,
                        revision=p.revision + 1,
                    ),
                ),
            )
            await self._output(p.route.session_id, row["round_id"], "recovery")

    async def _snapshot(self, row, context):
        p = context.position
        summary = WorstResults()
        async with self.conn.execute(
            "SELECT * FROM nr_round_scores WHERE session_id=? AND round_id=? AND ordinary_posts>=5 ORDER BY actor_id",
            (p.route.session_id, p.round_id),
        ) as cursor:
            while batch := await cursor.fetchmany(100):
                values = []
                for candidate in batch:
                    score = decode_score(candidate)
                    summary.observe(score)
                    actual = score.actual_percent
                    values.append(
                        (
                            p.route.session_id,
                            p.round_id,
                            candidate["actor_id"],
                            score.ordinary_posts,
                            str(actual.numerator) if actual is not None else None,
                            str(actual.denominator) if actual is not None else None,
                            score.raw_faults,
                        )
                    )
                await self.conn.executemany(
                    "INSERT INTO nr_picker_candidates(session_id,round_id,actor_id,ordinary_posts,actual_n,actual_d,raw_faults) VALUES (?,?,?,?,?,?,?)",
                    values,
                )
        if not summary.eligible:
            new = replace(
                p,
                lifecycle=Lifecycle.PAUSED,
                pause_reason=PauseReason.REVIVAL,
                advancement=Advancement.IDLE,
                revision=p.revision + 1,
            )
            await self.unit.set_owner(p, replace(context, position=new))
            await _execute(
                self.conn,
                "UPDATE nr_round_pacing SET stage='revival',owner_revision=? WHERE session_id=? AND round_id=?",
                (new.revision, p.route.session_id, p.round_id),
            )
            await self._output(p.route.session_id, p.round_id, "revival")
            return
        if summary.exclude_worst:
            await _execute(
                self.conn,
                "UPDATE nr_picker_candidates SET excluded=1 WHERE session_id=? AND round_id=? AND actual_n=? AND actual_d=?",
                (
                    p.route.session_id,
                    p.round_id,
                    str(summary.worst.numerator),
                    str(summary.worst.denominator),
                ),
            )
        await _execute(
            self.conn,
            """INSERT INTO nr_rotation_members(session_id,actor_id)
            SELECT session_id,actor_id FROM nr_picker_candidates WHERE session_id=? AND round_id=?
            ON CONFLICT(session_id,actor_id) DO NOTHING""",
            (p.route.session_id, p.round_id),
        )
        rotation = await _row(
            self.conn, "SELECT * FROM nr_rotation WHERE session_id=?", (p.route.session_id,)
        )
        count = await _row(
            self.conn,
            "SELECT COUNT(*) FROM nr_picker_candidates WHERE session_id=? AND round_id=? AND excluded=0",
            (p.route.session_id, p.round_id),
        )
        where = "c.session_id=? AND c.round_id=? AND c.excluded=0"
        values = (p.route.session_id, p.round_id)
        if count[0] > 1 and rotation["previous_picker"] is not None:
            where += " AND c.actor_id<>?"
            values += (rotation["previous_picker"],)
        cycle = rotation["cycle"]
        query = f"FROM nr_picker_candidates AS c JOIN nr_rotation_members AS m USING(session_id,actor_id) WHERE {where} AND m.served_cycle<?"
        unserved = await _row(self.conn, "SELECT COUNT(*) " + query, (*values, cycle))
        if not unserved[0]:
            cycle += 1
        rng = Random(row["random_seed"])
        picker, seen = None, 0
        async with self.conn.execute(
            "SELECT c.actor_id " + query + " ORDER BY c.actor_id", (*values, cycle)
        ) as cursor:
            while batch := await cursor.fetchmany(100):
                for candidate in batch:
                    seen += 1
                    if rng.randrange(seen) == 0:
                        picker = candidate[0]
        if picker is None:
            raise IncompatibleState("Rotation has no provable surviving candidate.")
        new = replace(p, advancement=Advancement.CHOOSING, revision=p.revision + 1)
        await self.unit.set_owner(p, replace(context, position=new))
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET stage='selected',picker_id=?,picker_cycle=?,owner_revision=? WHERE session_id=? AND round_id=?",
            (picker, cycle, new.revision, p.route.session_id, p.round_id),
        )

    async def _activated(self, session, old_round, picker, effective_at):
        row = await self._record(session, old_round)
        if row is None:
            return
        if row["stage"] != "selected" or row["picker_id"] != picker:
            raise IncompatibleState("Activation lacks its frozen picker snapshot.")
        await _execute(
            self.conn,
            "UPDATE nr_rotation SET cycle=?,previous_picker=? WHERE session_id=?",
            (row["picker_cycle"], picker, session),
        )
        await _execute(
            self.conn,
            "UPDATE nr_rotation_members SET served_cycle=? WHERE session_id=? AND actor_id=?",
            (row["picker_cycle"], session, picker),
        )
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET stage='completed' WHERE session_id=? AND round_id=?",
            (session, old_round),
        )
        context = await self.unit.get_context(session)
        estimate = await _row(
            self.conn,
            "SELECT COUNT(*) FROM nr_round_scores WHERE session_id=? AND round_id=? AND ordinary_posts>=5",
            (session, old_round),
        )
        seed = (
            int.from_bytes(
                sha256(f"{row['random_seed']}:{context.position.round_id}".encode()).digest()[:8],
                "big",
            )
            >> 1
        )
        await self._insert(context, effective_at, estimate[0], message_target(estimate[0]), seed)
