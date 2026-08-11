#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from integrations.common.project_models import (
    ProjectModelsError,
    discover_project_models,
    resolve_project_context,
)
from integrations.common.stack_definition import normalize_model_stacks

_ROLES = (
    "repository-explorer",
    "repository-verifier",
    "correctness-critic",
    "architecture-advisor",
    "implementation-worker",
)


def _stacks():
    return normalize_model_stacks(
        {
            "schemaVersion": 2,
            "defaultStack": "default",
            "models": {
                "gpt-fast": {
                    "family": "gpt",
                    "routes": {
                        "openai": "gpt-fast",
                        "other": "gpt-fast-upstream",
                    },
                },
                "claude-quality": {
                    "family": "claude",
                    "routes": {"anthropic": "claude-quality"},
                },
            },
            "stacks": {
                "default": {
                    "controller": [
                        {
                            "id": "oc-c-1111111111111111",
                            "model": "gpt-fast",
                            "providers": ["openai"],
                        }
                    ],
                    "agents": {
                        role: [
                            {
                                "id": f"oc-c-{index:016x}",
                                "model": "gpt-fast",
                                "providers": ["openai"],
                            }
                        ]
                        for index, role in enumerate(_ROLES, start=2)
                    },
                }
            },
        }
    )


def _document(
    controller: str = "gpt-fast",
    jira_profile: str | None = "work",
    github_account: str | None = "work-account",
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "controller": controller,
        "agents": {
            role: "claude-quality" if role == "architecture-advisor" else "gpt-fast"
            for role in _ROLES
        },
        "jiraProfile": jira_profile,
        "githubAccount": github_account,
    }


def _legacy_document(controller: str = "gpt-fast") -> dict[str, object]:
    document = _document(controller)
    del document["jiraProfile"]
    del document["githubAccount"]
    return document


def _projects(root: Path) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "contexts": [
            {
                "root": str(root),
                "modelStack": "default",
                "accountPools": ["shared"],
                "atlassian": {
                    "url": "https://legacy.atlassian.net",
                    "username": "legacy@example.com",
                    "apiToken": "legacy-token",
                },
                "githubAccount": "legacy-account",
            }
        ],
    }


def _write(root: Path, document: object, filename: str = "config.json") -> Path:
    directory = root / ".orichum"
    directory.mkdir(exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class ProjectModelsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.outer = Path(self.temporary.name).resolve()
        self.root = self.outer / "context"
        self.child = self.root / "repo" / "src"
        self.child.mkdir(parents=True)
        self.stacks = _stacks()
        self.profiles = self.outer / "jira-profiles.json"
        self.profiles.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "profiles": {
                        "work": {
                            "url": "https://work.atlassian.net",
                            "username": "work@example.com",
                            "apiToken": "work-token",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.profiles.chmod(0o600)

    def test_absence_uses_machine_configuration(self) -> None:
        self.assertIsNone(discover_project_models(self.child, self.root, self.stacks))

    def test_nearest_configuration_controls_models_and_services(self) -> None:
        _write(self.root, _document("claude-quality", None, None))
        nearest = self.child.parent
        path = _write(nearest, _document("gpt-fast"))

        resolved, loaded = resolve_project_context(
            _projects(self.root),
            self.child,
            self.profiles,
            self.stacks,
        )

        assert loaded is not None
        self.assertEqual(loaded.path, path)
        self.assertTrue(loaded.manages_services)
        self.assertEqual(loaded.assignments["controller"], "gpt-fast")
        self.assertEqual(tuple(loaded.stacks.stacks), (loaded.stack_name,))
        controller = loaded.stacks.stacks[loaded.stack_name].controller[0]
        self.assertEqual(controller.providers, ("openai", "other"))
        route = resolved["route"]
        self.assertIs(route["atlassianConfigured"], True)
        self.assertEqual(route["jiraProfile"], "work")
        self.assertEqual(route["githubAccount"], "work-account")
        self.assertEqual(route["projectConfigSource"], str(path))
        self.assertEqual(len(route["projectConfigDigest"]), 64)

    def test_explicit_null_disables_machine_service_defaults(self) -> None:
        _write(self.child, _document(jira_profile=None, github_account=None))

        resolved, loaded = resolve_project_context(
            _projects(self.root),
            self.child,
            self.profiles,
            self.stacks,
        )

        assert loaded is not None
        route = resolved["route"]
        self.assertIs(route["atlassianConfigured"], False)
        self.assertIsNone(route["jiraProfile"])
        self.assertIsNone(route["githubAccount"])

    def test_legacy_models_file_keeps_machine_service_defaults(self) -> None:
        path = _write(self.child, _legacy_document(), "models.json")

        resolved, loaded = resolve_project_context(
            _projects(self.root),
            self.child,
            self.profiles,
            self.stacks,
        )

        assert loaded is not None
        self.assertEqual(loaded.path, path)
        self.assertFalse(loaded.manages_services)
        route = resolved["route"]
        self.assertIs(route["atlassianConfigured"], True)
        self.assertEqual(route["githubAccount"], "legacy-account")

    def test_context_root_is_included_and_parent_is_ignored(self) -> None:
        _write(self.outer, _document("claude-quality"))
        path = _write(self.root, _document("gpt-fast"))

        loaded = discover_project_models(self.child, self.root, self.stacks)

        assert loaded is not None
        self.assertEqual(loaded.path, path)

    def test_invalid_nearest_file_and_unknown_profile_fail_closed(self) -> None:
        _write(self.root, _document())
        path = _write(self.child.parent, _document())
        path.write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ProjectModelsError, "invalid JSON"):
            discover_project_models(self.child, self.root, self.stacks)

        path.write_text(json.dumps(_document(jira_profile="missing")), encoding="utf-8")
        with self.assertRaisesRegex(ProjectModelsError, "is not configured"):
            resolve_project_context(
                _projects(self.root),
                self.child,
                self.profiles,
                self.stacks,
            )

    def test_rejects_duplicate_keys_unknown_models_and_boolean_schema(self) -> None:
        cases = (
            (
                '{"schemaVersion":1,"schemaVersion":1,"controller":"gpt-fast",'
                '"agents":{},"jiraProfile":null,"githubAccount":null}',
                "duplicate key",
            ),
            (json.dumps(_document("missing-model")), "unknown logical model"),
            (
                json.dumps({**_document(), "schemaVersion": True}),
                "schemaVersion must be exactly 1",
            ),
        )
        for content, message in cases:
            with self.subTest(message=message):
                path = _write(self.child, _document())
                path.write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ProjectModelsError, message):
                    discover_project_models(self.child, self.root, self.stacks)

    def test_rejects_missing_roles_both_files_and_symlinks(self) -> None:
        agents = {role: "gpt-fast" for role in _ROLES[:-1]}
        _write(self.child, {**_document(), "agents": agents})
        with self.assertRaisesRegex(ProjectModelsError, "agents must contain"):
            discover_project_models(self.child, self.root, self.stacks)

        _write(self.child, _legacy_document(), "models.json")
        with self.assertRaisesRegex(ProjectModelsError, "cannot both be present"):
            discover_project_models(self.child, self.root, self.stacks)

        directory = self.child / ".orichum"
        (directory / "config.json").unlink()
        (directory / "models.json").unlink()
        target = self.outer / "target.json"
        target.write_text(json.dumps(_document()), encoding="utf-8")
        os.symlink(target, directory / "config.json")
        with self.assertRaisesRegex(ProjectModelsError, "regular file"):
            discover_project_models(self.child, self.root, self.stacks)

    def test_rejects_symlinked_directory_oversized_and_non_utf8_files(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "config.json").write_text(json.dumps(_document()), encoding="utf-8")
        os.symlink(target, self.child / ".orichum")
        with self.assertRaisesRegex(ProjectModelsError, "real directory"):
            discover_project_models(self.child, self.root, self.stacks)
        (self.child / ".orichum").unlink()

        directory = self.child / ".orichum"
        directory.mkdir()
        path = directory / "config.json"
        for content, message in (
            (b"x" * (16 * 1024 + 1), "no larger than 16 KiB"),
            (b"\xff", "UTF-8 JSON"),
        ):
            with self.subTest(message=message):
                path.write_bytes(content)
                with self.assertRaisesRegex(ProjectModelsError, message):
                    discover_project_models(self.child, self.root, self.stacks)


if __name__ == "__main__":
    unittest.main()
