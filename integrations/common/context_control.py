"""Bound shell results and compaction retries without editing conversation history.

Invoked by Claude Code hooks. State is keyed by the native conversation ID,
not the logical route ID shared by background forks. Original tool responses
are saved before any replacement is returned. Hook failures leave output intact.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

MAX_INPUT = 16 * 1024 * 1024
OUTPUT_BUDGET = 8000
MAX_ATTEMPTS = 2
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Keep this module parseable by the bootstrap/test host's Python 3.10+ too.
HOOK_ERRORS = (OSError, ValueError, KeyError, RecursionError)


def _private(value: os.stat_result, *, directory: bool = False) -> None:
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(value.st_mode)
        or value.st_uid != os.getuid()
        or stat.S_IMODE(value.st_mode) & 0o077
    ):
        raise ValueError("context state must be private and user-owned")


@contextlib.contextmanager
def _directory(path: Path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _private(os.fstat(descriptor), directory=True)
        yield descriptor
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _child_directory(parent: int, name: str):
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(
        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
    )
    try:
        _private(os.fstat(descriptor), directory=True)
        yield descriptor
    finally:
        os.close(descriptor)


def _read(parent: int, name: str, limit: int) -> bytes:
    descriptor = os.open(
        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
    )
    with os.fdopen(descriptor, "rb") as stream:
        _private(os.fstat(stream.fileno()))
        payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise ValueError("context state is too large")
        return payload


def _create(parent: int, name: str, payload: bytes) -> None:
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _store(parent: int, name: str, payload: bytes) -> None:
    temporary = f".{name}.{os.getpid()}.{time.monotonic_ns()}"
    _create(parent, temporary, payload)
    try:
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass


def _output(event: dict, root: Path, directory: int) -> dict:
    tool = event.get("tool_name")
    if tool not in {"Bash", "mcp__leanctx__ctx_shell"}:
        return {}
    response = event.get("tool_response")
    result = copy.deepcopy(response)
    slots = []
    if tool == "Bash" and isinstance(result, dict):
        if result.get("interrupted") or result.get("isImage"):
            return {}
        slots = [
            (result, key)
            for key in ("stdout", "stderr")
            if isinstance(result.get(key), str)
        ]
    elif tool == "mcp__leanctx__ctx_shell":
        # MCP hooks can receive a content array or a complete CallToolResult.
        if isinstance(result, dict) and result.get("isError"):
            return {}
        content = result.get("content") if isinstance(result, dict) else result
        if isinstance(content, list):
            slots = [
                (item, "text")
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
        elif isinstance(result, str):
            holder = {"text": result}
            slots = [(holder, "text")]
    if not slots or sum(len(item[key]) for item, key in slots) <= OUTPUT_BUDGET:
        return {}
    # Avoid changing structured MCP output or mixed binary/resource results;
    # consumers may depend on their complete shape and exact bytes.
    if tool != "Bash" and isinstance(result, dict) and "structuredContent" in result:
        return {}
    if tool != "Bash" and isinstance(content, list) and len(slots) != len(content):
        return {}
    original = json.dumps(response, ensure_ascii=True).encode()
    digest = hashlib.sha256(original).hexdigest()
    filename = f"{digest}.json"
    with _child_directory(directory, "context-artifacts") as artifacts:
        try:
            _create(artifacts, filename, original)
        except FileExistsError:
            if _read(artifacts, filename, MAX_INPUT * 6) != original:
                raise ValueError("context artifact identity mismatch")
    path = root / "context-artifacts" / filename
    notice = (
        f"\n[Orichum output budget: INCOMPLETE excerpt. The tool already ran. "
        f"Full tool_response JSON: {path}. SHA-256: {digest}. "
        "Read only relevant portions; do not rerun a state-changing command "
        "or infer success from omitted output.]\n"
    )
    # Bound aggregate text, including notices, not each block independently.
    if len(slots) * (len(notice) + 40) > OUTPUT_BUDGET:
        return {}
    allowance = OUTPUT_BUDGET // len(slots) - len(notice)
    for item, key in slots:
        value = item[key]
        if len(value) > allowance:
            half = allowance // 2
            item[key] = value[:half] + notice + value[-half:]
    if isinstance(result, str):
        result = holder["text"]
    if tool == "Bash":
        # Native Bash may have already spooled large stdout. Its renderer prefers
        # this metadata over stdout, which would hide our bounded replacement.
        # The unmodified metadata remains in the full response artifact above.
        result.pop("persistedOutputPath", None)
        result.pop("persistedOutputSize", None)
    field = "updatedToolOutput" if tool == "Bash" else "updatedMCPToolOutput"
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", field: result}}


def handle(event: dict, root: Path) -> dict:
    session = event.get("session_id")
    if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
        return {}
    kind = event.get("hook_event_name")
    with _directory(root) as directory:
        if kind == "PostToolUse":
            return _output(event, root, directory)
        if kind not in {"PreCompact", "PostCompact", "UserPromptSubmit"}:
            return {}
        if kind != "UserPromptSubmit" and event.get("trigger") not in {
            "manual",
            "auto",
        }:
            return {}
        with _child_directory(directory, "context-state") as states:
            lock = os.open(
                f"{session}.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                0o600,
                dir_fd=states,
            )
            try:
                _private(os.fstat(lock))
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                name = f"{session}.json"
                try:
                    state = json.loads(_read(states, name, 65536))
                except FileNotFoundError:
                    state = {}
                if not isinstance(state, dict):
                    raise ValueError("invalid compaction state")
                attempts = state.get("attempts", 0)
                if type(attempts) is not int or not 0 <= attempts <= MAX_ATTEMPTS:
                    raise ValueError("invalid compaction attempt count")
                if kind == "UserPromptSubmit":
                    # Only new user intent (not a tool turn) opens another cycle.
                    state["attempts"] = 0
                elif kind == "PostCompact":
                    summary = event.get("compact_summary")
                    if not isinstance(summary, str) or not summary:
                        return {}
                    state.update(
                        attempts=0,
                        lastSuccess=time.time(),
                        summaryCharacters=len(summary),
                    )
                else:
                    if event["trigger"] == "auto" and attempts >= MAX_ATTEMPTS:
                        return {
                            "decision": "block",
                            "reason": (
                                "Orichum stopped repeated automatic compaction after two "
                                "attempts without a successful checkpoint. Your conversation "
                                f"is preserved (Claude session {session}). No project action "
                                "was replayed. Use /compact with a concise handoff request "
                                "to retry explicitly; inspect orichum doctor if it fails."
                            ),
                        }
                    state.update(
                        attempts=min(attempts + 1, MAX_ATTEMPTS),
                        lastAttempt=time.time(),
                        trigger=event["trigger"],
                    )
                _store(states, name, json.dumps(state).encode())
            finally:
                os.close(lock)
    return {}


def main() -> None:
    try:
        payload = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(payload) > MAX_INPUT:
            return
        event = json.loads(payload)
        if not isinstance(event, dict):
            return
        root = Path(os.environ["CLAUDEX_RUN_DIR"])
        if not root.is_absolute():
            return
        result = handle(event, root)
        if result:
            print(json.dumps(result))
    except HOOK_ERRORS:
        # A hook must never lose output or obstruct work when storage is unsafe.
        print(
            "Orichum context guard unavailable; original output retained.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
