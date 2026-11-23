"""GIF-only enforcement and the two independent progression thresholds."""

import sqlite3
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from test_discord_ordinary_loop import next_offer, posts, settle
from test_discord_runtime import event, service
from test_drawing_conclusions import scheduled
from test_ordinary_flow import NOW, rows
from test_reboot_recovery import RecoveryTransport, fire, reading

from lets_play.discord_adapter.discord_io import DiscordTransport, message_snapshot, render
from lets_play.new_rules.breaking_moments import (
    DRAWING_CONCLUSIONS,
    GETTING_GRAPHIC,
    GIF_REQUIRED,
    ModeChoice,
    getting_graphic,
)
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    EventEnvelope,
    EventKind,
    MessageEvent,
    Outcome,
    ReferenceKind,
    SessionRoute,
)
from lets_play.presentation import default_presentation
from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS
from lets_play.storage.progression import Progression

GIF = "https://example.invalid/pond.gif"
UPLOAD = AttachmentSnapshot(101, "pond.GIF")
IMAGE = AttachmentSnapshot(102, "pond.png", "image/png")


@pytest.mark.parametrize(
    ("text", "metadata", "passes"),
    [
        (GIF, {}, True),
        (f" <{GIF}> ", {}, True),
        (f"[pond]({GIF})", {}, True),
        ("https://klipy.com/gifs/pond", {}, True),
        ("https://media.tenor.com/abc/tenor.mp4", {}, True),
        (None, {"attachments": (UPLOAD,)}, True),
        ("", {"attachments": (AttachmentSnapshot(103, "pond", "image/gif"),)}, True),
        ("  ", {"attachments": (UPLOAD,)}, True),
        ("", {}, False),
        (f"caption {GIF}", {}, False),
        (f"{GIF} {GIF}", {}, False),
        (f"{GIF} https://example.invalid", {}, False),
        (f"`{GIF}`", {}, False),
        (f"||{GIF}||", {}, False),
        ("https://example.invalid/pond.mp4", {}, False),
        ("caption", {"attachments": (UPLOAD,)}, False),
        (GIF, {"attachments": (UPLOAD,)}, False),
        (GIF, {"attachments": (IMAGE,)}, False),
        (None, {"attachments": (UPLOAD, IMAGE)}, False),
        (None, {"attachments": (IMAGE,)}, False),
        (GIF, {"sticker_ids": (103,)}, False),
        (GIF, {"is_voice_message": True}, False),
        (GIF, {"reference_kind": ReferenceKind.FORWARD}, False),
    ],
)
def test_only_one_gif_link_or_file_passes(text, metadata, passes):
    frame = EventEnvelope(
        SessionRoute(1, 2, "session"), EventKind.MESSAGE_CREATE, "s", 3, NOW, NOW, 1
    )
    result = getting_graphic(
        MessageEvent(frame, text, **metadata), ModeChoice("bout", GETTING_GRAPHIC)
    )
    assert result.outcome is (Outcome.PASS if passes else Outcome.FAIL)
    assert result.reason_code == (None if passes else GIF_REQUIRED)


def test_generated_gif_preview_is_not_an_upload():
    message = NS(
        id=123,
        author=NS(id=7, bot=False),
        content=GIF,
        created_at=NOW,
        attachments=[],
        embeds=[NS(type="gifv", url=GIF)],
        stickers=[],
        reference=None,
        flags=NS(voice=False),
        webhook_id=None,
        is_system=lambda: False,
    )
    snapshot = message_snapshot(message, SessionRoute(1, 2, "session"), NOW)
    assert getting_graphic(snapshot, ModeChoice("bout", GETTING_GRAPHIC)).outcome is Outcome.PASS


async def gif_post(runtime, route, source, actor=606, text=GIF):
    await runtime.submit(route, "event", event=event(runtime, route, source, text, actor))


async def profile(runtime, actor):
    async with runtime.store.transaction(write=False) as unit:
        return await Progression(unit).profile(actor)


@pytest.mark.asyncio
async def test_credit_on_two_and_badge_only_after_a_clean_full_bout(tmp_path, monkeypatch):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, scheduled_state = await scheduled(runtime, monkeypatch, mode=GETTING_GRAPHIC)
        runtime.clock.at = scheduled_state.offer.effective_at
        await gif_post(runtime, route, 81000)
        active = await runtime.state(route.session_id)
        assert active.mode.choice.card_id == GETTING_GRAPHIC
        assert active.mode.ends_at == runtime.clock.at + timedelta(seconds=120)
        assert render(active, kind="rules")[0].title.endswith("Getting Graphic")

        await gif_post(runtime, route, 81000)  # duplicate Gateway source
        await gif_post(runtime, route, 81001, 707)
        await gif_post(runtime, route, 81002, text=f"-# {GIF}")
        assert (await profile(runtime, 606)).games_played == 0
        assert (await profile(runtime, 707)).games_played == 0
        await gif_post(runtime, route, 81003)
        credited = await profile(runtime, 606)
        assert credited.games_played == 1 and credited.badges == ()

        for source in range(81004, 81007):
            await gif_post(runtime, route, source)
        for source in range(81007, 81011):
            await gif_post(runtime, route, source, 707)
        await gif_post(runtime, route, 81011, 707, f"caption {GIF}")
        await gif_post(runtime, route, 81011, 707, f"caption {GIF}")
        for source in range(81012, 81016):
            await gif_post(runtime, route, source, 808)
        await gif_post(runtime, route, 81017, 909)
        assert (await profile(runtime, 606)).badges == ()

        # The first receipt at expiry is judged under restored ordinary rules.
        runtime.clock.at = active.mode.ends_at
        await gif_post(runtime, route, 81016, 808, "over")
        await runtime.submit(route, "tick")
        await settle(runtime)
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == active.round_number
        assert restored.pacing.started_at == active.pacing.started_at
        assert restored.context.active_instances == scheduled_state.context.active_instances
        assert [k for _, k, _ in runtime.transport.messages].count("evolution_mode_ended") == 1
        assert runtime.transport.markers == [81011]
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("completed",)]
        assert (await profile(runtime, 606)).badges == ("mode_gif",)
        for actor in (606, 707, 808):
            assert (await profile(runtime, actor)).games_played == 1
        assert (await profile(runtime, 707)).badges == (await profile(runtime, 808)).badges == ()
        async with runtime.store.transaction(write=False) as unit:
            score = await unit.get_score(
                route.session_id, 707, round_id=active.context.position.round_id
            )
            assert (score.mode_observations, score.mode_quality, score.mode_faults) == (6, 5, 1)
            assert score.ordinary_posts == 0
        await posts(runtime, route, base=40000, actors=(606, 707, 808))
        assert (await profile(runtime, 606)).games_played == 1
        offer = await next_offer(runtime, route)
        assert all(not isinstance(o.incoming, ModeChoice) for o in offer.offer.hand.options)
        index = next(
            i for i, option in enumerate(offer.offer.hand.options) if not option.outgoing_ids
        )
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
        runtime.clock.at = (await runtime.state(route.session_id)).offer.effective_at
        await runtime.submit(route, "tick")
        await posts(runtime, route, base=50000)
        offer = await next_offer(runtime, route)
        (index,) = (
            i
            for i, option in enumerate(offer.offer.hand.options)
            if isinstance(option.incoming, ModeChoice)
        )
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
        runtime.clock.at = (await runtime.state(route.session_id)).offer.effective_at
        await gif_post(runtime, route, 81018, 909)
        assert (await profile(runtime, 909)).games_played == 0
        await gif_post(runtime, route, 81019, 909)
        assert (await profile(runtime, 909)).games_played == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("disruption", ["manager_end", "active_reboot", "scheduled_reboot"])
async def test_shortened_or_unobserved_bout_preserves_credit_without_awarding_badge(
    tmp_path, monkeypatch, disruption
):
    db = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch, include_breaking_moment=True) as runtime:
        route, saved = await scheduled(runtime, monkeypatch, mode=GETTING_GRAPHIC)
        starts = saved.offer.effective_at
        if disruption != "scheduled_reboot":
            runtime.clock.at = starts
            for source in range(81000, 81005):
                await gif_post(runtime, route, source)
            await settle(runtime)
        if disruption == "manager_end":
            assert await runtime.submit(route, "end", actor=303, authorized=True) == "ended"
            await settle(runtime)
            assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("ended",)]
            assert (await profile(runtime, 606)).games_played == 1
            assert (await profile(runtime, 606)).badges == ()
            return

    transport = RecoveryTransport()
    async with service(tmp_path, monkeypatch, transport=transport) as runtime:
        read_in = await reading(runtime, route)
        if disruption == "scheduled_reboot":
            copy = default_presentation()
            renderer = NS(copy=copy, _now=runtime.clock.now)
            notice, _ = DiscordTransport._recovery_notice(renderer, route, read_in)
            assert "Getting Graphic" in notice.description
            assert "Getting Graphic" in render(read_in, kind="rules")[0].description
        await fire(runtime, route, read_in.recovery_resume_at + timedelta(seconds=120))
        restored = await runtime.state(route.session_id)
        assert restored.mode is None and restored.round_number == 2
        assert restored.pacing.started_at == starts
        assert restored.context.active_instances == saved.context.active_instances
        assert rows(db, "SELECT outcome FROM nr_breaking_moments") == [("interrupted",)]
        assert not any(kind == "evolution_mode_ended" for _, kind, _ in transport.messages)
        earned = await profile(runtime, 606)
        assert earned.games_played == int(disruption == "active_reboot")
        assert earned.badges == ()


@pytest.mark.asyncio
async def test_schema_17_active_drawing_conclusions_and_earned_badge_survive_upgrade(
    tmp_path, monkeypatch
):
    db = tmp_path / "game.sqlite3"
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:17])
        patch.setattr(bootstrap, "SCHEMA_VERSION", 17)
        async with service(tmp_path, patch, include_breaking_moment=True) as runtime:
            route, saved = await scheduled(runtime, patch, mode=DRAWING_CONCLUSIONS)
            runtime.clock.at = saved.offer.effective_at
            for source in range(81000, 81003):
                await gif_post(runtime, route, source, text="🎨")
            await settle(runtime)
            assert rows(db, "PRAGMA user_version") == [(17,)]

    current = MIGRATIONS[-1]
    with monkeypatch.context() as patch:
        invalid = replace(current, statements=(*current.statements, "SELECT * FROM missing_table"))
        patch.setattr(bootstrap, "MIGRATIONS", (*MIGRATIONS[:-1], invalid))
        with pytest.raises(sqlite3.OperationalError, match="missing_table"):
            await bootstrap.initialize_database(db)
        assert rows(db, "PRAGMA user_version") == [(17,)]
        assert rows(db, "SELECT kind FROM nr_breaking_moments") == [(DRAWING_CONCLUSIONS,)]
        assert rows(db, "SELECT badge_key FROM nr_badges WHERE actor_id=606") == [("mode_emoji",)]

    async with service(tmp_path, monkeypatch, transport=RecoveryTransport()) as runtime:
        read_in = await reading(runtime, route)
        assert read_in.mode.choice.card_id == DRAWING_CONCLUSIONS
        await fire(runtime, route, read_in.recovery_resume_at)
        restored = await runtime.state(route.session_id)
        assert (
            restored.mode is None
            and restored.context.active_instances == saved.context.active_instances
        )
        earned = await profile(runtime, 606)
        assert earned.games_played == 1 and earned.badges == ("mode_emoji",)
        assert rows(db, "PRAGMA user_version") == [(18,)]
        assert rows(db, "PRAGMA foreign_key_check") == []
