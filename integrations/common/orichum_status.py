#!/usr/bin/env python3
"""Render a compact Orichum identity and session status line."""

from __future__ import annotations

import http.client
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import time
from typing import IO, Mapping, Sequence

from .account_registry import Account, AccountError, load_accounts
from .orichum_sessions import (
    LogicalSession,
    LogicalSessionError,
    load_logical_session,
)
from .provider_credentials import CredentialError, load_credential_fields


_FAMILY_LABELS = {
    "claude": "Claude",
    "gemini": "Gemini",
    "gpt": "GPT",
    "kimi": "Kimi",
}
_MAX_INPUT_BYTES = 64 * 1024
_QUOTA_CACHE_SECONDS = 60
_QUOTA_STALE_SECONDS = 15 * 60
_QUOTA_TIMEOUT_SECONDS = 2
_CODEX_WINDOWS = {18000: "five_hour", 604800: "seven_day"}
_CREDENTIAL_PROVIDER = {
    "anthropic": "claude",
    "kimi": "kimi",
    "openai": "codex",
}


def _text(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    value = value.strip()
    if (
        not value
        or len(value) > 96
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return fallback
    return value


def _percentage(value: object) -> str:
    if type(value) not in (int, float):
        return "—"
    number = float(value)
    if not math.isfinite(number) or number < 0 or number > 100:
        return "—"
    return f"{number:.0f}%"


def _nested_percentage(document: object, *path: str) -> str:
    value = document
    for key in path:
        if not isinstance(value, Mapping):
            return "—"
        value = value.get(key)
    return _percentage(value)


def _quota_percentage(
    payload: object,
    provider_quota: Mapping[str, object] | None,
    *path: str,
) -> str:
    native = _nested_percentage(payload, *path)
    if native != "—" or provider_quota is None:
        return native
    return _percentage(provider_quota.get(path[-2]))


def _percentage_number(value: object) -> float | None:
    if type(value) not in (int, float):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= 100 else None


def _used_percentage(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    used = value.get("used")
    limit = value.get("limit")
    remaining = value.get("remaining")
    if type(limit) not in (int, float):
        return None
    limit_number = float(limit)
    if type(used) in (int, float):
        used_number = float(used)
    elif type(remaining) in (int, float):
        used_number = limit_number - float(remaining)
    else:
        return None
    if (
        not math.isfinite(used_number)
        or not math.isfinite(limit_number)
        or used_number < 0
        or limit_number <= 0
    ):
        return None
    return min(100.0, used_number / limit_number * 100)


def _parse_provider_quota(
    provider: str, document: object
) -> dict[str, float]:
    if not isinstance(document, Mapping):
        return {}
    windows: dict[str, float] = {}
    if provider == "anthropic":
        for key in ("five_hour", "seven_day"):
            window = document.get(key)
            if not isinstance(window, Mapping):
                continue
            percentage = _percentage_number(window.get("utilization"))
            if percentage is not None:
                windows[key] = percentage
        return windows
    if provider == "kimi":
        percentage = _used_percentage(document.get("usage"))
        if percentage is not None:
            windows["seven_day"] = percentage
        limits = document.get("limits")
        if not isinstance(limits, list):
            return windows
        for limit in limits:
            if not isinstance(limit, Mapping):
                continue
            detail = limit.get("detail")
            detail = detail if isinstance(detail, Mapping) else limit
            window = limit.get("window")
            window = window if isinstance(window, Mapping) else limit
            duration = window.get("duration")
            unit = window.get("timeUnit")
            if type(duration) is not int or not isinstance(unit, str):
                continue
            normalized_unit = unit.upper()
            unit_seconds = (
                60
                if "MINUTE" in normalized_unit
                else 3600
                if "HOUR" in normalized_unit
                else 86400
                if "DAY" in normalized_unit
                else None
            )
            percentage = _used_percentage(detail)
            if unit_seconds is None or percentage is None:
                continue
            key = _CODEX_WINDOWS.get(duration * unit_seconds)
            if key is not None:
                windows[key] = percentage
        return windows
    if provider != "openai":
        return {}
    rate_limit = document.get("rate_limit")
    if not isinstance(rate_limit, Mapping):
        return {}
    for name in ("primary_window", "secondary_window"):
        window = rate_limit.get(name)
        if not isinstance(window, Mapping):
            continue
        seconds = window.get("limit_window_seconds")
        if type(seconds) is not int:
            continue
        key = _CODEX_WINDOWS.get(seconds)
        percentage = _percentage_number(window.get("used_percent"))
        if key is not None and percentage is not None:
            windows[key] = percentage
    return windows


def _request_provider_quota(
    account: Account,
    credential: Mapping[str, object],
) -> dict[str, float]:
    token = credential.get("access_token")
    if not isinstance(token, str) or not token:
        return {}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if account.provider == "openai":
        host = "chatgpt.com"
        path = "/backend-api/wham/usage"
        account_id = credential.get("account_id")
        if isinstance(account_id, str) and account_id:
            headers["ChatGPT-Account-Id"] = account_id
    elif account.provider == "anthropic":
        host = "api.anthropic.com"
        path = "/api/oauth/usage"
        headers["anthropic-beta"] = "oauth-2025-04-20"
    elif account.provider == "kimi":
        host = "api.kimi.com"
        path = "/coding/usages"
    else:
        return {}
    connection = http.client.HTTPSConnection(
        host, timeout=_QUOTA_TIMEOUT_SECONDS
    )
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(_MAX_INPUT_BYTES + 1)
        if (
            response.status < 200
            or response.status >= 300
            or len(body) > _MAX_INPUT_BYTES
        ):
            return {}
        return _parse_provider_quota(
            account.provider, json.loads(body.decode("utf-8"))
        )
    finally:
        connection.close()


def _read_quota_cache(
    path: Path,
    account_id: str,
    now: float,
) -> tuple[dict[str, float], float] | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(document, dict)
            or document.get("accountId") != account_id
            or type(document.get("fetchedAt")) not in (int, float)
            or not isinstance(document.get("windows"), dict)
        ):
            return None
        windows = {
            key: percentage
            for key in ("five_hour", "seven_day")
            if (
                percentage := _percentage_number(
                    document["windows"].get(key)
                )
            )
            is not None
        }
        age = max(0.0, now - float(document["fetchedAt"]))
        return windows, age
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None


def _write_quota_cache(
    path: Path,
    account_id: str,
    windows: Mapping[str, float],
    now: float,
) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{account_id}.",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            os.fchmod(temporary.fileno(), 0o600)
            json.dump(
                {
                    "accountId": account_id,
                    "fetchedAt": now,
                    "windows": dict(windows),
                },
                temporary,
                sort_keys=True,
                separators=(",", ":"),
            )
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _provider_quota(
    data_home: Path,
    account: Account,
    *,
    now: float | None = None,
) -> dict[str, float]:
    credential_provider = _CREDENTIAL_PROVIDER.get(account.provider)
    if credential_provider is None:
        return {}
    current_time = time.time() if now is None else now
    cache_path = (
        Path(data_home)
        / "state"
        / "quota-cache"
        / f"{account.id}.json"
    )
    cached = _read_quota_cache(cache_path, account.id, current_time)
    if cached is not None and cached[1] <= _QUOTA_CACHE_SECONDS:
        return cached[0]
    try:
        credential = load_credential_fields(
            Path(data_home) / "auth",
            account.credential_ref,
            expected_provider=credential_provider,
            fields=("access_token", "account_id"),
        )
        windows = _request_provider_quota(account, credential)
        if windows:
            _write_quota_cache(
                cache_path, account.id, windows, current_time
            )
            return windows
    except (
        CredentialError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        pass
    if cached is not None and cached[1] <= _QUOTA_STALE_SECONDS:
        return cached[0]
    return {}


def _active_route(
    session: LogicalSession,
    route_status: Mapping[str, object] | None,
) -> tuple[str, str, str]:
    route = session.controller.primary
    state = "primary"
    reason = "primary"
    if (
        isinstance(route_status, Mapping)
        and route_status.get("sessionId") == session.id
        and route_status.get("routeState") in {"primary", "fallback"}
        and route_status.get("reason") in {"primary", "retry", "cooldown"}
        and isinstance(route_status.get("accountId"), str)
    ):
        candidates = (
            session.controller.primary,
            *session.controller.fallbacks,
        )
        selected = next(
            (
                candidate
                for candidate in candidates
                if candidate.account_id == route_status["accountId"]
            ),
            None,
        )
        if selected is not None:
            route = selected
            state = str(route_status["routeState"])
            reason = str(route_status["reason"])
    label = "primary"
    if state == "fallback":
        label = (
            "fallback: cooldown"
            if reason == "cooldown"
            else "fallback: rate limit"
        )
    return route.family, route.account_id, label


def render_status(
    payload: object,
    session: LogicalSession,
    accounts: Sequence[Account],
    *,
    route_status: Mapping[str, object] | None,
    provider_quota: Mapping[str, object] | None = None,
    color: bool,
) -> str:
    """Render two bounded lines from Claude metrics and verified Orichum state."""
    family, account_id, route_label = _active_route(session, route_status)
    account_name = next(
        (
            account.name
            for account in accounts
            if account.id == account_id
        ),
        account_id,
    )
    model_display = session.controller.primary.logical_model
    if isinstance(payload, Mapping):
        model = payload.get("model")
        if isinstance(model, Mapping):
            model_display = _text(
                model.get("display_name"),
                _text(model.get("id"), model_display),
            )
    project = _text(Path(session.project_root).name, "project")
    stack = _text(session.stack, "stack")
    family_label = _FAMILY_LABELS.get(family, family.title())
    context = _nested_percentage(payload, "context_window", "used_percentage")
    if isinstance(payload, Mapping):
        window = payload.get("context_window")
        size = window.get("context_window_size") if isinstance(window, Mapping) else None
        if type(size) is int and 0 < size <= 10_000_000:
            context += f"/{size // 1000:,}k"
    five_hour = _quota_percentage(
        payload,
        provider_quota,
        "rate_limits",
        "five_hour",
        "used_percentage",
    )
    seven_day = _quota_percentage(
        payload,
        provider_quota,
        "rate_limits",
        "seven_day",
        "used_percentage",
    )

    if color:
        identity = f"\033[1;35mORICHUM\033[0m │ \033[36m{project}\033[0m │ {stack}"
        route = (
            f"\033[32m{family_label} · {model_display}\033[0m │ "
            f"{_text(account_name, account_id)} [{route_label}]"
        )
    else:
        identity = f"ORICHUM │ {project} │ {stack}"
        route = (
            f"{family_label} · {model_display} │ "
            f"{_text(account_name, account_id)} [{route_label}]"
        )
    return (
        f"{identity}\n{route} │ context {context} │ "
        f"5h {five_hour} │ 7d {seven_day}"
    )


def _fetch_route_status(
    data_home: Path,
    session_id: str,
) -> Mapping[str, object] | None:
    try:
        ports = json.loads(
            (data_home / "service-ports.json").read_text(encoding="utf-8")
        )
        port = ports["routeProxyPort"]
        if type(port) is not int or not 1024 <= port <= 65535:
            return None
        connection = http.client.HTTPConnection(
            "127.0.0.1", port, timeout=0.075
        )
        connection.request("GET", f"/status?session_id={session_id}")
        response = connection.getresponse()
        body = response.read(_MAX_INPUT_BYTES + 1)
        connection.close()
        if response.status != 200 or len(body) > _MAX_INPUT_BYTES:
            return None
        document = json.loads(body)
        if (
            not isinstance(document, dict)
            or document.get("sessionId") != session_id
        ):
            return None
        return document
    except (
        OSError,
        KeyError,
        UnicodeError,
        json.JSONDecodeError,
        http.client.HTTPException,
    ):
        return None


def main(
    *,
    input_stream: IO[str] | None = None,
    output_stream: IO[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    input_stream = sys.stdin if input_stream is None else input_stream
    output_stream = sys.stdout if output_stream is None else output_stream
    environment = os.environ if environment is None else environment
    try:
        raw = input_stream.read(_MAX_INPUT_BYTES + 1)
        if len(raw.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise ValueError("status input is too large")
        payload = json.loads(raw)
        session_id = environment["ORICHUM_SESSION_ID"]
        state_home = Path(environment["ORICHUM_STATE_HOME"])
        config_home = Path(environment["ORICHUM_CONFIG_HOME"])
        data_home = Path(environment["ORICHUM_DATA_HOME"])
        session = load_logical_session(state_home, session_id)
        accounts = load_accounts(config_home / "accounts.json")
        route_status = _fetch_route_status(data_home, session_id)
        _family, account_id, _label = _active_route(
            session, route_status
        )
        active_account = next(
            (account for account in accounts if account.id == account_id),
            None,
        )
        rendered = render_status(
            payload,
            session,
            accounts,
            route_status=route_status,
            provider_quota=(
                None
                if active_account is None
                else _provider_quota(data_home, active_account)
            ),
            color=(
                "NO_COLOR" not in environment
                and environment.get("TERM") != "dumb"
            ),
        )
    except (
        AccountError,
        KeyError,
        LogicalSessionError,
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
    ):
        rendered = "ORICHUM │ status unavailable"
    print(rendered, file=output_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
