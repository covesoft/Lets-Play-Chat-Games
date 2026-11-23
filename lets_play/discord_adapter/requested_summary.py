"""Live private views and a temporary consistent public-summary preview."""

import re

import discord

from lets_play.discord_adapter.score_ui import detailed_player_entry
from lets_play.presentation import PresentationError
from lets_play.storage.summary_store import SUMMARY_PAGE_SIZE

CONTROL = re.compile(r"nrs:([a-f0-9]{32}):(round|game):([0-9]{1,19}):([0-9]{1,19}):(n|p)\Z")
RESULTS_CONTROL = re.compile(r"nrr:([a-f0-9]{32}):(n|p):([0-9]{1,19}):([0-9]{1,7})\Z")
POST_CONTROL = re.compile(r"nrp:([a-f0-9]{32})(?::game:([0-9]{1,19}):(n|p))?\Z")
SAVED_CONTROL = re.compile(r"nrf:([a-f0-9]{32}):([0-9]{1,19}):(round|game):([0-9]{1,19}):(n|p)\Z")
PAGE_SIZE = SUMMARY_PAGE_SIZE


def add_button(view, copy, session, *, label="buttons.summary"):
    view.add_item(discord.ui.Button(label=copy.text(label), custom_id=f"nrs:{session}:round:0:0:n"))


def render(page, copy, *, snapshot=None, public=False, publication_control=None):
    automatic = bool(snapshot and snapshot.kind == "automatic")
    game_scope = page.scope == "game"
    embed = discord.Embed(colour=copy.color("summary"))
    if automatic:
        embed.title = copy.text("reports.mid_title")
        embed.description = copy.text("reports.mid_description", round=page.round_number)
    else:
        embed.title = copy.text("summary.game_title" if game_scope else "summary.round_title")
        if public and not game_scope and snapshot and snapshot.kind == "requested":
            embed.description = copy.text("summary.posted_round_description")
            embed.set_footer(text=copy.text("summary.posted_round_footer"))
            embed.timestamp = page.as_of
        else:
            as_of = (
                copy.text("summary.as_of", timestamp=int(page.as_of.timestamp()))
                if page.as_of
                else copy.text("summary.as_of_unknown")
            )
            status = copy.text(
                "summary.captured"
                if snapshot
                else "summary.ended"
                if page.lifecycle == "ended"
                else "summary.round_closed"
                if page.round_closed
                else "summary.paused"
                if page.lifecycle == "paused"
                else "summary.live"
            )
            embed.description = copy.text("summary.lead", status=status, as_of=as_of)
        round_heading = (
            "summary.round_traffic_heading_ended"
            if page.lifecycle == "ended" or page.round_closed
            else "summary.round_traffic_heading"
        )
        embed.add_field(
            name=(
                copy.text("summary.game_traffic_heading")
                if game_scope
                else copy.text(round_heading, round=page.round_number)
            ),
            value=copy.text(
                "summary.traffic", messages=page.human_messages, whispers=page.whispers
            ),
            inline=False,
        )
    if snapshot and page.scope == "round":
        # Names only here; /rules retains the complete current instructions.
        names = [copy.text(f"cards.{r.card_id}.name") for r in snapshot.rules]
        chunk = ""
        for name in names:
            line = copy.text("summary.rule", rule=name)
            if len(chunk) + len(line) + 1 > 1024:
                embed.add_field(name=copy.text("summary.rules_heading"), value=chunk, inline=False)
                chunk = ""
            chunk += ("\n" if chunk else "") + line
        if chunk:
            embed.add_field(name=copy.text("summary.rules_heading"), value=chunk, inline=False)
    for actor, score in page.players:
        embed.add_field(
            name=copy.text("results.player_heading"),
            value=detailed_player_entry(copy, actor, score),
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

    if public:
        view.add_item(
            discord.ui.Button(
                label=copy.text("buttons.summary_current"),
                custom_id=publication_control or f"nrs:{page.session_id}:round:0:0:n",
            )
        )
    else:
        button("buttons.summary_round", "round")
        button("buttons.summary_game", "game")
        if page.previous and page.players:
            button(
                "buttons.results_previous", page.scope, page.round_number, page.players[0][0], "p"
            )
        if page.following and page.players:
            button("buttons.results_next", page.scope, page.round_number, page.players[-1][0])
        if page.lifecycle == "playing":
            control = f"nrp:{page.session_id}"
            if page.scope == "game":
                direction = "p" if page.backwards else "n"
                control += f":game:{page.anchor}:{direction}"
            view.add_item(
                discord.ui.Button(label=copy.text("buttons.summary_post"), custom_id=control)
            )
    if (
        len(embed) > 6000
        or len(embed.fields) > 25
        or len(embed.title) > 256
        or len(embed.description) > 4096
        or len(embed.footer.text or "") > 2048
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
