"""A stale pre-commit schema observation must not reject committed initialization."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from homebrew_harness.storage.incremental_store import IncrementalStore
from homebrew_harness.storage.sqlite_store import BoundConnection


class SchemaOpenRaceTests(unittest.TestCase):
    def test_initializer_commits_between_schema_and_table_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = IncrementalStore(Path(directory))
            observed, committed = threading.Event(), threading.Event()
            errors, values = [], []
            execute = BoundConnection.execute
            intercepted = []

            class Cursor:
                def __init__(self, original):
                    self.original = original

                def fetchone(self):
                    row = self.original.fetchone()
                    self.original.close()
                    if row == (0,):
                        observed.set()
                        if not committed.wait(10):
                            raise AssertionError("Competing initializer did not commit.")
                    return row

            def interleave(connection, statement, *args, **kw):
                cursor = execute(connection, statement, *args, **kw)
                if (threading.current_thread().name == "stale-initializer"
                        and statement == "PRAGMA user_version" and not intercepted):
                    intercepted.append(True)
                    return Cursor(cursor)
                return cursor

            def open_stale():
                try:
                    with store.connection(initialize=True) as connection:
                        values.append(connection.execute("PRAGMA user_version").fetchone()[0])
                except BaseException as error:
                    errors.append(error)

            with patch.object(BoundConnection, "execute", interleave):
                thread = threading.Thread(target=open_stale, name="stale-initializer")
                thread.start()
                try:
                    self.assertTrue(observed.wait(10))
                    with IncrementalStore(Path(directory)).write() as connection:
                        connection.execute("INSERT INTO metadata(key,value) VALUES('fixture','1')")
                finally:
                    committed.set()
                    thread.join(10)
                self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(values, [1])
            with store.connection() as connection:
                self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='fixture'").fetchone()[0], "1")
