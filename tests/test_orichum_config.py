#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from integrations.common.orichum_config import (
    ConfigError,
    ConfigPaths,
    ResolvedConfig,
    default_config_paths,
    load_control_plane,
    redact_control_plane,
    validate_control_plane,
)


ROLES = (
    "repository-explorer",
    "repository-verifier",
    "correctness-critic",
    "architecture-advisor",
    "implementation-worker",
)


def jira(name: str) -> dict[str, str]:
    return {
        "url": f"https://{name}.atlassian.net",
        "username": f"{name}@example.com",
        "apiToken": f"{name}-token",
    }


class OrichumConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.config = self.root / "config"
        self.config.mkdir()
        (self.root / "xebia").mkdir()
        self.paths = default_config_paths(self.config)
        self.documents = self.valid_documents()
        self.write_documents()

    def valid_documents(self) -> dict[str, object]:
        models = {
            "controller/main": {
                "family": "gpt",
                "routes": {"openai": "controller/main"},
            }
        }
        agents = {}
        for ordinal, role in enumerate(ROLES, start=2):
            model = f"agent/{role}"
            models[model] = {
                "family": "gpt",
                "routes": {"openai": model},
            }
            agents[role] = [
                {
                    "id": f"oc-c-{ordinal:016x}",
                    "model": model,
                    "providers": ["openai"],
                }
            ]
        return {
            "model-stacks": {
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
            },
            "projects": {
                "schemaVersion": 1,
                "contexts": [
                    {
                        "root": str(self.root / "xebia"),
                        "atlassian": jira("xebia"),
                        "modelStack": None,
                        "accountPools": ["work", "shared"],
                    }
                ],
            },
            "providers": {
                "schemaVersion": 1,
                "providers": {
                    "openai": {
                        "type": "openai-compatible",
                        "transport": "cliproxy",
                        "authType": "codex",
                        "families": ["gpt"],
                        "familyPrefixes": {"gpt": ["gpt-"]},
                    },
                    "anthropic": {
                        "type": "anthropic",
                        "transport": "cliproxy",
                        "authType": "claude",
                        "families": ["claude"],
                        "familyPrefixes": {"claude": ["claude-"]},
                    },
                },
                "accountPools": {
                    "work": {"providers": ["openai"]},
                    "claude-only": {"providers": ["anthropic"]},
                    "shared": {"providers": ["openai", "anthropic"]},
                },
                "fallbackRoutes": {
                    "gpt": ["openai"],
                    "claude": ["anthropic"],
                },
            },
            "plugins": {
                "schemaVersion": 1,
                "marketplaces": [],
                "plugins": [],
            },
            "runtime": {
                "schemaVersion": 1,
                "controller": {
                    "effort": "high",
                    "maxToolUseConcurrency": 3,
                    "maxSubagentsPerSession": 24,
                },
            },
        }

    def write_documents(self) -> None:
        mapping = {
            "model-stacks": self.paths.model_stacks,
            "projects": self.paths.projects,
            "providers": self.paths.providers,
            "plugins": self.paths.plugins,
            "runtime": self.paths.runtime,
        }
        for name, path in mapping.items():
            path.write_text(
                json.dumps(self.documents[name]), encoding="utf-8"
            )
        self.paths.controller_policy.write_text(
            "# Orichum controller\n\nUse bounded delegation.\n",
            encoding="utf-8",
        )

    def load(self) -> ResolvedConfig:
        return load_control_plane(self.paths)

    def assert_invalid(self, mutator) -> None:
        mutator(self.documents)
        self.write_documents()
        with self.assertRaises(ConfigError):
            self.load()

    def test_default_paths_load_validate_and_record_relative_sources(self) -> None:
        self.assertEqual(
            self.paths,
            ConfigPaths(
                root=self.config,
                model_stacks=self.config / "model-stacks.json",
                projects=self.config / "projects.json",
                providers=self.config / "providers.json",
                plugins=self.config / "plugins.json",
                runtime=self.config / "runtime.json",
                controller_policy=self.config / "controller-policy.md",
            ),
        )

        resolved = self.load()

        validate_control_plane(resolved)
        self.assertEqual(resolved.documents["model-stacks"]["defaultStack"], "balanced")
        self.assertEqual(
            resolved.sources,
            {
                "model-stacks": "config/model-stacks.json",
                "projects": "config/projects.json",
                "providers": "config/providers.json",
                "plugins": "config/plugins.json",
                "runtime": "config/runtime.json",
                "controller-policy": "config/controller-policy.md",
            },
        )

    def test_rejects_missing_or_duplicate_model_declarations(self) -> None:
        self.assert_invalid(
            lambda documents: documents["model-stacks"]["models"].pop(
                "controller/main"
            )
        )
        self.documents = self.valid_documents()
        self.write_documents()
        raw = self.paths.model_stacks.read_text(encoding="utf-8")
        raw = raw.replace(
            '"controller/main": {',
            '"controller/main": {"provider":"openai","family":"gpt",'
            '"upstream":"duplicate"}, "controller/main": {',
            1,
        )
        self.paths.model_stacks.write_text(raw, encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.load()

    def test_rejects_missing_roles_unknown_roles_and_empty_candidates(self) -> None:
        self.assert_invalid(
            lambda documents: documents["model-stacks"]["stacks"]["balanced"][
                "agents"
            ].pop("repository-explorer")
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: documents["model-stacks"]["stacks"]["balanced"][
                "agents"
            ].update({"unknown": ["agent/repository-explorer"]})
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: documents["model-stacks"]["stacks"]["balanced"][
                "agents"
            ].update({"repository-explorer": []})
        )

    def test_rejects_unknown_stack_provider_pool_and_family_routes(self) -> None:
        self.assert_invalid(
            lambda documents: documents["projects"]["contexts"][0].update(
                {"modelStack": "missing"}
            )
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: (
                documents["model-stacks"]["models"]["controller/main"].update(
                    {"routes": {"missing": "controller/main"}}
                ),
                documents["model-stacks"]["stacks"]["balanced"]["controller"][
                    0
                ].update({"providers": ["missing"]}),
            )
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: documents["projects"]["contexts"][0][
                "accountPools"
            ].append("missing")
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: documents["providers"]["fallbackRoutes"].update(
                {"gpt": ["missing"]}
            )
        )

    def test_validates_a_routable_normal_scope(self) -> None:
        self.documents["projects"] = {
            "schemaVersion": 2,
            "normal": {
                "modelStack": "balanced",
                "accountPools": ["shared"],
            },
            "contexts": [],
        }
        self.write_documents()

        self.load()
        self.assert_invalid(
            lambda documents: documents["projects"]["normal"].update(
                {"accountPools": ["missing"]}
            )
        )

    def test_rejects_provider_family_mismatch_and_unrouted_model_family(self) -> None:
        self.assert_invalid(
            lambda documents: documents["providers"]["providers"]["openai"][
                "families"
            ].clear()
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: documents["providers"]["fallbackRoutes"].pop(
                "gpt"
            )
        )
        self.documents = self.valid_documents()
        self.write_documents()
        self.assert_invalid(
            lambda documents: documents["providers"]["providers"]["openai"][
                "families"
            ].append("future")
        )

    def test_rejects_invalid_provider_family_prefixes(self) -> None:
        mutations = (
            lambda provider: provider["familyPrefixes"].update(
                {"future": ["future-"]}
            ),
            lambda provider: provider["familyPrefixes"].update(
                {"gpt": []}
            ),
            lambda provider: provider["familyPrefixes"].update(
                {"gpt": [""]}
            ),
            lambda provider: provider["familyPrefixes"].update(
                {"gpt": ["gpt/", "gpt-"]}
            ),
            lambda provider: provider["familyPrefixes"].update(
                {"gpt": ["gpt-", "gpt-"]}
            ),
            lambda provider: provider["familyPrefixes"].update(
                {"gpt": ["gpt-", "gpt-5"]}
            ),
            lambda provider: provider.update({"familyPrefixes": {}}),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.documents = self.valid_documents()
                mutation(
                    self.documents["providers"]["providers"]["openai"]
                )
                self.write_documents()
                with self.assertRaises(ConfigError):
                    self.load()

    def test_rejects_project_pools_that_cannot_route_the_selected_stack(self) -> None:
        self.assert_invalid(
            lambda documents: documents["projects"]["contexts"][0].update(
                {"accountPools": ["claude-only"]}
            )
        )

    def test_rejects_unknown_provider_adapter_type(self) -> None:
        self.assert_invalid(
            lambda documents: documents["providers"]["providers"]["openai"].update(
                {"type": "not-a-real-adapter"}
            )
        )

    def test_rejects_invalid_runtime_limits_and_safety_invariants(self) -> None:
        for mutation in (
            lambda runtime: runtime["controller"].update({"effort": "ultra"}),
            lambda runtime: runtime["controller"].update(
                {"maxToolUseConcurrency": True}
            ),
            lambda runtime: runtime["controller"].update(
                {"maxToolUseConcurrency": 0}
            ),
            lambda runtime: runtime["controller"].update(
                {"maxSubagentsPerSession": 0}
            ),
        ):
            self.documents = self.valid_documents()
            mutation(self.documents["runtime"])
            self.write_documents()
            with self.assertRaises(ConfigError):
                self.load()

    def test_rejects_unknown_top_level_keys_non_finite_and_empty_policy(self) -> None:
        for name in ("model-stacks", "projects", "providers", "plugins", "runtime"):
            self.documents = self.valid_documents()
            self.documents[name]["unexpected"] = True
            self.write_documents()
            with self.assertRaises(ConfigError):
                self.load()

        self.documents = self.valid_documents()
        self.write_documents()
        self.paths.runtime.write_text(
            self.paths.runtime.read_text().replace("24", "NaN", 1),
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            self.load()

        self.documents = self.valid_documents()
        self.write_documents()
        self.paths.controller_policy.write_text(" \n", encoding="utf-8")
        with self.assertRaises(ConfigError):
            self.load()

    def test_redaction_is_recursive_and_does_not_mutate_resolved_state(self) -> None:
        resolved = self.load()
        documents = deepcopy(dict(resolved.documents))
        documents["machine-local"] = {
            "token": "secret-token",
            "nested": {
                "apiKey": "secret-key",
                "authorization": "Bearer top-secret",
                "endpoint": "https://user:password@example.test/path",
                "signed": "https://example.test/path?X-Amz-Signature=secret",
                "azure": "https://example.test/blob?sig=top-secret",
                "google": "https://example.test/api?key=AIzaExample",
                "name": "Work Claude",
            },
        }
        augmented = ResolvedConfig(documents=documents, sources=resolved.sources)

        redacted = redact_control_plane(augmented)

        self.assertEqual(redacted["machine-local"]["token"], "<redacted>")
        self.assertEqual(
            redacted["projects"]["contexts"][0]["atlassian"]["apiToken"],
            "<redacted>",
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["apiKey"], "<redacted>"
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["authorization"], "<redacted>"
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["endpoint"], "<redacted>"
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["signed"], "<redacted>"
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["azure"], "<redacted>"
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["google"], "<redacted>"
        )
        self.assertEqual(
            redacted["machine-local"]["nested"]["name"], "Work Claude"
        )
        self.assertEqual(
            redacted["controller-policy"], "<policy omitted>"
        )
        self.assertEqual(
            augmented.documents["machine-local"]["token"], "secret-token"
        )

    def test_rejects_credentials_in_free_form_sources_and_policy(self) -> None:
        for source in (
            "https://user:password@example.test/plugin.git",
            "https://example.test/plugin.git?api-key=top-secret",
            "https://example.test/plugin.git?access_key=AKIAEXAMPLE",
            "https://example.test/plugin.git?X-Amz-Signature=top-secret",
            "https://example.test/blob?sig=top-secret",
            "https://example.test/api?key=AIzaExample",
        ):
            self.documents = self.valid_documents()
            self.documents["plugins"]["marketplaces"] = [
                {"name": "private", "source": source}
            ]
            self.write_documents()
            with self.assertRaises(ConfigError):
                self.load()

        self.documents = self.valid_documents()
        self.write_documents()
        self.paths.controller_policy.write_text(
            "# Policy\n\nAuthorization: Bearer top-secret\n",
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            self.load()

    def test_allows_benign_basic_and_bearer_policy_prose(self) -> None:
        self.paths.controller_policy.write_text(
            "# Policy\n\n"
            "Use basic validation before deployment. "
            "A bearer process may transfer the request.\n",
            encoding="utf-8",
        )

        self.load()

    def test_sources_are_complete_and_custom_paths_are_not_misreported(self) -> None:
        resolved = self.load()
        with self.assertRaises(ConfigError):
            validate_control_plane(
                ResolvedConfig(documents=resolved.documents, sources={})
            )

        custom_runtime = self.config / ".." / "custom-runtime.json"
        custom_runtime.write_text(
            self.paths.runtime.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        custom_paths = ConfigPaths(
            root=self.paths.root,
            model_stacks=self.paths.model_stacks,
            projects=self.paths.projects,
            providers=self.paths.providers,
            plugins=self.paths.plugins,
            runtime=custom_runtime,
            controller_policy=self.paths.controller_policy,
        )

        custom = load_control_plane(custom_paths)

        self.assertEqual(
            custom.sources["runtime"],
            str((self.root / "custom-runtime.json").resolve()),
        )


if __name__ == "__main__":
    unittest.main()
