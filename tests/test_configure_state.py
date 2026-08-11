#!/usr/bin/env python3
from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from integrations.common.account_registry import Account
from integrations.common.configure_state import (
    CatalogueDrift,
    ConfigurationDraft,
    ConfigurationSnapshot,
    ModelSelection,
    ProjectTarget,
    _live_assignment,
    build_managed_stack,
    compatible_backup_accounts,
    load_configuration_snapshot,
    managed_stack_name,
    revalidate_draft,
    selections_for_stack,
    stack_is_live_compatible,
)
from integrations.common.orichum_config import ResolvedConfig
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_catalog import LiveCatalog, LiveModelChoice
from integrations.common.stack_definition import (
    normalize_model_stacks,
    serialize_model_stacks,
)


def _account(
    identifier: str,
    name: str,
    provider: str,
    *,
    priority: int = 100,
    state: str = "active",
) -> Account:
    return Account(
        id=identifier,
        name=name,
        provider=provider,
        credential_ref=f"{identifier}.json",
        pool="shared",
        routing_prefix=f"oc-r-{identifier[-16:]}",
        priority=priority,
        state=state,
        original_prefix=None,
        original_priority=None,
    )


def _snapshot() -> ConfigurationSnapshot:
    primary = _account(
        "oc-a-aaaaaaaaaaaaaaaa",
        "OpenAI primary",
        "openai",
    )
    backup = _account(
        "oc-a-bbbbbbbbbbbbbbbb",
        "OpenAI backup",
        "openai",
        priority=50,
    )
    other = _account(
        "oc-a-cccccccccccccccc",
        "Claude work",
        "anthropic",
    )
    stacks = normalize_model_stacks(
        {
            "schemaVersion": 2,
            "defaultStack": "balanced",
            "models": {
                "gpt-5.6-sol": {
                    "family": "gpt",
                    "routes": {"openai": "gpt-5.6-sol"},
                }
            },
            "stacks": {
                "balanced": {
                    "controller": [
                        {
                            "id": "oc-c-1111111111111111",
                            "model": "gpt-5.6-sol",
                            "providers": ["openai"],
                        }
                    ],
                    "agents": {
                        role: [
                            {
                                "id": f"oc-c-{index:016x}",
                                "model": "gpt-5.6-sol",
                                "providers": ["openai"],
                            }
                        ]
                        for index, role in enumerate(
                            (
                                "repository-explorer",
                                "repository-verifier",
                                "correctness-critic",
                                "architecture-advisor",
                                "implementation-worker",
                            ),
                            start=2,
                        )
                    },
                }
            },
        }
    )
    catalog = LiveCatalog(
        choices=(
            LiveModelChoice(
                family="gpt",
                provider="openai",
                upstream="gpt-5.6-sol",
                account_ids=(primary.id, backup.id),
                account_names=(primary.name, backup.name),
            ),
        ),
        unclassified=(),
    )
    selection = ModelSelection(
        model="gpt-5.6-sol",
        family="gpt",
        provider="openai",
        upstream="gpt-5.6-sol",
        account_ids=(primary.id, backup.id),
        account_names=(primary.name, backup.name),
    )
    return ConfigurationSnapshot(
        target=ProjectTarget(
            root=Path("/work/acme"),
            stack_name="balanced",
            pools=("shared",),
        ),
        accounts=(primary, backup, other),
        catalog=catalog,
        stacks=stacks,
        bindings=StackBindings({}),
        assignments=MappingProxyType(
            {
                role: selection
                for role in (
                    "controller",
                    "repository-explorer",
                    "repository-verifier",
                    "correctness-critic",
                    "architecture-advisor",
                    "implementation-worker",
                )
            }
        ),
    )


def _snapshot_with_alternate_profile() -> ConfigurationSnapshot:
    base = _snapshot()
    document = serialize_model_stacks(base.stacks)
    models = document["models"]
    stacks = document["stacks"]
    assert isinstance(models, dict)
    assert isinstance(stacks, dict)
    models["claude-sonnet-5"] = {
        "family": "claude",
        "routes": {"anthropic": "claude-sonnet-5"},
    }
    stacks["quality"] = {
        "controller": [
            {
                "id": "oc-c-9999999999999999",
                "model": "claude-sonnet-5",
                "providers": ["anthropic"],
            }
        ],
        "agents": {
            role: [
                {
                    "id": f"oc-c-{index:016x}",
                    "model": "claude-sonnet-5",
                    "providers": ["anthropic"],
                }
            ]
            for index, role in enumerate(
                base.stacks.stacks["balanced"].agents,
                start=100,
            )
        },
    }
    other = base.accounts[2]
    return replace(
        base,
        stacks=normalize_model_stacks(document),
        catalog=LiveCatalog(
            choices=(
                *base.catalog.choices,
                LiveModelChoice(
                    family="claude",
                    provider="anthropic",
                    upstream="claude-sonnet-5",
                    account_ids=(other.id,),
                    account_names=(other.name,),
                ),
            ),
            unclassified=(),
        ),
    )


class ConfigureStateTests(unittest.TestCase):
    def test_profile_switch_tracks_target_until_role_customization(self) -> None:
        snapshot = _snapshot_with_alternate_profile()
        selections = selections_for_stack(snapshot, "quality")

        draft = ConfigurationDraft.from_snapshot(snapshot).with_profile(
            replace(snapshot.target, stack_name="quality"),
            selections,
        )

        self.assertTrue(draft.changed)
        self.assertEqual(draft.profile_switch, "quality")
        self.assertEqual(
            {selection.model for selection in draft.role_models.values()},
            {"claude-sonnet-5"},
        )
        customized = draft.with_roles(
            ("controller",),
            snapshot.assignments["controller"],
        )
        self.assertIsNone(customized.profile_switch)

    def test_snapshot_uses_default_stack_when_project_inherits_it(self) -> None:
        existing = _snapshot()
        providers = {
            "providers": {"openai": {}},
            "accountPools": {"shared": {"providers": ["openai"]}},
        }
        config = ResolvedConfig(
            documents={"providers": providers, "projects": {}},
            sources={},
        )
        route = {
            "contextRootReal": "/work/acme",
            "modelStack": None,
            "accountPools": ["shared"],
        }

        with (
            mock.patch(
                "integrations.common.configure_state.load_stack_snapshot",
                return_value=mock.Mock(
                    stacks=existing.stacks,
                    bindings=existing.bindings,
                ),
            ),
            mock.patch(
                "integrations.common.configure_state.load_accounts",
                return_value=existing.accounts[:2],
            ),
            mock.patch(
                "integrations.common.configure_state.resolve_control_plane_context",
                return_value={"route": route},
            ),
            mock.patch(
                "integrations.common.configure_state.discover_project_models",
                return_value=None,
            ),
            mock.patch(
                "integrations.common.stack_wizard._runtime_catalog_port",
                return_value=13457,
            ),
            mock.patch(
                "integrations.common.stack_wizard._runtime_catalog_attester",
                return_value=mock.Mock(),
            ),
            mock.patch("integrations.common.configure_state.fetch_live_catalog"),
            mock.patch(
                "integrations.common.configure_state.project_live_catalog",
                return_value=existing.catalog,
            ),
        ):
            loaded = load_configuration_snapshot(
                {"config": Path("/private/config")},
                config,
                Path("/work/acme"),
            )

        self.assertEqual(loaded.target.stack_name, "balanced")

    def test_backup_candidates_are_same_provider_active_and_route_compatible(
        self,
    ) -> None:
        snapshot = _snapshot()

        candidates = compatible_backup_accounts(
            snapshot,
            snapshot.accounts[0],
        )

        self.assertEqual(
            tuple(account.name for account in candidates),
            ("OpenAI backup",),
        )

    def test_managed_stack_name_is_stable_and_hides_the_path(self) -> None:
        first = managed_stack_name(Path("/work/acme"))
        second = managed_stack_name(Path("/work/acme/../acme"))

        self.assertEqual(first, second)
        self.assertRegex(first, r"^orichum-project-[0-9a-f]{12}$")
        self.assertNotIn("acme", first)

    def test_revalidation_names_only_roles_with_missing_live_routes(self) -> None:
        snapshot = _snapshot()
        draft = ConfigurationDraft.from_snapshot(snapshot)
        refreshed = LiveCatalog(choices=(), unclassified=())

        drift = revalidate_draft(snapshot, draft, refreshed)

        self.assertEqual(
            drift,
            CatalogueDrift(
                invalid_roles=(
                    "controller",
                    "repository-explorer",
                    "repository-verifier",
                    "correctness-critic",
                    "architecture-advisor",
                    "implementation-worker",
                )
            ),
        )

    def test_snapshot_repr_does_not_expose_internal_account_ids(self) -> None:
        rendered = repr(_snapshot())

        self.assertNotIn("oc-a-aaaaaaaaaaaaaaaa", rendered)
        self.assertNotIn("oc-a-bbbbbbbbbbbbbbbb", rendered)

    def test_managed_stack_assigns_every_concrete_role(self) -> None:
        snapshot = _snapshot()
        draft = ConfigurationDraft.from_snapshot(snapshot).with_roles(
            tuple(snapshot.assignments),
            snapshot.assignments["controller"],
        )

        updated = build_managed_stack(snapshot, draft)

        stack = updated.stacks[managed_stack_name(snapshot.target.root)]
        self.assertEqual(stack.controller[0].model, "gpt-5.6-sol")
        self.assertEqual(tuple(stack.agents), tuple(snapshot.assignments)[1:])
        self.assertTrue(
            all(
                candidates[0].model == "gpt-5.6-sol"
                for candidates in stack.agents.values()
            )
        )

    def test_backup_must_cover_every_route_used_by_primary(self) -> None:
        snapshot = _snapshot()
        primary_only = replace(
            snapshot.assignments["repository-explorer"],
            account_ids=(snapshot.accounts[0].id,),
            account_names=(snapshot.accounts[0].name,),
        )
        assignments = dict(snapshot.assignments)
        assignments["repository-explorer"] = primary_only
        partial = replace(snapshot, assignments=assignments)

        candidates = compatible_backup_accounts(
            partial,
            partial.accounts[0],
        )

        self.assertEqual(candidates, ())

    def test_stack_compatibility_rejects_unavailable_provider_route(self) -> None:
        snapshot = _snapshot()
        document = {
            "schemaVersion": 2,
            "defaultStack": snapshot.stacks.default_stack,
            "models": {
                "gpt-5.6-sol": {
                    "family": "gpt",
                    "routes": {"openai": "gpt-5.6-sol"},
                },
                "claude-opus-5": {
                    "family": "claude",
                    "routes": {"anthropic": "claude-opus-5"},
                },
            },
            "stacks": {
                "balanced": {
                    "controller": [
                        {
                            "id": "oc-c-1111111111111111",
                            "model": "gpt-5.6-sol",
                            "providers": ["openai"],
                        }
                    ],
                    "agents": {
                        role: [
                            {
                                "id": f"oc-c-{index:016x}",
                                "model": "gpt-5.6-sol",
                                "providers": ["openai"],
                            }
                        ]
                        for index, role in enumerate(
                            tuple(snapshot.assignments)[1:],
                            start=2,
                        )
                    },
                },
                "offline": {
                    "controller": [
                        {
                            "id": "oc-c-9999999999999999",
                            "model": "claude-opus-5",
                            "providers": ["anthropic"],
                        }
                    ],
                    "agents": {
                        role: [
                            {
                                "id": f"oc-c-{index:016x}",
                                "model": "claude-opus-5",
                                "providers": ["anthropic"],
                            }
                        ]
                        for index, role in enumerate(
                            tuple(snapshot.assignments)[1:],
                            start=20,
                        )
                    },
                },
            },
        }
        expanded = replace(snapshot, stacks=normalize_model_stacks(document))

        self.assertTrue(stack_is_live_compatible(expanded, "balanced"))
        self.assertFalse(stack_is_live_compatible(expanded, "offline"))

    def test_assignment_skips_an_offline_leading_candidate(self) -> None:
        snapshot = _snapshot()
        document = serialize_model_stacks(snapshot.stacks)
        document["models"]["gpt-offline"] = {
            "family": "gpt",
            "routes": {"openai": "gpt-offline"},
        }
        document["stacks"]["balanced"]["controller"].insert(
            0,
            {
                "id": "oc-c-9999999999999999",
                "model": "gpt-offline",
                "providers": ["openai"],
            },
        )
        stacks = normalize_model_stacks(document)

        assignment = _live_assignment(
            stacks.stacks["balanced"].controller,
            stacks,
            snapshot.bindings,
            snapshot.catalog,
        )

        self.assertEqual(assignment.model, "gpt-5.6-sol")
        self.assertEqual(
            assignment.account_ids, snapshot.catalog.choices[0].account_ids
        )


if __name__ == "__main__":
    unittest.main()
