# LeanCTX

LeanCTX is Orichum's context optimizer on two complementary paths:

- a project-jailed MCP for compact reads, search, graphs, durable knowledge,
  anchored patches, and compressed shell output;
- one shared loopback proxy that reduces the accumulated request sent to the
  selected model on every turn.

The MCP prevents large tool results from entering the conversation. The proxy
handles context already present in the system prompt, history, and tool
results. Both use the same managed LeanCTX binary, but session MCP state stays
private while the wire proxy is shared across sessions.

## Fixed session contract

Every physical session receives one headless LeanCTX MCP with exactly:

- `ctx_read`
- `ctx_search`
- `ctx_tree`
- `ctx_expand`
- `ctx_graph`
- `ctx_impact`
- `ctx_callgraph`
- `ctx_knowledge`
- `ctx_overview`
- `ctx_patch`
- `ctx_shell`

Orichum validates that exact surface during installation and doctor checks,
then exercises a temporary one-file graph, symbol resolution, impact analysis,
task overview, and read-only knowledge recall under the production storage
topology. Missing tools or failures in those representative code-intelligence
and memory paths fail readiness instead of silently changing model behavior.

The MCP surface and the provider-resident surface are intentionally different.
Every bounded tool above remains available, while each logical session freezes
one provider-residency profile:

- `lean` is the default and keeps `ctx_read`, `ctx_search`, `ctx_tree`, and
  `ctx_shell` resident in every provider request;
- `full` keeps the previous nine-tool resident set by also retaining
  `ctx_graph`, `ctx_impact`, `ctx_callgraph`, `ctx_expand`, and `ctx_patch`.

`ctx_knowledge` and `ctx_overview` are deferred in both profiles. Other tools
outside the selected resident set remain discoverable through provider-native
tool search; they are not disabled. This reduces the always-present schema
prefix without changing LeanCTX configuration, indexing, semantic search,
cache paths, or the eleven-tool security contract.

Select a profile when starting or forking:

```bash
orichum run --leanctx-profile lean
orichum fork oc-s-0123456789abcdef --leanctx-profile full
```

New sessions default to `lean`. Resume retains the stored immutable profile.
Fork inherits its parent profile unless explicitly overridden. Historical
logical sessions created before profile persistence retain `full` behavior.

The process is pinned to the active Git repository. A launch from a configured
multi-repository parent is pinned to that verified parent. Graphs, knowledge,
archives, and aggregate statistics use one Orichum-owned shared LeanCTX data
root. Configuration, events, state, and cache remain private to the physical
session. Concurrent sessions can recall the same project knowledge without
sharing mutable session state.

## How the graph works

LeanCTX builds its index and property graph lazily when graph, impact, or
callgraph context is first requested. Orichum does not run a separate graph
command, precompute repository output, install Git refresh hooks, or write
generated graph files into the checkout.

Use the graph tools naturally through the controller:

- symbol and relationship questions route to `ctx_graph`;
- change-risk questions route to `ctx_impact`;
- callers and callees route to `ctx_callgraph`.

The controller does not choose between LeanCTX and another graph engine.

## Durable knowledge

`ctx_overview` gives the controller a task-aware project map plus a compact
wake-up briefing. `ctx_knowledge` recalls or records project-scoped facts,
decisions, conventions, outcomes, and gotchas across sessions. Automatic
capture is enabled, but the controller policy forbids storing raw source,
command output, transient speculation, or routine recaps as durable knowledge.

LeanCTX identifies a repository independently of its checkout path, preferring
its explicit project ID and Git remote identity. Clones and worktrees of the
same repository therefore reuse the same graph and knowledge store.

## Exactness and fallback

Compressed context is for understanding. Supported text edits use an anchored
`ctx_read` followed by `ctx_patch`. Use `ctx_shell` for every finite,
non-interactive shell command, independent of the CLI or whether it reads or
changes state. Use `ctx_shell(raw=true)` when exact output is required,
including decisive validation after state changes.

Orichum runs its private session MCP in LeanCTX's blocklist-only shell mode.
There is no executable-name allowlist to maintain: installed tools, custom
scripts, and future CLIs can use `ctx_shell` immediately. LeanCTX still blocks
its unconditional dangerous patterns and confines paths to the verified
project. Orichum disables LeanCTX output secret detection, so local
troubleshooting can inspect database values, PII, and credential-bearing
output without redaction; command approval remains with Claude Code. This
override is session-local and does not modify the user's global LeanCTX
configuration.

Native `Bash` is deferred and loaded only for interactive, streaming, or
long-running processes; redirects or file writes rejected by LeanCTX; or one
explicit fallback after `ctx_shell` cannot execute a command. Orichum does not
run the same command through both paths by default.

Native `Read`, `Edit`, and `Write` remain available for unsupported formats,
binary files, exact verification, or a LeanCTX failure in the controller.
Specialists use the stricter LeanCTX surface; the implementation worker retains
native edits and on-demand Bash but not a second raw repository-reading path.

Orichum disables LeanCTX's autonomous gateway, global shell hooks, daemon,
provider connectors, endpoint rewiring, and universal `ctx_call` surface. It
starts the request proxy itself with a small, validated Orichum-owned
configuration. Provider routing and credentials remain owned by the Orichum
route proxy and CLIProxyAPI.

Orichum also sets `LEAN_CTX_RULES_INJECTION=off` for every managed LeanCTX
process. The controller and project policy already provide the required
steering, so injecting LeanCTX-authored rule files again would add a duplicate
prompt prefix without adding capabilities or safety controls.

## Monitor savings

From a project:

```bash
orichum leanctx stats
orichum leanctx economics
orichum leanctx watch
orichum leanctx dashboard
```

`stats` prints two measurements:

- **Session MCP** shows source tokens processed and returned by the selected
  physical run.
- **Shared wire proxy** shows cumulative requests, bytes, and estimated tokens
  removed across all Orichum sessions since that shared proxy started.

`economics` reports four scopes without combining them:

- **Selected-session provider footprint** derives resident and deferred schema
  tokens from the logical session's frozen profile and LeanCTX tool health.
- **Shared rolling compression** aggregates timestamped source, returned,
  saved-token, and estimated avoided-USD records from the global savings
  ledger.
- **Shared rolling recorded prompt-cache estimates** aggregates cache-read
  volume and the upstream estimated cache discount recorded in that ledger.
- **LeanCTX all-time upstream estimate** shows gross saved tokens, injected
  overhead, net token estimate, turns, tool spend, and ROI from `lean-ctx gain`.

The rolling window defaults to 24 hours and can be set from 1 through 168:

```bash
orichum leanctx economics --hours 48
orichum leanctx economics --session oc-s-0123456789abcdef
```

Inside a live session, the logical ID defaults from `ORICHUM_SESSION_ID`.
Outside one, pass `--session`. Orichum uses the newest attached physical run for
that logical session's project to obtain the isolated LeanCTX environment.

All dollar values are upstream estimates, not provider invoices. The rolling
sections are shared across every Orichum project and are not selected-session
totals. The cache section includes only timestamped ledger records and may not
cover every provider request. The all-time summary has a different attribution
scope and fallback pricing, so Orichum never presents it as rolling-window
billing or combines it into a fabricated rolling net. ROI is shown as a dash
when the upstream summary has no recorded tool spend.

LeanCTX can append a negative `bounce` record when a previously estimated
compression saving must be corrected. Orichum includes that signed correction
in rolling compression USD only when the record matches LeanCTX's official
bounce shape; ordinary compression and caching records must remain
nonnegative. The ledger is validated read-only and is never repaired or
rewritten by Orichum.

`watch` opens the selected run's terminal observatory, and `dashboard` starts
its authenticated local web observatory in the foreground. They use that run's
private events together with LeanCTX's shared project ledger, so their aggregate
savings can exceed the selected run's `stats` row. Stop either with Ctrl+C.
They show MCP activity; use `stats` for the shared wire counters. A dash means
the relevant path has not observed measurable input yet.

Select a physical run when needed:

```bash
orichum leanctx list
orichum leanctx stats --run run.mrds3ghq
orichum leanctx dashboard --run run.mrds3ghq --port 3341 --open none
```

Without `--run`, Orichum uses the current attached run or the newest run for
the current project. It does not cross project boundaries or substitute an
older run merely because it has more activity.

`list` hides incompatible historical physical runs by default. Use `--all`
when diagnosing an older session contract.

## Verify

```bash
orichum doctor
```

Doctor performs a real MCP handshake with the managed binary and verifies the
eleven-tool contract against an isolated temporary fixture. It also verifies
the owned shared proxy, its loopback listener, and the complete route through
CLIProxyAPI. It does not index your project or launch a model session.
