"""Short, serialized SQLite units of work with database-backed event guards.

No Discord I/O or raw event input belongs here. Ownership mutation is a storage
primitive, not an implemented activation, round-award, or admission service.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from functools import wraps
from pathlib import Path

import aiosqlite

from lets_play.new_rules.contracts import EventKind, Lifecycle, Outcome, Phase, SessionPosition
from lets_play.new_rules.evaluation import (
    EVALUATOR_VERSION,
    FAILURE_REASONS,
    EvaluationContext,
    MessageDecision,
    ReactionDecision,
)
from lets_play.new_rules.pacing import PACING_VERSION
from lets_play.new_rules.parsing import PARSER_VERSION
from lets_play.new_rules.scoring import (
    SCORE_VERSION,
    ScoreTotals,
    ordinary_score,
    reaction_score,
    whisper_score,
)
from lets_play.storage.bootstrap import initialize_database
from lets_play.storage.game_codec import (
    SCORE_COLUMNS,
    decode_context,
    decode_score,
    encode_context,
    encode_score,
)


class StoreOverloaded(RuntimeError):
    """Bounded admission failed; the caller must surface/retry with backpressure."""


class TransactionAborted(RuntimeError):
    """A caught write error still makes the whole unit rollback-only."""


def _mutating(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        self._check(write=True)
        try:
            return await method(self, *args, **kwargs)
        except BaseException:
            self._rollback_only = True
            raise

    return guarded


def _unit_mutating(method):
    """Rollback-only writes for application facades over an existing unit."""

    @wraps(method)
    async def guarded(self, *args, **kwargs):
        self.unit._check(write=True)
        try:
            return await method(self, *args, **kwargs)
        except BaseException:
            self.unit._rollback_only = True
            raise

    return guarded


class ApplyStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE_EVENT = "duplicate_event"
    DUPLICATE_REACTION = "duplicate_reaction"
    STALE_OWNER = "stale_owner"
    UNKNOWN_SESSION = "unknown_session"


@dataclass(frozen=True, slots=True)
class TrafficTotals:
    human_messages: int
    whispers: int


@dataclass(frozen=True, slots=True)
class MarkerIntent:
    intent_id: int
    session_id: str
    channel_id: int
    source_id: str
    actor_id: int


async def _finish[T](work: Awaitable[T]) -> T:
    """Finish commit/rollback/close before releasing a shared connection.

    Repeated caller cancellation cannot leave cleanup queued behind a new unit.
    A cancelled commit can still have succeeded: retry its guarded source key.
    """
    task = asyncio.ensure_future(work)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _execute(conn: aiosqlite.Connection, sql: str, values: tuple = ()) -> int:
    async with conn.execute(sql, values) as cursor:
        return cursor.rowcount


async def _row(conn: aiosqlite.Connection, sql: str, values: tuple = ()) -> sqlite3.Row | None:
    async with conn.execute(sql, values) as cursor:
        return await cursor.fetchone()


@asynccontextmanager
async def open_game_store(
    path: Path, *, max_pending: int = 128, busy_timeout_ms: int = 1000
) -> AsyncIterator[GameStore]:
    if type(max_pending) is not int or not 1 <= max_pending <= 10_000:
        raise ValueError("Store capacity must be an integer in [1, 10000].")
    if type(busy_timeout_ms) is not int or not 1 <= busy_timeout_ms <= 10_000:
        raise ValueError("Busy timeout must be an integer in [1, 10000] milliseconds.")
    await initialize_database(path)
    # mode=rw prevents an accidentally missing/replaced path becoming a new file.
    resolved = await asyncio.to_thread(path.resolve)
    conn = await aiosqlite.connect(resolved.as_uri() + "?mode=rw", uri=True, isolation_level=None)
    try:
        conn.row_factory = aiosqlite.Row
        await _execute(conn, "PRAGMA foreign_keys=ON")
        await _execute(conn, "PRAGMA synchronous=FULL")
        await _execute(conn, f"PRAGMA busy_timeout={busy_timeout_ms}")
        row = await _row(conn, "PRAGMA journal_mode")
        if row[0] != "wal":
            raise sqlite3.DatabaseError("Gameplay requires a local WAL database.")
        store = GameStore(conn, max_pending)
        try:
            yield store
        finally:
            await store.close()
    finally:
        await _finish(conn.close())


class GameStore:
    """One connection/lock per service instance, bounded waiting, no object cache."""

    def __init__(self, conn: aiosqlite.Connection, max_pending: int):
        self._conn = conn
        self._lock = asyncio.Lock()
        self._max_pending = max_pending
        self._pending = 0
        self._closed = False
        self._failed = False
        self._owner_task = None

    async def close(self) -> None:
        if self._owner_task is asyncio.current_task():
            raise RuntimeError("Close the store outside its active unit of work.")
        self._closed = True

        async def drain():
            async with self._lock:
                pass

        await _finish(drain())

    @asynccontextmanager
    async def transaction(self, *, write: bool = True) -> AsyncIterator[GameUnitOfWork]:
        if self._owner_task is asyncio.current_task():
            raise RuntimeError(
                "Nested units on one store are not supported; reuse the active unit."
            )
        if self._closed or self._failed:
            raise RuntimeError("The game store is closed or unusable.")
        if self._pending >= self._max_pending:
            raise StoreOverloaded("Game-store capacity reached; apply explicit backpressure.")
        self._pending += 1
        try:
            async with self._lock:
                if self._closed or self._failed:
                    raise RuntimeError("The game store is closed or unusable.")
                unit = GameUnitOfWork(self._conn, write)
                self._owner_task = asyncio.current_task()
                try:
                    # BEGIN belongs inside try: cancellation may arrive after SQL
                    # was queued but before its result is delivered.
                    await _execute(self._conn, "BEGIN IMMEDIATE" if write else "BEGIN")
                    yield unit
                    if unit._rollback_only:
                        raise TransactionAborted("A failed write makes this unit rollback-only.")
                    await _finish(self._conn.commit())
                except BaseException:
                    try:
                        await _finish(self._conn.rollback())
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        self._failed = True
                        raise
                    raise
                finally:
                    unit._active = False
                    self._owner_task = None
        finally:
            self._pending -= 1

    async def apply(self, decision: MessageDecision | ReactionDecision) -> ApplyStatus:
        async with self.transaction() as unit:
            return await unit.apply(decision)


class GameUnitOfWork:
    def __init__(self, conn: aiosqlite.Connection, write: bool):
        self._conn = conn
        self._write = write
        self._active = True
        self._rollback_only = False
        self._ordered_flow = False
        self._opening_activation = False
        self._task = asyncio.current_task()

    def _check(self, *, write: bool = False) -> None:
        if not self._active or self._task is not asyncio.current_task():
            raise RuntimeError("A unit of work belongs only to its active owner task.")
        if write and not self._write:
            raise RuntimeError("This unit of work is read-only.")

    async def get_context(self, session_id: str) -> EvaluationContext | None:
        self._check()
        row = await _row(self._conn, "SELECT * FROM nr_sessions WHERE session_id=?", (session_id,))
        return decode_context(row) if row is not None else None

    @_mutating
    async def insert_session(self, context: EvaluationContext) -> None:
        """Internal persistence seed; not lobby/quorum/guild-capacity admission."""
        self._check(write=True)
        p = context.position
        if p.lifecycle is not Lifecycle.PLAYING or not p.round_id or p.revision != 0:
            raise ValueError("A new stored evaluation owner must be playing at revision zero.")
        reservation = await _row(
            self._conn,
            "SELECT session_id FROM nr_admissions WHERE channel_id=? AND stage<>'ended'",
            (p.route.channel_id,),
        )
        if reservation is not None and (
            reservation[0] != p.route.session_id or not self._opening_activation
        ):
            raise sqlite3.IntegrityError("The exact channel is already reserved.")
        await _execute(
            self._conn,
            """INSERT INTO nr_sessions (
                session_id,guild_id,channel_id,lifecycle,phase,advancement,round_id,
                ruleset_version,revision,offer_id,transition_id,pause_reason,
                context_json,parser_version,evaluator_version,score_version,pacing_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                p.route.session_id,
                p.route.guild_id,
                p.route.channel_id,
                p.lifecycle,
                p.phase,
                p.advancement,
                p.round_id,
                p.ruleset_version,
                p.revision,
                p.offer_id,
                p.transition_id,
                p.pause_reason,
                encode_context(context),
                PARSER_VERSION,
                EVALUATOR_VERSION,
                SCORE_VERSION,
                PACING_VERSION,
            ),
        )
        await _execute(
            self._conn,
            "INSERT INTO nr_rounds(session_id,round_id) VALUES (?,?)",
            (p.route.session_id, p.round_id),
        )
        if reservation is None:
            # Internal fixture/repository seed, not entitlement-aware admission.
            await _execute(
                self._conn,
                """INSERT INTO nr_admissions(session_id,guild_id,channel_id,stage,playing_session_id)
                VALUES (?,?,?,'legacy',?)""",
                (p.route.session_id, p.route.guild_id, p.route.channel_id, p.route.session_id),
            )

    @_mutating
    async def set_owner(self, expected: SessionPosition, replacement: EvaluationContext) -> bool:
        """Compare-and-set primitive; future activation/awards must share this unit."""
        self._check(write=True)
        current = await self.get_context(expected.route.session_id)
        if current is None or current.position != expected or expected.lifecycle is Lifecycle.ENDED:
            return False
        new = replacement.position
        if new.route != expected.route or new.revision != expected.revision + 1 or not new.round_id:
            raise ValueError("Ownership requires the same route and the next revision.")
        version_changed = new.ruleset_version == expected.ruleset_version + 1
        if new.ruleset_version != expected.ruleset_version and not version_changed:
            raise ValueError("Ruleset versions must stay fixed or advance by one.")
        if replacement.active_instances != current.active_instances and not version_changed:
            raise ValueError("A rule change requires a new ruleset version.")
        if new.round_id != expected.round_id:
            if not version_changed or new.lifecycle is not Lifecycle.PLAYING:
                raise ValueError("A new round owner requires a new playing ruleset.")
            # Existing rounds cannot be reopened via a stale owner or ABA update.
            await _execute(
                self._conn,
                """INSERT INTO nr_rounds(session_id,round_id,round_number)
                SELECT ?,?,MAX(round_number)+1 FROM nr_rounds WHERE session_id=?""",
                (new.route.session_id, new.round_id, new.route.session_id),
            )
        await _execute(
            self._conn,
            """UPDATE nr_sessions SET lifecycle=?,phase=?,advancement=?,round_id=?,
                ruleset_version=?,revision=?,offer_id=?,transition_id=?,pause_reason=?,context_json=?
                WHERE session_id=? AND revision=?""",
            (
                new.lifecycle,
                new.phase,
                new.advancement,
                new.round_id,
                new.ruleset_version,
                new.revision,
                new.offer_id,
                new.transition_id,
                new.pause_reason,
                encode_context(replacement),
                new.route.session_id,
                expected.revision,
            ),
        )
        if new.lifecycle is Lifecycle.ENDED:
            await _execute(
                self._conn,
                "UPDATE nr_admissions SET stage='ended' WHERE playing_session_id=?",
                (new.route.session_id,),
            )
        return True

    async def get_score(
        self, session_id: str, actor_id: int, *, round_id: str | None = None
    ) -> ScoreTotals | None:
        self._check()
        if round_id is None:
            row = await _row(
                self._conn,
                "SELECT * FROM nr_participants WHERE session_id=? AND actor_id=?",
                (session_id, actor_id),
            )
        else:
            row = await _row(
                self._conn,
                "SELECT * FROM nr_round_scores WHERE session_id=? AND round_id=? AND actor_id=?",
                (session_id, round_id, actor_id),
            )
        return decode_score(row) if row is not None else None

    async def get_traffic(
        self, session_id: str, *, round_id: str | None = None
    ) -> TrafficTotals | None:
        self._check()
        if round_id is None:
            row = await _row(
                self._conn,
                "SELECT human_messages,whispers FROM nr_sessions WHERE session_id=?",
                (session_id,),
            )
        else:
            row = await _row(
                self._conn,
                "SELECT human_messages,whispers FROM nr_rounds WHERE session_id=? AND round_id=?",
                (session_id, round_id),
            )
        return TrafficTotals(*row) if row is not None else None

    async def _add_score(
        self, session_id: str, round_id: str, actor_id: int, delta: ScoreTotals
    ) -> None:
        for table, keys in (
            ("nr_participants", {"session_id": session_id, "actor_id": actor_id}),
            (
                "nr_round_scores",
                {"session_id": session_id, "round_id": round_id, "actor_id": actor_id},
            ),
        ):
            where = " AND ".join(f"{key}=?" for key in keys)
            row = await _row(
                self._conn, f"SELECT * FROM {table} WHERE {where}", tuple(keys.values())
            )
            total = (decode_score(row) if row is not None else ScoreTotals()) + delta
            columns = (*keys, *SCORE_COLUMNS)
            update = ",".join(f"{column}=excluded.{column}" for column in SCORE_COLUMNS)
            await _execute(
                self._conn,
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)}) "
                f"ON CONFLICT ({','.join(keys)}) DO UPDATE SET {update}",
                (*keys.values(), *encode_score(total)),
            )
        await _execute(
            self._conn,
            """INSERT INTO nr_player_progress(session_id,actor_id,first_round_number)
            SELECT session_id,?,round_number FROM nr_rounds WHERE session_id=? AND round_id=?
            ON CONFLICT(session_id,actor_id) DO NOTHING""",
            (actor_id, session_id, round_id),
        )

    @_mutating
    async def apply(self, decision: MessageDecision | ReactionDecision) -> ApplyStatus:
        self._check(write=True)
        if not isinstance(decision, (MessageDecision, ReactionDecision)):
            raise TypeError("Only derived representative decisions may enter the store.")
        if not self._ordered_flow and await _row(
            self._conn,
            "SELECT 1 FROM nr_flow_watermarks WHERE session_id=?",
            (decision.position.route.session_id,),
        ):
            raise RuntimeError("This session requires ordered ordinary-flow ingress.")
        p = decision.position
        session_id, kind, source_id = decision.source_key
        is_message = isinstance(decision, MessageDecision)
        required_kind = EventKind.MESSAGE_CREATE if is_message else EventKind.REACTION_ADD
        if session_id != p.route.session_id or kind is not required_kind or not source_id:
            raise ValueError("The decision source key must match its owner and event kind.")
        current = await self.get_context(session_id)
        if current is None:
            return ApplyStatus.UNKNOWN_SESSION
        if await _row(
            self._conn,
            "SELECT 1 FROM nr_preplay_sources WHERE session_id=? AND event_kind=? AND source_id=?",
            decision.source_key,
        ):
            return ApplyStatus.DUPLICATE_EVENT
        if await _row(
            self._conn,
            "SELECT 1 FROM nr_paused_sources WHERE session_id=? AND event_kind=? AND source_id=?",
            decision.source_key,
        ):
            return ApplyStatus.DUPLICATE_EVENT
        prior = await _row(
            self._conn,
            "SELECT 1 FROM nr_processed_events WHERE session_id=? AND event_kind=? AND source_id=?",
            decision.source_key,
        )
        if prior is not None:
            return ApplyStatus.DUPLICATE_EVENT
        if current.position != p or p.lifecycle is not Lifecycle.PLAYING:
            return ApplyStatus.STALE_OWNER
        self._validate_decision(decision, current)
        if not is_message:
            prior = await _row(
                self._conn,
                "SELECT 1 FROM nr_reaction_offenses WHERE session_id=? AND actor_id=? AND target_message_id=?",
                decision.dedup_key,
            )
            if prior is not None:
                return ApplyStatus.DUPLICATE_REACTION
        # The writer lock covers guard checks and every effect. A failure at any
        # later statement rolls all of them back; no SQL OR IGNORE masks errors.
        await _execute(
            self._conn,
            "INSERT INTO nr_processed_events VALUES (?,?,?,?,?,?)",
            (*decision.source_key, p.round_id, p.ruleset_version, p.revision),
        )
        if decision.enroll:
            await self._add_score(session_id, p.round_id, decision.actor_id, decision.score)
        if is_message:
            for table, where, keys in (
                ("nr_sessions", "session_id=?", (session_id,)),
                ("nr_rounds", "session_id=? AND round_id=?", (session_id, p.round_id)),
            ):
                await _execute(
                    self._conn,
                    f"UPDATE {table} SET human_messages=human_messages+?,whispers=whispers+? WHERE {where}",
                    (decision.score.human_messages, decision.score.whispers, *keys),
                )
            if decision.needs_marker:
                await _execute(
                    self._conn,
                    "INSERT INTO nr_marker_intents(session_id,source_id,actor_id) VALUES (?,?,?)",
                    (session_id, source_id, decision.actor_id),
                )
            await _execute(
                self._conn,
                "UPDATE nr_rounds SET ordinary_posts=ordinary_posts+? WHERE session_id=? AND round_id=?",
                (decision.score.ordinary_posts, session_id, p.round_id),
            )
        else:
            await _execute(
                self._conn,
                "INSERT INTO nr_reaction_offenses VALUES (?,?,?,?)",
                (*decision.dedup_key, p.round_id),
            )
        results = decision.evaluations if is_message else (decision.evaluation,)
        for result in results:
            if result.outcome is Outcome.FAIL:
                await _execute(
                    self._conn,
                    """INSERT INTO nr_fault_counts VALUES (?,?,?,?,?,?,1)
                        ON CONFLICT(session_id,round_id,actor_id,ruleset_version,
                            rule_instance_id,reason_code)
                        DO UPDATE SET faults=faults+1""",
                    (
                        session_id,
                        p.round_id,
                        decision.actor_id,
                        p.ruleset_version,
                        result.rule_instance_id,
                        result.reason_code,
                    ),
                )
        # Local import keeps the repository primitive independent of its facade.
        # Score, credit flag, global increment and threshold badge share this unit.
        if decision.enroll:
            from lets_play.storage.progression import Progression

            await Progression(self).credit_ordinary(session_id, decision.actor_id)
        return ApplyStatus.APPLIED

    @staticmethod
    def _validate_decision(
        decision: MessageDecision | ReactionDecision, context: EvaluationContext
    ) -> None:
        if type(decision.actor_id) is not int or decision.actor_id <= 0:
            raise ValueError("A positive actor ID is required.")
        if isinstance(decision, MessageDecision):
            if decision.score.whispers:
                if decision.score != whisper_score() or decision.evaluations:
                    raise ValueError("A whisper may only add traffic.")
                return
            if context.position.phase is not Phase.ORDINARY:
                raise ValueError("Ordinary evaluation cannot be persisted as a mode result.")
            ids = tuple(item.instance_id for item in context.active_instances)
            if tuple(item.rule_instance_id for item in decision.evaluations) != ids:
                raise ValueError("Message results must cover the frozen active instances in order.")
            if decision.score != ordinary_score(decision.evaluations):
                raise ValueError("The decision score must agree with its derived outcomes.")
            for instance, result in zip(
                context.active_instances, decision.evaluations, strict=True
            ):
                if (
                    result.outcome is Outcome.FAIL
                    and result.reason_code != FAILURE_REASONS[instance.card_id]
                ) or (instance.card_id == "NR006" and result.outcome is not Outcome.NOT_APPLICABLE):
                    raise ValueError("Derived outcomes must use the implemented card reason codes.")
        else:
            expressionless = next(
                (i for i in context.active_instances if i.card_id == "NR006"), None
            )
            if (
                context.position.phase is not Phase.ORDINARY
                or expressionless is None
                or decision.evaluation.rule_instance_id != expressionless.instance_id
                or decision.evaluation.outcome is not Outcome.FAIL
                or decision.evaluation.reason_code != FAILURE_REASONS["NR006"]
                or decision.score != reaction_score()
                or decision.dedup_key[:2] != (context.position.route.session_id, decision.actor_id)
                or type(decision.dedup_key[2]) is not int
                or decision.dedup_key[2] <= 0
            ):
                raise ValueError("The reaction decision must describe one prohibited actor/target.")

    async def pending_markers(self, *, limit: int = 100) -> tuple[MarkerIntent, ...]:
        self._check()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Marker page limit must be an integer in [1, 100].")
        async with self._conn.execute(
            """SELECT i.intent_id,i.session_id,s.channel_id,i.source_id,i.actor_id
                FROM nr_marker_intents i JOIN nr_sessions s USING(session_id)
                ORDER BY i.intent_id LIMIT ?""",
            (limit,),
        ) as cursor:
            return tuple(MarkerIntent(*row) for row in await cursor.fetchall())

    @_mutating
    async def acknowledge_marker(self, intent_id: int) -> None:
        self._check(write=True)
        if type(intent_id) is not int or intent_id <= 0:
            raise ValueError("A positive marker intent ID is required.")
        await _execute(self._conn, "DELETE FROM nr_marker_intents WHERE intent_id=?", (intent_id,))
