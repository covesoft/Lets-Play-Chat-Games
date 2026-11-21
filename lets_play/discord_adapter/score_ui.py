"""Shared Discord score formatting for standings and summaries."""

from lets_play.new_rules.scoring import ScoreTotals
from lets_play.presentation import Presentation

SHOW_UNRATED_SCORE_PLACEHOLDER = False


def player_score_line(
    copy: Presentation, actor: int, totals: ScoreTotals, *, template: str = "results.player_score"
) -> str:
    player = copy.text("results.player", actor=actor)
    adjusted = totals.ranking_percent()
    if adjusted is None and not SHOW_UNRATED_SCORE_PLACEHOLDER:
        return player
    score = (
        copy.text("results.score_unrated")
        if adjusted is None
        else copy.text("results.score", points=round(10 * adjusted))
    )
    return copy.text(template, player=player, score=score)


def detailed_player_entry(copy: Presentation, actor: int, totals: ScoreTotals) -> str:
    actual = totals.actual_percent
    detail = copy.text(
        "results.score_detail",
        actual=copy.text("results.unrated")
        if actual is None
        else copy.text("results.actual", percent=float(actual)),
        faults=totals.raw_faults,
        opportunities=totals.opportunities,
        ordinary_posts=totals.ordinary_posts,
        reaction_failures=totals.reaction_failures,
    )
    return copy.text(
        "results.player_entry", player=player_score_line(copy, actor, totals), detail=detail
    )
