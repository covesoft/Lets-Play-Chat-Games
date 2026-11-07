"""New Rules general progression over committed derived evidence, without Discord I/O.

All writes use the caller's rollback-only unit. Results become authoritative only
after its commit. Theme/mode predicates, delivery, deletion and purge are separate.
"""

from dataclasses import dataclass
from enum import StrEnum

from lets_play.new_rules.contracts import Lifecycle
from lets_play.storage.game_codec import MAX_SQLITE_INTEGER, IncompatibleState
from lets_play.storage.game_store import GameUnitOfWork, _execute, _row
from lets_play.storage.game_store import _unit_mutating as _write
from lets_play.storage.offer_codec import decode_hand

PROGRESSION_VERSION = "general-1"
GENERAL_BADGES = frozenset(
    {"rulemaker", "spotless", "familiar_face", "party_animal", "very_careful"}
)


class ResolutionOrderError(ValueError):
    """Resolve real closures in creation order; never invent a missing closure."""


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    ALREADY_DONE = "already_done"
    NOT_CLOSED = "not_closed"
    UNKNOWN_SESSION = "unknown_session"


@dataclass(frozen=True, slots=True)
class NewRulesProfile:
    actor_id: int
    games_played: int
    badges: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlayerProgress:
    game_count_credited: bool
    current_clean_streak: int
    best_clean_streak: int
    personal_rounds_played: int
    party_rounds: int


@dataclass(frozen=True, slots=True)
class StreakNotice:
    notice_id: int
    session_id: str
    round_id: str
    actor_id: int
    milestone: int


def _actor(actor_id):
    if type(actor_id) is not int or not 1 <= actor_id <= MAX_SQLITE_INTEGER:
        raise ValueError("A positive SQLite-range human ID is required.")


def _limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Page limit must be an integer in [1, 100].")


class Progression:
    """Indexed reads and set-based general progress; no roster/profile cache."""

    def __init__(self, unit: GameUnitOfWork):
        self.unit = unit
        self.conn = unit._conn

    async def _context(self, session_id):
        context = await self.unit.get_context(session_id)
        if context is not None:
            row = await _row(
                self.conn,
                "SELECT progression_version FROM nr_sessions WHERE session_id=?",
                (session_id,),
            )
            if row[0] != PROGRESSION_VERSION:
                raise IncompatibleState("Live progression version needs explicit recovery.")
        return context

    async def profile(self, actor_id: int) -> NewRulesProfile:
        self.unit._check()
        _actor(actor_id)
        row = await _row(
            self.conn, "SELECT games_played FROM nr_profiles WHERE actor_id=?", (actor_id,)
        )
        async with self.conn.execute(
            "SELECT badge_key FROM nr_badges WHERE actor_id=? ORDER BY badge_key", (actor_id,)
        ) as cursor:
            badges = tuple(r[0] for r in await cursor.fetchall())
        return NewRulesProfile(actor_id, row[0] if row is not None else 0, badges)

    async def progress(self, session_id: str, actor_id: int) -> PlayerProgress | None:
        self.unit._check()
        _actor(actor_id)
        await self._context(session_id)
        row = await _row(
            self.conn,
            """SELECT game_count_credited,current_clean_streak,best_clean_streak,
            personal_rounds_played,party_rounds FROM nr_player_progress
            WHERE session_id=? AND actor_id=?""",
            (session_id, actor_id),
        )
        return PlayerProgress(bool(row[0]), *tuple(row)[1:]) if row is not None else None

    async def _badge(self, actor_id, badge):
        # Internal only: callers must prove a supported predicate from saved facts.
        if badge not in GENERAL_BADGES:
            raise ValueError("Unsupported general badge.")
        await _execute(
            self.conn,
            "INSERT INTO nr_profiles(actor_id) VALUES (?) ON CONFLICT(actor_id) DO NOTHING",
            (actor_id,),
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_badges VALUES (?,?) ON CONFLICT(actor_id,badge_key) DO NOTHING",
            (actor_id, badge),
        )

    @_write
    async def credit_ordinary(self, session_id: str, actor_id: int | None = None) -> None:
        """Reconcile proven ordinary credit; no caller-supplied boolean proof."""
        if actor_id is not None:
            _actor(actor_id)
        if await self._context(session_id) is None:
            return
        where = "p.session_id=? AND p.game_count_credited=0 AND s.ordinary_posts>=5"
        values = (session_id,)
        if actor_id is not None:
            where += " AND p.actor_id=?"
            values += (actor_id,)
        eligible = f"""SELECT p.actor_id FROM nr_player_progress AS p
            JOIN nr_participants AS s USING(session_id,actor_id) WHERE {where}"""
        if await _row(self.conn, f"SELECT 1 FROM ({eligible}) LIMIT 1", values) is None:
            return
        await _execute(
            self.conn,
            f"""INSERT INTO nr_profiles(actor_id) {eligible}
            ON CONFLICT(actor_id) DO NOTHING""",
            values,
        )
        # STRICT INTEGER rejects an overflowing increment; the full unit rolls back.
        await _execute(
            self.conn,
            f"UPDATE nr_profiles SET games_played=games_played+1 WHERE actor_id IN ({eligible})",
            values,
        )
        await _execute(
            self.conn,
            f"""INSERT INTO nr_badges(actor_id,badge_key)
            SELECT actor_id,'familiar_face' FROM nr_profiles WHERE games_played>=10
            AND actor_id IN ({eligible}) ON CONFLICT(actor_id,badge_key) DO NOTHING""",
            values,
        )
        await _execute(
            self.conn,
            f"""UPDATE nr_player_progress SET game_count_credited=1
            WHERE session_id=? AND actor_id IN ({eligible})""",
            (session_id, *values),
        )

    @_write
    async def award_activated_choice(self, session_id: str, offer_id: str) -> bool:
        if await self._context(session_id) is None:
            return False
        row = await _row(
            self.conn,
            """SELECT o.hand_json FROM nr_offers AS o JOIN nr_activations AS a
            USING(session_id,offer_id) WHERE o.session_id=? AND o.offer_id=?
            AND o.stage='activated' AND a.stage='activated' AND o.chosen_by_picker=1""",
            (session_id, offer_id),
        )
        if row is None:
            return False
        hand = decode_hand(row[0])
        await self._badge(hand.picker_id, "rulemaker")
        return True

    async def pending_rounds(self, session_id: str, *, limit: int = 100) -> tuple[str, ...]:
        self.unit._check()
        _limit(limit)
        async with self.conn.execute(
            """SELECT r.round_id FROM nr_rounds AS r JOIN nr_round_closures AS c
            USING(session_id,round_id) LEFT JOIN nr_round_resolutions AS x USING(session_id,round_id)
            WHERE r.session_id=? AND x.round_id IS NULL ORDER BY r.round_number LIMIT ?""",
            (session_id, limit),
        ) as cursor:
            return tuple(r[0] for r in await cursor.fetchall())

    @_write
    async def resolve_round(self, session_id: str, round_id: str) -> ResolutionStatus:
        """Resolve an activated old round once; open/revival rounds do not qualify."""
        return await self._resolve(session_id, round_id, ending=False)

    async def _resolve(self, session_id, round_id, *, ending):
        context = await self._context(session_id)
        if context is None:
            return ResolutionStatus.UNKNOWN_SESSION
        prior = await _row(
            self.conn,
            "SELECT 1 FROM nr_round_resolutions WHERE session_id=? AND round_id=?",
            (session_id, round_id),
        )
        if prior is not None:
            return ResolutionStatus.ALREADY_DONE
        boundary = await _row(
            self.conn,
            """SELECT c.offer_id FROM nr_round_closures AS c JOIN nr_activations AS a
            USING(session_id,offer_id) JOIN nr_offers AS o USING(session_id,offer_id)
            WHERE c.session_id=? AND c.round_id=? AND a.stage='activated' AND o.stage='activated'""",
            (session_id, round_id),
        )
        if ending:
            if (
                context.position.lifecycle is not Lifecycle.ENDED
                or context.position.round_id != round_id
            ):
                return ResolutionStatus.NOT_CLOSED
        elif boundary is None or context.position.round_id == round_id:
            return ResolutionStatus.NOT_CLOSED
        row = await _row(
            self.conn,
            "SELECT round_number FROM nr_rounds WHERE session_id=? AND round_id=?",
            (session_id, round_id),
        )
        previous = await _row(
            self.conn,
            "SELECT COALESCE(MAX(round_number),0) FROM nr_round_resolutions WHERE session_id=?",
            (session_id,),
        )
        if row is None or row[0] != previous[0] + 1:
            raise ResolutionOrderError("Resolve every earlier actual closure first.")
        number = row[0]
        if await _row(
            self.conn,
            "SELECT 1 FROM nr_player_progress WHERE session_id=? AND first_round_number IS NULL LIMIT 1",
            (session_id,),
        ):
            raise IncompatibleState("Missing legacy enrollment evidence needs explicit recovery.")
        await _execute(
            self.conn,
            "INSERT INTO nr_round_resolutions VALUES (?,?,?,?)",
            (session_id, round_id, number, "ending" if ending else "activation"),
        )
        await self.credit_ordinary(session_id)
        party = await _row(
            self.conn,
            "SELECT COUNT(*) FROM nr_round_scores WHERE session_id=? AND round_id=? AND ordinary_posts>=5",
            (session_id, round_id),
        )
        clean = """EXISTS(SELECT 1 FROM nr_round_scores AS s WHERE s.session_id=p.session_id
            AND s.actor_id=p.actor_id AND s.round_id=? AND s.ordinary_observations>=5
            AND s.ordinary_faults=0 AND s.reaction_failures=0
            AND s.mode_quality_n=CAST(s.mode_observations AS TEXT))"""
        active = """EXISTS(SELECT 1 FROM nr_round_scores AS s WHERE s.session_id=p.session_id
            AND s.actor_id=p.actor_id AND s.round_id=? AND s.ordinary_posts>=5)"""
        await _execute(
            self.conn,
            f"""UPDATE nr_player_progress AS p SET
            current_clean_streak=CASE WHEN {clean} THEN current_clean_streak+1 ELSE 0 END,
            best_clean_streak=MAX(best_clean_streak,CASE WHEN {clean} THEN current_clean_streak+1 ELSE 0 END),
            personal_rounds_played=personal_rounds_played+{active},
            party_rounds=party_rounds+(? AND {active})
            WHERE p.session_id=? AND first_round_number<=?""",
            (round_id, round_id, round_id, party[0] >= 5, round_id, session_id, number),
        )
        for badge, predicate in (
            ("spotless", "current_clean_streak>0"),
            ("party_animal", "party_rounds>=3"),
        ):
            await self._participant_badges(
                session_id, badge, f"first_round_number<={number} AND {predicate}"
            )
        await _execute(
            self.conn,
            """INSERT INTO nr_streak_notices(session_id,round_id,actor_id,milestone)
            SELECT session_id,?,actor_id,current_clean_streak FROM nr_player_progress
            WHERE session_id=? AND first_round_number<=? AND current_clean_streak IN (3,5,7,9)""",
            (round_id, session_id, number),
        )
        if boundary is not None:
            await self.award_activated_choice(session_id, boundary[0])
        return ResolutionStatus.RESOLVED

    async def _participant_badges(self, session_id, badge, predicate):
        # Predicates are static internal SQL, never input from Discord/configuration.
        eligible = f"SELECT actor_id FROM nr_player_progress WHERE session_id=? AND {predicate}"
        await _execute(
            self.conn,
            f"INSERT INTO nr_profiles(actor_id) {eligible} ON CONFLICT(actor_id) DO NOTHING",
            (session_id,),
        )
        await _execute(
            self.conn,
            f"INSERT INTO nr_badges SELECT actor_id,? FROM ({eligible}) WHERE 1 ON CONFLICT(actor_id,badge_key) DO NOTHING",
            (badge, session_id),
        )

    @_write
    async def finalize_ended(self, session_id: str) -> ResolutionStatus:
        """Compose with a real terminal owner update; this does not authorize/end a game."""
        context = await self._context(session_id)
        if context is None:
            return ResolutionStatus.UNKNOWN_SESSION
        if context.position.lifecycle is not Lifecycle.ENDED:
            return ResolutionStatus.NOT_CLOSED
        if await _row(
            self.conn, "SELECT 1 FROM nr_game_finalizations WHERE session_id=?", (session_id,)
        ):
            return ResolutionStatus.ALREADY_DONE
        await self._resolve(session_id, context.position.round_id, ending=True)
        await self._participant_badges(
            session_id, "very_careful", "best_clean_streak>=MAX(personal_rounds_played/3,3)"
        )
        await _execute(
            self.conn,
            "INSERT INTO nr_game_finalizations VALUES (?,?)",
            (session_id, context.position.round_id),
        )
        return ResolutionStatus.RESOLVED

    async def pending_streak_notices(self, *, limit: int = 100) -> tuple[StreakNotice, ...]:
        self.unit._check()
        _limit(limit)
        async with self.conn.execute(
            """SELECT notice_id,session_id,round_id,actor_id,milestone FROM nr_streak_notices
            WHERE acknowledged=0 ORDER BY notice_id LIMIT ?""",
            (limit,),
        ) as cursor:
            return tuple(StreakNotice(*row) for row in await cursor.fetchall())

    @_write
    async def acknowledge_streak_notice(self, notice_id: int) -> None:
        _actor(notice_id)
        await _execute(
            self.conn, "UPDATE nr_streak_notices SET acknowledged=1 WHERE notice_id=?", (notice_id,)
        )
