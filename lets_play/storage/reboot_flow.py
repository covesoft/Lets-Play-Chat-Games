"""Automatic recovery of acknowledged game phases through the existing engine.

Known warnings, choices and announced transitions keep their original clocks.
Unpublished work, modes and unknown sends stay fenced. A clean reboot needs no
human approval. Network work belongs to the adapter.
"""

from lets_play.new_rules.contracts import EventKind
from lets_play.storage.evolution_flow import REVIVAL_SECONDS
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.opening_flow import LOBBY_SECONDS, OpeningFlow
from lets_play.storage.ordinary_flow import CHATGAMES_CHOICE_SECONDS, OrdinaryFlow
from lets_play.storage.recovery_flow import RECOVERY_VERSION, RecoveryFlow, active_recovery


class RebootFlow:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def candidate(self, session):
        self.unit._check()
        row = await active_recovery(self.unit, session)
        if row is None or row["version"] != RECOVERY_VERSION:
            return None
        context = await self.unit.get_context(session)
        if context is None:
            if not await self._known_opening(session, row):
                return None
        else:
            p = context.position
            if (
                row["original_stage"] != "active"
                or p.phase != "ordinary"
                or p.lifecycle != "paused"
                or p.pause_reason != "recovery"
                or p.round_id != row["original_round"]
                or p.revision != row["owner_revision"]
            ):
                return None
            if row["original_pause"] == "revival" and row["original_lifecycle"] == "paused":
                if not await self._known_revival(session, p):
                    return None
            elif (
                row["original_pause"] is not None
                or row["original_lifecycle"] != "playing"
                or not await self._known_phase(session, p)
            ):
                return None
        # Existence checks are scoped independently of every visible work page.
        # A delivered engine record never clears an unrelated adapter claim.
        for table in (
            "nr_opening_outputs",
            "nr_ordinary_outputs",
            "nr_pacing_outputs",
            "nr_evolution_outputs",
        ):
            if await _row(
                self.conn,
                f"SELECT 1 FROM {table} WHERE session_id=? "
                "AND acknowledged_at_utc IS NULL AND retired_at_utc IS NULL LIMIT 1",
                (session,),
            ):
                return None
        for query, args in (
            (
                "SELECT 1 FROM nr_recovery_work WHERE session_id=? AND recovery_id=? "
                "AND status='unknown' LIMIT 1",
                (session, row["recovery_id"]),
            ),
            (
                "SELECT 1 FROM nr_discord_deliveries WHERE session_id=? AND "
                "(state<>'delivered' OR message_id IS NULL OR ack_sequence=0) LIMIT 1",
                (session,),
            ),
            (
                "SELECT 1 FROM nr_summary_outputs WHERE session_id=? AND "
                "(state IN ('sending','uncertain') OR (state='prepared' AND "
                "(attempted_at_utc IS NOT NULL OR message_id IS NOT NULL OR delivered_at_utc IS NOT NULL)) "
                "OR (state='delivered' AND (message_id IS NULL OR delivered_at_utc IS NULL))) LIMIT 1",
                (session,),
            ),
            (
                "SELECT 1 FROM nr_round_reports WHERE session_id=? AND "
                "(state<>'delivered' OR message_id IS NULL OR delivered_at_utc IS NULL) LIMIT 1",
                (session,),
            ),
            (
                "SELECT 1 FROM nr_recoveries WHERE session_id=? AND "
                "((notice_attempted_at_utc IS NOT NULL AND notice_message_id IS NULL) OR "
                "(notice_message_id IS NOT NULL AND notice_published_at_utc IS NULL)) "
                "AND recovery_id<>? LIMIT 1",
                (session, row["recovery_id"]),
            ),
        ):
            if await _row(self.conn, query, args):
                return None
        if row["stage"] == "ready" and row["notice_attempted_at_utc"] is not None:
            return None  # An in-flight/lost send is never a fresh attempt.
        if row["stage"] == "reading" and (
            row["notice_attempted_at_utc"] is None
            or row["notice_message_id"] is None
            or row["notice_published_at_utc"] is None
            or row["resume_at_utc"] is None
            or row["notice_ack_sequence"] == 0
        ):
            return None
        return row

    async def _publication(self, session, family, condition, args):
        table = {
            "opening": "nr_opening_outputs",
            "ordinary": "nr_ordinary_outputs",
            "pacing": "nr_pacing_outputs",
            "evolution": "nr_evolution_outputs",
        }[family]
        return await _row(
            self.conn,
            f"SELECT o.message_id,o.acknowledged_at_utc FROM {table} o "
            "JOIN nr_discord_deliveries d ON d.session_id=o.session_id "
            "AND d.output_id=o.output_id AND d.family=? WHERE o.session_id=? "
            f"AND {condition} AND o.message_id IS NOT NULL AND o.acknowledged_at_utc IS NOT NULL "
            "AND o.retired_at_utc IS NULL AND d.state='delivered' "
            "AND d.message_id=o.message_id AND d.ack_sequence>0 LIMIT 1",
            (family, session, *args),
        )

    async def _known_opening(self, session, owner):
        flow = OpeningFlow(self.unit)
        row = await flow._record(session)
        stage = row["stage"]
        if (
            stage != owner["original_stage"]
            or row["revision"] != owner["owner_revision"]
            or owner["original_lifecycle"] is not None
        ):
            return False
        kind = {
            "lobby_published": "lobby",
            "opening_published": "offer",
            "selected": "activation",
        }.get(stage)
        if kind is None:
            return False
        proof = await self._publication(session, "opening", "o.kind=?", (kind,))
        if proof is None:
            return False
        published = proof["acknowledged_at_utc"]
        if kind == "lobby":
            return (
                row["lobby_published_at_utc"] == published
                and row["lobby_deadline_utc"] == published + LOBBY_SECONDS * 1_000_000
            )
        if kind == "offer":
            offer = await flow.record(session)
            return bool(
                row["offer_published_at_utc"] == published
                and offer.choice_deadline
                and utc_int(offer.choice_deadline.expires_at)
                == published + CHATGAMES_CHOICE_SECONDS * 1_000_000
            )
        return (
            row["announced_at_utc"] == published
            and row["effective_at_utc"] is not None
            and published < row["effective_at_utc"]
        )

    async def _known_revival(self, session, position):
        if position.advancement != "idle" or position.offer_id or position.transition_id:
            return False
        row = await _row(
            self.conn,
            "SELECT r.* FROM nr_revivals r JOIN nr_round_pacing p "
            "ON p.session_id=r.session_id AND p.round_id=r.round_id AND p.attempt=r.attempt "
            "JOIN nr_evolution e ON e.session_id=r.session_id AND e.round_id=r.round_id "
            "WHERE r.session_id=? AND r.round_id=? AND r.stage='published' "
            "AND p.stage='revival' AND p.owner_revision=r.owner_revision AND e.phase='revival'",
            (session, position.round_id),
        )
        if row is None or row["owner_revision"] >= position.revision:
            return False
        proof = await self._publication(
            session,
            "pacing",
            "o.round_id=? AND o.attempt=? AND o.kind='revival'",
            (position.round_id, row["attempt"]),
        )
        return bool(
            proof
            and row["published_at_utc"] == proof["acknowledged_at_utc"]
            and row["deadline_utc"] == row["published_at_utc"] + REVIVAL_SECONDS * 1_000_000
        )

    async def _known_phase(self, session, position):
        pacing = await _row(
            self.conn,
            "SELECT stage,attempt,warning_published_at_utc,selection_due_at_utc "
            "FROM nr_round_pacing WHERE session_id=? AND round_id=?",
            (session, position.round_id),
        )
        if pacing is None:
            return False
        if position.advancement == "held":
            if position.offer_id or position.transition_id or pacing["stage"] != "cancelled":
                return False
            held = await _row(
                self.conn,
                "SELECT held_token,idle_anchor_at_utc FROM nr_evolution "
                "WHERE session_id=? AND round_id=? AND phase='held'",
                (session, position.round_id),
            )
            return bool(
                held
                and held["idle_anchor_at_utc"] is not None
                and await self._publication(
                    session, "evolution", "o.token=? AND o.kind='held'", (held["held_token"],)
                )
            )
        if position.advancement == "idle":
            return (
                not position.offer_id
                and not position.transition_id
                and pacing["stage"] == "waiting"
            )
        if position.advancement == "warning":
            proof = await self._publication(
                session,
                "pacing",
                "o.round_id=? AND o.attempt=? AND o.kind='warning'",
                (position.round_id, pacing["attempt"]),
            )
            return bool(
                not position.offer_id
                and not position.transition_id
                and pacing["stage"] == "warning_published"
                and proof
                and pacing["warning_published_at_utc"] == proof["acknowledged_at_utc"]
                and pacing["selection_due_at_utc"] is not None
                and pacing["selection_due_at_utc"] > pacing["warning_published_at_utc"]
            )
        if (
            position.advancement != "choosing"
            or not position.offer_id
            or pacing["stage"] != "selected"
        ):
            return False
        offer = await OrdinaryFlow(self.unit).get_offer(session, position.offer_id)
        if (
            offer is None
            or offer.stage not in ("published", "selected")
            or offer.published_at is None
        ):
            return False
        proof = await self._publication(
            session, "ordinary", "o.offer_id=? AND o.kind='offer'", (position.offer_id,)
        )
        if proof is None or proof["acknowledged_at_utc"] < utc_int(offer.published_at):
            return False
        options = {o.incoming.instance_id: o for o in offer.hand.options}
        if offer.selected_incoming is not None and offer.selected_incoming not in options:
            return False
        if offer.stage == "published":
            return not position.transition_id and offer.selected_outgoing is None
        activation = await _row(
            self.conn,
            "SELECT stage,announced_at_utc,effective_at_utc FROM nr_activations "
            "WHERE session_id=? AND offer_id=?",
            (session, position.offer_id),
        )
        proof = await self._publication(
            session, "ordinary", "o.offer_id=? AND o.kind='activation'", (position.offer_id,)
        )
        return bool(
            position.transition_id == position.offer_id + ":activation"
            and offer.selected_incoming in options
            and activation
            and activation["stage"] == "announced"
            and proof
            and activation["announced_at_utc"] == proof["acknowledged_at_utc"]
            and activation["announced_at_utc"] < activation["effective_at_utc"]
        )

    @_write
    async def prepare(self, frame, recovery, *, capabilities, templates):
        row = await self.candidate(frame.route.session_id)
        if row is None or row["recovery_id"] != recovery or row["stage"] != "paused":
            # Consume stale/rejected control in the same ordered barrier.
            await RecoveryFlow(self.unit).observe(frame, recovery, allow_resume=False)
            return "blocked"
        return await RecoveryFlow(self.unit).approve(
            frame, recovery, capabilities=capabilities, available_templates=templates
        )

    @_write
    async def claim(self, frame, recovery):
        flow = RecoveryFlow(self.unit)
        row, error = await flow._enter(frame, EventKind.RECOVERY_NOTICE_CLAIM, recovery)
        if error:
            return False
        current = await self.candidate(frame.route.session_id)
        if current is None or current["recovery_id"] != recovery or row["stage"] != "ready":
            return False
        return bool(
            await _execute(
                self.conn,
                "UPDATE nr_recoveries SET notice_attempted_at_utc=? WHERE session_id=? "
                "AND recovery_id=? AND stage='ready' AND notice_attempted_at_utc IS NULL "
                "AND notice_message_id IS NULL",
                (utc_int(frame.received_at), frame.route.session_id, recovery),
            )
        )

    @_write
    async def acknowledge(self, frame, recovery, message_id):
        row = await _row(
            self.conn,
            "SELECT * FROM nr_recoveries WHERE session_id=? AND recovery_id=?",
            (frame.route.session_id, recovery),
        )
        if row is None or row["notice_attempted_at_utc"] is None:
            raise IncompatibleState("A recovery notice ACK requires its durable send claim.")
        if row["notice_message_id"] not in (None, message_id):
            raise IncompatibleState("A recovery notice cannot change message identity.")
        result = await RecoveryFlow(self.unit).publish_notice(
            frame, recovery, message_id=message_id
        )
        # Retain known success even if manager ending or another disruption won
        # while HTTP was running. It must never revive the superseded generation.
        if result in ("closed", "stale"):
            await _execute(
                self.conn,
                "UPDATE nr_recoveries SET notice_message_id=COALESCE(notice_message_id,?),"
                "notice_published_at_utc=COALESCE(notice_published_at_utc,?) "
                "WHERE session_id=? AND recovery_id=?",
                (message_id, utc_int(frame.received_at), frame.route.session_id, recovery),
            )
        await _execute(
            self.conn,
            "UPDATE nr_recoveries SET notice_ack_sequence=MAX(notice_ack_sequence,?) "
            "WHERE session_id=? AND recovery_id=?",
            (frame.sequence, frame.route.session_id, recovery),
        )
        return result

    @_write
    async def block(self, frame, recovery):
        result = await RecoveryFlow(self.unit).observe(frame, recovery, allow_resume=False)
        # Keep publication/deadline proof, but stop displaying a promised read-in
        # once current access or another guard has actually failed.
        await _execute(
            self.conn,
            "UPDATE nr_recoveries SET stage='paused' WHERE session_id=? AND recovery_id=? AND stage='reading'",
            (frame.route.session_id, recovery),
        )
        return result

    @_write
    async def resume(self, frame, recovery, *, capabilities):
        row = await self.candidate(frame.route.session_id)
        allowed = bool(
            row
            and row["recovery_id"] == recovery
            and row["stage"] == "reading"
            and capabilities.available
        )
        if not allowed:
            return await self.block(frame, recovery)
        return await RecoveryFlow(self.unit).observe(frame, recovery)
