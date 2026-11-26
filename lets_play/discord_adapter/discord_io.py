"""SDK translation, capability checks and bounded ordinary-preview presentation."""

import asyncio
import re
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands

from lets_play.discord_adapter import requested_summary, round_report_ui
from lets_play.discord_adapter.lane import LaneUnavailable
from lets_play.discord_adapter.ordinary_ui import (
    add_active_rules,
    choice_detail,
    render_ordinary,
    resumption_rules,
    scheduled_mode,
)
from lets_play.discord_adapter.score_ui import detailed_player_entry
from lets_play.new_rules.breaking_moments import DRAWING_CONCLUSIONS
from lets_play.new_rules.contracts import (
    AttachmentSnapshot,
    EmojiIdentity,
    EventEnvelope,
    EventKind,
    ForwardedMessageSnapshot,
    MessageEvent,
    ReactionEvent,
    ReferenceKind,
)
from lets_play.presentation import PresentationError, default_presentation
from lets_play.storage.discord_store import DiscordStore
from lets_play.storage.game_codec import IncompatibleState
from lets_play.storage.game_store import StoreOverloaded
from lets_play.storage.guild_phrases import GuildPhraseBank
from lets_play.storage.opening_flow import OpeningCapabilities, OpeningFlow, OpeningStatus
from lets_play.storage.recovery_flow import RECOVERY_READ_IN_SECONDS
from lets_play.storage.round_reports import RoundReports
from lets_play.storage.summary_store import SummaryStore

# Command identifiers are stable protocol names, independent of sent-copy locale.
COMMAND_START = "start-game"
COMMAND_NEW_RULES = "new-rules"
COMMAND_RULES = "rules"
COMMAND_END = "end-game"
COMMAND_SUPPORT_SERVER = "support-server"
CONTROL = re.compile(
    r"nr:([a-f0-9]{32}):(join|pick|target|revive|results)(?::([0-9]{1,7}))?(?::([0-9]{1,7}))?\Z"
)

PUBLICATION_CONTROL = re.compile(
    r"nrq:([a-f0-9]{32}):(?:score:(summary|round):([1-9][0-9]{0,18})|"
    r"recovery:([a-f0-9]{32}))\Z"
)

TERMINAL_CONTROL = re.compile(r"nrt:([a-f0-9]{32}):(opening|evolution):([1-9][0-9]{0,18})\Z")


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
    forwarded = None
    if message.reference:
        target = message.reference.message_id
        if message.reference.type is discord.MessageReferenceType.forward:
            reference = ReferenceKind.FORWARD
            if len(message.message_snapshots) == 1:
                item = message.message_snapshots[0]
                extra_media = item.stickers or item.embeds or item.components
                forwarded = ForwardedMessageSnapshot(
                    item.content,
                    bool(item.attachments or extra_media or item.flags.voice),
                    is_voice_memo=(
                        item.flags.voice and len(item.attachments) == 1 and not extra_media
                    ),
                )
        elif message.type is discord.MessageType.thread_starter_message:
            reference = ReferenceKind.THREAD_STARTER
        elif (
            message.type is discord.MessageType.reply
            and message.reference.type is discord.MessageReferenceType.default
        ):
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
        forwarded_message=forwarded,
        is_native_poll=(
            getattr(message, "poll", None) is not None
            or getattr(message, "type", None) is discord.MessageType.poll_result
        ),
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


def button(
    view,
    label,
    session,
    action,
    index=None,
    target_index=None,
    *,
    style=discord.ButtonStyle.secondary,
):
    suffix = f":{index}" if index is not None else ""
    if target_index is not None:
        suffix += f":{target_index}"
    view.add_item(
        discord.ui.Button(label=label, custom_id=f"nr:{session}:{action}{suffix}", style=style)
    )


def phase_recovery_link(state, route, copy):
    if state.admission.stage == "opening_published":
        key, deadline = "opening", state.admission.choice_deadline.expires_at
    elif state.admission.stage == "lobby_published":
        key, deadline = "lobby", state.admission.lobby_deadline
    else:
        key, deadline = "revival", state.revival.deadline
    return copy.text(
        f"reboot.{key}_link",
        url=f"https://discord.com/channels/{route.guild_id}/{route.channel_id}/{state.phase_control_id}",
        deadline=copy.text("ordinary.deadline", timestamp=int(deadline.timestamp())),
    )


_RECOVERY_COUNTDOWN_KEYS = frozenset({"reboot.notice_countdown", "mode.interrupted_countdown"})


def _recovery_notice_key(state):
    if state.mode:
        return "mode.interrupted_countdown"
    if state.context is None:
        return (
            "reboot.opening_activation" if state.admission.stage == "selected" else "reboot.preplay"
        )
    if state.revival and state.revival.stage == "published":
        return "reboot.revival"
    if mode := scheduled_mode(state):
        return (
            "mode.scheduled_notice"
            if mode.card_id == DRAWING_CONCLUSIONS
            else "mode.scheduled_named_notice"
        )
    if state.offer and state.offer.stage == "selected" and state.offer_display_ready:
        return "reboot.activation_description"
    return "reboot.notice_countdown"


def render(state, *, kind=None, page=0, presentation=None, now=None):
    copy = presentation or default_presentation()
    a = state.admission
    if state.context is not None and a.stage != "ended":
        embed, view = render_ordinary(state, copy, button, kind=kind, now=now)
        if not any(
            (match := CONTROL.fullmatch(item.custom_id or "")) and match[2] in {"pick", "target"}
            for item in view.children
        ):
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
            embed.add_field(
                name=copy.text("results.player_heading"),
                value=detailed_player_entry(copy, actor, score),
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
        embed.description = copy.text("reboot.blocked_description")
        if state.recovery_resume_at:
            embed.colour = copy.color("reboot")
            embed.title = copy.text("reboot.notice_title")
            embed.description = copy.text(
                "reboot.opening_activation_reading"
                if a.stage == "selected"
                else "reboot.preplay_reading",
                timestamp=int(state.recovery_resume_at.timestamp()),
            )
            if a.stage == "selected" and state.selected_card:
                card = state.selected_card
                embed.add_field(
                    name=copy.text(f"cards.{card}.name"),
                    value=copy.text(f"cards.{card}.description"),
                    inline=False,
                )
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

    def _now(self):
        runtime = getattr(self.bot, "game_runtime", None)
        return runtime.clock.now() if runtime else datetime.now(UTC)

    async def publish_identified(self, route, output, state):
        if output.choice and state.panel_id is None:
            return await self.publish(
                route, output.ui_kind, state, publication_id=output.output_id, new_choice=True
            )
        return await self.publish(route, output.ui_kind, state, publication_id=output.output_id)

    @staticmethod
    def _publication_reference(route, family, kind, output_id):
        return f"New Rules · {route.session_id}:{family}:{kind}:{output_id}"

    async def publish(self, route, kind, state, *, publication_id=None, new_choice=False):
        channel = await self.channel(route)
        embed, view = render(state, kind=kind, presentation=self.copy, now=self._now())
        if publication_id is not None:
            if kind == "ordinary_offer":
                embed.set_footer(
                    text=self._publication_reference(route, "ordinary", "offer", publication_id)
                )
            elif kind == "evolution_mode_ended":
                view.add_item(
                    discord.ui.Button(
                        label=self.copy.text("buttons.rules_restored"),
                        custom_id=self._mode_end_control(route, publication_id),
                        disabled=True,
                    )
                )
            elif kind in {"closed", "evolution_closed_silent"}:
                family = "opening" if kind == "closed" else "evolution"
                view.add_item(
                    discord.ui.Button(
                        label=self.copy.text("buttons.final_results"),
                        custom_id=self._terminal_control(route, family, publication_id),
                    )
                )
        # A lobby expiry edits its invitation instead of leaving active Play controls.
        if (
            kind == "evolution_closed_silent" or kind == "ordinary_offer" and not new_choice
        ) and state.panel_id is None:
            raise LaneUnavailable("Panel publication has no known message to edit.")
        if kind == "evolution_closed_silent" or (
            state.panel_id is not None
            and (kind == "ordinary_offer" or (kind == "closed" and state.context is None))
        ):
            await channel.get_partial_message(state.panel_id).edit(
                embed=embed,
                view=view,
                content=None,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return state.panel_id
        reference_kind = {
            "pacing_warning": ("pacing", "warning"),
            "pacing_revival": ("pacing", "revival"),
            "activation": ("opening", "activation"),
            "ordinary_activation": ("ordinary", "activation"),
            "evolution_held": ("evolution", "held"),
        }.get(kind)
        if publication_id is not None and reference_kind is not None:
            embed.set_footer(
                text=self._publication_reference(route, *reference_kind, publication_id)
            )
        message = await channel.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        return message.id

    @staticmethod
    def _terminal_control(route, family, output_id):
        return f"nrt:{route.session_id}:{family}:{output_id}"

    @staticmethod
    def _mode_end_control(route, output_id):
        return f"nrm:{route.session_id}:end:{output_id}"

    async def find_choice_edit(self, route, output_id, target_id, attempted_at):
        reference = self._publication_reference(route, "ordinary", "offer", output_id)
        return await self._find_exact_edit(
            route,
            target_id,
            attempted_at,
            lambda message: len(message.embeds) == 1 and message.embeds[0].footer.text == reference,
        )

    async def find_choice_post(self, route, output_id, attempted_at):
        return await self._find_referenced_post(route, "ordinary", "offer", output_id, attempted_at)

    async def find_mode_end_publication(self, route, output_id, attempted_at):
        return await self._find_control_post(
            route,
            self._mode_end_control(route, output_id),
            attempted_at,
            legacy_footer=self._publication_reference(route, "evolution", "mode_ended", output_id),
        )

    async def find_terminal_publication(self, route, output, target_id, attempted_at):
        control = self._terminal_control(route, output.family, output.output_id)
        if target_id is None:
            return await self._find_control_post(route, control, attempted_at)
        return await self._find_exact_edit(
            route, target_id, attempted_at, lambda message: self._has_control(message, control)
        )

    async def _find_exact_edit(self, route, target_id, attempted_at, matches_output):
        channel = await self.channel(route)
        try:
            message = await channel.fetch_message(target_id)
        except discord.NotFound:
            return None
        if (
            message.id == target_id
            and message.author.id == self.bot.user.id
            and message.webhook_id is None
            and message.channel.id == route.channel_id
            and message.edited_at is not None
            and message.created_at <= attempted_at <= message.edited_at
            and message.created_at < message.edited_at
            and matches_output(message)
        ):
            return message.id, message.edited_at
        return None

    @staticmethod
    def _has_control(message, control):
        return any(
            isinstance(component, discord.Button) and component.custom_id == control
            for row in message.components
            for component in row.children
        )

    async def find_opening_publication(self, route, kind, attempted_at):
        # These controls identify a unique new post. An edit has no provable
        # creation timestamp for the publication it replaced.
        prefix = f"nr:{route.session_id}:"
        expected = {
            "lobby": {prefix + "join"},
            "offer": {prefix + f"pick:{i}" for i in range(3)},
        }[kind]

        def matches_controls(message):
            controls = [
                component.custom_id
                for row in message.components
                for component in row.children
                if isinstance(component, discord.Button)
            ]
            return len(controls) == len(expected) and set(controls) == expected

        return await self._find_original_post(route, attempted_at, matches_controls)

    async def find_pacing_publication(self, route, kind, output_id, attempted_at):
        return await self._find_referenced_post(route, "pacing", kind, output_id, attempted_at)

    async def find_activation_publication(self, route, family, output_id, attempted_at):
        return await self._find_referenced_post(
            route, family, "activation", output_id, attempted_at
        )

    async def find_held_publication(self, route, output_id, attempted_at):
        return await self._find_referenced_post(route, "evolution", "held", output_id, attempted_at)

    async def find_recovery_publication(self, route, recovery_id, attempted_at):
        return await self._find_control_post(
            route,
            f"nrq:{route.session_id}:recovery:{recovery_id}",
            attempted_at,
            legacy_footer=self._publication_reference(route, "recovery", "notice", recovery_id),
        )

    async def find_score_publication(self, route, family, output_id, attempted_at):
        return await self._find_control_post(
            route, f"nrq:{route.session_id}:score:{family}:{output_id}", attempted_at
        )

    async def _find_control_post(self, route, control, attempted_at, *, legacy_footer=None):
        def matches(message):
            return self._has_control(message, control) or bool(
                legacy_footer
                and len(message.embeds) == 1
                and message.embeds[0].footer.text == legacy_footer
            )

        return await self._find_original_post(route, attempted_at, matches)

    async def _find_referenced_post(self, route, family, kind, output_id, attempted_at):
        reference = self._publication_reference(route, family, kind, output_id)

        def matches_reference(message):
            return len(message.embeds) == 1 and message.embeds[0].footer.text == reference

        return await self._find_original_post(route, attempted_at, matches_reference)

    async def _find_original_post(self, route, attempted_at, matches_output):
        channel = await self.channel(route)
        match = None
        limit = 50
        count = 0
        async for message in channel.history(limit=limit, after=attempted_at, oldest_first=True):
            count += 1
            if (
                message.author.id != self.bot.user.id
                or message.webhook_id is not None
                or message.edited_at is not None
                or message.created_at < attempted_at
                or message.channel.id != route.channel_id
                or not matches_output(message)
            ):
                continue
            if match is not None:
                return None
            match = message.id, message.created_at
        return match if count < limit else None

    async def recovery_capabilities(self, route, panel_id):
        channel = await self.channel(route)
        facts = capabilities(channel, channel.guild.me, marker_verified=True)
        if not facts.available or self.marker.id is None or panel_id is None:
            raise LaneUnavailable("Recovery requires essential channel and marker access.")
        panel = channel.get_partial_message(panel_id)
        await panel.add_reaction(self.marker)
        await panel.remove_reaction(self.marker, self.bot.user)
        return facts

    def _recovery_notice(self, route, state):
        embed, view = render(state, kind="rules", presentation=self.copy)
        embed.title = self.copy.text("reboot.notice_title")
        embed.clear_fields()
        key = _recovery_notice_key(state)
        if state.context is None:
            if state.admission.stage == "selected" and state.selected_card:
                card = state.selected_card
                embed.add_field(
                    name=self.copy.text(f"cards.{card}.name"),
                    value=self.copy.text(f"cards.{card}.description"),
                    inline=False,
                )
        else:
            add_active_rules(embed, self.copy, resumption_rules(state))
        if key in _RECOVERY_COUNTDOWN_KEYS:
            deadline = state.recovery_resume_at or self._now() + timedelta(
                seconds=RECOVERY_READ_IN_SECONDS
            )
            embed.description = self.copy.text(key, timestamp=int(deadline.timestamp()))
        elif key == "mode.scheduled_named_notice":
            mode = scheduled_mode(state)
            embed.description = self.copy.text(
                key,
                seconds=RECOVERY_READ_IN_SECONDS,
                name=self.copy.text(f"cards.{mode.card_id}.name"),
            )
        else:
            embed.description = self.copy.text(key, seconds=RECOVERY_READ_IN_SECONDS)
        if state.offer_panel_id:
            embed.description += "\n\n" + choice_detail(state, self.copy)
        elif state.phase_control_id:
            embed.description += "\n\n" + phase_recovery_link(state, route, self.copy)
        embed.colour = self.copy.color("reboot")
        embed.set_footer(text=self.copy.text("reboot.recovery_footer"))
        view.add_item(
            discord.ui.Button(
                label=self.copy.text("buttons.game_status"),
                custom_id=f"nrq:{route.session_id}:recovery:{state.recovery_id}",
            )
        )
        return embed, view

    async def publish_recovery(self, route, state):
        channel = await self.channel(route)
        embed, view = self._recovery_notice(route, state)
        message = await channel.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        return message.id

    async def refresh_recovery_countdown(self, route, state):
        if (
            not state.recovery_resume_at
            or not state.recovery_notice_id
            or _recovery_notice_key(state) not in _RECOVERY_COUNTDOWN_KEYS
        ):
            return
        try:
            async with asyncio.timeout(5):
                channel = await self.channel(route)
                embed, view = self._recovery_notice(route, state)
                await channel.get_partial_message(state.recovery_notice_id).edit(
                    embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
                )
        except (discord.HTTPException, TimeoutError):
            # The notice already exists. A cosmetic correction cannot restart read-in.
            pass

    async def publish_summary(self, route, snapshot, page):
        embed, view = requested_summary.render(
            page,
            self.copy,
            snapshot=snapshot,
            public=True,
            publication_control=f"nrq:{route.session_id}:score:summary:{snapshot.summary_id}",
        )
        channel = await self.channel(route)
        message = await channel.send(
            embed=embed, view=view, allowed_mentions=discord.AllowedMentions.none()
        )
        return message.id

    async def publish_round(self, route, report, page):
        embed, view = round_report_ui.render(route.session_id, report.round_number, page, self.copy)
        view.add_item(
            discord.ui.Button(
                label=self.copy.text("buttons.round_continues"),
                custom_id=f"nrq:{route.session_id}:score:round:{report.round_number}",
                disabled=True,
            )
        )
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
        if not state.offer_display_ready and not state.recovery_id:
            # Only the publisher may reveal an unacknowledged choice/activation.
            # Its actual edit/send ACK establishes the authoritative message ID.
            return
        if (
            state.pacing
            and state.pacing.stage == "selected"
            and not state.offer
            and not state.recovery_id
        ):
            # The old countdown remains until the publisher edits it or posts
            # the claimed choice further down the channel.
            return
        signature = (
            state.panel_id,
            state.recovery_notice_id,
            state.admission.stage,
            state.recovery_id,
            state.context.position if state.context else None,
            state.offer.selected_incoming if state.offer else None,
            (state.revival.stage, state.revival.joined) if state.revival else None,
            state.pacing.selection_due_at if state.pacing else None,
        )
        if self.refreshed.get(route.session_id) == signature or state.panel_id is None:
            return
        channel = await self.channel(route)
        embed, view = render(state, presentation=self.copy)
        await channel.get_partial_message(state.panel_id).edit(embed=embed, view=view)
        if (
            state.recovery_notice_id
            and state.recovery_notice_id != state.panel_id
            and state.recovery_control_id
            and not state.recovery_id
        ):
            notice, notice_view = render(state, kind="rules", presentation=self.copy)
            if state.offer_panel_id:
                notice.description = choice_detail(state, self.copy, resumed=True)
            else:
                notice.description = phase_recovery_link(state, route, self.copy)
                # Lobby, opening and revival buttons belong to the saved panel.
                notice_view.clear_items()
            await channel.get_partial_message(state.recovery_notice_id).edit(
                embed=notice, view=notice_view, allowed_mentions=discord.AllowedMentions.none()
            )
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
        if bot.settings.support_url:
            bot.tree.add_command(
                app_commands.Command(
                    name=COMMAND_SUPPORT_SERVER,
                    description=self.copy.text("commands.support_server.description"),
                    callback=self.support_server,
                )
            )
        bot.tree.add_command(
            app_commands.Command(
                name="wordlist",
                description="Manage guild custom wordlist for Don't Forget",
                callback=self.wordlist,
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
            replies = {
                OpeningStatus.ADMITTED: self.copy.text("start.admitted"),
                OpeningStatus.CHANNEL_BUSY: self.copy.text("start.channel_busy"),
                OpeningStatus.CAPACITY: self.copy.text("start.capacity"),
            }
            async with self.runtime.store.transaction(write=False) as unit:
                gated = await OpeningFlow(unit).admission_gate(
                    interaction.guild_id,
                    interaction.channel_id,
                    now=self.runtime.clock.now(),
                    source_id=str(interaction.id),
                )
            if gated:
                await interaction.followup.send(
                    replies.get(gated, self.copy.text("start.failed")), ephemeral=True
                )
                return
            async with self.runtime.network_slots, asyncio.timeout(20):
                facts = await self.runtime.transport.probe(interaction.channel)
            _, result = await self.runtime.start(
                interaction.guild_id,
                interaction.channel_id,
                interaction.user.id,
                str(interaction.id),
                facts,
            )
            await interaction.followup.send(
                replies.get(result, self.copy.text("start.failed")),
                ephemeral=True,
            )
        except (LaneUnavailable, discord.HTTPException, TimeoutError, StoreOverloaded):
            await interaction.followup.send(
                self.copy.text("start.access_failed"),
                ephemeral=True,
            )

    async def rules(self, interaction: discord.Interaction):
        await self._show_rules(interaction, self.route(interaction))

    @app_commands.checks.has_permissions(manage_guild=True)
    async def wordlist(self, interaction: discord.Interaction):
        if not interaction.guild_id:
            return await interaction.response.send_message(
                "This command can only be used in a server.", ephemeral=True
            )
        async with self.runtime.store.transaction() as unit:
            phrases = await GuildPhraseBank(unit).seed(interaction.guild_id)
        from lets_play.discord_adapter.wordlist_ui import WordlistConfigLayoutView

        view = WordlistConfigLayoutView(self.bot, interaction.guild_id, phrases)
        await interaction.response.send_message(view=view, ephemeral=True)

    async def support_server(self, interaction: discord.Interaction):
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label=self.copy.text("buttons.join_support_server"),
                url=self.bot.settings.support_url,
            )
        )
        await interaction.response.send_message(
            self.copy.text("support_server.message"), view=view, ephemeral=True
        )

    async def _show_rules(self, interaction, route):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self.runtime.state(route.session_id) if route else None
        except (IncompatibleState, StoreOverloaded):
            await interaction.followup.send(
                self.copy.text("reboot.blocked_description"), ephemeral=True
            )
            return
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
            anchor = int(post[2] or 0)
            if not 0 <= anchor < 2**63:
                return None
            return self.runtime.submit(
                route,
                "summary_request",
                actor=interaction.user.id,
                source=str(interaction.id),
                scope="game" if post[2] is not None else "round",
                anchor=anchor,
                backwards=post[3] == "p",
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
        if (interaction.data or {}).get("custom_id", "").startswith("nrx:"):
            # Retired diagnostic controls disclose no saved internals.
            editing = await requested_summary.begin_private(interaction)
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("reboot.retired_status")
            )
            return
        terminal = TERMINAL_CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if terminal:
            ranked = requested_summary.RESULTS_CONTROL.fullmatch(f"nrr:{terminal[1]}:n:0:0")
            await self.ranked_results(interaction, ranked)
            return
        publication = PUBLICATION_CONTROL.fullmatch((interaction.data or {}).get("custom_id", ""))
        if publication:
            session, _, _, recovery_id = publication.groups()
            if recovery_id:
                # The status button is read-only and cannot act on a replacement game.
                route = self.route(interaction)
                if route and route.session_id == session and not interaction.user.bot:
                    await self._show_rules(interaction, route)
            else:
                current = requested_summary.CONTROL.fullmatch(f"nrs:{session}:round:0:0:n")
                await self.private_summary(interaction, current)
            return
        leaderboard = round_report_ui.CONTROL.fullmatch(
            (interaction.data or {}).get("custom_id", "")
        )
        if leaderboard:
            await self.round_leaderboard(interaction, leaderboard)
            return
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
            # Older public messages survive in Discord history. Their retired
            # saved-page controls now open current scores, never archived rows.
            session, summary_id, _, actor, _ = saved.groups()
            invalid = not (0 < int(summary_id) < 2**63 and 0 <= int(actor) < 2**63)
            current = requested_summary.CONTROL.fullmatch(
                f"nrs:{session}:round:{2**63 if invalid else 0}:0:n"
            )
            await self.private_summary(interaction, current)
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
                        "joined": self.copy.text(
                            "control.continued" if action == "revive" else "control.joined"
                        ),
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
                    "no_new_posts": "summary.post_no_new_posts",
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
                        page = await SummaryStore(unit).live_page(
                            match[1],
                            scope="game" if match[2] is not None else "round",
                            actor=int(match[2] or 0),
                            backwards=match[3] == "p",
                        )
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

    async def round_leaderboard(self, interaction, match):
        editing = await requested_summary.begin_private(interaction)
        session, number, direction, anchor = match.groups()
        page = None
        if self.runtime and self.runtime.ready and interaction.guild and not interaction.user.bot:
            try:
                async with self.runtime.store.transaction(write=False) as unit:
                    route = await DiscordStore(unit).route(session)
                    if route and (route.guild_id, route.channel_id) == (
                        interaction.guild_id,
                        interaction.channel_id,
                    ):
                        page = await RoundReports(unit).page(
                            session, int(number), anchor=int(anchor), backwards=direction == "p"
                        )
            except StoreOverloaded:
                pass
        if page is not None:
            try:
                embed, view = round_report_ui.render(session, int(number), page, self.copy)
            except PresentationError:
                page = None
        if page is None:
            await requested_summary.reply_private(
                interaction, editing, content=self.copy.text("reports.unavailable")
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
