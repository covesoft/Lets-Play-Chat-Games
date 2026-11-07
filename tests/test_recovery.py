"""Disruptive-startup barrier, publication evidence and exact recovery boundaries."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta

import pytest
from test_evolution import held, invited
from test_opening import CAPS, Lane, quorum, select
from test_ordinary_flow import NOW, execute, incoming, published, rows, scene, selected
from test_pacing import post, seed, warning

from lets_play.new_rules.contracts import (
    Advancement,
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    Phase,
)
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, TransactionAborted, open_game_store
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.opening_flow import OpeningStatus
from lets_play.storage.ordinary_flow import FlowOrderError, FlowStatus, OrdinaryFlow
from lets_play.storage.pacing_flow import RoundPacing
from lets_play.storage.progression import Progression
from lets_play.storage.recovery_flow import RecoveryFlow, RecoveryRequired


async def rec(s, method, kind, at, *args, actor=None, **kwargs):
    frame = s.frame(kind, at, actor)
    async with s.store.transaction() as unit:
        result = await getattr(RecoveryFlow(unit), method)(frame, *args, **kwargs)
    s.sequence += 1
    return result


async def begin(s, at=50, token="r1"):
    return await rec(s, "begin", EventKind.RECOVERY_BEGIN, at, token)


async def approve(s, at=51, token="r1", **kwargs):
    return await rec(
        s,
        "approve",
        EventKind.RECOVERY_APPROVE,
        at,
        token,
        capabilities=kwargs.pop("capabilities", CAPS),
        **kwargs,
    )


async def notice(s, at=60, token="r1", **kwargs):
    return await rec(
        s,
        "publish_notice",
        EventKind.RECOVERY_NOTICE_PUBLISHED,
        at,
        token,
        message_id=kwargs.pop("message_id", 9001),
        **kwargs,
    )


async def observe(s, at, token="r1", *, text=None, actor=303, source=None):
    frame = s.frame(
        EventKind.TIMER if text is None else EventKind.MESSAGE_CREATE,
        at,
        None if text is None else actor,
    )
    if source:
        frame = replace(frame, source_id=source)
    async with s.store.transaction() as unit:
        result = await RecoveryFlow(unit).observe(
            frame, token, event=MessageEvent(frame, text) if text is not None else None
        )
    s.sequence += 1
    return result


async def work(s, token="r1"):
    async with s.store.transaction(write=False) as unit:
        return await RecoveryFlow(unit).work("session", token)


async def record(s, token="r1"):
    async with s.store.transaction(write=False) as unit:
        return await RecoveryFlow(unit).record("session", token)


async def resolve_all(s, at, *, found=None):
    for output in await work(s):
        kw = found or {}
        assert await rec(
            s,
            "reconcile",
            EventKind.RECOVERY_RECONCILE,
            at,
            "r1",
            output.family,
            output.output_id,
            **kw,
        ) in {"unsent", "found"}


@pytest.mark.asyncio
async def test_disruption_pauses_before_due_timer_and_notice_ack_starts_read_in(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        original = await s.context()
        assert await begin(s) == "paused"
        assert (await s.context()).position.pause_reason is PauseReason.RECOVERY
        assert await approve(s) == "ready"
        assert (await record(s)).resume_at is None
        assert await observe(s, 59, text="PRIVATE_RECOVERY_CANARY") == "paused"
        assert rows(path, "SELECT COUNT(*) FROM nr_participants") == [(0,)]
        assert await notice(s) == "reading"
        assert (await record(s)).resume_at == NOW + timedelta(seconds=90)
        assert await observe(s, 89, text="over", source="paused-source") == "paused"
        result = await observe(s, 90, text="over")
        assert result.score_status is ApplyStatus.APPLIED
        p = (await s.context()).position
        assert p.lifecycle is Lifecycle.PLAYING and p.round_id == original.position.round_id
        assert p.ruleset_version == original.position.ruleset_version
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        frame = replace(s.frame(EventKind.MESSAGE_CREATE, 91, 303), source_id="paused-source")
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).ingest(MessageEvent(frame, "over"))
            ).score_status is ApplyStatus.DUPLICATE_EVENT
        assert "PRIVATE_RECOVERY_CANARY" not in str(rows(path, "SELECT * FROM nr_paused_sources"))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["ingest", "tick", "choice", "raw_apply"])
async def test_normal_dispatch_is_fenced_without_consuming_receipt_or_progress(tmp_path, method):
    from lets_play.new_rules.evaluation import evaluate_message

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        before = await s.context()
        await begin(s)
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        frame = s.frame(EventKind.MESSAGE_CREATE, 51, 303)
        with pytest.raises(RecoveryRequired):
            async with store.transaction() as unit:
                flow = OrdinaryFlow(unit)
                if method == "ingest":
                    await flow.ingest(MessageEvent(frame, "over"))
                elif method == "tick":
                    await flow.tick(replace(frame, kind=EventKind.TIMER))
                elif method == "choice":
                    await flow.choose(
                        replace(frame, kind=EventKind.CHOICE_SUBMIT), "stale", "stale"
                    )
                else:
                    unit._ordered_flow = True
                    await unit.apply(evaluate_message(MessageEvent(frame, "over"), before))
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark
        assert rows(path, "SELECT COUNT(*) FROM nr_processed_events") == [(0,)]
        assert await observe(s, 51, text="over") == "paused"


@pytest.mark.asyncio
async def test_repeated_begin_ack_reopen_and_lost_notice_ack_preserve_original_read_in(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        assert await begin(s, 51) == "already_done"
        await approve(s, 52)
        assert await notice(s, 100, published_at=NOW + timedelta(seconds=60)) == "reading"
        assert (await record(s)).resume_at == NOW + timedelta(seconds=90)
        assert await notice(s, 101, published_at=NOW + timedelta(seconds=60)) == "already_done"
        with pytest.raises(ValueError, match="identity"):
            await notice(s, 102, message_id=9999)
        n = s.sequence
    async with open_game_store(path) as store:
        s.store = store
        s.sequence = n
        assert await observe(s, 103) == "resumed"
        assert await notice(s, 104) == "already_done"
        assert (await record(s)).resume_at == NOW + timedelta(seconds=90)


@pytest.mark.asyncio
async def test_new_disruption_supersedes_old_notice_and_does_not_resume_from_old_token(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        await begin(s)
        await approve(s)
        await notice(s)
        assert await begin(s, 70, "r2") == "paused"
        assert (await record(s)).stage == "superseded"
        assert await observe(s, 90) == "stale"
        assert (await s.context()).position.lifecycle is Lifecycle.PAUSED
        assert await approve(s, 91, "r2") == "ready"
        assert await notice(s, 92, "r2", message_id=9002) == "reading"
        assert await observe(s, 121, "r2") == "paused"
        assert await observe(s, 122, "r2") == "resumed"
        assert (await s.context()).position.lifecycle is Lifecycle.PLAYING


@pytest.mark.asyncio
async def test_unknown_delivery_is_not_an_unsent_fact_and_pending_controls_are_hidden(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        await begin(s, 1)
        assert await approve(s, 2) == "unknown_delivery"
        assert await notice(s, 3) == "unavailable"
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).pending_outputs() == ()
            assert await RecoveryFlow(unit).pending_outputs() == ()
        assert len(await work(s)) == 1
        output = (await work(s))[0]
        assert output.family == "ordinary" and output.kind == "offer" and output.status == "unknown"
        assert (
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                4,
                "r1",
                output.family,
                output.output_id,
            )
            == "unsent"
        )
        assert await approve(s, 5) == "ready"
        async with store.transaction(write=False) as unit:
            assert len(await RecoveryFlow(unit).pending_outputs()) == 1
        await notice(s, 6)
        await observe(s, 36)
        assert (await s.record()).choice_deadline is None
        async with store.transaction(write=False) as unit:
            assert len(await OrdinaryFlow(unit).pending_outputs()) == 1
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 40, "offer", message_id=1001)
        assert (await s.record()).choice_deadline.expires_at == NOW + timedelta(seconds=100)


@pytest.mark.asyncio
async def test_lost_offer_ack_uses_actual_delivery_time_without_another_minute(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        await begin(s, 10)
        output = (await work(s))[0]
        assert (
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                11,
                "r1",
                output.family,
                output.output_id,
                message_id=1001,
                published_at=NOW + timedelta(seconds=5),
            )
            == "found"
        )
        assert (await s.record()).choice_deadline.expires_at == NOW + timedelta(seconds=65)
        await approve(s, 12)
        await notice(s, 20)
        await observe(s, 50)
        assert (await s.record()).choice_deadline.expires_at == NOW + timedelta(seconds=65)
        assert (
            await s.act("choose", EventKind.CHOICE_SUBMIT, 65, "offer", incoming(s), actor=303)
        ).status is FlowStatus.EXPIRED
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(1,)]


@pytest.mark.asyncio
async def test_partial_human_choice_and_original_deadline_survive_pause_resume(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await published(s)
        assert (
            await s.act("choose", EventKind.CHOICE_SUBMIT, 10, "offer", incoming(s), actor=303)
        ).status is FlowStatus.NEEDS_TARGET
        saved = await s.record()
        await begin(s, 11)
        await approve(s, 12)
        await notice(s, 13)
        await observe(s, 43)
        now = await s.record()
        assert now.choice_deadline == saved.choice_deadline
        assert now.selected_incoming == saved.selected_incoming and now.chosen_by_picker
        assert (
            await s.act("choose", EventKind.CHOICE_SUBMIT, 44, "offer", incoming(s), actor=303)
        ).status is FlowStatus.NEEDS_TARGET


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["known", "found", "unsent", "late", "unknown"])
async def test_interrupted_activation_requires_proven_timely_publication_and_never_double_closes(
    tmp_path, ack
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store, phrases=True)
        await published(s)
        await selected(s, phrase=True)
        if ack == "known":
            await s.act(
                "publish_activation", EventKind.ACTIVATION_PUBLISHED, 11, "offer", message_id=1002
            )
        await begin(s, 12)
        if ack in {"found", "late", "unsent"}:
            output = next(o for o in await work(s) if o.kind == "activation")
            kwargs = (
                {}
                if ack == "unsent"
                else {
                    "message_id": 1002,
                    "published_at": NOW + timedelta(seconds=15 if ack == "late" else 11),
                }
            )
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                16,
                "r1",
                output.family,
                output.output_id,
                **kwargs,
            )
        result = await approve(s, 20)
        if ack in {"unknown", "late"}:
            assert result == ("unknown_delivery" if ack == "unknown" else "late_announcement")
            assert (await s.context()).position.pause_reason is PauseReason.RECOVERY
            assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]
            return
        assert result == "ready"
        await notice(s, 21)
        assert await observe(s, 51) == "resumed"
        if ack == "unsent":
            assert (await s.context()).position.advancement is Advancement.HELD
            assert (await s.context()).position.round_id == "old-round"
            assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
            assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(0,)]
        else:
            assert (await s.context()).position.round_id == "offer:round"
            await s.act("tick", EventKind.TIMER, 52)
            assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]
            assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(1,)]
            async with store.transaction(write=False) as unit:
                assert "rulemaker" in (await Progression(unit).profile(303)).badges


@pytest.mark.asyncio
async def test_published_warning_overdue_in_recovery_uses_only_observed_candidates(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for t in range(5):
            await post(s, at=t)
        await warning(s)
        await begin(s, 890)
        await approve(s, 891)
        await notice(s, 892)
        for t in range(4):
            await observe(s, 893 + t, text="over", actor=999)
        assert await observe(s, 922) == "resumed"
        async with store.transaction(write=False) as unit:
            pacing = RoundPacing(unit)
            assert [c.actor_id for c in await pacing.candidates("session", "old-round")] == [303]
            assert (await pacing.record("session")).selection_due_at == NOW + timedelta(seconds=900)
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["held", "revival"])
async def test_resume_preserves_held_idle_anchor_and_original_revival_clock(tmp_path, kind):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        if kind == "held":
            await held(s)
            at = 850
            anchor = rows(path, "SELECT idle_anchor_at_utc FROM nr_evolution")
        else:
            r = await invited(s)
            at = 1100
        await begin(s, at)
        await resolve_all(s, at + 1)
        assert await approve(s, at + 2) == "ready"
        await notice(s, at + 3)
        await observe(s, at + 33)
        if kind == "held":
            assert rows(path, "SELECT idle_anchor_at_utc FROM nr_evolution") == anchor
            await s.act("tick", EventKind.TIMER, 900)
            assert (await s.context()).position.pause_reason is PauseReason.REVIVAL
        else:
            assert (await s.context()).position.pause_reason is PauseReason.REVIVAL
            assert rows(path, "SELECT deadline_utc FROM nr_revivals") == [(utc_int(r.deadline),)]
            assert rows(path, "SELECT COUNT(*) FROM nr_revival_members") == [(0,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("published_notice", [False, True])
async def test_revival_expiry_during_recovery_ends_without_waiting_for_notice_or_read_in(
    tmp_path, published_notice
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await invited(s)
        await begin(s, 1250)
        if published_notice:
            await approve(s, 1251)
            await notice(s, 1280)
        assert await observe(s, 1300) == "closed"
        assert (await s.context()).position.lifecycle is Lifecycle.ENDED
        assert (await record(s)).stage == "closed"
        assert rows(path, "SELECT COUNT(*) FROM nr_game_finalizations") == [(1,)]
        async with store.transaction(write=False) as unit:
            from lets_play.storage.evolution_flow import EvolutionFlow

            assert [o.kind for o in await EvolutionFlow(unit).pending_outputs()] == [
                "closed_silent"
            ]
        assert await observe(s, 1301) == "stale"


@pytest.mark.asyncio
async def test_opening_lobby_retains_absolute_expiry_and_no_fake_playing_owner(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = Lane(store)
        await s.admit()
        await s.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await begin(s, 100)
        await approve(s, 101)
        await notice(s, 110)
        assert await observe(s, 140) == "resumed"
        assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
        assert (await s.record()).lobby_deadline == NOW + timedelta(seconds=185)
        assert await s.act("join", EventKind.LOBBY_JOIN, 141, actor=404) is OpeningStatus.JOINED
        assert await s.act("join", EventKind.LOBBY_JOIN, 142, actor=505) is OpeningStatus.OPENING


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [180, 200])
async def test_expired_lobby_releases_capacity_during_recovery_even_without_notice(tmp_path, at):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = Lane(store)
        await s.admit()
        await s.act("publish", EventKind.LOBBY_PUBLISHED, 0, "lobby", message_id=1001)
        assert await begin(s, at) == "closed"
        assert (await s.record()).stage == "ended"
        assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
        assert await Lane(store, session="fresh").admit(at=at + 1) is OpeningStatus.ADMITTED


@pytest.mark.asyncio
async def test_found_lobby_delivery_can_already_be_expired_when_reconciled(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = Lane(store)
        await s.admit()
        await begin(s, 200)
        output = (await work(s))[0]
        assert (
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                201,
                "r1",
                output.family,
                output.output_id,
                message_id=1001,
                published_at=NOW,
            )
            == "found"
        )
        assert (await record(s)).stage == "closed"
        assert (await s.record()).lobby_deadline == NOW + timedelta(seconds=180)


@pytest.mark.asyncio
async def test_published_opening_choice_survives_read_in_without_resetting_sixty_seconds(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = Lane(store)
        await quorum(s)
        await s.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        saved = (await s.record()).choice_deadline
        await begin(s, 30)
        await approve(s, 31)
        await notice(s, 32)
        await observe(s, 62)
        assert (await s.record()).choice_deadline == saved
        offer = await s.record()
        assert (
            await s.act(
                "choose",
                EventKind.CHOICE_SUBMIT,
                80,
                offer.hand.options[0].incoming.instance_id,
                actor=offer.picker_id,
            )
            is OpeningStatus.EXPIRED
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", ["known", "unsent"])
async def test_opening_activation_recovery_hands_over_same_receipt_or_ends_unannounced_opening(
    tmp_path, ack
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = Lane(store)
        await select(s)
        if ack == "known":
            await s.act(
                "publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003
            )
        await begin(s, 32)
        if ack == "unsent":
            await resolve_all(s, 33)
        result = await approve(s, 34)
        if ack == "unsent":
            assert result == "closed"
            assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
            return
        await notice(s, 40)
        result = await observe(s, 70, text="over", actor=606)
        assert result.score_status is ApplyStatus.APPLIED
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]
        assert rows(path, "SELECT human_messages FROM nr_sessions") == [(1,)]
        assert rows(path, "SELECT started_at_utc FROM nr_rounds") == [
            (utc_int(NOW + timedelta(seconds=35)),)
        ]
        assert (await s.post(71)).score_status is ApplyStatus.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pause", [PauseReason.CAPABILITY, PauseReason.DEPENDENCY, PauseReason.RECOVERY]
)
async def test_preexisting_pause_is_not_silently_waived_by_startup_approval(tmp_path, pause):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction() as unit:
            c = await unit.get_context("session")
            p = c.position
            await unit.set_owner(
                p,
                replace(
                    c,
                    position=replace(
                        p, lifecycle=Lifecycle.PAUSED, pause_reason=pause, revision=p.revision + 1
                    ),
                ),
            )
        await begin(s)
        assert await approve(s) == "existing_pause"
        assert (await s.context()).position.pause_reason is PauseReason.RECOVERY
        assert (await record(s)).original_pause == pause.value


@pytest.mark.asyncio
async def test_mode_recovery_refuses_to_invent_observed_survival_or_resume_policy(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction() as unit:
            c = await unit.get_context("session")
            p = c.position
            await unit.set_owner(
                p, replace(c, position=replace(p, phase=Phase.MODE, revision=p.revision + 1))
            )
        await begin(s)
        assert await approve(s) == "unsupported_mode"
        assert rows(tmp_path / "game.sqlite3", "SELECT COUNT(*) FROM nr_badges") == [(0,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("authorized", [False, True])
async def test_manager_can_end_recovery_without_resume_or_duplicate_finalization(
    tmp_path, authorized
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        result = await rec(
            s, "end", EventKind.MANAGER_END, 51, "r1", actor=999, authorized=authorized
        )
        assert result == ("closed" if authorized else "unauthorized")
        assert rows(path, "SELECT COUNT(*) FROM nr_game_finalizations") == [(int(authorized),)]
        if authorized:
            assert await begin(s, 52, "r2") == "ended"
            assert (
                await rec(s, "end", EventKind.MANAGER_END, 53, "r1", actor=999, authorized=True)
                == "stale"
            )


@pytest.mark.asyncio
async def test_competing_connections_begin_and_resume_once(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        s = await seed(stores[0])
        frame = s.frame(EventKind.RECOVERY_BEGIN, 50)

        async def run(store, method, frame, *args):
            async with store.transaction() as unit:
                return await getattr(RecoveryFlow(unit), method)(frame, *args)

        assert sorted(
            await asyncio.gather(*(run(store, "begin", frame, "r1") for store in stores))
        ) == ["paused", "replay"]
        s.sequence += 1
        await approve(s)
        await notice(s)
        f = s.frame(EventKind.TIMER, 90)
        assert sorted(
            await asyncio.gather(*(run(store, "observe", f, "r1") for store in stores))
        ) == ["replay", "resumed"]
        assert rows(path, "SELECT COUNT(*) FROM nr_recoveries") == [(1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["begin", "resume", "proof"])
async def test_late_failure_rolls_back_watermark_owner_evidence_and_pending_notice(tmp_path, stage):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        if stage == "proof":
            s = await scene(store)
            await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
            await begin(s, 1)
            output = (await work(s))[0]
            target = "nr_recovery_work"
            operation = "UPDATE"
            frame = s.frame(EventKind.RECOVERY_RECONCILE, 2)
        else:
            s = await seed(store)
            if stage == "resume":
                await begin(s)
                await approve(s)
                await notice(s)
            frame = s.frame(
                EventKind.RECOVERY_BEGIN if stage == "begin" else EventKind.TIMER,
                50 if stage == "begin" else 90,
            )
            target = "nr_recoveries"
            operation = "INSERT" if stage == "begin" else "UPDATE"
        before = await s.context()
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        execute(
            path,
            f"CREATE TRIGGER fail_late BEFORE {operation} ON {target} BEGIN SELECT RAISE(ABORT,'late recovery failure'); END",
        )

        async def call(unit):
            service = RecoveryFlow(unit)
            if stage == "begin":
                return await service.begin(frame, "r1")
            if stage == "resume":
                return await service.observe(frame, "r1")
            return await service.reconcile(
                frame, "r1", output.family, output.output_id, message_id=111, published_at=NOW
            )

        with pytest.raises(sqlite3.IntegrityError, match="late recovery"):
            async with store.transaction() as unit:
                await call(unit)
        assert await s.context() == before
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark
        execute(path, "DROP TRIGGER fail_late")
        async with store.transaction() as unit:
            await call(unit)


@pytest.mark.asyncio
async def test_caught_order_error_is_rollback_only_and_cannot_skip_queued_chat(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        await begin(s)
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                with pytest.raises(FlowOrderError):
                    await RecoveryFlow(unit).observe(
                        s.frame(EventKind.TIMER, 100, seq=s.sequence + 2), "r1"
                    )
        assert await observe(s, 51, text="over") == "paused"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, -1])
async def test_discovery_and_output_reads_are_bounded(tmp_path, limit):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            for method, args in [
                ("live_routes", ()),
                ("pending_outputs", ()),
                ("work", ("session", "r1")),
            ]:
                with pytest.raises(ValueError):
                    await getattr(RecoveryFlow(unit), method)(*args, limit=limit)


@pytest.mark.asyncio
async def test_unknown_recovery_pin_refuses_without_consuming_a_receipt(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        execute(path, "UPDATE nr_recoveries SET version='future'")
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        with pytest.raises(IncompatibleState):
            await approve(s)
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "reaction"])
async def test_historical_delivery_after_actual_resume_cannot_backfill_missing_scores(
    tmp_path, kind
):
    from lets_play.new_rules.contracts import EmojiIdentity, ReactionEvent

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        await approve(s)
        await notice(s)
        await observe(s, 100)  # Late recovery receipt: enforcement really resumes at 100, not 90.
        frame = replace(
            s.frame(
                EventKind.MESSAGE_CREATE if kind == "message" else EventKind.REACTION_ADD, 101, 404
            ),
            created_at=NOW + timedelta(seconds=95),
        )
        event = (
            MessageEvent(frame, "PRIVATE_HISTORICAL_CANARY")
            if kind == "message"
            else ReactionEvent(frame, 1234, EmojiIdentity(unicode="x"))
        )
        async with store.transaction() as unit:
            assert (await OrdinaryFlow(unit).ingest(event)).status is FlowStatus.IGNORED
        s.sequence += 1
        assert rows(path, "SELECT COUNT(*) FROM nr_participants") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_reaction_offenses") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_session_activity") == [(0,)]
        assert rows(path, "SELECT resumed_at_utc FROM nr_recoveries") == [
            (utc_int(NOW + timedelta(seconds=100)),)
        ]
        assert (await post(s, at=102)).score_status is ApplyStatus.APPLIED
        assert "PRIVATE_HISTORICAL_CANARY" not in str(rows(path, "SELECT * FROM nr_paused_sources"))


@pytest.mark.asyncio
async def test_first_resume_receipt_with_old_creation_time_is_guarded_not_backfilled(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        await approve(s)
        await notice(s)
        frame = replace(
            s.frame(EventKind.MESSAGE_CREATE, 90, 303), created_at=NOW + timedelta(seconds=70)
        )
        async with store.transaction() as unit:
            result = await RecoveryFlow(unit).observe(
                frame, "r1", event=MessageEvent(frame, "over")
            )
        s.sequence += 1
        assert result.status is FlowStatus.IGNORED
        assert (await record(s)).stage == "resumed"
        assert rows(path, "SELECT COUNT(*) FROM nr_paused_sources") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_participants") == [(0,)]


@pytest.mark.asyncio
async def test_stale_recovery_token_during_new_pause_guards_source_but_cannot_consume_normal_chat_after_resume(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        await begin(s, 51, "r2")
        assert await observe(s, 52, text="over", source="stale-token-source") == "stale"
        assert rows(path, "SELECT COUNT(*) FROM nr_paused_sources") == [(1,)]
        await approve(s, 53, "r2")
        await notice(s, 54, "r2", message_id=9002)
        await observe(s, 84, "r2")
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        with pytest.raises(RecoveryRequired):
            await observe(s, 85, text="over")
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark
        assert (await post(s, at=85)).score_status is ApplyStatus.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_time", [-1, 100])
async def test_delivery_proof_requires_real_time_between_preparation_and_receipt(
    tmp_path, bad_time
):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        await begin(s, 1)
        output = (await work(s))[0]
        with pytest.raises(ValueError):
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                2,
                "r1",
                output.family,
                output.output_id,
                message_id=1001,
                published_at=NOW + timedelta(seconds=bad_time),
            )
        assert (await work(s))[0].status == "unknown"
        assert (await s.record()).choice_deadline is None


@pytest.mark.asyncio
async def test_conclusive_delivery_fact_cannot_be_changed_or_restart_original_clock(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        await begin(s, 1)
        output = (await work(s))[0]
        assert (
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                2,
                "r1",
                output.family,
                output.output_id,
                message_id=1001,
                published_at=NOW,
            )
            == "found"
        )
        assert (
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                3,
                "r1",
                output.family,
                output.output_id,
                message_id=1001,
                published_at=NOW,
            )
            == "already_done"
        )
        with pytest.raises(ValueError, match="cannot change"):
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                4,
                "r1",
                output.family,
                output.output_id,
            )
        assert (await s.record()).choice_deadline.expires_at == NOW + timedelta(seconds=60)


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["warning", "revival"])
async def test_lost_warning_or_revival_ack_uses_actual_publication_without_reshuffling(
    tmp_path, output
):
    from test_pacing import act as pacing_act

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        if output == "warning":
            await pacing_act(s, "tick", EventKind.TIMER, 870)
            at = 880
            publication = 875
            expected = 905
        else:
            await held(s)
            await s.act("tick", EventKind.TIMER, 900)
            at = 910
            publication = 905
            expected = 1205
        await begin(s, at)
        o = next(o for o in await work(s) if o.kind == output)
        assert (
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                at + 1,
                "r1",
                o.family,
                o.output_id,
                message_id=555,
                published_at=NOW + timedelta(seconds=publication),
            )
            == "found"
        )
        await approve(s, at + 2)
        await notice(s, at + 3)
        await observe(s, at + 33)
        if output == "warning":
            # Empty eligible pool moves on to revival, but the archived warning clock remains.
            import json

            history = json.loads(
                rows(
                    path,
                    "SELECT snapshot_json FROM nr_pacing_history ORDER BY attempt DESC LIMIT 1",
                )[0][0]
            )
            assert history["selection_due_at_utc"] == utc_int(NOW + timedelta(seconds=expected))
        else:
            assert rows(path, "SELECT deadline_utc FROM nr_revivals") == [
                (utc_int(NOW + timedelta(seconds=expected)),)
            ]
            assert (await s.context()).position.pause_reason is PauseReason.REVIVAL


@pytest.mark.asyncio
async def test_fresh_capabilities_are_required_before_a_recovery_notice(
    tmp_path,
):
    from lets_play.storage.opening_flow import OpeningCapabilities

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        assert (
            await approve(s, 51, capabilities=OpeningCapabilities(True, False, True))
            == "capability"
        )
        with pytest.raises(TypeError):
            await approve(s, 52, capabilities=True)
        assert (await record(s)).stage == "paused"


@pytest.mark.asyncio
async def test_cancelled_resume_rolls_back_owner_rebinding_and_retries_original_clock(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await published(s)
        await begin(s, 10)
        await approve(s, 11)
        await notice(s, 12)
        original = RecoveryFlow._resume
        entered = asyncio.Event()

        async def blocked(self, *args):
            await original(self, *args)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(RecoveryFlow, "_resume", blocked)
        task = asyncio.create_task(observe(s, 42))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await record(s)).stage == "reading"
        assert (await s.context()).position.lifecycle is Lifecycle.PAUSED
        assert (await s.record()).choice_deadline.expires_at == NOW + timedelta(seconds=65)
        monkeypatch.setattr(RecoveryFlow, "_resume", original)
        assert await observe(s, 42) == "resumed"


@pytest.mark.asyncio
async def test_discovery_pages_all_live_routes_and_excludes_ended_sessions(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        for i in range(102):
            s = Lane(store, session=f"s{i:03}", guild=1000 + i, channel=2000 + i)
            assert await s.admit() is OpeningStatus.ADMITTED
        last = Lane(store, session="ended", guild=9999, channel=9999)
        await last.admit()
        await last.act("cancel", EventKind.MANAGER_END, 1, actor=999, authorized=True)
        async with store.transaction(write=False) as unit:
            service = RecoveryFlow(unit)
            first = await service.live_routes()
            second = await service.live_routes(after_session=first[-1].session_id)
        assert len(first) == 100 and len(second) == 2
        assert len({r.session_id for r in first + second}) == 102
        assert all(r.last_sequence == 1 for r in first + second)


@pytest.mark.asyncio
async def test_ending_recovery_and_fk_cleanup_preserve_global_new_rules_credit(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for t in range(5):
            await post(s, at=t)
        await held(s, 5)
        await begin(s, 10)
        await resolve_all(s, 11)
        await observe(s, 12, text="over")
        assert (
            await rec(s, "end", EventKind.MANAGER_END, 13, "r1", actor=999, authorized=True)
            == "closed"
        )
        async with store.transaction() as unit:
            assert (await Progression(unit).profile(303)).games_played == 1
            await unit._conn.execute("DELETE FROM nr_sessions WHERE session_id='session'")
        assert rows(path, "SELECT COUNT(*) FROM nr_recoveries") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_recovery_work") == [(0,)]
        assert rows(path, "SELECT games_played FROM nr_profiles WHERE actor_id=303") == [(1,)]
        assert rows(path, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_recovery_write_methods_reject_read_only_and_closed_units(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction(write=False) as unit:
            with pytest.raises(RuntimeError):
                await RecoveryFlow(unit).begin(s.frame(EventKind.RECOVERY_BEGIN, 10), "r1")
        with pytest.raises(RuntimeError):
            await RecoveryFlow(unit).live_routes()


@pytest.mark.asyncio
async def test_legacy_revival_without_original_publication_clock_stays_paused_until_explicit_end(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        async with store.transaction() as unit:
            c = await unit.get_context("session")
            p = c.position
            await unit.set_owner(
                p,
                replace(
                    c,
                    position=replace(
                        p,
                        lifecycle=Lifecycle.PAUSED,
                        pause_reason=PauseReason.REVIVAL,
                        revision=p.revision + 1,
                    ),
                ),
            )
            await unit._conn.execute(
                "UPDATE nr_round_pacing SET stage='revival',owner_revision=?", (p.revision + 1,)
            )
        await begin(s)
        assert await approve(s) == "legacy_revival"
        assert rows(path, "SELECT COUNT(*) FROM nr_revivals") == [(0,)]
        assert (await record(s)).resume_at is None
        assert (
            await rec(s, "end", EventKind.MANAGER_END, 52, "r1", actor=999, authorized=True)
            == "closed"
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_game_finalizations") == [(1,)]


@pytest.mark.asyncio
async def test_warning_delivery_proof_cannot_precede_its_legal_warning_boundary(tmp_path):
    from test_pacing import act as pacing_act

    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        await pacing_act(s, "tick", EventKind.TIMER, 870)
        await begin(s, 880)
        output = next(o for o in await work(s) if o.kind == "warning")
        with pytest.raises(ValueError, match="legal boundary"):
            await rec(
                s,
                "reconcile",
                EventKind.RECOVERY_RECONCILE,
                881,
                "r1",
                output.family,
                output.output_id,
                message_id=555,
                published_at=NOW + timedelta(seconds=269),
            )
        assert (await work(s))[0].status == "unknown"
