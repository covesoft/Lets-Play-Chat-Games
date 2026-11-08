"""Private live score pages. These controls do not publish or ACK summaries."""

import re

import discord

from lets_play.presentation import PresentationError

CONTROL = re.compile(r"nrs:([a-f0-9]{32}):(round|game):([0-9]{1,19}):([0-9]{1,19}):(n|p)\Z")
RESULTS_CONTROL = re.compile(r"nrr:([a-f0-9]{32}):(n|p):([0-9]{1,19}):([0-9]{1,7})\Z")
PAGE_SIZE = 5


def add_button(view, copy, session):
    view.add_item(
        discord.ui.Button(
            label=copy.text("buttons.summary"), custom_id=f"nrs:{session}:round:0:0:n"
        )
    )


def render(page, copy):
    embed = discord.Embed(title=copy.text("summary.title"), colour=copy.color("summary"))
    as_of = (
        copy.text("summary.as_of", timestamp=int(page.as_of.timestamp()))
        if page.as_of
        else copy.text("summary.as_of_unknown")
    )
    embed.description = copy.text(
        "summary.description",
        round=page.round_number,
        messages=page.human_messages,
        whispers=page.whispers,
        as_of=as_of,
        status=copy.text(
            "summary.ended"
            if page.lifecycle == "ended"
            else "summary.round_closed"
            if page.round_closed
            else "summary.paused"
            if page.lifecycle == "paused"
            else "summary.live"
        ),
    )
    embed.add_field(
        name=copy.text("summary.scope_round" if page.scope == "round" else "summary.scope_game"),
        value=copy.text("summary.order"),
        inline=False,
    )
    for actor, score in page.players:
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
    if not page.players:
        embed.add_field(
            name=copy.text("summary.empty_heading"), value=copy.text("summary.empty"), inline=False
        )
    view = discord.ui.View(timeout=None)

    def button(label, scope, round_number=0, actor=0, direction="n"):
        view.add_item(
            discord.ui.Button(
                label=copy.text(label),
                custom_id=f"nrs:{page.session_id}:{scope}:{round_number}:{actor}:{direction}",
            )
        )

    button("buttons.summary_round", "round")
    button("buttons.summary_game", "game")
    if page.previous and page.players:
        button("buttons.results_previous", page.scope, page.round_number, page.players[0][0], "p")
    if page.following and page.players:
        button("buttons.results_next", page.scope, page.round_number, page.players[-1][0])
    if (
        len(embed) > 6000
        or len(embed.title) > 256
        or len(embed.description) > 4096
        or any(len(f.name) > 256 or len(f.value) > 1024 for f in embed.fields)
    ):
        raise PresentationError("Requested summary presentation exceeds Discord limits.")
    return embed, view


async def begin_private(interaction):
    editing = bool(getattr(getattr(interaction.message, "flags", None), "ephemeral", False))
    await interaction.response.defer(ephemeral=True, thinking=not editing)
    return editing


async def reply_private(interaction, editing, *, content=None, embed=None, view=None):
    mentions = discord.AllowedMentions.none()
    if editing:
        await interaction.edit_original_response(
            content=content, embed=embed, view=view, allowed_mentions=mentions
        )
    else:
        payload = {"allowed_mentions": mentions, "ephemeral": True}
        if content is not None:
            payload["content"] = content
        if embed is not None:
            payload["embed"] = embed
        if view is not None:
            payload["view"] = view
        await interaction.followup.send(**payload)
