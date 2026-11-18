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

    async def opening_probe(self, session):
        """Find a claimed opening post for the exact saved admission."""
        self.unit._check()
        return await _row(
            self.conn,
            """SELECT r.recovery_id,o.output_id,o.kind,d.attempted_at_utc,
                      CASE WHEN o.kind='activation' THEN a.effective_at_utc END AS cutover_utc
               FROM nr_recoveries r JOIN nr_admissions a USING(session_id)
               JOIN nr_recovery_work w USING(session_id,recovery_id)
               JOIN nr_opening_outputs o ON o.session_id=a.session_id AND o.output_id=w.output_id
               JOIN nr_discord_deliveries d ON d.session_id=o.session_id AND d.output_id=o.output_id
               WHERE a.session_id=? AND r.stage='paused' AND r.version=?
               AND w.family='opening' AND w.status='unknown' AND w.kind=o.kind
               AND d.family='opening' AND d.state='sending' AND d.message_id IS NULL
               AND d.ack_sequence=0 AND o.message_id IS NULL
               AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
               AND a.playing_session_id IS NULL AND a.revision=r.owner_revision
               AND a.stage=r.original_stage AND
               ((a.stage='lobby_prepared' AND o.kind='lobby')
                OR (a.stage='opening_prepared' AND o.kind='offer')
                OR (a.stage='selected' AND o.kind='activation'
                    AND a.announced_at_utc IS NULL AND a.effective_at_utc IS NOT NULL)) LIMIT 1""",
            (session, RECOVERY_VERSION),
        )

    async def pacing_probe(self, session):
        self.unit._check()
        return await _row(
            self.conn,
            """SELECT r.recovery_id,o.output_id,o.kind,d.attempted_at_utc
               FROM nr_recoveries r JOIN nr_sessions s USING(session_id)
               JOIN nr_recovery_work w USING(session_id,recovery_id)
               JOIN nr_round_pacing p ON p.session_id=s.session_id AND p.round_id=s.round_id
               JOIN nr_pacing_outputs o ON o.session_id=p.session_id AND o.round_id=p.round_id
                   AND o.attempt=p.attempt AND o.output_id=w.output_id
               JOIN nr_discord_deliveries d ON d.session_id=o.session_id AND d.output_id=o.output_id
               WHERE s.session_id=? AND r.stage='paused' AND r.version=? AND r.original_stage='active'
               AND s.lifecycle='paused' AND s.pause_reason='recovery' AND s.phase='ordinary'
               AND s.revision=r.owner_revision AND s.round_id=r.original_round
               AND w.family='pacing' AND w.status='unknown' AND w.kind=o.kind
               AND d.family='pacing' AND d.state='sending' AND d.message_id IS NULL
               AND d.ack_sequence=0 AND o.message_id IS NULL
               AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
               AND s.offer_id IS NULL AND s.transition_id IS NULL
               AND ((o.kind='warning' AND p.stage='warning_queued' AND s.advancement='warning'
                     AND r.original_lifecycle='playing' AND r.original_pause IS NULL)
                 OR (o.kind='revival' AND p.stage='revival' AND s.advancement='idle'
                     AND r.original_lifecycle='paused' AND r.original_pause='revival'
                     AND EXISTS (SELECT 1 FROM nr_revivals v WHERE v.session_id=p.session_id
                         AND v.round_id=p.round_id AND v.attempt=p.attempt
                         AND v.owner_revision=p.owner_revision AND v.stage='prepared'))) LIMIT 1""",
            (session, RECOVERY_VERSION),
        )

    async def ordinary_activation_probe(self, session):
        self.unit._check()
        return await _row(
            self.conn,
            """SELECT r.recovery_id,o.output_id,o.kind,d.attempted_at_utc,
                      a.effective_at_utc AS cutover_utc
               FROM nr_recoveries r JOIN nr_sessions s USING(session_id)
               JOIN nr_recovery_work w USING(session_id,recovery_id)
               JOIN nr_activations a ON a.session_id=s.session_id AND a.offer_id=s.offer_id
               JOIN nr_offers f ON f.session_id=a.session_id AND f.offer_id=a.offer_id
               JOIN nr_ordinary_outputs o ON o.session_id=a.session_id AND o.offer_id=a.offer_id
                   AND o.output_id=w.output_id
               JOIN nr_discord_deliveries d ON d.session_id=o.session_id AND d.output_id=o.output_id
               WHERE s.session_id=? AND r.stage='paused' AND r.version=? AND r.original_stage='active'
               AND r.original_lifecycle='playing' AND r.original_pause IS NULL
               AND s.lifecycle='paused' AND s.pause_reason='recovery' AND s.phase='ordinary'
               AND s.revision=r.owner_revision AND s.round_id=r.original_round
               AND s.advancement='choosing' AND s.transition_id=s.offer_id || ':activation'
               AND f.stage='selected' AND a.stage='prepared' AND a.announced_at_utc IS NULL
               AND a.owner_revision<s.revision
               AND w.family='ordinary' AND w.status='unknown' AND w.kind='activation'
               AND o.kind='activation' AND o.message_id IS NULL
               AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
               AND d.family='ordinary' AND d.state='sending' AND d.message_id IS NULL
               AND d.ack_sequence=0 LIMIT 1""",
            (session, RECOVERY_VERSION),
        )

    async def held_probe(self, session):
        self.unit._check()
        return await _row(
            self.conn,
            """SELECT r.recovery_id,o.output_id,o.kind,d.attempted_at_utc
               FROM nr_recoveries r JOIN nr_sessions s USING(session_id)
               JOIN nr_recovery_work w USING(session_id,recovery_id)
               JOIN nr_evolution e ON e.session_id=s.session_id AND e.round_id=s.round_id
               JOIN nr_evolution_outputs o ON o.session_id=e.session_id AND o.token=e.held_token
                   AND o.output_id=w.output_id
               JOIN nr_discord_deliveries d ON d.session_id=o.session_id AND d.output_id=o.output_id
               WHERE s.session_id=? AND r.stage='paused' AND r.version=? AND r.original_stage='active'
               AND r.original_lifecycle='playing' AND r.original_pause IS NULL
               AND s.lifecycle='paused' AND s.pause_reason='recovery' AND s.phase='ordinary'
               AND s.revision=r.owner_revision AND s.round_id=r.original_round
               AND s.advancement='held' AND s.offer_id IS NULL AND s.transition_id IS NULL
               AND e.phase='held' AND e.idle_anchor_at_utc IS NOT NULL
               AND w.family='evolution' AND w.status='unknown' AND w.kind='held'
               AND o.kind='held' AND o.message_id IS NULL
               AND o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL
               AND d.family='evolution' AND d.state='sending' AND d.message_id IS NULL
               AND d.ack_sequence=0 LIMIT 1""",
            (session, RECOVERY_VERSION),
        )

    async def superseded_notice_probe(self, session):
        self.unit._check()
        current = await active_recovery(self.unit, session)
        if (
            current is None
            or current["stage"] != "paused"
            or current["version"] != RECOVERY_VERSION
        ):
            return None
        return await _row(
            self.conn,
            """SELECT * FROM nr_recoveries WHERE session_id=? AND stage='superseded' AND version=?
               AND notice_attempted_at_utc IS NOT NULL AND notice_message_id IS NULL
               AND notice_published_at_utc IS NULL AND notice_ack_sequence=0
               AND notice_prepared_at_utc<=notice_attempted_at_utc
               ORDER BY notice_attempted_at_utc,recovery_id LIMIT 1""",
            (session, RECOVERY_VERSION),
        )

    @_write
    async def reconcile_superseded_notice(
        self, frame, current, superseded, message_id, published_at
    ):
        session = frame.route.session_id
        proof_target = await self.superseded_notice_probe(session)
        result = await RecoveryFlow(self.unit).observe(frame, current, allow_resume=False)
        if result != "paused":
            return result
        published = utc_int(published_at)
        if (
            proof_target is None
            or proof_target["recovery_id"] != superseded
            or type(message_id) is not int
            or not 0 < message_id < 2**63
            or not proof_target["notice_attempted_at_utc"]
            <= published
            <= utc_int(frame.received_at)
        ):
            return "unproven"
        await _execute(
            self.conn,
            """UPDATE nr_recoveries SET notice_message_id=?,notice_published_at_utc=?,notice_ack_sequence=?
               WHERE session_id=? AND recovery_id=? AND stage='superseded'""",
            (message_id, published, frame.sequence, session, superseded),
        )
        return "found"

    @_write
    async def reconcile_publication(
        self, frame, recovery, family, output_id, message_id, published_at
    ):
        session = frame.route.session_id
        probe_for = {
            "opening": self.opening_probe,
            "pacing": self.pacing_probe,
            "ordinary": self.ordinary_activation_probe,
            "evolution": self.held_probe,
        }[family]
        probe = await probe_for(session)
        published = utc_int(published_at)
        flow = RecoveryFlow(self.unit)
        if (
            probe is None
            or probe["recovery_id"] != recovery
            or probe["output_id"] != output_id
            or not probe["attempted_at_utc"] <= published <= utc_int(frame.received_at)
            or (probe["kind"] == "activation" and published >= probe["cutover_utc"])
        ):
            return await flow.observe(frame, recovery, allow_resume=False)
        result = await flow.reconcile(
            frame, recovery, family, output_id, message_id=message_id, published_at=published_at
        )
        if result == "found":
            changed = await _execute(
                self.conn,
                """UPDATE nr_discord_deliveries SET state='delivered',message_id=?,ack_sequence=?
                   WHERE session_id=? AND family=? AND output_id=? AND state='sending'""",
                (message_id, frame.sequence, session, family, output_id),
            )
            if not changed:
                raise IncompatibleState("Publication proof lost its adapter claim.")
        return result

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
        if row["revision"] != owner["owner_revision"] or owner["original_lifecycle"] is not None:
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
        if stage != owner["original_stage"]:
            # Keep the original snapshot even after proof advances a prepared stage.
            expected = {"lobby": "lobby_prepared", "offer": "opening_prepared"}.get(kind)
            if (
                expected is None
                or owner["original_stage"] != expected
                or not await _row(
                    self.conn,
                    """SELECT 1 FROM nr_recovery_work WHERE session_id=? AND recovery_id=?
                   AND family='opening' AND kind=? AND status='found'
                   AND message_id=? AND published_at_utc=?""",
                    (
                        session,
                        owner["recovery_id"],
                        kind,
                        proof["message_id"],
                        proof["acknowledged_at_utc"],
                    ),
                )
            ):
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
