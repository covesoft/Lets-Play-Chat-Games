"""Plain data shared by the future engine, repositories, and Discord adapter.

These types perform no I/O and implement no session transitions. Message and
attachment content is transient and excluded from repr; it is not a DB payload.
"""

from __future__ import annotations

from collections.abc import MutableSequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from unicodedata import normalize


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware timestamp is required.")
    return value.astimezone(UTC)


def _positive(value: int) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError("A positive integer ID is required.")


class Outcome(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class RuleInstance:
    instance_id: str
    card_id: str
    parameters: tuple[tuple[str, str], ...] = field(default=(), repr=False)

    def __post_init__(self) -> None:
        if not self.instance_id:
            raise ValueError("A rule instance ID is required.")
        if not (
            len(self.card_id) == 5
            and self.card_id.startswith("NR")
            and self.card_id[2:].isascii()
            and self.card_id[2:].isdigit()
            and 1 <= int(self.card_id[2:]) <= 40
        ):
            raise ValueError("An approved NR001–NR040 card ID is required.")
        if not isinstance(self.parameters, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not all(isinstance(value, str) for value in pair)
            for pair in self.parameters
        ):
            raise ValueError("Parameters must be immutable string pairs.")
        keys = [key for key, _ in self.parameters]
        if any(not key for key in keys) or len(set(keys)) != len(keys):
            raise ValueError("Parameter keys must be nonempty and unique.")


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    rule_instance_id: str
    outcome: Outcome
    reason_code: str | None = None

    def __post_init__(self) -> None:
        if not self.rule_instance_id or not isinstance(self.outcome, Outcome):
            raise ValueError("An instance ID and typed outcome are required.")
        if (self.outcome is Outcome.FAIL) != bool(self.reason_code):
            raise ValueError("Only a failed evaluation must have a reason code.")


class EventKind(StrEnum):
    MESSAGE_CREATE = "message_create"
    REACTION_ADD = "reaction_add"
    TIMER = "timer"
    MANAGER_END = "manager_end"
    CAPABILITY_CHANGED = "capability_changed"


@dataclass(frozen=True, slots=True)
class SessionRoute:
    guild_id: int
    channel_id: int
    session_id: str

    def __post_init__(self) -> None:
        _positive(self.guild_id)
        _positive(self.channel_id)
        if not self.session_id:
            raise ValueError("A session ID is required.")


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    route: SessionRoute
    kind: EventKind
    source_id: str
    actor_id: int | None
    created_at: datetime
    received_at: datetime
    sequence: int

    def __post_init__(self) -> None:
        if not self.source_id or not isinstance(self.kind, EventKind):
            raise ValueError("A stable source ID and typed event kind are required.")
        if self.actor_id is not None:
            _positive(self.actor_id)
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("A nonnegative arrival sequence is required.")
        object.__setattr__(self, "created_at", _utc(self.created_at))
        object.__setattr__(self, "received_at", _utc(self.received_at))

    @property
    def source_key(self) -> tuple[str, EventKind, str]:
        """Proposed session-scoped event identity, not implemented deduplication."""
        return self.route.session_id, self.kind, self.source_id


class ReferenceKind(StrEnum):
    NONE = "none"
    REPLY = "reply"
    FORWARD = "forward"
    THREAD_STARTER = "thread_starter"


@dataclass(frozen=True, slots=True)
class AttachmentSnapshot:
    attachment_id: int
    filename: str = field(repr=False)
    content_type: str | None = field(default=None, repr=False)
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        _positive(self.attachment_id)


@dataclass(frozen=True, slots=True)
class MessageEvent:
    envelope: EventEnvelope
    content: str | None = field(default=None, repr=False)
    attachments: tuple[AttachmentSnapshot, ...] = field(default=(), repr=False)
    sticker_ids: tuple[int, ...] = ()
    reference_kind: ReferenceKind = ReferenceKind.NONE
    referenced_message_id: int | None = None
    is_voice_message: bool = False
    is_bot: bool = False
    is_webhook: bool = False
    is_system: bool = False

    def __post_init__(self) -> None:
        if self.envelope.kind is not EventKind.MESSAGE_CREATE or self.envelope.actor_id is None:
            raise ValueError("A message-create envelope with an actor is required.")
        if self.content is not None and not isinstance(self.content, str):
            raise ValueError("Message content must be text or None.")
        if not isinstance(self.attachments, tuple) or not isinstance(self.sticker_ids, tuple):
            raise ValueError("Message metadata collections must be immutable tuples.")
        if not isinstance(self.reference_kind, ReferenceKind):
            raise ValueError("A typed reference kind is required.")
        if any(not isinstance(item, AttachmentSnapshot) for item in self.attachments):
            raise ValueError("Attachments must be plain metadata snapshots.")
        for sticker_id in self.sticker_ids:
            _positive(sticker_id)
        if self.referenced_message_id is not None:
            _positive(self.referenced_message_id)

    @property
    def is_human(self) -> bool:
        return not (self.is_bot or self.is_webhook or self.is_system)

    @property
    def is_reply(self) -> bool:
        return self.reference_kind is ReferenceKind.REPLY and self.referenced_message_id is not None


@dataclass(frozen=True, slots=True)
class EmojiIdentity:
    """Custom names/animation do not identify emoji. Unicode keeps meaningful joins/modifiers."""

    custom_id: int | None = None
    unicode: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.custom_id is None) == (self.unicode is None):
            raise ValueError("Supply one custom ID or one Unicode sequence.")
        if self.custom_id is not None:
            _positive(self.custom_id)
        elif not self.unicode or self.unicode.isspace():
            raise ValueError("A nonempty Unicode emoji sequence is required.")

    @property
    def key(self) -> tuple[str, int | str]:
        if self.custom_id is not None:
            return "custom", self.custom_id
        # Presentation selectors do not make another exclusion identity. Do not
        # remove ZWJ, skin modifiers, regional indicators, or keycap combining marks.
        value = normalize("NFC", self.unicode).replace("\ufe0e", "").replace("\ufe0f", "")
        return "unicode", value


@dataclass(frozen=True, slots=True)
class ReactionEvent:
    envelope: EventEnvelope
    target_message_id: int
    emoji: EmojiIdentity
    is_bot: bool = False

    def __post_init__(self) -> None:
        if self.envelope.kind is not EventKind.REACTION_ADD or self.envelope.actor_id is None:
            raise ValueError("A reaction-add envelope with an actor is required.")
        _positive(self.target_message_id)

    @property
    def offense_key(self) -> tuple[str, int, int]:
        """Does not contain round, rule instance, or emoji: one actor/target per game."""
        return self.envelope.route.session_id, self.envelope.actor_id, self.target_message_id


class Lifecycle(StrEnum):
    LOBBY = "lobby"
    OPENING_CHOICE = "opening_choice"
    PLAYING = "playing"
    PAUSED = "paused"
    ENDED = "ended"


class Phase(StrEnum):
    ORDINARY = "ordinary"
    MODE = "mode"


class Advancement(StrEnum):
    IDLE = "idle"
    WARNING = "warning"
    CHOOSING = "choosing"
    HELD = "held"


class PauseReason(StrEnum):
    REVIVAL = "revival"
    RECOVERY = "recovery"
    CAPABILITY = "capability"
    DEPENDENCY = "dependency"


@dataclass(frozen=True, slots=True)
class SessionPosition:
    route: SessionRoute
    lifecycle: Lifecycle
    phase: Phase = Phase.ORDINARY
    advancement: Advancement = Advancement.IDLE
    round_id: str | None = None
    ruleset_version: int = 0
    offer_id: str | None = None
    transition_id: str | None = None
    pause_reason: PauseReason | None = None

    def __post_init__(self) -> None:
        if not (
            isinstance(self.lifecycle, Lifecycle)
            and isinstance(self.phase, Phase)
            and isinstance(self.advancement, Advancement)
        ):
            raise ValueError("Typed lifecycle, phase, and advancement values are required.")
        if self.pause_reason is not None and not isinstance(self.pause_reason, PauseReason):
            raise ValueError("A typed pause reason is required.")
        if (self.lifecycle is Lifecycle.PAUSED) != (self.pause_reason is not None):
            raise ValueError("A pause reason belongs only to a paused session.")
        if type(self.ruleset_version) is not int or self.ruleset_version < 0:
            raise ValueError("A ruleset version cannot be negative.")
        if self.lifecycle is Lifecycle.PLAYING and not self.round_id:
            raise ValueError("Playing requires an owner round.")


class DeadlineKind(StrEnum):
    LOBBY = "lobby"
    WARNING = "warning"
    CHOICE = "choice"
    REVIVAL = "revival"
    MODE = "mode"
    HELD_IDLE = "held_idle"


@dataclass(frozen=True, slots=True)
class Deadline:
    token: str
    kind: DeadlineKind
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.token or not isinstance(self.kind, DeadlineKind):
            raise ValueError("A deadline token and typed kind are required.")
        object.__setattr__(self, "expires_at", _utc(self.expires_at))

    def is_due(self, now: datetime) -> bool:
        return _utc(now) >= self.expires_at


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class RandomSource(Protocol):
    def randrange(self, stop: int) -> int: ...

    def shuffle[T](self, values: MutableSequence[T]) -> None: ...
