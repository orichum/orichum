#!/usr/bin/env python3
"""Launch one project-bound mcp-atlassian process."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from .jira_profiles import (
    AtlassianConfig,
    AtlassianError,
    load_jira_profiles,
    normalize_atlassian,
)

_SAFE_ENVIRONMENT_KEYS = frozenset(
    {
        "CURL_CA_BUNDLE",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_PROXY",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
)


def load_project_atlassian(
    config_path: Path,
    profiles_path: Path,
    project_root: Path,
    profile: str | None = None,
) -> AtlassianConfig:
    try:
        document = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AtlassianError(
            "projects configuration is unavailable"
        ) from error
    contexts = document.get("contexts") if isinstance(document, dict) else None
    if not isinstance(contexts, list):
        raise AtlassianError("projects configuration is invalid")
    project_root = Path(project_root).expanduser().resolve(strict=True)
    for context in contexts:
        if not isinstance(context, dict) or "root" not in context:
            raise AtlassianError("projects configuration is invalid")
        try:
            root = Path(context["root"]).expanduser().resolve(strict=True)
        except (TypeError, OSError, RuntimeError) as error:
            raise AtlassianError("projects configuration is invalid") from error
        if root == project_root:
            if profile is not None:
                profiles = load_jira_profiles(profiles_path)
                try:
                    return profiles[profile]
                except KeyError as error:
                    raise AtlassianError(
                        f"Jira profile {profile} is not configured"
                    ) from error
            configured = normalize_atlassian(context.get("atlassian"))
            if configured is None:
                raise AtlassianError(
                    "project does not configure Atlassian"
                )
            return configured
    raise AtlassianError("project context is not configured")


def mcp_environment(
    config: AtlassianConfig,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    inherited = os.environ if source is None else source
    environment = {
        key: value
        for key, value in inherited.items()
        if key in _SAFE_ENVIRONMENT_KEYS
    }
    environment.update(
        {
            "JIRA_URL": config.url,
            "JIRA_USERNAME": config.username,
            "JIRA_API_TOKEN": config.api_token,
            "MCP_TRANSPORT": "stdio",
            "MCP_VERBOSE": "false",
            "READ_ONLY_MODE": "false",
        }
    )
    return environment


def managed_binary(data_root: Path) -> Path:
    data_root = Path(data_root).expanduser().resolve(strict=True)
    candidate = data_root / "tools" / "bin" / "mcp-atlassian"
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(data_root / "tools")
    except (OSError, RuntimeError, ValueError) as error:
        raise AtlassianError(
            "managed mcp-atlassian executable is unavailable"
        ) from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AtlassianError(
            "managed mcp-atlassian executable is unavailable"
        )
    return resolved


def serve(
    data_root: Path,
    config_path: Path,
    profiles_path: Path,
    project_root: Path,
    profile: str | None = None,
) -> None:
    config = load_project_atlassian(
        config_path,
        profiles_path,
        project_root,
        profile,
    )
    binary = managed_binary(data_root)
    os.execve(str(binary), [str(binary)], mcp_environment(config))


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="orichum-atlassian-mcp")
    parser.add_argument("data_root", type=Path)
    parser.add_argument("config_path", type=Path)
    parser.add_argument("profiles_path", type=Path)
    parser.add_argument("project_root", type=Path)
    parser.add_argument("profile", nargs="?")
    parsed = parser.parse_args(arguments)
    try:
        serve(
            parsed.data_root,
            parsed.config_path,
            parsed.profiles_path,
            parsed.project_root,
            parsed.profile,
        )
    except (AtlassianError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    raise AssertionError("mcp-atlassian process returned unexpectedly")


if __name__ == "__main__":
    raise SystemExit(main())
