"""Ordered opening, scoring and fail-closed restart integration.

The transport is injected. Every write commits before network work begins.
This bounded preview does not run later-round succession or automatic summaries.
"""

import asyncio
import logging
from dataclasses import replace
from random import Random
from uuid import uuid4

from lets_play.discord_adapter.lane import Lane, LaneUnavailable, ReceiptClock
from lets_play.new_rules.contracts import EventEnvelope, EventKind, SessionRoute
from lets_play.storage.discord_store import DiscordStore
from lets_play.storage.opening_flow import OpeningFlow, OpeningStatus
from lets_play.storage.recovery_flow import RecoveryFlow, active_recovery

logger = logging.getLogger(__name__)
# Deliberately small, proven text/media/reaction slice; no phrase dependency.
OPENING_CARDS = frozenset({"NR004", "NR006", "NR007", "NR011", "NR034"})
PUBLISH_KINDS = {
    "lobby": EventKind.LOBBY_PUBLISHED,
    "offer": EventKind.OFFER_PUBLISHED,
    "activation": EventKind.ACTIVATION_PUBLISHED,
    "closed": EventKind.END_PUBLISHED,
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
                self.routes[(route.guild_id, route.channel_id)] = route
            cursor = page[-1].session_id
        self.ready = True
        # No automatic resume or blind re-send. Managers can end saved games.
        for route in tuple(self.routes.values()):
            await self._show_pause(route)

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

    async def state(self, session, *, page=0):
        async with self.store.transaction(write=False) as unit:
            return await DiscordStore(unit).state(session, page=page)

    async def _handle(self, route, request):
        d, action = request.data, request.action
        kind = {
            "admit": EventKind.LOBBY_CREATE,
            "join": EventKind.LOBBY_JOIN,
            "choose": EventKind.CHOICE_SUBMIT,
            "tick": EventKind.TIMER,
            "timeout": EventKind.CHOICE_TIMEOUT,
            "end": EventKind.MANAGER_END,
            "pause": EventKind.RECOVERY_BEGIN,
        }.get(action)
        if action == "publish":
            kind = PUBLISH_KINDS[d["kind"]]
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
                await DiscordStore(unit).delivered(d["output_id"], d["message_id"])
            if action == "pause":
                result = (
                    await RecoveryFlow(unit).begin(frame, uuid4().hex) if not recovery else "paused"
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
                        frame=event.envelope, recovery_id=recovery, event=event
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
            elif recovery:
                await RecoveryFlow(unit).observe(frame, recovery)
                result = "paused"
            elif action == "admit":
                result = await flow.admit(
                    frame,
                    capabilities=d["capabilities"],
                    rng=self.rng,
                    available_templates=OPENING_CARDS,
                )
            elif action in {"join", "choose"}:
                expected = "lobby" if action == "join" else "offer"
                known = await flow.known_message_id(route.session_id, expected)
                if known != d["message_id"]:
                    # Consume rejected controls, preserving ordering and due boundaries.
                    result = await flow.tick(replace(frame, kind=EventKind.TIMER))
                    result = "stale"
                elif action == "join":
                    result = await flow.join(frame, capabilities=d["capabilities"])
                    if result is OpeningStatus.ALREADY_DONE and await DiscordStore(
                        unit
                    ).is_lobby_member(route.session_id, frame.actor_id):
                        result = "already_joined"
                else:
                    record = await flow.record(route.session_id)
                    if record.hand is None or not 0 <= d["index"] < 3:
                        result = await flow.tick(replace(frame, kind=EventKind.TIMER))
                        result = "stale"
                    else:
                        result = await flow.choose(
                            frame, record.hand.options[d["index"]].incoming.instance_id
                        )
            elif action == "publish":
                result = await flow.publish(frame, d["kind"], message_id=d["message_id"])
            elif action == "timeout":
                result = await flow.timeout(frame, rng=self.rng)
            elif action == "tick":
                result = await flow.tick(frame)
            else:
                raise ValueError("Unknown runtime action.")
        # All returned flow values are now committed. Never send inside the unit.
        if action not in {"admit", "pause"}:
            self._kick(route)
        await self._schedule(route)
        return result

    async def _schedule(self, route):
        state = await self.state(route.session_id)
        old = self.timers.pop(route.session_id, None)
        if old:
            old.cancel()
        if state is None or state.recovery_id or state.admission.stage == "ended":
            return
        deadline = state.admission.next_deadline
        if deadline is None:
            return
        action = "timeout" if state.admission.stage == "opening_published" else "tick"
        delay = max(0, (deadline - self.clock.now()).total_seconds())
        self.timers[route.session_id] = asyncio.get_running_loop().call_later(
            delay,
            self._timer,
            route,
            action,
        )

    def _timer(self, route, action):
        if not self.closed:
            future = self.submit(route, action)
            future.add_done_callback(self._consume)

    @staticmethod
    def _consume(future):
        if not future.cancelled():
            future.exception()

    def _kick(self, route):
        self.dirty.add(route.session_id)
        if self.closed:
            return
        task = self.publishers.get(route.session_id)
        if task is None or task.done():
            self.publishers[route.session_id] = asyncio.create_task(self._publish(route))

    async def _publish(self, route):
        try:
            while not self.closed:
                self.dirty.discard(route.session_id)
                async with self.store.transaction(write=False) as unit:
                    pending = await DiscordStore(unit).pending(route.session_id)
                    markers = await DiscordStore(unit).markers(route.session_id)
                if pending:
                    output_id, kind = pending
                    async with self.store.transaction() as unit:
                        claimed = await DiscordStore(unit).claim(
                            route.session_id, output_id, self.clock.now()
                        )
                    if not claimed:
                        await self._fault(route, "uncertain_delivery")
                        return
                    state = await self.state(route.session_id)
                    async with self.network_slots, asyncio.timeout(20):
                        message_id = await self.transport.publish(route, kind, state)
                    await asyncio.shield(
                        self.submit(
                            route, "publish", kind=kind, output_id=output_id, message_id=message_id
                        )
                    )
                    continue
                if markers:
                    for intent, source in markers:
                        async with self.network_slots, asyncio.timeout(20):
                            await self.transport.mark(route, int(source))
                        async with self.store.transaction() as unit:
                            await unit.acknowledge_marker(intent)
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
        logger.warning("Game recovery required: session=%s reason=%s", route.session_id, reason)
        state = await self.state(route.session_id)
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
