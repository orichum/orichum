#!/usr/bin/env python3
from __future__ import annotations

import unittest
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest import mock

from integrations.common.configure_state import ConfigurationDraft
from integrations.common.configure_wizard import ConfigureServices, run_configure
from integrations.common.model_routing import RoutingError
from integrations.common.stack_bindings import StackBindings
from integrations.common.stack_catalog import LiveCatalog, LiveModelChoice
from integrations.common.terminal_ui import BACK, Choice
from tests.test_configure_state import _snapshot, _snapshot_with_alternate_profile


class ScriptedUI:
    def __init__(
        self,
        choices: Sequence[str],
        *,
        text_values: Sequence[str] = (),
    ) -> None:
        self.choices = list(choices)
        self.text_values = list(text_values)
        self.choice_titles: list[str] = []
        self.choice_labels: list[tuple[str, ...]] = []
        self.choice_details: list[tuple[str, ...]] = []
        self.text_prompts: list[str] = []
        self.sections: list[tuple[str, tuple[tuple[str, str], ...]]] = []
        self.shown: list[str] = []

    def choose(
        self,
        title: str,
        options: Sequence[Choice],
        selected: int = 0,
        searchable: bool = False,
    ) -> int:
        del searchable
        labels = tuple(option.label for option in options)
        self.choice_titles.append(title)
        self.choice_labels.append(labels)
        self.choice_details.append(tuple(option.detail for option in options))
        if self.choices:
            wanted = self.choices.pop(0)
            if wanted == "Back" and wanted not in labels:
                return BACK
            return labels.index(wanted)
        return selected

    def confirm(self, prompt: str, default: bool = False) -> bool:
        del prompt
        return default

    def text(self, prompt: str, initial: str = "") -> str:
        self.text_prompts.append(prompt)
        if self.text_values:
            return self.text_values.pop(0)
        return initial

    def show(self, text: str) -> None:
        self.shown.append(text)

    def section(
        self,
        title: str,
        rows: Sequence[tuple[str, str]] = (),
    ) -> None:
        self.sections.append((title, tuple(rows)))


class ConfigureWizardTests(unittest.TestCase):
    def services(self) -> ConfigureServices:
        snapshot = _snapshot()
        return ConfigureServices(
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
            prepare_account=lambda provider: None,
            apply_draft=lambda snapshot, draft: None,
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )

    def test_top_level_menu_shows_four_clear_tasks_and_exit(self) -> None:
        io = ScriptedUI(["Exit"])

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=self.services(),
        )

        self.assertEqual(status, 0)
        self.assertEqual(io.choice_titles[0], "What do you want to change?")
        self.assertEqual(
            io.choice_labels[0],
            (
                "Models",
                "Accounts",
                "Check configuration",
                "Advanced settings",
                "Exit",
            ),
        )
        dashboard = next(
            rows for title, rows in io.sections if title == "Orichum configuration"
        )
        self.assertEqual(
            tuple(label for label, _value in dashboard),
            ("Project", "Profile", "Controller", "Accounts", "Changes"),
        )
        self.assertEqual(dict(dashboard)["Changes"], "None")
        self.assertIn("verify routes", io.choice_details[0][2])

    def test_project_configuration_is_shown_as_authoritative_and_read_only(
        self,
    ) -> None:
        source = Path("/work/acme/.orichum/config.json")
        snapshot = replace(
            _snapshot(),
            project_models_path=source,
            project_models_digest="a" * 64,
            project_models_checked=True,
            project_services_managed=True,
            jira_profile="work",
            github_account="alupao",
        )
        services = replace(
            self.services(),
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
        )
        io = ScriptedUI(["Models", "Advanced settings", "Exit"])

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(status, 0)
        dashboard = next(
            rows for title, rows in io.sections if title == "Orichum configuration"
        )
        self.assertEqual(dict(dashboard)["Profile"], "Project file")
        project_files = [
            rows for title, rows in io.sections if title == "Project configuration"
        ]
        self.assertEqual(len(project_files), 2)
        for project_file in project_files:
            self.assertEqual(dict(project_file)["File"], str(source))
            self.assertEqual(dict(project_file)["Jira profile"], "work")
            self.assertEqual(dict(project_file)["GitHub account"], "alupao")
        self.assertFalse(
            any("Recommended setup" in labels for labels in io.choice_labels)
        )

    def test_review_lists_every_concrete_role_and_session_effect(self) -> None:
        io = ScriptedUI(
            [
                "Models",
                "One model everywhere",
                "gpt-5.6-sol",
                "Review and apply changes",
                "Discard and exit",
            ]
        )

        run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=self.services(),
        )

        models = next(rows for title, rows in io.sections if title == "Models")
        self.assertEqual(len(models), 6)
        self.assertIn(
            "Changes apply to new sessions. Existing sessions are unchanged.",
            io.shown,
        )

    def test_check_can_repair_an_unready_project_once(self) -> None:
        verified: list[Path] = []
        reconciled: list[bool] = []

        def verify(project: Path) -> None:
            verified.append(project)
            if len(verified) == 1:
                raise RoutingError("configured project is not ready")

        services = replace(
            self.services(),
            reconcile=lambda verbose: reconciled.append(verbose) or 0,
            verify_project=verify,
        )
        io = ScriptedUI(["Check configuration", "Repair local services"])

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(status, 0)
        self.assertEqual(reconciled, [False])
        self.assertEqual(verified, [Path("/work/acme"), Path("/work/acme")])
        self.assertIn("Configuration repaired. Orichum is ready.", io.shown)

    def test_leaving_without_changes_does_not_apply_or_reconcile(self) -> None:
        applied: list[ConfigurationDraft] = []
        reconciled: list[bool] = []
        services = self.services()
        services = ConfigureServices(
            load_snapshot=services.load_snapshot,
            refresh_snapshot=services.refresh_snapshot,
            prepare_account=services.prepare_account,
            apply_draft=lambda snapshot, draft: applied.append(draft),
            reconcile=lambda verbose: reconciled.append(verbose) or 0,
            verify_project=services.verify_project,
        )

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=ScriptedUI(["Exit"]),
            services=services,
        )

        self.assertEqual(status, 0)
        self.assertEqual(applied, [])
        self.assertEqual(reconciled, [])

    def test_exit_with_pending_changes_requires_an_explicit_choice(self) -> None:
        applied: list[ConfigurationDraft] = []
        services = replace(
            self.services(),
            apply_draft=lambda snapshot, draft: applied.append(draft),
        )
        io = ScriptedUI(
            [
                "Models",
                "Recommended setup",
                "Exit",
                "Keep editing",
                "Review and apply changes",
                "Discard and exit",
            ]
        )

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(status, 0)
        self.assertEqual(applied, [])
        self.assertIn(
            (
                "Review and apply changes",
                "Discard and exit",
                "Keep editing",
            ),
            io.choice_labels,
        )

    def test_one_model_for_everything_uses_one_live_numbered_choice(self) -> None:
        applied: list[ConfigurationDraft] = []
        services = self.services()
        services = ConfigureServices(
            load_snapshot=services.load_snapshot,
            refresh_snapshot=services.refresh_snapshot,
            prepare_account=services.prepare_account,
            apply_draft=lambda snapshot, draft: applied.append(draft),
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )
        io = ScriptedUI(
            [
                "Models",
                "One model everywhere",
                "gpt-5.6-sol",
                "Review and apply changes",
                "Apply changes",
            ]
        )

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(status, 0)
        self.assertEqual(len(applied), 1)
        self.assertEqual(
            {selection.model for selection in applied[0].role_models.values()},
            {"gpt-5.6-sol"},
        )
        self.assertFalse(
            any("model" in prompt.casefold() for prompt in io.text_prompts)
        )

    def test_backup_flow_fixes_provider_and_derives_hidden_priority(self) -> None:
        applied: list[ConfigurationDraft] = []
        prepared: list[str] = []
        services = self.services()
        services = ConfigureServices(
            load_snapshot=services.load_snapshot,
            refresh_snapshot=services.refresh_snapshot,
            prepare_account=lambda provider: (
                prepared.append(provider)
                or SimpleNamespace(
                    provider=provider,
                    credential_ref="secret-credential.json",
                    suggested_name="Openai account",
                )
            ),
            apply_draft=lambda snapshot, draft: applied.append(draft),
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )
        io = ScriptedUI(
            [
                "Accounts",
                "Add a backup account",
                "OpenAI primary",
                "Review and apply changes",
                "Apply changes",
            ],
            text_values=["OpenAI new backup"],
        )

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(status, 0)
        self.assertEqual(prepared, ["openai"])
        self.assertNotIn("Claude work", io.choice_labels[2])
        pending = applied[0].pending_accounts[0]
        self.assertEqual(pending.provider, "openai")
        self.assertEqual(pending.intent, "backup")
        self.assertEqual(pending.priority, 50)
        self.assertEqual(pending.primary_name, "OpenAI primary")
        self.assertNotIn("secret-credential.json", repr(applied[0]))
        self.assertEqual(io.text_prompts, ["Account name"])
        self.assertIn(
            "Authentication is saved securely and can be reused if you cancel.",
            io.shown,
        )

    def test_accounts_menu_only_shows_guided_actions_and_clear_handoff(self) -> None:
        io = ScriptedUI(["Accounts", "Manage existing accounts", "Exit"])

        run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=self.services(),
        )

        self.assertEqual(
            io.choice_labels[1],
            (
                "Add an account",
                "Add a backup account",
                "Manage existing accounts",
                "Back",
            ),
        )
        command = next(
            rows for title, rows in io.sections if title == "Manage existing accounts"
        )
        self.assertEqual(command, (("Command", "orichum provider account --help"),))

    def test_back_during_account_placement_or_intent_keeps_draft_clean(self) -> None:
        config = SimpleNamespace(documents={"providers": {"providers": {"openai": {}}}})
        services = replace(
            self.services(),
            prepare_account=lambda provider: SimpleNamespace(
                credential_ref="new-openai.json",
                suggested_name="OpenAI new",
            ),
        )
        for choices in (
            ["Accounts", "Add an account", "Openai", "Back", "Exit"],
            [
                "Accounts",
                "Add an account",
                "Openai",
                "Current project",
                "Back",
                "Exit",
            ],
        ):
            with self.subTest(choices=choices):
                io = ScriptedUI(choices)

                status = run_configure(
                    {},
                    config,
                    Path("/work/acme"),
                    io=io,
                    services=services,
                )

                self.assertEqual(status, 0)
                dashboards = [
                    dict(rows)
                    for title, rows in io.sections
                    if title == "Orichum configuration"
                ]
                self.assertGreaterEqual(len(dashboards), 2)
                self.assertEqual(dashboards[-1]["Changes"], "None")

    def test_switch_profile_reviews_and_applies_target_models(self) -> None:
        snapshot = _snapshot_with_alternate_profile()
        applied: list[ConfigurationDraft] = []
        services = replace(
            self.services(),
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
            apply_draft=lambda current, draft: applied.append(draft),
        )
        io = ScriptedUI(
            [
                "Models",
                "Switch profile",
                "quality",
                "Review and apply changes",
                "Apply changes",
            ]
        )

        status = run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(status, 0)
        self.assertIn("quality", io.choice_labels[2])
        self.assertEqual(io.text_prompts, [])
        self.assertEqual(applied[0].profile_switch, "quality")
        self.assertEqual(applied[0].project.stack_name, "quality")
        self.assertEqual(
            {selection.model for selection in applied[0].role_models.values()},
            {"claude-sonnet-5"},
        )
        reviewed = next(
            rows for title, rows in io.sections if title == "Models" and len(rows) == 6
        )
        self.assertEqual({model for _role, model in reviewed}, {"claude-sonnet-5"})

    def test_switch_profile_recovers_from_an_unavailable_current_profile(self) -> None:
        base = _snapshot_with_alternate_profile()
        snapshot = replace(
            base,
            target=replace(base.target, stack_name="offline"),
            stacks=replace(
                base.stacks,
                stacks=MappingProxyType(
                    {
                        **base.stacks.stacks,
                        "offline": base.stacks.stacks["balanced"],
                    }
                ),
            ),
        )
        applied: list[ConfigurationDraft] = []
        services = replace(
            self.services(),
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
            apply_draft=lambda current, draft: applied.append(draft),
        )
        io = ScriptedUI(
            [
                "Models",
                "Switch profile",
                "quality",
                "Review and apply changes",
                "Apply changes",
            ]
        )

        with mock.patch(
            "integrations.common.configure_wizard.stack_is_live_compatible",
            side_effect=lambda current, name: name != "offline",
        ):
            run_configure(
                {},
                object(),
                Path("/work/acme"),
                io=io,
                services=services,
            )

        self.assertEqual(io.choice_labels[2], ("balanced", "quality"))
        self.assertEqual(applied[0].profile_switch, "quality")
        self.assertEqual(
            {selection.model for selection in applied[0].role_models.values()},
            {"claude-sonnet-5"},
        )

    def test_backup_flow_can_remove_project_account_lock(self) -> None:
        base = _snapshot()
        candidate = base.stacks.stacks[base.target.stack_name].controller[0]
        snapshot = replace(
            base,
            bindings=StackBindings({candidate.id: base.accounts[0].id}),
        )
        applied: list[ConfigurationDraft] = []
        services = ConfigureServices(
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
            prepare_account=lambda provider: SimpleNamespace(
                provider=provider,
                credential_ref="new-backup.json",
                suggested_name="Openai account",
            ),
            apply_draft=lambda current, draft: applied.append(draft),
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )
        io = ScriptedUI(
            [
                "Accounts",
                "Add a backup account",
                "OpenAI primary",
                "Allow OpenAI primary with OpenAI new backup [recommended]",
                "Review and apply changes",
                "Apply changes",
            ],
            text_values=["OpenAI new backup"],
        )

        run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(applied[0].binding_removals, (candidate.id,))

    def test_back_from_backup_lock_policy_does_not_stage_the_account(self) -> None:
        base = _snapshot()
        candidate = base.stacks.stacks[base.target.stack_name].controller[0]
        snapshot = replace(
            base,
            bindings=StackBindings({candidate.id: base.accounts[0].id}),
        )
        applied: list[ConfigurationDraft] = []
        services = ConfigureServices(
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
            prepare_account=lambda provider: SimpleNamespace(
                credential_ref="new-backup.json",
                suggested_name="Openai account",
            ),
            apply_draft=lambda current, draft: applied.append(draft),
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )
        io = ScriptedUI(
            [
                "Accounts",
                "Add a backup account",
                "OpenAI primary",
                "Back",
                "Exit",
            ],
            text_values=["OpenAI new backup"],
        )

        run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(applied, [])
        dashboards = [
            dict(rows)
            for title, rows in io.sections
            if title == "Orichum configuration"
        ]
        self.assertEqual(dashboards[-1]["Changes"], "None")

    def test_customize_role_can_mix_live_provider_models(self) -> None:
        base = _snapshot()
        claude = LiveModelChoice(
            family="claude",
            provider="anthropic",
            upstream="claude-sonnet-5",
            account_ids=(base.accounts[2].id,),
            account_names=(base.accounts[2].name,),
        )
        snapshot = replace(
            base,
            catalog=LiveCatalog(
                choices=(*base.catalog.choices, claude),
                unclassified=(),
            ),
        )
        applied: list[ConfigurationDraft] = []
        services = ConfigureServices(
            load_snapshot=lambda paths, config, project: snapshot,
            refresh_snapshot=lambda paths, config, project: snapshot,
            prepare_account=lambda provider: None,
            apply_draft=lambda current, draft: applied.append(draft),
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )
        io = ScriptedUI(
            [
                "Models",
                "Customize each role",
                "Correctness critic",
                "claude-sonnet-5",
                "Back",
                "Review and apply changes",
                "Apply changes",
            ]
        )

        run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(
            applied[0].role_models["controller"].model,
            "gpt-5.6-sol",
        )
        self.assertEqual(
            applied[0].role_models["correctness-critic"].model,
            "claude-sonnet-5",
        )

    def test_catalogue_drift_reopens_only_invalid_role_selections(self) -> None:
        base = _snapshot()
        claude = LiveModelChoice(
            family="claude",
            provider="anthropic",
            upstream="claude-sonnet-5",
            account_ids=(base.accounts[2].id,),
            account_names=(base.accounts[2].name,),
        )
        refreshed = replace(
            base,
            catalog=LiveCatalog(choices=(claude,), unclassified=()),
        )
        applied: list[ConfigurationDraft] = []
        services = ConfigureServices(
            load_snapshot=lambda paths, config, project: base,
            refresh_snapshot=lambda paths, config, project: refreshed,
            prepare_account=lambda provider: None,
            apply_draft=lambda current, draft: applied.append(draft),
            reconcile=lambda verbose: 0,
            verify_project=lambda project: None,
        )
        io = ScriptedUI(
            [
                "Models",
                "One model everywhere",
                "gpt-5.6-sol",
                "Review and apply changes",
                "Apply changes",
                *("claude-sonnet-5" for _ in range(6)),
                "Review and apply changes",
                "Apply changes",
            ]
        )

        run_configure(
            {},
            object(),
            Path("/work/acme"),
            io=io,
            services=services,
        )

        self.assertEqual(
            {selection.model for selection in applied[0].role_models.values()},
            {"claude-sonnet-5"},
        )


if __name__ == "__main__":
    unittest.main()
