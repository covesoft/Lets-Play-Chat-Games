"""Pure ordinary pacing and exact worst-result comparison, independent of storage."""

from dataclasses import dataclass
from fractions import Fraction

from lets_play.new_rules.scoring import ScoreTotals

PACING_VERSION = "ordinary-pacing-1"
MIN_WARNING_SECONDS = 270
FALLBACK_WARNING_SECONDS = 870
WARNING_SECONDS = 30
MIN_ACTIVITY_POSTS = 5
WARNING_TEXT = "Get ready! Another Rule is coming! Everyone who chats has a chance to choose."


def message_target(estimated_players: int) -> int:
    if type(estimated_players) is not int or not 0 <= estimated_players <= ((1 << 63) - 1) // 5:
        raise ValueError("The frozen player estimate must be a nonnegative bounded integer.")
    return max(15, 5 * estimated_players)


@dataclass(slots=True)
class WorstResults:
    """Streaming summary; unmeasured candidates count toward a nonempty survivor pool."""

    eligible: int = 0
    worst: Fraction | None = None
    worst_count: int = 0
    any_faults: bool = False

    def observe(self, score: ScoreTotals) -> None:
        if score.ordinary_posts < MIN_ACTIVITY_POSTS:
            raise ValueError("Only activity-qualified candidates enter the snapshot.")
        self.eligible += 1
        self.any_faults |= bool(score.raw_faults)
        actual = score.actual_percent
        if actual is not None:
            if self.worst is None or actual < self.worst:
                self.worst, self.worst_count = actual, 1
            elif actual == self.worst:
                self.worst_count += 1

    @property
    def exclude_worst(self) -> bool:
        return self.any_faults and self.worst is not None and self.worst_count < self.eligible
