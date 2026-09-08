"""Resolve user-owned dotfile links without replacing their symlinks."""

from __future__ import annotations

import os
import stat
from collections import deque
from pathlib import Path


class UnsafeProfile(ValueError):
    """A shell profile cannot be followed safely."""


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_mode, value.st_uid


class ProfilePath:
    """A profile target and the link/directory identities used to reach it."""

    def __init__(self, requested: Path, *, user_home: Path | None = None):
        self.path = Path(requested)
        self._guards: dict[Path, tuple[tuple[int, ...], str | None]] = {}
        try:
            observed = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISLNK(observed.st_mode):
            return
        try:
            self._remember(self.path, observed)
            root = (Path.home() if user_home is None else user_home).resolve(
                strict=True
            )
            self._directory(root)
            requested = self.path.parent.resolve(strict=True) / self.path.name
            pending = deque(requested.relative_to(root).parts)
            cursor = root
            links = 0
            resolved_file = False
            while pending:
                candidate = cursor / pending.popleft()
                value = candidate.lstat()
                if stat.S_ISLNK(value.st_mode):
                    links += 1
                    if links > 40:
                        raise UnsafeProfile(
                            "profile symlink chain is cyclic or too long"
                        )
                    destination = self._remember(candidate, value)
                    target = Path(destination)
                    if not target.is_absolute():
                        target = candidate.parent / target
                    target = Path(os.path.normpath(target))
                    pending = deque((*target.relative_to(root).parts, *pending))
                    cursor = root
                elif pending:
                    self._directory(candidate)
                    cursor = candidate
                else:
                    if (
                        not stat.S_ISREG(value.st_mode)
                        or value.st_uid != os.getuid()
                        or stat.S_IMODE(value.st_mode) & 0o022
                    ):
                        raise UnsafeProfile(
                            "profile target must be a user-owned, non-shared regular file"
                        )
                    self.path = candidate
                    resolved_file = True
            if not resolved_file:
                raise UnsafeProfile("profile symlink must point to a regular file")
            if not self.unchanged():
                raise UnsafeProfile("profile symlink changed while resolving")
        except (OSError, RuntimeError, ValueError) as error:
            if isinstance(error, UnsafeProfile):
                raise
            raise UnsafeProfile(
                "profile links must resolve to an existing file inside the user's home"
            ) from error

    def _remember(self, path: Path, value: os.stat_result) -> str:
        if value.st_uid != os.getuid():
            raise UnsafeProfile("profile symlink is owned by another user")
        destination = os.readlink(path)
        identity = (
            *_identity(value),
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        previous = self._guards.setdefault(path, (identity, destination))
        if previous != (identity, destination):
            raise UnsafeProfile("profile symlink changed while resolving")
        return destination

    def _directory(self, path: Path) -> None:
        value = path.lstat()
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != os.getuid()
            or stat.S_IMODE(value.st_mode) & 0o022
        ):
            raise UnsafeProfile(
                "profile target directory must be user-owned and not shared-writable"
            )
        previous = self._guards.setdefault(path, (_identity(value), None))
        if previous != (_identity(value), None):
            raise UnsafeProfile("profile target directory changed while resolving")

    def unchanged(self) -> bool:
        try:
            for path, (identity, destination) in self._guards.items():
                value = path.lstat()
                current = _identity(value)
                if destination is not None:
                    current = (
                        *current,
                        value.st_size,
                        value.st_mtime_ns,
                        value.st_ctime_ns,
                    )
                    if os.readlink(path) != destination:
                        return False
                if current != identity:
                    return False
        except OSError:
            return False
        return True
