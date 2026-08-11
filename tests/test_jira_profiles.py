#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from integrations.common.jira_profiles import (
    AtlassianError,
    load_jira_profiles,
)


def _document() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "profiles": {
            "work": {
                "url": "https://work.atlassian.net",
                "username": "person@example.com",
                "apiToken": "private-token",
            }
        },
    }


class JiraProfilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / "jira-profiles.json"

    def write(self, content: str) -> None:
        self.path.write_text(content, encoding="utf-8")
        self.path.chmod(0o600)

    def test_missing_registry_is_empty(self) -> None:
        self.assertEqual(dict(load_jira_profiles(self.path)), {})

    def test_loads_private_named_profiles(self) -> None:
        self.write(json.dumps(_document()))

        profiles = load_jira_profiles(self.path)

        self.assertEqual(tuple(profiles), ("work",))
        self.assertEqual(profiles["work"].url, "https://work.atlassian.net")
        self.assertEqual(profiles["work"].api_token, "private-token")

    def test_rejects_unsafe_mode_symlink_duplicates_and_invalid_alias(self) -> None:
        self.write(json.dumps(_document()))
        self.path.chmod(0o644)
        with self.assertRaisesRegex(AtlassianError, "unsafe"):
            load_jira_profiles(self.path)
        self.path.unlink()

        target = self.root / "target.json"
        target.write_text(json.dumps(_document()), encoding="utf-8")
        target.chmod(0o600)
        os.symlink(target, self.path)
        with self.assertRaisesRegex(AtlassianError, "unsafe"):
            load_jira_profiles(self.path)
        self.path.unlink()

        self.write('{"schemaVersion":1,"profiles":{},"profiles":{}}')
        with self.assertRaisesRegex(AtlassianError, "valid UTF-8 JSON"):
            load_jira_profiles(self.path)

        document = _document()
        document["profiles"] = {"Work Account": document["profiles"]["work"]}
        self.write(json.dumps(document))
        with self.assertRaisesRegex(AtlassianError, "profile name is invalid"):
            load_jira_profiles(self.path)


if __name__ == "__main__":
    unittest.main()
