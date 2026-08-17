#!/usr/bin/env python3
"""Project-aware monitoring for Orichum-managed LeanCTX sessions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import http.client
import json
import math
import os
from pathlib import Path
import re
import selectors
import socket
import stat
import subprocess
import tempfile
import time
from typing import Mapping, Sequence

from .session_config import (
    SessionError,
    require_owned_component,
    require_private_direct_child,
    verify_context_binding,
    verify_leanctx_attachment,
)


_RUN_ID = re.compile(r"^run\.[A-Za-z0-9_-]+$")
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_EVENTS_BYTES = 64 * 1024 * 1024
_MAX_LEDGER_BYTES = 64 * 1024 * 1024
_MAX_COMMAND_JSON_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 5.0


class LeanctxMonitorError(RuntimeError):
    """LeanCTX monitoring state cannot be resolved safely."""


@dataclass(frozen=True)
class LeanctxRun:
    run_id: str
    run_dir: Path
    project_root: Path
    created_at: str
    has_activity: bool
    attached: bool = True
    created_at_ns: int = 0


@dataclass(frozen=True)
class LeanctxStats:
    total_commands: int
    input_tokens: int
    output_tokens: int
    saved_tokens: int
    savings_percent: float


@dataclass(frozen=True)
class LeanctxProxyStats:
    requests_total: int
    requests_compressed: int
    bytes_original: int
    bytes_compressed: int
    saved_tokens: int
    savings_percent: float


@dataclass(frozen=True)
class LeanctxRollingEconomics:
    hours: int
    compression_events: int
    caching_events: int
    source_tokens: int
    returned_tokens: int
    saved_tokens: int
    cache_read_tokens: int
    compression_saved_usd: float
    cache_saved_usd: float
    compression_percent: float


@dataclass(frozen=True)
class LeanctxToolHealth:
    advertised_tools: int
    tool_schema_tokens: int
    instruction_tokens: int
    rules_tokens: int
    fixed_total_tokens: int
    total_recorded_calls: int
    tools: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class LeanctxGainSummary:
    total_commands: int
    input_tokens: int
    output_tokens: int
    tokens_saved: int
    gain_rate_percent: float
    injected_overhead_tokens_per_turn: int
    turns: int
    injected_overhead_total_tokens: int
    net_tokens_saved: int
    avoided_usd: float
    tool_spend_usd: float
    roi: float | None


def _private_json(path: Path) -> tuple[dict[str, object], os.stat_result]:
    observed = os.lstat(path)
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
    ):
        raise LeanctxMonitorError("session manifest is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        current = os.fstat(descriptor)
        if (current.st_dev, current.st_ino) != (
            observed.st_dev,
            observed.st_ino,
        ):
            raise LeanctxMonitorError("session manifest changed")
        payload = os.read(descriptor, _MAX_MANIFEST_BYTES + 1)
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise LeanctxMonitorError("session manifest is too large")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LeanctxMonitorError("session manifest is invalid") from error
    if not isinstance(document, dict):
        raise LeanctxMonitorError("session manifest is invalid")
    return document, observed


def _manifest_digests(document: dict[str, object]) -> tuple[str, str]:
    if set(document) != {
        "schemaVersion",
        "contextSha256",
        "effectiveModelsSha256",
    } or document.get("schemaVersion") != 1:
        raise LeanctxMonitorError("session manifest is invalid")
    context = document.get("contextSha256")
    effective = document.get("effectiveModelsSha256")
    for digest in (context, effective):
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise LeanctxMonitorError("session manifest is invalid")
    return context, effective


def _event_payload(directory: Path) -> bytes:
    path = directory / "events.jsonl"
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return b""
    except OSError as error:
        raise LeanctxMonitorError(
            "LeanCTX statistics are unavailable; run 'orichum doctor'"
        ) from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) & 0o022
        or observed.st_size > _MAX_EVENTS_BYTES
    ):
        raise LeanctxMonitorError(
            "LeanCTX statistics are invalid; run 'orichum doctor'"
        )
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (
                (current.st_dev, current.st_ino)
                != (observed.st_dev, observed.st_ino)
                or current.st_size != observed.st_size
            ):
                raise LeanctxMonitorError(
                    "LeanCTX statistics changed while reading"
                )
            chunks = []
            remaining = current.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise LeanctxMonitorError(
                        "LeanCTX statistics changed while reading"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise LeanctxMonitorError(
            "LeanCTX statistics are unavailable; run 'orichum doctor'"
        ) from error


def _event_counters(payload: bytes) -> tuple[int, int, int]:
    commands = 0
    input_tokens = 0
    saved = 0
    try:
        for raw_line in payload.splitlines():
            if not raw_line.strip():
                continue
            document = json.loads(raw_line)
            kind = document.get("kind") if isinstance(document, dict) else None
            if not isinstance(kind, dict) or kind.get("type") != "ToolCall":
                continue
            original = kind.get("tokens_original")
            reduced = kind.get("tokens_saved")
            if (
                type(original) is not int
                or type(reduced) is not int
                or original < 0
                or reduced < 0
                or reduced > original
            ):
                raise ValueError("invalid token counters")
            commands += 1
            input_tokens += original
            saved += reduced
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as error:
        raise LeanctxMonitorError(
            "LeanCTX statistics are invalid; run 'orichum doctor'"
        ) from error
    return commands, input_tokens, saved


def _activity_exists(directory: Path) -> bool:
    commands, _, _ = _event_counters(_event_payload(directory))
    return commands > 0


def _verified_run(
    workflow_root: Path,
    data_root: Path,
    sessions: Path,
    candidate: Path,
) -> LeanctxRun | None:
    run_dir = require_private_direct_child(sessions, candidate)
    manifest_path = run_dir / ".complete"
    try:
        os.lstat(manifest_path)
    except FileNotFoundError:
        return None
    document, manifest_stat = _private_json(manifest_path)
    context_digest, _ = _manifest_digests(document)
    binding = verify_context_binding(
        workflow_root,
        run_dir,
        run_dir / "context.json",
        context_digest,
        run_dir.name,
        data_root,
    )
    context = binding.context
    route = context.get("route") if isinstance(context, dict) else None
    repository = (
        context.get("repoRootReal") if isinstance(context, dict) else None
    )
    project = (
        repository
        if isinstance(repository, str) and repository
        else route.get("contextRootReal")
        if isinstance(route, dict) and route.get("scope") in {None, "context"}
        else context.get("launchDirReal")
        if isinstance(route, dict) and route.get("scope") == "normal"
        else None
    )
    if not isinstance(project, str) or not project:
        raise LeanctxMonitorError("session project context is invalid")
    project_root = Path(project)
    if not project_root.is_absolute():
        raise LeanctxMonitorError("session project context is invalid")
    created_at = datetime.fromtimestamp(
        manifest_stat.st_mtime,
        tz=timezone.utc,
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    unattached = LeanctxRun(
        run_id=binding.run_id,
        run_dir=binding.run_dir,
        project_root=project_root.resolve(strict=False),
        created_at=created_at,
        has_activity=False,
        attached=False,
        created_at_ns=manifest_stat.st_mtime_ns,
    )
    try:
        os.lstat(run_dir / "leanctx")
    except FileNotFoundError:
        return unattached
    try:
        os.lstat(run_dir / "leanctx" / "config" / "config.toml")
    except FileNotFoundError:
        return unattached
    try:
        leanctx = verify_leanctx_attachment(run_dir)
    except SessionError as error:
        if "does not match its contract" in str(error):
            return unattached
        raise LeanctxMonitorError(
            "LeanCTX attachment is unavailable or unsafe"
        ) from error
    return LeanctxRun(
        run_id=binding.run_id,
        run_dir=binding.run_dir,
        project_root=project_root.resolve(strict=False),
        created_at=created_at,
        has_activity=_activity_exists(leanctx / "state"),
        created_at_ns=manifest_stat.st_mtime_ns,
    )


def discover_runs(
    workflow_root: Path,
    data_root: Path,
) -> tuple[LeanctxRun, ...]:
    """Discover complete, verified LeanCTX runs newest first."""
    state_path = data_root / "state"
    sessions_path = state_path / "sessions"
    if not os.path.lexists(state_path) or not os.path.lexists(sessions_path):
        return ()
    try:
        state = require_owned_component(data_root, "state", private=True)
        sessions = require_owned_component(state, "sessions", private=True)
        runs = []
        for candidate in sorted(sessions.iterdir(), key=lambda path: path.name):
            if not candidate.name.startswith("run."):
                continue
            run = _verified_run(
                workflow_root,
                data_root,
                sessions,
                candidate,
            )
            if run is not None:
                runs.append(run)
    except LeanctxMonitorError:
        raise
    except (SessionError, OSError) as error:
        raise LeanctxMonitorError(
            "completed LeanCTX run is invalid"
        ) from error
    return tuple(
        sorted(
            runs,
            key=lambda run: (
                run.created_at_ns,
                run.created_at,
                run.run_id,
            ),
            reverse=True,
        )
    )


def select_run(
    runs: Sequence[LeanctxRun],
    project_root: Path | None,
    run_id: str | None,
    current_run_id: str | None = None,
) -> LeanctxRun:
    """Select an explicit, current, or newest run for a project."""
    if run_id is not None:
        if not _RUN_ID.fullmatch(run_id):
            raise LeanctxMonitorError("run identifier is invalid")
        for run in runs:
            if run.run_id == run_id:
                return run
        raise LeanctxMonitorError(f"LeanCTX run {run_id} was not found")
    if project_root is None:
        raise LeanctxMonitorError(
            "current directory is not mapped to an Orichum project"
        )
    expected = Path(project_root).resolve(strict=False)
    matches = tuple(run for run in runs if run.project_root == expected)
    if matches:
        if current_run_id is not None:
            for run in matches:
                if run.run_id == current_run_id:
                    return run
        return max(
            matches,
            key=lambda run: (
                run.created_at_ns,
                run.created_at,
                run.run_id,
            ),
        )
    raise LeanctxMonitorError(
        "current project has no LeanCTX activity; "
        "run 'orichum leanctx list' to inspect available runs"
    )


def require_attached(run: LeanctxRun) -> None:
    if not run.attached:
        raise LeanctxMonitorError(
            f"LeanCTX is not attached to run {run.run_id}; "
            "rerun install.sh and start a new Orichum session"
        )


def leanctx_environment(
    run: LeanctxRun,
    base: Mapping[str, str] | None = None,
    config_dir: Path | None = None,
) -> dict[str, str]:
    """Build the fixed LeanCTX store environment for one run."""
    environment = dict(os.environ if base is None else base)
    directory = run.run_dir / "leanctx"
    data_home = run.run_dir.parents[2] / "leanctx"
    environment.update(
        {
            "LEAN_CTX_CACHE_DIR": str(data_home / "cache"),
            "LEAN_CTX_CONFIG_DIR": str(config_dir or directory / "config"),
            "LEAN_CTX_DATA_DIR": str(data_home / "lean-ctx"),
            "LEAN_CTX_PROJECT_ROOT": str(run.project_root),
            "LEAN_CTX_RULES_INJECTION": "off",
            "LEAN_CTX_STATE_DIR": str(directory / "state"),
            "XDG_DATA_HOME": str(data_home),
        }
    )
    return environment


def read_stats(
    binary: Path,
    run: LeanctxRun,
    base: Mapping[str, str] | None = None,
) -> LeanctxStats:
    """Read token reduction from one physical run's verified event stream."""
    del binary, base
    commands, input_tokens, saved = _event_counters(
        _event_payload(run.run_dir / "leanctx" / "state")
    )
    output_tokens = input_tokens - saved
    percent = saved / input_tokens * 100.0 if input_tokens else 0.0
    return LeanctxStats(
        total_commands=commands,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        saved_tokens=saved,
        savings_percent=percent,
    )


def _bounded_owned_payload(
    path: Path,
    *,
    label: str,
    maximum_bytes: int,
) -> bytes:
    try:
        observed = os.lstat(path)
    except FileNotFoundError as error:
        raise LeanctxMonitorError(f"{label} is unavailable") from error
    except OSError as error:
        raise LeanctxMonitorError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) & 0o022
    ):
        raise LeanctxMonitorError(f"{label} is unsafe")
    if observed.st_size > maximum_bytes:
        raise LeanctxMonitorError(f"{label} is too large")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                opened.st_uid,
                opened.st_mode,
            ) != (
                observed.st_dev,
                observed.st_ino,
                observed.st_size,
                observed.st_mtime_ns,
                observed.st_ctime_ns,
                observed.st_uid,
                observed.st_mode,
            ):
                raise LeanctxMonitorError(f"{label} changed while opening")
            chunks = []
            remaining = opened.st_size
            while remaining:
                chunk = os.read(descriptor, min(1024 * 1024, remaining))
                if not chunk:
                    raise LeanctxMonitorError(f"{label} changed while reading")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            if (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_uid,
                after.st_mode,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                opened.st_uid,
                opened.st_mode,
            ):
                raise LeanctxMonitorError(f"{label} changed while reading")
            current_path = os.lstat(path)
            if (
                current_path.st_dev,
                current_path.st_ino,
                current_path.st_size,
                current_path.st_mtime_ns,
                current_path.st_ctime_ns,
                current_path.st_uid,
                current_path.st_mode,
            ) != (
                opened.st_dev,
                opened.st_ino,
                opened.st_size,
                opened.st_mtime_ns,
                opened.st_ctime_ns,
                opened.st_uid,
                opened.st_mode,
            ):
                raise LeanctxMonitorError(f"{label} changed while reading")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except LeanctxMonitorError:
        raise
    except OSError as error:
        raise LeanctxMonitorError(f"{label} is unavailable") from error


def _strict_json_object(payload: bytes) -> dict[str, object]:
    def object_from_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    def reject_constant(value: str) -> object:
        raise ValueError(f"invalid JSON constant: {value}")

    document = json.loads(
        payload,
        object_pairs_hook=object_from_pairs,
        parse_constant=reject_constant,
    )
    if not isinstance(document, dict):
        raise ValueError("JSON object required")
    return document


def _ledger_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("invalid ledger timestamp")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("invalid ledger timestamp")
    return timestamp.astimezone(timezone.utc)


def _nonnegative_integer(document: Mapping[str, object], field: str) -> int:
    value = document.get(field)
    if type(value) is not int or value < 0:
        raise ValueError(f"invalid {field}")
    return value


def _nonnegative_finite(document: Mapping[str, object], field: str) -> float:
    value = document.get(field)
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"invalid {field}")
    return number


def _finite_number(document: Mapping[str, object], field: str) -> float:
    value = document.get(field)
    if type(value) not in (int, float) or isinstance(value, bool):
        raise ValueError(f"invalid {field}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"invalid {field}")
    return number


def read_rolling_economics(
    data_root: Path,
    hours: int,
    *,
    now: datetime | None = None,
) -> LeanctxRollingEconomics:
    """Aggregate timestamped compression and cache estimates."""
    if type(hours) is not int or not 1 <= hours <= 168:
        raise LeanctxMonitorError("hours must be between 1 and 168")
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None or current.utcoffset() is None:
        raise LeanctxMonitorError("current time must include a timezone")
    current = current.astimezone(timezone.utc)
    cutoff = current - timedelta(hours=hours)
    payload = _bounded_owned_payload(
        data_root / "leanctx" / "lean-ctx" / "savings" / "ledger.jsonl",
        label="LeanCTX savings ledger",
        maximum_bytes=_MAX_LEDGER_BYTES,
    )
    compression_events = 0
    caching_events = 0
    source_tokens = 0
    returned_tokens = 0
    saved_tokens = 0
    cache_read_tokens = 0
    compression_saved_usd = 0.0
    cache_saved_usd = 0.0
    try:
        for raw_line in payload.splitlines():
            if not raw_line.strip():
                continue
            document = _strict_json_object(raw_line)
            timestamp = _ledger_timestamp(document.get("ts"))
            mechanism = document.get("mechanism")
            if mechanism not in {"compression", "caching"}:
                raise ValueError("invalid ledger mechanism")
            tool = document.get("tool")
            if not isinstance(tool, str) or not tool:
                raise ValueError("invalid ledger tool")
            baseline = _nonnegative_integer(document, "baseline_tokens")
            actual = _nonnegative_integer(document, "actual_tokens")
            saved = _nonnegative_integer(document, "saved_tokens")
            bounce_adjustment = _nonnegative_integer(
                document,
                "bounce_adjustment",
            )
            _nonnegative_finite(document, "unit_price_per_m_usd")
            estimated_usd = _finite_number(document, "saved_usd")
            is_bounce = tool == "bounce"
            if is_bounce and not (
                mechanism == "compression"
                and actual == baseline
                and saved == 0
                and 0 < bounce_adjustment <= baseline
                and estimated_usd < 0
            ):
                raise ValueError("invalid bounce correction")
            if not is_bounce and estimated_usd < 0:
                raise ValueError("invalid negative savings")
            if (
                actual > baseline
                or saved > baseline
                or (
                    mechanism == "compression"
                    and actual + saved != baseline
                )
                or (
                    mechanism == "caching"
                    and (actual != baseline or saved != 0)
                )
            ):
                raise ValueError("invalid ledger token relationship")
            if not cutoff <= timestamp <= current:
                continue
            if mechanism == "compression":
                compression_events += 1
                source_tokens += baseline
                returned_tokens += actual
                saved_tokens += saved
                compression_saved_usd += estimated_usd
            else:
                caching_events += 1
                cache_read_tokens += actual
                cache_saved_usd += estimated_usd
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        raise LeanctxMonitorError("LeanCTX savings ledger is invalid") from error
    if not (
        math.isfinite(compression_saved_usd)
        and math.isfinite(cache_saved_usd)
    ):
        raise LeanctxMonitorError("LeanCTX savings ledger is invalid")
    compression_percent = (
        saved_tokens / source_tokens * 100.0 if source_tokens else 0.0
    )
    return LeanctxRollingEconomics(
        hours=hours,
        compression_events=compression_events,
        caching_events=caching_events,
        source_tokens=source_tokens,
        returned_tokens=returned_tokens,
        saved_tokens=saved_tokens,
        cache_read_tokens=cache_read_tokens,
        compression_saved_usd=compression_saved_usd,
        cache_saved_usd=cache_saved_usd,
        compression_percent=compression_percent,
    )


def _managed_json(
    binary: Path,
    run: LeanctxRun,
    arguments: Sequence[str],
    *,
    label: str,
) -> dict[str, object]:
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
    try:
        process = subprocess.Popen(
            [str(binary), *arguments],
            env=leanctx_environment(run),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
        )
        if process.stdout is None:
            raise LeanctxMonitorError(f"{label} is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + _COMMAND_TIMEOUT_SECONDS
        captured = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise subprocess.TimeoutExpired(
                    [str(binary), *arguments],
                    _COMMAND_TIMEOUT_SECONDS,
                )
            chunk = os.read(
                process.stdout.fileno(),
                min(
                    64 * 1024,
                    _MAX_COMMAND_JSON_BYTES + 1 - len(captured),
                ),
            )
            if not chunk:
                break
            captured.extend(chunk)
            if len(captured) > _MAX_COMMAND_JSON_BYTES:
                raise LeanctxMonitorError(f"{label} is unavailable")
        remaining = max(0.0, deadline - time.monotonic())
        if process.wait(timeout=remaining) != 0:
            raise LeanctxMonitorError(f"{label} is unavailable")
        payload = bytes(captured)
    except LeanctxMonitorError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise LeanctxMonitorError(f"{label} is unavailable") from error
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.SubprocessError:
                    pass
            if process.stdout is not None:
                process.stdout.close()
    try:
        return _strict_json_object(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        OverflowError,
        TypeError,
        ValueError,
    ) as error:
        raise LeanctxMonitorError(f"{label} is invalid") from error


def read_tool_health(binary: Path, run: LeanctxRun) -> LeanctxToolHealth:
    """Read the selected run's strict LeanCTX tool-footprint summary."""
    label = "LeanCTX tool health"
    document = _managed_json(
        binary,
        run,
        ("tools", "health", "--json"),
        label=label,
    )
    try:
        advertised_tools = _nonnegative_integer(document, "advertised_tools")
        tool_schema_tokens = _nonnegative_integer(
            document,
            "tool_schema_tokens",
        )
        instruction_tokens = _nonnegative_integer(
            document,
            "instruction_tokens",
        )
        rules_tokens = _nonnegative_integer(document, "rules_tokens")
        fixed_total_tokens = _nonnegative_integer(
            document,
            "fixed_total_tokens",
        )
        total_recorded_calls = _nonnegative_integer(
            document,
            "total_recorded_calls",
        )
        rows = document.get("tools")
        if not isinstance(rows, list):
            raise ValueError("invalid tools")
        tools = []
        names = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid tool row")
            name = row.get("name")
            if (
                not isinstance(name, str)
                or re.fullmatch(r"ctx_[a-z0-9_]+", name) is None
                or name in names
            ):
                raise ValueError("invalid tool name")
            names.add(name)
            tools.append(
                (
                    name,
                    _nonnegative_integer(row, "schema_tokens"),
                    _nonnegative_integer(row, "calls"),
                )
            )
        if (
            advertised_tools != len(tools)
            or tool_schema_tokens != sum(row[1] for row in tools)
            or total_recorded_calls != sum(row[2] for row in tools)
            or fixed_total_tokens
            != tool_schema_tokens + instruction_tokens + rules_tokens
        ):
            raise ValueError("inconsistent tool health totals")
    except (TypeError, ValueError) as error:
        raise LeanctxMonitorError(f"{label} is invalid") from error
    return LeanctxToolHealth(
        advertised_tools=advertised_tools,
        tool_schema_tokens=tool_schema_tokens,
        instruction_tokens=instruction_tokens,
        rules_tokens=rules_tokens,
        fixed_total_tokens=fixed_total_tokens,
        total_recorded_calls=total_recorded_calls,
        tools=tuple(tools),
    )


def read_gain_summary(binary: Path, run: LeanctxRun) -> LeanctxGainSummary:
    """Read LeanCTX's official all-time upstream gain estimate."""
    label = "LeanCTX gain summary"
    document = _managed_json(
        binary,
        run,
        ("gain", "--json"),
        label=label,
    )
    try:
        summary = document.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("invalid summary")
        total_commands = _nonnegative_integer(summary, "total_commands")
        input_tokens = _nonnegative_integer(summary, "input_tokens")
        output_tokens = _nonnegative_integer(summary, "output_tokens")
        tokens_saved = _nonnegative_integer(summary, "tokens_saved")
        gain_rate_percent = _nonnegative_finite(summary, "gain_rate_pct")
        injected_overhead_tokens_per_turn = _nonnegative_integer(
            summary,
            "injected_overhead_tokens_per_turn",
        )
        turns = _nonnegative_integer(summary, "turns")
        injected_overhead_total_tokens = _nonnegative_integer(
            summary,
            "injected_overhead_total_tokens",
        )
        net_tokens_saved = summary.get("net_tokens_saved")
        if type(net_tokens_saved) is not int:
            raise ValueError("invalid net_tokens_saved")
        avoided_usd = _nonnegative_finite(summary, "avoided_usd")
        tool_spend_usd = _nonnegative_finite(summary, "tool_spend_usd")
        roi_value = summary.get("roi")
        if roi_value is None:
            if tool_spend_usd != 0.0:
                raise ValueError("invalid roi")
            roi = None
        else:
            roi = _nonnegative_finite(summary, "roi")
        if (
            gain_rate_percent > 100.0
            or output_tokens > input_tokens
            or tokens_saved != input_tokens - output_tokens
            or injected_overhead_total_tokens
            != injected_overhead_tokens_per_turn * turns
            or net_tokens_saved
            != tokens_saved - injected_overhead_total_tokens
        ):
            raise ValueError("inconsistent gain summary totals")
    except (TypeError, ValueError) as error:
        raise LeanctxMonitorError(f"{label} is invalid") from error
    return LeanctxGainSummary(
        total_commands=total_commands,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        tokens_saved=tokens_saved,
        gain_rate_percent=gain_rate_percent,
        injected_overhead_tokens_per_turn=(
            injected_overhead_tokens_per_turn
        ),
        turns=turns,
        injected_overhead_total_tokens=injected_overhead_total_tokens,
        net_tokens_saved=net_tokens_saved,
        avoided_usd=avoided_usd,
        tool_spend_usd=tool_spend_usd,
        roi=roi,
    )


def proxy_environment(
    data_root: Path,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the isolated environment of Orichum's shared LeanCTX proxy."""
    environment = dict(os.environ if base is None else base)
    directory = data_root / "leanctx" / "proxy"
    environment.update(
        {
            "LEAN_CTX_CACHE_DIR": str(directory / "cache"),
            "LEAN_CTX_CONFIG_DIR": str(directory / "config"),
            "LEAN_CTX_DATA_DIR": str(data_root / "leanctx" / "lean-ctx"),
            "LEAN_CTX_HEADLESS": "1",
            "LEAN_CTX_MINIMAL": "1",
            "LEAN_CTX_RULES_INJECTION": "off",
            "LEAN_CTX_STATE_DIR": str(directory / "state"),
            "XDG_DATA_HOME": str(data_root / "leanctx"),
        }
    )
    return environment


def read_proxy_stats(
    binary: Path,
    data_root: Path,
    port: int,
) -> LeanctxProxyStats:
    """Read authenticated wire-level statistics from the shared proxy."""
    if type(port) is not int or not 1024 <= port <= 65535:
        raise LeanctxMonitorError("LeanCTX proxy port is invalid")
    try:
        token_result = subprocess.run(
            [str(binary), "proxy", "token"],
            env=proxy_environment(data_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=3,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LeanctxMonitorError(
            "LeanCTX proxy statistics are unavailable; run 'orichum doctor'"
        ) from error
    token = token_result.stdout.strip()
    if (
        token_result.returncode != 0
        or not 32 <= len(token) <= 256
        or re.fullmatch(r"[A-Za-z0-9._~-]+", token) is None
    ):
        raise LeanctxMonitorError(
            "LeanCTX proxy statistics are unavailable; run 'orichum doctor'"
        )
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "GET",
            "/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        response = connection.getresponse()
        payload = response.read(1024 * 1024 + 1)
    except (OSError, http.client.HTTPException) as error:
        raise LeanctxMonitorError(
            "LeanCTX proxy statistics are unavailable; run 'orichum doctor'"
        ) from error
    finally:
        connection.close()
    if response.status != 200 or len(payload) > 1024 * 1024:
        raise LeanctxMonitorError(
            "LeanCTX proxy statistics are unavailable; run 'orichum doctor'"
        )
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise LeanctxMonitorError("LeanCTX proxy statistics are invalid") from error
    if not isinstance(document, dict):
        raise LeanctxMonitorError("LeanCTX proxy statistics are invalid")
    integer_fields = (
        "requests_total",
        "requests_compressed",
        "bytes_original",
        "bytes_compressed",
        "tokens_saved",
    )
    values: dict[str, int] = {}
    for field in integer_fields:
        value = document.get(field)
        if type(value) is not int or value < 0:
            raise LeanctxMonitorError("LeanCTX proxy statistics are invalid")
        values[field] = value
    percent = document.get("compression_ratio_pct")
    if isinstance(percent, str):
        if re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", percent) is None:
            raise LeanctxMonitorError("LeanCTX proxy statistics are invalid")
        savings_percent = float(percent)
    elif type(percent) in (int, float) and not isinstance(percent, bool):
        savings_percent = float(percent)
    else:
        raise LeanctxMonitorError("LeanCTX proxy statistics are invalid")
    if (
        not math.isfinite(savings_percent)
        or not 0.0 <= savings_percent <= 100.0
    ):
        raise LeanctxMonitorError("LeanCTX proxy statistics are invalid")
    return LeanctxProxyStats(
        requests_total=values["requests_total"],
        requests_compressed=values["requests_compressed"],
        bytes_original=values["bytes_original"],
        bytes_compressed=values["bytes_compressed"],
        saved_tokens=values["tokens_saved"],
        savings_percent=savings_percent,
    )


def managed_binary(data_root: Path) -> Path:
    """Resolve the fixed, current-user-owned Orichum LeanCTX executable."""
    try:
        binary_dir = require_owned_component(
            data_root,
            "bin",
            private=True,
        )
        binary = binary_dir / "lean-ctx"
        observed = os.lstat(binary)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o755
            or not os.access(binary, os.X_OK)
        ):
            raise LeanctxMonitorError("managed LeanCTX is unavailable")
        resolved = binary.resolve(strict=True)
        if resolved != binary or resolved.parent != binary_dir:
            raise LeanctxMonitorError("managed LeanCTX is unavailable")
        return resolved
    except (LeanctxMonitorError, SessionError, OSError) as error:
        raise LeanctxMonitorError(
            "managed LeanCTX is unavailable; run 'orichum doctor'"
        ) from error


def _port_is_free(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        probe.close()
    return True


def first_free_loopback_port(start: int = 3333) -> int:
    """Find the first available loopback TCP port at or after start."""
    if type(start) is not int or not 1 <= start <= 65535:
        raise LeanctxMonitorError("dashboard port must be between 1 and 65535")
    for port in range(start, 65536):
        if _port_is_free(port):
            return port
    raise LeanctxMonitorError("no local dashboard port is available")


def run_watch(binary: Path, run: LeanctxRun) -> int:
    """Run LeanCTX's live terminal monitor in the foreground."""
    try:
        completed = subprocess.run(
            [str(binary), "watch"],
            env=leanctx_environment(run),
            check=False,
        )
    except OSError as error:
        raise LeanctxMonitorError(
            "managed LeanCTX is unavailable; run 'orichum doctor'"
        ) from error
    return completed.returncode


def _dashboard_config(run: LeanctxRun) -> bytes:
    path = run.run_dir / "leanctx" / "config" / "config.toml"
    try:
        observed = os.lstat(path)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise LeanctxMonitorError("LeanCTX configuration is unsafe")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            current = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (
                observed.st_dev,
                observed.st_ino,
            ):
                raise LeanctxMonitorError("LeanCTX configuration changed")
            return os.read(descriptor, 1024 * 1024)
        finally:
            os.close(descriptor)
    except (LeanctxMonitorError, OSError) as error:
        raise LeanctxMonitorError(
            "LeanCTX configuration is unavailable; run 'orichum doctor'"
        ) from error


def _private_state_root(state_root: Path) -> Path:
    try:
        observed = os.lstat(state_root)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISDIR(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o700
        ):
            raise LeanctxMonitorError("Orichum state directory is unsafe")
        resolved = state_root.resolve(strict=True)
        if resolved != state_root:
            raise LeanctxMonitorError("Orichum state directory is unsafe")
        return resolved
    except (LeanctxMonitorError, OSError) as error:
        raise LeanctxMonitorError(
            "Orichum state directory is unavailable"
        ) from error


def run_dashboard(
    binary: Path,
    run: LeanctxRun,
    state_root: Path,
    port: int | None,
    open_mode: str,
) -> int:
    """Run the authenticated native dashboard against one session."""
    if open_mode not in {"browser", "none", "vscode"}:
        raise LeanctxMonitorError("dashboard open mode is invalid")
    if port is None:
        selected_port = first_free_loopback_port()
    else:
        if type(port) is not int or not 1 <= port <= 65535:
            raise LeanctxMonitorError(
                "dashboard port must be between 1 and 65535"
            )
        if not _port_is_free(port):
            raise LeanctxMonitorError(
                f"dashboard port is already occupied: {port}; "
                "omit --port to select one automatically"
            )
        selected_port = port
    state_root = _private_state_root(state_root)
    config = _dashboard_config(run)
    try:
        with tempfile.TemporaryDirectory(
            prefix="leanctx-dashboard.",
            dir=state_root,
        ) as temporary:
            config_dir = Path(temporary)
            config_path = config_dir / "config.toml"
            config_path.write_bytes(config)
            config_path.chmod(0o600)
            environment = leanctx_environment(
                run,
                config_dir=config_dir,
            )
            for name in (
                "LEAN_CTX_HTTP_TOKEN",
                "LEAN_CTX_DASHBOARD_ALLOWED_HOSTS",
                "LEAN_CTX_SCRAPE_TOKEN",
            ):
                environment.pop(name, None)
            environment["LEAN_CTX_DASHBOARD_AUTH"] = "true"
            completed = subprocess.run(
                [
                    str(binary),
                    "dashboard",
                    "--host=127.0.0.1",
                    f"--port={selected_port}",
                    f"--open={open_mode}",
                ],
                env=environment,
                check=False,
            )
            return completed.returncode
    except KeyboardInterrupt:
        return 130
    except OSError as error:
        raise LeanctxMonitorError(
            "LeanCTX dashboard could not be started"
        ) from error
