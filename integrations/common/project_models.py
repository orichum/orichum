#!/usr/bin/env python3
"""Resolve one repository-local Orichum configuration file."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .github_identity import GithubIdentityError, validate_github_account
from .jira_profiles import (
    AtlassianConfig,
    AtlassianError,
    load_jira_profiles,
    validate_jira_profile,
)
from .model_routing import LEGACY_ROLES, ROLES, RoutingError, validate_model_id
from .project_context import resolve_control_plane_context
from .stack_definition import (
    NormalizedStacks,
    StackCandidate,
    StackDefinition,
    candidate_id,
)

_MAX_PROJECT_CONFIG_BYTES = 16 * 1024
_PROJECT_DIRECTORY = ".orichum"
_PROJECT_FILE = "config.json"
_LEGACY_PROJECT_FILE = "models.json"


class ProjectModelsError(RoutingError):
    """A repository-local Orichum configuration is unsafe or invalid."""


@dataclass(frozen=True)
class ProjectModels:
    """Validated repository configuration and its ephemeral model stack."""

    path: Path
    digest: str
    stack_name: str
    assignments: Mapping[str, str]
    stacks: NormalizedStacks
    jira_profile: str | None
    github_account: str | None
    manages_services: bool


def _error(path: Path, message: str) -> ProjectModelsError:
    return ProjectModelsError(f"project configuration {path}: {message}")


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_state(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        _same_object(first, second)
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_uid == second.st_uid
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _read_candidate(directory: Path, filename: str) -> tuple[Path, bytes] | None:
    source = directory / filename
    try:
        observed_directory = os.lstat(directory)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _error(source, "configuration directory is unavailable") from error
    if stat.S_ISLNK(observed_directory.st_mode) or not stat.S_ISDIR(
        observed_directory.st_mode
    ):
        raise _error(source, "configuration directory must be a real directory")

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(directory, directory_flags)
    except OSError as error:
        raise _error(source, "configuration directory is unsafe") from error
    try:
        opened_directory = os.fstat(directory_fd)
        if not _same_state(observed_directory, opened_directory):
            raise _error(source, "configuration directory changed while opening")
        try:
            observed_file = os.stat(
                filename,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise _error(source, "file is unavailable") from error
        if (
            stat.S_ISLNK(observed_file.st_mode)
            or not stat.S_ISREG(observed_file.st_mode)
            or observed_file.st_size < 1
            or observed_file.st_size > _MAX_PROJECT_CONFIG_BYTES
        ):
            raise _error(source, "file must be a regular file no larger than 16 KiB")

        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(filename, file_flags, dir_fd=directory_fd)
        except OSError as error:
            raise _error(source, "file is unsafe") from error
        try:
            opened_file = os.fstat(file_fd)
            if not _same_state(observed_file, opened_file):
                raise _error(source, "file changed while opening")
            chunks = []
            remaining = _MAX_PROJECT_CONFIG_BYTES + 1
            while remaining:
                chunk = os.read(file_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            try:
                after_file = os.fstat(file_fd)
                current_file = os.stat(
                    filename,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise _error(
                    source,
                    "file changed or became unavailable while reading",
                ) from error
            if (
                len(content) > _MAX_PROJECT_CONFIG_BYTES
                or not _same_state(opened_file, after_file)
                or not _same_state(after_file, current_file)
            ):
                raise _error(source, "file changed while reading or exceeds 16 KiB")
        finally:
            os.close(file_fd)
        try:
            current_directory = os.lstat(directory)
        except OSError as error:
            raise _error(
                source,
                "configuration directory changed or became unavailable while reading",
            ) from error
        if not _same_state(opened_directory, current_directory):
            raise _error(source, "configuration directory changed while reading")
        return source, content
    finally:
        os.close(directory_fd)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite value {value}")


def _parse_configuration(
    path: Path,
    content: bytes,
    models: Mapping[str, object],
    *,
    manages_services: bool,
) -> tuple[Mapping[str, str], str | None, str | None]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as error:
        raise _error(path, "file must be UTF-8 JSON") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, json.JSONDecodeError) as error:
        raise _error(path, f"invalid JSON ({error})") from error
    expected = {"schemaVersion", "controller", "agents"}
    if manages_services:
        expected.update(("jiraProfile", "githubAccount"))
    if not isinstance(document, dict) or set(document) != expected:
        required = ", ".join(sorted(expected))
        raise _error(path, f"top level must contain exactly {required}")
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise _error(path, "schemaVersion must be exactly 1")
    agents = document["agents"]
    legacy_agents = isinstance(agents, dict) and set(agents) == set(LEGACY_ROLES)
    if not isinstance(agents, dict) or (
        set(agents) != set(LEGACY_ROLES) and set(agents) != set(ROLES)
    ):
        raise _error(path, f"agents must contain exactly {', '.join(ROLES)}")
    if legacy_agents:
        agents = {**agents, "planning-advisor": agents["architecture-advisor"]}
    raw_assignments = {
        "controller": document["controller"],
        **{role: agents[role] for role in ROLES},
    }
    assignments: dict[str, str] = {}
    for role, raw_model in raw_assignments.items():
        try:
            model = validate_model_id(raw_model, f"{role} model")
        except RoutingError as error:
            raise _error(path, str(error)) from error
        if model not in models:
            raise _error(path, f"{role} references unknown logical model {model}")
        assignments[role] = model

    jira_profile = None
    github_account = None
    if manages_services:
        jira_profile = document["jiraProfile"]
        if jira_profile is not None:
            try:
                jira_profile = validate_jira_profile(jira_profile)
            except AtlassianError as error:
                raise _error(path, str(error)) from error
        try:
            github_account = validate_github_account(document["githubAccount"])
        except GithubIdentityError as error:
            raise _error(path, str(error)) from error
    return MappingProxyType(assignments), jira_profile, github_account


def _ephemeral_stacks(
    content: bytes,
    assignments: Mapping[str, str],
    base: NormalizedStacks,
) -> tuple[str, NormalizedStacks]:
    digest = hashlib.sha256(content).hexdigest()
    stack_name = f"repository-local-{digest[:12]}"

    def candidate(role: str) -> StackCandidate:
        model = assignments[role]
        return StackCandidate(
            id=candidate_id(stack_name, role, 0, model),
            model=model,
            providers=tuple(base.models[model].routes),
        )

    stack = StackDefinition(
        name=stack_name,
        controller=(candidate("controller"),),
        agents=MappingProxyType({role: (candidate(role),) for role in ROLES}),
    )
    return stack_name, NormalizedStacks(
        default_stack=stack_name,
        models=base.models,
        stacks=MappingProxyType({stack_name: stack}),
    )


def update_project_jira(path: Path, jira_profile: str | None) -> None:
    path = Path(path)
    if jira_profile is not None:
        jira_profile = validate_jira_profile(jira_profile)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = -1
    file_fd = -1
    descriptor = -1
    lock_fd = -1
    temporary = f".{path.name}.{os.getpid()}.tmp"
    try:
        directory_fd = os.open(path.parent, directory_flags)
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(
            f".{path.name}.lock",
            lock_flags,
            0o600,
            dir_fd=directory_fd,
        )
        lock_details = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_details.st_mode)
            or lock_details.st_uid != os.getuid()
            or stat.S_IMODE(lock_details.st_mode) != 0o600
        ):
            raise _error(path, "configuration lock is unsafe")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        observed = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(observed.st_mode)
            or not stat.S_ISREG(observed.st_mode)
            or observed.st_size < 1
            or observed.st_size > _MAX_PROJECT_CONFIG_BYTES
        ):
            raise _error(path, "file must be a regular file no larger than 16 KiB")
        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path.name, file_flags, dir_fd=directory_fd)
        opened = os.fstat(file_fd)
        if not _same_state(observed, opened):
            raise _error(path, "file changed while opening")
        content = bytearray()
        while len(content) <= _MAX_PROJECT_CONFIG_BYTES:
            chunk = os.read(file_fd, _MAX_PROJECT_CONFIG_BYTES + 1 - len(content))
            if not chunk:
                break
            content.extend(chunk)
        after_read = os.fstat(file_fd)
        if len(content) > _MAX_PROJECT_CONFIG_BYTES or not _same_state(
            opened, after_read
        ):
            raise _error(path, "file changed while reading or exceeds 16 KiB")
        try:
            document = json.loads(
                bytes(content).decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeError, ValueError, json.JSONDecodeError) as error:
            raise _error(path, "invalid JSON") from error
        if not isinstance(document, dict) or set(document) != {
            "schemaVersion",
            "controller",
            "agents",
            "jiraProfile",
            "githubAccount",
        }:
            raise _error(path, "file does not manage project services")
        document["jiraProfile"] = jira_profile
        payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
        if len(payload) > _MAX_PROJECT_CONFIG_BYTES:
            raise _error(path, "file would exceed 16 KiB")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(
            temporary,
            flags,
            stat.S_IMODE(observed.st_mode),
            dir_fd=directory_fd,
        )
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("project configuration write made no progress")
            offset += written
        os.fsync(descriptor)
        current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not _same_state(after_read, current):
            raise _error(path, "file changed while updating")
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    except ProjectModelsError:
        raise
    except OSError as error:
        raise _error(path, "file could not be updated") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if file_fd >= 0:
            os.close(file_fd)
        if lock_fd >= 0:
            os.close(lock_fd)
        if directory_fd >= 0:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)


def ensure_project_config(
    project_root: Path,
    stack: StackDefinition,
    *,
    jira_profile: str | None = None,
    github_account: str | None = None,
) -> tuple[Path, bool]:
    """Create a repository-local configuration without replacing one."""
    try:
        root = Path(project_root).resolve(strict=True)
    except OSError as error:
        raise ProjectModelsError("project root is unavailable") from error
    if not root.is_dir():
        raise ProjectModelsError("project root must be a directory")

    directory = root / _PROJECT_DIRECTORY
    try:
        directory.mkdir(mode=0o755)
    except FileExistsError:
        pass
    except OSError as error:
        raise ProjectModelsError(
            "project configuration directory could not be created"
        ) from error
    try:
        observed_directory = os.lstat(directory)
    except OSError as error:
        raise ProjectModelsError(
            "project configuration directory is unavailable"
        ) from error
    if stat.S_ISLNK(observed_directory.st_mode) or not stat.S_ISDIR(
        observed_directory.st_mode
    ):
        raise ProjectModelsError(
            "project configuration directory must be a real directory"
        )

    configured = _read_candidate(directory, _PROJECT_FILE)
    if configured is not None:
        return configured[0], False
    legacy = _read_candidate(directory, _LEGACY_PROJECT_FILE)
    if legacy is not None:
        return legacy[0], False

    document = {
        "schemaVersion": 1,
        "controller": stack.controller[0].model,
        "agents": {role: stack.agents[role][0].model for role in ROLES},
        "jiraProfile": jira_profile,
        "githubAccount": github_account,
    }
    content = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        directory_fd = os.open(directory, directory_flags)
    except OSError as error:
        raise ProjectModelsError("project configuration directory is unsafe") from error
    path = directory / _PROJECT_FILE
    temporary_name = f".{_PROJECT_FILE}.{os.getpid()}.{secrets.token_hex(8)}"
    temporary_fd: int | None = None
    linked = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        temporary_fd = os.open(temporary_name, flags, 0o644, dir_fd=directory_fd)
        written = 0
        while written < len(content):
            written += os.write(temporary_fd, content[written:])
        os.fsync(temporary_fd)
        staged = os.fstat(temporary_fd)
        try:
            os.link(
                temporary_name,
                _PROJECT_FILE,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            return path, False
        linked = True
        try:
            os.stat(
                _LEGACY_PROJECT_FILE,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            published = os.stat(
                _PROJECT_FILE,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
            if _same_object(staged, published):
                os.unlink(_PROJECT_FILE, dir_fd=directory_fd)
            linked = False
            raise ProjectModelsError(
                f"project configuration {path}: legacy models.json appeared "
                "while the file was being created"
            )
        os.fsync(directory_fd)
    except OSError as error:
        if temporary_fd is not None:
            os.close(temporary_fd)
            temporary_fd = None
        if linked:
            try:
                published = os.stat(
                    _PROJECT_FILE,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                if _same_object(staged, published):
                    os.unlink(_PROJECT_FILE, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        raise ProjectModelsError(
            f"project configuration {path}: file could not be written"
        ) from error
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    return path, True


def discover_project_models(
    launch_dir: Path,
    context_root: Path,
    base: NormalizedStacks,
) -> ProjectModels | None:
    """Load the nearest project configuration without crossing its context root."""
    try:
        launch = Path(launch_dir).resolve(strict=True)
        root = Path(context_root).resolve(strict=True)
        launch.relative_to(root)
    except (OSError, ValueError) as error:
        raise ProjectModelsError(
            "project configuration discovery must stay inside the configured context"
        ) from error
    if not launch.is_dir() or not root.is_dir():
        raise ProjectModelsError("project configuration discovery requires directories")

    current = launch
    while True:
        directory = current / _PROJECT_DIRECTORY
        configured = _read_candidate(directory, _PROJECT_FILE)
        legacy = _read_candidate(directory, _LEGACY_PROJECT_FILE)
        if configured is not None and legacy is not None:
            raise _error(
                configured[0],
                "config.json and legacy models.json cannot both be present",
            )
        loaded = configured or legacy
        if loaded is not None:
            path, content = loaded
            manages_services = configured is not None
            assignments, jira_profile, github_account = _parse_configuration(
                path,
                content,
                base.models,
                manages_services=manages_services,
            )
            stack_name, stacks = _ephemeral_stacks(content, assignments, base)
            return ProjectModels(
                path=path,
                digest=hashlib.sha256(content).hexdigest(),
                stack_name=stack_name,
                assignments=assignments,
                stacks=stacks,
                jira_profile=jira_profile,
                github_account=github_account,
                manages_services=manages_services,
            )
        if current == root:
            return None
        current = current.parent


def resolve_project_context(
    project_document: object,
    launch_dir: Path,
    jira_profiles_path: Path,
    base: NormalizedStacks,
) -> tuple[dict[str, object], ProjectModels | None]:
    resolved = resolve_control_plane_context(project_document, launch_dir)
    route = resolved.get("route")
    if not isinstance(route, Mapping) or route.get("scope") != "context":
        return resolved, None
    project_models = discover_project_models(
        Path(str(resolved["launchDirReal"])),
        Path(str(route["contextRootReal"])),
        base,
    )
    if project_models is None or not project_models.manages_services:
        return resolved, project_models
    profiles: Mapping[str, AtlassianConfig] = {}
    if project_models.jira_profile is not None:
        profiles = load_jira_profiles(jira_profiles_path)
        if project_models.jira_profile not in profiles:
            raise ProjectModelsError(
                f"project configuration {project_models.path}: Jira profile "
                f"{project_models.jira_profile} is not configured"
            )
    selected_route = dict(route)
    selected_route["atlassianConfigured"] = project_models.jira_profile is not None
    selected_route["jiraProfile"] = project_models.jira_profile
    selected_route["githubAccount"] = project_models.github_account
    selected_route["projectConfigSource"] = str(project_models.path)
    selected_route["projectConfigDigest"] = project_models.digest
    selected = dict(resolved)
    selected["route"] = selected_route
    return selected, project_models
