"""Compact, exact-ranked round leaderboards; no copied historical scores."""

import re

import discord

from lets_play.presentation import PresentationError

CONTROL = re.compile(r"nrl:([a-f0-9]{32}):([0-9]{1,19}):(n|p):([0-9]{1,19})\Z")


def render(session, round_number, page, copy):
    players, previous, following = page
    embed = discord.Embed(
        title=copy.text("reports.round_title"),
        description=copy.text("reports.round_description", round=round_number),
        colour=copy.color("round_results"),
    )
    for actor, score in players:
        adjusted = score.ranking_percent()
        embed.add_field(
            name=copy.text("reports.unrated")
            if adjusted is None
            else copy.text("reports.score", percent=float(adjusted)),
            value=copy.text("results.player", actor=actor),
            inline=False,
        )
    if not players:
        embed.add_field(
            name=copy.text("summary.empty_heading"), value=copy.text("summary.empty"), inline=False
        )
    view = discord.ui.View(timeout=None)
    if previous and players:
        view.add_item(
            discord.ui.Button(
                label=copy.text("buttons.results_previous"),
                custom_id=f"nrl:{session}:{round_number}:p:{players[0][0]}",
            )
        )
    if following and players:
        view.add_item(
            discord.ui.Button(
                label=copy.text("buttons.results_next"),
                custom_id=f"nrl:{session}:{round_number}:n:{players[-1][0]}",
            )
        )
    if (
        len(embed) > 6000
        or len(embed.title) > 256
        or len(embed.description) > 4096
        or any(len(f.name) > 256 or len(f.value) > 1024 for f in embed.fields)
    ):
        raise PresentationError("Round report exceeds Discord limits.")
    return embed, view
