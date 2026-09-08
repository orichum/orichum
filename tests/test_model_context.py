from __future__ import annotations

import json
import unittest
from dataclasses import replace

from integrations.common.model_context import (
    binding_window,
    client_model,
    validate_client_model,
)
from integrations.common.model_routing import ROLES, RoutingError, validate_model_id
from integrations.common.orichum_cli import _effective_for
from integrations.common.orichum_sessions import ResolvedSessionPlan, RouteBinding
from integrations.common.route_selection import Route
from integrations.common.session_config import _parse_effective_models


def binding(model="gpt-5.6-sol", provider="openai", family="gpt"):
    return RouteBinding(
        Route(
            "oc-a-0000000000000001",
            provider,
            family,
            "logical-alias",
            "oc-r-0000000000000001/" + model,
            "ocp-0000000000000001",
            100,
            "shared",
        ),
        (),
    )


class ModelContextTests(unittest.TestCase):
    def test_exact_supported_models_get_per_model_hint(self):
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra"):
            route = binding(model)
            self.assertEqual(binding_window(route), 1_000_000)
            self.assertEqual(client_model(route), route.primary.upstream_model + "[1m]")
            self.assertFalse(route.primary.upstream_model.endswith("[1m]"))

    def test_unknown_models_providers_and_aliases_stay_conservative(self):
        for route in (
            binding("gpt-5.6-sol-new"),
            binding("gpt-5.6-sol", "antigravity"),
            binding("small-model"),
            binding("gpt-5.6-sol", family="claude"),
        ):
            self.assertEqual(binding_window(route), 200_000)
            self.assertEqual(client_model(route), route.primary.upstream_model)

    def test_fallback_limits_the_entire_binding(self):
        primary = binding()
        alternate = replace(primary.primary, provider="unknown")
        limited = replace(primary, fallbacks=(alternate,))
        self.assertEqual(binding_window(limited), 200_000)
        self.assertEqual(client_model(limited), primary.primary.upstream_model)

    def test_mixed_specialists_are_not_enlarged_by_controller(self):
        worker = binding("claude-sonnet-5", "anthropic", "claude")
        plan = ResolvedSessionPlan(
            "test", binding(), {role: worker for role in ROLES}, None
        )
        effective = _effective_for(plan)
        self.assertTrue(effective.controller.endswith("[1m]"))
        self.assertTrue(
            all(not model.endswith("[1m]") for model in effective.agents.values())
        )
        self.assertEqual(
            _parse_effective_models(json.dumps(effective.as_json()).encode()), effective
        )

    def test_hints_are_client_only_not_route_configuration(self):
        self.assertEqual(validate_client_model("model[1m]", "test"), "model[1m]")
        with self.assertRaises(RoutingError):
            validate_model_id("model[1m]", "route")
        for model in ("model[1m][1m]", "model[2m]", "model\n[1m]"):
            with self.assertRaises(RoutingError):
                validate_client_model(model, "test")
