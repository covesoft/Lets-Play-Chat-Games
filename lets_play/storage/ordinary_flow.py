"""Durable later-round ordinary choices, composed with general progression.

Every action carries a server-ordered envelope. Sequence gaps fail before a timer
can close a round over queued chat. Publication acknowledgement is an explicit
input, never inferred from preparing an output or from a successful mock send.
"""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.compatibility import legal_completions
from lets_play.new_rules.contracts import (
    Advancement,
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    Phase,
    RandomSource,
    ReactionEvent,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message, evaluate_reaction
from lets_play.new_rules.hands import ChoiceStatus, OrdinaryHand, revalidate_choice
from lets_play.new_rules.parsing import parse_message
from lets_play.new_rules.text_rules import phrase_keys
from lets_play.storage.game_store import ApplyStatus, GameUnitOfWork, _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import decode_hand, encode_hand, utc_datetime, utc_int

CHATGAMES_CHOICE_SECONDS = 60
CHATGAMES_ACTIVATION_LEAD_SECONDS = 5


class FlowOrderError(ValueError):
    """A gap/regressing server clock must be reconciled, never silently skipped."""


class FlowStatus(StrEnum):
    PREPARED = "prepared"
    PUBLISHED = "published"
    NEEDS_TARGET = "needs_target"
    SELECTED = "selected"
    ANNOUNCED = "announced"
    ACTIVATED = "activated"
    RECOVERY = "recovery"
    REPLAY = "replay"
    STALE = "stale"
    UNAUTHORIZED = "unauthorized"
    EXPIRED = "expired"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    ALREADY_DONE = "already_done"
    NOT_DUE = "not_due"
    IGNORED = "ignored"
    INGESTED = "ingested"


@dataclass(frozen=True, slots=True)
class OfferRecord:
    stage: str
    hand: OrdinaryHand
    published_at: datetime | None
    selected_incoming: str | None
    selected_outgoing: str | None
    effective_at: datetime | None
    chosen_by_picker: bool
    completed_by_timeout: bool

    @property
    def choice_deadline(self):
        """First ACK establishes the clock; a refreshed hand can inherit it."""
        return self.hand.deadline if self.published_at is not None else None


@dataclass(frozen=True, slots=True)
class FlowResult:
    status: FlowStatus
    score_status: ApplyStatus | None = None


@dataclass(frozen=True, slots=True)
class OutputIntent:
    output_id: int
    session_id: str
    offer_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class FlowWatermark:
    last_sequence: int
    observed_at: datetime


class OrdinaryFlow:
    """One facade over an existing unit: its returned effects remain provisional."""

    def __init__(self, unit: GameUnitOfWork):
        self.unit = unit
        self.conn = unit._conn

    async def watermark(self, session_id: str) -> FlowWatermark | None:
        self.unit._check()
        row = await _row(
            self.conn,
            "SELECT last_sequence,observed_at_utc FROM nr_flow_watermarks WHERE session_id=?",
            (session_id,),
        )
        return FlowWatermark(row[0], utc_datetime(row[1])) if row is not None else None

    async def get_offer(self, session_id: str, offer_id: str) -> OfferRecord | None:
        self.unit._check()
        row = await _row(
            self.conn,
            """SELECT o.*,a.effective_at_utc FROM nr_offers o
            LEFT JOIN nr_activations a USING(session_id,offer_id)
            WHERE o.session_id=? AND o.offer_id=?""",
            (session_id, offer_id),
        )
        if row is None:
            return None
        return OfferRecord(
            row["stage"],
            decode_hand(row["hand_json"]),
            utc_datetime(row["published_at_utc"]) if row["published_at_utc"] is not None else None,
            row["selected_incoming"],
            row["selected_outgoing"],
            utc_datetime(row["effective_at_utc"]) if row["effective_at_utc"] is not None else None,
            bool(row["chosen_by_picker"]),
            bool(row["completed_by_timeout"]),
        )

    async def used_keys(
        self, session_id: str, keys: tuple[tuple[str, ...], ...]
    ) -> frozenset[tuple[str, ...]]:
        self.unit._check()
        if not isinstance(keys, tuple) or len(keys) > 15:
            raise ValueError("A used-phrase lookup is bounded to fifteen candidate keys.")
        if not keys:
            return frozenset()
        encoded = tuple(self._key(key) for key in keys)
        async with self.conn.execute(
            f"SELECT phrase_key FROM nr_used_phrases WHERE session_id=? AND phrase_key IN ({','.join('?' for _ in encoded)})",
            (session_id, *encoded),
        ) as cursor:
            return frozenset(tuple(json.loads(row[0])) for row in await cursor.fetchall())

    @staticmethod
    def _key(key):
        if (
            not isinstance(key, tuple)
            or not key
            or not all(isinstance(word, str) and word for word in key)
        ):
            raise ValueError("A nonempty normalized phrase identity is required.")
        if parse_message(" ".join(key)).word_keys != key:
            raise ValueError("A phrase key must use shared normalized word identities.")
        return json.dumps(key, ensure_ascii=False, separators=(",", ":"))

    async def pending_outputs(self, *, limit=100, session_id=None) -> tuple[OutputIntent, ...]:
        self.unit._check()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Output page size must be an integer in [1,100].")
        async with self.conn.execute(
            """SELECT x.output_id,x.session_id,x.offer_id,x.kind
            FROM nr_ordinary_outputs x JOIN nr_offers o USING(session_id,offer_id)
            JOIN nr_sessions s USING(session_id)
            WHERE (? IS NULL OR x.session_id=?) AND x.acknowledged_at_utc IS NULL AND x.retired_at_utc IS NULL
            AND s.lifecycle<>'ended' AND NOT EXISTS(SELECT 1 FROM nr_recoveries r WHERE r.session_id=s.session_id AND r.stage IN ('paused','ready','reading')) AND
            ((x.kind='offer' AND o.stage='prepared') OR
             (x.kind='activation' AND o.stage='selected') OR
             (x.kind='recovery' AND o.stage='recovery'))
            ORDER BY x.output_id LIMIT ?""",
            (session_id, session_id, limit),
        ) as cursor:
            return tuple(OutputIntent(*row) for row in await cursor.fetchall())

    async def _output(self, session, offer, kind):
        await _execute(
            self.conn,
            "INSERT INTO nr_ordinary_outputs(session_id,offer_id,kind) VALUES (?,?,?)",
            (session, offer, kind),
        )

    async def known_message_id(self, session_id: str, offer_id: str, kind: str) -> int | None:
        self.unit._check()
        if kind not in {"offer", "activation", "recovery"}:
            raise ValueError("Unknown ordinary output kind.")
        row = await _row(
            self.conn,
            "SELECT message_id FROM nr_ordinary_outputs WHERE session_id=? AND offer_id=? AND kind=?",
            (session_id, offer_id, kind),
        )
        return row[0] if row is not None else None

    async def _enter(self, frame: EventEnvelope, kind: EventKind, available) -> bool:
        from lets_play.storage.recovery_flow import require_dispatchable

        await require_dispatchable(self.unit, frame.route.session_id)
        if frame.kind is not kind or frame.sequence < 1:
            raise FlowOrderError("A positive ordered envelope of the correct kind is required.")
        context = await self.unit.get_context(frame.route.session_id)
        if context is None or context.position.route != frame.route:
            raise FlowOrderError("The envelope must match an existing exact session route.")
        mark = await _row(
            self.conn,
            "SELECT * FROM nr_flow_watermarks WHERE session_id=?",
            (frame.route.session_id,),
        )
        last = mark["last_sequence"] if mark is not None else 0
        if frame.sequence <= last:
            return False
        if frame.sequence != last + 1:
            raise FlowOrderError("A prior envelope must commit before this sequence can run.")
        observed = utc_int(frame.received_at)
        if mark is not None and observed < mark["observed_at_utc"]:
            raise FlowOrderError("Server receipt time regressed; explicit recovery is required.")
        await _execute(
            self.conn,
            """INSERT INTO nr_flow_watermarks VALUES (?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET last_sequence=excluded.last_sequence,
            observed_at_utc=excluded.observed_at_utc""",
            (frame.route.session_id, frame.sequence, observed),
        )
        await self._advance_due(frame, available)
        from lets_play.storage.pacing_flow import RoundPacing

        await RoundPacing(self.unit)._advance_due(frame)
        from lets_play.storage.evolution_flow import EvolutionFlow

        await EvolutionFlow(self.unit)._advance_due(frame)
        return True

    @_write
    async def prepare_offer(self, frame: EventEnvelope, hand: OrdinaryHand) -> FlowResult:
        if not await self._enter(frame, EventKind.OFFER_PREPARE, None):
            return FlowResult(FlowStatus.REPLAY)
        return await self._prepare_entered(frame, hand)

    async def _prepare_entered(self, frame, hand):
        session = frame.route.session_id
        existing = await self.get_offer(session, hand.offer_id)
        if existing:
            if existing.hand.options != hand.options or existing.hand.picker_id != hand.picker_id:
                return FlowResult(FlowStatus.INVALID)
            return FlowResult(FlowStatus.ALREADY_DONE)
        context = await self.unit.get_context(session)
        p = context.position
        if (
            p != hand.owner
            or context.active_instances != hand.active_instances
            or p.lifecycle is not Lifecycle.PLAYING
            or p.phase is not Phase.ORDINARY
            or p.offer_id is not None
            or p.transition_id is not None
        ):
            return FlowResult(FlowStatus.STALE)
        from lets_play.storage.pacing_flow import RoundPacing

        pacing = await RoundPacing(self.unit).record(session)
        if pacing is not None:
            if pacing.stage != "selected":
                return FlowResult(FlowStatus.STALE)
            if pacing.picker_id != hand.picker_id:
                return FlowResult(FlowStatus.UNAUTHORIZED)
        # Recheck all three saved options; persist only frozen config/target IDs.
        decode_hand(encode_hand(hand))
        new = replace(
            p, advancement=Advancement.CHOOSING, offer_id=hand.offer_id, revision=p.revision + 1
        )
        await self.unit.set_owner(p, replace(context, position=new))
        saved = replace(hand, owner=new)
        await _execute(
            self.conn,
            "INSERT INTO nr_offers(session_id,offer_id,hand_json,prepared_at_utc,stage) VALUES (?,?,?,?,'prepared')",
            (session, hand.offer_id, encode_hand(saved), utc_int(frame.received_at)),
        )
        for instance in context.active_instances:
            if instance.card_id == "NR036":
                await _execute(
                    self.conn,
                    "INSERT INTO nr_used_phrases VALUES (?,?) ON CONFLICT(session_id,phrase_key) DO NOTHING",
                    (session, self._key(phrase_keys(instance))),
                )
        await self._output(session, hand.offer_id, "offer")
        return FlowResult(FlowStatus.PREPARED)

    async def _ack(self, session, offer, kind, message_id, observed):
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("A positive known output message ID is required.")
        row = await _row(
            self.conn,
            "SELECT * FROM nr_ordinary_outputs WHERE session_id=? AND offer_id=? AND kind=?",
            (session, offer, kind),
        )
        if row is None:
            raise ValueError("The publication has no prepared durable output.")
        if row["message_id"] is not None:
            if row["message_id"] != message_id:
                raise ValueError("A publication acknowledgement cannot change message identity.")
            return False
        await _execute(
            self.conn,
            "UPDATE nr_ordinary_outputs SET message_id=?,acknowledged_at_utc=? WHERE output_id=?",
            (message_id, observed, row["output_id"]),
        )
        return True

    @_write
    async def publish_offer(
        self, frame: EventEnvelope, offer_id: str, *, message_id: int
    ) -> FlowResult:
        if not await self._enter(frame, EventKind.OFFER_PUBLISHED, None):
            return FlowResult(FlowStatus.REPLAY)
        record = await self.get_offer(frame.route.session_id, offer_id)
        if record is None:
            return FlowResult(FlowStatus.INVALID)
        if record.stage != "prepared":
            # Known output retries are idempotent and cannot reset the clock.
            await self._ack(
                frame.route.session_id, offer_id, "offer", message_id, utc_int(frame.received_at)
            )
            return FlowResult(FlowStatus.ALREADY_DONE)
        current = await self.unit.get_context(frame.route.session_id)
        if current.position != record.hand.owner:
            return FlowResult(FlowStatus.STALE)
        await self._ack(
            frame.route.session_id, offer_id, "offer", message_id, utc_int(frame.received_at)
        )
        deadline = (
            record.hand.deadline
            if record.published_at is not None
            else replace(
                record.hand.deadline,
                expires_at=frame.received_at + timedelta(seconds=CHATGAMES_CHOICE_SECONDS),
            )
        )
        hand = replace(record.hand, deadline=deadline)
        await _execute(
            self.conn,
            "UPDATE nr_offers SET hand_json=?,published_at_utc=?,stage='published' WHERE session_id=? AND offer_id=?",
            (
                encode_hand(hand),
                utc_int(record.published_at or frame.received_at),
                frame.route.session_id,
                offer_id,
            ),
        )
        return FlowResult(FlowStatus.PUBLISHED)

    async def _completions(self, record, context, incoming_id, available):
        if (
            context.position != record.hand.owner
            or context.active_instances != record.hand.active_instances
        ):
            return ()
        option = next(
            (o for o in record.hand.options if o.incoming.instance_id == incoming_id), None
        )
        if option is None or option.incoming.card_id not in self._available(available):
            return ()
        if option.incoming.card_id == "NR036" and await self.used_keys(
            context.position.route.session_id, (phrase_keys(option.incoming),)
        ):
            return ()
        return tuple(
            c
            for c in legal_completions(context.active_instances, option.incoming)
            if c.outgoing_id in (None, *option.outgoing_ids)
        )

    @staticmethod
    def _available(available):
        if available is None:
            return frozenset(CARD_SPECS)
        if not isinstance(available, frozenset) or not available <= CARD_SPECS.keys():
            raise ValueError("Availability must be a frozen implemented-template set.")
        return available

    @_write
    async def choose(
        self,
        frame: EventEnvelope,
        offer_id: str,
        incoming_id: str,
        *,
        outgoing_id=None,
        available_templates=None,
    ) -> FlowResult:
        if not await self._enter(frame, EventKind.CHOICE_SUBMIT, available_templates):
            return FlowResult(FlowStatus.REPLAY)
        record = await self.get_offer(frame.route.session_id, offer_id)
        if record is None:
            return FlowResult(FlowStatus.INVALID)
        if record.stage != "published":
            return FlowResult(
                FlowStatus.ALREADY_DONE if record.stage != "prepared" else FlowStatus.UNAVAILABLE
            )
        if record.selected_incoming and record.selected_incoming != incoming_id:
            return FlowResult(FlowStatus.INVALID)
        current = await self.unit.get_context(frame.route.session_id)
        option = next(
            (o for o in record.hand.options if o.incoming.instance_id == incoming_id), None
        )
        keys = (
            (phrase_keys(option.incoming),) if option and option.incoming.card_id == "NR036" else ()
        )
        used = await self.used_keys(frame.route.session_id, keys)
        prepared = revalidate_choice(
            record.hand,
            current,
            incoming_id,
            now=frame.received_at,
            actor_id=frame.actor_id,
            outgoing_id=outgoing_id,
            used_phrases=used,
            available_templates=available_templates,
        )
        if prepared.status is ChoiceStatus.NEEDS_TARGET:
            await _execute(
                self.conn,
                "UPDATE nr_offers SET selected_incoming=?,chosen_by_picker=1 WHERE session_id=? AND offer_id=?",
                (incoming_id, frame.route.session_id, offer_id),
            )
            return FlowResult(FlowStatus.NEEDS_TARGET)
        if prepared.status is not ChoiceStatus.READY:
            status = {
                ChoiceStatus.UNAUTHORIZED: FlowStatus.UNAUTHORIZED,
                ChoiceStatus.EXPIRED: FlowStatus.EXPIRED,
                ChoiceStatus.STALE: FlowStatus.STALE,
                ChoiceStatus.INVALID: FlowStatus.INVALID,
            }.get(prepared.status, FlowStatus.UNAVAILABLE)
            return FlowResult(status)
        return await self._select(
            frame, record, current, incoming_id, prepared.completion.outgoing_id
        )

    @_write
    async def timeout(
        self, frame: EventEnvelope, offer_id: str, *, rng: RandomSource, available_templates=None
    ) -> FlowResult:
        if not await self._enter(frame, EventKind.CHOICE_TIMEOUT, available_templates):
            return FlowResult(FlowStatus.REPLAY)
        record = await self.get_offer(frame.route.session_id, offer_id)
        if record is None:
            return FlowResult(FlowStatus.INVALID)
        if record.stage != "published":
            return FlowResult(FlowStatus.ALREADY_DONE)
        if not record.hand.deadline.is_due(frame.received_at):
            return FlowResult(FlowStatus.NOT_DUE)
        current = await self.unit.get_context(frame.route.session_id)
        options = []
        for option in record.hand.options:
            if record.selected_incoming and option.incoming.instance_id != record.selected_incoming:
                continue
            completions = await self._completions(
                record, current, option.incoming.instance_id, available_templates
            )
            if completions:
                options.append((option, completions))
        if not options:
            # Refuse a fabricated fallback. Configured callers route relevant
            # capability/configuration changes through EvolutionFlow.recheck.
            return FlowResult(FlowStatus.UNAVAILABLE)
        option, completions = (
            options[rng.randrange(len(options))] if len(options) > 1 else options[0]
        )
        completion = (
            completions[rng.randrange(len(completions))] if len(completions) > 1 else completions[0]
        )
        return await self._select(
            frame, record, current, option.incoming.instance_id, completion.outgoing_id
        )

    async def _select(self, frame, record, context, incoming_id, outgoing_id):
        p = context.position
        transition = f"{record.hand.offer_id}:activation"
        new = replace(p, transition_id=transition, revision=p.revision + 1)
        await self.unit.set_owner(p, replace(context, position=new))
        hand = replace(record.hand, owner=new)
        effective = frame.received_at + timedelta(seconds=CHATGAMES_ACTIVATION_LEAD_SECONDS)
        # Discord timestamps have whole-second display precision. Announce the
        # exact stored boundary, with at least the configured publication lead.
        if effective.microsecond:
            effective = effective.replace(microsecond=0) + timedelta(seconds=1)
        await _execute(
            self.conn,
            "UPDATE nr_offers SET hand_json=?,selected_at_utc=?,selected_incoming=?,selected_outgoing=?,chosen_by_picker=MAX(chosen_by_picker,?),completed_by_timeout=?,stage='selected' WHERE session_id=? AND offer_id=?",
            (
                encode_hand(hand),
                utc_int(frame.received_at),
                incoming_id,
                outgoing_id,
                int(frame.kind is EventKind.CHOICE_SUBMIT),
                int(frame.kind is EventKind.CHOICE_TIMEOUT),
                frame.route.session_id,
                hand.offer_id,
            ),
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_activations VALUES (?,?,?,?,?,NULL,'prepared')",
            (
                frame.route.session_id,
                hand.offer_id,
                new.revision,
                f"{hand.offer_id}:round",
                utc_int(effective),
            ),
        )
        await self._output(frame.route.session_id, hand.offer_id, "activation")
        return FlowResult(FlowStatus.SELECTED)

    @_write
    async def publish_activation(
        self, frame: EventEnvelope, offer_id: str, *, message_id: int, available_templates=None
    ) -> FlowResult:
        if not await self._enter(frame, EventKind.ACTIVATION_PUBLISHED, available_templates):
            return FlowResult(FlowStatus.REPLAY)
        record = await self.get_offer(frame.route.session_id, offer_id)
        if record is None:
            return FlowResult(FlowStatus.INVALID)
        row = await _row(
            self.conn,
            "SELECT * FROM nr_activations WHERE session_id=? AND offer_id=?",
            (frame.route.session_id, offer_id),
        )
        if row is None:
            return FlowResult(FlowStatus.INVALID)
        if row["stage"] in ("recovery", "activated"):
            if row["stage"] == "activated":
                await self._ack(
                    frame.route.session_id,
                    offer_id,
                    "activation",
                    message_id,
                    utc_int(frame.received_at),
                )
            return FlowResult(
                FlowStatus.RECOVERY if row["stage"] == "recovery" else FlowStatus.ALREADY_DONE
            )
        if not await self._ack(
            frame.route.session_id, offer_id, "activation", message_id, utc_int(frame.received_at)
        ):
            return FlowResult(FlowStatus.ALREADY_DONE)
        await _execute(
            self.conn,
            "UPDATE nr_activations SET announced_at_utc=?,stage='announced' WHERE session_id=? AND offer_id=?",
            (utc_int(frame.received_at), frame.route.session_id, offer_id),
        )
        return FlowResult(FlowStatus.ANNOUNCED)

    async def _recover(self, frame, record):
        current = await self.unit.get_context(frame.route.session_id)
        p = current.position
        if p.lifecycle is Lifecycle.PLAYING:
            new = replace(
                p,
                lifecycle=Lifecycle.PAUSED,
                pause_reason=PauseReason.RECOVERY,
                revision=p.revision + 1,
            )
            await self.unit.set_owner(p, replace(current, position=new))
        await _execute(
            self.conn,
            "UPDATE nr_offers SET stage='recovery' WHERE session_id=? AND offer_id=?",
            (frame.route.session_id, record.hand.offer_id),
        )
        await _execute(
            self.conn,
            "UPDATE nr_activations SET stage='recovery' WHERE session_id=? AND offer_id=?",
            (frame.route.session_id, record.hand.offer_id),
        )
        await _execute(
            self.conn,
            """UPDATE nr_ordinary_outputs SET retired_at_utc=?
            WHERE session_id=? AND offer_id=? AND acknowledged_at_utc IS NULL
            AND retired_at_utc IS NULL""",
            (utc_int(frame.received_at), frame.route.session_id, record.hand.offer_id),
        )
        if p.lifecycle is not Lifecycle.ENDED:
            await self._output(frame.route.session_id, record.hand.offer_id, "recovery")

    async def _advance_due(self, frame, available):
        row = await _row(
            self.conn,
            "SELECT * FROM nr_activations WHERE session_id=? AND stage IN ('prepared','announced') AND effective_at_utc<=?",
            (frame.route.session_id, utc_int(frame.received_at)),
        )
        if row is None:
            return FlowStatus.NOT_DUE
        record = await self.get_offer(frame.route.session_id, row["offer_id"])
        current = await self.unit.get_context(frame.route.session_id)
        if (
            row["stage"] != "announced"
            or current.position.lifecycle is not Lifecycle.PLAYING
            or current.position.revision != row["owner_revision"]
        ):
            await self._recover(frame, record)
            return FlowStatus.RECOVERY
        completions = await self._completions(record, current, record.selected_incoming, available)
        completion = next(
            (c for c in completions if c.outgoing_id == record.selected_outgoing), None
        )
        if completion is None:
            await self._recover(frame, record)
            return FlowStatus.RECOVERY
        old = current.position
        new = replace(
            old,
            round_id=row["next_round_id"],
            ruleset_version=old.ruleset_version + 1,
            revision=old.revision + 1,
            advancement=Advancement.IDLE,
            offer_id=None,
            transition_id=None,
        )
        # Closure and general resolution share this unit; mode/theme work remains.
        await _execute(
            self.conn,
            "INSERT INTO nr_round_closures VALUES (?,?,?,?)",
            (frame.route.session_id, old.round_id, row["offer_id"], row["effective_at_utc"]),
        )
        await self.unit.set_owner(
            old, EvaluationContext(new, completion.instances, current.reaction_exclusions)
        )
        await _execute(
            self.conn,
            "UPDATE nr_rounds SET started_at_utc=? WHERE session_id=? AND round_id=?",
            (row["effective_at_utc"], frame.route.session_id, new.round_id),
        )
        incoming = next(
            i for i in completion.instances if i.instance_id == record.selected_incoming
        )
        if incoming.card_id == "NR036":
            await _execute(
                self.conn,
                "INSERT INTO nr_used_phrases VALUES (?,?)",
                (frame.route.session_id, self._key(phrase_keys(incoming))),
            )
        await _execute(
            self.conn,
            "UPDATE nr_offers SET stage='activated' WHERE session_id=? AND offer_id=?",
            (frame.route.session_id, row["offer_id"]),
        )
        await _execute(
            self.conn,
            "UPDATE nr_activations SET stage='activated' WHERE session_id=? AND offer_id=?",
            (frame.route.session_id, row["offer_id"]),
        )
        from lets_play.storage.progression import Progression

        await Progression(self.unit).resolve_round(frame.route.session_id, old.round_id)
        from lets_play.storage.pacing_flow import RoundPacing

        await RoundPacing(self.unit)._activated(
            frame.route.session_id, old.round_id, record.hand.picker_id, row["effective_at_utc"]
        )
        from lets_play.storage.evolution_flow import EvolutionFlow

        await EvolutionFlow(self.unit)._after_activation(frame)
        return FlowStatus.ACTIVATED

    @_write
    async def tick(self, frame: EventEnvelope, *, available_templates=None) -> FlowResult:
        # _enter advances the cutover; inspect its durable result afterwards.
        before = await _row(
            self.conn,
            "SELECT offer_id,stage FROM nr_activations WHERE session_id=? AND stage IN ('prepared','announced')",
            (frame.route.session_id,),
        )
        if not await self._enter(frame, EventKind.TIMER, available_templates):
            return FlowResult(FlowStatus.REPLAY)
        if before:
            after = await _row(
                self.conn,
                "SELECT stage FROM nr_activations WHERE session_id=? AND offer_id=?",
                (frame.route.session_id, before["offer_id"]),
            )
            if after["stage"] in ("activated", "recovery"):
                return FlowResult(FlowStatus(after["stage"]))
        return FlowResult(FlowStatus.NOT_DUE)

    @_write
    async def publish_recovery(
        self, frame: EventEnvelope, offer_id: str, *, message_id: int
    ) -> FlowResult:
        if not await self._enter(frame, EventKind.RECOVERY_PUBLISHED, None):
            return FlowResult(FlowStatus.REPLAY)
        record = await self.get_offer(frame.route.session_id, offer_id)
        if record is None or record.stage != "recovery":
            return FlowResult(FlowStatus.STALE)
        await self._ack(
            frame.route.session_id, offer_id, "recovery", message_id, utc_int(frame.received_at)
        )
        return FlowResult(FlowStatus.RECOVERY)

    @_write
    async def ingest(
        self, event: MessageEvent | ReactionEvent, *, available_templates=None
    ) -> FlowResult:
        if not isinstance(event, (MessageEvent, ReactionEvent)):
            raise TypeError("Only plain message/reaction event snapshots may enter ingress.")
        frame = event.envelope
        if not await self._enter(
            frame,
            EventKind.MESSAGE_CREATE if isinstance(event, MessageEvent) else EventKind.REACTION_ADD,
            available_templates,
        ):
            return FlowResult(FlowStatus.REPLAY)
        return await self._ingest_entered(event)

    async def _ingest_entered(self, event: MessageEvent | ReactionEvent) -> FlowResult:
        """Evaluate an already-ordered receipt, including an opening handover."""
        frame = event.envelope
        context = await self.unit.get_context(frame.route.session_id)
        boundary = await _row(
            self.conn,
            "SELECT resumed_at_utc FROM nr_recoveries WHERE session_id=? AND resumed_at_utc IS NOT NULL ORDER BY resumed_at_utc DESC LIMIT 1",
            (frame.route.session_id,),
        )
        if (
            context.position.lifecycle is not Lifecycle.ENDED
            and boundary is not None
            and utc_int(frame.created_at) < boundary[0]
        ):
            human = event.is_human if isinstance(event, MessageEvent) else not event.is_bot
            if human:
                await _execute(
                    self.conn,
                    "INSERT INTO nr_paused_sources VALUES (?,?,?) ON CONFLICT DO NOTHING",
                    frame.source_key,
                )
            return FlowResult(FlowStatus.IGNORED)
        if context.position.lifecycle is Lifecycle.PAUSED:
            human = event.is_human if isinstance(event, MessageEvent) else not event.is_bot
            if human:
                await _execute(
                    self.conn,
                    "INSERT INTO nr_paused_sources VALUES (?,?,?) ON CONFLICT DO NOTHING",
                    (frame.route.session_id, frame.kind, frame.source_id),
                )
            return FlowResult(FlowStatus.IGNORED)
        decision = (
            evaluate_message(event, context)
            if isinstance(event, MessageEvent)
            else evaluate_reaction(event, context)
        )
        if decision is None:
            return FlowResult(FlowStatus.IGNORED)
        self.unit._ordered_flow = True
        try:
            status = await self.unit.apply(decision)
        finally:
            self.unit._ordered_flow = False
        if isinstance(event, MessageEvent) and event.is_human and status is ApplyStatus.APPLIED:
            await _execute(
                self.conn,
                "INSERT INTO nr_session_activity VALUES (?,?) ON CONFLICT(session_id) DO UPDATE SET last_human_at_utc=excluded.last_human_at_utc",
                (frame.route.session_id, utc_int(frame.received_at)),
            )
        from lets_play.storage.pacing_flow import RoundPacing

        await RoundPacing(self.unit)._advance_due(frame)
        return FlowResult(FlowStatus.INGESTED, status)
