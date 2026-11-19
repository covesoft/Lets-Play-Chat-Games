"""Schema 14 adds durable targets without guessing targets for old claims."""

import sqlite3
from contextlib import closing

import pytest
from test_ordinary_flow import rows
from test_reboot_migration import released_twelve

from lets_play.storage import bootstrap
from lets_play.storage.migrations import MIGRATIONS, SCHEMA_VERSION


@pytest.mark.asyncio
async def test_upgrade_from_13_keeps_legacy_claim_without_inferred_target(tmp_path, monkeypatch):
    path = tmp_path / "game.sqlite3"
    await released_twelve(path, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(bootstrap, "MIGRATIONS", MIGRATIONS[:13])
        assert (await bootstrap.initialize_database(path)).schema_version == 13
    before = rows(path, "SELECT * FROM nr_discord_deliveries")
    assert (await bootstrap.initialize_database(path)).schema_version == SCHEMA_VERSION
    assert rows(path, "SELECT * FROM nr_discord_deliveries") == before
    assert rows(path, "SELECT * FROM nr_discord_publication_targets") == []
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO nr_discord_publication_targets VALUES ('ordinary',999,1000)")
    assert rows(path, "PRAGMA integrity_check") == [("ok",)]
