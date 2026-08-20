#!/usr/bin/env python3
"""Normalize immutable candidate-based model-stack definitions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Mapping

from .model_routing import (
    LEGACY_ROLES,
    ROLES,
    RoutingError,
    validate_model_id,
    validate_stack_name,
)


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CANDIDATE_ID = re.compile(r"^oc-c-[0-9a-f]{16}$")


class StackDefinitionError(RoutingError):
    """A model-stack document violates the normalized schema."""


@dataclass(frozen=True)
class ModelRoute:
    provider: str
    upstream: str


@dataclass(frozen=True)
class StackCandidate:
    id: str
    model: str
    providers: tuple[str, ...]


@dataclass(frozen=True)
class ModelDefinition:
    family: str
    routes: Mapping[str, str]


@dataclass(frozen=True)
class StackDefinition:
    name: str
    controller: tuple[StackCandidate, ...]
    agents: Mapping[str, tuple[StackCandidate, ...]]


@dataclass(frozen=True)
class NormalizedStacks(Mapping[str, object]):
    default_stack: str
    models: Mapping[str, ModelDefinition]
    stacks: Mapping[str, StackDefinition]

    def __getitem__(self, key: str) -> object:
        if key == "schemaVersion":
            return 2
        if key == "defaultStack":
            return self.default_stack
        if key == "stacks":
            return self.stacks
        raise KeyError(key)

    def __iter__(self):
        return iter(("schemaVersion", "defaultStack", "stacks"))

    def __len__(self) -> int:
        return 3


def candidate_id(stack: str, scope: str, ordinal: int, model: str) -> str:
    material = f"{stack}\0{scope}\0{ordinal}\0{model}".encode("utf-8")
    return "oc-c-" + hashlib.sha256(material).hexdigest()[:16]


def _exact(
    value: object, keys: set[str], label: str
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise StackDefinitionError(
            f"{label} must contain exactly {sorted(keys)}"
        )
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise StackDefinitionError(f"{label} is invalid")
    return value


def _model_id(value: object, label: str) -> str:
    try:
        return validate_model_id(value, label)
    except RoutingError as error:
        raise StackDefinitionError(str(error)) from error


def _stack_name(value: object, label: str) -> str:
    try:
        return validate_stack_name(value, label)
    except RoutingError as error:
        raise StackDefinitionError(str(error)) from error


def _parse_models(
    value: object, schema_version: int
) -> dict[str, ModelDefinition]:
    if not isinstance(value, dict) or not value:
        raise StackDefinitionError("models must be a non-empty object")
    models: dict[str, ModelDefinition] = {}
    for raw_model, raw_definition in value.items():
        model = _model_id(raw_model, "model")
        if schema_version == 1:
            definition = _exact(
                raw_definition,
                {"provider", "family", "upstream"},
                f"model {model}",
            )
            provider = _identifier(
                definition["provider"], f"model {model} provider"
            )
            routes = {
                provider: _model_id(
                    definition["upstream"], f"model {model} upstream"
                )
            }
        else:
            definition = _exact(
                raw_definition, {"family", "routes"}, f"model {model}"
            )
            raw_routes = definition["routes"]
            if not isinstance(raw_routes, dict) or not raw_routes:
                raise StackDefinitionError(
                    f"model {model} routes must be a non-empty object"
                )
            routes = {}
            for raw_provider, raw_upstream in raw_routes.items():
                provider = _identifier(
                    raw_provider, f"model {model} provider"
                )
                routes[provider] = _model_id(
                    raw_upstream, f"model {model} upstream"
                )
        family = _identifier(
            definition["family"], f"model {model} family"
        )
        models[model] = ModelDefinition(
            family=family,
            routes=MappingProxyType(dict(sorted(routes.items()))),
        )
    return models


def _candidate(
    raw_candidate: object,
    *,
    models: Mapping[str, ModelDefinition],
    seen_ids: set[str],
    label: str,
) -> StackCandidate:
    raw = _exact(
        raw_candidate, {"id", "model", "providers"}, label
    )
    candidate = raw["id"]
    if not isinstance(candidate, str) or not _CANDIDATE_ID.fullmatch(
        candidate
    ):
        raise StackDefinitionError(f"{label} has an unsafe candidate ID")
    if candidate in seen_ids:
        raise StackDefinitionError(f"{label} has a duplicate candidate ID")
    seen_ids.add(candidate)
    model = _model_id(raw["model"], f"{label} model")
    if model not in models:
        raise StackDefinitionError(f"{label} references unknown model {model}")
    raw_providers = raw["providers"]
    if not isinstance(raw_providers, list) or not raw_providers:
        raise StackDefinitionError(
            f"{label} providers must be a non-empty array"
        )
    providers = tuple(
        _identifier(provider, f"{label} provider")
        for provider in raw_providers
    )
    if len(providers) != len(set(providers)):
        raise StackDefinitionError(f"{label} providers must be unique")
    missing_routes = [
        provider
        for provider in providers
        if provider not in models[model].routes
    ]
    if missing_routes:
        raise StackDefinitionError(
            f"{label} provider has no route for model {model}"
        )
    return StackCandidate(candidate, model, providers)


def _candidate_list(
    value: object,
    *,
    models: Mapping[str, ModelDefinition],
    seen_ids: set[str],
    label: str,
) -> tuple[StackCandidate, ...]:
    if not isinstance(value, list) or not value:
        raise StackDefinitionError(
            f"{label} candidates must be a non-empty array"
        )
    candidates = tuple(
        _candidate(
            item,
            models=models,
            seen_ids=seen_ids,
            label=f"{label} candidate {ordinal}",
        )
        for ordinal, item in enumerate(value)
    )
    candidate_models = tuple(candidate.model for candidate in candidates)
    if len(candidate_models) != len(set(candidate_models)):
        raise StackDefinitionError(f"{label} has duplicate candidates")
    _validate_candidate_routes(candidates, models=models, label=label)
    families = {models[model].family for model in candidate_models}
    if len(families) != 1:
        raise StackDefinitionError(
            f"{label} candidates must use the same model family"
        )
    return candidates


def _validate_candidate_routes(
    candidates: tuple[StackCandidate, ...],
    *,
    models: Mapping[str, ModelDefinition],
    label: str,
) -> None:
    routes = [
        (provider, models[candidate.model].routes[provider])
        for candidate in candidates
        for provider in candidate.providers
    ]
    if len(routes) != len(set(routes)):
        raise StackDefinitionError(
            f"{label} has a duplicate provider route"
        )


def _migrated_candidates(
    value: object,
    *,
    stack: str,
    scope: str,
    models: Mapping[str, ModelDefinition],
    multiple: bool,
) -> tuple[StackCandidate, ...]:
    raw_models = value if multiple else [value]
    if not isinstance(raw_models, list) or not raw_models:
        raise StackDefinitionError(
            f"stack {stack} {scope} needs candidates"
        )
    model_ids = tuple(
        _model_id(model, f"stack {stack} {scope}") for model in raw_models
    )
    if len(model_ids) != len(set(model_ids)):
        raise StackDefinitionError(
            f"stack {stack} {scope} has duplicate candidates"
        )
    missing = [model for model in model_ids if model not in models]
    if missing:
        raise StackDefinitionError(
            f"stack {stack} {scope} references unknown model {missing[0]}"
        )
    families = {models[model].family for model in model_ids}
    if len(families) != 1:
        raise StackDefinitionError(
            f"stack {stack} {scope} candidates must use the same model family"
        )
    candidates = tuple(
        StackCandidate(
            id=candidate_id(stack, scope, ordinal, model),
            model=model,
            providers=tuple(models[model].routes),
        )
        for ordinal, model in enumerate(model_ids)
    )
    _validate_candidate_routes(
        candidates,
        models=models,
        label=f"stack {stack} {scope}",
    )
    return candidates


def _parse_stacks(
    value: object,
    *,
    schema_version: int,
    models: Mapping[str, ModelDefinition],
) -> dict[str, StackDefinition]:
    if not isinstance(value, dict) or not value:
        raise StackDefinitionError("stacks must be a non-empty object")
    stacks: dict[str, StackDefinition] = {}
    seen_ids: set[str] = set()
    for raw_name, raw_stack in value.items():
        name = _stack_name(raw_name, "stack name")
        stack = _exact(
            raw_stack, {"controller", "agents"}, f"stack {name}"
        )
        agents = stack["agents"]
        legacy_agents = isinstance(agents, dict) and set(agents) == set(LEGACY_ROLES)
        if not isinstance(agents, dict) or (
            set(agents) != set(LEGACY_ROLES)
            and set(agents) != set(ROLES)
        ):
            raise StackDefinitionError(
                f"stack {name} agents must contain exactly {sorted(set(ROLES))}"
            )
        roles = LEGACY_ROLES if legacy_agents else ROLES
        if schema_version == 1:
            controller = _migrated_candidates(
                stack["controller"],
                stack=name,
                scope="controller",
                models=models,
                multiple=False,
            )
            normalized_agents = {
                role: _migrated_candidates(
                    agents[role],
                    stack=name,
                    scope=role,
                    models=models,
                    multiple=True,
                )
                for role in roles
            }
        else:
            controller = _candidate_list(
                stack["controller"],
                models=models,
                seen_ids=seen_ids,
                label=f"stack {name} controller",
            )
            normalized_agents = {
                role: _candidate_list(
                    agents[role],
                    models=models,
                    seen_ids=seen_ids,
                    label=f"stack {name} role {role}",
                )
                for role in roles
            }
        if legacy_agents:
            architecture = normalized_agents["architecture-advisor"]
            planning = tuple(
                StackCandidate(
                    id=candidate_id(name, "planning-advisor", ordinal, candidate.model),
                    model=candidate.model,
                    providers=candidate.providers,
                )
                for ordinal, candidate in enumerate(architecture)
            )
            if any(candidate.id in seen_ids for candidate in planning):
                raise StackDefinitionError(
                    f"stack {name} planning-advisor has duplicate candidate IDs"
                )
            seen_ids.update(candidate.id for candidate in planning)
            normalized_agents["planning-advisor"] = planning
        stacks[name] = StackDefinition(
            name=name,
            controller=controller,
            agents=MappingProxyType(normalized_agents),
        )
    return stacks


def normalize_model_stacks(raw: object) -> NormalizedStacks:
    document = _exact(
        raw,
        {"schemaVersion", "defaultStack", "models", "stacks"},
        "model-stacks",
    )
    schema_version = document["schemaVersion"]
    if type(schema_version) is not int or schema_version not in (1, 2):
        raise StackDefinitionError(
            "model-stacks schemaVersion must be exactly 1 or 2"
        )
    default_stack = _stack_name(
        document["defaultStack"], "defaultStack"
    )
    models = _parse_models(document["models"], schema_version)
    stacks = _parse_stacks(
        document["stacks"],
        schema_version=schema_version,
        models=models,
    )
    if default_stack not in stacks:
        raise StackDefinitionError(
            "defaultStack does not name an existing stack"
        )
    return NormalizedStacks(
        default_stack=default_stack,
        models=MappingProxyType(models),
        stacks=MappingProxyType(stacks),
    )


def _serialize_candidate(candidate: StackCandidate) -> dict[str, object]:
    return {
        "id": candidate.id,
        "model": candidate.model,
        "providers": list(candidate.providers),
    }


def serialize_model_stacks(value: NormalizedStacks) -> dict[str, object]:
    return {
        "schemaVersion": 2,
        "defaultStack": value.default_stack,
        "models": {
            model: {
                "family": definition.family,
                "routes": dict(sorted(definition.routes.items())),
            }
            for model, definition in sorted(value.models.items())
        },
        "stacks": {
            name: {
                "controller": [
                    _serialize_candidate(candidate)
                    for candidate in stack.controller
                ],
                "agents": {
                    role: [
                        _serialize_candidate(candidate)
                        for candidate in stack.agents[role]
                    ]
                    for role in ROLES
                },
            }
            for name, stack in sorted(value.stacks.items())
        },
    }
