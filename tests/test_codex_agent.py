from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import runner_core
from lib.agents import codex


SESSION_ID = "019fa2b2-15f4-7a40-8d16-e3977765d17e"


def write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


class CodexCommandTests(unittest.TestCase):
    def test_fresh_command_uses_json_stdin_and_unattended_mode(self) -> None:
        command = codex.build_cmd(Path("/tmp/prompt with space.md"), None)
        self.assertIn("'codex' exec --json --color never", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        self.assertIn("'/tmp/prompt with space.md'", command)
        self.assertNotIn(" resume ", command)

    def test_resume_command_places_global_flags_before_resume(self) -> None:
        command = codex.build_cmd(
            Path("/tmp/prompt.md"),
            SESSION_ID,
            "gpt-5.6-sol",
        )
        self.assertIn("--model 'gpt-5.6-sol' resume", command)
        self.assertIn(f"resume '{SESSION_ID}' -", command)

    def test_renderer_splits_agent_text_from_jsonl(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": SESSION_ID},
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": "finished",
                },
            },
            {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
        ]
        proc = subprocess.run(
            [sys.executable, str(codex.render_script_path())],
            input="".join(json.dumps(event) + "\n" for event in events),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(proc.stdout, "finished\n")
        self.assertEqual(
            [json.loads(line) for line in proc.stderr.splitlines()],
            events,
        )


class CodexEventTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.rdir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_extract_and_compact_success(self) -> None:
        events = [
            {"type": "thread.started", "thread_id": SESSION_ID},
            {"type": "turn.started"},
            {
                "type": "item.started",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "git status --short",
                    "status": "in_progress",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "command_execution",
                    "command": "git status --short",
                    "aggregated_output": " M file.py\n",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_2",
                    "type": "agent_message",
                    "text": "The repository has one modified file.",
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 80,
                    "output_tokens": 20,
                    "reasoning_output_tokens": 10,
                },
            },
        ]
        write_jsonl(self.rdir / "stderr.log", events)
        summary = codex.extract(self.rdir)
        self.assertEqual(summary["sessionId"], SESSION_ID)
        self.assertEqual(summary["lastTokens"], 120)
        self.assertEqual(summary["toolCallCount"], 1)

        view = codex.compact_view(self.rdir, terminal=True, started_at=None)
        self.assertEqual(
            view["finalReply"]["text"],
            "The repository has one modified file.",
        )
        self.assertEqual(view["finalReply"]["totalToolCalls"], 1)
        self.assertEqual(view["finalReply"]["totalTokens"], 120)
        self.assertIn("git status --short", view["finalReply"]["recentToolCalls"][0])

    def test_failed_turn_is_interrupted(self) -> None:
        write_jsonl(
            self.rdir / "stderr.log",
            [
                {"type": "thread.started", "thread_id": SESSION_ID},
                {
                    "type": "turn.failed",
                    "error": {"code": "rate_limit", "message": "try later"},
                },
            ],
        )
        view = codex.compact_view(self.rdir, terminal=True, started_at=None)
        self.assertIsNone(view["finalReply"])
        self.assertEqual(view["interrupted"]["code"], "rate_limit")
        self.assertEqual(view["interrupted"]["reason"], "try later")

    def test_native_startup_error_is_interrupted(self) -> None:
        (self.rdir / "stderr.log").write_text(
            "bash: codex: command not found\n",
            encoding="utf-8",
        )
        view = codex.compact_view(self.rdir, terminal=True, started_at=None)
        self.assertEqual(view["interrupted"]["code"], "BackendStderr")
        self.assertIn("command not found", view["interrupted"]["reason"])

    def test_current_turn_cursor_excludes_prior_reply(self) -> None:
        first = [
            {"type": "thread.started", "thread_id": SESSION_ID},
            {
                "type": "item.completed",
                "item": {"id": "a", "type": "agent_message", "text": "old"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        second = [
            {
                "type": "item.completed",
                "item": {"id": "b", "type": "agent_message", "text": "new"},
            },
            {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 1}},
        ]
        first_text = "".join(json.dumps(event) + "\n" for event in first)
        (self.rdir / "stderr.log").write_text(
            first_text + "".join(json.dumps(event) + "\n" for event in second),
            encoding="utf-8",
        )
        (self.rdir / "meta.json").write_text(
            json.dumps({
                "agentTurnCursors": [
                    {"turn": 1, "stderrByte": 0},
                    {"turn": 2, "stderrByte": len(first_text.encode())},
                ]
            }),
            encoding="utf-8",
        )
        reply = codex.final_reply(self.rdir)
        self.assertEqual(reply["text"], "new")
        self.assertEqual(reply["totalTokens"], 3)


class CodexSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.codex_home = Path(self.temp.name) / "codex-home"
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.temp.cleanup()

    def make_session(self, root: str, cwd: Path, session_id: str = SESSION_ID) -> Path:
        directory = self.codex_home / root
        if root == "sessions":
            directory /= "2026/07/27"
        directory.mkdir(parents=True, exist_ok=True)
        transcript = directory / f"rollout-2026-07-27T00-00-00-{session_id}.jsonl"
        write_jsonl(
            transcript,
            [{
                "type": "session_meta",
                "payload": {"id": session_id, "cwd": str(cwd)},
            }],
        )
        return transcript

    def test_find_active_session_and_original_cwd(self) -> None:
        transcript = self.make_session("sessions", self.project)
        found = codex.find_session(SESSION_ID, str(Path(self.temp.name)))
        self.assertEqual(found["projectDir"], str(self.project))
        self.assertEqual(found["transcript"], str(transcript))
        self.assertFalse(found["sameProject"])

    def test_find_archived_same_project_session(self) -> None:
        self.make_session("archived_sessions", self.project)
        found = codex.find_session(SESSION_ID, str(self.project))
        self.assertTrue(found["sameProject"])


class CodexCatalogTests(unittest.TestCase):
    @mock.patch("lib.agents.codex.subprocess.run")
    def test_catalog_and_validation(self, run: mock.Mock) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["codex"],
            0,
            stdout=json.dumps({
                "models": [{
                    "slug": "gpt-test",
                    "display_name": "GPT Test",
                    "description": "fixture",
                    "visibility": "list",
                }]
            }),
            stderr="",
        )
        self.assertEqual(codex.available_models()[0]["model"], "gpt-test")
        self.assertEqual(codex.validate_model("gpt-test"), (True, ["gpt-test"]))
        self.assertEqual(codex.validate_model("bad"), (False, ["gpt-test"]))


class AdoptionResolutionTests(unittest.TestCase):
    def test_duplicate_verified_uuid_requires_explicit_agent(self) -> None:
        class Runtime:
            @staticmethod
            def looks_like_session_id(value: str) -> bool:
                return value == SESSION_ID

            @staticmethod
            def find_session(value: str, cwd: str) -> dict:
                return {"projectDir": cwd, "sameProject": True}

        class Registry:
            @staticmethod
            def names() -> list[str]:
                return ["claude", "codex"]

            @staticmethod
            def get(name: str) -> Runtime:
                return Runtime()

        result = runner_core._resolve_session_adoption(
            Registry(),
            SESSION_ID,
            None,
            "/tmp/project",
        )
        self.assertEqual(result["matches"], ["claude", "codex"])
        self.assertIn("explicit agent", result["hint"])


if __name__ == "__main__":
    unittest.main()
