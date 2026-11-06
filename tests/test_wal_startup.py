"""Real rollback-journal reader locks reproduce WAL transition contention."""
import asyncio
import sqlite3
import unittest
from unittest.mock import patch

import sheep_game.database as storage
from sheep_game.database import Database, SCHEMA, DATABASE_METADATA, SCHEMA_VERSION, one
import test_fresh_database as fixtures


class WalStartupTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FreshDatabaseTests.asyncSetUp
    asyncTearDown = fixtures.FreshDatabaseTests.asyncTearDown
    def make_delete_mode_database(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(self.path)
        try:
            for sql in SCHEMA:
                raw.execute(sql)
            raw.executemany('INSERT INTO sheep_metadata VALUES(?,?)', DATABASE_METADATA.items())
            raw.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            raw.commit()
        finally:
            raw.close()

    async def test_reader_lock_during_transition_retries_then_succeeds(self):
        self.make_delete_mode_database()
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        real_one = storage.one
        attempts = []

        async def contended_one(conn, sql, parameters=()):
            if sql == 'PRAGMA journal_mode=WAL':
                attempts.append(sql)
                if len(attempts) == 1:
                    # The schema validation transaction has committed; acquire
                    # a real reader lock exactly in the exposed transition gap.
                    reader.execute('BEGIN')
                    reader.execute('SELECT * FROM sheep_metadata').fetchall()
                elif len(attempts) == 3:
                    reader.rollback()
            return await real_one(conn, sql, parameters)

        with patch.object(storage, 'one', side_effect=contended_one), patch.object(storage, 'WAL_ATTEMPT_BUSY_TIMEOUT_MS', 0):
            await self.db.init()
        self.assertEqual(len(attempts), 3)
        async with self.db.transaction(write=False) as conn:
            self.assertEqual((await one(conn, 'PRAGMA journal_mode'))[0], 'wal')
            self.assertEqual((await one(conn, 'PRAGMA busy_timeout'))[0], 10000)
            self.assertEqual((await one(conn, 'SELECT COUNT(*) FROM guilds'))[0], 0)

    async def test_permanent_transition_lock_times_out_and_closes_failed_connection(self):
        self.make_delete_mode_database()
        reader = sqlite3.connect(self.path)
        self.addCleanup(reader.close)
        real_one = storage.one

        async def locked_one(conn, sql, parameters=()):
            if sql == 'PRAGMA journal_mode=WAL' and not reader.in_transaction:
                reader.execute('BEGIN')
                reader.execute('SELECT * FROM sheep_metadata').fetchall()
            return await real_one(conn, sql, parameters)

        with patch.object(storage, 'one', side_effect=locked_one), \
             patch.object(storage, 'WAL_STARTUP_TIMEOUT_SECONDS', .03), \
             patch.object(storage, 'WAL_ATTEMPT_BUSY_TIMEOUT_MS', 0), \
             patch.object(storage, 'WAL_RETRY_DELAY_SECONDS', .001):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'Timed out enabling'):
                await self.db.init()
        self.assertIsNone(self.db._conn)
        reader.rollback()
        await self.db.init()

    async def test_unrelated_wal_error_is_not_retried_or_suppressed(self):
        real_one = storage.one
        attempts = []

        async def broken_one(conn, sql, parameters=()):
            if sql == 'PRAGMA journal_mode=WAL':
                attempts.append(sql)
                raise sqlite3.OperationalError('unrelated disk failure')
            return await real_one(conn, sql, parameters)

        with patch.object(storage, 'one', side_effect=broken_one):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'unrelated disk'):
                await self.db.init()
        self.assertEqual(len(attempts), 1)
        self.assertIsNone(self.db._conn)

    async def test_repeated_parallel_first_opens_preserve_schema_and_integrity(self):
        for index in range(12):
            path = self.path.with_name(f'parallel-{index}.db')
            a, b = Database(path), Database(path)
            try:
                await asyncio.gather(a.init(), b.init())
                for db in (a, b):
                    async with db.transaction(write=False) as conn:
                        self.assertEqual((await one(conn, 'PRAGMA journal_mode'))[0], 'wal')
                        self.assertEqual((await one(conn, 'PRAGMA integrity_check'))[0], 'ok')
            finally:
                await a.close()
                await b.close()
