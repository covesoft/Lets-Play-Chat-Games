"""Real SQLite held/refresh/revival boundaries over the shared ordered lane."""

import asyncio
import json
import sqlite3
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta

import pytest
from test_ordinary_flow import NOW, Scene, execute, rows
from test_pacing import act as pace
from test_pacing import post, seed, warning
from test_pacing import record as pacing_record

from lets_play.new_rules.contracts import (
    Advancement,
    EmojiIdentity,
    EventKind,
    Lifecycle,
    MessageEvent,
    PauseReason,
    Phase,
    ReactionEvent,
)
from lets_play.storage.evolution_flow import EvolutionFlow
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import ApplyStatus, TransactionAborted, open_game_store
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.ordinary_flow import FlowOrderError, FlowStatus, OrdinaryFlow
from lets_play.storage.pacing_flow import RoundPacing
from lets_play.storage.progression import Progression

GOOD = frozenset({"NR004", "NR027", "NR025"})
OTHER = frozenset({"NR004", "NR027", "NR022"})
SCARCE = frozenset({"NR004", "NR027"})


async def ev(s, method, kind, at, *args, actor=None, **kwargs):
    frame = s.frame(kind, at, actor)
    async with s.store.transaction() as unit:
        result = await getattr(EvolutionFlow(unit), method)(frame, *args, **kwargs)
    s.sequence += 1
    return result


async def check(s, at=0, cards=GOOD, **kwargs):
    return await ev(
        s, "recheck", EventKind.ADVANCEMENT_RECHECK, at, available_templates=cards, **kwargs
    )


async def revival(s):
    async with s.store.transaction(write=False) as unit:
        return await EvolutionFlow(unit).revival("session")


async def held(s, at=0):
    assert await check(s, at, SCARCE) == "held"
    assert (await s.context()).position.advancement is Advancement.HELD


async def invited(s, at=900, publish_at=1000):
    await held(s)
    await s.act("tick", EventKind.TIMER, at)
    r = await revival(s)
    assert r.stage == "prepared" and r.joined == 0 and r.deadline is None
    assert (
        await ev(
            s,
            "publish_revival",
            EventKind.REVIVAL_PUBLISHED,
            publish_at,
            r.revival_id,
            message_id=2001,
        )
        == "published"
    )
    return await revival(s)


async def returned(s, at=1001, *, cards=GOOD):
    r = await invited(s)
    assert await check(s, 1000, cards) == "paused"
    for i, actor in enumerate((707, 808, 909)):
        result = await ev(
            s, "join_revival", EventKind.REVIVAL_JOIN, at + i, r.revival_id, actor=actor
        )
    assert result == ("held" if cards == SCARCE else "quorum")
    return await revival(s)


async def selected_offer(s):
    assert await check(s) == "unchanged"
    for i in range(5):
        await post(s, at=i + 1)
    await warning(s)
    await s.act("tick", EventKind.TIMER, 900)
    assert (
        await ev(s, "prepare_or_hold", EventKind.OFFER_PREPARE, 901, offer_id="offer") == "prepared"
    )
    await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 910, "offer", message_id=1002)
    return await s.record()


@pytest.mark.asyncio
async def test_held_continues_same_round_scoring_and_does_not_consume_a_turn_or_phrase(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        original = await s.context()
        for t in range(1, 7):
            assert (await post(s, at=t)).score_status is ApplyStatus.APPLIED
        assert await s.context() == original
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(6,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(0,)]
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(None,)]
        async with store.transaction(write=False) as unit:
            assert (await Progression(unit).profile(303)).games_played == 1
            assert len(await EvolutionFlow(unit).pending_outputs()) == 1
        assert await check(s, 7, SCARCE) == "held"
        assert rows(path, "SELECT COUNT(*) FROM nr_evolution_outputs WHERE kind='held'") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_picker_candidates") == [(0,)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "traffic", ["whisper", "human", "reaction", "bot", "webhook", "system", "duplicate", "settings"]
)
async def test_only_new_human_messages_refresh_held_idle_window(tmp_path, traffic):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        saved = None
        if traffic == "duplicate":
            saved = s.frame(EventKind.MESSAGE_CREATE, 1, 303)
            async with store.transaction() as unit:
                await OrdinaryFlow(unit).ingest(MessageEvent(saved, "over"))
            s.sequence += 1
        if traffic == "settings":
            await check(s, 899, SCARCE)
        else:
            kind = EventKind.REACTION_ADD if traffic == "reaction" else EventKind.MESSAGE_CREATE
            frame = s.frame(kind, 899, 303)
            if saved:
                frame = replace(frame, source_id=saved.source_id)
            event = (
                ReactionEvent(frame, 1111, EmojiIdentity(unicode="x"))
                if traffic == "reaction"
                else MessageEvent(
                    frame,
                    "(private aside)" if traffic == "whisper" else "over",
                    is_bot=traffic == "bot",
                    is_webhook=traffic == "webhook",
                    is_system=traffic == "system",
                )
            )
            async with store.transaction() as unit:
                await OrdinaryFlow(unit).ingest(event)
            s.sequence += 1
        await s.act("tick", EventKind.TIMER, 901)
        context = await s.context()
        if traffic in {"whisper", "human"}:
            assert context.position.lifecycle is Lifecycle.PLAYING
            await s.act("tick", EventKind.TIMER, 1799)
        assert (await s.context()).position.pause_reason is PauseReason.REVIVAL
        assert rows(path, "SELECT COUNT(*) FROM nr_revivals") == [(1,)]


@pytest.mark.asyncio
async def test_held_idle_cutoff_precedes_same_time_message_and_preserves_it_unscored(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        result = await post(s, at=900, text="PAUSED_PRIVATE_CANARY")
        assert result.status is FlowStatus.IGNORED
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_paused_sources") == [(1,)]
        assert "PAUSED_PRIVATE_CANARY" not in "\n".join(
            str(r) for r in rows(path, "SELECT * FROM nr_paused_sources")
        )


@pytest.mark.asyncio
async def test_restoration_waits_for_original_floor_then_uses_fresh_publication_clock(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        assert await check(s, 10) == "restoring"
        assert (await pacing_record(s)).attempt == 2
        await s.act("tick", EventKind.TIMER, 269)
        assert (await pacing_record(s)).stage == "waiting"
        await s.act("tick", EventKind.TIMER, 270)
        assert (await pacing_record(s)).stage == "warning_queued"
        await pace(
            s,
            "publish_warning",
            EventKind.WARNING_PUBLISHED,
            400,
            "old-round",
            message_id=111,
            attempt=2,
        )
        r = await pacing_record(s)
        assert r.selection_due_at == NOW + timedelta(seconds=430)
        await s.act("tick", EventKind.TIMER, 429)
        assert await revival(s) is None
        await s.act("tick", EventKind.TIMER, 430)
        assert (await revival(s)).stage == "prepared"
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]


@pytest.mark.asyncio
async def test_restoration_coalesces_and_old_warning_ack_cannot_reset_new_attempt(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for t in range(5):
            await post(s, at=t)
        await warning(s)
        assert await check(s, 880, SCARCE) == "held"
        assert await check(s, 881) == "restoring"
        attempt = (await pacing_record(s)).attempt
        assert await check(s, 882) == "unchanged"
        assert (await pacing_record(s)).attempt == attempt
        assert (
            await pace(
                s, "publish_warning", EventKind.WARNING_PUBLISHED, 883, "old-round", message_id=1001
            )
            == "stale"
        )
        assert (await pacing_record(s)).selection_due_at is None
        assert (
            await pace(
                s,
                "publish_warning",
                EventKind.WARNING_PUBLISHED,
                884,
                "old-round",
                message_id=1002,
                attempt=attempt,
            )
            == "published"
        )
        await s.act("tick", EventKind.TIMER, 914)
        async with store.transaction(write=False) as unit:
            assert len(await RoundPacing(unit).candidates("session", "old-round")) == 1
        assert (await s.context()).position.round_id == "old-round"
        history = json.loads(
            rows(path, "SELECT snapshot_json FROM nr_pacing_history WHERE attempt=1")[0][0]
        )
        assert history["selection_due_at_utc"] == utc_int(NOW + timedelta(seconds=900))


@pytest.mark.asyncio
async def test_settings_restore_rehold_does_not_reset_idle_history(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        assert await check(s, 899) == "restoring"
        assert await check(s, 901, SCARCE) == "held"
        assert (await s.context()).position.pause_reason is PauseReason.REVIVAL
        assert rows(path, "SELECT idle_anchor_at_utc FROM nr_evolution") == [(utc_int(NOW),)]


@pytest.mark.asyncio
async def test_no_full_hand_at_normal_cutoff_holds_without_drawing_a_picker(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await check(s)
        await warning(s)
        # Saved trusted capability facts can be updated atomically before the cutoff.
        execute(path, "UPDATE nr_evolution SET config_json=replace(config_json,'\"NR025\",','')")
        await s.act("tick", EventKind.TIMER, 900)
        assert (await s.context()).position.advancement is Advancement.HELD
        assert await revival(s) is None
        assert rows(path, "SELECT COUNT(*) FROM nr_picker_candidates") == [(0,)]


@pytest.mark.asyncio
async def test_revival_preparation_delayed_publication_and_reopen_keep_original_deadline(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        await s.act("tick", EventKind.TIMER, 900)
        r = await revival(s)
        await s.act("tick", EventKind.TIMER, 5000)
        assert (await s.context()).position.lifecycle is Lifecycle.PAUSED
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 5000, r.revival_id, actor=303)
            == "stale"
        )
        await ev(
            s, "publish_revival", EventKind.REVIVAL_PUBLISHED, 5000, r.revival_id, message_id=2001
        )
        n = s.sequence
    async with open_game_store(path) as store:
        s = Scene(store, None, None)
        s.sequence = n
        assert (await revival(s)).deadline == NOW + timedelta(seconds=5300)
        assert (
            await ev(
                s,
                "publish_revival",
                EventKind.REVIVAL_PUBLISHED,
                5100,
                r.revival_id,
                message_id=2001,
            )
            == "already_done"
        )
        with pytest.raises(ValueError, match="identity"):
            await ev(
                s,
                "publish_revival",
                EventKind.REVIVAL_PUBLISHED,
                5101,
                r.revival_id,
                message_id=2002,
            )
        assert (await revival(s)).deadline == NOW + timedelta(seconds=5300)


@pytest.mark.asyncio
async def test_three_distinct_humans_revive_same_round_without_scores_closure_or_automatic_starter(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        r = await invited(s)
        assert (
            await ev(
                s,
                "join_revival",
                EventKind.REVIVAL_JOIN,
                1001,
                r.revival_id,
                actor=303,
                is_human=False,
            )
            == "ignored"
        )
        assert await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1002, r.revival_id) == "ignored"
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1003, r.revival_id, actor=404)
            == "joined"
        )
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1004, r.revival_id, actor=404)
            == "already_done"
        )
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1005, r.revival_id, actor=505)
            == "joined"
        )
        assert (await revival(s)).joined == 2
        await check(s, 1006)
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1007, r.revival_id, actor=606)
            == "quorum"
        )
        p = (await s.context()).position
        assert (
            p.lifecycle is Lifecycle.PLAYING
            and p.round_id == "old-round"
            and p.ruleset_version == 1
        )
        assert (await revival(s)).picker_id in {404, 505, 606}
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_participants") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_profiles") == [(0,)]
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(None,)]
        async with store.transaction(write=False) as unit:
            offer = await OrdinaryFlow(unit).get_offer("session", p.offer_id)
            assert offer.stage == "prepared" and offer.choice_deadline is None


@pytest.mark.asyncio
async def test_successful_revival_without_hand_returns_held_and_resets_only_idle_observation(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        r = await returned(s, cards=SCARCE)
        p = (await s.context()).position
        assert p.lifecycle is Lifecycle.PLAYING and p.advancement is Advancement.HELD
        assert p.round_id == r.round_id == "old-round"
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_session_activity") == [(0,)]
        assert rows(path, "SELECT idle_anchor_at_utc FROM nr_evolution") == [
            (utc_int(NOW + timedelta(seconds=1003)),)
        ]
        await s.act("tick", EventKind.TIMER, 1902)
        assert (await s.context()).position.lifecycle is Lifecycle.PLAYING
        await s.act("tick", EventKind.TIMER, 1903)
        second = await revival(s)
        assert second.revival_id != r.revival_id and second.joined == 0
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1904, r.revival_id, actor=111)
            == "stale"
        )
        assert (
            await ev(
                s,
                "publish_revival",
                EventKind.REVIVAL_PUBLISHED,
                1905,
                r.revival_id,
                message_id=2001,
            )
            == "stale"
        )
        assert (await revival(s)).deadline is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "reaction"])
async def test_paused_sources_redelivered_after_quorum_cannot_gain_points_or_consume_reaction_guard(
    tmp_path, kind
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        r = await invited(s)
        frame = s.frame(
            EventKind.MESSAGE_CREATE if kind == "message" else EventKind.REACTION_ADD, 1000, 303
        )

        def event(f):
            return (
                MessageEvent(f, "over")
                if kind == "message"
                else ReactionEvent(f, 1111, EmojiIdentity(unicode="x"))
            )

        async with store.transaction() as unit:
            assert (await OrdinaryFlow(unit).ingest(event(frame))).status is FlowStatus.IGNORED
        s.sequence += 1
        await check(s, 1000)
        for i, a in enumerate((404, 505, 606)):
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1001 + i, r.revival_id, actor=a)
        later = replace(s.frame(frame.kind, 1004, 303), source_id=frame.source_id)
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).ingest(event(later))
            ).score_status is ApplyStatus.DUPLICATE_EVENT
        s.sequence += 1
        assert rows(path, "SELECT COUNT(*) FROM nr_reaction_offenses") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_session_activity") == [(0,)]
        assert (await post(s, at=1005)).score_status is ApplyStatus.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt", ["timer", "join", "publication", "message"])
async def test_revival_expiry_wins_at_exact_deadline_and_finalizes_once_releasing_capacity(
    tmp_path, receipt
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for t in range(5):
            await post(s, at=t)
        # Start a held idle episode after earned progression already exists.
        await held(s, 5)
        await s.act("tick", EventKind.TIMER, 905)
        r = await revival(s)
        await ev(
            s, "publish_revival", EventKind.REVIVAL_PUBLISHED, 1000, r.revival_id, message_id=2001
        )
        await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1001, r.revival_id, actor=404)
        await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1002, r.revival_id, actor=505)
        if receipt == "join":
            assert (
                await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1300, r.revival_id, actor=606)
                == "stale"
            )
        elif receipt == "publication":
            assert (
                await ev(
                    s,
                    "publish_revival",
                    EventKind.REVIVAL_PUBLISHED,
                    1300,
                    r.revival_id,
                    message_id=2001,
                )
                == "stale"
            )
        elif receipt == "message":
            assert (await post(s, at=1300)).status is FlowStatus.IGNORED
        else:
            await s.act("tick", EventKind.TIMER, 1300)
        await s.act("tick", EventKind.TIMER, 1301)
        assert (await s.context()).position.lifecycle is Lifecycle.ENDED
        assert (await revival(s)).stage == "expired"
        assert rows(path, "SELECT stage FROM nr_admissions") == [("ended",)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_game_finalizations") == [(1,)]
        async with store.transaction(write=False) as unit:
            assert (await Progression(unit).profile(303)).games_played == 1
            outputs = await EvolutionFlow(unit).pending_outputs()
            assert len(outputs) == 1 and outputs[0].kind == "closed_silent"
            assert await RoundPacing(unit).pending_outputs() == ()
        assert rows(
            path, "SELECT COUNT(*) FROM nr_evolution_outputs WHERE kind='closed_silent'"
        ) == [(1,)]


@pytest.mark.asyncio
async def test_revival_quorum_does_not_close_round_but_actual_activation_closes_once_with_three_player_estimate(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        r = await returned(s)
        p = (await s.context()).position
        async with store.transaction(write=False) as unit:
            offer = await OrdinaryFlow(unit).get_offer("session", p.offer_id)
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 1010, p.offer_id, message_id=3001)
        o = offer.hand.options[0]
        result = await s.act(
            "choose",
            EventKind.CHOICE_SUBMIT,
            1011,
            p.offer_id,
            o.incoming.instance_id,
            actor=r.picker_id,
            outgoing_id=o.outgoing_ids[0] if o.outgoing_ids else None,
        )
        assert result.status is FlowStatus.SELECTED
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 1012, p.offer_id, message_id=3002
        )
        await s.act("tick", EventKind.TIMER, 1016)
        await s.act("tick", EventKind.TIMER, 1017)
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(2,)]
        assert (await pacing_record(s)).estimated_players == 3
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(r.picker_id,)]
        assert rows(path, "SELECT phase,idle_anchor_at_utc FROM nr_evolution") == [("normal", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_at", ["before", "after"])
async def test_full_refresh_preserves_original_deadline_and_ack_identity_even_if_ack_is_late(
    tmp_path, ack_at
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        original = await selected_offer(s)
        assert original.choice_deadline.expires_at == NOW + timedelta(seconds=970)
        new_id = await check(s, 950, OTHER)
        assert new_id.startswith("offer:refresh:")
        assert await ev(s, "refresh_offer", EventKind.OFFER_REFRESH, 951, "offer") == new_id
        async with store.transaction(write=False) as unit:
            flow = OrdinaryFlow(unit)
            new = await flow.get_offer("session", new_id)
            assert (await flow.get_offer("session", "offer")).stage == "cancelled"
            assert new.choice_deadline == original.choice_deadline
            assert new.published_at == original.published_at
            assert len(new.hand.options) == 3 and new.selected_incoming is None
        when = 960 if ack_at == "before" else 980
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, when, new_id, message_id=4001)
        async with store.transaction(write=False) as unit:
            assert (
                await OrdinaryFlow(unit).get_offer("session", new_id)
            ).choice_deadline == original.choice_deadline
        result = await s.act(
            "choose",
            EventKind.CHOICE_SUBMIT,
            max(970, when),
            new_id,
            new.hand.options[0].incoming.instance_id,
            actor=new.hand.picker_id,
        )
        assert result.status is FlowStatus.EXPIRED
        assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(0,)]
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(None,)]


@pytest.mark.asyncio
async def test_unusable_offer_without_full_replacement_enters_held_same_round(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await selected_offer(s)
        old = (await s.context()).position
        assert await check(s, 920, SCARCE) == "held"
        assert (await s.record()).stage == "cancelled"
        p = (await s.context()).position
        assert p.round_id == old.round_id and p.ruleset_version == old.ruleset_version
        assert p.offer_id is None and p.advancement is Advancement.HELD
        async with store.transaction(write=False) as unit:
            assert await OrdinaryFlow(unit).pending_outputs() == ()
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]


@pytest.mark.asyncio
async def test_valid_frozen_offer_does_not_reroll_for_settings_only_changes(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        original = await selected_offer(s)
        assert await check(s, 920, GOOD | OTHER) == "unchanged"
        assert await ev(s, "refresh_offer", EventKind.OFFER_REFRESH, 921, "offer") == "unchanged"
        assert await s.record() == original
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(1,)]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [Phase.MODE, Phase.ORDINARY])
async def test_held_only_detector_does_not_truncate_other_phases_or_resume_capability_pause(
    tmp_path, phase
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        async with store.transaction() as unit:
            ctx = await unit.get_context("session")
            p = ctx.position
            new = replace(p, phase=phase, revision=p.revision + 1)
            if phase is Phase.ORDINARY:
                new = replace(new, lifecycle=Lifecycle.PAUSED, pause_reason=PauseReason.CAPABILITY)
            await unit.set_owner(p, replace(ctx, position=new))
        await s.act("tick", EventKind.TIMER, 900)
        assert await revival(s) is None
        result = await check(s, 901)
        assert result == ("stale" if phase is Phase.MODE else "paused")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table",
    [
        "nr_evolution_outputs",
        "nr_revivals",
        "nr_revival_members",
        "nr_offers",
        "nr_game_finalizations",
    ],
)
async def test_late_failure_rolls_back_receipt_owner_and_all_composed_effects(tmp_path, table):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        if table == "nr_evolution_outputs":
            method, kind, at, args, kw = (
                "recheck",
                EventKind.ADVANCEMENT_RECHECK,
                0,
                (),
                {"available_templates": SCARCE},
            )
        elif table == "nr_revivals":
            await held(s)
            method, kind, at, args, kw = "tick", EventKind.TIMER, 900, (), {}
        else:
            r = await invited(s)
            await check(s, 1000)
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1001, r.revival_id, actor=404)
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1002, r.revival_id, actor=505)
            method, kind, at, args, kw = (
                "join_revival",
                EventKind.REVIVAL_JOIN,
                1003,
                (r.revival_id,),
                {"actor_id": 606},
            )
            if table == "nr_game_finalizations":
                method, kind, at, args, kw = "tick", EventKind.TIMER, 1300, (), {}
        before = await s.context()
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        execute(
            path,
            f"CREATE TRIGGER late_failure BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'late failure'); END",
        )
        frame = s.frame(kind, at, kw.pop("actor_id", None))
        with pytest.raises(sqlite3.IntegrityError, match="late failure"):
            async with store.transaction() as unit:
                service = OrdinaryFlow(unit) if method == "tick" else EvolutionFlow(unit)
                await getattr(service, method)(frame, *args, **kw)
        assert await s.context() == before
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark
        execute(path, "DROP TRIGGER late_failure")
        async with store.transaction() as unit:
            service = OrdinaryFlow(unit) if method == "tick" else EvolutionFlow(unit)
            await getattr(service, method)(frame, *args, **kw)


@pytest.mark.asyncio
async def test_caught_error_is_rollback_only_and_order_gap_never_skips_chat(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                with pytest.raises(FlowOrderError):
                    await EvolutionFlow(unit).recheck(
                        s.frame(EventKind.ADVANCEMENT_RECHECK, 900, seq=s.sequence + 2),
                        available_templates=GOOD,
                    )
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark
        assert await revival(s) is None


@pytest.mark.asyncio
async def test_two_connections_racing_quorum_use_one_receipt_one_picker_one_offer(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        s = await seed(stores[0])
        r = await invited(s)
        await check(s, 1000)
        for i, a in enumerate((404, 505)):
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1001 + i, r.revival_id, actor=a)
        frame = s.frame(EventKind.REVIVAL_JOIN, 1003, 606)

        async def run(store):
            async with store.transaction() as unit:
                return await EvolutionFlow(unit).join_revival(frame, r.revival_id)

        assert sorted(await asyncio.gather(*(run(store) for store in stores))) == [
            "quorum",
            "replay",
        ]
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_revival_members") == [(3,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]


@pytest.mark.asyncio
async def test_cancelled_quorum_retries_same_saved_picker_without_partial_resume(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        r = await invited(s)
        await check(s, 1000)
        for i, a in enumerate((404, 505)):
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1001 + i, r.revival_id, actor=a)
        original = OrdinaryFlow._prepare_entered
        entered = asyncio.Event()

        async def block(self, *args):
            await original(self, *args)
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(OrdinaryFlow, "_prepare_entered", block)
        task = asyncio.create_task(
            ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1003, r.revival_id, actor=606)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await s.context()).position.lifecycle is Lifecycle.PAUSED
        assert (await revival(s)).joined == 2
        assert rows(path, "SELECT COUNT(*) FROM nr_offers") == [(0,)]
        monkeypatch.setattr(OrdinaryFlow, "_prepare_entered", original)
        assert (
            await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1003, r.revival_id, actor=606)
            == "quorum"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, -1])
async def test_output_pages_are_bounded(tmp_path, limit):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            with pytest.raises(ValueError):
                await EvolutionFlow(unit).pending_outputs(limit=limit)


@pytest.mark.asyncio
async def test_unknown_evolution_pin_refuses_and_rolls_back_receipt(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        execute(path, "UPDATE nr_evolution SET version='future'")
        mark = rows(path, "SELECT * FROM nr_flow_watermarks")
        with pytest.raises(IncompatibleState):
            await s.act("tick", EventKind.TIMER, 900)
        assert rows(path, "SELECT * FROM nr_flow_watermarks") == mark


@pytest.mark.asyncio
async def test_unpublished_offer_refresh_starts_first_clock_only_when_replacement_is_published(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await check(s)
        for t in range(5):
            await post(s, at=t)
        await warning(s)
        await s.act("tick", EventKind.TIMER, 900)
        await ev(s, "prepare_or_hold", EventKind.OFFER_PREPARE, 901, offer_id="offer")
        new_id = await check(s, 950, OTHER)
        async with store.transaction(write=False) as unit:
            assert (await OrdinaryFlow(unit).get_offer("session", new_id)).choice_deadline is None
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 1000, new_id, message_id=4001)
        async with store.transaction(write=False) as unit:
            assert (
                await OrdinaryFlow(unit).get_offer("session", new_id)
            ).choice_deadline.expires_at == NOW + timedelta(seconds=1060)


@pytest.mark.asyncio
async def test_refreshed_partial_choice_reopen_keeps_original_clock_without_consuming_unfulfilled_human_turn(
    tmp_path,
):
    from test_ordinary_flow import incoming, scene

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        # Use the ordinary pacing lane with this two-vocabulary-rule replacement fixture.
        from random import Random

        await pace(
            s,
            "register_round",
            EventKind.PACING_REGISTER,
            0,
            estimated_players=3,
            rng=Random(0),
            started_at=NOW,
        )
        await check(s, 0, frozenset({"NR004", "NR006", "NR027"}))
        for t in range(5):
            await post(s, at=t)
        await warning(s)
        await s.act("tick", EventKind.TIMER, 900)
        await ev(s, "prepare_or_hold", EventKind.OFFER_PREPARE, 901, offer_id="offer")
        original = await s.record()
        s.hand = original.hand
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 910, "offer", message_id=4001)
        assert (
            await s.act(
                "choose",
                EventKind.CHOICE_SUBMIT,
                920,
                "offer",
                incoming(s),
                actor=original.hand.picker_id,
            )
        ).status is FlowStatus.NEEDS_TARGET
        original = await s.record()
        assert await check(s, 921, frozenset({"NR004", "NR006", "NR027", "NR034"})) == "unchanged"
        assert (await s.record()).selected_incoming == original.selected_incoming
        new_id = await check(s, 922, frozenset({"NR004", "NR006", "NR034"}))
        assert new_id.startswith("offer:refresh:")
        n = s.sequence
    async with open_game_store(path) as store:
        s.store = store
        s.sequence = n
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 950, new_id, message_id=4002)
        async with store.transaction(write=False) as unit:
            fresh = await OrdinaryFlow(unit).get_offer("session", new_id)
            assert fresh.choice_deadline == original.choice_deadline
            assert fresh.selected_incoming is None and not fresh.chosen_by_picker
        await s.act("timeout", EventKind.CHOICE_TIMEOUT, 970, new_id, rng=Random(0))
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 971, new_id, message_id=4003
        )
        await s.act("tick", EventKind.TIMER, 975)
        async with store.transaction(write=False) as unit:
            assert (
                "rulemaker" not in (await Progression(unit).profile(original.hand.picker_id)).badges
            )


@pytest.mark.asyncio
async def test_frozen_phrase_survives_bank_edit_until_usage_or_capability_makes_offer_unusable(
    tmp_path,
):
    from lets_play.new_rules.hands import PhraseChoice

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        cards = frozenset({"NR004", "NR027", "NR036"})
        bank = (PhraseChoice("p", "red duck", "v1"),)
        await check(s, 0, cards, phrase_bank=bank, phrase_draw_limit=5)
        for t in range(5):
            await post(s, at=t)
        await warning(s)
        await s.act("tick", EventKind.TIMER, 900)
        await ev(s, "prepare_or_hold", EventKind.OFFER_PREPARE, 901, offer_id="offer")
        await s.act("publish_offer", EventKind.OFFER_PUBLISHED, 910, "offer", message_id=5001)
        frozen = await s.record()
        assert (
            await check(s, 920, cards, phrase_bank=(PhraseChoice("q", "blue goose", "v2"),))
            == "unchanged"
        )
        assert (await s.record()).hand == frozen.hand
        # A separately committed use of the frozen identity now requires a full refresh.
        execute(path, "INSERT INTO nr_used_phrases VALUES ('session','[\"red\",\"duck\"]')")
        new_id = await ev(s, "refresh_offer", EventKind.OFFER_REFRESH, 921, "offer")
        async with store.transaction(write=False) as unit:
            fresh = await OrdinaryFlow(unit).get_offer("session", new_id)
            assert fresh.choice_deadline == frozen.choice_deadline
            phrase = next(o.incoming for o in fresh.hand.options if o.incoming.card_id == "NR036")
            assert dict(phrase.parameters)["phrase"] == "blue goose"
        assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(1,)]


@pytest.mark.asyncio
async def test_unprovable_announced_change_uses_recovery_and_does_not_convert_a_promise_to_held(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        offer = await selected_offer(s)
        o = offer.hand.options[0]
        await s.act(
            "choose",
            EventKind.CHOICE_SUBMIT,
            920,
            "offer",
            o.incoming.instance_id,
            actor=offer.hand.picker_id,
            outgoing_id=o.outgoing_ids[0] if o.outgoing_ids else None,
        )
        await s.act(
            "publish_activation", EventKind.ACTIVATION_PUBLISHED, 921, "offer", message_id=3002
        )
        assert await check(s, 922, frozenset()) == "recovery"
        assert (await s.context()).position.pause_reason is PauseReason.RECOVERY
        assert (await s.record()).stage == "recovery"
        assert await check(s, 923) == "paused"
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]


@pytest.mark.asyncio
async def test_held_idle_deadline_and_original_activity_survive_reopen(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        await post(s, at=800, text="(aside)")
        n = s.sequence
        async with store.transaction(write=False) as unit:
            assert await EvolutionFlow(unit).next_deadline("session") == NOW + timedelta(
                seconds=1700
            )
    async with open_game_store(path) as store:
        s.store = store
        s.sequence = n
        await s.act("tick", EventKind.TIMER, 1699)
        assert (await s.context()).position.lifecycle is Lifecycle.PLAYING
        await s.act("tick", EventKind.TIMER, 1700)
        async with store.transaction(write=False) as unit:
            assert await EvolutionFlow(unit).next_deadline("session") is None
        r = await revival(s)
        await ev(
            s, "publish_revival", EventKind.REVIVAL_PUBLISHED, 1800, r.revival_id, message_id=2222
        )
        async with store.transaction(write=False) as unit:
            assert await EvolutionFlow(unit).next_deadline("session") == NOW + timedelta(
                seconds=2100
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["held", "revival", "expiry_boundary"])
async def test_manager_end_and_revival_expiry_share_one_terminal_finalization(tmp_path, ending):
    from lets_play.storage.opening_flow import OpeningFlow, OpeningStatus

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        if ending == "held":
            await held(s)
            at = 1
        else:
            await invited(s)
            at = 1300 if ending == "expiry_boundary" else 1001
        frame = s.frame(EventKind.MANAGER_END, at, 999)
        async with store.transaction() as unit:
            result = await OpeningFlow(unit).cancel(frame, authorized=True)
        s.sequence += 1
        assert result is (
            OpeningStatus.ALREADY_DONE if ending == "expiry_boundary" else OpeningStatus.ENDED
        )
        await s.act("tick", EventKind.TIMER, 1400)
        assert (await s.context()).position.lifecycle is Lifecycle.ENDED
        assert rows(path, "SELECT COUNT(*) FROM nr_game_finalizations") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]
        assert rows(path, "SELECT stage FROM nr_admissions") == [("ended",)]
        async with store.transaction(write=False) as unit:
            outputs = await EvolutionFlow(unit).pending_outputs()
            assert [o.kind for o in outputs] == (
                ["closed_silent"] if ending == "expiry_boundary" else []
            )


@pytest.mark.asyncio
async def test_held_and_silent_notice_ack_are_idempotent_and_stale_tokens_cannot_ack_new_panel(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await held(s)
        async with store.transaction(write=False) as unit:
            token = (await EvolutionFlow(unit).pending_outputs())[0].token
        assert (
            await ev(s, "publish_notice", EventKind.HELD_PUBLISHED, 1, token, message_id=6001)
            == "published"
        )
        assert (
            await ev(s, "publish_notice", EventKind.HELD_PUBLISHED, 2, token, message_id=6001)
            == "already_done"
        )
        with pytest.raises(ValueError, match="identity"):
            await ev(s, "publish_notice", EventKind.HELD_PUBLISHED, 3, token, message_id=6002)
        await check(s, 4)
        await held(s, 5)
        async with store.transaction(write=False) as unit:
            pending = await EvolutionFlow(unit).pending_outputs()
            assert len(pending) == 1 and pending[0].token != token
        await s.act("tick", EventKind.TIMER, 900)
        r = await revival(s)
        await ev(
            s, "publish_revival", EventKind.REVIVAL_PUBLISHED, 1000, r.revival_id, message_id=6003
        )
        await s.act("tick", EventKind.TIMER, 1300)
        assert (
            await ev(
                s,
                "publish_notice",
                EventKind.SILENT_END_PUBLISHED,
                1301,
                r.revival_id,
                message_id=6004,
            )
            == "published"
        )
        assert (
            await ev(
                s,
                "publish_notice",
                EventKind.SILENT_END_PUBLISHED,
                1302,
                r.revival_id,
                message_id=6004,
            )
            == "already_done"
        )
        async with store.transaction(write=False) as unit:
            assert await EvolutionFlow(unit).pending_outputs() == ()


@pytest.mark.asyncio
async def test_session_cleanup_cascades_new_transient_rows_preserving_global_new_rules_credit(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        for t in range(5):
            await post(s, at=t)
        await invited_after_activity(s)
        async with store.transaction() as unit:
            await unit._conn.execute("DELETE FROM nr_sessions WHERE session_id='session'")
        for table in (
            "nr_pacing_history",
            "nr_paused_sources",
            "nr_evolution",
            "nr_revivals",
            "nr_revival_members",
            "nr_evolution_outputs",
            "nr_session_activity",
            "nr_offer_refreshes",
        ):
            assert rows(path, f"SELECT COUNT(*) FROM {table}") == [(0,)]
        assert rows(path, "SELECT games_played FROM nr_profiles WHERE actor_id=303") == [(1,)]
        assert rows(path, "PRAGMA foreign_key_check") == []


async def invited_after_activity(s):
    await held(s, 5)
    await s.act("tick", EventKind.TIMER, 905)
    r = await revival(s)
    await ev(s, "publish_revival", EventKind.REVIVAL_PUBLISHED, 1000, r.revival_id, message_id=8001)
    await post(s, at=1001, text="PRIVATE_SUSPENDED_CANARY")
    await ev(s, "join_revival", EventKind.REVIVAL_JOIN, 1002, r.revival_id, actor=404)
    await s.act("tick", EventKind.TIMER, 1300)


@pytest.mark.asyncio
async def test_new_evolution_writes_require_live_write_unit(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await seed(store)
        async with store.transaction(write=False) as unit:
            with pytest.raises(RuntimeError):
                await EvolutionFlow(unit).recheck(
                    s.frame(EventKind.ADVANCEMENT_RECHECK, 0), available_templates=SCARCE
                )
        with pytest.raises(RuntimeError):
            await EvolutionFlow(unit).next_deadline("session")
