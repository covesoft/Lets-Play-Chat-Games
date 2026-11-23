"""The supported Breaking Moment through the Discord lane, SQLite and reboot."""

from datetime import timedelta

import discord
import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import activated, choice, event, opened, service
from test_ordinary_flow import NOW, execute, rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.discord_adapter.discord_io import render
from lets_play.new_rules.breaking_moments import (
    BREAKING_MOMENT_SECONDS,
    DRAWING_CONCLUSIONS,
    MODE_VALIDATORS,
    ModeChoice,
    drawing_conclusions,
    mode_ends_at,
)
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    MessageEvent,
    Outcome,
    ReactionEvent,
    SessionRoute,
)
from lets_play.presentation import default_presentation
from lets_play.storage import bootstrap, evolution_flow
from lets_play.storage.migrations import MIGRATIONS, SCHEMA_VERSION
from lets_play.storage.progression import Progression
from lets_play.storage.summary_store import SummaryStore


class DrawLast:
    """Take the last template ticket first, where the optional mode is listed."""

    def __init__(self, seed):
        self.draws = 0

    def randrange(self, size):
        self.draws += 1
        if self.draws == 2:
            return self.mode_index
        return size - 1


async def scheduled(runtime, monkeypatch, *, expressionless=False, mode=DRAWING_CONCLUSIONS):
    class DrawMode(DrawLast):
        mode_index = tuple(MODE_VALIDATORS).index(mode)

    monkeypatch.setattr(evolution_flow, "Random", DrawMode)
    route = await opened(runtime)
    if expressionless:
        opening = await choice(runtime, route)
        index = next(
            i
            for i, option in enumerate(opening.admission.hand.options)
            if option.incoming.card_id == "NR006"
        )
        await runtime.submit(
            route,
            "choose",
            actor=opening.admission.picker_id,
            message_id=opening.panel_id,
            index=index,
        )
        await settle(runtime)
        runtime.clock.at = (await runtime.state(route.session_id)).admission.effective_at
        await runtime.submit(route, "tick")
    else:
        await activated(runtime, route)
    before = await runtime.state(route.session_id)
    assert all(not isinstance(o.incoming, ModeChoice) for o in before.admission.hand.options)
    await posts(runtime, route)
    offer = await next_offer(runtime, route)
    options = offer.offer.hand.options
    (index,) = (i for i, o in enumerate(options) if isinstance(o.incoming, ModeChoice))
    assert options[index].outgoing_ids == ()
    embed, view = render(offer)
    copy = default_presentation()
    name = copy.text(f"cards.{mode}.name")
    description = copy.text(f"cards.{mode}.description")
    mode_label = copy.text("mode.choice_label", minutes=BREAKING_MOMENT_SECONDS // 60)
    assert embed.title == copy.text("ordinary.offer_title")
    assert embed.fields[index].name == view.children[index].label == name
    assert embed.fields[index].value == f"*{mode_label}*\n{description}"
    assert all(mode_label not in field.value for i, field in enumerate(embed.fields) if i != index)
    assert [item.style for item in view.children] == [
        discord.ButtonStyle.primary if i == index else discord.ButtonStyle.secondary
        for i in range(len(options))
    ]
    assert (
        await runtime.submit(
            route,
            "choose",
            actor=offer.offer.hand.picker_id,
            message_id=offer.panel_id,
            index=index,
        )
        == "selected"
    )
    await settle(runtime)
    state = await runtime.state(route.session_id)
    assert state.offer_display_ready
    assert state.round_number == 1 and state.mode is None
    rules_embed, _ = render(state, kind="rules")
    assert name not in rules_embed.title
    assert all(field.name != name for field in rules_embed.fields)
    activation = next(
        s for _, kind, s in runtime.transport.messages if kind == "ordinary_activation"
    )
    embed, _ = render(activation, kind="ordinary_activation")
    assert embed.title == copy.text("mode.named_title", name=name)
    start = state.offer.effective_at
    timing = copy.text(
        "mode.scheduled_description",
        starts=int(start.timestamp()),
        ends=int(mode_ends_at(start).timestamp()),
    )
    assert embed.description == f"{description}\n\n{timing}"
    assert not embed.fields
    return route, state


def reaction(runtime, route, source):
    at = runtime.clock.now()
    return ReactionEvent(
        EventEnvelope(route, EventKind.REACTION_ADD, source, 606, at, at, 0),
        87654,
        EmojiIdentity(unicode="🎨"),
    )


@pytest.mark.parametrize(
    ("text", "metadata", "passes"),
    [
        ("  👨‍👩‍👧‍👦 🇨🇦\n1️⃣ 🫱🏽‍🫲🏻 ", {}, True),
        ("<:star:123456789123456789><a:spin:123456789123456788> 🎨", {}, True),
        ("❤️", {}, True),
        ("", {}, False),
        (" \n ", {}, False),
        ("🎨 hello", {}, False),
        ("**🎨**", {}, False),
        ("🎨 https://example.invalid", {}, False),
        ("🎨 \u200d", {}, False),
        ("1", {}, False),
        ("<:no_id:>", {}, False),
        ("🎨", {"sticker_ids": (123,)}, False),
        ("🎨", {"attachments": (AttachmentSnapshot(123, "fixture.png"),)}, False),
        ("🎨", {"is_voice_message": True}, False),
    ],
)
def test_emoji_only(text, metadata, passes):
    frame = EventEnvelope(
        SessionRoute(1, 2, "session"), EventKind.MESSAGE_CREATE, "s", 3, NOW, NOW, 1
    )
    result = drawing_conclusions(MessageEvent(frame, text, **metadata), ModeChoice("bout"))
    assert (result.outcome is Outcome.PASS) == passes


@pytest.mark.asyncio
async def test_mode_cutover_scoring_timer_award_restore_and_hand_spacing(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, original = await scheduled(runtime, monkeypatch, expressionless=True)
        starts = original.offer.effective_at
        runtime.clock.at = starts - timedelta(microseconds=1)
        await runtime.submit(route, "event", event=event(runtime, route, 81000, "old round"))
        runtime.clock.at = starts
        first = event(runtime, route, 81001, "🎨")
        await runtime.submit(route, "event", event=first)
        active = await runtime.state(route.session_id)
        assert active.round_number == 2 and active.pacing.started_at == starts
        assert active.mode.ends_at == starts + timedelta(seconds=120) == active.next_deadline
        assert active.context.active_instances == original.context.active_instances
        assert route.session_id in runtime.timers
        embed, _ = render(active, kind="rules")
        copy = default_presentation()
        name = copy.text(f"cards.{DRAWING_CONCLUSIONS}.name")
        description = copy.text(f"cards.{DRAWING_CONCLUSIONS}.description")
        timing = copy.text("mode.active_description", ends=int(active.mode.ends_at.timestamp()))
        assert embed.title == copy.text("mode.named_title", name=name)
        assert embed.description == f"{description}\n\n{timing}"
        assert not embed.fields

        await runtime.submit(route, "event", event=first)
        ignored = reaction(runtime, route, "mode-reaction")
        await runtime.submit(route, "event", event=ignored)
        await runtime.submit(route, "event", event=event(runtime, route, 81002, "🤝"))
        await runtime.submit(route, "event", event=event(runtime, route, 81003, "😀", 707))
        await runtime.submit(
            route, "event", event=event(runtime, route, 81004, "-# ← whisper", 808)
        )
        async with runtime.store.transaction(write=False) as unit:
            assert (await Progression(unit).profile(606)).games_played == 0
            assert await unit.get_score(route.session_id, 808) is None
        await runtime.submit(route, "event", event=event(runtime, route, 81005, "😂"))
        bad = event(runtime, route, 81006, "emoji and words 😂")
        await runtime.submit(route, "event", event=bad)
        await runtime.submit(route, "event", event=bad)
        runtime.clock.at += timedelta(seconds=30)
        assert await runtime.submit(route, "summary_request", source="123") == "queued"
        await settle(runtime)
        assert runtime.transport.markers == [81006]
        assert [i.card_id for i in runtime.transport.summaries[0][1].rules] == [
            "drawing_conclusions"
        ]
        async with runtime.store.transaction(write=False) as unit:
            progress = Progression(unit)
            player = await progress.profile(606)
            assert player.games_played == 1 and player.badges == ("mode_emoji",)
            assert (await progress.profile(707)).games_played == 0
            assert "rulemaker" in (await progress.profile(original.offer.hand.picker_id)).badges
            score = await unit.get_score(
                route.session_id, 606, round_id=active.context.position.round_id
            )
            assert (score.mode_observations, score.mode_quality, score.mode_faults) == (4, 3, 1)
            assert (score.ordinary_posts, score.reaction_failures) == (0, 0)
            traffic = await unit.get_traffic(
                route.session_id, round_id=active.context.position.round_id
            )
            assert (traffic.human_messages, traffic.whispers) == (6, 1)
            assert await SummaryStore(unit).next_deadline(route.session_id) is None

        runtime.clock.at = active.mode.ends_at
        runtime.timers[route.session_id].cancel()
        runtime._timer(route, "tick")
        await settle(runtime)
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.context.position.phase == "ordinary"
        assert restored.context.position.round_id == active.context.position.round_id
        assert restored.context.active_instances == original.context.active_instances
        assert restored.pacing.started_at == starts
        assert [f.name for f in render(restored, kind="rules")[0].fields] == [
            copy.text("cards.NR006.name")
        ]
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("completed",)]
        assert rows(db, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]
        await runtime.submit(route, "event", event=ignored)
        await runtime.submit(route, "event", event=reaction(runtime, route, "real-reaction"))
        async with runtime.store.transaction(write=False) as unit:
            assert (await unit.get_score(route.session_id, 606)).reaction_failures == 1
        await posts(runtime, route, base=40000)
        offer = await next_offer(runtime, route)
        assert all(not isinstance(o.incoming, ModeChoice) for o in offer.offer.hand.options)


@pytest.mark.asyncio
@pytest.mark.parametrize("disrupted", ["scheduled", "active", "manager_end", "missing_proof"])
async def test_reopen_interrupts_bout_or_finishes_the_announced_round(
    tmp_path, monkeypatch, disrupted
):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, saved = await scheduled(runtime, monkeypatch)
        cutover = saved.offer.effective_at
        if disrupted != "scheduled":
            runtime.clock.at = cutover
            for n in range(3):
                await runtime.submit(route, "event", event=event(runtime, route, 81500 + n, "🎨"))
            await settle(runtime)
            saved = await runtime.state(route.session_id)
    if disrupted == "missing_proof":
        execute(
            db,
            "DELETE FROM nr_discord_deliveries WHERE family='ordinary' AND output_id IN "
            "(SELECT output_id FROM nr_ordinary_outputs WHERE kind='activation')",
        )
    transport = RecoveryTransport()
    async with service(
        tmp_path, monkeypatch, transport=transport, include_breaking_moment=True
    ) as runtime:
        if disrupted == "missing_proof":
            await settle(runtime)
            state = await runtime.state(route.session_id)
            assert state.recovery_id and not state.recovery_resume_at and not transport.recoveries
            return
        read_in = await reading(runtime, route)
        assert len(transport.recoveries) == 1 and not transport.messages
        assert read_in.context.position.phase == (
            "ordinary" if disrupted == "scheduled" else "mode"
        )
        if disrupted == "manager_end":
            assert await runtime.submit(route, "end", actor=303, authorized=True) == "closed"
            await settle(runtime)
            assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("ended",)]
            assert rows(db, "SELECT COUNT(*) FROM nr_round_resolutions") == [(2,)]
            async with runtime.store.transaction(write=False) as unit:
                assert (await Progression(unit).profile(606)).badges == ("mode_emoji",)
            return
        during_read_in = event(runtime, route, 81600, "🎨")
        await runtime.submit(route, "event", event=during_read_in)
        await fire(runtime, route, read_in.recovery_resume_at)
        resumed = await runtime.state(route.session_id)
        assert resumed.recovery_id is None and resumed.mode is None
        assert resumed.context.position.phase == "ordinary"
        assert resumed.context.active_instances == saved.context.active_instances
        assert resumed.round_number == 2 and resumed.pacing.started_at == cutover
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("interrupted",)]
        assert rows(db, "SELECT COUNT(*) FROM nr_round_resolutions") == [(1,)]
        assert rows(db, "SELECT output_id FROM nr_evolution_outputs WHERE kind='mode_ended'") == []
        assert not any(kind == "evolution_mode_ended" for _, kind, _ in transport.messages)
        assert len(transport.probes) == 2
        await runtime.submit(route, "event", event=during_read_in)
        await runtime.submit(route, "event", event=event(runtime, route, 81601, "over", 707))
        async with runtime.store.transaction(write=False) as unit:
            earned = await unit.get_score(route.session_id, 606)
            if disrupted == "active":
                assert earned.human_messages == 3 and earned.mode_quality == 3
                assert (await Progression(unit).profile(606)).badges == ("mode_emoji",)
            else:
                assert earned is None
            assert (await unit.get_score(route.session_id, 707)).ordinary_posts == 1


@pytest.mark.asyncio
async def test_schema_15_retains_existing_profiles_awards_and_foreign_keys(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:14])
        patch.setattr(bootstrap, "SCHEMA_VERSION", 14)
        assert (await bootstrap.initialize_database(db)).schema_version == 14
    execute(db, "INSERT INTO nr_profiles VALUES(303, 10)")
    execute(db, "INSERT INTO nr_badges VALUES(303, 'familiar_face')")
    assert (await bootstrap.initialize_database(db)).schema_version == SCHEMA_VERSION
    assert rows(db, "SELECT * FROM nr_profiles") == [(303, 10)]
    assert rows(db, "SELECT * FROM nr_badges") == [(303, "familiar_face")]
    execute(db, "INSERT INTO nr_badges VALUES(303, 'mode_emoji');")
    assert rows(db, "PRAGMA foreign_key_check") == []
