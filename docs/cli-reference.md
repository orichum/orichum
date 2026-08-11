# CLI reference

Run `orichum --help` for the command map and
`orichum COMMAND [SUBCOMMAND] --help` for the authoritative options installed
on the current machine.

Installer modes are separate from the `orichum` command:

```bash
./install.sh
./install.sh --verbose
# Install completely when fresh; otherwise reconcile verified local state.

./install.sh --upgrade
# Check upstream releases, upgrade managed tools, and run the full doctor.

./install.sh --uninstall
# Remove managed runtimes while preserving accounts, sessions, and project data.

./install.sh --uninstall --purge
# Also permanently remove Orichum's saved configuration and private data.
```

`orichum --version` prints a plain version only for an exact clean release tag.
Development builds add `+g.COMMIT`; `.dirty` means a declared runtime payload
file differed from that commit. Builds without Git metadata add
`+src.DIGEST`, using the content-addressed runtime digest.

| Command | Purpose |
|---|---|
| `orichum --version` | Print the installed Orichum release identity |
| `orichum completion zsh\|bash\|fish` | Print the native completion definition for one shell |
| `orichum setup [--verbose] [PROJECT]` | Resume first-run provider, runtime, automatic stack, projects-folder, and readiness setup |
| `orichum configure [--project PROJECT] [--verbose]` | Guide ongoing account, backup, model, agent-role, project, and repair configuration; defaults to the current project |
| `orichum` / `orichum run [--leanctx-profile lean\|full]` | Start a project-aware session; new sessions default to the lean provider-residency profile |
| `orichum config show` | Show the merged, redacted control plane |
| `orichum config validate` | Validate focused configuration |
| `orichum config paths` | Print the consolidated home, configuration, data, cache, and state paths |
| `orichum context list` | Show configured parent-directory contexts |
| `orichum context add ROOT [--model-stack STACK] [--pool POOL] [--github-account ACCOUNT]` | Add a parent-directory context; repeat `--pool` for ordered pools |
| `orichum context jira ROOT [--url URL] [--username USER]` | Prompt for and save Jira credentials directly on a project context |
| `orichum context jira ROOT --remove` | Remove Jira from a project context |
| `orichum context update ROOT ...` | Replace pools or GitHub identity; set or inherit a stack |
| `orichum context remove ROOT` | Remove a context mapping |
| `orichum context validate` | Validate all configured project contexts |
| `orichum models list` | List declared models |
| `orichum models stacks` | List configured stacks |
| `orichum models resolve [STACK]` | Resolve the effective project mapping or an explicit machine-local stack |
| `orichum models validate` | Validate model routing |
| `orichum stack available` | Show live provider/model choices |
| `orichum stack configure` | Create or edit a stack interactively |
| `orichum stack list` | List stacks |
| `orichum stack show STACK` | Inspect roles, providers, and account policy |
| `orichum provider configure` | Log in and register one provider account interactively |
| `orichum provider login TYPE` | Authenticate a provider through CLIProxyAPI |
| `orichum provider list` | List configured provider adapters and model families |
| `orichum provider accounts` | List named accounts |
| `orichum provider account add NAME PROVIDER CREDENTIAL_FILE POOL [--priority VALUE]` | Register a credential without using the wizard |
| `orichum provider account rename ACCOUNT NAME` | Change an account's display name |
| `orichum provider account priority ACCOUNT VALUE` | Set an alias or numeric priority |
| `orichum provider account enable ACCOUNT` / `orichum provider account disable ACCOUNT` | Change account availability |
| `orichum provider account remove ACCOUNT` | Remove an account's registry entry |
| `orichum provider account sync [ACCOUNT]` | Reconcile one or all registered credentials |
| `orichum plugin list` | List declared optional plugins |
| `orichum plugin add PLUGIN@MARKETPLACE [--source SOURCE]` | Declare and install a plugin |
| `orichum plugin sync` / `orichum plugin update` | Reconcile or refresh declared plugins |
| `orichum plugin remove PLUGIN@MARKETPLACE` | Uninstall and remove a declaration |
| `orichum leanctx list [--limit N \| --all]` | List attached LeanCTX runs; include incompatible historical runs with `--all` |
| `orichum leanctx stats [--run RUN]` | Show session MCP and shared wire-proxy savings |
| `orichum leanctx economics [--session SESSION] [--hours HOURS]` | Show selected-session schema footprint, rolling ledger estimates, and LeanCTX's all-time estimate |
| `orichum leanctx watch [--run RUN]` | Open LeanCTX's live terminal monitor |
| `orichum leanctx dashboard [--run RUN] [--port PORT] [--open MODE]` | Open the local authenticated LeanCTX Observatory |
| `orichum doctor` | Validate local component ownership, configuration, protocols, and service health |
| `orichum status [ID]` | Show the selected session's current model, named account, route state, and quota windows |
| `orichum sessions [--limit N \| --all]` | List recent logical sessions |
| `orichum sessions cleanup [--older-than DAYS] [--yes]` | Preview or remove inactive physical launch snapshots |
| `orichum sessions remove ID [--yes]` | Preview or remove one inactive leaf logical session |
| `orichum sessions clear [--yes]` | Preview or remove all inactive logical sessions |
| `orichum session routes ID` / `orichum sessions routes ID` | Inspect a session's frozen routes |
| `orichum resume ID` | Resume by Orichum logical ID or Claude session UUID |
| `orichum fork ID [--stack STACK] [--handoff-file FILE] [--leanctx-profile lean\|full]` | Create a child session; inherit the parent LeanCTX profile unless explicitly overridden |

When `STACK` is omitted, `orichum models resolve` checks the current project for
`.orichum/models.json` and includes its absolute `source` path when active. An
explicit `STACK` bypasses the repository mapping and resolves that named
machine-local stack.

Forward ordinary Claude Code arguments after `--`, for example:

```bash
orichum run -- -p "Summarize this repository"
```

Orichum rejects model, session, workspace, MCP, plugin, effort, tool-approval,
and permission-mode options because those are bound by its validated control
plane.

## Shell completion

The installer generates native zsh, Bash, and fish definitions. Completion
covers the full command tree, options, fixed choices, files and directories,
and safe local identifiers for stacks, providers, login types, account pools,
accounts, plugins, marketplaces, project contexts, logical sessions, and
physical runs. It reads only local configuration/state fields and never emits
credential contents.

Plugin installation completes declared plugin IDs directly and completes the
marketplace portion after `@`, for example `sample@off` to
`sample@official`. Option values also complete in both `--stack balanced` and
`--stack=balanced` forms.

Definitions can also be generated directly:

```bash
orichum completion zsh
orichum completion bash
orichum completion fish
```

Orichum stops completing its own grammar when a forwarded argument begins.
For example, arguments after `orichum run --` belong to Claude Code and are not
completed by Orichum.

## LeanCTX monitoring

```bash
orichum leanctx list
orichum leanctx list --all
orichum leanctx stats
orichum leanctx economics
orichum leanctx economics --session oc-s-0123456789abcdef --hours 48
orichum leanctx watch --run run.mrds3ghq
orichum leanctx dashboard --open browser
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

Inside a live session, monitoring uses that physical run. Otherwise it uses the
newest physical run for the current project, regardless of whether it has
recorded activity. `--run RUN` selects an ID explicitly. Implicit selection
never crosses project boundaries and never substitutes an older active run.
`list` shows up to 20 attached runs by default. Use `--limit N` to change that
bound or `--all` to include every attached and historical incompatible run.

`stats` has two sections. **Session MCP** compares source tokens processed by
LeanCTX with tokens returned to the model for the selected physical run.
**Shared wire proxy** reports cumulative request compression across all Orichum
sessions since the shared proxy started. These are optimizer counters, not
provider billing, prompt-cache, reasoning, or output-token totals. A dash means
that path has not observed measurable input yet.

`economics` resolves a logical session from `--session` or, inside a live
session, `ORICHUM_SESSION_ID`. It selects the newest attached physical run for
that session's project and reports four deliberately separate sections:

- the frozen `lean` or `full` provider-residency footprint;
- global compression records from the shared savings ledger in the last 24
  hours by default;
- global timestamped prompt-cache records in the same rolling window;
- LeanCTX's official all-time gain and injected-overhead estimate.

`--hours` accepts `1` through `168`. Rolling dollar figures are upstream
estimates from recorded ledger entries, not invoices. Prompt-cache records may
not cover every provider request. Both rolling sections are shared across all
Orichum projects rather than attributed to the selected session. The all-time
estimate has a different scope, so Orichum does not calculate or display a
rolling net-billing figure. ROI is shown as a dash when LeanCTX has no recorded
tool spend from which to calculate it.

`--port PORT` requests a specific loopback port. When omitted, Orichum selects
the first available port starting at `3333`. `--open` accepts `browser`,
`none`, or `vscode` and defaults to `browser`. The dashboard always binds to
`127.0.0.1`, keeps bearer-token authentication enabled, runs in the foreground,
and stops with Ctrl+C.

Physical `run.*` IDs refer to one isolated LeanCTX runtime. Logical `oc-s-*`
IDs refer to resumable Orichum sessions; use those with `resume`, `fork`, and
`status` or `session routes`. Inside a live session, `orichum status` uses
`ORICHUM_SESSION_ID`; from another shell, pass the logical ID explicitly.

`orichum run` must receive `--leanctx-profile` before the forwarded argument
separator. New sessions default to `lean`; `resume` retains the immutable
stored profile, and `fork` inherits it unless an override is supplied.
