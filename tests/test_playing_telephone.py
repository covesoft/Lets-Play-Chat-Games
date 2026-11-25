"""Playing Telephone through the Discord adapter, receipt lane, and SQLite."""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import event, service
from test_drawing_conclusions import scheduled
from test_evaluation import POSITION, context, message
from test_getting_graphic import profile
from test_ordinary_flow import NOW, rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.discord_adapter.discord_io import message_snapshot, render
from lets_play.new_rules.breaking_moments import (
    MOMENT_OF_SILENCE,
    PLAYING_TELEPHONE,
    VOICE_MEMO_REQUIRED,
    ModeChoice,
)
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    ForwardedMessageSnapshot,
    Outcome,
    Phase,
    ReferenceKind,
)
from lets_play.new_rules.evaluation import evaluate_message
from lets_play.presentation import default_presentation
from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS, SCHEMA_VERSION

CHOICE = ModeChoice("telephone-bout", PLAYING_TELEPHONE)
MODE = context("NR034", position=replace(POSITION, phase=Phase.MODE))
MEMO = AttachmentSnapshot(123, "voice-message.ogg", "audio/ogg", 4.0)
VOICE = {"is_voice_message": True, "attachments": (MEMO,)}
FORWARD = {
    "reference_kind": ReferenceKind.FORWARD,
    "forwarded_message": ForwardedMessageSnapshot("", True, is_voice_memo=True),
}


@pytest.mark.parametrize(
    ("duration", "qualifies"),
    [
        (None, False),
        (float("nan"), False),
        (float("inf"), False),
        (-1.0, False),
        (3.999, False),
        (4.0, True),
        (4.001, True),
    ],
)
def test_native_voice_passes_and_duration_only_controls_credit(duration, qualifies):
    voice = message(
        "", is_voice_message=True, attachments=(replace(MEMO, duration_seconds=duration),)
    )
    decision = evaluate_message(voice, MODE, mode=CHOICE)
    assert decision.evaluations[0].outcome is Outcome.PASS
    assert decision.qualifying_voice_memo is qualifies
    assert (decision.score.mode_quality, decision.score.raw_faults) == (1, 0)


@pytest.mark.parametrize(
    ("text", "metadata", "outcome"),
    [
        ("", FORWARD, Outcome.NOT_APPLICABLE),
        ("a note", FORWARD, Outcome.FAIL),
        (" ", VOICE, Outcome.FAIL),
        ("a caption", VOICE, Outcome.FAIL),
        ("hello", {}, Outcome.FAIL),
        ("", {"attachments": (MEMO,)}, Outcome.FAIL),
        ("", {"is_voice_message": True}, Outcome.FAIL),
        ("", {**VOICE, "sticker_ids": (601,)}, Outcome.FAIL),
        ("", {**VOICE, "attachments": (MEMO, replace(MEMO, attachment_id=124))}, Outcome.FAIL),
        ("", {**FORWARD, "attachments": (MEMO,)}, Outcome.FAIL),
        ("", {**FORWARD, "forwarded_message": ForwardedMessageSnapshot("text")}, Outcome.FAIL),
        ("", {**FORWARD, "forwarded_message": None}, Outcome.FAIL),
        (
            "",
            {
                **FORWARD,
                "forwarded_message": ForwardedMessageSnapshot("caption", True, is_voice_memo=True),
            },
            Outcome.FAIL,
        ),
        ("", {"sticker_ids": (601,)}, Outcome.FAIL),
        ("", {"is_native_poll": True}, Outcome.FAIL),
    ],
)
def test_telephone_other_posts(text, metadata, outcome):
    decision = evaluate_message(message(text, **metadata), MODE, mode=CHOICE)
    result = decision.evaluations[0]
    assert result.outcome is outcome
    assert not decision.qualifying_voice_memo
    assert decision.score.ordinary_posts == decision.score.mode_quality == 0
    assert decision.score.raw_faults == (outcome is Outcome.FAIL)
    assert result.reason_code == (VOICE_MEMO_REQUIRED if outcome is Outcome.FAIL else None)


@pytest.mark.parametrize("forwarded", [False, True])
def test_discord_sdk_projects_native_voice_flags_and_duration(forwarded):
    client = discord.Client(intents=discord.Intents.none())
    voice_flag = discord.MessageFlags(voice=True).value
    attachment = {
        "id": "123",
        "filename": "voice-message.ogg",
        "size": 5000,
        "url": "https://example.invalid/audio.ogg",
        "proxy_url": "https://example.invalid/proxy.ogg",
        "content_type": "audio/ogg",
        "duration_secs": 4.0,
        "waveform": "AA==",
    }
    data = {
        "id": "1441341766047752159",
        "type": 0,
        "content": "",
        "author": {"id": "3", "username": "Tester", "discriminator": "0", "avatar": None},
    }
    if forwarded:
        data["message_reference"] = {"type": 1, "message_id": "99", "channel_id": "20"}
        data["message_snapshots"] = [
            {
                "message": {
                    "type": 0,
                    "content": "",
                    "flags": voice_flag,
                    "embeds": [],
                    "attachments": [attachment],
                    "timestamp": NOW.isoformat(),
                    "edited_timestamp": None,
                }
            }
        ]
    else:
        data.update(flags=voice_flag, attachments=[attachment])
    raw = discord.Message(state=client._connection, channel=NS(guild=None, id=2), data=data)
    snapshot = message_snapshot(raw, POSITION.route, NOW)
    decision = evaluate_message(snapshot, MODE, mode=CHOICE)
    assert decision.evaluations[0].outcome is (
        Outcome.NOT_APPLICABLE if forwarded else Outcome.PASS
    )
    assert decision.qualifying_voice_memo is (not forwarded)


def voice_event(runtime, route, source, actor, *, duration=4.0):
    return replace(
        event(runtime, route, source, "", actor),
        is_voice_message=True,
        attachments=(replace(MEMO, duration_seconds=duration),),
    )


@pytest.mark.asyncio
async def test_three_distinct_memos_award_at_expiry_and_discard_bout_counts(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, announced = await scheduled(runtime, monkeypatch, mode=PLAYING_TELEPHONE)
        runtime.clock.at = announced.offer.effective_at
        very_vocal = [
            voice_event(runtime, route, source, 606, duration=duration)
            for source, duration in [(81001, 4.0), (81002, 4.2), (81003, 9.0), (81004, 6.0)]
        ]
        short = voice_event(runtime, route, 81005, 707, duration=3.999)
        forward = replace(event(runtime, route, 81006, "", 707), **FORWARD)
        whisper = replace(voice_event(runtime, route, 81007, 707), content="-# whisper")
        bad = event(runtime, route, 81008, "a regular post", 606)
        for item in (
            *very_vocal,
            very_vocal[0],
            short,
            forward,
            whisper,
            bad,
            voice_event(runtime, route, 81009, 707),
            voice_event(runtime, route, 81010, 707),
        ):
            await runtime.submit(route, "event", event=item)
        await settle(runtime)
        active = await runtime.state(route.session_id)
        copy = default_presentation()
        assert active.mode.ends_at == announced.offer.effective_at + timedelta(seconds=120)
        embed, _ = render(active, kind="rules")
        assert embed.title == copy.text(
            "mode.named_title", name=copy.text("cards.playing_telephone.name")
        )
        counts = "SELECT actor_id,qualifying_memos FROM nr_telephone_progress ORDER BY actor_id"
        assert rows(db, counts) == [(606, 3), (707, 2)]
        assert "mode_telephone" not in (await profile(runtime, 606)).badges
        assert (await profile(runtime, 606)).games_played == 0
        async with runtime.store.transaction(write=False) as unit:
            score = await unit.get_score(route.session_id, 707)
            assert (score.mode_observations, score.mode_quality, score.raw_faults) == (3, 3, 0)

        runtime.clock.at = active.mode.ends_at
        await runtime.submit(route, "event", event=voice_event(runtime, route, 81011, 707))
        await runtime.submit(route, "tick")
        await settle(runtime)
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == active.round_number
        assert restored.pacing.started_at == active.pacing.started_at
        assert restored.context.active_instances == announced.context.active_instances
        assert 81008 in runtime.transport.markers and 81006 not in runtime.transport.markers
        assert rows(db, counts) == []
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("completed",)]
        assert (await profile(runtime, 606)).badges == ("mode_telephone",)
        assert (await profile(runtime, 606)).games_played == 1
        assert "mode_telephone" not in (await profile(runtime, 707)).badges
        assert (await profile(runtime, 707)).games_played == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("disruption", ["manager_end", "active_reboot", "scheduled_reboot"])
async def test_end_and_recovery_only_resolve_memos_already_observed(
    tmp_path, monkeypatch, disruption
):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, announced = await scheduled(runtime, monkeypatch, mode=PLAYING_TELEPHONE)
        if disruption != "scheduled_reboot":
            runtime.clock.at = announced.offer.effective_at
            for offset in range(3):
                await runtime.submit(
                    route, "event", event=voice_event(runtime, route, 81001 + offset, 606)
                )
        if disruption == "manager_end":
            assert await runtime.submit(route, "end", actor=303, authorized=True) == "ended"
            await settle(runtime)
            assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("ended",)]
            assert (await profile(runtime, 606)).badges == ("mode_telephone",)
            assert (await profile(runtime, 606)).games_played == 1
            assert rows(db, "SELECT * FROM nr_telephone_progress") == []
            return

    async with service(tmp_path, monkeypatch, transport=RecoveryTransport()) as runtime:
        read_in = await reading(runtime, route)
        await fire(runtime, route, read_in.recovery_resume_at + timedelta(seconds=120))
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == 2
        assert restored.context.active_instances == announced.context.active_instances
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("interrupted",)]
        actor = await profile(runtime, 606)
        assert actor.badges == (("mode_telephone",) if disruption == "active_reboot" else ())
        assert actor.games_played == (disruption == "active_reboot")
        assert rows(db, "SELECT * FROM nr_telephone_progress") == []


@pytest.mark.asyncio
async def test_schema_20_upgrade_preserves_completed_silence_and_quietude(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:20])
        patch.setattr(bootstrap, "SCHEMA_VERSION", 20)
        async with service(tmp_path, patch, include_breaking_moment=True) as runtime:
            route, announced = await scheduled(runtime, patch, mode=MOMENT_OF_SILENCE)
            runtime.clock.at = announced.offer.effective_at + timedelta(seconds=120)
            await runtime.submit(route, "tick")
            await settle(runtime)
            assert "mode_silence" in (await profile(runtime, 303)).badges
            assert rows(db, "PRAGMA user_version") == [(20,)]

    await bootstrap.initialize_database(db)
    assert rows(db, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rows(db, "PRAGMA foreign_key_check") == []
    assert rows(db, "SELECT kind,outcome FROM nr_breaking_moments") == [
        (MOMENT_OF_SILENCE, "completed")
    ]
    assert rows(
        db, "SELECT actor_id FROM nr_badges WHERE badge_key='mode_silence' ORDER BY actor_id"
    ) == [
        (303,),
        (404,),
        (505,),
    ]
    assert rows(db, "SELECT * FROM nr_telephone_progress") == []
