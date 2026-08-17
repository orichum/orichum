#!/usr/bin/env python3
"""Create and verify workflow-owned, digest-bound session state."""

import argparse
import hashlib
import hmac
import json
import os
import secrets
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from integrations.common.atlassian_mcp import (
    AtlassianError,
    managed_binary as managed_atlassian_binary,
)
from integrations.common.leanctx_contract import (
    config_bytes as leanctx_config_bytes,
    mcp_server as leanctx_mcp_server,
)
from integrations.common.model_routing import (
    EffectiveStack,
    ROLES,
    RoutingError,
    load_catalog,
    load_routing_view,
    materialize_runtime_plugin,
    resolve_effective,
    validate_agent_contract,
    validate_model_id,
    validate_stack_name,
)
from integrations.common.project_context import ContextError, load_config, resolve_context


class SessionError(RuntimeError):
    """Raised when session state does not satisfy its ownership boundary."""


class _SessionMcpMismatch(SessionError):
    """An unpublished session's MCP snapshot no longer matches its context."""


@dataclass(frozen=True)
class SessionPaths:
    run_id: str
    run_dir: Path
    context_file: Path
    context_sha256: str
    mcp_file: Path
    effective_models_file: Path
    effective_models_sha256: str
    plugin_dir: Path
    controller_model: str


@dataclass(frozen=True)
class ContextBinding:
    """Descriptor-verified authority for one immutable session context."""

    workflow_root: Path
    run_id: str
    run_dir: Path
    context_file: Path
    context_sha256: str
    context: dict[str, object]


def _same_object(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _same_stable_state(
    first: os.stat_result, second: os.stat_result
) -> bool:
    return (
        _same_object(first, second)
        and stat.S_IFMT(first.st_mode) == stat.S_IFMT(second.st_mode)
        and stat.S_IMODE(first.st_mode) == stat.S_IMODE(second.st_mode)
        and first.st_uid == second.st_uid
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
        and first.st_ctime_ns == second.st_ctime_ns
    )


def _absolute_lexical(path: Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else Path.cwd() / path


def _stable_lstat(path: Path) -> os.stat_result:
    try:
        first = os.lstat(path)
        second = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as error:
        raise SessionError("session component is unavailable") from error
    if not _same_object(first, second):
        raise SessionError("session component changed during validation")
    return second


def _require_directory(
    path: Path,
    *,
    parent: Optional[Path] = None,
    expected_mode: Optional[int] = None,
) -> Path:
    path = _absolute_lexical(path)
    observed = _stable_lstat(path)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise SessionError("session component must be a real directory")
    if observed.st_uid != os.getuid():
        raise SessionError("session component has an unexpected owner")
    if expected_mode is not None and stat.S_IMODE(observed.st_mode) != expected_mode:
        raise SessionError("session component has unsafe permissions")

    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SessionError("session component cannot be canonicalized") from error
    if real != path:
        raise SessionError("session component is not a canonical path")
    if parent is not None:
        parent_real = Path(parent).resolve(strict=True)
        if path.parent != parent_real or real.parent != parent_real:
            raise SessionError("session component is not a direct child")

    final = _stable_lstat(path)
    if not _same_object(observed, final):
        raise SessionError("session component changed during validation")
    return real


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require_owned_component(
    parent: Path,
    name: str,
    *,
    private: bool,
    create: bool = False,
) -> Path:
    """Validate one fixed, current-UID-owned direct child directory."""
    if not name or name in {".", ".."} or Path(name).name != name:
        raise SessionError("invalid session component name")
    parent = _require_directory(parent)
    child = parent / name
    try:
        os.lstat(child)
    except FileNotFoundError:
        if not create:
            raise SessionError("required session component is missing")
        try:
            os.mkdir(child, 0o700)
        except FileExistsError:
            pass
        except OSError as error:
            raise SessionError("session component could not be created") from error
        else:
            try:
                _fsync_directory(parent)
            except OSError as error:
                raise SessionError(
                    "session component could not be created"
                ) from error
    except OSError as error:
        raise SessionError("session component is unavailable") from error
    return _require_directory(
        child,
        parent=parent,
        expected_mode=0o700 if private else None,
    )


def require_private_direct_child(
    parent: Path, child: Path, *, expected_mode: int = 0o700
) -> Path:
    """Validate a private canonical directory directly below its parent."""
    parent = _require_directory(parent, expected_mode=0o700)
    child = _absolute_lexical(child)
    if child.parent != parent:
        raise SessionError("session directory is not a direct child")
    return _require_directory(child, parent=parent, expected_mode=expected_mode)


def _require_owned_file(parent: Path, path: Path, expected_mode: int) -> Path:
    parent = _require_directory(parent, expected_mode=0o700)
    path = _absolute_lexical(path)
    if path.parent != parent:
        raise SessionError("session file is not a direct child")
    observed = _stable_lstat(path)
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise SessionError("session file must be a real regular file")
    if observed.st_uid != os.getuid():
        raise SessionError("session file has an unexpected owner")
    if stat.S_IMODE(observed.st_mode) != expected_mode:
        raise SessionError("session file has unsafe permissions")
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SessionError("session file cannot be canonicalized") from error
    if real != path or real.parent != parent:
        raise SessionError("session file is not canonical")
    final = _stable_lstat(path)
    if not _same_object(observed, final):
        raise SessionError("session file changed during validation")
    return real


def _canonical_json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _session_mcp_payload(
    context: dict[str, object],
    run_dir: Optional[Path] = None,
    data_root: Optional[Path] = None,
) -> dict[str, object]:
    """Expose only installed, project-relevant MCP servers for this session."""
    servers: dict[str, object] = {}
    route = context.get("route")

    if (
        isinstance(route, dict)
        and route.get("scope") in {None, "context"}
        and data_root is not None
    ):
        project_root = route.get("contextRootReal")
        if (
            route.get("atlassianConfigured") is True
            and isinstance(project_root, str)
            and project_root
        ):
            try:
                managed_atlassian_binary(data_root)
            except AtlassianError as error:
                raise SessionError(
                    "project Atlassian MCP is not ready"
                ) from error
            launcher = (
                Path(__file__).resolve().parents[2]
                / "bin"
                / "orichum-atlassian-mcp"
            )
            if not launcher.is_file() or not os.access(launcher, os.X_OK):
                raise SessionError(
                    "Orichum Atlassian MCP launcher is unavailable"
                )
            arguments = [project_root]
            jira_profile = route.get("jiraProfile")
            if jira_profile is not None:
                if not isinstance(jira_profile, str) or not jira_profile:
                    raise SessionError("project Jira profile is invalid")
                arguments.append(jira_profile)
            servers["atlassian"] = {
                "command": str(launcher),
                "args": arguments,
            }

    if run_dir is not None and data_root is not None:
        binary = _leanctx_binary(data_root)
        project_root = _leanctx_project_root(context)
        if binary is not None and project_root is not None:
            servers["leanctx"] = leanctx_mcp_server(
                binary,
                project_root,
                run_dir / "leanctx",
                data_root / "leanctx",
            )

    return {"mcpServers": servers}


def atomic_private_bytes(
    path: Path, data: bytes, mode: int = 0o600
) -> bytes:
    """Write private bytes through an exclusive no-follow temporary file."""
    if mode != 0o600:
        raise SessionError("session files must use mode 0600")
    path = _absolute_lexical(path)
    parent = require_private_direct_child(
        path.parent.parent, path.parent, expected_mode=0o700
    )
    if path.parent != parent or path.name in {"", ".", ".."}:
        raise SessionError("invalid session file path")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SessionError("session file path is unavailable") from error
    else:
        raise SessionError("session file already exists")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
    directory_fd = os.open(parent, directory_flags)
    temporary_name = f".{path.name}.{secrets.token_hex(12)}"
    file_fd: Optional[int] = None
    replaced = False
    try:
        parent_stat = os.fstat(directory_fd)
        if not _same_object(parent_stat, _stable_lstat(parent)):
            raise SessionError("session directory changed during file creation")
        open_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            open_flags |= os.O_NOFOLLOW
        file_fd = os.open(temporary_name, open_flags, mode, dir_fd=directory_fd)
        os.fchmod(file_fd, mode)
        written = 0
        while written < len(data):
            written += os.write(file_fd, data[written:])
        os.fsync(file_fd)
        temporary_stat = os.fstat(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        replaced = True
        os.fsync(directory_fd)
        final = _stable_lstat(path)
        if not _same_object(temporary_stat, final):
            raise SessionError("session file changed during installation")
        _require_owned_file(parent, path, mode)
    except OSError as error:
        raise SessionError("session file could not be written") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if not replaced:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
        os.close(directory_fd)
    return data


def atomic_json(path: Path, payload: dict, mode: int = 0o600) -> bytes:
    """Write canonical JSON through a private atomic file."""
    return atomic_private_bytes(path, _canonical_json_bytes(payload), mode)


def _leanctx_binary(data_root: Path) -> Path | None:
    try:
        binary_dir = _require_directory(
            data_root / "bin",
            parent=data_root,
            expected_mode=0o700,
        )
        return _require_owned_file(
            binary_dir,
            binary_dir / "lean-ctx",
            0o755,
        )
    except SessionError:
        return None


def _leanctx_project_root(context: dict[str, object]) -> Path | None:
    repository = context.get("repoRootReal")
    if isinstance(repository, str) and repository:
        return Path(repository)
    route = context.get("route")
    project = route.get("contextRootReal") if isinstance(route, dict) else None
    if isinstance(project, str) and project:
        return Path(project)
    if isinstance(route, dict) and route.get("scope") == "normal":
        launch = context.get("launchDirReal")
        if isinstance(launch, str) and launch:
            return Path(launch)
    return None


def _materialize_leanctx(
    context: dict[str, object],
    data_root: Path,
    run_dir: Path,
) -> None:
    if (
        _leanctx_binary(data_root) is None
        or _leanctx_project_root(context) is None
    ):
        return
    directory = require_owned_component(
        run_dir,
        "leanctx",
        private=True,
        create=True,
    )
    for name in ("config", "state"):
        require_owned_component(
            directory,
            name,
            private=True,
            create=True,
        )
    shared = require_owned_component(
        data_root,
        "leanctx",
        private=True,
        create=True,
    )
    for name in ("cache", "lean-ctx"):
        require_owned_component(
            shared,
            name,
            private=True,
            create=True,
        )
    atomic_private_bytes(
        directory / "config" / "config.toml",
        leanctx_config_bytes(),
        0o600,
    )


def verify_leanctx_attachment(run_dir: Path) -> Path:
    """Return a LeanCTX directory only when its exact contract is intact."""
    try:
        directory = require_private_direct_child(
            run_dir,
            run_dir / "leanctx",
            expected_mode=0o700,
        )
        config = require_private_direct_child(
            directory,
            directory / "config",
            expected_mode=0o700,
        )
        require_private_direct_child(
            directory,
            directory / "state",
            expected_mode=0o700,
        )
        data_root = run_dir.parents[2]
        shared = require_owned_component(
            data_root,
            "leanctx",
            private=True,
        )
        for name in ("cache", "lean-ctx"):
            require_owned_component(
                shared,
                name,
                private=True,
            )
        observed = _read_owned_file(config, "config.toml", 0o600)
    except SessionError as error:
        raise SessionError(
            "LeanCTX configuration is unavailable or unsafe"
        ) from error
    if not hmac.compare_digest(observed, leanctx_config_bytes()):
        raise SessionError("LeanCTX configuration does not match its contract")
    return directory


def _verify_leanctx(
    context: dict[str, object],
    data_root: Path,
    run_dir: Path,
) -> None:
    if (
        _leanctx_binary(data_root) is None
        or _leanctx_project_root(context) is None
    ):
        return
    verify_leanctx_attachment(run_dir)


def _validate_file_stat(observed: os.stat_result, expected_mode: int) -> None:
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISREG(observed.st_mode):
        raise SessionError("session file must be a real regular file")
    if observed.st_uid != os.getuid():
        raise SessionError("session file has an unexpected owner")
    if stat.S_IMODE(observed.st_mode) != expected_mode:
        raise SessionError("session file has unsafe permissions")


def _read_owned_file(
    parent: Path, file_name: str, expected_mode: int = 0o600
) -> bytes:
    """Read one fixed child once through a no-follow, parent-anchored descriptor."""
    if Path(file_name).name != file_name or file_name in {"", ".", ".."}:
        raise SessionError("invalid session file name")
    parent = require_private_direct_child(
        Path(parent).parent, parent, expected_mode=0o700
    )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise SessionError("no-follow file access is unavailable")
    directory_fd = os.open(parent, directory_flags | no_follow)
    file_fd: Optional[int] = None
    try:
        parent_before = os.fstat(directory_fd)
        if not _same_stable_state(parent_before, _stable_lstat(parent)):
            raise SessionError("session directory changed before file read")
        try:
            path_before = os.stat(
                file_name, dir_fd=directory_fd, follow_symlinks=False
            )
            file_fd = os.open(
                file_name, os.O_RDONLY | no_follow, dir_fd=directory_fd
            )
        except OSError as error:
            raise SessionError("session file could not be opened safely") from error
        descriptor_before = os.fstat(file_fd)
        _validate_file_stat(path_before, expected_mode)
        _validate_file_stat(descriptor_before, expected_mode)
        if not _same_stable_state(path_before, descriptor_before):
            raise SessionError("session file changed before reading")

        blocks = []
        while True:
            block = os.read(file_fd, 65536)
            if not block:
                break
            blocks.append(block)

        descriptor_after = os.fstat(file_fd)
        try:
            path_after = os.stat(
                file_name, dir_fd=directory_fd, follow_symlinks=False
            )
        except OSError as error:
            raise SessionError("session file changed during reading") from error
        _validate_file_stat(descriptor_after, expected_mode)
        _validate_file_stat(path_after, expected_mode)
        if not _same_stable_state(descriptor_before, descriptor_after):
            raise SessionError("session file descriptor changed during reading")
        if not _same_stable_state(descriptor_after, path_after):
            raise SessionError("session file path changed during reading")
        parent_after = os.fstat(directory_fd)
        if not _same_stable_state(
            parent_before, parent_after
        ) or not _same_stable_state(
            parent_after, _stable_lstat(parent)
        ):
            raise SessionError("session directory changed during file read")
        return b"".join(blocks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _sha256_owned_file(
    parent: Path, file_name: str, expected_mode: int = 0o600
) -> str:
    """Stream one fixed private child through a stable no-follow descriptor."""
    if Path(file_name).name != file_name or file_name in {"", ".", ".."}:
        raise SessionError("invalid session file name")
    parent = require_private_direct_child(
        Path(parent).parent, parent, expected_mode=0o700
    )
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise SessionError("no-follow file access is unavailable")
    directory_fd = os.open(parent, directory_flags | no_follow)
    file_fd: Optional[int] = None
    try:
        parent_before = os.fstat(directory_fd)
        if not _same_stable_state(parent_before, _stable_lstat(parent)):
            raise SessionError("session directory changed before file read")
        try:
            path_before = os.stat(
                file_name, dir_fd=directory_fd, follow_symlinks=False
            )
            file_fd = os.open(
                file_name, os.O_RDONLY | no_follow, dir_fd=directory_fd
            )
        except OSError as error:
            raise SessionError("session file could not be opened safely") from error
        descriptor_before = os.fstat(file_fd)
        _validate_file_stat(path_before, expected_mode)
        _validate_file_stat(descriptor_before, expected_mode)
        if not _same_stable_state(path_before, descriptor_before):
            raise SessionError("session file changed before reading")

        digest = hashlib.sha256()
        while True:
            block = os.read(file_fd, 64 * 1024)
            if not block:
                break
            digest.update(block)

        descriptor_after = os.fstat(file_fd)
        try:
            path_after = os.stat(
                file_name, dir_fd=directory_fd, follow_symlinks=False
            )
        except OSError as error:
            raise SessionError("session file changed during reading") from error
        _validate_file_stat(descriptor_after, expected_mode)
        _validate_file_stat(path_after, expected_mode)
        if not _same_stable_state(descriptor_before, descriptor_after):
            raise SessionError("session file descriptor changed during reading")
        if not _same_stable_state(descriptor_after, path_after):
            raise SessionError("session file path changed during reading")
        parent_after = os.fstat(directory_fd)
        if not _same_stable_state(
            parent_before, parent_after
        ) or not _same_stable_state(
            parent_after, _stable_lstat(parent)
        ):
            raise SessionError("session directory changed during file read")
        return digest.hexdigest()
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_workflow_root(workflow_root: Path) -> Path:
    return _require_directory(_absolute_lexical(workflow_root))


def _validated_session_ancestors(
    workflow_root: Path, data_root: Optional[Path] = None
) -> tuple[Path, Path, Path, Path]:
    workflow_root = _validated_workflow_root(workflow_root)
    if data_root is None:
        data_root = require_owned_component(workflow_root, "runtime", private=False)
    else:
        data_root = _require_directory(_absolute_lexical(data_root), expected_mode=0o700)
    state = require_owned_component(data_root, "state", private=True)
    sessions = require_owned_component(state, "sessions", private=True)
    return workflow_root, data_root, state, sessions


def _discard_incomplete_run(sessions: Path, run_dir: Path) -> None:
    """Remove one unpublished direct child without following a replacement."""
    if run_dir.parent != sessions or not run_dir.name.startswith("run."):
        raise SessionError("incomplete session path is not managed")
    try:
        observed = os.lstat(run_dir)
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(
            observed.st_mode
        ):
            os.unlink(run_dir)
        else:
            shutil.rmtree(run_dir)
        _fsync_directory(sessions)
    except FileNotFoundError:
        return
    except OSError as error:
        raise SessionError("incomplete session could not be removed") from error


def create_session(
    workflow_root: Path,
    launch_dir: Path,
    config_path: Path,
    data_root: Optional[Path] = None,
    *,
    routing_path: Path,
    models_path: Path,
    plugin_source: Path,
) -> SessionPaths:
    """Create one session with immutable routing and project MCP state."""
    workflow_root = _validated_workflow_root(workflow_root)
    verification_data_root = data_root
    if data_root is None:
        data_root = require_owned_component(workflow_root, "runtime", private=False)
    else:
        data_root = _require_directory(_absolute_lexical(data_root), expected_mode=0o700)
    context = resolve_context(load_config(config_path), launch_dir)
    routing = load_routing_view(routing_path)
    route = context.get("route")
    requested_stack = (
        route.get("modelStack")
        if isinstance(route, dict)
        and isinstance(route.get("modelStack"), str)
        else None
    )
    effective = resolve_effective(
        routing, load_catalog(models_path), requested_stack
    )
    return _materialize_session(
        workflow_root,
        data_root,
        context,
        effective,
        plugin_source,
        verification_data_root,
    )


def create_resolved_session(
    workflow_root: Path,
    *,
    data_root: Path,
    context: dict[str, object],
    effective: EffectiveStack,
    plugin_source: Path,
) -> SessionPaths:
    """Materialize one resolved Orichum run through hardened run machinery."""
    workflow_root = _validated_workflow_root(workflow_root)
    data_root = _require_directory(
        _absolute_lexical(data_root), expected_mode=0o700
    )
    try:
        effective = _parse_effective_models(
            _canonical_json_bytes(effective.as_json())
        )
    except (AttributeError, TypeError) as error:
        raise SessionError("resolved effective models are invalid") from error
    if not isinstance(context, dict):
        raise SessionError("resolved session context is invalid")
    return _materialize_session(
        workflow_root,
        data_root,
        context,
        effective,
        plugin_source,
        data_root,
    )


def _materialize_session(
    workflow_root: Path,
    data_root: Path,
    context: dict[str, object],
    effective: EffectiveStack,
    plugin_source: Path,
    verification_data_root: Optional[Path],
) -> SessionPaths:
    return _materialize_session_once(
        workflow_root,
        data_root,
        context,
        effective,
        plugin_source,
        verification_data_root,
    )


def _materialize_session_once(
    workflow_root: Path,
    data_root: Path,
    context: dict[str, object],
    effective: EffectiveStack,
    plugin_source: Path,
    verification_data_root: Optional[Path],
) -> SessionPaths:
    state = require_owned_component(data_root, "state", private=True, create=True)
    sessions = require_owned_component(state, "sessions", private=True, create=True)
    run_dir: Optional[Path] = None
    published = False
    try:
        try:
            run_dir = Path(tempfile.mkdtemp(prefix="run.", dir=sessions))
            _fsync_directory(sessions)
        except OSError as error:
            raise SessionError(
                "session directory could not be created"
            ) from error
        run_dir = require_private_direct_child(
            sessions, run_dir, expected_mode=0o700
        )
        physical_context = dict(context)
        context_file = run_dir / "context.json"
        context_bytes = atomic_json(context_file, physical_context, 0o600)
        context_sha256 = hashlib.sha256(context_bytes).hexdigest()

        effective_file = run_dir / "effective-models.json"
        effective_bytes = atomic_json(
            effective_file, effective.as_json(), 0o600
        )
        effective_sha256 = hashlib.sha256(effective_bytes).hexdigest()
        materialize_runtime_plugin(
            plugin_source, run_dir / "plugin", effective
        )
        _materialize_leanctx(physical_context, data_root, run_dir)

        mcp_file = run_dir / "mcp.json"
        atomic_json(
            mcp_file,
            _session_mcp_payload(physical_context, run_dir, data_root),
            0o600,
        )
        session = verify_session(
            workflow_root,
            run_dir,
            context_sha256,
            effective_sha256,
            verification_data_root,
        )
        atomic_json(
            run_dir / ".complete",
            {
                "schemaVersion": 1,
                "contextSha256": context_sha256,
                "effectiveModelsSha256": effective_sha256,
            },
            0o600,
        )
        published = True
        return session
    finally:
        if run_dir is not None and not published:
            _discard_incomplete_run(sessions, run_dir)


def verify_context_binding(
    workflow_root: Path,
    run_dir: Path,
    context_file: Path,
    context_sha256: str,
    run_id: str,
    data_root: Optional[Path] = None,
) -> ContextBinding:
    """Bind fixed authority fields to the exact verified context bytes."""
    workflow_root, _, _, sessions = _validated_session_ancestors(
        workflow_root, data_root
    )
    if (
        not isinstance(run_id, str)
        or not run_id.startswith("run.")
        or Path(run_id).name != run_id
        or run_id in {"", ".", ".."}
    ):
        raise SessionError("run identifier is invalid")
    run_dir = _absolute_lexical(run_dir)
    expected_run_dir = sessions / run_id
    if (
        run_dir != expected_run_dir
        or run_dir.parent != sessions
        or run_dir.name != run_id
    ):
        raise SessionError("run directory is not a managed direct child")
    run_dir = require_private_direct_child(sessions, run_dir, expected_mode=0o700)
    context_file = _absolute_lexical(context_file)
    if context_file != run_dir / "context.json":
        raise SessionError("context file is not the fixed session child")
    if (
        not isinstance(context_sha256, str)
        or len(context_sha256) != 64
        or any(character not in "0123456789abcdef" for character in context_sha256)
    ):
        raise SessionError("context digest is invalid")

    context_bytes = _read_owned_file(run_dir, "context.json", 0o600)
    observed_digest = hashlib.sha256(context_bytes).hexdigest()
    if not hmac.compare_digest(observed_digest, context_sha256):
        raise SessionError("context digest mismatch")
    try:
        context = json.loads(context_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise SessionError("session context is invalid") from error
    if not isinstance(context, dict):
        raise SessionError("session context is invalid")
    return ContextBinding(
        workflow_root=workflow_root,
        run_id=run_id,
        run_dir=run_dir,
        context_file=context_file,
        context_sha256=context_sha256,
        context=context,
    )


def _exact_effective_object(
    value: object, keys: set[str], label: str
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise SessionError(f"{label} has invalid fields")
    return value


def _parse_effective_models(data: bytes) -> EffectiveStack:
    try:
        raw = json.loads(data)
        document = _exact_effective_object(
            raw,
            {
                "schemaVersion",
                "stack",
                "controller",
                "configuredCandidates",
                "agents",
            },
            "effective model mapping",
        )
        if (
            type(document["schemaVersion"]) is not int
            or document["schemaVersion"] != 1
        ):
            raise SessionError("effective model mapping has invalid schema")
        stack_name = validate_stack_name(document["stack"], "effective stack")
        controller = validate_model_id(
            document["controller"], "effective controller"
        )
        candidates_raw = _exact_effective_object(
            document["configuredCandidates"],
            set(ROLES),
            "effective configured candidates",
        )
        agents_raw = _exact_effective_object(
            document["agents"], set(ROLES), "effective agents"
        )
        candidates: dict[str, tuple[str, ...]] = {}
        agents: dict[str, str] = {}
        for role in ROLES:
            values = candidates_raw[role]
            if not isinstance(values, list) or not values:
                raise SessionError(
                    f"effective role {role} has invalid candidates"
                )
            role_candidates = tuple(
                validate_model_id(value, f"effective role {role}")
                for value in values
            )
            if len(role_candidates) != len(set(role_candidates)):
                raise SessionError(
                    f"effective role {role} has duplicate candidates"
                )
            selected = validate_model_id(
                agents_raw[role], f"effective role {role}"
            )
            if selected not in role_candidates:
                raise SessionError(
                    f"effective role {role} selection is not configured"
                )
            candidates[role] = role_candidates
            agents[role] = selected
        return EffectiveStack(
            stack_name, controller, candidates, agents
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RoutingError) as error:
        raise SessionError("effective model mapping is invalid") from error


def _validate_plugin_directory_stat(observed: os.stat_result) -> None:
    if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
        raise SessionError("runtime plugin entry must be a real directory")
    if observed.st_uid != os.getuid():
        raise SessionError("runtime plugin entry has an unexpected owner")
    if stat.S_IMODE(observed.st_mode) != 0o700:
        raise SessionError("runtime plugin directory has unsafe permissions")


def _verify_plugin_file_entry(
    directory_fd: int,
    name: str,
    observed: os.stat_result,
    mode: int,
) -> None:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    file_fd: Optional[int] = None
    try:
        file_fd = os.open(
            name, os.O_RDONLY | no_follow, dir_fd=directory_fd
        )
        descriptor_before = os.fstat(file_fd)
        _validate_file_stat(descriptor_before, mode)
        if not _same_stable_state(observed, descriptor_before):
            raise SessionError(
                "runtime plugin file changed before verification"
            )
        descriptor_after = os.fstat(file_fd)
        path_after = os.stat(
            name, dir_fd=directory_fd, follow_symlinks=False
        )
        _validate_file_stat(descriptor_after, mode)
        _validate_file_stat(path_after, mode)
        if not _same_stable_state(
            descriptor_before, descriptor_after
        ) or not _same_stable_state(descriptor_after, path_after):
            raise SessionError(
                "runtime plugin file changed during verification"
            )
    except OSError as error:
        raise SessionError(
            "runtime plugin file could not be verified safely"
        ) from error
    finally:
        if file_fd is not None:
            os.close(file_fd)


def _verify_plugin_tree(
    directory: Path,
    file_modes: dict[Path, int],
    *,
    parent_fd: Optional[int] = None,
    entry_name: Optional[str] = None,
    expected: Optional[os.stat_result] = None,
) -> None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if not no_follow:
        raise SessionError("no-follow plugin access is unavailable")
    directory_fd: Optional[int] = None
    try:
        if parent_fd is None:
            path_before = _stable_lstat(directory)
            directory_fd = os.open(
                directory, directory_flags | no_follow
            )
        else:
            if entry_name is None:
                raise SessionError("runtime plugin entry name is missing")
            path_before = os.stat(
                entry_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            directory_fd = os.open(
                entry_name,
                directory_flags | no_follow,
                dir_fd=parent_fd,
            )
        descriptor_before = os.fstat(directory_fd)
        _validate_plugin_directory_stat(path_before)
        _validate_plugin_directory_stat(descriptor_before)
        if (
            not _same_stable_state(path_before, descriptor_before)
            or (
                expected is not None
                and not _same_stable_state(expected, descriptor_before)
            )
        ):
            raise SessionError(
                "runtime plugin directory changed before verification"
            )

        with os.scandir(directory_fd) as iterator:
            entries = list(iterator)
        for entry in entries:
            path = directory / entry.name
            try:
                observed = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise SessionError(
                    "runtime plugin entry is unavailable"
                ) from error
            if stat.S_ISLNK(observed.st_mode):
                raise SessionError("runtime plugin contains a symlink")
            if stat.S_ISDIR(observed.st_mode):
                _validate_plugin_directory_stat(observed)
                _require_directory(
                    path, parent=directory, expected_mode=0o700
                )
                _verify_plugin_tree(
                    path,
                    file_modes,
                    parent_fd=directory_fd,
                    entry_name=entry.name,
                    expected=observed,
                )
                continue
            if not stat.S_ISREG(observed.st_mode):
                raise SessionError(
                    "runtime plugin contains a special file"
                )
            mode = stat.S_IMODE(observed.st_mode)
            if mode not in {0o600, 0o700}:
                raise SessionError(
                    "runtime plugin file has unsafe permissions"
                )
            _validate_file_stat(observed, mode)
            _verify_plugin_file_entry(
                directory_fd, entry.name, observed, mode
            )
            file_modes[path] = mode

        descriptor_after = os.fstat(directory_fd)
        if parent_fd is None:
            path_after = _stable_lstat(directory)
        else:
            path_after = os.stat(
                entry_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        _validate_plugin_directory_stat(descriptor_after)
        _validate_plugin_directory_stat(path_after)
        if not _same_stable_state(
            descriptor_before, descriptor_after
        ) or not _same_stable_state(descriptor_after, path_after):
            raise SessionError(
                "runtime plugin directory changed during verification"
            )
    except OSError as error:
        raise SessionError(
            "runtime plugin could not be enumerated safely"
        ) from error
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _verify_runtime_plugin(
    run_dir: Path, effective: EffectiveStack
) -> Path:
    plugin_dir = require_private_direct_child(
        run_dir, run_dir / "plugin", expected_mode=0o700
    )
    file_modes: dict[Path, int] = {}
    _verify_plugin_tree(plugin_dir, file_modes)
    for role in ROLES:
        agents_dir = _require_directory(
            plugin_dir / "agents",
            parent=plugin_dir,
            expected_mode=0o700,
        )
        agent_name = f"{role}.md"
        agent_mode = file_modes.get(agents_dir / agent_name)
        if agent_mode not in {0o600, 0o700}:
            raise SessionError(
                f"runtime agent {role} has unsafe permissions"
            )
        agent_path = _require_owned_file(
            agents_dir, agents_dir / agent_name, agent_mode
        )
        try:
            text = _read_owned_file(
                agent_path.parent, agent_path.name, agent_mode
            ).decode("utf-8")
        except UnicodeDecodeError as error:
            raise SessionError(
                f"runtime agent {role} is not valid UTF-8"
            ) from error
        try:
            validate_agent_contract(
                text, role, effective.agents[role]
            )
        except RoutingError as error:
            raise SessionError(
                str(error)
            ) from error
    return plugin_dir


def verify_session(
    workflow_root: Path,
    run_dir: Path,
    context_sha256: str,
    effective_models_sha256: str,
    data_root: Optional[Path] = None,
) -> SessionPaths:
    """Revalidate immutable routing, context, plugin, and MCP session state."""
    run_dir = _absolute_lexical(run_dir)
    binding = verify_context_binding(
        workflow_root,
        run_dir,
        run_dir / "context.json",
        context_sha256,
        run_dir.name,
        data_root,
    )
    verified_data_root = binding.run_dir.parents[2]
    _verify_leanctx(binding.context, verified_data_root, binding.run_dir)
    mcp_file = binding.run_dir / "mcp.json"
    mcp_bytes = _read_owned_file(binding.run_dir, "mcp.json", 0o600)
    if mcp_bytes != _canonical_json_bytes(
        _session_mcp_payload(
            binding.context,
            binding.run_dir,
            verified_data_root,
        )
    ):
        raise _SessionMcpMismatch(
            "session MCP configuration does not match its context"
        )
    if (
        not isinstance(effective_models_sha256, str)
        or len(effective_models_sha256) != 64
        or any(
            character not in "0123456789abcdef"
            for character in effective_models_sha256
        )
    ):
        raise SessionError("effective model digest is invalid")
    effective_models_file = binding.run_dir / "effective-models.json"
    effective_bytes = _read_owned_file(
        binding.run_dir, "effective-models.json", 0o600
    )
    observed_digest = hashlib.sha256(effective_bytes).hexdigest()
    if not hmac.compare_digest(observed_digest, effective_models_sha256):
        raise SessionError("effective model digest mismatch")
    effective = _parse_effective_models(effective_bytes)
    plugin_dir = _verify_runtime_plugin(binding.run_dir, effective)
    return SessionPaths(
        run_id=binding.run_id,
        run_dir=binding.run_dir,
        context_file=binding.context_file,
        context_sha256=binding.context_sha256,
        mcp_file=mcp_file,
        effective_models_file=effective_models_file,
        effective_models_sha256=effective_models_sha256,
        plugin_dir=plugin_dir,
        controller_model=effective.controller,
    )


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--workflow-root", required=True, type=Path)
    create.add_argument("--launch-dir", required=True, type=Path)
    create.add_argument("--config", type=Path)
    create.add_argument("--data-root", type=Path)
    create.add_argument("--routing-config", required=True, type=Path)
    create.add_argument("--models-file", required=True, type=Path)
    create.add_argument("--plugin-source", required=True, type=Path)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--workflow-root", required=True, type=Path)
    verify.add_argument("--run-dir", required=True, type=Path)
    verify.add_argument("--context-sha256", required=True)
    verify.add_argument("--effective-models-sha256", required=True)
    verify.add_argument("--data-root", type=Path)
    return parser


def main() -> int:
    arguments = _create_parser().parse_args()
    try:
        if arguments.command == "create":
            config_path = arguments.config or (
                arguments.workflow_root / "controller" / "project-context.json"
            )
            session = create_session(
                arguments.workflow_root, arguments.launch_dir, config_path,
                arguments.data_root,
                routing_path=arguments.routing_config,
                models_path=arguments.models_file,
                plugin_source=arguments.plugin_source,
            )
            print(
                json.dumps(
                    {
                        "runId": session.run_id,
                        "runDir": str(session.run_dir),
                        "contextFile": str(session.context_file),
                        "contextSha256": session.context_sha256,
                        "mcpFile": str(session.mcp_file),
                        "effectiveModelsFile": str(
                            session.effective_models_file
                        ),
                        "effectiveModelsSha256": (
                            session.effective_models_sha256
                        ),
                        "pluginDir": str(session.plugin_dir),
                        "controllerModel": session.controller_model,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            verify_session(
                arguments.workflow_root,
                arguments.run_dir,
                arguments.context_sha256,
                arguments.effective_models_sha256,
                arguments.data_root,
            )
    except (
        SessionError,
        ContextError,
        RoutingError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ):
        print("ERROR: owned session state rejected", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
