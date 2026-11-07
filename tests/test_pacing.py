"""Published warning clocks, immutable score snapshots and persistent rotation."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
from random import Random

import pytest
from test_ordinary_flow import NOW, POSITION, Scene, execute, rows

from lets_play.new_rules.contracts import (
    Deadline,
    DeadlineKind,
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    RuleInstance,
)
from lets_play.new_rules.evaluation import EvaluationContext
from lets_play.new_rules.hands import build_hand
from lets_play.new_rules.pacing import message_target
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, open_game_store
from lets_play.storage.ordinary_flow import FlowOrderError, FlowStatus, OrdinaryFlow
from lets_play.storage.pacing_flow import RoundPacing


async def seed(store, *, estimate=3, previous=None, rng=None):
    ctx = EvaluationContext(
        POSITION, (RuleInstance("over", "NR034"), RuleInstance("reaction", "NR006"))
    )
    async with store.transaction() as unit:
        await unit.insert_session(ctx)
    s = Scene(store, ctx, None)
    assert (
        await act(
            s,
            "register_round",
            EventKind.PACING_REGISTER,
            0,
            estimated_players=estimate,
            rng=rng or Random(0),
            started_at=NOW,
            previous_picker_id=previous,
        )
        == "registered"
    )
    return s


async def act(s, method, kind, seconds, *args, **kwargs):
    frame = s.frame(kind, seconds)
    async with s.store.transaction() as unit:
        result = await getattr(RoundPacing(unit), method)(frame, *args, **kwargs)
    s.sequence += 1
    return result


async def post(s, *, actor=303, text="over", at=0, **kwargs):
    frame = s.frame(EventKind.MESSAGE_CREATE, at, actor)
    async with s.store.transaction() as unit:
        result = await OrdinaryFlow(unit).ingest(MessageEvent(frame, text, **kwargs))
    s.sequence += 1
    return result


async def record(s, round_id=None):
    async with s.store.transaction(write=False) as unit:
        return await RoundPacing(unit).record("session", round_id)


async def candidates(s, round_id="old-round"):
    async with s.store.transaction(write=False) as unit:
        return await RoundPacing(unit).candidates("session", round_id)


async def warning(s, *, at=870, publish_at=None):
    assert await act(s, "tick", EventKind.TIMER, at) == "warning_queued"
    assert (
        await act(
            s,
            "publish_warning",
            EventKind.WARNING_PUBLISHED,
            publish_at if publish_at is not None else at,
            "old-round",
            message_id=1001,
        )
        == "published"
    )


def score(
    *,
    posts=5,
    observations=5,
    quality=5,
    faults=0,
    offending=0,
    reactions=0,
    mode=0,
    mode_quality=0,
):
    return ScoreTotals(
        human_messages=posts + mode,
        ordinary_posts=posts,
        ordinary_observations=observations,
        ordinary_quality=quality,
        ordinary_faults=faults,
        ordinary_offending_messages=offending,
        reaction_failures=reactions,
        mode_observations=mode,
        mode_quality=mode_quality,
    )


async def aggregate(s, actor, total):
    # Saved derived score fixture, not a fabricated message/mode validation claim.
    async with s.store.transaction() as unit:
        await unit._add_score("session", "old-round", actor, total)


@pytest.mark.parametrize("estimate,target", [(0, 15), (3, 15), (6, 30), (10, 50)])
def test_frozen_message_target(estimate, target):
    assert message_target(estimate) == target


@pytest.mark.parametrize("estimate", [-1, True, 1.5, 1 << 63])
def test_invalid_estimate_is_not_coerced(estimate):
    with pytest.raises(ValueError):
        message_target(estimate)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "estimate,count,early", [(3, 15, True), (6, 29, False), (6, 30, True), (10, 50, True)]
)
async def test_early_target_latches_until_four_thirty_or_falls_back_at_fourteen_thirty(
    tmp_path, estimate, count, early
):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store, estimate=estimate)
        for _ in range(count):
            await post(s)
        r = await record(s)
        assert r.estimated_players == estimate and r.message_target == message_target(estimate)
        assert r.next_deadline == NOW + timedelta(seconds=270 if early else 870)
        assert await act(s, "tick", EventKind.TIMER, 269) == "waiting"
        assert await act(s, "tick", EventKind.TIMER, 270) == (
            "warning_queued" if early else "waiting"
        )
        if not early:
            assert await act(s, "tick", EventKind.TIMER, 870) == "warning_queued"
        assert (await s.context()).position.round_id == "old-round"


@pytest.mark.asyncio
async def test_target_reached_at_eight_minutes_queues_one_warning_and_duplicates_add_nothing(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(14):
            await post(s)
        assert await act(s, "tick", EventKind.TIMER, 479) == "waiting"
        assert (await post(s, at=480)).score_status is ApplyStatus.APPLIED
        assert (await record(s)).stage == "warning_queued"
        assert (await record(s)).target_reached_at == NOW + timedelta(seconds=480)
        for _ in range(3):
            await post(s, actor=404, at=480)
        assert rows(path, "SELECT COUNT(*) FROM nr_pacing_outputs WHERE kind='warning'") == [(1,)]
        assert (await record(s)).estimated_players == 3


@pytest.mark.asyncio
async def test_whisper_bot_and_system_traffic_do_not_advance_target(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(15):
            await post(s, text="-# over")
            await post(s, is_bot=True)
            await post(s, is_system=True)
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(0,)]
        assert await act(s, "tick", EventKind.TIMER, 270) == "waiting"
        for _ in range(15):
            await post(s, text=None, at=270)
        assert (await record(s)).stage == "warning_queued"
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(15,)]


@pytest.mark.asyncio
async def test_offending_posts_count_toward_aggregate_target(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(15):
            await post(s, text="wrong")
        assert await act(s, "tick", EventKind.TIMER, 270) == "warning_queued"


@pytest.mark.asyncio
async def test_delayed_publication_receives_full_thirty_seconds_past_normal_max(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        await warning(s, at=870, publish_at=890)
        assert (await record(s)).selection_due_at == NOW + timedelta(seconds=920)
        assert await act(s, "tick", EventKind.TIMER, 900) == "warning_published"
        assert await act(s, "tick", EventKind.TIMER, 919) == "warning_published"
        assert await act(s, "tick", EventKind.TIMER, 920) == "selected"
        assert (await record(s)).picker_id == 303


@pytest.mark.asyncio
async def test_preparation_without_ack_has_no_running_warning_or_picker_clock(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        assert await act(s, "tick", EventKind.TIMER, 870) == "warning_queued"
        assert await act(s, "tick", EventKind.TIMER, 5000) == "warning_queued"
        r = await record(s)
        assert r.warning_published_at is None and r.selection_due_at is None and r.picker_id is None
        assert (await s.context()).position.lifecycle is Lifecycle.PLAYING
        assert (await post(s, at=5000)).score_status is ApplyStatus.APPLIED


@pytest.mark.asyncio
async def test_warning_ack_retry_reopen_and_identity_conflict_do_not_reset_deadline(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        await warning(s)
        assert (
            await act(
                s, "publish_warning", EventKind.WARNING_PUBLISHED, 880, "old-round", message_id=1001
            )
            == "already_done"
        )
        with pytest.raises(ValueError, match="identity"):
            await act(
                s, "publish_warning", EventKind.WARNING_PUBLISHED, 881, "old-round", message_id=1002
            )
        assert (await record(s)).selection_due_at == NOW + timedelta(seconds=900)
    async with open_game_store(path) as store:
        s.store = store
        async with store.transaction(write=False) as unit:
            assert await RoundPacing(unit).known_warning_message_id("session", "old-round") == 1001
        assert await act(s, "tick", EventKind.TIMER, 900) == "selected"
        chosen = (await record(s)).picker_id
        assert (
            await act(
                s, "publish_warning", EventKind.WARNING_PUBLISHED, 901, "old-round", message_id=1001
            )
            == "already_done"
        )
        assert (await record(s)).picker_id == chosen


@pytest.mark.asyncio
async def test_cutoff_snapshot_is_immutable_while_old_round_scoring_continues(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        for _ in range(4):
            await post(s, actor=404)
        await warning(s)
        # An event received at the deadline crosses the snapshot before its own score.
        assert (await post(s, actor=404, at=900)).score_status is ApplyStatus.APPLIED
        assert [c.actor_id for c in await candidates(s)] == [303]
        before = await candidates(s)
        for _ in range(5):
            await post(s, actor=505, text="wrong", at=901)
        assert await candidates(s) == before
        async with store.transaction(write=False) as unit:
            assert (await unit.get_score("session", 404, round_id="old-round")).ordinary_posts == 5
            assert (await unit.get_score("session", 505, round_id="old-round")).raw_faults == 5
        assert (await s.context()).position.round_id == "old-round"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scores,excluded",
    [
        (
            {
                303: score(quality=4, faults=5, offending=1),
                404: score(quality=3, faults=2, offending=2),
            },
            {404},
        ),
        (
            {
                303: score(quality=4, faults=1, offending=1),
                404: score(quality=4, faults=1, offending=1),
                505: score(),
            },
            {303, 404},
        ),
        (
            {
                303: score(quality=4, faults=1, offending=1),
                404: score(quality=4, faults=1, offending=1),
            },
            set(),
        ),
        ({303: score(), 404: score(observations=0, quality=0)}, set()),
        (
            {303: score(quality=0, faults=5, offending=5), 404: score(observations=0, quality=0)},
            {303},
        ),
        ({303: score(observations=0, quality=0), 404: score(observations=0, quality=0)}, set()),
        (
            {303: score(reactions=1), 404: score(quality=Fraction(9, 2), faults=1, offending=1)},
            {303},
        ),
        (
            {
                303: score(quality=Fraction(19, 4), faults=1, offending=1),
                404: score(quality=Fraction(949999, 200000), faults=1, offending=1),
            },
            {404},
        ),
        (
            {
                303: score(quality=3, faults=2, offending=2, mode=20, mode_quality=20),
                404: score(quality=4, faults=1, offending=1),
            },
            {404},
        ),
    ],
)
async def test_exact_actual_compliance_filter_preserves_ties_and_unrated_candidates(
    tmp_path, scores, excluded
):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for actor, total in scores.items():
            await aggregate(s, actor, total)
        await warning(s)
        assert await act(s, "tick", EventKind.TIMER, 900) == "selected"
        snapshot = await candidates(s)
        assert {c.actor_id for c in snapshot if c.excluded} == excluded
        assert (await record(s)).picker_id in set(scores) - excluded


@pytest.mark.asyncio
async def test_sole_imperfect_previous_picker_can_choose(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store, previous=303)
        for _ in range(5):
            await post(s, text="wrong")
        await warning(s)
        assert await act(s, "tick", EventKind.TIMER, 900) == "selected"
        assert (await record(s)).picker_id == 303


@pytest.mark.asyncio
async def test_avoid_consecutive_picker_when_another_legal_successor_exists(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store, previous=303)
        for actor in (303, 404):
            for _ in range(5):
                await post(s, actor=actor)
        await warning(s)
        await act(s, "tick", EventKind.TIMER, 900)
        assert (await record(s)).picker_id == 404


@pytest.mark.asyncio
async def test_no_activity_qualified_players_pauses_for_revival_without_closing_round(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for actor in (303, 404, 505):
            for _ in range(4):
                await post(s, actor=actor)
        await warning(s)
        assert await act(s, "tick", EventKind.TIMER, 900) == "revival"
        ctx = await s.context()
        assert (
            ctx.position.lifecycle is Lifecycle.PAUSED
            and ctx.position.pause_reason is PauseReason.REVIVAL
        )
        assert (
            ctx.position.round_id == "old-round" and ctx.active_instances == s.ctx.active_instances
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]
        assert (await post(s, at=901)).status is FlowStatus.IGNORED
        async with store.transaction(write=False) as unit:
            assert [o.kind for o in await RoundPacing(unit).pending_outputs()] == ["revival"]


@pytest.mark.asyncio
async def test_timer_cannot_skip_an_earlier_queued_receipt(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(4):
            await post(s)
        await warning(s)
        future = replace(s.frame(EventKind.TIMER, 900), sequence=s.sequence + 2)
        with pytest.raises(FlowOrderError):
            async with store.transaction() as unit:
                await RoundPacing(unit).tick(future)
        await post(s, at=899)
        assert await act(s, "tick", EventKind.TIMER, 900) == "selected"


@pytest.mark.asyncio
async def test_raw_offer_preparation_cannot_bypass_warning_or_frozen_picker(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)

        async def hand(picker):
            return build_hand(
                await s.context(),
                offer_id="offer",
                picker_id=picker,
                deadline=Deadline("choice", DeadlineKind.CHOICE, NOW + timedelta(seconds=1000)),
                rng=Random(1),
            ).hand

        assert (
            await s.act("prepare_offer", EventKind.OFFER_PREPARE, 1, await hand(303))
        ).status is FlowStatus.STALE
        await warning(s)
        await act(s, "tick", EventKind.TIMER, 900)
        assert (
            await s.act("prepare_offer", EventKind.OFFER_PREPARE, 900, await hand(404))
        ).status is FlowStatus.UNAUTHORIZED
        assert (
            await s.act("prepare_offer", EventKind.OFFER_PREPARE, 900, await hand(303))
        ).status is FlowStatus.PREPARED


@pytest.mark.asyncio
async def test_competing_snapshot_timers_choose_once_and_do_not_consume_service(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        s = await seed(stores[0])
        for actor in (303, 404):
            for _ in range(5):
                await post(s, actor=actor)
        await warning(s)
        frame = s.frame(EventKind.TIMER, 900)

        async def run(store):
            async with store.transaction() as unit:
                return await RoundPacing(unit).tick(frame)

        assert sorted(await asyncio.gather(*(run(store) for store in stores))) == [
            "replay",
            "selected",
        ]
        assert rows(path, "SELECT COUNT(*) FROM nr_picker_candidates") == [(2,)]
        assert rows(path, "SELECT served_cycle FROM nr_rotation_members ORDER BY actor_id") == [
            (-1,),
            (-1,),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["warning", "snapshot"])
async def test_late_write_failure_rolls_back_receipt_owner_snapshot_and_output(tmp_path, stage):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        if stage == "snapshot":
            await warning(s)
        table = "nr_pacing_outputs" if stage == "warning" else "nr_picker_candidates"
        execute(
            path,
            f"CREATE TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'late failure'); END",
        )
        before = await s.context()
        at = 870 if stage == "warning" else 900
        with pytest.raises(sqlite3.IntegrityError):
            await act(s, "tick", EventKind.TIMER, at)
        assert await s.context() == before
        assert rows(path, "SELECT COUNT(*) FROM nr_picker_candidates") == [(0,)]
        execute(path, "DROP TRIGGER fail")
        assert await act(s, "tick", EventKind.TIMER, at) == (
            "warning_queued" if stage == "warning" else "selected"
        )


@pytest.mark.asyncio
async def test_paused_or_ended_owner_does_not_select_or_reopen(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        await warning(s)
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
        assert await act(s, "tick", EventKind.TIMER, 900) == "cancelled"
        assert (await s.context()).position.lifecycle is Lifecycle.ENDED
        assert not await candidates(s)
        async with store.transaction(write=False) as unit:
            assert not await RoundPacing(unit).pending_outputs()


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_bounded_snapshot_and_output_reads(tmp_path, limit):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            with pytest.raises(ValueError):
                await RoundPacing(unit).pending_outputs(limit=limit)
            with pytest.raises(ValueError):
                await RoundPacing(unit).candidates("session", "old-round", limit=limit)


async def select_round(s, start, actors):
    for actor in actors:
        for _ in range(5):
            await post(s, actor=actor, at=start)
    ctx = await s.context()
    round_id = ctx.position.round_id
    # A timer works independently of a further chat receipt.
    assert await act(s, "tick", EventKind.TIMER, start + 870) == "warning_queued"
    await act(
        s,
        "publish_warning",
        EventKind.WARNING_PUBLISHED,
        start + 870,
        round_id,
        message_id=5000 + s.sequence,
    )
    assert await act(s, "tick", EventKind.TIMER, start + 900) == "selected"
    return await record(s), start + 900


async def activate(s, at, *, acknowledge=True, fail_at=None):
    ctx = await s.context()
    chosen = (await record(s)).picker_id
    active = {i.card_id for i in ctx.active_instances}
    card = next(c for c in ("NR022", "NR025", "NR027") if c not in active)
    offer_id = f"offer-{s.sequence}"
    hand = build_hand(
        ctx,
        offer_id=offer_id,
        picker_id=chosen,
        deadline=Deadline(offer_id, DeadlineKind.CHOICE, NOW + timedelta(seconds=at + 60)),
        rng=Random(1),
        available_templates=frozenset({card, "NR004", "NR013"}),
    ).hand
    assert (
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, at, hand)
    ).status is FlowStatus.PREPARED
    await s.act(
        "publish_offer", EventKind.OFFER_PUBLISHED, at, offer_id, message_id=6000 + s.sequence
    )
    option = next(o for o in hand.options if o.incoming.card_id == card)
    target = option.outgoing_ids[0] if option.outgoing_ids else None
    assert (
        await s.act(
            "choose",
            EventKind.CHOICE_SUBMIT,
            at + 1,
            offer_id,
            option.incoming.instance_id,
            outgoing_id=target,
            actor=chosen,
        )
    ).status is FlowStatus.SELECTED
    if acknowledge:
        await s.act(
            "publish_activation",
            EventKind.ACTIVATION_PUBLISHED,
            at + 2,
            offer_id,
            message_id=7000 + s.sequence,
        )
    result = await s.act("tick", EventKind.TIMER, fail_at if fail_at is not None else at + 6)
    assert result.status is (FlowStatus.ACTIVATED if acknowledge else FlowStatus.RECOVERY)
    return at + 6


@pytest.mark.asyncio
async def test_rotation_is_shuffled_without_replacement_and_newcomer_joins_unserved_pool(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        now = 0
        picks = []
        for _ in range(3):
            snap, at = await select_round(s, now, (303, 404, 505))
            picks.append(snap.picker_id)
            assert rows(path, "SELECT cycle FROM nr_rotation") == [(0,)]
            now = await activate(s, at)
        assert len(set(picks)) == 3
        assert rows(path, "SELECT served_cycle FROM nr_rotation_members ORDER BY actor_id") == [
            (0,),
            (0,),
            (0,),
        ]
        snap, at = await select_round(s, now, (303, 404, 505, 606))
        assert snap.picker_id == 606
        assert rows(path, "SELECT cycle FROM nr_rotation") == [(0,)]
        now = await activate(s, at)
        snap, at = await select_round(s, now, (303, 404, 505, 606))
        assert snap.picker_id != 606
        await activate(s, at)
        assert rows(path, "SELECT cycle FROM nr_rotation") == [(1,)]


@pytest.mark.asyncio
async def test_initial_known_opening_picker_counts_as_already_served(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store, previous=303)
        snap, at = await select_round(s, 0, (303, 404, 505))
        assert snap.picker_id in {404, 505}
        now = await activate(s, at)
        snap2, at = await select_round(s, now, (303, 404, 505))
        assert snap2.picker_id == ({404, 505} - {snap.picker_id}).pop()


@pytest.mark.asyncio
async def test_service_is_not_consumed_by_selected_or_unfulfilled_offer(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        snap, at = await select_round(s, 0, (303, 404))
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(None,)]
        assert rows(path, "SELECT served_cycle FROM nr_rotation_members") == [(-1,), (-1,)]
        await activate(s, at, acknowledge=False)
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(None,)]
        assert rows(path, "SELECT served_cycle FROM nr_rotation_members") == [(-1,), (-1,)]
        assert (await s.context()).position.round_id == "old-round"


@pytest.mark.asyncio
async def test_next_target_uses_final_old_activity_and_clock_uses_announced_activation(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store, estimate=10)
        snap, at = await select_round(s, 0, (303, 404))
        for actor in (505, 606):
            for _ in range(5):
                await post(s, actor=actor, at=at)
        assert {c.actor_id for c in await candidates(s)} == {303, 404}
        # A late activation timer does not restart the announced round clock.
        await activate(s, at, fail_at=at + 50)
        new = await record(s)
        assert new.estimated_players == 4 and new.message_target == 20
        assert new.started_at == NOW + timedelta(seconds=at + 6)
        assert new.next_deadline == NOW + timedelta(seconds=at + 6 + 870)
        assert (await record(s, "old-round")).stage == "completed"


@pytest.mark.asyncio
async def test_late_rotation_failure_rolls_back_activation_and_general_resolution(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        snap, at = await select_round(s, 0, (303, 404))
        execute(
            path,
            "CREATE TRIGGER fail_rotation BEFORE UPDATE ON nr_rotation BEGIN SELECT RAISE(ABORT,'late rotation'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await activate(s, at)
        assert (await s.context()).position.round_id == "old-round"
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_badges") == [(0,)]
        execute(path, "DROP TRIGGER fail_rotation")
        assert (await s.act("tick", EventKind.TIMER, at + 6)).status is FlowStatus.ACTIVATED
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(snap.picker_id,)]


@pytest.mark.asyncio
async def test_cancelled_snapshot_retry_uses_same_saved_seed_and_rolls_back_every_row(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    original = RoundPacing._snapshot
    entered, release = asyncio.Event(), asyncio.Event()
    prepared = []

    async def hold(self, *args):
        await original(self, *args)
        prepared.append((await self.record("session")).picker_id)
        entered.set()
        await release.wait()

    async with open_game_store(path) as store:
        s = await seed(store)
        for actor in (303, 404, 505):
            for _ in range(5):
                await post(s, actor=actor)
        await warning(s)
        monkeypatch.setattr(RoundPacing, "_snapshot", hold)
        task = asyncio.create_task(act(s, "tick", EventKind.TIMER, 900))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rows(path, "SELECT COUNT(*) FROM nr_picker_candidates") == [(0,)]
        assert (await record(s)).stage == "warning_published"
        monkeypatch.setattr(RoundPacing, "_snapshot", original)
        assert await act(s, "tick", EventKind.TIMER, 900) == "selected"
        assert (await record(s)).picker_id == prepared[0]


@pytest.mark.asyncio
async def test_registration_retry_never_redraws_seed_or_changes_start_estimate(tmp_path):
    class Once:
        calls = 0

        def randrange(self, stop):
            self.calls += 1
            assert self.calls == 1
            return 5

    path = tmp_path / "game.sqlite3"
    rng = Once()
    async with open_game_store(path) as store:
        s = await seed(store, rng=rng)
        assert (
            await act(
                s,
                "register_round",
                EventKind.PACING_REGISTER,
                10,
                estimated_players=3,
                rng=rng,
                started_at=NOW,
            )
            == "already_done"
        )
        assert (
            await act(
                s,
                "register_round",
                EventKind.PACING_REGISTER,
                11,
                estimated_players=4,
                rng=rng,
                started_at=NOW,
            )
            == "invalid"
        )
        assert (await record(s)).started_at == NOW
        assert (await record(s)).estimated_players == 3
        assert rng.calls == 1


@pytest.mark.asyncio
async def test_unknown_used_legacy_start_is_not_guessed(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        ctx = EvaluationContext(POSITION, (RuleInstance("over", "NR034"),))
        async with store.transaction() as unit:
            await unit.insert_session(ctx)
        s = Scene(store, ctx, None)
        await post(s)
        with pytest.raises(IncompatibleState, match="known activation time"):
            await act(
                s,
                "register_round",
                EventKind.PACING_REGISTER,
                10,
                estimated_players=3,
                rng=Random(0),
                started_at=NOW,
            )
        assert await record(s) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [PauseReason.CAPABILITY, PauseReason.REVIVAL])
async def test_existing_pause_reason_survives_warning_cancellation(tmp_path, reason):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        await warning(s)
        ctx = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                ctx.position,
                replace(
                    ctx,
                    position=replace(
                        ctx.position,
                        lifecycle=Lifecycle.PAUSED,
                        pause_reason=reason,
                        revision=ctx.position.revision + 1,
                    ),
                ),
            )
        assert await act(s, "tick", EventKind.TIMER, 900) == "cancelled"
        assert (await s.context()).position.pause_reason is reason


@pytest.mark.asyncio
async def test_stale_owner_requires_explicit_recovery_and_never_silently_selects(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        await warning(s)
        ctx = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                ctx.position,
                replace(ctx, position=replace(ctx.position, revision=ctx.position.revision + 1)),
            )
        assert await act(s, "tick", EventKind.TIMER, 900) == "cancelled"
        assert (await s.context()).position.pause_reason is PauseReason.RECOVERY
        async with store.transaction(write=False) as unit:
            assert [o.kind for o in await RoundPacing(unit).pending_outputs()] == ["recovery"]


@pytest.mark.asyncio
async def test_pacing_pin_mismatch_rolls_back_receipt(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        last = s.sequence
        execute(path, "UPDATE nr_sessions SET pacing_version='future'")
        with pytest.raises(IncompatibleState):
            await post(s)
        assert rows(path, "SELECT last_sequence FROM nr_flow_watermarks") == [(last,)]
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(0,)]


@pytest.mark.asyncio
async def test_snapshot_pages_include_candidates_beyond_first_hundred_without_truncating(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction() as unit:
            for actor in range(1000, 1125):
                await unit._add_score(
                    "session", "old-round", actor, score(quality=4, faults=1, offending=1)
                )
            await unit._add_score("session", "old-round", 9999, score())
        await warning(s)
        assert await act(s, "tick", EventKind.TIMER, 900) == "selected"
        assert (await record(s)).picker_id == 9999
        async with store.transaction(write=False) as unit:
            api = RoundPacing(unit)
            first = await api.candidates("session", "old-round", limit=100)
            rest = await api.candidates(
                "session", "old-round", after_actor=first[-1].actor_id, limit=100
            )
        assert len(first) == 100 and len(rest) == 26
        assert rest[-1].actor_id == 9999 and not rest[-1].excluded


@pytest.mark.asyncio
async def test_duplicate_source_and_reaction_do_not_fill_target(tmp_path):
    from lets_play.new_rules.contracts import EmojiIdentity, ReactionEvent

    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(14):
            await post(s)
        frame = replace(
            s.frame(EventKind.MESSAGE_CREATE, 0, 303), source_id=f"message_create-{s.sequence}"
        )
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).ingest(MessageEvent(frame, "over"))
            ).score_status is ApplyStatus.DUPLICATE_EVENT
        s.sequence += 1
        frame = s.frame(EventKind.REACTION_ADD, 0, 404)
        async with store.transaction() as unit:
            await OrdinaryFlow(unit).ingest(ReactionEvent(frame, 1234, EmojiIdentity(unicode="👍")))
        s.sequence += 1
        assert await act(s, "tick", EventKind.TIMER, 270) == "waiting"


@pytest.mark.asyncio
async def test_stale_warning_output_is_filtered_before_a_timer_cancels_it(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        assert await act(s, "tick", EventKind.TIMER, 870) == "warning_queued"
        async with store.transaction(write=False) as unit:
            assert [o.kind for o in await RoundPacing(unit).pending_outputs()] == ["warning"]
        ctx = await s.context()
        async with store.transaction() as unit:
            await unit.set_owner(
                ctx.position,
                replace(ctx, position=replace(ctx.position, revision=ctx.position.revision + 1)),
            )
        async with store.transaction(write=False) as unit:
            assert not await RoundPacing(unit).pending_outputs()


@pytest.mark.asyncio
async def test_caught_pacing_write_error_cannot_commit_partial_snapshot(tmp_path):
    from lets_play.storage.game_store import TransactionAborted

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        await warning(s)
        before = s.sequence
        execute(
            path,
            "CREATE TRIGGER fail BEFORE INSERT ON nr_rotation_members BEGIN SELECT RAISE(ABORT,'snapshot failure'); END",
        )
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                try:
                    await RoundPacing(unit).tick(s.frame(EventKind.TIMER, 900))
                except sqlite3.IntegrityError:
                    pass
        assert rows(path, "SELECT last_sequence FROM nr_flow_watermarks") == [(before,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_picker_candidates") == [(0,)]


@pytest.mark.asyncio
async def test_pacing_writes_reject_read_only_unit(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction(write=False) as unit:
            with pytest.raises(RuntimeError, match="read-only"):
                await RoundPacing(unit).tick(s.frame(EventKind.TIMER, 870))
