"""Atomic admission and publication-safe opening. No Discord I/O or fake rounds."""

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from random import Random

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.compatibility import legal_completions
from lets_play.new_rules.contracts import (
    Advancement,
    Deadline,
    DeadlineKind,
    EventKind,
    Lifecycle,
    MessageEvent,
    ReactionEvent,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext
from lets_play.new_rules.hands import ChoiceStatus, OrdinaryHand, build_hand, revalidate_choice
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.new_rules.text_rules import phrase_keys
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import decode_hand, encode_hand, utc_datetime, utc_int
from lets_play.storage.opening_codec import decode_config, encode_config
from lets_play.storage.ordinary_flow import (
    CHATGAMES_ACTIVATION_LEAD_SECONDS,
    CHATGAMES_CHOICE_SECONDS,
    FlowOrderError,
    FlowResult,
    FlowStatus,
    OrdinaryFlow,
)
from lets_play.storage.pacing_flow import RoundPacing
from lets_play.storage.progression import Progression

OPENING_VERSION = "opening-1"
LOBBY_SECONDS = 180


class OpeningStatus(StrEnum):
    ADMITTED = "admitted"
    JOINED = "joined"
    OPENING = "opening"
    PUBLISHED = "published"
    SELECTED = "selected"
    ACTIVE = "active"
    RECOVERY = "recovery"
    ENDED = "ended"
    CAPACITY = "capacity"
    CHANNEL_BUSY = "channel_busy"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"
    INVALID = "invalid"
    STALE = "stale"
    EXPIRED = "expired"
    NOT_DUE = "not_due"
    REPLAY = "replay"
    ALREADY_DONE = "already_done"
    IGNORED = "ignored"


@dataclass(frozen=True, slots=True)
class OpeningCapabilities:
    """Trusted adapter facts; can_mark includes actual marker usability."""

    can_send: bool
    can_embed: bool
    can_mark: bool
    ordinary_supported: bool = True

    def __post_init__(self):
        if any(
            type(v) is not bool
            for v in (self.can_send, self.can_embed, self.can_mark, self.ordinary_supported)
        ):
            raise ValueError("Capability facts must be booleans.")

    @property
    def available(self):
        return self.can_send and self.can_embed and self.can_mark and self.ordinary_supported


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    session_id: str
    stage: str
    starter_id: int | None
    picker_id: int | None
    phrase_draw_limit: int | None
    joined: int
    lobby_deadline: datetime | None
    offer_published_at: datetime | None
    effective_at: datetime | None
    hand: OrdinaryHand | None = field(repr=False)

    @property
    def choice_deadline(self):
        return self.hand.deadline if self.offer_published_at is not None else None

    @property
    def next_deadline(self):
        if self.stage == "lobby_published":
            return self.lobby_deadline
        if self.stage == "opening_published":
            return self.hand.deadline.expires_at
        if self.stage == "selected":
            return self.effective_at
        return None


@dataclass(frozen=True, slots=True)
class OpeningOutput:
    output_id: int
    session_id: str
    kind: str


def _id(value):
    if type(value) is not int or not 1 <= value < (1 << 63):
        raise ValueError("A positive SQLite-range human/message identity is required.")


class OpeningFlow:
    """One facade over the caller's transaction; results are provisional until commit."""

    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def _record(self, session):
        row = await _row(self.conn, "SELECT * FROM nr_admissions WHERE session_id=?", (session,))
        if row is not None and row["version"] != OPENING_VERSION:
            raise IncompatibleState("Admission version needs explicit recovery.")
        return row

    async def record(self, session):
        self.unit._check()
        row = await self._record(session)
        if row is None:
            return None
        count = await _row(
            self.conn, "SELECT COUNT(*) FROM nr_lobby_members WHERE session_id=?", (session,)
        )
        return AdmissionRecord(
            session,
            row["stage"],
            row["starter_id"],
            row["picker_id"],
            row["phrase_draw_limit"],
            count[0],
            utc_datetime(row["lobby_deadline_utc"])
            if row["lobby_deadline_utc"] is not None
            else None,
            utc_datetime(row["offer_published_at_utc"])
            if row["offer_published_at_utc"] is not None
            else None,
            utc_datetime(row["effective_at_utc"]) if row["effective_at_utc"] is not None else None,
            decode_hand(row["hand_json"]) if row["hand_json"] is not None else None,
        )

    async def pending_outputs(self, *, limit=100):
        self.unit._check()
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Page limit must be in [1,100].")
        async with self.conn.execute(
            """SELECT o.output_id,o.session_id,o.kind FROM nr_opening_outputs AS o
            JOIN nr_admissions AS a USING(session_id) WHERE o.acknowledged_at_utc IS NULL
            AND o.retired_at_utc IS NULL AND ((o.kind='lobby' AND a.stage='lobby_prepared')
            OR (o.kind='offer' AND a.stage='opening_prepared') OR (o.kind='activation' AND a.stage='selected')
            OR (o.kind='recovery' AND a.stage='recovery') OR (o.kind='closed' AND a.stage='ended'))
            ORDER BY o.output_id LIMIT ?""",
            (limit,),
        ) as cursor:
            return tuple(OpeningOutput(*r) for r in await cursor.fetchall())

    async def known_message_id(self, session, kind):
        self.unit._check()
        if kind not in {"lobby", "offer", "activation", "recovery", "closed"}:
            raise ValueError("Unknown opening output kind.")
        row = await _row(
            self.conn,
            "SELECT message_id FROM nr_opening_outputs WHERE session_id=? AND kind=?",
            (session, kind),
        )
        return row[0] if row else None

    async def _output(self, session, kind):
        await _execute(
            self.conn,
            "INSERT INTO nr_opening_outputs(session_id,kind) VALUES (?,?) ON CONFLICT(session_id,kind) DO NOTHING",
            (session, kind),
        )

    async def _premium(self, guild, now):
        row = await _row(
            self.conn,
            "SELECT premium_until_utc FROM nr_guild_entitlements WHERE guild_id=?",
            (guild,),
        )
        return row is not None and row[0] is not None and row[0] > utc_int(now)

    @staticmethod
    def _context(row, *, picker_revision=None):
        return EvaluationContext(
            SessionPosition(
                SessionRoute(row["guild_id"], row["channel_id"], row["session_id"]),
                Lifecycle.OPENING_CHOICE,
                advancement=Advancement.CHOOSING,
                offer_id=f"{row['session_id']}:opening",
                transition_id=f"{row['session_id']}:opening:activation"
                if row["stage"] == "selected"
                else None,
                revision=row["revision"] if picker_revision is None else picker_revision,
            ),
            (),
        )

    @_write
    async def admit(
        self, frame, *, capabilities, rng, is_human=True, phrase_bank=(), available_templates=None
    ):
        if frame.kind is not EventKind.LOBBY_CREATE or frame.sequence != 1:
            raise FlowOrderError("Admission starts with its original sequence-one request.")
        _id(frame.actor_id)
        if type(is_human) is not bool:
            raise ValueError("Human actor fact must be boolean.")
        if not is_human:
            return OpeningStatus.IGNORED
        if not isinstance(capabilities, OpeningCapabilities):
            raise TypeError("Normalized opening capabilities are required.")
        existing = await self._record(frame.route.session_id)
        if existing:
            if (
                existing["guild_id"],
                existing["channel_id"],
                existing["starter_id"],
                existing["create_source_id"],
            ) != (frame.route.guild_id, frame.route.channel_id, frame.actor_id, frame.source_id):
                return OpeningStatus.INVALID
            return (
                OpeningStatus.ENDED if existing["stage"] == "ended" else OpeningStatus.ALREADY_DONE
            )
        if await _row(
            self.conn,
            "SELECT 1 FROM nr_admissions WHERE guild_id=? AND create_source_id=?",
            (frame.route.guild_id, frame.source_id),
        ):
            return OpeningStatus.INVALID
        if await _row(
            self.conn,
            "SELECT 1 FROM nr_admissions WHERE channel_id=? AND stage<>'ended'",
            (frame.route.channel_id,),
        ):
            return OpeningStatus.CHANNEL_BUSY
        premium = await self._premium(frame.route.guild_id, frame.received_at)
        count = await _row(
            self.conn,
            "SELECT COUNT(*) FROM nr_admissions WHERE guild_id=? AND stage<>'ended'",
            (frame.route.guild_id,),
        )
        if count[0] >= (5 if premium else 1):
            return OpeningStatus.CAPACITY
        if not capabilities.available:
            return OpeningStatus.UNAVAILABLE
        templates = frozenset(CARD_SPECS) if available_templates is None else available_templates
        config = encode_config(phrase_bank, templates)
        seed = rng.randrange(1 << 63)
        if type(seed) is not int or not 0 <= seed < (1 << 63):
            raise ValueError("Invalid injected random seed.")
        # Preflight before reservation; no public hand or running choice clock yet.
        proto = {
            "guild_id": frame.route.guild_id,
            "channel_id": frame.route.channel_id,
            "session_id": frame.route.session_id,
            "revision": 0,
            "stage": "lobby_prepared",
        }
        hand = build_hand(
            self._context(proto),
            offer_id=f"{frame.route.session_id}:opening",
            picker_id=frame.actor_id,
            deadline=Deadline(
                "opening-choice", DeadlineKind.CHOICE, frame.received_at + timedelta(seconds=60)
            ),
            rng=Random(seed),
            phrase_bank=phrase_bank,
            phrase_draw_limit=15 if premium else 5,
            available_templates=templates,
        ).hand
        if hand is None:
            return OpeningStatus.UNAVAILABLE
        await _execute(
            self.conn,
            """INSERT INTO nr_admissions(session_id,guild_id,channel_id,starter_id,
            create_source_id,stage,phrase_draw_limit,random_seed,config_json,prepared_at_utc,last_sequence,observed_at_utc)
            VALUES (?,?,?,?,?,'lobby_prepared',?,?,?,?,1,?)""",
            (
                frame.route.session_id,
                frame.route.guild_id,
                frame.route.channel_id,
                frame.actor_id,
                frame.source_id,
                15 if premium else 5,
                seed,
                config,
                utc_int(frame.received_at),
                utc_int(frame.received_at),
            ),
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_lobby_members VALUES (?,?)",
            (frame.route.session_id, frame.actor_id),
        )
        await self._output(frame.route.session_id, "lobby")
        return OpeningStatus.ADMITTED

    async def _enter(self, frame, kind, *, available_templates=None, capabilities=None):
        if frame.kind is not kind or frame.sequence < 1:
            raise FlowOrderError("A positive ordered envelope of the correct kind is required.")
        row = await self._record(frame.route.session_id)
        if row is None or (row["guild_id"], row["channel_id"]) != (
            frame.route.guild_id,
            frame.route.channel_id,
        ):
            raise FlowOrderError("An exact existing admission route is required.")
        if row["playing_session_id"] is not None:
            return await OrdinaryFlow(self.unit)._enter(frame, kind, available_templates)
        if frame.sequence <= row["last_sequence"]:
            return False
        if frame.sequence != row["last_sequence"] + 1:
            raise FlowOrderError("An earlier receipt must commit first.")
        observed = utc_int(frame.received_at)
        if observed < row["observed_at_utc"]:
            raise FlowOrderError("Server receipt time regressed.")
        await _execute(
            self.conn,
            "UPDATE nr_admissions SET last_sequence=?,observed_at_utc=? WHERE session_id=?",
            (frame.sequence, observed, frame.route.session_id),
        )
        if row["stage"] == "lobby_published" and observed >= row["lobby_deadline_utc"]:
            await self._end(row, frame, "expired")
        if row["stage"] == "selected" and observed >= row["effective_at_utc"]:
            if row["announced_at_utc"] is None or not self._capable(capabilities):
                await self._recover(row, frame)
            else:
                completion = self._completion(row, available_templates)
                if completion is None:
                    await self._recover(row, frame)
                else:
                    await self._activate(row, frame, completion)
                    # Handover owns the SAME receipt; its body has not been scored.
                    return await OrdinaryFlow(self.unit)._enter(frame, kind, available_templates)
        return True

    @staticmethod
    def _capable(capabilities):
        if capabilities is None:
            return True
        if not isinstance(capabilities, OpeningCapabilities):
            raise TypeError("Normalized opening capabilities are required.")
        return capabilities.available

    def _completion(self, row, available):
        hand = decode_hand(row["hand_json"])
        _, saved = decode_config(row["config_json"])
        current = saved if available is None else OrdinaryFlow._available(available)
        incoming = next(
            (
                o.incoming
                for o in hand.options
                if o.incoming.instance_id == row["selected_incoming"]
            ),
            None,
        )
        if incoming is None or incoming.card_id not in current:
            return None
        return next((c for c in legal_completions((), incoming) if c.outgoing_id is None), None)

    async def _retire(self, session, now):
        await _execute(
            self.conn,
            "UPDATE nr_opening_outputs SET retired_at_utc=? WHERE session_id=? AND acknowledged_at_utc IS NULL AND retired_at_utc IS NULL",
            (utc_int(now), session),
        )

    async def _end(self, row, frame, reason):
        await _execute(
            self.conn,
            "UPDATE nr_admissions SET stage='ended',end_reason=?,revision=revision+1 WHERE session_id=?",
            (reason, row["session_id"]),
        )
        await self._retire(row["session_id"], frame.received_at)
        await self._output(row["session_id"], "closed")

    async def _recover(self, row, frame):
        await _execute(
            self.conn,
            "UPDATE nr_admissions SET stage='recovery',revision=revision+1 WHERE session_id=?",
            (row["session_id"],),
        )
        await self._retire(row["session_id"], frame.received_at)
        await self._output(row["session_id"], "recovery")

    async def _ack(self, row, frame, kind, message_id):
        _id(message_id)
        output = await _row(
            self.conn,
            "SELECT * FROM nr_opening_outputs WHERE session_id=? AND kind=?",
            (row["session_id"], kind),
        )
        if output is None:
            return OpeningStatus.STALE
        if output["message_id"] is not None:
            if output["message_id"] != message_id:
                raise ValueError("Publication acknowledgement cannot change message identity.")
            return OpeningStatus.ALREADY_DONE
        expected = {
            "lobby": "lobby_prepared",
            "offer": "opening_prepared",
            "activation": "selected",
            "recovery": "recovery",
            "closed": "ended",
        }[kind]
        if row["stage"] != expected or output["retired_at_utc"] is not None:
            return OpeningStatus.STALE
        observed = utc_int(frame.received_at)
        await _execute(
            self.conn,
            "UPDATE nr_opening_outputs SET message_id=?,acknowledged_at_utc=? WHERE output_id=?",
            (message_id, observed, output["output_id"]),
        )
        if kind == "lobby":
            await _execute(
                self.conn,
                "UPDATE nr_admissions SET stage='lobby_published',lobby_published_at_utc=?,lobby_deadline_utc=? WHERE session_id=?",
                (
                    observed,
                    utc_int(frame.received_at + timedelta(seconds=LOBBY_SECONDS)),
                    row["session_id"],
                ),
            )
        elif kind == "offer":
            hand = decode_hand(row["hand_json"])
            hand = replace(
                hand,
                deadline=replace(
                    hand.deadline,
                    expires_at=frame.received_at + timedelta(seconds=CHATGAMES_CHOICE_SECONDS),
                ),
            )
            await _execute(
                self.conn,
                "UPDATE nr_admissions SET stage='opening_published',offer_published_at_utc=?,hand_json=? WHERE session_id=?",
                (observed, encode_hand(hand), row["session_id"]),
            )
        elif kind == "activation":
            await _execute(
                self.conn,
                "UPDATE nr_admissions SET announced_at_utc=? WHERE session_id=?",
                (observed, row["session_id"]),
            )
        return OpeningStatus.PUBLISHED

    @_write
    async def publish(
        self, frame, kind, *, message_id, available_templates=None, capabilities=None
    ):
        expected = {
            "lobby": EventKind.LOBBY_PUBLISHED,
            "offer": EventKind.OFFER_PUBLISHED,
            "activation": EventKind.ACTIVATION_PUBLISHED,
            "recovery": EventKind.RECOVERY_PUBLISHED,
            "closed": EventKind.END_PUBLISHED,
        }
        if kind not in expected:
            raise ValueError("Unknown opening output kind.")
        if not await self._enter(
            frame,
            expected[kind],
            available_templates=available_templates,
            capabilities=capabilities,
        ):
            return OpeningStatus.REPLAY
        row = await self._record(frame.route.session_id)
        return await self._ack(row, frame, kind, message_id)

    @_write
    async def join(
        self, frame, *, is_human=True, capabilities=None, available_templates=None, phrase_bank=None
    ):
        if not await self._enter(frame, EventKind.LOBBY_JOIN):
            return OpeningStatus.REPLAY
        _id(frame.actor_id)
        if type(is_human) is not bool:
            raise ValueError("Human actor fact must be boolean.")
        if not is_human:
            return OpeningStatus.IGNORED
        row = await self._record(frame.route.session_id)
        if row["stage"] == "ended":
            return OpeningStatus.ENDED
        if row["stage"] != "lobby_published":
            return (
                OpeningStatus.ALREADY_DONE
                if row["stage"] != "lobby_prepared"
                else OpeningStatus.UNAVAILABLE
            )
        inserted = await _execute(
            self.conn,
            "INSERT INTO nr_lobby_members VALUES (?,?) ON CONFLICT(session_id,actor_id) DO NOTHING",
            (row["session_id"], frame.actor_id),
        )
        if not inserted:
            return OpeningStatus.ALREADY_DONE
        async with self.conn.execute(
            "SELECT actor_id FROM nr_lobby_members WHERE session_id=? ORDER BY actor_id LIMIT 4",
            (row["session_id"],),
        ) as cursor:
            joined = tuple(r[0] for r in await cursor.fetchall())
        if len(joined) < 3:
            return OpeningStatus.JOINED
        if len(joined) != 3:
            raise IncompatibleState("The opening quorum must contain exactly three humans.")
        if not self._capable(capabilities):
            await self._end(row, frame, "unavailable")
            return OpeningStatus.UNAVAILABLE
        saved_phrases, saved_templates = decode_config(row["config_json"])
        phrases = saved_phrases if phrase_bank is None else phrase_bank
        templates = (
            saved_templates
            if available_templates is None
            else OrdinaryFlow._available(available_templates)
        )
        config = encode_config(phrases, templates)
        limit = (
            15
            if await self._premium(row["guild_id"], frame.received_at)
            else row["phrase_draw_limit"]
        )
        rng = Random(row["random_seed"])
        picker = joined[rng.randrange(len(joined))]
        context = self._context(row, picker_revision=row["revision"] + 1)
        hand = build_hand(
            context,
            offer_id=f"{row['session_id']}:opening",
            picker_id=picker,
            deadline=Deadline(
                "opening-choice",
                DeadlineKind.CHOICE,
                frame.received_at + timedelta(seconds=CHATGAMES_CHOICE_SECONDS),
            ),
            rng=rng,
            phrase_bank=phrases,
            phrase_draw_limit=limit,
            available_templates=templates,
        ).hand
        if hand is None:
            await self._end(row, frame, "unavailable")
            return OpeningStatus.UNAVAILABLE
        await _execute(
            self.conn,
            "UPDATE nr_admissions SET stage='opening_prepared',revision=revision+1,picker_id=?,hand_json=?,config_json=?,phrase_draw_limit=?,offer_prepared_at_utc=? WHERE session_id=?",
            (
                picker,
                encode_hand(hand),
                config,
                limit,
                utc_int(frame.received_at),
                row["session_id"],
            ),
        )
        await self._output(row["session_id"], "offer")
        return OpeningStatus.OPENING

    @_write
    async def choose(self, frame, incoming_id, *, available_templates=None):
        if not await self._enter(
            frame, EventKind.CHOICE_SUBMIT, available_templates=available_templates
        ):
            return OpeningStatus.REPLAY
        row = await self._record(frame.route.session_id)
        if row["stage"] != "opening_published":
            return (
                OpeningStatus.ALREADY_DONE
                if row["stage"] in ("active", "selected", "ended")
                else OpeningStatus.UNAVAILABLE
            )
        hand = decode_hand(row["hand_json"])
        _, saved = decode_config(row["config_json"])
        prepared = revalidate_choice(
            hand,
            self._context(row),
            incoming_id,
            now=frame.received_at,
            actor_id=frame.actor_id,
            available_templates=saved if available_templates is None else available_templates,
        )
        if prepared.status is not ChoiceStatus.READY:
            return {
                ChoiceStatus.UNAUTHORIZED: OpeningStatus.UNAUTHORIZED,
                ChoiceStatus.EXPIRED: OpeningStatus.EXPIRED,
                ChoiceStatus.STALE: OpeningStatus.STALE,
            }.get(prepared.status, OpeningStatus.INVALID)
        await self._select(row, frame, incoming_id, human=True)
        return OpeningStatus.SELECTED

    @_write
    async def timeout(self, frame, *, rng, available_templates=None):
        if not await self._enter(
            frame, EventKind.CHOICE_TIMEOUT, available_templates=available_templates
        ):
            return OpeningStatus.REPLAY
        row = await self._record(frame.route.session_id)
        if row["stage"] != "opening_published":
            return OpeningStatus.ALREADY_DONE
        hand = decode_hand(row["hand_json"])
        if not hand.deadline.is_due(frame.received_at):
            return OpeningStatus.NOT_DUE
        _, saved = decode_config(row["config_json"])
        available = (
            saved if available_templates is None else OrdinaryFlow._available(available_templates)
        )
        options = tuple(
            o
            for o in hand.options
            if o.incoming.card_id in available and legal_completions((), o.incoming)
        )
        if not options:
            await self._end(row, frame, "unavailable")
            return OpeningStatus.UNAVAILABLE
        option = options[rng.randrange(len(options))]
        await self._select(row, frame, option.incoming.instance_id, human=False)
        return OpeningStatus.SELECTED

    async def _select(self, row, frame, incoming, human):
        effective = frame.received_at + timedelta(seconds=CHATGAMES_ACTIVATION_LEAD_SECONDS)
        if effective.microsecond:
            effective = effective.replace(microsecond=0) + timedelta(seconds=1)
        hand = decode_hand(row["hand_json"])
        owner = replace(
            hand.owner, revision=row["revision"] + 1, transition_id=f"{hand.offer_id}:activation"
        )
        await _execute(
            self.conn,
            "UPDATE nr_admissions SET stage='selected',revision=revision+1,hand_json=?,selected_incoming=?,chosen_by_picker=?,selected_at_utc=?,effective_at_utc=? WHERE session_id=?",
            (
                encode_hand(replace(hand, owner=owner)),
                incoming,
                int(human),
                utc_int(frame.received_at),
                utc_int(effective),
                row["session_id"],
            ),
        )
        await self._output(row["session_id"], "activation")

    async def _activate(self, row, frame, completion):
        session = row["session_id"]
        hand = decode_hand(row["hand_json"])
        context = EvaluationContext(
            SessionPosition(
                frame.route, Lifecycle.PLAYING, round_id=f"{hand.offer_id}:round", ruleset_version=1
            ),
            completion.instances,
        )
        self.unit._opening_activation = True
        try:
            await self.unit.insert_session(context)
        finally:
            self.unit._opening_activation = False
        await _execute(
            self.conn,
            "UPDATE nr_rounds SET started_at_utc=? WHERE session_id=? AND round_id=?",
            (row["effective_at_utc"], session, context.position.round_id),
        )
        await _execute(
            self.conn,
            "UPDATE nr_admissions SET stage='active',playing_session_id=?,revision=revision+1 WHERE session_id=?",
            (session, session),
        )
        async with self.conn.execute(
            "SELECT actor_id FROM nr_lobby_members WHERE session_id=? ORDER BY actor_id LIMIT 4",
            (session,),
        ) as cursor:
            joined = tuple(r[0] for r in await cursor.fetchall())
        if len(joined) != 3:
            raise IncompatibleState("Opening activation needs its preserved three-human quorum.")
        for actor in joined:
            await self.unit._add_score(session, context.position.round_id, actor, ScoreTotals())
        await _execute(
            self.conn,
            """INSERT INTO nr_offers(session_id,offer_id,hand_json,prepared_at_utc,published_at_utc,
            selected_at_utc,selected_incoming,chosen_by_picker,completed_by_timeout,stage)
            VALUES (?,?,?,?,?,?,?,?,?,'activated')""",
            (
                session,
                hand.offer_id,
                row["hand_json"],
                row["offer_prepared_at_utc"],
                row["offer_published_at_utc"],
                row["selected_at_utc"],
                row["selected_incoming"],
                row["chosen_by_picker"],
                int(not row["chosen_by_picker"]),
            ),
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_activations VALUES (?,?,?,?,?,?,'activated')",
            (
                session,
                hand.offer_id,
                hand.owner.revision,
                context.position.round_id,
                row["effective_at_utc"],
                row["announced_at_utc"],
            ),
        )
        async with self.conn.execute(
            "SELECT kind,message_id,acknowledged_at_utc FROM nr_opening_outputs WHERE session_id=? AND kind IN ('offer','activation')",
            (session,),
        ) as cursor:
            known = await cursor.fetchall()
        for output in known:
            await _execute(
                self.conn,
                "INSERT INTO nr_ordinary_outputs(session_id,offer_id,kind,message_id,acknowledged_at_utc) VALUES (?,?,?,?,?)",
                (session, hand.offer_id, *tuple(output)),
            )
        incoming = completion.instances[0]
        if incoming.card_id == "NR036":
            await _execute(
                self.conn,
                "INSERT INTO nr_used_phrases VALUES (?,?)",
                (session, OrdinaryFlow._key(phrase_keys(incoming))),
            )
        await Progression(self.unit).award_activated_choice(session, hand.offer_id)
        # First round is born here. No prior round closure or resolved award row.
        await RoundPacing(self.unit)._initialize(
            context, row["effective_at_utc"], len(joined), row["random_seed"], row["picker_id"]
        )
        from lets_play.storage.evolution_flow import EvolutionFlow

        phrases, templates = decode_config(row["config_json"])
        await EvolutionFlow(self.unit)._configure(
            context,
            phrase_bank=phrases,
            available_templates=templates,
            phrase_draw_limit=row["phrase_draw_limit"],
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_flow_watermarks VALUES (?,?,?)",
            (session, row["last_sequence"], row["observed_at_utc"]),
        )

    @_write
    async def tick(self, frame, *, available_templates=None, capabilities=None):
        if not await self._enter(
            frame,
            EventKind.TIMER,
            available_templates=available_templates,
            capabilities=capabilities,
        ):
            return OpeningStatus.REPLAY
        row = await self._record(frame.route.session_id)
        if row["playing_session_id"] is not None and row["stage"] != "ended":
            return OpeningStatus.ACTIVE
        return {"ended": OpeningStatus.ENDED, "recovery": OpeningStatus.RECOVERY}.get(
            row["stage"], OpeningStatus.NOT_DUE
        )

    @_write
    async def cancel(self, frame, *, authorized):
        if not await self._enter(frame, EventKind.MANAGER_END):
            return OpeningStatus.REPLAY
        if type(authorized) is not bool or not authorized or frame.actor_id is None:
            return OpeningStatus.UNAUTHORIZED
        row = await self._record(frame.route.session_id)
        if row["stage"] == "ended":
            return OpeningStatus.ALREADY_DONE
        if row["playing_session_id"] is not None:
            context = await self.unit.get_context(frame.route.session_id)
            owner = context.position
            ended = replace(
                owner, lifecycle=Lifecycle.ENDED, pause_reason=None, revision=owner.revision + 1
            )
            await self.unit.set_owner(owner, replace(context, position=ended))
            await Progression(self.unit).finalize_ended(row["session_id"])
        await self._end(row, frame, "manager")
        return OpeningStatus.ENDED

    @_write
    async def capability_changed(self, frame, capabilities):
        if not isinstance(capabilities, OpeningCapabilities):
            raise TypeError("Normalized opening capabilities are required.")
        if not await self._enter(frame, EventKind.CAPABILITY_CHANGED, capabilities=capabilities):
            return OpeningStatus.REPLAY
        if self._capable(capabilities):
            return OpeningStatus.NOT_DUE
        row = await self._record(frame.route.session_id)
        if row["stage"] == "ended":
            return OpeningStatus.ALREADY_DONE
        if row["playing_session_id"] is not None:
            return OpeningStatus.STALE
        if row["stage"] == "lobby_prepared":
            await self._end(row, frame, "unavailable")
            return OpeningStatus.ENDED
        if row["stage"] != "recovery":
            await self._recover(row, frame)
        return OpeningStatus.RECOVERY

    @_write
    async def ingest(self, event, *, available_templates=None, capabilities=None):
        if not isinstance(event, (MessageEvent, ReactionEvent)):
            raise TypeError("Plain message/reaction snapshots are required.")
        frame = event.envelope
        if not await self._enter(
            frame,
            EventKind.MESSAGE_CREATE if isinstance(event, MessageEvent) else EventKind.REACTION_ADD,
            available_templates=available_templates,
            capabilities=capabilities,
        ):
            return FlowResult(FlowStatus.REPLAY)
        row = await self._record(frame.route.session_id)
        if row["playing_session_id"] is not None:
            return await OrdinaryFlow(self.unit)._ingest_entered(event)
        duplicate = False
        if row["stage"] != "ended":
            inserted = await _execute(
                self.conn,
                "INSERT INTO nr_preplay_sources VALUES (?,?,?) ON CONFLICT(session_id,event_kind,source_id) DO NOTHING",
                frame.source_key,
            )
            duplicate = not inserted
        return FlowResult(FlowStatus.IGNORED, ApplyStatus.DUPLICATE_EVENT if duplicate else None)
