#!/usr/bin/env python3
"""Small width-aware terminal primitives for guided Orichum commands."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TextIO

BACK = -1


class UiCancelled(RuntimeError):
    """Interactive input ended before the user applied a draft."""


@dataclass(frozen=True)
class Choice:
    label: str
    detail: str = ""
    marker: str = ""

    def search_text(self) -> str:
        return " ".join((self.label, self.detail, self.marker)).casefold()


class WizardIO(Protocol):
    def choose(
        self,
        title: str,
        options: Sequence[Choice],
        selected: int = 0,
        searchable: bool = False,
    ) -> int: ...

    def confirm(self, prompt: str, default: bool = False) -> bool: ...

    def text(self, prompt: str, initial: str = "") -> str: ...

    def show(self, text: str) -> None: ...

    def section(
        self,
        title: str,
        rows: Sequence[tuple[str, str]] = (),
    ) -> None: ...


class TerminalUI:
    """Line-oriented terminal UI with stable narrow-width rendering."""

    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        environment: Mapping[str, str] | None = None,
        width: int | None = None,
    ) -> None:
        self._stdin = sys.stdin if stdin is None else stdin
        self._stdout = sys.stdout if stdout is None else stdout
        self._environment = os.environ if environment is None else environment
        observed = shutil.get_terminal_size((80, 24)).columns
        self._width = max(20, width if width is not None else observed)
        self._has_block = False

    def show(self, text: str) -> None:
        self._write_block(self._wrap_lines(text))

    def section(
        self,
        title: str,
        rows: Sequence[tuple[str, str]] = (),
    ) -> None:
        lines = [title]
        if self._width < 56:
            for label, value in rows:
                lines.append(f"  {self._clip(label, self._width - 2)}")
                lines.append(f"    {self._clip(value, self._width - 4)}")
        elif rows:
            label_width = min(
                max(len(label) for label, _ in rows),
                max(1, self._width // 2),
            )
            for label, value in rows:
                clipped = self._clip(label, label_width)
                prefix = f"  {clipped:<{label_width}}  "
                lines.append(prefix + self._clip(value, self._width - len(prefix)))
        self._write_block(lines)

    def choose(
        self,
        title: str,
        options: Sequence[Choice],
        selected: int = 0,
        searchable: bool = False,
    ) -> int:
        normalized = tuple(
            option if isinstance(option, Choice) else Choice(str(option))
            for option in options
        )
        if not normalized:
            raise UiCancelled("no choices are available")
        selected = min(max(selected, 0), len(normalized) - 1)
        visible = tuple(range(len(normalized)))
        while True:
            lines = [title]
            for ordinal, index in enumerate(visible, start=1):
                option = normalized[index]
                marker = option.marker
                suffix = f" [{marker}]" if marker else ""
                detail = f"  {option.detail}" if option.detail else ""
                lines.append(
                    self._clip(
                        f"{ordinal}. {option.label}{detail}{suffix}",
                        self._width,
                    )
                )
            self._write_block(lines)
            answer = self._readline("Selection: ").strip()
            if answer.casefold() in {"b", "back", "esc"}:
                return BACK
            if searchable and answer.startswith("/"):
                query = answer[1:].strip().casefold()
                visible = tuple(
                    index
                    for index, option in enumerate(normalized)
                    if query in option.search_text()
                )
                if not visible:
                    self.show("No matches.")
                    visible = tuple(range(len(normalized)))
                elif selected not in visible:
                    selected = visible[0]
                continue
            if not answer:
                return selected
            try:
                ordinal = int(answer, 10)
            except ValueError:
                continue
            if 1 <= ordinal <= len(visible):
                return visible[ordinal - 1]

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = " [Y/n] " if default else " [y/N] "
        answer = self._readline(prompt + suffix).strip().casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        return default

    def text(self, prompt: str, initial: str = "") -> str:
        suffix = f" [{initial}]" if initial else ""
        answer = self._readline(f"{prompt}{suffix}: ").strip()
        return answer or initial

    def error(self, message: str, recovery: str) -> None:
        self.section("Configuration stopped", (("Reason", message),))
        self.show(recovery)

    def _readline(self, prompt: str) -> str:
        try:
            self._stdout.write(prompt)
            self._stdout.flush()
            answer = self._stdin.readline()
        except KeyboardInterrupt as error:
            self._stdout.write("\n")
            self._stdout.flush()
            raise UiCancelled("configuration cancelled") from error
        if answer == "":
            self._stdout.write("\n")
            self._stdout.flush()
            raise UiCancelled("configuration cancelled")
        return answer

    def _write_block(self, lines: Sequence[str]) -> None:
        if self._has_block:
            self._stdout.write("\n")
        self._stdout.write("\n".join(lines).rstrip() + "\n")
        self._stdout.flush()
        self._has_block = True

    def _wrap_lines(self, text: str) -> tuple[str, ...]:
        lines = text.rstrip("\n").splitlines() or [""]
        return tuple(self._clip(line, self._width) for line in lines)

    @staticmethod
    def _clip(value: str, width: int) -> str:
        width = max(1, width)
        if len(value) <= width:
            return value
        if width < 4:
            return value[:width]
        return value[: width - 1] + "…"
