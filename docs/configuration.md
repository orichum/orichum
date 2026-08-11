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
| `projects.json` | Parent paths, stack overrides, pools, default GitHub identities, and optional legacy Jira credentials |
| `jira-profiles.json` | Private named Jira URL, username, and API-token profiles selected by repositories |
| `plugins.json` | Optional Claude Code marketplaces and plugins |
| `runtime.json` | Controller effort, tool concurrency, and session subagent limit |
| `controller-policy.md` | Product-managed sole-writer and deterministic tool-routing policy |
| `accounts.json` | Private named-account registry managed by provider commands |
| `stack-bindings.json` | Private machine-local named-account locks |

A project may commit one `.orichum/config.json` file containing its controller,
five specialist models, Jira profile name, and GitHub account name. For example:

```json
{
  "schemaVersion": 1,
  "controller": "gpt-5.6-terra",
  "agents": {
    "repository-explorer": "gpt-5.6-terra",
    "repository-verifier": "gpt-5.6-terra",
    "correctness-critic": "claude-sonnet-5",
    "architecture-advisor": "claude-opus-5",
    "implementation-worker": "gpt-5.6-sol"
  },
  "jiraProfile": "work",
  "githubAccount": "alupao"
}
```

The file contains names only. It cannot contain provider routes, credentials,
tokens, service URLs, commands, environment variables, account pools, or
fallback policy. Logical models must already exist in machine-local
`model-stacks.json`; a non-null Jira profile must exist in private
`jira-profiles.json`; and a non-null GitHub account must be logged in through
`gh auth`.

Orichum searches from the canonical launch directory to the matched context
root and uses the nearest file. An invalid nearest file fails closed. Explicit
`null` disables the corresponding Jira or GitHub integration instead of
inheriting its machine-local default. Legacy `.orichum/models.json` files remain
supported for model assignments only; they cannot coexist with `config.json`
in the same directory.

`orichum context jira ROOT` remains the compatible machine-local default and
stores its URL, username, and token in private `projects.json`. Named profiles
selected by `.orichum/config.json` live in private `jira-profiles.json`:

```json
{
  "schemaVersion": 1,
  "profiles": {
    "work": {
      "url": "https://example.atlassian.net",
      "username": "person@example.com",
      "apiToken": "..."
    }
  }
}
```

Both machine-local files are mode `0600` and must not be committed or shared.
Jira tokens and GitHub authentication never enter `.orichum/config.json`.

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
`orichum context`, `orichum context jira`, and `orichum plugin` commands over
direct machine-local JSON editing. Edit repository `.orichum/config.json`
directly when the project owns its models and service account names.

Orichum's private LeanCTX MCP uses blocklist-only shell execution so arbitrary
finite CLIs work without changing `~/.config/lean-ctx/config.toml`. Project
jailing, secret redaction, dangerous-pattern blocking, and Claude Code command
approval remain active. Project-bound sessions keep private configuration and
state while reusing shared data and cache directories. They use two indexing
threads, a 12% soft process-RSS target, and lazy automatic download of the local
MiniLM semantic model. Orichum does not prewarm or explicitly build the semantic
index.
