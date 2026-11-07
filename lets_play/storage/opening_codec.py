"""Versioned opening configuration snapshots; never serialize received messages."""

import json

from lets_play.new_rules.catalog import CARD_SPECS
from lets_play.new_rules.evaluation import EVALUATOR_VERSION
from lets_play.new_rules.hands import PhraseChoice
from lets_play.new_rules.parsing import PARSER_VERSION
from lets_play.storage.game_codec import IncompatibleState


def encode_config(phrases, templates):
    if not isinstance(templates, frozenset) or not templates <= CARD_SPECS.keys():
        raise ValueError("Opening availability must contain implemented ordinary templates.")
    if (
        not isinstance(phrases, tuple)
        or len(phrases) > 15
        or not all(isinstance(p, PhraseChoice) for p in phrases)
    ):
        raise ValueError("Opening configuration needs at most 15 ordered phrase snapshots.")
    if len({p.phrase_id for p in phrases}) != len(phrases) or len({p.key for p in phrases}) != len(
        phrases
    ):
        raise ValueError("Opening phrases need unique IDs and normalized keys.")
    return json.dumps(
        {
            "format": 1,
            "parser": PARSER_VERSION,
            "evaluator": EVALUATOR_VERSION,
            "templates": sorted(templates),
            "phrases": [[p.phrase_id, p.text, p.revision] for p in phrases],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_config(value):
    try:
        data = json.loads(value)
        if (
            set(data) != {"format", "parser", "evaluator", "templates", "phrases"}
            or type(data["format"]) is not int
            or data["format"] != 1
            or data["parser"] != PARSER_VERSION
            or data["evaluator"] != EVALUATOR_VERSION
        ):
            raise ValueError
        if (
            not isinstance(data["templates"], list)
            or len(set(data["templates"])) != len(data["templates"])
            or not isinstance(data["phrases"], list)
            or len(data["phrases"]) > 15
            or any(
                not isinstance(p, list) or len(p) != 3 or not all(isinstance(v, str) for v in p)
                for p in data["phrases"]
            )
        ):
            raise ValueError
        templates = frozenset(data["templates"])
        phrases = tuple(PhraseChoice(*p) for p in data["phrases"])
        encode_config(phrases, templates)
        return phrases, templates
    except (ValueError, TypeError, KeyError, AttributeError, NotImplementedError):
        raise IncompatibleState("Opening configuration needs explicit recovery.") from None
