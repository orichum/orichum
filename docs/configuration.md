# Configuration

For normal interactive changes, run `orichum configure` from the target project.
The complete guided account, backup, model, role, project, review, repair, and
Advanced flows are documented in
[Installation, setup, and configuration](setup-and-configuration.md).

Orichum exposes several focused files as one validated control plane. Edit the
installed copies shown by:

```bash
orichum config paths
```

| File | Responsibility |
|---|---|
| `model-stacks.json` | Models, families, controller candidates, and specialist role candidates |
| `providers.json` | Provider adapters, auth types, pools, and family route order |
| `projects.json` | Parent paths, stack overrides, pools, GitHub identities, and optional Jira credentials |
| `plugins.json` | Optional Claude Code marketplaces and plugins |
| `runtime.json` | Controller effort, tool concurrency, and session subagent limit |
| `controller-policy.md` | Product-managed sole-writer and deterministic tool-routing policy |
| `accounts.json` | Private named-account registry managed by provider commands |
| `stack-bindings.json` | Private machine-local named-account locks |

A project may also commit `.orichum/models.json`. This deliberately narrow file
contains only the controller logical model and the five specialist logical
models. It cannot contain providers, accounts, credentials, pools, fallbacks,
commands, or tools. The referenced logical models must already exist in the
machine-local `model-stacks.json`.

The nearest valid project file between the launch directory and matched context
root overrides only model assignment for fresh sessions. Machine-local account
availability, provider routes, and credentials still control how those models
are reached. Because repository write access grants model-selection authority,
review changes to this file like other executable-development configuration.

Jira is the deliberate local exception to credential references:
`orichum context jira ROOT` stores its URL, username, and token together on
that project entry in private `projects.json`. The installed file is mode
`0600`, contains the raw token, and must not be committed, shared, or copied
into a repository. `orichum config show` redacts `apiToken`; `orichum context
list` shows only the Jira URL. This keeps each project's Jira identity
independent without another account registry.

`projects.json`, `accounts.json`, `stack-bindings.json`, authentication data,
and session state are private machine-local files and must not be committed.
The project-local `.orichum/models.json` is the only repository configuration
exception and contains no secrets.

The installer preserves user-managed JSON configuration. Reconciliation
normalizes `projects.json` to the current schema while preserving active
routing fields. It always refreshes `controller-policy.md` from the checked-out
Orichum release. Do not edit the installed policy directly: a stale or modified
copy is rejected, and the next installer run restores the release policy.

Validate after an edit:

```bash
orichum config show
orichum config validate
orichum models validate
orichum models resolve
orichum context validate
```

All normal mutable files live below `~/.orichum`. Set `ORICHUM_HOME` to one
absolute private path before installation when the complete layout must live
elsewhere. `ORICHUM_CONFIG_HOME`, `ORICHUM_DATA_HOME`, and
`ORICHUM_CACHE_HOME` are advanced, fine-grained overrides; using them splits
the otherwise consolidated layout. Logical sessions always remain below the
selected data root so the CLI and resident route service resolve the same
state.

Prefer `orichum stack configure`, `orichum provider account`,
`orichum context`, `orichum context jira`, and `orichum plugin`
commands over direct machine-local JSON editing. Edit repository
`.orichum/models.json` directly when a project intentionally owns its simple
role-to-model mapping.

Orichum's private LeanCTX MCP uses blocklist-only shell execution so arbitrary
finite CLIs work without changing `~/.config/lean-ctx/config.toml`. Project
jailing, secret redaction, dangerous-pattern blocking, and Claude Code command
approval remain active. Project-bound sessions keep private configuration and
state while reusing shared data and cache directories. They use two indexing
threads, a 12% soft process-RSS target, and lazy automatic download of the local
MiniLM semantic model. Orichum does not prewarm or explicitly build the semantic
index.
