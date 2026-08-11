#!/usr/bin/env python3
"""Load private machine-local Jira profiles."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

MAX_JIRA_PROFILES_BYTES = 2 * 1024 * 1024
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class AtlassianError(RuntimeError):
    """A Jira profile or project Atlassian configuration is invalid."""


def validate_jira_profile(value: object) -> str:
    if not isinstance(value, str) or not _PROFILE.fullmatch(value):
        raise AtlassianError("Jira profile name is invalid")
    return value


def _non_blank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AtlassianError(f"{label} must be a non-empty string")
    return value.strip()


def _jira_url(value: object) -> str:
    value = _non_blank(value, "Jira URL").rstrip("/")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise AtlassianError("Jira URL is invalid") from error
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AtlassianError(
            "Jira URL must be an HTTPS origin without credentials, "
            "a query, or a fragment"
        )
    return value


@dataclass(frozen=True)
class AtlassianConfig:
    url: str
    username: str
    api_token: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _jira_url(self.url))
        object.__setattr__(self, "username", _non_blank(self.username, "Jira username"))
        object.__setattr__(
            self, "api_token", _non_blank(self.api_token, "Jira API token")
        )

    def as_json(self) -> dict[str, str]:
        return {
            "url": self.url,
            "username": self.username,
            "apiToken": self.api_token,
        }


def normalize_atlassian(value: object) -> AtlassianConfig | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "url",
        "username",
        "apiToken",
    }:
        raise AtlassianError(
            "atlassian must contain exactly url, username, and apiToken"
        )
    return AtlassianConfig(
        url=value["url"],
        username=value["username"],
        api_token=value["apiToken"],
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite value {value}")


def _read_private(path: Path) -> bytes | None:
    path = Path(path)
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AtlassianError("Jira profile registry is unavailable") from error
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > MAX_JIRA_PROFILES_BYTES
    ):
        raise AtlassianError("Jira profile registry is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise AtlassianError(
            "Jira profile registry could not be opened safely"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise AtlassianError("Jira profile registry changed while opening")
        content = bytearray()
        while len(content) <= MAX_JIRA_PROFILES_BYTES:
            chunk = os.read(
                descriptor,
                min(65536, MAX_JIRA_PROFILES_BYTES + 1 - len(content)),
            )
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > MAX_JIRA_PROFILES_BYTES:
            raise AtlassianError("Jira profile registry is too large")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise AtlassianError("Jira profile registry changed while reading")
        return bytes(content)
    finally:
        os.close(descriptor)


def load_jira_profiles(path: Path) -> Mapping[str, AtlassianConfig]:
    content = _read_private(path)
    if content is None:
        return MappingProxyType({})
    try:
        text = content.decode("utf-8")
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise AtlassianError(
            "Jira profile registry must be valid UTF-8 JSON"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "profiles",
    }:
        raise AtlassianError(
            "Jira profile registry must contain exactly schemaVersion and profiles"
        )
    if type(document["schemaVersion"]) is not int or document["schemaVersion"] != 1:
        raise AtlassianError("Jira profile registry schemaVersion must be exactly 1")
    raw_profiles = document["profiles"]
    if not isinstance(raw_profiles, dict):
        raise AtlassianError("Jira profiles must be an object")
    profiles: dict[str, AtlassianConfig] = {}
    for raw_name, raw_profile in raw_profiles.items():
        name = validate_jira_profile(raw_name)
        profiles[name] = normalize_atlassian(raw_profile)
        if profiles[name] is None:
            raise AtlassianError(f"Jira profile {name} is invalid")
    return MappingProxyType(profiles)
