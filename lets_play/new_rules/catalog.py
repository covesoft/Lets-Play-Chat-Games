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
            CardSpec("NR001", "Poker Face", Deck.EXPRESSION),
            CardSpec("NR002", "Moving Pictures Prohibited", Deck.EXPRESSION),
            CardSpec("NR003", "Sticker Shock", Deck.EXPRESSION),
            CardSpec("NR004", "Unattached", Deck.EXPRESSION),
            CardSpec("NR005", "Keep It Here", Deck.EXPRESSION),
            CardSpec("NR006", "Expressionless", Deck.EXPRESSION),
            CardSpec("NR007", "Inside Voice", Deck.TYPOGRAPHY, Slot.CASING),
            CardSpec("NR008", "SPEAK UP", Deck.TYPOGRAPHY, Slot.CASING),
            CardSpec("NR009", "No Accounting", Deck.TYPOGRAPHY),
            CardSpec("NR010", "Questionable Conduct", Deck.TYPOGRAPHY, Slot.TERMINAL),
            CardSpec("NR011", "Are You Sure?", Deck.TYPOGRAPHY, Slot.TERMINAL),
            CardSpec("NR012", "With Gusto", Deck.TYPOGRAPHY, Slot.TERMINAL),
            CardSpec("NR013", "No Questions", Deck.TYPOGRAPHY),
            CardSpec("NR014", "Curb The Enthusiasm", Deck.TYPOGRAPHY),
            CardSpec("NR015", "One Breath", Deck.TYPOGRAPHY),
            CardSpec("NR016", "Plain Clothes", Deck.TYPOGRAPHY),
            CardSpec("NR017", "Briefly", Deck.BREVITY),
            CardSpec("NR018", "Use Your Words", Deck.BREVITY),
            CardSpec("NR019", "One Word Will Do", Deck.BREVITY),
            CardSpec("NR020", "Room to Elaborate", Deck.BREVITY),
            CardSpec("NR021", "Small Print", Deck.BREVITY),
            CardSpec("NR022", "No Echoes", Deck.VOCABULARY),
            CardSpec("NR023", "Nothing Personal", Deck.EXPRESSION),
            CardSpec("NR024", "So Agreeable", Deck.CLAUSES, Slot.PREFIX),
            CardSpec("NR025", "No Apologies", Deck.VOCABULARY),
            CardSpec("NR026", "No Laughing Matter", Deck.VOCABULARY),
            CardSpec("NR027", "No Doubts", Deck.VOCABULARY),
            CardSpec("NR028", "No Excuses", Deck.VOCABULARY),
            CardSpec("NR029", "No Objections", Deck.VOCABULARY),
            CardSpec("NR030", "Selfless Conversation", Deck.VOCABULARY),
            CardSpec("NR031", "No Second Person", Deck.VOCABULARY),
            CardSpec("NR032", "Mind Your Language", Deck.VOCABULARY),
            CardSpec("NR033", "Please Proceed", Deck.CLAUSES, Slot.PREFIX),
            CardSpec("NR034", "Roger Wilco", Deck.CLAUSES),
            CardSpec("NR035", "The Royal We", Deck.CLAUSES),
            CardSpec("NR036", "Don’t Forget", Deck.CLAUSES),
            CardSpec("NR037", "Decree Mode", Deck.TYPOGRAPHY, Slot.CASING),
            CardSpec("NR038", "A Small Pause", Deck.TYPOGRAPHY),
            CardSpec("NR039", "Little Words", Deck.BREVITY),
            CardSpec("NR040", "Pass It On", Deck.EXPRESSION),
        )
    }
)
