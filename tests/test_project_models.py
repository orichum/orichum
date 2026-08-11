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


def _document(controller: str = "gpt-fast") -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "controller": controller,
        "agents": {
            role: "claude-quality" if role == "architecture-advisor" else "gpt-fast"
            for role in _ROLES
        },
    }


def _write(root: Path, document: object) -> Path:
    directory = root / ".orichum"
    directory.mkdir()
    path = directory / "models.json"
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

    def test_absence_uses_machine_configuration(self) -> None:
        self.assertIsNone(discover_project_models(self.child, self.root, self.stacks))

    def test_nearest_mapping_wins_and_builds_one_ephemeral_stack(self) -> None:
        _write(self.root, _document("claude-quality"))
        nearest = self.child.parent
        path = _write(nearest, _document("gpt-fast"))

        loaded = discover_project_models(self.child, self.root, self.stacks)

        assert loaded is not None
        self.assertEqual(loaded.path, path)
        self.assertEqual(loaded.assignments["controller"], "gpt-fast")
        self.assertEqual(tuple(loaded.stacks.stacks), (loaded.stack_name,))
        controller = loaded.stacks.stacks[loaded.stack_name].controller[0]
        self.assertEqual(controller.providers, ("openai", "other"))
        self.assertEqual(loaded.stacks.models, self.stacks.models)

    def test_context_root_is_included_and_parent_is_ignored(self) -> None:
        _write(self.outer, _document("claude-quality"))
        path = _write(self.root, _document("gpt-fast"))

        loaded = discover_project_models(self.child, self.root, self.stacks)

        assert loaded is not None
        self.assertEqual(loaded.path, path)

    def test_invalid_nearest_mapping_fails_instead_of_using_parent(self) -> None:
        _write(self.root, _document())
        directory = self.child.parent / ".orichum"
        directory.mkdir()
        (directory / "models.json").write_text("{", encoding="utf-8")

        with self.assertRaisesRegex(ProjectModelsError, "invalid JSON"):
            discover_project_models(self.child, self.root, self.stacks)

    def test_rejects_duplicate_keys_unknown_models_and_boolean_schema(self) -> None:
        cases = (
            (
                '{"schemaVersion":1,"schemaVersion":1,"controller":"gpt-fast",'
                '"agents":{}}',
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
                directory = self.child / ".orichum"
                directory.mkdir(exist_ok=True)
                (directory / "models.json").write_text(content, encoding="utf-8")
                with self.assertRaisesRegex(ProjectModelsError, message):
                    discover_project_models(self.child, self.root, self.stacks)

    def test_rejects_missing_or_unknown_agent_roles(self) -> None:
        for agents in (
            {role: "gpt-fast" for role in _ROLES[:-1]},
            {**{role: "gpt-fast" for role in _ROLES}, "planning-advisor": "gpt-fast"},
        ):
            with self.subTest(agents=tuple(agents)):
                directory = self.child / ".orichum"
                directory.mkdir(exist_ok=True)
                (directory / "models.json").write_text(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "controller": "gpt-fast",
                            "agents": agents,
                        }
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ProjectModelsError, "agents must contain"):
                    discover_project_models(self.child, self.root, self.stacks)

    def test_rejects_symlinked_directory_or_file(self) -> None:
        target = self.root / "target"
        target.mkdir()
        (target / "models.json").write_text(json.dumps(_document()), encoding="utf-8")
        os.symlink(target, self.child / ".orichum")
        with self.assertRaisesRegex(ProjectModelsError, "real directory"):
            discover_project_models(self.child, self.root, self.stacks)
        (self.child / ".orichum").unlink()

        directory = self.child / ".orichum"
        directory.mkdir()
        os.symlink(target / "models.json", directory / "models.json")
        with self.assertRaisesRegex(ProjectModelsError, "regular file"):
            discover_project_models(self.child, self.root, self.stacks)

    def test_rejects_oversized_and_non_utf8_files(self) -> None:
        directory = self.child / ".orichum"
        directory.mkdir()
        path = directory / "models.json"
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
