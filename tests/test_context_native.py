"""Opt-in native Claude hook contract; no provider, credentials or project work.

ORICHUM_NATIVE_CLAUDE=/absolute/path/to/claude python3 -m unittest tests.test_context_native
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from integrations.common.model_context import binding_window, client_model
from integrations.common.orichum_cli import _materialize_session_claudex_config
from tests.test_model_context import binding

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("ORICHUM_NATIVE_CLAUDE"), "opt-in native Claude contract"
)
class NativeContextTests(unittest.TestCase):
    def test_native_bash_output_replacement_preserves_full_artifact(self):
        self.exercise("Bash")

    def test_native_mcp_output_replacement_preserves_full_artifact(self):
        self.exercise("mcp__leanctx__ctx_shell")

    def test_native_per_model_window_and_wire_identity(self):
        for route in (binding(), binding("small-model")):
            self.exercise(
                "Bash",
                model=client_model(route),
                expected_window=binding_window(route),
            )

    def exercise(self, tool, *, model="claude-sonnet-4-6", expected_window=None):
        requests = []
        headers = []
        stream_seconds = (
            float(os.environ.get("ORICHUM_NATIVE_STREAM_SECONDS", "0"))
            if tool == "Bash" and expected_window is None
            else 0
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                document = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                if self.path.startswith("/v1/messages/count_tokens"):
                    payload = b'{"input_tokens":100}'
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                requests.append(document)
                headers.append(dict(self.headers))
                has_result = any(
                    isinstance(message.get("content"), list)
                    and any(
                        block.get("type") == "tool_result"
                        for block in message["content"]
                    )
                    for message in document.get("messages", [])
                )
                message = {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                }
                events = [{"type": "message_start", "message": message}]
                if not has_result:
                    events.extend(
                        [
                            {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {
                                    "type": "tool_use",
                                    "id": "tool_budget",
                                    "name": tool,
                                    "input": {},
                                },
                            },
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": json.dumps(
                                        {
                                            "command": "printf '%040000d' 0",
                                            "description": "Print test fixture",
                                        }
                                    ),
                                },
                            },
                        ]
                    )
                else:
                    events.extend(
                        [
                            {
                                "type": "content_block_start",
                                "index": 0,
                                "content_block": {"type": "text", "text": ""},
                            },
                            {
                                "type": "content_block_delta",
                                "index": 0,
                                "delta": {
                                    "type": "text_delta",
                                    "text": "Native context test complete.",
                                },
                            },
                        ]
                    )
                events.extend(
                    [
                        {"type": "content_block_stop", "index": 0},
                        {
                            "type": "message_delta",
                            "delta": {
                                "stop_reason": "end_turn" if has_result else "tool_use",
                                "stop_sequence": None,
                            },
                            "usage": {"output_tokens": 20},
                        },
                        {"type": "message_stop"},
                    ]
                )
                payload = "".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                    for event in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                if stream_seconds and not has_result:
                    self.wfile.write(
                        f"event: message_start\ndata: {json.dumps(events[0])}\n\n".encode()
                    )
                    # Exercise a progressing response, not five minutes of
                    # heartbeat-only silence. Older native clients correctly
                    # stop streams with no model events despite SSE pings.
                    progress_start = {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    }
                    self.wfile.write(
                        f"event: content_block_start\ndata: {json.dumps(progress_start)}\n\n".encode()
                    )
                    self.wfile.flush()
                    deadline = time.monotonic() + stream_seconds
                    while time.monotonic() < deadline:
                        progress_delta = {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": "."},
                        }
                        self.wfile.write(
                            f"event: content_block_delta\ndata: {json.dumps(progress_delta)}\n\n".encode()
                        )
                        self.wfile.flush()
                        time.sleep(min(1, max(0, deadline - time.monotonic())))
                    self.wfile.write(
                        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
                    )
                    remainder = [
                        {**event, "index": event["index"] + 1}
                        if "index" in event
                        else event
                        for event in events[1:]
                    ]
                    payload = "".join(
                        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                        for event in remainder
                    ).encode()
                self.wfile.write(payload)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            home = root / "home"
            home.mkdir(mode=0o700)
            settings = root / "settings.json"
            hooks = json.loads(
                (ROOT / "controller/plugin/hooks/hooks.json").read_text()
            )
            # Settings hooks have no plugin root. Expand the production wrapper
            # path as the native plugin loader does, retaining the real wrapper.
            for entry in hooks["hooks"]["PostToolUse"]:
                for hook in entry["hooks"]:
                    hook["command"] = hook["command"].replace(
                        "${CLAUDE_PLUGIN_ROOT}", str(ROOT / "controller/plugin")
                    )
            settings.write_text(
                json.dumps({"hooks": {"PostToolUse": hooks["hooks"]["PostToolUse"]}})
            )
            mcp_config = {"mcpServers": {}}
            if tool != "Bash":
                fixture = root / "mcp_fixture.py"
                fixture.write_text("""import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request.get("method")
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "context-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "ctx_shell", "description": "Print test fixture",
                  "inputSchema": {"type": "object", "properties": {
                      "command": {"type": "string"}, "description": {"type": "string"}}}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "0" * 20000}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
                mcp_config["mcpServers"]["leanctx"] = {
                    "command": sys.executable,
                    "args": [str(fixture)],
                }
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            claudex_command = None
            try:
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith(
                        ("ANTHROPIC_", "CLAUDE_", "CLAUDEX_", "ORICHUM_")
                    )
                }
                environment.update(
                    HOME=str(home),
                    CLAUDE_CONFIG_DIR=str(home / "claude"),
                    CLAUDEX_RUN_DIR=str(root),
                    CLAUDEX_WORKFLOW_ROOT=str(ROOT),
                    CLAUDE_PLUGIN_ROOT=str(ROOT / "controller/plugin"),
                    ORICHUM_PYTHON=sys.executable,
                    ANTHROPIC_AUTH_TOKEN="local-test",
                    ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}",
                    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                    CLAUDE_CODE_AUTO_COMPACT_WINDOW="1000000",
                )
                command = [
                    os.environ["ORICHUM_NATIVE_CLAUDE"],
                    "--debug-file",
                    str(root / "debug.log"),
                    "-p",
                    "Print test fixture then stop.",
                    "--model",
                    model,
                    "--tools",
                    "Bash" if tool == "Bash" else "",
                    "--allowedTools",
                    tool,
                    "--settings",
                    str(settings),
                    "--setting-sources",
                    "user",
                    "--no-session-persistence",
                    "--strict-mcp-config",
                    "--mcp-config",
                    json.dumps(mcp_config),
                    "--system-prompt",
                    "Execute only the specified fixture command.",
                    "--max-turns",
                    "2",
                    "--output-format",
                    "json",
                ]
                if claudex := os.environ.get("ORICHUM_NATIVE_CLAUDEX"):
                    shared = root / "shared.toml"
                    shared.write_text(
                        f"claude_binary = {json.dumps(os.environ['ORICHUM_NATIVE_CLAUDE'])}\n"
                        'proxy_port = 13456\nhyperlinks = "off"\n'
                        '[[profiles]]\nname = "gpt"\nprovider_type = "DirectAnthropic"\n'
                        # Any request through Claudex's translation proxy fails:
                        # only the materialized direct transport reaches the fixture.
                        'base_url = "http://127.0.0.1:1"\napi_key = "fixture"\n'
                        'default_model = "claude-sonnet-4-6"\nenabled = true\n'
                        '[profiles.custom_headers]\nX-Orichum-Session-ID = "unbound"\n'
                    )
                    shared.chmod(0o600)
                    with socket.socket() as reservation:
                        reservation.bind(("127.0.0.1", 0))
                        proxy_port = reservation.getsockname()[1]
                    config = _materialize_session_claudex_config(
                        shared,
                        SimpleNamespace(
                            logical=SimpleNamespace(id="oc-s-0000000000000001"),
                            physical=SimpleNamespace(run_dir=root),
                        ),
                        proxy_port,
                        {"HOME": str(home)},
                        route_proxy_port=server.server_port,
                    )
                    claudex_command = [claudex, "--config", str(config)]
                    command = claudex_command + ["run", "gpt"] + command[1:]
                    environment["CLAUDEX_CONFIG_FILE"] = str(config)
                result = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=stream_seconds + 45,
                )
            finally:
                if claudex_command is not None:
                    subprocess.run(
                        claudex_command + ["proxy", "stop"],
                        env=environment,
                        capture_output=True,
                        timeout=10,
                    )
                server.shutdown()
                server.server_close()
                worker.join()
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            self.assertTrue(
                all(
                    request["model"] == model.removesuffix("[1m]")
                    for request in requests
                )
            )
            if expected_window is not None:
                output = json.loads(result.stdout)
                self.assertEqual(
                    output["modelUsage"][model]["contextWindow"], expected_window
                )
            outputs = [
                block
                for request in requests
                for message in request.get("messages", [])
                if isinstance(message.get("content"), list)
                for block in message["content"]
                if block.get("type") == "tool_result"
            ]
            self.assertTrue(outputs, result.stdout[-2000:])
            if claudex_command is not None:
                self.assertTrue(
                    all(
                        item.get("X-Orichum-Session-ID") == "oc-s-0000000000000001"
                        for item in headers
                    )
                )
            self.assertTrue(
                "Orichum output budget" in json.dumps(outputs[-1]),
                "Output hook did not replace the result. "
                + (root / "debug.log").read_text()[-4000:],
            )
            self.assertLess(len(json.dumps(outputs[-1])), 10000)
            (artifact,) = (root / "context-artifacts").iterdir()
            original = json.loads(artifact.read_text())
            if tool == "Bash":
                # Native Bash spools >30k before PostToolUse; preserve both the
                # received response and its reference to complete native output.
                self.assertEqual(len(original["stdout"]), 30000)
                self.assertEqual(
                    len(Path(original["persistedOutputPath"]).read_text()), 40000
                )
            else:
                content = (
                    original.get("content") if isinstance(original, dict) else original
                )
                self.assertEqual(len(content[0]["text"]), 20000)
