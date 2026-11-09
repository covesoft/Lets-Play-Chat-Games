"""Ordered opening, scoring and fail-closed restart integration.

The transport is injected. Every write commits before network work begins.
Later ordinary rounds, reports and settled-round automatic reboot recovery share
that lane. Uncertain sends remain fenced for later proof reconciliation.
"""

import asyncio
import logging
from dataclasses import replace
from random import Random
from uuid import uuid4

from lets_play.discord_adapter.lane import Lane, LaneUnavailable, ReceiptClock
from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.contracts import EventEnvelope, EventKind, SessionRoute
from lets_play.storage.discord_store import DiscordStore
from lets_play.storage.evolution_flow import EvolutionFlow
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.offer_codec import utc_datetime
from lets_play.storage.opening_flow import OpeningFlow, OpeningStatus
from lets_play.storage.ordinary_flow import OrdinaryFlow
from lets_play.storage.pacing_flow import RoundPacing
from lets_play.storage.reboot_flow import RebootFlow
from lets_play.storage.recovery_flow import RecoveryFlow, active_recovery
from lets_play.storage.round_reports import RoundReports
from lets_play.storage.summary_store import SummaryStore

logger = logging.getLogger(__name__)
# Deliberately small, proven text/media/reaction slice; no phrase dependency.
OPENING_CARDS = frozenset({"NR004", "NR006", "NR007", "NR011", "NR034"})
ORDINARY_CARDS = frozenset(CARD_SPECS) - {"NR036"}
PUBLISH_KINDS = {
    "lobby": EventKind.LOBBY_PUBLISHED,
    "offer": EventKind.OFFER_PUBLISHED,
    "activation": EventKind.ACTIVATION_PUBLISHED,
    "closed": EventKind.END_PUBLISHED,
    "ordinary_offer": EventKind.OFFER_PUBLISHED,
    "ordinary_activation": EventKind.ACTIVATION_PUBLISHED,
    "pacing_warning": EventKind.WARNING_PUBLISHED,
    "pacing_revival": EventKind.REVIVAL_PUBLISHED,
    "evolution_held": EventKind.HELD_PUBLISHED,
    "evolution_closed_silent": EventKind.SILENT_END_PUBLISHED,
}


class OpeningRuntime:
    def __init__(self, store, transport, *, clock=None):
        self.store, self.transport = store, transport
        self.clock = clock or ReceiptClock()
        self.routes = {}  # Exact (guild,channel) -> route; live routes only.
        self.lanes = {}
        self.publishers = {}
        self.timers = {}
        self.dirty = set()
        self.configured = set()
        self.work_queued = set()
        self.recovery_routes = set()
        self.blocked_recoveries = set()
        self.network_slots = asyncio.Semaphore(4)
        self.lifecycle_lock = asyncio.Lock()
        self.ready = False
        self.closed = False
        self.rng = Random()

    def _lane(self, route):
        if route.session_id not in self.lanes:
            self.lanes[route.session_id] = Lane(
                lambda request: self._handle(route, request),
                lambda reason: self._fault(route, reason),
                self.clock,
            )
        return self.lanes[route.session_id]

    def submit(self, route, action, **data):
        return self._lane(route).submit(action, **data)

    async def initialize(self):
        """Enter EVERY durable recovery barrier before allowing normal intake/timers."""
        async with self.lifecycle_lock:
            await self._initialize()

    async def _initialize(self):
        self.ready = False
        self.recovery_routes.clear()
        self.blocked_recoveries.clear()
        for timer in self.timers.values():
            timer.cancel()
        self.timers.clear()
        # Stop sends and finish already-admitted receipts before allocating any
        # startup barrier. A cancelled in-flight send retains its durable claim.
        tasks = tuple(self.publishers.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(lane.queue.join() for lane in self.lanes.values()))
        cursor = ""
        while True:
            async with self.store.transaction(write=False) as unit:
                page = await RecoveryFlow(unit).live_routes(after_session=cursor)
            if not page:
                break
            for saved in page:
                route = SessionRoute(saved.guild_id, saved.channel_id, saved.session_id)
                await self._barrier(route, new_disruption=True)
                self.recovery_routes.add(route.session_id)
                self.routes[(route.guild_id, route.channel_id)] = route
            cursor = page[-1].session_id
        cursor = 0
        while True:
            async with self.store.transaction() as unit:
                cursor = await SummaryStore(unit).compact_delivered(after_summary=cursor)
            if cursor is None:
                break
        self.ready = True
        # All barriers exist before recovery publishers or normal work can run.
        for route in tuple(self.routes.values()):
            self._kick(route)

    async def _frame(self, unit, route, kind, received, actor=None, source=None, created=None):
        last, floor = await DiscordStore(unit).receipt_position(route.session_id)
        sequence = last + 1
        received = max(received, floor) if floor else received
        return EventEnvelope(
            route, kind, source or uuid4().hex, actor, created or received, received, sequence
        )

    async def _barrier(self, route, *, new_disruption=False):
        async with self.store.transaction() as unit:
            existing = await active_recovery(unit, route.session_id)
            if existing and not new_disruption:
                if existing["stage"] == "reading":
                    frame = await self._frame(unit, route, EventKind.TIMER, self.clock.now())
                    await RebootFlow(unit).block(frame, existing["recovery_id"])
                return
            frame = await self._frame(unit, route, EventKind.RECOVERY_BEGIN, self.clock.now())
            await RecoveryFlow(unit).begin(frame, uuid4().hex)

    async def start(self, guild_id, channel_id, actor_id, source_id, capabilities):
        if not self.ready or self.closed:
            raise LaneUnavailable("Game service is not ready.")
        route = SessionRoute(guild_id, channel_id, uuid4().hex)
        result = await asyncio.shield(
            self.submit(
                route,
                "admit",
                actor=actor_id,
                source=source_id,
                capabilities=capabilities,
            )
        )
        if result is OpeningStatus.ADMITTED:
            self.routes[(guild_id, channel_id)] = route
            self._kick(route)
        else:
            await self.lanes.pop(route.session_id).close()
        return route, result

    async def state(self, session, *, page=0, result_anchor=0, result_backwards=False):
        async with self.store.transaction(write=False) as unit:
            return await DiscordStore(unit).state(
                session, page=page, result_anchor=result_anchor, result_backwards=result_backwards
            )

    async def _handle(self, route, request):
        d, action = request.data, request.action
        kind = {
            "admit": EventKind.LOBBY_CREATE,
            "join": EventKind.LOBBY_JOIN,
            "choose": EventKind.CHOICE_SUBMIT,
            "target": EventKind.CHOICE_SUBMIT,
            "revive": EventKind.REVIVAL_JOIN,
            "tick": EventKind.TIMER,
            "timeout": EventKind.CHOICE_TIMEOUT,
            "end": EventKind.MANAGER_END,
            "pause": EventKind.RECOVERY_BEGIN,
            "configure": EventKind.ADVANCEMENT_RECHECK,
            "prepare": EventKind.OFFER_PREPARE,
            "summary_request": EventKind.TIMER,
            "summary_ack": EventKind.TIMER,
            "round_ack": EventKind.TIMER,
            "recovery_prepare": EventKind.RECOVERY_APPROVE,
            "recovery_claim": EventKind.RECOVERY_NOTICE_CLAIM,
            "recovery_ack": EventKind.RECOVERY_NOTICE_PUBLISHED,
            "recovery_resume": EventKind.TIMER,
        }.get(action)
        if action == "publish":
            kind = PUBLISH_KINDS[d["output"].ui_kind]
        elif action == "event":
            kind = d["event"].envelope.kind
        async with self.store.transaction() as unit:
            frame = await self._frame(
                unit,
                route,
                kind,
                request.received_at,
                d.get("actor"),
                d.get("source"),
                d["event"].envelope.created_at if action == "event" else None,
            )
            flow = OpeningFlow(unit)
            recovery_row = await active_recovery(unit, route.session_id)
            recovery = recovery_row["recovery_id"] if recovery_row else None
            if action == "publish":
                await DiscordStore(unit).delivered(d["output"], d["message_id"], frame.sequence)
            if action == "pause":
                result = (
                    await RecoveryFlow(unit).begin(frame, uuid4().hex)
                    if not recovery
                    else await RebootFlow(unit).block(frame, recovery)
                )
            elif action == "recovery_prepare":
                result = await RebootFlow(unit).prepare(
                    frame,
                    d["recovery_id"],
                    capabilities=d["capabilities"],
                    templates=ORDINARY_CARDS,
                )
            elif action == "recovery_claim":
                result = await RebootFlow(unit).claim(frame, d["recovery_id"])
            elif action == "recovery_ack":
                result = await RebootFlow(unit).acknowledge(
                    frame, d["recovery_id"], d["message_id"]
                )
            elif action == "recovery_resume":
                result = await RebootFlow(unit).resume(
                    frame, d["recovery_id"], capabilities=d["capabilities"]
                )
            elif action == "event":
                event = replace(
                    d["event"],
                    envelope=replace(
                        frame,
                        actor_id=d["event"].envelope.actor_id,
                        source_id=d["event"].envelope.source_id,
                    ),
                )
                result = (
                    await RecoveryFlow(unit).observe(
                        event.envelope, recovery, event=event, allow_resume=False
                    )
                    if recovery
                    else await flow.ingest(event)
                )
            elif action == "end":
                result = (
                    await RecoveryFlow(unit).end(frame, recovery, authorized=d["authorized"])
                    if recovery
                    else await flow.cancel(frame, authorized=d["authorized"])
                )
            elif action in {"summary_ack", "round_ack"}:
                if recovery:
                    await RecoveryFlow(unit).observe(frame, recovery, allow_resume=False)
                else:
                    await flow.tick(frame)
                if action == "summary_ack":
                    result = await SummaryStore(unit).acknowledge(
                        route.session_id,
                        d["output_key"],
                        message_id=d["message_id"],
                        now=frame.received_at,
                    )
                else:
                    result = await RoundReports(unit).acknowledge(
                        d["report"], message_id=d["message_id"], now=frame.received_at
                    )
            elif recovery:
                await RecoveryFlow(unit).observe(frame, recovery, allow_resume=False)
                result = "paused"
            elif action == "admit":
                result = await flow.admit(
                    frame,
                    capabilities=d["capabilities"],
                    rng=self.rng,
                    available_templates=OPENING_CARDS,
                )
            else:
                result = await self._normal(unit, route, frame, action, d)
            if not recovery and action not in {"pause", "end", "admit"}:
                await self._maybe_auto(unit, route, frame)
        self.work_queued.discard(d.get("work_key"))
        if action == "configure" and not recovery:
            self.configured.add(route.session_id)
        await self._followups(route)
        if action not in {"admit", "pause"}:
            self._kick(route)
        await self._schedule(route)
        return result

    async def _maybe_auto(self, unit, route, frame):
        context = await unit.get_context(route.session_id)
        if not context or context.position.lifecycle != "playing":
            return
        summaries = SummaryStore(unit)
        await summaries.initialize(route.session_id)
        if await RoundReports(unit).pending(route.session_id):
            return
        due = await summaries.next_deadline(route.session_id, now=frame.received_at)
        if due and frame.received_at >= due:
            await summaries.prepare(
                route.session_id, f"auto:{frame.sequence}", now=frame.received_at
            )

    async def _normal(self, unit, route, frame, action, d):
        opening, flow, evolution = OpeningFlow(unit), OrdinaryFlow(unit), EvolutionFlow(unit)
        context = await unit.get_context(route.session_id)

        async def stale():
            await opening.tick(replace(frame, kind=EventKind.TIMER))
            return "stale"

        if action == "configure":
            return await evolution.recheck(
                frame, available_templates=d.get("templates", ORDINARY_CARDS)
            )
        if action == "prepare":
            pacing = await RoundPacing(unit).record(route.session_id)
            if (
                not context
                or context.position.round_id != d["round_id"]
                or not pacing
                or pacing.attempt != d["attempt"]
            ):
                return await stale()
            return await evolution.prepare_or_hold(frame, offer_id=d["offer_id"])
        if action in {"join", "choose", "target", "revive"}:
            known = await DiscordStore(unit).control_message(route.session_id, action)
            if known != d["message_id"]:
                return await stale()
            if action == "join":
                result = await opening.join(frame, capabilities=d["capabilities"])
                if result is OpeningStatus.ALREADY_DONE and await DiscordStore(
                    unit
                ).is_lobby_member(route.session_id, frame.actor_id):
                    return "already_joined"
                return result
            if action == "revive":
                revival = await evolution.revival(route.session_id)
                if not revival:
                    return await stale()
                result = await evolution.join_revival(frame, revival.revival_id)
                return (
                    "already_joined"
                    if result == "already_done"
                    else "revived"
                    if result == "quorum"
                    else result
                )
            record = (
                await flow.get_offer(route.session_id, context.position.offer_id)
                if context and context.position.offer_id
                else None
            )
            hand = record.hand if record else (await opening.record(route.session_id)).hand
            if hand is None or not 0 <= d["index"] < 3:
                return await stale()
            option = hand.options[d["index"]]
            if record:
                outgoing = None
                if action == "target":
                    target = d.get("target_index", -1)
                    if (
                        not 0 <= target < len(option.outgoing_ids)
                        or record.selected_incoming != option.incoming.instance_id
                    ):
                        return await stale()
                    outgoing = option.outgoing_ids[target]
                result = await flow.choose(
                    frame, hand.offer_id, option.incoming.instance_id, outgoing_id=outgoing
                )
                return result.status.value
            return await opening.choose(frame, option.incoming.instance_id)
        if action == "publish":
            output, message = d["output"], d["message_id"]
            if output.family == "opening":
                return await opening.publish(frame, output.kind, message_id=message)
            if output.family == "ordinary":
                method = flow.publish_offer if output.kind == "offer" else flow.publish_activation
                return await method(frame, output.token, message_id=message)
            if output.family == "pacing":
                if output.kind == "warning":
                    return await RoundPacing(unit).publish_warning(
                        frame, output.round_id, message_id=message, attempt=output.attempt
                    )
                return await evolution.publish_revival(frame, output.token, message_id=message)
            return await evolution.publish_notice(frame, output.token, message_id=message)
        if action == "timeout":
            if context:
                if context.position.offer_id:
                    result = await flow.timeout(frame, context.position.offer_id, rng=self.rng)
                    if result.status.value == "unavailable":
                        refreshed = await evolution._refresh_entered(
                            frame, context.position.offer_id
                        )
                        if refreshed == "unchanged":
                            raise LaneUnavailable("A saved timeout has no provable completion.")
                        return refreshed
                    return result.status.value
                return await opening.tick(replace(frame, kind=EventKind.TIMER))
            return await opening.timeout(frame, rng=self.rng)
        if action == "tick":
            return await opening.tick(frame)
        if action == "summary_request":
            # Observe due phase boundaries before capturing, in the same receipt.
            await opening.tick(frame)
            key = "request:" + frame.source_id
            return await SummaryStore(unit).request(route.session_id, key, now=frame.received_at)
        raise ValueError("Unknown runtime action.")

    async def _followups(self, route):
        if self.closed:
            return
        state = await self.state(route.session_id)
        if not state or not state.context or state.recovery_id or state.admission.stage == "ended":
            return
        session = route.session_id
        if session not in self.configured:
            key = (session, "configure")
            if key not in self.work_queued:
                self.work_queued.add(key)
                self.submit(route, "configure", work_key=key).add_done_callback(self._consume)
        elif (
            state.pacing
            and state.pacing.stage == "selected"
            and not state.context.position.offer_id
        ):
            key = (session, state.context.position.round_id, state.pacing.attempt)
            if key not in self.work_queued:
                self.work_queued.add(key)
                self.submit(
                    route,
                    "prepare",
                    round_id=key[1],
                    attempt=key[2],
                    offer_id=f"{key[1]}:choice:{key[2]}",
                    work_key=key,
                ).add_done_callback(self._consume)

    async def _schedule(self, route):
        if self.closed or not self.ready:
            return
        state = await self.state(route.session_id)
        old = self.timers.pop(route.session_id, None)
        if old:
            old.cancel()
        if self.closed or not self.ready or not state or state.admission.stage == "ended":
            return
        if state.recovery_id:
            if state.recovery_resume_at and route.session_id not in self.blocked_recoveries:
                delay = max(0, (state.recovery_resume_at - self.clock.now()).total_seconds())
                self.timers[route.session_id] = asyncio.get_running_loop().call_later(
                    delay, self._timer, route, "recovery_resume"
                )
            return
        deadline = state.next_deadline
        async with self.store.transaction(write=False) as unit:
            summary_due = (
                None
                if await RoundReports(unit).pending(route.session_id)
                else await SummaryStore(unit).next_deadline(route.session_id, now=self.clock.now())
            )
        if summary_due and (deadline is None or summary_due < deadline):
            deadline = summary_due
            action = "tick"
        else:
            action = (
                "timeout"
                if state.timeout_due or state.admission.stage == "opening_published"
                else "tick"
            )
        if deadline is None:
            return
        delay = max(0, (deadline - self.clock.now()).total_seconds())
        self.timers[route.session_id] = asyncio.get_running_loop().call_later(
            delay, self._timer, route, action
        )

    def _timer(self, route, action):
        if not self.closed and self.routes.get((route.guild_id, route.channel_id)) == route:
            if action == "recovery_resume":
                self._kick(route)
                return
            future = self.submit(route, action)
            future.add_done_callback(self._consume)

    @staticmethod
    def _consume(future):
        if not future.cancelled():
            future.exception()

    def _kick(self, route):
        self.dirty.add(route.session_id)
        if self.closed or not self.ready:
            return
        task = self.publishers.get(route.session_id)
        if task is None or task.done():
            self.publishers[route.session_id] = asyncio.create_task(self._publish(route))

    async def _publish(self, route):
        try:
            while not self.closed and self.ready:
                self.dirty.discard(route.session_id)
                async with self.store.transaction(write=False) as unit:
                    recovery = await active_recovery(unit, route.session_id)
                    pending = await DiscordStore(unit).pending(route.session_id)
                    markers = await DiscordStore(unit).markers(route.session_id)
                if recovery:
                    if await self._publish_recovery(route):
                        continue
                    state = await self.state(route.session_id)
                    if state and state.admission.stage == "ended":
                        continue
                    return
                if pending:
                    if pending.kind == "recovery":
                        await self._fault(route, "flow_recovery")
                        return
                    async with self.store.transaction() as unit:
                        claimed = await DiscordStore(unit).claim(
                            route.session_id, pending, self.clock.now()
                        )
                    if not claimed:
                        async with self.store.transaction(write=False) as unit:
                            if await DiscordStore(unit).pending(route.session_id) != pending:
                                continue
                        await self._fault(route, "uncertain_delivery")
                        return
                    state = await self.state(route.session_id)
                    async with self.network_slots, asyncio.timeout(20):
                        message_id = await self.transport.publish(route, pending.ui_kind, state)
                    await asyncio.shield(
                        self.submit(route, "publish", output=pending, message_id=message_id)
                    )
                    continue
                if markers:
                    for intent, source in markers:
                        async with self.network_slots, asyncio.timeout(20):
                            await self.transport.mark(route, int(source))
                        async with self.store.transaction() as unit:
                            await unit.acknowledge_marker(intent)
                    continue
                if await self._publish_round(route):
                    continue
                if await self._publish_summary(route):
                    continue
                state = await self.state(route.session_id)
                if state and state.admission.stage in {"active", "ended"}:
                    async with self.network_slots, asyncio.timeout(20):
                        await self.transport.refresh(route, state)
                if state and state.admission.stage == "recovery":
                    await self._fault(route, "opening_publication")
                if state and state.admission.stage == "ended":
                    async with self.store.transaction(write=False) as unit:
                        if await DiscordStore(unit).pending(route.session_id):
                            continue
                    await self._retire(route)
                if route.session_id in self.dirty:
                    continue
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._fault(route, type(exc).__name__)
        finally:
            self.publishers.pop(route.session_id, None)

    async def _publish_recovery(self, route):
        session = route.session_id
        if session in self.blocked_recoveries:
            return False
        async with self.store.transaction(write=False) as unit:
            candidate = await RebootFlow(unit).candidate(session)
        if session not in self.recovery_routes or candidate is None:
            self.blocked_recoveries.add(session)
            await self._show_pause(route)
            return False
        recovery = candidate["recovery_id"]
        if candidate["stage"] == "reading":
            if self.clock.now() < utc_datetime(candidate["resume_at_utc"]):
                return False
            state = await self.state(session)
            async with self.network_slots, asyncio.timeout(20):
                facts = await self.transport.recovery_capabilities(route, state.panel_id)
            result = await self.submit(
                route, "recovery_resume", recovery_id=recovery, capabilities=facts
            )
            if result == "resumed":
                self.recovery_routes.discard(session)
                return True
            self.blocked_recoveries.add(session)
            await self._show_pause(route)
            return False
        if candidate["stage"] == "paused":
            state = await self.state(session)
            async with self.network_slots, asyncio.timeout(20):
                facts = await self.transport.recovery_capabilities(route, state.panel_id)
            if (
                await self.submit(
                    route, "recovery_prepare", recovery_id=recovery, capabilities=facts
                )
                != "ready"
            ):
                self.blocked_recoveries.add(session)
                await self._show_pause(route)
                return False
            return True
        async with self.network_slots, asyncio.timeout(20):
            if not await self.submit(route, "recovery_claim", recovery_id=recovery):
                return False
            state = await self.state(session)
            message_id = await self.transport.publish_recovery(route, state)
            await asyncio.shield(
                self.submit(route, "recovery_ack", recovery_id=recovery, message_id=message_id)
            )
        return False

    async def _publish_summary(self, route):
        async with self.store.transaction(write=False) as unit:
            context = await unit.get_context(route.session_id)
            if (
                context is None
                or context.position.lifecycle == "ended"
                or await active_recovery(unit, route.session_id)
            ):
                return False
            summary = await SummaryStore(unit).pending(route.session_id)
            if summary is None:
                return False
            page = await SummaryStore(unit).preview(summary)
        if summary.state != "prepared":
            await self._fault(route, "uncertain_summary_delivery")
            return False
        try:
            async with self.network_slots, asyncio.timeout(20):
                async with self.store.transaction() as unit:
                    claimed = await SummaryStore(unit).claim(
                        route.session_id, summary.output_key, now=self.clock.now()
                    )
                if not claimed:
                    await self._schedule(route)
                    return False
                message_id = await self.transport.publish_summary(route, summary, page)
            await asyncio.shield(
                self.submit(
                    route, "summary_ack", output_key=summary.output_key, message_id=message_id
                )
            )
        except asyncio.CancelledError:
            # The durable sending claim is intentionally retained on shutdown.
            raise
        except Exception:
            async with self.store.transaction() as unit:
                await SummaryStore(unit).mark_uncertain(route.session_id, summary.output_key)
            raise
        return True

    async def _publish_round(self, route):
        async with self.store.transaction(write=False) as unit:
            reports = RoundReports(unit)
            report = await reports.pending(route.session_id)
            if report is None:
                return False
            page = await reports.page(route.session_id, report.round_number)
        if report.state is not None:
            await self._fault(route, "uncertain_round_report")
            return False
        try:
            async with self.network_slots, asyncio.timeout(20):
                async with self.store.transaction() as unit:
                    claimed = await RoundReports(unit).claim(report, now=self.clock.now())
                if not claimed:
                    return False
                message_id = await self.transport.publish_round(route, report, page)
            await asyncio.shield(
                self.submit(route, "round_ack", report=report, message_id=message_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self.store.transaction() as unit:
                await RoundReports(unit).uncertain(report)
            raise
        return True

    async def _retire(self, route):
        if self.routes.get((route.guild_id, route.channel_id)) == route:
            self.routes.pop((route.guild_id, route.channel_id), None)
        lane = self.lanes.pop(route.session_id, None)
        if lane and asyncio.current_task() is not lane.task:
            await lane.queue.join()
            await lane.close()
        timer = self.timers.pop(route.session_id, None)
        if timer:
            timer.cancel()
        self.dirty.discard(route.session_id)
        self.configured.discard(route.session_id)
        self.work_queued = {key for key in self.work_queued if key[0] != route.session_id}

    async def _show_pause(self, route):
        try:
            state = await self.state(route.session_id)
            async with self.network_slots, asyncio.timeout(20):
                await self.transport.paused(route, state)
        except Exception as exc:
            logger.warning(
                "Game pause output unavailable: session=%s type=%s",
                route.session_id,
                type(exc).__name__,
            )

    async def _fault(self, route, reason):
        self.blocked_recoveries.add(route.session_id)
        logger.warning("Game recovery required: session=%s reason=%s", route.session_id, reason)
        try:
            state = await self.state(route.session_id)
        except IncompatibleState:
            state = None
        if state and state.admission.stage == "ended":
            await self._retire(route)
            return
        timer = self.timers.pop(route.session_id, None)
        if timer:
            timer.cancel()
        # Serialize the barrier with ingress. A worker fault already owns its lane;
        # other delivery failures enter through a control receipt below.
        lane = self.lanes.get(route.session_id)
        if lane and not lane.fenced and asyncio.current_task() is not lane.task:
            await asyncio.shield(lane.submit("pause"))
        else:
            await self._barrier(route)
        await self._show_pause(route)

    async def pause(self, route):
        await asyncio.shield(self.submit(route, "pause"))
        await self._show_pause(route)

    async def disconnected(self):
        async with self.lifecycle_lock:
            await self._disconnected()

    async def _disconnected(self):
        self.ready = False
        for timer in self.timers.values():
            timer.cancel()
        self.timers.clear()
        for route in tuple(self.routes.values()):
            await self.pause(route)

    async def close(self):
        self.closed, self.ready = True, False
        for timer in self.timers.values():
            timer.cancel()
        for task in tuple(self.publishers.values()):
            task.cancel()
        await asyncio.gather(*tuple(self.publishers.values()), return_exceptions=True)
        await asyncio.gather(*(lane.close() for lane in tuple(self.lanes.values())))
        self.publishers.clear()
        self.lanes.clear()
        self.routes.clear()
        self.timers.clear()
        self.configured.clear()
        self.work_queued.clear()
        self.recovery_routes.clear()
        self.blocked_recoveries.clear()
