#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from integrations.common.runtime_bundle import (
    RuntimeBundleError,
    activate,
    build,
    prune,
    rollback_activation,
    rollback_attempt,
    validate,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RuntimeBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.home = self.root / "home"
        self.stage = self.root / "stage"

    def copy_source(self, name: str) -> Path:
        source = self.root / name
        shutil.copytree(
            REPOSITORY_ROOT,
            source,
            symlinks=True,
            ignore=shutil.ignore_patterns(
                ".git",
                ".worktrees",
                "__pycache__",
                "*.pyc",
            ),
        )
        return source

    def git(self, source: Path, *arguments: str) -> str:
        return subprocess.run(
            ("git", "-C", str(source), *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()

    def git_source(self) -> tuple[Path, str]:
        source = self.copy_source("git-source")
        self.git(source, "init", "--quiet")
        self.git(source, "config", "user.name", "Orichum Tests")
        self.git(source, "config", "user.email", "tests@orichum.invalid")
        self.git(source, "add", "--all")
        self.git(source, "commit", "--quiet", "-m", "baseline")
        return source, self.git(source, "rev-parse", "HEAD")

    @staticmethod
    def build_identity(release: Path) -> dict[str, object]:
        return json.loads(
            (release / "build-identity.json").read_text(encoding="utf-8")
        )

    def test_build_copies_only_the_runtime_allowlist(self) -> None:
        release = build(REPOSITORY_ROOT, self.stage)

        self.assertTrue((release / "bin" / "orichum").is_file())
        self.assertTrue(
            (release / "integrations" / "common" / "orichum_cli.py").is_file()
        )
        self.assertTrue(
            (release / "controller" / "plugin" / "hooks" / "hooks.json").is_file()
        )
        self.assertTrue((release / "controller" / "settings.json").is_file())
        self.assertTrue((release / "config" / "runtime.json").is_file())
        self.assertTrue((release / "build-identity.json").is_file())
        self.assertTrue((release / "runtime-manifest.json").is_file())
        self.assertFalse((release / "README.md").exists())
        self.assertFalse((release / "docs").exists())
        self.assertFalse((release / "tests").exists())
        self.assertFalse((release / ".git").exists())
        self.assertFalse(any(release.rglob("__pycache__")))
        self.assertFalse(any(release.rglob("*.pyc")))
        validate(release)

    def test_build_records_clean_git_source_identity(self) -> None:
        source, commit = self.git_source()

        identity = self.build_identity(build(source, self.stage))

        self.assertEqual(
            identity,
            {
                "schemaVersion": 1,
                "version": "0.1.0-rc.12",
                "sourceKind": "git",
                "sourceCommit": commit,
                "dirty": False,
                "exactTag": False,
            },
        )

    def test_build_records_exact_matching_release_tag(self) -> None:
        source, commit = self.git_source()
        self.git(source, "tag", "v0.1.0-rc.12")

        identity = self.build_identity(build(source, self.stage))

        self.assertEqual(identity["sourceCommit"], commit)
        self.assertTrue(identity["exactTag"])
        self.assertFalse(identity["dirty"])

    def test_build_marks_declared_payload_changes_dirty(self) -> None:
        source, _ = self.git_source()
        runtime_config = source / "config" / "runtime.json"
        runtime_config.write_bytes(runtime_config.read_bytes() + b"\n")

        identity = self.build_identity(build(source, self.stage))

        self.assertTrue(identity["dirty"])
        self.assertFalse(identity["exactTag"])

    def test_build_marks_deleted_payload_file_dirty(self) -> None:
        source, _ = self.git_source()
        (source / "controller" / "plugin" / "agents" /
         "repository-explorer.md").unlink()

        identity = self.build_identity(build(source, self.stage))

        self.assertTrue(identity["dirty"])

    def test_build_ignores_unrelated_dirty_files(self) -> None:
        source, _ = self.git_source()
        readme = source / "README.md"
        readme.write_bytes(readme.read_bytes() + b"\nlocal note\n")

        identity = self.build_identity(build(source, self.stage))

        self.assertFalse(identity["dirty"])

    def test_build_uses_source_identity_without_git_metadata(self) -> None:
        source = self.copy_source("source-without-git")

        identity = self.build_identity(build(source, self.stage))

        self.assertEqual(
            identity,
            {
                "schemaVersion": 1,
                "version": "0.1.0-rc.12",
                "sourceKind": "source",
                "sourceCommit": None,
                "dirty": False,
                "exactTag": False,
            },
        )

    def test_build_is_content_addressed_and_reproducible(self) -> None:
        first = build(REPOSITORY_ROOT, self.stage / "first")
        second = build(REPOSITORY_ROOT, self.stage / "second")

        self.assertEqual(first.name, second.name)
        first_manifest = json.loads(
            (first / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        second_manifest = json.loads(
            (second / "runtime-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(first_manifest, second_manifest)
        self.assertEqual(first_manifest["digest"], first.name)

    def test_installed_runtime_rebuild_preserves_identity(self) -> None:
        source, _ = self.git_source()
        staged = build(source, self.stage / "initial")
        installed, _ = activate(staged, self.home)

        rebuilt = build(installed, self.stage / "rebuild")

        self.assertEqual(rebuilt.name, installed.name)
        self.assertEqual(
            self.build_identity(rebuilt),
            self.build_identity(installed),
        )
        reconciled, _ = activate(rebuilt, self.home)
        prune(self.home, (reconciled,))
        self.assertEqual(reconciled, installed)
        self.assertTrue(installed.is_dir())

    def test_validate_rejects_modified_release(self) -> None:
        release = build(REPOSITORY_ROOT, self.stage)
        launcher = release / "bin" / "orichum"
        launcher.write_bytes(launcher.read_bytes() + b"\n")

        with self.assertRaisesRegex(
            RuntimeBundleError, "runtime file (?:size|digest) mismatch"
        ):
            validate(release)

    def test_activate_installs_real_release_and_switches_pointer(self) -> None:
        staged = build(REPOSITORY_ROOT, self.stage)

        release, previous = activate(staged, self.home)

        self.assertIsNone(previous)
        self.assertTrue(release.is_dir())
        self.assertFalse(release.is_symlink())
        current = self.home / "runtime" / "current"
        self.assertTrue(current.is_symlink())
        self.assertEqual(
            current.resolve(strict=True),
            release.resolve(strict=True),
        )
        validate(release)

    def test_activation_can_be_rolled_back_without_runtime_debris(self) -> None:
        staged = build(REPOSITORY_ROOT, self.stage)
        release, previous = activate(staged, self.home)

        rollback_activation(self.home, release, previous)

        self.assertFalse((self.home / "runtime").exists())

    def test_failed_activation_attempt_without_a_release_is_reversible(self) -> None:
        predicted = self.home / "runtime" / "releases" / ("0" * 64)

        rollback_attempt(self.home, predicted, None)

        self.assertFalse((self.home / "runtime").exists())

    def test_build_rejects_symlinked_source_payload(self) -> None:
        source = self.root / "source"
        shutil.copytree(
            REPOSITORY_ROOT,
            source,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        target = source / "VERSION"
        target.unlink()
        target.symlink_to(REPOSITORY_ROOT / "VERSION")

        with self.assertRaisesRegex(RuntimeBundleError, "symlink"):
            build(source, self.stage)


if __name__ == "__main__":
    unittest.main()
