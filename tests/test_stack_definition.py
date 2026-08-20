#!/usr/bin/env python3
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import MappingProxyType
import unittest

from integrations.common.model_routing import ROLES as ROUTED_ROLES
from integrations.common.stack_definition import (
    StackDefinitionError,
    candidate_id,
    normalize_model_stacks,
    serialize_model_stacks,
)


ROLES = (
    "repository-explorer",
    "repository-verifier",
    "correctness-critic",
    "architecture-advisor",
    "implementation-worker",
)


class StackDefinitionTests(unittest.TestCase):
    def test_shipped_stack_prefers_current_opus_by_provider(self) -> None:
        document = json.loads(
            (Path(__file__).parents[1] / "config/model-stacks.json").read_text(
                encoding="utf-8"
            )
        )

        normalized = normalize_model_stacks(document)
        candidates = normalized.stacks["balanced"].agents[
            "architecture-advisor"
        ]

        self.assertEqual(
            [(candidate.model, candidate.providers) for candidate in candidates],
            [
                ("claude-opus-5", ("anthropic",)),
                ("claude-opus-4-6-thinking", ("antigravity",)),
            ],
        )
        self.assertEqual(
            [
                (candidate.model, candidate.providers)
                for candidate in normalized.stacks["balanced"].agents[
                    "planning-advisor"
                ]
            ],
            [(candidate.model, candidate.providers) for candidate in candidates],
        )
        self.assertEqual(
            normalized.models["claude-opus-5"].routes["anthropic"],
            "claude-opus-5",
        )
        self.assertEqual(
            normalized.models["claude-opus-4-6-thinking"].routes[
                "antigravity"
            ],
            "claude-opus-4-6-thinking",
        )
    def v1_document(self) -> dict[str, object]:
        models = {
            "gpt-5.6-sol": {
                "provider": "openai",
                "family": "gpt",
                "upstream": "gpt-5.6-sol",
            },
            "gpt-5.6-terra": {
                "provider": "openai",
                "family": "gpt",
                "upstream": "gpt-5.6-terra",
            },
            "claude-sonnet-5": {
                "provider": "anthropic",
                "family": "claude",
                "upstream": "claude-sonnet-5",
            },
            "claude-opus-4-8": {
                "provider": "anthropic",
                "family": "claude",
                "upstream": "claude-opus-4-8",
            },
        }
        return {
            "schemaVersion": 1,
            "defaultStack": "balanced",
            "models": models,
            "stacks": {
                "balanced": {
                    "controller": "gpt-5.6-sol",
                    "agents": {
                        "repository-explorer": ["gpt-5.6-terra"],
                        "repository-verifier": ["gpt-5.6-terra"],
                        "correctness-critic": ["claude-sonnet-5"],
                        "architecture-advisor": ["claude-opus-4-8"],
                        "implementation-worker": ["gpt-5.6-sol"],
                    },
                }
            },
        }

    def v2_document(self) -> dict[str, object]:
        document = self.v1_document()
        document["schemaVersion"] = 2
        document["models"] = {
            model: {
                "family": metadata["family"],
                "routes": {
                    metadata["provider"]: metadata["upstream"],
                },
            }
            for model, metadata in document["models"].items()
        }
        stack = document["stacks"]["balanced"]
        controller_model = stack["controller"]
        stack["controller"] = [
            {
                "id": "oc-c-0000000000000001",
                "model": controller_model,
                "providers": ["openai"],
            }
        ]
        for ordinal, role in enumerate(ROLES, start=2):
            model = stack["agents"][role][0]
            provider = next(iter(document["models"][model]["routes"]))
            stack["agents"][role] = [
                {
                    "id": f"oc-c-{ordinal:016x}",
                    "model": model,
                    "providers": [provider],
                }
            ]
        return document

    def test_v1_migration_is_stable_and_preserves_selection(self):
        first = normalize_model_stacks(self.v1_document())
        second = normalize_model_stacks(serialize_model_stacks(first))
        self.assertEqual(
            serialize_model_stacks(first), serialize_model_stacks(second)
        )
        candidate = first.stacks["balanced"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-8")
        self.assertEqual(candidate.providers, ("anthropic",))
        self.assertRegex(candidate.id, r"^oc-c-[0-9a-f]{16}$")
        planning = first.stacks["balanced"].agents["planning-advisor"]
        self.assertEqual(
            [(item.model, item.providers) for item in planning],
            [(item.model, item.providers) for item in [candidate]],
        )
        self.assertNotEqual(planning[0].id, candidate.id)


    def test_v1_migration_enforces_exact_route_uniqueness_stably(self):
        allowed = self.v1_document()
        allowed["models"]["gpt-5.6-terra-proxy"] = {
            "provider": "antigravity",
            "family": "gpt",
            "upstream": "gpt-5.6-terra",
        }
        allowed["stacks"]["balanced"]["agents"][
            "repository-explorer"
        ].append("gpt-5.6-terra-proxy")

        migrated = normalize_model_stacks(allowed)
        reloaded = normalize_model_stacks(
            serialize_model_stacks(migrated)
        )

        self.assertEqual(
            serialize_model_stacks(migrated),
            serialize_model_stacks(reloaded),
        )

        duplicate = self.v1_document()
        duplicate["models"]["gpt-5.6-terra-alias"] = {
            "provider": "openai",
            "family": "gpt",
            "upstream": "gpt-5.6-terra",
        }
        duplicate["stacks"]["balanced"]["agents"][
            "repository-explorer"
        ].append("gpt-5.6-terra-alias")

        with self.assertRaisesRegex(
            StackDefinitionError, "duplicate provider route"
        ):
            normalize_model_stacks(duplicate)

    def test_rejects_cross_family_candidates_for_one_role(self):
        document = self.v2_document()
        document["stacks"]["balanced"]["agents"][
            "architecture-advisor"
        ].append(
            {
                "id": "oc-c-1111111111111111",
                "model": "gpt-5.6-sol",
                "providers": ["openai"],
            }
        )
        with self.assertRaisesRegex(
            StackDefinitionError, "same model family"
        ):
            normalize_model_stacks(document)

    def test_native_v2_preserves_candidate_ids_and_returns_immutable_state(
        self,
    ) -> None:
        normalized = normalize_model_stacks(self.v2_document())

        self.assertEqual(
            normalized.stacks["balanced"].controller[0].id,
            "oc-c-0000000000000001",
        )
        self.assertIsInstance(normalized.models, MappingProxyType)
        self.assertIsInstance(
            normalized.models["gpt-5.6-sol"].routes, MappingProxyType
        )
        self.assertIsInstance(normalized.stacks, MappingProxyType)
        self.assertIsInstance(
            normalized.stacks["balanced"].agents, MappingProxyType
        )
        with self.assertRaises(TypeError):
            normalized.stacks["other"] = normalized.stacks["balanced"]

    def test_normalized_state_retains_the_routing_mapping_view(self) -> None:
        normalized = normalize_model_stacks(self.v2_document())

        self.assertEqual(normalized["schemaVersion"], 2)
        self.assertEqual(normalized["defaultStack"], "balanced")
        self.assertIs(normalized["stacks"], normalized.stacks)
        self.assertEqual(
            tuple(normalized), ("schemaVersion", "defaultStack", "stacks")
        )

    def test_serialization_is_canonical_and_preserves_candidate_order(
        self,
    ) -> None:
        document = self.v2_document()
        document["models"] = dict(reversed(document["models"].items()))

        serialized = serialize_model_stacks(normalize_model_stacks(document))

        self.assertEqual(serialized["schemaVersion"], 2)
        self.assertEqual(
            list(serialized["models"]),
            [
                "claude-opus-4-8",
                "claude-sonnet-5",
                "gpt-5.6-sol",
                "gpt-5.6-terra",
            ],
        )
        self.assertEqual(
            list(serialized["stacks"]["balanced"]["agents"]),
            list(ROUTED_ROLES),
        )
        self.assertEqual(
            serialized["stacks"]["balanced"]["controller"][0]["id"],
            "oc-c-0000000000000001",
        )

    def test_candidate_id_uses_the_stable_contract(self) -> None:
        self.assertEqual(
            candidate_id("balanced", "controller", 0, "gpt-5.6-sol"),
            "oc-c-c64159d152c2cf90",
        )
        self.assertNotEqual(
            candidate_id("balanced", "controller", 0, "gpt-5.6-sol"),
            candidate_id("balanced", "controller", 1, "gpt-5.6-sol"),
        )

    def test_rejects_invalid_document_and_schema_fields(self) -> None:
        cases = (
            [],
            {"schemaVersion": 2},
            {**self.v2_document(), "extra": True},
            {**self.v2_document(), "schemaVersion": 3},
            {**self.v2_document(), "schemaVersion": True},
        )
        for document in cases:
            with self.subTest(document=document):
                with self.assertRaises(StackDefinitionError):
                    normalize_model_stacks(document)

    def test_rejects_unsafe_unknown_duplicate_and_empty_candidates(
        self,
    ) -> None:
        def mutate_unsafe_id(document):
            document["stacks"]["balanced"]["controller"][0]["id"] = (
                "unsafe\ncandidate"
            )

        def mutate_unknown_model(document):
            document["stacks"]["balanced"]["controller"][0]["model"] = (
                "missing/model"
            )

        def mutate_duplicate_id(document):
            document["stacks"]["balanced"]["agents"][
                "repository-explorer"
            ][0]["id"] = "oc-c-0000000000000001"

        def mutate_duplicate_model(document):
            candidate = deepcopy(
                document["stacks"]["balanced"]["agents"][
                    "repository-explorer"
                ][0]
            )
            candidate["id"] = "oc-c-1111111111111111"
            document["stacks"]["balanced"]["agents"][
                "repository-explorer"
            ].append(candidate)

        def mutate_empty_controller(document):
            document["stacks"]["balanced"]["controller"] = []

        def mutate_empty_role(document):
            document["stacks"]["balanced"]["agents"][
                "repository-explorer"
            ] = []

        for mutation in (
            mutate_unsafe_id,
            mutate_unknown_model,
            mutate_duplicate_id,
            mutate_duplicate_model,
            mutate_empty_controller,
            mutate_empty_role,
        ):
            document = self.v2_document()
            mutation(document)
            with self.subTest(mutation=mutation.__name__):
                with self.assertRaises(StackDefinitionError):
                    normalize_model_stacks(document)

    def test_rejects_candidate_provider_without_a_model_route(self) -> None:
        document = self.v2_document()
        document["stacks"]["balanced"]["controller"][0]["providers"] = [
            "anthropic"
        ]

        with self.assertRaisesRegex(StackDefinitionError, "route"):
            normalize_model_stacks(document)

    def test_rejects_duplicate_provider_upstream_routes_in_candidate_list(
        self,
    ) -> None:
        document = self.v2_document()
        document["models"]["gpt-5.6-terra-alias"] = {
            "family": "gpt",
            "routes": {"openai": "gpt-5.6-terra"},
        }
        document["stacks"]["balanced"]["agents"][
            "repository-explorer"
        ].append(
            {
                "id": "oc-c-1111111111111111",
                "model": "gpt-5.6-terra-alias",
                "providers": ["openai"],
            }
        )

        with self.assertRaisesRegex(
            StackDefinitionError, "duplicate provider route"
        ):
            normalize_model_stacks(document)

    def test_rejects_extra_nested_fields_and_invalid_provider_lists(
        self,
    ) -> None:
        def mutate_model(document):
            document["models"]["gpt-5.6-sol"]["extra"] = True

        def mutate_stack(document):
            document["stacks"]["balanced"]["extra"] = True

        def mutate_candidate(document):
            document["stacks"]["balanced"]["controller"][0]["extra"] = True

        def mutate_empty_providers(document):
            document["stacks"]["balanced"]["controller"][0]["providers"] = []

        def mutate_duplicate_provider(document):
            document["stacks"]["balanced"]["controller"][0]["providers"] = [
                "openai",
                "openai",
            ]

        def mutate_unsafe_provider(document):
            document["stacks"]["balanced"]["controller"][0]["providers"] = [
                "unsafe/provider"
            ]

        for mutation in (
            mutate_model,
            mutate_stack,
            mutate_candidate,
            mutate_empty_providers,
            mutate_duplicate_provider,
            mutate_unsafe_provider,
        ):
            document = self.v2_document()
            mutation(document)
            with self.subTest(mutation=mutation.__name__):
                with self.assertRaises(StackDefinitionError):
                    normalize_model_stacks(document)


if __name__ == "__main__":
    unittest.main()
