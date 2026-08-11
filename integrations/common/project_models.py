#!/usr/bin/env python3
"""Resolve repository-local model assignments within one project context."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .model_routing import ROLES, RoutingError, validate_model_id
from .stack_definition import (
    NormalizedStacks,
    StackCandidate,
    StackDefinition,
    candidate_id,
)

_MAX_PROJECT_MODELS_BYTES = 16 * 1024
_PROJECT_DIRECTORY = ".orichum"
_PROJECT_FILE = "models.json"


class ProjectModelsError(RoutingError):
    """A repository-local model mapping is unsafe or invalid."""


@dataclass(frozen=True)
class ProjectModels:
    """Validated repository assignments and their ephemeral model stack."""

    path: Path
    digest: str
    stack_name: str
    assignments: Mapping[str, str]
    stacks: NormalizedStacks


def _error(path: Path, message: str) -> ProjectModelsError:
    return ProjectModelsError(f"project model mapping {path}: {message}")


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_state(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        first.st_size,
        first.st_mtime_ns,
    ) == (
        second.st_dev,
        second.st_ino,
        second.st_size,
        second.st_mtime_ns,
    )


def _read_candidate(directory: Path) -> tuple[Path, bytes] | None:
    source = directory / _PROJECT_FILE
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
        if not _same_object(observed_directory, opened_directory):
            raise _error(source, "configuration directory changed while opening")
        try:
            observed_file = os.stat(
                _PROJECT_FILE,
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
            or observed_file.st_size > _MAX_PROJECT_MODELS_BYTES
        ):
            raise _error(source, "file must be a regular file no larger than 16 KiB")

        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            file_fd = os.open(_PROJECT_FILE, file_flags, dir_fd=directory_fd)
        except OSError as error:
            raise _error(source, "file is unsafe") from error
        try:
            opened_file = os.fstat(file_fd)
            if not _same_state(observed_file, opened_file):
                raise _error(source, "file changed while opening")
            chunks = []
            remaining = _MAX_PROJECT_MODELS_BYTES + 1
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
                    _PROJECT_FILE,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise _error(
                    source,
                    "file changed or became unavailable while reading",
                ) from error
            if (
                len(content) > _MAX_PROJECT_MODELS_BYTES
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
        if not _same_object(opened_directory, current_directory):
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


def _parse_assignments(
    path: Path,
    content: bytes,
    models: Mapping[str, object],
) -> Mapping[str, str]:
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
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "controller",
        "agents",
    }:
        raise _error(
            path,
            "top level must contain exactly schemaVersion, controller, and agents",
        )
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise _error(path, "schemaVersion must be exactly 1")
    agents = document["agents"]
    if not isinstance(agents, dict) or set(agents) != set(ROLES):
        raise _error(path, f"agents must contain exactly {', '.join(ROLES)}")
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
    return MappingProxyType(assignments)


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


def discover_project_models(
    launch_dir: Path,
    context_root: Path,
    base: NormalizedStacks,
) -> ProjectModels | None:
    """Load the nearest project mapping without crossing its context root."""
    try:
        launch = Path(launch_dir).resolve(strict=True)
        root = Path(context_root).resolve(strict=True)
        launch.relative_to(root)
    except (OSError, ValueError) as error:
        raise ProjectModelsError(
            "project model discovery must stay inside the configured context"
        ) from error
    if not launch.is_dir() or not root.is_dir():
        raise ProjectModelsError("project model discovery requires directories")

    current = launch
    while True:
        loaded = _read_candidate(current / _PROJECT_DIRECTORY)
        if loaded is not None:
            path, content = loaded
            assignments = _parse_assignments(path, content, base.models)
            stack_name, stacks = _ephemeral_stacks(content, assignments, base)
            return ProjectModels(
                path=path,
                digest=hashlib.sha256(content).hexdigest(),
                stack_name=stack_name,
                assignments=assignments,
                stacks=stacks,
            )
        if current == root:
            return None
        current = current.parent
