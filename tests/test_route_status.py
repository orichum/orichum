#!/usr/bin/env python3
"""Tests for bounded, session-scoped route telemetry."""

from datetime import datetime, timezone
import unittest

from integrations.common.route_selection import Route


class RouteStatusTests(unittest.TestCase):
    def test_records_the_active_fallback_without_private_route_fields(self) -> None:
        try:
            from integrations.common.route_status import RouteStatusStore
        except ImportError as error:
            self.fail(f"route status store is missing: {error}")

        route = Route(
            account_id="oc-a-0000000000000002",
            provider="antigravity",
            family="claude",
            logical_model="claude-opus-4-8",
            upstream_model="oc-r-0000000000000002/claude-opus-4-8",
            claudex_profile="ocp-0000000000000002",
            priority=20,
            pool="claude",
        )
        store = RouteStatusStore(
            wall_clock=lambda: datetime(
                2026, 7, 27, tzinfo=timezone.utc
            ),
        )

        store.select(
            "oc-s-0000000000000001",
            "oc-rq-0000000000000001",
            route,
            route_state="fallback",
            reason="retry",
        )
        store.complete(
            "oc-s-0000000000000001",
            "oc-rq-0000000000000001",
            200,
            512,
        )

        self.assertEqual(
            store.get("oc-s-0000000000000001").as_public_json(),
            {
                "sessionId": "oc-s-0000000000000001",
                "requestId": "oc-rq-0000000000000001",
                "accountId": route.account_id,
                "provider": route.provider,
                "family": route.family,
                "logicalModel": route.logical_model,
                "routeState": "fallback",
                "reason": "retry",
                "responseState": "complete",
                "lastHttpStatus": 200,
                "bytesForwarded": 512,
                "failureKind": None,
                "updatedAt": "2026-07-27T00:00:00+00:00",
            },
        )


if __name__ == "__main__":
    unittest.main()
