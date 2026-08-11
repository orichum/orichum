#!/usr/bin/env python3
"""Short-lived installer transactions for the live Orichum control plane."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile

from .account_registry import (
    AccountError,
    account_registry_lock,
    load_accounts,
)
from .orichum_config import (
    MAX_CONFIG_BYTES,
    default_config_paths,
    load_control_plane,
)
from .plugin_registry import (
    PluginRegistryError,
    plugin_registry_lock,
)
from .project_context import control_plane_transaction
from .stack_bindings import MAX_BINDING_BYTES, load_stack_bindings
from .stack_definition import serialize_model_stacks
from .stack_store import (
    StackStoreError,
    load_stack_snapshot,
    planned_stack_digests,
    restore_stack_files,
    save_stack,
    validate_stack_bindings,
)


class InstallControlPlaneError(RuntimeError):
    """Installed control-plane migration failed closed."""


_BOOTSTRAP_FILES = (
    "projects.json",
    "jira-profiles.json",
    "providers.json",
    "plugins.json",
    "runtime.json",
    "controller-policy.md",
    "accounts.json",
)
_MANIFEST_NAME = "installed-control-plane.json"
_ACTIVE_PHASES = frozenset(
    {"prepared", "saving", "committed", "rollbackConflict"}
)
_TERMINAL_PHASES = frozenset({"rolledBack", "finalized"})


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _private_bytes(path: Path, label: str, limit: int) -> bytes:
    try:
        before = os.lstat(path)
    except OSError as error:
        raise InstallControlPlaneError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise InstallControlPlaneError(f"{label} is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallControlPlaneError(
            f"{label} could not be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino)
            != (before.st_dev, before.st_ino)
        ):
            raise InstallControlPlaneError(
                f"{label} changed while opening"
            )
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise InstallControlPlaneError(f"{label} is too large")
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
            raise InstallControlPlaneError(
                f"{label} changed while reading"
            )
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_private_root(path: Path) -> None:
    try:
        observed = os.lstat(path)
        resolved = path.resolve(strict=True)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise InstallControlPlaneError(
            "installed configuration root is unavailable"
        ) from error
    if (
        stat.S_ISLNK(observed.st_mode)
        or not stat.S_ISDIR(observed.st_mode)
        or observed.st_uid != os.getuid()
        or stat.S_IMODE(observed.st_mode) != 0o700
        or (observed.st_dev, observed.st_ino)
        != (confirmed.st_dev, confirmed.st_ino)
        or resolved != path
    ):
        raise InstallControlPlaneError(
            "installed configuration root is unsafe"
        )


def _private_child(path: Path) -> Path:
    requested = Path(path).absolute()
    requested_parent = requested.parent
    try:
        before = os.lstat(requested_parent)
        resolved = requested_parent.resolve(strict=True)
        after = os.lstat(requested_parent)
        confirmed = os.lstat(resolved)
    except (OSError, RuntimeError) as error:
        raise InstallControlPlaneError(
            "installer journal parent is unavailable"
        ) from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o700
        or resolved != requested_parent
    ):
        raise InstallControlPlaneError(
            "installer journal parent is unsafe"
        )
    if (
        (before.st_dev, before.st_ino)
        != (after.st_dev, after.st_ino)
        or (after.st_dev, after.st_ino)
        != (confirmed.st_dev, confirmed.st_ino)
    ):
        raise InstallControlPlaneError(
            "installer journal parent changed during validation"
        )
    _require_private_root(resolved)
    return resolved / requested.name


def _journal_checkpoint(phase: str) -> None:
    """Test seam after the journal domain is bound to held descriptors."""


def _verify_install_lock(lock_path: Path, descriptor: int) -> None:
    if type(descriptor) is not int or descriptor < 0:
        raise InstallControlPlaneError(
            "held installer lock descriptor is invalid"
        )
    lock_path = Path(lock_path).absolute()
    try:
        held = os.fstat(descriptor)
        current_lock = os.lstat(lock_path)
        resolved = lock_path.resolve(strict=True)
    except OSError as error:
        raise InstallControlPlaneError(
            "held installer lock is unavailable"
        ) from error
    if (
        resolved != lock_path
        or not stat.S_ISDIR(held.st_mode)
        or held.st_uid != os.getuid()
        or stat.S_IMODE(held.st_mode) != 0o700
        or stat.S_ISLNK(current_lock.st_mode)
        or not stat.S_ISDIR(current_lock.st_mode)
        or current_lock.st_uid != os.getuid()
        or stat.S_IMODE(current_lock.st_mode) != 0o700
        or (held.st_dev, held.st_ino)
        != (current_lock.st_dev, current_lock.st_ino)
    ):
        raise InstallControlPlaneError(
            "held installer lock identity is invalid"
        )


@dataclass
class _JournalDomain:
    state_fd: int
    name: str
    journal_fd: int = -1


@contextmanager
def _journal_domain(
    snapshot_root: Path,
    install_lock_path: Path,
    install_lock_fd: int,
    *,
    create: bool,
):
    snapshot_root = _private_child(snapshot_root)
    _verify_install_lock(install_lock_path, install_lock_fd)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    state_fd = os.open(snapshot_root.parent, flags)
    opened_state = os.fstat(state_fd)
    current_state = os.lstat(snapshot_root.parent)
    if (
        not stat.S_ISDIR(opened_state.st_mode)
        or opened_state.st_uid != os.getuid()
        or stat.S_IMODE(opened_state.st_mode) != 0o700
        or (opened_state.st_dev, opened_state.st_ino)
        != (current_state.st_dev, current_state.st_ino)
    ):
        os.close(state_fd)
        raise InstallControlPlaneError(
            "installer journal parent changed while opening"
        )
    domain = _JournalDomain(
        state_fd=state_fd,
        name=snapshot_root.name,
    )
    try:
        _journal_checkpoint("verified")
        journal_created = False
        try:
            details = os.stat(
                domain.name,
                dir_fd=domain.state_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            if not create:
                yield domain
                return
            os.mkdir(domain.name, mode=0o700, dir_fd=domain.state_fd)
            os.fsync(domain.state_fd)
            journal_created = True
            details = os.stat(
                domain.name,
                dir_fd=domain.state_fd,
                follow_symlinks=False,
            )
        if (
            stat.S_ISLNK(details.st_mode)
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise InstallControlPlaneError(
                "installer journal directory is unsafe"
            )
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        domain.journal_fd = os.open(
            domain.name,
            flags,
            dir_fd=domain.state_fd,
        )
        opened = os.fstat(domain.journal_fd)
        if (opened.st_dev, opened.st_ino) != (
            details.st_dev,
            details.st_ino,
        ):
            raise InstallControlPlaneError(
                "installer journal changed while opening"
            )
        if journal_created:
            _activation_checkpoint("journal-created")
        yield domain
    finally:
        if domain.journal_fd >= 0:
            os.close(domain.journal_fd)
        os.close(domain.state_fd)


def _exists_at(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _private_bytes_at(
    directory_fd: int,
    name: str,
    label: str,
    limit: int,
) -> bytes:
    try:
        before = os.stat(
            name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise InstallControlPlaneError(f"{label} is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
    ):
        raise InstallControlPlaneError(f"{label} is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (
            before.st_dev,
            before.st_ino,
        ):
            raise InstallControlPlaneError(
                f"{label} changed while opening"
            )
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(
                descriptor,
                min(65536, limit + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > limit:
            raise InstallControlPlaneError(f"{label} is too large")
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
            raise InstallControlPlaneError(
                f"{label} changed while reading"
            )
        return bytes(content)
    finally:
        os.close(descriptor)


def _atomic_private_at(
    directory_fd: int,
    name: str,
    payload: bytes,
    *,
    exclusive: bool,
) -> None:
    temporary: str | None = None
    target = name
    if not exclusive:
        temporary = f".{name}.{os.getpid()}.{secrets.token_hex(8)}"
        target = temporary
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(
        target,
        flags,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("private write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if temporary is not None:
            os.replace(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            temporary = None
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _atomic_private(path: Path, payload: bytes, *, exclusive: bool) -> None:
    if exclusive:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags, 0o600)
        temporary = None
    else:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temporary = Path(name)
        os.fchmod(descriptor, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("private write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if temporary is not None:
            os.replace(temporary, path)
            temporary = None
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def rollback_claude_settings(
    destination: Path,
    expected: Path,
    snapshot: Path | None,
) -> None:
    """Restore managed Claude settings without replacing concurrent drift."""
    destination = Path(destination).absolute()
    expected_payload = _private_bytes(
        Path(expected),
        "managed Claude settings",
        MAX_CONFIG_BYTES,
    )
    snapshot_payload: bytes | None = None
    snapshot_mode = 0o600
    if snapshot is not None:
        snapshot = Path(snapshot)
        before = os.lstat(snapshot)
        snapshot_mode = stat.S_IMODE(before.st_mode)
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or snapshot_mode & 0o022
        ):
            raise InstallControlPlaneError("prior Claude settings is unsafe")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(snapshot, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise InstallControlPlaneError(
                    "prior Claude settings changed while opening"
                )
            payload = bytearray()
            while len(payload) <= MAX_CONFIG_BYTES:
                chunk = os.read(
                    descriptor,
                    min(65536, MAX_CONFIG_BYTES + 1 - len(payload)),
                )
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > MAX_CONFIG_BYTES:
                raise InstallControlPlaneError(
                    "prior Claude settings is too large"
                )
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
                raise InstallControlPlaneError(
                    "prior Claude settings changed while reading"
                )
            snapshot_payload = bytes(payload)
        finally:
            os.close(descriptor)

    parent_before = os.lstat(destination.parent)
    _require_private_root(destination.parent)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(destination.parent, flags)
    parent_opened = os.fstat(directory_fd)
    if (parent_opened.st_dev, parent_opened.st_ino) != (
        parent_before.st_dev,
        parent_before.st_ino,
    ):
        os.close(directory_fd)
        raise InstallControlPlaneError(
            "Claude settings directory changed while opening"
        )

    displaced = f".{destination.name}.rollback.{secrets.token_hex(8)}"
    candidate = f".{destination.name}.prior.{secrets.token_hex(8)}"
    displaced_exists = False
    candidate_exists = False
    restored = False
    try:
        if snapshot_payload is not None:
            _atomic_private_at(
                directory_fd,
                candidate,
                snapshot_payload,
                exclusive=True,
            )
            candidate_exists = True
            os.chmod(candidate, snapshot_mode, dir_fd=directory_fd)
        try:
            current = os.stat(
                destination.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise InstallControlPlaneError(
                "managed Claude settings is unavailable"
            ) from error
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o600
        ):
            raise InstallControlPlaneError(
                "managed Claude settings is unsafe"
            )
        try:
            os.rename(
                destination.name,
                displaced,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            displaced_exists = True
        except OSError as error:
            raise InstallControlPlaneError(
                "Claude settings changed during rollback"
            ) from error

        current_payload = _private_bytes_at(
            directory_fd,
            displaced,
            "managed Claude settings",
            MAX_CONFIG_BYTES,
        )
        if current_payload != expected_payload:
            raise InstallControlPlaneError(
                "Claude settings changed during rollback"
            )
        if snapshot_payload is None:
            if _exists_at(directory_fd, destination.name):
                raise InstallControlPlaneError(
                    "Claude settings changed during rollback"
                )
        else:
            try:
                os.link(
                    candidate,
                    destination.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise InstallControlPlaneError(
                    "Claude settings changed during rollback"
                ) from error
            os.unlink(candidate, dir_fd=directory_fd)
            candidate_exists = False
        os.fsync(directory_fd)
        restored = True
    finally:
        if displaced_exists:
            if not restored and not _exists_at(
                directory_fd,
                destination.name,
            ):
                try:
                    os.link(
                        displaced,
                        destination.name,
                        src_dir_fd=directory_fd,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    pass
            os.unlink(displaced, dir_fd=directory_fd)
        if candidate_exists:
            os.unlink(candidate, dir_fd=directory_fd)
        os.fsync(directory_fd)
        os.close(directory_fd)


def _snapshot(
    path: Path,
    journal_fd: int,
    name: str,
    limit: int,
) -> bytes | None:
    for suffix in ("data", "present", "absent"):
        try:
            os.unlink(f"{name}.{suffix}", dir_fd=journal_fd)
        except FileNotFoundError:
            pass
    if not _lexists(path):
        _atomic_private_at(
            journal_fd,
            f"{name}.absent",
            b"",
            exclusive=True,
        )
        return None
    payload = _private_bytes(path, name, limit)
    _atomic_private_at(
        journal_fd,
        f"{name}.data",
        payload,
        exclusive=True,
    )
    _atomic_private_at(
        journal_fd,
        f"{name}.present",
        b"",
        exclusive=True,
    )
    return payload


def _candidate_payload(
    repository_root: Path, installed_root: Path, name: str
) -> bytes:
    if name == "controller-policy.md":
        return (repository_root / "config" / name).read_bytes()
    installed = installed_root / name
    if _lexists(installed):
        payload = _private_bytes(installed, name, MAX_CONFIG_BYTES)
        if name == "projects.json":
            return _normalize_projects_payload(payload)
        return payload
    if name == "accounts.json":
        return b'{"schemaVersion":2,"accounts":[]}\n'
    return (repository_root / "config" / name).read_bytes()


def _normalize_projects_payload(payload: bytes) -> bytes:
    """Normalize private project bindings without guessing Jira credentials."""
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise InstallControlPlaneError(
            "installed projects.json is invalid"
        ) from error
    if (
        not isinstance(document, dict)
        or set(document) != {"schemaVersion", "contexts"}
        or document.get("schemaVersion") != 1
        or not isinstance(document.get("contexts"), list)
    ):
        raise InstallControlPlaneError(
            "installed projects.json is invalid"
        )
    common = (
        "root",
        "modelStack",
        "accountPools",
    )
    current = {*common, "atlassian", "githubAccount"}
    retired = {
        "atlassianAccount",
        "dockerProfile",
        "memoryPalace",
        "memoryWing",
    }
    normalized = []
    for context in document["contexts"]:
        if (
            not isinstance(context, dict)
            or any(name not in context for name in common)
            or not set(context).issubset(current | retired)
        ):
            raise InstallControlPlaneError(
                "installed projects.json is invalid"
            )
        item = {name: context[name] for name in common}
        item["atlassian"] = context.get("atlassian")
        if "githubAccount" in context:
            item["githubAccount"] = context["githubAccount"]
        normalized.append(item)
    return (
        json.dumps(
            {"schemaVersion": 1, "contexts": normalized},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    ).encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _activation_checkpoint(phase: str) -> None:
    """Test seam for deterministic interruption at durable phase boundaries."""


def _rollback_checkpoint(phase: str) -> None:
    """Test seam for deterministic rollback concurrency boundaries."""


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_journal(domain: _JournalDomain) -> None:
    if domain.journal_fd < 0:
        return
    for name in os.listdir(domain.journal_fd):
        details = os.stat(
            name,
            dir_fd=domain.journal_fd,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(details.st_mode):
            raise InstallControlPlaneError(
                "installer journal contains an unsafe entry"
            )
        os.unlink(name, dir_fd=domain.journal_fd)
    os.fsync(domain.journal_fd)
    os.close(domain.journal_fd)
    domain.journal_fd = -1
    os.rmdir(domain.name, dir_fd=domain.state_fd)
    os.fsync(domain.state_fd)


def _write_manifest(
    journal_fd: int,
    manifest: dict[str, object],
) -> None:
    _atomic_private_at(
        journal_fd,
        _MANIFEST_NAME,
        (
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode(),
        exclusive=False,
    )


def _load_manifest(journal_fd: int) -> dict[str, object]:
    try:
        manifest = json.loads(
            _private_bytes_at(
                journal_fd,
                _MANIFEST_NAME,
                "installer control-plane manifest",
                MAX_CONFIG_BYTES,
            )
        )
    except (
        UnicodeError,
        json.JSONDecodeError,
        RecursionError,
    ) as error:
        raise InstallControlPlaneError(
            "installer control-plane manifest is invalid"
        ) from error
    if not isinstance(manifest, dict):
        raise InstallControlPlaneError(
            "installer control-plane manifest is invalid"
        )
    has_prior_policy = "priorPolicyPresent" in manifest
    has_policy_digest = "activatedPolicyDigest" in manifest
    has_prior_projects = "priorProjectsPresent" in manifest
    has_projects_digest = "activatedProjectsDigest" in manifest
    policy_fields_valid = (
        not has_prior_policy
        and not has_policy_digest
    ) or (
        has_prior_policy
        and has_policy_digest
        and isinstance(manifest.get("priorPolicyPresent"), bool)
        and isinstance(manifest.get("activatedPolicyDigest"), str)
    )
    projects_fields_valid = (
        not has_prior_projects
        and not has_projects_digest
    ) or (
        has_prior_projects
        and has_projects_digest
        and isinstance(manifest.get("priorProjectsPresent"), bool)
        and isinstance(manifest.get("activatedProjectsDigest"), str)
    )
    if (
        manifest.get("schemaVersion") != 2
        or manifest.get("phase")
        not in _ACTIVE_PHASES | _TERMINAL_PHASES
        or not isinstance(manifest.get("priorModelPresent"), bool)
        or not isinstance(manifest.get("priorBindingPresent"), bool)
        or not policy_fields_valid
        or not projects_fields_valid
        or not isinstance(manifest.get("installedRoot"), str)
        or not isinstance(manifest.get("bootstrapDigests"), dict)
        or not isinstance(manifest.get("activationStates"), list)
        or not isinstance(manifest.get("conflicts"), list)
    ):
        raise InstallControlPlaneError(
            "installer control-plane manifest is invalid"
        )
    return manifest


def _optional_digest(path: Path, label: str, limit: int) -> str | None:
    if not _lexists(path):
        return None
    return _digest(_private_bytes(path, label, limit))


def _stack_state(
    model_path: Path, binding_path: Path
) -> tuple[str | None, str | None]:
    if _lexists(model_path):
        snapshot = load_stack_snapshot(model_path, binding_path)
        return snapshot.stack_digest, snapshot.binding_digest
    return (
        None,
        _optional_digest(
            binding_path, "stack bindings", MAX_BINDING_BYTES
        ),
    )


def _state_document(
    model_digest: str | None, binding_digest: str | None
) -> dict[str, str | None]:
    return {
        "modelDigest": model_digest,
        "bindingDigest": binding_digest,
    }


def stage(
    repository_root: Path, installed_root: Path, candidate_root: Path
) -> None:
    repository_root = Path(repository_root).resolve(strict=True)
    installed_root = Path(installed_root).resolve(strict=True)
    candidate_root = Path(candidate_root).resolve(strict=False)
    _require_private_root(installed_root)
    if candidate_root.exists():
        shutil.rmtree(candidate_root)
    candidate_root.mkdir(mode=0o700, parents=True)
    with control_plane_transaction(installed_root):
        model_path = installed_root / "model-stacks.json"
        binding_path = installed_root / "stack-bindings.json"
        if _lexists(model_path):
            current = load_stack_snapshot(model_path, binding_path)
            bindings = load_stack_bindings(binding_path)
            validate_stack_bindings(
                current.stacks,
                bindings,
                load_accounts(installed_root / "accounts.json"),
            )
            model_payload = (
                json.dumps(
                    serialize_model_stacks(current.stacks),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n"
            ).encode()
        else:
            if _lexists(binding_path):
                raise InstallControlPlaneError(
                    "stack bindings exist without model stacks"
                )
            model_payload = (
                repository_root / "config/model-stacks.json"
            ).read_bytes()
        payloads = {
            name: _candidate_payload(
                repository_root, installed_root, name
            )
            for name in _BOOTSTRAP_FILES
        }
        payloads["model-stacks.json"] = model_payload
        if _lexists(binding_path):
            payloads["stack-bindings.json"] = _private_bytes(
                binding_path, "stack bindings", MAX_BINDING_BYTES
            )
        for name, payload in payloads.items():
            destination = candidate_root / name
            destination.write_bytes(payload)
            destination.chmod(0o600)
    load_control_plane(default_config_paths(candidate_root))
    if (candidate_root / "stack-bindings.json").exists():
        load_stack_bindings(candidate_root / "stack-bindings.json")


def activate(
    candidate_root: Path,
    installed_root: Path,
    snapshot_root: Path,
    install_lock_path: Path,
    install_lock_fd: int,
) -> None:
    candidate_root = Path(candidate_root).resolve(strict=True)
    installed_root = Path(installed_root).resolve(strict=True)
    with _journal_domain(
        snapshot_root,
        install_lock_path,
        install_lock_fd,
        create=True,
    ) as journal:
        _activate_bound(candidate_root, installed_root, journal)


def _activate_bound(
    candidate_root: Path,
    installed_root: Path,
    journal: _JournalDomain,
) -> None:
    _require_private_root(installed_root)
    if journal.journal_fd < 0:
        raise InstallControlPlaneError(
            "installer journal directory is unavailable"
        )
    if _exists_at(journal.journal_fd, _MANIFEST_NAME):
        raise InstallControlPlaneError(
            "unfinished installer control-plane journal exists"
        )
    with control_plane_transaction(installed_root):
        model_path = installed_root / "model-stacks.json"
        binding_path = installed_root / "stack-bindings.json"
        if _lexists(model_path):
            load_stack_snapshot(model_path, binding_path)
        elif _lexists(binding_path):
            raise InstallControlPlaneError(
                "stack bindings exist without model stacks"
            )
        prior_model = _snapshot(
            model_path,
            journal.journal_fd,
            "installed-model-stacks",
            MAX_CONFIG_BYTES,
        )
        prior_binding = _snapshot(
            binding_path,
            journal.journal_fd,
            "installed-stack-bindings",
            MAX_BINDING_BYTES,
        )
        policy_path = installed_root / "controller-policy.md"
        prior_policy = _snapshot(
            policy_path,
            journal.journal_fd,
            "installed-controller-policy",
            MAX_CONFIG_BYTES,
        )
        candidate_policy = (
            candidate_root / "controller-policy.md"
        ).read_bytes()
        projects_path = installed_root / "projects.json"
        prior_projects = _snapshot(
            projects_path,
            journal.journal_fd,
            "installed-projects",
            MAX_CONFIG_BYTES,
        )
        candidate_projects = (
            candidate_root / "projects.json"
        ).read_bytes()
        bootstrap_payloads = {
            name: (candidate_root / name).read_bytes()
            for name in _BOOTSTRAP_FILES
            if name not in {"controller-policy.md", "projects.json"}
            and not _lexists(installed_root / name)
        }
        initial_model = (
            (candidate_root / "model-stacks.json").read_bytes()
            if prior_model is None
            else None
        )
        prior_state = _state_document(
            None if prior_model is None else _digest(prior_model),
            None if prior_binding is None else _digest(prior_binding),
        )
        activation_states: list[dict[str, str | None]] = []
        if initial_model is not None:
            activation_states.append(
                _state_document(
                    _digest(initial_model),
                    prior_state["bindingDigest"],
                )
            )
        manifest: dict[str, object] = {
            "schemaVersion": 2,
            "phase": "prepared",
            "installedRoot": str(installed_root),
            "priorModelPresent": prior_model is not None,
            "priorBindingPresent": prior_binding is not None,
            "priorPolicyPresent": prior_policy is not None,
            "activatedPolicyDigest": _digest(candidate_policy),
            "priorProjectsPresent": prior_projects is not None,
            "activatedProjectsDigest": _digest(candidate_projects),
            "priorState": prior_state,
            "bootstrapDigests": {
                name: _digest(payload)
                for name, payload in bootstrap_payloads.items()
            },
            "activationStates": activation_states,
            "conflicts": [],
        }
        _write_manifest(journal.journal_fd, manifest)
        _activation_checkpoint("prepared")

        for name, payload in bootstrap_payloads.items():
            _atomic_private(
                installed_root / name,
                payload,
                exclusive=True,
            )
            _activation_checkpoint(f"bootstrap:{name}")
        _atomic_private(
            policy_path,
            candidate_policy,
            exclusive=False,
        )
        _activation_checkpoint("controller-policy-installed")
        _atomic_private(
            projects_path,
            candidate_projects,
            exclusive=False,
        )
        _activation_checkpoint("projects-installed")
        if initial_model is not None:
            _atomic_private(
                model_path,
                initial_model,
                exclusive=True,
            )
            _activation_checkpoint("bootstrap:model-stacks.json")

        current = load_stack_snapshot(model_path, binding_path)
        bindings = load_stack_bindings(binding_path)
        validate_stack_bindings(
            current.stacks,
            bindings,
            load_accounts(installed_root / "accounts.json"),
        )
        planned = planned_stack_digests(
            current, current.stacks, bindings
        )
        planned_state = _state_document(*planned)
        if planned_state not in activation_states:
            activation_states.append(planned_state)
        manifest["phase"] = "saving"
        _write_manifest(journal.journal_fd, manifest)
        _activation_checkpoint("saving")

        save_stack(current, current.stacks, bindings)
        _activation_checkpoint("stack-saved")
        saved = load_stack_snapshot(model_path, binding_path)
        if (saved.stack_digest, saved.binding_digest) != planned:
            raise InstallControlPlaneError(
                "installed stack state differs from the activation plan"
            )
        manifest["phase"] = "committed"
        _write_manifest(journal.journal_fd, manifest)
        _activation_checkpoint("committed")
        load_control_plane(default_config_paths(installed_root))


def rollback(
    installed_root: Path,
    snapshot_root: Path,
    install_lock_path: Path,
    install_lock_fd: int,
) -> None:
    installed_root = Path(installed_root).resolve(strict=True)
    with _journal_domain(
        snapshot_root,
        install_lock_path,
        install_lock_fd,
        create=False,
    ) as journal:
        _rollback_bound(installed_root, journal)


def _rollback_bound(
    installed_root: Path,
    journal: _JournalDomain,
) -> None:
    if journal.journal_fd < 0:
        return
    if not _exists_at(journal.journal_fd, _MANIFEST_NAME):
        _remove_journal(journal)
        return
    manifest = _load_manifest(journal.journal_fd)
    if manifest["installedRoot"] != str(installed_root):
        raise InstallControlPlaneError(
            "installer journal belongs to a different installed "
            "configuration root"
        )
    with control_plane_transaction(installed_root):
        if manifest["phase"] in _TERMINAL_PHASES:
            _remove_journal(journal)
            return
        original_model = (
            _private_bytes_at(
                journal.journal_fd,
                "installed-model-stacks.data",
                "installed model-stack snapshot",
                MAX_CONFIG_BYTES,
            )
            if manifest["priorModelPresent"]
            else None
        )
        original_binding = (
            _private_bytes_at(
                journal.journal_fd,
                "installed-stack-bindings.data",
                "installed stack-binding snapshot",
                MAX_BINDING_BYTES,
            )
            if manifest["priorBindingPresent"]
            else None
        )
        policy_managed = "priorPolicyPresent" in manifest
        original_policy = (
            _private_bytes_at(
                journal.journal_fd,
                "installed-controller-policy.data",
                "installed controller-policy snapshot",
                MAX_CONFIG_BYTES,
            )
            if policy_managed and manifest["priorPolicyPresent"]
            else None
        )
        projects_managed = "priorProjectsPresent" in manifest
        original_projects = (
            _private_bytes_at(
                journal.journal_fd,
                "installed-projects.data",
                "installed projects snapshot",
                MAX_CONFIG_BYTES,
            )
            if projects_managed and manifest["priorProjectsPresent"]
            else None
        )
        prior_state = manifest.get("priorState")
        if not isinstance(prior_state, dict):
            raise InstallControlPlaneError(
                "installer control-plane manifest is invalid"
            )
        conflicts: list[str] = []
        model_path = installed_root / "model-stacks.json"
        binding_path = installed_root / "stack-bindings.json"
        try:
            current_state = _state_document(
                *_stack_state(model_path, binding_path)
            )
            if current_state != prior_state:
                if current_state not in manifest["activationStates"]:
                    conflicts.append(
                        "model-stacks.json/stack-bindings.json"
                    )
                elif current_state["modelDigest"] is None:
                    conflicts.append(
                        "model-stacks.json/stack-bindings.json"
                    )
                else:
                    restore_stack_files(
                        model_path,
                        binding_path,
                        expected_stack_digest=current_state[
                            "modelDigest"
                        ],
                        expected_binding_digest=current_state[
                            "bindingDigest"
                        ],
                        original_model=original_model,
                        original_binding=original_binding,
                    )
        except StackStoreError:
            conflicts.append("model-stacks.json/stack-bindings.json")

        if policy_managed:
            policy_path = installed_root / "controller-policy.md"
            current_policy_digest = _optional_digest(
                policy_path,
                "controller-policy.md",
                MAX_CONFIG_BYTES,
            )
            prior_policy_digest = (
                None if original_policy is None else _digest(original_policy)
            )
            if current_policy_digest == manifest["activatedPolicyDigest"]:
                if original_policy is None:
                    policy_path.unlink()
                    _fsync_directory(installed_root)
                else:
                    _atomic_private(
                        policy_path,
                        original_policy,
                        exclusive=False,
                    )
            elif current_policy_digest != prior_policy_digest:
                conflicts.append("controller-policy.md")

        if projects_managed:
            projects_path = installed_root / "projects.json"
            current_projects_digest = _optional_digest(
                projects_path,
                "projects.json",
                MAX_CONFIG_BYTES,
            )
            prior_projects_digest = (
                None
                if original_projects is None
                else _digest(original_projects)
            )
            if current_projects_digest == manifest["activatedProjectsDigest"]:
                if original_projects is None:
                    projects_path.unlink()
                    _fsync_directory(installed_root)
                else:
                    _atomic_private(
                        projects_path,
                        original_projects,
                        exclusive=False,
                    )
            elif current_projects_digest != prior_projects_digest:
                conflicts.append("projects.json")

        bootstrap_digests = manifest["bootstrapDigests"]
        if not isinstance(bootstrap_digests, dict):
            raise InstallControlPlaneError(
                "installer control-plane manifest is invalid"
            )
        bootstrap_unlinked = False
        for name in reversed(_BOOTSTRAP_FILES):
            if name not in bootstrap_digests:
                continue
            expected = bootstrap_digests[name]
            if not isinstance(expected, str):
                raise InstallControlPlaneError(
                    "installer control-plane manifest is invalid"
                )
            path = installed_root / name
            try:
                if name == "accounts.json":
                    guard = account_registry_lock(path)
                elif name == "plugins.json":
                    guard = plugin_registry_lock(path)
                else:
                    guard = nullcontext()
                _rollback_checkpoint(f"before-lock:{name}")
                with guard:
                    observed = _optional_digest(
                        path, name, MAX_CONFIG_BYTES
                    )
                    if observed is None:
                        continue
                    if observed != expected:
                        conflicts.append(name)
                        continue
                    path.unlink()
                    bootstrap_unlinked = True
            except (
                AccountError,
                InstallControlPlaneError,
                PluginRegistryError,
            ):
                conflicts.append(name)
                continue

        if bootstrap_unlinked:
            _fsync_directory(installed_root)
        manifest["conflicts"] = conflicts
        manifest["phase"] = (
            "rollbackConflict" if conflicts else "rolledBack"
        )
        _write_manifest(journal.journal_fd, manifest)
        if conflicts:
            if len(conflicts) == 1:
                detail = f"{conflicts[0]} changed after installer activation"
            else:
                detail = (
                    "control-plane files changed after installer activation: "
                    + ", ".join(conflicts)
                )
            raise InstallControlPlaneError(detail)
        _remove_journal(journal)


def recover(
    installed_root: Path,
    snapshot_root: Path,
    install_lock_path: Path,
    install_lock_fd: int,
) -> None:
    """Recover an unfinished journal while the installer lock is owned."""
    rollback(
        installed_root,
        snapshot_root,
        install_lock_path,
        install_lock_fd,
    )


def finalize(
    snapshot_root: Path,
    install_lock_path: Path,
    install_lock_fd: int,
) -> None:
    """Remove a committed journal after the outer install is durable."""
    with _journal_domain(
        snapshot_root,
        install_lock_path,
        install_lock_fd,
        create=False,
    ) as journal:
        if journal.journal_fd < 0:
            return
        manifest = _load_manifest(journal.journal_fd)
        if manifest["phase"] != "committed":
            raise InstallControlPlaneError(
                "installer control-plane journal is not committed"
            )
        manifest["phase"] = "finalized"
        _write_manifest(journal.journal_fd, manifest)
        _remove_journal(journal)
