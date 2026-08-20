#!/usr/bin/env python3
from __future__ import annotations

import contextlib
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import pty
import shutil
import struct
import sys
import termios
import tempfile
import threading
import traceback
from types import MappingProxyType, ModuleType
from typing import Callable
import unittest
from unittest import mock

from integrations.common import stack_wizard
from integrations.common.account_registry import Account, update_accounts
from integrations.common.model_routing import resolve_effective
from integrations.common.orichum_config import ResolvedConfig
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_catalog import LiveCatalog, LiveModelChoice
from integrations.common.stack_definition import (
    NormalizedStacks,
    normalize_model_stacks,
)
from integrations.common.stack_store import StackSnapshot
from integrations.common.stack_wizard import (
    BACK,
    Choice,
    StackWizard,
    TerminalWizardIO,
    WizardCancelled,
    WizardResult,
    build_recommended_stack,
    run_stack_wizard,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _account(
    identifier: str,
    name: str,
    provider: str,
    prefix: str,
) -> Account:
    return Account(
        id=identifier,
        name=name,
        provider=provider,
        credential_ref=f"{provider}.json",
        pool="shared",
        routing_prefix=prefix,
        priority=100,
        state="active",
        original_prefix=None,
        original_priority=None,
    )


class ScriptedIO:
    """Select named choices while accepting inherited defaults in between."""

    def __init__(
        self,
        *,
        choices: list[str] | None = None,
        confirmations: list[bool] | None = None,
        text: list[str] | None = None,
        cancel_at: int | None = None,
        disconnect_at: int | None = None,
        back_at: int | None = None,
        back_titles: list[str] | None = None,
        back_title_occurrences: dict[str, list[int]] | None = None,
    ) -> None:
        self.choices = list(choices or [])
        self.confirmations = list(confirmations or [])
        self.text_values = list(text or [])
        self.cancel_at = cancel_at
        self.disconnect_at = disconnect_at
        self.back_at = back_at
        self.back_titles = list(back_titles or [])
        self.back_title_occurrences = {
            title: list(occurrences)
            for title, occurrences in (
                back_title_occurrences or {}
            ).items()
        }
        self.title_calls: dict[str, int] = {}
        self.calls = 0
        self.shown: list[str] = []
        self.titles: list[str] = []

    def _before(self) -> None:
        self.calls += 1
        if self.cancel_at == self.calls:
            raise KeyboardInterrupt
        if self.disconnect_at == self.calls:
            raise EOFError

    def choose(
        self,
        title: str,
        options: list[Choice],
        selected: int = 0,
        searchable: bool = False,
    ) -> int:
        self._before()
        self.titles.append(title)
        self.title_calls[title] = self.title_calls.get(title, 0) + 1
        if self.back_at == self.calls:
            return BACK
        if self.back_titles and self.back_titles[0] == title:
            self.back_titles.pop(0)
            return BACK
        occurrences = self.back_title_occurrences.get(title, [])
        if occurrences and occurrences[0] == self.title_calls[title]:
            occurrences.pop(0)
            return BACK
        labels = [option.label for option in options]
        if self.choices and self.choices[0] in labels:
            return labels.index(self.choices.pop(0))
        return selected

    def confirm(self, prompt: str, default: bool = False) -> bool:
        self._before()
        if self.confirmations:
            return self.confirmations.pop(0)
        return default

    def text(self, prompt: str, initial: str = "") -> str:
        self._before()
        if self.text_values:
            return self.text_values.pop(0)
        return initial

    def show(self, summary: str) -> None:
        self.shown.append(summary)


class StackWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.accounts = (
            _account(
                "oc-a-aaaaaaaaaaaaaaaa",
                "Work OpenAI",
                "openai",
                "oc-r-aaaaaaaaaaaaaaaa",
            ),
            _account(
                "oc-a-bbbbbbbbbbbbbbbb",
                "Work Claude",
                "anthropic",
                "oc-r-bbbbbbbbbbbbbbbb",
            ),
            _account(
                "oc-a-cccccccccccccccc",
                "Personal Antigravity",
                "antigravity",
                "oc-r-cccccccccccccccc",
            ),
        )
        self.stacks = normalize_model_stacks(
            {
                "schemaVersion": 2,
                "defaultStack": "balanced",
                "models": {
                    "gpt-5.6-sol": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-5.6-sol"},
                    },
                    "gpt-5.6-terra": {
                        "family": "gpt",
                        "routes": {"openai": "gpt-5.6-terra"},
                    },
                    "claude-sonnet-5": {
                        "family": "claude",
                        "routes": {"anthropic": "claude-sonnet-5"},
                    },
                    "claude-opus-4-8": {
                        "family": "claude",
                        "routes": {"anthropic": "claude-opus-4-8"},
                    },
                },
                "stacks": {
                    "balanced": {
                        "controller": [
                            {
                                "id": "oc-c-c64159d152c2cf90",
                                "model": "gpt-5.6-sol",
                                "providers": ["openai"],
                            }
                        ],
                        "agents": {
                            "repository-explorer": [
                                {
                                    "id": "oc-c-1db0df6c362e02e9",
                                    "model": "gpt-5.6-terra",
                                    "providers": ["openai"],
                                }
                            ],
                            "repository-verifier": [
                                {
                                    "id": "oc-c-38e35c710e57f9ea",
                                    "model": "gpt-5.6-terra",
                                    "providers": ["openai"],
                                }
                            ],
                            "correctness-critic": [
                                {
                                    "id": "oc-c-a69e16d6ee83ad12",
                                    "model": "claude-sonnet-5",
                                    "providers": ["anthropic"],
                                }
                            ],
                            "architecture-advisor": [
                                {
                                    "id": "oc-c-15e855e1e22ff2c1",
                                    "model": "claude-opus-4-8",
                                    "providers": ["anthropic"],
                                }
                            ],
                            "implementation-worker": [
                                {
                                    "id": "oc-c-a24e82f9843f457c",
                                    "model": "gpt-5.6-sol",
                                    "providers": ["openai"],
                                }
                            ],
                        },
                    }
                },
            }
        )
        self.snapshot = StackSnapshot(
            self.stacks,
            StackBindings({}),
            "0" * 64,
            None,
        )
        self.catalog = LiveCatalog(
            choices=(
                LiveModelChoice(
                    "claude",
                    "antigravity",
                    "claude-opus-4-6-thinking",
                    ("oc-a-cccccccccccccccc",),
                    ("Personal Antigravity",),
                ),
                LiveModelChoice(
                    "claude",
                    "anthropic",
                    "claude-opus-4-8",
                    ("oc-a-bbbbbbbbbbbbbbbb",),
                    ("Work Claude",),
                ),
                LiveModelChoice(
                    "claude",
                    "anthropic",
                    "claude-sonnet-5",
                    ("oc-a-bbbbbbbbbbbbbbbb",),
                    ("Work Claude",),
                ),
                LiveModelChoice(
                    "gpt",
                    "openai",
                    "gpt-5.6-sol",
                    ("oc-a-aaaaaaaaaaaaaaaa",),
                    ("Work OpenAI",),
                ),
                LiveModelChoice(
                    "gpt",
                    "openai",
                    "gpt-5.6-terra",
                    ("oc-a-aaaaaaaaaaaaaaaa",),
                    ("Work OpenAI",),
                ),
            ),
            unclassified=(),
        )

    def test_recommended_openai_stack_uses_role_specific_models(self) -> None:
        openai_catalog = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider == "openai"
            ),
            unclassified=(),
        )

        updated = build_recommended_stack(self.snapshot, openai_catalog)

        stack = updated.stacks["recommended"]
        self.assertEqual(stack.controller[0].model, "gpt-5.6-sol")
        self.assertEqual(
            stack.agents["repository-explorer"][0].model,
            "gpt-5.6-terra",
        )
        self.assertEqual(
            stack.agents["repository-verifier"][0].model,
            "gpt-5.6-terra",
        )
        self.assertEqual(
            stack.agents["correctness-critic"][0].model,
            "gpt-5.6-terra",
        )
        self.assertEqual(
            stack.agents["architecture-advisor"][0].model,
            "gpt-5.6-sol",
        )
        self.assertEqual(
            stack.agents["implementation-worker"][0].model,
            "gpt-5.6-sol",
        )
        for candidates in (stack.controller, *stack.agents.values()):
            self.assertEqual(candidates[0].providers, ("openai",))

    def test_recommended_stack_replaces_an_unusable_default(self) -> None:
        openai_catalog = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider == "openai"
            ),
            unclassified=(),
        )

        updated = build_recommended_stack(self.snapshot, openai_catalog)

        self.assertEqual(updated.default_stack, "recommended")
        effective = resolve_effective(
            updated,
            tuple(choice.upstream for choice in openai_catalog.choices),
        )
        self.assertEqual(effective.stack_name, "recommended")

    def test_recommended_stack_preserves_a_usable_default(self) -> None:
        updated = build_recommended_stack(self.snapshot, self.catalog)

        self.assertEqual(updated.default_stack, "balanced")

    def test_recommended_stack_supports_one_unfamiliar_live_model(self) -> None:
        catalog = LiveCatalog(
            choices=(
                LiveModelChoice(
                    "kimi",
                    "kimi",
                    "kimi-for-coding",
                    ("oc-a-dddddddddddddddd",),
                    ("Personal Kimi",),
                ),
            ),
            unclassified=(),
        )

        updated = build_recommended_stack(self.snapshot, catalog)

        definition = updated.models["kimi-for-coding"]
        self.assertEqual(definition.family, "kimi")
        self.assertEqual(dict(definition.routes), {"kimi": "kimi-for-coding"})
        stack = updated.stacks["recommended"]
        for candidates in (stack.controller, *stack.agents.values()):
            self.assertEqual(candidates[0].model, "kimi-for-coding")
            self.assertEqual(candidates[0].providers, ("kimi",))

    def test_recommended_stack_reuses_identical_definition(self) -> None:
        openai_catalog = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider == "openai"
            ),
            unclassified=(),
        )
        first = build_recommended_stack(self.snapshot, openai_catalog)
        snapshot = replace(self.snapshot, stacks=first)

        second = build_recommended_stack(snapshot, openai_catalog)

        self.assertIs(second, first)

    def test_recommended_stack_reuses_different_live_compatible_definition(
        self,
    ) -> None:
        openai_catalog = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider == "openai"
            ),
            unclassified=(),
        )
        document = stack_wizard.serialize_model_stacks(self.snapshot.stacks)
        terra = {
            "id": "",
            "model": "gpt-5.6-terra",
            "providers": ["openai"],
        }
        document["stacks"]["recommended"] = {
            "controller": [
                {
                    **terra,
                    "id": stack_wizard.candidate_id(
                        "recommended", "controller", 0, terra["model"]
                    ),
                }
            ],
            "agents": {
                role: [
                    {
                        **terra,
                        "id": stack_wizard.candidate_id(
                            "recommended", role, 0, terra["model"]
                        ),
                    }
                ]
                for role in stack_wizard.ROLES
            },
        }
        existing = normalize_model_stacks(document)
        snapshot = replace(self.snapshot, stacks=existing)

        reused = build_recommended_stack(snapshot, openai_catalog)

        self.assertEqual(
            reused.stacks["recommended"],
            existing.stacks["recommended"],
        )
        self.assertEqual(reused.default_stack, "recommended")

    def test_recommended_stack_does_not_overwrite_name_collision(self) -> None:
        document = stack_wizard.serialize_model_stacks(self.snapshot.stacks)
        collision = json.loads(
            json.dumps(document["stacks"]["balanced"])
        )
        collision["controller"][0]["id"] = stack_wizard.candidate_id(
            "recommended", "controller", 0, "gpt-5.6-sol"
        )
        for role, candidates in collision["agents"].items():
            for ordinal, candidate in enumerate(candidates):
                candidate["id"] = stack_wizard.candidate_id(
                    "recommended", role, ordinal, candidate["model"]
                )
        document["stacks"]["recommended"] = collision
        snapshot = replace(
            self.snapshot,
            stacks=normalize_model_stacks(document),
        )
        openai_catalog = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider == "openai"
            ),
            unclassified=(),
        )

        with self.assertRaisesRegex(
            stack_wizard.RoutingError,
            "recommended already exists",
        ):
            build_recommended_stack(snapshot, openai_catalog)

    def test_recommended_stack_commit_validates_saves_and_assigns(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        project = Path(temporary.name).resolve() / "project"
        project.mkdir()
        projects = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": str(project),
                    "atlassian": None,
                    "modelStack": None,
                    "accountPools": ["shared"],
                }
            ],
        }
        config = ResolvedConfig(
            documents={
                "model-stacks": stack_wizard.serialize_model_stacks(
                    self.snapshot.stacks
                ),
                "projects": projects,
                "providers": {
                    "providers": {
                        "openai": {"families": ["gpt"]},
                    }
                },
            },
            sources={},
        )
        openai_catalog = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider == "openai"
            ),
            unclassified=(),
        )
        calls: list[str] = []
        paths = {"config": Path("/private/config"), "data": Path("/data")}

        endpoint = mock.Mock(port=8317)
        with (
            mock.patch.object(
                stack_wizard,
                "load_management_endpoint",
                return_value=endpoint,
                create=True,
            ) as load_endpoint,
            mock.patch.object(
                stack_wizard,
                "attest_owned_connection",
                create=True,
            ) as attest,
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ) as fetch,
            mock.patch.object(
                stack_wizard,
                "control_plane_transaction",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                stack_wizard,
                "stack_binding_transaction",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                stack_wizard, "load_control_plane", return_value=config
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard,
                "load_accounts",
                return_value=(self.accounts[0],),
            ),
            mock.patch.object(stack_wizard, "validate_account_bindings"),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=openai_catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "validate_control_plane",
                side_effect=lambda *_args: calls.append("control-plane"),
            ),
            mock.patch.object(
                stack_wizard,
                "validate_stack_assignment",
                side_effect=lambda *_args: calls.append("assignment"),
            ),
            mock.patch.object(
                stack_wizard,
                "save_stack",
                side_effect=lambda *_args: calls.append("save"),
            ) as save,
            mock.patch.object(
                stack_wizard,
                "assign_stack_to_context",
                side_effect=lambda *_args: calls.append("assign"),
            ) as assign,
        ):
            name = stack_wizard.create_recommended_stack(
                paths, config, project
            )
            fetch.call_args.kwargs["attest"](45678)

        self.assertEqual(name, "recommended")
        self.assertEqual(
            calls, ["control-plane", "assignment", "save", "assign"]
        )
        saved = save.call_args.args[1]
        self.assertIn("recommended", saved.stacks)
        self.assertIs(save.call_args.args[2], self.snapshot.bindings)
        self.assertEqual(assign.call_args.args[2], "recommended")
        self.assertEqual(
            load_endpoint.call_args_list,
            [mock.call(paths["data"]), mock.call(paths["data"])],
        )
        fetch.assert_called_once_with(8317, attest=mock.ANY)
        attest.assert_called_once_with(endpoint, 45678)

    def test_stack_bootstrap_does_not_reload_the_cli_module(self) -> None:
        endpoint = mock.Mock(port=8317)
        poison = ModuleType("integrations.common.orichum_cli")
        with (
            mock.patch.dict(
                sys.modules,
                {"integrations.common.orichum_cli": poison},
            ),
            mock.patch.object(
                stack_wizard,
                "load_management_endpoint",
                return_value=endpoint,
            ),
        ):
            try:
                port = stack_wizard._runtime_catalog_port(
                    {"data": Path("/private/data")}
                )
            except (AttributeError, ImportError) as error:
                self.fail(f"stack bootstrap reloaded the CLI module: {error}")

        self.assertEqual(port, 8317)

    def test_clone_select_review_save_and_assign(self) -> None:
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
            ],
            confirmations=[True, True],
            text=["heavy"],
        )

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        self.assertEqual(result.stack_name, "heavy")
        self.assertTrue(result.save)
        self.assertTrue(result.assign_current_project)
        candidate = result.stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-6-thinking")
        self.assertEqual(candidate.providers, ("antigravity",))
        self.assertEqual(
            result.bindings.candidate_accounts[candidate.id],
            "oc-a-cccccccccccccccc",
        )
        review = "\n".join(io_adapter.shown)
        self.assertIn("Step 4/5", review)
        self.assertIn("Personal Antigravity", review)
        self.assertNotIn("oc-a-", review)
        self.assertNotIn("oc-c-", review)
        self.assertNotIn("oc-r-", review)
        self.assertNotIn(".json", review)
        self.assertTrue(
            all(title.startswith("Step ") for title in io_adapter.titles)
        )

    def test_setup_flow_can_default_project_assignment_to_yes(self) -> None:
        wizard = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            ScriptedIO(),
            assignment_default=True,
        )

        self.assertTrue(
            wizard._assignment_choice(Path("/work/project"), "balanced")
        )

    def test_cancel_and_terminal_loss_return_no_mutation(self) -> None:
        for io_adapter in (
            ScriptedIO(cancel_at=2),
            ScriptedIO(disconnect_at=3),
        ):
            with self.subTest(adapter=type(io_adapter).__name__):
                result = StackWizard(
                    self.snapshot,
                    self.catalog,
                    self.accounts,
                    io_adapter,
                ).run(Path("/work/project"))
                self.assertFalse(result.save)
                self.assertEqual(result.stacks, self.snapshot.stacks)
                self.assertEqual(result.bindings, self.snapshot.bindings)

    def test_controller_picker_remains_in_step_two(self) -> None:
        io_adapter = ScriptedIO(
            choices=[
                "Create new",
                "openai",
                "gpt",
                "gpt-5.6-sol",
                "Automatic within provider",
            ],
            confirmations=[False],
            text=["fast"],
        )

        StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        controller_titles = [
            title
            for title in io_adapter.titles
            if "controller" in title.lower()
        ]
        self.assertTrue(controller_titles)
        self.assertTrue(
            all(
                title.startswith("Step 2/5")
                for title in controller_titles
            )
        )

    def test_back_navigates_stages_and_preserves_changed_draft(self) -> None:
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
                "Back to agents",
                "Continue to review",
                "Continue to save",
            ],
            confirmations=[True, False],
            text=["heavy"],
            back_titles=[
                "Step 2/5 · Controller",
                "Step 3/5 · Agents",
            ],
        )

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        self.assertTrue(result.save)
        self.assertFalse(result.assign_current_project)
        candidate = result.stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-6-thinking")
        self.assertEqual(candidate.providers, ("antigravity",))
        self.assertEqual(
            io_adapter.titles.count("Step 1/5 · Stack"), 2
        )
        self.assertEqual(
            io_adapter.titles.count("Step 2/5 · Controller"), 3
        )
        self.assertGreaterEqual(
            io_adapter.titles.count("Step 3/5 · Agents"), 3
        )
        self.assertEqual(
            io_adapter.titles.count("Step 4/5 · Review action"), 2
        )

    def test_back_from_initial_stack_stage_cancels(self) -> None:
        io_adapter = ScriptedIO(
            back_titles=["Step 1/5 · Stack"]
        )

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        self.assertFalse(result.save)
        self.assertEqual(result.stacks, self.snapshot.stacks)

    def test_rename_after_back_preserves_changed_draft(self) -> None:
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
                "Continue to review",
                "Continue to save",
            ],
            confirmations=[True, False],
            text=["heavy", "renamed-heavy"],
            back_title_occurrences={
                "Step 3/5 · Agents": [2],
                "Step 2/5 · Controller": [2],
            },
        )

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        candidate = result.stacks.stacks["renamed-heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-6-thinking")
        self.assertEqual(candidate.providers, ("antigravity",))
        self.assertEqual(
            result.bindings.candidate_accounts[candidate.id],
            "oc-a-cccccccccccccccc",
        )
        self.assertNotIn("heavy", result.stacks.stacks)

    def test_final_catalog_refresh_repicks_only_disappeared_candidate(self) -> None:
        refreshed = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.upstream != "claude-opus-4-6-thinking"
            ),
            unclassified=(),
        )
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
                "anthropic",
                "claude",
                "claude-opus-4-8",
                "Automatic within provider",
            ],
            confirmations=[True, True, False],
            text=["heavy"],
        )
        catalogues = iter((refreshed, refreshed))

        result = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
            refresh_catalog=lambda: next(catalogues),
        ).run(Path("/work/project"))

        self.assertTrue(result.save)
        candidate = result.stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-8")
        self.assertNotIn(candidate.id, result.bindings.candidate_accounts)
        self.assertIn(
            "availability changed for architecture-advisor candidate "
            "claude-opus-4-6-thinking",
            "\n".join(io_adapter.shown).lower(),
        )

    def test_review_shows_logical_alias_and_exact_provider_upstream(
        self,
    ) -> None:
        document = stack_wizard.serialize_model_stacks(self.stacks)
        definition = document["models"].pop("claude-opus-4-8")
        definition["routes"]["antigravity"] = (
            "claude-opus-4-6-thinking"
        )
        document["models"]["claude-opus"] = definition
        architecture = document["stacks"]["balanced"]["agents"][
            "architecture-advisor"
        ][0]
        architecture["model"] = "claude-opus"
        architecture["providers"] = ["anthropic", "antigravity"]
        architecture["id"] = stack_wizard.candidate_id(
            "balanced", "architecture-advisor", 0, "claude-opus"
        )
        planning = document["stacks"]["balanced"]["agents"][
            "planning-advisor"
        ][0]
        planning["model"] = "claude-opus"
        planning["providers"] = ["anthropic", "antigravity"]
        planning["id"] = stack_wizard.candidate_id(
            "balanced", "planning-advisor", 0, "claude-opus"
        )
        stacks = normalize_model_stacks(document)
        snapshot = StackSnapshot(stacks, StackBindings({}), "0" * 64, None)
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "Continue to review",
            ],
            confirmations=[False],
            text=["alias-review"],
        )

        StackWizard(
            snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        review = "\n".join(io_adapter.shown)
        self.assertIn("claude-opus", review)
        self.assertIn("anthropic/claude-opus-4-8", review)
        self.assertIn(
            "antigravity/claude-opus-4-6-thinking", review
        )
        self.assertNotIn("oc-a-", review)
        self.assertNotIn("oc-c-", review)
        self.assertNotIn("oc-r-", review)
        self.assertNotIn(".json", review)

    def test_edit_noop_preserves_native_candidate_ids_and_bindings(self) -> None:
        document = stack_wizard.serialize_model_stacks(self.stacks)
        candidates = [
            candidate
            for stack in document["stacks"].values()
            for choices in (
                stack["controller"],
                *stack["agents"].values(),
            )
            for candidate in choices
        ]
        for ordinal, candidate in enumerate(candidates, 1):
            candidate["id"] = f"oc-c-{ordinal + 100:016x}"
        stacks = normalize_model_stacks(document)
        architecture_id = stacks.stacks["balanced"].agents[
            "architecture-advisor"
        ][0].id
        snapshot = StackSnapshot(
            stacks,
            StackBindings(
                {architecture_id: "oc-a-bbbbbbbbbbbbbbbb"}
            ),
            "0" * 64,
            None,
        )
        io_adapter = ScriptedIO(
            choices=[
                "Edit existing",
                "balanced",
                "Continue to review",
                "Continue to save",
            ],
            confirmations=[True, False],
        )

        result = StackWizard(
            snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
        ).run(Path("/work/project"))

        before = {
            (scope, candidate.model): candidate.id
            for scope, choices in (
                ("controller", stacks.stacks["balanced"].controller),
                *stacks.stacks["balanced"].agents.items(),
            )
            for candidate in choices
        }
        after = {
            (scope, candidate.model): candidate.id
            for scope, choices in (
                (
                    "controller",
                    result.stacks.stacks["balanced"].controller,
                ),
                *result.stacks.stacks["balanced"].agents.items(),
            )
            for candidate in choices
        }
        self.assertEqual(after, before)
        self.assertEqual(
            dict(result.bindings.candidate_accounts),
            {architecture_id: "oc-a-bbbbbbbbbbbbbbbb"},
        )

    def test_edit_reorder_preserves_candidate_ids_and_local_bindings(
        self,
    ) -> None:
        document = stack_wizard.serialize_model_stacks(self.stacks)
        architecture = document["stacks"]["balanced"]["agents"][
            "architecture-advisor"
        ]
        architecture[0]["id"] = "oc-c-1111111111111111"
        architecture.append(
            {
                "id": "oc-c-2222222222222222",
                "model": "claude-sonnet-5",
                "providers": ["anthropic"],
            }
        )
        stacks = normalize_model_stacks(document)
        snapshot = StackSnapshot(
            stacks,
            StackBindings(
                {
                    "oc-c-1111111111111111": "oc-a-bbbbbbbbbbbbbbbb",
                    "oc-c-2222222222222222": "oc-a-bbbbbbbbbbbbbbbb",
                }
            ),
            "0" * 64,
            None,
        )
        wizard = StackWizard(
            snapshot, self.catalog, self.accounts, ScriptedIO()
        )
        draft = wizard._draft_from_stack(
            "Edit existing", "balanced", "balanced"
        )
        agents = dict(draft.agents)
        agents["architecture-advisor"] = tuple(
            reversed(agents["architecture-advisor"])
        )
        draft = replace(
            draft, agents=MappingProxyType(agents)
        )

        materialized, bindings = wizard._materialize(draft)

        self.assertEqual(
            [
                candidate.id
                for candidate in materialized.stacks[
                    "balanced"
                ].agents["architecture-advisor"]
            ],
            ["oc-c-2222222222222222", "oc-c-1111111111111111"],
        )
        self.assertEqual(
            dict(bindings.candidate_accounts),
            {
                "oc-c-1111111111111111": "oc-a-bbbbbbbbbbbbbbbb",
                "oc-c-2222222222222222": "oc-a-bbbbbbbbbbbbbbbb",
            },
        )

    def test_edit_changed_candidate_keeps_its_existing_identity(self) -> None:
        wizard = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            ScriptedIO(),
        )
        draft = wizard._draft_from_stack(
            "Edit existing", "balanced", "balanced"
        )
        original = draft.agents["architecture-advisor"][0]
        agents = dict(draft.agents)
        agents["architecture-advisor"] = (
            replace(original, model="claude-sonnet-5"),
        )
        draft = replace(draft, agents=MappingProxyType(agents))

        materialized, _ = wizard._materialize(draft)

        self.assertEqual(
            materialized.stacks["balanced"].agents[
                "architecture-advisor"
            ][0].id,
            self.stacks.stacks["balanced"].agents[
                "architecture-advisor"
            ][0].id,
        )

    def test_review_allows_internal_prefixes_in_display_values(self) -> None:
        document = stack_wizard.serialize_model_stacks(self.stacks)
        document["models"]["gpt-5.6-sol"]["routes"]["openai"] = (
            "oc-r-labs"
        )
        stacks = normalize_model_stacks(document)
        controller_id = stacks.stacks["balanced"].controller[0].id
        snapshot = StackSnapshot(
            stacks,
            StackBindings(
                {controller_id: "oc-a-aaaaaaaaaaaaaaaa"}
            ),
            "0" * 64,
            None,
        )
        accounts = (
            replace(self.accounts[0], name="oc-a-heavy"),
            *self.accounts[1:],
        )
        catalog = LiveCatalog(
            choices=(
                LiveModelChoice(
                    "gpt",
                    "openai",
                    "oc-r-labs",
                    ("oc-a-aaaaaaaaaaaaaaaa",),
                    ("oc-a-heavy",),
                ),
                *(
                    choice
                    for choice in self.catalog.choices
                    if choice.upstream != "gpt-5.6-sol"
                ),
            ),
            unclassified=(),
        )
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "Continue to review",
            ],
            confirmations=[False],
            text=["display-prefixes"],
        )

        StackWizard(
            snapshot, catalog, accounts, io_adapter
        ).run(Path("/work/project"))

        review = "\n".join(io_adapter.shown)
        self.assertIn("oc-a-heavy", review)
        self.assertIn("openai/oc-r-labs", review)

    def test_safe_delete_rejects_default_and_referenced_stack(self) -> None:
        for projects, expected in (
            ({"contexts": []}, "cannot delete the default stack"),
            (
                {
                    "contexts": [
                        {
                            "root": "/work",
                            "modelStack": "heavy",
                        }
                    ]
                },
                "stack is referenced by /work",
            ),
        ):
            stacks = self.snapshot.stacks
            if expected.startswith("stack is referenced"):
                document = {
                    "schemaVersion": 2,
                    "defaultStack": stacks.default_stack,
                    "models": {
                        name: {
                            "family": model.family,
                            "routes": dict(model.routes),
                        }
                        for name, model in stacks.models.items()
                    },
                    "stacks": {
                        "balanced": {
                            "controller": [
                                {
                                    "id": candidate.id,
                                    "model": candidate.model,
                                    "providers": list(candidate.providers),
                                }
                                for candidate in stacks.stacks[
                                    "balanced"
                                ].controller
                            ],
                            "agents": {
                                role: [
                                    {
                                        "id": candidate.id,
                                        "model": candidate.model,
                                        "providers": list(
                                            candidate.providers
                                        ),
                                    }
                                    for candidate in stacks.stacks[
                                        "balanced"
                                    ].agents[role]
                                ]
                                for role in stacks.stacks[
                                    "balanced"
                                ].agents
                            },
                        },
                        "heavy": {
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
                                        "model": candidates[0].model,
                                        "providers": list(
                                            candidates[0].providers
                                        ),
                                    }
                                ]
                                for index, (role, candidates) in enumerate(
                                    stacks.stacks[
                                        "balanced"
                                    ].agents.items(),
                                    2,
                                )
                            },
                        },
                    },
                }
                stacks = normalize_model_stacks(document)
            snapshot = StackSnapshot(stacks, StackBindings({}), "0" * 64, None)
            target = "balanced" if projects["contexts"] == [] else "heavy"
            io_adapter = ScriptedIO(
                choices=["Delete existing", target],
                confirmations=[True],
            )
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(RuntimeError, expected):
                    StackWizard(
                        snapshot,
                        self.catalog,
                        self.accounts,
                        io_adapter,
                        projects=projects,
                    ).run(Path("/work/project"))

    def test_cli_runner_cancel_has_no_persistence(self) -> None:
        paths = {"config": Path("/private/config"), "data": Path("/data")}
        config = ResolvedConfig(
            documents={
                "model-stacks": {},
                "projects": {"schemaVersion": 1, "contexts": []},
                "providers": {},
            },
            sources={},
        )
        result = WizardResult(
            self.snapshot.stacks,
            self.snapshot.bindings,
            "",
            False,
            False,
        )
        terminal = _TTYStringIO()
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard,
                "load_accounts",
                return_value=self.accounts,
            ),
            mock.patch.object(
                stack_wizard, "validate_account_bindings"
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=self.catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard,
                "run",
                return_value=result,
            ),
            mock.patch.object(stack_wizard, "save_stack") as save,
            mock.patch.object(
                stack_wizard, "load_control_plane"
            ) as reload_config,
            mock.patch.object(
                stack_wizard, "assign_stack_to_context"
            ) as assign,
        ):
            status = run_stack_wizard(
                paths, config, Path("/work/project")
            )

        self.assertEqual(status, 0)
        save.assert_not_called()
        reload_config.assert_not_called()
        assign.assert_not_called()
        self.assertIn("No changes saved", terminal.getvalue())

    def test_cli_runner_reloads_validates_saves_then_assigns(self) -> None:
        paths = {"config": Path("/private/config"), "data": Path("/data")}
        projects = {
            "schemaVersion": 1,
            "contexts": [
                {
                    "root": "/work/project",
                    "atlassian": None,
                    "modelStack": None,
                    "accountPools": ["shared"],
                }
            ],
        }
        config = ResolvedConfig(
            documents={
                "model-stacks": stack_wizard.serialize_model_stacks(
                    self.snapshot.stacks
                ),
                "projects": projects,
                "providers": {"providers": {}},
            },
            sources={},
        )
        result = WizardResult(
            self.snapshot.stacks,
            self.snapshot.bindings,
            "balanced",
            True,
            True,
        )
        terminal = _TTYStringIO()
        calls: list[str] = []
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard,
                "load_accounts",
                return_value=self.accounts,
            ),
            mock.patch.object(
                stack_wizard, "validate_account_bindings"
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=self.catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard,
                "run",
                return_value=result,
            ),
            mock.patch.object(
                stack_wizard,
                "control_plane_transaction",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                stack_wizard,
                "stack_binding_transaction",
                return_value=contextlib.nullcontext(),
            ),
            mock.patch.object(
                stack_wizard,
                "validate_control_plane",
                side_effect=lambda *_args: calls.append(
                    "validate-control-plane"
                ),
            ),
            mock.patch.object(
                stack_wizard,
                "save_stack",
                side_effect=lambda *_args: calls.append("save"),
            ) as save,
            mock.patch.object(
                stack_wizard,
                "load_control_plane",
                side_effect=lambda *_args: (
                    calls.append("reload") or config
                ),
            ) as reload_config,
            mock.patch.object(
                stack_wizard,
                "resolve_control_plane_context",
                return_value={
                    "route": {
                        "contextRootReal": "/work/project",
                    }
                },
            ),
            mock.patch.object(
                stack_wizard,
                "validate_stack_assignment",
                side_effect=lambda *_args: calls.append("validate"),
            ) as validate,
            mock.patch.object(
                stack_wizard,
                "assign_stack_to_context",
                side_effect=lambda *_args: (
                    calls.append("assign") or Path("/work/project")
                ),
            ) as assign,
        ):
            status = run_stack_wizard(
                paths, config, Path("/work/project")
            )

        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [
                "reload",
                "validate-control-plane",
                "validate",
                "save",
                "assign",
            ],
        )
        save.assert_called_once_with(
            self.snapshot, result.stacks, result.bindings
        )
        reload_config.assert_called_once()
        validate.assert_called_once()
        assign.assert_called_once()
        output = terminal.getvalue()
        self.assertIn("Saved stack balanced", output)
        self.assertIn("/work/project", output)
        self.assertNotIn("oc-a-", output)
        self.assertNotIn("oc-c-", output)
        self.assertNotIn("oc-r-", output)
        self.assertNotIn(".json", output)

    def _config_root(
        self,
        stacks,
        *,
        projects: dict[str, object] | None = None,
    ) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "config"
        shutil.copytree(REPO_ROOT / "config", root)
        root = root.resolve()
        root.chmod(0o700)
        (root / "model-stacks.json").write_text(
            json.dumps(
                stack_wizard.serialize_model_stacks(stacks),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if projects is not None:
            (root / "projects.json").write_text(
                json.dumps(projects, indent=2) + "\n",
                encoding="utf-8",
            )
        return root

    def _created_heavy_result(
        self,
        snapshot: StackSnapshot | None = None,
    ) -> WizardResult:
        snapshot = self.snapshot if snapshot is None else snapshot
        return StackWizard(
            snapshot,
            self.catalog,
            self.accounts,
            ScriptedIO(
                choices=[
                    "Clone existing",
                    "balanced",
                    "antigravity",
                    "claude",
                    "claude-opus-4-6-thinking",
                    "Personal Antigravity",
                ],
                confirmations=[True, False],
                text=["heavy"],
            ),
        ).run(Path("/work/project"))

    def _with_offline_stack(
        self, stacks: NormalizedStacks
    ) -> NormalizedStacks:
        document = stack_wizard.serialize_model_stacks(stacks)
        document["models"]["claude-offline"] = {
            "family": "claude",
            "routes": {"anthropic": "claude-offline"},
        }
        offline = json.loads(
            json.dumps(document["stacks"]["balanced"])
        )
        offline["controller"] = [
            {
                "id": stack_wizard.candidate_id(
                    "offline", "controller", 0, "claude-offline"
                ),
                "model": "claude-offline",
                "providers": ["anthropic"],
            }
        ]
        for role, candidates in offline["agents"].items():
            for ordinal, candidate in enumerate(candidates):
                candidate["id"] = stack_wizard.candidate_id(
                    "offline",
                    role,
                    ordinal,
                    candidate["model"],
                )
        document["stacks"]["offline"] = offline
        return normalize_model_stacks(document)

    def _run_mocked_commit(
        self,
        config_root: Path,
        initial: ResolvedConfig,
        snapshot: StackSnapshot,
        result: WizardResult,
        catalog: LiveCatalog,
    ) -> tuple[int, mock.Mock]:
        terminal = _TTYStringIO()
        save = mock.Mock()
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard, "load_accounts", return_value=self.accounts
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard,
                "run",
                return_value=result,
            ),
            mock.patch.object(stack_wizard, "save_stack", save),
        ):
            status = run_stack_wizard(
                {"config": config_root, "data": config_root.parent},
                initial,
                Path("/work/project"),
            )
        return status, save

    def _run_while_control_plane_writer_holds_lock(
        self,
        config_root: Path,
        initial: ResolvedConfig,
        result: WizardResult,
        mutate: Callable[[], None],
    ) -> tuple[list[BaseException], mock.Mock]:
        writer_entered = threading.Event()
        commit_attempted = threading.Event()
        release_writer = threading.Event()
        runner_done = threading.Event()
        failures: list[BaseException] = []
        real_transaction = stack_wizard.control_plane_transaction
        save = mock.Mock()

        @contextlib.contextmanager
        def observed_transaction(path: Path):
            commit_attempted.set()
            with real_transaction(path):
                yield

        def writer() -> None:
            try:
                with real_transaction(config_root):
                    mutate()
                    writer_entered.set()
                    if not release_writer.wait(2):
                        raise AssertionError(
                            "test did not release control-plane writer"
                        )
            except BaseException as error:
                failures.append(error)

        def runner() -> None:
            try:
                run_stack_wizard(
                    {"config": config_root, "data": config_root.parent},
                    initial,
                    Path("/work/project"),
                )
            except BaseException as error:
                failures.append(error)
            finally:
                runner_done.set()

        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        self.assertTrue(writer_entered.wait(2))
        terminal = _TTYStringIO()
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard, "load_accounts", return_value=self.accounts
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value={"object": "list", "data": []},
            ),
            mock.patch.object(
                stack_wizard,
                "project_live_catalog",
                return_value=self.catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard, "run", return_value=result
            ),
            mock.patch.object(
                stack_wizard,
                "control_plane_transaction",
                side_effect=observed_transaction,
            ),
            mock.patch.object(stack_wizard, "save_stack", save),
        ):
            runner_thread = threading.Thread(target=runner)
            runner_thread.start()
            self.assertTrue(commit_attempted.wait(2))
            self.assertFalse(runner_done.is_set())
            release_writer.set()
            runner_thread.join(2)
            writer_thread.join(2)
            self.assertFalse(runner_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())
        return failures, save

    def test_concurrent_provider_change_is_reloaded_before_save(self) -> None:
        result = self._created_heavy_result()
        config_root = self._config_root(self.snapshot.stacks)
        initial = stack_wizard.load_control_plane(
            stack_wizard.default_config_paths(config_root)
        )

        def remove_antigravity_claude_route() -> None:
            path = config_root / "providers.json"
            providers = json.loads(path.read_text(encoding="utf-8"))
            antigravity = providers["providers"]["antigravity"]
            antigravity["families"] = ["google"]
            del antigravity["familyPrefixes"]["claude"]
            providers["fallbackRoutes"]["claude"] = ["anthropic"]
            path.write_text(
                json.dumps(providers, indent=2) + "\n",
                encoding="utf-8",
            )

        failures, save = (
            self._run_while_control_plane_writer_holds_lock(
                config_root,
                initial,
                result,
                remove_antigravity_claude_route,
            )
        )

        self.assertEqual(len(failures), 1)
        self.assertRegex(
            str(failures[0]), "does not support model family claude"
        )
        save.assert_not_called()

    def test_concurrent_project_reference_blocks_stack_deletion(self) -> None:
        created = self._created_heavy_result()
        config_root = self._config_root(created.stacks)
        initial = stack_wizard.load_control_plane(
            stack_wizard.default_config_paths(config_root)
        )
        current_snapshot = StackSnapshot(
            created.stacks,
            created.bindings,
            "1" * 64,
            None,
        )
        deleted_stacks, deleted_bindings = stack_wizard.delete_stack(
            current_snapshot,
            "heavy",
            initial.documents["projects"],
        )
        result = WizardResult(
            deleted_stacks,
            deleted_bindings,
            "heavy",
            True,
            False,
        )

        def reference_heavy() -> None:
            projects = {
                "schemaVersion": 1,
                "contexts": [
                    {
                        "root": "/work/project",
                        "atlassian": None,
                        "modelStack": "heavy",
                        "accountPools": ["shared"],
                    }
                ],
            }
            (config_root / "projects.json").write_text(
                json.dumps(projects, indent=2) + "\n",
                encoding="utf-8",
            )

        failures, save = (
            self._run_while_control_plane_writer_holds_lock(
                config_root,
                initial,
                result,
                reference_heavy,
            )
        )

        self.assertEqual(len(failures), 1)
        self.assertRegex(str(failures[0]), "unknown model stack")
        save.assert_not_called()

    def test_concurrent_account_removal_is_reloaded_before_save(
        self,
    ) -> None:
        config_root = self._config_root(self.snapshot.stacks)
        initial = stack_wizard.load_control_plane(
            stack_wizard.default_config_paths(config_root)
        )
        registry = config_root / "accounts.json"
        update_accounts(registry, lambda _accounts: self.accounts)
        raw_catalog = {
            "object": "list",
            "data": [
                {
                    "id": (
                        f"{account.routing_prefix}/{choice.upstream}"
                    )
                }
                for account in self.accounts
                for choice in self.catalog.choices
                if choice.provider == account.provider
            ],
        }
        result = WizardResult(
            self.snapshot.stacks,
            self.snapshot.bindings,
            "balanced",
            True,
            False,
        )
        confirmed = threading.Event()
        writer_entered = threading.Event()
        binding_attempted = threading.Event()
        release_writer = threading.Event()
        runner_done = threading.Event()
        failures: list[BaseException] = []
        save = mock.Mock()
        real_binding_transaction = (
            stack_wizard.stack_binding_transaction
        )

        def remove_openai_account() -> None:
            try:
                if not confirmed.wait(2):
                    raise AssertionError(
                        "wizard did not reach confirmation"
                    )
                with real_binding_transaction(
                    config_root / "stack-bindings.json"
                ):
                    update_accounts(
                        registry,
                        lambda accounts: tuple(
                            account
                            for account in accounts
                            if account.provider != "openai"
                        ),
                    )
                    writer_entered.set()
                    if not release_writer.wait(2):
                        raise AssertionError(
                            "test did not release account writer"
                        )
            except BaseException as error:
                failures.append(error)

        def confirmed_result(*_args) -> WizardResult:
            confirmed.set()
            if not writer_entered.wait(2):
                raise AssertionError(
                    "account writer did not acquire binding lock"
                )
            return result

        @contextlib.contextmanager
        def observed_binding_transaction(path: Path):
            binding_attempted.set()
            with real_binding_transaction(path) as transaction:
                yield transaction

        def runner() -> None:
            try:
                run_stack_wizard(
                    {"config": config_root, "data": config_root.parent},
                    initial,
                    Path("/work/project"),
                )
            except BaseException as error:
                failures.append(error)
            finally:
                runner_done.set()

        writer_thread = threading.Thread(target=remove_openai_account)
        writer_thread.start()
        terminal = _TTYStringIO()
        with (
            mock.patch.object(stack_wizard.sys, "stdin", terminal),
            mock.patch.object(stack_wizard.sys, "stdout", terminal),
            mock.patch.object(
                stack_wizard, "_runtime_catalog_port", return_value=8317
            ),
            mock.patch.object(
                stack_wizard,
                "fetch_live_catalog",
                return_value=raw_catalog,
            ),
            mock.patch.object(
                stack_wizard,
                "load_stack_snapshot",
                return_value=self.snapshot,
            ),
            mock.patch.object(
                stack_wizard.StackWizard,
                "run",
                side_effect=confirmed_result,
            ),
            mock.patch.object(
                stack_wizard,
                "stack_binding_transaction",
                side_effect=observed_binding_transaction,
            ),
            mock.patch.object(stack_wizard, "save_stack", save),
        ):
            runner_thread = threading.Thread(target=runner)
            runner_thread.start()
            self.assertTrue(binding_attempted.wait(2))
            self.assertFalse(runner_done.is_set())
            release_writer.set()
            runner_thread.join(2)
            writer_thread.join(2)
            self.assertFalse(runner_thread.is_alive())
            self.assertFalse(writer_thread.is_alive())

        self.assertEqual(len(failures), 1)
        self.assertRegex(
            str(failures[0]),
            "gpt-5.6-sol is not live through openai",
        )
        save.assert_not_called()

    def test_unrelated_offline_stack_does_not_block_target_create_or_edit(
        self,
    ) -> None:
        current_stacks = self._with_offline_stack(self.snapshot.stacks)
        snapshot = StackSnapshot(
            current_stacks,
            self.snapshot.bindings,
            "2" * 64,
            None,
        )
        created = self._created_heavy_result(snapshot)
        edited = StackWizard(
            snapshot,
            self.catalog,
            self.accounts,
            ScriptedIO(
                choices=[
                    "Edit existing",
                    "balanced",
                    "antigravity",
                    "claude",
                    "claude-opus-4-6-thinking",
                    "Personal Antigravity",
                ],
                confirmations=[True, False],
            ),
        ).run(Path("/work/project"))

        for operation, result in (
            ("create", created),
            ("edit", edited),
        ):
            with self.subTest(operation=operation):
                config_root = self._config_root(current_stacks)
                initial = stack_wizard.load_control_plane(
                    stack_wizard.default_config_paths(config_root)
                )

                status, save = self._run_mocked_commit(
                    config_root,
                    initial,
                    snapshot,
                    result,
                    self.catalog,
                )

                self.assertEqual(status, 0)
                save.assert_called_once_with(
                    snapshot, result.stacks, result.bindings
                )

    def test_delete_ignores_target_and_unrelated_offline_routes(
        self,
    ) -> None:
        offline_stacks = self._with_offline_stack(
            self.snapshot.stacks
        )
        offline_snapshot = StackSnapshot(
            offline_stacks,
            self.snapshot.bindings,
            "3" * 64,
            None,
        )
        created = self._created_heavy_result(offline_snapshot)
        current = StackSnapshot(
            created.stacks,
            created.bindings,
            "4" * 64,
            None,
        )

        for target in ("offline", "heavy"):
            with self.subTest(target=target):
                updated, bindings = stack_wizard.delete_stack(
                    current,
                    target,
                    {"schemaVersion": 1, "contexts": []},
                )
                result = WizardResult(
                    updated,
                    bindings,
                    target,
                    True,
                    False,
                )
                config_root = self._config_root(current.stacks)
                initial = stack_wizard.load_control_plane(
                    stack_wizard.default_config_paths(config_root)
                )

                status, save = self._run_mocked_commit(
                    config_root,
                    initial,
                    current,
                    result,
                    self.catalog,
                )

                self.assertEqual(status, 0)
                save.assert_called_once_with(
                    current, result.stacks, result.bindings
                )

    def test_locked_target_disappearance_resumes_picker_then_saves(
        self,
    ) -> None:
        config_root = self._config_root(self.snapshot.stacks)
        initial = stack_wizard.load_control_plane(
            stack_wizard.default_config_paths(config_root)
        )
        refreshed = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider != "antigravity"
            ),
            unclassified=(),
        )
        current_catalog = [self.catalog]
        confirmed = threading.Event()
        writer_entered = threading.Event()
        commit_attempted = threading.Event()
        release_writer = threading.Event()
        commit_lock_active = threading.Event()
        failures: list[BaseException] = []
        failure_details: list[str] = []
        real_control_transaction = (
            stack_wizard.control_plane_transaction
        )
        save = mock.Mock()

        class RetryIO(ScriptedIO):
            def choose(
                inner_self,
                title: str,
                options: list[Choice],
                selected: int = 0,
                searchable: bool = False,
            ) -> int:
                if (
                    title
                    == "Step 3/5 · architecture-advisor · Provider"
                    and inner_self.title_calls.get(title, 0) == 1
                ):
                    if commit_lock_active.is_set():
                        raise AssertionError(
                            "picker resumed before locks were released"
                        )
                return super().choose(
                    title,
                    options,
                    selected=selected,
                    searchable=searchable,
                )

        io_adapter = RetryIO(
            choices=[
                "Clone existing",
                "balanced",
                "Configure controller",
                "openai",
                "gpt",
                "gpt-5.6-terra",
                "Automatic within provider",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
                "anthropic",
                "claude",
                "claude-opus-4-8",
                "Automatic within provider",
            ],
            confirmations=[True, True],
            text=["heavy"],
        )
        original_run = stack_wizard.StackWizard.run

        def run_then_block(
            wizard: StackWizard, launch_dir: Path
        ) -> WizardResult:
            result = original_run(wizard, launch_dir)
            confirmed.set()
            if not writer_entered.wait(2):
                raise AssertionError(
                    "catalog writer did not acquire control lock"
                )
            return result

        def change_live_catalog() -> None:
            try:
                if not confirmed.wait(2):
                    raise AssertionError(
                        "wizard did not finish confirmation"
                    )
                with real_control_transaction(config_root):
                    current_catalog[0] = refreshed
                    writer_entered.set()
                    if not release_writer.wait(2):
                        raise AssertionError(
                            "test did not release catalog writer"
                        )
            except BaseException as error:
                failures.append(error)
                failure_details.append(traceback.format_exc())

        @contextlib.contextmanager
        def observed_control_transaction(path: Path):
            commit_attempted.set()
            with real_control_transaction(path):
                commit_lock_active.set()
                try:
                    yield
                finally:
                    commit_lock_active.clear()

        def configure() -> None:
            try:
                run_stack_wizard(
                    {
                        "config": config_root,
                        "data": config_root.parent,
                    },
                    initial,
                    config_root.parent,
                )
            except BaseException as error:
                failures.append(error)
                failure_details.append(traceback.format_exc())

        terminal = _TTYStringIO()
        writer_thread = threading.Thread(target=change_live_catalog)
        writer_thread.start()
        try:
            with (
                mock.patch.object(stack_wizard.sys, "stdin", terminal),
                mock.patch.object(stack_wizard.sys, "stdout", terminal),
                mock.patch.object(
                    stack_wizard,
                    "_runtime_catalog_port",
                    return_value=8317,
                ),
                mock.patch.object(
                    stack_wizard,
                    "load_accounts",
                    return_value=self.accounts,
                ),
                mock.patch.object(
                    stack_wizard,
                    "fetch_live_catalog",
                    side_effect=lambda *_args, **_kwargs: current_catalog[0],
                ),
                mock.patch.object(
                    stack_wizard,
                    "project_live_catalog",
                    side_effect=lambda raw, *_args: raw,
                ),
                mock.patch.object(
                    stack_wizard,
                    "load_stack_snapshot",
                    return_value=self.snapshot,
                ),
                mock.patch.object(
                    stack_wizard,
                    "TerminalWizardIO",
                    return_value=io_adapter,
                ),
                mock.patch.object(
                    stack_wizard.StackWizard,
                    "run",
                    autospec=True,
                    side_effect=run_then_block,
                ),
                mock.patch.object(
                    stack_wizard,
                    "control_plane_transaction",
                    side_effect=observed_control_transaction,
                ),
                mock.patch.object(stack_wizard, "save_stack", save),
            ):
                runner_thread = threading.Thread(target=configure)
                runner_thread.start()
                self.assertTrue(commit_attempted.wait(2))
                release_writer.set()
                runner_thread.join(2)
                self.assertFalse(runner_thread.is_alive())
        except BaseException as error:
            failures.append(error)
            failure_details.append(traceback.format_exc())
        finally:
            release_writer.set()
            writer_thread.join(2)

        self.assertEqual(failures, [], "\n".join(failure_details))
        save.assert_called_once()
        saved_stacks = save.call_args.args[1]
        candidate = saved_stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        controller = saved_stacks.stacks["heavy"].controller[0]
        self.assertEqual(controller.model, "gpt-5.6-terra")
        self.assertEqual(controller.providers, ("openai",))
        self.assertEqual(candidate.model, "claude-opus-4-8")
        self.assertEqual(candidate.providers, ("anthropic",))
        self.assertIn(
            "availability changed for architecture-advisor candidate",
            "\n".join(io_adapter.shown).lower(),
        )

    def test_resumed_picker_defers_refresh_to_locked_commit(self) -> None:
        refreshed = LiveCatalog(
            choices=tuple(
                choice
                for choice in self.catalog.choices
                if choice.provider != "antigravity"
            ),
            unclassified=(),
        )
        io_adapter = ScriptedIO(
            choices=[
                "Clone existing",
                "balanced",
                "antigravity",
                "claude",
                "claude-opus-4-6-thinking",
                "Personal Antigravity",
                "anthropic",
                "claude",
                "claude-opus-4-8",
                "Automatic within provider",
            ],
            confirmations=[True, False, True],
            text=["heavy"],
        )
        wizard = StackWizard(
            self.snapshot,
            self.catalog,
            self.accounts,
            io_adapter,
            refresh_catalog=lambda: self.catalog,
        )
        initial = wizard.run(Path("/work/project"))
        stale_refresh_calls = 0

        def stale_refresh() -> LiveCatalog:
            nonlocal stale_refresh_calls
            stale_refresh_calls += 1
            raise AssertionError("startup refresh context was reused")

        wizard._refresh_catalog = stale_refresh
        missing = wizard.missing_live_choice(initial, refreshed)
        self.assertIsNotNone(missing)

        result = wizard.resume_missing(
            missing,
            refreshed,
            self._config_root(self.snapshot.stacks).parent,
            {"schemaVersion": 1, "contexts": []},
        )

        self.assertTrue(result.save)
        self.assertEqual(stale_refresh_calls, 0)
        candidate = result.stacks.stacks["heavy"].agents[
            "architecture-advisor"
        ][0]
        self.assertEqual(candidate.model, "claude-opus-4-8")
        self.assertEqual(candidate.providers, ("anthropic",))


class _TTYStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class TerminalWizardIOTests(unittest.TestCase):
    def _pty_choose(
        self,
        payload: bytes,
        *,
        options: list[Choice] | None = None,
        environment: dict[str, str] | None = None,
        width: int = 80,
    ) -> tuple[int, str]:
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        fcntl.ioctl(
            slave,
            termios.TIOCSWINSZ,
            struct.pack("HHHH", 24, width, 0, 0),
        )
        reader = os.fdopen(os.dup(slave), "r", encoding="utf-8", newline="")
        writer = os.fdopen(
            os.dup(slave),
            "w",
            encoding="utf-8",
            newline="",
            buffering=1,
        )
        os.close(slave)
        self.addCleanup(reader.close)
        self.addCleanup(writer.close)
        adapter = TerminalWizardIO(
            stdin=reader,
            stdout=writer,
            environment=environment or {"TERM": "xterm-256color"},
        )
        primed = bytearray()
        failure: list[BaseException] = []
        finished = threading.Event()

        def feed_after_render() -> None:
            try:
                readable, _, _ = __import__("select").select(
                    [master], [], [], 2
                )
                if not readable:
                    raise AssertionError("terminal adapter did not render")
                primed.extend(os.read(master, 65536))
                os.write(master, payload)
                while not finished.is_set():
                    readable, _, _ = __import__("select").select(
                        [master], [], [], 0.05
                    )
                    if readable:
                        primed.extend(os.read(master, 65536))
            except BaseException as error:
                failure.append(error)

        feeder = threading.Thread(target=feed_after_render)
        feeder.start()
        try:
            selected = adapter.choose(
                "Step 2/5 · Model",
                options
                or [
                    Choice("first"),
                    Choice("second", marker="inherited"),
                    Choice("needle model"),
                ],
                searchable=True,
            )
        finally:
            finished.set()
        feeder.join()
        if failure:
            raise failure[0]
        output = bytearray(primed)
        while True:
            readable, _, _ = __import__("select").select([master], [], [], 0)
            if not readable:
                break
            chunk = os.read(master, 65536)
            if not chunk:
                break
            output.extend(chunk)
        return selected, output.decode("utf-8", errors="replace")

    def test_raw_adapter_supports_arrows_numbers_search_and_back(self) -> None:
        selected, _ = self._pty_choose(b"\x1b[B\r")
        self.assertEqual(selected, 1)
        selected, _ = self._pty_choose(b"3")
        self.assertEqual(selected, 2)
        selected, _ = self._pty_choose(b"/needle\r")
        self.assertEqual(selected, 2)
        selected, _ = self._pty_choose(b"\x1b")
        self.assertEqual(selected, BACK)

    def test_raw_adapter_empty_search_ignores_enter_until_back(self) -> None:
        selected, output = self._pty_choose(b"/missing\r\x1b")

        self.assertEqual(selected, BACK)
        self.assertIn("No matches", output)

    def test_raw_adapter_ctrl_c_cancels_without_waiting(self) -> None:
        with self.assertRaises(WizardCancelled):
            self._pty_choose(b"\x03")

    def test_raw_renderer_returns_to_left_margin_for_each_line(self) -> None:
        selected, output = self._pty_choose(
            b"\r",
            options=[Choice("first"), Choice("second")],
        )

        self.assertEqual(selected, 0)
        self.assertIn(
            "\x1b[HStep 2/5 · Model\r\n"
            "> 1) first\r\n"
            "  2) second\r\n",
            output,
        )

    def test_no_color_narrow_render_has_progress_and_full_review_model(
        self,
    ) -> None:
        selected, output = self._pty_choose(
            b"\r",
            options=[
                Choice(
                    "very-long-provider-label",
                    detail="claude-opus-4-6-thinking",
                    marker="current",
                )
            ],
            environment={"TERM": "xterm-256color", "NO_COLOR": "1"},
            width=28,
        )
        self.assertEqual(selected, 0)
        self.assertIn("Step 2/5", output)
        self.assertIn("current", output)
        self.assertNotIn("claude-opus-4-6-thinking", output)
        self.assertNotRegex(output, r"\x1b\[[0-9;]*m")

        visible = io.StringIO()
        adapter = TerminalWizardIO(
            stdin=io.StringIO(),
            stdout=visible,
            environment={"TERM": "dumb", "NO_COLOR": "1"},
        )
        adapter.show(
            "Step 4/5 · Review\n"
            "architecture-advisor: claude-opus-4-6-thinking"
        )
        self.assertIn("claude-opus-4-6-thinking", visible.getvalue())

    def test_numbered_line_fallback_supports_default_and_search(self) -> None:
        visible = io.StringIO()
        adapter = TerminalWizardIO(
            stdin=io.StringIO("/needle\n1\n"),
            stdout=visible,
            environment={"TERM": "dumb"},
        )
        selected = adapter.choose(
            "Step 3/5 · Agents",
            [Choice("first"), Choice("Needle model")],
            selected=0,
            searchable=True,
        )
        self.assertEqual(selected, 1)
        self.assertIn("1)", visible.getvalue())


if __name__ == "__main__":
    unittest.main()
