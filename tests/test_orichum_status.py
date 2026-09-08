#!/usr/bin/env python3
"""Tests for the compact, session-aware Orichum status line."""

import unittest
import json
import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from integrations.common.account_registry import Account
from integrations.common.model_routing import ROLES
from integrations.common.orichum_sessions import LogicalSession, RouteBinding
from integrations.common.route_selection import Route


class OrichumStatusTests(unittest.TestCase):
    def test_status_reports_native_context_capacity(self):
        from integrations.common.orichum_status import render_status
        session, accounts, route_status = self._status_fixture()
        result = render_status(
            {"context_window": {"used_percentage": 41, "context_window_size": 1000000}},
            session, accounts, route_status=route_status, color=False,
        )
        self.assertIn("context 41%/1,000k", result)

    @staticmethod
    def _status_fixture() -> tuple[
        LogicalSession,
        tuple[Account, ...],
        dict[str, object],
    ]:
        primary = Route(
            account_id="oc-a-0000000000000001",
            provider="anthropic",
            family="claude",
            logical_model="claude-opus-4-8",
            upstream_model="oc-r-0000000000000001/claude-opus-4-8",
            claudex_profile="ocp-0000000000000001",
            priority=10,
            pool="claude",
        )
        fallback = Route(
            account_id="oc-a-0000000000000002",
            provider="anthropic",
            family="claude",
            logical_model="claude-opus-4-8",
            upstream_model="oc-r-0000000000000002/claude-opus-4-8",
            claudex_profile="ocp-0000000000000002",
            priority=20,
            pool="claude",
        )
        binding = RouteBinding(primary=primary, fallbacks=(fallback,))
        session = LogicalSession(
            id="oc-s-0000000000000001",
            claude_session_id="00000000-0000-4000-8000-000000000001",
            parent_id=None,
            project_root=Path("/work/demo"),
            stack="balanced",
            controller=binding,
            agents={role: binding for role in ROLES},
            leanctx_profile="full",
            created_at="2026-07-27T00:00:00+00:00",
        )
        accounts = (
            Account(
                id=primary.account_id,
                name="Work Claude",
                provider=primary.provider,
                credential_ref="shared.json",
                pool="claude",
                routing_prefix="oc-r-0000000000000001",
                priority=10,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
            Account(
                id=fallback.account_id,
                name="Backup Claude",
                provider=fallback.provider,
                credential_ref="backup.json",
                pool="claude",
                routing_prefix="oc-r-0000000000000002",
                priority=20,
                state="active",
                original_prefix=None,
                original_priority=None,
            ),
        )
        route_status = {
            "sessionId": session.id,
            "accountId": fallback.account_id,
            "provider": fallback.provider,
            "family": fallback.family,
            "logicalModel": fallback.logical_model,
            "routeState": "fallback",
            "reason": "retry",
        }
        return session, accounts, route_status

    def test_controller_settings_enable_the_orichum_status_line(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        settings = json.loads(
            (repository / "controller" / "settings.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn("statusLine", settings)
        self.assertEqual(
            settings["statusLine"],
            {
                "type": "command",
                "command": (
                    '"$CLAUDEX_WORKFLOW_ROOT/bin/orichum-statusline"'
                ),
                "padding": 0,
            },
        )
        self.assertTrue(
            (repository / "bin" / "orichum-statusline").is_file()
        )

    def test_main_degrades_to_orichum_identity_when_state_is_unavailable(
        self,
    ) -> None:
        from integrations.common.orichum_status import main

        output = io.StringIO()
        result = main(
            input_stream=io.StringIO("{}"),
            output_stream=output,
            environment={},
        )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "ORICHUM │ status unavailable\n")

    def test_main_does_not_leak_invalid_local_state_errors(self) -> None:
        from integrations.common.orichum_status import main

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = io.StringIO()
            result = main(
                input_stream=io.StringIO("{}"),
                output_stream=output,
                environment={
                    "ORICHUM_SESSION_ID": "oc-s-0000000000000001",
                    "ORICHUM_STATE_HOME": str(root / "missing-state"),
                    "ORICHUM_CONFIG_HOME": str(root / "missing-config"),
                    "ORICHUM_DATA_HOME": str(root / "missing-data"),
                    "NO_COLOR": "1",
                },
            )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "ORICHUM │ status unavailable\n")

    def test_renders_active_fallback_account_and_claude_metrics(self) -> None:
        try:
            from integrations.common.orichum_status import render_status
        except ImportError as error:
            self.fail(f"Orichum status renderer is missing: {error}")

        session, accounts, route_status = self._status_fixture()
        payload = {
            "model": {
                "id": session.controller.fallbacks[0].upstream_model,
                "display_name": "Opus 4.8",
            },
            "context_window": {"used_percentage": 41.2},
            "rate_limits": {
                "five_hour": {"used_percentage": 63},
                "seven_day": {"used_percentage": None},
            },
        }

        self.assertEqual(
            render_status(
                payload,
                session,
                accounts,
                route_status=route_status,
                color=False,
            ),
            "ORICHUM │ demo │ balanced\n"
            "Claude · Opus 4.8 │ Backup Claude [fallback: rate limit] │ "
            "context 41% │ 5h 63% │ 7d —",
        )

    def test_provider_quota_fills_missing_claude_code_metrics(self) -> None:
        from integrations.common.orichum_status import render_status

        session, accounts, route_status = self._status_fixture()

        self.assertEqual(
            render_status(
                {
                    "model": {"display_name": "Opus 4.8"},
                    "context_window": {"used_percentage": 41.2},
                },
                session,
                accounts,
                route_status=route_status,
                provider_quota={"five_hour": 17, "seven_day": 38},
                color=False,
            ),
            "ORICHUM │ demo │ balanced\n"
            "Claude · Opus 4.8 │ Backup Claude [fallback: rate limit] │ "
            "context 41% │ 5h 17% │ 7d 38%",
        )

    def test_parses_provider_quota_windows_without_assuming_order(self) -> None:
        from integrations.common.orichum_status import _parse_provider_quota

        codex = _parse_provider_quota(
            "openai",
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 21,
                        "limit_window_seconds": 604800,
                    },
                    "secondary_window": {
                        "used_percent": 14,
                        "limit_window_seconds": 18000,
                    },
                }
            },
        )
        claude = _parse_provider_quota(
            "anthropic",
            {
                "five_hour": {"utilization": 0.0},
                "seven_day": {"utilization": 12.0},
            },
        )
        kimi = _parse_provider_quota(
            "kimi",
            {
                "usage": {"used": 30, "limit": 120},
                "limits": [
                    {
                        "window": {
                            "duration": 300,
                            "timeUnit": "MINUTE",
                        },
                        "detail": {"used": 45, "limit": 100},
                    }
                ],
            },
        )

        self.assertEqual(codex, {"five_hour": 14.0, "seven_day": 21.0})
        self.assertEqual(claude, {"five_hour": 0.0, "seven_day": 12.0})
        self.assertEqual(kimi, {"five_hour": 45.0, "seven_day": 25.0})

    def test_kimi_quota_uses_the_documented_coding_usage_endpoint(self) -> None:
        from integrations.common.orichum_status import _request_provider_quota

        account = Account(
            id="oc-a-0000000000000003",
            name="Kimi",
            provider="kimi",
            credential_ref="kimi.json",
            pool="shared",
            routing_prefix="oc-r-0000000000000003",
            priority=10,
            state="active",
            original_prefix=None,
            original_priority=None,
        )
        response = unittest.mock.Mock()
        response.status = 200
        response.read.return_value = json.dumps(
            {"usage": {"used": 1, "limit": 4}}
        ).encode("utf-8")
        connection = unittest.mock.Mock()
        connection.getresponse.return_value = response

        with patch(
            "integrations.common.orichum_status.http.client.HTTPSConnection",
            return_value=connection,
        ) as connect:
            result = _request_provider_quota(
                account, {"access_token": "secret"}
            )

        connect.assert_called_once_with("api.kimi.com", timeout=2)
        connection.request.assert_called_once_with(
            "GET",
            "/coding/usages",
            headers={
                "Authorization": "Bearer secret",
                "Accept": "application/json",
            },
        )
        self.assertEqual(result, {"seven_day": 25.0})

    def test_provider_quota_cache_uses_safe_account_scoped_values(self) -> None:
        from integrations.common.orichum_status import _provider_quota

        _session, accounts, _route_status = self._status_fixture()
        account = accounts[0]
        with tempfile.TemporaryDirectory() as temporary:
            data_home = Path(temporary)
            auth_dir = data_home / "auth"
            auth_dir.mkdir(mode=0o700)
            credential = auth_dir / account.credential_ref
            credential.write_text(
                json.dumps(
                    {
                        "type": "claude",
                        "access_token": "secret-token",
                    }
                ),
                encoding="utf-8",
            )
            credential.chmod(0o600)

            with patch(
                "integrations.common.orichum_status._request_provider_quota",
                return_value={"five_hour": 17.0, "seven_day": 38.0},
            ):
                first = _provider_quota(data_home, account, now=1000.0)

            with patch(
                "integrations.common.orichum_status._request_provider_quota",
                side_effect=AssertionError("fresh cache must avoid network"),
            ):
                second = _provider_quota(data_home, account, now=1030.0)

            cache = (
                data_home
                / "state"
                / "quota-cache"
                / f"{account.id}.json"
            ).read_text(encoding="utf-8")

        self.assertEqual(first, {"five_hour": 17.0, "seven_day": 38.0})
        self.assertEqual(second, first)
        self.assertNotIn("secret-token", cache)


if __name__ == "__main__":
    unittest.main()
