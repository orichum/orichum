from pathlib import Path
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import integrations.common.model_routing as model_routing
from integrations.common.model_routing import (
    EffectiveStack,
    ROLES,
    RoutingError,
    load_catalog,
    load_routing,
    materialize_runtime_plugin,
    resolve_effective,
    validate_stack_name,
)
from integrations.common.stack_definition import normalize_model_stacks


class ModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve(strict=True)
        self.routing_path = self.root / "model-routing.json"
        self.catalog_path = self.root / "models.json"
        agents = {
            role: ["preferred/" + role, "fallback/" + role]
            for role in ROLES
        }
        self.routing = {
            "schemaVersion": 1,
            "defaultStack": "balanced",
            "stacks": {
                "balanced": {
                    "controller": "controller/main",
                    "agents": agents,
                },
                "xebia": {
                    "controller": "controller/xebia",
                    "agents": agents,
                },
            },
        }
        self.routing_path.write_text(json.dumps(self.routing), encoding="utf-8")
        available = ["controller/main"] + [
            "fallback/" + role for role in ROLES
        ]
        self.catalog_path.write_text(
            json.dumps({"object": "list", "data": [
                {"id": model} for model in available
            ]}),
            encoding="utf-8",
        )
        self.run_dir = self.root / "run.test"
        self.run_dir.mkdir(mode=0o700)
        self.source_plugin = self.root / "source-plugin"
        (self.source_plugin / "agents").mkdir(parents=True)
        (self.source_plugin / "workflows").mkdir()
        for role in ROLES:
            tools = (
                "mcp__leanctx__ctx_read, mcp__leanctx__ctx_search, "
                "mcp__leanctx__ctx_tree, mcp__leanctx__ctx_expand, "
                "mcp__leanctx__ctx_graph, mcp__leanctx__ctx_impact, "
                "mcp__leanctx__ctx_callgraph"
            )
            if role == "implementation-worker":
                tools += (
                    ", mcp__leanctx__ctx_patch, "
                    "mcp__leanctx__ctx_shell, Edit, Write, Bash"
                )
            (self.source_plugin / "agents" / f"{role}.md").write_text(
                "---\n"
                f"name: {role}\n"
                "mcpServers: [leanctx]\n"
                f"tools: {tools}\n"
                "model: inherit\n"
                "---\n"
                f"{role} instructions\n",
                encoding="utf-8",
            )
        (self.source_plugin / "workflows" / "review.js").write_text(
            "export default 'unchanged';\n",
            encoding="utf-8",
        )
        self.effective = EffectiveStack(
            "balanced",
            "controller/main",
            {role: ("preferred/" + role,) for role in ROLES},
            {role: "preferred/" + role for role in ROLES},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def focused_model_stacks(self) -> dict[str, object]:
        return {
            "schemaVersion": 2,
            "defaultStack": "balanced",
            "models": {
                "controller/main": {
                    "family": "gpt",
                    "routes": {"openai": "upstream/controller"},
                },
                "agent/main": {
                    "family": "gpt",
                    "routes": {"openai": "upstream/agent"},
                },
            },
            "stacks": {
                "balanced": {
                    "controller": [
                        {
                            "id": "oc-c-0000000000000001",
                            "model": "controller/main",
                            "providers": ["openai"],
                        }
                    ],
                    "agents": {
                        role: [
                            {
                                "id": f"oc-c-{index:016x}",
                                "model": "agent/main",
                                "providers": ["openai"],
                            }
                        ]
                        for index, role in enumerate(ROLES, start=2)
                    },
                }
            },
        }

    def test_default_stack_uses_ordered_agent_fallbacks(self) -> None:
        routing = load_routing(self.routing_path)
        effective = resolve_effective(
            routing, load_catalog(self.catalog_path)
        )
        self.assertEqual(effective.stack_name, "balanced")
        self.assertEqual(effective.controller, "controller/main")
        self.assertEqual(
            effective.agents,
            {role: "fallback/" + role for role in ROLES},
        )

    def test_legacy_routing_inherits_architecture_candidates_for_planning(self) -> None:
        routing = json.loads(json.dumps(self.routing))
        for stack in routing["stacks"].values():
            stack["agents"].pop("planning-advisor")
        self.routing_path.write_text(json.dumps(routing), encoding="utf-8")

        effective = resolve_effective(
            load_routing(self.routing_path), load_catalog(self.catalog_path)
        )

        self.assertEqual(
            effective.candidates["planning-advisor"],
            effective.candidates["architecture-advisor"],
        )
        self.assertEqual(
            effective.agents["planning-advisor"],
            effective.agents["architecture-advisor"],
        )

    def test_candidate_stack_resolves_ordered_logical_model_ids(self) -> None:
        models = {
            "controller/main": {
                "family": "gpt",
                "routes": {"openai": "upstream/controller"},
            }
        }
        agents = {}
        next_id = 2
        for role in ROLES:
            preferred = "preferred/" + role
            fallback = "fallback/" + role
            models[preferred] = {
                "family": "gpt",
                "routes": {"openai": "upstream/" + preferred},
            }
            models[fallback] = {
                "family": "gpt",
                "routes": {"openai": "upstream/" + fallback},
            }
            agents[role] = [
                {
                    "id": f"oc-c-{next_id:016x}",
                    "model": preferred,
                    "providers": ["openai"],
                },
                {
                    "id": f"oc-c-{next_id + 1:016x}",
                    "model": fallback,
                    "providers": ["openai"],
                },
            ]
            next_id += 2
        normalized = normalize_model_stacks(
            {
                "schemaVersion": 2,
                "defaultStack": "balanced",
                "models": models,
                "stacks": {
                    "balanced": {
                        "controller": [
                            {
                                "id": "oc-c-0000000000000001",
                                "model": "controller/main",
                                "providers": ["openai"],
                            }
                        ],
                        "agents": agents,
                    }
                },
            }
        )

        effective = resolve_effective(
            normalized,
            ["controller/main"]
            + ["fallback/" + role for role in ROLES],
        )

        self.assertEqual(effective.controller, "controller/main")
        self.assertEqual(
            effective.candidates,
            {
                role: (
                    "preferred/" + role,
                    "fallback/" + role,
                )
                for role in ROLES
            },
        )
        self.assertEqual(
            effective.agents,
            {role: "fallback/" + role for role in ROLES},
        )

    def test_catalogue_marks_all_normalized_stack_candidates_configured(
        self,
    ) -> None:
        document = self.focused_model_stacks()
        document["models"]["agent/fallback"] = {
            "family": "gpt",
            "routes": {"openai": "upstream/fallback"},
        }
        document["stacks"]["balanced"]["agents"][
            "repository-explorer"
        ].append(
            {
                "id": "oc-c-0000000000000008",
                "model": "agent/fallback",
                "providers": ["openai"],
            }
        )
        normalized = normalize_model_stacks(document)

        rendered = model_routing._render_catalogue_table(
            normalized,
            ("controller/main", "agent/main", "agent/fallback"),
            None,
            None,
        )

        for model in ("controller/main", "agent/main", "agent/fallback"):
            self.assertIn(
                f"{model}".ljust(len("controller/main"))
                + " | configured candidate",
                rendered,
            )

    def test_routing_view_accepts_focused_model_stacks_document(self) -> None:
        focused = {
            "schemaVersion": 2,
            "defaultStack": "balanced",
            "models": {
                "controller/main": {
                    "family": "gpt",
                    "routes": {"openai": "upstream/controller"},
                },
                **{
                    "fallback/" + role: {
                        "family": "gpt",
                        "routes": {"openai": "upstream/" + role},
                    }
                    for role in ROLES
                },
            },
            "stacks": {
                "balanced": {
                    "controller": [
                        {
                            "id": "oc-c-0000000000000001",
                            "model": "controller/main",
                            "providers": ["openai"],
                        }
                    ],
                    "agents": {
                        role: [
                            {
                                "id": f"oc-c-{index:016x}",
                                "model": "fallback/" + role,
                                "providers": ["openai"],
                            }
                        ]
                        for index, role in enumerate(ROLES, start=2)
                    },
                }
            },
        }
        self.routing_path.write_text(json.dumps(focused), encoding="utf-8")

        loaded = model_routing.load_routing_view(self.routing_path)

        effective = resolve_effective(
            loaded,
            ["controller/main"]
            + ["fallback/" + role for role in ROLES],
        )
        self.assertEqual(effective.stack_name, "balanced")
        self.assertEqual(effective.controller, "controller/main")

    def test_routing_view_rejects_duplicate_top_level_key(self) -> None:
        raw = json.dumps(self.focused_model_stacks()).replace(
            '"defaultStack": "balanced"',
            '"defaultStack": "balanced", "defaultStack": "balanced"',
            1,
        )
        self.routing_path.write_text(raw, encoding="utf-8")

        with self.assertRaisesRegex(RoutingError, "duplicate JSON key"):
            model_routing.load_routing_view(self.routing_path)

    def test_routing_view_rejects_duplicate_nested_key(self) -> None:
        raw = json.dumps(self.focused_model_stacks()).replace(
            '"family": "gpt"',
            '"family": "gpt", "family": "gpt"',
            1,
        )
        self.routing_path.write_text(raw, encoding="utf-8")

        with self.assertRaisesRegex(RoutingError, "duplicate JSON key"):
            model_routing.load_routing_view(self.routing_path)

    def test_materialized_plugin_rewrites_only_model_frontmatter(self) -> None:
        plugin = materialize_runtime_plugin(
            self.source_plugin, self.run_dir / "plugin", self.effective
        )
        for role in ROLES:
            text = (plugin / "agents" / f"{role}.md").read_text()
            self.assertIn("mcpServers: [leanctx]", text)
            self.assertIn("mcp__leanctx__ctx_read", text)
            self.assertNotIn("tools: Read", text)
            self.assertEqual(
                [
                    line
                    for line in text.splitlines()
                    if line.startswith("model: ")
                ],
                [f"model: {self.effective.agents[role]}"],
            )
        self.assertEqual(
            (plugin / "workflows" / "review.js").read_bytes(),
            (self.source_plugin / "workflows" / "review.js").read_bytes(),
        )

    def test_materialized_plugin_is_private_and_preserves_executable_files(
        self,
    ) -> None:
        executable = self.source_plugin / "workflows" / "run.sh"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        plugin = materialize_runtime_plugin(
            self.source_plugin, self.run_dir / "plugin", self.effective
        )

        self.assertEqual(plugin.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (plugin / "workflows" / "review.js").stat().st_mode & 0o777,
            0o600,
        )
        self.assertEqual(
            (plugin / "workflows" / "run.sh").stat().st_mode & 0o777,
            0o700,
        )

    def test_materialized_plugin_rejects_symlink(self) -> None:
        link = self.source_plugin / "workflows" / "linked.js"
        link.symlink_to(self.source_plugin / "workflows" / "review.js")
        with self.assertRaises(RoutingError):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_agent_without_leanctx_contract(
        self,
    ) -> None:
        role = ROLES[0]
        agent = self.source_plugin / "agents" / f"{role}.md"
        text = agent.read_text(encoding="utf-8")
        agent.write_text(
            text.replace(
                "tools: mcp__leanctx__ctx_read, "
                "mcp__leanctx__ctx_search, "
                "mcp__leanctx__ctx_tree, "
                "mcp__leanctx__ctx_expand, "
                "mcp__leanctx__ctx_graph, "
                "mcp__leanctx__ctx_impact, "
                "mcp__leanctx__ctx_callgraph",
                "tools: Read, Glob, Grep",
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RoutingError, "LeanCTX tool contract"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )

        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_duplicate_sensitive_frontmatter(
        self,
    ) -> None:
        role = ROLES[0]
        agent = self.source_plugin / "agents" / f"{role}.md"
        text = agent.read_text(encoding="utf-8")
        agent.write_text(
            text.replace(
                f"name: {role}\n",
                f"name: {role}\ntools: Read, Glob, Grep\n",
                1,
            ),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(RoutingError, "LeanCTX tool contract"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )

    def test_materialized_plugin_rejects_existing_destination(self) -> None:
        (self.run_dir / "plugin").mkdir()
        with self.assertRaises(RoutingError):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )

    def test_materialized_plugin_rejects_missing_agent(self) -> None:
        (self.source_plugin / "agents" / f"{ROLES[0]}.md").unlink()
        with self.assertRaises(RoutingError):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_invalid_agent_frontmatter(self) -> None:
        agent = self.source_plugin / "agents" / f"{ROLES[0]}.md"
        agent.write_text("model: inherit\n", encoding="utf-8")
        with self.assertRaisesRegex(RoutingError, "invalid frontmatter"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_rejects_special_file(self) -> None:
        os.mkfifo(self.source_plugin / "workflows" / "special")
        with self.assertRaisesRegex(RoutingError, "special file"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )
        self.assertFalse((self.run_dir / "plugin").exists())

    def test_materialized_plugin_requires_private_destination_parent(
        self,
    ) -> None:
        self.run_dir.chmod(0o755)
        with self.assertRaisesRegex(RoutingError, "unsafe permissions"):
            materialize_runtime_plugin(
                self.source_plugin, self.run_dir / "plugin", self.effective
            )

    def test_materialized_plugin_rejects_foreign_owned_nested_directory(
        self,
    ) -> None:
        nested = self.source_plugin / "workflows"
        real_lstat = os.lstat

        def foreign_owner(path, *args, **kwargs):
            observed = real_lstat(path, *args, **kwargs)
            if not args and not kwargs and Path(path) == nested:
                values = list(observed)
                values[4] = observed.st_uid + 1
                return os.stat_result(values)
            return observed

        with mock.patch.object(
            model_routing.os, "lstat", side_effect=foreign_owner
        ):
            with self.assertRaisesRegex(RoutingError, "owner"):
                materialize_runtime_plugin(
                    self.source_plugin,
                    self.run_dir / "plugin",
                    self.effective,
                )

    def test_materialized_plugin_rejects_checked_directory_replacement(
        self,
    ) -> None:
        source_agents = self.source_plugin / "agents"
        replacement = self.root / "replacement-agents"
        displaced = self.root / "displaced-agents"
        shutil.copytree(source_agents, replacement)
        real_mkdir = os.mkdir
        swapped = False

        def swap_after_check(path, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            path = Path(path)
            if (
                not swapped
                and dir_fd is None
                and path == self.run_dir / "plugin" / "agents"
            ):
                source_agents.rename(displaced)
                replacement.rename(source_agents)
                swapped = True
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with mock.patch.object(
            model_routing.os, "mkdir", side_effect=swap_after_check
        ):
            with self.assertRaisesRegex(RoutingError, "changed"):
                materialize_runtime_plugin(
                    self.source_plugin,
                    self.run_dir / "plugin",
                    self.effective,
                )
        self.assertTrue(swapped)

    def test_materialized_plugin_rejects_in_place_source_mutation(
        self,
    ) -> None:
        target = self.source_plugin / "workflows" / "review.js"
        target_inode = os.lstat(target).st_ino
        real_read = os.read
        mutated = False

        def mutate_during_read(descriptor: int, size: int) -> bytes:
            nonlocal mutated
            data = real_read(descriptor, size)
            if not mutated and os.fstat(descriptor).st_ino == target_inode:
                target.write_text("changed during read with a new size\n")
                mutated = True
            return data

        with mock.patch.object(
            model_routing.os, "read", side_effect=mutate_during_read
        ):
            with self.assertRaisesRegex(RoutingError, "changed"):
                materialize_runtime_plugin(
                    self.source_plugin,
                    self.run_dir / "plugin",
                    self.effective,
                )
        self.assertTrue(mutated)

    def test_explicit_stack_with_missing_controller_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            RoutingError, "controller/xebia.*unavailable"
        ):
            resolve_effective(
                load_routing(self.routing_path),
                load_catalog(self.catalog_path),
                "xebia",
            )

    def test_unknown_stack_is_rejected(self) -> None:
        with self.assertRaisesRegex(RoutingError, "stack.*missing"):
            resolve_effective(
                load_routing(self.routing_path),
                load_catalog(self.catalog_path),
                "missing",
            )

    def test_model_id_cannot_inject_frontmatter(self) -> None:
        self.routing["stacks"]["balanced"]["controller"] = (
            "safe\nmodel: injected"
        )
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaisesRegex(RoutingError, "model ID"):
            load_routing(self.routing_path)

    def test_unknown_role_is_rejected(self) -> None:
        agents = self.routing["stacks"]["balanced"]["agents"]
        agents["unknown-role"] = ["provider/model"]
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaises(RoutingError):
            load_routing(self.routing_path)

    def test_duplicate_candidates_are_rejected(self) -> None:
        agents = self.routing["stacks"]["balanced"]["agents"]
        agents["repository-explorer"] = ["same/model", "same/model"]
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaises(RoutingError):
            load_routing(self.routing_path)

    def test_boolean_schema_version_is_rejected(self) -> None:
        self.routing["schemaVersion"] = True
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaisesRegex(RoutingError, "schemaVersion"):
            load_routing(self.routing_path)

    def test_float_schema_version_is_rejected(self) -> None:
        self.routing["schemaVersion"] = 1.0
        self.routing_path.write_text(
            json.dumps(self.routing), encoding="utf-8"
        )
        with self.assertRaisesRegex(RoutingError, "schemaVersion"):
            load_routing(self.routing_path)

    def test_validate_stack_name_returns_safe_name(self) -> None:
        self.assertEqual(validate_stack_name("balanced"), "balanced")

    def test_validate_stack_name_rejects_invalid_values(self) -> None:
        for value in (None, "", "Upper", "safe\nstack", "a" * 64):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    RoutingError, "project model stack.*invalid"
                ):
                    validate_stack_name(value, "project model stack")
