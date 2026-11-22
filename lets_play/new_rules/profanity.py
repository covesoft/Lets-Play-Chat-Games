"""Private, immutable gameplay vocabulary for Mind Your Language."""

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType

from lets_play.new_rules.contracts import RuleInstance
from lets_play.new_rules.parsing import MessageView, is_word_gap, parse_message

CARD_ID = "NR032"
REVISION_KEY = "list_revision"
MAX_LIST_BYTES = 256 * 1024
MAX_ENTRY_WORDS = 16


def list_revision(instance: RuleInstance) -> str | None:
    params = dict(instance.parameters)
    revision = params.get(REVISION_KEY, "")
    if (
        len(params) != 1
        or len(revision) != 64
        or any(char not in "0123456789abcdef" for char in revision)
    ):
        return None
    return revision


@dataclass(frozen=True, slots=True, init=False)
class ProfanityList:
    revision: str
    _entries: MappingProxyType[int, frozenset[tuple[str, ...]]] = field(repr=False, compare=False)

    def __init__(self, text: str) -> None:
        if len(text.encode("utf-8")) > MAX_LIST_BYTES:
            raise ValueError("The private gameplay list is too large.")
        entries = set()
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if len(line) > 256:
                raise ValueError("Invalid private gameplay list entry.")
            view = parse_message(line)
            if (
                not 1 <= len(view.words) <= MAX_ENTRY_WORDS
                or view.formatting
                or view.links
                or view.objects
                or not all(is_word_gap(gap) for gap in _gaps(view))
            ):
                raise ValueError("Invalid private gameplay list entry.")
            entries.add(view.word_keys)
        if not entries:
            raise ValueError("The private gameplay list is empty.")
        by_length = {}
        for entry in entries:
            by_length.setdefault(len(entry), set()).add(entry)
        object.__setattr__(
            self,
            "_entries",
            MappingProxyType({n: frozenset(keys) for n, keys in by_length.items()}),
        )
        canonical = json.dumps(sorted(entries), ensure_ascii=False, separators=(",", ":"))
        object.__setattr__(self, "revision", sha256(canonical.encode("utf-8")).hexdigest())

    @property
    def entry_count(self) -> int:
        return sum(len(entries) for entries in self._entries.values())

    def instantiate(self, instance_id: str) -> RuleInstance:
        return RuleInstance(instance_id, CARD_ID, ((REVISION_KEY, self.revision),))

    def matches(self, view: MessageView) -> bool:
        keys = view.word_keys
        for size, entries in self._entries.items():
            for start in range(len(keys) - size + 1):
                if keys[start : start + size] not in entries:
                    continue
                if all(
                    is_word_gap(view.logical[view.words[i].end : view.words[i + 1].start])
                    for i in range(start, start + size - 1)
                ):
                    return True
        return False


def _gaps(view: MessageView):
    end = 0
    for word in view.words:
        yield view.logical[end : word.start]
        end = word.end
    yield view.logical[end:]


def list_available(instances: tuple[RuleInstance, ...], profanity: ProfanityList | None) -> bool:
    return all(
        profanity is not None and list_revision(instance) == profanity.revision
        for instance in instances
        if instance.card_id == CARD_ID
    )


def load_profanity_list(path: Path | None) -> ProfanityList | None:
    if path is None:
        return None
    try:
        with path.open("rb") as source:
            data = source.read(MAX_LIST_BYTES + 1)
        if len(data) <= MAX_LIST_BYTES:
            return ProfanityList(data.decode("utf-8-sig"))
    except (OSError, ValueError):
        # Diagnostics deliberately omit the path, entry and underlying exception.
        pass
    return None
