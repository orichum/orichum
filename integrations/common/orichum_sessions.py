#!/usr/bin/env python3
"""Immutable, private logical-session bindings for Orichum."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
from typing import Collection, Mapping, Sequence
from types import MappingProxyType
import uuid

from .account_registry import Account
from .leanctx_profiles import (
    DEFAULT_LEANCTX_PROFILE,
    LEANCTX_PROFILE_FULL,
    validate_leanctx_profile,
)
from .model_routing import (
    EffectiveStack,
    LEGACY_ROLES,
    ROLES,
    RoutingError,
    validate_model_id,
    validate_stack_name,
)
from .route_selection import Route, RouteError, route_chain
from .stack_bindings import StackBindings
from .stack_definition import (
    NormalizedStacks,
    StackCandidate,
    StackDefinitionError,
    normalize_model_stacks,
)


MAX_BINDING_BYTES = 1024 * 1024
_SESSION_ID = re.compile(r"oc-s-[a-f0-9]{16}")
_ACCOUNT_ID = re.compile(r"oc-a-[a-f0-9]{16}")
_PROFILE = re.compile(r"ocp-[a-f0-9]{16}")
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_UPSTREAM = re.compile(
    r"oc-r-[a-f0-9]{16}/[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}"
)
_RUN_ID = re.compile(r"run\.[A-Za-z0-9_]{1,64}")


class LogicalSessionError(RuntimeError):
    """Logical session state failed closed validation."""


@dataclass(frozen=True)
class RouteBinding:
    primary: Route
    fallbacks: tuple[Route, ...]


@dataclass(frozen=True)
class LogicalSession:
    id: str
    claude_session_id: str
    parent_id: str | None
    project_root: Path
    stack: str
    controller: RouteBinding
    agents: Mapping[str, RouteBinding]
    leanctx_profile: str
    created_at: str


@dataclass(frozen=True)
class ResolvedSessionPlan:
    stack: str
    controller: RouteBinding
    agents: Mapping[str, RouteBinding]
    effective: EffectiveStack


@dataclass(frozen=True)
class PhysicalRunCleanup:
    run_id: str
    status: str


@dataclass(frozen=True)
class LogicalSessionCleanup:
    session_id: str
    status: str


def _require_private_directory(path: Path, label: str) -> Path:
    try:
        observed = os.lstat(path)
        resolved = path.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as failure:
        raise LogicalSessionError(f"{label} is unavailable") from failure
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or observed.st_dev != confirmed.st_dev
        or observed.st_ino != confirmed.st_ino
    ):
        raise LogicalSessionError(f"{label} is unsafe")
    return resolved


def _session_root(state_home: Path) -> Path:
    state = _require_private_directory(Path(state_home), "Orichum state directory")
    root = state / "logical-sessions"
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as failure:
        raise LogicalSessionError(
            "logical session directory could not be created"
        ) from failure
    return _require_private_directory(root, "logical session directory")


def _staging_root(state_home: Path) -> Path:
    state = _require_private_directory(Path(state_home), "Orichum state directory")
    root = state / "logical-session-staging"
    try:
        os.mkdir(root, 0o700)
    except FileExistsError:
        pass
    except OSError as failure:
        raise LogicalSessionError(
            "logical session staging directory could not be created"
        ) from failure
    return _require_private_directory(root, "logical session staging directory")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise LogicalSessionError(f"{label} is invalid")
    return value


def _route_json(route: Route) -> dict[str, object]:
    return {
        "accountId": route.account_id,
        "provider": route.provider,
        "family": route.family,
        "logicalModel": route.logical_model,
        "upstreamModel": route.upstream_model,
        "profile": route.claudex_profile,
        "priority": route.priority,
        "pool": route.pool,
    }


def _parse_route(value: object) -> Route:
    keys = {
        "accountId",
        "provider",
        "family",
        "logicalModel",
        "upstreamModel",
        "profile",
        "priority",
        "pool",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise LogicalSessionError("session route has invalid fields")
    account_id = value["accountId"]
    profile = value["profile"]
    upstream = value["upstreamModel"]
    if not isinstance(account_id, str) or not _ACCOUNT_ID.fullmatch(account_id):
        raise LogicalSessionError("session route account ID is invalid")
    if not isinstance(profile, str) or not _PROFILE.fullmatch(profile):
        raise LogicalSessionError("session route profile is invalid")
    if (
        not isinstance(upstream, str)
        or not _UPSTREAM.fullmatch(upstream)
        or "://" in upstream
    ):
        raise LogicalSessionError("session route upstream model is invalid")
    if (
        type(value["priority"]) is not int
        or value["priority"] < 0
        or value["priority"] > 1000
    ):
        raise LogicalSessionError("session route priority is invalid")
    try:
        logical_model = validate_model_id(
            value["logicalModel"], "logical session model"
        )
    except RoutingError as failure:
        raise LogicalSessionError("session route model is invalid") from failure
    return Route(
        account_id=account_id,
        provider=_identifier(value["provider"], "session route provider"),
        family=_identifier(value["family"], "session route family"),
        logical_model=logical_model,
        upstream_model=upstream,
        claudex_profile=profile,
        priority=value["priority"],
        pool=_identifier(value["pool"], "session route pool"),
    )


def _binding_json(binding: RouteBinding) -> dict[str, object]:
    return {
        "primary": _route_json(binding.primary),
        "fallbacks": [_route_json(route) for route in binding.fallbacks],
    }


def _parse_binding(value: object) -> RouteBinding:
    if not isinstance(value, dict) or set(value) != {"primary", "fallbacks"}:
        raise LogicalSessionError("route binding has invalid fields")
    raw_fallbacks = value["fallbacks"]
    if not isinstance(raw_fallbacks, list):
        raise LogicalSessionError("route fallbacks must be an array")
    primary = _parse_route(value["primary"])
    fallbacks = tuple(_parse_route(route) for route in raw_fallbacks)
    if len(fallbacks) > 1:
        raise LogicalSessionError("route binding permits at most one fallback")
    routes = (primary, *fallbacks)
    if any(
        route.family != primary.family
        or route.logical_model != primary.logical_model
        for route in fallbacks
    ):
        raise LogicalSessionError("route fallbacks must remain in family and model")
    if len({route.account_id for route in routes}) != len(routes):
        raise LogicalSessionError("route binding account IDs must be unique")
    if len({route.upstream_model for route in routes}) != len(routes):
        raise LogicalSessionError("route binding upstream models must be unique")
    return RouteBinding(primary=primary, fallbacks=fallbacks)


def _session_json(session: LogicalSession) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "id": session.id,
        "claudeSessionId": session.claude_session_id,
        "parentId": session.parent_id,
        "projectRoot": str(session.project_root),
        "stack": session.stack,
        "controller": _binding_json(session.controller),
        "agents": {
            role: _binding_json(session.agents[role]) for role in ROLES
        },
        "leanctxProfile": session.leanctx_profile,
        "createdAt": session.created_at,
    }


def _parse_session(value: object) -> LogicalSession:
    base_keys = {
        "schemaVersion",
        "id",
        "claudeSessionId",
        "parentId",
        "projectRoot",
        "stack",
        "controller",
        "agents",
        "createdAt",
    }
    if not isinstance(value, dict):
        raise LogicalSessionError("logical session has invalid fields")
    schema = value.get("schemaVersion")
    if type(schema) is not int or schema not in {1, 2}:
        raise LogicalSessionError(
            "logical session schemaVersion must be exactly 1 or 2"
        )
    expected_keys = base_keys if schema == 1 else base_keys | {"leanctxProfile"}
    if set(value) != expected_keys:
        raise LogicalSessionError("logical session has invalid fields")
    if schema == 1:
        leanctx_profile = LEANCTX_PROFILE_FULL
    else:
        try:
            leanctx_profile = validate_leanctx_profile(value["leanctxProfile"])
        except ValueError as failure:
            raise LogicalSessionError("logical session LeanCTX profile is invalid") from failure
    identifier = value["id"]
    parent = value["parentId"]
    if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
        raise LogicalSessionError("logical session ID is invalid")
    if parent is not None and (
        not isinstance(parent, str) or not _SESSION_ID.fullmatch(parent)
    ):
        raise LogicalSessionError("logical session parent is invalid")
    try:
        parsed_uuid = uuid.UUID(value["claudeSessionId"])
    except (AttributeError, TypeError, ValueError) as failure:
        raise LogicalSessionError("Claude session ID is invalid") from failure
    if parsed_uuid.version != 4 or str(parsed_uuid) != value["claudeSessionId"]:
        raise LogicalSessionError("Claude session ID must be canonical UUID v4")
    claude_id = str(parsed_uuid)
    raw_root = value["projectRoot"]
    if not isinstance(raw_root, str) or not Path(raw_root).is_absolute():
        raise LogicalSessionError("logical session project root is invalid")
    project_root = Path(raw_root).resolve(strict=False)
    if str(project_root) != raw_root:
        raise LogicalSessionError("logical session project root is not canonical")
    try:
        stack = validate_stack_name(value["stack"], "logical session stack")
    except RoutingError as failure:
        raise LogicalSessionError("logical session stack is invalid") from failure
    agents = value["agents"]
    legacy_agents = isinstance(agents, dict) and set(agents) == set(LEGACY_ROLES)
    if not isinstance(agents, dict) or (
        set(agents) != set(LEGACY_ROLES)
        and set(agents) != set(ROLES)
    ):
        raise LogicalSessionError("logical session agents are invalid")
    if legacy_agents:
        agents = {**agents, "planning-advisor": agents["architecture-advisor"]}
    created_at = value["createdAt"]
    try:
        parsed_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as failure:
        raise LogicalSessionError("logical session timestamp is invalid") from failure
    if parsed_time.tzinfo != timezone.utc:
        raise LogicalSessionError("logical session timestamp must be UTC")
    return LogicalSession(
        id=identifier,
        claude_session_id=claude_id,
        parent_id=parent,
        project_root=project_root,
        stack=stack,
        controller=_parse_binding(value["controller"]),
        agents=MappingProxyType(
            {role: _parse_binding(agents[role]) for role in ROLES}
        ),
        leanctx_profile=leanctx_profile,
        created_at=created_at,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise LogicalSessionError(f"duplicate session field {key!r}")
        result[key] = value
    return result


def _read_binding(directory: Path) -> bytes:
    path = directory / "binding.json"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as failure:
        raise LogicalSessionError("logical session binding is unavailable") from failure
    try:
        observed = os.fstat(descriptor)
        if (
            not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise LogicalSessionError("logical session binding is unsafe")
        content = os.read(descriptor, MAX_BINDING_BYTES + 1)
        if len(content) > MAX_BINDING_BYTES or os.read(descriptor, 1):
            raise LogicalSessionError("logical session binding is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
        ):
            raise LogicalSessionError("logical session binding changed while reading")
        return content
    finally:
        os.close(descriptor)


def _decode_binding(content: bytes) -> LogicalSession:
    def reject_constant(value: str) -> object:
        raise LogicalSessionError(f"non-finite session value {value}")

    try:
        raw = json.loads(
            content.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as failure:
        raise LogicalSessionError("logical session binding is invalid JSON") from failure
    return _parse_session(raw)


def _write_all(descriptor: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(descriptor, content[offset:])
        if written <= 0:
            raise OSError("logical session write made no progress")
        offset += written


def resolve_session_plan(
    config: Mapping[str, object],
    accounts: Sequence[Account],
    *,
    pools: Sequence[str],
    requested_stack: str | None,
    health: Mapping[str, str],
    selection_ordinal: int,
    bindings: StackBindings | None = None,
    available_models: Collection[str] | None = None,
) -> ResolvedSessionPlan:
    """Resolve and pin every controller/agent route for a new session."""
    try:
        raw_stacks = config["model-stacks"]
        stacks = (
            raw_stacks
            if isinstance(raw_stacks, NormalizedStacks)
            else normalize_model_stacks(raw_stacks)
        )
        provider_document = config["providers"]
        stack_name = requested_stack or stacks.default_stack
        stack = stacks.stacks[stack_name]
    except (KeyError, TypeError, StackDefinitionError) as failure:
        raise LogicalSessionError("session model stack is incomplete") from failure
    try:
        stack_name = validate_stack_name(stack_name, "logical session stack")
    except RoutingError as failure:
        raise LogicalSessionError("session model stack is invalid") from failure
    route_config = {
        "models": stacks.models,
        "providers": provider_document,
    }
    bindings = StackBindings({}) if bindings is None else bindings

    def bind_candidate(
        candidate: StackCandidate, ordinal: int
    ) -> RouteBinding:
        try:
            model = stacks.models[candidate.model]
            locked = bindings.candidate_accounts.get(candidate.id)
            chain = route_chain(
                accounts,
                pools=pools,
                family=model.family,
                logical_model=candidate.model,
                allowed_providers=candidate.providers,
                locked_account_id=locked,
                upstream_by_provider=model.routes,
                config=route_config,
                health=health,
                selection_ordinal=ordinal,
                available_models=available_models,
            )
        except (KeyError, TypeError, RouteError) as failure:
            raise LogicalSessionError(
                f"no safe account route is available for {candidate.model}"
            ) from failure
        return _parse_binding(
            {
                "primary": _route_json(chain[0]),
                "fallbacks": [_route_json(route) for route in chain[1:]],
            }
        )

    controller = None
    controller_failures = []
    for candidate in stack.controller:
        try:
            controller = bind_candidate(candidate, selection_ordinal)
            break
        except LogicalSessionError as failure:
            controller_failures.append(failure)
    if controller is None:
        raise LogicalSessionError(
            "no safe account route is available for controller"
        ) from controller_failures[-1]
    agent_bindings: dict[str, RouteBinding] = {}
    for index, role in enumerate(ROLES, start=1):
        selected = None
        failures = []
        for candidate in stack.agents[role]:
            try:
                selected = bind_candidate(
                    candidate, selection_ordinal + index
                )
                break
            except LogicalSessionError as failure:
                failures.append(failure)
        if selected is None:
            raise LogicalSessionError(
                f"no safe account route is available for role {role}"
            ) from failures[-1]
        agent_bindings[role] = selected
    frozen_agents = MappingProxyType(agent_bindings)
    effective_agents = {
        role: frozen_agents[role].primary.upstream_model for role in ROLES
    }
    effective = EffectiveStack(
        stack_name=stack_name,
        controller=controller.primary.upstream_model,
        candidates={
            role: (effective_agents[role],) for role in ROLES
        },
        agents=effective_agents,
    )
    return ResolvedSessionPlan(
        stack=stack_name,
        controller=controller,
        agents=frozen_agents,
        effective=effective,
    )


def create_logical_session(
    state_home: Path,
    *,
    project_root: Path,
    stack: str,
    controller: RouteBinding,
    agents: Mapping[str, RouteBinding],
    parent_id: str | None = None,
    leanctx_profile: str | None = None,
) -> LogicalSession:
    root = _session_root(state_home)
    staging = _staging_root(state_home)
    canonical_project = Path(project_root).resolve(strict=False)
    parent = None
    if parent_id is not None:
        parent = load_logical_session(state_home, parent_id)
        if parent.project_root != canonical_project:
            raise LogicalSessionError(
                "forked logical session must stay in its parent project"
            )
    try:
        selected_leanctx_profile = validate_leanctx_profile(
            leanctx_profile
            if leanctx_profile is not None
            else (
                parent.leanctx_profile
                if parent is not None
                else DEFAULT_LEANCTX_PROFILE
            )
        )
    except ValueError as failure:
        raise LogicalSessionError("logical session LeanCTX profile is invalid") from failure
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )
    for _ in range(32):
        identifier = f"oc-s-{secrets.token_hex(8)}"
        session = _parse_session(
            {
                "schemaVersion": 2,
                "id": identifier,
                "claudeSessionId": str(uuid.uuid4()),
                "parentId": parent_id,
                "projectRoot": str(canonical_project),
                "stack": stack,
                "controller": _binding_json(controller),
                "agents": {
                    role: _binding_json(agents[role]) for role in agents
                },
                "leanctxProfile": selected_leanctx_profile,
                "createdAt": now,
            }
        )
        directory = staging / identifier
        published = root / identifier
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            continue
        except OSError as failure:
            raise LogicalSessionError(
                "logical session could not be allocated"
            ) from failure
        descriptor = -1
        try:
            payload = (
                json.dumps(
                    _session_json(session),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            if len(payload) > MAX_BINDING_BYTES:
                raise LogicalSessionError("logical session binding is too large")
            descriptor = os.open(
                directory / "binding.json",
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            directory_fd = os.open(
                directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            root_fd = os.open(
                staging, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            os.rename(directory, published)
            root_fd = os.open(
                root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            return load_logical_session(state_home, identifier)
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                (directory / "binding.json").unlink()
            except FileNotFoundError:
                pass
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            try:
                (published / "binding.json").unlink()
            except FileNotFoundError:
                pass
            try:
                published.rmdir()
            except FileNotFoundError:
                pass
            raise
    raise LogicalSessionError("could not allocate a logical session ID")


def load_logical_session(state_home: Path, identifier: str) -> LogicalSession:
    if not isinstance(identifier, str) or not _SESSION_ID.fullmatch(identifier):
        raise LogicalSessionError("logical session selector is invalid")
    root = _session_root(state_home)
    directory = _require_private_directory(
        root / identifier, "logical session"
    )
    if directory.parent != root:
        raise LogicalSessionError("logical session escaped its state directory")
    session = _decode_binding(_read_binding(directory))
    if session.id != identifier:
        raise LogicalSessionError("logical session ID does not match its path")
    return session


def resolve_logical_session(state_home: Path, selector: str) -> LogicalSession:
    if isinstance(selector, str) and _SESSION_ID.fullmatch(selector):
        return load_logical_session(state_home, selector)
    try:
        parsed_uuid = uuid.UUID(selector)
    except (AttributeError, TypeError, ValueError) as failure:
        raise LogicalSessionError("session selector is invalid") from failure
    if parsed_uuid.version != 4 or str(parsed_uuid) != selector:
        raise LogicalSessionError("session selector is invalid")
    matches = tuple(
        session
        for session in list_logical_sessions(state_home)
        if session.claude_session_id == selector
    )
    if not matches:
        raise LogicalSessionError("session selector was not found")
    if len(matches) != 1:
        raise LogicalSessionError("session selector is ambiguous")
    return matches[0]


def list_logical_sessions(state_home: Path) -> tuple[LogicalSession, ...]:
    root = _session_root(state_home)
    try:
        entries = tuple(os.scandir(root))
    except OSError as failure:
        raise LogicalSessionError("logical sessions could not be listed") from failure
    names = []
    for entry in entries:
        if not _SESSION_ID.fullmatch(entry.name) or not entry.is_dir(
            follow_symlinks=False
        ):
            raise LogicalSessionError(
                "logical session directory contains an unexpected entry"
            )
        names.append(entry.name)
    names.sort()
    sessions = tuple(load_logical_session(state_home, name) for name in names)
    return tuple(sorted(sessions, key=lambda item: (item.created_at, item.id)))


def _active_logical_session_ids(state_home: Path) -> frozenset[str]:
    state = _require_private_directory(Path(state_home), "Orichum state directory")
    leases = state / "claudex-port-leases"
    try:
        os.lstat(leases)
    except FileNotFoundError:
        return frozenset()
    except OSError as failure:
        raise LogicalSessionError(
            "Claudex proxy lease directory is unavailable"
        ) from failure
    leases = _require_private_directory(
        leases, "Claudex proxy lease directory"
    )
    try:
        entries = tuple(os.scandir(leases))
    except OSError as failure:
        raise LogicalSessionError(
            "Claudex proxy leases could not be listed"
        ) from failure
    active: set[str] = set()
    for entry in entries:
        if (
            not re.fullmatch(r"[0-9]{1,5}\.json", entry.name)
            or not entry.is_file(follow_symlinks=False)
        ):
            raise LogicalSessionError(
                "Claudex proxy lease directory contains an unexpected entry"
            )
        path = leases / entry.name
        try:
            observed = os.lstat(path)
        except OSError as failure:
            raise LogicalSessionError(
                "Claudex proxy lease is unavailable"
            ) from failure
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
            or observed.st_size > 4096
        ):
            raise LogicalSessionError("Claudex proxy lease is unsafe")
        try:
            content = path.read_bytes()
        except OSError as failure:
            raise LogicalSessionError(
                "Claudex proxy lease is unavailable"
            ) from failure
        if len(content) != observed.st_size:
            raise LogicalSessionError("Claudex proxy lease changed while reading")
        try:
            document = json.loads(content)
        except (UnicodeError, json.JSONDecodeError) as failure:
            raise LogicalSessionError("Claudex proxy lease is invalid") from failure
        if (
            not isinstance(document, dict)
            or set(document) != {"pid", "runId", "sessionId"}
            or type(document["pid"]) is not int
            or document["pid"] < 1
            or not isinstance(document["runId"], str)
            or not _RUN_ID.fullmatch(document["runId"])
            or not isinstance(document["sessionId"], str)
            or not _SESSION_ID.fullmatch(document["sessionId"])
        ):
            raise LogicalSessionError("Claudex proxy lease is invalid")
        try:
            os.kill(document["pid"], 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        except OSError as failure:
            raise LogicalSessionError(
                "Claudex proxy lease owner could not be checked"
            ) from failure
        active.add(document["sessionId"])
    return frozenset(active)


def _delete_logical_session(
    state_home: Path, session: LogicalSession
) -> None:
    root = _session_root(state_home)
    directory = _require_private_directory(
        root / session.id, "logical session"
    )
    try:
        entries = tuple(os.scandir(directory))
    except OSError as failure:
        raise LogicalSessionError(
            "logical session could not be inspected"
        ) from failure
    if (
        len(entries) != 1
        or entries[0].name != "binding.json"
        or not entries[0].is_file(follow_symlinks=False)
    ):
        raise LogicalSessionError("logical session contains an unexpected entry")
    staging = _staging_root(state_home)
    staged = staging / f"delete-{session.id}"
    try:
        os.rename(directory, staged)
        (staged / "binding.json").unlink()
        staged.rmdir()
    except OSError as failure:
        if staged.is_dir() and not directory.exists():
            try:
                os.rename(staged, directory)
            except OSError:
                pass
        raise LogicalSessionError(
            f"logical session {session.id} could not be removed"
        ) from failure


def remove_logical_session(
    state_home: Path,
    selector: str,
    *,
    apply: bool,
) -> LogicalSessionCleanup:
    """Preview or remove one inactive leaf logical session."""
    sessions = list_logical_sessions(state_home)
    target = resolve_logical_session(state_home, selector)
    if target.id in _active_logical_session_ids(state_home):
        raise LogicalSessionError("active logical session cannot be removed")
    if any(session.parent_id == target.id for session in sessions):
        raise LogicalSessionError(
            "logical session has a child session; remove its children first"
        )
    if apply:
        _delete_logical_session(state_home, target)
        return LogicalSessionCleanup(target.id, "removed")
    return LogicalSessionCleanup(target.id, "eligible")


def clear_logical_sessions(
    state_home: Path,
    *,
    apply: bool,
) -> tuple[LogicalSessionCleanup, ...]:
    """Preview or remove all inactive logical sessions."""
    sessions = list_logical_sessions(state_home)
    by_id = {session.id: session for session in sessions}
    active = set(_active_logical_session_ids(state_home)) & set(by_id)
    retained = set(active)
    pending = list(active)
    while pending:
        parent_id = by_id[pending.pop()].parent_id
        if parent_id is not None and parent_id in by_id and parent_id not in retained:
            retained.add(parent_id)
            pending.append(parent_id)
    removable = [session for session in sessions if session.id not in retained]
    if apply:
        depth: dict[str, int] = {}

        def session_depth(session: LogicalSession) -> int:
            if session.id in depth:
                return depth[session.id]
            seen = {session.id}
            parent_id = session.parent_id
            value = 0
            while parent_id is not None and parent_id in by_id:
                if parent_id in seen:
                    raise LogicalSessionError("logical session ancestry is cyclic")
                seen.add(parent_id)
                value += 1
                parent_id = by_id[parent_id].parent_id
            depth[session.id] = value
            return value

        for session in sorted(removable, key=session_depth, reverse=True):
            _delete_logical_session(state_home, session)
    return tuple(
        LogicalSessionCleanup(
            session.id,
            (
                "active-preserved"
                if session.id in active
                else "parent-preserved"
                if session.id in retained
                else "removed"
                if apply
                else "eligible"
            ),
        )
        for session in sessions
    )


def _port_is_live(port: int) -> bool:
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=0.2)
    except OSError:
        return False
    connection.close()
    return True


def _run_port(run_dir: Path) -> int | None:
    path = run_dir / "claudex-proxy-port"
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as failure:
        raise LogicalSessionError("physical session port is unavailable") from failure
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISREG(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o600
        or observed.st_size > 16
    ):
        raise LogicalSessionError("physical session port file is unsafe")
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as failure:
        raise LogicalSessionError("physical session port is unavailable") from failure
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise LogicalSessionError("physical session port is invalid")
    return int(value)


def cleanup_physical_runs(
    state_home: Path,
    *,
    older_than_days: int,
    apply: bool,
    now: float | None = None,
) -> tuple[PhysicalRunCleanup, ...]:
    """Preview or remove inactive physical runs without touching logical sessions."""
    if type(older_than_days) is not int or older_than_days < 1:
        raise LogicalSessionError("older-than days must be a positive integer")
    state = _require_private_directory(Path(state_home), "Orichum state directory")
    sessions = state / "sessions"
    try:
        os.lstat(sessions)
    except FileNotFoundError:
        return ()
    except OSError as failure:
        raise LogicalSessionError(
            "physical session directory is unavailable"
        ) from failure
    sessions = _require_private_directory(
        sessions, "physical session directory"
    )
    cutoff = (
        datetime.now(tz=timezone.utc).timestamp() if now is None else float(now)
    ) - older_than_days * 86_400
    selected: list[PhysicalRunCleanup] = []
    try:
        entries = tuple(os.scandir(sessions))
    except OSError as failure:
        raise LogicalSessionError(
            "physical sessions could not be listed"
        ) from failure
    for entry in sorted(entries, key=lambda item: item.name):
        if not _RUN_ID.fullmatch(entry.name) or not entry.is_dir(
            follow_symlinks=False
        ):
            raise LogicalSessionError(
                "physical session directory contains an unexpected entry"
            )
        run_dir = _require_private_directory(
            sessions / entry.name, "physical session"
        )
        manifest = run_dir / ".complete"
        try:
            observed = os.lstat(manifest)
        except FileNotFoundError:
            continue
        except OSError as failure:
            raise LogicalSessionError(
                "physical session manifest is unavailable"
            ) from failure
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_uid != os.getuid()
            or stat.S_IMODE(observed.st_mode) != 0o600
        ):
            raise LogicalSessionError("physical session manifest is unsafe")
        if observed.st_mtime > cutoff:
            continue
        port = _run_port(run_dir)
        if port is not None and _port_is_live(port):
            continue
        status = "eligible"
        if apply:
            try:
                shutil.rmtree(run_dir)
            except OSError as failure:
                raise LogicalSessionError(
                    f"physical session {entry.name} could not be removed"
                ) from failure
            status = "removed"
        selected.append(PhysicalRunCleanup(entry.name, status))
    return tuple(selected)
