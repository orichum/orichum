"""Native, provider-free tool/compaction soak through the real route proxy.

ORICHUM_NATIVE_CLAUDE=/path/to/claude ORICHUM_CONTEXT_SOAK_TURNS=256 \
    python3 -m unittest tests.test_context_soak_native

The fixture reports synthetic token usage to exercise repeated full-window
transitions quickly. This tests lifecycle/transport, not provider capacity or
the quality of model-generated summaries. No credentials/project data are used.
"""

from __future__ import annotations

import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from integrations.common.context_control import handle
from integrations.common.model_context import client_model
from tests.test_model_context import binding
from tests.test_route_proxy import ProxyHarness

ROOT = Path(__file__).resolve().parents[1]
HANDOFF = (
    "Task: continue the numbered context fixture. Approved: create only fixture "
    "markers in this temporary directory. Denied: no external actions. "
    "Completed steps are recorded in step-* files; do not repeat them. "
    "Pending: finish the remaining numbered steps and report completion."
)


def interactive_run(command, root, environment, state, timeout):
    """Exercise the product's REPL, not the SDK print loop (which differs)."""
    config_dir = root / "claude"
    config_dir.mkdir(mode=0o700)
    onboarding = {
        "hasCompletedOnboarding": True,
        "theme": "dark",
        "projects": {
            str(root): {
                "hasTrustDialogAccepted": True,
                "hasCompletedProjectOnboarding": True,
            }
        },
        "customApiKeyResponses": {"approved": ["local-fixture"], "rejected": []},
    }
    (config_dir / ".claude.json").write_text(json.dumps(onboarding))
    (root / ".claude.json").write_text(json.dumps(onboarding))
    master, slave = pty.openpty()
    process = subprocess.Popen(
        command,
        cwd=root,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    screen = b""
    deadline = time.monotonic() + timeout
    finished_at = None
    progress = None
    last_progress = time.monotonic()
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            current = (state.get("issued"), state.get("compactions"))
            if current != progress:
                progress, last_progress = current, time.monotonic()
            elif time.monotonic() - last_progress > 90:
                break
            if (
                state.get("expect_block")
                and "Reactive compact blocked by PreCompact hook"
                in (root / "debug.log").read_text()
            ):
                state["blocked"] = True
                return screen.decode(errors="replace")
            if state.get("finished"):
                finished_at = finished_at or time.monotonic()
                if time.monotonic() - finished_at > 2:
                    return screen.decode(errors="replace")
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    block = os.read(master, 65536)
                except OSError:
                    break
                screen = (screen + block)[-20000:]
        diagnostic = "\n".join(
            line[:400]
            for line in (root / "debug.log").read_text().splitlines()
            if "compact" in line.lower() and "output budget" not in line
        )[-4000:]
        raise AssertionError(
            "Interactive context soak did not finish: "
            + str(state)
            + diagnostic
            + "\nTerminal tail: "
            + screen.decode(errors="replace")[-2000:]
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        os.close(master)


def stream_message(model, block, tokens):
    tool = block["type"] == "tool_use"
    start = {**block, "input": {}} if tool else {"type": "text", "text": ""}
    delta = (
        {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
        if tool
        else {"type": "text_delta", "text": block["text"]}
    )
    events = [
        {
            "type": "message_start",
            "message": {
                "id": f"msg_fixture_{time.monotonic_ns()}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": tokens,
                    "output_tokens": 1,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": start},
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {
                "stop_reason": "tool_use" if tool else "end_turn",
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": tokens,
                "output_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
            },
        },
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


@unittest.skipUnless(
    os.environ.get("ORICHUM_NATIVE_CLAUDE"), "opt-in native context soak"
)
class NativeContextSoakTests(unittest.TestCase):
    def test_repeated_compaction_preserves_progress_and_does_not_replay_tools(self):
        turns = int(os.environ.get("ORICHUM_CONTEXT_SOAK_TURNS", "48"))
        self.assertGreaterEqual(turns, 24)
        self.assertLessEqual(turns, 4096)
        self.exercise(turns)

    def test_native_retry_guard_blocks_exhausted_automatic_compaction(self):
        self.exercise(24, block_compaction=True)

    def exercise(self, turns, *, block_compaction=False):
        model = client_model(binding())
        primary = model.removesuffix("[1m]")
        fallback = primary.replace("0000000000000001/", "0000000000000002/")
        state = {
            "issued": 0,
            "compactions": 0,
            "last_compact": 0,
            "fallback_injected": False,
            "retained_handoffs": 0,
            "bounded_results": 0,
            "wire_models": set(),
            "count_calls": 0,
            "requests": [],
            "failed_tools": set(),
            "completed_tools": set(),
            "summaries_without_hook": 0,
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                document = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                if "count_tokens" in self.path:
                    state["count_calls"] += 1
                    tokens = (
                        968000 if state["issued"] - state["last_compact"] >= 8 else 1000
                    )
                    payload = json.dumps({"input_tokens": tokens}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                wire_model = document["model"]
                if len(state["requests"]) < 12:
                    state["requests"].append(
                        {
                            "max_tokens": document.get("max_tokens"),
                            "tools": len(document.get("tools", [])),
                            "system": str(document.get("system", ""))[:120],
                            "last": str(document.get("messages", [])[-1:])[:200],
                        }
                    )
                state["wire_models"].add(wire_model)
                # Compaction can include tools, and a blocked hook can be followed
                # by a normal, uncompacted continuation. Distinguish the native
                # summarization prompt and independently verify hook lifecycle.
                is_compact = (
                    "You are a helpful AI assistant tasked with summarizing conversations."
                    in json.dumps(document.get("system", []))
                    or "Your task is to create a detailed summary"
                    in json.dumps(document.get("messages", []))
                )
                hook_pending = any(
                    json.loads(path.read_text()).get("attempts", 0) > 0
                    for path in (root / "context-state").glob("*.json")
                )
                if is_compact and not hook_pending:
                    state["summaries_without_hook"] += 1
                if is_compact and not state["fallback_injected"]:
                    state["fallback_injected"] = True
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not document.get("tools") and not is_compact:
                    block = {"type": "text", "text": "Ready"}
                    tokens = 1
                elif is_compact:
                    state["compactions"] += 1
                    state["last_compact"] = state["issued"]
                    block = {"type": "text", "text": HANDOFF}
                    tokens = 1000
                else:
                    for message in document.get("messages", []):
                        content = message.get("content")
                        if not isinstance(content, list):
                            continue
                        for result in content:
                            if result.get("type") != "tool_result":
                                continue
                            key = (
                                "failed_tools"
                                if result.get("is_error")
                                else "completed_tools"
                            )
                            state[key].add(result.get("tool_use_id"))
                    encoded = json.dumps(document.get("messages", []))
                    if HANDOFF in encoded:
                        state["retained_handoffs"] += 1
                    if "Orichum output budget" in encoded:
                        state["bounded_results"] += 1
                    if state["issued"] >= turns or state.get("expect_block"):
                        state["finished"] = True
                        block = {"type": "text", "text": "Context soak complete."}
                    else:
                        state["issued"] += 1
                        number = state["issued"]
                        block = {
                            "type": "tool_use",
                            "id": f"tool_{number}",
                            "name": "Bash",
                            "input": {
                                "command": f"test ! -e step-{number} && printf done > step-{number} && printf '%040000d' 0",
                                "description": f"Execute fixture step {number} exactly once",
                            },
                        }
                    since_compact = state["issued"] - state["last_compact"]
                    tokens = (
                        968000 if since_compact >= 8 else 1000 + since_compact * 5000
                    )
                    if (
                        block_compaction
                        and state["issued"] == 8
                        and not state.get("expect_block")
                    ):
                        # Seed the real guard's exhausted history, then let native
                        # threshold compaction invoke the production hook. Unit
                        # tests cover counting failures/resetting this history;
                        # this test proves that native actually honors the block.
                        (counter,) = (root / "context-state").glob("*.json")
                        for _ in range(2):
                            handle(
                                {
                                    "session_id": counter.stem,
                                    "hook_event_name": "PreCompact",
                                    "trigger": "auto",
                                },
                                root,
                            )
                        state["expect_block"] = True
                payload = stream_message(wire_model, block, tokens)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory(prefix="orichum-context-soak-") as temporary:
            # macOS temp paths can traverse /var -> /private/var. Native trust
            # uses the physical cwd, so seed onboarding with that same identity.
            root = Path(temporary).resolve()
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=upstream.serve_forever, daemon=True)
            worker.start()
            try:
                with ProxyHarness(upstream.server_port, {primary: fallback}) as proxy:
                    hooks = json.loads(
                        (ROOT / "controller/plugin/hooks/hooks.json").read_text()
                    )["hooks"]
                    selected = {
                        key: hooks[key]
                        for key in (
                            "PreCompact",
                            "PostCompact",
                            "PostToolUse",
                            "UserPromptSubmit",
                        )
                    }
                    for entries in selected.values():
                        for entry in entries:
                            for hook in entry["hooks"]:
                                hook["command"] = hook["command"].replace(
                                    "${CLAUDE_PLUGIN_ROOT}",
                                    str(ROOT / "controller/plugin"),
                                )
                    settings = root / "settings.json"
                    settings.write_text(
                        json.dumps(
                            {
                                "hooks": selected,
                                "autoCompactEnabled": True,
                                "precomputeCompactionEnabled": False,
                            }
                        )
                    )
                    environment = {
                        key: value
                        for key, value in os.environ.items()
                        if not key.startswith(
                            ("ANTHROPIC_", "CLAUDE_", "CLAUDEX_", "ORICHUM_")
                        )
                        and key not in {"DISABLE_COMPACT", "DISABLE_AUTO_COMPACT"}
                    }
                    environment.update(
                        HOME=str(root),
                        CLAUDE_CONFIG_DIR=str(root / "claude"),
                        CLAUDEX_RUN_DIR=str(root),
                        CLAUDEX_WORKFLOW_ROOT=str(ROOT),
                        ORICHUM_PYTHON=sys.executable,
                        ANTHROPIC_AUTH_TOKEN="local-fixture",
                        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{proxy.port}",
                        ANTHROPIC_CUSTOM_HEADERS="X-Orichum-Session-ID: oc-s-0000000000000001",
                        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                        CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000",
                    )
                    screen = interactive_run(
                        [
                            os.environ["ORICHUM_NATIVE_CLAUDE"],
                            HANDOFF,
                            "--model",
                            model,
                            "--tools",
                            "Bash",
                            "--allowedTools",
                            "Bash",
                            "--settings",
                            str(settings),
                            "--setting-sources",
                            "user",
                            "--strict-mcp-config",
                            "--mcp-config",
                            '{"mcpServers":{}}',
                            "--system-prompt",
                            "Run only the numbered local fixture steps.",
                            "--debug-file",
                            str(root / "debug.log"),
                        ],
                        root,
                        environment,
                        state,
                        max(90, turns * 4),
                    )
                    events = list(proxy.events)
            finally:
                upstream.shutdown()
                upstream.server_close()
                worker.join()
            diagnostic = "\n".join(
                line[:400]
                for line in (root / "debug.log").read_text().splitlines()
                if "compact" in line.lower() and "output budget" not in line
            )[-5000:]
            self.assertFalse(state["failed_tools"], str(state))
            self.assertEqual(state["summaries_without_hook"], 0)
            if block_compaction:
                self.assertTrue(state.get("blocked"), diagnostic)
                self.assertEqual(state["issued"], 8)
                self.assertEqual(state["compactions"], 0, str(state) + diagnostic)
                self.assertFalse(state["fallback_injected"])
                (counter,) = (root / "context-state").glob("*.json")
                self.assertEqual(json.loads(counter.read_text())["attempts"], 2)
                self.assertIn(
                    "Orichum stopped repeated automatic compaction", diagnostic
                )
                return
            self.assertTrue(state.get("finished"), screen[-2000:] + diagnostic)
            self.assertEqual(
                {path.name: path.read_text() for path in root.glob("step-*")},
                {f"step-{number}": "done" for number in range(1, turns + 1)},
                str(state) + diagnostic,
            )
            self.assertEqual(
                state["completed_tools"],
                {f"tool_{number}" for number in range(1, turns + 1)},
            )
            self.assertGreaterEqual(
                state["compactions"], turns // 8 - 1, str(state) + diagnostic
            )
            self.assertGreater(state["retained_handoffs"], state["compactions"])
            self.assertGreater(state["bounded_results"], turns // 2)
            self.assertEqual(state["wire_models"], {primary, fallback})
            self.assertTrue(
                any(event.get("event") == "route-retry" for event in events)
            )
            (checkpoint,) = root.glob("compaction-checkpoint-*.json")
            self.assertEqual(
                json.loads(checkpoint.read_text())["compactSummary"], HANDOFF
            )
            (counter,) = (root / "context-state").glob("*.json")
            self.assertEqual(json.loads(counter.read_text())["attempts"], 0)
            self.assertTrue((root / "context-artifacts").is_dir())
            print(
                f"Native soak: {turns} tools, {state['compactions']} compactions, "
                "fallback exercised; progress and handoff preserved."
            )
