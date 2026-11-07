"""SDK translation, capability checks and bounded opening-preview presentation."""

import asyncio
import re

import discord
from discord import app_commands

from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    MessageEvent,
    ReactionEvent,
    ReferenceKind,
)
from lets_play.storage.discord_store import DiscordStore
from lets_play.storage.opening_flow import OpeningCapabilities, OpeningStatus

CARD_COPY = {
    "NR004": "No file attachments. Link previews are not attachments.",
    "NR006": "No reactions. Each human can incur one fault per target message for this game.",
    "NR007": "Use no uppercase letters.",
    "NR011": "End text with exactly three periods: ...",
    "NR034": "End text with the word over. Punctuation may follow it.",
}
CONTROL = re.compile(r"nr:([a-f0-9]{32}):(join|pick|results)(?::([0-9]{1,7}))?\Z")


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


def button(view, label, session, action, index=None):
    suffix = f":{index}" if index is not None else ""
    view.add_item(discord.ui.Button(label=label, custom_id=f"nr:{session}:{action}{suffix}"))


def render(state, *, kind=None, page=0):
    a = state.admission
    session = a.session_id
    view = discord.ui.View(timeout=None)
    embed = discord.Embed(title="New Rules", colour=0x8665C7)
    if kind == "closed" or a.stage == "ended":
        embed.title = "New Rules — game over"
        embed.description = "The game has ended. Earned progress has been applied."
        for actor, score in state.scores:
            actual = (
                "Unrated" if score.actual_percent is None else f"{float(score.actual_percent):.2f}%"
            )
            ranking = (
                "Unrated"
                if score.ranking_percent() is None
                else f"{float(score.ranking_percent()):.2f}"
            )
            embed.add_field(
                name=f"Player {actor}",
                value=(
                    f"Actual compliance: {actual} · adjusted score: {ranking}\n"
                    f"Faults: {score.raw_faults} · opportunities: {score.opportunities}\n"
                    f"Ordinary posts: {score.ordinary_posts} · reaction faults: {score.reaction_failures}"
                ),
                inline=False,
            )
        if not state.scores:
            embed.description = "The game has ended. No scored participation was recorded."
        if page:
            button(view, "Previous results", session, "results", page - 1)
        if state.more_scores:
            button(view, "More results", session, "results", page + 1)
        embed.set_footer(text=f"Results page {page + 1} · players ordered by ID")
    elif state.recovery_id or a.stage == "recovery":
        embed.title = "New Rules — paused"
        embed.description = (
            "Enforcement is paused after an interruption. Scores and earned progress are preserved. "
            "A server manager can use /end-game to close this game."
        )
    elif kind == "lobby" or a.stage.startswith("lobby"):
        embed.description = (
            "Chat under unusual rules! The starter has joined; two more people must press Play.\n\n"
            "Once play begins, rules apply to everyone posting in this exact channel. "
            "Start a message with `-# ` to whisper: the whole message is exempt.\n\n"
            "The invitation lasts three minutes. Use /rules for current rules. "
            "A manager can end the game with /end-game.\n\n"
            "**Opening preview:** one rule remains until a manager ends the game."
        )
        button(view, "Play", session, "join")
    elif kind == "offer" or a.stage.startswith("opening"):
        embed.title = "New Rules — choose the first rule"
        embed.description = (
            f"<@{a.picker_id}> chooses one card. The choice lasts 60 seconds from publication."
        )
        for index, option in enumerate(a.hand.options):
            card = option.incoming.card_id
            embed.add_field(name=CARD_SPECS[card].name, value=CARD_COPY[card], inline=False)
            button(view, CARD_SPECS[card].name, session, "pick", index)
    else:
        selected = a.stage == "selected"
        embed.title = (
            "New Rules — first rule scheduled" if selected else "New Rules — current rules"
        )
        embed.description = (
            f"The rule takes effect <t:{int(a.effective_at.timestamp())}:T>. No rule applies before then."
            if selected
            else "Round 1 · Everyone posting here participates. Start with `-# ` to whisper."
        )
        if selected:
            # The selected incoming ID is persisted separately; runtime supplies the
            # actual rule via context only after activation. Resolve through state.
            cards = (state.selected_card,) if state.selected_card else ()
        else:
            cards = (
                tuple(i.card_id for i in state.context.active_instances) if state.context else ()
            )
        for card in cards:
            embed.add_field(name=CARD_SPECS[card].name, value=CARD_COPY[card], inline=False)
    return embed, view


class DiscordTransport:
    def __init__(self, bot):
        self.bot = bot
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
            "Checking New Rules reaction access…", allowed_mentions=discord.AllowedMentions.none()
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
        embed, view = render(state, kind=kind)
        # A lobby expiry edits its invitation instead of leaving active Play controls.
        if kind == "closed" and state.context is None and state.panel_id:
            await channel.get_partial_message(state.panel_id).edit(
                embed=embed, view=view, content=None
            )
            return state.panel_id
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
        signature = (state.panel_id, state.admission.stage, state.recovery_id)
        if self.refreshed.get(route.session_id) == signature or state.panel_id is None:
            return
        channel = await self.channel(route)
        embed, view = render(state)
        await channel.get_partial_message(state.panel_id).edit(embed=embed, view=view)
        self.refreshed[route.session_id] = signature

    async def paused(self, route, state):
        if state is None or state.admission.stage == "ended":
            return
        channel = await self.channel(route)
        embed, view = render(state)
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
            name="start-game", description="Start a chat game", guild_only=True
        )
        start.add_command(
            app_commands.Command(
                name="new-rules", description="Open a New Rules lobby", callback=self.start
            )
        )
        bot.tree.add_command(start)
        bot.tree.add_command(
            app_commands.Command(
                name="rules", description="View New Rules and the current game", callback=self.rules
            )
        )
        bot.tree.add_command(
            app_commands.Command(
                name="end-game",
                description="End this channel's game (server managers)",
                callback=self.end,
            )
        )

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
                "The opening preview is available only in development mode.", ephemeral=True
            )
            return
        if (
            not interaction.guild
            or interaction.user.bot
            or not self.runtime
            or not self.runtime.ready
        ):
            await interaction.followup.send(
                "Start this game in an available server channel.", ephemeral=True
            )
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
                OpeningStatus.ADMITTED: "The public invitation is being posted.",
                OpeningStatus.CHANNEL_BUSY: "This channel already has a game.",
                OpeningStatus.CAPACITY: "This server has reached its simultaneous-game limit.",
            }
            await interaction.followup.send(
                replies.get(result, "A complete playable lobby could not be opened."),
                ephemeral=True,
            )
        except (LaneUnavailable, discord.HTTPException, TimeoutError):
            await interaction.followup.send(
                "I could not verify channel and animated-marker access. Check my permissions, then try again.",
                ephemeral=True,
            )

    async def rules(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        route = self.route(interaction)
        state = await self.runtime.state(route.session_id) if route else None
        if state:
            embed, view = render(state)
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(
                "New Rules is a chat game: remember the current rules while you talk. "
                "Start with /start-game new-rules; three humans are needed. "
                "Once active, everyone posting in the exact game channel participates. "
                "Start a message with `-# ` to whisper.",
                ephemeral=True,
            )

    def capture_control(self, interaction):
        """Called synchronously at SDK dispatch, before HTTP acknowledgement/tasks."""
        route = self.route(interaction)
        if not route or interaction.user.bot:
            return None
        if interaction.type is discord.InteractionType.application_command:
            if (
                interaction.data.get("name") != "end-game"
                or not interaction.permissions.manage_guild
            ):
                return None
            return self.runtime.submit(
                route, "end", actor=interaction.user.id, source=str(interaction.id), authorized=True
            ), False
        match = CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if not match or interaction.message is None:
            return None
        session, action, number = match.groups()
        if session != route.session_id or action not in {"join", "pick"}:
            return None
        facts = capabilities(interaction.channel, interaction.guild.me, marker_verified=True)
        unavailable = not facts.available
        future = self.runtime.submit(
            route,
            "pause" if unavailable else ("join" if action == "join" else "choose"),
            actor=interaction.user.id,
            source=str(interaction.id),
            message_id=interaction.message.id,
            capabilities=facts,
            index=int(number) if number else 0,
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
            await interaction.followup.send(
                "A server manager must use this in the game's channel.", ephemeral=True
            )
            return
        try:
            await future
            await interaction.followup.send(
                "The game is closed; final results are being posted.", ephemeral=True
            )
        except LaneUnavailable:
            await interaction.followup.send(
                "The game worker is unavailable; restart the bot to recover manager controls.",
                ephemeral=True,
            )

    async def component(self, interaction):
        match = CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if not match:
            return
        session, action, number = match.groups()
        route = self.route(interaction)
        captured = getattr(interaction, "extras", {}).pop("new_rules_receipt", None)
        future, unavailable = captured if captured else (None, False)
        if (
            future is None
            and route
            and route.session_id == session
            and action in {"join", "pick"}
            and not interaction.user.bot
            and interaction.message is not None
        ):
            facts = capabilities(interaction.channel, interaction.guild.me, marker_verified=True)
            unavailable = not facts.available
            future = self.runtime.submit(
                route,
                "pause" if unavailable else ("join" if action == "join" else "choose"),
                actor=interaction.user.id,
                source=str(interaction.id),
                message_id=interaction.message.id,
                capabilities=facts,
                index=int(number) if number else 0,
            )
        await interaction.response.defer(ephemeral=True, thinking=True)
        if (
            not self.runtime
            or not self.runtime.ready
            or not interaction.guild
            or interaction.user.bot
        ):
            await interaction.followup.send("This game is unavailable.", ephemeral=True)
            return
        if future is not None:
            try:
                result = await future
                if unavailable:
                    await self.runtime._show_pause(route)
                    reply = "Essential channel access is unavailable; enforcement is paused."
                else:
                    replies = {
                        "joined": "You're in!",
                        "opening": "Three players joined; the first picker is being announced.",
                        "selected": "Choice saved; the start time is being announced.",
                        "unauthorized": "Only the named picker can choose.",
                        "paused": "Enforcement is paused.",
                    }
                    reply = replies.get(
                        str(result), "That control is no longer available or was already used."
                    )
                await interaction.followup.send(reply, ephemeral=True)
            except LaneUnavailable:
                await interaction.followup.send(
                    "This game is paused. A manager can use /end-game.", ephemeral=True
                )
            return
        # Read-only result paging and expired controls need no lane allocation.
        async with self.runtime.store.transaction(write=False) as unit:
            saved = await DiscordStore(unit).route(session)
        if saved is None or (saved.guild_id, saved.channel_id) != (
            interaction.guild_id,
            interaction.channel_id,
        ):
            await interaction.followup.send(
                "This control belongs to a different or expired game.", ephemeral=True
            )
            return
        if action == "results" and number is not None:
            state = await self.runtime.state(session, page=int(number))
            if state.admission.stage != "ended":
                await interaction.followup.send(
                    "Final results are available after the game ends.", ephemeral=True
                )
                return
            embed, view = render(state, kind="closed", page=int(number))
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.followup.send(
                "That game has ended or its controls have expired.", ephemeral=True
            )
