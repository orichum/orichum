#!/usr/bin/env python3
"""Unified Orichum command dispatcher."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from decimal import Decimal, ROUND_HALF_UP
import fcntl
import http.client
from io import StringIO
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence, TextIO
import webbrowser

from . import leanctx_monitor
from .account_registry import (
    Account,
    AccountError,
    account_transaction,
    find_account,
    load_accounts,
    new_account,
    parse_priority,
    update_accounts,
    validate_account_bindings,
)
from .cliproxy_management import (
    ManagementError,
    cancel_oauth,
    load_management_endpoint,
    oauth_status,
    patch_auth_fields,
    start_oauth,
    submit_oauth_callback,
)
from .configure_state import (
    ConfigurationDraft,
    ConfigurationSnapshot,
    build_managed_stack,
    compatible_backup_accounts,
    load_configuration_snapshot,
    managed_stack_name,
    stack_is_live_compatible,
)
from .configure_wizard import run_configure
from .github_identity import GithubIdentityError, ensure_github_identity
from .leanctx_contract import (
    AUTO_APPROVED_TOOLS as LEANCTX_AUTO_APPROVED_TOOLS,
)
from .leanctx_profiles import (
    DEFAULT_LEANCTX_PROFILE,
    LEANCTX_PROFILES,
    resident_tool_names,
)
from .orichum_config import (
    ConfigError,
    ResolvedConfig,
    default_config_paths,
    load_control_plane,
    redact_control_plane,
)
from .orichum_completion import (
    CompletionError,
    render_completion,
    set_completion,
)
from .orichum_sessions import (
    LogicalSession,
    LogicalSessionCleanup,
    LogicalSessionError,
    PhysicalRunCleanup,
    RouteBinding,
    clear_logical_sessions,
    cleanup_physical_runs,
    create_logical_session,
    list_logical_sessions,
    load_logical_session,
    remove_logical_session,
    resolve_logical_session,
    resolve_session_plan,
)
from .orichum_status import main as render_status_main
from .model_routing import EffectiveStack, ROLES, RoutingError
from .project_context import (
    ContextError,
    add_context_commands,
    assign_stack_to_context,
    configure_normal_scope,
    control_plane_transaction,
    resolve_control_plane_context,
)
from .project_models import (
    ProjectModels,
    discover_project_models,
    ensure_project_config,
    resolve_project_context,
)
from .provider_credentials import (
    CredentialError,
    credential_metadata_transaction,
    list_credentials,
    repair_credential_modes,
    resolve_credential_ref,
)
from .route_selection import RouteError, validate_route_credential
from .stack_bindings import (
    StackBindingError,
    StackBindings,
    load_stack_bindings,
    stack_binding_transaction,
)

from .stack_definition import normalize_model_stacks
from .stack_catalog import (
    CatalogError,
    fetch_live_catalog,
    project_live_catalog,
)
from .stack_store import (
    StackStoreError,
    load_stack_snapshot,
    save_stack,
)
from .stack_wizard import create_recommended_stack, run_stack_wizard
from .terminal_ui import UiCancelled
from .session_config import (
    SessionError,
    SessionPaths,
    create_resolved_session,
)


WORKFLOW_ROOT = Path(__file__).resolve().parents[2]
_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "antigravity": "Antigravity",
    "kimi": "Kimi",
    "openai": "OpenAI",
}


class CliError(RuntimeError):
    """An Orichum command cannot be completed safely."""


@dataclass(frozen=True)
class PendingProviderAccount:
    """Authenticated provider credential awaiting account registration."""

    provider: str
    credential_ref: str = field(repr=False)
    suggested_name: str




@dataclass
class SetupDiagnostics:
    path: Path
    verbose: bool
    _handle: TextIO

    @classmethod
    def create(
        cls,
        paths: Mapping[str, Path],
        *,
        verbose: bool,
    ) -> "SetupDiagnostics":
        data_root = Path(paths["data"])
        data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_dir = data_root / "logs"
        try:
            log_dir.mkdir(mode=0o700)
        except FileExistsError:
            pass
        details = os.lstat(log_dir)
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise CliError("Orichum diagnostic log directory is unsafe")
        descriptor, raw_path = tempfile.mkstemp(
            prefix="setup-",
            suffix=".log",
            dir=log_dir,
            text=True,
        )
        os.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        return cls(Path(raw_path), verbose, handle)

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def emit(self, value: str = "") -> None:
        print(value)
        self._handle.write(value + "\n")
        self._handle.flush()

    def sensitive(self, value: str) -> None:
        print(value, flush=True)

    def technical(self, value: str) -> None:
        self._handle.write(value)
        self._handle.flush()
        if self.verbose:
            print(value, end="")

    def run_command(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str] | None = None,
    ) -> int:
        child_environment = os.environ.copy()
        if environment is not None:
            child_environment.update(environment)
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            bufsize=1,
        )
        if process.stdout is None:
            process.kill()
            raise CliError("diagnostic capture is unavailable")
        try:
            while chunk := process.stdout.read(8192):
                self.technical(chunk)
        finally:
            process.stdout.close()
        return process.wait()


def _base_release_version() -> str:
    try:
        version = (WORKFLOW_ROOT / "VERSION").read_text(
            encoding="ascii"
        ).strip()
    except (OSError, UnicodeError):
        return "unknown"
    return (
        version
        if re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)"
            r"(?:-[0-9A-Za-z.-]+)?",
            version,
        )
        else "unknown"
    )


def _runtime_manifest_digest() -> str | None:
    try:
        document = json.loads(
            (WORKFLOW_ROOT / "runtime-manifest.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if type(document) is not dict:
        return None
    digest = document.get("digest")
    if (
        type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        return None
    return digest


def _build_identity(version: str) -> dict[str, object] | None:
    try:
        document = json.loads(
            (WORKFLOW_ROOT / "build-identity.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        type(document) is not dict
        or set(document) != {
            "schemaVersion",
            "version",
            "sourceKind",
            "sourceCommit",
            "dirty",
            "exactTag",
        }
        or type(document["schemaVersion"]) is not int
        or document["schemaVersion"] != 1
        or document["version"] != version
        or type(document["dirty"]) is not bool
        or type(document["exactTag"]) is not bool
    ):
        return None
    source_kind = document["sourceKind"]
    commit = document["sourceCommit"]
    if source_kind == "source":
        return (
            document
            if commit is None
            and not document["dirty"]
            and not document["exactTag"]
            else None
        )
    if (
        source_kind != "git"
        or type(commit) is not str
        or len(commit) not in {40, 64}
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        return None
    return document


def _source_fallback_version(version: str) -> str:
    digest = _runtime_manifest_digest()
    return f"{version}+src.{digest[:12] if digest else 'unknown'}"


def _release_version() -> str:
    version = _base_release_version()
    if version == "unknown":
        return version
    identity = _build_identity(version)
    if identity is None or identity["sourceKind"] == "source":
        return _source_fallback_version(version)
    if identity["exactTag"] and not identity["dirty"]:
        return version
    commit = str(identity["sourceCommit"])
    suffix = f"+g.{commit[:12]}"
    if identity["dirty"]:
        suffix += ".dirty"
    return version + suffix


@dataclass(frozen=True)
class PreparedLaunch:
    logical: LogicalSession
    physical: SessionPaths


def _home(
    environment: Mapping[str, str],
    override: str,
    xdg: str,
    fallback: str,
) -> Path:
    raw = environment.get(override)
    if raw is None:
        base = environment.get(xdg)
        raw = str(Path(base) / "orichum") if base else str(Path.home() / fallback)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise CliError(f"{override} must be an absolute path")
    return path.resolve(strict=False)


def _paths(environment: Mapping[str, str] | None = None) -> dict[str, Path]:
    environment = os.environ if environment is None else environment
    home_raw = environment.get("ORICHUM_HOME")
    if home_raw is None:
        user_home = environment.get("HOME")
        home_raw = str(
            (Path(user_home) if user_home else Path.home()) / ".orichum"
        )
    home = Path(home_raw).expanduser()
    if not home.is_absolute():
        raise CliError("ORICHUM_HOME must be an absolute path")
    home = home.resolve(strict=False)

    data_raw = environment.get("ORICHUM_DATA_HOME")
    data = (
        _home(
            environment,
            "ORICHUM_DATA_HOME",
            "XDG_DATA_HOME",
            ".local/share/orichum",
        )
        if data_raw is not None
        else home
    )
    config = (
        _home(
            environment,
            "ORICHUM_CONFIG_HOME",
            "XDG_CONFIG_HOME",
            ".config/orichum",
        )
        if environment.get("ORICHUM_CONFIG_HOME") is not None
        else home / "config"
    )
    cache = (
        _home(
            environment,
            "ORICHUM_CACHE_HOME",
            "XDG_CACHE_HOME",
            ".cache/orichum",
        )
        if environment.get("ORICHUM_CACHE_HOME") is not None
        else home / "cache"
    )
    return {
        "home": home,
        "config": config,
        "data": data,
        "state": data / "state",
        "cache": cache,
    }


def _load() -> tuple[dict[str, Path], ResolvedConfig]:
    paths = _paths()
    config = load_control_plane(default_config_paths(paths["config"]))
    try:
        installed_policy = (
            paths["config"] / "controller-policy.md"
        ).read_bytes()
        declared_policy = (
            WORKFLOW_ROOT / "config" / "controller-policy.md"
        ).read_bytes()
    except OSError as error:
        raise CliError("controller policy is unavailable") from error
    if installed_policy != declared_policy:
        raise CliError(
            "installed controller policy is stale; rerun install.sh"
        )
    return paths, config


def _render_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    normalized = [tuple(str(value) for value in row) for row in rows]
    widths = [
        max([len(header), *(len(row[index]) for row in normalized)])
        for index, header in enumerate(headers)
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def row(values: Sequence[str]) -> str:
        return (
            "|"
            + "|".join(
                f" {value:<{width}} "
                for value, width in zip(values, widths, strict=True)
            )
            + "|"
        )

    rendered = [border, row(headers), border]
    rendered.extend(row(values) for values in normalized)
    rendered.append(border)
    return "\n".join(rendered) + "\n"


def _config_show(
    config: ResolvedConfig,
    *,
    redacted: bool = True,
) -> dict[str, object]:
    values = redact_control_plane(config) if redacted else dict(config.documents)
    return {
        name: {"source": config.sources[name], "value": values[name]}
        for name in sorted(values)
    }


def _context_list(config: ResolvedConfig) -> str:
    contexts = config.documents["projects"]["contexts"]
    rows = [
        (
            context["root"],
            (
                context["atlassian"]["url"]
                if isinstance(context["atlassian"], dict)
                else "—"
            ),
            context.get("githubAccount") or "—",
            context["modelStack"] or "default",
            ", ".join(context["accountPools"]),
        )
        for context in contexts
    ]
    return _render_table(
        (
            "ROOT",
            "JIRA",
            "GITHUB",
            "MODEL STACK",
            "ACCOUNT POOLS",
        ),
        rows,
    )


def _model_list(config: ResolvedConfig) -> str:
    models = normalize_model_stacks(
        config.documents["model-stacks"]
    ).models
    rows = [
        (
            model,
            ", ".join(metadata.routes),
            metadata.family,
            ", ".join(metadata.routes.values()),
        )
        for model, metadata in sorted(models.items())
    ]
    return _render_table(("MODEL", "PROVIDER", "FAMILY", "UPSTREAM"), rows)


def _stack_list(config: ResolvedConfig) -> str:
    document = normalize_model_stacks(config.documents["model-stacks"])
    default = document.default_stack
    rows = [
        (
            name,
            "yes" if name == default else "—",
            ", ".join(candidate.model for candidate in stack.controller),
        )
        for name, stack in sorted(document.stacks.items())
    ]
    return _render_table(("STACK", "DEFAULT", "CONTROLLER"), rows)


def _stack_available(
    paths: Mapping[str, Path], config: ResolvedConfig
) -> str:
    _verify_runtime(paths)
    ports = _runtime_service_ports(paths)
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    routing = normalize_model_stacks(config.documents["model-stacks"])
    catalog = project_live_catalog(
        fetch_live_catalog(ports["cliproxyPort"]),
        accounts,
        routing.models,
        config.documents["providers"],
    )
    rows = [
        (
            choice.provider,
            choice.family,
            choice.upstream,
            ", ".join(choice.account_names),
            "selectable",
        )
        for choice in catalog.choices
    ]
    rows.extend(
        (
            model.provider,
            "unclassified",
            model.upstream,
            ", ".join(model.account_names),
            "not selectable",
        )
        for model in catalog.unclassified
    )
    return _render_table(
        ("PROVIDER", "FAMILY", "MODEL", "ACCOUNTS", "STATUS"),
        sorted(rows, key=lambda row: (row[0], row[1], row[2], row[3])),
    )


def _stack_show(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    name: str,
) -> str:
    document = normalize_model_stacks(config.documents["model-stacks"])
    stack = document.stacks.get(name)
    if stack is None:
        available = ", ".join(sorted(document.stacks))
        raise CliError(
            f"model stack is not configured: {name}; "
            f"available stacks: {available}"
        )
    binding_path = paths["config"] / "stack-bindings.json"
    bindings = (
        load_stack_bindings(binding_path)
        if binding_path.exists()
        else StackBindings({})
    )
    locked_ids = set(bindings.candidate_accounts.values())
    accounts = (
        {
            account.id: account.name
            for account in load_accounts(
                paths["config"] / "accounts.json"
            )
            if account.state == "active"
        }
        if locked_ids
        else {}
    )
    rows = []
    for role, candidates in (
        ("controller", stack.controller),
        *((role, stack.agents[role]) for role in ROLES),
    ):
        for ordinal, candidate in enumerate(candidates, 1):
            locked = bindings.candidate_accounts.get(candidate.id)
            rows.append(
                (
                    role,
                    str(ordinal),
                    candidate.model,
                    ", ".join(candidate.providers),
                    (
                        accounts.get(locked, "Unavailable named account")
                        if locked is not None
                        else "Automatic within provider"
                    ),
                )
            )
    return _render_table(
        ("ROLE", "CANDIDATE", "MODEL", "PROVIDER", "ACCOUNT POLICY"),
        rows,
    )


def _resolve_stack(
    config: ResolvedConfig,
    requested: str | None,
    *,
    launch_dir: Path | None = None,
) -> dict[str, object]:
    document = normalize_model_stacks(config.documents["model-stacks"])
    project_models = None
    if requested is None and launch_dir is not None:
        context = resolve_control_plane_context(
            config.documents["projects"], launch_dir
        )
        route = context.get("route")
        if (
            isinstance(route, Mapping)
            and route.get("scope") in {None, "context"}
        ):
            project_models = discover_project_models(
                Path(str(context["launchDirReal"])),
                Path(str(route["contextRootReal"])),
                document,
            )
            if project_models is not None:
                document = project_models.stacks
    stack_name = (
        project_models.stack_name
        if project_models is not None
        else requested or document.default_stack
    )
    try:
        stack = document.stacks[stack_name]
    except KeyError as error:
        available = ", ".join(sorted(document.stacks))
        raise CliError(
            f"model stack is not configured: {stack_name}; "
            f"available stacks: {available}"
        ) from error
    resolved = {
        "stack": stack_name,
        "controller": stack.controller[0].model,
        "configuredCandidates": {
            role: [candidate.model for candidate in stack.agents[role]]
            for role in ROLES
        },
        "agents": {
            role: stack.agents[role][0].model for role in ROLES
        },
    }
    if project_models is not None:
        resolved["source"] = str(project_models.path)
    return resolved


def _provider_list(config: ResolvedConfig) -> str:
    providers = config.documents["providers"]["providers"]
    rows = [
        (
            provider,
            details["type"],
            details["transport"],
            ", ".join(details["families"]),
        )
        for provider, details in sorted(providers.items())
    ]
    return _render_table(("PROVIDER", "ADAPTER", "TRANSPORT", "FAMILIES"), rows)


def _account_list(accounts: Sequence[Account]) -> str:
    rows = [
        (
            account.id,
            account.name,
            account.provider,
            account.pool,
            str(account.priority),
            account.state.upper(),
        )
        for account in sorted(accounts, key=lambda item: (item.pool, -item.priority, item.name))
    ]
    return _render_table(
        ("ID", "NAME", "PROVIDER", "POOL", "PRIORITY", "STATE"), rows
    )


def _session_list(sessions: Sequence[LogicalSession]) -> str:
    rows = [
        (
            session.id,
            session.created_at,
            str(session.project_root),
            session.stack,
            session.controller.primary.family,
            session.controller.primary.logical_model,
            session.parent_id or "—",
        )
        for session in sessions
    ]
    return _render_table(
        ("ID", "CREATED", "PROJECT", "STACK", "FAMILY", "MODEL", "PARENT"),
        rows,
    )


def _session_status(paths: Mapping[str, Path], session_id: str) -> str:
    load_logical_session(paths["state"], session_id)
    output = StringIO()
    render_status_main(
        input_stream=StringIO("{}"),
        output_stream=output,
        environment={
            "ORICHUM_SESSION_ID": session_id,
            "ORICHUM_STATE_HOME": str(paths["state"]),
            "ORICHUM_CONFIG_HOME": str(paths["config"]),
            "ORICHUM_DATA_HOME": str(paths["data"]),
            "TERM": "dumb",
        },
    )
    rendered = output.getvalue()
    if rendered == "ORICHUM │ status unavailable\n":
        raise CliError("session status is unavailable; run orichum doctor")
    return f"SESSION │ {session_id}\n{rendered}"


def _physical_cleanup_report(
    runs: Sequence[PhysicalRunCleanup], *, older_than_days: int, applied: bool
) -> str:
    if not runs:
        return (
            f"No inactive physical runs older than {older_than_days} day(s).\n"
        )
    rows = [(run.run_id, run.status.upper()) for run in runs]
    report = _render_table(("RUN", "STATUS"), rows)
    if applied:
        return report + f"Removed {len(runs)} physical run(s).\n"
    return (
        report
        + "Preview only. Re-run with --yes to remove these physical runs.\n"
    )


def _logical_cleanup_report(
    sessions: Sequence[LogicalSessionCleanup], *, applied: bool
) -> str:
    if not sessions:
        return "No logical sessions to clear.\n"
    rows = [
        (session.session_id, session.status.upper())
        for session in sessions
    ]
    report = _render_table(("SESSION", "STATUS"), rows)
    affected = sum(
        session.status in {"eligible", "removed"} for session in sessions
    )
    if applied:
        return (
            report
            + f"Removed {affected} logical session(s). "
            "Claude Code history and LeanCTX knowledge were preserved.\n"
        )
    if affected == 0:
        return report + "No inactive logical sessions can be removed.\n"
    return (
        report
        + "Preview only. Re-run with --yes to remove these logical sessions.\n"
    )


def _leanctx_project_root(
    config: ResolvedConfig,
    launch_dir: Path,
) -> Path | None:
    context = resolve_control_plane_context(
        config.documents["projects"],
        launch_dir,
    )
    repository = context.get("repoRootReal")
    if isinstance(repository, str) and repository:
        return Path(repository)
    route = context.get("route")
    root = route.get("contextRootReal") if isinstance(route, dict) else None
    if isinstance(root, str) and root:
        return Path(root)
    if isinstance(route, dict) and route.get("scope") == "normal":
        launch = context.get("launchDirReal")
        if isinstance(launch, str) and launch:
            return Path(launch)
    return None


def _leanctx_list(
    runs: Sequence[leanctx_monitor.LeanctxRun],
    selected_run_id: str | None,
) -> str:
    rows = [
        (
            run.run_id,
            run.created_at,
            str(run.project_root),
            "yes" if run.attached else "no",
            "yes" if run.has_activity else "no",
            "yes" if run.run_id == selected_run_id else "—",
        )
        for run in runs
    ]
    return _render_table(
        ("RUN", "CREATED", "PROJECT", "ATTACHED", "ACTIVITY", "SELECTED"),
        rows,
    )


def _leanctx_stats(
    run: leanctx_monitor.LeanctxRun,
    stats: leanctx_monitor.LeanctxStats,
    proxy: leanctx_monitor.LeanctxProxyStats,
) -> str:
    if stats.input_tokens:
        savings = Decimal(str(stats.savings_percent)).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
        reduction = f"{savings}%"
    else:
        reduction = "—"
    session = _render_table(
        (
            "RUN",
            "PROJECT",
            "COMMANDS",
            "SOURCE",
            "RETURNED",
            "SAVED",
            "REDUCTION",
        ),
        (
            (
                run.run_id,
                str(run.project_root),
                f"{stats.total_commands:,}",
                f"{stats.input_tokens:,}",
                f"{stats.output_tokens:,}",
                f"{stats.saved_tokens:,}",
                reduction,
            ),
        ),
    )
    wire_reduction = (
        f"{Decimal(str(proxy.savings_percent)).quantize(
            Decimal('0.1'),
            rounding=ROUND_HALF_UP,
        )}%"
        if proxy.bytes_original
        else "—"
    )
    wire = _render_table(
        (
            "REQUESTS",
            "COMPRESSED",
            "SOURCE BYTES",
            "FORWARDED BYTES",
            "EST. TOKENS",
            "REDUCTION",
        ),
        (
            (
                f"{proxy.requests_total:,}",
                f"{proxy.requests_compressed:,}",
                f"{proxy.bytes_original:,}",
                f"{proxy.bytes_compressed:,}",
                f"{proxy.saved_tokens:,}",
                wire_reduction,
            ),
        ),
    )
    return f"Session MCP\n{session}\nShared wire proxy\n{wire}"


def _estimated_usd(value: float) -> str:
    return f"${Decimal(str(value)):,.6f}"


def _leanctx_economics(
    session: LogicalSession,
    health: leanctx_monitor.LeanctxToolHealth,
    rolling: leanctx_monitor.LeanctxRollingEconomics,
    gain: leanctx_monitor.LeanctxGainSummary,
) -> str:
    resident = {
        name.removeprefix("mcp__leanctx__")
        for name in resident_tool_names(session.leanctx_profile)
    }
    schema_by_tool = {name: tokens for name, tokens, _ in health.tools}
    if not resident <= schema_by_tool.keys():
        raise CliError(
            "LeanCTX tool health is incompatible with the selected profile"
        )
    resident_schema = sum(schema_by_tool[name] for name in resident)
    deferred_tools = health.advertised_tools - len(resident)
    removed_schema = health.tool_schema_tokens - resident_schema
    if deferred_tools < 0 or removed_schema < 0:
        raise CliError(
            "LeanCTX tool health is incompatible with the selected profile"
        )
    footprint = _render_table(
        (
            "PROFILE",
            "ADVERTISED",
            "RESIDENT",
            "DEFERRED",
            "RESIDENT SCHEMA",
            "REMOVED PREFIX",
            "RECORDED CALLS",
        ),
        (
            (
                session.leanctx_profile,
                f"{health.advertised_tools:,}",
                f"{len(resident):,}",
                f"{deferred_tools:,}",
                f"{resident_schema:,}",
                f"{removed_schema:,}",
                f"{health.total_recorded_calls:,}",
            ),
        ),
    )
    reduction = (
        f"{Decimal(str(rolling.compression_percent)).quantize(
            Decimal('0.1'),
            rounding=ROUND_HALF_UP,
        )}%"
        if rolling.source_tokens
        else "—"
    )
    compression = _render_table(
        (
            "EVENTS",
            "SOURCE",
            "RETURNED",
            "SAVED",
            "REDUCTION",
            "EST. USD AVOIDED",
        ),
        (
            (
                f"{rolling.compression_events:,}",
                f"{rolling.source_tokens:,}",
                f"{rolling.returned_tokens:,}",
                f"{rolling.saved_tokens:,}",
                reduction,
                _estimated_usd(rolling.compression_saved_usd),
            ),
        ),
    )
    caching = _render_table(
        ("RECORDED REQUESTS", "CACHE-READ TOKENS", "EST. CACHE DISCOUNT"),
        (
            (
                f"{rolling.caching_events:,}",
                f"{rolling.cache_read_tokens:,}",
                _estimated_usd(rolling.cache_saved_usd),
            ),
        ),
    )
    all_time = _render_table(
        (
            "COMMANDS",
            "TURNS",
            "GROSS SAVED",
            "INJECTED OVERHEAD",
            "NET ESTIMATE",
            "EST. USD AVOIDED",
            "TOOL SPEND",
            "ROI",
        ),
        (
            (
                f"{gain.total_commands:,}",
                f"{gain.turns:,}",
                f"{gain.tokens_saved:,}",
                f"{gain.injected_overhead_total_tokens:,}",
                f"{gain.net_tokens_saved:,}",
                _estimated_usd(gain.avoided_usd),
                _estimated_usd(gain.tool_spend_usd),
                "—" if gain.roi is None else f"{gain.roi:.3f}x",
            ),
        ),
    )
    return (
        f"Selected-session provider footprint\n{footprint}\n"
        f"Shared rolling compression (last {rolling.hours} hours)\n"
        f"{compression}\n"
        "Shared rolling recorded prompt-cache estimates "
        f"(last {rolling.hours} hours)\n{caching}"
        "Rolling ledger records are shared across all Orichum projects; "
        "they are not selected-session totals.\n"
        "Ledger cache records are estimates and not complete provider billing.\n\n"
        f"LeanCTX all-time upstream estimate\n{all_time}"
        "All-time figures use upstream attribution and are not "
        "rolling-window billing.\n"
    )


def _session_routes(
    session: LogicalSession, accounts: Sequence[Account]
) -> str:
    bindings = (
        ("controller", session.controller),
        *((role, session.agents[role]) for role in ROLES),
    )
    rows = []
    for role, binding in bindings:
        primary = binding.primary
        fallback = (
            f"{route.account_id} ({route.provider})"
            for route in binding.fallbacks[:1]
        )
        fallback_name = next(fallback, "—")
        rows.append(
            (
                role,
                primary.logical_model,
                primary.provider,
                primary.account_id,
                fallback_name,
            )
        )
    return _render_table(
        ("ROLE", "MODEL", "PROVIDER", "PRIMARY ACCOUNT", "FALLBACK"),
        rows,
    )


def _effective_for(session: LogicalSession) -> EffectiveStack:
    agents = {
        role: session.agents[role].primary.upstream_model for role in ROLES
    }
    return EffectiveStack(
        stack_name=session.stack,
        controller=session.controller.primary.upstream_model,
        candidates={role: (agents[role],) for role in ROLES},
        agents=agents,
    )


def _validate_session_routes(
    session: LogicalSession,
    accounts: Sequence[Account],
    *,
    auth_dir: Path,
    provider_document: Mapping[str, object],
) -> None:
    _validate_plan_routes(
        session.controller,
        session.agents,
        accounts,
        auth_dir=auth_dir,
        provider_document=provider_document,
    )


def _validate_plan_routes(
    controller: RouteBinding,
    agents: Mapping[str, RouteBinding],
    accounts: Sequence[Account],
    *,
    auth_dir: Path,
    provider_document: Mapping[str, object],
) -> None:
    bindings = (controller, *(agents[role] for role in ROLES))
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        for route in (binding.primary, *binding.fallbacks):
            key = (route.account_id, route.logical_model)
            if key in seen:
                continue
            validate_route_credential(
                route,
                accounts,
                auth_dir=auth_dir,
                provider_document=provider_document,
            )
            seen.add(key)


def _verify_runtime(paths: Mapping[str, Path]) -> None:
    verifier = WORKFLOW_ROOT / "bin" / "orichum-runtime-ready"
    if not verifier.is_file() or verifier.is_symlink():
        raise CliError("Orichum runtime verifier is unavailable")
    try:
        completed = subprocess.run(
            [str(verifier), str(paths["data"])],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired as error:
        raise CliError("Orichum runtime health verification timed out") from error
    if completed.returncode != 0:
        verifier_error = getattr(completed, "stderr", "") or ""
        if "runtime source differs" in verifier_error:
            raise CliError(
                "Orichum runtime source differs from the installed route "
                "service; run install.sh"
            )
        try:
            accounts = load_accounts(paths["config"] / "accounts.json")
        except AccountError:
            accounts = ()
        if not accounts:
            raise CliError(
                "no provider account is registered; run orichum setup"
            )
        raise CliError("Orichum services are not owned and ready; run install.sh")


def _runtime_service_ports(paths: Mapping[str, Path]) -> dict[str, int]:
    try:
        document = json.loads(
            _read_stable_file(
                paths["data"] / "service-ports.json",
                "service port state",
                64 * 1024,
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CliError("service port state is unavailable") from error
    expected = {
        "claudexProxyPort",
        "cliproxyPort",
        "leanctxProxyPort",
        "routeProxyPort",
    }
    if not isinstance(document, dict) or set(document) != expected:
        raise CliError("service port state is invalid")
    ports = {}
    for name in expected:
        value = document[name]
        if type(value) is not int or value < 1024 or value > 65535:
            raise CliError("service port state is invalid")
        ports[name] = value
    if len(set(ports.values())) != len(ports):
        raise CliError("service ports must be distinct")
    return ports


def _live_models(paths: Mapping[str, Path]) -> frozenset[str]:
    try:
        ports = _runtime_service_ports(paths)
        cliproxy_port = ports["cliproxyPort"]
        connection = http.client.HTTPConnection(
            "127.0.0.1", cliproxy_port, timeout=3
        )
        connection.request("GET", "/v1/models")
        response = connection.getresponse()
        payload = response.read(2 * 1024 * 1024 + 1)
        connection.close()
        document = json.loads(payload)
        available = frozenset(
            item["id"]
            for item in document["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        )
    except (
        CliError,
        UnicodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
        OSError,
    ) as error:
        raise CliError("live Orichum model catalogue is unavailable") from error
    if response.status != 200 or len(payload) > 2 * 1024 * 1024:
        raise CliError("live Orichum model catalogue is unavailable")
    return available


def _validate_live_models(
    paths: Mapping[str, Path],
    controller: RouteBinding,
    agents: Mapping[str, RouteBinding],
    *,
    available: frozenset[str] | None = None,
) -> None:
    if available is None:
        available = _live_models(paths)
    bindings = (
        ("controller", controller),
        *((role, agents[role]) for role in ROLES),
    )
    missing = {
        (role, route.logical_model)
        for role, binding in bindings
        for route in (binding.primary, *binding.fallbacks)
        if route.upstream_model not in available
    }
    if missing:
        raise CliError(
            "bound model routes are not live for: "
            + ", ".join(
                f"{role} ({model})"
                for role, model in sorted(missing)
            )
        )


def _session_root(
    context: Mapping[str, object],
    route: Mapping[str, object],
) -> Path:
    if route.get("scope") in {None, "context"}:
        root = route.get("contextRootReal")
        if isinstance(root, str) and root:
            return Path(root)
    if route.get("scope") == "normal":
        launch = context.get("launchDirReal")
        if isinstance(launch, str) and launch:
            return Path(launch)
    raise CliError("session scope is invalid")



def _session_model_inputs(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    context: Mapping[str, object],
    project_models: ProjectModels | None,
) -> tuple[Mapping[str, object], str | None, StackBindings, ProjectModels | None]:
    route = context.get("route")
    if not isinstance(route, Mapping):
        raise CliError("launch directory is not mapped to an Orichum project")
    if project_models is None:
        return (
            config.documents,
            route.get("modelStack"),
            load_stack_bindings(paths["config"] / "stack-bindings.json"),
            None,
        )
    documents = dict(config.documents)
    documents["model-stacks"] = project_models.stacks
    return (
        documents,
        project_models.stack_name,
        StackBindings({}),
        project_models,
    )

def _prepare_new_session(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    launch_dir: Path,
    leanctx_profile: str = DEFAULT_LEANCTX_PROFILE,
) -> PreparedLaunch:
    _verify_runtime(paths)
    base = normalize_model_stacks(config.documents["model-stacks"])
    context, project_models = resolve_project_context(
        config.documents["projects"],
        launch_dir,
        Path(paths["config"]) / "jira-profiles.json",
        base,
    )
    route = context.get("route")
    if not isinstance(route, dict):
        raise CliError("launch directory is not mapped to an Orichum project")
    session_config, requested_stack, bindings, _project_models = (
        _session_model_inputs(paths, config, context, project_models)
    )
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    available = _live_models(paths)
    ordinal = int.from_bytes(os.urandom(8), "big")
    plan = resolve_session_plan(
        session_config,
        accounts,
        pools=tuple(route["accountPools"]),
        requested_stack=requested_stack,
        health={},
        selection_ordinal=ordinal,
        bindings=bindings,
        available_models=available,
    )
    _validate_plan_routes(
        controller=plan.controller,
        agents=plan.agents,
        accounts=accounts,
        auth_dir=paths["data"] / "auth",
        provider_document=config.documents["providers"],
    )
    _validate_live_models(
        paths, plan.controller, plan.agents, available=available
    )
    physical = create_resolved_session(
        WORKFLOW_ROOT,
        data_root=paths["data"],
        context=context,
        effective=plan.effective,
        plugin_source=WORKFLOW_ROOT / "controller" / "plugin",
    )
    logical = create_logical_session(
        paths["state"],
        project_root=_session_root(context, route),
        stack=plan.stack,
        controller=plan.controller,
        agents=plan.agents,
        leanctx_profile=leanctx_profile,
    )
    return PreparedLaunch(logical, physical)


def _prepare_resume(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    identifier: str,
    launch_dir: Path,
) -> PreparedLaunch:
    _verify_runtime(paths)
    logical = resolve_logical_session(paths["state"], identifier)
    context, _project_models = resolve_project_context(
        config.documents["projects"],
        launch_dir,
        Path(paths["config"]) / "jira-profiles.json",
        normalize_model_stacks(config.documents["model-stacks"]),
    )
    route = context.get("route")
    if (
        not isinstance(route, dict)
        or _session_root(context, route) != logical.project_root
    ):
        raise CliError("resume must be launched inside the session workspace")
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    _validate_session_routes(
        logical,
        accounts,
        auth_dir=paths["data"] / "auth",
        provider_document=config.documents["providers"],
    )
    _validate_live_models(paths, logical.controller, logical.agents)
    physical = create_resolved_session(
        WORKFLOW_ROOT,
        data_root=paths["data"],
        context=context,
        effective=_effective_for(logical),
        plugin_source=WORKFLOW_ROOT / "controller" / "plugin",
    )
    return PreparedLaunch(logical, physical)


def _read_handoff(path: Path) -> str:
    path = Path(path)
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise CliError("handoff file is unavailable") from error
    if (
        not stat.S_ISREG(observed.st_mode)
        or stat.S_ISLNK(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_size < 1
        or observed.st_size > 16 * 1024
    ):
        raise CliError("handoff file is unsafe or exceeds 16 KiB")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
            or opened.st_mtime_ns != observed.st_mtime_ns
        ):
            raise CliError("handoff file changed while opening")
        content = os.read(descriptor, 16 * 1024 + 1)
        if len(content) > 16 * 1024 or os.read(descriptor, 1):
            raise CliError("handoff file exceeds 16 KiB")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise CliError("handoff file changed while reading")
    finally:
        os.close(descriptor)
    try:
        handoff = content.decode("utf-8").strip()
    except UnicodeError as error:
        raise CliError("handoff file must be UTF-8") from error
    if not handoff or "\x00" in handoff:
        raise CliError("handoff file is empty or invalid")
    return handoff


def _prepare_fork(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    identifier: str,
    launch_dir: Path,
    requested_stack: str | None,
    handoff_file: Path | None,
    leanctx_profile: str | None = None,
) -> tuple[PreparedLaunch, str]:
    _verify_runtime(paths)
    parent = load_logical_session(paths["state"], identifier)
    context, _project_models = resolve_project_context(
        config.documents["projects"],
        launch_dir,
        Path(paths["config"]) / "jira-profiles.json",
        normalize_model_stacks(config.documents["model-stacks"]),
    )
    route = context.get("route")
    if (
        not isinstance(route, dict)
        or _session_root(context, route) != parent.project_root
    ):
        raise CliError("fork must be launched inside the session workspace")
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    if requested_stack is None:
        controller = parent.controller
        agents = parent.agents
        stack = parent.stack
        effective = _effective_for(parent)
    else:
        available = _live_models(paths)
        plan = resolve_session_plan(
            config.documents,
            accounts,
            pools=tuple(route["accountPools"]),
            requested_stack=requested_stack,
            health={},
            selection_ordinal=int.from_bytes(os.urandom(8), "big"),
            bindings=load_stack_bindings(
                paths["config"] / "stack-bindings.json"
            ),
            available_models=available,
        )
        controller = plan.controller
        agents = plan.agents
        stack = plan.stack
        effective = plan.effective
    family_changed = (
        controller.primary.family != parent.controller.primary.family
    )
    if family_changed and handoff_file is None:
        raise CliError("cross-family fork requires --handoff-file")
    handoff = (
        _read_handoff(handoff_file)
        if handoff_file is not None
        else (
            f"Explicit fork of Orichum session {parent.id}. Reconstruct active "
            "work from the repository state and the user's next message; do "
            "not assume access to the parent transcript."
        )
    )
    _validate_plan_routes(
        controller=controller,
        agents=agents,
        accounts=accounts,
        auth_dir=paths["data"] / "auth",
        provider_document=config.documents["providers"],
    )
    _validate_live_models(paths, controller, agents)
    physical = create_resolved_session(
        WORKFLOW_ROOT,
        data_root=paths["data"],
        context=context,
        effective=effective,
        plugin_source=WORKFLOW_ROOT / "controller" / "plugin",
    )
    logical = create_logical_session(
        paths["state"],
        project_root=parent.project_root,
        stack=stack,
        controller=controller,
        agents=agents,
        parent_id=parent.id,
        leanctx_profile=leanctx_profile,
    )
    return PreparedLaunch(logical, physical), handoff


def _replace_account(
    accounts: tuple[Account, ...],
    selector: str,
    **changes: object,
) -> tuple[Account, ...]:
    selected = find_account(accounts, selector)
    return tuple(
        replace(account, **changes) if account.id == selected.id else account
        for account in accounts
    )


def _mutate_account(
    parsed: argparse.Namespace,
    paths: Mapping[str, Path],
    config: ResolvedConfig,
) -> None:
    registry = paths["config"] / "accounts.json"
    bindings_path = paths["config"] / "stack-bindings.json"
    provider_document = config.documents["providers"]
    auth_dir = paths["data"] / "auth"
    management_endpoint = None

    def management():
        nonlocal management_endpoint
        if management_endpoint is None:
            management_endpoint = load_management_endpoint(paths["data"])
        return management_endpoint

    def validated(
        accounts: Sequence[Account],
    ) -> tuple[Account, ...]:
        result = tuple(accounts)
        validate_account_bindings(result, provider_document)
        return result

    def credential_for(account: Account):
        try:
            provider = provider_document["providers"][account.provider]
            expected_type = provider["authType"]
        except (KeyError, TypeError) as error:
            raise CliError("account provider configuration is incomplete") from error
        return resolve_credential_ref(
            auth_dir,
            account.credential_ref,
            expected_provider=expected_type,
        )

    def publish(account: Account) -> None:
        before = credential_for(account)
        if before.disabled:
            raise CliError("credential is disabled in CLIProxyAPI")
        patch_auth_fields(
            management(),
            account.credential_ref,
            {"prefix": account.routing_prefix, "priority": account.priority},
        )
        after = credential_for(account)
        if (
            after.provider != before.provider
            or after.disabled
            or after.prefix != account.routing_prefix
            or after.priority != account.priority
        ):
            raise CliError("CLIProxyAPI credential publication was not verified")

    def unpublish(account: Account) -> None:
        before = credential_for(account)
        restore_prefix = account.original_prefix or ""
        restore_priority = (
            account.original_priority
            if account.original_priority is not None
            else 0
        )
        patch_auth_fields(
            management(),
            account.credential_ref,
            {"prefix": restore_prefix, "priority": restore_priority},
        )
        if (
            account.original_prefix is None
            or account.original_priority is None
        ):
            patch_auth_fields(
                management(),
                account.credential_ref,
                {
                    "prefix": account.original_prefix,
                    "priority": account.original_priority,
                },
            )
        after = credential_for(account)
        if (
            after.provider != before.provider
            or after.disabled != before.disabled
            or after.prefix != account.original_prefix
            or after.priority != account.original_priority
        ):
            raise CliError("CLIProxyAPI credential restoration was not verified")

    def synchronize(account: Account) -> None:
        if account.state == "pending-add":
            publish(account)
            update_accounts(
                registry,
                lambda accounts: validated(
                    _replace_account(accounts, account.id, state="active")
                ),
            )
        elif account.state == "pending-remove":
            unpublish(account)
            update_accounts(
                registry,
                lambda accounts: validated(
                    tuple(item for item in accounts if item.id != account.id)
                ),
            )

    action = parsed.account_command
    with (
        stack_binding_transaction(bindings_path) as binding_transaction,
        account_transaction(registry),
        credential_metadata_transaction(auth_dir),
    ):
        if action == "add":
            providers = provider_document["providers"]
            pools = provider_document["accountPools"]
            provider = providers.get(parsed.provider)
            pool = pools.get(parsed.pool)
            if not isinstance(provider, dict):
                raise CliError(f"provider is not configured: {parsed.provider}")
            if (
                not isinstance(pool, dict)
                or parsed.provider not in pool.get("providers", ())
            ):
                raise CliError(
                    f"provider {parsed.provider} is not authorized by pool {parsed.pool}"
                )
            credential = resolve_credential_ref(
                auth_dir,
                parsed.credential_ref,
                expected_provider=provider["authType"],
            )
            if credential.disabled:
                raise CliError("credential is disabled in CLIProxyAPI")
            priority = parse_priority(parsed.priority)
            created: list[Account] = []

            def add(accounts: tuple[Account, ...]) -> tuple[Account, ...]:
                if any(
                    account.credential_ref == parsed.credential_ref
                    for account in accounts
                ):
                    raise AccountError(
                        "credential reference is already assigned to an account"
                    )
                account = new_account(
                    name=parsed.name,
                    provider=parsed.provider,
                    credential_ref=parsed.credential_ref,
                    pool=parsed.pool,
                    priority=priority,
                    existing=accounts,
                    state="pending-add",
                    original_prefix=credential.prefix,
                    original_priority=credential.priority,
                )
                proposed = validated((*accounts, account))
                created.append(account)
                return proposed

            update_accounts(registry, add)
            account = created[0]
            synchronize(account)
        elif action == "remove":
            current = find_account(load_accounts(registry), parsed.selector)
            stacks = normalize_model_stacks(
                config.documents["model-stacks"]
            )
            usage = {
                candidate.id: (stack_name, role)
                for stack_name, stack in stacks.stacks.items()
                for role, candidates in (
                    ("controller", stack.controller),
                    *((name, stack.agents[name]) for name in ROLES),
                )
                for candidate in candidates
            }
            stack_bindings = binding_transaction.load()
            current_bindings = StackBindings(
                {
                    candidate: account
                    for candidate, account in (
                        stack_bindings.candidate_accounts.items()
                    )
                    if candidate in usage
                }
            )
            if current_bindings != stack_bindings:
                current_bindings = binding_transaction.save(
                    current_bindings,
                    expected_digest=binding_transaction.digest(),
                )
            for candidate, account in (
                current_bindings.candidate_accounts.items()
            ):
                if account == current.id:
                    stack_name, role = usage[candidate]
                    raise CliError(
                        "account cannot be removed: bound by "
                        f"stack {stack_name} role {role}"
                    )
            if current.state == "pending-remove":
                synchronize(current)
                return
            updated = update_accounts(
                registry,
                lambda accounts: validated(
                    _replace_account(
                        accounts, parsed.selector, state="pending-remove"
                    )
                ),
            )
            synchronize(find_account(updated, parsed.selector))
        elif action == "sync":
            accounts = load_accounts(registry)
            selected = (
                (find_account(accounts, parsed.selector),)
                if parsed.selector
                else accounts
            )
            for account in selected:
                synchronize(account)
        else:
            changes: dict[str, object]
            if action == "rename":
                changes = {"name": parsed.name}
            elif action == "priority":
                priority = parse_priority(parsed.priority)
                current = find_account(load_accounts(registry), parsed.selector)
                if current.state == "disabled":
                    update_accounts(
                        registry,
                        lambda accounts: validated(
                            _replace_account(
                                accounts,
                                parsed.selector,
                                priority=priority,
                            )
                        ),
                    )
                    return
                if current.state != "active":
                    raise CliError(
                        "pending account operation must be synchronized first"
                    )
                updated = update_accounts(
                    registry,
                    lambda accounts: validated(
                        _replace_account(
                            accounts,
                            parsed.selector,
                            priority=priority,
                            state="pending-add",
                        )
                    ),
                )
                synchronize(find_account(updated, parsed.selector))
                return
            elif action == "enable":
                current = find_account(load_accounts(registry), parsed.selector)
                if current.state == "active":
                    return
                if current.state != "disabled":
                    raise CliError(
                        "pending account operation must be synchronized first"
                    )
                updated = update_accounts(
                    registry,
                    lambda accounts: validated(
                        _replace_account(
                            accounts, parsed.selector, state="pending-add"
                        )
                    ),
                )
                synchronize(find_account(updated, parsed.selector))
                return
            elif action == "disable":
                current = find_account(load_accounts(registry), parsed.selector)
                if current.state == "disabled":
                    return
                if current.state != "active":
                    raise CliError(
                        "pending account operation must be synchronized first"
                    )
                changes = {"state": "disabled"}
            else:
                raise AssertionError("unreachable account action")
            update_accounts(
                registry,
                lambda accounts: validated(
                    _replace_account(accounts, parsed.selector, **changes)
                ),
            )


def _configuration_model_changes(
    config: ResolvedConfig,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> tuple[bool, bool]:
    model_changed = draft.profile_switch is None and any(
        draft.role_models.get(role) != snapshot.assignments.get(role)
        for role in draft.role_models
    )
    stack_changed = draft.project.stack_name != snapshot.target.stack_name
    if not snapshot.project_models_checked:
        return model_changed, stack_changed
    base_stacks = normalize_model_stacks(config.documents["model-stacks"])
    project_models = discover_project_models(
        snapshot.launch_root or snapshot.target.root,
        snapshot.target.root,
        base_stacks,
    )
    expected_source = (
        snapshot.project_models_path,
        snapshot.project_models_digest,
    )
    current_source = (
        project_models.path if project_models is not None else None,
        project_models.digest if project_models is not None else None,
    )
    if current_source != expected_source:
        raise CliError(
            "project model mapping changed while configuration was open; "
            "restart orichum configure"
        )
    if project_models is not None and (model_changed or stack_changed):
        raise CliError(
            "project models are controlled by "
            f"{project_models.path}; edit that JSON file directly"
        )
    return model_changed, stack_changed


def _apply_configuration_draft(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> None:
    """Apply a confirmed guided draft and compensate new accounts on failure."""
    model_changed, stack_changed = _configuration_model_changes(
        config,
        snapshot,
        draft,
    )
    registry = Path(paths["config"]) / "accounts.json"
    created: list[Account] = []
    try:
        before = load_accounts(registry)
        known_ids = {account.id for account in before}
        for pending in draft.pending_accounts:
            _mutate_account(
                argparse.Namespace(
                    account_command="add",
                    name=pending.name,
                    provider=pending.provider,
                    credential_ref=pending.credential_ref,
                    pool=pending.pool,
                    priority=str(pending.priority),
                ),
                paths,
                config,
            )
            current_accounts = load_accounts(registry)
            added = tuple(
                account
                for account in current_accounts
                if (
                    account.id not in known_ids
                    and account.credential_ref == pending.credential_ref
                )
            )
            if len(added) != 1:
                raise CliError("new provider account could not be identified")
            created.append(added[0])
            known_ids.update(account.id for account in current_accounts)
        backup_drafts = tuple(
            pending
            for pending in draft.pending_accounts
            if pending.intent == "backup"
        )
        if backup_drafts:
            refreshed = load_configuration_snapshot(
                paths,
                config,
                snapshot.launch_root or snapshot.target.root,
            )
            created_by_credential = {
                account.credential_ref: account for account in created
            }
            for pending in backup_drafts:
                if pending.primary_id is None:
                    raise CliError("backup account has no primary account")
                primary = find_account(
                    refreshed.accounts,
                    pending.primary_id,
                )
                backup = created_by_credential.get(pending.credential_ref)
                if (
                    backup is None
                    or backup
                    not in compatible_backup_accounts(refreshed, primary)
                ):
                    raise CliError(
                        "backup account does not advertise a compatible route"
                    )

        bindings_changed = bool(draft.binding_removals)
        if not model_changed and not stack_changed and not bindings_changed:
            return
        config_root = Path(paths["config"])
        binding_path = config_root / "stack-bindings.json"
        current = load_stack_snapshot(
            config_root / "model-stacks.json",
            binding_path,
        )
        current_snapshot = replace(
            snapshot,
            stacks=current.stacks,
            bindings=current.bindings,
        )
        if model_changed:
            updated = build_managed_stack(current_snapshot, draft)
            stack_name = managed_stack_name(snapshot.target.root)
        else:
            updated = current.stacks
            stack_name = draft.project.stack_name
        updated_bindings = StackBindings(
            {
                candidate: account
                for candidate, account in (
                    current.bindings.candidate_accounts.items()
                )
                if candidate not in draft.binding_removals
            }
        )
        if stack_changed and not model_changed:
            availability = refreshed if backup_drafts else load_configuration_snapshot(
                paths,
                config,
                snapshot.launch_root or snapshot.target.root,
            )
            compatibility_snapshot = replace(
                current_snapshot,
                accounts=availability.accounts,
                catalog=availability.catalog,
                bindings=updated_bindings,
            )
            if not stack_is_live_compatible(
                compatibility_snapshot,
                stack_name,
            ):
                raise CliError(
                    "selected model profile or stack is not usable for this project"
                )
        with control_plane_transaction(config_root):
            with stack_binding_transaction(binding_path):
                save_stack(current, updated, updated_bindings)
                if model_changed or stack_changed:
                    assign_stack_to_context(
                        config_root / "projects.json",
                        snapshot.target.root,
                        stack_name,
                        updated.stacks,
                    )
    except BaseException:
        for account in reversed(created):
            try:
                _mutate_account(
                    argparse.Namespace(
                        account_command="remove",
                        selector=account.id,
                    ),
                    paths,
                    config,
                )
            except BaseException:
                pass
        raise


def _interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _prompt_choice(
    heading: str,
    choices: Sequence[tuple[str, str]],
    *,
    default: int = 0,
) -> str:
    if not choices or default < 0 or default >= len(choices):
        raise CliError("interactive choice configuration is invalid")
    print(heading)
    for index, (label, _value) in enumerate(choices, start=1):
        suffix = " [default]" if index - 1 == default else ""
        print(f"  {index}. {label}{suffix}")
    while True:
        raw = _prompt_input(f"Select [{default + 1}]: ").strip()
        if not raw:
            return choices[default][1]
        if raw.isdigit() and 1 <= int(raw) <= len(choices):
            return choices[int(raw) - 1][1]
        print(f"Enter a number from 1 to {len(choices)}.")


def _prompt_text(label: str, default: str) -> str:
    raw = _prompt_input(f"{label} [{default}]: ").strip()
    return raw or default


def _prompt_confirm() -> bool:
    while True:
        raw = _prompt_input(
            "Register this account? [Y/n]: "
        ).strip().lower()
        if raw in {"", "y", "yes"}:
            return True
        if raw in {"n", "no"}:
            return False
        print("Enter y or n.")


def _prompt_input(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt) as error:
        raise CliError("setup cancelled") from error


def _managed_provider_login(
    paths: Mapping[str, Path],
    login_type: str,
    provider_label: str,
    diagnostics: SetupDiagnostics,
) -> int:
    endpoint = load_management_endpoint(paths["data"])
    session = start_oauth(endpoint, login_type)
    diagnostics.emit(f"{provider_label} authentication")
    diagnostics.emit("  Open this URL:")
    diagnostics.sensitive(f"    {session.url}")
    try:
        headless = bool(
            os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")
        )
        browser_opened = False
        if headless:
            diagnostics.emit(
                "  SSH session detected; open the URL on your machine."
            )
        else:
            diagnostics.emit("  Opening your browser…")
            try:
                browser_opened = webbrowser.open(session.url, new=2)
            except (OSError, webbrowser.Error):
                browser_opened = False
            if not browser_opened:
                diagnostics.emit("  Browser did not open automatically.")
        if headless or not browser_opened:
            diagnostics.emit(
                "  Paste the final callback URL, or press Enter if the "
                "callback completed automatically."
            )
            callback_url = _prompt_input("Callback URL: ").strip()
            if callback_url:
                submit_oauth_callback(
                    endpoint,
                    session.state,
                    callback_url,
                )

        diagnostics.emit("  Waiting for authentication…")
        deadline = time.monotonic() + 30 * 60
        while time.monotonic() < deadline:
            if oauth_status(endpoint, session.state) == "ok":
                diagnostics.emit("  ✓ Signed in")
                return 0
            time.sleep(1)
    except (CliError, ManagementError):
        try:
            cancel_oauth(endpoint, session.state)
        except ManagementError:
            pass
        raise
    except KeyboardInterrupt as error:
        try:
            cancel_oauth(endpoint, session.state)
        except ManagementError:
            pass
        raise CliError("setup cancelled") from error
    try:
        cancel_oauth(endpoint, session.state)
    except ManagementError:
        pass
    raise CliError("provider authentication timed out")


def _prepare_provider_account(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    provider_name: str,
    *,
    onboarding: bool = False,
    diagnostics: SetupDiagnostics | None = None,
    chooser: Callable[..., str] | None = None,
) -> PendingProviderAccount:
    """Authenticate or reuse one credential without registering an account."""
    provider_document = config.documents["providers"]
    providers = provider_document["providers"]
    provider = providers.get(provider_name)
    if not isinstance(provider, Mapping):
        raise CliError(f"provider is not configured: {provider_name}")
    auth_type = provider.get("authType")
    if not isinstance(auth_type, str):
        raise CliError("provider authentication configuration is incomplete")
    choose = _prompt_choice if chooser is None else chooser
    auth_dir = Path(paths["data"]) / "auth"
    if auth_dir.exists():
        repair_credential_modes(auth_dir)
        before_credentials = list_credentials(auth_dir)
    else:
        before_credentials = ()
    before_refs = {credential.path.name for credential in before_credentials}
    accounts = load_accounts(Path(paths["config"]) / "accounts.json")
    assigned = {account.credential_ref for account in accounts}
    existing = tuple(
        credential
        for credential in before_credentials
        if (
            credential.provider == auth_type
            and not credential.disabled
            and credential.path.name not in assigned
        )
    )
    credential = None
    if existing:
        if onboarding:
            if len(existing) != 1:
                raise CliError(
                    "multiple unregistered authentications are available; "
                    "run 'orichum provider configure' to choose one"
                )
            credential = existing[0]
        else:
            selected_ref = choose(
                "Use an existing authentication or sign in again:",
                (
                    *(
                        (
                            f"Existing {provider_name.title()} "
                            f"authentication {index}",
                            item.path.name,
                        )
                        for index, item in enumerate(existing, start=1)
                    ),
                    ("Authenticate another account", "__login__"),
                ),
            )
            if selected_ref != "__login__":
                credential = next(
                    item for item in existing if item.path.name == selected_ref
                )
    if credential is None:
        if onboarding and diagnostics is not None:
            status = _managed_provider_login(
                paths,
                auth_type,
                _PROVIDER_LABELS.get(provider_name, provider_name.title()),
                diagnostics,
            )
        else:
            status = _run_external(
                "orichum-login",
                [auth_type],
                environment={"ORICHUM_PROVIDER_CONFIGURE": "1"},
            )
        if status != 0:
            raise CliError("provider authentication did not complete")
    elif onboarding and diagnostics is not None:
        label = _PROVIDER_LABELS.get(provider_name, provider_name.title())
        diagnostics.emit(f"{label} authentication")
        diagnostics.emit("  ✓ Already configured")
    repair_credential_modes(auth_dir)
    compatible = tuple(
        item
        for item in list_credentials(auth_dir)
        if (
            item.provider == auth_type
            and not item.disabled
            and item.path.name not in assigned
        )
    )
    if credential is None and not compatible:
        raise CliError(
            "authentication completed, but no reusable compatible account "
            "was found"
        )
    if credential is None:
        created = tuple(
            item for item in compatible if item.path.name not in before_refs
        )
        if len(created) == 1:
            credential = created[0]
        elif onboarding:
            raise CliError(
                "authentication completed, but its account could not be "
                "identified uniquely; run 'orichum provider configure'"
            )
        else:
            selected_ref = choose(
                "Choose the authenticated account:",
                tuple(
                    (
                        f"Authenticated {provider_name.title()} account "
                        f"{index}",
                        item.path.name,
                    )
                    for index, item in enumerate(compatible, start=1)
                ),
            )
            credential = next(
                item for item in compatible if item.path.name == selected_ref
            )
    return PendingProviderAccount(
        provider=provider_name,
        credential_ref=credential.path.name,
        suggested_name=f"{provider_name.title()} account",
    )


def _provider_configure(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    onboarding: bool = False,
    diagnostics: SetupDiagnostics | None = None,
) -> int:
    provider_document = config.documents["providers"]
    providers = provider_document["providers"]
    provider_choices = tuple(
        (
            f"{name} ({', '.join(details['families'])})",
            name,
        )
        for name, details in providers.items()
    )
    provider_name = _prompt_choice("Choose a provider:", provider_choices)
    pending = _prepare_provider_account(
        paths,
        config,
        provider_name,
        onboarding=onboarding,
        diagnostics=diagnostics,
    )
    name = _prompt_text("Account name", pending.suggested_name)
    if onboarding:
        pool = "shared"
        priority = "primary"
    else:
        pools = tuple(
            pool_name
            for pool_name, details in provider_document["accountPools"].items()
            if provider_name in details["providers"]
        )
        default_pool = pools.index("shared") if "shared" in pools else 0
        pool = _prompt_choice(
            "Choose where this account is available:",
            tuple((pool_name, pool_name) for pool_name in pools),
            default=default_pool,
        )
        priority = _prompt_choice(
            "Choose account priority:",
            (
                ("Primary", "primary"),
                ("Secondary", "secondary"),
                ("Reserve", "reserve"),
            ),
        )
        print(
            "\nAccount summary:\n"
            f"  Name:     {name}\n"
            f"  Provider: {provider_name}\n"
            f"  Pool:     {pool}\n"
            f"  Priority: {priority}"
        )
        if not _prompt_confirm():
            print("No account was registered.")
            return 0
    _mutate_account(
        argparse.Namespace(
            account_command="add",
            name=name,
            provider=provider_name,
            credential_ref=pending.credential_ref,
            pool=pool,
            priority=priority,
        ),
        paths,
        config,
    )
    if not onboarding:
        print(f"Provider account ready: {name}")
    return 0


def _active_provider_accounts(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
) -> tuple[Account, ...]:
    accounts = load_accounts(paths["config"] / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    return tuple(account for account in accounts if account.state == "active")


def _runtime_ready(paths: Mapping[str, Path]) -> bool:
    try:
        _verify_runtime(paths)
    except CliError:
        return False
    return True


def _reconcile_runtime(
    diagnostics: SetupDiagnostics | None = None,
) -> int:
    installer = WORKFLOW_ROOT / "install.sh"
    if not installer.is_file() or installer.is_symlink():
        raise CliError("active runtime installer is unavailable")
    if diagnostics is not None:
        return diagnostics.run_command(
            [str(installer), "--verbose"],
            cwd=WORKFLOW_ROOT,
        )
    completed = subprocess.run(
        [str(installer)],
        cwd=str(WORKFLOW_ROOT),
        check=False,
    )
    return completed.returncode


def _setup_project_path(requested: str | None) -> Path:
    raw = requested or _prompt_text("Projects folder", "~/projects")
    if requested is None:
        try:
            Path(raw).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise CliError("project root is unavailable") from error
    try:
        project = Path(raw).expanduser().resolve(strict=True)
    except OSError as error:
        raise CliError("project root is unavailable") from error
    if not project.is_dir():
        raise CliError("project root must be a directory")
    return project


def _display_setup_path(path: Path) -> str:
    path = Path(path)
    try:
        relative = path.relative_to(Path.home())
    except ValueError:
        return str(path)
    return "~" if not relative.parts else f"~/{relative}"


def _project_context_mapped(config: ResolvedConfig, project: Path) -> bool:
    resolved = resolve_control_plane_context(
        config.documents["projects"], project
    )
    route = resolved.get("route")
    return isinstance(route, Mapping) and route.get("scope") == "context"


def _setup_project_ready(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    project: Path,
) -> bool:
    try:
        _verify_runtime(paths)
        base = normalize_model_stacks(config.documents["model-stacks"])
        context, project_models = resolve_project_context(
            config.documents["projects"],
            project,
            Path(paths["config"]) / "jira-profiles.json",
            base,
        )
        route = context.get("route")
        if not isinstance(route, dict):
            return False
        session_config, requested_stack, bindings, _project_models = (
            _session_model_inputs(paths, config, context, project_models)
        )
        accounts = load_accounts(paths["config"] / "accounts.json")
        validate_account_bindings(accounts, config.documents["providers"])
        available = _live_models(paths)
        plan = resolve_session_plan(
            session_config,
            accounts,
            pools=tuple(route["accountPools"]),
            requested_stack=requested_stack,
            health={},
            selection_ordinal=0,
            bindings=bindings,
            available_models=available,
        )
        _validate_plan_routes(
            controller=plan.controller,
            agents=plan.agents,
            accounts=accounts,
            auth_dir=paths["data"] / "auth",
            provider_document=config.documents["providers"],
        )
        _validate_live_models(
            paths, plan.controller, plan.agents, available=available
        )
    except (
        AccountError,
        CliError,
        CredentialError,
        LogicalSessionError,
        RouteError,
        RoutingError,
        StackBindingError,
    ):
        return False
    return True


def _setup_normal_ready(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
) -> bool:
    try:
        _verify_runtime(paths)
        base = normalize_model_stacks(config.documents["model-stacks"])
        context, project_models = resolve_project_context(
            config.documents["projects"],
            Path.home(),
            Path(paths["config"]) / "jira-profiles.json",
            base,
        )
        route = context.get("route")
        if not isinstance(route, dict) or route.get("scope") != "normal":
            return False
        session_config, requested_stack, bindings, _project_models = (
            _session_model_inputs(paths, config, context, project_models)
        )
        accounts = load_accounts(paths["config"] / "accounts.json")
        validate_account_bindings(accounts, config.documents["providers"])
        available = _live_models(paths)
        plan = resolve_session_plan(
            session_config,
            accounts,
            pools=tuple(route["accountPools"]),
            requested_stack=requested_stack,
            health={},
            selection_ordinal=0,
            bindings=bindings,
            available_models=available,
        )
        _validate_plan_routes(
            controller=plan.controller,
            agents=plan.agents,
            accounts=accounts,
            auth_dir=paths["data"] / "auth",
            provider_document=config.documents["providers"],
        )
        _validate_live_models(
            paths, plan.controller, plan.agents, available=available
        )
    except (
        AccountError,
        CliError,
        CredentialError,
        LogicalSessionError,
        RouteError,
        RoutingError,
        StackBindingError,
    ):
        return False
    return True

def _ensure_setup_project_config(
    config: ResolvedConfig,
    project: Path,
) -> tuple[Path, bool]:
    base = normalize_model_stacks(config.documents["model-stacks"])
    context = resolve_control_plane_context(config.documents["projects"], project)
    route = context.get("route")
    if not isinstance(route, dict):
        raise CliError("setup is incomplete: the project context is unavailable")
    configured_stack = route.get("modelStack")
    stack_name = (
        configured_stack
        if isinstance(configured_stack, str)
        else base.default_stack
    )
    if stack_name not in base.stacks:
        raise CliError("setup is incomplete: the project model stack is unavailable")
    jira_profile = route.get("jiraProfile")
    if route.get("atlassianConfigured") is True and jira_profile is None:
        raise CliError(
            "setup cannot create a project config while Jira credentials are "
            "stored in the legacy project context"
        )
    return ensure_project_config(
        project,
        base.stacks[stack_name],
        jira_profile=jira_profile,
        github_account=route.get("githubAccount"),
    )


def _setup(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    requested_project: str | None,
    *,
    normal_scope: bool = False,
    verbose: bool = False,
) -> int:
    if normal_scope and requested_project is not None:
        raise CliError("setup --user does not accept a project path")
    try:
        diagnostics = SetupDiagnostics.create(paths, verbose=verbose)
    except (CliError, OSError) as error:
        print("Setup stopped while preparing private diagnostics.")
        print(f"\nReason:\n  {error}")
        print("\nRun:\n  orichum setup")
        return 2

    def stopped(
        action: str,
        status: int,
        *,
        reason: str | None = None,
    ) -> int:
        diagnostics.emit(f"Setup stopped while {action}.")
        diagnostics.emit("")
        diagnostics.emit("Reason:")
        diagnostics.emit(
            "  " + (reason or f"the setup step exited with status {status}")
        )
        diagnostics.emit("")
        diagnostics.emit("Run:")
        diagnostics.emit("  orichum setup")
        diagnostics.emit("")
        diagnostics.emit("Diagnostics:")
        diagnostics.emit(f"  {diagnostics.path}")
        return status

    diagnostics.emit("Setting up Orichum…")
    diagnostics.emit("")
    try:
        active_accounts = _active_provider_accounts(paths, config)
        account_reused = bool(active_accounts)
        if not active_accounts:
            status = _provider_configure(
                paths,
                config,
                onboarding=True,
                diagnostics=diagnostics,
            )
            if status != 0:
                return stopped("configuring the provider account", status)
            paths, config = _load()
            active_accounts = _active_provider_accounts(paths, config)
            if not active_accounts:
                raise CliError(
                    "setup stopped before a provider account was registered"
                )
        else:
            diagnostics.emit("Authentication")
            diagnostics.emit("  ✓ Already configured")

        account = sorted(
            active_accounts,
            key=lambda current: (-current.priority, current.name),
        )[0]
        diagnostics.emit("")
        diagnostics.emit("Account")
        diagnostics.emit(f"  Name: {account.name}")
        diagnostics.emit(
            "  ✓ Already configured"
            if account_reused
            else "  ✓ Registered as primary"
        )

        if not _runtime_ready(paths):
            status = _reconcile_runtime(diagnostics)
            if status != 0:
                return stopped("starting Orichum services", status)
            paths, config = _load()

        if normal_scope:
            projects = config.documents["projects"]
            if not isinstance(projects, Mapping):
                raise CliError("normal scope configuration is unavailable")
            if projects.get("normal") is None:
                base = normalize_model_stacks(config.documents["model-stacks"])
                raw_pools = config.documents["providers"].get("accountPools")
                if not isinstance(raw_pools, Mapping):
                    raise CliError("provider account pools are unavailable")
                configure_normal_scope(
                    Path(paths["config"]) / "projects.json",
                    model_stack=None,
                    account_pools=tuple(
                        dict.fromkeys(
                            account.pool for account in active_accounts
                        )
                    ),
                    known_stacks=base.stacks,
                    known_pools=raw_pools,
                )
                paths, config = _load()
            stack_reused = _setup_normal_ready(paths, config)
            if not stack_reused:
                create_recommended_stack(paths, config, launch_dir=Path.home())
                paths, config = _load()
                status = _reconcile_runtime(diagnostics)
                if status != 0:
                    return stopped("starting Orichum services", status)
                paths, config = _load()
                if not _setup_normal_ready(paths, config):
                    raise CliError(
                        "setup is incomplete: the normal scope has no usable model stack"
                    )
            diagnostics.emit("")
            diagnostics.emit("Normal scope")
            diagnostics.emit("  ✓ Ready for non-project work")
            diagnostics.emit("  Stack: recommended" if not stack_reused else "  ✓ Already configured")
            status = _run_external("orichum-doctor", [], diagnostics=diagnostics)
            if status != 0:
                return stopped("verifying Orichum services", status)
            diagnostics.emit("")
            diagnostics.emit("Services")
            diagnostics.emit("  ✓ Ready")
            diagnostics.emit("")
            diagnostics.emit("Orichum is ready.")
            return 0

        project = _setup_project_path(requested_project)
        context_reused = _project_context_mapped(config, project)
        if not context_reused:
            pools = tuple(
                dict.fromkeys(
                    current.pool
                    for current in sorted(
                        active_accounts,
                        key=lambda current: -current.priority,
                    )
                )
            )
            context_arguments = ["add", str(project)]
            for pool in pools:
                context_arguments.extend(("--pool", pool))
            status = _run_external(
                "orichum-context",
                context_arguments,
                diagnostics=diagnostics,
            )
            if status != 0:
                return stopped("configuring the projects folder", status)
            paths, config = _load()
        diagnostics.emit("")
        diagnostics.emit("Projects")
        diagnostics.emit(f"  Folder: {_display_setup_path(project)}")
        diagnostics.emit(
            "  ✓ Already configured"
            if context_reused
            else "  ✓ Configured"
        )

        stack_reused = _setup_project_ready(paths, config, project)
        if not stack_reused:
            create_recommended_stack(paths, config, launch_dir=project)
            paths, config = _load()
            status = _reconcile_runtime(diagnostics)
            if status != 0:
                return stopped("starting Orichum services", status)
            paths, config = _load()
            if not _setup_project_ready(paths, config, project):
                raise CliError(
                    "setup is incomplete: the project has no usable model stack"
                )
        project_config_path, project_config_created = (
            _ensure_setup_project_config(config, project)
        )
        if not _setup_project_ready(paths, config, project):
            raise CliError(
                "setup is incomplete: the project configuration is unusable"
            )
        diagnostics.emit("")
        diagnostics.emit("Models")
        diagnostics.emit(
            "  ✓ Already configured"
            if stack_reused
            else "  ✓ Recommended stack created"
        )
        diagnostics.emit(f"  File: {_display_setup_path(project_config_path)}")
        if project_config_created:
            diagnostics.emit("  ✓ Project configuration created")

        status = _run_external(
            "orichum-doctor", [], diagnostics=diagnostics
        )
        if status != 0:
            return stopped("verifying Orichum services", status)
        diagnostics.emit("")
        diagnostics.emit("Services")
        diagnostics.emit("  ✓ Ready")
        diagnostics.emit("")
        diagnostics.emit("Orichum is ready.")
        return 0
    except (
        AccountError,
        CatalogError,
        CliError,
        ConfigError,
        ContextError,
        CredentialError,
        ManagementError,
        OSError,
        RouteError,
        RoutingError,
        StackBindingError,
        StackStoreError,
    ) as error:
        diagnostics.technical(f"ERROR: {error}\n")
        return stopped("configuring Orichum", 2, reason=str(error))
    finally:
        diagnostics.close()


def _run_external(
    name: str,
    arguments: list[str],
    *,
    environment: Mapping[str, str] | None = None,
    diagnostics: SetupDiagnostics | None = None,
) -> int:
    candidate = WORKFLOW_ROOT / "bin" / name
    executable = str(candidate) if candidate.is_file() else shutil.which(name)
    if executable is None:
        raise CliError(f"required command is not installed: {name}")
    child_environment = os.environ.copy()
    if environment is not None:
        child_environment.update(environment)
    if diagnostics is not None:
        return diagnostics.run_command(
            [executable, *arguments],
            cwd=WORKFLOW_ROOT,
            environment=environment,
        )
    completed = subprocess.run(
        [executable, *arguments],
        check=False,
        cwd=WORKFLOW_ROOT,
        env=child_environment,
    )
    return completed.returncode


_OWNED_CLAUDE_OPTIONS = (
    "--agents",
    "--effort",
    "--model",
    "--fallback-model",
    "--config",
    "--plugin-dir",
    "--plugin-url",
    "--append-system-prompt",
    "--append-system-prompt-file",
    "--system-prompt",
    "--system-prompt-file",
    "--agent",
    "--settings",
    "--mcp-config",
    "--strict-mcp-config",
    "--permission-mode",
    "--allowedTools",
    "--allowed-tools",
    "--disallowedTools",
    "--disallowed-tools",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--session-id",
    "--resume",
    "--continue",
    "--fork-session",
    "--from-pr",
    "--no-session-persistence",
    "--safe-mode",
    "--bare",
    "--worktree",
    "--tmux",
)
_OWNED_CLAUDE_SHORT_OPTIONS = ("-c", "-r", "-w")


def _validate_user_claude_arguments(arguments: Sequence[str]) -> list[str]:
    result = list(arguments)
    if result and result[0] == "--":
        result.pop(0)
    for argument in result:
        if any(
            argument == owned or argument.startswith(f"{owned}=")
            for owned in _OWNED_CLAUDE_OPTIONS
        ) or any(
            argument == owned or argument.startswith(owned)
            for owned in _OWNED_CLAUDE_SHORT_OPTIONS
        ):
            raise CliError(f"Orichum owns Claude option: {argument}")
    return result


def _read_stable_file(path: Path, label: str, maximum: int) -> bytes:
    try:
        observed = os.lstat(path)
    except OSError as error:
        raise CliError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or observed.st_size > maximum
    ):
        raise CliError(f"{label} is unsafe")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != observed.st_dev
            or opened.st_ino != observed.st_ino
        ):
            raise CliError(f"{label} changed while opening")
        content = os.read(descriptor, maximum + 1)
        if len(content) > maximum or os.read(descriptor, 1):
            raise CliError(f"{label} is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            raise CliError(f"{label} changed while reading")
        return content
    finally:
        os.close(descriptor)


def _port_is_available(port: int) -> bool:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        listener.close()
    return True


def _reserve_session_claudex_port(
    state_home: Path,
    run_dir: Path,
    session_id: str,
    preferred_port: int,
    excluded_ports: frozenset[int],
) -> int:
    if (
        type(preferred_port) is not int
        or preferred_port < 1024
        or preferred_port > 65535
    ):
        raise CliError("preferred Claudex proxy port is invalid")
    leases = state_home / "claudex-port-leases"
    leases.mkdir(mode=0o700, parents=True, exist_ok=True)
    if leases.is_symlink() or not leases.is_dir():
        raise CliError("Claudex proxy lease state is unsafe")
    leases.chmod(0o700)
    lock_path = state_home / ".claudex-port.lock"
    lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    lock_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_descriptor = os.open(lock_path, lock_flags, 0o600)
    except OSError as error:
        raise CliError("Claudex proxy port lock is unavailable") from error
    try:
        os.fchmod(lock_descriptor, 0o600)
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        span = 65535 - 1024 + 1
        for attempt in range(span):
            candidate = 1024 + (preferred_port - 1024 + attempt) % span
            if candidate in excluded_ports:
                continue
            lease = leases / f"{candidate}.json"
            if lease.exists() or lease.is_symlink():
                if lease.is_symlink() or not lease.is_file():
                    continue
                try:
                    document = json.loads(
                        lease.read_text(encoding="utf-8")
                    )
                    owner_pid = document.get("pid")
                    if type(owner_pid) is int and owner_pid > 0:
                        os.kill(owner_pid, 0)
                        continue
                except (OSError, UnicodeError, json.JSONDecodeError):
                    pass
                if not _port_is_available(candidate):
                    continue
                try:
                    lease.unlink()
                except OSError:
                    continue
            if not _port_is_available(candidate):
                continue
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(lease, flags, 0o600)
            except FileExistsError:
                continue
            try:
                try:
                    os.fchmod(descriptor, 0o600)
                    payload = json.dumps(
                        {
                            "pid": os.getpid(),
                            "runId": run_dir.name,
                            "sessionId": session_id,
                        },
                        separators=(",", ":"),
                    ).encode("utf-8")
                    offset = 0
                    while offset < len(payload):
                        written = os.write(descriptor, payload[offset:])
                        if written <= 0:
                            raise CliError(
                                "Claudex proxy lease write stalled"
                            )
                        offset += written
                    os.fsync(descriptor)
                except (OSError, CliError):
                    lease.unlink(missing_ok=True)
                    raise
            finally:
                os.close(descriptor)
            port_file = run_dir / "claudex-proxy-port"
            port_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            port_flags |= getattr(os, "O_CLOEXEC", 0)
            port_flags |= getattr(os, "O_NOFOLLOW", 0)
            try:
                port_descriptor = os.open(port_file, port_flags, 0o600)
                try:
                    os.fchmod(port_descriptor, 0o600)
                    payload = f"{candidate}\n".encode("ascii")
                    offset = 0
                    while offset < len(payload):
                        written = os.write(
                            port_descriptor, payload[offset:]
                        )
                        if written <= 0:
                            raise CliError(
                                "session Claudex proxy port write stalled"
                            )
                        offset += written
                    os.fsync(port_descriptor)
                finally:
                    os.close(port_descriptor)
            except (OSError, CliError) as error:
                port_file.unlink(missing_ok=True)
                lease.unlink(missing_ok=True)
                raise CliError(
                    "session Claudex proxy port could not be recorded"
                ) from error
            return candidate
    finally:
        os.close(lock_descriptor)
    raise CliError("no Claudex proxy port is available")


def _materialize_session_claudex_config(
    source: Path,
    prepared: PreparedLaunch,
    proxy_port: int,
    inherited_environment: Mapping[str, str],
) -> Path:
    content = _read_stable_file(source, "Claudex configuration", 1024 * 1024)
    marker = b'X-Orichum-Session-ID = "unbound"'
    if content.count(marker) != 1:
        raise CliError("Claudex configuration lacks the Orichum session marker")
    content = content.replace(
        marker,
        f'X-Orichum-Session-ID = "{prepared.logical.id}"'.encode("ascii"),
    )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise CliError("Claudex configuration is not UTF-8") from error
    proxy_pattern = re.compile(r"(?m)^proxy_port = [0-9]+$")
    if len(proxy_pattern.findall(text)) != 1:
        raise CliError("Claudex configuration lacks one proxy port")
    text = proxy_pattern.sub(f"proxy_port = {proxy_port}", text)
    if "[profiles.extra_env]" in text:
        raise CliError("Claudex configuration already defines profile environment")
    real_home = inherited_environment.get("HOME") or str(Path.home())
    restored_environment = {"HOME": real_home}
    for name in ("XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        value = inherited_environment.get(name)
        if value:
            restored_environment[name] = value
    extra_environment = "\n".join(
        f"{name} = {json.dumps(value)}"
        for name, value in restored_environment.items()
    )
    content = (
        text.rstrip()
        + "\n\n[profiles.extra_env]\n"
        + extra_environment
        + "\n"
    ).encode("utf-8")
    output = prepared.physical.run_dir / "claudex.toml"
    descriptor = os.open(
        output,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(content):
            written = os.write(descriptor, content[offset:])
            if written <= 0:
                raise CliError("session Claudex configuration write stalled")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return output


def _github_config_for_session(
    paths: Mapping[str, Path], physical: SessionPaths
) -> Path | None:
    try:
        physical_context = json.loads(
            _read_stable_file(
                physical.context_file,
                "session project context",
                2 * 1024 * 1024,
            )
        )
        physical_route = physical_context.get("route")
        github_account = (
            physical_route.get("githubAccount")
            if isinstance(physical_route, dict)
            else None
        )
        return (
            ensure_github_identity(paths["data"], github_account)
            if github_account is not None
            else None
        )
    except (
        GithubIdentityError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as error:
        raise CliError("project GitHub identity is unavailable") from error


def _materialize_launch_policy(
    policy: Path,
    physical: SessionPaths,
    handoff: str | None,
) -> Path:
    try:
        policy_bytes = _read_stable_file(
            policy, "controller policy", 1024 * 1024
        )
        context = json.loads(
            _read_stable_file(
                physical.context_file,
                "session project context",
                2 * 1024 * 1024,
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CliError("session launch policy could not be prepared") from error
    route = context.get("route") if isinstance(context, dict) else None
    if not isinstance(route, dict):
        raise CliError("session binding is unavailable")
    scope = route.get("scope", "context")
    atlassian_configured = route.get("atlassianConfigured")
    jira_profile = route.get("jiraProfile")
    github_account = route.get("githubAccount")
    project_config_source = route.get("projectConfigSource")
    if scope == "normal":
        workspace_root = context.get("launchDirReal")
        if (
            not isinstance(workspace_root, str)
            or not workspace_root
            or atlassian_configured is not False
            or jira_profile is not None
            or github_account is not None
            or project_config_source is not None
        ):
            raise CliError("normal session binding is invalid")
        scope_label = "User normal scope"
        config_label = "none"
    elif scope == "context":
        workspace_root = route.get("contextRootReal")
        if (
            not isinstance(workspace_root, str)
            or not workspace_root
            or type(atlassian_configured) is not bool
            or (
                jira_profile is not None
                and (not isinstance(jira_profile, str) or not jira_profile)
            )
            or (
                github_account is not None
                and (not isinstance(github_account, str) or not github_account)
            )
            or (
                project_config_source is not None
                and (
                    not isinstance(project_config_source, str)
                    or not project_config_source
                )
            )
        ):
            raise CliError("session project binding is invalid")
        scope_label = "Project context"
        config_label = (
            "none"
            if project_config_source is None
            else json.dumps(project_config_source)
        )
    else:
        raise CliError("session binding scope is invalid")

    def shown(value: str | None) -> str:
        return "none" if value is None else json.dumps(value)

    leanctx_binding = (
        "- LeanCTX project memory follows the verified project root.\n\n"
        if scope == "context"
        else "- LeanCTX follows the verified workspace root.\n\n"
    )
    binding = (
        "\n\n## Verified Orichum session bindings\n\n"
        "These values are frozen and authoritative for this physical session:\n\n"
        f"- Scope: {scope_label}\n"
        f"- Workspace root: {json.dumps(workspace_root)}\n"
        f"- Jira configured: {'yes' if atlassian_configured else 'no'}\n"
        f"- Jira profile: {shown(jira_profile)}\n"
        f"- GitHub account: {shown(github_account)}\n"
        f"- Repository configuration file: {config_label}\n"
        + leanctx_binding
        + "When Jira is configured, the `atlassian` MCP server is already "
        "bound to this physical session and project. Diagnose empty or "
        "rejected Jira results against that project's credentials and "
        "upstream permissions.\n"
    ).encode("utf-8")
    payload = policy_bytes + binding
    if handoff is not None:
        payload += (
            b"\n\n## Explicit session handoff\n\n"
            + handoff.encode("utf-8")
            + b"\n"
        )

    launch_policy = physical.run_dir / "launch-policy.md"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(launch_policy, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise CliError("session launch policy write stalled")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return launch_policy


def _managed_python_entrypoint(data_home: Path) -> str:
    private_root = data_home / "python"
    entrypoint = data_home / "bin" / "orichum-python"
    try:
        root_stat = private_root.lstat()
        entrypoint_stat = entrypoint.lstat()
        resolved_root = private_root.resolve(strict=True)
        resolved_interpreter = entrypoint.resolve(strict=True)
        interpreter_stat = resolved_interpreter.stat()
        resolved_interpreter.relative_to(resolved_root)
    except (OSError, RuntimeError, ValueError) as error:
        raise CliError("private Orichum Python is unavailable or unsafe") from error
    if (
        stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or not stat.S_ISLNK(entrypoint_stat.st_mode)
        or not stat.S_ISREG(interpreter_stat.st_mode)
        or root_stat.st_uid != os.getuid()
        or interpreter_stat.st_uid != os.getuid()
        or stat.S_IMODE(root_stat.st_mode) & 0o022
        or stat.S_IMODE(interpreter_stat.st_mode) & 0o022
        or not os.access(resolved_interpreter, os.X_OK)
    ):
        raise CliError("private Orichum Python is unavailable or unsafe")
    try:
        if not os.path.samefile(resolved_interpreter, sys.executable):
            raise CliError("Orichum CLI is not running on its private Python")
    except OSError as error:
        raise CliError("private Orichum Python identity is unavailable") from error
    return str(entrypoint)


def _session_environment(
    prepared: PreparedLaunch,
    paths: Mapping[str, Path],
    runtime: Mapping[str, object],
    github_config: Path | None,
    claudex_config: Path,
) -> dict[str, str]:
    physical = prepared.physical
    orichum_home = paths.get("home", paths["data"])
    orichum_cache = paths.get("cache", orichum_home / "cache")
    claudex_home = physical.run_dir / "claudex-home"
    claudex_home.mkdir(mode=0o700)
    claudex_home.chmod(0o700)
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CLAUDEX_")
    }
    for key in (
        "CLAUDE_CODE_SUBAGENT_MODEL",
        "CLAUDE_CODE_DISABLE_WORKFLOWS",
        "CLAUDE_CODE_EFFORT_LEVEL",
        "ANTHROPIC_CUSTOM_HEADERS",
    ):
        environment.pop(key, None)
    if github_config is not None:
        for key in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GITHUB_ENTERPRISE_TOKEN",
            "GH_HOST",
        ):
            environment.pop(key, None)
    managed_python = _managed_python_entrypoint(paths["data"])
    environment.update(
        {
            "HOME": str(claudex_home),
            "ORICHUM_HOME": str(orichum_home),
            "ORICHUM_SESSION_ID": prepared.logical.id,
            "CLAUDEX_RESUME_HINT": f"orichum resume {prepared.logical.id}",
            "ORICHUM_STATE_HOME": str(paths["state"]),
            "ORICHUM_CONFIG_HOME": str(paths["config"]),
            "ORICHUM_DATA_HOME": str(paths["data"]),
            "ORICHUM_CACHE_HOME": str(orichum_cache),
            "ORICHUM_PYTHON": managed_python,
            "ORICHUM_PYTHON_VALIDATED": managed_python,
            "CLAUDEX_CONFIG_FILE": str(claudex_config),
            "CLAUDEX_MCP_CONFIG": str(physical.mcp_file),
            "CLAUDEX_RUN_DIR": str(physical.run_dir),
            "CLAUDEX_CONTEXT_FILE": str(physical.context_file),
            "CLAUDEX_CONTEXT_SHA256": physical.context_sha256,
            "CLAUDEX_EFFECTIVE_MODELS_FILE": str(
                physical.effective_models_file
            ),
            "CLAUDEX_RUN_ID": physical.run_id,
            "CLAUDEX_WORKFLOW_ROOT": str(WORKFLOW_ROOT),
            "CLAUDEX_DATA_DIR": str(paths["data"]),
            "CLAUDE_CONFIG_DIR": str(paths["data"] / "claude-config"),
            "CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY": str(
                runtime["maxToolUseConcurrency"]
            ),
            "CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION": str(
                runtime["maxSubagentsPerSession"]
            ),
            "CLAUDE_CODE_MAX_RETRIES": "2",
            "ENABLE_TOOL_SEARCH": "true",
            "CLAUDE_CODE_ALWAYS_ENABLE_EFFORT": "1",
            "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION": "false",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
        }
    )
    if prepared.logical.controller.primary.family == "gpt":
        environment.update(
            {
                "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
                "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "82",
            }
        )
    for name in ("XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        if name in os.environ:
            isolated = claudex_home / (
                "cache" if name == "XDG_CACHE_HOME" else "runtime"
            )
            isolated.mkdir(mode=0o700)
            isolated.chmod(0o700)
            environment[name] = str(isolated)
    if github_config is not None:
        environment["GH_CONFIG_DIR"] = str(github_config)
    return environment


def _logical_model_display_name(model: str) -> str:
    components = model.split("-")
    if components and components[0] in {"claude", "gemini", "kimi"}:
        components = components[1:]
    return " ".join(component.title() for component in components)


def _set_terminal_title(
    prepared: PreparedLaunch,
    *,
    stream: TextIO = sys.stderr,
    environment: Mapping[str, str] = os.environ,
) -> None:
    if environment.get("TERM", "").lower() == "dumb" or not stream.isatty():
        return
    title = (
        f"Orichum — {prepared.logical.project_root.name} — "
        f"{_logical_model_display_name(
            prepared.logical.controller.primary.logical_model
        )}"
    )
    if any(ord(character) < 32 or ord(character) == 127 for character in title):
        return
    stream.write(f"\x1b]0;{title[:120]}\x07")
    stream.flush()


def _launch_session(
    prepared: PreparedLaunch,
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    *,
    resume: bool,
    arguments: Sequence[str],
    handoff: str | None = None,
) -> None:
    user_arguments = _validate_user_claude_arguments(arguments)
    claudex = paths["data"] / "bin" / "claudex"
    shared_claudex_config = (
        paths["data"] / "model-config" / "current" / "claudex.toml"
    )
    policy = paths["config"] / "controller-policy.md"
    for path, label in (
        (claudex, "Claudex runtime"),
        (shared_claudex_config, "Claudex configuration"),
        (policy, "controller policy"),
    ):
        if not path.is_file() or path.is_symlink():
            raise CliError(f"{label} is unavailable")
    runtime_ports = _runtime_service_ports(paths)
    claudex_config = _materialize_session_claudex_config(
        shared_claudex_config,
        prepared,
        _reserve_session_claudex_port(
            paths["state"],
            prepared.physical.run_dir,
            prepared.logical.id,
            runtime_ports["claudexProxyPort"],
            frozenset(
                runtime_ports[name]
                for name in (
                    "cliproxyPort",
                    "leanctxProxyPort",
                    "routeProxyPort",
                )
            ),
        ),
        os.environ,
    )
    runtime = config.documents["runtime"]["controller"]
    physical = prepared.physical
    github_config = _github_config_for_session(paths, physical)
    environment = _session_environment(
        prepared,
        paths,
        runtime,
        github_config,
        claudex_config,
    )
    launch_policy = _materialize_launch_policy(
        policy, physical, handoff
    )
    command = [
        str(claudex),
        "--config",
        str(claudex_config),
        "run",
        "gpt",
        "--model",
        physical.controller_model,
        "--mcp-config",
        str(physical.mcp_file),
        "--strict-mcp-config",
        "--allowedTools",
        ",".join(
            (
                "Workflow",
                *(
                    f"mcp__leanctx__{tool}"
                    for tool in LEANCTX_AUTO_APPROVED_TOOLS
                ),
            )
        ),
        "--effort",
        runtime["effort"],
        "--append-system-prompt-file",
        str(launch_policy),
        "--plugin-dir",
        str(physical.plugin_dir),
    ]
    if resume:
        command.extend(["--resume", prepared.logical.claude_session_id])
    else:
        command.extend(["--session-id", prepared.logical.claude_session_id])
    command.extend(user_arguments)
    _set_terminal_title(prepared, environment=environment)
    os.execvpe(str(claudex), command, environment)


def _deferred(label: str) -> int:
    print(f"ERROR: {label} not yet installed", file=sys.stderr)
    return 2


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return number


def _economics_hours(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 168:
        raise argparse.ArgumentTypeError("value must be between 1 and 168")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orichum",
        description=(
            "Run project-aware AI sessions and manage Orichum's local "
            "control plane."
        ),
        epilog=(
            "Run 'orichum COMMAND --help' for command-specific options. "
            "Forward Claude Code arguments with 'orichum run -- ARG ...'."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"Orichum {_release_version()}",
        help="print the installed Orichum version and exit",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    def command(
        subparsers: argparse._SubParsersAction,
        name: str,
        summary: str,
        *,
        description: str | None = None,
    ) -> argparse.ArgumentParser:
        return subparsers.add_parser(
            name,
            help=summary,
            description=description or summary,
        )

    run = command(
        commands,
        "run",
        "Start a project-aware session.",
        description=(
            "Create a logical session for the current project and launch "
            "Claude Code through Orichum's validated routing control plane."
        ),
    )
    run.add_argument(
        "--leanctx-profile",
        choices=LEANCTX_PROFILES,
        default=DEFAULT_LEANCTX_PROFILE,
        help="resident LeanCTX tool profile (default: lean)",
    )
    run.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        metavar="CLAUDE_ARG",
        help="Claude Code arguments forwarded after '--'",
    )

    setup = command(
        commands,
        "setup",
        "Complete first-run setup.",
        description=(
            "Configure a provider account, reconcile the installed runtime, "
            "prepare a model stack, map a project, and verify readiness."
        ),
    )
    setup.add_argument(
        "--user",
        action="store_true",
        help="configure the user normal scope for non-project work",
    )
    setup.add_argument(
        "--verbose",
        action="store_true",
        help="stream technical diagnostics while retaining the private log",
    )
    set_completion(
        setup.add_argument(
            "project",
            nargs="?",
            metavar="PROJECT",
            help="project root or parent directory; prompts when omitted",
        ),
        "directory",
    )

    configure = command(
        commands,
        "configure",
        "Review and tune one project.",
        description=(
            "Review models, accounts, health, and advanced settings for one "
            "Orichum project from a compact guided dashboard."
        ),
    )
    set_completion(
        configure.add_argument(
            "--project",
            metavar="PROJECT",
            help="configured project root; defaults to the current project",
        ),
        "context",
    )
    configure.add_argument(
        "--verbose",
        action="store_true",
        help="stream technical diagnostics while retaining the private log",
    )

    config = command(
        commands,
        "config",
        "Inspect and validate configuration.",
    )
    config_action = config.add_subparsers(
        dest="config_command",
        required=True,
        metavar="COMMAND",
    )
    show_config = command(
        config_action,
        "show",
        "Show the merged configuration.",
    )
    show_config.add_argument(
        "--raw",
        action="store_true",
        help="include unredacted values for local troubleshooting",
    )
    for name, help_text in (
        ("validate", "Validate the focused control plane."),
        ("paths", "Print configuration and data paths."),
    ):
        command(config_action, name, help_text)

    context = command(
        commands,
        "context",
        "Manage project contexts.",
    )
    context_action = context.add_subparsers(
        dest="context_command",
        required=True,
        metavar="COMMAND",
    )
    add_context_commands(context_action)

    models = command(
        commands,
        "models",
        "Inspect models and resolved stacks.",
    )
    model_action = models.add_subparsers(
        dest="models_command",
        required=True,
        metavar="COMMAND",
    )
    command(model_action, "list", "List declared models.")
    command(model_action, "stacks", "List configured stacks.")
    command(model_action, "validate", "Validate model routing.")
    resolve = command(
        model_action,
        "resolve",
        "Resolve effective routes for a stack.",
    )
    set_completion(
        resolve.add_argument(
            "stack",
            nargs="?",
            metavar="STACK",
            help="stack name; defaults to the configured default stack",
        ),
        "stack",
    )

    stack = command(
        commands,
        "stack",
        "Configure model stacks.",
    )
    stack_action = stack.add_subparsers(
        dest="stack_command",
        required=True,
        metavar="COMMAND",
    )
    command(
        stack_action,
        "available",
        "Show live provider and model choices.",
    )
    command(stack_action, "list", "List configured stacks.")
    show_stack = command(
        stack_action,
        "show",
        "Inspect one stack.",
    )
    set_completion(
        show_stack.add_argument(
            "name",
            metavar="STACK",
            help="configured stack name",
        ),
        "stack",
    )
    command(
        stack_action,
        "configure",
        "Create or edit a stack interactively.",
    )

    provider = command(
        commands,
        "provider",
        "Manage provider accounts.",
    )
    provider_action = provider.add_subparsers(
        dest="provider_command",
        required=True,
        metavar="COMMAND",
    )
    command(
        provider_action,
        "list",
        "List configured provider adapters.",
    )
    command(
        provider_action,
        "configure",
        "Configure and register an account interactively.",
    )
    login = command(
        provider_action,
        "login",
        "Authenticate through CLIProxyAPI.",
    )
    set_completion(
        login.add_argument(
            "login_type",
            metavar="TYPE",
            help="provider login type",
        ),
        "auth-type",
    )
    login.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        metavar="LOGIN_ARG",
        help="arguments forwarded to the provider login flow",
    )
    command(
        provider_action,
        "accounts",
        "List named accounts.",
    )
    account = command(
        provider_action,
        "account",
        "Manage one named account.",
    )
    account_action = account.add_subparsers(
        dest="account_command",
        required=True,
        metavar="COMMAND",
    )
    add = command(account_action, "add", "Register an account.")
    add.add_argument("name", metavar="NAME", help="account display name")
    set_completion(
        add.add_argument(
            "provider",
            metavar="PROVIDER",
            help="configured provider adapter",
        ),
        "provider",
    )
    set_completion(
        add.add_argument(
            "credential_ref",
            metavar="CREDENTIAL_FILE",
            help="managed provider credential file",
        ),
        "file",
    )
    set_completion(
        add.add_argument(
            "pool",
            metavar="POOL",
            help="account pool to join",
        ),
        "pool",
    )
    add.add_argument(
        "--priority",
        default="primary",
        metavar="VALUE",
        help="priority alias or numeric weight (default: primary)",
    )
    rename = command(account_action, "rename", "Rename an account.")
    set_completion(
        rename.add_argument(
            "selector",
            metavar="ACCOUNT",
            help="account ID, name, or alias",
        ),
        "account",
    )
    rename.add_argument("name", metavar="NAME", help="new display name")
    priority = command(
        account_action,
        "priority",
        "Change account priority.",
    )
    set_completion(
        priority.add_argument(
            "selector",
            metavar="ACCOUNT",
            help="account ID, name, or alias",
        ),
        "account",
    )
    priority.add_argument(
        "priority",
        metavar="VALUE",
        help="priority alias or numeric weight",
    )
    for name, help_text in (
        ("enable", "Enable an account."),
        ("disable", "Disable an account."),
        ("remove", "Remove an account."),
    ):
        account_command = command(account_action, name, help_text)
        set_completion(
            account_command.add_argument(
                "selector",
                metavar="ACCOUNT",
                help="account ID, name, or alias",
            ),
            "account",
        )
    sync = command(
        account_action,
        "sync",
        "Reconcile account credentials.",
    )
    set_completion(
        sync.add_argument(
            "selector",
            nargs="?",
            metavar="ACCOUNT",
            help="account ID, name, or alias; defaults to every account",
        ),
        "account",
    )

    plugin = command(
        commands,
        "plugin",
        "Manage optional Claude Code plugins.",
    )
    plugin_action = plugin.add_subparsers(
        dest="plugin_command",
        required=True,
        metavar="COMMAND",
    )
    command(plugin_action, "list", "List declared plugins.")
    plugin_add = command(
        plugin_action,
        "add",
        "Declare, install, and enable a plugin.",
    )
    set_completion(
        plugin_add.add_argument(
            "plugin",
            metavar="PLUGIN@MARKETPLACE",
            help="Claude Code plugin identifier",
        ),
        "plugin-add",
    )
    plugin_add.add_argument(
        "--source",
        metavar="SOURCE",
        help="marketplace source when it is not already declared",
    )
    plugin_remove = command(
        plugin_action,
        "remove",
        "Uninstall and remove a declared plugin.",
    )
    set_completion(
        plugin_remove.add_argument(
            "plugin",
            metavar="PLUGIN@MARKETPLACE",
            help="declared Claude Code plugin identifier",
        ),
        "plugin",
    )
    command(plugin_action, "sync", "Reconcile declared plugins.")
    command(plugin_action, "update", "Update declared plugins.")

    leanctx = command(
        commands,
        "leanctx",
        "Inspect and monitor LeanCTX.",
    )
    leanctx_action = leanctx.add_subparsers(
        dest="leanctx_command",
        required=True,
        metavar="COMMAND",
    )
    leanctx_list = command(
        leanctx_action,
        "list",
        "List recent LeanCTX runs.",
    )
    leanctx_list.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="number of newest runs to show (default: 20)",
    )
    leanctx_list.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="show every run",
    )
    for name, help_text in (
        ("stats", "Show exact context savings."),
        ("watch", "Open the terminal monitor."),
    ):
        leanctx_command = command(leanctx_action, name, help_text)
        set_completion(
            leanctx_command.add_argument(
                "--run",
                metavar="RUN",
                help=(
                    "physical LeanCTX run ID; defaults to the current project"
                ),
            ),
            "run",
        )
    economics = command(
        leanctx_action,
        "economics",
        "Show profile footprint and savings estimates.",
    )
    set_completion(
        economics.add_argument(
            "--session",
            metavar="SESSION",
            help="logical session ID; defaults to the current live session",
        ),
        "logical-session",
    )
    economics.add_argument(
        "--hours",
        type=_economics_hours,
        default=24,
        metavar="HOURS",
        help="rolling ledger window from 1 through 168 hours (default: 24)",
    )
    dashboard = command(
        leanctx_action,
        "dashboard",
        "Open the local authenticated Observatory.",
    )
    set_completion(
        dashboard.add_argument(
            "--run",
            metavar="RUN",
            help="physical LeanCTX run ID; defaults to the current project",
        ),
        "run",
    )
    dashboard.add_argument(
        "--port",
        type=int,
        metavar="PORT",
        help="loopback port; defaults to the first available port",
    )
    dashboard.add_argument(
        "--open",
        dest="open_mode",
        choices=("browser", "none", "vscode"),
        default="browser",
        help="how to open the Observatory (default: browser)",
    )
    command(
        commands,
        "doctor",
        "Verify the complete local installation.",
    )
    status = command(
        commands,
        "status",
        "Show live identity, routing, and quota for a session.",
    )
    set_completion(
        status.add_argument(
            "session_id",
            nargs="?",
            metavar="SESSION",
            help="logical session ID; defaults to the current live session",
        ),
        "logical-session",
    )
    sessions = command(
        commands,
        "sessions",
        "Inspect and clean sessions.",
    )
    sessions.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="number of newest sessions to show (default: 20)",
    )
    sessions.add_argument(
        "--all",
        dest="show_all",
        action="store_true",
        help="show every logical session",
    )
    sessions_action = sessions.add_subparsers(
        dest="sessions_command",
        metavar="COMMAND",
    )
    sessions_routes = command(
        sessions_action,
        "routes",
        "Inspect frozen routes for one session.",
    )
    set_completion(
        sessions_routes.add_argument(
            "session_id",
            metavar="SESSION",
            help="Orichum logical or Claude session ID",
        ),
        "logical-session",
    )
    sessions_cleanup = command(
        sessions_action,
        "cleanup",
        "Preview or remove inactive physical launch snapshots.",
    )
    sessions_cleanup.add_argument(
        "--older-than",
        type=int,
        default=7,
        metavar="DAYS",
        help="minimum snapshot age in days (default: 7)",
    )
    sessions_cleanup.add_argument(
        "--yes",
        action="store_true",
        help="remove the previewed snapshots",
    )
    sessions_remove = command(
        sessions_action,
        "remove",
        "Preview or remove one inactive logical session.",
    )
    set_completion(
        sessions_remove.add_argument(
            "session_id",
            metavar="SESSION",
            help="Orichum or Claude session ID",
        ),
        "logical-session",
    )
    sessions_remove.add_argument(
        "--yes",
        action="store_true",
        help="remove the previewed logical session",
    )
    sessions_clear = command(
        sessions_action,
        "clear",
        "Preview or remove all inactive logical sessions.",
    )
    sessions_clear.add_argument(
        "--yes",
        action="store_true",
        help="remove the previewed logical sessions",
    )
    session = command(
        commands,
        "session",
        "Inspect one logical session.",
    )
    session_action = session.add_subparsers(
        dest="session_command",
        required=True,
        metavar="COMMAND",
    )
    session_routes = command(
        session_action,
        "routes",
        "Inspect frozen routes.",
    )
    set_completion(
        session_routes.add_argument(
            "session_id",
            metavar="SESSION",
            help="Orichum logical or Claude session ID",
        ),
        "logical-session",
    )
    resume = command(
        commands,
        "resume",
        "Resume by Orichum or Claude session ID.",
    )
    set_completion(
        resume.add_argument(
            "session_id",
            metavar="SESSION",
            help="Orichum logical or Claude session ID",
        ),
        "logical-session",
    )
    resume.add_argument(
        "arguments",
        nargs=argparse.REMAINDER,
        metavar="CLAUDE_ARG",
        help="Claude Code arguments forwarded after the session ID",
    )
    fork = command(
        commands,
        "fork",
        "Fork a session onto another stack.",
    )
    set_completion(
        fork.add_argument(
            "session_id",
            metavar="SESSION",
            help="Orichum logical or Claude parent session ID",
        ),
        "logical-session",
    )
    set_completion(
        fork.add_argument(
            "--stack",
            metavar="STACK",
            help="target stack; defaults to the parent stack",
        ),
        "stack",
    )
    set_completion(
        fork.add_argument(
            "--handoff-file",
            type=Path,
            metavar="FILE",
            help="UTF-8 handoff document to attach to the child session",
        ),
        "file",
    )
    fork.add_argument(
        "--leanctx-profile",
        choices=LEANCTX_PROFILES,
        help="override the parent LeanCTX tool profile",
    )
    completion = command(
        commands,
        "completion",
        "Generate native shell completion definitions.",
    )
    completion.add_argument(
        "shell",
        choices=("zsh", "bash", "fish"),
        help="shell definition to generate",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    try:
        if parsed.command is None:
            raise CliError("run Orichum through the installed launcher")
        if parsed.command == "context" and parsed.context_command != "list":
            return _run_external("orichum-context", arguments[1:])
        if parsed.command == "plugin":
            return _run_external("orichum-plugin", arguments[1:])
        if parsed.command == "doctor":
            return _run_external("orichum-doctor", [])
        if parsed.command == "completion":
            print(render_completion(parser, parsed.shell), end="")
            return 0
        if parsed.command == "config" and parsed.config_command == "paths":
            paths = _paths()
            print(
                json.dumps(
                    {name: str(path) for name, path in paths.items()},
                    sort_keys=True,
                )
            )
            return 0
        if (
            parsed.command == "stack"
            and parsed.stack_command == "configure"
            and not _interactive_terminal()
        ):
            raise CliError(
                "stack configuration requires an interactive terminal"
            )
        if (
            parsed.command == "provider"
            and parsed.provider_command == "configure"
            and not _interactive_terminal()
        ):
            raise CliError(
                "provider configuration requires an interactive terminal"
            )
        if parsed.command == "setup" and not _interactive_terminal():
            raise CliError("setup requires an interactive terminal")
        if parsed.command == "configure" and not _interactive_terminal():
            raise CliError("configure requires an interactive terminal")
        if parsed.command == "status":
            session_id = parsed.session_id or os.environ.get(
                "ORICHUM_SESSION_ID"
            )
            if not session_id:
                raise CliError(
                    "a logical session ID is required; run "
                    "orichum status <session-id>"
                )
            if not re.fullmatch(r"oc-s-[a-f0-9]{16}", session_id):
                raise CliError("logical session ID is invalid")
        if (
            parsed.command == "leanctx"
            and parsed.leanctx_command == "economics"
            and not (parsed.session or os.environ.get("ORICHUM_SESSION_ID"))
        ):
            raise CliError(
                "a logical session ID is required; run "
                "orichum leanctx economics --session <session-id>"
            )
        paths, config = _load()
        if parsed.command == "setup":
            return _setup(
                paths,
                config,
                parsed.project,
                normal_scope=parsed.user,
                verbose=parsed.verbose,
            )
        if parsed.command == "configure":
            project = (
                Path(parsed.project).expanduser()
                if parsed.project is not None
                else Path.cwd()
            )
            return run_configure(
                paths,
                config,
                project,
                verbose=parsed.verbose,
            )
        if parsed.command == "status":
            print(_session_status(paths, session_id), end="")
            return 0
        if parsed.command == "provider" and parsed.provider_command == "login":
            return _run_external(
                "orichum-login",
                [parsed.login_type, *parsed.arguments],
            )
        if parsed.command == "leanctx":
            if parsed.leanctx_command == "economics":
                identifier = parsed.session or os.environ.get(
                    "ORICHUM_SESSION_ID"
                )
                if not identifier:
                    raise CliError(
                        "a logical session ID is required; run "
                        "orichum leanctx economics --session <session-id>"
                    )
                logical = resolve_logical_session(paths["state"], identifier)
                attached_runs = tuple(
                    run
                    for run in leanctx_monitor.discover_runs(
                        WORKFLOW_ROOT,
                        paths["data"],
                    )
                    if run.attached
                )
                selected = leanctx_monitor.select_run(
                    attached_runs,
                    logical.project_root,
                    None,
                )
                binary = leanctx_monitor.managed_binary(paths["data"])
                health = leanctx_monitor.read_tool_health(binary, selected)
                rolling = leanctx_monitor.read_rolling_economics(
                    paths["data"],
                    parsed.hours,
                )
                gain = leanctx_monitor.read_gain_summary(binary, selected)
                print(
                    _leanctx_economics(logical, health, rolling, gain),
                    end="",
                )
                return 0
            runs = leanctx_monitor.discover_runs(
                WORKFLOW_ROOT,
                paths["data"],
            )
            if parsed.leanctx_command == "list":
                project_root = _leanctx_project_root(config, Path.cwd())
                selected_run_id = None
                attached_runs = tuple(run for run in runs if run.attached)
                if project_root is not None:
                    try:
                        selected_run_id = leanctx_monitor.select_run(
                            attached_runs,
                            project_root,
                            None,
                            current_run_id=os.environ.get(
                                "CLAUDEX_RUN_ID"
                            ),
                        ).run_id
                    except leanctx_monitor.LeanctxMonitorError:
                        pass
                shown = (
                    runs
                    if parsed.show_all
                    else attached_runs[: parsed.limit]
                )
                print(_leanctx_list(shown, selected_run_id), end="")
                if not parsed.show_all:
                    messages = []
                    if len(attached_runs) > len(shown):
                        messages.append(
                            f"Showing newest {len(shown)} of "
                            f"{len(attached_runs)} attached runs."
                        )
                    historical = len(runs) - len(attached_runs)
                    if historical:
                        suffix = "" if historical == 1 else "s"
                        messages.append(
                            f"Use --all to include {historical} "
                            f"historical run{suffix}."
                        )
                    if messages:
                        print(" ".join(messages))
                return 0
            project_root = (
                None
                if parsed.run is not None
                else _leanctx_project_root(config, Path.cwd())
            )
            selected = leanctx_monitor.select_run(
                runs,
                project_root,
                parsed.run,
                current_run_id=os.environ.get("CLAUDEX_RUN_ID"),
            )
            leanctx_monitor.require_attached(selected)
            binary = leanctx_monitor.managed_binary(paths["data"])
            if parsed.leanctx_command == "stats":
                stats = leanctx_monitor.read_stats(binary, selected)
                ports = _runtime_service_ports(paths)
                proxy_stats = leanctx_monitor.read_proxy_stats(
                    binary,
                    paths["data"],
                    ports["leanctxProxyPort"],
                )
                print(
                    _leanctx_stats(selected, stats, proxy_stats),
                    end="",
                )
                return 0
            if parsed.leanctx_command == "watch":
                return leanctx_monitor.run_watch(binary, selected)
            return leanctx_monitor.run_dashboard(
                binary,
                selected,
                paths["state"],
                port=parsed.port,
                open_mode=parsed.open_mode,
            )
        if parsed.command == "run":
            prepared = _prepare_new_session(
                paths,
                config,
                launch_dir=Path.cwd(),
                leanctx_profile=parsed.leanctx_profile,
            )
            _launch_session(
                prepared,
                paths,
                config,
                resume=False,
                arguments=parsed.arguments,
            )
            raise AssertionError("session launch returned unexpectedly")
        if parsed.command in {"session", "sessions"}:
            if (
                parsed.command == "sessions"
                and parsed.sessions_command == "cleanup"
            ):
                cleaned = cleanup_physical_runs(
                    paths["state"],
                    older_than_days=parsed.older_than,
                    apply=parsed.yes,
                )
                print(
                    _physical_cleanup_report(
                        cleaned,
                        older_than_days=parsed.older_than,
                        applied=parsed.yes,
                    ),
                    end="",
                )
                return 0
            if (
                parsed.command == "sessions"
                and parsed.sessions_command == "remove"
            ):
                removed = remove_logical_session(
                    paths["state"],
                    parsed.session_id,
                    apply=parsed.yes,
                )
                print(
                    _logical_cleanup_report(
                        (removed,), applied=parsed.yes
                    ),
                    end="",
                )
                return 0
            if (
                parsed.command == "sessions"
                and parsed.sessions_command == "clear"
            ):
                cleared = clear_logical_sessions(
                    paths["state"],
                    apply=parsed.yes,
                )
                print(
                    _logical_cleanup_report(
                        cleared, applied=parsed.yes
                    ),
                    end="",
                )
                return 0
            route_request = (
                parsed.command == "session"
                or parsed.sessions_command == "routes"
            )
            if route_request:
                logical = load_logical_session(
                    paths["state"], parsed.session_id
                )
                accounts = load_accounts(
                    paths["config"] / "accounts.json"
                )
                validate_account_bindings(
                    accounts, config.documents["providers"]
                )
                print(_session_routes(logical, accounts), end="")
            else:
                logical_sessions = tuple(
                    sorted(
                        list_logical_sessions(paths["state"]),
                        key=lambda session: (
                            session.created_at,
                            session.id,
                        ),
                        reverse=True,
                    )
                )
                shown = (
                    logical_sessions
                    if parsed.show_all
                    else logical_sessions[: parsed.limit]
                )
                print(
                    _session_list(shown),
                    end="",
                )
                if (
                    not parsed.show_all
                    and len(logical_sessions) > len(shown)
                ):
                    print(
                        f"Showing newest {len(shown)} of "
                        f"{len(logical_sessions)} sessions. "
                        "Use --all to show every session."
                    )
            return 0
        if parsed.command == "resume":
            prepared = _prepare_resume(
                paths,
                config,
                identifier=parsed.session_id,
                launch_dir=Path.cwd(),
            )
            _launch_session(
                prepared,
                paths,
                config,
                resume=True,
                arguments=parsed.arguments,
            )
            raise AssertionError("session launch returned unexpectedly")
        if parsed.command == "fork":
            prepared, handoff = _prepare_fork(
                paths,
                config,
                identifier=parsed.session_id,
                launch_dir=Path.cwd(),
                requested_stack=parsed.stack,
                handoff_file=parsed.handoff_file,
                leanctx_profile=parsed.leanctx_profile,
            )
            _launch_session(
                prepared,
                paths,
                config,
                resume=False,
                arguments=(),
                handoff=handoff,
            )
            raise AssertionError("session launch returned unexpectedly")
        if parsed.command == "config":
            if parsed.config_command == "validate":
                return 0
            print(
                json.dumps(
                    _config_show(config, redacted=not parsed.raw),
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if parsed.command == "context":
            print(_context_list(config), end="")
            return 0
        if parsed.command == "stack":
            if parsed.stack_command == "available":
                print(_stack_available(paths, config), end="")
                return 0
            if parsed.stack_command == "list":
                print(_stack_list(config), end="")
                return 0
            if parsed.stack_command == "show":
                print(
                    _stack_show(paths, config, parsed.name),
                    end="",
                )
                return 0
            _verify_runtime(paths)
            return run_stack_wizard(
                paths, config, launch_dir=Path.cwd()
            )
        if parsed.command == "models":
            if parsed.models_command == "list":
                print(_model_list(config), end="")
            elif parsed.models_command == "stacks":
                print(_stack_list(config), end="")
            elif parsed.models_command == "resolve":
                print(
                    json.dumps(
                        _resolve_stack(
                            config,
                            parsed.stack,
                            launch_dir=Path.cwd(),
                        ),
                        indent=2,
                        sort_keys=True,
                    )
                )
            return 0
        if parsed.command == "provider":
            if parsed.provider_command == "list":
                print(_provider_list(config), end="")
            elif parsed.provider_command == "configure":
                return _provider_configure(paths, config)
            elif parsed.provider_command == "accounts":
                accounts = load_accounts(paths["config"] / "accounts.json")
                validate_account_bindings(
                    accounts, config.documents["providers"]
                )
                print(_account_list(accounts), end="")
            else:
                _mutate_account(parsed, paths, config)
            return 0
    except UiCancelled:
        print(
            "Configuration cancelled.\nRun: orichum configure",
            file=sys.stderr,
        )
        return 130
    except (
        AccountError,
        CatalogError,
        CliError,
        CompletionError,
        ConfigError,
        ContextError,
        CredentialError,
        LogicalSessionError,
        leanctx_monitor.LeanctxMonitorError,
        ManagementError,
        OSError,
        RouteError,
        RoutingError,
        SessionError,
        StackBindingError,
        StackStoreError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
