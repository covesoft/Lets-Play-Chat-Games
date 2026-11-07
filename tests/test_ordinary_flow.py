"""Publication clocks, ordered cutover, rollback, replay and durable ordinary choices."""

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from random import Random

import pytest

from lets_play.new_rules.contracts import (
    Deadline,
    DeadlineKind,
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.hands import PhraseChoice, build_hand
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, TransactionAborted, open_game_store
from lets_play.storage.offer_codec import decode_hand, encode_hand, utc_datetime, utc_int
from lets_play.storage.ordinary_flow import FlowOrderError, FlowStatus, OrdinaryFlow

NOW = datetime(2026, 11, 7, tzinfo=UTC)
ROUTE = SessionRoute(101, 202, "session")
POSITION = SessionPosition(ROUTE, Lifecycle.PLAYING, round_id="old-round", ruleset_version=1)


class Scene:
    def __init__(self, store, ctx, hand):
        self.store, self.ctx, self.hand = store, ctx, hand
        self.sequence = 0

    def frame(self, kind, seconds, actor=None, seq=None):
        return EventEnvelope(
            ROUTE,
            kind,
            f"{kind}-{seq or self.sequence + 1}",
            actor,
            NOW + timedelta(seconds=seconds),
            NOW + timedelta(seconds=seconds),
            seq or self.sequence + 1,
        )

    async def act(self, method, kind, seconds, *args, actor=None, **kwargs):
        frame = self.frame(kind, seconds, actor)
        async with self.store.transaction() as unit:
            result = await getattr(OrdinaryFlow(unit), method)(frame, *args, **kwargs)
        self.sequence += 1
        return result

    async def record(self):
        async with self.store.transaction(write=False) as unit:
            return await OrdinaryFlow(unit).get_offer("session", "offer")

    async def context(self):
        async with self.store.transaction(write=False) as unit:
            return await unit.get_context("session")

    async def post(self, seconds, text):
        frame = self.frame(EventKind.MESSAGE_CREATE, seconds, 303)
        async with self.store.transaction() as unit:
            result = await OrdinaryFlow(unit).ingest(MessageEvent(frame, text))
        self.sequence += 1
        return result


async def scene(store, *, phrases=False):
    ctx = EvaluationContext(
        POSITION, (RuleInstance("echo", "NR022"), RuleInstance("sorry", "NR025"))
    )
    cards = frozenset({"NR004", "NR006", "NR036" if phrases else "NR027"})
    hand = build_hand(
        ctx,
        offer_id="offer",
        picker_id=303,
        deadline=Deadline("original-token", DeadlineKind.CHOICE, NOW + timedelta(seconds=60)),
        rng=Random(0),
        phrase_bank=(PhraseChoice("p", "red duck", "revision-one"),) if phrases else (),
        available_templates=cards,
    ).hand
    async with store.transaction() as unit:
        await unit.insert_session(ctx)
    return Scene(store, ctx, hand)


async def published(s, at=5):
    assert (
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
    ).status is FlowStatus.PREPARED
    assert (
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, at, "offer", message_id=1001)
    ).status is FlowStatus.PUBLISHED


def incoming(s, card="NR027"):
    return next(o.incoming.instance_id for o in s.hand.options if o.incoming.card_id == card)


async def selected(s, *, phrase=False, at=10):
    return await s.act(
        "choose",
        EventKind.CHOICE_SUBMIT,
        at,
        "offer",
        incoming(s, "NR036" if phrase else "NR027"),
        actor=303,
        **({} if phrase else {"outgoing_id": "echo"}),
    )


def rows(path, sql, values=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, values).fetchall()


def execute(path, sql):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(sql)


@pytest.mark.asyncio
async def test_publication_starts_clock_and_partial_choice_reopen_never_resets_it(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        assert (await s.record()).published_at is None
        assert (await s.record()).choice_deadline is None
        assert (
            await s.act("choose", EventKind.CHOICE_SUBMIT, 10, "offer", incoming(s), actor=303)
        ).status is FlowStatus.UNAVAILABLE
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1001)
        record = await s.record()
        assert record.hand.deadline.expires_at == NOW + timedelta(seconds=80)
        assert record.hand.deadline.token == "original-token"
        assert (
            await s.act("choose", EventKind.CHOICE_SUBMIT, 30, "offer", incoming(s), actor=303)
        ).status is FlowStatus.NEEDS_TARGET
        assert (
            await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 40, "offer", message_id=1001)
        ).status is FlowStatus.ALREADY_DONE
        seq = s.sequence
    async with open_game_store(path) as store:
        s.store = store
        s.sequence = seq
        record = await s.record()
        assert record.selected_incoming == incoming(s)
        assert record.hand.deadline.expires_at == NOW + timedelta(seconds=80)
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).known_message_id("session", "offer", "offer") == 1001
        assert (
            await s.act(
                "choose",
                EventKind.CHOICE_SUBMIT,
                80,
                "offer",
                incoming(s),
                outgoing_id="echo",
                actor=303,
            )
        ).status is FlowStatus.EXPIRED


@pytest.mark.asyncio
async def test_normal_cutover_keeps_selection_period_scores_in_old_round(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await published(s)
        assert (await selected(s)).status is FlowStatus.SELECTED
        assert (await s.context()).active_instances == s.ctx.active_instances
        assert (
            await s.act(
                "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
            )
        ).status is FlowStatus.ANNOUNCED
        assert (await s.post(14, "maybe")).score_status is ApplyStatus.APPLIED
        assert (await s.post(15, "maybe")).score_status is ApplyStatus.APPLIED
        current = await s.context()
        assert current.position.round_id == "offer:round" and current.position.ruleset_version == 2
        assert current.position.offer_id is None and current.position.transition_id is None
        async with store.transaction(write=False) as unit:
            assert (
                await unit.get_score("session", 303, round_id="old-round")
            ).actual_percent == 100
            assert (
                await unit.get_score("session", 303, round_id="offer:round")
            ).actual_percent == 50
            assert (await unit.get_score("session", 303)).ordinary_quality == Fraction(3, 2)
            assert len(await unit.pending_markers()) == 1
        assert (await s.act("tick", EventKind.TIMER, 40)).status is FlowStatus.NOT_DUE
    assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(1,)]
    assert rows(path, "SELECT closed_at_utc FROM nr_round_closures") == [
        (utc_int(NOW + timedelta(seconds=15)),)
    ]
    assert rows(path, "SELECT started_at_utc FROM nr_rounds WHERE round_id='offer:round'") == [
        (utc_int(NOW + timedelta(seconds=15)),)
    ]


@pytest.mark.asyncio
async def test_no_acknowledgement_enters_recovery_at_boundary_without_hidden_activation(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store, phrases=True)
        await published(s)
        assert (await selected(s, phrase=True)).status is FlowStatus.SELECTED
        assert (await s.post(14, "red duck")).score_status is ApplyStatus.APPLIED
        assert (await s.post(15, "red duck")).status is FlowStatus.IGNORED
        current = await s.context()
        assert current.position.lifecycle is Lifecycle.PAUSED
        assert (
            current.position.round_id == "old-round"
            and current.active_instances == s.ctx.active_instances
        )
        assert (await s.record()).stage == "recovery"
        async with store.transaction(write=False) as unit:
            output = await OrdinaryFlow(unit).pending_outputs()
            assert [i.kind for i in output] == ["recovery"]
            assert await OrdinaryFlow(unit).used_keys("session", (("red", "duck"),)) == frozenset()
        assert (
            await s.act(
                "publish_activation", EventKind.ACTIVATION_PUBLISHED, 16, "offer", message_id=1002
            )
        ).status is FlowStatus.RECOVERY
        await s.act("publish_recovery", EventKind.RECOVERY_PUBLISHED, 17, "offer", message_id=1003)
        assert (await s.context()).position.lifecycle is Lifecycle.PAUSED
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).pending_outputs() == ()
    assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]


@pytest.mark.asyncio
async def test_timer_cannot_overtake_a_queued_older_message(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        late_tick = s.frame(EventKind.TIMER, 15, seq=s.sequence + 2)
        with pytest.raises(FlowOrderError, match="prior envelope"):
            async with store.transaction() as unit:
                await OrdinaryFlow(unit).tick(late_tick)
        assert (await s.context()).position.round_id == "old-round"
        await s.post(14, "maybe")
        async with store.transaction() as unit:
            assert (await OrdinaryFlow(unit).tick(late_tick)).status is FlowStatus.ACTIVATED
        s.sequence += 1
        async with store.transaction(write=False) as unit:
            assert (
                await unit.get_score("session", 303, round_id="old-round")
            ).actual_percent == 100


@pytest.mark.asyncio
async def test_partial_template_is_retained_when_timeout_selects_only_target(tmp_path):
    class TargetRNG:
        def __init__(self):
            self.stops = []

        def randrange(self, n):
            self.stops.append(n)
            return n - 1

    rng = TargetRNG()
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        assert (
            await s.act("choose", EventKind.CHOICE_SUBMIT, 10, "offer", incoming(s), actor=303)
        ).status is FlowStatus.NEEDS_TARGET
        assert (
            await s.act(
                "choose", EventKind.CHOICE_SUBMIT, 11, "offer", incoming(s, "NR004"), actor=303
            )
        ).status is FlowStatus.INVALID
        assert (
            await s.act("timeout", EventKind.CHOICE_TIMEOUT, 65, "offer", rng=rng)
        ).status is FlowStatus.SELECTED
        assert rng.stops == [2]
        record = await s.record()
        assert record.selected_incoming == incoming(s) and record.selected_outgoing == "sorry"
        assert record.chosen_by_picker and record.completed_by_timeout


@pytest.mark.asyncio
async def test_timeout_templates_have_one_ticket_independent_of_targets(tmp_path):
    class FirstRNG:
        def __init__(self):
            self.stops = []

        def randrange(self, n):
            self.stops.append(n)
            return 0

    rng = FirstRNG()
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        assert (
            await s.act("timeout", EventKind.CHOICE_TIMEOUT, 64, "offer", rng=rng)
        ).status is FlowStatus.NOT_DUE
        assert rng.stops == []
        assert (
            await s.act("timeout", EventKind.CHOICE_TIMEOUT, 65, "offer", rng=rng)
        ).status is FlowStatus.SELECTED
        assert rng.stops[0] == 3
        assert not (await s.record()).chosen_by_picker
        assert (await s.record()).completed_by_timeout
        assert (
            await s.act("timeout", EventKind.CHOICE_TIMEOUT, 66, "offer", rng=rng)
        ).status is FlowStatus.ALREADY_DONE


@pytest.mark.asyncio
async def test_publication_and_selection_replays_survive_reopen(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        frame = s.frame(EventKind.ACTIVATION_PUBLISHED, 11)
        async with store.transaction() as unit:
            await OrdinaryFlow(unit).publish_activation(frame, "offer", message_id=1002)
        s.sequence += 1
    async with open_game_store(path) as store:
        s.store = store
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).publish_activation(frame, "offer", message_id=1002)
            ).status is FlowStatus.REPLAY
        assert (
            await s.act(
                "publish_activation", EventKind.ACTIVATION_PUBLISHED, 12, "offer", message_id=1002
            )
        ).status is FlowStatus.ALREADY_DONE
        assert (await s.act("tick", EventKind.TIMER, 15)).status is FlowStatus.ACTIVATED
        assert (
            await s.act(
                "choose",
                EventKind.CHOICE_SUBMIT,
                16,
                "offer",
                incoming(s),
                outgoing_id="echo",
                actor=303,
            )
        ).status is FlowStatus.ALREADY_DONE
    assert rows(path, "SELECT COUNT(*) FROM nr_activations") == [(1,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(1,)]


@pytest.mark.asyncio
async def test_independent_connection_manual_timeout_race_cannot_select_twice(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as first, open_game_store(path) as second:
        s = await scene(first)
        await published(s)
        manual = s.frame(EventKind.CHOICE_SUBMIT, 64, 303, seq=3)
        timeout = s.frame(EventKind.CHOICE_TIMEOUT, 65, seq=4)

        async def choose():
            async with first.transaction() as unit:
                return await OrdinaryFlow(unit).choose(
                    manual, "offer", incoming(s), outgoing_id="echo"
                )

        async def expire():
            try:
                async with second.transaction() as unit:
                    return await OrdinaryFlow(unit).timeout(timeout, "offer", rng=Random(0))
            except FlowOrderError:
                return None

        picked, expired = await asyncio.gather(choose(), expire())
        assert picked.status is FlowStatus.SELECTED
        if expired is None:
            expired = await expire()
        assert expired.status is FlowStatus.ALREADY_DONE
    assert rows(path, "SELECT COUNT(*) FROM nr_activations") == [(1,)]


@pytest.mark.asyncio
async def test_phrase_usage_commits_only_with_cutover_and_remains_after_replacement(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store, phrases=True)
        await published(s)
        await selected(s, phrase=True)
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).used_keys("session", (("red", "duck"),)) == frozenset()
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        await s.act("tick", EventKind.TIMER, 15)
        current = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                current.position,
                replace(
                    current,
                    position=replace(
                        current.position, revision=current.position.revision + 1, ruleset_version=3
                    ),
                    active_instances=s.ctx.active_instances,
                ),
            )
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).used_keys(
                "session", (("red", "duck"), ("blue", "goose"))
            ) == frozenset({("red", "duck")})


@pytest.mark.asyncio
async def test_late_cutover_failure_rolls_back_closure_phrase_and_owner_then_retry(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store, phrases=True)
        await published(s)
        await selected(s, phrase=True)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        execute(
            path,
            "CREATE TRIGGER fail_usage BEFORE INSERT ON nr_used_phrases BEGIN SELECT RAISE(ABORT,'fixture failure'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await s.act("tick", EventKind.TIMER, 15)
        assert (await s.context()).position.round_id == "old-round"
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds WHERE round_id='offer:round'") == [(0,)]
        execute(path, "DROP TRIGGER fail_usage")
        assert (await s.act("tick", EventKind.TIMER, 15)).status is FlowStatus.ACTIVATED


@pytest.mark.asyncio
async def test_caught_flow_write_failure_makes_whole_unit_rollback_only(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        execute(
            path,
            "CREATE TRIGGER fail_output BEFORE INSERT ON nr_ordinary_outputs BEGIN SELECT RAISE(ABORT,'fixture failure'); END",
        )
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                try:
                    await OrdinaryFlow(unit).prepare_offer(
                        s.frame(EventKind.OFFER_PREPARE, 0), s.hand
                    )
                except sqlite3.IntegrityError:
                    pass
        assert (await s.context()).position == POSITION
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_flow_watermarks") == [(0,)]


@pytest.mark.asyncio
async def test_raw_store_scoring_cannot_bypass_ordered_ingress(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        current = await s.context()
        event = MessageEvent(s.frame(EventKind.MESSAGE_CREATE, 10, 303), "hello")
        with pytest.raises(RuntimeError, match="ordered"):
            await store.apply(evaluate_message(event, current))
        assert (await s.post(10, "hello")).score_status is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_wrong_picker_and_stale_owner_cannot_select(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        assert (
            await s.act(
                "choose",
                EventKind.CHOICE_SUBMIT,
                10,
                "offer",
                incoming(s),
                outgoing_id="echo",
                actor=999,
            )
        ).status is FlowStatus.UNAUTHORIZED
        current = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                current.position,
                replace(
                    current,
                    position=replace(current.position, revision=current.position.revision + 1),
                ),
            )
        assert (
            await s.act(
                "choose",
                EventKind.CHOICE_SUBMIT,
                11,
                "offer",
                incoming(s),
                outgoing_id="echo",
                actor=303,
            )
        ).status is FlowStatus.STALE


@pytest.mark.asyncio
async def test_capability_loss_at_cutover_recovers_without_applying_new_rule(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        result = await s.act(
            "tick", EventKind.TIMER, 15, available_templates=frozenset({"NR004", "NR006"})
        )
        assert result.status is FlowStatus.RECOVERY
        assert (await s.context()).position.round_id == "old-round"


@pytest.mark.asyncio
async def test_all_unusable_timeout_choices_refuse_without_replacing_or_consuming(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        result = await s.act(
            "timeout",
            EventKind.CHOICE_TIMEOUT,
            65,
            "offer",
            rng=Random(0),
            available_templates=frozenset(),
        )
        assert result.status is FlowStatus.UNAVAILABLE
        assert (await s.context()).position.round_id == "old-round"
        assert (await s.record()).stage == "published"


@pytest.mark.asyncio
async def test_ended_owner_cannot_reopen_at_cutover(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        current = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                current.position,
                replace(
                    current,
                    position=replace(
                        current.position,
                        lifecycle=Lifecycle.ENDED,
                        revision=current.position.revision + 1,
                    ),
                ),
            )
        await s.act("tick", EventKind.TIMER, 15)
        assert (await s.context()).position.lifecycle is Lifecycle.ENDED
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).pending_outputs() == ()


@pytest.mark.asyncio
async def test_receipt_clock_regression_fails_without_advancing_sequence(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        with pytest.raises(FlowOrderError, match="regressed"):
            await s.post(4, "hello")
        assert (await s.post(6, "hello")).score_status is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_publication_identity_conflict_and_failed_ack_do_not_reset_deadline(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        original = (await s.record()).hand.deadline
        with pytest.raises(ValueError, match="identity"):
            await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 6, "offer", message_id=9999)
        assert (await s.record()).hand.deadline == original
        assert (
            await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 6, "offer", message_id=1001)
        ).status is FlowStatus.ALREADY_DONE


@pytest.mark.asyncio
async def test_offer_codec_stores_config_not_witness_or_received_chat(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store, phrases=True)
        await published(s)
        encoded = encode_hand((await s.record()).hand)
        assert "witness" not in encoded
        assert "red duck" in encoded
        assert decode_hand(encoded) == (await s.record()).hand
        await s.post(6, "CHAT_ONLY_CANARY")
        with closing(sqlite3.connect(path)) as conn:
            assert "CHAT_ONLY_CANARY" not in "\n".join(conn.iterdump())
        invalid = json.loads(encoded)
        invalid["evaluator"] = "future"
        with pytest.raises(IncompatibleState):
            decode_hand(json.dumps(invalid))


@pytest.mark.asyncio
async def test_bounded_output_and_usage_reads_reject_oversized_inputs(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        async with store.transaction(write=False) as unit:
            flow = OrdinaryFlow(unit)
            with pytest.raises(ValueError):
                await flow.pending_outputs(limit=101)
            with pytest.raises(ValueError):
                await flow.used_keys("session", tuple(("word",) for _ in range(16)))


@pytest.mark.asyncio
async def test_real_released_schema_two_upgrades_without_touching_score_rows(tmp_path):
    from lets_play.storage import initialize_database
    from lets_play.storage.migrations import SCHEMA_VERSION

    path = tmp_path / "old.sqlite3"
    fixture = Path(__file__).parent / "fixtures/released_schema2.sql"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(fixture.read_text(encoding="utf-8"))
        from lets_play.new_rules.evaluation import EVALUATOR_VERSION
        from lets_play.new_rules.parsing import PARSER_VERSION
        from lets_play.new_rules.scoring import SCORE_VERSION
        from lets_play.storage.game_codec import encode_context

        context = EvaluationContext(POSITION, (RuleInstance("over", "NR034"),))
        conn.execute(
            """INSERT INTO nr_sessions(session_id,guild_id,channel_id,lifecycle,
            phase,advancement,round_id,ruleset_version,revision,context_json,
            parser_version,evaluator_version,score_version)
            VALUES ('session',101,202,'playing','ordinary','idle','old-round',1,0,?,?,?,?)""",
            (encode_context(context), PARSER_VERSION, EVALUATOR_VERSION, SCORE_VERSION),
        )
        conn.execute("INSERT INTO nr_rounds(session_id,round_id) VALUES ('session','old-round')")
        conn.execute("""INSERT INTO nr_participants(session_id,actor_id,human_messages,
            ordinary_posts,ordinary_observations,ordinary_quality_n)
            VALUES ('session',303,1,1,1,'1')""")
    before = rows(path, "SELECT * FROM nr_participants")
    assert (await initialize_database(path)).schema_version == SCHEMA_VERSION == 5
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
    assert rows(path, "PRAGMA foreign_key_check") == []
    assert rows(path, "SELECT * FROM nr_participants") == before
    assert rows(path, "SELECT started_at_utc FROM nr_rounds") == [(None,)]


@pytest.mark.parametrize("offset", [0, 1, -7])
def test_time_codec_roundtrips_exact_microseconds(offset):
    from datetime import timezone

    date = NOW.replace(microsecond=123456).astimezone(timezone(timedelta(hours=offset)))
    assert utc_datetime(utc_int(date)) == date
    with pytest.raises(ValueError):
        utc_int(date.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_migration_three_failure_restores_released_schema_two(tmp_path, monkeypatch):
    from lets_play.storage import bootstrap
    from lets_play.storage.migrations import Migration

    path = tmp_path / "old.sqlite3"
    fixture = Path(__file__).parent / "fixtures/released_schema2.sql"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(fixture.read_text(encoding="utf-8"))
    original = bootstrap.MIGRATIONS
    failed = Migration(3, original[2].name, (*original[2].statements, "INVALID SQL"))
    monkeypatch.setattr(bootstrap, "MIGRATIONS", (*original[:2], failed))
    with pytest.raises(sqlite3.OperationalError):
        await bootstrap.initialize_database(path)
    assert rows(path, "PRAGMA user_version") == [(2,)]
    assert "started_at_utc" not in {row[1] for row in rows(path, "PRAGMA table_info(nr_rounds)")}
    assert rows(path, "SELECT COUNT(*) FROM sqlite_schema WHERE name='nr_offers'") == [(0,)]
    monkeypatch.setattr(bootstrap, "MIGRATIONS", original)
    assert (await bootstrap.initialize_database(path)).schema_version == bootstrap.SCHEMA_VERSION


@pytest.mark.asyncio
async def test_cancellation_rolls_back_offer_owner_outputs_and_order_mark(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        entered = asyncio.Event()

        async def work():
            async with store.transaction() as unit:
                await OrdinaryFlow(unit).prepare_offer(s.frame(EventKind.OFFER_PREPARE, 0), s.hand)
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await s.context()).position == POSITION
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_flow_watermarks") == [(0,)]
        assert (
            await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        ).status is FlowStatus.PREPARED


@pytest.mark.asyncio
async def test_competing_cutover_ticks_close_round_once(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as first, open_game_store(path) as second:
        s = await scene(first)
        await published(s)
        await selected(s)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )

        async def tick(store, seq):
            try:
                async with store.transaction() as unit:
                    return await OrdinaryFlow(unit).tick(s.frame(EventKind.TIMER, 15, seq=seq))
            except FlowOrderError:
                return None

        first_result, second_result = await asyncio.gather(tick(first, 5), tick(second, 6))
        assert first_result.status is FlowStatus.ACTIVATED
        if second_result is None:
            second_result = await tick(second, 6)
        assert second_result.status is FlowStatus.NOT_DUE
    assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(1,)]
    assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(2,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("when", [15, 16])
async def test_late_activation_acknowledgement_cannot_backdate_cutover(tmp_path, when):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        result = await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, when, "offer", message_id=1002
        )
        assert result.status is FlowStatus.RECOVERY
        assert (await s.context()).position.round_id == "old-round"


@pytest.mark.asyncio
async def test_existing_revival_pause_is_not_overwritten_or_finalized(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        current = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                current.position,
                replace(
                    current,
                    position=replace(
                        current.position,
                        lifecycle=Lifecycle.PAUSED,
                        pause_reason=PauseReason.REVIVAL,
                        revision=current.position.revision + 1,
                    ),
                ),
            )
        await s.act("tick", EventKind.TIMER, 15)
        current = await s.context()
        assert current.position.pause_reason is PauseReason.REVIVAL
        assert current.position.round_id == "old-round"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d.update(format=True),
        lambda d: d.update(options=[]),
        lambda d: d.update(deadline=["token", True]),
        lambda d: d.update(unexpected="not-allowed"),
    ],
)
def test_offer_codec_rejects_malformed_or_extended_payloads(mutation):
    context = EvaluationContext(POSITION, ())
    hand = build_hand(
        context,
        offer_id="codec",
        picker_id=303,
        deadline=Deadline("choice", DeadlineKind.CHOICE, NOW),
        rng=Random(0),
        available_templates=frozenset({"NR004", "NR006", "NR011"}),
    ).hand
    data = json.loads(encode_hand(hand))
    mutation(data)
    with pytest.raises(IncompatibleState):
        decode_hand(json.dumps(data))


@pytest.mark.asyncio
async def test_announced_boundary_is_whole_second_and_has_full_publication_lead(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s, at=10.123456)
        record = await s.record()
        assert record.effective_at == NOW + timedelta(seconds=16)
        assert record.effective_at.microsecond == 0
        assert record.effective_at >= NOW + timedelta(seconds=15.123456)
