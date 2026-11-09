"""Read-only, exact-route recovery facts; never an approval or dispatch path."""

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256

from lets_play.new_rules.evaluation import EVALUATOR_VERSION
from lets_play.new_rules.pacing import PACING_VERSION
from lets_play.new_rules.parsing import PARSER_VERSION
from lets_play.new_rules.scoring import SCORE_VERSION
from lets_play.storage.evolution_flow import EVOLUTION_VERSION
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import _row
from lets_play.storage.offer_codec import decode_hand, utc_datetime
from lets_play.storage.opening_flow import OPENING_VERSION
from lets_play.storage.progression import PROGRESSION_VERSION
from lets_play.storage.recovery_flow import RECOVERY_VERSION, active_recovery

PAGE_SIZE = 5
FAMILIES = ("opening", "ordinary", "pacing", "evolution", "adapter", "summary", "round")
_ENGINE_TABLES = (
    "nr_opening_outputs",
    "nr_ordinary_outputs",
    "nr_pacing_outputs",
    "nr_evolution_outputs",
)


@dataclass(frozen=True, slots=True)
class OutputFact:
    cursor: int
    family: str
    evidence: str
    message_id: int | None
    published_at: datetime | None


@dataclass(frozen=True, slots=True)
class RecoveryStatus:
    session_id: str
    generation: str
    stage: str
    phases: tuple[tuple[str, str | None], ...]
    clocks: tuple[tuple[str, datetime | None], ...]
    blockers: tuple[str, ...]
    unresolved: tuple[str, ...]
    family: int
    outputs: tuple[OutputFact, ...]
    previous: bool
    following: bool


def _generation(recovery):
    # An opaque bounded token also handles older non-UUID recovery identities.
    return sha256(recovery.encode()).hexdigest()[:24]


def _time(value):
    try:
        return utc_datetime(value) if value is not None else None
    except (ValueError, OverflowError):
        return None


class RecoveryStatusStore:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    def _outputs(self, family, session, recovery):
        """Each projection returns only identity/evidence, never a saved payload.

        Engine work includes this generation's reconciled facts and outstanding
        outputs, including missing source rows. Adapter views retain unresolved
        claims plus their latest complete ACK. The families can overlap: no ACK
        in one family clears another family's unknown send.
        """
        if family <= 4:
            table, name = _ENGINE_TABLES[family - 1], FAMILIES[family - 1]
            return (
                f"""SELECT o.output_id AS cursor,
                CASE WHEN w.status='unsent' AND o.acknowledged_at_utc IS NULL
                     AND o.message_id IS NULL THEN 'unsent'
                WHEN w.status='found' AND w.message_id=o.message_id
                     AND w.published_at_utc=o.acknowledged_at_utc THEN 'found'
                WHEN w.status IS NULL AND o.message_id IS NOT NULL
                     AND o.acknowledged_at_utc IS NOT NULL THEN 'acknowledged'
                ELSE 'unknown' END AS evidence,
                o.message_id,o.acknowledged_at_utc AS published_at
                FROM {table} o LEFT JOIN nr_recovery_work w
                ON w.session_id=o.session_id AND w.output_id=o.output_id
                    AND w.family=? AND w.recovery_id=?
                WHERE o.session_id=? AND (w.output_id IS NOT NULL OR
                    (o.acknowledged_at_utc IS NULL AND o.retired_at_utc IS NULL))
                UNION ALL SELECT w.output_id,'unknown',NULL,NULL FROM nr_recovery_work w
                WHERE w.session_id=? AND w.recovery_id=? AND w.family=?
                AND NOT EXISTS (SELECT 1 FROM {table} o
                    WHERE o.session_id=w.session_id AND o.output_id=w.output_id)""",
                (name, recovery, session, session, recovery, name),
            )
        if family == 5:
            return (
                """SELECT output_id AS cursor,
                CASE WHEN state='delivered' AND message_id IS NOT NULL AND ack_sequence>0
                THEN 'acknowledged' ELSE 'unknown' END AS evidence,
                message_id,NULL AS published_at FROM nr_discord_deliveries
                WHERE session_id=? AND family=? AND
                (state<>'delivered' OR message_id IS NULL OR ack_sequence=0 OR output_id=(
                    SELECT MAX(output_id) FROM nr_discord_deliveries WHERE session_id=?
                    AND family=? AND state='delivered' AND message_id IS NOT NULL
                    AND ack_sequence>0))""",
                (session, "opening", session, "opening"),
            )
        if family == 6:
            return (
                """SELECT summary_id AS cursor,
                CASE WHEN state='delivered' AND message_id IS NOT NULL
                    AND delivered_at_utc IS NOT NULL THEN 'acknowledged'
                WHEN state='prepared' AND attempted_at_utc IS NULL AND message_id IS NULL
                    THEN 'prepared' ELSE 'unknown' END AS evidence,
                message_id,delivered_at_utc AS published_at FROM nr_summary_outputs
                WHERE session_id=? AND (state<>'delivered' OR message_id IS NULL
                OR delivered_at_utc IS NULL OR summary_id=(SELECT MAX(summary_id)
                    FROM nr_summary_outputs WHERE session_id=? AND state='delivered'
                    AND message_id IS NOT NULL AND delivered_at_utc IS NOT NULL))""",
                (session, session),
            )
        return (
            """SELECT r.round_number AS cursor,
            CASE WHEN d.state='delivered' AND d.message_id IS NOT NULL
                AND d.delivered_at_utc IS NOT NULL THEN 'acknowledged'
                ELSE 'unknown' END AS evidence,
            d.message_id,d.delivered_at_utc AS published_at FROM nr_round_reports d
            JOIN nr_rounds r USING(session_id,round_id) WHERE d.session_id=?
            AND (d.state<>'delivered' OR d.message_id IS NULL OR d.delivered_at_utc IS NULL
                OR r.round_number=(SELECT MAX(r2.round_number) FROM nr_round_reports d2
                JOIN nr_rounds r2 USING(session_id,round_id) WHERE d2.session_id=?
                AND d2.state='delivered' AND d2.message_id IS NOT NULL
                AND d2.delivered_at_utc IS NOT NULL))""",
            (session, session),
        )

    async def read(self, route, *, generation=None, family=0, anchor=0, backwards=False):
        self.unit._check()
        if (
            type(family) is not int
            or not 0 <= family <= 10
            or type(anchor) is not int
            or not 0 <= anchor < 2**63
            or type(backwards) is not bool
        ):
            raise ValueError("Invalid recovery status cursor.")
        admission = await _row(
            self.conn,
            "SELECT stage,version,revision,playing_session_id,lobby_published_at_utc,"
            "lobby_deadline_utc,offer_published_at_utc,hand_json,effective_at_utc,"
            "announced_at_utc FROM nr_admissions WHERE session_id=? AND guild_id=? "
            "AND channel_id=? AND stage<>'ended'",
            (route.session_id, route.guild_id, route.channel_id),
        )
        if (
            admission is None
            or (recovery := await active_recovery(self.unit, route.session_id)) is None
        ):
            return None
        token = _generation(recovery["recovery_id"])
        if generation is not None and generation != token:
            return None
        session = route.session_id
        owner = await _row(
            self.conn,
            "SELECT lifecycle,phase,advancement,pause_reason,round_id,revision,offer_id,"
            "parser_version,evaluator_version,score_version,progression_version,pacing_version "
            "FROM nr_sessions WHERE session_id=?",
            (session,),
        )
        blockers = ["resume_unavailable", "capabilities_unknown"]
        clocks = [
            ("recovery_began", _time(recovery["began_at_utc"])),
            ("notice_published", _time(recovery["notice_published_at_utc"])),
            ("read_in", _time(recovery["resume_at_utc"])),
        ]
        phases = [
            ("original_stage", recovery["original_stage"]),
            ("original_lifecycle", recovery["original_lifecycle"]),
            ("original_pause", recovery["original_pause"]),
            ("current_stage", admission["stage"]),
        ]
        if recovery["version"] != RECOVERY_VERSION or admission["version"] != OPENING_VERSION:
            blockers.append("version")
        if admission["stage"] == "legacy":
            blockers.append("legacy")
        if recovery["original_pause"] not in (None, "revival") or admission["stage"] == "recovery":
            blockers.append("existing_pause")
        if bool(admission["playing_session_id"]) != bool(owner):
            blockers.append("missing_state")
        revision = owner["revision"] if owner else admission["revision"]
        if revision != recovery["owner_revision"]:
            blockers.append("owner_changed")
        if owner:
            phases.extend((key, owner[key]) for key in ("lifecycle", "phase", "advancement"))
            if owner["phase"] != "ordinary":
                blockers.append("mode")
            if owner["lifecycle"] != "paused" or owner["pause_reason"] != "recovery":
                blockers.append("owner_changed")
            if any(
                owner[key] != expected
                for key, expected in (
                    ("parser_version", PARSER_VERSION),
                    ("evaluator_version", EVALUATOR_VERSION),
                    ("score_version", SCORE_VERSION),
                    ("progression_version", PROGRESSION_VERSION),
                    ("pacing_version", PACING_VERSION),
                )
            ):
                blockers.append("version")
            if owner["round_id"] != recovery["original_round"]:
                blockers.append("owner_changed")
            pacing = await _row(
                self.conn,
                "SELECT started_at_utc,warning_published_at_utc,selection_due_at_utc "
                "FROM nr_round_pacing WHERE session_id=? AND round_id=?",
                (session, owner["round_id"]),
            )
            if pacing:
                clocks.extend(
                    (key, _time(pacing[column]))
                    for key, column in (
                        ("round_started", "started_at_utc"),
                        ("warning_published", "warning_published_at_utc"),
                        ("picker_due", "selection_due_at_utc"),
                    )
                )
            else:
                blockers.append("missing_state")
            evolution = await _row(
                self.conn, "SELECT version FROM nr_evolution WHERE session_id=?", (session,)
            )
            if evolution and evolution[0] != EVOLUTION_VERSION:
                blockers.append("version")
            revival = await _row(
                self.conn,
                "SELECT published_at_utc,deadline_utc FROM nr_revivals WHERE session_id=? "
                "AND stage IN ('prepared','published') LIMIT 1",
                (session,),
            )
            if revival:
                clocks.extend(
                    (key, _time(revival[column]))
                    for key, column in (
                        ("revival_published", "published_at_utc"),
                        ("revival_due", "deadline_utc"),
                    )
                )
                if revival["deadline_utc"] is None:
                    blockers.append("clock_unknown")
            elif recovery["original_pause"] == "revival":
                blockers.append("legacy_revival")
            if owner["offer_id"]:
                offer = await _row(
                    self.conn,
                    "SELECT hand_json,published_at_utc FROM nr_offers "
                    "WHERE session_id=? AND offer_id=?",
                    (session, owner["offer_id"]),
                )
                activation = await _row(
                    self.conn,
                    "SELECT effective_at_utc,announced_at_utc FROM nr_activations "
                    "WHERE session_id=? AND offer_id=?",
                    (session, owner["offer_id"]),
                )
                self._choice_clocks(offer, clocks, blockers)
                if activation:
                    clocks.extend(
                        (key, _time(activation[column]))
                        for key, column in (
                            ("activation_announced", "announced_at_utc"),
                            ("activation_due", "effective_at_utc"),
                        )
                    )
        else:
            clocks.extend(
                (key, _time(admission[column]))
                for key, column in (
                    ("lobby_published", "lobby_published_at_utc"),
                    ("lobby_due", "lobby_deadline_utc"),
                )
            )
            if admission["stage"] in ("opening_published", "selected"):
                self._choice_clocks(
                    {
                        "hand_json": admission["hand_json"],
                        "published_at_utc": admission["offer_published_at_utc"],
                    },
                    clocks,
                    blockers,
                )
            if admission["stage"] == "selected":
                clocks.extend(
                    (key, _time(admission[column]))
                    for key, column in (
                        ("activation_announced", "announced_at_utc"),
                        ("activation_due", "effective_at_utc"),
                    )
                )
        unresolved = []
        for index in range(1, 11):
            query, args = self._family_query(index, session, recovery["recovery_id"])
            if await _row(
                self.conn, f"SELECT 1 FROM ({query}) WHERE evidence='unknown' LIMIT 1", args
            ):
                unresolved.append(self.family_name(index))
        if unresolved:
            blockers.append("unknown_delivery")
        outputs, previous, following = (), False, False
        if family:
            query, args = self._family_query(family, session, recovery["recovery_id"])
            op, order = ("<", "DESC") if backwards else (">", "ASC")
            async with self.conn.execute(
                f"SELECT * FROM ({query}) WHERE cursor{op}? ORDER BY cursor {order} LIMIT ?",
                (*args, anchor if anchor or not backwards else 2**63 - 1, PAGE_SIZE + 1),
            ) as cursor:
                batch = await cursor.fetchall()
            more = len(batch) > PAGE_SIZE
            selected = batch[:PAGE_SIZE]
            if backwards:
                selected.reverse()
            outputs = tuple(
                OutputFact(
                    r["cursor"],
                    self.family_name(family),
                    r["evidence"],
                    r["message_id"],
                    _time(r["published_at"]),
                )
                for r in selected
            )
            previous, following = (more, bool(anchor)) if backwards else (bool(anchor), more)
        return RecoveryStatus(
            session,
            token,
            recovery["stage"],
            tuple(phases),
            tuple(clocks),
            tuple(dict.fromkeys(blockers)),
            tuple(unresolved),
            family,
            outputs,
            previous,
            following,
        )

    def _family_query(self, index, session, recovery):
        if index <= 7:
            return self._outputs(index, session, recovery)
        query, _ = self._outputs(5, session, recovery)
        family = FAMILIES[index - 7]  # ordinary, pacing, evolution
        return query, (session, family, session, family)

    @staticmethod
    def family_name(index):
        return (
            "adapter_opening"
            if index == 5
            else ("adapter_" + FAMILIES[index - 7] if index >= 8 else FAMILIES[index - 1])
        )

    @staticmethod
    def _choice_clocks(offer, clocks, blockers):
        if offer is None:
            blockers.append("missing_state")
            return
        clocks.append(("choice_published", _time(offer["published_at_utc"])))
        deadline = None
        if offer["published_at_utc"] is not None:
            try:
                deadline = decode_hand(offer["hand_json"]).deadline.expires_at
            except (IncompatibleState, ValueError, TypeError, KeyError):
                blockers.append("version")
        clocks.append(("choice_due", deadline))
        if deadline is None:
            blockers.append("clock_unknown")
