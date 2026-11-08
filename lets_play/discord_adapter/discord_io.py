"""SDK translation, capability checks and bounded ordinary-preview presentation."""

import asyncio
import re

import discord
from discord import app_commands

from lets_play.discord_adapter import requested_summary
from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.discord_adapter.ordinary_ui import render_ordinary
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    MessageEvent,
    ReactionEvent,
    ReferenceKind,
)
from lets_play.presentation import PresentationError, default_presentation
from lets_play.storage.discord_store import DiscordStore
from lets_play.storage.game_store import StoreOverloaded
from lets_play.storage.opening_flow import OpeningCapabilities, OpeningStatus
from lets_play.storage.summary_store import SummaryStore

# Command identifiers are stable protocol names, independent of sent-copy locale.
COMMAND_START = "start-game"
COMMAND_NEW_RULES = "new-rules"
COMMAND_RULES = "rules"
COMMAND_END = "end-game"
CONTROL = re.compile(
    r"nr:([a-f0-9]{32}):(join|pick|target|revive|results)(?::([0-9]{1,7}))?(?::([0-9]{1,7}))?\Z"
)


def capabilities(channel, member, *, marker_verified=False):
    if channel is None or member is None or not callable(getattr(channel, "send", None)):
        return OpeningCapabilities(False, False, False)
    if not callable(getattr(channel, "get_partial_message", None)):
        return OpeningCapabilities(False, False, False)
    perms = channel.permissions_for(member)
    thread = isinstance(channel, discord.Thread)
    can_send = perms.view_channel and (
        perms.send_messages_in_threads if thread else perms.send_messages
    )
    # Archived/locked surfaces are not silently reopened by starting a game.
    can_send = (
        can_send
        and not getattr(channel, "archived", False)
        and not getattr(channel, "locked", False)
    )
    return OpeningCapabilities(
        bool(can_send),
        bool(perms.embed_links),
        bool(marker_verified and perms.add_reactions and perms.read_message_history),
    )


def message_snapshot(message, route, received):
    reference = ReferenceKind.NONE
    target = None
    if message.reference:
        target = message.reference.message_id
        if message.reference.type is discord.MessageReferenceType.forward:
            reference = ReferenceKind.FORWARD
        elif message.type is discord.MessageType.thread_starter_message:
            reference = ReferenceKind.THREAD_STARTER
        elif message.type is discord.MessageType.reply:
            reference = ReferenceKind.REPLY
    frame = EventEnvelope(
        route,
        EventKind.MESSAGE_CREATE,
        str(message.id),
        message.author.id,
        message.created_at,
        received,
        0,
    )
    return MessageEvent(
        frame,
        message.content,
        tuple(
            AttachmentSnapshot(a.id, a.filename, a.content_type, a.duration)
            for a in message.attachments
        ),
        tuple(s.id for s in message.stickers),
        reference,
        target,
        message.flags.voice,
        message.author.bot,
        message.webhook_id is not None,
        message.is_system(),
    )


def reaction_snapshot(payload, route, received, *, source_id):
    # Reaction actions have no snowflake/timestamp. Never substitute the target's age.
    frame = EventEnvelope(
        route, EventKind.REACTION_ADD, source_id, payload.user_id, received, received, 0
    )
    identity = (
        EmojiIdentity(custom_id=payload.emoji.id)
        if payload.emoji.id
        else EmojiIdentity(unicode=payload.emoji.name)
    )
    return ReactionEvent(frame, payload.message_id, identity, payload.member.bot)


def button(view, label, session, action, index=None, target_index=None):
    suffix = f":{index}" if index is not None else ""
    if target_index is not None:
        suffix += f":{target_index}"
    view.add_item(discord.ui.Button(label=label, custom_id=f"nr:{session}:{action}{suffix}"))


def render(state, *, kind=None, page=0, presentation=None):
    copy = presentation or default_presentation()
    a = state.admission
    if state.context is not None and a.stage != "ended":
        embed, view = render_ordinary(state, copy, button, kind=kind)
        requested_summary.add_button(view, copy, a.session_id)
        return embed, view
    session = a.session_id
    view = discord.ui.View(timeout=None)
    embed = discord.Embed(title=copy.text("game.title"), colour=copy.color())
    if kind == "closed" or a.stage == "ended":
        embed.colour = copy.color("results")
        embed.title = copy.text("results.title")
        embed.description = copy.text("results.description")
        for actor, score in state.scores:
            actual = (
                copy.text("results.unrated")
                if score.actual_percent is None
                else copy.text("results.actual", percent=float(score.actual_percent))
            )
            ranking = (
                copy.text("results.unrated")
                if score.ranking_percent() is None
                else copy.text("results.ranking", percent=float(score.ranking_percent()))
            )
            embed.add_field(
                name=copy.text("results.player_heading"),
                value=copy.text(
                    "results.player_entry",
                    player=copy.text("results.player", actor=actor),
                    detail=copy.text(
                        "results.detail",
                        actual=actual,
                        ranking=ranking,
                        faults=score.raw_faults,
                        opportunities=score.opportunities,
                        ordinary_posts=score.ordinary_posts,
                        reaction_failures=score.reaction_failures,
                    ),
                ),
                inline=False,
            )
        if not state.scores:
            embed.description = copy.text("results.empty")
        if state.previous_scores and state.scores:
            view.add_item(
                discord.ui.Button(
                    label=copy.text("buttons.results_previous"),
                    custom_id=f"nrr:{session}:p:{state.scores[0][0]}:{max(0, page - 1)}",
                )
            )
        if state.more_scores and state.scores:
            view.add_item(
                discord.ui.Button(
                    label=copy.text("buttons.results_next"),
                    custom_id=f"nrr:{session}:n:{state.scores[-1][0]}:{page + 1}",
                )
            )
        if state.context is not None:
            embed.set_footer(text=copy.text("results.footer", page=page + 1))
    elif state.recovery_id or a.stage == "recovery":
        embed.colour = copy.color("paused")
        embed.title = copy.text("paused.title")
        embed.description = copy.text("paused.description")
    elif kind == "lobby" or a.stage.startswith("lobby"):
        embed.colour = copy.color("lobby")
        embed.description = copy.text("lobby.description")
        button(view, copy.text("buttons.play"), session, "join")
    elif kind == "offer" or a.stage.startswith("opening"):
        embed.colour = copy.color("offer")
        embed.title = copy.text("offer.title")
        embed.description = copy.text("offer.description", picker=a.picker_id)
        for index, option in enumerate(a.hand.options):
            card = option.incoming.card_id
            label = copy.text(f"cards.{card}.name")
            embed.add_field(name=label, value=copy.text(f"cards.{card}.description"), inline=False)
            button(view, label, session, "pick", index)
    else:
        selected = a.stage == "selected"
        embed.colour = copy.color("scheduled" if selected else "active")
        embed.title = copy.text("scheduled.title" if selected else "active.title")
        embed.description = (
            copy.text("scheduled.description", timestamp=int(a.effective_at.timestamp()))
            if selected
            else copy.text("active.description")
        )
        if selected:
            cards = (state.selected_card,) if state.selected_card else ()
        else:
            cards = (
                tuple(i.card_id for i in state.context.active_instances) if state.context else ()
            )
        for card in cards:
            embed.add_field(
                name=copy.text(f"cards.{card}.name"),
                value=copy.text(f"cards.{card}.description"),
                inline=False,
            )
    return embed, view


class DiscordTransport:
    def __init__(self, bot):
        self.bot = bot
        self.copy = getattr(bot, "presentation", None) or default_presentation()
        self.marker = discord.PartialEmoji.from_str(bot.settings.ding_emoji or "")
        self.refreshed = {}

    async def channel(self, route):
        channel = self.bot.get_channel(route.channel_id) or await self.bot.fetch_channel(
            route.channel_id
        )
        if getattr(getattr(channel, "guild", None), "id", None) != route.guild_id:
            raise LaneUnavailable("The exact game channel is unavailable.")
        return channel

    async def probe(self, channel):
        facts = capabilities(channel, channel.guild.me, marker_verified=True)
        if not facts.available or self.marker.id is None:
            raise LaneUnavailable(
                "This channel needs view, send, embeds, history, and reaction access with a custom marker."
            )
        probe = await channel.send(
            self.copy.text("probe.message"), allowed_mentions=discord.AllowedMentions.none()
        )
        try:
            await probe.add_reaction(self.marker)
            await probe.remove_reaction(self.marker, self.bot.user)
        finally:
            try:
                await probe.delete()
            except discord.HTTPException:
                pass
        return facts

    async def publish(self, route, kind, state):
        channel = await self.channel(route)
        embed, view = render(state, kind=kind, presentation=self.copy)
        # A lobby expiry edits its invitation instead of leaving active Play controls.
        if kind == "evolution_closed_silent" and state.panel_id is None:
            raise LaneUnavailable("Silent closure has no known panel to edit.")
        if kind == "evolution_closed_silent" or (
            kind == "closed" and state.context is None and state.panel_id
        ):
            await channel.get_partial_message(state.panel_id).edit(
                embed=embed, view=view, content=None
            )
            return state.panel_id
        message = await channel.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        return message.id

    async def publish_summary(self, route, snapshot, page):
        embed, view = requested_summary.render(page, self.copy, snapshot=snapshot, public=True)
        channel = await self.channel(route)
        message = await channel.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        return message.id

    async def mark(self, route, message_id):
        channel = await self.channel(route)
        try:
            await channel.get_partial_message(message_id).add_reaction(self.marker)
        except discord.NotFound as exc:
            if (
                exc.code != 10008
            ):  # Deleted target is local; unknown emoji is essential feedback loss.
                raise

    async def refresh(self, route, state):
        if state.admission.stage == "ended":
            self.refreshed.pop(route.session_id, None)
            return
        signature = (
            state.panel_id,
            state.admission.stage,
            state.recovery_id,
            state.context.position if state.context else None,
            state.offer.selected_incoming if state.offer else None,
            (state.revival.stage, state.revival.joined) if state.revival else None,
        )
        if self.refreshed.get(route.session_id) == signature or state.panel_id is None:
            return
        channel = await self.channel(route)
        embed, view = render(state, presentation=self.copy)
        await channel.get_partial_message(state.panel_id).edit(embed=embed, view=view)
        self.refreshed[route.session_id] = signature

    async def paused(self, route, state):
        if state is None or state.admission.stage == "ended":
            return
        channel = await self.channel(route)
        embed, view = render(state, presentation=self.copy)
        if state.panel_id:
            await channel.get_partial_message(state.panel_id).edit(embed=embed, view=view)
        else:
            # Uncertain sends have no safe replacement-send proof. /rules remains
            # available even when no acknowledged panel can be edited.
            return


class DiscordGame:
    def __init__(self, bot):
        self.bot = bot
        start = app_commands.Group(
            name=COMMAND_START,
            description=self.copy.text("commands.start.description"),
            guild_only=True,
        )
        start.add_command(
            app_commands.Command(
                name=COMMAND_NEW_RULES,
                description=self.copy.text("commands.new_rules.description"),
                callback=self.start,
            )
        )
        bot.tree.add_command(start)
        bot.tree.add_command(
            app_commands.Command(
                name=COMMAND_RULES,
                description=self.copy.text("commands.rules.description"),
                callback=self.rules,
            )
        )
        bot.tree.add_command(
            app_commands.Command(
                name=COMMAND_END,
                description=self.copy.text("commands.end.description"),
                callback=self.end,
            )
        )

    @property
    def copy(self):
        return self.bot.presentation

    @property
    def runtime(self):
        return self.bot.game_runtime

    def route(self, interaction):
        if not interaction.guild_id or not self.runtime or not self.runtime.ready:
            return None
        return self.runtime.routes.get((interaction.guild_id, interaction.channel_id))

    async def start(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not self.bot.settings.development_mode:
            await interaction.followup.send(
                self.copy.text("start.development_only"), ephemeral=True
            )
            return
        if (
            not interaction.guild
            or interaction.user.bot
            or not self.runtime
            or not self.runtime.ready
        ):
            await interaction.followup.send(self.copy.text("start.unavailable"), ephemeral=True)
            return
        try:
            async with self.runtime.network_slots, asyncio.timeout(20):
                facts = await self.runtime.transport.probe(interaction.channel)
            _, result = await self.runtime.start(
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                str(interaction.id),
                facts,
            )
            replies = {
                OpeningStatus.ADMITTED: self.copy.text("start.admitted"),
                OpeningStatus.CHANNEL_BUSY: self.copy.text("start.channel_busy"),
                OpeningStatus.CAPACITY: self.copy.text("start.capacity"),
            }
            await interaction.followup.send(
                replies.get(result, self.copy.text("start.failed")),
                ephemeral=True,
            )
        except (LaneUnavailable, discord.HTTPException, TimeoutError):
            await interaction.followup.send(
                self.copy.text("start.access_failed"),
                ephemeral=True,
            )

    async def rules(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        route = self.route(interaction)
        state = await self.runtime.state(route.session_id) if route else None
        if state:
            embed, view = render(state, kind="rules", presentation=self.copy)
            if state.context is None:
                view.clear_items()
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(
                self.copy.text("rules.explanation"),
                ephemeral=True,
            )

    def capture_control(self, interaction):
        """Called synchronously at SDK dispatch, before HTTP acknowledgement/tasks."""
        route = self.route(interaction)
        if not route or interaction.user.bot:
            return None
        post = requested_summary.POST_CONTROL.fullmatch(
            (interaction.data or {}).get("custom_id", "")
        )
        if post and post[1] == route.session_id and interaction.message is not None:
            return self.runtime.submit(
                route, "summary_request", actor=interaction.user.id, source=str(interaction.id)
            ), False
        if interaction.type is discord.InteractionType.application_command:
            if (
                interaction.data.get("name") != COMMAND_END
                or not interaction.permissions.manage_guild
            ):
                return None
            return self.runtime.submit(
                route, "end", actor=interaction.user.id, source=str(interaction.id), authorized=True
            ), False
        match = CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if not match or interaction.message is None:
            return None
        session, action, number, target = match.groups()
        if session != route.session_id or action not in {"join", "pick", "target", "revive"}:
            return None
        facts = capabilities(interaction.channel, interaction.guild.me, marker_verified=True)
        unavailable = not facts.available
        future = self.runtime.submit(
            route,
            "pause" if unavailable else ("choose" if action == "pick" else action),
            actor=interaction.user.id,
            source=str(interaction.id),
            message_id=interaction.message.id,
            capabilities=facts,
            index=int(number) if number else 0,
            target_index=int(target) if target is not None else -1,
        )
        return future, unavailable

    async def end(self, interaction: discord.Interaction):
        route = self.route(interaction)
        captured = getattr(interaction, "extras", {}).pop("new_rules_receipt", None)
        future = captured[0] if captured else None
        if (
            future is None
            and route
            and interaction.permissions.manage_guild
            and not interaction.user.bot
        ):
            # Queue before deferring: an HTTP acknowledgement must not allow a
            # later timer/message to overtake this already-received control.
            future = self.runtime.submit(
                route, "end", actor=interaction.user.id, source=str(interaction.id), authorized=True
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        if future is None:
            if (
                not interaction.guild
                or interaction.user.bot
                or not interaction.permissions.manage_guild
            ):
                key = "end.manager_required"
            elif not self.runtime or not self.runtime.ready:
                key = "control.unavailable"
            else:
                key = "end.no_game_here"
            await interaction.followup.send(self.copy.text(key), ephemeral=True)
            return
        try:
            result = await future
            key = "end.no_game_here" if result is OpeningStatus.ALREADY_DONE else "end.closed"
            await interaction.followup.send(self.copy.text(key), ephemeral=True)
        except LaneUnavailable:
            await interaction.followup.send(
                self.copy.text("end.worker_unavailable"),
                ephemeral=True,
            )

    async def component(self, interaction):
        post = requested_summary.POST_CONTROL.fullmatch(
            (interaction.data or {}).get("custom_id", "")
        )
        if post:
            await self.post_summary(interaction, post)
            return
        saved = requested_summary.SAVED_CONTROL.fullmatch(
            (interaction.data or {}).get("custom_id", "")
        )
        if saved:
            await self.saved_summary(interaction, saved)
            return
        summary = requested_summary.CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if summary:
            await self.private_summary(interaction, summary)
            return
        ranked = requested_summary.RESULTS_CONTROL.fullmatch(
            (interaction.data or {}).get("custom_id", "")
        )
        if ranked:
            await self.ranked_results(interaction, ranked)
            return
        match = CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if not match:
            return
        session, action, number, target = match.groups()
        if action == "results" and number is not None:
            # Legacy ID-offset buttons reopen the ranked first page in the same
            # private menu; new controls then use exact-score cursors.
            ranked = requested_summary.RESULTS_CONTROL.fullmatch(f"nrr:{session}:n:0:0")
            await self.ranked_results(interaction, ranked)
            return
        route = self.route(interaction)
        captured = getattr(interaction, "extras", {}).pop("new_rules_receipt", None)
        future, unavailable = captured if captured else (None, False)
        if (
            future is None
            and route
            and route.session_id == session
            and action in {"join", "pick", "target", "revive"}
            and not interaction.user.bot
            and interaction.message is not None
        ):
            facts = capabilities(interaction.channel, interaction.guild.me, marker_verified=True)
            unavailable = not facts.available
            future = self.runtime.submit(
                route,
                "pause" if unavailable else ("choose" if action == "pick" else action),
                actor=interaction.user.id,
                source=str(interaction.id),
                message_id=interaction.message.id,
                capabilities=facts,
                index=int(number) if number else 0,
                target_index=int(target) if target is not None else -1,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        if (
            not self.runtime
            or not self.runtime.ready
            or not interaction.guild
            or interaction.user.bot
        ):
            await interaction.followup.send(self.copy.text("control.unavailable"), ephemeral=True)
            return
        if future is not None:
            try:
                result = await future
                if unavailable:
                    await self.runtime._show_pause(route)
                    reply = self.copy.text("control.access_lost")
                else:
                    replies = {
                        "joined": self.copy.text("control.joined"),
                        "revived": self.copy.text("control.revived"),
                        "needs_target": self.copy.text("control.needs_target"),
                        "already_joined": self.copy.text("control.already_joined"),
                        "opening": self.copy.text("control.opening"),
                        "selected": self.copy.text("control.selected"),
                        "unauthorized": self.copy.text("control.unauthorized"),
                        "paused": self.copy.text("control.paused"),
                    }
                    reply = replies.get(str(result), self.copy.text("control.stale"))
                await interaction.followup.send(reply, ephemeral=True)
            except LaneUnavailable:
                await interaction.followup.send(self.copy.text("control.recovery"), ephemeral=True)
            return
        # Expired controls need no lane allocation.
        async with self.runtime.store.transaction(write=False) as unit:
            saved = await DiscordStore(unit).route(session)
        if saved is None or (saved.guild_id, saved.channel_id) != (
            interaction.guild_id,
            interaction.channel_id,
        ):
            await interaction.followup.send(self.copy.text("control.wrong_game"), ephemeral=True)
            return
        await interaction.followup.send(self.copy.text("control.expired"), ephemeral=True)

    async def post_summary(self, interaction, match):
        # Preserve dispatch ordering even when the HTTP defer is slow.
        captured = getattr(interaction, "extras", {}).pop("new_rules_receipt", None)
        if captured is None:
            captured = self.capture_control(interaction)
        editing = await requested_summary.begin_private(interaction)
        if (
            not self.runtime
            or not self.runtime.ready
            or not interaction.guild
            or interaction.user.bot
            or self.route(interaction) is None
            or self.route(interaction).session_id != match[1]
        ):
            reply = "summary.unavailable"
        elif captured is None:
            reply = "summary.unavailable"
        else:
            try:
                status = await asyncio.shield(captured[0])
                reply = {
                    "queued": "summary.post_queued",
                    "shared": "summary.post_shared",
                    "recent": "summary.post_recent",
                    "uncertain": "summary.post_uncertain",
                    "paused": "control.paused",
                }.get(status, "summary.unavailable")
            except LaneUnavailable:
                reply = "control.recovery"
        # Keep the menu usable and edit the originating private response.
        page = None
        if self.runtime and self.runtime.ready and interaction.guild and not interaction.user.bot:
            try:
                async with self.runtime.store.transaction(write=False) as unit:
                    route = await DiscordStore(unit).route(match[1])
                    if route and (route.guild_id, route.channel_id) == (
                        interaction.guild_id,
                        interaction.channel_id,
                    ):
                        page = await SummaryStore(unit).live_page(match[1])
            except StoreOverloaded:
                pass
        embed = view = None
        if page:
            try:
                embed, view = requested_summary.render(page, self.copy)
            except PresentationError:
                pass
        await requested_summary.reply_private(
            interaction, editing, content=self.copy.text(reply), embed=embed, view=view
        )

    async def saved_summary(self, interaction, match):
        editing = await requested_summary.begin_private(interaction)
        session, summary_id, scope, actor, direction = match.groups()
        page = snapshot = None
        if self.runtime and self.runtime.ready and interaction.guild and not interaction.user.bot:
            try:
                async with self.runtime.store.transaction(write=False) as unit:
                    route = await DiscordStore(unit).route(session)
                    if route and (route.guild_id, route.channel_id) == (
                        interaction.guild_id,
                        interaction.channel_id,
                    ):
                        summaries = SummaryStore(unit)
                        snapshot = await summaries.delivered_snapshot(session, int(summary_id))
                        if snapshot:
                            page = await summaries.captured_page(
                                snapshot,
                                scope=scope,
                                actor=int(actor),
                                backwards=direction == "p",
                                limit=requested_summary.PAGE_SIZE,
                            )
            except StoreOverloaded:
                pass
        if page:
            try:
                embed, view = requested_summary.render(page, self.copy, snapshot=snapshot)
            except PresentationError:
                page = None
        if page is None:
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("summary.unavailable")
            )
        else:
            await requested_summary.reply_private(interaction, editing, embed=embed, view=view)

    async def private_summary(self, interaction, match):
        editing = await requested_summary.begin_private(interaction)
        if (
            not self.runtime
            or not self.runtime.ready
            or not interaction.guild
            or interaction.user.bot
        ):
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("control.unavailable")
            )
            return
        session, scope, round_number, actor, direction = match.groups()
        page = None
        try:
            async with self.runtime.store.transaction(write=False) as unit:
                route = await DiscordStore(unit).route(session)
                page = None
                if route and (route.guild_id, route.channel_id) == (
                    interaction.guild_id,
                    interaction.channel_id,
                ):
                    page = await SummaryStore(unit).live_page(
                        session,
                        scope=scope,
                        round_number=int(round_number),
                        actor=int(actor),
                        backwards=direction == "p",
                        limit=requested_summary.PAGE_SIZE,
                    )
        except StoreOverloaded:
            pass
        if page is not None:
            try:
                embed, view = requested_summary.render(page, self.copy)
            except PresentationError:
                page = None
        if page is None:
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("summary.unavailable")
            )
        else:
            await requested_summary.reply_private(interaction, editing, embed=embed, view=view)

    async def ranked_results(self, interaction, match):
        editing = await requested_summary.begin_private(interaction)
        if (
            not self.runtime
            or not self.runtime.ready
            or not interaction.guild
            or interaction.user.bot
        ):
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("control.unavailable")
            )
            return
        session, direction, anchor, page = match.groups()
        try:
            async with self.runtime.store.transaction(write=False) as unit:
                route = await DiscordStore(unit).route(session)
                state = None
                if route and (route.guild_id, route.channel_id) == (
                    interaction.guild_id,
                    interaction.channel_id,
                ):
                    state = await DiscordStore(unit).state(
                        session,
                        page=int(page),
                        result_anchor=int(anchor),
                        result_backwards=direction == "p",
                    )
            if state is None or state.admission.stage != "ended":
                raise ValueError("Final results are unavailable.")
            embed, view = render(state, kind="closed", page=int(page), presentation=self.copy)
        except (ValueError, StoreOverloaded):
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("results.unavailable")
            )
            return
        await requested_summary.reply_private(interaction, editing, embed=embed, view=view)
