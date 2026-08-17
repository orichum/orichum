#!/usr/bin/env python3
"""Resolve immutable workflow context from a physical launch directory."""

import argparse
import contextlib
import contextvars
import fcntl
import getpass
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Collection, Optional

from .atlassian_mcp import (
    AtlassianConfig,
    AtlassianError,
    normalize_atlassian,
)
from .github_identity import GithubIdentityError, validate_github_account
from .model_routing import (
    RoutingError,
    load_routing_view,
    validate_stack_name,
)
from .orichum_completion import set_completion


class ContextError(RuntimeError):
    pass


_CONTEXT_REQUIRED_KEYS = {"root", "atlassian"}
_CONTEXT_OPTIONAL_KEYS = {
    "modelStack",
    "accountPools",
    "githubAccount",
}
_CONTROL_PLANE_ROOT: contextvars.ContextVar[Path | None] = (
    contextvars.ContextVar("control_plane_root", default=None)
)


def _context_object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ContextError(f"{label} must be an object")
    keys = set(value)
    if (
        not _CONTEXT_REQUIRED_KEYS.issubset(keys)
        or keys - _CONTEXT_REQUIRED_KEYS - _CONTEXT_OPTIONAL_KEYS
    ):
        raise ContextError(f"{label} has invalid fields")
    return value


def _model_stack(
    value: object, stacks: Optional[dict] = None
) -> Optional[str]:
    if value is None:
        return None
    try:
        name = validate_stack_name(value, "modelStack")
    except RoutingError as error:
        raise ContextError("modelStack is invalid") from error
    if stacks is not None and name not in stacks:
        raise ContextError("modelStack is not configured")
    return name


def _expand(value: str, home: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ContextError("configured path must be a non-empty string")
    expanded = (
        Path(str(home) + value[1:])
        if value == "~" or value.startswith("~/")
        else Path(value)
    )
    return expanded.expanduser()


def _contains(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _git_root(launch_real: Path) -> Optional[str]:
    completed = subprocess.run(
        ["git", "-C", str(launch_real), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    if completed.returncode != 0:
        return None
    root = Path(completed.stdout.strip()).resolve(strict=True)
    return str(root) if _contains(root, launch_real) else None


def _require_exact_keys(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        raise ContextError(f"{label} must contain exactly {sorted(expected)}")
    return value


def _require_non_blank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContextError(f"{label} must be a non-empty string")
    return value


def _require_optional_non_blank(
    value: object, label: str
) -> Optional[str]:
    if value is None:
        return None
    return _require_non_blank(value, label)


def _atlassian_config(value: object) -> AtlassianConfig | None:
    try:
        return normalize_atlassian(value)
    except AtlassianError as error:
        raise ContextError(str(error)) from error


def _structural_path(value: object, home: Path, label: str) -> Path:
    value = _require_non_blank(value, label)
    if value == "~" or value.startswith("~/"):
        expanded = Path(home) / value[2:] if value != "~" else Path(home)
    elif value.startswith("~"):
        raise ContextError(f"{label} uses unsupported tilde syntax")
    else:
        expanded = Path(value)
    if not expanded.is_absolute():
        raise ContextError(f"{label} must be absolute or use ~/ syntax")
    return Path(os.path.normpath(str(expanded)))


def _structural_existing_ancestor_path(
    path: Path, home: Path, label: str, *, reject_symlinks: bool
) -> Path:
    home_real = home.resolve(strict=False)
    lexical_root = Path(path.anchor)
    if path == lexical_root or path == home:
        raise ContextError(f"{label} is unsafe")
    cursor = lexical_root
    for component in path.parts[1:]:
        cursor /= component
        try:
            value = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as error:
            raise ContextError(f"{label} existing ancestor is inaccessible") from error
        if reject_symlinks and stat.S_ISLNK(value.st_mode):
            raise ContextError(f"{label} existing ancestors must not be symlinks")
    try:
        canonical = path.resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise ContextError(f"{label} could not be resolved") from error
    if canonical == Path(canonical.anchor) or canonical == home_real:
        raise ContextError(f"{label} is unsafe")
    return canonical


def _normal_scope(
    value: object,
    *,
    stacks: Optional[dict] = None,
    account_pools: Optional[set[str]] = None,
) -> dict | None:
    if value is None:
        return None
    normal = _require_exact_keys(
        value,
        {"modelStack", "accountPools"},
        "normal scope",
    )
    model_stack = _model_stack(normal["modelStack"], stacks)
    pools = normal["accountPools"]
    if (
        not isinstance(pools, list)
        or not pools
        or any(not isinstance(pool, str) or not pool.strip() for pool in pools)
        or len(pools) != len(set(pools))
    ):
        raise ContextError("normal accountPools must be a non-empty unique list")
    if account_pools is not None and any(pool not in account_pools for pool in pools):
        raise ContextError("normal accountPools names an unknown pool")
    return {"modelStack": model_stack, "accountPools": list(pools)}


def validate_config_document(
    raw: object,
    home: Path,
    stacks: Optional[dict] = None,
    account_pools: Optional[set[str]] = None,
) -> None:
    """Validate an already-parsed portable project-context document."""
    if not isinstance(raw, dict):
        raise ContextError("configuration must be an object")
    focused = "schemaVersion" in raw
    if focused:
        schema_version = raw.get("schemaVersion")
        expected = (
            {"schemaVersion", "contexts"}
            if schema_version == 1
            else {"schemaVersion", "normal", "contexts"}
        )
    else:
        expected = {"contexts"}
    config = _require_exact_keys(raw, expected, "configuration")
    if focused and (
        type(config["schemaVersion"]) is not int
        or config["schemaVersion"] not in {1, 2}
    ):
        raise ContextError("schemaVersion must be exactly 1 or 2")
    if config.get("schemaVersion") == 2:
        _normal_scope(config["normal"], stacks=stacks, account_pools=account_pools)
    raw_contexts = config["contexts"]
    if not isinstance(raw_contexts, list):
        raise ContextError("contexts must be a list")

    lexical_roots = []
    for index, raw_context in enumerate(raw_contexts):
        context = _context_object(raw_context, f"context {index}")
        root = _structural_path(context["root"], home, "root")
        _atlassian_config(context["atlassian"])
        _model_stack(context.get("modelStack"), stacks)
        try:
            validate_github_account(context.get("githubAccount"))
        except GithubIdentityError as error:
            raise ContextError("githubAccount is invalid") from error
        pools = context.get("accountPools")
        if focused:
            if (
                not isinstance(pools, list)
                or not pools
                or any(
                    not isinstance(pool, str) or not pool.strip()
                    for pool in pools
                )
                or len(pools) != len(set(pools))
            ):
                raise ContextError("accountPools must be a non-empty unique list")
            if account_pools is not None and any(
                pool not in account_pools for pool in pools
            ):
                raise ContextError("accountPools names an unknown pool")
        elif pools is not None:
            raise ContextError("accountPools requires schemaVersion")
        if any(_contains(existing_root, root) or _contains(root, existing_root)
               for existing_root in lexical_roots):
            raise ContextError("configured roots must not overlap")
        lexical_roots.append(root)


def validate_config_structure(
    config_path: Path, home: Optional[Path] = None
) -> None:
    """Validate routing structure and existing path ancestors without mutation."""
    home = Path.home() if home is None else Path(home)
    try:
        with Path(config_path).open(encoding="utf-8") as handle:
            raw = json.load(handle)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ContextError("configuration could not be parsed") from error

    validate_config_document(raw, home)
    for context in raw["contexts"]:
        root = _structural_path(context["root"], home, "root")
        _structural_existing_ancestor_path(
            root, home, "root", reject_symlinks=False
        )


def load_config(config_path: Path, home: Optional[Path] = None) -> dict:
    """Load and fully validate immutable routing configuration."""
    home = Path.home() if home is None else Path(home)
    with Path(config_path).open(encoding="utf-8") as handle:
        raw = json.load(handle)

    config = _require_exact_keys(raw, {"contexts"}, "configuration")

    raw_contexts = config["contexts"]
    if not isinstance(raw_contexts, list):
        raise ContextError("contexts must be a list")

    contexts = []
    canonical_roots = set()
    for index, raw_context in enumerate(raw_contexts):
        context = _context_object(raw_context, f"context {index}")
        root_path = _expand(context["root"], home)
        if not root_path.is_absolute():
            raise ContextError("configured root must expand to an absolute path")
        try:
            root_real = root_path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ContextError("configured root must resolve to a directory") from error
        if not root_real.is_dir():
            raise ContextError("configured root must resolve to a directory")
        if root_real == Path(root_real.anchor) or root_real == home.resolve(strict=False):
            raise ContextError("configured root is unsafe")

        atlassian = _atlassian_config(context["atlassian"])
        model_stack = _model_stack(context.get("modelStack"))
        if any(_contains(existing_root, root_real) or _contains(root_real, existing_root)
               for existing_root in canonical_roots):
            raise ContextError("configured roots must not overlap")
        canonical_roots.add(root_real)
        contexts.append(
            {
                "root": root_real,
                "atlassian": (
                    atlassian.as_json() if atlassian is not None else None
                ),
                "modelStack": model_stack,
            }
        )

    return {"contexts": contexts}


def resolve_context(config: dict, launch_dir: Path) -> dict:
    """Resolve one fixed route and one independent Git root for a launch."""
    launch_real = Path(launch_dir).resolve(strict=True)
    if not launch_real.is_dir():
        raise ContextError("launch directory must resolve to a directory")

    matches = [
        context
        for context in config["contexts"]
        if _contains(context["root"], launch_real)
    ]
    matches.sort(key=lambda context: len(context["root"].parts), reverse=True)

    route = None
    if matches:
        selected = matches[0]
        route = {
            "id": selected["root"].name,
            "contextRootReal": str(selected["root"]),
            "atlassianConfigured": selected["atlassian"] is not None,
            "modelStack": selected["modelStack"],
        }

    return {
        "schemaVersion": 1,
        "launchDirReal": str(launch_real),
        "repoRootReal": _git_root(launch_real),
        "route": route,
    }


def resolve_control_plane_context(
    project_document: object,
    launch_dir: Path,
    home: Optional[Path] = None,
) -> dict:
    """Resolve the validated Orichum project document without a temp file."""
    home = Path.home() if home is None else Path(home)
    if not isinstance(project_document, dict):
        raise ContextError("projects document has invalid schema")
    schema_version = project_document.get("schemaVersion")
    if schema_version is None:
        document = _require_exact_keys(
            project_document, {"contexts"}, "projects"
        )
    else:
        expected = (
            {"schemaVersion", "contexts"}
            if schema_version == 1
            else {"schemaVersion", "normal", "contexts"}
        )
        document = _require_exact_keys(project_document, expected, "projects")
    if (
        schema_version not in {None, 1, 2}
        or (schema_version is not None and type(schema_version) is not int)
        or not isinstance(document["contexts"], list)
    ):
        raise ContextError("projects document has invalid schema")
    normal = (
        _normal_scope(document["normal"])
        if schema_version == 2
        else None
    )
    normalized = []
    pools_by_root: dict[str, tuple[str, ...]] = {}
    canonical_roots: set[Path] = set()
    for index, raw in enumerate(document["contexts"]):
        if not isinstance(raw, dict):
            raise ContextError(f"project context {index} must be an object")
        context = raw
        expected = {
            "root",
            "atlassian",
            "modelStack",
            "accountPools",
        }
        if set(context) not in (expected, expected | {"githubAccount"}):
            raise ContextError(f"project context {index} has invalid fields")
        pools = context["accountPools"]
        if (
            not isinstance(pools, list)
            or not pools
            or any(not isinstance(pool, str) or not pool for pool in pools)
            or len(pools) != len(set(pools))
        ):
            raise ContextError("project accountPools are invalid")
        root = _expand(context["root"], home)
        try:
            root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ContextError("configured root must resolve to a directory") from error
        if not root.is_dir():
            raise ContextError("configured root must resolve to a directory")
        if root == Path(root.anchor) or root == home.resolve(strict=False):
            raise ContextError("configured root is unsafe")
        if any(
            _contains(existing, root) or _contains(root, existing)
            for existing in canonical_roots
        ):
            raise ContextError("configured roots must not overlap")
        canonical_roots.add(root)
        atlassian = _atlassian_config(context["atlassian"])
        normalized.append(
            {
                "root": root,
                "atlassian": (
                    atlassian.as_json()
                    if atlassian is not None
                    else None
                ),
                "modelStack": _model_stack(context["modelStack"]),
                "githubAccount": validate_github_account(
                    context.get("githubAccount")
                ),
            }
        )
        pools_by_root[str(root)] = tuple(pools)
    resolved = resolve_context({"contexts": normalized}, launch_dir)
    route = resolved.get("route")
    if isinstance(route, dict):
        route["scope"] = "context"
        route["accountPools"] = list(pools_by_root[route["contextRootReal"]])
        route["githubAccount"] = next(
            context["githubAccount"]
            for context in normalized
            if str(context["root"]) == route["contextRootReal"]
        )
    elif normal is not None:
        resolved["route"] = {
            "id": "normal",
            "scope": "normal",
            "atlassianConfigured": False,
            "modelStack": normal["modelStack"],
            "accountPools": normal["accountPools"],
            "githubAccount": None,
        }
    return resolved


def _atomic_json(output: Path, payload: dict) -> None:
    output = Path(output)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(file_descriptor, 0o600)
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        os.close(file_descriptor) if _descriptor_is_open(file_descriptor) else None
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _descriptor_is_open(file_descriptor: int) -> bool:
    try:
        os.fstat(file_descriptor)
        return True
    except OSError:
        return False


def _read_context_document(
    config_path: Path,
    home: Path,
    stacks: Optional[dict] = None,
    account_pools: Optional[set[str]] = None,
) -> dict:
    try:
        with Path(config_path).open(encoding="utf-8") as handle:
            document = json.load(handle)
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ContextError("configuration could not be parsed") from error
    validate_config_document(document, home, stacks, account_pools)
    return document


def _fsync_context_directory(parent: Path) -> None:
    descriptor = os.open(
        parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


_MAX_CONTEXT_RECOVERY_BYTES = 2 * 1024 * 1024


def _context_recovery_paths(
    config_path: Path,
) -> tuple[Path, Path]:
    parent = config_path.parent
    return (
        parent / f".{config_path.name}.transaction.json",
        parent / f".{config_path.name}.transaction.original",
    )


def _stage_context_bytes(
    path: Path, payload: bytes, mode: int
) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError(
                    "configuration write made no progress"
                )
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        return temporary
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _private_context_artifact(
    path: Path, label: str
) -> bytes | None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ContextError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(details.st_mode)
        or not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise ContextError(f"{label} is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ContextError(f"{label} is unavailable") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (details.st_dev, details.st_ino)
        ):
            raise ContextError(f"{label} changed while opening")
        chunks = []
        size = 0
        while True:
            chunk = os.read(
                descriptor,
                min(
                    65536,
                    _MAX_CONTEXT_RECOVERY_BYTES + 1 - size,
                ),
            )
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > _MAX_CONTEXT_RECOVERY_BYTES:
                raise ContextError(f"{label} is too large")
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
            raise ContextError(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _replace_private_context_artifact(
    path: Path, payload: bytes
) -> None:
    temporary = _stage_context_bytes(path, payload, 0o600)
    try:
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _context_marker_payload(
    state: str, original_mode: int
) -> bytes:
    return (
        json.dumps(
            {
                "schemaVersion": 1,
                "state": state,
                "originalMode": original_mode,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_context_marker(
    config_path: Path, state: str, original_mode: int
) -> None:
    if state not in {"pending", "committed"}:
        raise ContextError("configuration transaction state is invalid")
    marker, _ = _context_recovery_paths(config_path)
    _replace_private_context_artifact(
        marker, _context_marker_payload(state, original_mode)
    )


def _load_context_marker(
    config_path: Path,
) -> tuple[str, int] | None:
    marker, _ = _context_recovery_paths(config_path)
    content = _private_context_artifact(
        marker, "configuration transaction marker"
    )
    if content is None:
        return None
    try:
        raw = json.loads(content.decode("utf-8"))
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise ContextError(
            "configuration transaction marker is invalid"
        ) from error
    if (
        not isinstance(raw, dict)
        or set(raw)
        != {"schemaVersion", "state", "originalMode"}
        or type(raw["schemaVersion"]) is not int
        or raw["schemaVersion"] != 1
        or raw["state"] not in {"pending", "committed"}
        or type(raw["originalMode"]) is not int
        or raw["originalMode"] < 0
        or raw["originalMode"] > 0o777
    ):
        raise ContextError(
            "configuration transaction marker is invalid"
        )
    return raw["state"], raw["originalMode"]


def _remove_context_recovery_artifacts(
    config_path: Path,
) -> None:
    marker, original = _context_recovery_paths(config_path)
    if _private_context_artifact(
        original, "configuration recovery file"
    ) is not None:
        original.unlink()
    if _private_context_artifact(
        marker, "configuration transaction marker"
    ) is not None:
        marker.unlink()
    _fsync_context_directory(config_path.parent)


def _cleanup_context_recovery(config_path: Path) -> None:
    try:
        _remove_context_recovery_artifacts(config_path)
    except BaseException:
        pass


def _restore_context_original(
    config_path: Path, original_mode: int
) -> None:
    _, original = _context_recovery_paths(config_path)
    content = _private_context_artifact(
        original, "configuration recovery file"
    )
    if content is None:
        raise ContextError(
            "configuration recovery file is unavailable"
        )
    temporary = _stage_context_bytes(
        config_path, content, original_mode
    )
    try:
        os.replace(temporary, config_path)
        temporary = None
        _fsync_context_directory(config_path.parent)
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _finish_context_recovery(
    config_path: Path, original_mode: int
) -> None:
    _write_context_marker(
        config_path, "committed", original_mode
    )
    _fsync_context_directory(config_path.parent)
    _cleanup_context_recovery(config_path)


def _recover_context_transaction(config_path: Path) -> None:
    marker = _load_context_marker(config_path)
    _, original = _context_recovery_paths(config_path)
    if marker is None:
        if _private_context_artifact(
            original, "configuration recovery file"
        ) is not None:
            _cleanup_context_recovery(config_path)
        return
    state, original_mode = marker
    if state == "pending":
        _restore_context_original(config_path, original_mode)
        _finish_context_recovery(config_path, original_mode)
        return
    _cleanup_context_recovery(config_path)


def _rollback_context_transaction(
    config_path: Path, original_mode: int
) -> None:
    try:
        _write_context_marker(
            config_path, "pending", original_mode
        )
        _restore_context_original(config_path, original_mode)
        _finish_context_recovery(config_path, original_mode)
    except BaseException as error:
        raise ContextError(
            "configuration transaction rollback failed"
        ) from error


def _write_context_document(config_path: Path, document: dict) -> None:
    config_path = Path(config_path)
    mode = stat.S_IMODE(config_path.stat().st_mode)
    original = config_path.read_bytes()
    payload = (json.dumps(document, indent=2) + "\n").encode("utf-8")
    temporary: Path | None = None
    recovery_ready = False
    durable_commit = False
    try:
        temporary = _stage_context_bytes(
            config_path, payload, mode
        )
        _, original_path = _context_recovery_paths(config_path)
        _replace_private_context_artifact(
            original_path, original
        )
        _fsync_context_directory(config_path.parent)
        _write_context_marker(config_path, "pending", mode)
        recovery_ready = True
        _fsync_context_directory(config_path.parent)
        os.replace(temporary, config_path)
        temporary = None
        _fsync_context_directory(config_path.parent)
        _write_context_marker(config_path, "committed", mode)
        _fsync_context_directory(config_path.parent)
        durable_commit = True
    except BaseException as error:
        if temporary is not None:
            try:
                temporary.unlink()
            except BaseException:
                pass
        if recovery_ready:
            try:
                _rollback_context_transaction(config_path, mode)
            except BaseException as rollback_error:
                raise ContextError(
                    "configuration transaction rollback failed"
                ) from rollback_error
        else:
            _cleanup_context_recovery(config_path)
        if isinstance(error, ContextError):
            raise
        raise ContextError(
            "configuration durability could not be confirmed"
        ) from error
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except BaseException:
                pass
    if durable_commit:
        _cleanup_context_recovery(config_path)


@contextlib.contextmanager
def control_plane_transaction(config_root: Path):
    """Serialize mutations that depend on documents in one config root."""
    root = Path(config_root).resolve(strict=False)
    active_root = _CONTROL_PLANE_ROOT.get()
    if active_root is not None:
        if active_root != root:
            raise ContextError(
                "nested control-plane transactions require one config root"
            )
        yield
        return

    descriptor = None
    try:
        descriptor = os.open(root, os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        if descriptor is not None:
            os.close(descriptor)
        raise ContextError("configuration lock is unavailable") from error
    token = _CONTROL_PLANE_ROOT.set(root)
    try:
        yield
    finally:
        _CONTROL_PLANE_ROOT.reset(token)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


@contextlib.contextmanager
def _context_lock(config_path: Path):
    config_path = Path(config_path)
    with control_plane_transaction(config_path.parent):
        _recover_context_transaction(config_path)
        yield


def configure_normal_scope(
    config_path: Path,
    *,
    model_stack: str | None,
    account_pools: Collection[str],
    known_stacks: Collection[str],
    known_pools: Collection[str],
) -> None:
    if model_stack is not None and model_stack not in known_stacks:
        raise ContextError("model stack is unknown")
    pools = list(dict.fromkeys(account_pools))
    if not pools or any(pool not in known_pools for pool in pools):
        raise ContextError("normal accountPools are invalid")
    config_path = Path(config_path)
    home = Path.home()
    with _context_lock(config_path):
        document = _read_context_document(config_path, home)
        candidate = {
            "schemaVersion": 2,
            "normal": {
                "modelStack": model_stack,
                "accountPools": pools,
            },
            "contexts": list(document["contexts"]),
        }
        validate_config_document(
            candidate,
            home,
            dict.fromkeys(known_stacks),
            set(known_pools),
        )
        _write_context_document(config_path, candidate)


def assign_stack_to_context(
    config_path: Path,
    launch_dir: Path,
    stack: str,
    known_stacks: Collection[str],
) -> Path:
    if stack not in known_stacks:
        raise ContextError("model stack is unknown")
    config_path = Path(config_path)
    home = Path.home()
    with _context_lock(config_path):
        document = _read_context_document(config_path, home)
        resolved = resolve_control_plane_context(
            document, launch_dir, home=home
        )
        route = resolved.get("route")
        if (
            not isinstance(route, dict)
            or route.get("scope") != "context"
        ):
            raise ContextError("current directory has no project context")
        matched = Path(route["contextRootReal"])
        for context in document["contexts"]:
            root = _context_root(
                context["root"], home, must_exist=True
            )
            if root.resolve() == matched:
                context["modelStack"] = stack
                break
        else:
            raise ContextError(
                "matched project context disappeared"
            )
        validate_config_document(document, home)
        _write_context_document(config_path, document)
        return matched


def configure_project_atlassian(
    config_path: Path,
    launch_dir: Path,
    config: AtlassianConfig | None,
) -> Path:
    """Set or clear Jira credentials directly on one project context."""
    config_path = Path(config_path)
    home = Path.home()
    with _context_lock(config_path):
        document = _read_context_document(config_path, home)
        resolved = resolve_control_plane_context(
            document, launch_dir, home=home
        )
        route = resolved.get("route")
        if (
            not isinstance(route, dict)
            or route.get("scope") != "context"
        ):
            raise ContextError("current directory has no project context")
        matched = Path(route["contextRootReal"])
        for context in document["contexts"]:
            root = _context_root(
                context["root"], home, must_exist=True
            )
            if root.resolve() == matched:
                context["atlassian"] = (
                    config.as_json() if config is not None else None
                )
                break
        else:
            raise ContextError(
                "matched project context disappeared"
            )
        validate_config_document(document, home)
        _write_context_document(config_path, document)
        return matched


def _context_root(value: str, home: Path, *, must_exist: bool) -> Path:
    root = _structural_path(value, home, "root")
    if root == Path(root.anchor) or root == home:
        raise ContextError("root is unsafe")
    if not must_exist:
        return root
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ContextError("root must resolve to an existing directory") from error
    if not resolved.is_dir():
        raise ContextError("root must resolve to an existing directory")
    if resolved == Path(resolved.anchor) or resolved == home.resolve(strict=False):
        raise ContextError("root is unsafe")
    return resolved


def _validate_context_candidate(
    document: dict,
    home: Path,
    stacks: Optional[dict] = None,
    account_pools: Optional[set[str]] = None,
) -> None:
    validate_config_document(document, home, stacks, account_pools)
    canonical_roots = []
    for context in document["contexts"]:
        root = _context_root(context["root"], home, must_exist=True)
        if any(_contains(existing_root, root) or _contains(root, existing_root)
               for existing_root in canonical_roots):
            raise ContextError("configured roots must not overlap")
        canonical_roots.append(root)


def _find_context_index(contexts: list[dict], root: Path, home: Path) -> int:
    for index, context in enumerate(contexts):
        if _context_root(context["root"], home, must_exist=False) == root:
            return index
    raise ContextError("context root is not configured")


def _find_exact_context_index(contexts: list[dict], root: str) -> int:
    for index, context in enumerate(contexts):
        if context["root"] == root:
            return index
    raise ContextError("context root is not configured")


def _find_canonical_context_index(
    contexts: list[dict], root: Path, home: Path
) -> int:
    for index, context in enumerate(contexts):
        if _context_root(context["root"], home, must_exist=True) == root:
            return index
    raise ContextError("context root is not configured")


def _build_add_candidate(
    document: dict,
    parsed: argparse.Namespace,
    home: Path,
    account_pools: Optional[set[str]] = None,
) -> tuple[dict, dict]:
    root = _context_root(parsed.root, home, must_exist=True)
    context = {
        "root": str(root),
        "atlassian": None,
        "modelStack": parsed.model_stack,
    }
    if "schemaVersion" in document:
        context["githubAccount"] = validate_github_account(
            parsed.github_account
        )
    if "schemaVersion" in document:
        requested = list(parsed.pool or ())
        if not requested:
            requested.append("shared")
        context["accountPools"] = list(dict.fromkeys(requested))
    candidate = {
        **(
            {"schemaVersion": document["schemaVersion"]}
            if "schemaVersion" in document
            else {}
        ),
        **({"normal": document["normal"]} if "normal" in document else {}),
        "contexts": [*document["contexts"], context],
    }
    return candidate, context


def _render_context_table(contexts: list[dict], default_stack: str) -> str:
    columns = (
        ("PROJECT ROOT", "root"),
        ("MODEL STACK", "modelStack"),
        ("JIRA", "atlassian"),
        ("GITHUB ACCOUNT", "githubAccount"),
    )

    def render_value(context: dict, key: str) -> str:
        value = (
            context.get(key)
            if key in {"modelStack", "githubAccount"}
            else context[key]
        )
        if key == "modelStack" and value is None:
            return f"{default_stack} (global)"
        if key == "atlassian" and isinstance(value, dict):
            return str(value["url"])
        return "—" if value is None else value

    rows = [
        tuple(render_value(context, key) for _, key in columns)
        for context in contexts
    ]
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, (header, _) in enumerate(columns)
    ]
    border = "+" + "+".join("-" * (width + 2) for width in widths) + "+"

    def render_row(values: tuple[str, ...]) -> str:
        return "| " + " | ".join(
            value.ljust(width) for value, width in zip(values, widths)
        ) + " |"

    header = tuple(label for label, _ in columns)
    return "\n".join((border, render_row(header), border,
                      *(render_row(row) for row in rows), border)) + "\n"
def _load_context_routing(path: Path) -> dict[str, object]:
    return load_routing_view(path)


def _load_account_pool_names(path: Optional[Path]) -> Optional[set[str]]:
    if path is None:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError, OSError) as error:
        raise ContextError("providers configuration could not be parsed") from error
    if (
        not isinstance(raw, dict)
        or type(raw.get("schemaVersion")) is not int
        or raw["schemaVersion"] != 1
        or not isinstance(raw.get("accountPools"), dict)
    ):
        raise ContextError("providers configuration has invalid accountPools")
    pools = set(raw["accountPools"])
    if not pools or any(not isinstance(pool, str) or not pool for pool in pools):
        raise ContextError("providers configuration has invalid accountPools")
    return pools


def add_context_commands(
    commands: argparse._SubParsersAction,
) -> None:
    def command(name: str, summary: str) -> argparse.ArgumentParser:
        return commands.add_parser(
            name,
            help=summary,
            description=summary,
        )

    command("list", "List configured project contexts.")
    add = command("add", "Add a project context mapping.")
    set_completion(
        add.add_argument(
            "root",
            metavar="ROOT",
            help="project root or parent directory to map",
        ),
        "directory",
    )
    set_completion(
        add.add_argument(
            "--model-stack",
            metavar="STACK",
            help="model stack to bind to the context",
        ),
        "stack",
    )
    set_completion(
        add.add_argument(
            "--pool",
            action="append",
            metavar="POOL",
            help="account pool to use; repeat for ordered fallback",
        ),
        "pool",
    )
    add.add_argument(
        "--github-account",
        metavar="ACCOUNT",
        help="GitHub account identity to bind",
    )
    jira = command("jira", "Configure or remove Jira for a project context.")
    set_completion(
        jira.add_argument(
            "root",
            metavar="ROOT",
            help="configured project root",
        ),
        "context",
    )
    jira.add_argument(
        "--url",
        metavar="URL",
        help="Jira base URL",
    )
    jira.add_argument(
        "--username",
        metavar="USER",
        help="Jira username or email address",
    )
    jira.add_argument(
        "--remove",
        action="store_true",
        help="remove Jira from the context",
    )
    update = command("update", "Update a project context mapping.")
    set_completion(
        update.add_argument(
            "root",
            metavar="ROOT",
            help="configured project root",
        ),
        "context",
    )
    set_completion(
        update.add_argument(
            "--model-stack",
            metavar="STACK",
            help="replace the bound model stack",
        ),
        "stack",
    )
    set_completion(
        update.add_argument(
            "--pool",
            action="append",
            metavar="POOL",
            help="replace account pools; repeat for ordered fallback",
        ),
        "pool",
    )
    update.add_argument(
        "--github-account",
        metavar="ACCOUNT",
        help="replace the bound GitHub account",
    )
    update.add_argument(
        "--no-github-account",
        action="store_true",
        help="remove the bound GitHub account",
    )
    update.add_argument(
        "--inherit-model-stack",
        action="store_true",
        help="inherit the model stack instead of binding one explicitly",
    )
    remove = command("remove", "Remove a project context mapping.")
    set_completion(
        remove.add_argument(
            "root",
            metavar="ROOT",
            help="configured project root",
        ),
        "context",
    )
    remove.add_argument(
        "--yes",
        action="store_true",
        help="remove without an interactive confirmation",
    )
    command("validate", "Validate every configured project context.")


def context_main(arguments: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="orichum context",
        description="Manage project context mappings.",
    )
    parser.add_argument(
        "--config",
        required=True,
        type=Path,
        metavar="FILE",
        help="project context configuration file",
    )
    parser.add_argument(
        "--routing-config",
        required=True,
        type=Path,
        metavar="FILE",
        help="model stack configuration file",
    )
    parser.add_argument(
        "--providers-config",
        type=Path,
        metavar="FILE",
        help="provider and account-pool configuration file",
    )
    commands = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="COMMAND",
    )
    add_context_commands(commands)
    parsed = parser.parse_args(arguments)
    home = Path.home()

    try:
        requested_stack = getattr(parsed, "model_stack", None)
        if requested_stack is not None:
            requested_stack = _model_stack(requested_stack)
            parsed.model_stack = requested_stack
        if (
            parsed.command == "update"
            and parsed.model_stack is not None
            and parsed.inherit_model_stack
        ):
            raise ContextError(
                "modelStack cannot be explicit and inherited"
            )

        if parsed.command == "add":
            with _context_lock(parsed.config):
                routing = _load_context_routing(parsed.routing_config)
                routing_stacks = routing["stacks"]
                account_pools = _load_account_pool_names(
                    parsed.providers_config
                )
                document = _read_context_document(parsed.config, home)
                candidate, _ = _build_add_candidate(
                    document, parsed, home, account_pools
                )
                _validate_context_candidate(
                    candidate, home, routing_stacks, account_pools
                )
                _write_context_document(parsed.config, candidate)
            return 0

        lock = _context_lock(parsed.config)
        with lock:
            routing = _load_context_routing(parsed.routing_config)
            routing_stacks = routing["stacks"]
            account_pools = _load_account_pool_names(
                parsed.providers_config
            )
            document = _read_context_document(
                parsed.config,
                home,
                routing_stacks
                if parsed.command in ("list", "validate")
                else None,
                account_pools,
            )
            contexts = document["contexts"]
            if parsed.command == "validate":
                _validate_context_candidate(
                    document, home, routing_stacks, account_pools
                )
                return 0
            if parsed.command == "list":
                print(
                    _render_context_table(
                        contexts, str(routing["defaultStack"])
                    ),
                    end="",
                )
                return 0
            try:
                root = _context_root(parsed.root, home, must_exist=False)
                index = _find_context_index(contexts, root, home)
            except ContextError:
                if parsed.command != "remove":
                    raise
                index = _find_exact_context_index(contexts, parsed.root)
            if parsed.command == "jira":
                if parsed.remove:
                    if parsed.url is not None or parsed.username is not None:
                        raise ContextError(
                            "--remove cannot be combined with Jira fields"
                        )
                    configured = None
                else:
                    existing = _atlassian_config(
                        contexts[index]["atlassian"]
                    )
                    default_url = existing.url if existing is not None else ""
                    default_username = (
                        existing.username if existing is not None else ""
                    )
                    url = parsed.url or input(
                        "Jira URL"
                        + (f" [{default_url}]" if default_url else "")
                        + ": "
                    ).strip() or default_url
                    username = parsed.username or input(
                        "Jira username"
                        + (
                            f" [{default_username}]"
                            if default_username
                            else ""
                        )
                        + ": "
                    ).strip() or default_username
                    token = getpass.getpass(
                        "Jira API token"
                        + (" [keep existing]" if existing is not None else "")
                        + ": "
                    ).strip()
                    if not token and existing is not None:
                        token = existing.api_token
                    try:
                        configured = AtlassianConfig(
                            url=url,
                            username=username,
                            api_token=token,
                        )
                    except AtlassianError as error:
                        raise ContextError(str(error)) from error
                candidate = {
                    **({"schemaVersion": document["schemaVersion"]} if "schemaVersion" in document else {}),
                    **(
                        {"normal": document["normal"]}
                        if "normal" in document
                        else {}
                    ),
                    "contexts": list(contexts),
                }
                replacement = dict(contexts[index])
                replacement["atlassian"] = (
                    configured.as_json()
                    if configured is not None
                    else None
                )
                candidate["contexts"][index] = replacement
                _validate_context_candidate(
                    candidate, home, routing_stacks, account_pools
                )
                _write_context_document(parsed.config, candidate)
                return 0
            if parsed.command == "update":
                if (
                    all(
                        value is None
                        for value in (
                            parsed.model_stack,
                            parsed.pool,
                            parsed.github_account,
                        )
                    )
                    and not parsed.inherit_model_stack
                    and not parsed.no_github_account
                ):
                    raise ContextError("update requires a replacement field")
                replacement = dict(contexts[index])
                if parsed.model_stack is not None:
                    replacement["modelStack"] = parsed.model_stack
                elif parsed.inherit_model_stack:
                    replacement["modelStack"] = None
                if parsed.github_account is not None:
                    replacement["githubAccount"] = validate_github_account(
                        parsed.github_account
                    )
                elif parsed.no_github_account:
                    replacement["githubAccount"] = None
                if parsed.pool is not None:
                    if "schemaVersion" not in document:
                        raise ContextError(
                            "--pool requires a focused projects configuration"
                        )
                    replacement["accountPools"] = list(
                        dict.fromkeys(parsed.pool)
                    )
                candidate = {
                    **({"schemaVersion": document["schemaVersion"]} if "schemaVersion" in document else {}),
                    **(
                        {"normal": document["normal"]}
                        if "normal" in document
                        else {}
                    ),
                    "contexts": list(contexts),
                }
                candidate["contexts"][index] = replacement
                _validate_context_candidate(
                    candidate, home, routing_stacks, account_pools
                )
                _write_context_document(parsed.config, candidate)
                return 0

            context = contexts[index]
            print(json.dumps(context, indent=2))
            if not parsed.yes:
                try:
                    confirmation = input("Type REMOVE to confirm: ")
                except EOFError as error:
                    raise ContextError("remove requires confirmation") from error
                if confirmation != "REMOVE":
                    raise ContextError("remove requires confirmation")
            candidate = {
                **({"schemaVersion": document["schemaVersion"]} if "schemaVersion" in document else {}),
                **(
                    {"normal": document["normal"]}
                    if "normal" in document
                    else {}
                ),
                "contexts": list(contexts),
            }
            del candidate["contexts"][index]
            validate_config_document(
                candidate, home, routing_stacks, account_pools
            )
            _write_context_document(parsed.config, candidate)
            return 0
    except (ContextError, RoutingError):
        print("ERROR: project context operation rejected", file=sys.stderr)
        return 1


def main(arguments: Optional[list[str]] = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if arguments and arguments[0] == "context":
        return context_main(arguments[1:])
    if arguments and arguments[0] == "validate-config":
        parser = argparse.ArgumentParser()
        parser.add_argument("command", choices=("validate-config",))
        parser.add_argument("--config", required=True, type=Path)
        parsed = parser.parse_args(arguments)
        try:
            validate_config_structure(parsed.config)
        except ContextError:
            print("ERROR: project context configuration rejected", file=sys.stderr)
            return 1
        return 0

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--launch-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parsed = parser.parse_args(arguments)

    payload = resolve_context(load_config(parsed.config), parsed.launch_dir)
    _atomic_json(parsed.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
