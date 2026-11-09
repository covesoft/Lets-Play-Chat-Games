"""Presentation only for later ordinary phases; state/legality stay in the engine."""

import discord


def rule_text(copy, instance):
    if instance.card_id == "NR036":
        return copy.text("cards.NR036.description", phrase=dict(instance.parameters)["phrase"])
    return copy.text(f"cards.{instance.card_id}.description")


def render_ordinary(state, copy, button, *, kind=None):
    p = state.context.position
    session = p.route.session_id
    offer, pacing, revival = state.offer, state.pacing, state.revival
    embed = discord.Embed(title=copy.text("active.title"), colour=copy.color("active"))
    view = discord.ui.View(timeout=None)
    rules_only = kind == "rules"

    def rules(instances):
        for instance in instances:
            embed.add_field(
                name=copy.text(f"cards.{instance.card_id}.name"),
                value=rule_text(copy, instance),
                inline=False,
            )

    if state.recovery_id or (p.lifecycle == "paused" and p.pause_reason != "revival"):
        embed.title = copy.text("paused.title")
        embed.colour = copy.color("paused")
        if state.recovery_resume_at:
            embed.title = copy.text("reboot.notice_title")
            embed.colour = copy.color("reboot")
            embed.description = copy.text(
                "reboot.reading_description", timestamp=int(state.recovery_resume_at.timestamp())
            )
        else:
            embed.description = (
                copy.text("reboot.blocked_description")
                if state.recovery_id
                else copy.text("paused.description")
            )
        rules(state.context.active_instances)
    elif p.lifecycle == "paused" and p.pause_reason == "revival":
        embed.title = copy.text("revival.title")
        embed.colour = copy.color("revival")
        embed.description = copy.text(
            "revival.description", joined=revival.joined if revival else 0
        )
        if not rules_only:
            button(view, copy.text("buttons.revive"), session, "revive")
        else:
            rules(state.context.active_instances)
    elif offer and offer.stage in {"prepared", "published"} and not rules_only:
        embed.title = copy.text("ordinary.offer_title")
        embed.colour = copy.color("offer")
        clock = (
            copy.text(
                "ordinary.deadline", timestamp=int(offer.hand.deadline.expires_at.timestamp())
            )
            if offer.published_at
            else copy.text("ordinary.clock_pending")
        )
        embed.description = copy.text(
            "ordinary.offer_description", picker=offer.hand.picker_id, deadline=clock
        )
        chosen = offer.selected_incoming
        active = {i.instance_id: i for i in state.context.active_instances}
        for index, option in enumerate(offer.hand.options):
            if chosen and option.incoming.instance_id != chosen:
                continue
            card = option.incoming.card_id
            label = copy.text(f"cards.{card}.name")
            description = rule_text(copy, option.incoming)
            if option.outgoing_ids:
                targets = ", ".join(
                    copy.text(f"cards.{active[t].card_id}.name") for t in option.outgoing_ids
                )
                description = copy.text(
                    "ordinary.replacement", description=description, targets=targets
                )
            embed.add_field(name=label, value=description, inline=False)
            if chosen:
                for target_index, target in enumerate(option.outgoing_ids):
                    button(
                        view,
                        copy.text(f"cards.{active[target].card_id}.name"),
                        session,
                        "target",
                        index,
                        target_index,
                    )
            else:
                button(view, label, session, "pick", index)
        if chosen:
            embed.description = copy.text(
                "ordinary.target_description", picker=offer.hand.picker_id, deadline=clock
            )
    elif offer and offer.stage == "selected" and kind != "rules":
        embed.title = copy.text("ordinary.scheduled_title")
        embed.colour = copy.color("scheduled")
        embed.description = copy.text(
            "ordinary.scheduled_description", timestamp=int(offer.effective_at.timestamp())
        )
        option = next(
            o for o in offer.hand.options if o.incoming.instance_id == offer.selected_incoming
        )
        result = next(
            c.instances for c in option.completions if c.outgoing_id == offer.selected_outgoing
        )
        rules(result)
    else:
        embed.description = copy.text("round.description", round=state.round_number)
        if p.advancement == "held":
            embed.title = copy.text("held.title")
            embed.colour = copy.color("held")
            embed.description = copy.text("held.description")
        elif pacing and pacing.stage in {"warning_queued", "warning_published"}:
            embed.title = copy.text("warning.title")
            embed.colour = copy.color("warning")
            embed.description = copy.text("warning.description")
        elif offer and offer.stage == "selected":
            embed.description = copy.text(
                "ordinary.current_until", timestamp=int(offer.effective_at.timestamp())
            )
        elif offer:
            embed.description = copy.text(
                "ordinary.current_during_choice", picker=offer.hand.picker_id
            )
        rules(state.context.active_instances)
    return embed, view
