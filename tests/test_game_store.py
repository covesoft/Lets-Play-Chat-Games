"""Real on-disk SQLite transactions, races, restart, cancellation, and privacy."""

import asyncio
import sqlite3
import sys
from contextlib import AsyncExitStack, closing
from dataclasses import replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from lets_play.new_rules.contracts import (
    Advancement,
    AttachmentSnapshot,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    Outcome,
    PauseReason,
    Phase,
    ReactionEvent,
    RuleEvaluation,
    RuleInstance,
    SessionPosition,
    SessionRoute,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message, evaluate_reaction
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage import game_store as storage
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import (
    ApplyStatus,
    StoreOverloaded,
    TrafficTotals,
    TransactionAborted,
    open_game_store,
)

ROUTE = SessionRoute(101, 202, "session-a")
POSITION = SessionPosition(ROUTE, Lifecycle.PLAYING, round_id="round-a", ruleset_version=1)
NOW = datetime(2026, 11, 7, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[1]


def context(*cards, position=POSITION, exclusions=()):
    return EvaluationContext(
        position,
        tuple(RuleInstance(f"rule-{i}", card) for i, card in enumerate(cards)),
        exclusions,
    )


def envelope(ctx, source, *, reaction=False, actor=303):
    return EventEnvelope(
        ctx.position.route,
        EventKind.REACTION_ADD if reaction else EventKind.MESSAGE_CREATE,
        source,
        actor,
        NOW,
        NOW,
        1,
    )


def message(ctx, source="message-a", text="**over...**", actor=303, **kwargs):
    return evaluate_message(MessageEvent(envelope(ctx, source, actor=actor), text, **kwargs), ctx)


def reaction(ctx, source="reaction-a", *, actor=303, target=404, emoji=None):
    return evaluate_reaction(
        ReactionEvent(
            envelope(ctx, source, reaction=True, actor=actor),
            target,
            emoji or EmojiIdentity(unicode="👍"),
        ),
        ctx,
    )


async def seed(store, ctx):
    async with store.transaction() as unit:
        await unit.insert_session(ctx)


async def score(store, *, actor=303, session="session-a", round_id=None):
    async with store.transaction(write=False) as unit:
        return await unit.get_score(session, actor, round_id=round_id)


def query(path, sql, values=()):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(sql, values).fetchall()


def execute(path, sql):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(sql)


@pytest.mark.asyncio
async def test_atomic_message_scores_fault_breakdown_marker_and_restart(tmp_path):
    path = tmp_path / "#Code naïve folder" / "game.sqlite3"
    ctx = context("NR034", "NR011", "NR016")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
        total = await score(store)
        assert total.ordinary_quality == Fraction(2, 3)
        assert total.ordinary_posts == total.ordinary_observations == total.raw_faults == 1
        assert await score(store, round_id="round-a") == total
    async with open_game_store(path) as store:
        assert await score(store) == total
        assert await store.apply(message(ctx)) is ApplyStatus.DUPLICATE_EVENT
        async with store.transaction(write=False) as unit:
            assert await unit.get_context("session-a") == ctx
            assert await unit.get_traffic("session-a") == TrafficTotals(1, 0)
            intents = await unit.pending_markers()
            assert len(intents) == 1 and intents[0].source_id == "message-a"
        async with store.transaction() as unit:
            await unit.acknowledge_marker(intents[0].intent_id)
            await unit.acknowledge_marker(intents[0].intent_id)
        async with store.transaction(write=False) as unit:
            assert await unit.pending_markers() == ()
    assert query(path, "SELECT rule_instance_id,reason_code,faults FROM nr_fault_counts") == [
        ("rule-2", "formatting_forbidden", 1)
    ]
    assert query(path, "PRAGMA integrity_check") == [("ok",)]
    assert query(path, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_same_connection_duplicate_burst_is_exactly_one_internal_effect(tmp_path):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        decision = message(ctx, text="oops")
        statuses = await asyncio.gather(*(store.apply(decision) for _ in range(50)))
        assert statuses.count(ApplyStatus.APPLIED) == 1
        assert statuses.count(ApplyStatus.DUPLICATE_EVENT) == 49
        assert (await score(store)).ordinary_posts == 1
        async with store.transaction(write=False) as unit:
            assert len(await unit.pending_markers()) == 1


@pytest.mark.asyncio
async def test_independent_connections_race_on_source_and_actor_target_guards(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR006", "NR034")
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(4)]
        await seed(stores[0], ctx)
        message_results = await asyncio.gather(
            *(stores[i % 4].apply(message(ctx)) for i in range(12))
        )
        assert message_results.count(ApplyStatus.APPLIED) == 1
        reactions = await asyncio.gather(
            *(stores[i % 4].apply(reaction(ctx, str(i))) for i in range(12))
        )
        assert reactions.count(ApplyStatus.APPLIED) == 1
        assert reactions.count(ApplyStatus.DUPLICATE_REACTION) == 11
        total = await score(stores[0])
        assert total.ordinary_posts == total.reaction_failures == 1
    assert query(path, "SELECT COUNT(*) FROM nr_processed_events") == [(2,)]
    assert query(path, "SELECT COUNT(*) FROM nr_reaction_offenses") == [(1,)]


PROCESS_WORKER = """
import asyncio, sys
from datetime import UTC, datetime
from pathlib import Path
from lets_play.new_rules.contracts import *
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.storage.game_store import open_game_store
async def run():
    route=SessionRoute(101,202,"session-a")
    ctx=EvaluationContext(SessionPosition(route,Lifecycle.PLAYING,round_id="round-a",ruleset_version=1),(RuleInstance("rule-0","NR034"),))
    now=datetime(2026,11,7,tzinfo=UTC)
    event=MessageEvent(EventEnvelope(route,EventKind.MESSAGE_CREATE,"process-message",303,now,now,1),"over...")
    async with open_game_store(Path(sys.argv[1])) as store:
        print((await store.apply(evaluate_message(event,ctx))).value)
asyncio.run(run())
"""


@pytest.mark.asyncio
async def test_separate_processes_cannot_double_apply_a_source(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        await seed(store, context("NR034"))
        workers = [
            await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                PROCESS_WORKER,
                str(path),
                cwd=REPO_ROOT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            for _ in range(2)
        ]
        outputs = await asyncio.gather(*(worker.communicate() for worker in workers))
        assert all(worker.returncode == 0 for worker in workers), outputs
        assert sorted(out.strip() for out, err in outputs) == [b"applied", b"duplicate_event"]
        assert (await score(store)).ordinary_posts == 1


@pytest.mark.asyncio
async def test_many_distinct_events_do_not_lose_additive_quality(tmp_path):
    ctx = context("NR034", "NR011", "NR016")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        results = await asyncio.gather(*(store.apply(message(ctx, str(i))) for i in range(60)))
        assert set(results) == {ApplyStatus.APPLIED}
        total = await score(store)
        assert total.ordinary_quality == 40
        assert total.ordinary_posts == total.raw_faults == 60
        assert total.actual_percent == Fraction(200, 3)


@pytest.mark.asyncio
async def test_late_marker_failure_rolls_back_guard_enrollment_scores_and_traffic(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        execute(
            path,
            "CREATE TRIGGER fault BEFORE INSERT ON nr_marker_intents BEGIN SELECT RAISE(ABORT,'fixture failure'); END",
        )
        with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
            await store.apply(message(ctx, text="wrong"))
        assert await score(store) is None
        assert query(path, "SELECT COUNT(*) FROM nr_processed_events") == [(0,)]
        assert query(path, "SELECT human_messages FROM nr_sessions") == [(0,)]
        execute(path, "DROP TRIGGER fault")
        assert await store.apply(message(ctx, text="wrong")) is ApplyStatus.APPLIED
        assert (await score(store)).ordinary_faults == 1


@pytest.mark.asyncio
async def test_catching_a_write_error_cannot_commit_a_partial_unit(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        execute(
            path,
            "CREATE TRIGGER fault BEFORE INSERT ON nr_marker_intents BEGIN SELECT RAISE(ABORT,'fixture failure'); END",
        )
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                await unit.apply(message(ctx, "good", "over..."))
                try:
                    await unit.apply(message(ctx, "bad", "wrong"))
                except sqlite3.IntegrityError:
                    pass
        assert await score(store) is None
        assert query(path, "SELECT COUNT(*) FROM nr_processed_events") == [(0,)]


@pytest.mark.asyncio
async def test_owner_mutation_and_event_effects_share_a_rollback_boundary(tmp_path):
    ctx = context("NR034")
    advanced = replace(ctx, position=replace(POSITION, advancement=Advancement.WARNING, revision=1))
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        with pytest.raises(RuntimeError, match="fixture"):
            async with store.transaction() as unit:
                assert await unit.apply(message(ctx)) is ApplyStatus.APPLIED
                assert await unit.set_owner(POSITION, advanced)
                raise RuntimeError("fixture")
        async with store.transaction(write=False) as unit:
            assert await unit.get_context("session-a") == ctx
        assert await score(store) is None
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_monotonic_revision_rejects_aba_and_old_sources_remain_idempotent(tmp_path):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
        warning = replace(
            ctx, position=replace(POSITION, advancement=Advancement.WARNING, revision=1)
        )
        restored = replace(ctx, position=replace(POSITION, revision=2))
        async with store.transaction() as unit:
            assert await unit.set_owner(POSITION, warning)
            assert await unit.set_owner(warning.position, restored)
            assert not await unit.set_owner(POSITION, warning)
        assert await store.apply(message(ctx, "unseen-old-event")) is ApplyStatus.STALE_OWNER
        assert await store.apply(message(ctx)) is ApplyStatus.DUPLICATE_EVENT
        assert await store.apply(message(restored, "current-event")) is ApplyStatus.APPLIED
        assert (await score(store)).ordinary_posts == 2


@pytest.mark.asyncio
async def test_owner_wins_boundary_race_before_waiting_event(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as first, open_game_store(path) as second:
        await seed(first, ctx)
        next_ctx = replace(
            ctx, position=replace(POSITION, round_id="round-b", ruleset_version=2, revision=1)
        )
        async with first.transaction() as unit:
            pending = asyncio.create_task(second.apply(message(ctx)))
            assert await unit.set_owner(POSITION, next_ctx)
        assert await pending is ApplyStatus.STALE_OWNER
        assert await score(first) is None
        assert await first.apply(message(next_ctx)) is ApplyStatus.APPLIED
        assert await score(first, round_id="round-a") is None
        assert (await score(first, round_id="round-b")).ordinary_posts == 1


@pytest.mark.asyncio
async def test_reaction_key_survives_round_changes_and_restart(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR006")
    advanced = replace(
        ctx, position=replace(POSITION, round_id="round-b", ruleset_version=2, revision=1)
    )
    async with open_game_store(path) as store:
        await seed(store, ctx)
        assert await store.apply(reaction(ctx)) is ApplyStatus.APPLIED
        async with store.transaction() as unit:
            assert await unit.set_owner(POSITION, advanced)
    async with open_game_store(path) as store:
        assert (
            await store.apply(
                reaction(advanced, "different-emoji", emoji=EmojiIdentity(custom_id=701))
            )
            is ApplyStatus.DUPLICATE_REACTION
        )
        assert (
            await store.apply(reaction(advanced, "different-target", target=405))
            is ApplyStatus.APPLIED
        )
        assert (
            await store.apply(reaction(advanced, "other-actor", actor=304)) is ApplyStatus.APPLIED
        )
        total = await score(store)
        assert total.reaction_failures == total.raw_faults == 2
        assert total.ordinary_posts == total.human_messages == 0
        assert (await score(store, round_id="round-a")).reaction_failures == 1
        assert (await score(store, round_id="round-b")).reaction_failures == 1
        async with store.transaction(write=False) as unit:
            assert await unit.pending_markers() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", list(Phase))
async def test_whispers_persist_traffic_without_enrolling_or_scoring(tmp_path, phase):
    ctx = context("NR004", position=replace(POSITION, phase=phase))
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        decision = message(
            ctx, text="-# whole message", attachments=(AttachmentSnapshot(501, "private-name.txt"),)
        )
        assert await store.apply(decision) is ApplyStatus.APPLIED
        assert await store.apply(decision) is ApplyStatus.DUPLICATE_EVENT
        assert await score(store) is None
        async with store.transaction(write=False) as unit:
            assert await unit.get_traffic("session-a") == TrafficTotals(1, 1)
            assert await unit.get_traffic("session-a", round_id="round-a") == TrafficTotals(1, 1)
            assert await unit.pending_markers() == ()


@pytest.mark.asyncio
async def test_unrated_media_participation_is_stored_separately_from_whispers(tmp_path):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        assert await store.apply(message(ctx, text=None)) is ApplyStatus.APPLIED
        assert await store.apply(message(ctx, "whisper", "-# traffic")) is ApplyStatus.APPLIED
        total = await score(store)
        assert total.ordinary_posts == total.human_messages == 1
        assert total.actual_percent is None and total.whispers == 0
        async with store.transaction(write=False) as unit:
            assert await unit.get_traffic("session-a") == TrafficTotals(2, 1)


@pytest.mark.asyncio
async def test_revival_pause_preserves_round_totals_and_ended_owner_is_terminal(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    paused = replace(
        ctx,
        position=replace(
            POSITION, lifecycle=Lifecycle.PAUSED, pause_reason=PauseReason.REVIVAL, revision=1
        ),
    )
    resumed = replace(ctx, position=replace(POSITION, revision=2))
    ended = replace(ctx, position=replace(POSITION, lifecycle=Lifecycle.ENDED, revision=3))
    async with open_game_store(path) as store:
        await seed(store, ctx)
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
        async with store.transaction() as unit:
            assert await unit.set_owner(POSITION, paused)
        assert await store.apply(message(ctx, "while-paused")) is ApplyStatus.STALE_OWNER
        async with store.transaction() as unit:
            assert await unit.set_owner(paused.position, resumed)
        assert await store.apply(message(resumed, "after-pause")) is ApplyStatus.APPLIED
        async with store.transaction() as unit:
            assert await unit.set_owner(resumed.position, ended)
            assert not await unit.set_owner(
                ended.position, replace(ctx, position=replace(POSITION, revision=4))
            )
            new_route = replace(ROUTE, session_id="new-game")
            await unit.insert_session(context("NR034", position=replace(POSITION, route=new_route)))
        assert await store.apply(message(resumed, "after-end")) is ApplyStatus.STALE_OWNER
        assert (await score(store, round_id="round-a")).ordinary_posts == 2


@pytest.mark.asyncio
async def test_channel_reservation_and_source_namespace_are_database_constraints(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        same_channel = context(
            "NR034", position=replace(POSITION, route=replace(ROUTE, session_id="other"))
        )
        with pytest.raises(sqlite3.IntegrityError):
            await seed(store, same_channel)
        other = context("NR034", position=replace(POSITION, route=SessionRoute(101, 203, "other")))
        await seed(store, other)
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
        assert await store.apply(message(other)) is ApplyStatus.APPLIED
        assert (await score(store, session="other")).ordinary_posts == 1


@pytest.mark.asyncio
async def test_invalid_derived_inputs_never_consume_a_source_guard(tmp_path):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        assert await store.apply(message(ctx)) is ApplyStatus.UNKNOWN_SESSION
        await seed(store, ctx)
        good = message(ctx)
        for bad in (
            replace(good, score=ScoreTotals()),
            replace(good, evaluations=()),
            replace(good, source_key=("wrong-session", EventKind.MESSAGE_CREATE, "message-a")),
        ):
            with pytest.raises(ValueError):
                await store.apply(bad)
        assert await score(store) is None
        assert await store.apply(good) is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_arbitrary_failure_text_is_not_a_persistable_reason_code(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        decision = message(ctx, text="wrong")
        unsafe = replace(
            decision, evaluations=(RuleEvaluation("rule-0", Outcome.FAIL, "CHAT_ONLY_CANARY"),)
        )
        with pytest.raises(ValueError, match="reason codes"):
            await store.apply(unsafe)
        assert await store.apply(decision) is ApplyStatus.APPLIED
    assert query(path, "SELECT DISTINCT reason_code FROM nr_fault_counts") == [
        ("required_ending_word",)
    ]


@pytest.mark.asyncio
async def test_two_connections_can_compare_and_set_the_owner_only_once(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as first, open_game_store(path) as second:
        await seed(first, ctx)
        candidates = [
            replace(ctx, position=replace(POSITION, revision=1, advancement=value))
            for value in (Advancement.WARNING, Advancement.HELD)
        ]

        async def change(store, candidate):
            async with store.transaction() as unit:
                return await unit.set_owner(POSITION, candidate)

        results = await asyncio.gather(change(first, candidates[0]), change(second, candidates[1]))
        assert results.count(True) == 1 and results.count(False) == 1
        async with first.transaction(write=False) as unit:
            assert await unit.get_context("session-a") == candidates[results.index(True)]


@pytest.mark.asyncio
async def test_private_chat_metadata_never_enters_storage_but_frozen_configuration_does(tmp_path):
    path = tmp_path / "game.sqlite3"
    clause = RuleInstance(
        "clause", "NR036", (("phrase", "dancing duck"), ("phrase_revision", "r1"))
    )
    ctx = EvaluationContext(
        POSITION, (clause,), (EmojiIdentity(unicode="❤️"), EmojiIdentity(custom_id=701))
    )
    async with open_game_store(path) as store:
        await seed(store, ctx)
        decision = message(
            ctx,
            text="CHAT_ONLY_CANARY https://example.invalid/SECRET_URL",
            attachments=(AttachmentSnapshot(501, "SECRET_FILENAME.txt"),),
        )
        assert await store.apply(decision) is ApplyStatus.APPLIED
        async with store.transaction(write=False) as unit:
            assert await unit.get_context("session-a") == ctx
    with closing(sqlite3.connect(path)) as conn:
        dump = "\n".join(conn.iterdump())
    assert "dancing duck" in dump
    assert all(value not in dump for value in ("CHAT_ONLY_CANARY", "SECRET_URL", "SECRET_FILENAME"))


@pytest.mark.asyncio
async def test_frozen_clause_instances_keep_independent_fault_aggregates(tmp_path):
    path = tmp_path / "game.sqlite3"
    clauses = tuple(
        RuleInstance(f"clause-{i}", "NR036", (("phrase", phrase),))
        for i, phrase in enumerate(("red duck", "blue goose"))
    )
    ctx = EvaluationContext(POSITION, clauses)
    async with open_game_store(path) as store:
        await seed(store, ctx)
        await store.apply(message(ctx, text="neither clause"))
        await store.apply(message(ctx, "second", "red duck"))
        total = await score(store)
        assert total.ordinary_quality == Fraction(1, 2)
        assert total.raw_faults == 3 and total.offending_messages == 2
    assert query(
        path, "SELECT rule_instance_id,faults FROM nr_fault_counts ORDER BY rule_instance_id"
    ) == [("clause-0", 1), ("clause-1", 2)]


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["parser_version", "evaluator_version", "score_version"])
async def test_incompatible_pinned_semantics_refuse_reinterpretation(tmp_path, column):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        execute(path, f"UPDATE nr_sessions SET {column}='future-fixture-version'")
        with pytest.raises(IncompatibleState):
            await store.apply(message(ctx))
        assert query(path, "SELECT COUNT(*) FROM nr_processed_events") == [(0,)]


@pytest.mark.asyncio
async def test_decimal_text_fraction_storage_is_exact_beyond_integer_and_float_ranges(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        await store.apply(message(ctx))
        numerator, denominator = 10**60 + 1, 10**61 + 7
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(
                "UPDATE nr_participants SET ordinary_quality_n=?,ordinary_quality_d=?,ordinary_faults=1,ordinary_offending_messages=1",
                (str(numerator), str(denominator)),
            )
        assert (await score(store)).ordinary_quality == Fraction(numerator, denominator)
        execute(path, "UPDATE nr_participants SET ordinary_quality_d='0'")
        with pytest.raises(IncompatibleState, match="Stored score is invalid"):
            await score(store)


@pytest.mark.asyncio
async def test_cancellation_rolls_back_before_the_shared_connection_is_reused(tmp_path):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        entered = asyncio.Event()
        never = asyncio.Event()

        async def worker():
            async with store.transaction() as unit:
                await unit.apply(message(ctx))
                entered.set()
                await never.wait()

        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await score(store) is None
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_cancellation_after_begin_is_queued_cannot_leak_a_transaction(tmp_path, monkeypatch):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        began, never = asyncio.Event(), asyncio.Event()
        real_execute = storage._execute
        first = True

        async def gated(conn, sql, values=()):
            nonlocal first
            result = await real_execute(conn, sql, values)
            if sql == "BEGIN IMMEDIATE" and first:
                first = False
                began.set()
                await never.wait()
            return result

        monkeypatch.setattr(storage, "_execute", gated)
        task = asyncio.create_task(store.apply(message(ctx)))
        await began.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_abandon_rollback(tmp_path, monkeypatch):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        entered, rollback_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        real_rollback = store._conn.rollback

        async def gated_rollback():
            rollback_started.set()
            await release.wait()
            await real_rollback()

        monkeypatch.setattr(store._conn, "rollback", gated_rollback)

        async def worker():
            async with store.transaction() as unit:
                await unit.apply(message(ctx))
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(worker())
        await entered.wait()
        task.cancel()
        await rollback_started.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await score(store) is None
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_cancelled_commit_acknowledgement_is_safely_retried_by_source_key(
    tmp_path, monkeypatch
):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        await seed(store, ctx)
        committed, release = asyncio.Event(), asyncio.Event()
        real_commit = store._conn.commit

        async def delayed_ack():
            await real_commit()
            committed.set()
            await release.wait()

        monkeypatch.setattr(store._conn, "commit", delayed_ack)
        task = asyncio.create_task(store.apply(message(ctx)))
        await committed.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await store.apply(message(ctx)) is ApplyStatus.DUPLICATE_EVENT
        assert (await score(store)).ordinary_posts == 1


@pytest.mark.asyncio
async def test_close_drains_an_active_unit_and_rejects_new_work(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        entered, release = asyncio.Event(), asyncio.Event()

        async def worker():
            async with store.transaction() as unit:
                await unit.apply(message(ctx))
                entered.set()
                await release.wait()

        task = asyncio.create_task(worker())
        await entered.wait()
        closing_task = asyncio.create_task(store.close())
        await asyncio.sleep(0)  # Let close stop admission; no wall-clock wait.
        with pytest.raises(RuntimeError, match="closed"):
            await store.apply(message(ctx, "late"))
        assert not closing_task.done()
        release.set()
        await task
        await closing_task
    assert query(path, "SELECT ordinary_posts FROM nr_participants") == [(1,)]


@pytest.mark.asyncio
async def test_bounded_admission_and_unit_lifetime_fail_explicitly(tmp_path):
    ctx = context("NR034")
    async with open_game_store(tmp_path / "game.sqlite3", max_pending=1) as store:
        await seed(store, ctx)
        async with store.transaction() as unit:
            with pytest.raises(StoreOverloaded):
                await asyncio.create_task(store.apply(message(ctx)))
            with pytest.raises(RuntimeError, match="Nested"):
                await store.apply(message(ctx))
            with pytest.raises(RuntimeError, match="owner task"):
                await asyncio.create_task(unit.get_context("session-a"))
        with pytest.raises(RuntimeError, match="active owner"):
            await unit.get_context("session-a")
        async with store.transaction(write=False) as unit:
            with pytest.raises(RuntimeError, match="read-only"):
                await unit.insert_session(ctx)
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
    with pytest.raises(RuntimeError, match="closed"):
        await store.apply(message(ctx))


@pytest.mark.asyncio
async def test_real_write_contention_fails_boundedly_then_retries_same_source(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path, busy_timeout_ms=10) as store:
        await seed(store, ctx)
        with closing(sqlite3.connect(path)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            try:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    await store.apply(message(ctx))
            finally:
                lock.rollback()
        assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
        assert (await score(store)).ordinary_posts == 1


@pytest.mark.asyncio
async def test_wal_reader_snapshot_does_not_block_committing_gameplay(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        with closing(sqlite3.connect(path)) as reader:
            reader.execute("BEGIN")
            assert reader.execute("SELECT COUNT(*) FROM nr_processed_events").fetchone() == (0,)
            assert await store.apply(message(ctx)) is ApplyStatus.APPLIED
            assert reader.execute("SELECT COUNT(*) FROM nr_processed_events").fetchone() == (0,)
            reader.rollback()
            assert reader.execute("SELECT COUNT(*) FROM nr_processed_events").fetchone() == (1,)


@pytest.mark.asyncio
async def test_runtime_pragmas_query_indexes_and_cleanup_foreign_keys(tmp_path):
    path = tmp_path / "game.sqlite3"
    ctx = context("NR006", "NR034")
    async with open_game_store(path) as store:
        await seed(store, ctx)
        await store.apply(message(ctx, text="wrong"))
        await store.apply(reaction(ctx))
        for sql, expected in (
            ("PRAGMA foreign_keys", 1),
            ("PRAGMA synchronous", 2),
            ("PRAGMA busy_timeout", 1000),
        ):
            assert (await storage._row(store._conn, sql))[0] == expected
        for table, where in (
            (
                "nr_processed_events",
                "session_id='s' AND event_kind='message_create' AND source_id='m'",
            ),
            ("nr_reaction_offenses", "session_id='s' AND actor_id=1 AND target_message_id=2"),
            ("nr_participants", "session_id='s' AND actor_id=1"),
        ):
            plan = query(path, f"EXPLAIN QUERY PLAN SELECT * FROM {table} WHERE {where}")
            assert any("SEARCH" in row[3] for row in plan)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(
            "DELETE FROM nr_sessions"
        )  # Administrative fixture, not an application purge API.
        for table in (
            "nr_rounds",
            "nr_participants",
            "nr_round_scores",
            "nr_processed_events",
            "nr_reaction_offenses",
            "nr_marker_intents",
            "nr_fault_counts",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [{"max_pending": 0}, {"max_pending": True}, {"busy_timeout_ms": 0}, {"busy_timeout_ms": 10001}],
)
async def test_bad_store_bounds_fail_before_creating_a_database(tmp_path, kwargs):
    path = tmp_path / "game.sqlite3"
    with pytest.raises(ValueError):
        async with open_game_store(path, **kwargs):
            pytest.fail("Invalid store bounds were accepted")
    assert not path.exists()
