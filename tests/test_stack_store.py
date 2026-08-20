#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest import mock

from integrations.common.account_registry import Account, update_accounts
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_definition import (
    normalize_model_stacks,
    serialize_model_stacks,
)
from integrations.common.stack_catalog import LiveCatalog, LiveModelChoice
from integrations.common.stack_store import (
    StackSnapshot,
    StackStoreError,
    delete_stack,
    load_stack_snapshot,
    save_stack,
    validate_stack_assignment,
    validate_stack_bindings,
)


ROLES = (
    "repository-explorer",
    "repository-verifier",
    "correctness-critic",
    "architecture-advisor",
    "implementation-worker",
)


class StackStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.private = self.root / "private"
        self.private.mkdir()
        self.private.chmod(0o700)
        self.portable = self.private
        self.model_path = self.private / "model-stacks.json"
        self.binding_path = self.private / "stack-bindings.json"
        self.account_id = "oc-a-1111111111111111"
        self.register_account(self.account_id)
        self.write_model_document(self.document())
        self.snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        self.projects = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": str(self.root / "project"),
                    "atlassian": None,
                    "modelStack": None,
                    "accountPools": ["shared"],
                }
            ],
        }

    def register_account(
        self, identifier: str, *, state: str = "active"
    ) -> None:
        account = Account(
            id=identifier,
            name=identifier,
            provider="openai",
            credential_ref=f"{identifier}.json",
            pool="shared",
            routing_prefix=f"oc-r-{identifier.removeprefix('oc-a-')}",
            priority=100,
            state=state,
            original_prefix=None,
            original_priority=None,
        )
        update_accounts(
            self.private / "accounts.json",
            lambda accounts: (*accounts, account),
        )

    @staticmethod
    def candidate(identifier: str, model: str) -> dict[str, object]:
        return {
            "id": identifier,
            "model": model,
            "providers": ["openai"],
        }

    def stack(self, prefix: str) -> dict[str, object]:
        return {
            "controller": [
                self.candidate(f"oc-c-{prefix}000000000000001", "gpt-main")
            ],
            "agents": {
                role: [
                    self.candidate(
                        f"oc-c-{prefix}{ordinal:015x}", "gpt-worker"
                    )
                ]
                for ordinal, role in enumerate(ROLES, start=2)
            },
        }

    def document(self, *, include_heavy: bool = True) -> dict[str, object]:
        stacks = {"balanced": self.stack("1")}
        if include_heavy:
            stacks["heavy"] = self.stack("2")
        return {
            "schemaVersion": 2,
            "defaultStack": "balanced",
            "models": {
                "gpt-main": {
                    "family": "gpt",
                    "routes": {"openai": "gpt-main-upstream"},
                },
                "gpt-worker": {
                    "family": "gpt",
                    "routes": {"openai": "gpt-worker-upstream"},
                },
            },
            "stacks": stacks,
        }

    def document_with_stack(self, name: str) -> dict[str, object]:
        document = self.document()
        document["stacks"][name] = self.stack("3")
        return document

    def write_model_document(self, document: dict[str, object]) -> None:
        self.model_path.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":"))
            + "\n",
            encoding="utf-8",
        )
        self.model_path.chmod(0o600)

    def read_model_document(self) -> dict[str, object]:
        return json.loads(self.model_path.read_text(encoding="utf-8"))

    def write_bindings(self, bindings: dict[str, str]) -> None:
        self.binding_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "candidateAccounts": bindings,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.binding_path.chmod(0o600)

    def projects_for(self, stack: str) -> dict[str, object]:
        projects = json.loads(json.dumps(self.projects))
        projects["contexts"][0]["modelStack"] = stack
        return projects

    def test_concurrent_stack_edit_is_rejected_without_overwrite(self):
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        external = self.document_with_stack("external")
        self.write_model_document(external)

        with self.assertRaisesRegex(
            StackStoreError, "changed during update"
        ):
            save_stack(snapshot, snapshot.stacks, snapshot.bindings)

        self.assertEqual(self.read_model_document(), external)

    def test_concurrent_binding_edit_is_rejected_without_model_overwrite(self):
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        self.write_bindings(
            {"oc-c-ffffffffffffffff": self.account_id}
        )
        external_binding = self.binding_path.read_bytes()

        with self.assertRaisesRegex(
            StackStoreError, "changed during update"
        ):
            save_stack(snapshot, snapshot.stacks, snapshot.bindings)

        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), external_binding)

    def test_concurrent_saves_serialize_and_stale_writer_never_stages(self):
        first_snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        second_snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        first_staged = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()
        second_staged = threading.Event()
        first_errors: list[BaseException] = []
        second_errors: list[BaseException] = []
        first_update = normalize_model_stacks(
            self.document_with_stack("first")
        )
        from integrations.common import stack_store

        real_stage = stack_store._stage

        def controlled_stage(
            path: Path, payload: bytes, mode: int
        ) -> Path:
            if threading.current_thread().name == "first-stack-save":
                if path == self.model_path and not first_staged.is_set():
                    staged = real_stage(path, payload, mode)
                    first_staged.set()
                    if not release_first.wait(timeout=2):
                        raise AssertionError(
                            "fixture did not release first save"
                        )
                    return staged
            else:
                second_staged.set()
            return real_stage(path, payload, mode)

        def first_save() -> None:
            try:
                save_stack(
                    first_snapshot,
                    first_update,
                    first_snapshot.bindings,
                )
            except BaseException as error:
                first_errors.append(error)

        def second_save() -> None:
            second_started.set()
            try:
                save_stack(
                    second_snapshot,
                    second_snapshot.stacks,
                    second_snapshot.bindings,
                )
            except BaseException as error:
                second_errors.append(error)

        with mock.patch.object(
            stack_store, "_stage", side_effect=controlled_stage
        ):
            first = threading.Thread(
                target=first_save, name="first-stack-save"
            )
            second = threading.Thread(
                target=second_save, name="second-stack-save"
            )
            first.start()
            self.assertTrue(first_staged.wait(timeout=2))
            second.start()
            self.assertTrue(second_started.wait(timeout=2))
            self.assertFalse(second_staged.wait(timeout=0.1))
            release_first.set()
            first.join(timeout=2)
            second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(first_errors, [])
        self.assertEqual(len(second_errors), 1)
        self.assertRegex(
            str(second_errors[0]), "changed during update"
        )
        self.assertFalse(second_staged.is_set())

    def test_rejects_deleting_referenced_or_default_stack(self):
        with self.assertRaisesRegex(StackStoreError, "default stack"):
            delete_stack(self.snapshot, "balanced", self.projects)
        with self.assertRaisesRegex(StackStoreError, "referenced by"):
            delete_stack(
                self.snapshot, "heavy", self.projects_for("heavy")
            )

    def test_delete_removes_stack_and_only_its_candidate_bindings(self):
        heavy_controller = "oc-c-2000000000000001"
        balanced_controller = "oc-c-1000000000000001"
        snapshot = StackSnapshot(
            stacks=self.snapshot.stacks,
            bindings=StackBindings(
                {
                    heavy_controller: self.account_id,
                    balanced_controller: self.account_id,
                }
            ),
            stack_digest=self.snapshot.stack_digest,
            binding_digest=self.snapshot.binding_digest,
        )

        updated, bindings = delete_stack(
            snapshot, "heavy", self.projects
        )

        self.assertEqual(tuple(updated.stacks), ("balanced",))
        self.assertEqual(
            dict(bindings.candidate_accounts),
            {balanced_controller: self.account_id},
        )

    def test_first_rename_failure_preserves_both_originals(self):
        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        original_binding = self.binding_path.read_bytes()
        real_replace = os.replace
        targets: list[Path] = []
        failed = False

        def fail_binding(source: object, target: object) -> None:
            nonlocal failed
            targets.append(Path(target))
            if Path(target) == self.binding_path and not failed:
                failed = True
                raise OSError("injected first rename failure")
            real_replace(source, target)

        with mock.patch(
            "integrations.common.stack_store.os.replace",
            side_effect=fail_binding,
        ), self.assertRaisesRegex(StackStoreError, "could not be saved"):
            save_stack(snapshot, snapshot.stacks, snapshot.bindings)

        self.assertEqual(
            [
                target
                for target in targets
                if target in (self.binding_path, self.model_path)
            ],
            [self.binding_path, self.model_path, self.binding_path],
        )
        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), original_binding)
        self.assertEqual(
            [
                path
                for path in self.private.iterdir()
                if path.name.startswith((".model-stacks.json.", ".stack-bindings.json."))
            ],
            [],
        )

    def test_second_rename_failure_restores_fsynced_binding_snapshot(self):
        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        original_binding = self.binding_path.read_bytes()
        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )
        real_replace = os.replace
        targets: list[Path] = []
        failed = False

        def fail_model_once(source: object, target: object) -> None:
            nonlocal failed
            targets.append(Path(target))
            if Path(target) == self.model_path and not failed:
                failed = True
                raise OSError("injected second rename failure")
            real_replace(source, target)

        with mock.patch(
            "integrations.common.stack_store.os.replace",
            side_effect=fail_model_once,
        ), self.assertRaisesRegex(StackStoreError, "could not be saved"):
            save_stack(snapshot, snapshot.stacks, updated_bindings)

        self.assertEqual(
            [
                target
                for target in targets
                if target in (self.binding_path, self.model_path)
            ],
            [
                self.binding_path,
                self.model_path,
                self.model_path,
                self.binding_path,
            ],
        )
        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), original_binding)

    def test_rollback_failure_retains_fsynced_binding_snapshot(self):
        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        original_binding = self.binding_path.read_bytes()
        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )
        real_replace = os.replace
        binding_replacements = 0
        model_replacements = 0

        def fail_model_and_restore(
            source: object, target: object
        ) -> None:
            nonlocal binding_replacements, model_replacements
            if Path(target) == self.binding_path:
                binding_replacements += 1
                if binding_replacements == 2:
                    raise OSError("injected rollback failure")
            if Path(target) == self.model_path:
                model_replacements += 1
                if model_replacements == 1:
                    raise OSError("injected model rename failure")
            real_replace(source, target)

        with mock.patch(
            "integrations.common.stack_store.os.replace",
            side_effect=fail_model_and_restore,
        ), self.assertRaisesRegex(StackStoreError, "rollback failed"):
            save_stack(snapshot, snapshot.stacks, updated_bindings)

        model_recovery = (
            self.private
            / ".model-stacks.transaction.model.original"
        )
        binding_recovery = (
            self.private
            / ".model-stacks.transaction.bindings.original"
        )
        marker = self.private / ".model-stacks.transaction.json"
        self.assertEqual(model_recovery.read_bytes(), original_model)
        self.assertEqual(binding_recovery.read_bytes(), original_binding)
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["state"],
            "pending",
        )
        for recovery in (model_recovery, binding_recovery, marker):
            self.assertEqual(
                stat.S_IMODE(recovery.stat().st_mode), 0o600
            )

        recovered = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), original_binding)
        self.assertEqual(
            dict(recovered.bindings.candidate_accounts),
            {"oc-c-1000000000000001": self.account_id},
        )
        self.assertEqual(
            list(self.private.glob(".model-stacks.transaction*")), []
        )

    def test_second_rename_failure_removes_new_binding_when_none_existed(self):
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )
        real_replace = os.replace
        failed = False

        def fail_model(source: object, target: object) -> None:
            nonlocal failed
            if Path(target) == self.model_path and not failed:
                failed = True
                raise OSError("injected model rename failure")
            real_replace(source, target)

        with mock.patch(
            "integrations.common.stack_store.os.replace",
            side_effect=fail_model,
        ), self.assertRaisesRegex(StackStoreError, "could not be saved"):
            save_stack(snapshot, snapshot.stacks, updated_bindings)

        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertFalse(self.binding_path.exists())

    def test_post_second_rename_interrupt_restores_both_originals(self):
        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        original_binding = self.binding_path.read_bytes()
        updated_stacks = normalize_model_stacks(
            self.document_with_stack("updated")
        )
        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )
        real_replace = os.replace
        interrupted = False

        def interrupt_after_model(
            source: object, target: object
        ) -> None:
            nonlocal interrupted
            real_replace(source, target)
            if Path(target) == self.model_path and not interrupted:
                interrupted = True
                raise KeyboardInterrupt(
                    "injected post-model-replace interrupt"
                )

        with mock.patch(
            "integrations.common.stack_store.os.replace",
            side_effect=interrupt_after_model,
        ), self.assertRaises(StackStoreError):
            save_stack(snapshot, updated_stacks, updated_bindings)

        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), original_binding)

    def test_recovery_snapshots_and_marker_are_synced_in_separate_epochs(self):
        from integrations.common import stack_store

        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )
        events: list[str] = []
        real_replace_private_file = stack_store._replace_private_file
        real_replace = os.replace
        real_fsync_directory = stack_store._fsync_directory

        def observe_private_replace(path: Path, payload: bytes) -> None:
            if path.name == stack_store._MODEL_ORIGINAL:
                events.append("snapshot:model")
            elif path.name == stack_store._BINDING_ORIGINAL:
                events.append("snapshot:binding")
            elif path.name == stack_store._TRANSACTION_MARKER:
                events.append(
                    "marker:" + json.loads(payload.decode("utf-8"))["state"]
                )
            real_replace_private_file(path, payload)

        def observe_replace(source: object, target: object) -> None:
            if Path(target) == self.binding_path:
                events.append("publish:binding")
            elif Path(target) == self.model_path:
                events.append("publish:model")
            real_replace(source, target)

        def observe_fsync(path: Path) -> None:
            events.append("fsync:directory")
            real_fsync_directory(path)

        with (
            mock.patch.object(
                stack_store,
                "_replace_private_file",
                side_effect=observe_private_replace,
            ),
            mock.patch.object(
                stack_store.os, "replace", side_effect=observe_replace
            ),
            mock.patch.object(
                stack_store,
                "_fsync_directory",
                side_effect=observe_fsync,
            ),
        ):
            save_stack(
                snapshot, snapshot.stacks, updated_bindings
            )

        snapshot_sync = events.index(
            "fsync:directory",
            events.index("snapshot:binding") + 1,
        )
        pending = events.index("marker:pending")
        marker_sync = events.index("fsync:directory", pending + 1)
        self.assertLess(events.index("snapshot:model"), snapshot_sync)
        self.assertLess(events.index("snapshot:binding"), snapshot_sync)
        self.assertLess(snapshot_sync, pending)
        self.assertLess(pending, marker_sync)
        self.assertLess(marker_sync, events.index("publish:binding"))
        self.assertLess(marker_sync, events.index("publish:model"))

    def test_crash_before_snapshot_directory_sync_never_publishes_marker(self):
        from integrations.common import stack_store

        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        original_model = self.model_path.read_bytes()
        original_binding = self.binding_path.read_bytes()
        observations: list[bool] = []
        calls = 0
        real_fsync_directory = stack_store._fsync_directory

        def crash_at_first_sync(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                observations.append(
                    (self.private / stack_store._TRANSACTION_MARKER).exists()
                )
                raise OSError("injected snapshot directory sync failure")
            real_fsync_directory(path)

        with mock.patch.object(
            stack_store,
            "_fsync_directory",
            side_effect=crash_at_first_sync,
        ), self.assertRaises(StackStoreError):
            save_stack(
                snapshot,
                snapshot.stacks,
                StackBindings(
                    {"oc-c-2000000000000001": self.account_id}
                ),
            )

        self.assertEqual(observations, [False])
        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), original_binding)

    def test_every_directory_fsync_boundary_has_defined_outcome(self):
        from integrations.common import stack_store

        for boundary in (1, 2, 3, 4, 5):
            with self.subTest(boundary=boundary):
                self.write_model_document(self.document())
                self.write_bindings(
                    {"oc-c-1000000000000001": self.account_id}
                )
                for name in (
                    ".model-stacks.transaction.json",
                    ".model-stacks.transaction.model.original",
                    ".model-stacks.transaction.bindings.original",
                ):
                    try:
                        (self.private / name).unlink()
                    except FileNotFoundError:
                        pass
                snapshot = load_stack_snapshot(
                    self.model_path, self.binding_path
                )
                original_model = self.model_path.read_bytes()
                original_binding = self.binding_path.read_bytes()
                updated_bindings = StackBindings(
                    {"oc-c-2000000000000001": self.account_id}
                )
                calls = 0
                real_fsync_directory = stack_store._fsync_directory

                def fail_boundary(path: Path) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == boundary:
                        raise OSError(
                            f"injected directory fsync {boundary}"
                        )
                    real_fsync_directory(path)

                with mock.patch.object(
                    stack_store,
                    "_fsync_directory",
                    side_effect=fail_boundary,
                ):
                    if boundary < 5:
                        with self.assertRaises(StackStoreError):
                            save_stack(
                                snapshot,
                                snapshot.stacks,
                                updated_bindings,
                            )
                        self.assertEqual(
                            self.model_path.read_bytes(), original_model
                        )
                        self.assertEqual(
                            self.binding_path.read_bytes(), original_binding
                        )
                    else:
                        save_stack(
                            snapshot,
                            snapshot.stacks,
                            updated_bindings,
                        )
                        self.assertEqual(
                            json.loads(
                                self.binding_path.read_text(
                                    encoding="utf-8"
                                )
                            )["candidateAccounts"],
                            {
                                "oc-c-2000000000000001": self.account_id
                            },
                        )
                self.assertGreaterEqual(calls, boundary)

    def write_recovery_file(self, name: str, payload: bytes) -> Path:
        path = self.private / name
        path.write_bytes(payload)
        path.chmod(0o600)
        return path

    def write_transaction_marker(
        self, state: str, *, binding_existed: bool
    ) -> Path:
        return self.write_recovery_file(
            ".model-stacks.transaction.json",
            (
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "state": state,
                        "bindingExisted": binding_existed,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )

    def test_restart_recovers_pending_transaction_before_loading(self):
        self.write_bindings(
            {"oc-c-1000000000000001": self.account_id}
        )
        original_model = self.model_path.read_bytes()
        original_binding = self.binding_path.read_bytes()
        self.write_recovery_file(
            ".model-stacks.transaction.model.original",
            original_model,
        )
        self.write_recovery_file(
            ".model-stacks.transaction.bindings.original",
            original_binding,
        )
        self.write_transaction_marker("pending", binding_existed=True)
        self.write_model_document(self.document_with_stack("external"))
        self.write_bindings(
            {"oc-c-2000000000000001": self.account_id}
        )

        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )

        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(self.binding_path.read_bytes(), original_binding)
        self.assertNotIn("external", snapshot.stacks.stacks)
        self.assertEqual(
            dict(snapshot.bindings.candidate_accounts),
            {"oc-c-1000000000000001": self.account_id},
        )
        self.assertEqual(
            list(self.private.glob(".model-stacks.transaction*")), []
        )

    def test_restart_preserves_committed_targets_and_cleans_recovery(self):
        original_model = self.model_path.read_bytes()
        self.write_recovery_file(
            ".model-stacks.transaction.model.original",
            original_model,
        )
        self.write_recovery_file(
            ".model-stacks.transaction.bindings.original",
            b'{"schemaVersion":1,"candidateAccounts":{}}\n',
        )
        self.write_transaction_marker("committed", binding_existed=True)
        committed = self.document_with_stack("committed")
        self.write_model_document(committed)
        self.write_bindings(
            {"oc-c-2000000000000001": self.account_id}
        )

        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )

        self.assertIn("committed", snapshot.stacks.stacks)
        self.assertEqual(
            dict(snapshot.bindings.candidate_accounts),
            {"oc-c-2000000000000001": self.account_id},
        )
        self.assertEqual(
            list(self.private.glob(".model-stacks.transaction*")), []
        )

    def test_committed_cleanup_interrupt_returns_success_and_next_load_cleans(self):
        from integrations.common import stack_store

        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )

        with mock.patch.object(
            stack_store,
            "_remove_recovery_artifacts",
            side_effect=KeyboardInterrupt(
                "injected committed cleanup interrupt"
            ),
        ):
            save_stack(
                self.snapshot,
                self.snapshot.stacks,
                updated_bindings,
            )

        marker = self.private / ".model-stacks.transaction.json"
        self.assertEqual(
            json.loads(marker.read_text(encoding="utf-8"))["state"],
            "committed",
        )
        loaded = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        self.assertEqual(
            dict(loaded.bindings.candidate_accounts),
            {"oc-c-2000000000000001": self.account_id},
        )
        self.assertEqual(
            list(self.private.glob(".model-stacks.transaction*")), []
        )

    def test_model_staging_is_private_at_replacement(self):
        modes: list[int] = []
        real_replace = os.replace

        def inspect_model_stage(
            source: object, target: object
        ) -> None:
            if Path(target) == self.model_path:
                modes.append(stat.S_IMODE(Path(source).stat().st_mode))
            real_replace(source, target)

        with mock.patch(
            "integrations.common.stack_store.os.replace",
            side_effect=inspect_model_stage,
        ):
            save_stack(
                self.snapshot,
                self.snapshot.stacks,
                StackBindings(
                    {"oc-c-2000000000000001": self.account_id}
                ),
            )

        self.assertEqual(modes, [0o600])

    def test_save_writes_private_binding_and_lock_with_canonical_documents(self):
        updated_bindings = StackBindings(
            {"oc-c-2000000000000001": self.account_id}
        )

        save_stack(
            self.snapshot, self.snapshot.stacks, updated_bindings
        )

        self.assertEqual(
            json.loads(self.binding_path.read_text(encoding="utf-8")),
            {
                "schemaVersion": 1,
                "candidateAccounts": {
                    "oc-c-2000000000000001": self.account_id
                },
            },
        )
        self.assertEqual(stat.S_IMODE(self.binding_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.model_path.stat().st_mode), 0o600)
        self.assertEqual(
            stat.S_IMODE(
                (self.private / ".model-stacks.lock").stat().st_mode
            ),
            0o600,
        )
        self.assertEqual(
            self.read_model_document(),
            serialize_model_stacks(self.snapshot.stacks),
        )

    def test_save_preserves_absent_binding_when_empty_and_unchanged(self):
        self.assertFalse(self.binding_path.exists())

        save_stack(
            self.snapshot,
            self.snapshot.stacks,
            self.snapshot.bindings,
        )

        self.assertFalse(self.binding_path.exists())

    def test_combined_binding_validation_requires_usable_matching_account(self):
        candidate = "oc-c-2000000000000001"
        valid = self.active_account()
        validate_stack_bindings(
            self.snapshot.stacks,
            StackBindings({candidate: valid.id}),
            (valid,),
        )

        with self.assertRaisesRegex(StackStoreError, "unknown candidate"):
            validate_stack_bindings(
                self.snapshot.stacks,
                StackBindings(
                    {"oc-c-ffffffffffffffff": valid.id}
                ),
                (valid,),
            )

        disabled = Account(
            **{
                **valid.__dict__,
                "state": "disabled",
            }
        )
        with self.assertRaisesRegex(StackStoreError, "active account"):
            validate_stack_bindings(
                self.snapshot.stacks,
                StackBindings({candidate: valid.id}),
                (disabled,),
            )

        wrong_provider = Account(
            **{
                **valid.__dict__,
                "provider": "anthropic",
            }
        )
        with self.assertRaisesRegex(StackStoreError, "allowed provider"):
            validate_stack_bindings(
                self.snapshot.stacks,
                StackBindings({candidate: valid.id}),
                (wrong_provider,),
            )

    def test_save_rejects_binding_to_missing_candidate_or_inactive_account(self):
        missing_candidate = StackBindings(
            {"oc-c-ffffffffffffffff": self.account_id}
        )
        with self.assertRaisesRegex(
            StackStoreError, "unknown candidate"
        ) as missing:
            save_stack(
                self.snapshot, self.snapshot.stacks, missing_candidate
            )
        self.assertNotIn("oc-c-", str(missing.exception))

        inactive_id = "oc-a-2222222222222222"
        self.register_account(inactive_id, state="disabled")
        inactive = StackBindings(
            {"oc-c-2000000000000001": inactive_id}
        )
        with self.assertRaisesRegex(StackStoreError, "active account"):
            save_stack(self.snapshot, self.snapshot.stacks, inactive)

        self.assertFalse(self.binding_path.exists())

    def test_load_ignores_stale_binding_only_for_missing_candidate(self):
        self.write_bindings(
            {"oc-c-ffffffffffffffff": self.account_id}
        )

        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )

        self.assertEqual(snapshot.bindings, StackBindings({}))
        self.assertIsNotNone(snapshot.binding_digest)

    def test_load_and_save_reject_unsafe_files_and_lock(self):
        model_target = self.portable / "real-model-stacks.json"
        self.model_path.replace(model_target)
        self.model_path.symlink_to(model_target)
        with self.assertRaisesRegex(StackStoreError, "unsafe"):
            load_stack_snapshot(self.model_path, self.binding_path)

        self.model_path.unlink()
        model_target.replace(self.model_path)
        self.model_path.chmod(0o640)
        with self.assertRaisesRegex(StackStoreError, "unsafe"):
            load_stack_snapshot(self.model_path, self.binding_path)
        self.model_path.chmod(0o600)
        snapshot = load_stack_snapshot(
            self.model_path, self.binding_path
        )
        lock_target = self.private / "lock-target"
        lock_target.write_text("", encoding="utf-8")
        lock_target.chmod(0o600)
        model_lock = self.private / ".model-stacks.lock"
        model_lock.unlink()
        model_lock.symlink_to(lock_target)
        with self.assertRaisesRegex(StackStoreError, "lock is unsafe"):
            save_stack(snapshot, snapshot.stacks, snapshot.bindings)

        model_lock.unlink()
        self.write_bindings({})
        self.binding_path.chmod(0o644)
        with self.assertRaisesRegex(StackStoreError, "unsafe"):
            load_stack_snapshot(self.model_path, self.binding_path)

    def test_model_and_binding_must_share_the_private_parent(self):
        other = self.root / "other"
        other.mkdir()
        other_model = other / "model-stacks.json"
        other_model.write_bytes(self.model_path.read_bytes())
        other_model.chmod(0o600)

        with self.assertRaisesRegex(StackStoreError, "same private parent"):
            load_stack_snapshot(other_model, self.binding_path)

    def live_catalog(
        self, *, include_worker: bool = True
    ) -> LiveCatalog:
        choices = [
            LiveModelChoice(
                family="gpt",
                provider="openai",
                upstream="gpt-main-upstream",
                account_ids=(self.account_id,),
                account_names=(self.account_id,),
            )
        ]
        if include_worker:
            choices.append(
                LiveModelChoice(
                    family="gpt",
                    provider="openai",
                    upstream="gpt-worker-upstream",
                    account_ids=(self.account_id,),
                    account_names=(self.account_id,),
                )
            )
        return LiveCatalog(
            choices=tuple(choices), unclassified=()
        )

    def active_account(
        self, identifier: str | None = None, *, pool: str = "shared"
    ) -> Account:
        identifier = identifier or self.account_id
        return Account(
            id=identifier,
            name=identifier,
            provider="openai",
            credential_ref=f"{identifier}.json",
            pool=pool,
            routing_prefix=f"oc-r-{identifier.removeprefix('oc-a-')}",
            priority=100,
            state="active",
            original_prefix=None,
            original_priority=None,
        )

    def test_assignment_requires_live_controller_and_every_agent_role(self):
        validate_stack_assignment(
            "heavy",
            {"accountPools": ["shared"]},
            self.snapshot.stacks,
            StackBindings(
                {"oc-c-2000000000000001": self.account_id}
            ),
            (self.active_account(),),
            {"openai": {"families": ["gpt"]}},
            self.live_catalog(),
        )

        with self.assertRaisesRegex(
            StackStoreError, "implementation-worker|agent role"
        ):
            validate_stack_assignment(
                "heavy",
                {"accountPools": ["shared"]},
                self.snapshot.stacks,
                StackBindings({}),
                (self.active_account(),),
                {"openai": {"families": ["gpt"]}},
                self.live_catalog(include_worker=False),
            )

    def test_assignment_rejects_locked_account_outside_context_or_exact_route(self):
        other_id = "oc-a-3333333333333333"
        locked = StackBindings(
            {"oc-c-2000000000000001": other_id}
        )
        for account, catalog in (
            (
                self.active_account(other_id, pool="other"),
                LiveCatalog(
                    choices=(
                        LiveModelChoice(
                            family="gpt",
                            provider="openai",
                            upstream="gpt-main-upstream",
                            account_ids=(other_id,),
                            account_names=(other_id,),
                        ),
                    ),
                    unclassified=(),
                ),
            ),
            (
                self.active_account(other_id),
                self.live_catalog(),
            ),
        ):
            with self.subTest(
                pool=account.pool
            ), self.assertRaisesRegex(
                StackStoreError, "locked candidate"
            ) as rejected:
                validate_stack_assignment(
                    "heavy",
                    {"accountPools": ["shared"]},
                    self.snapshot.stacks,
                    locked,
                    (self.active_account(), account),
                    {"openai": {"families": ["gpt"]}},
                    catalog,
                )
            self.assertNotIn("oc-c-", str(rejected.exception))

    def test_invalid_assignment_does_not_mutate_saved_stack_or_projects(self):
        original_model = self.model_path.read_bytes()
        original_projects = json.dumps(self.projects, sort_keys=True)

        with self.assertRaisesRegex(StackStoreError, "unknown"):
            validate_stack_assignment(
                "missing",
                {"accountPools": ["shared"]},
                self.snapshot.stacks,
                StackBindings({}),
                (self.active_account(),),
                {"openai": {"families": ["gpt"]}},
                self.live_catalog(),
            )

        self.assertEqual(self.model_path.read_bytes(), original_model)
        self.assertEqual(
            json.dumps(self.projects, sort_keys=True),
            original_projects,
        )


if __name__ == "__main__":
    unittest.main()
