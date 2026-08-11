#!/usr/bin/env python3
from __future__ import annotations

import io
import unittest

from integrations.common.terminal_ui import (
    Choice,
    TerminalUI,
    UiCancelled,
)


class TerminalUiTests(unittest.TestCase):
    def test_narrow_section_stacks_values_without_overflow(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdout=output,
            width=32,
            environment={"NO_COLOR": "1"},
        )

        ui.section(
            "Models",
            (("Architecture advisor", "claude-opus-5"),),
        )

        self.assertEqual(
            output.getvalue(),
            "Models\n  Architecture advisor\n    claude-opus-5\n",
        )

    def test_wide_section_aligns_values(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdout=output,
            width=80,
            environment={"NO_COLOR": "1"},
        )

        ui.section(
            "Accounts",
            (("OpenAI primary", "Personal"), ("OpenAI backup", "Backup")),
        )

        self.assertEqual(
            output.getvalue(),
            "Accounts\n  OpenAI primary  Personal\n  OpenAI backup   Backup\n",
        )

    def test_search_selects_from_filtered_numbered_choices(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdin=io.StringIO("/terra\n1\n"),
            stdout=output,
            environment={"NO_COLOR": "1"},
            width=80,
        )

        selected = ui.choose(
            "Choose a model",
            (Choice("gpt-5.6-sol"), Choice("gpt-5.6-terra")),
            searchable=True,
        )

        self.assertEqual(selected, 1)
        self.assertIn("1. gpt-5.6-terra", output.getvalue())

    def test_search_default_is_always_a_visible_choice(self) -> None:
        options = (
            Choice("gpt-5.6-sol"),
            Choice("gpt-5.6-terra"),
            Choice("gpt-5.6-terra-mini"),
        )
        for selected, expected in ((0, 1), (2, 2)):
            with self.subTest(selected=selected):
                ui = TerminalUI(
                    stdin=io.StringIO("/terra\n\n"),
                    stdout=io.StringIO(),
                    environment={"NO_COLOR": "1"},
                    width=80,
                )

                result = ui.choose(
                    "Choose a model",
                    options,
                    selected=selected,
                    searchable=True,
                )

                self.assertEqual(result, expected)

    def test_only_explicit_markers_are_rendered_as_current(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdin=io.StringIO("\n"),
            stdout=output,
            environment={"NO_COLOR": "1"},
            width=80,
        )

        selected = ui.choose(
            "Choose an action",
            (Choice("Models"), Choice("Accounts", marker="current")),
        )

        self.assertEqual(selected, 0)
        self.assertIn("1. Models\n", output.getvalue())
        self.assertNotIn("1. Models [current]", output.getvalue())
        self.assertIn("2. Accounts [current]", output.getvalue())

    def test_no_color_suppresses_ansi(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdout=output,
            width=80,
            environment={"NO_COLOR": "1"},
        )

        ui.show("Configured")

        self.assertEqual(output.getvalue(), "Configured\n")
        self.assertNotIn("\x1b[", output.getvalue())

    def test_end_of_input_cancels_on_a_clean_line(self) -> None:
        output = io.StringIO()
        ui = TerminalUI(
            stdin=io.StringIO(""),
            stdout=output,
            environment={"NO_COLOR": "1"},
            width=80,
        )

        with self.assertRaises(UiCancelled):
            ui.confirm("Apply changes?", default=False)

        self.assertTrue(output.getvalue().endswith("\n"))


if __name__ == "__main__":
    unittest.main()
