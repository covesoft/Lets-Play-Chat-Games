"""General New Rules progress against real WAL files and actual flow activation."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack, closing
from dataclasses import replace
from datetime import timedelta
from random import Random

import pytest
from test_ordinary_flow import (
    NOW,
    POSITION,
    ROUTE,
    Scene,
    execute,
    incoming,
    published,
    rows,
    scene,
    selected,
)

from lets_play.new_rules.contracts import (
    Deadline,
    DeadlineKind,
    EmojiIdentity,
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    ReactionEvent,
    RuleInstance,
)
from lets_play.new_rules.evaluation import EvaluationContext, evaluate_message
from lets_play.new_rules.hands import build_hand
from lets_play.new_rules.scoring import ScoreTotals
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, open_game_store
from lets_play.storage.ordinary_flow import FlowStatus, OrdinaryFlow
from lets_play.storage.progression import Progression, ResolutionStatus


@pytest.mark.asyncio
async def test_streak_notices_are_persisted_once_and_best_run_survives_missing_round(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        now = 0
        for _ in range(10):
            for _ in range(5):
                await post(s, at=now)
            now, _ = await close_round(s, at=now + 1)
        p = await progress(store)
        assert (p.current_clean_streak, p.best_clean_streak, p.personal_rounds_played) == (
            10,
            10,
            10,
        )
        assert "very_careful" not in (await profile(store)).badges
        async with store.transaction(write=False) as unit:
            notices = await Progression(unit).pending_streak_notices()
        assert [n.milestone for n in notices] == [3, 5, 7, 9]
        async with store.transaction() as unit:
            await Progression(unit).acknowledge_streak_notice(notices[0].notice_id)
            await Progression(unit).acknowledge_streak_notice(notices[0].notice_id)
        now, _ = await close_round(s, at=now + 1)
        assert (await progress(store)).current_clean_streak == 0
        for _ in range(3):
            for _ in range(5):
                await post(s, at=now)
            now, _ = await close_round(s, at=now + 1)
        assert (await progress(store)).best_clean_streak == 10
        async with store.transaction(write=False) as unit:
            notices = await Progression(unit).pending_streak_notices()
        assert [n.milestone for n in notices] == [5, 7, 9, 3]
        assert await ended(s) is ResolutionStatus.RESOLVED
        assert "very_careful" in (await profile(store)).badges
    async with open_game_store(path) as store:
        async with store.transaction(write=False) as unit:
            assert await Progression(unit).pending_streak_notices() == notices


@pytest.mark.asyncio
async def test_late_joiner_has_no_prearrival_rounds_and_a_missed_round_breaks_streak(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        now = 0
        for _ in range(2):
            for _ in range(5):
                await post(s, at=now)
            now, _ = await close_round(s, at=now + 1)
        assert await progress(store, 404) is None
        for _ in range(5):
            await post(s, 404, at=now)
        now, _ = await close_round(s, at=now + 1)
        p = await progress(store, 404)
        assert (p.current_clean_streak, p.personal_rounds_played) == (1, 1)
        now, _ = await close_round(s, at=now + 1)
        p = await progress(store, 404)
        assert (p.current_clean_streak, p.best_clean_streak, p.personal_rounds_played) == (0, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("players", [4, 5])
async def test_party_animal_uses_distinct_completed_rounds_and_participation_not_accuracy(
    tmp_path, players
):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        now = 0
        for round_index in range(3):
            for actor in range(303, 303 + players):
                for _ in range(5):
                    await post(s, actor, text="wrong" if actor == 303 else "over", at=now)
            if round_index < 2:
                now, _ = await close_round(s, at=now + 1)
                assert "party_animal" not in (await profile(store)).badges
            else:
                await ended(s)
        assert ("party_animal" in (await profile(store)).badges) == (players == 5)
        assert "spotless" not in (await profile(store)).badges
        assert (await progress(store)).party_rounds == (3 if players == 5 else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rounds,best,award",
    [
        (2, 2, False),
        (3, 3, True),
        (11, 3, True),
        (12, 3, False),
        (14, 4, True),
        (15, 4, False),
        (29, 9, True),
        (30, 9, False),
    ],
)
async def test_very_careful_final_personal_round_floor_boundaries(tmp_path, rounds, best, award):
    # Rehearse the finalization boundary with already-resolved earlier progress.
    # The final current round contributes the last activity-qualified round.
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s, text="wrong")
        execute(
            path,
            f"UPDATE nr_player_progress SET personal_rounds_played={rounds - 1},best_clean_streak={best}",
        )
        assert "very_careful" not in (await profile(store)).badges
        await ended(s)
        assert ("very_careful" in (await profile(store)).badges) == award
        assert (await progress(store)).personal_rounds_played == rounds


@pytest.mark.asyncio
async def test_cutover_resolution_failure_rolls_back_owner_closure_and_awards(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
        )
        execute(
            path,
            "CREATE TRIGGER fail_award BEFORE INSERT ON nr_badges BEGIN SELECT RAISE(ABORT,'late resolution failure'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await s.act("tick", EventKind.TIMER, 15)
        assert (await s.context()).position.round_id == "old-round"
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert (await profile(store)).badges == ()
        execute(path, "DROP TRIGGER fail_award")
        assert (await s.act("tick", EventKind.TIMER, 15)).status is FlowStatus.ACTIVATED
        assert "rulemaker" in (await profile(store)).badges


@pytest.mark.asyncio
async def test_caught_failure_is_rollback_only_and_finalization_failure_does_not_end_owner(
    tmp_path,
):
    from lets_play.storage.game_store import TransactionAborted

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        ctx = await s.context()
        execute(
            path,
            "CREATE TRIGGER fail_finalize BEFORE INSERT ON nr_game_finalizations BEGIN SELECT RAISE(ABORT,'late finalization'); END",
        )
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                await unit.set_owner(
                    ctx.position,
                    replace(
                        ctx, position=replace(ctx.position, lifecycle=Lifecycle.ENDED, revision=1)
                    ),
                )
                try:
                    await Progression(unit).finalize_ended("session")
                except sqlite3.IntegrityError:
                    pass
        assert (await s.context()).position.lifecycle is Lifecycle.PLAYING
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert "spotless" not in (await profile(store)).badges
        execute(path, "DROP TRIGGER fail_finalize")
        assert await ended(s) is ResolutionStatus.RESOLVED


@pytest.mark.asyncio
async def test_cancellation_after_qualification_work_rolls_back_credit_and_score(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    original = Progression.credit_ordinary
    entered = asyncio.Event()
    hold = asyncio.Event()

    async def hold_after_credit(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        entered.set()
        await hold.wait()

    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(4):
            await post(s)
        monkeypatch.setattr(Progression, "credit_ordinary", hold_after_credit)
        task = asyncio.create_task(post(s))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await profile(store)).games_played == 0
        assert rows(path, "SELECT ordinary_posts FROM nr_participants") == [(4,)]
        monkeypatch.setattr(Progression, "credit_ordinary", original)
        await post(s)
        assert (await profile(store)).games_played == 1


@pytest.mark.asyncio
async def test_read_only_and_expired_units_cannot_mutate_progress(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            p = Progression(unit)
            with pytest.raises(RuntimeError, match="read-only"):
                await p.credit_ordinary("session")
            with pytest.raises(RuntimeError, match="read-only"):
                await p.resolve_round("session", "old-round")
            with pytest.raises(RuntimeError, match="read-only"):
                await p.finalize_ended("session")
        with pytest.raises(RuntimeError, match="owner task"):
            await p.profile(303)


async def profile(store, actor=303):
    async with store.transaction(write=False) as unit:
        return await Progression(unit).profile(actor)


async def progress(store, actor=303, session_id="session"):
    async with store.transaction(write=False) as unit:
        return await Progression(unit).progress(session_id, actor)


async def seed(store, *, session="session", channel=202, cards=("NR034", "NR006")):
    ctx = EvaluationContext(
        replace(POSITION, route=replace(ROUTE, session_id=session, channel_id=channel)),
        tuple(RuleInstance(f"r-{c}", c) for c in cards),
    )
    async with store.transaction() as unit:
        await unit.insert_session(ctx)
    return Scene(store, ctx, None)


async def post(s, actor=303, text="over", *, at=0):
    frame = replace(s.frame(EventKind.MESSAGE_CREATE, at, actor), route=s.ctx.position.route)
    async with s.store.transaction() as unit:
        result = await OrdinaryFlow(unit).ingest(MessageEvent(frame, text))
    s.sequence += 1
    return result


async def close_round(s, *, at=10):
    ctx = await s.context()
    active = {item.card_id for item in ctx.active_instances}
    card = next(c for c in ("NR022", "NR025", "NR027") if c not in active)
    offer_id = f"offer-{s.sequence}"
    s.hand = build_hand(
        ctx,
        offer_id=offer_id,
        picker_id=303,
        deadline=Deadline(offer_id, DeadlineKind.CHOICE, NOW + timedelta(seconds=at + 60)),
        rng=Random(0),
        available_templates=frozenset({card, "NR004", "NR013"}),
    ).hand
    assert s.hand is not None
    assert (
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, at, s.hand)
    ).status is FlowStatus.PREPARED
    await s.act(
        "publish_offer", EventKind.OFFER_PUBLISHED, at, offer_id, message_id=1000 + s.sequence
    )
    option = next(o for o in s.hand.options if o.incoming.card_id == card)
    result = await s.act(
        "choose", EventKind.CHOICE_SUBMIT, at + 1, offer_id, option.incoming.instance_id, actor=303
    )
    if result.status is FlowStatus.NEEDS_TARGET:
        result = await s.act(
            "choose",
            EventKind.CHOICE_SUBMIT,
            at + 2,
            offer_id,
            option.incoming.instance_id,
            outgoing_id=option.outgoing_ids[0],
            actor=303,
        )
    assert result.status is FlowStatus.SELECTED
    await s.act(
        "publish_activation",
        EventKind.ACTIVATION_PUBLISHED,
        at + 3,
        offer_id,
        message_id=2000 + s.sequence,
    )
    async with s.store.transaction(write=False) as unit:
        record = await OrdinaryFlow(unit).get_offer("session", offer_id)
    seconds = (record.effective_at - NOW).total_seconds()
    assert (await s.act("tick", EventKind.TIMER, seconds)).status is FlowStatus.ACTIVATED
    return seconds, offer_id


async def ended(s):
    current = await s.context()
    async with s.store.transaction() as unit:
        assert await unit.set_owner(
            current.position,
            replace(
                current,
                position=replace(
                    current.position,
                    lifecycle=Lifecycle.ENDED,
                    pause_reason=None,
                    revision=current.position.revision + 1,
                ),
            ),
        )
        return await Progression(unit).finalize_ended("session")


@pytest.mark.asyncio
async def test_fifth_post_commits_credit_once_reopen_and_further_posts(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(4):
            await post(s)
        assert (await profile(store)).games_played == 0
        assert not (await progress(store)).game_count_credited
        await post(s)
        assert (await profile(store)).games_played == 1
        assert (await progress(store)).game_count_credited
        frame = replace(
            s.frame(EventKind.MESSAGE_CREATE, 0, 303),
            sequence=s.sequence,
            source_id="message_create-5",
        )
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).ingest(MessageEvent(frame, "over"))
            ).status is FlowStatus.REPLAY
    async with open_game_store(path) as store:
        s.store = store
        await post(s)
        async with store.transaction() as unit:
            await Progression(unit).credit_ordinary("session", 303)
        assert (await profile(store)).games_played == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,cards,credit,spotless",
    [
        ("over", ("NR034",), True, True),
        ("wrong", ("NR034",), True, False),
        (None, ("NR034",), True, False),
        ("-# over", ("NR034",), False, False),
    ],
)
async def test_credit_and_clean_sample_are_different(tmp_path, text, cards, credit, spotless):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store, cards=cards)
        for _ in range(5):
            await post(s, text=text)
        await ended(s)
        p = await profile(store)
        assert p.games_played == int(credit)
        assert ("spotless" in p.badges) == spotless
        live = await progress(store)
        if credit:
            assert live.personal_rounds_played == 1
            assert live.current_clean_streak == int(spotless)
        else:
            assert live is None


@pytest.mark.asyncio
async def test_reaction_only_does_not_credit_and_reaction_fault_breaks_clean(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        frame = s.frame(EventKind.REACTION_ADD, 0, 404)
        async with store.transaction() as unit:
            await OrdinaryFlow(unit).ingest(ReactionEvent(frame, 900, EmojiIdentity(unicode="👍")))
        s.sequence += 1
        assert (await profile(store, 404)).games_played == 0
        for _ in range(5):
            await post(s)
        frame = s.frame(EventKind.REACTION_ADD, 0, 303)
        async with store.transaction() as unit:
            await OrdinaryFlow(unit).ingest(ReactionEvent(frame, 901, EmojiIdentity(unicode="👍")))
        s.sequence += 1
        await ended(s)
        assert (await profile(store)).games_played == 1
        assert "spotless" not in (await profile(store)).badges
        assert (await progress(store)).current_clean_streak == 0


@pytest.mark.asyncio
async def test_ordinary_posts_across_rounds_count_one_game(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(3):
            await post(s)
        time, _ = await close_round(s)
        assert (await profile(store)).games_played == 0
        for _ in range(2):
            await post(s, at=time)
        assert (await profile(store)).games_played == 1
        assert (await progress(store)).personal_rounds_played == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [0, 8, 9, 10])
async def test_familiar_face_atomic_threshold_and_existing_badge_never_blocks_credit(
    tmp_path, initial
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        execute(path, f"INSERT INTO nr_profiles VALUES (303,{initial})")
        if initial == 10:
            execute(path, "INSERT INTO nr_badges VALUES (303,'familiar_face')")
        for _ in range(5):
            await post(s)
        p = await profile(store)
        assert p.games_played == initial + 1
        assert ("familiar_face" in p.badges) == (initial + 1 >= 10)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["nr_profiles", "nr_badges", "nr_player_progress"])
async def test_late_credit_failure_rolls_back_score_flag_profile_and_watermark(tmp_path, trigger):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        execute(path, "INSERT INTO nr_profiles VALUES (303,9)")
        for _ in range(4):
            await post(s)
        action = "INSERT" if trigger == "nr_badges" else "UPDATE"
        execute(
            path,
            f"CREATE TRIGGER fail_credit BEFORE {action} ON {trigger} BEGIN SELECT RAISE(ABORT,'credit failure'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await post(s)
        assert (await profile(store)).games_played == 9
        assert not (await progress(store)).game_count_credited
        assert rows(path, "SELECT ordinary_posts FROM nr_participants") == [(4,)]
        assert rows(path, "SELECT last_sequence FROM nr_flow_watermarks") == [(4,)]
        execute(path, "DROP TRIGGER fail_credit")
        await post(s)
        assert (await profile(store)).games_played == 10
        assert "familiar_face" in (await profile(store)).badges


@pytest.mark.asyncio
async def test_same_fifth_message_two_connections_is_one_credit(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        s = await seed(stores[0])
        for _ in range(4):
            await post(s)
        event = MessageEvent(s.frame(EventKind.MESSAGE_CREATE, 0, 303), "over")

        async def run(store):
            async with store.transaction() as unit:
                return (await OrdinaryFlow(unit).ingest(event)).status

        assert sorted(await asyncio.gather(*(run(store) for store in stores))) == sorted(
            [FlowStatus.INGESTED, FlowStatus.REPLAY]
        )
        assert (await profile(stores[0])).games_played == 1


@pytest.mark.asyncio
async def test_two_sessions_same_user_concurrently_increment_global_count_twice(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        contexts = []
        for index, store in enumerate(stores):
            s = await seed(store, session=f"s{index}", channel=202 + index)
            ctx = s.ctx
            for n in range(4):
                frame = replace(
                    s.frame(EventKind.MESSAGE_CREATE, 0, 303),
                    route=ctx.position.route,
                    source_id=f"m{n}",
                )
                assert (
                    await store.apply(evaluate_message(MessageEvent(frame, "over"), ctx))
                    is ApplyStatus.APPLIED
                )
            contexts.append(ctx)
        await asyncio.gather(
            *(
                store.apply(
                    evaluate_message(
                        MessageEvent(
                            replace(
                                Scene(store, ctx, None).frame(EventKind.MESSAGE_CREATE, 0, 303),
                                route=ctx.position.route,
                                source_id="fifth",
                            ),
                            "over",
                        ),
                        ctx,
                    )
                )
                for store, ctx in zip(stores, contexts, strict=True)
            )
        )
        assert (await profile(stores[0])).games_played == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("human,partial", [(True, False), (False, False), (True, True)])
async def test_rulemaker_requires_activation_and_deliberate_incoming_choice(
    tmp_path, human, partial
):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        if human:
            if partial:
                result = await s.act(
                    "choose", EventKind.CHOICE_SUBMIT, 10, "offer", incoming(s), actor=303
                )
                assert result.status is FlowStatus.NEEDS_TARGET
                await s.act("timeout", EventKind.CHOICE_TIMEOUT, 65, "offer", rng=Random(0))
                when = 66
            else:
                await selected(s)
                when = 11
        else:
            await s.act("timeout", EventKind.CHOICE_TIMEOUT, 65, "offer", rng=Random(0))
            when = 66
        assert "rulemaker" not in (await profile(store)).badges
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, when, "offer", message_id=1002
        )
        rec = await s.record()
        await s.act("tick", EventKind.TIMER, (rec.effective_at - NOW).total_seconds())
        p = await profile(store)
        assert p.games_played == 0
        assert ("rulemaker" in p.badges) == human
        async with store.transaction() as unit:
            assert (
                await Progression(unit).resolve_round("session", "old-round")
                is ResolutionStatus.ALREADY_DONE
            )
            await Progression(unit).award_activated_choice("session", "offer")
        assert await profile(store) == p


@pytest.mark.asyncio
async def test_failed_publication_preserves_open_round_and_grants_no_rulemaker(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        await selected(s)
        await s.act("tick", EventKind.TIMER, 15)
        async with store.transaction() as unit:
            assert (
                await Progression(unit).resolve_round("session", "old-round")
                is ResolutionStatus.NOT_CLOSED
            )
            assert not await Progression(unit).award_activated_choice("session", "offer")
        assert (await profile(store)).badges == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["playing", "revival", "recovery"])
async def test_open_and_paused_rounds_cannot_resolve_or_finalize(tmp_path, state):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        if state != "playing":
            ctx = await s.context()
            async with store.transaction() as unit:
                await unit.set_owner(
                    ctx.position,
                    replace(
                        ctx,
                        position=replace(
                            ctx.position,
                            lifecycle=Lifecycle.PAUSED,
                            pause_reason=PauseReason(state),
                            revision=1,
                        ),
                    ),
                )
        async with store.transaction() as unit:
            p = Progression(unit)
            assert await p.resolve_round("session", "old-round") is ResolutionStatus.NOT_CLOSED
            assert await p.finalize_ended("session") is ResolutionStatus.NOT_CLOSED
        assert (await progress(store)).personal_rounds_played == 0
        assert "spotless" not in (await profile(store)).badges


@pytest.mark.asyncio
async def test_real_cutover_resolves_old_round_and_finalization_is_once_only(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        when, offer = await close_round(s)
        assert "spotless" in (await profile(store)).badges
        assert (await progress(store)).current_clean_streak == 1
        for _ in range(5):
            await post(s, at=when)
        assert await ended(s) is ResolutionStatus.RESOLVED
        before = await progress(store)
        async with store.transaction() as unit:
            assert (
                await Progression(unit).finalize_ended("session") is ResolutionStatus.ALREADY_DONE
            )
        assert await progress(store) == before
        assert before.personal_rounds_played == 2 and before.current_clean_streak == 2
        assert "very_careful" not in (await profile(store)).badges
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(2,)]
    async with open_game_store(path) as store:
        async with store.transaction() as unit:
            assert (
                await Progression(unit).finalize_ended("session") is ResolutionStatus.ALREADY_DONE
            )
        assert await progress(store) == before


@pytest.mark.asyncio
async def test_progression_pin_mismatch_rolls_back_fifth_post(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(4):
            await post(s)
        execute(path, "UPDATE nr_sessions SET progression_version='future'")
        with pytest.raises(IncompatibleState):
            await post(s)
        assert rows(path, "SELECT ordinary_posts FROM nr_participants") == [(4,)]
        assert (await profile(store)).games_played == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", [(1 << 63) - 1])
async def test_profile_counter_overflow_is_an_atomic_failure(tmp_path, initial):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        execute(path, f"INSERT INTO nr_profiles VALUES (303,{initial})")
        for _ in range(4):
            await post(s)
        with pytest.raises(sqlite3.IntegrityError):
            await post(s)
        assert (await profile(store)).games_played == initial
        assert rows(path, "SELECT ordinary_posts FROM nr_participants") == [(4,)]


@pytest.mark.asyncio
async def test_profile_survives_gameplay_cascade_without_session_archive_or_chat(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(5):
            await post(s, text="PRIVATE_CHAT_CANARY over")
        await ended(s)
        before = await profile(store)
        async with store.transaction() as unit:
            await unit._conn.execute("DELETE FROM nr_sessions WHERE session_id='session'")
        assert await profile(store) == before
        assert await progress(store) is None
        assert rows(path, "PRAGMA foreign_key_check") == []
        with closing(sqlite3.connect(path)) as conn:
            dump = "\n".join(conn.iterdump())
            assert "PRIVATE_CHAT_CANARY" not in dump and "old-round" not in dump


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_bounded_reads_reject_invalid_limits(tmp_path, limit):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            p = Progression(unit)
            with pytest.raises(ValueError):
                await p.pending_rounds("session", limit=limit)
            with pytest.raises(ValueError):
                await p.pending_streak_notices(limit=limit)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [0, -1, True, 1.0, 1 << 63])
async def test_profile_reads_validate_ids_and_do_not_create_rows(tmp_path, actor):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            with pytest.raises(ValueError):
                await Progression(unit).profile(actor)
        assert (await profile(store)).games_played == 0
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_profiles") == [(0,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("faults", [0, 1])
async def test_aggregate_mode_faults_block_clean_without_fabricating_mode_credit(tmp_path, faults):
    # Only stored score arithmetic is exercised; there is no implemented mode ingress.
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(5):
            await post(s)
        async with store.transaction() as unit:
            await unit._add_score(
                "session",
                "old-round",
                303,
                ScoreTotals(human_messages=2, mode_observations=2, mode_quality=2 - faults),
            )
        await ended(s)
        assert ("spotless" in (await profile(store)).badges) == (faults == 0)
        assert (await profile(store)).games_played == 1


@pytest.mark.asyncio
async def test_mode_successes_alone_are_not_ordinary_game_credit_or_clean_sample(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction() as unit:
            await unit._add_score(
                "session",
                "old-round",
                303,
                ScoreTotals(human_messages=20, mode_observations=20, mode_quality=20),
            )
        await ended(s)
        assert (await profile(store)).games_played == 0
        assert (await profile(store)).badges == ()


@pytest.mark.asyncio
async def test_lost_commit_acknowledgement_retries_without_double_credit(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for _ in range(4):
            await post(s)
        committed, release = asyncio.Event(), asyncio.Event()
        real = store._conn.commit

        async def delayed_ack():
            await real()
            committed.set()
            await release.wait()

        monkeypatch.setattr(store._conn, "commit", delayed_ack)
        task = asyncio.create_task(post(s))
        await asyncio.wait_for(committed.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await post(s)).status is FlowStatus.REPLAY
        assert (await profile(store)).games_played == 1
        assert (await progress(store)).game_count_credited


@pytest.mark.asyncio
async def test_new_receipt_duplicate_source_does_not_earn_a_fifth_post(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        for _ in range(4):
            await post(s)
        frame = replace(s.frame(EventKind.MESSAGE_CREATE, 0, 303), source_id="message_create-4")
        async with store.transaction() as unit:
            result = await OrdinaryFlow(unit).ingest(MessageEvent(frame, "over"))
        s.sequence += 1
        assert result.score_status is ApplyStatus.DUPLICATE_EVENT
        assert (await profile(store)).games_played == 0
        await post(s)
        assert (await profile(store)).games_played == 1
