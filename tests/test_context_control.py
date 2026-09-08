from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from integrations.common import context_control as context


class ContextControlTests(unittest.TestCase):
    def setUp(self):
        fixture = tempfile.TemporaryDirectory()
        self.addCleanup(fixture.cleanup)
        self.root = Path(fixture.name)

    def event(self, kind, session="parent", **values):
        return context.handle(
            {"hook_event_name": kind, "session_id": session, **values}, self.root
        )

    def test_two_attempts_then_break_loop_until_success(self):
        for _ in range(2):
            self.assertEqual(self.event("PreCompact", trigger="auto"), {})
        blocked = self.event("PreCompact", trigger="auto")
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("parent", blocked["reason"])
        self.assertEqual(self.event("PreCompact", trigger="manual"), {})
        self.event("PostCompact", trigger="manual", compact_summary="handoff")
        self.assertEqual(self.event("PreCompact", trigger="auto"), {})

    def test_user_prompt_resets_but_tool_turns_do_not(self):
        self.event("PreCompact", trigger="auto")
        self.event("PreCompact", trigger="auto")
        self.event("PostToolUse", tool_name="Read", tool_response="small")
        self.assertEqual(self.event("PreCompact", trigger="auto")["decision"], "block")
        self.event("UserPromptSubmit", prompt="continue")
        self.assertEqual(self.event("PreCompact", trigger="auto"), {})

    def test_background_forks_do_not_share_attempts_or_completion(self):
        self.event("PreCompact", trigger="auto")
        self.event("PreCompact", trigger="auto")
        self.assertEqual(self.event("PreCompact", "fork", trigger="auto"), {})
        self.event("PostCompact", "fork", trigger="auto", compact_summary="fork result")
        self.assertEqual(self.event("PreCompact", trigger="auto")["decision"], "block")
        self.assertTrue((self.root / "context-state/fork.json").exists())

    def test_bash_output_is_bounded_with_lossless_private_artifact(self):
        response = {
            "stdout": "first\n" + "x" * 40000 + "\nlast",
            "stderr": "warning",
            "interrupted": False,
            "isImage": False,
            "exitCode": 7,
            "persistedOutputPath": "/private/native-output.txt",
            "persistedOutputSize": 40011,
        }
        result = self.event("PostToolUse", tool_name="Bash", tool_response=response)
        updated = result["hookSpecificOutput"]["updatedToolOutput"]
        self.assertLessEqual(
            len(updated["stdout"]) + len(updated["stderr"]), context.OUTPUT_BUDGET
        )
        self.assertTrue(updated["stdout"].startswith("first\n"))
        self.assertTrue(updated["stdout"].endswith("\nlast"))
        self.assertIn("INCOMPLETE", updated["stdout"])
        self.assertEqual(updated["stderr"], "warning")
        self.assertEqual(updated["exitCode"], 7)
        self.assertNotIn("persistedOutputPath", updated)
        self.assertNotIn("persistedOutputSize", updated)
        (artifact,) = (self.root / "context-artifacts").iterdir()
        self.assertEqual(json.loads(artifact.read_bytes()), response)
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            self.event("PostToolUse", tool_name="Bash", tool_response=response), result
        )

    def test_mcp_array_and_call_result_shapes(self):
        content = [{"type": "text", "text": "x" * 30000}]
        for response in (content, {"content": content}, "x" * 30000):
            with self.subTest(shape=type(response)):
                result = self.event(
                    "PostToolUse",
                    tool_name="mcp__leanctx__ctx_shell",
                    tool_response=response,
                )
                updated = result["hookSpecificOutput"]["updatedMCPToolOutput"]
                self.assertEqual(type(updated), type(response))
                self.assertLess(len(json.dumps(updated)), 10000)

    def test_exact_reads_small_outputs_errors_and_images_unchanged(self):
        for tool, response in (
            ("mcp__leanctx__ctx_read", "x" * 30000),
            ("Read", {"content": "x" * 30000}),
            ("Bash", {"stdout": "OK", "stderr": ""}),
            ("Bash", {"stdout": "x" * 30000, "interrupted": True}),
            ("Bash", {"stdout": "x" * 30000, "isImage": True}),
            (
                "mcp__leanctx__ctx_shell",
                {"content": [{"type": "text", "text": "x" * 30000}], "isError": True},
            ),
            ("mcp__leanctx__ctx_shell", [{"type": "image", "data": "x" * 30000}]),
            (
                "mcp__leanctx__ctx_shell",
                {
                    "content": [{"type": "text", "text": "x" * 30000}],
                    "structuredContent": {"value": 1},
                },
            ),
            (
                "mcp__leanctx__ctx_shell",
                [
                    {"type": "text", "text": "x" * 30000},
                    {"type": "image", "data": "fixture"},
                ],
            ),
        ):
            with self.subTest(tool=tool):
                self.assertEqual(
                    self.event("PostToolUse", tool_name=tool, tool_response=response),
                    {},
                )

    def test_unsafe_artifact_directory_keeps_original_output(self):
        external = self.root / "external"
        external.mkdir()
        (self.root / "context-artifacts").symlink_to(external)
        result = self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "parent",
                "tool_name": "Bash",
                "tool_response": {"stdout": "x" * 30000},
            }
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(list(external.iterdir()), [])

    def run_hook(self, event):
        return subprocess.run(
            [sys.executable, str(Path(context.__file__))],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            env={**os.environ, "CLAUDEX_RUN_DIR": str(self.root)},
        )

    def test_invalid_session_cannot_escape_state_directory(self):
        for session in ("../outside", "", "a/b", "a\nb"):
            self.assertEqual(self.event("PreCompact", session, trigger="auto"), {})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_corrupt_state_does_not_block_or_overwrite(self):
        self.event("PreCompact", trigger="auto")
        state = self.root / "context-state/parent.json"
        state.write_text("corrupt")
        result = self.run_hook(
            {"hook_event_name": "PreCompact", "session_id": "parent", "trigger": "auto"}
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(state.read_text(), "corrupt")

    def test_symlinked_state_or_lock_cannot_modify_another_file(self):
        states = self.root / "context-state"
        states.mkdir(mode=0o700)
        external = self.root / "external.json"
        external.write_text('{"attempts": 0}')
        external.chmod(0o600)
        for name in ("parent.json", "parent.lock"):
            with self.subTest(name=name):
                target = states / name
                target.unlink(missing_ok=True)
                target.symlink_to(external)
                result = self.run_hook(
                    {
                        "hook_event_name": "PreCompact",
                        "session_id": "parent",
                        "trigger": "auto",
                    }
                )
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.returncode, 0)
                self.assertEqual(external.read_text(), '{"attempts": 0}')
                target.unlink()

    def test_failed_artifact_write_never_returns_a_replacement(self):
        response = {"stdout": "x" * 30000}
        with mock.patch.object(context, "_create", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.event("PostToolUse", tool_name="Bash", tool_response=response)
        self.assertEqual(response, {"stdout": "x" * 30000})

    def test_existing_artifact_must_match_before_output_is_replaced(self):
        response = {"stdout": "x" * 30000}
        self.event("PostToolUse", tool_name="Bash", tool_response=response)
        (artifact,) = (self.root / "context-artifacts").iterdir()
        artifact.write_text("damaged")
        result = self.run_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "parent",
                "tool_name": "Bash",
                "tool_response": response,
            }
        )
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(artifact.read_text(), "damaged")

    def test_many_text_blocks_obey_aggregate_budget(self):
        response = [{"type": "text", "text": "x" * 5000} for _ in range(4)]
        result = self.event(
            "PostToolUse", tool_name="mcp__leanctx__ctx_shell", tool_response=response
        )
        blocks = result["hookSpecificOutput"]["updatedMCPToolOutput"]
        self.assertLessEqual(
            sum(len(item["text"]) for item in blocks), context.OUTPUT_BUDGET
        )

    def test_checkpoint_files_are_isolated_by_native_conversation(self):
        scripts = Path(__file__).resolve().parents[1] / "controller/plugin/scripts"
        transcript = self.root / "transcript.jsonl"
        transcript.write_text("")
        transcript.chmod(0o600)
        environment = {**os.environ, "CLAUDEX_RUN_DIR": str(self.root)}
        for session in ("parent", "fork"):
            result = subprocess.run(
                ["bash", str(scripts / "save-compaction-checkpoint.sh")],
                input=json.dumps(
                    {
                        "session_id": session,
                        "trigger": "manual",
                        "cwd": str(self.root),
                        "transcript_path": str(transcript),
                        "compact_summary": session,
                    }
                ),
                text=True,
                capture_output=True,
                env=environment,
                check=True,
            )
            self.assertEqual(result.stdout, "")
        for session in ("parent", "fork"):
            checkpoint = self.root / f"compaction-checkpoint-{session}.json"
            self.assertEqual(
                json.loads(checkpoint.read_text())["compactSummary"], session
            )
            result = subprocess.run(
                ["bash", str(scripts / "restore-compaction-checkpoint.sh")],
                input=json.dumps(
                    {"session_id": session, "source": "compact", "cwd": str(self.root)}
                ),
                text=True,
                capture_output=True,
                env=environment,
                check=True,
            )
            self.assertIn("additionalContext", result.stdout)


if __name__ == "__main__":
    unittest.main()
