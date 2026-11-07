"""Ordered, immutable migrations, run inside an explicit transaction."""

from dataclasses import dataclass

APPLICATION_ID = int.from_bytes(b"LPLY", "big")


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]


MIGRATIONS = (
    Migration(
        version=1,
        name="foundation",
        statements=(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL)",
        ),
    ),
)
SCHEMA_VERSION = MIGRATIONS[-1].version
