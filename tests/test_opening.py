"""Admission, quorum and first activation use real WAL files and public facades."""

import asyncio
import sqlite3
from contextlib import AsyncExitStack
from dataclasses import replace
from datetime import timedelta
from random import Random

import pytest
from test_ordinary_flow import NOW, execute, rows

from lets_play.new_rules.contracts import (
    EventEnvelope,
    EventKind,
    Lifecycle,
    MessageEvent,
    SessionRoute,
)
from lets_play.storage.game_store import ApplyStatus, open_game_store
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.opening_flow import OpeningCapabilities, OpeningFlow, OpeningStatus
from lets_play.storage.ordinary_flow import FlowOrderError, FlowStatus, OrdinaryFlow
from lets_play.storage.progression import Progression

CAPS = OpeningCapabilities(True, True, True)
CARDS = frozenset({"NR034", "NR004", "NR006"})


class Lane:
    def __init__(self, store, *, session="session", guild=101, channel=202):
        self.store = store
        self.route = SessionRoute(guild, channel, session)
        self.sequence = 0

    def frame(self, kind, at, actor=None, *, source=None, sequence=None):
        n = sequence if sequence is not None else self.sequence + 1
        return EventEnvelope(
            self.route,
            kind,
            source or f"{self.route.session_id}:{kind}-{n}",
            actor,
            NOW + timedelta(seconds=at),
            NOW + timedelta(seconds=at),
            n,
        )

    async def act(self, method, kind, at, *args, actor=None, **kwargs):
        frame = self.frame(kind, at, actor)
        async with self.store.transaction() as unit:
            result = await getattr(OpeningFlow(unit), method)(frame, *args, **kwargs)
        self.sequence += 1
        return result

    async def admit(self, *, at=0, capabilities=CAPS, **kwargs):
        return await self.act(
            "admit",
            EventKind.LOBBY_CREATE,
            at,
            actor=303,
            capabilities=capabilities,
            rng=Random(0),
            available_templates=kwargs.pop("available_templates", CARDS),
            **kwargs,
        )

    async def record(self):
        async with self.store.transaction(write=False) as unit:
            return await OpeningFlow(unit).record(self.route.session_id)

    async def post(self, at, text="over", *, actor=606, source=None):
        frame = self.frame(EventKind.MESSAGE_CREATE, at, actor, source=source)
        async with self.store.transaction() as unit:
            result = await OpeningFlow(unit).ingest(MessageEvent(frame, text))
        self.sequence += 1
        return result


async def quorum(lane, *, publish=5, join=10, **admission):
    assert await lane.admit(**admission) is OpeningStatus.ADMITTED
    assert (
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, publish, "lobby", message_id=1001)
        is OpeningStatus.PUBLISHED
    )
    assert await lane.act("join", EventKind.LOBBY_JOIN, join, actor=404) is OpeningStatus.JOINED
    assert (
        await lane.act("join", EventKind.LOBBY_JOIN, join + 1, actor=505) is OpeningStatus.OPENING
    )
    return await lane.record()


async def select(lane, *, automatic=False, card="NR034", **admission):
    record = await quorum(lane, **admission)
    assert (
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        is OpeningStatus.PUBLISHED
    )
    if automatic:
        assert (
            await lane.act("timeout", EventKind.CHOICE_TIMEOUT, 80, rng=Random(0))
            is OpeningStatus.SELECTED
        )
    else:
        incoming = next(
            o.incoming.instance_id for o in record.hand.options if o.incoming.card_id == card
        )
        assert (
            await lane.act("choose", EventKind.CHOICE_SUBMIT, 30, incoming, actor=record.picker_id)
            is OpeningStatus.SELECTED
        )
    return await lane.record()


@pytest.mark.asyncio
async def test_lobby_reserves_capacity_without_creating_a_game_or_round(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        assert await lane.admit() is OpeningStatus.ADMITTED
        record = await lane.record()
        assert record.stage == "lobby_prepared" and record.joined == 1
        assert record.lobby_deadline is None and record.next_deadline is None
        assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_profiles") == [(0,)]
        assert await Lane(store, session="other", channel=203).admit() is OpeningStatus.CAPACITY
        assert await Lane(store, session="same-channel").admit() is OpeningStatus.CHANNEL_BUSY


@pytest.mark.asyncio
async def test_publication_starts_lobby_expiry_and_duplicate_request_keeps_identity(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        original = lane.frame(EventKind.LOBBY_CREATE, 0, 303)
        assert await lane.admit() is OpeningStatus.ADMITTED
        async with store.transaction() as unit:
            assert (
                await OpeningFlow(unit).admit(original, capabilities=CAPS, rng=Random(99))
                is OpeningStatus.ALREADY_DONE
            )
        assert await lane.act("tick", EventKind.TIMER, 1000) is OpeningStatus.NOT_DUE
        assert (
            await lane.act("publish", EventKind.LOBBY_PUBLISHED, 1010, "lobby", message_id=1001)
            is OpeningStatus.PUBLISHED
        )
        assert (await lane.record()).lobby_deadline == NOW + timedelta(seconds=1190)
        assert (
            await lane.act("publish", EventKind.LOBBY_PUBLISHED, 1020, "lobby", message_id=1001)
            is OpeningStatus.ALREADY_DONE
        )
        assert (await lane.record()).lobby_deadline == NOW + timedelta(seconds=1190)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "badcaps",
    [
        OpeningCapabilities(False, True, True),
        OpeningCapabilities(True, False, True),
        OpeningCapabilities(True, True, False),
        OpeningCapabilities(True, True, True, False),
    ],
)
async def test_unavailable_capabilities_do_not_reserve_anything(tmp_path, badcaps):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        assert await Lane(store).admit(capabilities=badcaps) is OpeningStatus.UNAVAILABLE
        assert rows(path, "SELECT COUNT(*) FROM nr_admissions") == [(0,)]


@pytest.mark.asyncio
async def test_hand_shortage_fails_preflight_before_capacity_reservation(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        assert (
            await Lane(store).admit(available_templates=frozenset({"NR034", "NR004"}))
            is OpeningStatus.UNAVAILABLE
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_admissions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_lobby_members") == [(0,)]


@pytest.mark.asyncio
async def test_duplicate_human_clicks_and_bot_clicks_do_not_inflate_quorum(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        await lane.admit()
        assert (
            await lane.act("join", EventKind.LOBBY_JOIN, 1, actor=404) is OpeningStatus.UNAVAILABLE
        )
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        assert (
            await lane.act("join", EventKind.LOBBY_JOIN, 6, actor=303) is OpeningStatus.ALREADY_DONE
        )
        assert (
            await lane.act("join", EventKind.LOBBY_JOIN, 7, actor=404, is_human=False)
            is OpeningStatus.IGNORED
        )
        assert await lane.act("join", EventKind.LOBBY_JOIN, 8, actor=404) is OpeningStatus.JOINED
        assert (
            await lane.act("join", EventKind.LOBBY_JOIN, 9, actor=404) is OpeningStatus.ALREADY_DONE
        )
        assert (await lane.record()).joined == 2
        assert await lane.act("join", EventKind.LOBBY_JOIN, 10, actor=505) is OpeningStatus.OPENING
        picked = (await lane.record()).picker_id
        assert picked in {303, 404, 505}
        assert (
            await lane.act("join", EventKind.LOBBY_JOIN, 11, actor=606)
            is OpeningStatus.ALREADY_DONE
        )
        assert (await lane.record()).joined == 3 and (await lane.record()).picker_id == picked


@pytest.mark.asyncio
async def test_lobby_expires_at_exact_deadline_and_late_third_join_cannot_reopen(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await lane.admit()
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await lane.act("join", EventKind.LOBBY_JOIN, 10, actor=404)
        assert await lane.act("join", EventKind.LOBBY_JOIN, 185, actor=505) is OpeningStatus.ENDED
        assert (await lane.record()).joined == 2
        assert await Lane(store, session="new", channel=202).admit(at=185) is OpeningStatus.ADMITTED
        async with store.transaction(write=False) as unit:
            assert "closed" in [o.kind for o in await OpeningFlow(unit).pending_outputs()]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(0,)]


@pytest.mark.asyncio
async def test_quorum_before_expiry_has_no_choice_clock_until_offer_publication(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        record = await quorum(lane, join=183)
        assert record.hand.owner.lifecycle is Lifecycle.OPENING_CHOICE
        assert record.hand.owner.round_id is None
        assert record.hand.active_instances == ()
        assert record.choice_deadline is None and record.next_deadline is None
        assert await lane.act("tick", EventKind.TIMER, 500) is OpeningStatus.NOT_DUE
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 510, "offer", message_id=1002)
        assert (await lane.record()).choice_deadline.expires_at == NOW + timedelta(seconds=570)


@pytest.mark.asyncio
async def test_failed_quorum_preflight_ends_cleanly_instead_of_creating_a_ruleless_game(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await lane.admit()
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await lane.act("join", EventKind.LOBBY_JOIN, 10, actor=404)
        assert (
            await lane.act(
                "join",
                EventKind.LOBBY_JOIN,
                11,
                actor=505,
                available_templates=frozenset({"NR034"}),
            )
            is OpeningStatus.UNAVAILABLE
        )
        assert (await lane.record()).stage == "ended"
        assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
        assert await Lane(store, session="replacement").admit(at=11) is OpeningStatus.ADMITTED


@pytest.mark.asyncio
async def test_opening_offer_authorizes_only_frozen_picker_and_preserves_original_deadline(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        record = await quorum(lane)
        incoming = record.hand.options[0].incoming.instance_id
        assert (
            await lane.act("choose", EventKind.CHOICE_SUBMIT, 15, incoming, actor=record.picker_id)
            is OpeningStatus.UNAVAILABLE
        )
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        assert (
            await lane.act("choose", EventKind.CHOICE_SUBMIT, 25, incoming, actor=999)
            is OpeningStatus.UNAUTHORIZED
        )
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 30, "offer", message_id=1002)
        assert (await lane.record()).choice_deadline.expires_at == NOW + timedelta(seconds=80)
        assert (
            await lane.act("choose", EventKind.CHOICE_SUBMIT, 80, incoming, actor=record.picker_id)
            is OpeningStatus.EXPIRED
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_profiles") == [(0,)]


@pytest.mark.asyncio
async def test_first_activation_creates_one_round_pacing_and_rulemaker_but_no_play_credit(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        chosen = await select(lane)
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(0,)]
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        assert await lane.act("tick", EventKind.TIMER, 35) is OpeningStatus.ACTIVE
        async with store.transaction(write=False) as unit:
            ctx = await unit.get_context("session")
            assert ctx.position.round_id == "session:opening:round"
            assert [i.card_id for i in ctx.active_instances] == ["NR034"]
            p = await Progression(unit).profile(chosen.picker_id)
            assert p.games_played == 0 and p.badges == ("rulemaker",)
            for actor in (303, 404, 505):
                total = await unit.get_score("session", actor)
                assert total.ordinary_posts == total.observations == 0
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_closures") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(0,)]
        assert rows(path, "SELECT started_at_utc,ordinary_posts FROM nr_rounds") == [
            (utc_int(NOW + timedelta(seconds=35)), 0)
        ]
        assert rows(path, "SELECT estimated_players,message_target FROM nr_round_pacing") == [
            (3, 15)
        ]
        assert rows(path, "SELECT previous_picker FROM nr_rotation") == [(chosen.picker_id,)]


@pytest.mark.asyncio
async def test_activation_boundary_message_is_scored_once_after_handover(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        assert (await lane.post(34)).status is FlowStatus.IGNORED
        first = await lane.post(35, source="first-scored")
        assert first.score_status is ApplyStatus.APPLIED
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(1,)]
        # OrdinaryFlow takes over with the next sequence, not a fresh lane.
        frame = lane.frame(EventKind.MESSAGE_CREATE, 36, 606)
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).ingest(MessageEvent(frame, "over"))
            ).score_status is ApplyStatus.APPLIED
        lane.sequence += 1
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(2,)]


@pytest.mark.asyncio
async def test_preplay_redelivery_after_activation_does_not_become_a_scored_message(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await lane.admit()
        await lane.post(1, text="PRIVATE_PREPLAY_CANARY", source="preplay")
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await lane.act("join", EventKind.LOBBY_JOIN, 10, actor=404)
        await lane.act("join", EventKind.LOBBY_JOIN, 11, actor=505)
        r = await lane.record()
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        incoming = next(
            o.incoming.instance_id for o in r.hand.options if o.incoming.card_id == "NR034"
        )
        await lane.act("choose", EventKind.CHOICE_SUBMIT, 30, incoming, actor=r.picker_id)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        assert (
            await lane.post(35, text="PRIVATE_PREPLAY_CANARY", source="preplay")
        ).score_status is ApplyStatus.DUPLICATE_EVENT
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_marker_intents") == [(0,)]


@pytest.mark.asyncio
async def test_automatic_opening_timeout_activates_without_rulemaker(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane, automatic=True)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 81, "activation", message_id=1003)
        assert await lane.act("tick", EventKind.TIMER, 85) is OpeningStatus.ACTIVE
        assert rows(path, "SELECT COUNT(*) FROM nr_badges") == [(0,)]
        assert rows(path, "SELECT chosen_by_picker,completed_by_timeout FROM nr_offers") == [(0, 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize("ack", [False, True])
async def test_missing_or_exact_cutoff_ack_enters_recovery_without_invisible_enforcement(
    tmp_path, ack
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        if ack:
            assert (
                await lane.act(
                    "publish", EventKind.ACTIVATION_PUBLISHED, 35, "activation", message_id=1003
                )
                is OpeningStatus.STALE
            )
        else:
            assert await lane.act("tick", EventKind.TIMER, 35) is OpeningStatus.RECOVERY
        assert (await lane.record()).stage == "recovery"
        assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_badges") == [(0,)]
        assert (
            await Lane(store, session="other", channel=203).admit(at=35) is OpeningStatus.CAPACITY
        )


@pytest.mark.asyncio
async def test_exact_expiry_uses_free_capacity_and_active_premium_allows_five_slots(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        execute(path, f"INSERT INTO nr_guild_entitlements VALUES (101,{utc_int(NOW)})")
        assert await Lane(store).admit() is OpeningStatus.ADMITTED
        assert await Lane(store, session="other", channel=203).admit() is OpeningStatus.CAPACITY
        execute(
            path,
            f"UPDATE nr_guild_entitlements SET premium_until_utc={utc_int(NOW + timedelta(seconds=100))}",
        )
        for n in range(1, 5):
            lane = Lane(store, session=f"s{n}", channel=202 + n)
            assert await lane.admit() is OpeningStatus.ADMITTED
            assert (await lane.record()).phrase_draw_limit == 15
        assert await Lane(store, session="sixth", channel=999).admit() is OpeningStatus.CAPACITY


@pytest.mark.asyncio
async def test_expired_premium_protects_admitted_slots_and_does_not_shrink_phrase_window(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        execute(
            path,
            f"INSERT INTO nr_guild_entitlements VALUES (101,{utc_int(NOW + timedelta(seconds=10))})",
        )
        lanes = [Lane(store, session=f"s{n}", channel=202 + n) for n in range(5)]
        for lane in lanes:
            assert await lane.admit() is OpeningStatus.ADMITTED
        for lane in lanes[:-1]:
            assert (
                await lane.act("cancel", EventKind.MANAGER_END, 20, actor=999, authorized=True)
                is OpeningStatus.ENDED
            )
        assert await Lane(store, session="new", channel=999).admit(at=20) is OpeningStatus.CAPACITY
        last = lanes[-1]
        await last.act("publish", EventKind.LOBBY_PUBLISHED, 20, "lobby", message_id=1001)
        await last.act("join", EventKind.LOBBY_JOIN, 21, actor=404)
        await last.act("join", EventKind.LOBBY_JOIN, 22, actor=505)
        assert (await last.record()).phrase_draw_limit == 15
        await last.act("cancel", EventKind.MANAGER_END, 23, actor=999, authorized=True)
        fresh = Lane(store, session="new", channel=999)
        assert await fresh.admit(at=23) is OpeningStatus.ADMITTED
        assert (await fresh.record()).phrase_draw_limit == 5


@pytest.mark.asyncio
async def test_manager_authorization_is_required_starter_has_no_implicit_cancel_power(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        await lane.admit()
        assert (
            await lane.act("cancel", EventKind.MANAGER_END, 1, actor=303, authorized=False)
            is OpeningStatus.UNAUTHORIZED
        )
        assert (await lane.record()).stage == "lobby_prepared"
        assert (
            await lane.act("cancel", EventKind.MANAGER_END, 2, actor=999, authorized=True)
            is OpeningStatus.ENDED
        )
        assert await lane.act("join", EventKind.LOBBY_JOIN, 3, actor=505) is OpeningStatus.ENDED


@pytest.mark.asyncio
async def test_two_connections_cannot_race_past_free_guild_capacity(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        lanes = [Lane(s, session=f"s{n}", channel=202 + n) for n, s in enumerate(stores)]
        statuses = await asyncio.gather(*(lane.admit() for lane in lanes))
        assert sorted(statuses) == sorted([OpeningStatus.ADMITTED, OpeningStatus.CAPACITY])
        assert rows(path, "SELECT COUNT(*) FROM nr_admissions WHERE stage<>'ended'") == [(1,)]


@pytest.mark.asyncio
async def test_two_connections_same_channel_reservation_is_unique(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        execute(
            path,
            f"INSERT INTO nr_guild_entitlements VALUES (101,{utc_int(NOW + timedelta(days=1))})",
        )
        lanes = [Lane(s, session=f"s{n}") for n, s in enumerate(stores)]
        statuses = await asyncio.gather(*(lane.admit() for lane in lanes))
        assert sorted(statuses) == sorted([OpeningStatus.ADMITTED, OpeningStatus.CHANNEL_BUSY])


@pytest.mark.asyncio
async def test_receipt_gap_cannot_overtake_a_queued_third_join(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        await lane.admit()
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await lane.act("join", EventKind.LOBBY_JOIN, 10, actor=404)
        with pytest.raises(FlowOrderError):
            async with store.transaction() as unit:
                await OpeningFlow(unit).tick(
                    lane.frame(EventKind.TIMER, 185, sequence=lane.sequence + 2)
                )
        assert await lane.act("join", EventKind.LOBBY_JOIN, 184, actor=505) is OpeningStatus.OPENING


@pytest.mark.asyncio
@pytest.mark.parametrize("table", ["nr_lobby_members", "nr_opening_outputs"])
async def test_late_admission_failure_rolls_back_capacity_roster_and_prompt(tmp_path, table):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        execute(
            path,
            f"CREATE TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'late admission'); END",
        )
        lane = Lane(store)
        with pytest.raises(sqlite3.IntegrityError):
            await lane.admit()
        assert rows(path, "SELECT COUNT(*) FROM nr_admissions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_lobby_members") == [(0,)]
        execute(path, "DROP TRIGGER fail")
        assert await lane.admit() is OpeningStatus.ADMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table", ["nr_offers", "nr_badges", "nr_round_pacing", "nr_flow_watermarks"]
)
async def test_late_first_activation_failure_rolls_back_every_effect_and_handover(tmp_path, table):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        seq = lane.sequence
        execute(
            path,
            f"CREATE TRIGGER fail BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'late activation'); END",
        )
        with pytest.raises(sqlite3.IntegrityError):
            await lane.act("tick", EventKind.TIMER, 35)
        assert (await lane.record()).stage == "selected"
        assert rows(path, "SELECT COUNT(*) FROM nr_sessions") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_profiles") == [(0,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_flow_watermarks") == [(0,)]
        assert rows(path, "SELECT last_sequence FROM nr_admissions") == [(seq,)]
        execute(path, "DROP TRIGGER fail")
        assert await lane.act("tick", EventKind.TIMER, 35) is OpeningStatus.ACTIVE


@pytest.mark.asyncio
async def test_caught_quorum_error_is_rollback_only_without_consuming_third_human(tmp_path):
    from lets_play.storage.game_store import TransactionAborted

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await lane.admit()
        await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await lane.act("join", EventKind.LOBBY_JOIN, 10, actor=404)
        execute(
            path,
            "CREATE TRIGGER fail BEFORE INSERT ON nr_opening_outputs WHEN NEW.kind='offer' BEGIN SELECT RAISE(ABORT,'late quorum'); END",
        )
        with pytest.raises(TransactionAborted):
            async with store.transaction() as unit:
                try:
                    await OpeningFlow(unit).join(lane.frame(EventKind.LOBBY_JOIN, 11, 505))
                except sqlite3.IntegrityError:
                    pass
        assert (await lane.record()).joined == 2
        assert (await lane.record()).stage == "lobby_published"
        execute(path, "DROP TRIGGER fail")
        assert await lane.act("join", EventKind.LOBBY_JOIN, 11, actor=505) is OpeningStatus.OPENING


@pytest.mark.asyncio
async def test_cancelled_activation_retry_creates_one_first_round(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    real = OpeningFlow._activate
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(self, *args):
        await real(self, *args)
        entered.set()
        await release.wait()

    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        monkeypatch.setattr(OpeningFlow, "_activate", held)
        task = asyncio.create_task(lane.act("tick", EventKind.TIMER, 35))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(0,)]
        assert (await lane.record()).stage == "selected"
        monkeypatch.setattr(OpeningFlow, "_activate", real)
        assert await lane.act("tick", EventKind.TIMER, 35) is OpeningStatus.ACTIVE


@pytest.mark.asyncio
async def test_lost_activation_commit_ack_is_retried_as_replay_without_lost_boundary_message(
    tmp_path, monkeypatch
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        committed, release = asyncio.Event(), asyncio.Event()
        original = store._conn.commit

        async def held():
            await original()
            committed.set()
            await release.wait()

        monkeypatch.setattr(store._conn, "commit", held)
        task = asyncio.create_task(lane.post(35, source="boundary-post"))
        await asyncio.wait_for(committed.wait(), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await lane.post(35, source="boundary-post")).status is FlowStatus.REPLAY
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(1,)]
        assert (await lane.post(36, source="after-boundary")).score_status is ApplyStatus.APPLIED
        assert rows(path, "SELECT ordinary_posts FROM nr_rounds") == [(2,)]


@pytest.mark.asyncio
async def test_two_connections_activate_same_opening_only_once(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        lane = Lane(stores[0])
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        frame = lane.frame(EventKind.TIMER, 35)

        async def run(store):
            async with store.transaction() as unit:
                return await OpeningFlow(unit).tick(frame)

        assert sorted(await asyncio.gather(*(run(s) for s in stores))) == sorted(
            [OpeningStatus.ACTIVE, OpeningStatus.REPLAY]
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_badges") == [(1,)]


@pytest.mark.asyncio
async def test_opening_reopen_retains_roster_hand_deadline_and_known_messages(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await quorum(lane)
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        before = await lane.record()
    async with open_game_store(path) as store:
        lane.store = store
        assert await lane.record() == before
        assert (
            await lane.act("publish", EventKind.OFFER_PUBLISHED, 25, "offer", message_id=1002)
            is OpeningStatus.ALREADY_DONE
        )
        assert (await lane.record()).choice_deadline == before.choice_deadline
        async with store.transaction(write=False) as unit:
            assert await OpeningFlow(unit).known_message_id("session", "lobby") == 1001
            assert await OpeningFlow(unit).known_message_id("session", "offer") == 1002


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,at,message_id", [("lobby", 6, 2001), ("offer", 21, 2002)])
async def test_conflicting_publication_identity_rolls_back_without_resetting_time(
    tmp_path, kind, at, message_id
):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        if kind == "lobby":
            await lane.admit()
            await lane.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        else:
            await quorum(lane)
            await lane.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        before = await lane.record()
        with pytest.raises(ValueError, match="identity"):
            await lane.act(
                "publish",
                EventKind.LOBBY_PUBLISHED if kind == "lobby" else EventKind.OFFER_PUBLISHED,
                at,
                kind,
                message_id=message_id,
            )
        assert await lane.record() == before


@pytest.mark.asyncio
async def test_selected_phrase_is_consumed_only_at_first_activation(tmp_path):
    from lets_play.new_rules.hands import PhraseChoice

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(
            lane,
            card="NR036",
            available_templates=frozenset({"NR004", "NR034", "NR036"}),
            phrase_bank=(PhraseChoice("p", "red duck", "rev"),),
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(0,)]
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        await lane.act("tick", EventKind.TIMER, 35)
        assert rows(path, "SELECT COUNT(*) FROM nr_used_phrases") == [(1,)]
        async with store.transaction(write=False) as unit:
            assert (await unit.get_context("session")).active_instances[0].card_id == "NR036"


@pytest.mark.asyncio
async def test_capability_loss_before_first_activation_preserves_reservation_without_enforcing(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        assert (
            await lane.act(
                "tick", EventKind.TIMER, 35, capabilities=OpeningCapabilities(True, True, False)
            )
            is OpeningStatus.RECOVERY
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(0,)]
        assert (await lane.post(36)).status is FlowStatus.IGNORED


@pytest.mark.asyncio
async def test_known_unpublished_capability_failure_releases_capacity(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        await lane.admit()
        assert (
            await lane.act(
                "capability_changed",
                EventKind.CAPABILITY_CHANGED,
                1,
                OpeningCapabilities(False, True, True),
            )
            is OpeningStatus.ENDED
        )
        assert await Lane(store, session="retry").admit(at=1) is OpeningStatus.ADMITTED


@pytest.mark.asyncio
async def test_active_manager_end_resolves_general_progress_and_releases_slot_before_delivery(
    tmp_path,
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        for _ in range(5):
            await lane.post(35)
        assert (
            await lane.act("cancel", EventKind.MANAGER_END, 36, actor=999, authorized=True)
            is OpeningStatus.ENDED
        )
        async with store.transaction(write=False) as unit:
            profile = await Progression(unit).profile(606)
            assert profile.games_played == 1 and "spotless" in profile.badges
            assert (await unit.get_context("session")).position.lifecycle is Lifecycle.ENDED
            assert [o.kind for o in await OpeningFlow(unit).pending_outputs()] == ["closed"]
        assert await Lane(store, session="fresh").admit(at=36) is OpeningStatus.ADMITTED
        assert (
            await lane.act("cancel", EventKind.MANAGER_END, 37, actor=999, authorized=True)
            is OpeningStatus.ALREADY_DONE
        )


@pytest.mark.asyncio
async def test_manager_end_at_activation_boundary_honors_due_first_order_and_then_ends(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        assert (
            await lane.act("cancel", EventKind.MANAGER_END, 35, actor=999, authorized=True)
            is OpeningStatus.ENDED
        )
        assert rows(path, "SELECT lifecycle FROM nr_sessions") == [("ended",)]
        assert rows(path, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]


@pytest.mark.asyncio
async def test_ended_gameplay_cleanup_removes_opening_guards_but_keeps_global_profile(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.post(30, source="ignored-before-rules")
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        for _ in range(5):
            await lane.post(35)
        await lane.act("cancel", EventKind.MANAGER_END, 36, actor=999, authorized=True)
        async with store.transaction() as unit:
            before = await Progression(unit).profile(606)
            await unit._conn.execute("DELETE FROM nr_sessions WHERE session_id='session'")
        async with store.transaction(write=False) as unit:
            assert await Progression(unit).profile(606) == before
        for table in (
            "nr_admissions",
            "nr_lobby_members",
            "nr_opening_outputs",
            "nr_preplay_sources",
        ):
            assert rows(path, f"SELECT COUNT(*) FROM {table}") == [(0,)]
        assert rows(path, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["playing", "paused", "held"])
async def test_internal_legacy_sessions_in_all_admitted_states_count_against_capacity(
    tmp_path, state
):
    from test_game_store import context, seed

    from lets_play.new_rules.contracts import Advancement, PauseReason

    async with open_game_store(tmp_path / "game.sqlite3") as store:
        ctx = context("NR034")
        await seed(store, ctx)
        if state != "playing":
            owner = replace(
                ctx.position,
                revision=1,
                **(
                    {"lifecycle": Lifecycle.PAUSED, "pause_reason": PauseReason.REVIVAL}
                    if state == "paused"
                    else {"advancement": Advancement.HELD}
                ),
            )
            async with store.transaction() as unit:
                await unit.set_owner(ctx.position, replace(ctx, position=owner))
        assert await Lane(store, session="new", channel=203).admit() is OpeningStatus.CAPACITY


@pytest.mark.asyncio
async def test_raw_playing_seed_cannot_steal_a_lobby_reservation(tmp_path):
    from test_game_store import POSITION, context

    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        await lane.admit()
        ctx = context("NR034", position=replace(POSITION, route=lane.route))
        with pytest.raises(sqlite3.IntegrityError):
            async with store.transaction() as unit:
                await unit.insert_session(ctx)
        assert (await lane.record()).stage == "lobby_prepared"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
async def test_opening_reads_are_bounded(tmp_path, limit):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        async with store.transaction(write=False) as unit:
            with pytest.raises(ValueError):
                await OpeningFlow(unit).pending_outputs(limit=limit)


@pytest.mark.asyncio
async def test_pre_rule_reaction_replay_does_not_consume_a_new_prohibited_target_key(tmp_path):
    from lets_play.new_rules.contracts import EmojiIdentity, ReactionEvent

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane, card="NR006")
        frame = lane.frame(EventKind.REACTION_ADD, 30, 606, source="pre-rule-reaction")
        async with store.transaction() as unit:
            assert (
                await OpeningFlow(unit).ingest(
                    ReactionEvent(frame, 999, EmojiIdentity(unicode="👍"))
                )
            ).status is FlowStatus.IGNORED
        lane.sequence += 1
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        frame = lane.frame(EventKind.REACTION_ADD, 35, 606, source="pre-rule-reaction")
        async with store.transaction() as unit:
            assert (
                await OpeningFlow(unit).ingest(
                    ReactionEvent(frame, 999, EmojiIdentity(unicode="👍"))
                )
            ).score_status is ApplyStatus.DUPLICATE_EVENT
        lane.sequence += 1
        assert rows(path, "SELECT COUNT(*) FROM nr_reaction_offenses") == [(0,)]
        frame = lane.frame(EventKind.REACTION_ADD, 36, 606, source="new-reaction")
        async with store.transaction() as unit:
            assert (
                await OpeningFlow(unit).ingest(
                    ReactionEvent(frame, 999, EmojiIdentity(unicode="👍"))
                )
            ).score_status is ApplyStatus.APPLIED
        assert rows(path, "SELECT COUNT(*) FROM nr_reaction_offenses") == [(1,)]


@pytest.mark.asyncio
async def test_admission_config_and_hand_contain_no_preplay_chat(tmp_path):
    from contextlib import closing

    from lets_play.storage.game_codec import IncompatibleState

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await lane.admit()
        await lane.post(1, text="PRIVATE_ADMISSION_CANARY")
        with closing(sqlite3.connect(path)) as conn:
            assert "PRIVATE_ADMISSION_CANARY" not in "\n".join(conn.iterdump())
        execute(path, "UPDATE nr_admissions SET version='future'")
        with pytest.raises(IncompatibleState):
            await lane.act("tick", EventKind.TIMER, 2)
        assert rows(path, "SELECT last_sequence FROM nr_admissions") == [(2,)]


@pytest.mark.asyncio
async def test_late_first_activation_timer_uses_announced_start_and_no_extra_round(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        assert await lane.act("tick", EventKind.TIMER, 60) is OpeningStatus.ACTIVE
        assert rows(path, "SELECT started_at_utc FROM nr_rounds") == [
            (utc_int(NOW + timedelta(seconds=35)),)
        ]
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]


@pytest.mark.asyncio
async def test_premium_fifth_slot_race_cannot_admit_sixth(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        execute(
            path,
            f"INSERT INTO nr_guild_entitlements VALUES (101,{utc_int(NOW + timedelta(days=1))})",
        )
        for n in range(4):
            assert (
                await Lane(stores[0], session=f"old{n}", channel=300 + n).admit()
                is OpeningStatus.ADMITTED
            )
        lanes = [Lane(s, session=f"race{n}", channel=400 + n) for n, s in enumerate(stores)]
        assert sorted(await asyncio.gather(*(candidate.admit() for candidate in lanes))) == sorted(
            [OpeningStatus.ADMITTED, OpeningStatus.CAPACITY]
        )
        assert rows(path, "SELECT COUNT(*) FROM nr_admissions WHERE stage<>'ended'") == [(5,)]


@pytest.mark.asyncio
async def test_manual_selection_before_expiry_wins_over_following_timeout(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with AsyncExitStack() as stack:
        stores = [await stack.enter_async_context(open_game_store(path)) for _ in range(2)]
        lane = Lane(stores[0])
        rec = await quorum(lane)
        await lane.act("publish", EventKind.OFFER_PUBLISHED, 20, "offer", message_id=1002)
        incoming = next(
            o.incoming.instance_id for o in rec.hand.options if o.incoming.card_id == "NR034"
        )
        manual = lane.frame(EventKind.CHOICE_SUBMIT, 79, rec.picker_id)
        timeout = lane.frame(EventKind.CHOICE_TIMEOUT, 80, sequence=manual.sequence + 1)

        async def pick():
            async with stores[0].transaction() as unit:
                return await OpeningFlow(unit).choose(manual, incoming)

        async def expire():
            try:
                async with stores[1].transaction() as unit:
                    return await OpeningFlow(unit).timeout(timeout, rng=Random(4))
            except FlowOrderError:
                return None

        chosen, automatic = await asyncio.gather(pick(), expire())
        assert chosen is OpeningStatus.SELECTED
        if automatic is None:
            automatic = await expire()
        assert automatic is OpeningStatus.ALREADY_DONE
        assert rows(path, "SELECT chosen_by_picker,selected_incoming FROM nr_admissions") == [
            (1, incoming)
        ]


@pytest.mark.asyncio
async def test_read_only_opening_unit_cannot_reserve_or_mutate(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        lane = Lane(store)
        async with store.transaction(write=False) as unit:
            with pytest.raises(RuntimeError, match="read-only"):
                await OpeningFlow(unit).admit(
                    lane.frame(EventKind.LOBBY_CREATE, 0, 303), capabilities=CAPS, rng=Random(0)
                )


@pytest.mark.asyncio
async def test_old_opening_controls_after_handover_cannot_activate_again(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        lane = Lane(store)
        r = await select(lane)
        await lane.act("publish", EventKind.ACTIVATION_PUBLISHED, 31, "activation", message_id=1003)
        await lane.act("tick", EventKind.TIMER, 35)
        incoming = next(
            o.incoming.instance_id for o in r.hand.options if o.incoming.card_id == "NR034"
        )
        assert (
            await lane.act("choose", EventKind.CHOICE_SUBMIT, 36, incoming, actor=r.picker_id)
            is OpeningStatus.ALREADY_DONE
        )
        async with store.transaction() as unit:
            assert (
                await OrdinaryFlow(unit).timeout(
                    lane.frame(EventKind.CHOICE_TIMEOUT, 80), r.hand.offer_id, rng=Random(0)
                )
            ).status is FlowStatus.ALREADY_DONE
        assert rows(path, "SELECT COUNT(*) FROM nr_rounds") == [(1,)]
        assert rows(path, "SELECT COUNT(*) FROM nr_badges") == [(1,)]
