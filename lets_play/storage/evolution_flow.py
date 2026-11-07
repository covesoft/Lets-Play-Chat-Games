"""Held ordinary play, complete-hand refresh and publication-owned revival.

All actions share OrdinaryFlow's server receipt lane and the caller's transaction.
Outputs describe pending work; no method sends to Discord or infers delivery.
"""

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from random import Random

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.contracts import (
    Advancement,
    Deadline,
    DeadlineKind,
    EventKind,
    Lifecycle,
    PauseReason,
    Phase,
)
from lets_play.new_rules.hands import build_hand
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import utc_datetime, utc_int
from lets_play.storage.opening_codec import decode_config, encode_config
from lets_play.storage.ordinary_flow import FlowStatus, OrdinaryFlow
from lets_play.storage.pacing_flow import RoundPacing, _page

HELD_IDLE_SECONDS = 900
REVIVAL_SECONDS = 300
EVOLUTION_VERSION = "ordinary-evolution-1"


@dataclass(frozen=True, slots=True)
class RevivalRecord:
    revival_id: str
    round_id: str
    attempt: int
    stage: str
    published_at: datetime | None
    deadline: datetime | None
    joined: int
    picker_id: int | None


@dataclass(frozen=True, slots=True)
class EvolutionOutput:
    output_id: int
    session_id: str
    token: str
    kind: str


class EvolutionFlow:
    """Returned effects are provisional until the enclosing write unit commits."""

    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def next_deadline(self, session_id: str) -> datetime | None:
        """Known held/revival timer boundary; no clock is inferred from preparation."""
        self.unit._check()
        context = await self.unit.get_context(session_id)
        row = await self._configured(session_id)
        if context is None or row is None:
            return None
        p = context.position
        from lets_play.storage.recovery_flow import active_recovery

        if await active_recovery(self.unit, session_id):
            return None
        revival = await _row(
            self.conn,
            "SELECT * FROM nr_revivals WHERE session_id=? AND stage IN ('prepared','published')",
            (session_id,),
        )
        if revival is not None and self._owns_revival(revival, p):
            return (
                utc_datetime(revival["deadline_utc"])
                if revival["deadline_utc"] is not None
                else None
            )
        if (
            p.lifecycle is Lifecycle.PLAYING
            and p.phase is Phase.ORDINARY
            and p.advancement is Advancement.HELD
            and row["phase"] == "held"
        ):
            activity = await _row(
                self.conn,
                "SELECT last_human_at_utc FROM nr_session_activity WHERE session_id=?",
                (session_id,),
            )
            anchor = row["idle_anchor_at_utc"]
            if anchor is None:
                raise IncompatibleState("Held idle observation has no original anchor.")
            return utc_datetime(
                max(anchor, activity[0] if activity else anchor) + HELD_IDLE_SECONDS * 1_000_000
            )
        return None

    async def _configured(self, session):
        row = await _row(self.conn, "SELECT * FROM nr_evolution WHERE session_id=?", (session,))
        if row is not None and row["version"] != EVOLUTION_VERSION:
            raise IncompatibleState("Ordinary evolution version needs explicit recovery.")
        return row

    async def _configure(
        self, context, *, phrase_bank=None, available_templates=None, phrase_draw_limit=None
    ):
        p = context.position
        row = await self._configured(p.route.session_id)
        if row is None:
            admission = await _row(
                self.conn,
                "SELECT phrase_draw_limit FROM nr_admissions WHERE session_id=?",
                (p.route.session_id,),
            )
            phrases, templates = (), frozenset(CARD_SPECS)
            limit = admission[0] if admission else None
        else:
            phrases, templates = decode_config(row["config_json"])
            limit = row["phrase_draw_limit"]
        phrases = phrases if phrase_bank is None else phrase_bank
        templates = templates if available_templates is None else available_templates
        limit = limit if phrase_draw_limit is None else phrase_draw_limit
        if limit is not None and (type(limit) is not int or limit not in (5, 15)):
            raise ValueError("A trusted session phrase window is 5 or 15.")
        if phrases and limit is None:
            raise IncompatibleState("Reconcile the legacy session phrase allowance explicitly.")
        config = encode_config(phrases, templates)
        await _execute(
            self.conn,
            """INSERT INTO nr_evolution(session_id,round_id,config_json,phrase_draw_limit,phase)
            VALUES (?,?,?,?,'normal') ON CONFLICT(session_id) DO UPDATE SET
            config_json=excluded.config_json,phrase_draw_limit=excluded.phrase_draw_limit""",
            (p.route.session_id, p.round_id, config, limit),
        )
        return await self._configured(p.route.session_id)

    async def _build(self, frame, context, picker, offer_id):
        row = await self._configured(frame.route.session_id)
        if row is None:
            row = await self._configure(context)
        phrases, templates = decode_config(row["config_json"])
        if phrases and row["phrase_draw_limit"] is None:
            raise IncompatibleState("Reconcile the session phrase allowance explicitly.")
        used = await OrdinaryFlow(self.unit).used_keys(
            frame.route.session_id, tuple(p.key for p in phrases)
        )
        pacing = await RoundPacing(self.unit)._record(
            frame.route.session_id, context.position.round_id
        )
        if pacing is None:
            raise IncompatibleState("Held play requires a known ordinary round clock.")
        seed = self._seed(pacing["random_seed"], offer_id)
        result = build_hand(
            context,
            offer_id=offer_id,
            picker_id=picker,
            deadline=Deadline(
                f"{offer_id}:deadline",
                DeadlineKind.CHOICE,
                frame.received_at + timedelta(seconds=60),
            ),
            rng=Random(seed),
            phrase_bank=phrases,
            # With no phrases this value affects no draw and asserts no entitlement.
            phrase_draw_limit=row["phrase_draw_limit"] or 5,
            used_phrases=used,
            available_templates=templates,
        )
        return result.hand

    @staticmethod
    def _seed(seed, token):
        return int.from_bytes(sha256(f"{seed}:{token}".encode()).digest()[:8], "big") >> 1

    async def _new_attempt(self, frame, context, *, stage="waiting", revival_id=None):
        p = context.position
        pacing = RoundPacing(self.unit)
        row = await pacing._record(p.route.session_id, p.round_id)
        if row is None:
            raise IncompatibleState("A fresh attempt needs the original round clock.")
        await _execute(
            self.conn,
            "INSERT INTO nr_pacing_history VALUES (?,?,?,?,?)",
            (
                p.route.session_id,
                p.round_id,
                row["attempt"],
                json.dumps(dict(row), separators=(",", ":")),
                utc_int(frame.received_at),
            ),
        )
        await self._retire(frame, "nr_pacing_outputs")
        attempt = row["attempt"] + 1
        await _execute(
            self.conn,
            """UPDATE nr_round_pacing SET attempt=?,stage=?,owner_revision=?,
            target_reached_at_utc=?,warning_published_at_utc=NULL,selection_due_at_utc=NULL,
            picker_id=NULL,picker_cycle=NULL,random_seed=?,revival_id=? WHERE session_id=? AND round_id=?""",
            (
                attempt,
                stage,
                p.revision,
                utc_int(frame.received_at),
                self._seed(row["random_seed"], str(attempt)),
                revival_id,
                p.route.session_id,
                p.round_id,
            ),
        )
        return await pacing._record(p.route.session_id, p.round_id)

    async def _retire(self, frame, table):
        if table not in {"nr_pacing_outputs", "nr_ordinary_outputs", "nr_evolution_outputs"}:
            raise ValueError("Unknown output table.")
        await _execute(
            self.conn,
            f"UPDATE {table} SET retired_at_utc=? WHERE session_id=? AND acknowledged_at_utc IS NULL AND retired_at_utc IS NULL",
            (utc_int(frame.received_at), frame.route.session_id),
        )

    async def _hold(self, frame, context):
        p = context.position
        if (
            p.lifecycle is not Lifecycle.PLAYING
            or p.phase is not Phase.ORDINARY
            or p.transition_id is not None
        ):
            return "stale"
        row = await self._configured(p.route.session_id)
        if row is None:
            row = await self._configure(context)
        if p.advancement is Advancement.HELD and row["phase"] == "held":
            return "held"
        if p.offer_id is not None:
            await _execute(
                self.conn,
                "UPDATE nr_offers SET stage='cancelled' WHERE session_id=? AND offer_id=? AND stage IN ('prepared','published')",
                (p.route.session_id, p.offer_id),
            )
        await self._retire(frame, "nr_ordinary_outputs")
        await self._retire(frame, "nr_pacing_outputs")
        new = replace(
            p,
            advancement=Advancement.HELD,
            offer_id=None,
            transition_id=None,
            revision=p.revision + 1,
        )
        await self.unit.set_owner(p, replace(context, position=new))
        token = f"{p.round_id}:held:{frame.sequence}"
        await _execute(
            self.conn,
            "UPDATE nr_evolution SET phase='held',idle_anchor_at_utc=COALESCE(idle_anchor_at_utc,?),held_token=? WHERE session_id=?",
            (utc_int(frame.received_at), token, p.route.session_id),
        )
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET stage='cancelled',owner_revision=? WHERE session_id=? AND round_id=?",
            (new.revision, p.route.session_id, p.round_id),
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_evolution_outputs(session_id,token,kind) VALUES (?,?,'held') ON CONFLICT DO NOTHING",
            (p.route.session_id, token),
        )
        return "held"

    @_write
    async def recheck(
        self, frame, *, phrase_bank=None, available_templates=None, phrase_draw_limit=None
    ):
        """Relevant server configuration/capability facts, serialized before new choices."""
        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.ADVANCEMENT_RECHECK, None):
            return "replay"
        context = await self.unit.get_context(frame.route.session_id)
        p = context.position
        if p.lifecycle is Lifecycle.ENDED or p.phase is not Phase.ORDINARY:
            return "stale"
        row = await self._configure(
            context,
            phrase_bank=phrase_bank,
            available_templates=available_templates,
            phrase_draw_limit=phrase_draw_limit,
        )
        if p.lifecycle is not Lifecycle.PLAYING:
            # Recovery/capability pauses need explicit reconciliation, never auto-resume.
            return "paused"
        if p.transition_id is not None:
            offer = await OrdinaryFlow(self.unit).get_offer(p.route.session_id, p.offer_id)
            templates = decode_config(row["config_json"])[1]
            completions = await OrdinaryFlow(self.unit)._completions(
                offer, context, offer.selected_incoming, templates
            )
            if not any(c.outgoing_id == offer.selected_outgoing for c in completions):
                await OrdinaryFlow(self.unit)._recover(frame, offer)
                return "recovery"
            return "unchanged"
        if p.offer_id is not None:
            return await self._refresh_entered(frame, p.offer_id)
        hand = await self._build(frame, context, 1, "capacity-probe")
        if hand is None:
            result = await self._hold(frame, context)
            await self._advance_due(frame)
            return result
        if p.advancement is Advancement.HELD:
            new = replace(p, advancement=Advancement.IDLE, revision=p.revision + 1)
            context = replace(context, position=new)
            await self.unit.set_owner(p, context)
            await self._new_attempt(frame, context)
            await self._retire(frame, "nr_evolution_outputs")
            await _execute(
                self.conn,
                "UPDATE nr_evolution SET phase='restoring' WHERE session_id=?",
                (p.route.session_id,),
            )
            await RoundPacing(self.unit)._advance_due(frame)
            return "restoring"
        return "unchanged"

    @_write
    async def prepare_or_hold(self, frame, *, offer_id):
        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.OFFER_PREPARE, None):
            return "replay"
        context = await self.unit.get_context(frame.route.session_id)
        p = context.position
        if (
            p.lifecycle is not Lifecycle.PLAYING
            or p.phase is not Phase.ORDINARY
            or p.offer_id is not None
            or p.transition_id is not None
        ):
            return "stale"
        pacing = await RoundPacing(self.unit)._record(p.route.session_id, p.round_id)
        if pacing is None or pacing["stage"] != "selected":
            return "stale"
        hand = await self._build(frame, context, pacing["picker_id"], offer_id)
        if hand is None:
            return await self._hold(frame, context)
        result = await OrdinaryFlow(self.unit)._prepare_entered(frame, hand)
        if result.status is FlowStatus.PREPARED:
            await _execute(
                self.conn,
                "UPDATE nr_evolution SET phase='choosing' WHERE session_id=?",
                (p.route.session_id,),
            )
        return result.status.value

    @_write
    async def refresh_offer(self, frame, offer_id):
        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.OFFER_REFRESH, None):
            return "replay"
        return await self._refresh_entered(frame, offer_id)

    async def _refresh_entered(self, frame, offer_id):
        session = frame.route.session_id
        mapped = await _row(
            self.conn,
            "SELECT new_offer_id FROM nr_offer_refreshes WHERE session_id=? AND old_offer_id=?",
            (session, offer_id),
        )
        if mapped:
            return mapped[0]
        flow = OrdinaryFlow(self.unit)
        record = await flow.get_offer(session, offer_id)
        context = await self.unit.get_context(session)
        if (
            record is None
            or record.stage not in ("prepared", "published")
            or context.position != record.hand.owner
        ):
            return "stale"
        config = await self._configured(session)
        if config is None:
            config = await self._configure(context)
        templates = decode_config(config["config_json"])[1]
        if all(
            [
                await flow._completions(record, context, o.incoming.instance_id, templates)
                for o in record.hand.options
            ]
        ):
            return "unchanged"
        p = context.position
        new_id = f"{offer_id}:refresh:{frame.sequence}"
        new = replace(p, offer_id=None, revision=p.revision + 1)
        proposal = replace(context, position=new)
        hand = await self._build(frame, proposal, record.hand.picker_id, new_id)
        if hand is None:
            return await self._hold(frame, context)
        if record.published_at is not None:
            hand = replace(hand, deadline=record.hand.deadline)
        await _execute(
            self.conn,
            "UPDATE nr_offers SET stage='cancelled' WHERE session_id=? AND offer_id=?",
            (session, offer_id),
        )
        await self._retire(frame, "nr_ordinary_outputs")
        await self.unit.set_owner(p, proposal)
        result = await flow._prepare_entered(frame, hand)
        if result.status is not FlowStatus.PREPARED:
            raise IncompatibleState("A complete refresh could not claim its unchanged picker.")
        if record.published_at is not None:
            await _execute(
                self.conn,
                "UPDATE nr_offers SET published_at_utc=? WHERE session_id=? AND offer_id=?",
                (utc_int(record.published_at), session, new_id),
            )
        await _execute(
            self.conn, "INSERT INTO nr_offer_refreshes VALUES (?,?,?)", (session, offer_id, new_id)
        )
        return new_id

    async def _begin_revival(self, frame, context):
        p = context.position
        if (
            p.lifecycle is not Lifecycle.PLAYING
            or p.phase is not Phase.ORDINARY
            or p.transition_id is not None
        ):
            return
        await self._configure(context)
        await self._retire(frame, "nr_ordinary_outputs")
        if p.offer_id:
            await _execute(
                self.conn,
                "UPDATE nr_offers SET stage='cancelled' WHERE session_id=? AND offer_id=? AND stage IN ('prepared','published')",
                (p.route.session_id, p.offer_id),
            )
        new = replace(
            p,
            lifecycle=Lifecycle.PAUSED,
            pause_reason=PauseReason.REVIVAL,
            advancement=Advancement.IDLE,
            offer_id=None,
            transition_id=None,
            revision=p.revision + 1,
        )
        context = replace(context, position=new)
        await self.unit.set_owner(p, context)
        revival_id = f"{p.round_id}:revival:{frame.sequence}"
        pacing = await self._new_attempt(frame, context, stage="revival", revival_id=revival_id)
        await _execute(
            self.conn,
            "INSERT INTO nr_revivals(session_id,revival_id,round_id,attempt,owner_revision,prepared_at_utc,random_seed,stage) VALUES (?,?,?,?,?,?,?,'prepared')",
            (
                p.route.session_id,
                revival_id,
                p.round_id,
                pacing["attempt"],
                new.revision,
                utc_int(frame.received_at),
                pacing["random_seed"],
            ),
        )
        await _execute(
            self.conn,
            "UPDATE nr_evolution SET phase='revival' WHERE session_id=?",
            (p.route.session_id,),
        )
        await self._retire(frame, "nr_evolution_outputs")
        await RoundPacing(self.unit)._output(p.route.session_id, p.round_id, "revival")

    async def revival(self, session_id, revival_id=None):
        self.unit._check()
        if revival_id is None:
            row = await _row(
                self.conn,
                "SELECT * FROM nr_revivals WHERE session_id=? ORDER BY prepared_at_utc DESC,attempt DESC LIMIT 1",
                (session_id,),
            )
        else:
            row = await _row(
                self.conn,
                "SELECT * FROM nr_revivals WHERE session_id=? AND revival_id=?",
                (session_id, revival_id),
            )
        if row is None:
            return None
        count = await _row(
            self.conn,
            "SELECT COUNT(*) FROM nr_revival_members WHERE session_id=? AND revival_id=?",
            (session_id, row["revival_id"]),
        )
        return RevivalRecord(
            row["revival_id"],
            row["round_id"],
            row["attempt"],
            row["stage"],
            utc_datetime(row["published_at_utc"]) if row["published_at_utc"] is not None else None,
            utc_datetime(row["deadline_utc"]) if row["deadline_utc"] is not None else None,
            count[0],
            row["picker_id"],
        )

    @staticmethod
    def _message_id(message_id):
        if type(message_id) is not int or not 1 <= message_id < (1 << 63):
            raise ValueError("A positive known SQLite message ID is required.")

    @_write
    async def publish_revival(self, frame, revival_id, *, message_id):
        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.REVIVAL_PUBLISHED, None):
            return "replay"
        self._message_id(message_id)
        row = await _row(
            self.conn,
            "SELECT * FROM nr_revivals WHERE session_id=? AND revival_id=?",
            (frame.route.session_id, revival_id),
        )
        if row is None or row["stage"] not in ("prepared", "published"):
            return "stale"
        output = await _row(
            self.conn,
            "SELECT * FROM nr_pacing_outputs WHERE session_id=? AND round_id=? AND attempt=? AND kind='revival'",
            (frame.route.session_id, row["round_id"], row["attempt"]),
        )
        if output is None or output["retired_at_utc"] is not None:
            return "stale"
        if output["message_id"] is not None:
            if output["message_id"] != message_id:
                raise ValueError("A revival acknowledgement cannot change message identity.")
            return "already_done"
        await _execute(
            self.conn,
            "UPDATE nr_pacing_outputs SET message_id=?,acknowledged_at_utc=? WHERE output_id=?",
            (message_id, utc_int(frame.received_at), output["output_id"]),
        )
        await _execute(
            self.conn,
            "UPDATE nr_revivals SET stage='published',published_at_utc=?,deadline_utc=? WHERE session_id=? AND revival_id=?",
            (
                utc_int(frame.received_at),
                utc_int(frame.received_at + timedelta(seconds=REVIVAL_SECONDS)),
                frame.route.session_id,
                revival_id,
            ),
        )
        return "published"

    @_write
    async def join_revival(self, frame, revival_id, *, is_human=True):
        if not await OrdinaryFlow(self.unit)._enter(frame, EventKind.REVIVAL_JOIN, None):
            return "replay"
        if type(is_human) is not bool:
            raise ValueError("Human identity is a trusted boolean fact.")
        if not is_human or frame.actor_id is None:
            return "ignored"
        row = await _row(
            self.conn,
            "SELECT * FROM nr_revivals WHERE session_id=? AND revival_id=?",
            (frame.route.session_id, revival_id),
        )
        if row is None or row["stage"] != "published":
            return "stale"
        context = await self.unit.get_context(frame.route.session_id)
        if not self._owns_revival(row, context.position):
            return "stale"
        inserted = await _execute(
            self.conn,
            "INSERT INTO nr_revival_members VALUES (?,?,?) ON CONFLICT DO NOTHING",
            (frame.route.session_id, revival_id, frame.actor_id),
        )
        if not inserted:
            return "already_done"
        async with self.conn.execute(
            "SELECT actor_id FROM nr_revival_members WHERE session_id=? AND revival_id=? ORDER BY actor_id LIMIT 4",
            (frame.route.session_id, revival_id),
        ) as cursor:
            members = tuple(r[0] for r in await cursor.fetchall())
        if len(members) < 3:
            return "joined"
        if len(members) != 3:
            raise IncompatibleState("Revival quorum must freeze exactly three human returners.")
        picker = members[Random(row["random_seed"]).randrange(3)]
        await _execute(
            self.conn,
            "UPDATE nr_revivals SET stage='quorum',picker_id=? WHERE session_id=? AND revival_id=?",
            (picker, frame.route.session_id, revival_id),
        )
        p = context.position
        new = replace(
            p,
            lifecycle=Lifecycle.PLAYING,
            pause_reason=None,
            advancement=Advancement.CHOOSING,
            revision=p.revision + 1,
        )
        context = replace(context, position=new)
        await self.unit.set_owner(p, context)
        await _execute(
            self.conn,
            "UPDATE nr_evolution SET idle_anchor_at_utc=?,phase='choosing' WHERE session_id=?",
            (utc_int(frame.received_at), frame.route.session_id),
        )
        rotation = await _row(
            self.conn, "SELECT cycle FROM nr_rotation WHERE session_id=?", (frame.route.session_id,)
        )
        for actor in members:
            await _execute(
                self.conn,
                "INSERT INTO nr_rotation_members(session_id,actor_id) VALUES (?,?) ON CONFLICT DO NOTHING",
                (frame.route.session_id, actor),
            )
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET stage='selected',picker_id=?,picker_cycle=?,owner_revision=?,next_estimate=3 WHERE session_id=? AND round_id=?",
            (picker, rotation[0], new.revision, frame.route.session_id, p.round_id),
        )
        offer_id = f"{revival_id}:offer"
        hand = await self._build(frame, context, picker, offer_id)
        if hand is None:
            await self._hold(frame, context)
            return "held"
        result = await OrdinaryFlow(self.unit)._prepare_entered(frame, hand)
        if result.status is not FlowStatus.PREPARED:
            raise IncompatibleState("Revival cannot prepare its frozen returner's hand.")
        return "quorum"

    @staticmethod
    def _owns_revival(row, p):
        return (
            p.lifecycle is Lifecycle.PAUSED
            and p.pause_reason is PauseReason.REVIVAL
            and p.round_id == row["round_id"]
            and p.revision == row["owner_revision"]
        )

    async def _advance_due(self, frame):
        context = await self.unit.get_context(frame.route.session_id)
        p = context.position
        row = await self._configured(p.route.session_id)
        if row is None:
            return
        revival = await _row(
            self.conn,
            "SELECT * FROM nr_revivals WHERE session_id=? AND stage IN ('prepared','published')",
            (p.route.session_id,),
        )
        if p.lifecycle is Lifecycle.ENDED:
            if row["phase"] != "ended":
                await self._retire(frame, "nr_pacing_outputs")
                await self._retire(frame, "nr_ordinary_outputs")
                await self._retire(frame, "nr_evolution_outputs")
                await _execute(
                    self.conn,
                    "UPDATE nr_revivals SET stage='cancelled' WHERE session_id=? AND stage IN ('prepared','published')",
                    (p.route.session_id,),
                )
                await _execute(
                    self.conn,
                    "UPDATE nr_evolution SET phase='ended' WHERE session_id=?",
                    (p.route.session_id,),
                )
            return
        if revival is not None:
            if not self._owns_revival(revival, p):
                await _execute(
                    self.conn,
                    "UPDATE nr_revivals SET stage='cancelled' WHERE session_id=? AND revival_id=?",
                    (p.route.session_id, revival["revival_id"]),
                )
                await self._retire(frame, "nr_pacing_outputs")
                return
            if (
                revival["deadline_utc"] is not None
                and utc_int(frame.received_at) >= revival["deadline_utc"]
            ):
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

                await Progression(self.unit).finalize_ended(p.route.session_id)
                await _execute(
                    self.conn,
                    "UPDATE nr_revivals SET stage='expired' WHERE session_id=? AND revival_id=?",
                    (p.route.session_id, revival["revival_id"]),
                )
                await self._retire(frame, "nr_pacing_outputs")
                await self._retire(frame, "nr_evolution_outputs")
                await _execute(
                    self.conn,
                    "UPDATE nr_evolution SET phase='ended' WHERE session_id=?",
                    (p.route.session_id,),
                )
                await _execute(
                    self.conn,
                    "INSERT INTO nr_evolution_outputs(session_id,token,kind) VALUES (?,?,'closed_silent') ON CONFLICT DO NOTHING",
                    (p.route.session_id, revival["revival_id"]),
                )
            return
        if (
            p.lifecycle is Lifecycle.PLAYING
            and p.phase is Phase.ORDINARY
            and p.advancement is Advancement.HELD
            and row["phase"] == "held"
        ):
            if frame.received_at >= await self.next_deadline(p.route.session_id):
                await self._begin_revival(frame, context)

    async def _after_activation(self, frame):
        context = await self.unit.get_context(frame.route.session_id)
        await _execute(
            self.conn,
            "UPDATE nr_evolution SET round_id=?,phase='normal',idle_anchor_at_utc=NULL,held_token=NULL WHERE session_id=?",
            (context.position.round_id, frame.route.session_id),
        )
        await self._retire(frame, "nr_evolution_outputs")

    async def pending_outputs(self, *, limit=100):
        self.unit._check()
        _page(limit)
        async with self.conn.execute(
            """SELECT o.output_id,o.session_id,o.token,o.kind FROM nr_evolution_outputs o
            JOIN nr_sessions s USING(session_id) JOIN nr_evolution e USING(session_id)
            WHERE o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
            AND NOT EXISTS(SELECT 1 FROM nr_recoveries r WHERE r.session_id=s.session_id AND r.stage IN ('paused','ready','reading')) AND
            ((o.kind='held' AND s.lifecycle='playing' AND s.advancement='held' AND e.phase='held' AND e.held_token=o.token)
            OR (o.kind='closed_silent' AND s.lifecycle='ended')) ORDER BY o.output_id LIMIT ?""",
            (limit,),
        ) as cursor:
            return tuple(EvolutionOutput(*r) for r in await cursor.fetchall())

    @_write
    async def publish_notice(self, frame, token, *, message_id):
        kind = {
            EventKind.HELD_PUBLISHED: "held",
            EventKind.SILENT_END_PUBLISHED: "closed_silent",
        }.get(frame.kind)
        if kind is None:
            raise ValueError("A typed held/silent-end acknowledgement is required.")
        if not await OrdinaryFlow(self.unit)._enter(frame, frame.kind, None):
            return "replay"
        self._message_id(message_id)
        output = await _row(
            self.conn,
            "SELECT * FROM nr_evolution_outputs WHERE session_id=? AND token=? AND kind=?",
            (frame.route.session_id, token, kind),
        )
        if output is None or output["retired_at_utc"] is not None:
            return "stale"
        if output["message_id"] is not None:
            if output["message_id"] != message_id:
                raise ValueError("A notice acknowledgement cannot change message identity.")
            return "already_done"
        await _execute(
            self.conn,
            "UPDATE nr_evolution_outputs SET message_id=?,acknowledged_at_utc=? WHERE output_id=?",
            (message_id, utc_int(frame.received_at), output["output_id"]),
        )
        return "published"
