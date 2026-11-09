"""Explicit disruption barrier and evidence-based ordinary/opening recovery.

A dispatcher stops intake, enumerates live routes, and enters each barrier BEFORE
running overdue timers. Nothing here sends, scans Discord, or backfills chat.
"""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.contracts import (
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    Phase,
    ReactionEvent,
)
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import decode_hand, encode_hand, utc_datetime, utc_int
from lets_play.storage.opening_codec import decode_config
from lets_play.storage.ordinary_flow import FlowOrderError, OrdinaryFlow

RECOVERY_VERSION = "startup-recovery-1"
RECOVERY_READ_IN_SECONDS = 30
_ACTIVE = "('paused','ready','reading')"
_TABLES = {
    "opening": "nr_opening_outputs",
    "ordinary": "nr_ordinary_outputs",
    "pacing": "nr_pacing_outputs",
    "evolution": "nr_evolution_outputs",
}


class RecoveryRequired(RuntimeError):
    """Route this receipt through RecoveryFlow; normal dispatch is fenced."""


async def active_recovery(unit, session):
    return await _row(
        unit._conn,
        f"SELECT * FROM nr_recoveries WHERE session_id=? AND stage IN {_ACTIVE}",
        (session,),
    )


async def require_dispatchable(unit, session):
    if await active_recovery(unit, session):
        raise RecoveryRequired("The session requires explicit recovery dispatch.")


def _page(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("A page contains 1 through 100 entries.")


def _id(value):
    if type(value) is not int or not 1 <= value < (1 << 63):
        raise ValueError("A positive known SQLite message ID is required.")


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    recovery_id: str
    stage: str
    original_stage: str
    original_lifecycle: str | None
    original_pause: str | None
    plan: str | None
    notice_message_id: int | None
    resume_at: datetime | None


@dataclass(frozen=True, slots=True)
class RecoveryOutput:
    session_id: str
    recovery_id: str
    plan: str


@dataclass(frozen=True, slots=True)
class RecoveryWork:
    family: str
    output_id: int
    kind: str
    status: str
    message_id: int | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class LiveRoute:
    session_id: str
    guild_id: int
    channel_id: int
    last_sequence: int
    observed_at: datetime | None


class RecoveryFlow:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def live_routes(self, *, after_session="", limit=100):
        """Discovery only; one stopped dispatcher still owns sequence allocation."""
        self.unit._check()
        _page(limit)
        if not isinstance(after_session, str):
            raise ValueError("A session cursor must be text.")
        async with self.conn.execute(
            """SELECT a.session_id,a.guild_id,a.channel_id,
            COALESCE(w.last_sequence,a.last_sequence),COALESCE(w.observed_at_utc,a.observed_at_utc)
            FROM nr_admissions a LEFT JOIN nr_flow_watermarks w USING(session_id)
            WHERE a.stage<>'ended' AND a.session_id>? ORDER BY a.session_id LIMIT ?""",
            (after_session, limit),
        ) as cursor:
            return tuple(
                LiveRoute(*r[:4], utc_datetime(r[4]) if r[4] is not None else None)
                for r in await cursor.fetchall()
            )

    async def _record(self, session, recovery):
        row = await _row(
            self.conn,
            "SELECT * FROM nr_recoveries WHERE session_id=? AND recovery_id=?",
            (session, recovery),
        )
        if row is not None and row["version"] != RECOVERY_VERSION:
            raise IncompatibleState("Startup recovery version needs explicit reconciliation.")
        return row

    async def record(self, session, recovery):
        self.unit._check()
        r = await self._record(session, recovery)
        return (
            RecoveryRecord(
                r["recovery_id"],
                r["stage"],
                r["original_stage"],
                r["original_lifecycle"],
                r["original_pause"],
                r["plan"],
                r["notice_message_id"],
                utc_datetime(r["resume_at_utc"]) if r["resume_at_utc"] is not None else None,
            )
            if r
            else None
        )

    async def work(self, session, recovery, *, limit=100):
        self.unit._check()
        _page(limit)
        async with self.conn.execute(
            "SELECT family,output_id,kind,status,message_id,published_at_utc FROM nr_recovery_work WHERE session_id=? AND recovery_id=? ORDER BY family,output_id LIMIT ?",
            (session, recovery, limit),
        ) as cursor:
            return tuple(
                RecoveryWork(*r[:5], utc_datetime(r[5]) if r[5] is not None else None)
                for r in await cursor.fetchall()
            )

    async def pending_outputs(self, *, limit=100):
        self.unit._check()
        _page(limit)
        async with self.conn.execute(
            "SELECT r.session_id,r.recovery_id,r.plan FROM nr_recoveries r JOIN nr_admissions a USING(session_id) WHERE r.stage='ready' AND a.stage<>'ended' ORDER BY r.session_id LIMIT ?",
            (limit,),
        ) as cursor:
            return tuple(RecoveryOutput(*r) for r in await cursor.fetchall())

    async def _order(self, frame, kind):
        if frame.kind is not kind or frame.sequence < 1:
            raise FlowOrderError("An ordered recovery receipt of the correct kind is required.")
        admission = await _row(
            self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (frame.route.session_id,)
        )
        if admission is None or (admission["guild_id"], admission["channel_id"]) != (
            frame.route.guild_id,
            frame.route.channel_id,
        ):
            raise FlowOrderError("Recovery requires an exact existing route.")
        mark = await _row(
            self.conn,
            "SELECT * FROM nr_flow_watermarks WHERE session_id=?",
            (frame.route.session_id,),
        )
        last = mark["last_sequence"] if mark else admission["last_sequence"]
        observed = mark["observed_at_utc"] if mark else admission["observed_at_utc"]
        if frame.sequence <= last:
            return False
        if frame.sequence != last + 1 or (
            observed is not None and utc_int(frame.received_at) < observed
        ):
            raise FlowOrderError("Recovery cannot skip queued receipts or regress server time.")
        if admission["playing_session_id"] is not None:
            await _execute(
                self.conn,
                "INSERT INTO nr_flow_watermarks VALUES (?,?,?) ON CONFLICT(session_id) DO UPDATE SET last_sequence=excluded.last_sequence,observed_at_utc=excluded.observed_at_utc",
                (frame.route.session_id, frame.sequence, utc_int(frame.received_at)),
            )
        else:
            await _execute(
                self.conn,
                "UPDATE nr_admissions SET last_sequence=?,observed_at_utc=? WHERE session_id=?",
                (frame.sequence, utc_int(frame.received_at), frame.route.session_id),
            )
        return True

    async def _enter(self, frame, kind, recovery):
        if not await self._order(frame, kind):
            return None, "replay"
        row = await self._record(frame.route.session_id, recovery)
        if row is None or row["stage"] not in ("paused", "ready", "reading"):
            return row, "stale"
        await self._settle_terminal(frame, row)
        row = await self._record(frame.route.session_id, recovery)
        if row["stage"] == "closed":
            return row, "closed"
        return row, None

    @_write
    async def begin(self, frame, recovery_id):
        """Call before due timers; a new genuine disruption uses a new stable ID."""
        if not isinstance(recovery_id, str) or not recovery_id:
            raise ValueError("A stable recovery identity is required.")
        if not await self._order(frame, EventKind.RECOVERY_BEGIN):
            return "replay"
        existing = await self._record(frame.route.session_id, recovery_id)
        if existing:
            return "already_done"
        session = frame.route.session_id
        admission = await _row(
            self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (session,)
        )
        if admission["stage"] == "ended":
            return "ended"
        previous = await active_recovery(self.unit, session)
        work = []
        if previous is None:
            # Freeze only this session's unacknowledged outputs. A backlog must
            # be reconciled explicitly rather than silently truncated.
            for family in _TABLES:
                table = _TABLES[family]
                async with self.conn.execute(
                    f"SELECT output_id FROM {table} WHERE session_id=? AND acknowledged_at_utc IS NULL AND retired_at_utc IS NULL ORDER BY output_id LIMIT 101",
                    (session,),
                ) as cursor:
                    candidates = await cursor.fetchall()
                if len(candidates) > 100:
                    raise IncompatibleState(
                        "Recovery output backlog needs bounded operator reconciliation."
                    )
                # No absent ACK is treated as evidence that delivery failed.
                for c in candidates:
                    o = await _row(self.conn, f"SELECT * FROM {table} WHERE output_id=?", (c[0],))
                    work.append((family, c[0], o["kind"], "unknown", None, None))
        else:
            await self._record(session, previous["recovery_id"])
            async with self.conn.execute(
                "SELECT family,output_id,kind,status,message_id,published_at_utc FROM nr_recovery_work WHERE session_id=? AND recovery_id=?",
                (session, previous["recovery_id"]),
            ) as cursor:
                work = [tuple(r) for r in await cursor.fetchall()]
            await _execute(
                self.conn,
                "UPDATE nr_recoveries SET stage='superseded' WHERE session_id=? AND recovery_id=?",
                (session, previous["recovery_id"]),
            )
        if len(work) > 100:
            raise IncompatibleState("Recovery work exceeds the bounded reconciliation page.")
        context = await self.unit.get_context(session)
        if context is None:
            lifecycle = pause = round_id = None
            revision = admission["revision"]
        else:
            p = context.position
            lifecycle = p.lifecycle.value
            pause = p.pause_reason.value if p.pause_reason else None
            round_id = p.round_id
            # A physical pause fences scoring even for code that reads context.
            new = replace(
                p,
                lifecycle=Lifecycle.PAUSED,
                pause_reason=PauseReason.RECOVERY,
                revision=p.revision + 1,
            )
            await self.unit.set_owner(p, replace(context, position=new))
            revision = new.revision
        if previous:
            lifecycle, pause, round_id = (
                previous["original_lifecycle"],
                previous["original_pause"],
                previous["original_round"],
            )
        await _execute(
            self.conn,
            """INSERT INTO nr_recoveries(session_id,recovery_id,stage,original_stage,original_lifecycle,original_pause,original_round,owner_revision,began_at_utc)
            VALUES (?,?,'paused',?,?,?,?,?,?)""",
            (
                session,
                recovery_id,
                previous["original_stage"] if previous else admission["stage"],
                lifecycle,
                pause,
                round_id,
                revision,
                utc_int(frame.received_at),
            ),
        )
        for family, output_id, kind, status, message_id, published in work:
            await _execute(
                self.conn,
                "INSERT INTO nr_recovery_work VALUES (?,?,?,?,?,?,?,?)",
                (session, recovery_id, family, output_id, kind, status, message_id, published),
            )
        row = await self._record(session, recovery_id)
        await self._settle_terminal(frame, row)
        return (
            "closed"
            if (await self._record(session, recovery_id))["stage"] == "closed"
            else "paused"
        )

    async def _settle_terminal(self, frame, recovery):
        session = frame.route.session_id
        admission = await _row(
            self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (session,)
        )
        if admission["stage"] == "ended":
            await _execute(
                self.conn,
                "UPDATE nr_recoveries SET stage='closed' WHERE session_id=? AND recovery_id=?",
                (session, recovery["recovery_id"]),
            )
            return
        if admission["playing_session_id"] is None:
            if (
                admission["stage"] == "lobby_published"
                and utc_int(frame.received_at) >= admission["lobby_deadline_utc"]
            ):
                from lets_play.storage.opening_flow import OpeningFlow

                await OpeningFlow(self.unit)._end(admission, frame, "expired")
                await _execute(
                    self.conn,
                    "UPDATE nr_recoveries SET stage='closed' WHERE session_id=? AND recovery_id=?",
                    (session, recovery["recovery_id"]),
                )
            return
        context = await self.unit.get_context(session)
        if recovery["original_pause"] != "revival":
            return
        revival = await _row(
            self.conn,
            "SELECT * FROM nr_revivals WHERE session_id=? AND stage IN ('prepared','published')",
            (session,),
        )
        if (
            revival is not None
            and revival["deadline_utc"] is not None
            and utc_int(frame.received_at) >= revival["deadline_utc"]
        ):
            if context.position.revision != recovery["owner_revision"]:
                raise IncompatibleState("Recovery owner changed outside its barrier.")
            p = context.position
            await self.unit.set_owner(
                p,
                replace(
                    context,
                    position=replace(
                        p, lifecycle=Lifecycle.ENDED, pause_reason=None, revision=p.revision + 1
                    ),
                ),
            )
            from lets_play.storage.evolution_flow import EvolutionFlow
            from lets_play.storage.progression import Progression

            await Progression(self.unit).finalize_ended(session)
            await _execute(
                self.conn,
                "UPDATE nr_revivals SET stage='expired' WHERE session_id=? AND revival_id=?",
                (session, revival["revival_id"]),
            )
            evolution = EvolutionFlow(self.unit)
            for table in ("nr_pacing_outputs", "nr_evolution_outputs", "nr_ordinary_outputs"):
                await evolution._retire(frame, table)
            await _execute(
                self.conn, "UPDATE nr_evolution SET phase='ended' WHERE session_id=?", (session,)
            )
            await _execute(
                self.conn,
                "INSERT INTO nr_evolution_outputs(session_id,token,kind) VALUES (?,?,'closed_silent') ON CONFLICT DO NOTHING",
                (session, revival["revival_id"]),
            )
            await _execute(
                self.conn,
                "UPDATE nr_recoveries SET stage='closed' WHERE session_id=? AND recovery_id=?",
                (session, recovery["recovery_id"]),
            )

    @_write
    async def reconcile(
        self, frame, recovery_id, family, output_id, *, message_id=None, published_at=None
    ):
        """Trusted delivery proof, or a conclusive never-sent fact; never guess absence."""
        row, error = await self._enter(frame, EventKind.RECOVERY_RECONCILE, recovery_id)
        if error:
            return error
        if family not in _TABLES or type(output_id) is not int:
            raise ValueError("An exact durable output identity is required.")
        work = await _row(
            self.conn,
            "SELECT * FROM nr_recovery_work WHERE session_id=? AND recovery_id=? AND family=? AND output_id=?",
            (frame.route.session_id, recovery_id, family, output_id),
        )
        if work is None:
            return "stale"
        if (message_id is None) != (published_at is None):
            raise ValueError("Delivery requires both a message ID and actual publication time.")
        status = "unsent" if message_id is None else "found"
        published = utc_int(published_at) if published_at is not None else None
        if message_id is not None:
            _id(message_id)
            if published > utc_int(frame.received_at):
                raise ValueError("Publication cannot be in the future.")
        if work["status"] != "unknown":
            if (work["status"], work["message_id"], work["published_at_utc"]) != (
                status,
                message_id,
                published,
            ):
                raise ValueError("Reconciled delivery facts cannot change.")
            return "already_done"
        if row["stage"] != "paused":
            return "stale"
        if status == "found":
            await self._found(frame, work, message_id, published)
        await _execute(
            self.conn,
            "UPDATE nr_recovery_work SET status=?,message_id=?,published_at_utc=? WHERE session_id=? AND recovery_id=? AND family=? AND output_id=?",
            (status, message_id, published, frame.route.session_id, recovery_id, family, output_id),
        )
        await self._settle_terminal(frame, row)
        return status

    async def _found(self, frame, work, message, published):
        family, kind = work["family"], work["kind"]
        table = _TABLES[family]
        output = await _row(
            self.conn,
            f"SELECT * FROM {table} WHERE session_id=? AND output_id=?",
            (frame.route.session_id, work["output_id"]),
        )
        if output is None:
            raise IncompatibleState("Recovery evidence lost its source output.")
        session = frame.route.session_id
        if output["message_id"] is not None and output["message_id"] != message:
            raise ValueError("Delivery cannot change a known message identity.")
        if family == "opening":
            admission = await _row(
                self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (session,)
            )
            earliest = {
                "lobby": "prepared_at_utc",
                "offer": "offer_prepared_at_utc",
                "activation": "selected_at_utc",
            }.get(kind)
            if earliest and (admission[earliest] is None or published < admission[earliest]):
                raise ValueError("Publication precedes output preparation.")
            if kind == "lobby":
                await _execute(
                    self.conn,
                    "UPDATE nr_admissions SET stage='lobby_published',lobby_published_at_utc=?,lobby_deadline_utc=? WHERE session_id=?",
                    (published, published + 180_000_000, session),
                )
            elif kind == "offer":
                hand = decode_hand(admission["hand_json"])
                hand = replace(
                    hand,
                    deadline=replace(
                        hand.deadline, expires_at=utc_datetime(published) + timedelta(seconds=60)
                    ),
                )
                await _execute(
                    self.conn,
                    "UPDATE nr_admissions SET stage='opening_published',offer_published_at_utc=?,hand_json=? WHERE session_id=?",
                    (published, encode_hand(hand), session),
                )
            elif kind == "activation":
                await _execute(
                    self.conn,
                    "UPDATE nr_admissions SET announced_at_utc=? WHERE session_id=?",
                    (published, session),
                )
        elif family == "ordinary":
            record = await OrdinaryFlow(self.unit).get_offer(session, output["offer_id"])
            source = await _row(
                self.conn,
                "SELECT * FROM nr_offers WHERE session_id=? AND offer_id=?",
                (session, output["offer_id"]),
            )
            earliest = (
                source["selected_at_utc"] if kind == "activation" else source["prepared_at_utc"]
            )
            if published < earliest:
                raise ValueError("Publication precedes output preparation.")
            if kind == "offer":
                hand = record.hand
                if record.published_at is None:
                    hand = replace(
                        hand,
                        deadline=replace(
                            hand.deadline,
                            expires_at=utc_datetime(published) + timedelta(seconds=60),
                        ),
                    )
                await _execute(
                    self.conn,
                    "UPDATE nr_offers SET stage='published',hand_json=?,published_at_utc=COALESCE(published_at_utc,?) WHERE session_id=? AND offer_id=?",
                    (encode_hand(hand), published, session, hand.offer_id),
                )
            elif kind == "activation":
                await _execute(
                    self.conn,
                    "UPDATE nr_activations SET announced_at_utc=?,stage='announced' WHERE session_id=? AND offer_id=?",
                    (published, session, record.hand.offer_id),
                )
        elif family == "pacing":
            from lets_play.storage.pacing_flow import RoundPacing

            pacing = await RoundPacing(self.unit)._record(session, output["round_id"])
            if pacing["attempt"] != output["attempt"]:
                raise IncompatibleState("An old output cannot acknowledge another attempt.")
            if published < pacing["started_at_utc"]:
                raise ValueError("Publication precedes round activation.")
            if kind == "warning":
                from lets_play.new_rules.pacing import FALLBACK_WARNING_SECONDS, MIN_WARNING_SECONDS

                fallback = pacing["started_at_utc"] + FALLBACK_WARNING_SECONDS * 1_000_000
                earliest = max(
                    pacing["started_at_utc"] + MIN_WARNING_SECONDS * 1_000_000,
                    min(
                        pacing["target_reached_at_utc"]
                        if pacing["target_reached_at_utc"] is not None
                        else fallback,
                        fallback,
                    ),
                )
                prior = await _row(
                    self.conn,
                    "SELECT retired_at_utc FROM nr_pacing_history WHERE session_id=? AND round_id=? AND attempt=?",
                    (session, output["round_id"], output["attempt"] - 1),
                )
                if prior is not None:
                    earliest = max(earliest, prior[0])
                if published < earliest:
                    raise ValueError("Publication precedes this warning attempt's legal boundary.")
                await _execute(
                    self.conn,
                    "UPDATE nr_round_pacing SET stage='warning_published',warning_published_at_utc=?,selection_due_at_utc=? WHERE session_id=? AND round_id=?",
                    (published, published + 30_000_000, session, output["round_id"]),
                )
            elif kind == "revival":
                revival = await _row(
                    self.conn,
                    "SELECT * FROM nr_revivals WHERE session_id=? AND round_id=? AND attempt=?",
                    (session, output["round_id"], output["attempt"]),
                )
                if revival is None:
                    raise IncompatibleState(
                        "A legacy revival has no recoverable publication clock."
                    )
                if published < revival["prepared_at_utc"]:
                    raise ValueError("Publication precedes invitation preparation.")
                await _execute(
                    self.conn,
                    "UPDATE nr_revivals SET stage='published',published_at_utc=?,deadline_utc=? WHERE session_id=? AND revival_id=?",
                    (published, published + 300_000_000, session, revival["revival_id"]),
                )
        await _execute(
            self.conn,
            f"UPDATE {table} SET message_id=?,acknowledged_at_utc=? WHERE output_id=?",
            (message, published, work["output_id"]),
        )

    @_write
    async def approve(self, frame, recovery_id, *, capabilities, available_templates=None):
        from lets_play.storage.opening_flow import OpeningCapabilities

        row, error = await self._enter(frame, EventKind.RECOVERY_APPROVE, recovery_id)
        if error:
            return error
        if not isinstance(capabilities, OpeningCapabilities):
            raise TypeError("Fresh normalized essential capability facts are required.")
        if row["stage"] != "paused":
            return "already_done"
        if not capabilities.available:
            return "capability"
        unknown = await _row(
            self.conn,
            "SELECT 1 FROM nr_recovery_work WHERE session_id=? AND recovery_id=? AND status='unknown' LIMIT 1",
            (frame.route.session_id, recovery_id),
        )
        if unknown:
            return "unknown_delivery"
        context = await self.unit.get_context(frame.route.session_id)
        admission = await _row(
            self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (frame.route.session_id,)
        )
        from lets_play.storage.opening_flow import OPENING_VERSION

        if admission["version"] != OPENING_VERSION:
            raise IncompatibleState("Opening version needs explicit recovery.")
        if context:
            if (
                context.position.revision != row["owner_revision"]
                or context.position.round_id != row["original_round"]
            ):
                return "stale_owner"
            if context.position.phase is not Phase.ORDINARY:
                return "unsupported_mode"
            if row["original_pause"] not in (None, "revival"):
                return "existing_pause"
            from lets_play.storage.progression import Progression

            await Progression(self.unit)._context(frame.route.session_id)
            from lets_play.storage.pacing_flow import RoundPacing

            await RoundPacing(self.unit)._record(frame.route.session_id, context.position.round_id)
            from lets_play.storage.evolution_flow import EvolutionFlow

            config = await EvolutionFlow(self.unit)._configured(frame.route.session_id)
            saved = decode_config(config["config_json"])[1] if config else frozenset(CARD_SPECS)
            if row["original_pause"] == "revival" and not await _row(
                self.conn,
                "SELECT 1 FROM nr_revivals WHERE session_id=? AND stage IN ('prepared','published')",
                (frame.route.session_id,),
            ):
                return "legacy_revival"
        else:
            if admission["revision"] != row["owner_revision"]:
                return "stale_owner"
            if admission["stage"] == "recovery":
                return "existing_pause"
            saved = decode_config(admission["config_json"])[1]
        templates = (
            saved if available_templates is None else OrdinaryFlow._available(available_templates)
        )
        plan = "continue"
        if context and context.position.offer_id:
            offer = await OrdinaryFlow(self.unit).get_offer(
                frame.route.session_id, context.position.offer_id
            )
            if offer is None:
                return "missing_offer"
            prospective = replace(
                context,
                position=replace(
                    context.position,
                    lifecycle=Lifecycle(row["original_lifecycle"]),
                    pause_reason=PauseReason(row["original_pause"])
                    if row["original_pause"]
                    else None,
                ),
            )
            bound = replace(offer, hand=replace(offer.hand, owner=prospective.position))
            fields = (
                "route",
                "phase",
                "advancement",
                "round_id",
                "ruleset_version",
                "offer_id",
                "transition_id",
            )
            if offer.hand.active_instances != context.active_instances or any(
                getattr(offer.hand.owner, name) != getattr(context.position, name)
                for name in fields
            ):
                return "stale_offer"
            if context.position.transition_id:
                activation = await _row(
                    self.conn,
                    "SELECT * FROM nr_activations WHERE session_id=? AND offer_id=?",
                    (frame.route.session_id, context.position.offer_id),
                )
                if activation is None:
                    return "missing_activation"
                if activation["announced_at_utc"] is None:
                    proof = await _row(
                        self.conn,
                        """SELECT 1 FROM nr_recovery_work w JOIN nr_ordinary_outputs o USING(output_id)
                        WHERE w.session_id=? AND w.recovery_id=? AND w.family='ordinary'
                        AND w.status='unsent' AND o.session_id=w.session_id AND o.offer_id=? AND o.kind='activation'""",
                        (frame.route.session_id, recovery_id, context.position.offer_id),
                    )
                    if proof is None:
                        return "unknown_delivery"
                    plan = "hold"
                elif activation["announced_at_utc"] >= activation["effective_at_utc"]:
                    return "late_announcement"
                else:
                    completions = await OrdinaryFlow(self.unit)._completions(
                        bound, prospective, offer.selected_incoming, templates
                    )
                    if not any(c.outgoing_id == offer.selected_outgoing for c in completions):
                        return "unprovable_activation"
            elif not all(
                [
                    await OrdinaryFlow(self.unit)._completions(
                        bound, prospective, o.incoming.instance_id, templates
                    )
                    for o in offer.hand.options
                ]
            ):
                plan = "hold"
        if not context and admission["stage"] == "selected":
            if admission["announced_at_utc"] is None:
                proof = await _row(
                    self.conn,
                    "SELECT 1 FROM nr_recovery_work WHERE session_id=? AND recovery_id=? AND family='opening' AND kind='activation' AND status='unsent'",
                    (frame.route.session_id, recovery_id),
                )
                if proof is None:
                    return "unknown_delivery"
                from lets_play.storage.opening_flow import OpeningFlow

                await OpeningFlow(self.unit)._end(admission, frame, "unavailable")
                await self._settle_terminal(frame, row)
                return "closed"
            if admission["announced_at_utc"] >= admission["effective_at_utc"]:
                return "late_announcement"
            from lets_play.storage.opening_flow import OpeningFlow

            if OpeningFlow(self.unit)._completion(admission, templates) is None:
                return "unprovable_activation"
        await _execute(
            self.conn,
            "UPDATE nr_recoveries SET stage='ready',plan=?,templates_json=?,notice_prepared_at_utc=? WHERE session_id=? AND recovery_id=?",
            (
                plan,
                json.dumps(sorted(templates)),
                utc_int(frame.received_at),
                frame.route.session_id,
                recovery_id,
            ),
        )
        return "ready"

    @_write
    async def publish_notice(self, frame, recovery_id, *, message_id, published_at=None):
        row, error = await self._enter(frame, EventKind.RECOVERY_NOTICE_PUBLISHED, recovery_id)
        if error:
            if (
                row is not None
                and row["notice_message_id"] is not None
                and row["stage"] == "resumed"
            ):
                if row["notice_message_id"] != message_id:
                    raise ValueError("A recovery notice cannot change identity.")
                return "already_done"
            return error
        _id(message_id)
        if row["notice_message_id"] is not None:
            if row["notice_message_id"] != message_id:
                raise ValueError("A recovery notice cannot change identity.")
            return "already_done"
        if row["stage"] != "ready":
            return "unavailable"
        actual = frame.received_at if published_at is None else published_at
        if not row["notice_prepared_at_utc"] <= utc_int(actual) <= utc_int(frame.received_at):
            raise ValueError(
                "Recovery notice publication must follow preparation and precede its receipt."
            )
        await _execute(
            self.conn,
            "UPDATE nr_recoveries SET stage='reading',notice_message_id=?,notice_published_at_utc=?,resume_at_utc=? WHERE session_id=? AND recovery_id=?",
            (
                message_id,
                utc_int(actual),
                utc_int(actual + timedelta(seconds=RECOVERY_READ_IN_SECONDS)),
                frame.route.session_id,
                recovery_id,
            ),
        )
        return "reading"

    async def _resume(self, frame, row):
        session = frame.route.session_id
        templates = frozenset(json.loads(row["templates_json"]))
        context = await self.unit.get_context(session)
        if context:
            p = context.position
            if (
                p.revision != row["owner_revision"]
                or p.lifecycle is not Lifecycle.PAUSED
                or p.pause_reason is not PauseReason.RECOVERY
            ):
                raise IncompatibleState("Recovery cannot resume a superseded owner.")
            new = replace(
                p,
                lifecycle=Lifecycle(row["original_lifecycle"]),
                pause_reason=PauseReason(row["original_pause"]) if row["original_pause"] else None,
                revision=p.revision + 1,
            )
            if row["plan"] == "hold":
                new = replace(new, offer_id=None, transition_id=None)
            context = replace(context, position=new)
            await self.unit.set_owner(p, context)
            if row["plan"] == "hold":
                if p.offer_id:
                    await _execute(
                        self.conn,
                        "UPDATE nr_offers SET stage='cancelled' WHERE session_id=? AND offer_id=?",
                        (session, p.offer_id),
                    )
                    await _execute(
                        self.conn,
                        "UPDATE nr_activations SET stage='recovery' WHERE session_id=? AND offer_id=? AND stage<>'activated'",
                        (session, p.offer_id),
                    )
                from lets_play.storage.evolution_flow import EvolutionFlow

                evolution = EvolutionFlow(self.unit)
                await evolution._configure(context, available_templates=templates)
                await evolution._hold(frame, context)
            elif p.offer_id:
                offer = await OrdinaryFlow(self.unit).get_offer(session, p.offer_id)
                await _execute(
                    self.conn,
                    "UPDATE nr_offers SET hand_json=? WHERE session_id=? AND offer_id=?",
                    (encode_hand(replace(offer.hand, owner=new)), session, p.offer_id),
                )
                await _execute(
                    self.conn,
                    "UPDATE nr_activations SET owner_revision=? WHERE session_id=? AND offer_id=? AND stage IN ('prepared','announced')",
                    (new.revision, session, p.offer_id),
                )
            if row["plan"] != "hold":
                await _execute(
                    self.conn,
                    "UPDATE nr_round_pacing SET owner_revision=? WHERE session_id=? AND round_id=?",
                    (new.revision, session, new.round_id),
                )
                await _execute(
                    self.conn,
                    "UPDATE nr_revivals SET owner_revision=? WHERE session_id=? AND stage IN ('prepared','published')",
                    (new.revision, session),
                )
        await _execute(
            self.conn,
            "UPDATE nr_recoveries SET stage='resumed',resumed_at_utc=? WHERE session_id=? AND recovery_id=?",
            (utc_int(frame.received_at), session, row["recovery_id"]),
        )
        if context is None:
            from lets_play.storage.opening_flow import OpeningFlow

            admission = await _row(
                self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (session,)
            )
            await OpeningFlow(self.unit)._advance_entered(frame, admission, templates)
        if await self.unit.get_context(session):
            from lets_play.storage.evolution_flow import EvolutionFlow
            from lets_play.storage.pacing_flow import RoundPacing

            await OrdinaryFlow(self.unit)._advance_due(frame, templates)
            await RoundPacing(self.unit)._advance_due(frame)
            await EvolutionFlow(self.unit)._advance_due(frame)

    @_write
    async def observe(self, frame, recovery_id, *, event=None, allow_resume=True):
        """Consume queued controls/timers/chat while fenced; never fetch old chat."""
        if frame.kind in (EventKind.MESSAGE_CREATE, EventKind.REACTION_ADD) and event is None:
            raise ValueError("Recovery chat/reaction ingress requires its plain event snapshot.")
        if event is not None and (
            not isinstance(event, (MessageEvent, ReactionEvent)) or event.envelope != frame
        ):
            raise ValueError("An exact plain ingress snapshot is required.")
        if event is not None and await active_recovery(self.unit, frame.route.session_id) is None:
            raise RecoveryRequired("Recovery ended; use normal scoring ingress for this event.")
        row, error = await self._enter(frame, frame.kind, recovery_id)
        if error:
            if (
                error == "stale"
                and event is not None
                and await active_recovery(self.unit, frame.route.session_id)
            ):
                await self._guard_event(frame, event)
            return error
        if (
            allow_resume
            and row["stage"] == "reading"
            and utc_int(frame.received_at) >= row["resume_at_utc"]
        ):
            await self._resume(frame, row)
            if event is not None and await self.unit.get_context(frame.route.session_id):
                return await OrdinaryFlow(self.unit)._ingest_entered(event)
            if event is not None:
                await self._guard_event(frame, event)
            return "resumed"
        if event is not None:
            await self._guard_event(frame, event)
        return "paused"

    async def _guard_event(self, frame, event):
        human = event.is_human if isinstance(event, MessageEvent) else not event.is_bot
        if human:
            context = await self.unit.get_context(frame.route.session_id)
            table = "nr_paused_sources" if context is not None else "nr_preplay_sources"
            await _execute(
                self.conn,
                f"INSERT INTO {table} VALUES (?,?,?) ON CONFLICT DO NOTHING",
                frame.source_key,
            )

    @_write
    async def end(self, frame, recovery_id, *, authorized):
        row, error = await self._enter(frame, EventKind.MANAGER_END, recovery_id)
        if error:
            return error
        if type(authorized) is not bool or not authorized or frame.actor_id is None:
            return "unauthorized"
        context = await self.unit.get_context(frame.route.session_id)
        if context:
            p = context.position
            await self.unit.set_owner(
                p,
                replace(
                    context,
                    position=replace(
                        p, lifecycle=Lifecycle.ENDED, pause_reason=None, revision=p.revision + 1
                    ),
                ),
            )
            from lets_play.storage.progression import Progression

            await Progression(self.unit).finalize_ended(frame.route.session_id)
        from lets_play.storage.opening_flow import OpeningFlow

        admission = await _row(
            self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (frame.route.session_id,)
        )
        await OpeningFlow(self.unit)._end(admission, frame, "manager")
        await _execute(
            self.conn,
            "UPDATE nr_recoveries SET stage='closed' WHERE session_id=? AND recovery_id=?",
            (frame.route.session_id, recovery_id),
        )
        return "closed"
