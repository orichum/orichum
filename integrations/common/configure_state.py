#!/usr/bin/env python3
"""Read-only state and draft projection for guided configuration."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from .account_registry import Account, load_accounts, validate_account_bindings
from .model_routing import ROLES, RoutingError
from .orichum_config import ResolvedConfig
from .project_models import resolve_project_context
from .stack_bindings import StackBindings
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
from .stack_store import load_stack_snapshot

ROLE_ORDER = ("controller", *ROLES)
ROLE_LABELS = MappingProxyType(
    {
        "controller": "Controller",
        "repository-explorer": "Repository explorer",
        "repository-verifier": "Repository verifier",
        "correctness-critic": "Correctness critic",
        "architecture-advisor": "Architecture advisor",
        "planning-advisor": "Planning advisor",
        "implementation-worker": "Implementation worker",
    }
)
WORK_TYPES = MappingProxyType(
    {
        "Controller": ("controller",),
        "Research": ("repository-explorer", "repository-verifier"),
        "Review": ("correctness-critic",),
        "Architecture": ("architecture-advisor", "planning-advisor"),
        "Implementation": ("implementation-worker",),
    }
)


@dataclass(frozen=True)
class ProjectTarget:
    root: Path
    stack_name: str
    pools: tuple[str, ...]


@dataclass(frozen=True)
class ModelSelection:
    model: str
    family: str
    provider: str
    upstream: str = field(repr=False)
    account_ids: tuple[str, ...] = field(repr=False)
    account_names: tuple[str, ...]


RoleAssignment = ModelSelection


@dataclass(frozen=True)
class AccountPlan:
    primary: Account = field(repr=False)
    backup: Account | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PendingAccount:
    provider: str
    credential_ref: str = field(repr=False)
    name: str
    pool: str
    priority: int
    intent: str
    primary_id: str | None = field(default=None, repr=False)
    primary_name: str | None = None


@dataclass(frozen=True)
class ConfigurationSnapshot:
    target: ProjectTarget
    accounts: tuple[Account, ...] = field(repr=False)
    catalog: LiveCatalog
    stacks: NormalizedStacks = field(repr=False)
    bindings: StackBindings = field(repr=False)
    assignments: Mapping[str, ModelSelection]
    launch_root: Path | None = None
    project_models_path: Path | None = None
    project_models_digest: str | None = field(default=None, repr=False)
    project_models_checked: bool = False
    project_services_managed: bool = False
    jira_profile: str | None = None
    github_account: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "assignments",
            MappingProxyType(dict(self.assignments)),
        )
        if self.launch_root is None:
            object.__setattr__(self, "launch_root", self.target.root)


@dataclass(frozen=True)
class ConfigurationDraft:
    project: ProjectTarget
    role_models: Mapping[str, ModelSelection]
    account_plans: Mapping[str, AccountPlan] = field(
        default_factory=dict,
        repr=False,
    )
    pending_accounts: tuple[PendingAccount, ...] = ()
    binding_removals: tuple[str, ...] = field(default=(), repr=False)
    profile_switch: str | None = None
    changed: bool = False

    def __post_init__(self) -> None:
        unknown = set(self.role_models) - set(ROLE_ORDER)
        if unknown:
            raise RoutingError("configuration draft has unknown roles")
        object.__setattr__(
            self,
            "role_models",
            MappingProxyType(dict(self.role_models)),
        )
        object.__setattr__(
            self,
            "account_plans",
            MappingProxyType(dict(self.account_plans)),
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ConfigurationSnapshot,
    ) -> ConfigurationDraft:
        return cls(
            project=snapshot.target,
            role_models=snapshot.assignments,
        )

    def with_roles(
        self,
        roles: Sequence[str],
        selection: ModelSelection,
    ) -> ConfigurationDraft:
        updated = dict(self.role_models)
        for role in roles:
            if role not in ROLE_ORDER:
                raise RoutingError("configuration draft has unknown roles")
            updated[role] = selection
        return ConfigurationDraft(
            project=self.project,
            role_models=updated,
            account_plans=self.account_plans,
            pending_accounts=self.pending_accounts,
            binding_removals=self.binding_removals,
            profile_switch=None,
            changed=True,
        )

    def with_pending_account(
        self,
        pending: PendingAccount,
    ) -> ConfigurationDraft:
        return ConfigurationDraft(
            project=self.project,
            role_models=self.role_models,
            account_plans=self.account_plans,
            pending_accounts=(*self.pending_accounts, pending),
            binding_removals=self.binding_removals,
            profile_switch=self.profile_switch,
            changed=True,
        )

    def with_project(self, project: ProjectTarget) -> ConfigurationDraft:
        return ConfigurationDraft(
            project=project,
            role_models=self.role_models,
            account_plans=self.account_plans,
            pending_accounts=self.pending_accounts,
            binding_removals=self.binding_removals,
            profile_switch=None,
            changed=project != self.project or self.changed,
        )

    def with_profile(
        self,
        project: ProjectTarget,
        role_models: Mapping[str, ModelSelection],
    ) -> ConfigurationDraft:
        profile_changed = project != self.project or dict(role_models) != dict(
            self.role_models
        )
        return ConfigurationDraft(
            project=project,
            role_models=role_models,
            account_plans=self.account_plans,
            pending_accounts=self.pending_accounts,
            binding_removals=self.binding_removals,
            profile_switch=(
                project.stack_name if profile_changed else self.profile_switch
            ),
            changed=profile_changed or self.changed,
        )

    def with_binding_removals(
        self,
        candidates: Sequence[str],
    ) -> ConfigurationDraft:
        return ConfigurationDraft(
            project=self.project,
            role_models=self.role_models,
            account_plans=self.account_plans,
            pending_accounts=self.pending_accounts,
            binding_removals=tuple(
                dict.fromkeys((*self.binding_removals, *candidates))
            ),
            profile_switch=self.profile_switch,
            changed=True,
        )


@dataclass(frozen=True)
class ConfigurationReview:
    project: Path
    account_rows: tuple[tuple[str, str], ...]
    model_rows: tuple[tuple[str, str], ...]
    session_notice: str


@dataclass(frozen=True)
class CatalogueDrift:
    invalid_roles: tuple[str, ...]


def managed_stack_name(project_root: Path) -> str:
    canonical = str(Path(project_root).expanduser().resolve(strict=False))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"orichum-project-{digest}"


def compatible_backup_accounts(
    snapshot: ConfigurationSnapshot,
    primary: Account,
) -> tuple[Account, ...]:
    if primary.state != "active":
        return ()
    used_routes = tuple(
        selection
        for selection in snapshot.assignments.values()
        if (
            selection.provider == primary.provider
            and primary.id in selection.account_ids
        )
    )
    compatible = []
    for account in snapshot.accounts:
        if (
            account.id == primary.id
            or account.state != "active"
            or account.provider != primary.provider
        ):
            continue
        if used_routes and all(
            account.id in selection.account_ids for selection in used_routes
        ):
            compatible.append(account)
    return tuple(
        sorted(
            compatible,
            key=lambda account: (-account.priority, account.name, account.id),
        )
    )


def stack_is_live_compatible(
    snapshot: ConfigurationSnapshot,
    stack_name: str,
) -> bool:
    stack = snapshot.stacks.stacks.get(stack_name)
    if stack is None:
        return False

    def candidate_is_live(candidate: object) -> bool:
        definition = snapshot.stacks.models.get(candidate.model)
        if definition is None:
            return False
        bound_account = snapshot.bindings.candidate_accounts.get(candidate.id)
        return any(
            choice.family == definition.family
            and choice.provider == provider
            and choice.upstream == definition.routes.get(provider)
            and bool(choice.account_ids)
            and (bound_account is None or bound_account in choice.account_ids)
            for provider in candidate.providers
            for choice in snapshot.catalog.choices
        )

    return all(
        any(candidate_is_live(candidate) for candidate in candidates)
        for candidates in (stack.controller, *stack.agents.values())
    )


def _live_assignment(
    candidates: Sequence[StackCandidate],
    stacks: NormalizedStacks,
    bindings: StackBindings,
    catalog: LiveCatalog,
) -> ModelSelection:
    for candidate in candidates:
        definition = stacks.models[candidate.model]
        bound_account = bindings.candidate_accounts.get(candidate.id)
        for provider in candidate.providers:
            upstream = definition.routes.get(provider)
            if upstream is None:
                continue
            for choice in catalog.choices:
                if (
                    choice.family == definition.family
                    and choice.provider == provider
                    and choice.upstream == upstream
                    and (bound_account is None or bound_account in choice.account_ids)
                ):
                    if bound_account is None:
                        account_ids = choice.account_ids
                        account_names = choice.account_names
                    else:
                        matching = [
                            (identifier, name)
                            for identifier, name in zip(
                                choice.account_ids,
                                choice.account_names,
                                strict=True,
                            )
                            if identifier == bound_account
                        ]
                        account_ids = tuple(item[0] for item in matching)
                        account_names = tuple(item[1] for item in matching)
                    return ModelSelection(
                        model=candidate.model,
                        family=definition.family,
                        provider=provider,
                        upstream=upstream,
                        account_ids=account_ids,
                        account_names=account_names,
                    )
    candidate = candidates[0]
    definition = stacks.models[candidate.model]
    provider = candidate.providers[0]
    return ModelSelection(
        model=candidate.model,
        family=definition.family,
        provider=provider,
        upstream=definition.routes[provider],
        account_ids=(),
        account_names=(),
    )


def selections_for_stack(
    snapshot: ConfigurationSnapshot,
    stack_name: str,
) -> Mapping[str, ModelSelection]:
    stack = snapshot.stacks.stacks.get(stack_name)
    if stack is None:
        raise RoutingError("model profile is unavailable")
    selections = {
        "controller": _live_assignment(
            stack.controller,
            snapshot.stacks,
            snapshot.bindings,
            snapshot.catalog,
        )
    }
    for role in ROLES:
        selections[role] = _live_assignment(
            stack.agents[role],
            snapshot.stacks,
            snapshot.bindings,
            snapshot.catalog,
        )
    return MappingProxyType(selections)


def selection_for_choice(
    snapshot: ConfigurationSnapshot,
    choice: LiveModelChoice,
) -> ModelSelection:
    model = choice.upstream
    for logical, definition in snapshot.stacks.models.items():
        if (
            definition.family == choice.family
            and definition.routes.get(choice.provider) == choice.upstream
        ):
            model = logical
            break
    return ModelSelection(
        model=model,
        family=choice.family,
        provider=choice.provider,
        upstream=choice.upstream,
        account_ids=choice.account_ids,
        account_names=choice.account_names,
    )


def recommended_selections(
    snapshot: ConfigurationSnapshot,
) -> Mapping[str, ModelSelection]:
    from .stack_wizard import _RECOMMENDED_MODELS

    selections: dict[str, ModelSelection] = {}
    for role in ROLE_ORDER:
        preferences = _RECOMMENDED_MODELS[role]

        def rank(choice: LiveModelChoice) -> tuple[int, str, str]:
            selection = selection_for_choice(snapshot, choice)
            for value in (selection.model, selection.upstream):
                try:
                    return (
                        preferences.index(value),
                        choice.provider,
                        choice.upstream,
                    )
                except ValueError:
                    continue
            return (len(preferences), choice.provider, choice.upstream)

        if not snapshot.catalog.choices:
            raise RoutingError("no compatible live model is available")
        selections[role] = selection_for_choice(
            snapshot,
            min(snapshot.catalog.choices, key=rank),
        )
    return MappingProxyType(selections)


def build_managed_stack(
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> NormalizedStacks:
    stack_name = managed_stack_name(draft.project.root)
    if (
        stack_name in snapshot.stacks.stacks
        and snapshot.target.stack_name != stack_name
    ):
        raise RoutingError(
            "the project-managed stack name is already used by another stack"
        )
    missing = [role for role in ROLE_ORDER if role not in draft.role_models]
    if missing:
        raise RoutingError("configuration draft is missing model roles")
    document = serialize_model_stacks(snapshot.stacks)
    raw_models = document["models"]
    raw_stacks = document["stacks"]
    if not isinstance(raw_models, dict) or not isinstance(raw_stacks, dict):
        raise RoutingError("model stack configuration is invalid")

    def raw_candidate(role: str) -> dict[str, object]:
        selection = draft.role_models[role]
        definition = raw_models.get(selection.model)
        if definition is None:
            raw_models[selection.model] = {
                "family": selection.family,
                "routes": {selection.provider: selection.upstream},
            }
        elif (
            not isinstance(definition, dict)
            or definition.get("family") != selection.family
            or not isinstance(definition.get("routes"), dict)
        ):
            raise RoutingError(f"model {selection.model} conflicts with its live route")
        else:
            routes = definition["routes"]
            observed = routes.get(selection.provider)
            if observed not in {None, selection.upstream}:
                raise RoutingError(
                    f"model {selection.model} conflicts with its live route"
                )
            routes[selection.provider] = selection.upstream
        return {
            "id": candidate_id(stack_name, role, 0, selection.model),
            "model": selection.model,
            "providers": [selection.provider],
        }

    raw_stacks[stack_name] = {
        "controller": [raw_candidate("controller")],
        "agents": {role: [raw_candidate(role)] for role in ROLES},
    }
    return normalize_model_stacks(document)


def review_draft(
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
) -> ConfigurationReview:
    account_rows = []
    for pending in draft.pending_accounts:
        label = pending.provider.title()
        if pending.intent == "backup" and pending.primary_name:
            account_rows.append((f"{label} primary", pending.primary_name))
            account_rows.append((f"{label} backup", pending.name))
        else:
            account_rows.append((f"{label} {pending.intent}", pending.name))
    for provider, plan in sorted(draft.account_plans.items()):
        account_rows.append((f"{provider.title()} primary", plan.primary.name))
        if plan.backup is not None:
            account_rows.append((f"{provider.title()} backup", plan.backup.name))
    if not account_rows:
        visible = {
            account.name
            for selection in draft.role_models.values()
            for account in snapshot.accounts
            if account.id in selection.account_ids
        }
        account_rows.extend(("Available", name) for name in sorted(visible))
    model_rows = tuple(
        (ROLE_LABELS[role], draft.role_models[role].model)
        for role in ROLE_ORDER
        if role in draft.role_models
    )
    return ConfigurationReview(
        project=draft.project.root,
        account_rows=tuple(account_rows),
        model_rows=model_rows,
        session_notice=(
            "Changes apply to new sessions. Existing sessions are unchanged."
        ),
    )


def revalidate_draft(
    snapshot: ConfigurationSnapshot,
    draft: ConfigurationDraft,
    refreshed_catalog: LiveCatalog,
) -> CatalogueDrift:
    del snapshot
    available = {
        (choice.family, choice.provider, choice.upstream)
        for choice in refreshed_catalog.choices
        if choice.account_ids
    }
    invalid = tuple(
        role
        for role in ROLE_ORDER
        if role in draft.role_models
        and (
            draft.role_models[role].family,
            draft.role_models[role].provider,
            draft.role_models[role].upstream,
        )
        not in available
    )
    return CatalogueDrift(invalid_roles=invalid)


def load_configuration_snapshot(
    paths: Mapping[str, Path],
    config: ResolvedConfig,
    project: Path,
) -> ConfigurationSnapshot:
    config_root = Path(paths["config"])
    stack_snapshot = load_stack_snapshot(
        config_root / "model-stacks.json",
        config_root / "stack-bindings.json",
    )
    accounts = load_accounts(config_root / "accounts.json")
    provider_document = config.documents["providers"]
    validate_account_bindings(accounts, provider_document)
    resolved, project_models = resolve_project_context(
        config.documents["projects"],
        Path(project),
        config_root / "jira-profiles.json",
        stack_snapshot.stacks,
    )
    route = resolved.get("route")
    if not isinstance(route, Mapping) or route.get("scope") == "normal":
        raise RoutingError("configuration requires a project context")
    pools = route.get("accountPools")
    if not isinstance(pools, list) or not all(isinstance(pool, str) for pool in pools):
        raise RoutingError("project account availability is invalid")
    eligible = tuple(
        account
        for account in accounts
        if account.state == "active" and account.pool in pools
    )
    from .stack_wizard import (  # Avoid a module-level wizard dependency.
        _runtime_catalog_attester,
        _runtime_catalog_port,
    )

    launch_root = Path(str(resolved.get("launchDirReal", project)))
    effective_stacks = (
        project_models.stacks if project_models is not None else stack_snapshot.stacks
    )
    effective_bindings = (
        StackBindings({}) if project_models is not None else stack_snapshot.bindings
    )
    port = _runtime_catalog_port(paths)
    catalog = project_live_catalog(
        fetch_live_catalog(
            port,
            attest=_runtime_catalog_attester(paths, port),
        ),
        eligible,
        effective_stacks.models,
        provider_document,
    )
    if project_models is not None:
        stack_name = project_models.stack_name
    else:
        stack_name = route.get("modelStack")
        if stack_name is None:
            stack_name = effective_stacks.default_stack
        elif not isinstance(stack_name, str):
            raise RoutingError("project model stack is invalid")
    stack = effective_stacks.stacks.get(stack_name)
    if stack is None:
        raise RoutingError("project model stack is unavailable")

    def assignment(role: str) -> ModelSelection:
        candidates = stack.controller if role == "controller" else stack.agents[role]
        return _live_assignment(
            candidates,
            effective_stacks,
            effective_bindings,
            catalog,
        )

    assignments = MappingProxyType({role: assignment(role) for role in ROLE_ORDER})
    return ConfigurationSnapshot(
        target=ProjectTarget(
            root=Path(str(route["contextRootReal"])),
            stack_name=stack_name,
            pools=tuple(pools),
        ),
        accounts=eligible,
        catalog=catalog,
        stacks=effective_stacks,
        bindings=effective_bindings,
        assignments=assignments,
        launch_root=launch_root,
        project_models_path=(
            project_models.path if project_models is not None else None
        ),
        project_models_digest=(
            project_models.digest if project_models is not None else None
        ),
        project_models_checked=True,
        project_services_managed=(
            project_models.manages_services if project_models is not None else False
        ),
        jira_profile=route.get("jiraProfile"),
        github_account=route.get("githubAccount"),
    )
