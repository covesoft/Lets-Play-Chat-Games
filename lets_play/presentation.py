"""Editable sent-copy catalogs, independent of rule IDs, scoring and storage.

The bundled English catalog is the fallback. Optional owner JSON overrides are
loaded once, validated before startup, and never written back to public source.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from string import Formatter
from types import MappingProxyType

MAX_CATALOG_BYTES = 128 * 1024
# Only these templates accept arguments. Attribute/index lookups are prohibited.
TEMPLATE_FIELDS = {
    "summary.description": frozenset({"round", "messages", "whispers", "as_of", "status"}),
    "summary.as_of": frozenset({"timestamp"}),
    "round.description": frozenset({"round"}),
    "ordinary.offer_description": frozenset({"picker", "deadline"}),
    "ordinary.target_description": frozenset({"picker", "deadline"}),
    "ordinary.deadline": frozenset({"timestamp"}),
    "ordinary.replacement": frozenset({"description", "targets"}),
    "ordinary.scheduled_description": frozenset({"timestamp"}),
    "ordinary.current_until": frozenset({"timestamp"}),
    "ordinary.current_during_choice": frozenset({"picker"}),
    "revival.description": frozenset({"joined"}),
    "cards.NR036.description": frozenset({"phrase"}),
    "results.actual": frozenset({"percent"}),
    "results.ranking": frozenset({"percent"}),
    "results.player": frozenset({"actor"}),
    "results.player_entry": frozenset({"player", "detail"}),
    "results.detail": frozenset(
        {"actual", "ranking", "faults", "opportunities", "ordinary_posts", "reaction_failures"}
    ),
    "results.footer": frozenset({"page"}),
    "offer.description": frozenset({"picker"}),
    "scheduled.description": frozenset({"timestamp"}),
}


class PresentationError(ValueError):
    """Key-only diagnostics; never echo private override contents or paths."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PresentationError("Duplicate presentation key.")
        result[key] = value
    return result


def _read(data):
    if len(data) > MAX_CATALOG_BYTES:
        raise PresentationError("Presentation file exceeds the size limit.")
    try:
        result = json.loads(data, object_pairs_hook=_object)
    except (ValueError, UnicodeError) as exc:
        raise PresentationError(
            "Presentation file must contain valid JSON without duplicate keys."
        ) from exc
    if not isinstance(result, dict) or result.keys() - {"locale", "strings", "colors"}:
        raise PresentationError("Presentation file must contain locale, strings and/or colors.")
    return result


def _validate_strings(strings):
    for key, text in strings.items():
        if not isinstance(text, str) or not text.strip():
            raise PresentationError(f"Presentation string must be nonempty: {key}")
        expected = TEMPLATE_FIELDS.get(key, frozenset())
        try:
            fields = list(Formatter().parse(text))
            names = {name for _, name, _, _ in fields if name is not None}
            if names != expected or any(
                name is not None
                and (not name.isidentifier() or conversion or "{" in spec or "}" in spec)
                for _, name, spec, conversion in fields
            ):
                raise ValueError
            # Catch inappropriate format specs now, before a send can fail.
            text.format(
                **{
                    name: (
                        "sample"
                        if name
                        in {
                            "as_of",
                            "status",
                            "actual",
                            "ranking",
                            "deadline",
                            "description",
                            "targets",
                            "phrase",
                            "player",
                            "detail",
                        }
                        else 1
                    )
                    for name in expected
                }
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise PresentationError(f"Invalid presentation placeholders: {key}") from exc
        if key.startswith("commands.") and len(text) > 100:
            raise PresentationError(f"Command description exceeds 100 characters: {key}")
        if (
            key.startswith("buttons.") or (key.startswith("cards.") and key.endswith(".name"))
        ) and len(text) > 80:
            raise PresentationError(f"Button label exceeds 80 characters: {key}")


@dataclass(frozen=True, slots=True)
class Presentation:
    locale: str
    strings: Mapping[str, str]
    colors: Mapping[str, int]

    def text(self, key, **values):
        if values.keys() != TEMPLATE_FIELDS.get(key, frozenset()):
            raise PresentationError(f"Incorrect presentation arguments: {key}")
        try:
            return self.strings[key].format(**values)
        except (KeyError, ValueError, TypeError) as exc:
            raise PresentationError(f"Invalid presentation key or value: {key}") from exc

    def color(self, key="default"):
        return self.colors.get(key, self.colors["default"])


def load_presentation(path: Path | None = None):
    base = _read(files("lets_play").joinpath("resources", "en-US.json").read_bytes())
    if (
        base.keys() != {"locale", "strings", "colors"}
        or not isinstance(base["strings"], dict)
        or not isinstance(base["colors"], dict)
        or TEMPLATE_FIELDS.keys() - base["strings"].keys()
        or "default" not in base["colors"]
    ):
        raise PresentationError("Bundled presentation catalog is incomplete.")
    strings, palette = dict(base["strings"]), dict(base["colors"])
    locale = base["locale"]
    if path is not None:
        try:
            with path.open("rb") as stream:
                override = _read(stream.read(MAX_CATALOG_BYTES + 1))
        except OSError as exc:
            raise PresentationError(
                "CHATGAMES_PRESENTATION_PATH must name a readable JSON file."
            ) from exc
        for group, original in (("strings", strings), ("colors", palette)):
            extra = override.get(group, {})
            if not isinstance(extra, dict) or extra.keys() - original.keys():
                raise PresentationError(f"Unknown or invalid presentation {group} keys.")
            original.update(extra)
        locale = override.get("locale", locale)
    if not isinstance(locale, str) or not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", locale
    ):
        raise PresentationError("Presentation locale must be a language tag.")
    _validate_strings(strings)
    colors = {}
    for key, value in palette.items():
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise PresentationError(f"Presentation color must use #RRGGBB: {key}")
        colors[key] = int(value[1:], 16)
    return Presentation(locale, MappingProxyType(strings), MappingProxyType(colors))


@lru_cache(maxsize=1)
def default_presentation():
    return load_presentation()
