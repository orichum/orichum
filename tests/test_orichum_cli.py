#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from integrations.common import orichum_cli
from integrations.common import project_context
from integrations.common import stack_bindings
from integrations.common.configure_state import (
    ConfigurationDraft,
    PendingAccount,
    selections_for_stack,
)
from integrations.common.leanctx_monitor import (
    LeanctxGainSummary,
    LeanctxProxyStats,
    LeanctxRollingEconomics,
    LeanctxRun,
    LeanctxStats,
    LeanctxToolHealth,
)
from integrations.common.stack_bindings import (
    StackBindingError,
    StackBindings,
    save_stack_bindings,
)
from integrations.common.stack_definition import serialize_model_stacks


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class OrichumCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.environment = {
            "ORICHUM_HOME": str(self.root / "orichum"),
            "ORICHUM_CONFIG_HOME": str(REPOSITORY_ROOT / "config"),
            "ORICHUM_DATA_HOME": str(self.root / "data"),
            "ORICHUM_CACHE_HOME": str(self.root / "cache"),
            "ORICHUM_SESSION_ID": "",
        }

    def run_cli(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.environment, clear=False),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            status = orichum_cli.main(list(arguments))
        return status, stdout.getvalue(), stderr.getvalue()

    def version_output(
        self,
        identity: dict[str, object] | str | None,
        manifest_digest: str | None = (
            "8d8406645ad44b4c744fca4fd285aa3d87d7c2559d0810ae20c5dc313162e5ae"
        ),
    ) -> str:
        runtime = Path(tempfile.mkdtemp(
            dir=self.root,
            prefix="version-runtime.",
        ))
        (runtime / "VERSION").write_text(
            "0.1.0-rc.12\n",
            encoding="ascii",
        )
        if isinstance(identity, dict):
            (runtime / "build-identity.json").write_text(
                json.dumps(identity) + "\n",
                encoding="utf-8",
            )
        elif isinstance(identity, str):
            (runtime / "build-identity.json").write_text(
                identity,
                encoding="utf-8",
            )
        if manifest_digest is not None:
            (runtime / "runtime-manifest.json").write_text(
                json.dumps({
                    "schemaVersion": 1,
                    "digest": manifest_digest,
                    "files": [],
                }) + "\n",
                encoding="utf-8",
            )
        stdout = io.StringIO()
        with (
            mock.patch.object(orichum_cli, "WORKFLOW_ROOT", runtime),
            contextlib.redirect_stdout(stdout),
            self.assertRaises(SystemExit) as raised,
        ):
            orichum_cli.build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)
        return stdout.getvalue()

    def test_default_paths_are_consolidated_under_orichum_home(self) -> None:
        home = self.root / "home"

        paths = orichum_cli._paths({"HOME": str(home)})

        self.assertEqual(
            paths,
            {
                "home": home / ".orichum",
                "cache": home / ".orichum" / "cache",
                "config": home / ".orichum" / "config",
                "data": home / ".orichum",
                "state": home / ".orichum" / "state",
            },
        )

    def test_configure_parser_is_goal_based_and_project_targeted(self) -> None:
        parser = orichum_cli.build_parser()
        parsed = parser.parse_args(
            ["configure", "--project", "/work/acme", "--verbose"]
        )

        self.assertEqual(parsed.command, "configure")
        self.assertEqual(parsed.project, "/work/acme")
        self.assertTrue(parsed.verbose)
        configure = next(
            action.choices["configure"]
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        help_text = configure.format_help()
        self.assertIn(
            "models, accounts, health, and advanced settings",
            help_text,
        )
        self.assertNotIn("candidate", help_text.casefold())
        self.assertNotIn("routing prefix", help_text.casefold())

    def test_configure_dispatches_to_guided_wizard(self) -> None:
        paths = orichum_cli._paths(self.environment)
        config = object()
        with (
            mock.patch.object(orichum_cli, "_interactive_terminal", return_value=True),
            mock.patch.object(orichum_cli, "_load", return_value=(paths, config)),
            mock.patch.object(orichum_cli, "run_configure", return_value=0) as run,
        ):
            status = orichum_cli.main(
                ["configure", "--project", "/work/acme", "--verbose"]
            )

        self.assertEqual(status, 0)
        run.assert_called_once_with(
            paths,
            config,
            Path("/work/acme"),
            verbose=True,
        )

    def test_configuration_apply_registers_account_and_assigns_stack(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        changed = replace(
            snapshot.assignments["controller"],
            model="gpt-5.6-terra",
            upstream="gpt-5.6-terra",
        )
        draft = (
            ConfigurationDraft.from_snapshot(snapshot)
            .with_roles(("controller",), changed)
            .with_pending_account(
                PendingAccount(
                    provider="openai",
                    credential_ref="new-openai.json",
                    name="OpenAI new",
                    pool="shared",
                    priority=50,
                    intent="additional",
                )
            )
        )
        created = replace(
            snapshot.accounts[1],
            id="oc-a-dddddddddddddddd",
            routing_prefix="oc-r-dddddddddddddddd",
            name="OpenAI new",
            credential_ref="new-openai.json",
        )
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
        }
        paths["config"].mkdir(mode=0o700)
        stack_snapshot = SimpleNamespace(
            stacks=snapshot.stacks,
            bindings=snapshot.bindings,
        )
        with (
            mock.patch.object(orichum_cli, "_mutate_account") as mutate,
            mock.patch.object(
                orichum_cli,
                "load_accounts",
                side_effect=(snapshot.accounts, (*snapshot.accounts, created)),
            ),
            mock.patch.object(
                orichum_cli,
                "load_stack_snapshot",
                return_value=stack_snapshot,
            ),
            mock.patch.object(
                orichum_cli,
                "build_managed_stack",
                return_value=snapshot.stacks,
            ),
            mock.patch.object(orichum_cli, "save_stack") as save,
            mock.patch.object(
                orichum_cli,
                "assign_stack_to_context",
            ) as assign,
            mock.patch.object(
                orichum_cli,
                "control_plane_transaction",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                orichum_cli,
                "stack_binding_transaction",
                return_value=contextlib.nullcontext(),
            ),
        ):
            orichum_cli._apply_configuration_draft(
                paths,
                object(),
                snapshot,
                draft,
            )

        self.assertEqual(mutate.call_count, 1)
        self.assertEqual(mutate.call_args.args[0].account_command, "add")
        self.assertEqual(mutate.call_args.args[0].priority, "50")
        save.assert_called_once()
        assign.assert_called_once_with(
            paths["config"] / "projects.json",
            snapshot.target.root,
            orichum_cli.managed_stack_name(snapshot.target.root),
            snapshot.stacks.stacks,
        )

    def test_configuration_apply_switches_to_existing_profile_without_rebuild(
        self,
    ) -> None:
        from tests.test_configure_state import _snapshot_with_alternate_profile

        snapshot = _snapshot_with_alternate_profile()
        draft = ConfigurationDraft.from_snapshot(snapshot).with_profile(
            replace(snapshot.target, stack_name="quality"),
            selections_for_stack(snapshot, "quality"),
        )
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
        }
        paths["config"].mkdir(mode=0o700)
        stack_snapshot = SimpleNamespace(
            stacks=snapshot.stacks,
            bindings=snapshot.bindings,
        )
        with (
            mock.patch.object(
                orichum_cli,
                "load_accounts",
                return_value=snapshot.accounts,
            ),
            mock.patch.object(
                orichum_cli,
                "load_stack_snapshot",
                return_value=stack_snapshot,
            ),
            mock.patch.object(
                orichum_cli,
                "load_configuration_snapshot",
                return_value=snapshot,
            ),
            mock.patch.object(
                orichum_cli,
                "stack_is_live_compatible",
                return_value=True,
            ),
            mock.patch.object(orichum_cli, "build_managed_stack") as build,
            mock.patch.object(orichum_cli, "save_stack") as save,
            mock.patch.object(
                orichum_cli,
                "assign_stack_to_context",
            ) as assign,
            mock.patch.object(
                orichum_cli,
                "control_plane_transaction",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                orichum_cli,
                "stack_binding_transaction",
                return_value=contextlib.nullcontext(),
            ),
        ):
            orichum_cli._apply_configuration_draft(
                paths,
                object(),
                snapshot,
                draft,
            )

        build.assert_not_called()
        save.assert_called_once()
        assign.assert_called_once_with(
            paths["config"] / "projects.json",
            snapshot.target.root,
            "quality",
            snapshot.stacks.stacks,
        )

    def test_configuration_apply_removes_incompatible_new_backup(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        primary = snapshot.accounts[0]
        created = replace(
            snapshot.accounts[1],
            id="oc-a-dddddddddddddddd",
            routing_prefix="oc-r-dddddddddddddddd",
            name="Unusable backup",
            credential_ref="unusable-backup.json",
        )
        draft = ConfigurationDraft.from_snapshot(snapshot).with_pending_account(
            PendingAccount(
                provider="openai",
                credential_ref=created.credential_ref,
                name=created.name,
                pool="shared",
                priority=50,
                intent="backup",
                primary_id=primary.id,
                primary_name=primary.name,
            )
        )
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
        }
        paths["config"].mkdir(mode=0o700)
        refreshed = replace(
            snapshot,
            accounts=(*snapshot.accounts, created),
        )
        with (
            mock.patch.object(orichum_cli, "_mutate_account") as mutate,
            mock.patch.object(
                orichum_cli,
                "load_accounts",
                side_effect=(snapshot.accounts, (*snapshot.accounts, created)),
            ),
            mock.patch.object(
                orichum_cli,
                "load_configuration_snapshot",
                return_value=refreshed,
            ),
            self.assertRaisesRegex(
                orichum_cli.CliError,
                "compatible route",
            ),
        ):
            orichum_cli._apply_configuration_draft(
                paths,
                object(),
                snapshot,
                draft,
            )

        self.assertEqual(mutate.call_count, 2)
        self.assertEqual(mutate.call_args_list[0].args[0].account_command, "add")
        self.assertEqual(
            mutate.call_args_list[1].args[0].account_command,
            "remove",
        )

    def test_configuration_apply_rolls_back_first_when_second_add_fails(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        first = replace(
            snapshot.accounts[1],
            id="oc-a-dddddddddddddddd",
            routing_prefix="oc-r-dddddddddddddddd",
            name="First new",
            credential_ref="first-new.json",
        )
        draft = ConfigurationDraft.from_snapshot(snapshot)
        for name, credential in (
            (first.name, first.credential_ref),
            ("Second new", "second-new.json"),
        ):
            draft = draft.with_pending_account(
                PendingAccount(
                    provider="openai",
                    credential_ref=credential,
                    name=name,
                    pool="shared",
                    priority=50,
                    intent="additional",
                )
            )
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
        }
        paths["config"].mkdir(mode=0o700)
        with (
            mock.patch.object(
                orichum_cli,
                "_mutate_account",
                side_effect=(None, orichum_cli.CliError("second add failed"), None),
            ) as mutate,
            mock.patch.object(
                orichum_cli,
                "load_accounts",
                side_effect=(snapshot.accounts, (*snapshot.accounts, first)),
            ),
            self.assertRaisesRegex(orichum_cli.CliError, "second add failed"),
        ):
            orichum_cli._apply_configuration_draft(
                paths,
                object(),
                snapshot,
                draft,
            )

        self.assertEqual(mutate.call_count, 3)
        self.assertEqual(
            mutate.call_args_list[2].args[0].account_command,
            "remove",
        )
        self.assertEqual(mutate.call_args_list[2].args[0].selector, first.id)

    def test_configuration_apply_rejects_unusable_existing_stack(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        draft = ConfigurationDraft.from_snapshot(snapshot).with_project(
            replace(snapshot.target, stack_name="offline")
        )
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
        }
        paths["config"].mkdir(mode=0o700)
        stack_snapshot = SimpleNamespace(
            stacks=snapshot.stacks,
            bindings=snapshot.bindings,
        )
        with (
            mock.patch.object(
                orichum_cli,
                "load_accounts",
                return_value=snapshot.accounts,
            ),
            mock.patch.object(
                orichum_cli,
                "load_stack_snapshot",
                return_value=stack_snapshot,
            ),
            mock.patch.object(
                orichum_cli,
                "load_configuration_snapshot",
                return_value=snapshot,
            ),
            mock.patch.object(
                orichum_cli,
                "stack_is_live_compatible",
                return_value=False,
            ),
            mock.patch.object(orichum_cli, "save_stack") as save,
            mock.patch.object(orichum_cli, "assign_stack_to_context") as assign,
            self.assertRaisesRegex(
                orichum_cli.CliError,
                "not usable",
            ),
        ):
            orichum_cli._apply_configuration_draft(
                paths,
                object(),
                snapshot,
                draft,
            )

        save.assert_not_called()
        assign.assert_not_called()

    def test_project_model_mapping_replaces_stack_and_ignores_candidate_locks(
        self,
    ) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        launch = project / "src"
        launch.mkdir(parents=True)
        directory = project / ".orichum"
        directory.mkdir()
        document = {
            "schemaVersion": 1,
            "controller": "gpt-5.6-sol",
            "agents": {
                role: "gpt-5.6-sol" for role in orichum_cli.ROLES
            },
        }
        (directory / "models.json").write_text(
            json.dumps(document),
            encoding="utf-8",
        )
        config = SimpleNamespace(
            documents={
                "model-stacks": serialize_model_stacks(snapshot.stacks),
                "projects": {},
            }
        )
        context = {
            "launchDirReal": str(launch),
            "route": {
                "contextRootReal": str(project),
                "modelStack": "balanced",
            },
        }
        paths = {"config": self.root / "config"}

        project_models = orichum_cli.discover_project_models(
            launch,
            project,
            snapshot.stacks,
        )
        assert project_models is not None
        with mock.patch.object(orichum_cli, "load_stack_bindings") as load:
            documents, stack_name, bindings, project_models = (
                orichum_cli._session_model_inputs(
                    paths,
                    config,
                    context,
                    project_models,
                )
            )

        load.assert_not_called()
        assert project_models is not None
        self.assertEqual(stack_name, project_models.stack_name)
        self.assertIs(documents["model-stacks"], project_models.stacks)
        self.assertEqual(bindings, StackBindings({}))
        self.assertEqual(
            project_models.stacks.stacks[stack_name].controller[0].providers,
            ("openai",),
        )

    def test_new_session_uses_project_model_inputs(self) -> None:
        project = self.root / "project"
        launch = project / "src"
        launch.mkdir(parents=True)
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(
            documents={
                "model-stacks": object(),
                "projects": {},
                "providers": {},
            }
        )
        context = {
            "launchDirReal": str(launch),
            "route": {
                "contextRootReal": str(project),
                "accountPools": ["shared"],
            },
        }
        session_documents = {"model-stacks": object()}
        bindings = StackBindings({})
        controller = object()
        agents = {role: object() for role in orichum_cli.ROLES}
        plan = SimpleNamespace(
            stack="repository-local-test",
            controller=controller,
            agents=agents,
            effective=object(),
        )
        logical = object()
        physical = object()

        with (
            mock.patch.object(orichum_cli, "_verify_runtime"),
            mock.patch.object(
                orichum_cli,
                "normalize_model_stacks",
                return_value=object(),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_project_context",
                return_value=(context, None),
            ),
            mock.patch.object(
                orichum_cli,
                "_session_model_inputs",
                return_value=(
                    session_documents,
                    "repository-local-test",
                    bindings,
                    object(),
                ),
            ),
            mock.patch.object(orichum_cli, "load_accounts", return_value=()),
            mock.patch.object(orichum_cli, "validate_account_bindings"),
            mock.patch.object(
                orichum_cli,
                "_live_models",
                return_value=frozenset(),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_session_plan",
                return_value=plan,
            ) as resolve,
            mock.patch.object(orichum_cli, "_validate_plan_routes"),
            mock.patch.object(orichum_cli, "_validate_live_models"),
            mock.patch.object(
                orichum_cli,
                "create_resolved_session",
                return_value=physical,
            ),
            mock.patch.object(
                orichum_cli,
                "create_logical_session",
                return_value=logical,
            ),
        ):
            prepared = orichum_cli._prepare_new_session(
                paths,
                config,
                launch_dir=launch,
            )

        self.assertIs(prepared.logical, logical)
        self.assertIs(prepared.physical, physical)
        self.assertIs(resolve.call_args.args[0], session_documents)
        self.assertEqual(
            resolve.call_args.kwargs["requested_stack"],
            "repository-local-test",
        )
        self.assertIs(resolve.call_args.kwargs["bindings"], bindings)

    def test_models_resolve_reports_project_source_and_explicit_stack_bypasses_it(
        self,
    ) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        launch = project / "src"
        launch.mkdir(parents=True)
        directory = project / ".orichum"
        directory.mkdir()
        source = directory / "models.json"
        source.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "controller": "gpt-5.6-sol",
                    "agents": {
                        role: "gpt-5.6-sol" for role in orichum_cli.ROLES
                    },
                }
            ),
            encoding="utf-8",
        )
        config = SimpleNamespace(
            documents={
                "model-stacks": serialize_model_stacks(snapshot.stacks),
                "projects": {},
            }
        )
        context = {
            "launchDirReal": str(launch),
            "route": {"contextRootReal": str(project)},
        }

        with mock.patch.object(
            orichum_cli,
            "resolve_control_plane_context",
            return_value=context,
        ):
            project_result = orichum_cli._resolve_stack(
                config,
                None,
                launch_dir=launch,
            )
            explicit_result = orichum_cli._resolve_stack(
                config,
                "balanced",
                launch_dir=launch,
            )

        self.assertEqual(project_result["source"], str(source))
        self.assertTrue(
            str(project_result["stack"]).startswith("repository-local-")
        )
        self.assertEqual(explicit_result["stack"], "balanced")
        self.assertNotIn("source", explicit_result)

    def test_setup_readiness_rejects_invalid_project_model_mapping(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        project.mkdir()
        directory = project / ".orichum"
        directory.mkdir()
        (directory / "config.json").write_text("{", encoding="utf-8")
        config = SimpleNamespace(
            documents={
                "model-stacks": serialize_model_stacks(snapshot.stacks),
                "projects": {},
            }
        )

        with (
            mock.patch.object(orichum_cli, "_verify_runtime"),
            mock.patch.object(
                orichum_cli,
                "resolve_project_context",
                side_effect=orichum_cli.RoutingError("invalid JSON"),
            ),
        ):
            ready = orichum_cli._setup_project_ready(
                {"config": self.root / "config"},
                config,
                project,
            )

        self.assertFalse(ready)

    def test_project_model_mapping_blocks_configure_model_writes_and_drift(
        self,
    ) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        launch = project / "src"
        launch.mkdir(parents=True)
        directory = project / ".orichum"
        directory.mkdir()
        path = directory / "config.json"
        document = {
            "schemaVersion": 1,
            "controller": "gpt-5.6-sol",
            "agents": {
                role: "gpt-5.6-sol" for role in orichum_cli.ROLES
            },
            "jiraProfile": None,
            "githubAccount": "alupao",
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        base_document = serialize_model_stacks(snapshot.stacks)
        project_models = orichum_cli.discover_project_models(
            launch,
            project,
            snapshot.stacks,
        )
        assert project_models is not None
        local = replace(
            snapshot,
            target=replace(
                snapshot.target,
                root=project,
                stack_name=project_models.stack_name,
            ),
            stacks=project_models.stacks,
            bindings=StackBindings({}),
            launch_root=launch,
            project_models_path=project_models.path,
            project_models_digest=project_models.digest,
            project_models_checked=True,
            project_services_managed=True,
        )
        config = SimpleNamespace(documents={"model-stacks": base_document})
        changed = ConfigurationDraft.from_snapshot(local).with_project(
            replace(local.target, stack_name="balanced")
        )

        with self.assertRaisesRegex(orichum_cli.CliError, "edit that JSON file"):
            orichum_cli._configuration_model_changes(config, local, changed)

        path.write_text(
            json.dumps({**document, "controller": "gpt-5.6-sol"}, indent=2),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(orichum_cli.CliError, "changed while"):
            orichum_cli._configuration_model_changes(
                config,
                local,
                ConfigurationDraft.from_snapshot(local),
            )

    def test_project_model_mapping_appearing_during_configure_is_rejected(
        self,
    ) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        launch = project / "src"
        launch.mkdir(parents=True)
        opened = replace(
            snapshot,
            target=replace(snapshot.target, root=project),
            launch_root=launch,
            project_models_checked=True,
        )
        config = SimpleNamespace(
            documents={
                "model-stacks": serialize_model_stacks(snapshot.stacks),
                "projects": {},
            }
        )
        directory = project / ".orichum"
        directory.mkdir()
        (directory / "models.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "controller": "gpt-5.6-sol",
                    "agents": {
                        role: "gpt-5.6-sol" for role in orichum_cli.ROLES
                    },
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(orichum_cli.CliError, "changed while"):
            orichum_cli._configuration_model_changes(
                config,
                opened,
                ConfigurationDraft.from_snapshot(opened),
            )

    def test_orichum_home_can_be_overridden_as_one_unit(self) -> None:
        home = self.root / "private-orichum"

        paths = orichum_cli._paths(
            {
                "HOME": str(self.root / "home"),
                "ORICHUM_HOME": str(home),
            }
        )

        self.assertEqual(paths["home"], home)
        self.assertEqual(paths["data"], home)
        self.assertEqual(paths["config"], home / "config")
        self.assertEqual(paths["cache"], home / "cache")
        self.assertEqual(paths["state"], home / "state")

    def test_fine_grained_path_overrides_remain_available(self) -> None:
        paths = orichum_cli._paths(
            {
                "HOME": str(self.root / "home"),
                "ORICHUM_HOME": str(self.root / "orichum"),
                "ORICHUM_DATA_HOME": str(self.root / "data"),
                "ORICHUM_CONFIG_HOME": str(self.root / "config"),
                "ORICHUM_CACHE_HOME": str(self.root / "cache"),
            }
        )

        self.assertEqual(paths["home"], self.root / "orichum")
        self.assertEqual(paths["data"], self.root / "data")
        self.assertEqual(paths["config"], self.root / "config")
        self.assertEqual(paths["cache"], self.root / "cache")
        self.assertEqual(paths["state"], self.root / "data" / "state")

    def test_config_validate_paths_and_redacted_show(self) -> None:
        status, stdout, stderr = self.run_cli("config", "validate")
        self.assertEqual((status, stdout, stderr), (0, "", ""))

        status, stdout, stderr = self.run_cli("config", "paths")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout),
            {
                "cache": str(self.root / "cache"),
                "config": str(REPOSITORY_ROOT / "config"),
                "data": str(self.root / "data"),
                "home": str(self.root / "orichum"),
                "state": str(self.root / "data" / "state"),
            },
        )
        self.assertFalse((self.root / "data").exists())

        status, stdout, stderr = self.run_cli("config", "show")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        shown = json.loads(stdout)
        self.assertEqual(
            shown["controller-policy"]["value"], "<policy omitted>"
        )
        self.assertEqual(
            shown["model-stacks"]["source"], "config/model-stacks.json"
        )
        self.assertNotIn("secret", stdout.lower())
        self.assertNotIn("authorization:", stdout.lower())

        status, stdout, stderr = self.run_cli("config", "show", "--raw")
        self.assertEqual((status, stderr), (0, ""))
        raw = json.loads(stdout)
        self.assertNotEqual(raw["controller-policy"]["value"], "<policy omitted>")

    def test_config_validate_rejects_stale_managed_controller_policy(
        self,
    ) -> None:
        config_home = self.root / "config"
        shutil.copytree(REPOSITORY_ROOT / "config", config_home)
        policy = config_home / "controller-policy.md"
        policy.write_text("# stale policy\n", encoding="utf-8")
        policy.chmod(0o600)
        self.environment["ORICHUM_CONFIG_HOME"] = str(config_home)

        status, stdout, stderr = self.run_cli("config", "validate")

        self.assertEqual((status, stdout), (2, ""))
        self.assertEqual(
            stderr,
            "ERROR: installed controller policy is stale; "
            "rerun install.sh\n",
        )

    def test_version_uses_release_identity_file(self) -> None:
        output = self.version_output({
            "schemaVersion": 1,
            "version": "0.1.0-rc.12",
            "sourceKind": "git",
            "sourceCommit": "b118c9f5f8e3e9e822be10552184b7e1b1c2cbba",
            "dirty": False,
            "exactTag": True,
        })

        self.assertEqual(output, "Orichum 0.1.0-rc.12\n")

    def test_version_identifies_clean_git_development_build(self) -> None:
        output = self.version_output({
            "schemaVersion": 1,
            "version": "0.1.0-rc.12",
            "sourceKind": "git",
            "sourceCommit": "b118c9f5f8e3e9e822be10552184b7e1b1c2cbba",
            "dirty": False,
            "exactTag": False,
        })

        self.assertEqual(
            output,
            "Orichum 0.1.0-rc.12+g.b118c9f5f8e3\n",
        )

    def test_version_identifies_dirty_git_development_build(self) -> None:
        output = self.version_output({
            "schemaVersion": 1,
            "version": "0.1.0-rc.12",
            "sourceKind": "git",
            "sourceCommit": "b118c9f5f8e3e9e822be10552184b7e1b1c2cbba",
            "dirty": True,
            "exactTag": True,
        })

        self.assertEqual(
            output,
            "Orichum 0.1.0-rc.12+g.b118c9f5f8e3.dirty\n",
        )

    def test_version_uses_manifest_digest_without_git(self) -> None:
        output = self.version_output({
            "schemaVersion": 1,
            "version": "0.1.0-rc.12",
            "sourceKind": "source",
            "sourceCommit": None,
            "dirty": False,
            "exactTag": False,
        })

        self.assertEqual(
            output,
            "Orichum 0.1.0-rc.12+src.8d8406645ad4\n",
        )

    def test_version_missing_or_invalid_identity_never_looks_released(
        self,
    ) -> None:
        for identity, digest, expected in (
            (None, "8" * 64, "Orichum 0.1.0-rc.12+src.888888888888\n"),
            ("not json\n", "9" * 64, "Orichum 0.1.0-rc.12+src.999999999999\n"),
            ({
                "schemaVersion": True,
                "version": "0.1.0-rc.12",
                "sourceKind": "git",
                "sourceCommit": "b118c9f5f8e3e9e822be10552184b7e1b1c2cbba",
                "dirty": False,
                "exactTag": True,
            }, "a" * 64, "Orichum 0.1.0-rc.12+src.aaaaaaaaaaaa\n"),
            (None, None, "Orichum 0.1.0-rc.12+src.unknown\n"),
        ):
            with self.subTest(identity=identity, digest=digest):
                self.assertEqual(
                    self.version_output(identity, digest),
                    expected,
                )

    def test_help_explains_top_level_commands(self) -> None:
        help_text = orichum_cli.build_parser().format_help().casefold()

        self.assertIn("complete first-run setup", help_text)
        self.assertIn("start a project-aware session", help_text)
        self.assertIn("manage provider accounts", help_text)
        self.assertIn("inspect and monitor leanctx", help_text)
        self.assertIn("inspect and clean sessions", help_text)

    def test_every_public_command_has_complete_help_metadata(self) -> None:
        def walk(
            parser: argparse.ArgumentParser,
            path: tuple[str, ...],
        ) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
            found = [(path, parser)]
            for action in parser._actions:
                if not isinstance(action, argparse._SubParsersAction):
                    continue
                for name, child in action.choices.items():
                    found.extend(walk(child, (*path, name)))
            return found

        for path, parser in walk(orichum_cli.build_parser(), ("orichum",)):
            with self.subTest(command=" ".join(path)):
                self.assertTrue(parser.description)
                for action in parser._actions:
                    if isinstance(
                        action,
                        (argparse._HelpAction, argparse._SubParsersAction),
                    ):
                        continue
                    self.assertNotIn(action.help, (None, argparse.SUPPRESS))
                    if action.choices is not None:
                        continue
                    if action.option_strings and action.nargs == 0:
                        continue
                    self.assertIsNotNone(action.metavar)

    def test_every_public_command_path_renders_help(self) -> None:
        parser = orichum_cli.build_parser()

        def paths(
            current: argparse.ArgumentParser,
            prefix: tuple[str, ...] = (),
        ) -> list[tuple[str, ...]]:
            found = [prefix]
            for action in current._actions:
                if not isinstance(action, argparse._SubParsersAction):
                    continue
                for name, child in action.choices.items():
                    found.extend(paths(child, (*prefix, name)))
            return found

        for path in paths(parser):
            with self.subTest(command=" ".join(("orichum", *path))):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with contextlib.redirect_stdout(
                    stdout
                ), contextlib.redirect_stderr(
                    stderr
                ), self.assertRaises(SystemExit) as raised:
                    parser.parse_args([*path, "--help"])

                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertIn("usage:", stdout.getvalue())

    def test_run_and_fork_parse_leanctx_profiles(self) -> None:
        parser = orichum_cli.build_parser()

        default_run = parser.parse_args(["run"])
        full_run = parser.parse_args(
            ["run", "--leanctx-profile", "full", "review", "this"]
        )
        inherited_fork = parser.parse_args(
            ["fork", "oc-s-0000000000000001"]
        )
        lean_fork = parser.parse_args(
            [
                "fork",
                "oc-s-0000000000000001",
                "--leanctx-profile",
                "lean",
            ]
        )

        self.assertEqual(default_run.leanctx_profile, "lean")
        self.assertEqual(full_run.leanctx_profile, "full")
        self.assertEqual(full_run.arguments, ["review", "this"])
        self.assertIsNone(inherited_fork.leanctx_profile)
        self.assertEqual(lean_fork.leanctx_profile, "lean")


    def test_context_models_provider_and_plugin_read_only_commands(self) -> None:
        status, stdout, stderr = self.run_cli("context", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("ACCOUNT POOLS", stdout)
        self.assertNotIn("~/xebia", stdout)

        status, stdout, stderr = self.run_cli("models", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("gpt-5.6-sol", stdout)
        self.assertIn("openai", stdout)

        status, stdout, stderr = self.run_cli("models", "validate")
        self.assertEqual((status, stdout, stderr), (0, "", ""))

        status, stdout, stderr = self.run_cli("models", "resolve")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        resolved = json.loads(stdout)
        self.assertEqual(resolved["stack"], "balanced")
        self.assertEqual(resolved["controller"], "gpt-5.6-sol")

        status, stdout, stderr = self.run_cli("models", "stacks")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("STACK", stdout)
        self.assertIn("DEFAULT", stdout)
        self.assertIn("balanced", stdout)
        self.assertIn("gpt-5.6-sol", stdout)

        status, stdout, stderr = self.run_cli(
            "models", "resolve", "missing-stack"
        )
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: model stack is not configured: missing-stack; "
            "available stacks: balanced\n",
        )

        status, stdout, stderr = self.run_cli("provider", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("antigravity", stdout)
        self.assertIn("openai-compatible", stdout)

    def test_stack_available_is_read_only_and_redacts_route_metadata(self) -> None:
        accounts = (
            orichum_cli.Account(
                id="oc-a-1111111111111111",
                name="Work Claude",
                provider="anthropic",
                credential_ref="claude-work.json",
                pool="shared",
                routing_prefix="oc-r-1111111111111111",
                priority=100,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
            orichum_cli.Account(
                id="oc-a-2222222222222222",
                name="Antigravity",
                provider="antigravity",
                credential_ref="antigravity-work.json",
                pool="shared",
                routing_prefix="oc-r-2222222222222222",
                priority=50,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
        )
        raw = {
            "object": "list",
            "data": [
                {
                    "id": (
                        "oc-r-1111111111111111/"
                        "claude-sonnet-5"
                    )
                },
                {
                    "id": (
                        "oc-r-2222222222222222/"
                        "future-model"
                    )
                },
            ],
        }

        with (
            mock.patch.object(orichum_cli, "_verify_runtime") as verify,
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={
                    "claudexProxyPort": 13457,
                    "cliproxyPort": 8317,
                    "routeProxyPort": 13456,
                },
            ),
            mock.patch.object(
                orichum_cli, "load_accounts", return_value=accounts
            ),
            mock.patch.object(
                orichum_cli, "fetch_live_catalog", return_value=raw
            ) as fetch,
        ):
            status, stdout, stderr = self.run_cli(
                "stack", "available"
            )

        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        verify.assert_called_once()
        fetch.assert_called_once_with(8317)
        self.assertIn("PROVIDER", stdout)
        self.assertIn("FAMILY", stdout)
        self.assertIn("MODEL", stdout)
        self.assertIn("ACCOUNTS", stdout)
        self.assertIn("STATUS", stdout)
        self.assertIn("anthropic", stdout)
        self.assertIn("claude-sonnet-5", stdout)
        self.assertIn("Work Claude", stdout)
        self.assertIn("future-model", stdout)
        self.assertIn("unclassified", stdout)
        self.assertIn("not selectable", stdout)
        self.assertNotIn("oc-r-", stdout)
        self.assertNotIn("oc-a-", stdout)
        self.assertNotIn("claude-work.json", stdout)
        self.assertNotIn("antigravity-work.json", stdout)

    def test_stack_list_and_show_are_scriptable_and_redacted(self) -> None:
        status, stdout, stderr = self.run_cli("stack", "list")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("STACK", stdout)
        self.assertIn("balanced", stdout)

        status, stdout, stderr = self.run_cli(
            "stack", "show", "balanced"
        )
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("architecture-advisor", stdout)
        self.assertIn("claude-opus-5", stdout)
        self.assertIn("claude-opus-4-6-thinking", stdout)
        self.assertIn("anthropic", stdout)
        self.assertIn("antigravity", stdout)
        self.assertIn("Automatic within provider", stdout)
        self.assertNotIn("oc-a-", stdout)
        self.assertNotIn("oc-c-", stdout)
        self.assertNotIn("oc-r-", stdout)
        self.assertNotIn(".json", stdout)

    def test_stack_configure_rejects_non_tty_before_wizard_dispatch(
        self,
    ) -> None:
        with mock.patch.object(
            orichum_cli, "run_stack_wizard", return_value=0
        ) as wizard:
            status, stdout, stderr = self.run_cli(
                "stack", "configure"
            )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: stack configuration requires an interactive terminal\n",
        )
        wizard.assert_not_called()

    def test_stack_configure_verifies_full_runtime_before_wizard(
        self,
    ) -> None:
        paths = {"data": self.root / "data"}
        config = object()
        with (
            mock.patch.object(
                orichum_cli, "_interactive_terminal", return_value=True
            ),
            mock.patch.object(
                orichum_cli, "_load", return_value=(paths, config)
            ),
            mock.patch.object(orichum_cli, "_verify_runtime") as verify,
            mock.patch.object(
                orichum_cli, "run_stack_wizard", return_value=0
            ) as wizard,
        ):
            status = orichum_cli.main(["stack", "configure"])

        self.assertEqual(status, 0)
        verify.assert_called_once_with(paths)
        wizard.assert_called_once_with(
            paths,
            config,
            launch_dir=Path.cwd(),
        )

    def test_provider_configure_rejects_non_tty_before_login(self) -> None:
        with mock.patch.object(
            orichum_cli, "_run_external", return_value=0
        ) as run:
            status, stdout, stderr = self.run_cli(
                "provider", "configure"
            )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: provider configuration requires an interactive terminal\n",
        )
        run.assert_not_called()

    def test_setup_rejects_non_tty_before_mutation(self) -> None:
        with mock.patch.object(
            orichum_cli, "_run_external", return_value=0
        ) as run:
            status, stdout, stderr = self.run_cli("setup")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "ERROR: setup requires an interactive terminal\n",
        )
        run.assert_not_called()

    def test_setup_accepts_verbose_diagnostics(self) -> None:
        parser = orichum_cli.build_parser()

        self.assertTrue(parser.parse_args(["setup", "--verbose"]).verbose)

    def test_setup_diagnostics_are_private_bounded_and_redacted(self) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )
        callback = (
            "http://localhost:1455/auth/callback?code=secret-code"
            "&state=secret-state"
        )
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = diagnostics.run_command(
                [
                    sys.executable,
                    "-c",
                    (
                        "print('callback " + callback + "')\n"
                        "print('Authorization: Bearer secret-token')\n"
                        "print('Authorization: Token header-secret')\n"
                        "print('{\"accessToken\":\"camel-secret\",'"
                        "      '\"client_secret\":\"client-secret\"}')\n"
                        "print('API_KEY=environment-secret')"
                    ),
                ],
                cwd=self.root,
            )
        diagnostics.close()

        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(diagnostics.path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(diagnostics.path.stat().st_mode & 0o777, 0o600)
        logged = diagnostics.path.read_text(encoding="utf-8")
        self.assertIn(callback, logged)
        self.assertIn("secret-code", logged)
        self.assertIn("secret-state", logged)
        self.assertIn("secret-token", logged)
        self.assertIn("header-secret", logged)
        self.assertIn("camel-secret", logged)
        self.assertIn("client-secret", logged)
        self.assertIn("environment-secret", logged)

    def test_setup_diagnostics_retain_noisy_newline_free_children(
        self,
    ) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )

        status = diagnostics.run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 8192)",
            ],
            cwd=self.root,
        )
        diagnostics.close()

        self.assertEqual(status, 0)
        logged = diagnostics.path.read_text(encoding="utf-8")
        self.assertEqual(logged, "x" * 8192)

    def test_managed_provider_login_prints_owned_progress_without_secrets(
        self,
    ) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )
        session = SimpleNamespace(
            url="https://auth.openai.com/oauth?state=secret-state",
            state="secret-state",
        )
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "", "SSH_TTY": ""},
                clear=False,
            ),
            mock.patch.object(
                orichum_cli, "load_management_endpoint", return_value=object()
            ),
            mock.patch.object(
                orichum_cli, "start_oauth", return_value=session
            ),
            mock.patch.object(
                orichum_cli, "oauth_status", side_effect=("wait", "ok")
            ),
            mock.patch.object(orichum_cli.webbrowser, "open", return_value=True),
            mock.patch.object(orichum_cli.time, "sleep"),
            contextlib.redirect_stdout(stdout),
        ):
            status = orichum_cli._managed_provider_login(
                {"data": data},
                "codex",
                "OpenAI",
                diagnostics,
            )
        diagnostics.close()

        self.assertEqual(status, 0)
        self.assertEqual(
            stdout.getvalue(),
            "OpenAI authentication\n"
            "  Open this URL:\n"
            f"    {session.url}\n"
            "  Opening your browser…\n"
            "  Waiting for authentication…\n"
            "  ✓ Signed in\n",
        )
        logged = diagnostics.path.read_text(encoding="utf-8")
        self.assertNotIn(session.url, logged)
        self.assertNotIn(session.state, logged)

    def test_managed_provider_login_prints_url_when_browser_does_not_open(
        self,
    ) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )
        endpoint = object()
        session = SimpleNamespace(
            url="https://auth.openai.com/oauth?state=secret-state",
            state="secret-state",
        )
        stdout = io.StringIO()
        callback = (
            "http://localhost:1455/auth/callback?"
            "code=secret-code&state=secret-state"
        )
        with (
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "", "SSH_TTY": ""},
                clear=False,
            ),
            mock.patch.object(
                orichum_cli,
                "load_management_endpoint",
                return_value=endpoint,
            ),
            mock.patch.object(
                orichum_cli, "start_oauth", return_value=session
            ),
            mock.patch.object(
                orichum_cli, "oauth_status", side_effect=("wait", "ok")
            ),
            mock.patch.object(orichum_cli, "cancel_oauth") as cancel,
            mock.patch.object(
                orichum_cli, "submit_oauth_callback"
            ) as submit,
            mock.patch.object(
                orichum_cli.webbrowser, "open", return_value=False
            ),
            mock.patch("builtins.input", return_value=callback),
            mock.patch.object(orichum_cli.time, "sleep"),
            contextlib.redirect_stdout(stdout),
        ):
            status = orichum_cli._managed_provider_login(
                {"data": data},
                "codex",
                "OpenAI",
                diagnostics,
            )
        diagnostics.close()

        self.assertEqual(status, 0)
        output = stdout.getvalue()
        self.assertIn("  Browser did not open automatically.\n", output)
        self.assertIn(f"    {session.url}\n", output)
        self.assertLess(
            output.index(session.url),
            output.index("  Waiting for authentication…"),
        )
        submit.assert_called_once_with(endpoint, session.state, callback)
        cancel.assert_not_called()
        logged = diagnostics.path.read_text(encoding="utf-8")
        self.assertNotIn(session.url, logged)
        self.assertNotIn(session.state, logged)
        self.assertNotIn(callback, logged)

    def test_managed_provider_login_recovers_from_browser_launcher_errors(
        self,
    ) -> None:
        for browser_error in (
            OSError("browser launcher unavailable"),
            orichum_cli.webbrowser.Error("no runnable browser"),
        ):
            with self.subTest(browser_error=type(browser_error).__name__):
                data = self.root / type(browser_error).__name__
                data.mkdir(mode=0o700)
                diagnostics = orichum_cli.SetupDiagnostics.create(
                    {"data": data},
                    verbose=False,
                )
                endpoint = object()
                session = SimpleNamespace(
                    url=(
                        "https://auth.openai.com/oauth?"
                        "state=secret-state"
                    ),
                    state="secret-state",
                )
                stdout = io.StringIO()
                callback = (
                    "http://localhost:1455/auth/callback?"
                    "code=secret-code&state=secret-state"
                )
                with (
                    mock.patch.dict(
                        os.environ,
                        {"SSH_CONNECTION": "", "SSH_TTY": ""},
                        clear=False,
                    ),
                    mock.patch.object(
                        orichum_cli,
                        "load_management_endpoint",
                        return_value=endpoint,
                    ),
                    mock.patch.object(
                        orichum_cli, "start_oauth", return_value=session
                    ),
                    mock.patch.object(
                        orichum_cli,
                        "oauth_status",
                        side_effect=("wait", "ok"),
                    ),
                    mock.patch.object(
                        orichum_cli, "cancel_oauth"
                    ) as cancel,
                    mock.patch.object(
                        orichum_cli, "submit_oauth_callback"
                    ) as submit,
                    mock.patch.object(
                        orichum_cli.webbrowser,
                        "open",
                        side_effect=browser_error,
                    ),
                    mock.patch("builtins.input", return_value=callback),
                    mock.patch.object(orichum_cli.time, "sleep"),
                    contextlib.redirect_stdout(stdout),
                ):
                    status = orichum_cli._managed_provider_login(
                        {"data": data},
                        "codex",
                        "OpenAI",
                        diagnostics,
                    )
                diagnostics.close()

                self.assertEqual(status, 0)
                output = stdout.getvalue()
                self.assertIn(session.url, output)
                self.assertIn("  Waiting for authentication…", output)
                submit.assert_called_once_with(
                    endpoint,
                    session.state,
                    callback,
                )
                cancel.assert_not_called()
                logged = diagnostics.path.read_text(encoding="utf-8")
                self.assertNotIn(session.url, logged)
                self.assertNotIn(session.state, logged)
                self.assertNotIn(callback, logged)

    def test_managed_provider_login_uses_callback_paste_over_ssh(
        self,
    ) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )
        endpoint = object()
        session = SimpleNamespace(
            url="https://auth.openai.com/oauth?state=secret-state",
            state="secret-state",
        )
        callback = (
            "http://localhost:1455/auth/callback?"
            "code=secret-code&state=secret-state"
        )
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "client 1 server 22"},
                clear=False,
            ),
            mock.patch.object(
                orichum_cli,
                "load_management_endpoint",
                return_value=endpoint,
            ),
            mock.patch.object(
                orichum_cli, "start_oauth", return_value=session
            ),
            mock.patch.object(
                orichum_cli, "oauth_status", return_value="ok"
            ),
            mock.patch.object(
                orichum_cli, "submit_oauth_callback"
            ) as submit,
            mock.patch.object(orichum_cli.webbrowser, "open") as browser,
            mock.patch("builtins.input", return_value=callback),
            contextlib.redirect_stdout(stdout),
        ):
            status = orichum_cli._managed_provider_login(
                {"data": data},
                "codex",
                "OpenAI",
                diagnostics,
            )
        diagnostics.close()

        self.assertEqual(status, 0)
        browser.assert_not_called()
        submit.assert_called_once_with(endpoint, session.state, callback)
        output = stdout.getvalue()
        self.assertIn(session.url, output)
        self.assertIn("SSH session detected", output)
        logged = diagnostics.path.read_text(encoding="utf-8")
        self.assertNotIn(session.url, logged)
        self.assertNotIn(callback, logged)

    def test_managed_provider_login_cancels_when_browser_launch_is_cancelled(
        self,
    ) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )
        endpoint = object()
        session = SimpleNamespace(
            url="https://auth.openai.com/oauth?state=secret-state",
            state="secret-state",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "", "SSH_TTY": ""},
                clear=False,
            ),
            mock.patch.object(
                orichum_cli,
                "load_management_endpoint",
                return_value=endpoint,
            ),
            mock.patch.object(
                orichum_cli, "start_oauth", return_value=session
            ),
            mock.patch.object(orichum_cli, "cancel_oauth") as cancel,
            mock.patch.object(
                orichum_cli.webbrowser,
                "open",
                side_effect=KeyboardInterrupt,
            ),
            self.assertRaisesRegex(orichum_cli.CliError, "setup cancelled"),
        ):
            orichum_cli._managed_provider_login(
                {"data": data},
                "codex",
                "OpenAI",
                diagnostics,
            )
        diagnostics.close()

        cancel.assert_called_once_with(endpoint, session.state)
        logged = diagnostics.path.read_text(encoding="utf-8")
        self.assertNotIn(session.url, logged)
        self.assertNotIn(session.state, logged)

    def test_managed_provider_login_cancels_failed_session(self) -> None:
        data = self.root / "data"
        data.mkdir(mode=0o700)
        diagnostics = orichum_cli.SetupDiagnostics.create(
            {"data": data},
            verbose=False,
        )
        endpoint = object()
        session = SimpleNamespace(
            url="https://auth.openai.com/oauth?state=secret-state",
            state="secret-state",
        )
        with (
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "", "SSH_TTY": ""},
                clear=False,
            ),
            mock.patch.object(
                orichum_cli,
                "load_management_endpoint",
                return_value=endpoint,
            ),
            mock.patch.object(
                orichum_cli, "start_oauth", return_value=session
            ),
            mock.patch.object(
                orichum_cli,
                "oauth_status",
                side_effect=orichum_cli.ManagementError("failed"),
            ),
            mock.patch.object(orichum_cli, "cancel_oauth") as cancel,
            mock.patch.object(orichum_cli.webbrowser, "open", return_value=True),
            self.assertRaises(orichum_cli.ManagementError),
        ):
            orichum_cli._managed_provider_login(
                {"data": data},
                "codex",
                "OpenAI",
                diagnostics,
            )
        diagnostics.close()

        cancel.assert_called_once_with(endpoint, session.state)

    def test_setup_project_config_preserves_named_jira_profile(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        project.mkdir()
        config = SimpleNamespace(
            documents={
                "model-stacks": serialize_model_stacks(snapshot.stacks),
                "projects": {},
            }
        )
        route = {
            "modelStack": snapshot.stacks.default_stack,
            "atlassianConfigured": True,
            "jiraProfile": "work",
            "githubAccount": "alupao",
        }

        with (
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={"route": route},
            ),
            mock.patch.object(
                orichum_cli,
                "ensure_project_config",
                return_value=(project / ".orichum" / "config.json", True),
            ) as ensure,
        ):
            orichum_cli._ensure_setup_project_config(config, project)

        ensure.assert_called_once_with(
            project,
            snapshot.stacks.stacks[snapshot.stacks.default_stack],
            jira_profile="work",
            github_account="alupao",
        )

    def test_setup_project_config_rejects_legacy_inline_jira(self) -> None:
        from tests.test_configure_state import _snapshot

        snapshot = _snapshot()
        project = self.root / "project"
        project.mkdir()
        config = SimpleNamespace(
            documents={
                "model-stacks": serialize_model_stacks(snapshot.stacks),
                "projects": {},
            }
        )

        with (
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {
                        "modelStack": snapshot.stacks.default_stack,
                        "atlassianConfigured": True,
                        "githubAccount": None,
                    }
                },
            ),
            self.assertRaisesRegex(
                orichum_cli.CliError,
                "legacy project context",
            ),
        ):
            orichum_cli._ensure_setup_project_config(config, project)

    def test_setup_runs_only_missing_phases_and_verifies_project(self) -> None:
        project = self.root / "project"
        project.mkdir()
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(documents={"projects": {"contexts": []}})
        refreshed = SimpleNamespace(
            documents={"projects": {"contexts": [{"root": str(project)}]}}
        )

        with (
            mock.patch.object(
                orichum_cli,
                "_active_provider_accounts",
                side_effect=(
                    (),
                    (
                        SimpleNamespace(
                            name="Work Claude",
                            pool="xebia",
                            priority=100,
                        ),
                    ),
                ),
            ),
            mock.patch.object(
                orichum_cli, "_provider_configure", return_value=0
            ) as provider,
            mock.patch.object(
                orichum_cli, "_runtime_ready", return_value=False
            ),
            mock.patch.object(
                orichum_cli, "_reconcile_runtime", return_value=0
            ) as reconcile,
            mock.patch.object(
                orichum_cli, "_load", return_value=(paths, refreshed)
            ),
            mock.patch.object(
                orichum_cli,
                "_project_context_mapped",
                side_effect=(False, True),
            ),
            mock.patch.object(
                orichum_cli,
                "_ensure_setup_project_config",
                return_value=(project / ".orichum" / "config.json", True),
            ) as ensure_config,
            mock.patch.object(
                orichum_cli,
                "_setup_project_ready",
                side_effect=(False, True, True),
            ) as ready,
            mock.patch.object(
                orichum_cli,
                "create_recommended_stack",
                return_value="recommended",
            ) as recommended,
            mock.patch.object(
                orichum_cli, "_run_external", return_value=0
            ) as external,
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                status = orichum_cli._setup(paths, config, str(project))

        self.assertEqual(status, 0)
        provider.assert_called_once_with(
            paths,
            config,
            onboarding=True,
            diagnostics=mock.ANY,
        )
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(
            reconcile.call_args_list,
            [mock.call(mock.ANY), mock.call(mock.ANY)],
        )
        external.assert_has_calls(
            (
                mock.call(
                    "orichum-context",
                    ["add", str(project), "--pool", "xebia"],
                    diagnostics=mock.ANY,
                ),
                mock.call(
                    "orichum-doctor", [], diagnostics=mock.ANY
                ),
            )
        )
        recommended.assert_called_once_with(
            paths, refreshed, launch_dir=project
        )
        ensure_config.assert_called_once_with(refreshed, project)
        self.assertEqual(ready.call_count, 3)
        output = stdout.getvalue()
        self.assertIn("Models\n  ✓ Recommended stack created", output)
        self.assertIn("  File: ", output)
        self.assertIn("  ✓ Project configuration created", output)
        self.assertTrue(output.endswith("Orichum is ready.\n"))

    def test_setup_user_configures_normal_scope_from_active_account_pools(
        self,
    ) -> None:
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(
            documents={
                "projects": {"schemaVersion": 2, "normal": None, "contexts": []},
                "model-stacks": object(),
                "providers": {"accountPools": {"personal": {}}},
            }
        )
        refreshed = SimpleNamespace(
            documents={
                "projects": {
                    "schemaVersion": 2,
                    "normal": {"modelStack": "recommended", "accountPools": ["personal"]},
                    "contexts": [],
                }
            }
        )
        account = SimpleNamespace(
            name="Personal GPT", pool="personal", priority=100
        )

        with (
            mock.patch.object(
                orichum_cli, "_active_provider_accounts", return_value=(account,)
            ),
            mock.patch.object(orichum_cli, "_runtime_ready", return_value=True),
            mock.patch.object(
                orichum_cli,
                "normalize_model_stacks",
                return_value=SimpleNamespace(stacks={"balanced": object()}),
            ),
            mock.patch.object(
                orichum_cli, "configure_normal_scope"
            ) as configure,
            mock.patch.object(
                orichum_cli, "_load", return_value=(paths, refreshed)
            ),
            mock.patch.object(
                orichum_cli, "_setup_normal_ready", side_effect=(False, True)
            ),
            mock.patch.object(
                orichum_cli, "create_recommended_stack"
            ) as recommended,
            mock.patch.object(
                orichum_cli, "_reconcile_runtime", return_value=0
            ) as reconcile,
            mock.patch.object(
                orichum_cli, "_run_external", return_value=0
            ) as external,
        ):
            status = orichum_cli._setup(paths, config, None, normal_scope=True)

        self.assertEqual(status, 0)
        configure.assert_called_once_with(
            paths["config"] / "projects.json",
            model_stack=None,
            account_pools=("personal",),
            known_stacks={"balanced": mock.ANY},
            known_pools={"personal": {}},
        )
        recommended.assert_called_once_with(
            paths, refreshed, launch_dir=Path.home()
        )
        reconcile.assert_called_once_with(mock.ANY)
        external.assert_called_once_with(
            "orichum-doctor", [], diagnostics=mock.ANY
        )

    def test_setup_user_rejects_project_path_before_any_setup_work(self) -> None:
        with mock.patch.object(orichum_cli.SetupDiagnostics, "create") as create:
            with self.assertRaisesRegex(
                orichum_cli.CliError, "setup --user does not accept a project path"
            ):
                orichum_cli._setup(
                    {},
                    SimpleNamespace(),
                    str(self.root / "project"),
                    normal_scope=True,
                )

        create.assert_not_called()

    def test_setup_reuses_completed_account_runtime_context_and_stack(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(
            documents={"projects": {"contexts": [{"root": str(project)}]}}
        )

        stdout = io.StringIO()
        with (
            mock.patch.object(
                orichum_cli,
                "_active_provider_accounts",
                return_value=(
                    SimpleNamespace(
                        name="Personal GPT",
                        pool="shared",
                        priority=100,
                    ),
                ),
            ),
            mock.patch.object(
                orichum_cli, "_provider_configure", return_value=0
            ) as provider,
            mock.patch.object(
                orichum_cli, "_runtime_ready", return_value=True
            ),
            mock.patch.object(
                orichum_cli, "_reconcile_runtime", return_value=0
            ) as reconcile,
            mock.patch.object(
                orichum_cli, "_project_context_mapped", return_value=True
            ),
            mock.patch.object(
                orichum_cli, "_setup_project_ready", return_value=True
            ),
            mock.patch.object(
                orichum_cli,
                "_ensure_setup_project_config",
                return_value=(project / ".orichum" / "config.json", False),
            ),
            mock.patch.object(
                orichum_cli,
                "_load",
                side_effect=AssertionError("completed setup must not reload"),
            ) as load,
            mock.patch.object(
                orichum_cli, "run_stack_wizard", return_value=0
            ) as wizard,
            mock.patch.object(
                orichum_cli, "_run_external", return_value=0
            ) as external,
            contextlib.redirect_stdout(stdout),
        ):
            status = orichum_cli._setup(paths, config, str(project))

        self.assertEqual(status, 0)
        provider.assert_not_called()
        reconcile.assert_not_called()
        load.assert_not_called()
        wizard.assert_not_called()
        external.assert_called_once_with(
            "orichum-doctor", [], diagnostics=mock.ANY
        )
        output = stdout.getvalue()
        self.assertIn("Setting up Orichum…", output)
        self.assertIn("Account\n  Name: Personal GPT", output)
        self.assertIn("Projects\n", output)
        self.assertIn("Models\n  ✓ Already configured", output)
        self.assertIn("Services\n  ✓ Ready", output)
        self.assertTrue(output.endswith("Orichum is ready.\n"))
        self.assertNotIn("127.0.0.1", output)
        self.assertNotIn("Next:", output)

    def test_setup_reconciles_healthy_runtime_after_creating_stack(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(
            documents={"projects": {"contexts": [{"root": str(project)}]}}
        )
        account = SimpleNamespace(
            name="Personal GPT",
            pool="shared",
            priority=100,
        )

        with (
            mock.patch.object(
                orichum_cli,
                "_active_provider_accounts",
                return_value=(account,),
            ),
            mock.patch.object(
                orichum_cli, "_runtime_ready", return_value=True
            ),
            mock.patch.object(
                orichum_cli, "_reconcile_runtime", return_value=0
            ) as reconcile,
            mock.patch.object(
                orichum_cli, "_project_context_mapped", return_value=True
            ),
            mock.patch.object(
                orichum_cli,
                "_setup_project_ready",
                side_effect=(False, True, True),
            ),
            mock.patch.object(
                orichum_cli,
                "_ensure_setup_project_config",
                return_value=(project / ".orichum" / "config.json", True),
            ),
            mock.patch.object(
                orichum_cli,
                "create_recommended_stack",
                return_value="recommended",
            ),
            mock.patch.object(
                orichum_cli, "_load", return_value=(paths, config)
            ),
            mock.patch.object(
                orichum_cli, "_run_external", return_value=0
            ),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            status = orichum_cli._setup(paths, config, str(project))

        self.assertEqual(status, 0)
        reconcile.assert_called_once_with(mock.ANY)

    def test_setup_stops_when_provider_configuration_is_cancelled(self) -> None:
        project = self.root / "project"
        project.mkdir()
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(documents={"projects": {"contexts": []}})

        with (
            mock.patch.object(
                orichum_cli, "_active_provider_accounts", return_value=()
            ),
            mock.patch.object(
                orichum_cli, "_provider_configure", return_value=7
            ),
            mock.patch.object(
                orichum_cli, "_reconcile_runtime", return_value=0
            ) as reconcile,
            mock.patch.object(
                orichum_cli, "_run_external", return_value=0
            ) as external,
        ):
            status = orichum_cli._setup(paths, config, str(project))

        self.assertEqual(status, 7)
        reconcile.assert_not_called()
        external.assert_not_called()

    def test_setup_reports_recovery_when_provider_registration_is_incomplete(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        paths = {
            "config": self.root / "config",
            "data": self.root / "data",
            "state": self.root / "state",
        }
        config = SimpleNamespace(documents={"projects": {"contexts": []}})

        stdout = io.StringIO()
        with (
            mock.patch.object(
                orichum_cli, "_active_provider_accounts", return_value=()
            ),
            mock.patch.object(
                orichum_cli, "_provider_configure", return_value=0
            ),
            mock.patch.object(
                orichum_cli, "_load", return_value=(paths, config)
            ),
            contextlib.redirect_stdout(stdout),
        ):
            status = orichum_cli._setup(paths, config, str(project))

        self.assertEqual(status, 2)
        output = stdout.getvalue()
        self.assertIn("Setup stopped while configuring Orichum.", output)
        self.assertIn(
            "Reason:\n"
            "  setup stopped before a provider account was registered",
            output,
        )
        self.assertIn("  orichum setup", output)
        diagnostic_logs = tuple(
            (paths["data"] / "logs").glob("setup-*.log")
        )
        self.assertEqual(len(diagnostic_logs), 1)
        self.assertIn(
            f"Diagnostics:\n  {diagnostic_logs[0]}",
            output,
        )
        self.assertNotIn("  orichum doctor", output)

    def test_setup_reconciliation_uses_active_runtime_installer(self) -> None:
        runtime = self.root / "runtime"
        runtime.mkdir()
        installer = runtime / "install.sh"
        installer.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        installer.chmod(0o700)
        completed = SimpleNamespace(returncode=0)

        with (
            mock.patch.object(orichum_cli, "WORKFLOW_ROOT", runtime),
            mock.patch.object(
                orichum_cli.subprocess, "run", return_value=completed
            ) as run,
        ):
            status = orichum_cli._reconcile_runtime()

        self.assertEqual(status, 0)
        run.assert_called_once_with(
            [str(installer)],
            cwd=str(runtime),
            check=False,
        )

    def test_setup_persists_and_reuses_completed_phases(self) -> None:
        config_home, obsolete = self.provision_account_runtime()
        obsolete.unlink()
        project = self.root / "project"
        project.mkdir()
        model_path = config_home / "model-stacks.json"
        model_document = json.loads(model_path.read_text(encoding="utf-8"))
        model = "gpt-5.6-sol"
        model_document["defaultStack"] = "setup"
        model_document["stacks"] = {
            "setup": {
                "controller": [
                    {
                        "id": "oc-c-0000000000000001",
                        "model": model,
                        "providers": ["openai"],
                    }
                ],
                "agents": {
                    role: [
                        {
                            "id": f"oc-c-{index:016x}",
                            "model": model,
                            "providers": ["openai"],
                        }
                    ]
                    for index, role in enumerate(
                        orichum_cli.ROLES, start=2
                    )
                },
            }
        }
        model_path.write_text(
            json.dumps(model_document), encoding="utf-8"
        )
        model_path.chmod(0o600)
        data_home = Path(self.environment["ORICHUM_DATA_HOME"])
        credential = data_home / "auth" / "codex-work.json"
        runtime = {"ready": False}
        calls: list[tuple[str, tuple[str, ...]]] = []

        def external(
            name: str,
            arguments: list[str],
            *,
            environment: dict[str, str] | None = None,
            diagnostics: object | None = None,
        ) -> int:
            del environment, diagnostics
            calls.append((name, tuple(arguments)))
            if name == "orichum-context":
                return project_context.context_main(
                    [
                        "--config",
                        str(config_home / "projects.json"),
                        "--routing-config",
                        str(model_path),
                        "--providers-config",
                        str(config_home / "providers.json"),
                        *arguments,
                    ]
                )
            if name == "orichum-doctor":
                return 0
            raise AssertionError(f"unexpected external command: {name}")

        def managed_login(*_arguments, **_kwargs) -> int:
            credential.write_text(
                json.dumps(
                    {
                        "type": "codex",
                        "email": "private@example.com",
                        "access_token": "DO-NOT-PRINT",
                    }
                ),
                encoding="utf-8",
            )
            credential.chmod(0o600)
            return 0

        def reconcile(_diagnostics: object) -> int:
            runtime["ready"] = True
            return 0

        def live_models(_paths: dict[str, Path]) -> frozenset[str]:
            accounts = json.loads(
                (config_home / "accounts.json").read_text(encoding="utf-8")
            )["accounts"]
            return frozenset(
                f"{account['routingPrefix']}/{model}"
                for account in accounts
            )

        with (
            mock.patch.object(
                orichum_cli, "_interactive_terminal", return_value=True
            ),
            mock.patch.object(
                orichum_cli,
                "_runtime_ready",
                side_effect=lambda _paths: runtime["ready"],
            ),
            mock.patch.object(
                orichum_cli, "_reconcile_runtime", side_effect=reconcile
            ) as reconcile_call,
            mock.patch.object(
                orichum_cli,
                "_managed_provider_login",
                side_effect=managed_login,
            ) as login,
            mock.patch.object(orichum_cli, "_verify_runtime"),
            mock.patch.object(
                orichum_cli,
                "_live_models",
                side_effect=live_models,
            ),
            mock.patch.object(
                orichum_cli, "_run_external", side_effect=external
            ),
            mock.patch.object(
                orichum_cli, "run_stack_wizard", return_value=0
            ) as wizard,
            mock.patch(
                "builtins.input",
                side_effect=("4", "Work GPT", "1", "", ""),
            ),
        ):
            first = self.run_cli("setup", str(project))
            second = self.run_cli("setup", str(project))

        self.assertEqual(first[0], 0, first[2])
        self.assertEqual(second[0], 0, second[2])
        reconcile_call.assert_called_once_with(mock.ANY)
        login.assert_called_once()
        self.assertEqual(login.call_args.args[2], "OpenAI")
        wizard.assert_not_called()
        self.assertEqual(
            [name for name, _arguments in calls],
            [
                "orichum-context",
                "orichum-doctor",
                "orichum-doctor",
            ],
        )
        accounts = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"]
        contexts = json.loads(
            (config_home / "projects.json").read_text(encoding="utf-8")
        )["contexts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["pool"], "shared")
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["accountPools"], ["shared"])
        combined_output = first[1] + second[1]
        self.assertNotIn(credential.name, combined_output)
        self.assertNotIn("private@example.com", combined_output)
        self.assertNotIn("DO-NOT-PRINT", combined_output)

    def test_external_diagnostics_use_argv_runner_without_shell(self) -> None:
        with mock.patch.object(orichum_cli, "_run_external", return_value=8) as run:
            status, _, _ = self.run_cli("doctor")
            self.assertEqual(status, 8)
            run.assert_called_once_with("orichum-doctor", [])

        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            status, _, _ = self.run_cli("provider", "login", "codex")
            self.assertEqual(status, 0)
            run.assert_called_once_with("orichum-login", ["codex"])

        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            status, _, _ = self.run_cli(
                "plugin", "add", "github@official"
            )
            self.assertEqual(status, 0)
            run.assert_called_once_with(
                "orichum-plugin", ["add", "github@official"]
            )

        with mock.patch.object(orichum_cli, "_run_external", return_value=0) as run:
            status, _, _ = self.run_cli(
                "context", "add", "/work/acme", "--pool", "shared"
            )
            self.assertEqual(status, 0)
            run.assert_called_once_with(
                "orichum-context",
                ["add", "/work/acme", "--pool", "shared"],
            )

    def test_delegated_command_help_uses_the_unified_parser(self) -> None:
        cases = (
            (
                ("context", "add", "--help"),
                "usage: orichum context add",
                "--model-stack STACK",
            ),
            (
                ("context", "validate", "--help"),
                "usage: orichum context validate",
                "Validate every configured project context.",
            ),
            (
                ("plugin", "add", "--help"),
                "usage: orichum plugin add",
                "PLUGIN@MARKETPLACE",
            ),
            (
                ("plugin", "list", "--help"),
                "usage: orichum plugin list",
                "List declared plugins.",
            ),
        )
        for arguments, usage, detail in cases:
            with self.subTest(arguments=arguments):
                stdout = io.StringIO()
                stderr = io.StringIO()
                with mock.patch.object(
                    orichum_cli, "_run_external", return_value=0
                ) as run, contextlib.redirect_stdout(
                    stdout
                ), contextlib.redirect_stderr(
                    stderr
                ), self.assertRaises(SystemExit) as raised:
                    orichum_cli.main(list(arguments))

                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(stderr.getvalue(), "")
                self.assertIn(usage, stdout.getvalue())
                self.assertIn(detail, stdout.getvalue())
                run.assert_not_called()

    def test_runtime_service_ports_accepts_only_four_distinct_ports(
        self,
    ) -> None:
        data_home = self.root / "data"
        data_home.mkdir(mode=0o700)
        ports = data_home / "service-ports.json"
        ports.write_text(
            json.dumps(
                {
                    "claudexProxyPort": 13456,
                    "cliproxyPort": 8317,
                    "leanctxProxyPort": 13458,
                    "routeProxyPort": 13457,
                }
            ),
            encoding="utf-8",
        )
        ports.chmod(0o600)

        self.assertEqual(
            orichum_cli._runtime_service_ports({"data": data_home}),
            {
                "claudexProxyPort": 13456,
                "cliproxyPort": 8317,
                "leanctxProxyPort": 13458,
                "routeProxyPort": 13457,
            },
        )

    def provision_account_runtime(self) -> tuple[Path, Path]:
        config_home = self.root / "private-config"
        shutil.copytree(REPOSITORY_ROOT / "config", config_home)
        config_home.chmod(0o700)
        providers_path = config_home / "providers.json"
        providers = json.loads(providers_path.read_text(encoding="utf-8"))
        providers["accountPools"]["xebia"] = {
            "providers": ["anthropic", "antigravity", "openai"]
        }
        providers["accountPools"]["realtime"] = {
            "providers": ["anthropic", "antigravity", "openai"]
        }
        providers_path.write_text(json.dumps(providers), encoding="utf-8")
        providers_path.chmod(0o600)
        data_home = self.root / "private-data"
        auth_dir = data_home / "auth"
        auth_dir.mkdir(parents=True, mode=0o700)
        data_home.chmod(0o700)
        ports = data_home / "service-ports.json"
        ports.write_text(
            json.dumps({"cliproxyPort": 18317}), encoding="utf-8"
        )
        ports.chmod(0o600)
        key = data_home / "cliproxy-management.key"
        key.write_text("a" * 48 + "\n", encoding="ascii")
        key.chmod(0o600)
        credential = auth_dir / "claude-work.json"
        credential.write_text(
            json.dumps(
                {
                    "type": "claude",
                    "email": "work@example.com",
                    "access_token": "DO-NOT-PRINT",
                }
            ),
            encoding="utf-8",
        )
        credential.chmod(0o600)
        self.environment["ORICHUM_CONFIG_HOME"] = str(config_home)
        self.environment["ORICHUM_DATA_HOME"] = str(data_home)
        management = mock.patch.object(orichum_cli, "patch_auth_fields")
        self.management_patch = management.start()
        self.addCleanup(management.stop)
        def apply_fields(_endpoint, reference: str, fields: dict[str, object]):
            target = auth_dir / reference
            document = json.loads(target.read_text(encoding="utf-8"))
            document.update(fields)
            target.write_text(json.dumps(document), encoding="utf-8")
            target.chmod(0o600)
        self.management_patch.side_effect = apply_fields
        return config_home, credential

    def test_provider_account_lifecycle_is_named_private_and_redacted(self) -> None:
        config_home, credential = self.provision_account_runtime()

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Xebia Claude",
            "anthropic",
            credential.name,
            "xebia",
            "--priority",
            "primary",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        registry = config_home / "accounts.json"
        self.assertEqual(registry.stat().st_mode & 0o777, 0o600)
        document = json.loads(registry.read_text(encoding="utf-8"))
        account_id = document["accounts"][0]["id"]
        self.assertEqual(document["accounts"][0]["priority"], 100)
        self.management_patch.assert_called_once()
        self.assertEqual(
            self.management_patch.call_args.args[1], credential.name
        )
        self.assertEqual(
            self.management_patch.call_args.args[2],
            {
                "prefix": document["accounts"][0]["routingPrefix"],
                "priority": 100,
            },
        )

        status, stdout, stderr = self.run_cli("provider", "accounts")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Xebia Claude", stdout)
        self.assertIn("anthropic", stdout)
        self.assertIn("ACTIVE", stdout)
        self.assertNotIn(credential.name, stdout)
        self.assertNotIn("DO-NOT-PRINT", stdout)
        self.assertNotIn(document["accounts"][0]["routingPrefix"], stdout)

        for arguments in (
            ("priority", account_id, "secondary"),
            ("rename", account_id, "Primary Work"),
            ("disable", account_id),
            ("enable", account_id),
        ):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.run_cli(
                    "provider", "account", *arguments
                )
                self.assertEqual((status, stdout, stderr), (0, "", ""))

        updated = json.loads(registry.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(updated["name"], "Primary Work")
        self.assertEqual(updated["priority"], 50)
        self.assertEqual(updated["state"], "active")

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account_id
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["accounts"], []
        )

    def test_provider_configure_logs_in_and_registers_named_account(self) -> None:
        config_home, credential = self.provision_account_runtime()
        credential_document = credential.read_text(encoding="utf-8")
        credential.unlink()

        def authenticate(*_arguments, **_kwargs):
            credential.write_text(credential_document, encoding="utf-8")
            credential.chmod(0o644)
            return 0

        with (
            mock.patch.object(
                orichum_cli, "_interactive_terminal", return_value=True
            ),
            mock.patch.object(
                orichum_cli,
                "_run_external",
                side_effect=authenticate,
            ) as run,
            mock.patch(
                "builtins.input",
                side_effect=("1", "Work Claude", "", "", ""),
            ),
        ):
            status, stdout, stderr = self.run_cli(
                "provider", "configure"
            )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        run.assert_called_once_with(
            "orichum-login",
            ["claude"],
            environment={"ORICHUM_PROVIDER_CONFIGURE": "1"},
        )
        document = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(document["accounts"]), 1)
        account = document["accounts"][0]
        self.assertEqual(account["name"], "Work Claude")
        self.assertEqual(account["provider"], "anthropic")
        self.assertEqual(account["credentialRef"], credential.name)
        self.assertEqual(account["pool"], "shared")
        self.assertEqual(account["priority"], 100)
        self.assertEqual(account["state"], "active")
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        self.assertIn("Provider account ready: Work Claude", stdout)
        self.assertNotIn(credential.name, stdout)
        self.assertNotIn("work@example.com", stdout)
        self.assertNotIn("DO-NOT-PRINT", stdout)

    def test_prepare_provider_account_does_not_register_it(self) -> None:
        config_home, credential = self.provision_account_runtime()

        with (
            mock.patch.dict(os.environ, self.environment, clear=False),
            mock.patch("builtins.input", return_value=""),
        ):
            paths, config = orichum_cli._load()
            pending = orichum_cli._prepare_provider_account(
                paths,
                config,
                "anthropic",
            )

        self.assertEqual(pending.provider, "anthropic")
        self.assertEqual(pending.credential_ref, credential.name)
        self.assertNotIn(credential.name, repr(pending))
        self.assertEqual(
            orichum_cli.load_accounts(config_home / "accounts.json"),
            (),
        )

    def test_onboarding_registers_first_account_without_advanced_prompts(
        self,
    ) -> None:
        config_home, credential = self.provision_account_runtime()
        credential.chmod(0o644)
        stdout = io.StringIO()

        with (
            mock.patch.dict(os.environ, self.environment, clear=False),
            mock.patch("builtins.input", side_effect=("1", "Work Claude")),
            contextlib.redirect_stdout(stdout),
        ):
            paths, config = orichum_cli._load()
            status = orichum_cli._provider_configure(
                paths,
                config,
                onboarding=True,
            )

        self.assertEqual(status, 0)
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        self.assertEqual(account["name"], "Work Claude")
        self.assertEqual(account["credentialRef"], credential.name)
        self.assertEqual(account["pool"], "shared")
        self.assertEqual(account["priority"], 100)
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        output = stdout.getvalue()
        self.assertNotIn("Choose where this account is available", output)
        self.assertNotIn("Choose account priority", output)
        self.assertNotIn("Register this account", output)
        self.assertNotIn("Account summary", output)

    def test_setup_project_folder_defaults_to_projects_and_creates_it(
        self,
    ) -> None:
        home = self.root / "home"
        home.mkdir()

        with (
            mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False),
            mock.patch("builtins.input", return_value=""),
        ):
            project = orichum_cli._setup_project_path(None)

        self.assertEqual(project, home / "projects")
        self.assertTrue(project.is_dir())

        with mock.patch.dict(os.environ, {"HOME": str(home)}, clear=False):
            self.assertEqual(
                orichum_cli._display_setup_path(project),
                "~/projects",
            )

    def test_setup_explicit_project_folder_must_already_exist(self) -> None:
        missing = self.root / "missing"

        with self.assertRaisesRegex(
            orichum_cli.CliError,
            "project root is unavailable",
        ):
            orichum_cli._setup_project_path(str(missing))

        self.assertFalse(missing.exists())

    def test_provider_configure_reuses_prior_unregistered_login(self) -> None:
        config_home, credential = self.provision_account_runtime()

        with (
            mock.patch.object(
                orichum_cli, "_interactive_terminal", return_value=True
            ),
            mock.patch.object(
                orichum_cli,
                "_run_external",
                return_value=99,
            ) as run,
            mock.patch(
                "builtins.input",
                side_effect=("", "", "Work Claude", "", "", ""),
            ),
        ):
            status, stdout, stderr = self.run_cli(
                "provider", "configure"
            )

        self.assertEqual(status, 0, stderr)
        self.assertEqual(stderr, "")
        run.assert_not_called()
        document = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(document["accounts"]), 1)
        account = document["accounts"][0]
        self.assertEqual(account["name"], "Work Claude")
        self.assertEqual(account["provider"], "anthropic")
        self.assertEqual(account["credentialRef"], credential.name)
        self.assertIn("Provider account ready: Work Claude", stdout)
        self.assertNotIn(credential.name, stdout)
        self.assertNotIn("work@example.com", stdout)
        self.assertNotIn("DO-NOT-PRINT", stdout)

    def test_provider_configuration_handles_terminal_interrupt_cleanly(
        self,
    ) -> None:
        self.provision_account_runtime()

        with (
            mock.patch.object(
                orichum_cli, "_interactive_terminal", return_value=True
            ),
            mock.patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            status, stdout, stderr = self.run_cli(
                "provider", "configure"
            )

        self.assertEqual(status, 2)
        self.assertTrue(stdout.startswith("Choose a provider:\n"))
        self.assertEqual(stderr, "ERROR: setup cancelled\n")

    def test_account_remove_rejects_stack_candidate_binding(self) -> None:
        config_home, credential = self.provision_account_runtime()
        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Bound Claude",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        save_stack_bindings(
            config_home / "stack-bindings.json",
            StackBindings(
                {"oc-c-a69e16d6ee83ad12": account["id"]}
            ),
            expected_digest=None,
        )

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account["id"]
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("balanced", stderr)
        self.assertIn("correctness-critic", stderr)
        self.assertNotIn(credential.name, stderr)
        self.assertNotIn(account["routingPrefix"], stderr)
        self.assertEqual(
            json.loads(
                (config_home / "accounts.json").read_text(encoding="utf-8")
            )["accounts"][0]["state"],
            "active",
        )

    def test_account_remove_prunes_orphan_candidate_binding(self) -> None:
        config_home, credential = self.provision_account_runtime()
        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Orphaned Claude",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        bindings_path = config_home / "stack-bindings.json"
        save_stack_bindings(
            bindings_path,
            StackBindings(
                {"oc-c-ffffffffffffffff": account["id"]}
            ),
            expected_digest=None,
        )

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account["id"]
        )

        self.assertEqual((status, stdout, stderr), (0, "", ""))
        self.assertEqual(
            json.loads(
                (config_home / "accounts.json").read_text(encoding="utf-8")
            )["accounts"],
            [],
        )
        self.assertEqual(
            orichum_cli.load_stack_bindings(bindings_path),
            StackBindings({}),
        )

    def test_account_remove_serializes_against_new_binding_save(self) -> None:
        config_home, credential = self.provision_account_runtime()
        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Racing Claude",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        account_id = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]["id"]
        with mock.patch.dict(os.environ, self.environment, clear=False):
            paths, config = orichum_cli._load()

        removal_inside = threading.Event()
        release_removal = threading.Event()
        binding_attempted = threading.Event()
        binding_completed = threading.Event()
        original_find = orichum_cli.find_account
        original_flock = stack_bindings.fcntl.flock
        remove_errors: list[BaseException] = []
        binding_errors: list[BaseException] = []

        def blocking_find(accounts, selector):
            if (
                threading.current_thread().name == "account-removal"
                and not removal_inside.is_set()
            ):
                removal_inside.set()
                if not release_removal.wait(timeout=2):
                    raise AssertionError("removal test was not released")
            return original_find(accounts, selector)

        def observed_flock(descriptor: int, operation: int) -> None:
            if threading.current_thread().name == "binding-save":
                binding_attempted.set()
            original_flock(descriptor, operation)

        def remove() -> None:
            try:
                orichum_cli._mutate_account(
                    SimpleNamespace(
                        account_command="remove",
                        selector=account_id,
                    ),
                    paths,
                    config,
                )
            except BaseException as error:
                remove_errors.append(error)

        def bind() -> None:
            try:
                save_stack_bindings(
                    config_home / "stack-bindings.json",
                    StackBindings(
                        {"oc-c-a69e16d6ee83ad12": account_id}
                    ),
                    expected_digest=None,
                )
            except BaseException as error:
                binding_errors.append(error)
            finally:
                binding_completed.set()

        with (
            mock.patch.object(
                orichum_cli, "find_account", side_effect=blocking_find
            ),
            mock.patch.object(
                stack_bindings.fcntl,
                "flock",
                side_effect=observed_flock,
            ),
        ):
            removal = threading.Thread(target=remove, name="account-removal")
            binding = threading.Thread(target=bind, name="binding-save")
            removal.start()
            self.assertTrue(removal_inside.wait(timeout=2))
            binding.start()
            self.assertTrue(binding_attempted.wait(timeout=2))
            self.assertFalse(binding_completed.is_set())
            release_removal.set()
            removal.join(timeout=2)
            binding.join(timeout=2)

        self.assertFalse(removal.is_alive())
        self.assertFalse(binding.is_alive())
        self.assertEqual(remove_errors, [])
        self.assertEqual(len(binding_errors), 1)
        self.assertIsInstance(binding_errors[0], StackBindingError)
        self.assertIn("not registered", str(binding_errors[0]))
        self.assertFalse((config_home / "stack-bindings.json").exists())

    def test_account_add_rejects_provider_pool_and_credential_mismatch(self) -> None:
        _, credential = self.provision_account_runtime()
        cases = (
            ("missing", credential.name, "xebia"),
            ("anthropic", credential.name, "missing"),
            ("openai", credential.name, "xebia"),
            ("anthropic", "../claude-work.json", "xebia"),
        )
        for provider, reference, pool in cases:
            with self.subTest(provider=provider, reference=reference, pool=pool):
                status, stdout, stderr = self.run_cli(
                    "provider",
                    "account",
                    "add",
                    "Rejected",
                    provider,
                    reference,
                    pool,
                )
                self.assertEqual(status, 2)
                self.assertEqual(stdout, "")
                self.assertIn("ERROR:", stderr)

    def test_account_add_failure_leaves_recoverable_pending_route(self) -> None:
        config_home, credential = self.provision_account_runtime()
        self.management_patch.side_effect = orichum_cli.ManagementError(
            "injected management failure"
        )

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Work Claude",
            "anthropic",
            credential.name,
            "xebia",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("injected management failure", stderr)
        registry = config_home / "accounts.json"
        pending = json.loads(
            registry.read_text(encoding="utf-8")
        )["accounts"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "pending-add")

    def test_account_add_timeout_reconciles_and_remove_restores_metadata(self) -> None:
        config_home, credential = self.provision_account_runtime()
        original = json.loads(credential.read_text(encoding="utf-8"))
        original.update({"prefix": "prior-route", "priority": 7})
        credential.write_text(json.dumps(original), encoding="utf-8")
        credential.chmod(0o600)

        apply_fields = self.management_patch.side_effect
        first = True

        def apply_then_timeout(endpoint, reference, fields):
            nonlocal first
            apply_fields(endpoint, reference, fields)
            if first:
                first = False
                raise orichum_cli.ManagementError("ambiguous timeout")

        self.management_patch.side_effect = apply_then_timeout
        status, _, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Recoverable",
            "anthropic",
            credential.name,
            "xebia",
        )
        self.assertEqual(status, 2)
        self.assertIn("ambiguous timeout", stderr)
        registry = config_home / "accounts.json"
        account = json.loads(registry.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(account["state"], "pending-add")

        self.management_patch.side_effect = apply_fields
        status, stdout, stderr = self.run_cli(
            "provider", "account", "sync", account["id"]
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        active = json.loads(registry.read_text(encoding="utf-8"))["accounts"][0]
        self.assertEqual(active["state"], "active")

        status, stdout, stderr = self.run_cli(
            "provider", "account", "remove", account["id"]
        )
        self.assertEqual((status, stdout, stderr), (0, "", ""))
        restored = json.loads(credential.read_text(encoding="utf-8"))
        self.assertEqual(restored["prefix"], "prior-route")
        self.assertEqual(restored["priority"], 7)
        self.assertEqual(
            json.loads(registry.read_text(encoding="utf-8"))["accounts"], []
        )

    def test_account_publication_requires_readback_before_activation(self) -> None:
        config_home, credential = self.provision_account_runtime()
        self.management_patch.side_effect = None

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Unverified",
            "anthropic",
            credential.name,
            "xebia",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("not verified", stderr)
        account = json.loads(
            (config_home / "accounts.json").read_text(encoding="utf-8")
        )["accounts"][0]
        self.assertEqual(account["state"], "pending-add")

    def test_account_add_and_enable_reject_disabled_live_credential(self) -> None:
        config_home, credential = self.provision_account_runtime()
        document = json.loads(credential.read_text(encoding="utf-8"))
        document["disabled"] = True
        credential.write_text(json.dumps(document), encoding="utf-8")
        credential.chmod(0o600)

        status, stdout, stderr = self.run_cli(
            "provider",
            "account",
            "add",
            "Disabled",
            "anthropic",
            credential.name,
            "xebia",
        )

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("disabled", stderr)
        self.assertFalse((config_home / "accounts.json").exists())


    def test_paths_and_context_delegation_do_not_require_valid_config(self) -> None:
        self.environment["ORICHUM_CONFIG_HOME"] = str(
            self.root / "missing-config"
        )

        status, stdout, stderr = self.run_cli("config", "paths")
        self.assertEqual(status, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(
            json.loads(stdout)["config"], str(self.root / "missing-config")
        )

        with mock.patch.object(
            orichum_cli, "_run_external", return_value=7
        ) as run:
            for arguments in (("context", "add", "/tmp/project"),):
                with self.subTest(arguments=arguments):
                    status, stdout, stderr = self.run_cli(*arguments)
                    self.assertEqual(status, 7)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, "")
            run.assert_called_once_with(
                "orichum-context", ["add", "/tmp/project"]
            )

    def test_status_renders_the_selected_logical_session(self) -> None:
        session_id = "oc-s-0000000000000001"

        def render_status(**arguments: object) -> int:
            arguments["output_stream"].write(
                "ORICHUM │ xebia │ balanced\n"
                "GPT · GPT 5.6 Sol │ Personal GPT [primary] │ "
                "context — │ 5h 12% │ 7d 34%\n"
            )
            return 0

        with (
            mock.patch.object(
                orichum_cli,
                "load_logical_session",
                return_value=object(),
            ) as load,
            mock.patch.object(
                orichum_cli,
                "render_status_main",
                side_effect=render_status,
            ),
        ):
            status, stdout, stderr = self.run_cli("status", session_id)

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn(f"SESSION │ {session_id}", stdout)
        self.assertIn("Personal GPT [primary]", stdout)
        load.assert_called_once_with(
            self.root / "data" / "state",
            session_id,
        )

    def test_status_uses_the_current_session_environment(self) -> None:
        session_id = "oc-s-0000000000000001"
        self.environment["ORICHUM_SESSION_ID"] = session_id

        def render_status(**arguments: object) -> int:
            arguments["output_stream"].write("ORICHUM │ current\n")
            return 0

        with (
            mock.patch.object(
                orichum_cli,
                "load_logical_session",
                return_value=object(),
            ),
            mock.patch.object(
                orichum_cli,
                "render_status_main",
                side_effect=render_status,
            ),
        ):
            status, stdout, stderr = self.run_cli("status")

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn(f"SESSION │ {session_id}", stdout)
        self.assertIn("ORICHUM │ current", stdout)

    def test_status_rejects_missing_or_malformed_session_before_config_load(
        self,
    ) -> None:
        self.environment["ORICHUM_CONFIG_HOME"] = str(
            self.root / "missing-config"
        )
        for arguments, message in (
            (("status",), "orichum status <session-id>"),
            (("status", "invalid"), "logical session ID is invalid"),
        ):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.run_cli(*arguments)
                self.assertEqual((status, stdout), (2, ""))
                self.assertIn(message, stderr)

    def test_terminal_title_identifies_the_orichum_session(self) -> None:
        prepared = SimpleNamespace(
            logical=SimpleNamespace(
                project_root=Path("/work/xebia"),
                controller=SimpleNamespace(
                    primary=SimpleNamespace(
                        logical_model="claude-opus-5"
                    )
                ),
            )
        )
        events: list[str] = []

        class RecordingStream(io.StringIO):
            def isatty(self) -> bool:
                return True

            def flush(self) -> None:
                events.append("flush")

        stream = RecordingStream()
        orichum_cli._set_terminal_title(
            prepared,
            stream=stream,
            environment={"TERM": "xterm-256color"},
        )

        self.assertEqual(
            stream.getvalue(),
            "\x1b]0;Orichum — xebia — Opus 5\x07",
        )
        self.assertEqual(events, ["flush"])

    def test_plain_orichum_is_an_explicit_session_runtime_gate(self) -> None:
        status, stdout, stderr = self.run_cli()
        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("installed launcher", stderr)

    def test_caller_cannot_override_pinned_models_agents_or_transcript(self) -> None:
        blocked = (
            "--agents={}",
            "--fallback-model=other",
            "--continue",
            "-c",
            "--resume=00000000-0000-4000-8000-000000000000",
            "-r00000000-0000-4000-8000-000000000000",
            "--from-pr=123",
            "--safe-mode",
            "--allowedTools=mcp__other__*",
            "--allowed-tools",
            "--disallowedTools=mcp__leanctx__ctx_read",
            "--disallowed-tools",
            "--plugin-url=https://example.invalid/plugin.zip",
            "--worktree=review",
            "-wreview",
            "--tmux",
            "--bare",
        )
        for argument in blocked:
            with self.subTest(argument=argument):
                with self.assertRaises(orichum_cli.CliError):
                    orichum_cli._validate_user_claude_arguments([argument])

    def test_run_and_resume_dispatch_owned_session_launch(self) -> None:
        prepared = object()
        with (
            mock.patch.object(
                orichum_cli, "_prepare_new_session", return_value=prepared
            ) as prepare,
            mock.patch.object(
                orichum_cli,
                "_launch_session",
                side_effect=SystemExit(0),
            ) as launch,
            self.assertRaises(SystemExit),
        ):
            self.run_cli("run", "review", "this")
        prepare.assert_called_once()
        self.assertEqual(
            prepare.call_args.kwargs["leanctx_profile"], "lean"
        )
        self.assertFalse(launch.call_args.kwargs["resume"])
        self.assertEqual(
            launch.call_args.kwargs["arguments"], ["review", "this"]
        )

        with (
            mock.patch.object(
                orichum_cli, "_prepare_resume", return_value=prepared
            ) as prepare,
            mock.patch.object(
                orichum_cli,
                "_launch_session",
                side_effect=SystemExit(0),
            ) as launch,
            self.assertRaises(SystemExit),
        ):
            self.run_cli("resume", "oc-s-0000000000000001", "continue")
        self.assertEqual(
            prepare.call_args.kwargs["identifier"],
            "oc-s-0000000000000001",
        )
        self.assertTrue(launch.call_args.kwargs["resume"])
        self.assertEqual(launch.call_args.kwargs["arguments"], ["continue"])

    def test_session_launch_preapproves_only_bounded_leanctx_tools(self) -> None:
        data = self.root / "data"
        config_home = self.root / "config"
        state = data / "state"
        run_dir = state / "sessions" / "run.test"
        plugin = run_dir / "plugin"
        for directory in (data / "bin", data / "model-config" / "current",
                          config_home, state, run_dir, plugin):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        claudex = data / "bin" / "claudex"
        shared_config = data / "model-config" / "current" / "claudex.toml"
        policy = config_home / "controller-policy.md"
        for path in (claudex, shared_config, policy):
            path.write_text("test\n", encoding="utf-8")
            path.chmod(0o700 if path == claudex else 0o600)
        physical = SimpleNamespace(
            run_dir=run_dir,
            mcp_file=run_dir / "mcp.json",
            context_file=run_dir / "context.json",
            effective_models_file=run_dir / "effective-models.json",
            plugin_dir=plugin,
            controller_model="gpt-5.6-sol",
        )
        physical.context_file.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "launchDirReal": "/Users/example/xebia/project",
                    "repoRootReal": "/Users/example/xebia/project",
                    "route": {
                        "id": "xebia",
                        "contextRootReal": "/Users/example/xebia",
                        "atlassianConfigured": True,
                        "modelStack": None,
                        "accountPools": ["xebia", "shared"],
                        "githubAccount": "athevar-xebia",
                    },
                }
            ),
            encoding="utf-8",
        )
        physical.context_file.chmod(0o600)
        prepared = SimpleNamespace(
            logical=SimpleNamespace(
                id="oc-s-0000000000000001",
                claude_session_id="00000000-0000-4000-8000-000000000001",
            ),
            physical=physical,
        )
        paths = {
            "data": data,
            "config": config_home,
            "state": state,
        }
        resolved = SimpleNamespace(documents={"runtime": {"controller": {
            "effort": "high",
            "maxToolUseConcurrency": 3,
            "maxSubagentsPerSession": 24,
        }}})
        with (
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={
                    "cliproxyPort": 8317,
                    "claudexProxyPort": 13457,
                    "leanctxProxyPort": 13458,
                    "routeProxyPort": 13456,
                },
            ),
            mock.patch.object(
                orichum_cli,
                "_reserve_session_claudex_port",
                return_value=13459,
            ) as reserve,
            mock.patch.object(
                orichum_cli,
                "_materialize_session_claudex_config",
                return_value=run_dir / "claudex.toml",
            ),
            mock.patch.object(
                orichum_cli,
                "_github_config_for_session",
                return_value=None,
            ),
            mock.patch.object(
                orichum_cli,
                "_session_environment",
                return_value={"ORICHUM_SESSION_ID": prepared.logical.id},
            ),
            mock.patch.object(orichum_cli.os, "execvpe") as execute,
        ):
            orichum_cli._launch_session(
                prepared,
                paths,
                resolved,
                resume=False,
                arguments=("-p", "read with LeanCTX"),
            )

        command = execute.call_args.args[1]
        self.assertIn("--allowedTools", command)
        allowed_index = command.index("--allowedTools")
        self.assertEqual(
            command[allowed_index + 1],
            ",".join((
                "Workflow",
                "mcp__leanctx__ctx_read",
                "mcp__leanctx__ctx_search",
                "mcp__leanctx__ctx_tree",
                "mcp__leanctx__ctx_expand",
                "mcp__leanctx__ctx_graph",
                "mcp__leanctx__ctx_impact",
                "mcp__leanctx__ctx_callgraph",
                "mcp__leanctx__ctx_knowledge",
                "mcp__leanctx__ctx_overview",
            )),
        )
        self.assertNotIn(
            "mcp__leanctx__ctx_patch",
            command[allowed_index + 1].split(","),
        )
        self.assertNotIn(
            "mcp__leanctx__ctx_shell",
            command[allowed_index + 1].split(","),
        )
        self.assertEqual(
            reserve.call_args.args[-1],
            frozenset({8317, 13456, 13458}),
        )
        policy_index = command.index("--append-system-prompt-file")
        launch_policy = Path(command[policy_index + 1])
        self.assertEqual(launch_policy, run_dir / "launch-policy.md")
        binding_prompt = launch_policy.read_text(encoding="utf-8")
        self.assertIn("Jira configured: yes", binding_prompt)
        self.assertIn('GitHub account: "athevar-xebia"', binding_prompt)
        self.assertIn(
            "LeanCTX project memory follows the verified project root",
            binding_prompt,
        )
        self.assertIn(
            "already bound to this physical session", binding_prompt
        )
        self.assertIn(
            "bound to this physical session and project",
            binding_prompt,
        )
        self.assertNotIn("mcp__leanctx__*", command)
        self.assertNotIn("--dangerously-skip-permissions", command)

    def test_sessions_empty_table_is_redacted(self) -> None:
        (self.root / "data" / "state").mkdir(parents=True, mode=0o700)
        status, stdout, stderr = self.run_cli("sessions")
        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("PROJECT", stdout)
        self.assertNotIn("credential", stdout.lower())
        self.assertNotIn("routing", stdout.lower())

    def test_sessions_lists_newest_twenty_by_default(self) -> None:
        sessions = tuple(
            SimpleNamespace(
                id=f"oc-s-{index:016x}",
                created_at=f"2026-07-{index + 1:02d}T10:00:00Z",
                project_root=Path(f"/project/{index}"),
                stack="balanced",
                controller=SimpleNamespace(
                    primary=SimpleNamespace(
                        family="gpt",
                        logical_model="gpt-5.6-sol",
                    )
                ),
                parent_id=None,
            )
            for index in range(25)
        )
        with mock.patch.object(
            orichum_cli,
            "list_logical_sessions",
            return_value=sessions,
        ):
            status, stdout, stderr = self.run_cli("sessions")

        self.assertEqual((status, stderr), (0, ""))
        self.assertNotIn("oc-s-0000000000000000", stdout)
        self.assertIn("oc-s-0000000000000018", stdout)
        self.assertIn("Showing newest 20 of 25 sessions", stdout)

    def test_sessions_all_disables_default_limit(self) -> None:
        parser = orichum_cli.build_parser()

        parsed = parser.parse_args(["sessions", "--all"])
        limited = parser.parse_args(["sessions", "--limit", "7"])

        self.assertTrue(parsed.show_all)
        self.assertEqual(limited.limit, 7)

    def test_sessions_cleanup_previews_without_deleting(self) -> None:
        state = self.root / "data" / "state"
        state.mkdir(parents=True, mode=0o700)
        sessions = state / "sessions"
        stale = sessions / "run.stale"
        sessions.mkdir(mode=0o700)
        stale.mkdir(mode=0o700)
        (stale / ".complete").write_text("{}", encoding="utf-8")
        (stale / ".complete").chmod(0o600)
        os.utime(stale / ".complete", (1_700_000_000, 1_700_000_000))

        with mock.patch(
            "integrations.common.orichum_sessions.datetime"
        ) as clock:
            clock.now.return_value.timestamp.return_value = 1_701_000_000
            status, stdout, stderr = self.run_cli(
                "sessions", "cleanup", "--older-than", "7"
            )

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("run.stale", stdout)
        self.assertIn("--yes", stdout)
        self.assertTrue(stale.is_dir())

    def test_sessions_remove_and_clear_expose_preview_and_apply(self) -> None:
        parser = orichum_cli.build_parser()
        session_id = "oc-s-0000000000000001"

        remove = parser.parse_args(["sessions", "remove", session_id])
        clear = parser.parse_args(["sessions", "clear", "--yes"])

        self.assertEqual(remove.session_id, session_id)
        self.assertFalse(remove.yes)
        self.assertTrue(clear.yes)

        with mock.patch.object(
            orichum_cli,
            "remove_logical_session",
            return_value=SimpleNamespace(
                session_id=session_id,
                status="eligible",
            ),
        ) as remove_session:
            status, stdout, stderr = self.run_cli(
                "sessions", "remove", session_id
            )

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("Preview only", stdout)
        self.assertIn("--yes", stdout)
        self.assertEqual(remove_session.call_args.kwargs["apply"], False)

        with mock.patch.object(
            orichum_cli,
            "clear_logical_sessions",
            return_value=(
                SimpleNamespace(session_id=session_id, status="removed"),
            ),
        ) as clear_sessions:
            status, stdout, stderr = self.run_cli(
                "sessions", "clear", "--yes"
            )

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("Removed 1 logical session", stdout)
        self.assertIn("Claude Code history", stdout)
        self.assertEqual(clear_sessions.call_args.kwargs["apply"], True)

    def test_leanctx_parser_exposes_bounded_monitoring_commands(self) -> None:
        parser = orichum_cli.build_parser()

        self.assertEqual(
            parser.parse_args(["leanctx", "stats", "--run", "run.one"])
            .leanctx_command,
            "stats",
        )
        dashboard = parser.parse_args(
            [
                "leanctx",
                "dashboard",
                "--run",
                "run.one",
                "--port",
                "3341",
                "--open",
                "none",
            ]
        )
        self.assertEqual(dashboard.port, 3341)
        self.assertEqual(dashboard.open_mode, "none")
        self.assertEqual(
            parser.parse_args(["leanctx", "watch"]).leanctx_command,
            "watch",
        )
        self.assertEqual(
            parser.parse_args(["leanctx", "list"]).leanctx_command,
            "list",
        )
        self.assertTrue(
            parser.parse_args(["leanctx", "list", "--all"]).show_all
        )
        self.assertEqual(
            parser.parse_args(["leanctx", "list", "--limit", "7"]).limit,
            7,
        )
        economics = parser.parse_args(
            [
                "leanctx",
                "economics",
                "--session",
                "oc-s-0000000000000001",
                "--hours",
                "48",
            ]
        )
        self.assertEqual(economics.leanctx_command, "economics")
        self.assertEqual(economics.session, "oc-s-0000000000000001")
        self.assertEqual(economics.hours, 48)
        self.assertEqual(
            parser.parse_args(["leanctx", "economics"]).hours,
            24,
        )
        for hours in ("0", "169"):
            with self.subTest(hours=hours), self.assertRaises(SystemExit):
                parser.parse_args(
                    ["leanctx", "economics", "--hours", hours]
                )

    def test_leanctx_project_root_prefers_repository_over_parent(self) -> None:
        parent = self.root / "parent"
        repository = parent / "repository"
        repository.mkdir(parents=True)
        config = SimpleNamespace(documents={"projects": {}})
        with mock.patch.object(
            orichum_cli,
            "resolve_control_plane_context",
            return_value={
                "repoRootReal": str(repository),
                "route": {"contextRootReal": str(parent)},
            },
        ):
            selected = orichum_cli._leanctx_project_root(
                config,
                repository,
            )

        self.assertEqual(selected, repository)

    def test_leanctx_list_marks_newest_run_for_current_project(self) -> None:
        project = self.root / "project"
        project.mkdir()
        current = LeanctxRun(
            "run.current",
            self.root / "data" / "state" / "sessions" / "run.current",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        older = LeanctxRun(
            "run.older",
            self.root / "data" / "state" / "sessions" / "run.older",
            project,
            "2026-07-26T10:00:00Z",
            False,
        )
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(current, older),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
        ):
            status, stdout, stderr = self.run_cli("leanctx", "list")

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("RUN", stdout)
        self.assertIn("PROJECT", stdout)
        self.assertIn("ATTACHED", stdout)
        self.assertIn("run.current", stdout)
        current_row = next(
            line for line in stdout.splitlines() if "run.current" in line
        )
        older_row = next(
            line for line in stdout.splitlines() if "run.older" in line
        )
        self.assertIn("yes", current_row)
        self.assertIn("—", older_row)

    def test_leanctx_list_hides_unattached_history_unless_requested(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        attached = LeanctxRun(
            "run.attached",
            self.root / "data" / "state" / "sessions" / "run.attached",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        historical = LeanctxRun(
            "run.historical",
            self.root / "data" / "state" / "sessions" / "run.historical",
            project,
            "2026-07-26T10:00:00Z",
            False,
            attached=False,
        )
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(attached, historical),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
        ):
            status, stdout, stderr = self.run_cli("leanctx", "list")
            all_status, all_stdout, all_stderr = self.run_cli(
                "leanctx", "list", "--all"
            )

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("run.attached", stdout)
        self.assertNotIn("run.historical", stdout)
        self.assertIn("Use --all to include 1 historical run.", stdout)
        self.assertEqual((all_status, all_stderr), (0, ""))
        self.assertIn("run.historical", all_stdout)

    def test_leanctx_stats_rejects_newest_unattached_run(self) -> None:
        project = self.root / "project"
        project.mkdir()
        selected = LeanctxRun(
            "run.unattached",
            self.root / "data" / "state" / "sessions" / "run.unattached",
            project,
            "2026-07-27T10:00:00Z",
            False,
            attached=False,
        )
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(selected,),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
        ):
            status, stdout, stderr = self.run_cli("leanctx", "stats")

        self.assertEqual((status, stdout), (2, ""))
        self.assertEqual(
            stderr,
            "ERROR: LeanCTX is not attached to run run.unattached; "
            "rerun install.sh and start a new Orichum session\n",
        )

    def test_leanctx_stats_selects_project_and_renders_exact_savings(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        selected = LeanctxRun(
            "run.current",
            self.root / "data" / "state" / "sessions" / "run.current",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        binary = self.root / "data" / "bin" / "lean-ctx"
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(selected,),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "managed_binary",
                return_value=binary,
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "read_stats",
                return_value=LeanctxStats(
                    4,
                    14261,
                    1590,
                    12671,
                    88.85,
                ),
            ) as read,
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={"leanctxProxyPort": 13458},
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "read_proxy_stats",
                return_value=LeanctxProxyStats(
                    requests_total=7,
                    requests_compressed=5,
                    bytes_original=48000,
                    bytes_compressed=24000,
                    saved_tokens=6000,
                    savings_percent=50.0,
                ),
            ) as read_proxy,
        ):
            status, stdout, stderr = self.run_cli("leanctx", "stats")

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("Session MCP", stdout)
        self.assertIn("run.current", stdout)
        self.assertIn("SOURCE", stdout)
        self.assertIn("RETURNED", stdout)
        self.assertIn("REDUCTION", stdout)
        self.assertIn("14,261", stdout)
        self.assertIn("12,671", stdout)
        self.assertIn("88.9%", stdout)
        self.assertIn("Shared wire proxy", stdout)
        self.assertIn("REQUESTS", stdout)
        self.assertIn("COMPRESSED", stdout)
        self.assertIn("EST. TOKENS", stdout)
        self.assertIn("48,000", stdout)
        self.assertIn("24,000", stdout)
        self.assertIn("6,000", stdout)
        self.assertIn("50.0%", stdout)
        read.assert_called_once_with(binary, selected)
        read_proxy.assert_called_once_with(
            binary,
            self.root / "data",
            13458,
        )

    def test_leanctx_stats_does_not_invent_reduction_without_source_tokens(
        self,
    ) -> None:
        run = LeanctxRun(
            "run.current",
            self.root / "run.current",
            self.root / "project",
            "2026-07-27T10:00:00Z",
            True,
        )

        rendered = orichum_cli._leanctx_stats(
            run,
            LeanctxStats(2, 0, 0, 0, 0.0),
            LeanctxProxyStats(0, 0, 0, 0, 0, 0.0),
        )

        row = next(
            line for line in rendered.splitlines() if "run.current" in line
        )
        self.assertEqual(row.split("|")[-2].strip(), "—")

    def test_leanctx_economics_renders_separate_scopes_and_profile_footprint(
        self,
    ) -> None:
        logical = SimpleNamespace(leanctx_profile="lean")
        health = LeanctxToolHealth(
            advertised_tools=5,
            tool_schema_tokens=1300,
            instruction_tokens=449,
            rules_tokens=1053,
            fixed_total_tokens=2802,
            total_recorded_calls=12,
            tools=(
                ("ctx_read", 284, 5),
                ("ctx_search", 326, 1),
                ("ctx_tree", 135, 2),
                ("ctx_shell", 208, 4),
                ("ctx_graph", 347, 0),
            ),
        )
        rolling = LeanctxRollingEconomics(
            hours=24,
            compression_events=3,
            caching_events=7,
            source_tokens=10000,
            returned_tokens=2500,
            saved_tokens=7500,
            cache_read_tokens=108032,
            compression_saved_usd=0.01875,
            cache_saved_usd=0.486144,
            compression_percent=75.0,
        )
        gain = LeanctxGainSummary(
            total_commands=323,
            input_tokens=534803,
            output_tokens=435189,
            tokens_saved=99614,
            gain_rate_percent=18.626,
            injected_overhead_tokens_per_turn=3233,
            turns=680,
            injected_overhead_total_tokens=2198440,
            net_tokens_saved=-2098826,
            avoided_usd=0.249035,
            tool_spend_usd=0.450788,
            roi=0.552444,
        )

        rendered = orichum_cli._leanctx_economics(
            logical,
            health,
            rolling,
            gain,
        )

        self.assertIn("Selected-session provider footprint", rendered)
        self.assertIn("lean", rendered)
        self.assertIn("RESIDENT", rendered)
        self.assertIn("DEFERRED", rendered)
        self.assertIn("953", rendered)
        self.assertIn("347", rendered)
        self.assertIn("Shared rolling compression (last 24 hours)", rendered)
        self.assertIn("10,000", rendered)
        self.assertIn("7,500", rendered)
        self.assertIn("75.0%", rendered)
        self.assertIn(
            "Shared rolling recorded prompt-cache estimates (last 24 hours)",
            rendered,
        )
        self.assertIn("108,032", rendered)
        self.assertIn("LeanCTX all-time upstream estimate", rendered)
        self.assertIn("-2,098,826", rendered)
        self.assertIn("shared across all Orichum projects", rendered)
        self.assertIn("not complete provider billing", rendered)
        self.assertIn("not rolling-window billing", rendered)
        self.assertNotIn("rolling net", rendered.lower())
        self.assertTrue(
            orichum_cli._estimated_usd(1e100).endswith(".000000")
        )

    def test_leanctx_economics_resolves_session_and_newest_attached_project_run(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        attached = LeanctxRun(
            "run.attached",
            self.root / "data" / "state" / "sessions" / "run.attached",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        historical = LeanctxRun(
            "run.historical",
            self.root / "data" / "state" / "sessions" / "run.historical",
            project,
            "2026-07-28T10:00:00Z",
            False,
            attached=False,
        )
        logical = SimpleNamespace(
            id="oc-s-0000000000000001",
            project_root=project,
            leanctx_profile="lean",
        )
        health = LeanctxToolHealth(
            4,
            953,
            449,
            1053,
            2455,
            12,
            (
                ("ctx_read", 284, 5),
                ("ctx_search", 326, 1),
                ("ctx_tree", 135, 2),
                ("ctx_shell", 208, 4),
            ),
        )
        rolling = LeanctxRollingEconomics(
            48, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0
        )
        gain = LeanctxGainSummary(
            0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0.0, 0.0, 0.0
        )
        binary = self.root / "data" / "bin" / "lean-ctx"
        self.environment["ORICHUM_SESSION_ID"] = logical.id
        with (
            mock.patch.object(
                orichum_cli,
                "resolve_logical_session",
                return_value=logical,
            ) as resolve,
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(historical, attached),
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "select_run",
                return_value=attached,
            ) as select,
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "managed_binary",
                return_value=binary,
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "read_tool_health",
                return_value=health,
            ) as read_health,
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "read_rolling_economics",
                return_value=rolling,
            ) as read_rolling,
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "read_gain_summary",
                return_value=gain,
            ) as read_gain,
        ):
            status, stdout, stderr = self.run_cli(
                "leanctx",
                "economics",
                "--hours",
                "48",
            )

        self.assertEqual((status, stderr), (0, ""))
        self.assertIn("Selected-session provider footprint", stdout)
        resolve.assert_called_once_with(
            self.root / "data" / "state",
            logical.id,
        )
        self.assertEqual(select.call_args.args[0], (attached,))
        self.assertEqual(select.call_args.args[1], project)
        read_health.assert_called_once_with(binary, attached)
        read_rolling.assert_called_once_with(
            self.root / "data",
            48,
        )
        read_gain.assert_called_once_with(binary, attached)

    def test_leanctx_economics_requires_a_logical_session(self) -> None:
        status, stdout, stderr = self.run_cli("leanctx", "economics")

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("logical session ID is required", stderr)

    def test_leanctx_economics_renders_unavailable_roi_as_dash(self) -> None:
        rendered = orichum_cli._leanctx_economics(
            SimpleNamespace(leanctx_profile="lean"),
            LeanctxToolHealth(
                4,
                953,
                449,
                1053,
                2455,
                0,
                (
                    ("ctx_read", 284, 0),
                    ("ctx_search", 326, 0),
                    ("ctx_tree", 135, 0),
                    ("ctx_shell", 208, 0),
                ),
            ),
            LeanctxRollingEconomics(
                24, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0
            ),
            LeanctxGainSummary(
                0, 0, 0, 0, 0.0, 0, 0, 0, 0, 0.0, 0.0, None
            ),
        )

        row = next(
            line
            for line in rendered.splitlines()
            if line.startswith("| 0")
            and line.count("|") == 9
            and "| —" in line
        )
        self.assertEqual(row.split("|")[-2].strip(), "—")

    def test_leanctx_economics_explicit_session_overrides_environment(
        self,
    ) -> None:
        self.environment["ORICHUM_SESSION_ID"] = "oc-s-0000000000000001"
        explicit = "oc-s-0000000000000002"
        with mock.patch.object(
            orichum_cli,
            "resolve_logical_session",
            side_effect=orichum_cli.LogicalSessionError("stop after resolve"),
        ) as resolve:
            status, stdout, stderr = self.run_cli(
                "leanctx",
                "economics",
                "--session",
                explicit,
            )

        self.assertEqual((status, stdout), (2, ""))
        self.assertIn("stop after resolve", stderr)
        resolve.assert_called_once_with(
            self.root / "data" / "state",
            explicit,
        )

    def test_leanctx_dashboard_propagates_selected_options_and_status(
        self,
    ) -> None:
        project = self.root / "project"
        project.mkdir()
        selected = LeanctxRun(
            "run.current",
            self.root / "data" / "state" / "sessions" / "run.current",
            project,
            "2026-07-27T10:00:00Z",
            True,
        )
        binary = self.root / "data" / "bin" / "lean-ctx"
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(selected,),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "managed_binary",
                return_value=binary,
            ),
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "run_dashboard",
                return_value=7,
            ) as dashboard,
        ):
            status, stdout, stderr = self.run_cli(
                "leanctx",
                "dashboard",
                "--run",
                "run.current",
                "--port",
                "3341",
                "--open",
                "none",
            )

        self.assertEqual((status, stdout, stderr), (7, "", ""))
        dashboard.assert_called_once_with(
            binary,
            selected,
            self.root / "data" / "state",
            port=3341,
            open_mode="none",
        )

    def test_leanctx_implicit_selection_error_is_concise(self) -> None:
        project = self.root / "project"
        project.mkdir()
        with (
            mock.patch.object(
                orichum_cli.leanctx_monitor,
                "discover_runs",
                return_value=(),
            ),
            mock.patch.object(
                orichum_cli,
                "resolve_control_plane_context",
                return_value={
                    "route": {"contextRootReal": str(project)}
                },
            ),
        ):
            status, stdout, stderr = self.run_cli("leanctx", "stats")

        self.assertEqual((status, stdout), (2, ""))
        self.assertEqual(
            stderr,
            "ERROR: current project has no LeanCTX activity; "
            "run 'orichum leanctx list' to inspect available runs\n",
        )

    def test_session_routes_prints_opaque_account_ids_not_display_names(
        self,
    ) -> None:
        def route(account_id: str, provider: str, model: str):
            return SimpleNamespace(
                account_id=account_id,
                provider=provider,
                logical_model=model,
            )

        controller = SimpleNamespace(
            primary=route("oc-a-gpt", "openai", "gpt-5.6-sol"),
            fallbacks=(),
        )
        critic = SimpleNamespace(
            primary=route(
                "oc-a-claude", "anthropic", "claude-sonnet-5"
            ),
            fallbacks=(
                route(
                    "oc-a-antigravity",
                    "antigravity",
                    "claude-sonnet-5",
                ),
            ),
        )
        session = SimpleNamespace(
            controller=controller,
            agents={
                role: critic if role == "correctness-critic" else controller
                for role in orichum_cli.ROLES
            },
        )
        accounts = (
            SimpleNamespace(id="oc-a-gpt", name="Personal GPT"),
            SimpleNamespace(id="oc-a-claude", name="Work Claude"),
            SimpleNamespace(
                id="oc-a-antigravity", name="Antigravity Reserve"
            ),
        )

        output = orichum_cli._session_routes(session, accounts)

        self.assertIn("oc-a-gpt", output)
        self.assertIn("oc-a-claude", output)
        self.assertIn("oc-a-antigravity (antigravity)", output)
        self.assertNotIn("Personal GPT", output)
        self.assertNotIn("Work Claude", output)
        self.assertNotIn("Antigravity Reserve", output)

    def test_fork_dispatches_fresh_session_with_bounded_handoff(self) -> None:
        prepared = object()
        with (
            mock.patch.object(
                orichum_cli,
                "_prepare_fork",
                return_value=(prepared, "bounded handoff"),
            ) as prepare,
            mock.patch.object(
                orichum_cli,
                "_launch_session",
                side_effect=SystemExit(0),
            ) as launch,
            self.assertRaises(SystemExit),
        ):
            self.run_cli(
                "fork",
                "oc-s-0000000000000001",
                "--stack",
                "balanced",
            )
        self.assertEqual(
            prepare.call_args.kwargs["identifier"],
            "oc-s-0000000000000001",
        )
        self.assertEqual(prepare.call_args.kwargs["requested_stack"], "balanced")
        self.assertIsNone(prepare.call_args.kwargs["leanctx_profile"])
        self.assertFalse(launch.call_args.kwargs["resume"])
        self.assertEqual(launch.call_args.kwargs["handoff"], "bounded handoff")

    def test_handoff_reader_rejects_public_symlink_and_oversized_files(self) -> None:
        handoff = self.root / "handoff.md"
        handoff.write_text("Current task and verified state.", encoding="utf-8")
        handoff.chmod(0o600)
        self.assertEqual(
            orichum_cli._read_handoff(handoff),
            "Current task and verified state.",
        )
        handoff.chmod(0o644)
        with self.assertRaises(orichum_cli.CliError):
            orichum_cli._read_handoff(handoff)
        handoff.chmod(0o600)
        linked = self.root / "linked.md"
        linked.symlink_to(handoff)
        with self.assertRaises(orichum_cli.CliError):
            orichum_cli._read_handoff(linked)
        handoff.write_bytes(b"x" * (16 * 1024 + 1))
        handoff.chmod(0o600)
        with self.assertRaises(orichum_cli.CliError):
            orichum_cli._read_handoff(handoff)

    def test_session_github_identity_is_resolved_from_immutable_context(self) -> None:
        physical = mock.Mock(context_file=self.root / "context.json")
        expected = self.root / "github" / "work"
        with (
            mock.patch.object(
                orichum_cli,
                "_read_stable_file",
                return_value=json.dumps(
                    {"route": {"githubAccount": "athevar-xebia"}}
                ).encode(),
            ),
            mock.patch.object(
                orichum_cli,
                "ensure_github_identity",
                return_value=expected,
            ) as ensure,
        ):
            resolved = orichum_cli._github_config_for_session(
                {"data": self.root / "data"}, physical
            )

        self.assertEqual(resolved, expected)
        ensure.assert_called_once_with(
            self.root / "data", "athevar-xebia"
        )

    def test_session_claudex_ports_are_distinct_and_exclude_services(self) -> None:
        state = self.root / "data" / "state"
        state.mkdir(parents=True, mode=0o700)
        first_run = state / "sessions" / "run.first"
        second_run = state / "sessions" / "run.second"
        first_run.mkdir(parents=True, mode=0o700)
        second_run.mkdir(mode=0o700)

        first = orichum_cli._reserve_session_claudex_port(
            state,
            first_run,
            "oc-s-0000000000000001",
            13456,
            frozenset({8317, 8787, 13457}),
        )
        second = orichum_cli._reserve_session_claudex_port(
            state,
            second_run,
            "oc-s-0000000000000002",
            13456,
            frozenset({8317, 8787, 13457}),
        )

        self.assertNotEqual(first, second)
        self.assertNotIn(first, {8317, 8787, 13457})
        self.assertNotIn(second, {8317, 8787, 13457})
        self.assertEqual(
            (first_run / "claudex-proxy-port").read_text(
                encoding="ascii"
            ),
            f"{first}\n",
        )
        self.assertEqual(
            (second_run / "claudex-proxy-port").read_text(
                encoding="ascii"
            ),
            f"{second}\n",
        )

    def test_session_claudex_config_isolates_proxy_and_restores_user_env(
        self,
    ) -> None:
        source = self.root / "shared.toml"
        source.write_text(
            "\n".join(
                (
                    'claude_binary = "/usr/bin/claude"',
                    "proxy_port = 13456",
                    "",
                    "[[profiles]]",
                    'name = "gpt"',
                    "",
                    "[profiles.custom_headers]",
                    'X-Orichum-Session-ID = "unbound"',
                    "",
                )
            ),
            encoding="utf-8",
        )
        source.chmod(0o600)
        run_dir = self.root / "run"
        run_dir.mkdir(mode=0o700)
        prepared = SimpleNamespace(
            logical=SimpleNamespace(id="oc-s-0000000000000001"),
            physical=SimpleNamespace(run_dir=run_dir),
        )

        output = orichum_cli._materialize_session_claudex_config(
            source,
            prepared,
            14567,
            {
                "HOME": "/Users/example",
                "XDG_CACHE_HOME": "/var/cache/example",
                "XDG_RUNTIME_DIR": "/var/run/example",
            },
        )

        rendered = output.read_text(encoding="utf-8")
        self.assertIn("proxy_port = 14567", rendered)
        self.assertNotIn("proxy_port = 13456", rendered)
        self.assertIn(
            'X-Orichum-Session-ID = "oc-s-0000000000000001"',
            rendered,
        )
        self.assertIn("[profiles.extra_env]", rendered)
        self.assertIn('HOME = "/Users/example"', rendered)
        self.assertIn(
            'XDG_CACHE_HOME = "/var/cache/example"', rendered
        )
        self.assertIn(
            'XDG_RUNTIME_DIR = "/var/run/example"', rendered
        )
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_runtime_verifier_timeout_is_reported_as_cli_error(self) -> None:
        with mock.patch.object(
            orichum_cli.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(
                ["orichum-runtime-ready"], 30
            ),
        ):
            with self.assertRaisesRegex(
                orichum_cli.CliError,
                "runtime health verification timed out",
            ):
                orichum_cli._verify_runtime(
                    {
                        "data": self.root / "data",
                        "config": self.root / "config",
                    }
                )

    def test_runtime_source_drift_requires_reinstallation(self) -> None:
        completed = SimpleNamespace(
            returncode=1,
            stderr=(
                "ERROR: Orichum runtime source differs from the installed "
                "route service; run install.sh\n"
            ),
        )
        with mock.patch.object(
            orichum_cli.subprocess,
            "run",
            return_value=completed,
        ):
            with self.assertRaisesRegex(
                orichum_cli.CliError,
                "runtime source differs.*run install.sh",
            ):
                orichum_cli._verify_runtime(
                    {
                        "data": self.root / "data",
                        "config": self.root / "config",
                    }
                )

    def test_live_model_catalogue_uses_verified_cliproxy_endpoint(self) -> None:
        response = SimpleNamespace(
            status=200,
            read=lambda _maximum: json.dumps(
                {"data": [{"id": "gpt-5.6-sol"}]}
            ).encode("utf-8"),
        )
        connection = mock.MagicMock()
        connection.getresponse.return_value = response
        with (
            mock.patch.object(
                orichum_cli,
                "_runtime_service_ports",
                return_value={
                    "claudexProxyPort": 13457,
                    "cliproxyPort": 8317,
                    "routeProxyPort": 13456,
                },
            ),
            mock.patch.object(
                orichum_cli.http.client,
                "HTTPConnection",
                return_value=connection,
            ) as connect,
        ):
            models = orichum_cli._live_models(
                {"data": self.root / "data"}
            )
        connect.assert_called_once_with("127.0.0.1", 8317, timeout=3)
        self.assertEqual(models, frozenset({"gpt-5.6-sol"}))

    def test_missing_live_models_reports_roles_without_routing_prefixes(
        self,
    ) -> None:
        def route(model: str, upstream: str) -> SimpleNamespace:
            return SimpleNamespace(
                logical_model=model,
                upstream_model=upstream,
            )

        controller = SimpleNamespace(
            primary=route(
                "gpt-5.6-sol",
                "oc-r-1111111111111111/gpt-5.6-sol",
            ),
            fallbacks=(),
        )
        worker = SimpleNamespace(
            primary=route(
                "gpt-5.6-terra",
                "oc-r-2222222222222222/gpt-5.6-terra",
            ),
            fallbacks=(),
        )
        agents = {role: worker for role in orichum_cli.ROLES}

        def prepare(*_args: object, **_kwargs: object) -> object:
            orichum_cli._validate_live_models(
                {},
                controller,
                agents,
                available=frozenset(),
            )
            raise AssertionError("unreachable")

        with mock.patch.object(
            orichum_cli, "_prepare_new_session", side_effect=prepare
        ):
            status, stdout, stderr = self.run_cli("run")

        self.assertEqual(status, 2)
        self.assertEqual(stdout, "")
        self.assertIn("controller", stderr)
        self.assertIn("gpt-5.6-sol", stderr)
        self.assertIn("repository-explorer", stderr)
        self.assertNotIn("oc-r-", stderr)

    def test_session_environment_scrubs_token_identity_overrides(self) -> None:
        physical = SimpleNamespace(
            mcp_file=self.root / "mcp.json",
            run_dir=self.root / "run",
            context_file=self.root / "context.json",
            context_sha256="a" * 64,
            effective_models_file=self.root / "models.json",
            run_id="run-1",
        )
        prepared = SimpleNamespace(
            logical=SimpleNamespace(
                id="oc-s-0000000000000001",
                controller=SimpleNamespace(
                    primary=SimpleNamespace(family="gpt")
                ),
            ),
            physical=physical,
        )
        physical.run_dir.mkdir(mode=0o700)
        paths = {
            "state": self.root / "data" / "state",
            "config": self.root / "config",
            "data": self.root / "data",
        }
        managed_python = (
            paths["data"] / "python" / "cpython-3.14.6" / "bin" / "python3.14"
        )
        managed_python.parent.mkdir(mode=0o700, parents=True)
        managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_python.chmod(0o700)
        (paths["data"] / "bin").mkdir(mode=0o700)
        (paths["data"] / "bin" / "orichum-python").symlink_to(managed_python)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "HOME": "/Users/example",
                    "XDG_CACHE_HOME": "/var/cache/example",
                    "XDG_RUNTIME_DIR": "/var/run/example",
                    "GH_TOKEN": "wrong-account",
                    "GITHUB_TOKEN": "wrong-account",
                    "GH_HOST": "enterprise.example",
                    "ORICHUM_PYTHON": "/tmp/caller-python",
                    "ORICHUM_PYTHON_VALIDATED": "/tmp/caller-python",
                    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
                    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "82",
                },
                clear=False,
            ),
            mock.patch.object(
                orichum_cli.sys, "executable", str(managed_python)
            ),
        ):
            environment = orichum_cli._session_environment(
                prepared,
                paths,
                {
                    "maxToolUseConcurrency": 3,
                    "maxSubagentsPerSession": 24,
                },
                self.root / "github" / "work",
                self.root / "claudex.toml",
            )

        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("GH_HOST", environment)
        self.assertEqual(
            environment["GH_CONFIG_DIR"],
            str(self.root / "github" / "work"),
        )
        self.assertEqual(
            environment["HOME"],
            str(physical.run_dir / "claudex-home"),
        )
        self.assertEqual(
            environment["XDG_CACHE_HOME"],
            str(physical.run_dir / "claudex-home" / "cache"),
        )
        self.assertEqual(
            environment["XDG_RUNTIME_DIR"],
            str(physical.run_dir / "claudex-home" / "runtime"),
        )
        self.assertEqual(
            environment["ORICHUM_PYTHON"],
            str(paths["data"] / "bin" / "orichum-python"),
        )
        self.assertEqual(
            environment["ORICHUM_PYTHON_VALIDATED"],
            environment["ORICHUM_PYTHON"],
        )
        self.assertEqual(
            environment["CLAUDEX_RESUME_HINT"],
            "orichum resume oc-s-0000000000000001",
        )
        self.assertNotIn("CLAUDE_CODE_MAX_CONTEXT_TOKENS", environment)
        self.assertNotIn("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", environment)

    def test_session_without_selected_identity_preserves_github_environment(
        self,
    ) -> None:
        physical = SimpleNamespace(
            mcp_file=self.root / "mcp.json",
            run_dir=self.root / "run",
            context_file=self.root / "context.json",
            context_sha256="a" * 64,
            effective_models_file=self.root / "models.json",
            run_id="run-1",
        )
        prepared = SimpleNamespace(
            logical=SimpleNamespace(
                id="oc-s-0000000000000001",
                controller=SimpleNamespace(
                    primary=SimpleNamespace(family="claude")
                ),
            ),
            physical=physical,
        )
        physical.run_dir.mkdir(mode=0o700)
        paths = {
            "state": self.root / "data" / "state",
            "config": self.root / "config",
            "data": self.root / "data",
        }
        managed_python = (
            paths["data"] / "python" / "cpython-3.14.6" / "bin" / "python3.14"
        )
        managed_python.parent.mkdir(mode=0o700, parents=True)
        managed_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        managed_python.chmod(0o700)
        (paths["data"] / "bin").mkdir(mode=0o700)
        (paths["data"] / "bin" / "orichum-python").symlink_to(managed_python)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "GH_TOKEN": "caller-token",
                    "GH_HOST": "github.example",
                    "GH_CONFIG_DIR": "/caller/github-config",
                },
                clear=False,
            ),
            mock.patch.object(
                orichum_cli.sys, "executable", str(managed_python)
            ),
        ):
            environment = orichum_cli._session_environment(
                prepared,
                paths,
                {
                    "maxToolUseConcurrency": 3,
                    "maxSubagentsPerSession": 24,
                },
                None,
                self.root / "claudex.toml",
            )

        self.assertEqual(environment["GH_TOKEN"], "caller-token")
        self.assertEqual(environment["GH_HOST"], "github.example")
        self.assertEqual(
            environment["GH_CONFIG_DIR"], "/caller/github-config"
        )





if __name__ == "__main__":
    unittest.main()
