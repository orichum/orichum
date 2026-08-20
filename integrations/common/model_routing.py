#!/usr/bin/env python3
"""Validate and resolve portable provider-agnostic model stacks."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
from typing import Mapping, Optional, Sequence


ROLES: tuple[str, ...] = (
    "repository-explorer",
    "repository-verifier",
    "correctness-critic",
    "architecture-advisor",
    "implementation-worker",
    "planning-advisor",
)
LEGACY_ROLES: tuple[str, ...] = ROLES[:-1]
_STACK_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,254}$")
_AGENT_FILES = {role: f"agents/{role}.md" for role in ROLES}
_AGENT_CONTEXT_TOOLS = (
    "mcp__leanctx__ctx_read",
    "mcp__leanctx__ctx_search",
    "mcp__leanctx__ctx_tree",
    "mcp__leanctx__ctx_expand",
    "mcp__leanctx__ctx_graph",
    "mcp__leanctx__ctx_impact",
    "mcp__leanctx__ctx_callgraph",
)
_IMPLEMENTATION_TOOLS = (
    *_AGENT_CONTEXT_TOOLS,
    "mcp__leanctx__ctx_patch",
    "mcp__leanctx__ctx_shell",
    "Edit",
    "Write",
    "Bash",
)


class RoutingError(RuntimeError):
    pass


class ModelAvailabilityError(RoutingError):
    pass


@dataclass(frozen=True)
class EffectiveStack:
    stack_name: str
    controller: str
    candidates: Mapping[str, tuple[str, ...]]
    agents: Mapping[str, str]
    legacy_agents: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "stack": self.stack_name,
            "controller": self.controller,
            "configuredCandidates": {
                role: list(self.candidates[role]) for role in ROLES
            },
            "agents": {role: self.agents[role] for role in ROLES},
        }


def _exact_object(value: object, keys: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise RoutingError(f"{label} must contain exactly {sorted(keys)}")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RoutingError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def validate_stack_name(value: object, label: str = "stack name") -> str:
    if not isinstance(value, str) or not _STACK_PATTERN.fullmatch(value):
        raise RoutingError(f"{label} is invalid")
    return value


def validate_model_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _MODEL_PATTERN.fullmatch(value):
        raise RoutingError(f"{label} has an unsafe model ID")
    return value


def validate_routing_document(raw: object) -> dict[str, object]:
    """Validate and normalize one already-parsed model-routing document."""
    document = _exact_object(
        raw, {"schemaVersion", "defaultStack", "stacks"}, "routing"
    )
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise RoutingError("schemaVersion must be exactly 1")
    default = validate_stack_name(document["defaultStack"], "defaultStack")
    stacks = document["stacks"]
    if not isinstance(stacks, dict) or not stacks:
        raise RoutingError("stacks must be a non-empty object")
    normalized: dict[str, object] = {}
    for raw_name, raw_stack in stacks.items():
        name = validate_stack_name(raw_name)
        stack = _exact_object(
            raw_stack, {"controller", "agents"}, f"stack {name}"
        )
        controller = validate_model_id(
            stack["controller"], f"stack {name} controller"
        )
        agents = stack["agents"]
        legacy_agents = isinstance(agents, dict) and set(agents) == set(LEGACY_ROLES)
        if not isinstance(agents, dict) or (
            set(agents) != set(LEGACY_ROLES)
            and set(agents) != set(ROLES)
        ):
            raise RoutingError(
                f"stack {name} agents must contain exactly {sorted(set(ROLES))}"
            )
        if legacy_agents:
            agents = {
                **agents,
                "planning-advisor": agents["architecture-advisor"],
            }
        normalized_agents = {}
        for role in ROLES:
            values = agents[role]
            if not isinstance(values, list) or not values:
                raise RoutingError(
                    f"stack {name} role {role} needs candidates"
                )
            candidates = tuple(
                validate_model_id(value, f"stack {name} role {role}")
                for value in values
            )
            if len(candidates) != len(set(candidates)):
                raise RoutingError(
                    f"stack {name} role {role} has duplicate candidates"
                )
            normalized_agents[role] = candidates
        normalized[name] = {
            "controller": controller,
            "agents": normalized_agents,
        }
    if default not in normalized:
        raise RoutingError("defaultStack does not name an existing stack")
    return {
        "schemaVersion": 1,
        "defaultStack": default,
        "stacks": normalized,
    }


def load_routing(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RoutingError("model routing could not be parsed") from error
    return validate_routing_document(raw)


def load_routing_view(path: Path) -> object:
    """Load the routing portion of either routing.json or model-stacks.json."""
    try:
        raw = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RoutingError("model routing could not be parsed") from error
    if isinstance(raw, dict) and set(raw) == {
        "schemaVersion",
        "defaultStack",
        "models",
        "stacks",
    }:
        if raw["schemaVersion"] == 1 and raw["models"] == {}:
            return validate_routing_document(
                {
                    "schemaVersion": raw["schemaVersion"],
                    "defaultStack": raw["defaultStack"],
                    "stacks": raw["stacks"],
                }
            )
        from .stack_definition import normalize_model_stacks

        return normalize_model_stacks(raw)
    return validate_routing_document(raw)


def load_catalog(path: Path) -> tuple[str, ...]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RoutingError("model catalogue could not be parsed") from error
    if (
        not isinstance(raw, dict)
        or raw.get("object") != "list"
        or not isinstance(raw.get("data"), list)
    ):
        raise RoutingError("model catalogue has an invalid shape")
    result = []
    for item in raw["data"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise RoutingError("model catalogue contains an invalid entry")
        result.append(validate_model_id(item["id"], "catalogue"))
    return tuple(dict.fromkeys(result))


def resolve_effective(
    routing: object,
    catalogue: Sequence[str],
    requested_stack: Optional[str] = None,
) -> EffectiveStack:
    from .stack_definition import NormalizedStacks

    available = set(catalogue)
    if isinstance(routing, NormalizedStacks):
        name = requested_stack or routing.default_stack
        if name not in routing.stacks:
            raise RoutingError(f"model stack {name!r} is missing")
        stack = routing.stacks[name]
        controller_models = tuple(
            candidate.model for candidate in stack.controller
        )
        controller = next(
            (model for model in controller_models if model in available),
            None,
        )
        if controller is None:
            raise ModelAvailabilityError(
                f"stack {name} controller has no available candidate: "
                + ", ".join(controller_models)
            )
        candidates = {
            role: tuple(
                candidate.model for candidate in stack.agents[role]
            )
            for role in ROLES
        }
        selected = {}
        for role in ROLES:
            selected_model = next(
                (
                    model
                    for model in candidates[role]
                    if model in available
                ),
                None,
            )
            if selected_model is None:
                raise ModelAvailabilityError(
                    f"stack {name} role {role} has no available candidate: "
                    + ", ".join(candidates[role])
                )
            selected[role] = selected_model
        return EffectiveStack(name, controller, candidates, selected)
    if not isinstance(routing, Mapping):
        raise RoutingError("model routing is invalid")
    name = requested_stack or str(routing["defaultStack"])
    stacks = routing["stacks"]
    if not isinstance(stacks, Mapping) or name not in stacks:
        raise RoutingError(f"model stack {name!r} is missing")
    stack = stacks[name]
    if not isinstance(stack, Mapping):
        raise RoutingError(f"model stack {name!r} is invalid")
    controller = str(stack["controller"])
    if controller not in available:
        raise ModelAvailabilityError(
            f"stack {name} controller {controller} is unavailable"
        )
    candidates = stack["agents"]
    if not isinstance(candidates, Mapping):
        raise RoutingError(f"model stack {name!r} is invalid")
    selected = {}
    for role in ROLES:
        role_candidates = tuple(candidates[role])
        selected_model = next(
            (model for model in role_candidates if model in available), None
        )
        if selected_model is None:
            raise ModelAvailabilityError(
                f"stack {name} role {role} has no available candidate: "
                + ", ".join(role_candidates)
            )
        selected[role] = selected_model
    return EffectiveStack(name, controller, candidates, selected)


def _render_stack_table(
    effective: EffectiveStack,
    catalogue: Sequence[str],
    scope: str,
) -> str:
    available = set(catalogue)
    rows = [
        (
            effective.stack_name,
            scope,
            "controller",
            f"{effective.controller} [available]",
            effective.controller,
            "ready",
        )
    ]
    for role in ROLES:
        candidates = " -> ".join(
            f"{model} [{'available' if model in available else 'unavailable'}]"
            for model in effective.candidates[role]
        )
        rows.append(
            (
                effective.stack_name,
                scope,
                role,
                candidates,
                effective.agents[role],
                "ready",
            )
        )
    headers = ("STACK", "SCOPE", "ROLE", "CANDIDATES", "SELECTED", "STATUS")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(values: Sequence[str]) -> str:
        return " | ".join(
            value.ljust(width) for value, width in zip(values, widths)
        ).rstrip()

    return "\n".join(
        (
            render(headers),
            "-+-".join("-" * width for width in widths),
            *(render(row) for row in rows),
        )
    ) + "\n"


def _selected_routes(effective: Optional[EffectiveStack]) -> dict[str, list[str]]:
    selected: dict[str, list[str]] = {}
    if effective is None:
        return selected
    selected.setdefault(effective.controller, []).append("controller")
    for role in ROLES:
        selected.setdefault(effective.agents[role], []).append(role)
    return selected


def _configured_models(
    routing: Mapping[str, object], requested_stack: Optional[str]
) -> set[str]:
    from .stack_definition import NormalizedStacks, StackDefinition

    name = requested_stack or str(routing["defaultStack"])
    if isinstance(routing, NormalizedStacks):
        stack = routing.stacks.get(name)
        if not isinstance(stack, StackDefinition):
            return set()
        return {
            candidate.model
            for candidates in (
                stack.controller,
                *stack.agents.values(),
            )
            for candidate in candidates
        }
    stacks = routing["stacks"]
    if not isinstance(stacks, Mapping) or name not in stacks:
        return set()
    stack = stacks[name]
    if not isinstance(stack, Mapping):
        return set()
    configured = {str(stack["controller"])}
    candidates = stack["agents"]
    if isinstance(candidates, Mapping):
        for role in ROLES:
            configured.update(str(model) for model in candidates[role])
    return configured


def _render_catalogue_table(
    routing: Mapping[str, object],
    catalogue: Sequence[str],
    requested_stack: Optional[str],
    effective: Optional[EffectiveStack],
) -> str:
    selected = _selected_routes(effective)
    configured = _configured_models(routing, requested_stack)
    rows = []
    for model in catalogue:
        if model in selected:
            route = "selected: " + ", ".join(selected[model])
        elif model in configured:
            route = "configured candidate"
        else:
            route = "unconfigured"
        rows.append((model, route))
    headers = ("MODEL", "ROUTING")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        for index in range(len(headers))
    ]

    def render(values: Sequence[str]) -> str:
        return " | ".join(
            value.ljust(width) for value, width in zip(values, widths)
        ).rstrip()

    return "\n".join(
        (
            render(headers),
            "-+-".join("-" * width for width in widths),
            *(render(row) for row in rows),
        )
    ) + "\n"


def _render_unresolved_stack(
    routing: Mapping[str, object], requested_stack: Optional[str], scope: str
) -> str:
    name = requested_stack or str(routing["defaultStack"])
    headers = ("STACK", "SCOPE", "STATUS")
    row = (name, scope, "unresolved")
    widths = [
        max(len(headers[index]), len(row[index]))
        for index in range(len(headers))
    ]

    def render(values: Sequence[str]) -> str:
        return " | ".join(
            value.ljust(width) for value, width in zip(values, widths)
        ).rstrip()

    return "\n".join(
        (
            render(headers),
            "-+-".join("-" * width for width in widths),
            render(row),
        )
    ) + "\n"


def _write_effective(path: Path, effective: EffectiveStack) -> None:
    payload = (
        json.dumps(
            effective.as_json(),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _write_private_file(Path(path), payload, 0o600)


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orichum models")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "validate"):
        subcommand = commands.add_parser(command)
        subcommand.add_argument("--routing-config", required=True, type=Path)
        subcommand.add_argument("--models-file", required=True, type=Path)
        subcommand.add_argument("stack", nargs="?")
        if command == "validate":
            subcommand.add_argument("--effective-output", type=Path)
    return parser


def main(arguments: Optional[list[str]] = None) -> int:
    parsed = _create_parser().parse_args(arguments)
    try:
        routing = load_routing_view(parsed.routing_config)
        catalogue = load_catalog(parsed.models_file)
        if parsed.command == "list":
            scope = "selected" if parsed.stack is not None else "global"
            try:
                effective = resolve_effective(
                    routing, catalogue, parsed.stack
                )
            except RoutingError as error:
                print(
                    _render_catalogue_table(
                        routing, catalogue, parsed.stack, None
                    ),
                    end="",
                )
                print(
                    _render_unresolved_stack(routing, parsed.stack, scope),
                    end="",
                )
                print(f"WARNING: {error}", file=sys.stderr)
                return 0
            print(
                _render_catalogue_table(
                    routing, catalogue, parsed.stack, effective
                ),
                end="",
            )
            print(_render_stack_table(effective, catalogue, scope), end="")
            return 0
        effective = resolve_effective(routing, catalogue, parsed.stack)
        if parsed.effective_output is not None:
            _write_effective(parsed.effective_output, effective)
    except ModelAvailabilityError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 42
    except (OSError, RoutingError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


def _rewrite_agent_model(text: str, model: str, role: str) -> str:
    lines = text.splitlines(keepends=True)
    indexes = [
        index for index, line in enumerate(lines) if line.startswith("model: ")
    ]
    if len(indexes) != 1 or not lines or lines[0].strip() != "---":
        raise RoutingError(f"agent {role} has invalid frontmatter")
    newline = "\n" if lines[indexes[0]].endswith("\n") else ""
    lines[indexes[0]] = f"model: {model}{newline}"
    rewritten = "".join(lines)
    validate_agent_contract(rewritten, role, model)
    return rewritten


def validate_agent_contract(text: str, role: str, model: str) -> None:
    """Require one deterministic LeanCTX surface for every runtime agent."""
    if role not in ROLES:
        raise RoutingError(f"agent role {role!r} is invalid")
    lines = text.splitlines()
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError as error:
        raise RoutingError(f"agent {role} has invalid frontmatter") from error
    frontmatter = lines[1:frontmatter_end]
    expected_tools = (
        _IMPLEMENTATION_TOOLS
        if role == "implementation-worker"
        else _AGENT_CONTEXT_TOOLS
    )
    expected = {
        "name": f"name: {role}",
        "model": f"model: {model}",
        "mcpServers": "mcpServers: [leanctx]",
        "tools": "tools: " + ", ".join(expected_tools),
    }
    sensitive: dict[str, list[str]] = {name: [] for name in expected}
    for line in frontmatter:
        match = re.match(r"^\s*(name|model|mcpServers|tools)\s*:", line)
        if match is not None:
            sensitive[match.group(1)].append(line)
    if (
        not lines
        or lines[0] != "---"
        or any(sensitive[name] != [value] for name, value in expected.items())
    ):
        raise RoutingError(
            f"agent {role} does not match the LeanCTX tool contract"
        )


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_source_state(
    first: os.stat_result, second: os.stat_result
) -> bool:
    return (
        _same_object(first, second)
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_uid == second.st_uid
        and first.st_gid == second.st_gid
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _require_source_owner(observed: os.stat_result) -> None:
    if observed.st_uid != os.getuid():
        raise RoutingError("plugin source entry has an unexpected owner")


def _canonical_directory(path: Path, mode: Optional[int] = None) -> Path:
    path = path if path.is_absolute() else Path.cwd() / path
    try:
        observed = os.lstat(path)
        canonical = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RoutingError("plugin directory is unavailable") from error
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise RoutingError("plugin directory must be a real directory")
    if observed.st_uid != os.getuid():
        raise RoutingError("plugin directory has an unexpected owner")
    if mode is not None and stat.S_IMODE(observed.st_mode) != mode:
        raise RoutingError("plugin directory has unsafe permissions")
    if canonical != path:
        raise RoutingError("plugin directory is not canonical")
    final = os.lstat(path)
    if not _same_object(observed, final):
        raise RoutingError("plugin directory changed during validation")
    return canonical


def _read_regular_file(
    directory_fd: int,
    name: str,
    path: Path,
    observed: os.stat_result,
) -> bytes:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise RoutingError("no-follow plugin access is unavailable")
    descriptor: Optional[int] = None
    try:
        _require_source_owner(observed)
        descriptor = os.open(
            name, os.O_RDONLY | no_follow, dir_fd=directory_fd
        )
        descriptor_before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(descriptor_before.st_mode)
            or not _same_source_state(observed, descriptor_before)
        ):
            raise RoutingError("plugin source file changed before reading")
        _require_source_owner(descriptor_before)
        blocks = []
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            blocks.append(block)
        descriptor_after = os.fstat(descriptor)
        path_after = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        _require_source_owner(descriptor_after)
        _require_source_owner(path_after)
        if (
            not _same_source_state(descriptor_before, descriptor_after)
            or not _same_source_state(descriptor_after, path_after)
            or not _same_source_state(path_after, os.lstat(path))
        ):
            raise RoutingError("plugin source file changed during reading")
        return b"".join(blocks)
    except OSError as error:
        raise RoutingError("plugin source file could not be read safely") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_private_file(path: Path, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise RoutingError("no-follow plugin access is unavailable")
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(path, flags | no_follow, mode)
        os.fchmod(descriptor, mode)
        written = 0
        while written < len(data):
            written += os.write(descriptor, data[written:])
        os.fsync(descriptor)
    except OSError as error:
        raise RoutingError("runtime plugin file could not be created") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def materialize_runtime_plugin(
    source: Path, destination: Path, effective: EffectiveStack
) -> Path:
    """Copy one source plugin into a private session and bind its agent models."""
    source = _canonical_directory(Path(source))
    destination = (
        Path(destination)
        if Path(destination).is_absolute()
        else Path.cwd() / destination
    )
    parent = _canonical_directory(destination.parent, 0o700)
    if (
        destination.parent != parent
        or destination.name in {"", ".", ".."}
        or destination != parent / destination.name
    ):
        raise RoutingError("runtime plugin must be a direct session child")
    try:
        os.lstat(destination)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise RoutingError("runtime plugin destination is unavailable") from error
    else:
        raise RoutingError("runtime plugin destination already exists")

    rewritten: set[str] = set()

    def copy_directory(
        source_dir: Path,
        destination_dir: Path,
        expected: Optional[os.stat_result] = None,
    ) -> None:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        if not no_follow:
            raise RoutingError("no-follow plugin access is unavailable")
        source_path_before = os.lstat(source_dir)
        _require_source_owner(source_path_before)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        source_fd: Optional[int] = None
        try:
            source_fd = os.open(
                source_dir, directory_flags | no_follow
            )
            source_before = os.fstat(source_fd)
            _require_source_owner(source_before)
            if (
                stat.S_ISLNK(source_path_before.st_mode)
                or not stat.S_ISDIR(source_before.st_mode)
                or not _same_source_state(source_path_before, source_before)
                or (
                    expected is not None
                    and not _same_source_state(expected, source_before)
                )
            ):
                raise RoutingError(
                    "plugin source directory changed before reading"
                )
            with os.scandir(source_fd) as iterator:
                entries = list(iterator)
            for entry in entries:
                source_path = source_dir / entry.name
                destination_path = destination_dir / entry.name
                try:
                    observed = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise RoutingError(
                        "plugin source entry is unavailable"
                    ) from error
                _require_source_owner(observed)
                if stat.S_ISLNK(observed.st_mode):
                    raise RoutingError("plugin source contains a symlink")
                if stat.S_ISDIR(observed.st_mode):
                    try:
                        os.mkdir(destination_path, 0o700)
                        os.chmod(destination_path, 0o700)
                    except OSError as error:
                        raise RoutingError(
                            "runtime plugin directory could not be created"
                        ) from error
                    copy_directory(
                        source_path, destination_path, observed
                    )
                    continue
                if not stat.S_ISREG(observed.st_mode):
                    raise RoutingError(
                        "plugin source contains a special file"
                    )
                data = _read_regular_file(
                    source_fd, entry.name, source_path, observed
                )
                relative = source_path.relative_to(source).as_posix()
                for role, agent_file in _AGENT_FILES.items():
                    if relative == agent_file:
                        try:
                            text = data.decode("utf-8")
                        except UnicodeDecodeError as error:
                            raise RoutingError(
                                f"agent {role} has invalid frontmatter"
                            ) from error
                        data = _rewrite_agent_model(
                            text, effective.agents[role], role
                        ).encode("utf-8")
                        rewritten.add(role)
                        break
                mode = (
                    0o700
                    if stat.S_IMODE(observed.st_mode) & 0o111
                    else 0o600
                )
                _write_private_file(destination_path, data, mode)
            source_after = os.fstat(source_fd)
            source_path_after = os.lstat(source_dir)
            _require_source_owner(source_after)
            _require_source_owner(source_path_after)
            if (
                not _same_source_state(source_before, source_after)
                or not _same_source_state(
                    source_after, source_path_after
                )
            ):
                raise RoutingError(
                    "plugin source changed during materialization"
                )
        except OSError as error:
            raise RoutingError("plugin source could not be enumerated") from error
        finally:
            if source_fd is not None:
                os.close(source_fd)

    created = False
    try:
        os.mkdir(destination, 0o700)
        created = True
        os.chmod(destination, 0o700)
        copy_directory(source, destination)
        if rewritten != set(ROLES):
            raise RoutingError("plugin source is missing a required agent")
    except RoutingError:
        if created:
            shutil.rmtree(destination)
        raise
    except OSError as error:
        if created:
            shutil.rmtree(destination)
        raise RoutingError("runtime plugin could not be created") from error
    return _canonical_directory(destination, 0o700)


if __name__ == "__main__":
    raise SystemExit(main())
