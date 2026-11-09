"""Private manager status, with bounded output pages and no mutation controls."""

import re

import discord

from lets_play.presentation import PresentationError
from lets_play.storage.recovery_status import RecoveryStatusStore

CONTROL = re.compile(r"nrx:([a-f0-9]{32}):([a-f0-9]{24}):(0|[1-9]|10):([0-9]{1,19}):(n|p|s)\Z")


def control(status, family=0, anchor=0, direction="n"):
    return f"nrx:{status.session_id}:{status.generation}:{family}:{anchor}:{direction}"


def add_button(view, copy, status):
    view.add_item(
        discord.ui.Button(label=copy.text("buttons.recovery_status"), custom_id=control(status))
    )


def _state(copy, value):
    key = f"recovery.state.{value}" if value is not None else "recovery.none"
    return copy.text(key if key in copy.strings else "recovery.unknown")


def render(status, copy):
    embed = discord.Embed(title=copy.text("recovery.title"), colour=copy.color("recovery"))
    embed.description = copy.text(
        "recovery.description", generation=status.generation[:8], stage=_state(copy, status.stage)
    )
    if status.family == 0:
        embed.add_field(
            name=copy.text("recovery.phase_heading"),
            value="\n".join(
                copy.text(
                    "recovery.fact",
                    label=copy.text(f"recovery.phase.{key}"),
                    value=_state(copy, value),
                )
                for key, value in status.phases
            ),
            inline=False,
        )
        embed.add_field(
            name=copy.text("recovery.clock_heading"),
            value="\n".join(
                copy.text(
                    "recovery.fact",
                    label=copy.text(f"recovery.clock.{key}"),
                    value=(
                        copy.text("recovery.timestamp", timestamp=int(value.timestamp()))
                        if value
                        else copy.text("recovery.unknown")
                    ),
                )
                for key, value in status.clocks
            ),
            inline=False,
        )
        embed.add_field(
            name=copy.text("recovery.blocker_heading"),
            value="\n".join(copy.text(f"recovery.blocker.{key}") for key in status.blockers),
            inline=False,
        )
        embed.add_field(
            name=copy.text("recovery.output_heading"),
            value=(
                "\n".join(copy.text(f"recovery.family.{family}") for family in status.unresolved)
                if status.unresolved
                else copy.text("recovery.no_unknown")
            ),
            inline=False,
        )
    else:
        family = RecoveryStatusStore.family_name(status.family)
        embed.add_field(
            name=copy.text(f"recovery.family.{family}"),
            value=copy.text("recovery.output_scope"),
            inline=False,
        )
        for output in status.outputs:
            # IDs and actual timestamps only. Neither a message ID nor an ACK
            # sequence alone is presented as actual publication-time proof.
            embed.add_field(
                name=copy.text("recovery.output", number=output.cursor),
                value=copy.text(
                    "recovery.output_fact",
                    evidence=copy.text(f"recovery.evidence.{output.evidence}"),
                    message=str(output.message_id)
                    if output.message_id
                    else copy.text("recovery.unknown"),
                    published=(
                        copy.text(
                            "recovery.timestamp", timestamp=int(output.published_at.timestamp())
                        )
                        if output.published_at
                        else copy.text("recovery.unknown")
                    ),
                ),
                inline=False,
            )
        if not status.outputs:
            embed.add_field(
                name=copy.text("recovery.output_heading"),
                value=copy.text("recovery.empty"),
                inline=False,
            )
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(label=copy.text("buttons.recovery_overview"), custom_id=control(status))
    )
    selector = discord.ui.Select(
        custom_id=control(status, direction="s"),
        placeholder=copy.text("recovery.select"),
        options=[
            discord.SelectOption(
                label=copy.text(f"recovery.family.{RecoveryStatusStore.family_name(i)}"),
                value=str(i),
                default=i == status.family,
            )
            for i in range(1, 11)
        ],
    )
    view.add_item(selector)
    if status.previous and status.outputs:
        view.add_item(
            discord.ui.Button(
                label=copy.text("buttons.recovery_previous"),
                custom_id=control(status, status.family, status.outputs[0].cursor, "p"),
            )
        )
    if status.following and status.outputs:
        view.add_item(
            discord.ui.Button(
                label=copy.text("buttons.recovery_next"),
                custom_id=control(status, status.family, status.outputs[-1].cursor),
            )
        )
    if (
        len(embed) > 6000
        or len(embed.fields) > 25
        or len(embed.title) > 256
        or len(embed.description) > 4096
        or any(len(f.name) > 256 or len(f.value) > 1024 for f in embed.fields)
        or len(selector.placeholder) > 150
        or any(len(option.label) > 100 for option in selector.options)
    ):
        raise PresentationError("Recovery status presentation exceeds Discord limits.")
    return embed, view
