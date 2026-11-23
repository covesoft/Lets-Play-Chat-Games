"""Breaking Moment ownership within the ordinary receipt and round lifecycle."""

from dataclasses import dataclass, replace
from datetime import datetime

from lets_play.new_rules.breaking_moments import MODE_VALIDATORS, ModeChoice, mode_ends_at
from lets_play.new_rules.contracts import Advancement, Lifecycle, Phase
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import _execute, _row
from lets_play.storage.offer_codec import utc_datetime, utc_int


@dataclass(frozen=True, slots=True)
class BreakingMoment:
    offer_id: str
    choice: ModeChoice
    ends_at: datetime
    outcome: str | None


class BreakingFlow:
    def __init__(self, unit):
        self.unit, self.conn = unit, unit._conn

    async def record(self, session, round_id):
        row = await _row(
            self.conn,
            "SELECT * FROM nr_breaking_moments WHERE session_id=? AND round_id=?",
            (session, round_id),
        )
        if row is None:
            return None
        if row["kind"] not in MODE_VALIDATORS:
            raise IncompatibleState("The saved Breaking Moment needs a supported predicate.")
        return BreakingMoment(
            row["offer_id"],
            ModeChoice(row["choice_id"], row["kind"]),
            utc_datetime(row["ends_at_utc"]),
            row["outcome"],
        )

    async def active(self, position):
        if (
            position.phase is not Phase.MODE
            or position.advancement is not Advancement.IDLE
            or position.offer_id is not None
            or position.transition_id is not None
        ):
            return None
        bout = await self.record(position.route.session_id, position.round_id)
        return bout if bout and bout.outcome is None else None

    async def start(self, position, offer_id, choice, activated_at):
        await _execute(
            self.conn,
            "INSERT INTO nr_breaking_moments VALUES (?,?,?,?,?,?,NULL)",
            (
                position.route.session_id,
                position.round_id,
                offer_id,
                choice.instance_id,
                choice.card_id,
                utc_int(mode_ends_at(utc_datetime(activated_at))),
            ),
        )

    async def advance(self, frame, *, interrupted=False):
        context = await self.unit.get_context(frame.route.session_id)
        if context is None:
            return
        p = context.position
        if p.lifecycle is not Lifecycle.PLAYING or p.phase is not Phase.MODE:
            return
        bout = await self.active(p)
        if bout is None:
            if interrupted:
                raise IncompatibleState("Recovery needs a saved Breaking Moment.")
            return
        if not interrupted and frame.received_at < bout.ends_at:
            return
        new = replace(
            p, phase=Phase.ORDINARY, ruleset_version=p.ruleset_version + 1, revision=p.revision + 1
        )
        await self.unit.set_owner(p, replace(context, position=new))
        await _execute(
            self.conn,
            "UPDATE nr_breaking_moments SET outcome=? WHERE session_id=? AND round_id=?",
            ("interrupted" if interrupted else "completed", p.route.session_id, p.round_id),
        )
        await _execute(
            self.conn,
            "UPDATE nr_round_pacing SET ruleset_version=?,owner_revision=? WHERE session_id=? AND round_id=?",
            (new.ruleset_version, new.revision, p.route.session_id, p.round_id),
        )

        # After an interruption the recovery panel already shows the restored rules.
        if not interrupted:
            from lets_play.storage.progression import Progression

            await Progression(self.unit).award_completed_mode(p.route.session_id, p.round_id)
            await _execute(
                self.conn,
                "INSERT INTO nr_evolution_outputs(session_id,token,kind) VALUES (?,?,'mode_ended')",
                (p.route.session_id, p.round_id),
            )

    async def close(self, position):
        await _execute(
            self.conn,
            "UPDATE nr_breaking_moments SET outcome='ended' WHERE session_id=? AND round_id=? AND outcome IS NULL",
            (position.route.session_id, position.round_id),
        )
