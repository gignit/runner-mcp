from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import runner_core
from lib.agents import opencode


SESSION_ID = "ses_e4564d9ddffeYQR1DrCUApLIYz"


def make_db(path: Path, rows: list[tuple[str, str]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
        conn.executemany("INSERT INTO session VALUES (?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


class OpencodeFindSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.data = root / "data"
        (self.data / "opencode").mkdir(parents=True)
        self.caller = root / "caller"
        self.caller.mkdir()
        self.other = root / "other"
        self.other.mkdir()
        self.env = mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": str(self.data)}, clear=False)
        self.env.start()
        os.environ.pop("OPENCODE_DB", None)

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def test_session_in_another_directory_reports_that_directory(self) -> None:
        make_db(self.data / "opencode" / "opencode.db", [(SESSION_ID, str(self.other))])
        found = opencode.find_session(SESSION_ID, str(self.caller))
        self.assertEqual(found["projectDir"], str(self.other))
        self.assertFalse(found["sameProject"])

    def test_session_in_caller_directory_is_same_project(self) -> None:
        make_db(self.data / "opencode" / "opencode.db", [(SESSION_ID, str(self.caller))])
        found = opencode.find_session(SESSION_ID, str(self.caller))
        self.assertTrue(found["sameProject"])

    def test_channel_database_is_searched(self) -> None:
        make_db(self.data / "opencode" / "opencode.db", [])
        make_db(self.data / "opencode" / "opencode-beta.db", [(SESSION_ID, str(self.other))])
        found = opencode.find_session(SESSION_ID, str(self.caller))
        self.assertEqual(found["projectDir"], str(self.other))

    def test_opencode_db_override(self) -> None:
        db = Path(self.temp.name) / "custom.db"
        make_db(db, [(SESSION_ID, str(self.other))])
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(db)}):
            found = opencode.find_session(SESSION_ID, str(self.caller))
        self.assertEqual(found["database"], str(db))

    def test_missing_session_in_readable_database_is_none(self) -> None:
        make_db(self.data / "opencode" / "opencode.db", [("ses_other", str(self.other))])
        self.assertIsNone(opencode.find_session(SESSION_ID, str(self.caller)))

    def test_no_database_is_unverified_and_keeps_caller_cwd(self) -> None:
        found = opencode.find_session(SESSION_ID, str(self.caller))
        self.assertIsNone(found["projectDir"])
        self.assertTrue(found["sameProject"])

    def test_non_session_id_is_none(self) -> None:
        self.assertIsNone(opencode.find_session("not-a-session", str(self.caller)))

    def test_adoption_spawns_in_session_directory(self) -> None:
        make_db(self.data / "opencode" / "opencode.db", [(SESSION_ID, str(self.other))])

        class Registry:
            def names(self) -> list[str]:
                return ["opencode"]

            def get(self, name: str):
                return opencode

        resolved = runner_core._resolve_session_adoption(
            Registry(), SESSION_ID, None, str(self.caller))
        self.assertEqual(resolved, {"runtime": "opencode", "projectDir": str(self.other)})

    def test_continuation_resolves_session_directory(self) -> None:
        make_db(self.data / "opencode" / "opencode.db", [(SESSION_ID, str(self.other))])
        self.assertEqual(
            runner_core._session_project_dir(opencode, SESSION_ID, str(self.caller)),
            str(self.other))
        self.assertIsNone(runner_core._session_project_dir(opencode, None, str(self.caller)))


if __name__ == "__main__":
    unittest.main()
