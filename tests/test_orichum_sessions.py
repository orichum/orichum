#!/usr/bin/env python3
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import integrations.common.orichum_cli as orichum_cli
import integrations.common.orichum_sessions as orichum_sessions
import integrations.common.session_config as session_config
from integrations.common.model_routing import ROLES
from integrations.common.account_registry import Account
from integrations.common.orichum_config import ResolvedConfig
from integrations.common.orichum_sessions import (
    LogicalSessionError,
    RouteBinding,
    clear_logical_sessions,
    cleanup_physical_runs,
    create_logical_session,
    list_logical_sessions,
    load_logical_session,
    remove_logical_session,
    resolve_session_plan,
    update_session_controller,
)
from integrations.common.route_selection import Route
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_definition import normalize_model_stacks
from integrations.common.project_context import (
    ContextError,
    resolve_control_plane_context,
)


class OrichumSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name).resolve() / "state"
        self.state.mkdir(mode=0o700)

    def test_physical_run_cleanup_previews_then_removes_only_stale_runs(
        self,
    ) -> None:
        sessions = self.state / "sessions"
        sessions.mkdir(mode=0o700)
        stale = sessions / "run.stale"
        recent = sessions / "run.recent"
        for run in (stale, recent):
            run.mkdir(mode=0o700)
            (run / ".complete").write_text("{}", encoding="utf-8")
            (run / ".complete").chmod(0o600)
        os.utime(stale / ".complete", (1_700_000_000, 1_700_000_000))

        preview = cleanup_physical_runs(
            self.state,
            older_than_days=7,
            apply=False,
            now=1_701_000_000,
        )

        self.assertEqual([item.run_id for item in preview], ["run.stale"])
        self.assertEqual(preview[0].status, "eligible")
        self.assertTrue(stale.is_dir())
        removed = cleanup_physical_runs(
            self.state,
            older_than_days=7,
            apply=True,
            now=1_701_000_000,
        )
        self.assertEqual(removed[0].status, "removed")
        self.assertFalse(stale.exists())
        self.assertTrue(recent.is_dir())

    def test_physical_run_cleanup_skips_live_translator(self) -> None:
        sessions = self.state / "sessions"
        sessions.mkdir(mode=0o700)
        active = sessions / "run.active"
        active.mkdir(mode=0o700)
        (active / ".complete").write_text("{}", encoding="utf-8")
        (active / ".complete").chmod(0o600)
        (active / "claudex-proxy-port").write_text("12345\n", encoding="ascii")
        (active / "claudex-proxy-port").chmod(0o600)
        os.utime(active / ".complete", (1_700_000_000, 1_700_000_000))

        with mock.patch(
            "integrations.common.orichum_sessions._port_is_live",
            return_value=True,
        ):
            result = cleanup_physical_runs(
                self.state,
                older_than_days=7,
                apply=True,
                now=1_701_000_000,
            )

        self.assertEqual(result, ())
        self.assertTrue(active.is_dir())

    def test_logical_session_removal_previews_then_removes_a_leaf(self) -> None:
        parent = self.create(1)
        child = create_logical_session(
            self.state,
            project_root=parent.project_root,
            stack=parent.stack,
            controller=parent.controller,
            agents=parent.agents,
            parent_id=parent.id,
        )

        preview = remove_logical_session(
            self.state, child.id, apply=False
        )

        self.assertEqual((preview.session_id, preview.status), (child.id, "eligible"))
        self.assertEqual(len(list_logical_sessions(self.state)), 2)

        removed = remove_logical_session(
            self.state, child.claude_session_id, apply=True
        )

        self.assertEqual((removed.session_id, removed.status), (child.id, "removed"))
        self.assertEqual(list_logical_sessions(self.state), (parent,))

    def test_logical_session_removal_rejects_active_and_parent_sessions(
        self,
    ) -> None:
        parent = self.create(1)
        child = create_logical_session(
            self.state,
            project_root=parent.project_root,
            stack=parent.stack,
            controller=parent.controller,
            agents=parent.agents,
            parent_id=parent.id,
        )

        with self.assertRaisesRegex(LogicalSessionError, "child session"):
            remove_logical_session(self.state, parent.id, apply=True)

        leases = self.state / "claudex-port-leases"
        leases.mkdir(mode=0o700)
        lease = leases / "13457.json"
        lease.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "runId": "run.active",
                    "sessionId": child.id,
                }
            ),
            encoding="utf-8",
        )
        lease.chmod(0o600)

        with self.assertRaisesRegex(LogicalSessionError, "active"):
            remove_logical_session(self.state, child.id, apply=True)

    def test_logical_session_clear_preserves_active_session_and_parent(
        self,
    ) -> None:
        parent = self.create(1)
        child = create_logical_session(
            self.state,
            project_root=parent.project_root,
            stack=parent.stack,
            controller=parent.controller,
            agents=parent.agents,
            parent_id=parent.id,
        )
        removable = self.create(20)
        leases = self.state / "claudex-port-leases"
        leases.mkdir(mode=0o700)
        lease = leases / "13457.json"
        lease.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "runId": "run.active",
                    "sessionId": child.id,
                }
            ),
            encoding="utf-8",
        )
        lease.chmod(0o600)

        preview = clear_logical_sessions(self.state, apply=False)

        self.assertEqual(
            {item.session_id: item.status for item in preview},
            {
                parent.id: "parent-preserved",
                child.id: "active-preserved",
                removable.id: "eligible",
            },
        )
        applied = clear_logical_sessions(self.state, apply=True)
        self.assertEqual(
            {item.session_id: item.status for item in applied},
            {
                parent.id: "parent-preserved",
                child.id: "active-preserved",
                removable.id: "removed",
            },
        )
        self.assertEqual(
            {session.id for session in list_logical_sessions(self.state)},
            {parent.id, child.id},
        )

    def binding(
        self,
        logical_model: str,
        family: str,
        ordinal: int,
    ) -> RouteBinding:
        def route(index: int) -> Route:
            suffix = f"{ordinal:08x}{index:08x}"
            return Route(
                account_id=f"oc-a-{suffix}",
                provider="anthropic" if family == "claude" else "openai",
                family=family,
                logical_model=logical_model,
                upstream_model=f"oc-r-{suffix}/{logical_model}",
                claudex_profile=f"ocp-{suffix}",
                priority=100 - index,
                pool="work",
            )

        return RouteBinding(primary=route(0), fallbacks=(route(1),))

    def create(self, ordinal: int = 1):
        controller = self.binding("gpt-5.6-sol", "gpt", ordinal)
        agents = {
            role: self.binding(
                "claude-sonnet-5" if role == "correctness-critic"
                else "gpt-5.6-terra",
                "claude" if role == "correctness-critic" else "gpt",
                ordinal + index + 1,
            )
            for index, role in enumerate(ROLES)
        }
        return create_logical_session(
            self.state,
            project_root=Path("/work/project"),
            stack="balanced",
            controller=controller,
            agents=agents,
        )

    def test_create_load_and_list_preserve_private_immutable_binding(self) -> None:
        session = self.create()

        self.assertTrue(session.id.startswith("oc-s-"))
        self.assertRegex(
            session.claude_session_id,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )
        directory = self.state / "logical-sessions" / session.id
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE((directory / "binding.json").stat().st_mode), 0o600
        )
        self.assertEqual(load_logical_session(self.state, session.id), session)
        self.assertEqual(list_logical_sessions(self.state), (session,))
        self.assertIsNone(session.parent_id)
        self.assertEqual(session.controller.primary.family, "gpt")
        self.assertEqual(
            set(session.agents),
            set(ROLES),
        )
        with self.assertRaises(TypeError):
            session.agents[ROLES[0]] = session.controller

    def test_controller_update_preserves_identity_and_agent_bindings(self) -> None:
        session = self.create()
        for family, model in (("gpt", "gpt-6-astra"), ("claude", "claude-opus-5")):
            with self.subTest(family=family):
                controller = self.binding(model, family, 50)
                updated = update_session_controller(self.state, session, controller)
                self.assertEqual(updated, replace(session, controller=controller))
                self.assertEqual(load_logical_session(self.state, session.id), updated)
                self.assertEqual(
                    orichum_sessions.resolve_logical_session(
                        self.state, session.claude_session_id
                    ),
                    updated,
                )
                from integrations.common.route_proxy import RouteIndex

                self.assertEqual(
                    RouteIndex(self.state).routes_for(
                        session.id, controller.primary.upstream_model
                    ),
                    (controller.primary, controller.fallbacks[0]),
                )
                session = updated

    def test_controller_update_is_noop_when_unchanged(self) -> None:
        session = self.create()
        with mock.patch.object(orichum_sessions.os, "replace") as publish:
            self.assertEqual(
                update_session_controller(self.state, session, session.controller),
                session,
            )
        publish.assert_not_called()

    def test_repeating_same_controller_update_is_idempotent(self) -> None:
        session = self.create()
        controller = self.binding("gpt-6-astra", "gpt", 50)
        updated = update_session_controller(self.state, session, controller)
        with mock.patch.object(orichum_sessions.os, "replace") as publish:
            self.assertEqual(
                update_session_controller(self.state, session, controller), updated
            )
        publish.assert_not_called()

    def test_failed_controller_publication_preserves_session(self) -> None:
        session = self.create()
        controller = self.binding("gpt-6-astra", "gpt", 50)
        with mock.patch.object(
            orichum_sessions.os, "replace", side_effect=OSError("disk failure")
        ):
            with self.assertRaisesRegex(OSError, "disk failure"):
                update_session_controller(self.state, session, controller)
        self.assertEqual(load_logical_session(self.state, session.id), session)
        directory = self.state / "logical-sessions" / session.id
        self.assertEqual([path.name for path in directory.iterdir()], ["binding.json"])

    def test_concurrent_controller_changes_do_not_overwrite_each_other(self) -> None:
        session = self.create()

        def update(ordinal: int):
            try:
                return update_session_controller(
                    self.state, session, self.binding("gpt-6-astra", "gpt", ordinal)
                )
            except LogicalSessionError as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = tuple(executor.map(update, (50, 60)))
        successes = [result for result in results if not isinstance(result, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(load_logical_session(self.state, session.id), successes[0])
        failures = [result for result in results if isinstance(result, Exception)]
        self.assertIn("changed during resume", str(failures[0]))

    def _resume_fixture(self, session, *, family="gpt", project_override=False):
        model = "gpt-6-astra" if family == "gpt" else "claude-opus-5"
        provider = "openai" if family == "gpt" else "anthropic"
        stacks = {
            "schemaVersion": 1,
            "defaultStack": "balanced",
            "models": {
                model: {"provider": provider, "family": family, "upstream": model},
                "unavailable-new-agent": {
                    "provider": provider,
                    "family": family,
                    "upstream": "unavailable-new-agent",
                },
            },
            "stacks": {
                "balanced": {
                    "controller": model,
                    "agents": {role: ["unavailable-new-agent"] for role in ROLES},
                }
            },
        }
        account = Account(
            id="oc-a-0000000000000050",
            name="Controller",
            provider=provider,
            credential_ref="controller.json",
            pool="work",
            routing_prefix="oc-r-0000000000000050",
            priority=100,
            state="active",
            original_prefix=None,
            original_priority=None,
        )
        config = SimpleNamespace(
            documents={
                "model-stacks": stacks,
                "projects": {},
                "providers": {
                    "providers": {
                        provider: {"authType": "codex" if family == "gpt" else "claude"}
                    },
                    "accountPools": {"work": {"providers": [provider]}},
                    "fallbackRoutes": {family: [provider]},
                },
            }
        )
        project_models = None
        if project_override:
            project_models = SimpleNamespace(
                stacks=normalize_model_stacks(stacks), stack_name="balanced"
            )
            # The project file, not this machine default, must select the controller.
            config.documents["model-stacks"] = json.loads(
                (
                    Path(__file__).resolve().parents[1] / "config" / "model-stacks.json"
                ).read_text()
            )
        context = {
            "route": {
                "scope": "context",
                "contextRootReal": str(session.project_root),
                "accountPools": ["work"],
                "modelStack": None,
            }
        }
        available = frozenset(
            {
                f"{account.routing_prefix}/{model}",
                *(
                    route.upstream_model
                    for binding in session.agents.values()
                    for route in (binding.primary, *binding.fallbacks)
                ),
            }
        )
        patches = (
            mock.patch.object(orichum_cli, "_verify_runtime"),
            mock.patch.object(
                orichum_cli,
                "resolve_project_context",
                return_value=(context, project_models),
            ),
            mock.patch.object(orichum_cli, "load_accounts", return_value=(account,)),
            mock.patch.object(orichum_cli, "validate_account_bindings"),
            mock.patch.object(orichum_cli, "_validate_session_routes"),
            mock.patch.object(orichum_cli, "_live_models", return_value=available),
            mock.patch.object(
                orichum_cli, "load_stack_bindings", return_value=StackBindings({})
            ),
            mock.patch.object(
                orichum_cli, "create_resolved_session", return_value=object()
            ),
        )
        from contextlib import ExitStack

        stack = ExitStack()
        self.addCleanup(stack.close)
        mocks = tuple(stack.enter_context(patch) for patch in patches)
        return config, mocks

    def test_resume_adopts_configured_controller_without_replacing_conversation(
        self,
    ) -> None:
        for family, project_override in (("gpt", False), ("claude", True)):
            with self.subTest(family=family, project_override=project_override):
                session = self.create()
                config, mocks = self._resume_fixture(
                    session, family=family, project_override=project_override
                )
                prepared = orichum_cli._prepare_resume(
                    {
                        "state": self.state,
                        "data": self.state.parent,
                        "config": self.state.parent / "config",
                    },
                    config,
                    identifier=session.id
                    if family == "gpt"
                    else session.claude_session_id,
                    launch_dir=session.project_root,
                )
                updated = prepared.logical
                self.assertEqual(updated.controller.primary.family, family)
                self.assertNotEqual(updated.controller, session.controller)
                self.assertEqual(
                    updated, replace(session, controller=updated.controller)
                )
                self.assertEqual(load_logical_session(self.state, session.id), updated)
                effective = mocks[-1].call_args.kwargs["effective"]
                self.assertEqual(
                    effective.controller, updated.controller.primary.upstream_model
                )
                self.assertEqual(
                    effective.agents,
                    {
                        role: binding.primary.upstream_model
                        for role, binding in session.agents.items()
                    },
                )
                mocks[4].assert_called_once()
                self.assertEqual(mocks[4].call_args.args[0], updated)

    def test_resume_failure_leaves_controller_and_transcript_identity_intact(
        self,
    ) -> None:
        for failure_stage in ("catalogue", "credentials", "physical-run"):
            with self.subTest(stage=failure_stage):
                session = self.create()
                config, mocks = self._resume_fixture(session)
                if failure_stage == "catalogue":
                    mocks[5].return_value = frozenset()
                elif failure_stage == "credentials":
                    mocks[4].side_effect = orichum_cli.CliError(
                        "credentials unavailable"
                    )
                else:
                    mocks[-1].side_effect = orichum_cli.CliError(
                        "physical run unavailable"
                    )
                with self.assertRaises((LogicalSessionError, orichum_cli.CliError)):
                    orichum_cli._prepare_resume(
                        {
                            "state": self.state,
                            "data": self.state.parent,
                            "config": self.state.parent / "config",
                        },
                        config,
                        identifier=session.id,
                        launch_dir=session.project_root,
                    )
                self.assertEqual(load_logical_session(self.state, session.id), session)

    def test_new_logical_session_persists_leanctx_profile(self) -> None:
        controller = self.binding("gpt-5.6-sol", "gpt", 1)
        agents = {
            role: self.binding("gpt-5.6-terra", "gpt", index + 2)
            for index, role in enumerate(ROLES)
        }

        session = create_logical_session(
            self.state,
            project_root=Path("/work/project"),
            stack="balanced",
            controller=controller,
            agents=agents,
            leanctx_profile="lean",
        )

        binding = (
            self.state
            / "logical-sessions"
            / session.id
            / "binding.json"
        )
        document = json.loads(binding.read_text(encoding="utf-8"))
        self.assertEqual(document["schemaVersion"], 2)
        self.assertEqual(document["leanctxProfile"], "lean")
        self.assertEqual(session.leanctx_profile, "lean")
        self.assertEqual(
            load_logical_session(self.state, session.id).leanctx_profile,
            "lean",
        )

    def test_schema_v1_logical_session_loads_as_full_profile(self) -> None:
        session = self.create()
        binding = (
            self.state
            / "logical-sessions"
            / session.id
            / "binding.json"
        )
        document = json.loads(binding.read_text(encoding="utf-8"))
        document["schemaVersion"] = 1
        document.pop("leanctxProfile", None)
        binding.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        binding.chmod(0o600)

        loaded = load_logical_session(self.state, session.id)

        self.assertEqual(loaded.leanctx_profile, "full")

    def test_legacy_logical_session_inherits_architecture_route(self) -> None:
        session = self.create()
        binding = (
            self.state
            / "logical-sessions"
            / session.id
            / "binding.json"
        )
        document = json.loads(binding.read_text(encoding="utf-8"))
        document["agents"].pop("planning-advisor")
        binding.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        binding.chmod(0o600)

        loaded = load_logical_session(self.state, session.id)

        self.assertEqual(
            loaded.agents["planning-advisor"],
            loaded.agents["architecture-advisor"],
        )

    def test_schema_v2_logical_session_rejects_unknown_leanctx_profile(
        self,
    ) -> None:
        session = self.create()
        binding = (
            self.state
            / "logical-sessions"
            / session.id
            / "binding.json"
        )
        document = json.loads(binding.read_text(encoding="utf-8"))
        document["schemaVersion"] = 2
        document["leanctxProfile"] = "wide"
        binding.write_text(
            json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        binding.chmod(0o600)

        with self.assertRaisesRegex(LogicalSessionError, "profile"):
            load_logical_session(self.state, session.id)

    def test_resolve_accepts_logical_or_claude_session_id(self) -> None:
        session = self.create()
        resolver = getattr(
            orichum_sessions, "resolve_logical_session", None
        )
        self.assertIsNotNone(resolver)

        self.assertEqual(resolver(self.state, session.id), session)
        self.assertEqual(
            resolver(self.state, session.claude_session_id),
            session,
        )

    def test_resolve_rejects_unknown_or_duplicate_claude_session_id(
        self,
    ) -> None:
        resolver = getattr(
            orichum_sessions, "resolve_logical_session", None
        )
        self.assertIsNotNone(resolver)
        unknown = "00000000-0000-4000-8000-000000000099"
        with self.assertRaisesRegex(LogicalSessionError, "not found"):
            resolver(self.state, unknown)

        duplicate = "00000000-0000-4000-8000-000000000001"
        with mock.patch.object(
            orichum_sessions.uuid, "uuid4", return_value=duplicate
        ):
            self.create(1)
            self.create(2)
        with self.assertRaisesRegex(LogicalSessionError, "ambiguous"):
            resolver(self.state, duplicate)

    def test_parent_is_recorded_without_reusing_claude_session_uuid(self) -> None:
        parent = self.create(1)
        child = create_logical_session(
            self.state,
            project_root=parent.project_root,
            stack=parent.stack,
            controller=parent.controller,
            agents=parent.agents,
            parent_id=parent.id,
        )

        self.assertEqual(child.parent_id, parent.id)
        self.assertNotEqual(child.id, parent.id)
        self.assertNotEqual(child.claude_session_id, parent.claude_session_id)

    def test_concurrent_creates_are_distinct_and_complete(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            sessions = tuple(executor.map(self.create, range(1, 17)))

        self.assertEqual(len({session.id for session in sessions}), 16)
        self.assertEqual(
            len({session.claude_session_id for session in sessions}), 16
        )
        self.assertEqual(len(list_logical_sessions(self.state)), 16)

    def test_rejects_tamper_symlink_permissions_and_unknown_session(self) -> None:
        session = self.create()
        directory = self.state / "logical-sessions" / session.id
        binding = directory / "binding.json"

        binding.chmod(0o644)
        with self.assertRaises(LogicalSessionError):
            load_logical_session(self.state, session.id)
        binding.chmod(0o600)

        original = binding.read_bytes()
        binding.unlink()
        outside = self.state / "outside.json"
        outside.write_bytes(original)
        outside.chmod(0o600)
        binding.symlink_to(outside)
        with self.assertRaises(LogicalSessionError):
            load_logical_session(self.state, session.id)

        with self.assertRaises(LogicalSessionError):
            load_logical_session(self.state, "../escape")

    def test_rejects_mixed_family_fallback_and_secret_shaped_route_fields(self) -> None:
        primary = self.binding("gpt-5.6-sol", "gpt", 1).primary
        wrong_family = self.binding("claude-sonnet-5", "claude", 2).primary
        with self.assertRaises(LogicalSessionError):
            create_logical_session(
                self.state,
                project_root=Path("/work/project"),
                stack="balanced",
                controller=RouteBinding(primary, (wrong_family,)),
                agents={
                    role: self.binding("gpt-5.6-terra", "gpt", index + 3)
                    for index, role in enumerate(ROLES)
                },
            )

        extra_fallback = self.binding("gpt-5.6-sol", "gpt", 3).primary
        with self.assertRaisesRegex(LogicalSessionError, "at most one"):
            create_logical_session(
                self.state,
                project_root=Path("/work/project"),
                stack="balanced",
                controller=RouteBinding(
                    primary,
                    (
                        self.binding("gpt-5.6-sol", "gpt", 2).primary,
                        extra_fallback,
                    ),
                ),
                agents={
                    role: self.binding("gpt-5.6-terra", "gpt", index + 20)
                    for index, role in enumerate(ROLES)
                },
            )

        unsafe = Route(
            **{
                **primary.__dict__,
                "upstream_model": "https://token@example.com/model",
            }
        )
        with self.assertRaises(LogicalSessionError):
            create_logical_session(
                self.state,
                project_root=Path("/work/project"),
                stack="balanced",
                controller=RouteBinding(unsafe, ()),
                agents={
                    role: self.binding("gpt-5.6-terra", "gpt", index + 10)
                    for index, role in enumerate(ROLES)
                },
            )

    def test_rejects_non_v4_uuid_parent_project_change_and_unknown_entries(self) -> None:
        session = self.create()
        binding = (
            self.state
            / "logical-sessions"
            / session.id
            / "binding.json"
        )
        document = json.loads(binding.read_text(encoding="utf-8"))
        document["claudeSessionId"] = "00000000-0000-1000-8000-000000000000"
        binding.write_text(json.dumps(document), encoding="utf-8")
        binding.chmod(0o600)
        with self.assertRaisesRegex(LogicalSessionError, "UUID v4"):
            load_logical_session(self.state, session.id)

        parent = self.create(50)
        with self.assertRaisesRegex(LogicalSessionError, "parent project"):
            create_logical_session(
                self.state,
                project_root=Path("/work/other"),
                stack=parent.stack,
                controller=parent.controller,
                agents=parent.agents,
                parent_id=parent.id,
            )

        unexpected = self.state / "logical-sessions" / ".temporary"
        unexpected.mkdir(mode=0o700)
        with self.assertRaisesRegex(LogicalSessionError, "unexpected entry"):
            list_logical_sessions(self.state)


    def test_control_plane_context_rejects_unsafe_and_canonical_alias_roots(
        self,
    ) -> None:
        home = Path(self.temporary.name).resolve() / "home"
        project = home / "work"
        project.mkdir(parents=True)
        alias = home / "alias"
        alias.symlink_to(project, target_is_directory=True)

        def context(root: str) -> dict[str, object]:
            return {
                "root": root,
                "atlassian": None,
                "modelStack": None,
                "accountPools": ["shared"],
            }

        with self.assertRaisesRegex(ContextError, "unsafe"):
            resolve_control_plane_context(
                {
                    "schemaVersion": 1,
                    "contexts": [context("~")],
                },
                home,
                home=home,
            )
        with self.assertRaisesRegex(ContextError, "overlap"):
            resolve_control_plane_context(
                {
                    "schemaVersion": 1,
                    "contexts": [
                        context("~/work"),
                        context("~/alias"),
                    ],
                },
                project,
                home=home,
            )

    def test_incomplete_staging_entries_are_never_visible_to_readers(self) -> None:
        staging = self.state / "logical-session-staging"
        staging.mkdir(mode=0o700)
        pending = staging / "oc-s-0000000000000001"
        pending.mkdir(mode=0o700)

        self.assertEqual(list_logical_sessions(self.state), ())

    def test_session_plan_pins_primary_fallback_and_routed_effective_models(self) -> None:
        models = {
            "gpt-controller": {
                "provider": "openai",
                "family": "gpt",
                "upstream": "gpt-controller",
            },
            "gpt-worker": {
                "provider": "openai",
                "family": "gpt",
                "upstream": "gpt-worker",
            },
        }
        config = {
            "model-stacks": {
                "schemaVersion": 1,
                "defaultStack": "balanced",
                "models": models,
                "stacks": {
                    "balanced": {
                        "controller": "gpt-controller",
                        "agents": {
                            role: ["gpt-worker"] for role in ROLES
                        },
                    }
                },
            },
            "providers": {
                "providers": {"openai": {"authType": "codex"}},
                "accountPools": {"work": {"providers": ["openai"]}},
                "fallbackRoutes": {"gpt": ["openai"]},
            },
        }
        config["model-stacks"] = normalize_model_stacks(
            config["model-stacks"]
        )

        def account(suffix: str, priority: int) -> Account:
            return Account(
                id=f"oc-a-{suffix}",
                name=f"Account {suffix}",
                provider="openai",
                credential_ref=f"codex-{suffix}.json",
                pool="work",
                routing_prefix=f"oc-r-{suffix}",
                priority=priority,
                state="active",
                original_prefix=None,
                original_priority=None,
            )

        plan = resolve_session_plan(
            config,
            (
                account("0000000000000001", 100),
                account("0000000000000002", 50),
            ),
            pools=("work",),
            requested_stack=None,
            health={},
            selection_ordinal=0,
        )

        self.assertEqual(plan.stack, "balanced")
        self.assertEqual(
            plan.controller.primary.upstream_model,
            "oc-r-0000000000000001/gpt-controller",
        )
        self.assertEqual(len(plan.controller.fallbacks), 1)
        self.assertEqual(
            plan.controller.fallbacks[0].account_id,
            "oc-a-0000000000000002",
        )
        self.assertEqual(
            plan.effective.controller,
            plan.controller.primary.upstream_model,
        )
        for role in ROLES:
            self.assertEqual(
                plan.effective.agents[role],
                plan.agents[role].primary.upstream_model,
            )

        preferred = RouteBinding(plan.controller.fallbacks[0], (plan.controller.primary,))
        resumed = resolve_session_plan(
            config,
            (account("0000000000000001", 100), account("0000000000000002", 50)),
            pools=("work",), requested_stack=None, health={}, selection_ordinal=0,
            available_models={
                route.upstream_model for route in (preferred.primary, *preferred.fallbacks)
            },
            pinned_agents=plan.agents,
            preferred_controller=preferred,
        )
        self.assertEqual(resumed.controller, preferred)
        self.assertEqual(resumed.agents, plan.agents)

        without_backup = RouteBinding(plan.controller.primary, ())
        refreshed = resolve_session_plan(
            config,
            (account("0000000000000001", 100), account("0000000000000002", 50)),
            pools=("work",), requested_stack=None, health={}, selection_ordinal=0,
            pinned_agents=plan.agents, preferred_controller=without_backup,
        )
        self.assertEqual(refreshed.controller, plan.controller)

    def test_session_plan_tries_candidates_in_order_and_honors_account_binding(
        self,
    ) -> None:
        controller_candidates = [
            {
                "id": "oc-c-1111111111111111",
                "model": "gpt-unavailable",
                "providers": ["openai"],
            },
            {
                "id": "oc-c-2222222222222222",
                "model": "gpt-controller",
                "providers": ["openai"],
            },
        ]
        agent_candidate = {
            "id": "oc-c-3333333333333333",
            "model": "gpt-worker",
            "providers": ["openai"],
        }
        config = {
            "model-stacks": {
                "schemaVersion": 2,
                "defaultStack": "balanced",
                "models": {
                    "gpt-unavailable": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-unavailable"},
                    },
                    "gpt-controller": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-controller-live"},
                    },
                    "gpt-worker": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-worker-live"},
                    },
                },
                "stacks": {
                    "balanced": {
                        "controller": controller_candidates,
                        "agents": {
                            role: [dict(agent_candidate, id=f"oc-c-{index:016x}")]
                            for index, role in enumerate(ROLES, start=3)
                        },
                    }
                },
            },
            "providers": {
                "providers": {"openai": {"authType": "codex"}},
                "accountPools": {"work": {"providers": ["openai"]}},
                "fallbackRoutes": {"gpt": ["openai"]},
            },
        }

        def account(suffix: str, priority: int) -> Account:
            return Account(
                id=f"oc-a-{suffix}",
                name=f"Account {suffix}",
                provider="openai",
                credential_ref=f"codex-{suffix}.json",
                pool="work",
                routing_prefix=f"oc-r-{suffix}",
                priority=priority,
                state="active",
                original_prefix=None,
                original_priority=None,
            )

        primary = account("0000000000000001", 100)
        secondary = account("0000000000000002", 50)
        bindings = StackBindings(
            {"oc-c-2222222222222222": secondary.id}
        )
        available = {
            f"{primary.routing_prefix}/gpt-controller-live",
            f"{secondary.routing_prefix}/gpt-controller-live",
            f"{primary.routing_prefix}/gpt-worker-live",
            f"{secondary.routing_prefix}/gpt-worker-live",
        }

        plan = resolve_session_plan(
            config,
            (primary, secondary),
            pools=("work",),
            requested_stack=None,
            health={},
            selection_ordinal=0,
            bindings=bindings,
            available_models=available,
        )

        self.assertEqual(plan.controller.primary.account_id, secondary.id)
        self.assertEqual(plan.controller.fallbacks, ())
        self.assertEqual(
            plan.controller.primary.upstream_model,
            f"{secondary.routing_prefix}/gpt-controller-live",
        )


if __name__ == "__main__":
    unittest.main()
