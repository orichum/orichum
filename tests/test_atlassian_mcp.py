#!/usr/bin/env python3
import json
import tempfile
import unittest
from pathlib import Path

from integrations.common.atlassian_mcp import (
    AtlassianConfig,
    load_project_atlassian,
    mcp_environment,
)
from integrations.common.project_context import configure_project_atlassian
from integrations.common.session_config import _session_mcp_payload


class AtlassianMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.config = self.root / "projects.json"
        self.config.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "contexts": [
                        {
                            "root": str(self.project),
                            "atlassian": None,
                            "modelStack": None,
                            "accountPools": ["shared"],
                        }
                    ],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.config.chmod(0o600)
        self.profiles = self.root / "jira-profiles.json"
        self.profiles.write_text(
            json.dumps({"schemaVersion": 1, "profiles": {}}) + "\n",
            encoding="utf-8",
        )
        self.profiles.chmod(0o600)

    def test_project_context_stores_one_complete_jira_configuration(self) -> None:
        configure_project_atlassian(
            self.config,
            self.project,
            AtlassianConfig(
                url="https://xebia.atlassian.net",
                username="arvind@example.com",
                api_token="xebia-token",
            ),
        )

        document = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(
            document["contexts"][0]["atlassian"],
            {
                "url": "https://xebia.atlassian.net",
                "username": "arvind@example.com",
                "apiToken": "xebia-token",
            },
        )
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        loaded = load_project_atlassian(self.config, self.profiles, self.project)
        self.assertEqual(loaded.api_token, "xebia-token")

    def test_named_profile_replaces_direct_project_credentials(self) -> None:
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
            )
            + "\n",
            encoding="utf-8",
        )
        self.profiles.chmod(0o600)

        loaded = load_project_atlassian(
            self.config,
            self.profiles,
            self.project,
            "work",
        )

        self.assertEqual(loaded.url, "https://work.atlassian.net")
        self.assertEqual(loaded.api_token, "work-token")

    def test_mcp_environment_forwards_only_process_basics_and_jira(self) -> None:
        environment = mcp_environment(
            AtlassianConfig(
                url="https://xebia.atlassian.net",
                username="arvind@example.com",
                api_token="xebia-token",
            ),
            {
                "PATH": "/bin",
                "HTTPS_PROXY": "http://proxy.example:8080",
                "GH_TOKEN": "do-not-forward",
                "AWS_SECRET_ACCESS_KEY": "do-not-forward",
            },
        )

        self.assertEqual(environment["PATH"], "/bin")
        self.assertEqual(
            environment["HTTPS_PROXY"], "http://proxy.example:8080"
        )
        self.assertNotIn("GH_TOKEN", environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertEqual(
            environment["JIRA_URL"], "https://xebia.atlassian.net"
        )
        self.assertEqual(environment["JIRA_API_TOKEN"], "xebia-token")
        self.assertEqual(environment["MCP_TRANSPORT"], "stdio")
        self.assertEqual(environment["READ_ONLY_MODE"], "false")

    def test_session_loads_atlassian_only_for_configured_project(self) -> None:
        tools = self.root / "tools" / "bin"
        tools.mkdir(parents=True)
        binary = tools / "mcp-atlassian"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        configured = {
            "route": {
                "contextRootReal": str(self.project),
                "atlassianConfigured": True,
                "jiraProfile": "work",
            }
        }
        unconfigured = {
            "route": {
                "contextRootReal": str(self.project),
                "atlassianConfigured": False,
            }
        }

        payload = _session_mcp_payload(configured, data_root=self.root)
        server = payload["mcpServers"]["atlassian"]
        self.assertEqual(server["args"], [str(self.project), "work"])
        self.assertNotIn("xebia-token", json.dumps(payload))
        self.assertNotIn(
            "atlassian",
            _session_mcp_payload(unconfigured, data_root=self.root)[
                "mcpServers"
            ],
        )


if __name__ == "__main__":
    unittest.main()
