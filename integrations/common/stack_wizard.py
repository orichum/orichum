#!/usr/bin/env python3
"""Interactive, terminal-independent model-stack configuration."""

from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path
import re
import select
import shutil
import signal
import sys
import termios
from types import MappingProxyType
from typing import Callable, Mapping, Protocol, Sequence, TextIO
import tty

from .account_registry import Account, load_accounts, validate_account_bindings
from .cliproxy_management import (
    attest_owned_connection,
    load_management_endpoint,
)
from .model_routing import ROLES, RoutingError, validate_stack_name
from .orichum_config import (
    ResolvedConfig,
    default_config_paths,
    load_control_plane,
    validate_control_plane,
)
from .project_context import (
    assign_stack_to_context,
    configure_normal_scope,
    control_plane_transaction,
    resolve_control_plane_context,
)
from .stack_bindings import StackBindings, stack_binding_transaction
from .stack_catalog import (
    LiveCatalog,
    LiveModelChoice,
    fetch_live_catalog,
    project_live_catalog,
)
from .stack_definition import (
    NormalizedStacks,
    StackCandidate,
    candidate_id,
    normalize_model_stacks,
    serialize_model_stacks,
)
from .stack_store import (
    StackSnapshot,
    delete_stack,
    load_stack_snapshot,
    save_stack,
    validate_stack_assignment,
)


BACK = -1
_INTERNAL_ID = re.compile(
    r"(?<![A-Za-z0-9])oc-(?:a|c|r)-[a-f0-9]{16}(?![A-Za-z0-9])"
)

_RECOMMENDED_MODELS = {
    "controller": (
        "gpt-5.6-sol",
        "claude-opus-5",
        "claude-opus-4-6-thinking",
        "claude-sonnet-5",
        "gpt-5.6-terra",
    ),
    "repository-explorer": (
        "gpt-5.6-terra",
        "claude-sonnet-5",
        "gpt-5.6-sol",
        "claude-opus-5",
    ),
    "repository-verifier": (
        "gpt-5.6-terra",
        "claude-sonnet-5",
        "gpt-5.6-sol",
        "claude-opus-5",
    ),
    "correctness-critic": (
        "claude-sonnet-5",
        "gpt-5.6-terra",
        "gpt-5.6-sol",
        "claude-opus-5",
    ),
    "architecture-advisor": (
        "claude-opus-5",
        "claude-opus-4-6-thinking",
        "gpt-5.6-sol",
        "claude-sonnet-5",
    ),
    "implementation-worker": (
        "gpt-5.6-sol",
        "claude-sonnet-5",
        "claude-opus-5",
        "gpt-5.6-terra",
    ),
    "planning-advisor": (
        "claude-opus-5",
        "claude-opus-4-6-thinking",
        "gpt-5.6-sol",
        "claude-sonnet-5",
    ),
}


class WizardCancelled(RuntimeError):
    """The user or terminal ended the wizard without mutation."""


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

    def show(self, summary: str) -> None: ...


@dataclass(frozen=True)
class WizardResult:
    stacks: NormalizedStacks
    bindings: StackBindings
    stack_name: str
    save: bool
    assign_current_project: bool


@dataclass(frozen=True)
class _DraftCandidate:
    source_id: str | None
    model: str
    family: str
    providers: tuple[str, ...]
    account_id: str | None
    account_name: str | None
    inherited: bool


@dataclass(frozen=True)
class _Draft:
    action: str
    source_name: str
    stack_name: str
    controller: tuple[_DraftCandidate, ...]
    agents: Mapping[str, tuple[_DraftCandidate, ...]]


@dataclass(frozen=True)
class _MissingChoice:
    scope: str
    ordinal: int
    model: str
    provider: str


class StackWizard:
    """Build a validated proposal without performing persistence."""

    def __init__(
        self,
        snapshot: StackSnapshot,
        catalog: LiveCatalog,
        accounts: Sequence[Account],
        io: WizardIO,
        *,
        refresh_catalog: Callable[[], LiveCatalog] | None = None,
        projects: Mapping[str, object] | None = None,
        assignment_default: bool = False,
    ) -> None:
        self._snapshot = snapshot
        self._catalog = catalog
        self._accounts = tuple(
            account for account in accounts if account.state == "active"
        )
        self._accounts_by_id = {
            account.id: account for account in self._accounts
        }
        self._io = io
        self._refresh_catalog = refresh_catalog or (lambda: self._catalog)
        self._projects = projects
        self._assignment_default = assignment_default
        self._pending_draft: _Draft | None = None

    def run(self, launch_dir: Path) -> WizardResult:
        try:
            return self._run(Path(launch_dir))
        except (
            BrokenPipeError,
            EOFError,
            KeyboardInterrupt,
            WizardCancelled,
        ):
            return self._cancelled()

    def _run(
        self,
        launch_dir: Path,
        *,
        draft: _Draft | None = None,
        stage: int = 1,
        refresh_before_return: bool = True,
    ) -> WizardResult:
        actions = (
            Choice("Create new"),
            Choice("Clone existing"),
            Choice("Edit existing"),
            Choice("Delete existing"),
        )
        names = tuple(sorted(self._snapshot.stacks.stacks))
        action_index = (
            next(
                (
                    index
                    for index, action in enumerate(actions)
                    if draft is not None
                    and action.label == draft.action
                ),
                0,
            )
        )
        while True:
            if stage == 1:
                chosen_action = self._checked_choose(
                    "Step 1/5 · Stack",
                    actions,
                    selected=action_index,
                    searchable=False,
                )
                if chosen_action == BACK:
                    raise WizardCancelled
                action_index = chosen_action
                action = actions[action_index].label

                if action == "Delete existing":
                    source = self._choose_stack(names)
                    if source is None:
                        continue
                    if not self._io.confirm(
                        f"Delete stack {source}? This cannot be undone.",
                        default=False,
                    ):
                        return self._cancelled(source)
                    updated, bindings = delete_stack(
                        self._snapshot,
                        source,
                        self._projects or {"contexts": []},
                    )
                    return WizardResult(
                        updated,
                        bindings,
                        source,
                        True,
                        False,
                    )

                source = (
                    self._choose_stack(
                        names,
                        selected_name=(
                            draft.source_name
                            if draft is not None
                            and draft.action == action
                            else None
                        ),
                    )
                    if action in ("Clone existing", "Edit existing")
                    else self._snapshot.stacks.default_stack
                )
                if source is None:
                    continue
                if action == "Edit existing":
                    stack_name = source
                else:
                    stack_name = self._new_stack_name(
                        draft.stack_name
                        if draft is not None
                        and draft.action == action
                        and draft.source_name == source
                        else source,
                        pending_name=(
                            draft.stack_name
                            if draft is not None
                            and draft.action == action
                            and draft.source_name == source
                            else None
                        ),
                    )
                if (
                    draft is not None
                    and draft.action == action
                    and draft.source_name == source
                ):
                    draft = replace(draft, stack_name=stack_name)
                else:
                    draft = self._draft_from_stack(
                        action, source, stack_name
                    )
                stage = 2
                continue

            if draft is None:
                raise RoutingError("stack draft is unavailable")

            if stage == 2:
                controller_action = self._checked_choose(
                    "Step 2/5 · Controller",
                    (
                        Choice("Keep inherited", marker="inherited"),
                        Choice("Configure controller"),
                    ),
                    selected=1 if draft.action == "Create new" else 0,
                )
                if controller_action == BACK:
                    stage = 1
                    continue
                if controller_action == 1:
                    configured = self._configure_scope(
                        "controller", draft.controller
                    )
                    if configured is None:
                        continue
                    draft = replace(draft, controller=configured)
                stage = 3
                continue

            if stage == 3:
                draft, went_back = self._configure_agents(draft)
                if went_back:
                    stage = 2
                    continue
                stage = 4
                continue

            proposed_stacks, proposed_bindings = self._materialize(draft)
            self._io.show(
                self._review(draft, proposed_stacks, proposed_bindings)
            )
            review_action = self._checked_choose(
                "Step 4/5 · Review action",
                (
                    Choice("Back to agents"),
                    Choice("Continue to save"),
                ),
                selected=1,
            )
            if review_action in (BACK, 0):
                stage = 3
                continue
            if not self._io.confirm(
                f"Save stack {draft.stack_name}?", default=False
            ):
                return self._cancelled(draft.stack_name)

            if not refresh_before_return:
                break
            current_catalog = self._refresh_catalog()
            missing = self._first_missing(
                proposed_stacks,
                proposed_bindings,
                draft.stack_name,
                current_catalog,
            )
            if missing is None:
                self._catalog = current_catalog
                break
            self._catalog = current_catalog
            self._io.show(
                "Availability changed for "
                f"{missing.scope} candidate {missing.model} via "
                f"{missing.provider}. Choose a current live model."
            )
            draft = self._repick_missing(draft, missing)
            stage = 4

        assign = self._assignment_choice(launch_dir, draft.stack_name)
        self._pending_draft = draft
        return WizardResult(
            proposed_stacks,
            proposed_bindings,
            draft.stack_name,
            True,
            assign,
        )

    def missing_live_choice(
        self,
        result: WizardResult,
        catalog: LiveCatalog,
    ) -> _MissingChoice | None:
        if result.stack_name not in result.stacks.stacks:
            return None
        return self._first_missing(
            result.stacks,
            result.bindings,
            result.stack_name,
            catalog,
        )

    def resume_missing(
        self,
        missing: _MissingChoice,
        catalog: LiveCatalog,
        launch_dir: Path,
        projects: Mapping[str, object],
    ) -> WizardResult:
        try:
            draft = self._pending_draft
            if draft is None:
                raise RoutingError(
                    f"stack {self._snapshot.stacks.default_stack} "
                    f"{missing.scope} model {missing.model} is not live "
                    f"through {missing.provider}"
                )
            self._catalog = catalog
            self._projects = projects
            self._io.show(
                "Availability changed for "
                f"{missing.scope} candidate {missing.model} via "
                f"{missing.provider}. Choose a current live model."
            )
            draft = self._repick_missing(draft, missing)
            return self._run(
                Path(launch_dir),
                draft=draft,
                stage=4,
                refresh_before_return=False,
            )
        except (
            BrokenPipeError,
            EOFError,
            KeyboardInterrupt,
            WizardCancelled,
        ):
            return self._cancelled()

    def _cancelled(self, stack_name: str = "") -> WizardResult:
        return WizardResult(
            self._snapshot.stacks,
            self._snapshot.bindings,
            stack_name,
            False,
            False,
        )

    def _checked_choose(
        self,
        title: str,
        options: Sequence[Choice],
        selected: int = 0,
        searchable: bool = False,
    ) -> int:
        if not options:
            raise RoutingError(f"{title} has no live choices")
        selected = min(max(selected, 0), len(options) - 1)
        chosen = self._io.choose(
            title,
            options,
            selected=selected,
            searchable=searchable,
        )
        if chosen == BACK:
            return BACK
        if type(chosen) is not int or not 0 <= chosen < len(options):
            raise WizardCancelled
        return chosen

    def _choose_stack(
        self,
        names: Sequence[str],
        *,
        selected_name: str | None = None,
    ) -> str | None:
        selected = (
            names.index(selected_name)
            if selected_name in names
            else 0
        )
        chosen = self._checked_choose(
            "Step 1/5 · Existing stack",
            tuple(
                Choice(
                    name,
                    marker=(
                        "current default"
                        if name == self._snapshot.stacks.default_stack
                        else ""
                    ),
                )
                for name in names
            ),
            selected=selected,
            searchable=len(names) > 9,
        )
        if chosen == BACK:
            return None
        return names[chosen]

    def _new_stack_name(
        self,
        initial: str,
        *,
        pending_name: str | None = None,
    ) -> str:
        suggested = (
            initial if pending_name is not None else f"{initial}-copy"
        )
        while True:
            raw = self._io.text("New stack name", initial=suggested).strip()
            try:
                name = validate_stack_name(raw)
            except RoutingError as error:
                self._io.show(str(error))
                continue
            if (
                name in self._snapshot.stacks.stacks
                and name != pending_name
            ):
                self._io.show(f"Stack {name} already exists.")
                continue
            return name

    def _draft_from_stack(
        self, action: str, source_name: str, stack_name: str
    ) -> _Draft:
        source = self._snapshot.stacks.stacks[source_name]
        preserve_identity = action == "Edit existing"
        return _Draft(
            action=action,
            source_name=source_name,
            stack_name=stack_name,
            controller=tuple(
                self._draft_candidate(
                    candidate,
                    preserve_identity=preserve_identity,
                )
                for candidate in source.controller
            ),
            agents=MappingProxyType(
                {
                    role: tuple(
                        self._draft_candidate(
                            candidate,
                            preserve_identity=preserve_identity,
                        )
                        for candidate in source.agents[role]
                    )
                    for role in ROLES
                }
            ),
        )

    def _draft_candidate(
        self,
        candidate: StackCandidate,
        *,
        preserve_identity: bool,
    ) -> _DraftCandidate:
        definition = self._snapshot.stacks.models[candidate.model]
        account_id = self._snapshot.bindings.candidate_accounts.get(
            candidate.id
        )
        account = self._accounts_by_id.get(account_id or "")
        return _DraftCandidate(
            source_id=candidate.id if preserve_identity else None,
            model=candidate.model,
            family=definition.family,
            providers=candidate.providers,
            account_id=account_id,
            account_name=account.name if account is not None else None,
            inherited=True,
        )

    def _configure_agents(
        self, draft: _Draft
    ) -> tuple[_Draft, bool]:
        ordered_roles = (
            "architecture-advisor",
            "planning-advisor",
            *(
                role
                for role in ROLES
                if role not in {"architecture-advisor", "planning-advisor"}
            ),
        )
        selected = 0
        while True:
            options = tuple(
                Choice(
                    role,
                    detail=", ".join(
                        candidate.model for candidate in draft.agents[role]
                    ),
                    marker=(
                        "inherited"
                        if all(
                            candidate.inherited
                            for candidate in draft.agents[role]
                        )
                        else "changed"
                    ),
                )
                for role in ordered_roles
            ) + (Choice("Continue to review"),)
            chosen = self._checked_choose(
                "Step 3/5 · Agents",
                options,
                selected=selected,
                searchable=False,
            )
            if chosen == BACK:
                return draft, True
            if chosen == len(ordered_roles):
                return draft, False
            role = ordered_roles[chosen]
            configured = self._configure_scope(role, draft.agents[role])
            if configured is not None:
                agents = dict(draft.agents)
                agents[role] = configured
                draft = replace(
                    draft, agents=MappingProxyType(agents)
                )
            selected = len(ordered_roles)

    def _configure_scope(
        self,
        scope: str,
        current: tuple[_DraftCandidate, ...],
    ) -> tuple[_DraftCandidate, ...] | None:
        title_prefix = (
            "Step 2/5 · Controller"
            if scope == "controller"
            else f"Step 3/5 · {scope}"
        )
        picked = self._pick_candidate(
            current=current[0] if current else None,
            required_family=None,
            excluded_models=frozenset(),
            title_prefix=title_prefix,
        )
        if picked is None:
            return None
        candidates = [picked]
        while True:
            alternatives = self._catalog_choices(
                family=picked.family,
                excluded_models=frozenset(
                    candidate.model for candidate in candidates
                ),
            )
            if not alternatives:
                break
            action = self._checked_choose(
                f"{title_prefix} · Startup candidates",
                (
                    Choice("Finish role"),
                    Choice("Add startup candidate"),
                ),
            )
            if action in (BACK, 0):
                break
            alternate = self._pick_candidate(
                current=None,
                required_family=picked.family,
                excluded_models=frozenset(
                    candidate.model for candidate in candidates
                ),
                title_prefix=title_prefix,
            )
            if alternate is None:
                break
            candidates.append(alternate)
        return tuple(candidates)

    def _pick_candidate(
        self,
        *,
        current: _DraftCandidate | None,
        required_family: str | None,
        excluded_models: frozenset[str],
        title_prefix: str,
    ) -> _DraftCandidate | None:
        provider = current.providers[0] if current else None
        family = required_family or (current.family if current else None)
        model = current.model if current else None
        while True:
            choices = self._catalog_choices(
                family=required_family,
                excluded_models=excluded_models,
            )
            providers = tuple(sorted({choice.provider for choice in choices}))
            provider_options = tuple(
                Choice(
                    item,
                    marker=(
                        "inherited"
                        if current is not None
                        and item in current.providers
                        else ""
                    ),
                )
                for item in providers
            )
            selected = providers.index(provider) if provider in providers else 0
            chosen = self._checked_choose(
                f"{title_prefix} · Provider",
                provider_options,
                selected=selected,
                searchable=len(providers) > 9,
            )
            if chosen == BACK:
                return None
            provider = providers[chosen]

            while True:
                families = tuple(
                    sorted(
                        {
                            choice.family
                            for choice in choices
                            if choice.provider == provider
                        }
                    )
                )
                family_options = tuple(
                    Choice(
                        item,
                        marker=(
                            "inherited"
                            if current is not None
                            and item == current.family
                            else ""
                        ),
                    )
                    for item in families
                )
                family_selected = (
                    families.index(family) if family in families else 0
                )
                family_index = self._checked_choose(
                    f"{title_prefix} · Family",
                    family_options,
                    selected=family_selected,
                    searchable=len(families) > 9,
                )
                if family_index == BACK:
                    break
                family = families[family_index]

                while True:
                    models = tuple(
                        choice
                        for choice in choices
                        if choice.provider == provider
                        and choice.family == family
                    )
                    model_options = tuple(
                        Choice(
                            choice.upstream,
                            detail=(
                                f"{choice.provider} · "
                                f"{', '.join(choice.account_names)}"
                            ),
                            marker=(
                                "inherited"
                                if current is not None
                                and self._current_upstream(
                                    current, provider
                                )
                                == choice.upstream
                                else ""
                            ),
                        )
                        for choice in models
                    )
                    selected_model = next(
                        (
                            index
                            for index, choice in enumerate(models)
                            if choice.upstream == model
                            or self._current_upstream(
                                current, provider
                            )
                            == choice.upstream
                        ),
                        0,
                    )
                    model_index = self._checked_choose(
                        f"{title_prefix} · Exact live model",
                        model_options,
                        selected=selected_model,
                        searchable=len(models) > 6,
                    )
                    if model_index == BACK:
                        break
                    live = models[model_index]
                    logical_model = self._logical_model(live)
                    account_options = (
                        Choice("Automatic within provider"),
                    ) + tuple(
                        Choice(name)
                        for name in live.account_names
                    )
                    selected_account = 0
                    if current is not None and current.account_id is not None:
                        selected_account = next(
                            (
                                index + 1
                                for index, account_id in enumerate(
                                    live.account_ids
                                )
                                if account_id == current.account_id
                            ),
                            0,
                        )
                    account_index = self._checked_choose(
                        f"{title_prefix} · Account policy",
                        account_options,
                        selected=selected_account,
                        searchable=len(account_options) > 9,
                    )
                    if account_index == BACK:
                        continue
                    account_id = (
                        None
                        if account_index == 0
                        else live.account_ids[account_index - 1]
                    )
                    account_name = (
                        None
                        if account_index == 0
                        else live.account_names[account_index - 1]
                    )
                    return _DraftCandidate(
                        source_id=(
                            current.source_id
                            if current is not None
                            else None
                        ),
                        model=logical_model,
                        family=live.family,
                        providers=(live.provider,),
                        account_id=account_id,
                        account_name=account_name,
                        inherited=False,
                    )
                continue

    def _catalog_choices(
        self,
        *,
        family: str | None,
        excluded_models: frozenset[str],
    ) -> tuple[LiveModelChoice, ...]:
        return tuple(
            choice
            for choice in self._catalog.choices
            if (family is None or choice.family == family)
            and self._logical_model(choice) not in excluded_models
        )

    def _current_upstream(
        self,
        current: _DraftCandidate | None,
        provider: str,
    ) -> str | None:
        if current is None:
            return None
        definition = self._snapshot.stacks.models.get(current.model)
        if definition is not None:
            return definition.routes.get(provider)
        return current.model

    def _logical_model(self, live: LiveModelChoice) -> str:
        for model, definition in self._snapshot.stacks.models.items():
            if (
                definition.family == live.family
                and definition.routes.get(live.provider) == live.upstream
            ):
                return model
        return live.upstream

    def _materialize(
        self, draft: _Draft
    ) -> tuple[NormalizedStacks, StackBindings]:
        document = serialize_model_stacks(self._snapshot.stacks)
        raw_models = document["models"]
        if not isinstance(raw_models, dict):
            raise RoutingError("model stack models are invalid")
        for candidate in (
            *draft.controller,
            *(
                candidate
                for role in ROLES
                for candidate in draft.agents[role]
            ),
        ):
            definition = raw_models.get(candidate.model)
            if definition is None:
                raw_models[candidate.model] = {
                    "family": candidate.family,
                    "routes": {
                        provider: self._upstream(
                            candidate.model, provider
                        )
                        for provider in candidate.providers
                    },
                }
                continue
            if (
                not isinstance(definition, dict)
                or definition.get("family") != candidate.family
                or not isinstance(definition.get("routes"), dict)
            ):
                raise RoutingError(
                    f"model {candidate.model} conflicts with live family"
                )
            routes = definition["routes"]
            for provider in candidate.providers:
                routes.setdefault(
                    provider,
                    self._upstream(candidate.model, provider),
                )

        raw_stacks = document["stacks"]
        if not isinstance(raw_stacks, dict):
            raise RoutingError("model stacks are invalid")

        def identifier(
            scope: str, ordinal: int, candidate: _DraftCandidate
        ) -> str:
            if (
                draft.action == "Edit existing"
                and candidate.source_id is not None
            ):
                return candidate.source_id
            return candidate_id(
                draft.stack_name,
                scope,
                ordinal,
                candidate.model,
            )

        def raw_candidates(
            scope: str, candidates: tuple[_DraftCandidate, ...]
        ) -> list[dict[str, object]]:
            return [
                {
                    "id": identifier(scope, ordinal, candidate),
                    "model": candidate.model,
                    "providers": list(candidate.providers),
                }
                for ordinal, candidate in enumerate(candidates)
            ]

        raw_stacks[draft.stack_name] = {
            "controller": raw_candidates(
                "controller", draft.controller
            ),
            "agents": {
                role: raw_candidates(role, draft.agents[role])
                for role in ROLES
            },
        }
        stacks = normalize_model_stacks(document)

        target_ids = {
            candidate.id
            for candidates in (
                stacks.stacks[draft.stack_name].controller,
                *stacks.stacks[draft.stack_name].agents.values(),
            )
            for candidate in candidates
        }
        original_target_ids = (
            {
                candidate.id
                for candidates in (
                    self._snapshot.stacks.stacks[
                        draft.stack_name
                    ].controller,
                    *self._snapshot.stacks.stacks[
                        draft.stack_name
                    ].agents.values(),
                )
                for candidate in candidates
            }
            if draft.stack_name in self._snapshot.stacks.stacks
            else set()
        )
        bindings = {
            candidate: account
            for candidate, account in (
                self._snapshot.bindings.candidate_accounts.items()
            )
            if candidate not in original_target_ids
        }
        selected_drafts = (
            ("controller", draft.controller),
            *((role, draft.agents[role]) for role in ROLES),
        )
        for scope, candidates in selected_drafts:
            for ordinal, candidate in enumerate(candidates):
                candidate_identifier = identifier(
                    scope, ordinal, candidate
                )
                if candidate_identifier not in target_ids:
                    raise RoutingError("candidate normalization failed")
                if candidate.account_id is not None:
                    bindings[candidate_identifier] = candidate.account_id
        return stacks, StackBindings(bindings)

    def _upstream(self, model: str, provider: str) -> str:
        definition = self._snapshot.stacks.models.get(model)
        if definition is not None and provider in definition.routes:
            return definition.routes[provider]
        for choice in self._catalog.choices:
            if (
                choice.provider == provider
                and self._logical_model(choice) == model
            ):
                return choice.upstream
        raise RoutingError(
            f"model {model} is no longer live through provider {provider}"
        )

    def _review(
        self,
        draft: _Draft,
        stacks: NormalizedStacks,
        bindings: StackBindings,
    ) -> str:
        default_status = (
            "default"
            if draft.stack_name == stacks.default_stack
            else "not default"
        )
        lines = [
            "Step 4/5 · Review",
            f"Stack: {draft.stack_name} ({default_status})",
        ]
        for scope, candidates in (
            ("controller", draft.controller),
            *((role, draft.agents[role]) for role in ROLES),
        ):
            for ordinal, candidate in enumerate(candidates, 1):
                policy = (
                    candidate.account_name
                    if candidate.account_name is not None
                    else "Automatic within provider"
                )
                marker = "inherited" if candidate.inherited else "changed"
                live = next(
                    (
                        choice
                        for choice in self._catalog.choices
                        if choice.family == candidate.family
                        and choice.provider in candidate.providers
                        and self._logical_model(choice) == candidate.model
                    ),
                    None,
                )
                recovery = (
                    "available"
                    if candidate.account_id is None
                    and live is not None
                    and len(live.account_names) > 1
                    else "fixed"
                )
                definition = stacks.models[candidate.model]
                routes = ", ".join(
                    f"{provider}/{definition.routes[provider]}"
                    for provider in candidate.providers
                )
                lines.append(
                    f"[{marker}] {scope} #{ordinal}: {candidate.model} · "
                    f"routes {routes} · "
                    f"account {policy} · same-model recovery {recovery}"
                )
        rendered = "\n".join(lines)
        if _INTERNAL_ID.search(rendered):
            raise RoutingError("review contains internal metadata")
        return rendered

    def _first_missing(
        self,
        stacks: NormalizedStacks,
        bindings: StackBindings,
        stack_name: str,
        catalog: LiveCatalog,
    ) -> _MissingChoice | None:
        selected = stacks.stacks[stack_name]
        for scope, candidates in (
            ("controller", selected.controller),
            *((role, selected.agents[role]) for role in ROLES),
        ):
            for ordinal, candidate in enumerate(candidates):
                definition = stacks.models[candidate.model]
                locked = bindings.candidate_accounts.get(candidate.id)
                for provider in candidate.providers:
                    live = next(
                        (
                            choice
                            for choice in catalog.choices
                            if choice.family == definition.family
                            and choice.provider == provider
                            and choice.upstream
                            == definition.routes[provider]
                        ),
                        None,
                    )
                    if live is None or (
                        locked is not None
                        and locked not in live.account_ids
                    ):
                        return _MissingChoice(
                            scope,
                            ordinal,
                            candidate.model,
                            provider,
                        )
        return None

    def _repick_missing(
        self, draft: _Draft, missing: _MissingChoice
    ) -> _Draft:
        candidates = (
            draft.controller
            if missing.scope == "controller"
            else draft.agents[missing.scope]
        )
        old = candidates[missing.ordinal]
        replacement = self._pick_candidate(
            current=old,
            required_family=old.family,
            excluded_models=frozenset(
                candidate.model
                for index, candidate in enumerate(candidates)
                if index != missing.ordinal
            ),
            title_prefix=(
                "Step 2/5 · Controller"
                if missing.scope == "controller"
                else f"Step 3/5 · {missing.scope}"
            ),
        )
        if replacement is None:
            raise WizardCancelled
        updated = (
            *candidates[: missing.ordinal],
            replacement,
            *candidates[missing.ordinal + 1 :],
        )
        if missing.scope == "controller":
            return replace(draft, controller=updated)
        agents = dict(draft.agents)
        agents[missing.scope] = updated
        return replace(draft, agents=MappingProxyType(agents))

    def _assignment_choice(
        self, launch_dir: Path, stack_name: str
    ) -> bool:
        if self._projects is None:
            return self._io.confirm(
                "Step 5/5 · Activate\n"
                f"Assign {stack_name} to the current project "
                f"({launch_dir})?",
                default=self._assignment_default,
            )
        resolved = resolve_control_plane_context(
            self._projects, launch_dir
        )
        route = resolved.get("route")
        if not isinstance(route, Mapping):
            self._io.show(
                "Step 5/5 · Activate\n"
                "No project context matches the current directory; "
                "the stack will remain unassigned."
            )
            return False
        root = str(route["contextRootReal"])
        self._io.show(f"Step 5/5 · Activate\nMatched project: {root}")
        return self._io.confirm(
            f"Assign {stack_name} to {root}?",
            default=self._assignment_default,
        )


class TerminalWizardIO:
    """Small standard-library terminal adapter with a numbered fallback."""

    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._stdin = sys.stdin if stdin is None else stdin
        self._stdout = sys.stdout if stdout is None else stdout
        self._environment = (
            os.environ if environment is None else environment
        )

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
            raise WizardCancelled
        selected = min(max(selected, 0), len(normalized) - 1)
        if self._advanced():
            return self._raw_choose(
                title, normalized, selected, searchable
            )
        return self._line_choose(
            title, normalized, selected, searchable
        )

    def confirm(self, prompt: str, default: bool = False) -> bool:
        suffix = " [Y/n] " if default else " [y/N] "
        try:
            self._stdout.write(prompt + suffix)
            self._stdout.flush()
            answer = self._stdin.readline()
        except KeyboardInterrupt as error:
            raise WizardCancelled from error
        if answer == "":
            raise WizardCancelled
        answer = answer.strip().casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        return default

    def text(self, prompt: str, initial: str = "") -> str:
        suffix = f" [{initial}]" if initial else ""
        try:
            self._stdout.write(f"{prompt}{suffix}: ")
            self._stdout.flush()
            answer = self._stdin.readline()
        except KeyboardInterrupt as error:
            raise WizardCancelled from error
        if answer == "":
            raise WizardCancelled
        value = answer.rstrip("\r\n")
        return value if value else initial

    def show(self, summary: str) -> None:
        self._stdout.write(summary.rstrip("\n") + "\n")
        self._stdout.flush()

    def _advanced(self) -> bool:
        try:
            return (
                self._stdin.isatty()
                and self._stdout.isatty()
                and self._environment.get("TERM", "") not in ("", "dumb")
                and self._stdin.fileno() >= 0
            )
        except (AttributeError, OSError):
            return False

    def _raw_choose(
        self,
        title: str,
        options: tuple[Choice, ...],
        selected: int,
        searchable: bool,
    ) -> int:
        descriptor = self._stdin.fileno()
        previous = termios.tcgetattr(descriptor)
        old_resize = None
        resized = False

        def on_resize(_signum: int, _frame: object) -> None:
            nonlocal resized
            resized = True

        if hasattr(signal, "SIGWINCH"):
            old_resize = signal.signal(signal.SIGWINCH, on_resize)
        query = ""
        try:
            tty.setraw(descriptor, when=termios.TCSANOW)
            while True:
                matching = self._matching(options, query)
                if matching and selected not in matching:
                    selected = matching[0]
                self._render_raw(
                    title, options, matching, selected, query
                )
                resized = False
                try:
                    key = os.read(descriptor, 1)
                except InterruptedError:
                    if resized:
                        continue
                    raise
                if key == b"":
                    raise WizardCancelled
                if key == b"\x03":
                    raise WizardCancelled
                if key == b"\x1b":
                    sequence = key
                    while select.select([descriptor], [], [], 0.03)[0]:
                        sequence += os.read(descriptor, 1)
                        if len(sequence) >= 3:
                            break
                    if sequence in (b"\x1b[A", b"\x1bOA"):
                        if matching:
                            selected = matching[
                                (matching.index(selected) - 1)
                                % len(matching)
                            ]
                        continue
                    if sequence in (b"\x1b[B", b"\x1bOB"):
                        if matching:
                            selected = matching[
                                (matching.index(selected) + 1)
                                % len(matching)
                            ]
                        continue
                    return BACK
                if key in (b"\r", b"\n"):
                    if matching:
                        return selected
                    continue
                if key in (b"\x7f", b"\x08") and query:
                    query = query[:-1]
                    continue
                if key == b"/" and searchable:
                    query = ""
                    continue
                if key.isdigit() and key != b"0" and not query:
                    ordinal = int(key)
                    if matching and ordinal <= len(matching):
                        return matching[ordinal - 1]
                    continue
                if searchable and key >= b" ":
                    try:
                        query += key.decode("utf-8")
                    except UnicodeError:
                        pass
        finally:
            termios.tcsetattr(descriptor, termios.TCSANOW, previous)
            if old_resize is not None:
                signal.signal(signal.SIGWINCH, old_resize)
            self._stdout.write("\r\n")
            self._stdout.flush()

    def _matching(
        self, options: tuple[Choice, ...], query: str
    ) -> tuple[int, ...]:
        folded = query.casefold()
        return tuple(
            index
            for index, option in enumerate(options)
            if not folded or folded in option.search_text()
        )

    def _render_raw(
        self,
        title: str,
        options: tuple[Choice, ...],
        matching: tuple[int, ...],
        selected: int,
        query: str,
    ) -> None:
        try:
            width = os.get_terminal_size(
                self._stdout.fileno()
            ).columns
        except (AttributeError, OSError):
            width = shutil.get_terminal_size(
                fallback=(80, 24)
            ).columns
        width = max(20, width)
        lines = [title]
        if query:
            lines.append(f"Search: {query}")
        if not matching:
            lines.append("No matches")
        for visible, index in enumerate(matching, 1):
            option = options[index]
            marker = f"[{option.marker}] " if option.marker else ""
            detail = f" — {option.detail}" if option.detail else ""
            prefix = ">" if index == selected else " "
            lines.append(
                self._clip(
                    f"{prefix} {visible}) {marker}{option.label}{detail}",
                    width,
                )
            )
        lines.append(
            "↑/↓ move · number select · / search · "
            "Enter keep · Esc back"
        )
        self._stdout.write(
            "\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n"
        )
        self._stdout.flush()

    def _line_choose(
        self,
        title: str,
        options: tuple[Choice, ...],
        selected: int,
        searchable: bool,
    ) -> int:
        matching = tuple(range(len(options)))
        while True:
            self._stdout.write(title + "\n")
            for ordinal, index in enumerate(matching, 1):
                option = options[index]
                marker = f" [{option.marker}]" if option.marker else ""
                detail = f" — {option.detail}" if option.detail else ""
                inherited = " *" if index == selected else ""
                self._stdout.write(
                    f"{ordinal}) {option.label}{marker}{detail}{inherited}\n"
                )
            self._stdout.write(
                "Selection"
                + (" (or /search)" if searchable else "")
                + ": "
            )
            self._stdout.flush()
            try:
                answer = self._stdin.readline()
            except KeyboardInterrupt as error:
                raise WizardCancelled from error
            if answer == "":
                raise WizardCancelled
            answer = answer.strip()
            if not answer:
                if matching:
                    return (
                        selected
                        if selected in matching
                        else matching[0]
                    )
                continue
            if answer.casefold() in {"b", "back", "esc"}:
                return BACK
            if searchable and answer.startswith("/"):
                query = answer[1:].casefold()
                matching = tuple(
                    index
                    for index, option in enumerate(options)
                    if query in option.search_text()
                )
                if not matching:
                    self._stdout.write("No matches.\n")
                elif len(matching) == 1:
                    return matching[0]
                continue
            try:
                ordinal = int(answer)
            except ValueError:
                continue
            if 1 <= ordinal <= len(matching):
                return matching[ordinal - 1]

    @staticmethod
    def _clip(value: str, width: int) -> str:
        if len(value) <= width:
            return value
        if width < 4:
            return value[:width]
        return value[: width - 1] + "…"


def _runtime_catalog_port(paths: Mapping[str, Path]) -> int:
    return load_management_endpoint(Path(paths["data"])).port


def _runtime_catalog_attester(
    paths: Mapping[str, Path],
    expected_port: int,
) -> Callable[[int], None]:
    def attest(client_port: int) -> None:
        endpoint = load_management_endpoint(Path(paths["data"]))
        if endpoint.port != expected_port:
            raise RoutingError("CLIProxyAPI port changed during discovery")
        attest_owned_connection(endpoint, client_port)

    return attest


def _matched_context(
    projects: Mapping[str, object],
    launch_dir: Path,
) -> Mapping[str, object]:
    resolved = resolve_control_plane_context(projects, launch_dir)
    route = resolved.get("route")
    if not isinstance(route, Mapping):
        raise RoutingError("current directory has no project context")
    matched = Path(str(route["contextRootReal"])).resolve(strict=False)
    contexts = projects.get("contexts")
    if not isinstance(contexts, list):
        raise RoutingError("projects document is invalid")
    for context in contexts:
        if not isinstance(context, Mapping):
            continue
        raw_root = context.get("root")
        if not isinstance(raw_root, str):
            continue
        root = Path(raw_root).expanduser().resolve(strict=False)
        if root == matched:
            return context
    raise RoutingError("matched project context disappeared")


def _logical_live_model(
    snapshot: StackSnapshot,
    choice: LiveModelChoice,
) -> str:
    for model, definition in snapshot.stacks.models.items():
        if (
            definition.family == choice.family
            and definition.routes.get(choice.provider) == choice.upstream
        ):
            return model
    return choice.upstream


def _recommended_choice(
    snapshot: StackSnapshot,
    catalog: LiveCatalog,
    scope: str,
) -> LiveModelChoice:
    preferences = _RECOMMENDED_MODELS[scope]

    def rank(choice: LiveModelChoice) -> tuple[int, str, str]:
        model = _logical_live_model(snapshot, choice)
        try:
            preferred = preferences.index(model)
        except ValueError:
            try:
                preferred = preferences.index(choice.upstream)
            except ValueError:
                preferred = len(preferences)
        return preferred, choice.provider, choice.upstream

    if not catalog.choices:
        raise RoutingError("no compatible live model is available")
    return min(catalog.choices, key=rank)


def _stack_is_live_compatible(
    snapshot: StackSnapshot,
    catalog: LiveCatalog,
    stack_name: str,
) -> bool:
    stack = snapshot.stacks.stacks[stack_name]

    def candidate_is_live(candidate: StackCandidate) -> bool:
        definition = snapshot.stacks.models.get(candidate.model)
        if definition is None:
            return False
        return any(
            choice.family == definition.family
            and choice.provider in candidate.providers
            and definition.routes.get(choice.provider) == choice.upstream
            for choice in catalog.choices
        )

    return all(
        any(candidate_is_live(candidate) for candidate in candidates)
        for candidates in (stack.controller, *stack.agents.values())
    )


def build_recommended_stack(
    snapshot: StackSnapshot,
    catalog: LiveCatalog,
    stack_name: str = "recommended",
) -> NormalizedStacks:
    """Build one deterministic live-compatible stack without account pins."""
    document = serialize_model_stacks(snapshot.stacks)
    raw_models = document["models"]
    raw_stacks = document["stacks"]
    if not isinstance(raw_models, dict) or not isinstance(raw_stacks, dict):
        raise RoutingError("model stack configuration is invalid")
    default_is_live = _stack_is_live_compatible(
        snapshot,
        catalog,
        snapshot.stacks.default_stack,
    )
    existing = snapshot.stacks.stacks.get(stack_name)
    if existing is not None:
        if _stack_is_live_compatible(snapshot, catalog, stack_name):
            if default_is_live:
                return snapshot.stacks
            document["defaultStack"] = stack_name
            return normalize_model_stacks(document)
        raise RoutingError(
            f"stack {stack_name} already exists with another definition"
        )

    def candidate(scope: str) -> dict[str, object]:
        choice = _recommended_choice(snapshot, catalog, scope)
        model = _logical_live_model(snapshot, choice)
        definition = raw_models.get(model)
        if definition is None:
            raw_models[model] = {
                "family": choice.family,
                "routes": {choice.provider: choice.upstream},
            }
        else:
            if (
                not isinstance(definition, dict)
                or definition.get("family") != choice.family
                or not isinstance(definition.get("routes"), dict)
            ):
                raise RoutingError(
                    f"model {model} conflicts with live family"
                )
            definition["routes"].setdefault(
                choice.provider, choice.upstream
            )
        return {
            "id": candidate_id(stack_name, scope, 0, model),
            "model": model,
            "providers": [choice.provider],
        }

    raw_stacks[stack_name] = {
        "controller": [candidate("controller")],
        "agents": {
            role: [candidate(role)]
            for role in ROLES
        },
    }
    if not default_is_live:
        document["defaultStack"] = stack_name
    updated = normalize_model_stacks(document)
    return updated


def create_recommended_stack(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    launch_dir: Path,
) -> str:
    """Persist and assign a recommended stack for one configured context."""
    del config
    config_root = Path(paths["config"])
    model_path = config_root / "model-stacks.json"
    binding_path = config_root / "stack-bindings.json"
    port = _runtime_catalog_port(paths)
    attest = _runtime_catalog_attester(paths, port)
    stack_name = "recommended"
    with control_plane_transaction(config_root):
        with stack_binding_transaction(binding_path):
            current = load_control_plane(default_config_paths(config_root))
            snapshot = load_stack_snapshot(model_path, binding_path)
            accounts = load_accounts(config_root / "accounts.json")
            validate_account_bindings(
                accounts, current.documents["providers"]
            )
            resolved = resolve_control_plane_context(
                current.documents["projects"], Path(launch_dir)
            )
            route = resolved.get("route")
            if not isinstance(route, Mapping):
                raise RoutingError("current directory has no configured scope")
            normal_scope = route.get("scope") == "normal"
            context = (
                route
                if normal_scope
                else _matched_context(current.documents["projects"], Path(launch_dir))
            )
            pools = context.get("accountPools")
            if not isinstance(pools, list) or not all(
                isinstance(pool, str) for pool in pools
            ):
                raise RoutingError("project account pools are invalid")
            eligible = tuple(
                account
                for account in accounts
                if account.state == "active" and account.pool in pools
            )
            catalog = project_live_catalog(
                fetch_live_catalog(port, attest=attest),
                eligible,
                snapshot.stacks.models,
                current.documents["providers"],
            )
            updated = build_recommended_stack(
                snapshot, catalog, stack_name
            )
            proposed = ResolvedConfig(
                documents={
                    **current.documents,
                    "model-stacks": serialize_model_stacks(updated),
                },
                sources=current.sources,
            )
            validate_control_plane(proposed)
            validate_stack_assignment(
                stack_name,
                context,
                updated,
                snapshot.bindings,
                accounts,
                current.documents["providers"],
                catalog,
            )
            if updated is not snapshot.stacks:
                save_stack(snapshot, updated, snapshot.bindings)
            if normal_scope:
                raw_pools = current.documents["providers"].get("accountPools")
                if not isinstance(raw_pools, Mapping):
                    raise RoutingError("provider account pools are invalid")
                configure_normal_scope(
                    config_root / "projects.json",
                    model_stack=stack_name,
                    account_pools=pools,
                    known_stacks=updated.stacks,
                    known_pools=raw_pools,
                )
            else:
                assign_stack_to_context(
                    config_root / "projects.json",
                    Path(launch_dir),
                    stack_name,
                    updated.stacks,
                )
    return stack_name


def run_stack_wizard(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    launch_dir: Path,
    *,
    assignment_default: bool = False,
) -> int:
    """Run one interactive proposal and persist only its confirmed result."""
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "ERROR: stack configuration requires an interactive terminal",
            file=sys.stderr,
        )
        return 2

    config_root = Path(paths["config"])
    model_path = config_root / "model-stacks.json"
    binding_path = config_root / "stack-bindings.json"
    snapshot = load_stack_snapshot(model_path, binding_path)
    accounts = load_accounts(config_root / "accounts.json")
    validate_account_bindings(accounts, config.documents["providers"])
    port = _runtime_catalog_port(paths)
    attest = _runtime_catalog_attester(paths, port)
    latest: LiveCatalog | None = None

    def refresh() -> LiveCatalog:
        nonlocal latest
        latest = project_live_catalog(
            fetch_live_catalog(port, attest=attest),
            accounts,
            snapshot.stacks.models,
            config.documents["providers"],
        )
        return latest

    initial = refresh()
    wizard = StackWizard(
        snapshot,
        initial,
        accounts,
        TerminalWizardIO(),
        refresh_catalog=refresh,
        projects=config.documents["projects"],
        assignment_default=assignment_default,
    )
    result = wizard.run(Path(launch_dir))
    if not result.save:
        print("No changes saved.")
        return 0

    matched: Path | None = None
    while True:
        missing: _MissingChoice | None = None
        current_catalog: LiveCatalog | None = None
        retry_catalog: LiveCatalog | None = None
        retry_projects: Mapping[str, object] | None = None
        with control_plane_transaction(config_root):
            with stack_binding_transaction(binding_path):
                current = load_control_plane(
                    default_config_paths(config_root)
                )
                current_accounts = load_accounts(
                    config_root / "accounts.json"
                )
                validate_account_bindings(
                    current_accounts, current.documents["providers"]
                )
                proposed = ResolvedConfig(
                    documents={
                        **current.documents,
                        "model-stacks": serialize_model_stacks(
                            result.stacks
                        ),
                    },
                    sources=current.sources,
                )
                validate_control_plane(proposed)
                if result.stack_name in result.stacks.stacks:
                    current_catalog = project_live_catalog(
                        fetch_live_catalog(port, attest=attest),
                        current_accounts,
                        result.stacks.models,
                        current.documents["providers"],
                    )
                    missing = wizard.missing_live_choice(
                        result, current_catalog
                    )
                    if missing is not None:
                        retry_catalog = current_catalog
                        retry_projects = current.documents["projects"]
                if missing is None:
                    if result.assign_current_project:
                        if current_catalog is None:
                            raise RoutingError(
                                "assignment live catalogue is unavailable"
                            )
                        context = _matched_context(
                            current.documents["projects"],
                            Path(launch_dir),
                        )
                        validate_stack_assignment(
                            result.stack_name,
                            context,
                            result.stacks,
                            result.bindings,
                            current_accounts,
                            current.documents["providers"],
                            current_catalog,
                        )
                    save_stack(
                        snapshot, result.stacks, result.bindings
                    )
                    if result.assign_current_project:
                        matched = assign_stack_to_context(
                            config_root / "projects.json",
                            Path(launch_dir),
                            result.stack_name,
                            result.stacks.stacks,
                        )
        if missing is None:
            break
        if retry_catalog is None or retry_projects is None:
            raise RoutingError(
                "live availability retry state is unavailable"
            )
        result = wizard.resume_missing(
            missing,
            retry_catalog,
            Path(launch_dir),
            retry_projects,
        )
        if not result.save:
            print("No changes saved.")
            return 0

    if matched is not None:
        print(f"Saved stack {result.stack_name}.")
        print(f"Assigned project {matched}.")
    elif result.stack_name in result.stacks.stacks:
        print(f"Saved stack {result.stack_name}.")
    else:
        print(f"Deleted stack {result.stack_name}.")
    return 0
