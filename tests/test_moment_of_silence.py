"""Moment of Silence receipts, native poll metadata, and Quietude progression."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS

import discord
import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import event, service
from test_drawing_conclusions import scheduled
from test_evaluation import POSITION, context, message
from test_getting_graphic import profile
from test_ordinary_flow import NOW, rows
from test_reboot_recovery import RecoveryTransport, fire, reading
from test_why_are_we_shouting import send

from lets_play.discord_adapter.discord_io import message_snapshot
from lets_play.new_rules.breaking_moments import (
    MOMENT_OF_SILENCE,
    SILENCE_REQUIRED,
    WHY_ARE_WE_SHOUTING,
    ModeChoice,
)
from lets_play.new_rules.contracts import AttachmentSnapshot, Outcome, Phase, ReferenceKind
from lets_play.new_rules.evaluation import evaluate_message
from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS, SCHEMA_VERSION

CHOICE = ModeChoice("bout", MOMENT_OF_SILENCE)
MODE = context("NR034", position=replace(POSITION, phase=Phase.MODE))
IMAGE = AttachmentSnapshot(123, "sample.png", "image/png")


@pytest.mark.parametrize(
    ("text", "metadata"),
    [
        ("hello", {}),
        ("🎨", {}),
        ("https://example.invalid/a.gif", {}),
        (" # normal content", {}),
        ("-#no space", {}),
        ("hello\n-# later line", {}),
        (None, {"sticker_ids": (607,)}),
        (None, {"attachments": (IMAGE,)}),
        (None, {"reference_kind": ReferenceKind.FORWARD}),
        ("hello", {"reference_kind": ReferenceKind.REPLY, "referenced_message_id": 502}),
        (None, {"is_voice_message": True}),
        ("x" * 20001, {}),
    ],
)
def test_silence_faults_every_nonexempt_human_post_once(text, metadata):
    decision = evaluate_message(message(text, **metadata), MODE, mode=CHOICE)
    assert len(decision.evaluations) == 1
    assert decision.evaluations[0].outcome is Outcome.FAIL
    assert decision.evaluations[0].reason_code == SILENCE_REQUIRED
    assert decision.score.raw_faults == decision.score.mode_observations == 1
    assert decision.score.mode_quality == decision.score.ordinary_posts == 0
    assert decision.needs_marker


@pytest.mark.parametrize(
    "post",
    [
        message("-# quiet", attachments=(IMAGE,)),
        message("Poll caption", is_native_poll=True),
        message(None, is_native_poll=True),
        message("app output", is_bot=True),
        message("system event", is_system=True),
        message("webhook output", is_webhook=True),
    ],
)
def test_silence_ignored_posts_create_no_decision(post):
    assert evaluate_message(post, MODE, mode=CHOICE) is None


@pytest.mark.parametrize("kind", ["native", "result", "link"])
def test_discord_sdk_distinguishes_native_poll_messages_from_links(kind):
    client = discord.Client(intents=discord.Intents.none())
    data = {
        "id": "1441341766047752159",
        "type": discord.MessageType.poll_result.value if kind == "result" else 0,
        "content": "https://discord.com/channels/1/2/3" if kind == "link" else "",
        "author": {"id": "3", "username": "Tester", "discriminator": "0", "avatar": None},
    }
    if kind == "native":
        data["poll"] = {
            "question": {"text": "Duck or goose?"},
            "answers": [
                {"answer_id": 1, "poll_media": {"text": "Duck"}},
                {"answer_id": 2, "poll_media": {"text": "Goose"}},
            ],
            "expiry": "2026-11-25T00:00:00+00:00",
        }
    raw = discord.Message(state=client._connection, channel=NS(guild=None, id=2), data=data)
    snapshot = message_snapshot(raw, POSITION.route, NOW)
    assert snapshot.is_native_poll is (kind != "link")
    assert (evaluate_message(snapshot, MODE, mode=CHOICE) is None) is (kind != "link")


@pytest.mark.asyncio
async def test_full_bout_uses_outgoing_round_eligibility_and_restores_rules(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, announced = await scheduled(runtime, monkeypatch, mode=MOMENT_OF_SILENCE)
        starts = announced.offer.effective_at
        runtime.clock.at = starts - timedelta(microseconds=1)
        # Both actors missed the picker snapshot. Only 606 reaches five at cutover.
        for offset in range(5):
            await runtime.submit(
                route, "event", event=event(runtime, route, 81000 + offset, "ready over", 606)
            )
        for offset in range(4):
            await runtime.submit(
                route, "event", event=event(runtime, route, 81100 + offset, "ready over", 707)
            )

        runtime.clock.at = starts
        whisper = event(runtime, route, 81201, "-# quiet", 303)
        poll = replace(event(runtime, route, 81202, "", 505), is_native_poll=True)
        fault = replace(event(runtime, route, 81203, None, 404), sticker_ids=(601,))
        for item in (whisper, poll, fault, fault):
            await runtime.submit(route, "event", event=item)
        await settle(runtime)
        active = await runtime.state(route.session_id)
        assert active.mode.ends_at == starts + timedelta(seconds=120)
        round_id = active.context.position.round_id
        for actor in (303, 404, 505, 606, 707):
            assert "mode_silence" not in (await profile(runtime, actor)).badges
        async with runtime.store.transaction(write=False) as unit:
            traffic = await unit.get_traffic(route.session_id, round_id=round_id)
            assert (traffic.human_messages, traffic.whispers) == (1, 0)
            for actor in (303, 505, 606, 707):
                assert await unit.get_score(route.session_id, actor, round_id=round_id) is None
            score = await unit.get_score(route.session_id, 404, round_id=round_id)
            assert (score.mode_observations, score.mode_quality, score.ordinary_posts) == (1, 0, 0)

        runtime.clock.at = active.mode.ends_at
        # The first post on the expiry boundary is under the restored ordinary rules.
        await runtime.submit(route, "event", event=event(runtime, route, 81204, "back over", 606))
        for item in (poll, whisper, fault):
            await runtime.submit(route, "event", event=item)
        await runtime.submit(route, "tick")
        await settle(runtime)
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == active.round_number
        assert restored.context.active_instances == announced.context.active_instances
        assert restored.pacing.started_at == starts
        assert runtime.transport.markers == [81203]
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("completed",)]
        assert [kind for _, kind, _ in runtime.transport.messages].count(
            "evolution_mode_ended"
        ) == 1
        for actor in (303, 505, 606):
            earned = await profile(runtime, actor)
            assert earned.games_played == 1 and "mode_silence" in earned.badges
        for actor in (404, 707, 999):
            assert "mode_silence" not in (await profile(runtime, actor)).badges
        async with runtime.store.transaction(write=False) as unit:
            traffic = await unit.get_traffic(route.session_id, round_id=round_id)
            assert (traffic.human_messages, traffic.whispers) == (2, 0)
            score = await unit.get_score(route.session_id, 606, round_id=round_id)
            assert score.ordinary_posts == 1 and score.raw_faults == 0

        await posts(runtime, route, actors=(303, 404, 505, 606), base=40000)
        offer = await next_offer(runtime, route)
        assert all(not isinstance(o.incoming, ModeChoice) for o in offer.offer.hand.options)


@pytest.mark.asyncio
@pytest.mark.parametrize("disruption", ["manager_end", "active_reboot", "scheduled_reboot"])
async def test_interrupted_silence_does_not_award_quietude(tmp_path, monkeypatch, disruption):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, announced = await scheduled(runtime, monkeypatch, mode=MOMENT_OF_SILENCE)
        if disruption != "scheduled_reboot":
            runtime.clock.at = announced.offer.effective_at
            await runtime.submit(route, "tick")
            await settle(runtime)
        if disruption == "manager_end":
            assert await runtime.submit(route, "end", actor=303, authorized=True) == "ended"
            await settle(runtime)
            assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("ended",)]
            assert "mode_silence" not in (await profile(runtime, 303)).badges
            return

    async with service(tmp_path, monkeypatch, transport=RecoveryTransport()) as runtime:
        read_in = await reading(runtime, route)
        await fire(runtime, route, read_in.recovery_resume_at + timedelta(seconds=120))
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == 2
        assert restored.pacing.started_at == announced.offer.effective_at
        assert restored.context.active_instances == announced.context.active_instances
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("interrupted",)]
        assert "mode_silence" not in (await profile(runtime, 303)).badges


@pytest.mark.asyncio
async def test_schema_19_upgrade_keeps_completed_heading_bout_and_badge(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:19])
        patch.setattr(bootstrap, "SCHEMA_VERSION", 19)
        async with service(tmp_path, patch, include_breaking_moment=True) as runtime:
            route, announced = await scheduled(runtime, patch, mode=WHY_ARE_WE_SHOUTING)
            runtime.clock.at = announced.offer.effective_at
            for source in range(82000, 82003):
                await send(runtime, route, source)
            active = await runtime.state(route.session_id)
            runtime.clock.at = active.mode.ends_at
            await runtime.submit(route, "tick")
            await settle(runtime)
            assert (await profile(runtime, 606)).badges == ("mode_heading",)
            assert rows(db, "PRAGMA user_version") == [(19,)]

    await bootstrap.initialize_database(db)
    assert rows(db, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rows(db, "PRAGMA foreign_key_check") == []
    assert rows(db, "SELECT kind,outcome FROM nr_breaking_moments") == [
        (WHY_ARE_WE_SHOUTING, "completed")
    ]
    assert rows(db, "SELECT badge_key FROM nr_badges WHERE actor_id=606") == [("mode_heading",)]
