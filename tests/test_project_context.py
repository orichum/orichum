#!/usr/bin/env python3
import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from integrations.common import project_context
from integrations.common.project_context import (
    ContextError,
    assign_stack_to_context,
    configure_normal_scope,
    control_plane_transaction,
    load_config,
    resolve_context,
)


def jira(name: str) -> dict[str, str]:
    return {
        "url": f"https://{name}.atlassian.net",
        "username": f"{name}@example.com",
        "apiToken": f"{name}-token",
    }


class ProjectContextTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.xebia = self.root / "xebia"
        self.complion = self.root / "complion"
        self.xebia_repo = self.xebia / "repo"
        self.complion_repo = self.complion / "nested" / "repo"
        for directory in (
            self.xebia_repo,
            self.complion_repo,
            self.root / "elsewhere",
            self.root / "xebia-old",
        ):
            directory.mkdir(parents=True)
        self.config = {
            "contexts": [
                {
                    "root": str(self.xebia),
                    "atlassian": jira("xebia"),
                },
                {
                    "root": str(self.complion),
                    "atlassian": jira("realtime"),
                },
            ],
        }
        self.config_path = self.write_config(self.config)

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_config(self, payload):
        path = self.root / f"config-{len(list(self.root.glob('config-*.json')))}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def resolve(self, launch_dir, payload=None):
        config_path = self.config_path if payload is None else self.write_config(payload)
        return resolve_context(load_config(config_path, home=self.root), launch_dir)

    def init_git(self, path):
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_longest_component_boundary_and_unmapped(self):
        xebia = self.resolve(self.xebia / "repo")["route"]
        self.assertEqual(xebia["id"], "xebia")
        self.assertEqual(xebia["contextRootReal"], str(self.xebia))
        self.assertIs(xebia["atlassianConfigured"], True)

        complion = self.resolve(self.complion / "nested" / "repo")["route"]
        self.assertEqual(complion["id"], "complion")
        self.assertEqual(complion["contextRootReal"], str(self.complion))
        self.assertIs(complion["atlassianConfigured"], True)
        self.assertIsNone(self.resolve(self.root / "xebia-old")["route"])
        self.assertIsNone(self.resolve(self.root / "elsewhere")["route"])



    def test_symlink_uses_physical_target(self):
        link = self.root / "linked-repo"
        link.symlink_to(self.xebia / "repo", target_is_directory=True)
        result = self.resolve(link)
        self.assertEqual(result["launchDirReal"], str((self.xebia / "repo").resolve()))
        self.assertEqual(result["route"]["id"], "xebia")


    def test_duplicate_canonical_symlink_roots_fail_closed(self):
        linked_root = self.root / "linked-xebia"
        linked_root.symlink_to(self.xebia, target_is_directory=True)
        bad = json.loads(json.dumps(self.config))
        bad["contexts"][1]["root"] = str(linked_root)
        with self.assertRaises(ContextError):
            load_config(self.write_config(bad), home=self.root)

    def test_canonical_home_or_filesystem_root_is_rejected(self):
        for target in (self.root, Path("/")):
            with self.subTest(target=target):
                linked_root = self.root / f"unsafe-{len(list(self.root.glob('unsafe-*')))}"
                linked_root.symlink_to(target, target_is_directory=True)
                bad = json.loads(json.dumps(self.config))
                bad["contexts"] = [bad["contexts"][0]]
                bad["contexts"][0]["root"] = str(linked_root)
                with self.assertRaises(ContextError):
                    load_config(self.write_config(bad), home=self.root)



    def test_git_root_is_independent_physical_and_optional(self):
        self.init_git(self.xebia_repo)
        nested = self.xebia_repo / "nested" / "deeper"
        nested.mkdir(parents=True)
        result = self.resolve(nested)
        self.assertEqual(result["repoRootReal"], str(self.xebia_repo.resolve(strict=True)))

        self.assertIsNone(self.resolve(self.xebia)["repoRootReal"])

        unmapped_repo = self.root / "unmapped-repo"
        unmapped_nested = unmapped_repo / "nested"
        unmapped_nested.mkdir(parents=True)
        self.init_git(unmapped_repo)
        unmapped = self.resolve(unmapped_nested)
        self.assertIsNone(unmapped["route"])
        self.assertEqual(unmapped["repoRootReal"], str(unmapped_repo.resolve(strict=True)))



    def test_legacy_context_without_model_stack_inherits_default(self):
        config = load_config(self.config_path, home=self.root)
        self.assertIsNone(config["contexts"][0]["modelStack"])

    def test_resolved_route_carries_explicit_model_stack(self):
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        document["contexts"][0]["modelStack"] = "xebia"
        self.config_path.write_text(json.dumps(document), encoding="utf-8")

        route = resolve_context(
            load_config(self.config_path, home=self.root), self.xebia_repo
        )["route"]

        self.assertEqual(route["modelStack"], "xebia")



    def test_launch_directory_must_resolve_strictly(self):
        with self.assertRaises(FileNotFoundError):
            self.resolve(self.root / "missing-launch")

    def test_cli_writes_canonical_atomic_private_output(self):
        output = self.root / "context-output.json"
        output.write_text("old contents", encoding="utf-8")
        os.chmod(output, 0o644)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "--config",
                str(self.config_path),
                "--launch-dir",
                str(self.complion_repo),
                "--output",
                str(output),
            ],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        payload = json.loads(output.read_text(encoding="utf-8"))
        expected = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        self.assertEqual(output.read_text(encoding="utf-8"), expected)
        self.assertEqual(list(self.root.glob(f".{output.name}.*")), [])


class StackContextAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.home = self.root / "home"
        self.workspace = self.root / "work" / "workspace"
        self.other = self.root / "work" / "other"
        self.nested = self.workspace / "nested"
        for directory in (
            self.home,
            self.nested,
            self.other,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "projects.json"
        self.document = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": str(self.workspace),
                    "atlassian": jira("dev"),
                    "modelStack": None,
                    "accountPools": ["shared"],
                },
                {
                    "root": str(self.other),
                    "atlassian": jira("other"),
                    "modelStack": "balanced",
                    "accountPools": ["shared"],
                },
            ],
        }
        self.write_document(self.document)
        self.config_path.chmod(0o640)

    def write_document(self, document):
        self.config_path.write_text(
            json.dumps(document, indent=2) + "\n", encoding="utf-8"
        )

    def assign(self, launch_dir, stack, known=("balanced", "heavy")):
        with mock.patch.object(Path, "home", return_value=self.home):
            return assign_stack_to_context(
                self.config_path, launch_dir, stack, known
            )

    def test_assigns_only_the_physically_matched_context_atomically(self):
        launch_link = self.root / "linked-launch"
        launch_link.symlink_to(self.nested, target_is_directory=True)

        matched = self.assign(launch_link, "heavy")

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(matched, self.workspace.resolve())
        self.assertEqual(saved["contexts"][0]["modelStack"], "heavy")
        self.assertEqual(saved["contexts"][1]["modelStack"], "balanced")
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)
        self.assertEqual(
            list(self.root.glob(f".{self.config_path.name}.*")), []
        )

    def test_normal_scope_configuration_preserves_project_contexts(self):
        document = {
            "schemaVersion": 2,
            "normal": None,
            "contexts": self.document["contexts"],
        }
        self.write_document(document)

        configure_normal_scope(
            self.config_path,
            model_stack="heavy",
            account_pools=("shared",),
            known_stacks=("balanced", "heavy"),
            known_pools=("shared",),
        )

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            saved["normal"],
            {
                "modelStack": "heavy",
                "accountPools": ["shared"],
            },
        )
        self.assertEqual(saved["contexts"], self.document["contexts"])
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o640)

    def test_assignment_uses_shared_control_plane_transaction(self):
        attempted = threading.Event()
        completed = threading.Event()
        failures = []

        def assign_in_thread():
            attempted.set()
            try:
                self.assign(self.nested, "heavy")
            except BaseException as error:
                failures.append(error)
            finally:
                completed.set()

        with control_plane_transaction(self.config_path.parent):
            worker = threading.Thread(target=assign_in_thread)
            worker.start()
            self.assertTrue(attempted.wait(2))
            self.assertFalse(completed.is_set())

        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["contexts"][0]["modelStack"], "heavy")

    def test_unknown_stack_and_unmatched_directory_do_not_mutate(self):
        original = self.config_path.read_bytes()

        with self.assertRaisesRegex(ContextError, "unknown"):
            self.assign(self.nested, "missing")
        self.assertEqual(self.config_path.read_bytes(), original)

        unmatched = self.root / "unmatched"
        unmatched.mkdir()
        with self.assertRaisesRegex(ContextError, "no project context"):
            self.assign(unmatched, "heavy")
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_assignment_requires_parent_directory_fsync(self):
        original = self.config_path.read_bytes()
        calls = 0
        real_fsync = project_context._fsync_context_directory

        def fail_target_fsync(parent):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected context directory fsync")
            real_fsync(parent)

        with mock.patch.object(
            project_context,
            "_fsync_context_directory",
            side_effect=fail_target_fsync,
        ), self.assertRaisesRegex(ContextError, "durability"):
            self.assign(self.nested, "heavy")

        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(
            list(self.root.glob(f".{self.config_path.name}.transaction*")),
            [],
        )

    def test_assignment_fsyncs_backup_before_publishing_pending_marker(self):
        original = self.config_path.read_bytes()
        marker, backup = project_context._context_recovery_paths(
            self.config_path
        )
        calls = 0
        marker_at_backup_fsync = None
        backup_at_backup_fsync = None

        def fail_backup_fsync(parent):
            nonlocal calls, marker_at_backup_fsync, backup_at_backup_fsync
            calls += 1
            if calls == 1:
                backup_at_backup_fsync = backup.read_bytes()
                marker_at_backup_fsync = marker.exists()
                raise OSError("injected backup directory fsync")
            raise AssertionError(
                "unexpected directory fsync after backup failure"
            )

        with mock.patch.object(
            project_context,
            "_fsync_context_directory",
            side_effect=fail_backup_fsync,
        ), mock.patch.object(
            project_context,
            "_cleanup_context_recovery",
            return_value=None,
        ), mock.patch.object(
            project_context,
            "_rollback_context_transaction",
            return_value=None,
        ), self.assertRaisesRegex(ContextError, "durability"):
            self.assign(self.nested, "heavy")

        self.assertEqual(backup_at_backup_fsync, original)
        self.assertFalse(marker_at_backup_fsync)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), original)
        self.assertFalse(marker.exists())

        with mock.patch.object(Path, "home", return_value=self.home):
            with project_context._context_lock(self.config_path):
                recovered = project_context._read_context_document(
                    self.config_path, self.home
                )

        self.assertIsNone(recovered["contexts"][0]["modelStack"])
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(
            list(self.root.glob(f".{self.config_path.name}.transaction*")),
            [],
        )

    def test_pending_marker_fsync_follows_durable_backup_and_recovers(self):
        original = self.config_path.read_bytes()
        marker, backup = project_context._context_recovery_paths(
            self.config_path
        )
        real_fsync = project_context._fsync_context_directory
        calls = 0
        backup_synced = False
        marker_at_backup_fsync = None
        backup_at_pending_fsync = None
        canonical_at_pending_fsync = None

        def fail_pending_fsync(parent):
            nonlocal calls, backup_synced, marker_at_backup_fsync
            nonlocal backup_at_pending_fsync
            nonlocal canonical_at_pending_fsync
            calls += 1
            if calls == 1:
                marker_at_backup_fsync = marker.exists()
                real_fsync(parent)
                backup_synced = True
                return
            if calls == 2:
                backup_at_pending_fsync = backup.read_bytes()
                canonical_at_pending_fsync = self.config_path.read_bytes()
                raise OSError("injected pending directory fsync")
            raise AssertionError(
                "unexpected directory fsync after pending failure"
            )

        with mock.patch.object(
            project_context,
            "_fsync_context_directory",
            side_effect=fail_pending_fsync,
        ), mock.patch.object(
            project_context,
            "_rollback_context_transaction",
            side_effect=OSError("injected rollback interruption"),
        ), self.assertRaisesRegex(ContextError, "rollback"):
            self.assign(self.nested, "heavy")

        self.assertFalse(marker_at_backup_fsync)
        self.assertTrue(backup_synced)
        self.assertEqual(backup_at_pending_fsync, original)
        self.assertEqual(canonical_at_pending_fsync, original)
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["state"],
            "pending",
        )

        with mock.patch.object(Path, "home", return_value=self.home):
            with project_context._context_lock(self.config_path):
                recovered = project_context._read_context_document(
                    self.config_path, self.home
                )

        self.assertIsNone(recovered["contexts"][0]["modelStack"])
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(
            list(self.root.glob(f".{self.config_path.name}.transaction*")),
            [],
        )

    def test_assignment_rollback_double_fault_recovers_on_next_locked_read(self):
        original = self.config_path.read_bytes()
        real_fsync = project_context._fsync_context_directory
        real_replace = os.replace
        fsync_calls = 0
        config_replacements = 0

        def fail_target_fsync(parent):
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 3:
                raise OSError("injected target directory fsync")
            real_fsync(parent)

        def fail_rollback_replace(source, target):
            nonlocal config_replacements
            if Path(target) == self.config_path:
                config_replacements += 1
                if config_replacements == 2:
                    raise OSError("injected context rollback failure")
            real_replace(source, target)

        with mock.patch.object(
            project_context,
            "_fsync_context_directory",
            side_effect=fail_target_fsync,
        ), mock.patch(
            "integrations.common.project_context.os.replace",
            side_effect=fail_rollback_replace,
        ), self.assertRaisesRegex(ContextError, "rollback"):
            self.assign(self.nested, "heavy")

        marker = (
            self.root / f".{self.config_path.name}.transaction.json"
        )
        backup = (
            self.root / f".{self.config_path.name}.transaction.original"
        )
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["state"],
            "pending",
        )
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

        with mock.patch.object(Path, "home", return_value=self.home):
            with project_context._context_lock(self.config_path):
                recovered = project_context._read_context_document(
                    self.config_path, self.home
                )

        self.assertIsNone(recovered["contexts"][0]["modelStack"])
        self.assertEqual(self.config_path.read_bytes(), original)
        self.assertEqual(
            list(self.root.glob(f".{self.config_path.name}.transaction*")),
            [],
        )

    def test_committed_cleanup_interrupt_preserves_new_assignment(self):
        with mock.patch.object(
            project_context,
            "_remove_context_recovery_artifacts",
            side_effect=KeyboardInterrupt(
                "injected committed cleanup interrupt"
            ),
        ):
            matched = self.assign(self.nested, "heavy")

        self.assertEqual(matched, self.workspace.resolve())
        marker = (
            self.root / f".{self.config_path.name}.transaction.json"
        )
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["state"],
            "committed",
        )
        with mock.patch.object(Path, "home", return_value=self.home):
            with project_context._context_lock(self.config_path):
                recovered = project_context._read_context_document(
                    self.config_path, self.home
                )

        self.assertEqual(recovered["contexts"][0]["modelStack"], "heavy")
        self.assertEqual(
            list(self.root.glob(f".{self.config_path.name}.transaction*")),
            [],
        )


class ContextCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.config_path = self.root / "project-context.json"
        self.config_path.write_text('{\n  "contexts": []\n}\n', encoding="utf-8")
        os.chmod(self.config_path, 0o640)
        self.routing_path = self.root / "model-routing.json"
        self.routing_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "defaultStack": "balanced",
                    "stacks": {
                        "balanced": {
                            "controller": "controller-balanced",
                            "agents": {
                                "repository-explorer": ["explorer-balanced"],
                                "repository-verifier": ["verifier-balanced"],
                                "correctness-critic": ["critic-balanced"],
                                "architecture-advisor": ["advisor-balanced"],
                                "implementation-worker": ["worker-balanced"],
                            },
                        },
                        "xebia": {
                            "controller": "controller-xebia",
                            "agents": {
                                "repository-explorer": ["explorer-xebia"],
                                "repository-verifier": ["verifier-xebia"],
                                "correctness-critic": ["critic-xebia"],
                                "architecture-advisor": ["advisor-xebia"],
                                "implementation-worker": ["worker-xebia"],
                            },
                        },
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.providers_path = self.root / "providers.json"
        self.providers_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "providers": {},
                    "accountPools": {
                        "work": {"providers": []},
                        "shared": {"providers": []},
                    },
                    "fallbackRoutes": {},
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.focused_routing_path = self.root / "model-stacks.json"
        focused_routing = json.loads(self.routing_path.read_text(encoding="utf-8"))
        focused_routing["models"] = {}
        self.focused_routing_path.write_text(
            json.dumps(focused_routing) + "\n", encoding="utf-8"
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def run_context(self, *arguments, input_text=None, environment=None):
        command_environment = os.environ.copy()
        command_environment.update(
            {
                "HOME": str(self.root),
            }
        )
        if environment is not None:
            command_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "context",
                "--config",
                str(self.config_path),
                "--routing-config",
                str(self.routing_path),
                *arguments,
            ],
            cwd=REPO_ROOT,
            env=command_environment,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
        )

    def run_focused_context(self, *arguments, input_text=None, environment=None):
        command_environment = os.environ.copy()
        command_environment.update(
            {
                "HOME": str(self.root),
            }
        )
        if environment is not None:
            command_environment.update(environment)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "context",
                "--config",
                str(self.config_path),
                "--routing-config",
                str(self.focused_routing_path),
                "--providers-config",
                str(self.providers_path),
                *arguments,
            ],
            cwd=REPO_ROOT,
            env=command_environment,
            input=input_text,
            check=False,
            capture_output=True,
            text=True,
        )

    def load_contexts(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))["contexts"]

    def write_contexts(self, contexts):
        self.config_path.write_text(
            json.dumps({"contexts": contexts}, indent=2) + "\n",
            encoding="utf-8",
        )

    def init_git(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "-q", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(path),
                "config", "user.email", "tests@example.invalid",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git", "-C", str(path),
                "config", "user.name", "Context tests",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        (path / "fixture.py").write_text("pass\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(path), "add", "fixture.py"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-qm", "Fixture commit"],
            check=True,
            capture_output=True,
            text=True,
        )


    def test_focused_add_preserves_schema_and_assigns_ordered_account_pools(self):
        self.config_path.write_text(
            '{"schemaVersion":1,"contexts":[]}\n', encoding="utf-8"
        )

        added = self.run_focused_context(
            "add",
            str(self.workspace),
            "--github-account",
            "work-account",
        )

        self.assertEqual(added.returncode, 0, added.stderr)
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(document["schemaVersion"], 1)
        self.assertEqual(
            document["contexts"][0]["accountPools"],
            ["shared"],
        )
        self.assertEqual(
            document["contexts"][0]["githubAccount"], "work-account"
        )

        updated = self.run_focused_context(
            "update",
            str(self.workspace),
            "--pool",
            "shared",
            "--no-github-account",
        )
        self.assertEqual(updated.returncode, 0, updated.stderr)
        document = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIsNone(document["contexts"][0]["atlassian"])
        self.assertIsNone(document["contexts"][0]["githubAccount"])
        self.assertEqual(document["contexts"][0]["accountPools"], ["shared"])

    def test_jira_command_configures_and_removes_project_credentials(self):
        self.config_path.write_text(
            '{"schemaVersion":1,"contexts":[]}\n', encoding="utf-8"
        )
        added = self.run_focused_context("add", str(self.workspace))
        self.assertEqual(added.returncode, 0, added.stderr)
        arguments = [
            "--config",
            str(self.config_path),
            "--routing-config",
            str(self.focused_routing_path),
            "--providers-config",
            str(self.providers_path),
            "jira",
            str(self.workspace),
            "--url",
            "https://work.atlassian.net",
            "--username",
            "work@example.com",
        ]
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}), mock.patch(
            "integrations.common.project_context.getpass.getpass",
            return_value="work-token",
        ):
            self.assertEqual(project_context.context_main(arguments), 0)
        configured = self.load_contexts()[0]["atlassian"]
        self.assertEqual(configured, jira("work"))

        removed = self.run_focused_context(
            "jira", str(self.workspace), "--remove"
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIsNone(self.load_contexts()[0]["atlassian"])





    def test_update_rejects_explicit_and_inherited_model_stack_together(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "atlassian": jira("dev"),
                    "modelStack": None,
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")

        rejected = self.run_context(
            "update",
            str(self.workspace),
            "--model-stack",
            "xebia",
            "--inherit-model-stack",
        )

        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

    def test_validate_and_list_reject_persisted_undeclared_model_stack(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "atlassian": jira("dev"),
                    "modelStack": "missing",
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")

        for command in ("validate", "list"):
            with self.subTest(command=command):
                rejected = self.run_context(command)
                self.assertEqual(rejected.returncode, 1)
                self.assertEqual(
                    self.config_path.read_text(encoding="utf-8"), original
                )

    def test_mutation_candidate_cannot_preserve_undeclared_model_stack(self):
        self.write_contexts(
            [
                {
                    "root": str(self.workspace),
                    "atlassian": jira("dev"),
                    "modelStack": "missing",
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")

        rejected = self.run_context(
            "update",
            str(self.workspace),
            "--github-account",
            "next",
        )
        self.assertEqual(rejected.returncode, 1)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)

        repaired = self.run_context(
            "update",
            str(self.workspace),
            "--model-stack",
            "xebia",
        )
        self.assertEqual(repaired.returncode, 0, repaired.stderr)
        self.assertEqual(self.load_contexts()[0]["modelStack"], "xebia")

    def test_add_without_jira_renders_placeholder(self):
        added = self.run_context("add", str(self.workspace))

        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIsNone(self.load_contexts()[0]["atlassian"])

        listed = self.run_context("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("| —", listed.stdout)











    def test_list_renders_headers_for_an_empty_configuration(self):
        listed = self.run_context("list")

        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("| PROJECT ROOT | MODEL STACK | JIRA", listed.stdout)
        self.assertEqual(len(listed.stdout.splitlines()), 4)



    def test_launcher_resolves_an_installed_symlink(self):
        installed = self.root / "bin" / "orichum-context"
        installed.parent.mkdir()
        installed.symlink_to(REPO_ROOT / "bin" / "orichum-context")
        data_home = self.root / "data"
        managed_python = (
            data_home / "python" / "cpython-3.14.6" / "bin" / "python3.14"
        )
        managed_python.parent.mkdir(mode=0o700, parents=True)
        managed_python.write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$*" == *platform.python_implementation* ]]; then\n'
            "  printf 'CPython\\t3.14.6\\n'\n"
            "  exit 0\n"
            "fi\n"
            f'exec "{sys.executable}" "$@"\n',
            encoding="utf-8",
        )
        managed_python.chmod(0o700)
        (data_home / "bin").mkdir(mode=0o700)
        (data_home / "bin" / "orichum-python").symlink_to(managed_python)
        environment = os.environ.copy()
        environment["ORICHUM_CONFIG_HOME"] = str(REPO_ROOT / "config")
        environment["ORICHUM_DATA_HOME"] = str(data_home)
        completed = subprocess.run(
            [str(installed), "list"],
            cwd=self.root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("| PROJECT ROOT", completed.stdout)
        self.assertIn("| JIRA", completed.stdout)

    def test_add_rejects_canonical_root_alias_overlap_before_writing(self):
        alias = self.root / "workspace-alias"
        nested = self.workspace / "nested"
        alias.symlink_to(self.workspace, target_is_directory=True)
        nested.mkdir()
        self.write_contexts(
            [
                {
                    "root": str(alias),
                    "atlassian": jira("dev"),
                }
            ]
        )
        original = self.config_path.read_text(encoding="utf-8")
        rejected = self.run_context("add", str(nested))
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)


    def test_update_rejects_roots_that_canonically_resolve_to_home_or_filesystem_root(self):
        for target in (self.root, Path("/")):
            with self.subTest(target=target):
                alias = self.root / f"unsafe-{len(list(self.root.glob('unsafe-*')))}"
                alias.symlink_to(target, target_is_directory=True)
                self.write_contexts(
                    [
                        {
                            "root": str(alias),
                            "atlassian": jira("dev"),
                        }
                    ]
                )
                original = self.config_path.read_text(encoding="utf-8")
                rejected = self.run_context(
                    "update",
                    str(alias),
                    "--github-account",
                    "next",
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertEqual(self.config_path.read_text(encoding="utf-8"), original)



    def test_validate_rejects_unsafe_canonical_roots_without_changing_list(self):
        home_alias = self.root / "home-alias"
        filesystem_alias = self.root / "filesystem-alias"
        home_alias.symlink_to(self.root, target_is_directory=True)
        filesystem_alias.symlink_to(Path("/"), target_is_directory=True)
        for root in ("/", "~", str(home_alias), str(filesystem_alias)):
            with self.subTest(root=root):
                self.write_contexts(
                    [
                        {
                            "root": root,
                            "atlassian": jira("dev"),
                        }
                    ]
                )
                validated = self.run_context("validate")
                self.assertNotEqual(validated.returncode, 0)
                listed = self.run_context("list")
                self.assertEqual(listed.returncode, 0, listed.stderr)
                self.assertIn(root, listed.stdout)

    def test_remove_yes_can_recover_an_exact_unsafe_root_mapping(self):
        self.write_contexts(
            [
                {
                    "root": "/",
                    "atlassian": jira("dev"),
                }
            ]
        )
        removed = self.run_context("remove", "/", "--yes")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertEqual(self.load_contexts(), [])


class StructuralConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name).resolve()
        self.config = {
            "contexts": [
                {
                    "root": "~/xebia",
                    "atlassian": jira("xebia"),
                },
                {
                    "root": "~/complion",
                    "atlassian": jira("realtime"),
                },
            ],
        }

    def tearDown(self):
        self.temporary_directory.cleanup()

    def write_config(self, payload, *, raw=False):
        path = self.home / "structural-config.json"
        path.write_text(payload if raw else json.dumps(payload), encoding="utf-8")
        return path

    def validate(self, payload):
        return project_context.validate_config_structure(
            self.write_config(payload), home=self.home
        )

    def run_cli(self, config_path):
        environment = os.environ.copy()
        environment["HOME"] = str(self.home)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "integrations.common.project_context",
                "validate-config",
                "--config",
                str(config_path),
            ],
            cwd=REPO_ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )






    def test_overlapping_roots_fail_closed(self):
        overlapping = json.loads(json.dumps(self.config))
        overlapping["contexts"][1]["root"] = "~/xebia/nested"
        with self.assertRaises(ContextError):
            self.validate(overlapping)




if __name__ == "__main__":
    unittest.main()
