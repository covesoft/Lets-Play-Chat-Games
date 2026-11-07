"""Metadata for the implemented ordinary subset, not the complete game deck."""

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

# Mechanical settings; independent of premium and no total active-rule ceiling.
CHATGAMES_MAX_ACTIVE_BREVITY_RULES = 1
CHATGAMES_MAX_ACTIVE_VOCABULARY_RULES = 2
CHATGAMES_MAX_ACTIVE_DONT_FORGET = 2


class Deck(StrEnum):
    EXPRESSION = "expression"
    TYPOGRAPHY = "typography"
    BREVITY = "brevity"
    VOCABULARY = "vocabulary"
    CLAUSES = "clauses"


class Slot(StrEnum):
    CASING = "casing"
    PREFIX = "prefix"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class CardSpec:
    card_id: str
    name: str
    deck: Deck
    slot: Slot | None = None


CARD_SPECS = MappingProxyType(
    {
        item.card_id: item
        for item in (
            CardSpec("NR004", "Unattached", Deck.EXPRESSION),
            CardSpec("NR006", "Expressionless", Deck.EXPRESSION),
            CardSpec("NR007", "Inside Voice", Deck.TYPOGRAPHY, Slot.CASING),
            CardSpec("NR008", "SPEAK UP", Deck.TYPOGRAPHY, Slot.CASING),
            CardSpec("NR010", "Questionable Conduct", Deck.TYPOGRAPHY, Slot.TERMINAL),
            CardSpec("NR011", "Are You Sure?", Deck.TYPOGRAPHY, Slot.TERMINAL),
            CardSpec("NR013", "No Questions", Deck.TYPOGRAPHY),
            CardSpec("NR016", "Plain Clothes", Deck.TYPOGRAPHY),
            CardSpec("NR017", "Briefly", Deck.BREVITY),
            CardSpec("NR019", "One Word Will Do", Deck.BREVITY),
            CardSpec("NR022", "No Echoes", Deck.VOCABULARY),
            CardSpec("NR024", "So Agreeable", Deck.CLAUSES, Slot.PREFIX),
            CardSpec("NR025", "No Apologies", Deck.VOCABULARY),
            CardSpec("NR027", "Indecisiveness Banned", Deck.VOCABULARY),
            CardSpec("NR033", "Please Proceed", Deck.CLAUSES, Slot.PREFIX),
            CardSpec("NR034", "Roger Wilco", Deck.CLAUSES),
            CardSpec("NR036", "Don’t Forget", Deck.CLAUSES),
        )
    }
)
