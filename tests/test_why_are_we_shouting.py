"""Per-line heading enforcement and full-bout progression."""

from dataclasses import replace
from datetime import timedelta

import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import event, service
from test_drawing_conclusions import scheduled
from test_getting_graphic import profile
from test_ordinary_flow import NOW, rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.discord_adapter.discord_io import render
from lets_play.new_rules.breaking_moments import (
    GETTING_GRAPHIC,
    HEADING_REQUIRED,
    WHY_ARE_WE_SHOUTING,
    ModeChoice,
    why_are_we_shouting,
)
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    EventEnvelope,
    EventKind,
    MessageEvent,
    Outcome,
    SessionRoute,
)
from lets_play.presentation import default_presentation
from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS, SCHEMA_VERSION

IMAGE = AttachmentSnapshot(123, "sample.png", "image/png")


@pytest.mark.parametrize(
    "text,outcome",
    [
        ("# hello", Outcome.PASS),
        ("## second level", Outcome.PASS),
        ("### third level", Outcome.PASS),
        ("# first\r\n   \r\n## second\r### third\n", Outcome.PASS),
        ("\n### first\n_ _   \n  _   _  \n## second", Outcome.PASS),
        ("### 🎨", Outcome.PASS),
        ("### header\nplain", Outcome.FAIL),
        ("plain\n### header", Outcome.FAIL),
        ("### first\nplain\n## second", Outcome.FAIL),
        ("### header\n-# a later whisper", Outcome.FAIL),
        (" # indented", Outcome.FAIL),
        ("#hashtag", Outcome.FAIL),
        ("###\ttab", Outcome.FAIL),
        ("###\u00a0not an ASCII space", Outcome.FAIL),
        ("#### too deep", Outcome.FAIL),
        ("###  ", Outcome.FAIL),
        ("```\n### within a fence\n```", Outcome.FAIL),
        ("_ some text _", Outcome.FAIL),
        ("_", Outcome.FAIL),
        ("___", Outcome.FAIL),
        (r"\_ \_", Outcome.FAIL),
        ("* *", Outcome.FAIL),
        ("` `", Outcome.FAIL),
        ("~~ ~~", Outcome.FAIL),
        ("__ __", Outcome.FAIL),
        (None, Outcome.NOT_APPLICABLE),
        ("", Outcome.NOT_APPLICABLE),
        (" \n \t\r\n_ _    ", Outcome.NOT_APPLICABLE),
        ("_ _" + "\n" * 13 + "_ _", Outcome.NOT_APPLICABLE),
        ("### _ _", Outcome.NOT_APPLICABLE),
    ],
)
def test_each_nonblank_line_needs_a_heading(text, outcome):
    frame = EventEnvelope(
        SessionRoute(1, 2, "session"), EventKind.MESSAGE_CREATE, "s", 3, NOW, NOW, 1
    )
    result = why_are_we_shouting(
        MessageEvent(frame, text, attachments=(IMAGE,)), ModeChoice("bout", WHY_ARE_WE_SHOUTING)
    )
    assert result.outcome is outcome
    assert result.reason_code == (HEADING_REQUIRED if outcome is Outcome.FAIL else None)


async def send(runtime, route, source, content="### I can hear you!", actor=606, *, media=False):
    message = event(runtime, route, source, content, actor)
    if media:
        message = replace(message, attachments=(IMAGE,))
    await runtime.submit(route, "event", event=message)


@pytest.mark.asyncio
async def test_each_clean_player_earns_credit_and_head_on_at_full_expiry(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, announced = await scheduled(runtime, monkeypatch, mode=WHY_ARE_WE_SHOUTING)
        runtime.clock.at = announced.offer.effective_at
        await send(runtime, route, 81001, "### first\n\n## second\n# third")
        active = await runtime.state(route.session_id)
        assert active.mode.choice.card_id == WHY_ARE_WE_SHOUTING
        assert active.mode.ends_at == active.pacing.started_at + timedelta(seconds=120)
        description = default_presentation().text(f"cards.{WHY_ARE_WE_SHOUTING}.description")
        assert render(active, kind="rules")[0].description.startswith(f"{description}\n\n")

        await send(runtime, route, 81001, "### first\n\n## second\n# third")
        await send(runtime, route, 81002, "-# whisper\nplain text", media=True)
        await send(runtime, route, 81003, "", media=True)
        await send(runtime, route, 81004, "_ _" + "\n" * 13 + "_ _")
        await send(runtime, route, 81005)
        await send(runtime, route, 81006, "# a heading caption", media=True)
        await send(runtime, route, 81007, "### heading\n* *\n` `", 707)
        await send(runtime, route, 81007, "### heading\n* *\n` `", 707)
        await send(runtime, route, 81020, None, 909, media=True)
        for offset in range(3):
            await send(runtime, route, 81008 + offset, actor=707)
            await send(runtime, route, 81011 + offset, actor=303)
        for offset in range(2):
            await send(runtime, route, 81014 + offset, actor=808)
        assert (await profile(runtime, 606)).games_played == 0
        assert (await profile(runtime, 606)).badges == ()

        runtime.clock.at = active.mode.ends_at
        await runtime.submit(route, "tick")
        await runtime.submit(route, "tick")
        await settle(runtime)
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == active.round_number
        assert restored.pacing.started_at == active.pacing.started_at
        assert restored.context.active_instances == announced.context.active_instances
        assert [kind for _, kind, _ in runtime.transport.messages].count(
            "evolution_mode_ended"
        ) == 1
        assert runtime.transport.markers == [81007]
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("completed",)]

        for actor in (606, 303):
            earned = await profile(runtime, actor)
            assert earned.games_played == 1 and "mode_heading" in earned.badges
        for actor in (707, 808, 909):
            earned = await profile(runtime, actor)
            assert earned.games_played == 0 and earned.badges == ()
        async with runtime.store.transaction(write=False) as unit:
            clean = await unit.get_score(
                route.session_id, 606, round_id=active.context.position.round_id
            )
            failed = await unit.get_score(
                route.session_id, 707, round_id=active.context.position.round_id
            )
            assert (clean.mode_observations, clean.mode_quality, clean.ordinary_posts) == (3, 3, 0)
            assert (failed.mode_observations, failed.mode_quality, failed.mode_faults) == (4, 3, 1)
            assert await unit.get_score(route.session_id, 909) is None
        await posts(runtime, route, base=40000)
        offer = await next_offer(runtime, route)
        assert all(not isinstance(o.incoming, ModeChoice) for o in offer.offer.hand.options)


@pytest.mark.asyncio
@pytest.mark.parametrize("disruption", ["manager_end", "active_reboot", "scheduled_reboot"])
async def test_interrupted_bout_cannot_earn_credit_or_head_on(tmp_path, monkeypatch, disruption):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, announced = await scheduled(runtime, monkeypatch, mode=WHY_ARE_WE_SHOUTING)
        if disruption != "scheduled_reboot":
            runtime.clock.at = announced.offer.effective_at
            for source in range(81001, 81004):
                await send(runtime, route, source)
        if disruption == "manager_end":
            assert await runtime.submit(route, "end", actor=303, authorized=True) == "ended"
            await settle(runtime)
            assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("ended",)]
            earned = await profile(runtime, 606)
            assert earned.games_played == 0 and earned.badges == ()
            return

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        read_in = await reading(runtime, route)
        await fire(runtime, route, read_in.recovery_resume_at + timedelta(seconds=120))
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == 2
        assert restored.pacing.started_at == announced.offer.effective_at
        assert restored.context.active_instances == announced.context.active_instances
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("interrupted",)]
        earned = await profile(runtime, 606)
        assert earned.games_played == 0 and earned.badges == ()


@pytest.mark.asyncio
async def test_schema_18_upgrade_keeps_completed_gif_bout_and_badge(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:18])
        patch.setattr(bootstrap, "SCHEMA_VERSION", 18)
        async with service(tmp_path, patch, include_breaking_moment=True) as runtime:
            route, announced = await scheduled(runtime, patch, mode=GETTING_GRAPHIC)
            runtime.clock.at = announced.offer.effective_at
            for source in range(81001, 81006):
                await send(runtime, route, source, "https://example.invalid/pond.gif")
            active = await runtime.state(route.session_id)
            runtime.clock.at = active.mode.ends_at
            await runtime.submit(route, "tick")
            await settle(runtime)
            assert (await profile(runtime, 606)).badges == ("mode_gif",)
            assert rows(db, "PRAGMA user_version") == [(18,)]

    await bootstrap.initialize_database(db)
    assert rows(db, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert rows(db, "PRAGMA foreign_key_check") == []
    assert rows(db, "SELECT kind,outcome FROM nr_breaking_moments") == [
        (GETTING_GRAPHIC, "completed")
    ]
    assert rows(db, "SELECT badge_key FROM nr_badges WHERE actor_id=606") == [("mode_gif",)]
