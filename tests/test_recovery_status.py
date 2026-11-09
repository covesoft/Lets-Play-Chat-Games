"""Read-only WAL recovery facts, every outbound family and private SDK controls."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
from test_discord_ordinary_loop import settle
from test_discord_runtime import activated, event, opened, service
from test_live_feedback import interaction
from test_opening import Lane
from test_ordinary_flow import NOW, ROUTE, incoming, published, scene
from test_pacing import seed
from test_recovery import approve, begin, notice, rec

from lets_play.bot import LetsPlayBot
from lets_play.config import load_settings
from lets_play.discord_adapter import recovery_ui
from lets_play.new_rules.contracts import EventKind
from lets_play.presentation import default_presentation
from lets_play.storage.game_store import open_game_store
from lets_play.storage.offer_codec import utc_int
from lets_play.storage.recovery_status import PAGE_SIZE, RecoveryStatusStore


def snapshot(path):
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        return tuple(conn.iterdump())


async def status(s, **kwargs):
    async with s.store.transaction(write=False) as unit:
        return await RecoveryStatusStore(unit).read(
            s.route if hasattr(s, "route") else ROUTE, **kwargs
        )


@pytest.mark.asyncio
async def test_original_lobby_clock_and_generation_scope_are_read_only_even_after_expiry(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = Lane(store)
        await s.admit()
        await s.act("publish", EventKind.LOBBY_PUBLISHED, 5, "lobby", message_id=1001)
        await begin(s, 100)
        before = snapshot(path)
        first = await status(s)
        assert dict(first.clocks)["lobby_due"] == NOW + timedelta(seconds=185)
        assert dict(first.phases)["original_stage"] == "lobby_published"
        for family in range(11):
            assert await status(s, generation=first.generation, family=family)
        # A status read never processes overdue timers or reserves a receipt.
        assert snapshot(path) == before
        for route in (
            replace(s.route, guild_id=999),
            replace(s.route, channel_id=999),
            replace(s.route, session_id="another"),
        ):
            async with store.transaction(write=False) as unit:
                assert await RecoveryStatusStore(unit).read(route) is None
        assert await status(s, generation="old") is None
        assert snapshot(path) == before
        await begin(s, 110, "r2")
        assert await status(s, generation=first.generation) is None
        assert (await status(s)).generation != first.generation


@pytest.mark.asyncio
async def test_partial_choice_deadline_and_known_read_in_survive_status_without_dispatch(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await scene(store)
        await published(s)
        await s.act("choose", EventKind.CHOICE_SUBMIT, 10, "offer", incoming(s), actor=303)
        await begin(s, 11)
        await approve(s, 12)
        await notice(s, 13)
        before = snapshot(path)
        saved = await status(s)
        clocks = dict(saved.clocks)
        assert saved.stage == "reading"
        assert clocks["choice_due"] == NOW + timedelta(seconds=65)
        assert clocks["read_in"] == NOW + timedelta(seconds=43)
        assert dict(saved.phases)["advancement"] == "choosing"
        assert "resume_unavailable" in saved.blockers
        assert "capabilities_unknown" in saved.blockers
        assert snapshot(path) == before


@pytest.mark.asyncio
async def test_current_engine_work_can_be_unknown_found_or_proven_unsent(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        await begin(s, 1)
        unknown = await status(s, family=2)
        output = unknown.outputs[0]
        assert output.evidence == "unknown" and unknown.unresolved == ("ordinary",)
        await rec(
            s,
            "reconcile",
            EventKind.RECOVERY_RECONCILE,
            2,
            "r1",
            "ordinary",
            output.cursor,
            message_id=1001,
            published_at=NOW + timedelta(seconds=1),
        )
        known = await status(s, family=2)
        assert known.outputs[0].evidence == "found"
        assert known.outputs[0].published_at == NOW + timedelta(seconds=1)
        assert not known.unresolved
        # An inconsistent proof is never presented as healthy.
        async with store.transaction() as unit:
            await unit._conn.execute("UPDATE nr_recovery_work SET message_id=1002")
        assert (await status(s, family=2)).outputs[0].evidence == "unknown"


@pytest.mark.asyncio
async def test_engine_work_pages_beyond_first_hundred_and_never_leaks_other_routes(tmp_path):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = Lane(store)
        await s.admit()
        await begin(s, 1)
        other = Lane(store, session="other", guild=999, channel=888)
        await other.admit()
        await begin(other, 1)
        async with store.transaction() as unit:
            for n in range(1, 108):
                await unit._conn.execute(
                    "INSERT INTO nr_recovery_work VALUES ('session','r1','ordinary',?,'offer',?,?,?)",
                    (n, "unsent" if n < 107 else "unknown", None, None),
                )
        before = snapshot(path)
        first = await status(s, family=2)
        assert "ordinary" in first.unresolved  # Unknown beyond the first 100 rows.
        assert len(first.outputs) == PAGE_SIZE and first.following and not first.previous
        all_ids = []
        current = first
        while True:
            all_ids.extend(o.cursor for o in current.outputs)
            if not current.following:
                break
            current = await status(s, family=2, anchor=current.outputs[-1].cursor)
        assert all_ids == list(range(1, 108))
        assert current.outputs[-1].evidence == "unknown"  # Missing source rows too.
        back = await status(s, family=2, anchor=current.outputs[0].cursor, backwards=True)
        assert [o.cursor for o in back.outputs] == list(range(101, 106))
        assert all(o.family == "ordinary" for o in back.outputs)
        assert snapshot(path) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "problem,blocker",
    [
        ("parser", "version"),
        ("opening", "version"),
        ("recovery", "version"),
        ("capability", "existing_pause"),
        ("mode", "mode"),
        ("legacy", "legacy"),
        ("revival", "legacy_revival"),
        ("owner", "owner_changed"),
    ],
)
async def test_unsupported_state_is_named_without_decoding_private_payloads(
    tmp_path, problem, blocker
):
    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = await seed(store)
        await begin(s)
        sql = {
            "parser": "UPDATE nr_sessions SET parser_version='future'",
            "opening": "UPDATE nr_admissions SET version='future'",
            "recovery": "UPDATE nr_recoveries SET version='future'",
            "capability": "UPDATE nr_recoveries SET original_pause='capability'",
            "mode": "UPDATE nr_sessions SET phase='mode'",
            "legacy": "UPDATE nr_admissions SET stage='legacy'",
            "revival": "UPDATE nr_recoveries SET original_pause='revival'",
            "owner": "UPDATE nr_recoveries SET owner_revision=999",
        }[problem]
        async with store.transaction() as unit:
            await unit._conn.execute(sql)
            await unit._conn.execute("UPDATE nr_sessions SET context_json='PRIVATE_STATUS_CANARY'")
        before = snapshot(path)
        saved = await status(s)
        assert blocker in saved.blockers
        assert "PRIVATE_STATUS_CANARY" not in repr(saved)
        assert snapshot(path) == before


@pytest.mark.asyncio
async def test_all_adapter_families_stay_independent_of_empty_engine_work(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.submit(route, "event", event=event(runtime, route, 999, "over"))
        await settle(runtime)
        await runtime.initialize()
        async with runtime.store.transaction() as unit:
            for family in ("opening", "ordinary", "pacing", "evolution"):
                await unit._conn.execute(
                    "INSERT INTO nr_discord_deliveries VALUES (?,?,?,'sending',?,NULL,0)",
                    (route.session_id, family, 900, utc_int(runtime.clock.now())),
                )
            await unit._conn.execute(
                "INSERT INTO nr_summary_outputs(session_id,output_key,kind,state,round_id,"
                "round_number,captured_at_utc,human_messages,whispers,round_messages,"
                "round_whispers,rules_json,attempted_at_utc) "
                "SELECT session_id,'status','requested','uncertain',round_id,1,?,0,0,0,0,"
                "'PRIVATE_STATUS_CANARY',? FROM nr_sessions",
                (utc_int(NOW), utc_int(NOW)),
            )
            # A real resolution FK, without allocating any fake scoring events.
            await unit._conn.execute(
                "INSERT INTO nr_round_resolutions SELECT session_id,round_id,1,'activation' FROM nr_rounds",
            )
            await unit._conn.execute(
                "INSERT INTO nr_round_reports SELECT session_id,round_id,'uncertain',?,"
                "NULL,NULL,0,0 FROM nr_rounds",
                (utc_int(NOW),),
            )
        before = snapshot(path)
        async with runtime.store.transaction(write=False) as unit:
            reader = RecoveryStatusStore(unit)
            view = await reader.read(route)
            assert set(view.unresolved) == {
                "adapter_opening",
                "adapter_ordinary",
                "adapter_pacing",
                "adapter_evolution",
                "summary",
                "round",
            }
            for family in (5, 6, 7, 8, 9, 10):
                page = await reader.read(route, family=family)
                assert any(o.evidence == "unknown" for o in page.outputs)
                assert "PRIVATE_STATUS_CANARY" not in repr(page)
        assert snapshot(path) == before


@pytest.mark.asyncio
async def test_rules_status_is_private_in_place_and_allocates_no_receipt(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction()
            before = snapshot(path)
            await bot.game_adapter.rules(request)
            payload = request.followup.send.call_args.kwargs
            button = next(
                b
                for b in payload["view"].children
                if b.label == bot.presentation.text("buttons.recovery_status")
            )
            assert payload["ephemeral"] and not payload["allowed_mentions"].everyone
            runtime.submit = lambda *a, **kw: pytest.fail("Status must not allocate a receipt")
            request.message.flags = NS(ephemeral=True)
            request.edit_original_response = AsyncMock()
            request.followup.send.reset_mock()
            request.data = {"custom_id": button.custom_id}
            request.type = discord.InteractionType.component
            assert bot.game_adapter.capture_control(request) is None
            await bot.game_adapter.component(request)
            request.followup.send.assert_not_awaited()
            menu = request.edit_original_response.call_args.kwargs
            assert "saved facts" in menu["embed"].description
            selector = next(c for c in menu["view"].children if hasattr(c, "options"))
            assert len(selector.options) == 10
            request.data = {"custom_id": selector.custom_id, "values": ["5"]}
            await bot.game_adapter.component(request)
            assert (
                "delivery receipts"
                in request.edit_original_response.call_args.kwargs["embed"].fields[0].name
            )
            assert snapshot(path) == before
            assert len(runtime.transport.messages) == 3
            bot.game_runtime = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "nonmanager",
        "revoked",
        "bot",
        "guild",
        "channel",
        "generation",
        "ended",
        "cursor",
        "selector",
    ],
)
async def test_status_controls_reauthorize_and_refuse_stale_or_wrong_scope(
    tmp_path, monkeypatch, invalid
):
    path = tmp_path / "game.sqlite3"
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await runtime.initialize()
        async with runtime.store.transaction(write=False) as unit:
            saved = await RecoveryStatusStore(unit).read(route)
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(manager=invalid != "nonmanager")
            request.message.flags = NS(ephemeral=True)
            request.edit_original_response = AsyncMock()
            request.data = {"custom_id": recovery_ui.control(saved)}
            if invalid == "revoked":
                await bot.game_adapter.component(request)
                request.permissions.manage_guild = False
            elif invalid == "bot":
                request.user.bot = True
            elif invalid == "guild":
                request.guild_id = 999
            elif invalid == "channel":
                request.channel_id = 999
            elif invalid == "generation":
                await runtime.initialize()
            elif invalid == "ended":
                await runtime.submit(route, "end", actor=303, authorized=True)
                await settle(runtime)
            elif invalid == "cursor":
                request.data = {"custom_id": recovery_ui.control(saved, 5, 2**63)}
            elif invalid == "selector":
                request.data = {
                    "custom_id": recovery_ui.control(saved, direction="s"),
                    "values": [[]],
                }
            before = snapshot(path)
            await bot.game_adapter.component(request)
            kwargs = request.edit_original_response.call_args.kwargs
            assert kwargs.get("embed") is None and kwargs.get("view") is None
            assert snapshot(path) == before
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_manager_rules_can_inspect_unsupported_saved_version_and_copy_is_editable(
    tmp_path, monkeypatch
):
    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        async with runtime.store.transaction() as unit:
            await unit._conn.execute("UPDATE nr_sessions SET parser_version='future'")
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction()
            await bot.game_adapter.rules(request)
            embed = request.followup.send.call_args.kwargs["embed"]
            assert "saved version" in str(embed.to_dict())
            assert embed.colour.value == bot.presentation.color("recovery")
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_status_payload_limits_and_invalid_cursors(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = Lane(store, session="f" * 32)
        await s.admit()
        await begin(s)
        saved = await status(s)
        for family in range(11):
            page = await status(s, family=family)
            embed, view = recovery_ui.render(page, default_presentation())
            assert len(embed) <= 6000 and len(embed.fields) <= 25
            assert all(len(c.custom_id) <= 100 for c in view.children)
            assert all(len(f.value) <= 1024 for f in embed.fields)
        assert len(recovery_ui.control(saved, 10, 2**63 - 1, "p")) <= 100
        for kwargs in (
            {"family": True},
            {"family": 11},
            {"anchor": -1},
            {"anchor": 2**63},
            {"backwards": 1},
        ):
            with pytest.raises(ValueError):
                await status(s, **kwargs)


@pytest.mark.asyncio
async def test_real_never_sent_engine_proof_does_not_clear_an_adapter_send_claim(tmp_path):
    async with open_game_store(tmp_path / "game.sqlite3") as store:
        s = await scene(store)
        await s.act("prepare_offer", EventKind.OFFER_PREPARE, 0, s.hand)
        await begin(s, 1)
        output = (await status(s, family=2)).outputs[0]
        await rec(s, "reconcile", EventKind.RECOVERY_RECONCILE, 2, "r1", "ordinary", output.cursor)
        async with store.transaction() as unit:
            await unit._conn.execute(
                "INSERT INTO nr_discord_deliveries VALUES ('session','ordinary',?,'sending',?,NULL,0)",
                (output.cursor, utc_int(NOW)),
            )
        saved = await status(s, family=2)
        assert saved.outputs[0].evidence == "unsent"
        assert saved.unresolved == ("adapter_ordinary",)


@pytest.mark.asyncio
@pytest.mark.parametrize("manager,recovering", [(False, True), (True, False)])
async def test_rules_entry_is_limited_to_managers_with_an_active_recovery(
    tmp_path, monkeypatch, manager, recovering
):
    async with service(tmp_path, monkeypatch) as runtime:
        await opened(runtime)
        if recovering:
            await runtime.initialize()
        bot = LetsPlayBot(load_settings(base_dir=tmp_path, environ={}))
        async with bot:
            bot.game_runtime = runtime
            request = interaction(manager=manager)
            await bot.game_adapter.rules(request)
            payload = request.followup.send.call_args.kwargs
            assert not any(c.custom_id.startswith("nrx:") for c in payload["view"].children)
            bot.game_runtime = None


@pytest.mark.asyncio
async def test_prepared_summary_and_ack_without_publication_time_remain_distinct(
    tmp_path, monkeypatch
):
    from lets_play.storage.summary_store import SummaryStore

    async with service(tmp_path, monkeypatch) as runtime:
        route = await opened(runtime)
        await activated(runtime, route)
        await runtime.initialize()
        # Build a retained prepared capture using the same storage API; no send.
        async with runtime.store.transaction() as unit:
            # Summary prepare is appropriately fenced during recovery, so retain
            # a pre-disruption capture fixture using its durable schema instead.
            await unit._conn.execute(
                "INSERT INTO nr_summary_outputs(session_id,output_key,kind,state,round_id,"
                "round_number,captured_at_utc,human_messages,whispers,round_messages,"
                "round_whispers,rules_json) SELECT session_id,'prepared','requested',"
                "'prepared',round_id,1,?,0,0,0,0,'[]' FROM nr_sessions",
                (utc_int(NOW),),
            )
        async with runtime.store.transaction(write=False) as unit:
            assert await SummaryStore(unit).pending(route.session_id)
            reader = RecoveryStatusStore(unit)
            saved = await reader.read(route, family=6)
            assert saved.outputs[0].evidence == "prepared"
            assert "summary" not in saved.unresolved
            ack = await reader.read(route, family=5)
            assert ack.outputs[0].evidence == "acknowledged"
            assert ack.outputs[0].message_id and ack.outputs[0].published_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["recovery.select", "recovery.family.opening"])
async def test_owner_override_component_limits_fail_privately_without_state_changes(tmp_path, key):
    import json

    from lets_play.presentation import PresentationError, load_presentation

    path = tmp_path / "game.sqlite3"
    async with open_game_store(path) as store:
        s = Lane(store, session="f" * 32)
        await s.admit()
        await begin(s)
        override = tmp_path / "copy.json"
        override.write_text(json.dumps({"strings": {key: "x" * 151}}))
        before = snapshot(path)
        with pytest.raises(PresentationError):
            recovery_ui.render(await status(s), load_presentation(override))
        assert snapshot(path) == before
