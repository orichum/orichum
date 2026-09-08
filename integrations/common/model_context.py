"""Model-specific Claude context hints, without changing provider route IDs.

Verified 2026-09-08 against https://developers.openai.com/api/docs/models/compare
and https://developers.openai.com/api/docs/models/gpt-5.6-luna . These exact
OpenAI models advertise 1,050,000 tokens. Claude's per-model [1m] hint enables
1,000,000, leaving a conservative 50,000-token margin. Native acceptance tests
assert that Claude strips the hint from HTTP model IDs. Do not use the global
MAX_CONTEXT override: it would also enlarge smaller specialist models.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .model_routing import validate_model_id

if TYPE_CHECKING:
    from .orichum_sessions import RouteBinding
    from .route_selection import Route

DEFAULT_WINDOW = 200_000
LARGE_WINDOW = 1_000_000
LARGE_MODELS = frozenset(
    {"gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
)


def route_window(route: Route) -> int:
    # Resolve by actual provider and upstream model, not a user-chosen logical
    # alias. Unknown providers/models retain the existing conservative window.
    upstream = route.upstream_model.partition("/")[2]
    if (
        route.provider == "openai"
        and route.family == "gpt"
        and upstream in LARGE_MODELS
    ):
        return LARGE_WINDOW
    return DEFAULT_WINDOW


def binding_window(binding: RouteBinding) -> int:
    # Any automatic fallback must accept the same conversation as the primary.
    return min(route_window(route) for route in (binding.primary, *binding.fallbacks))


def client_model(binding: RouteBinding) -> str:
    model = binding.primary.upstream_model
    return model + "[1m]" if binding_window(binding) == LARGE_WINDOW else model


def validate_client_model(value: object, label: str) -> str:
    """Allow one native hint only in digest-bound *client* model snapshots.

    Stack configuration and persisted HTTP routes still use validate_model_id
    and cannot inject these annotations.
    """
    if isinstance(value, str) and value.endswith("[1m]"):
        return validate_model_id(value[:-4], label) + "[1m]"
    return validate_model_id(value, label)
