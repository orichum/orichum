# Orichum

**Pronounced:** *OR-ih-kum*, following *orichalcum*.

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

> Run Claude Code with the models, accounts, project tools, memory, and
> specialist agents that fit each project.

Orichum is an independent harness for Claude Code. You continue working inside
Claude Code while Orichum prepares the right model stack, provider account,
project context, and optional tools for the directory you launched from.

Once setup is complete, daily use is simply:

```bash
cd ~/projects/my-app
orichum
```

## What Orichum does

- Runs GPT, Claude, Google, Kimi, and other configured models inside the
  familiar Claude Code interface.
- Lets you choose models and named provider accounts while handling safe
  same-family account recovery automatically.
- Loads the right GitHub identity, project tools, memory, and code graph from
  the directory where you start it.
- Keeps concurrent and resumed sessions isolated, while the controller chooses
  relevant context and specialist agents for each task.

You do not need to understand the routing architecture before using Orichum.
Start with one provider and one project; add the other capabilities only when
you need them.

## Install

Orichum supports macOS, Linux with a systemd user manager, and WSL2 with
systemd enabled.

```bash
curl -fsSL https://raw.githubusercontent.com/orichum/orichum/main/bootstrap.sh | bash
```

The bootstrap installs missing prerequisites, including Claude Code, Codex CLI,
uv, jq, and the host tools Orichum requires. It keeps its checkout at
`~/.local/share/orichum`; rerunning the same command updates it and reconciles
the installation.

The installer prepares the complete local runtime. Without a provider account,
it completes safely in `pending-provider-login` state and prints one next
command:

```bash
orichum setup
```

That resumable setup asks for the provider, account name, and projects folder
(default `~/projects`). It registers the first account as Primary in Orichum's
internal `shared` group, creates a compatible recommended model stack, maps the
projects folder, creates `.orichum/config.json` there for direct project
configuration, reconciles services, and runs the final health check. Orichum
installs its runtime and private user-managed state under `~/.orichum`. A
repository may intentionally commit one `.orichum/config.json` containing only
its model choices and Jira/GitHub account names; credentials remain outside the
repository. Moving the checkout does not break the installed command.
Later, rerun the same bootstrap command to update Orichum and reconcile the
installation. Add `--upgrade` to refresh managed tools and run their complete
probes; the full doctor check follows once a provider route is available. See
[Installation and upgrades](docs/installation.md) for details, locations, port
handling, and uninstall options.

Check the installed Orichum release with `orichum --version`. See the
[Changelog](CHANGELOG.md) for release history and current release-candidate
limitations.

To remove the Orichum runtime while keeping accounts, sessions, project
configuration, and LeanCTX project knowledge for a later reinstall:

```bash
~/.local/share/orichum/install.sh --uninstall
```

Use `~/.local/share/orichum/install.sh --uninstall --purge` only when you also
want to permanently remove Orichum's saved configuration and data.

## Your first Orichum session

Run the guided setup after installation:

```bash
orichum setup
```

Setup asks only for provider, account name, and projects folder. It hides
credential filenames, account groups, priorities, and the internal distinction
between the Codex login type and the OpenAI provider adapter. If setup is
interrupted, run the same command again; completed phases are shown as already
configured and are not repeated. Use `orichum setup --verbose` only when you
need live technical diagnostics; normal setup remains concise.

When setup reports readiness, enter a repository below the configured parent
and launch:

```bash
cd ~/projects/my-app
orichum
```

Orichum resolves the project, prepares an isolated session with the selected
model, account, and relevant tools, then opens Claude Code. From this point,
work normally; the controller decides when project context or a configured
specialist is useful.

The Orichum status line keeps the active model, named account, route state,
context usage, and available provider limits visible. See
[Status line](docs/status-line.md).

If launch fails, start with:

```bash
orichum doctor
```

For ongoing changes, use one guided command from the project you want to
change:

```bash
orichum configure
```

It adds accounts, configures a same-provider backup, discovers live models,
assigns models by work type or individual role, previews the complete effect,
and applies it to new sessions. Use `orichum configure --project ROOT` when you
are administering another configured project. The low-level provider, stack,
and context commands remain available under the guided Advanced area and for
automation.

A project can keep its simple choices in one visible `.orichum/config.json`:

```json
{
  "schemaVersion": 1,
  "controller": "gpt-5.6-terra",
  "agents": {
    "repository-explorer": "gpt-5.6-terra",
    "repository-verifier": "gpt-5.6-terra",
    "correctness-critic": "gpt-5.6-terra",
    "architecture-advisor": "gpt-5.6-sol",
    "implementation-worker": "gpt-5.6-sol"
  },
  "jiraProfile": null,
  "githubAccount": "alupao"
}
```

Edit this file directly to change project models or account names. It never
contains Jira tokens, GitHub tokens, provider credentials, or routing policy.

## Daily use

| What you want to do | Command |
|---|---|
| Start in the current project | `orichum` |
| Configure accounts, backups, models, or project settings | `orichum configure` |
| Edit project models and Jira/GitHub account names | `.orichum/config.json` |
| Check project mappings | `orichum context list` |
| Configure project Jira | `orichum context jira ROOT` |
| Remove project Jira | `orichum context jira ROOT --remove` |
| List or inspect stacks | `orichum stack list` / `orichum stack show STACK` |
| Check named accounts | `orichum provider accounts` |
| List sessions | `orichum sessions` |
| Inspect a session's live status | `orichum status SESSION_ID` |
| Inspect a session's routes | `orichum session routes SESSION_ID` |
| Resume a session | `orichum resume SESSION_ID` |
| Remove one session from Orichum | `orichum sessions remove SESSION_ID` |
| Clear inactive sessions from Orichum | `orichum sessions clear` |
| Monitor context savings | `orichum leanctx stats`, `orichum leanctx watch`, or `orichum leanctx dashboard` |
| Check the installation | `orichum doctor` |
| Show the active home, config, cache, and state paths | `orichum config paths` |
| Update or reconcile Orichum | Rerun the [bootstrap command](#install); add `--upgrade` for a complete managed-tool refresh |

The complete command map is in the [CLI reference](docs/cli-reference.md).

## Add capabilities when you need them

- **More accounts:** use `orichum configure` to add a named account or an
  explicit same-provider backup without entering pools or priorities. See
  [Multi-account routing](docs/multi-account-usage.md).
- **More model families:** use `orichum configure` to authenticate another
  provider and choose its live models for controller or specialist work. See
  [Model stacks](docs/model-stacks.md).
- **Resumes and family changes:** resume a frozen session or fork it with a
  bounded handoff onto another stack. See [Sessions](docs/sessions.md).
- **Memory and code intelligence:** LeanCTX recalls durable decisions, reads
  live source, and answers structural or impact questions. See
  [Memory and code graph](docs/memory-and-code-graph.md).
- **Live source context:** LeanCTX gives the controller compact reads, search,
  trees, lossless expansion, approved text patches, and compressed output from
  arbitrary finite CLIs. Specialists reuse the same jailed context engine
  instead of falling back to raw repository reads. A shared LeanCTX wire proxy
  also trims growing conversation history before each request reaches the
  provider.
  See [LeanCTX](docs/leanctx.md).
- **Plugins:** declare and synchronize optional Claude Code plugins through
  Orichum. See [Plugins](docs/plugins.md).
- **Jira:** keep credentials private and select a named Jira profile through the
  project's `.orichum/config.json`. Orichum loads `mcp-atlassian` only for
  sessions that resolve a Jira binding. See
  [MCP integrations](docs/mcp-integrations.md).
- **Specialist agents:** let the controller delegate bounded exploration,
  review, architecture, or implementation work while keeping one writer. See
  [Subagents](docs/subagents.md).

## How Orichum fits together

```mermaid
flowchart LR
    P["Project directory"] --> O["Orichum"]
    O --> S["Isolated Claude Code session"]
    M["Selected account and model"] -. "frozen route" .-> S
    S --> L["LeanCTX context and knowledge"]
    S --> W["Shared LeanCTX wire optimization"]
    S -. "only for a bound project" .-> D["mcp-atlassian"]
```

The directory where you run `orichum` selects the project configuration. When
present, `.orichum/config.json` supplies the project's models and Jira/GitHub
account names. Orichum opens a private session using those choices and exposes
only the relevant project context; you do not manually route each request.

Read [Architecture](docs/architecture.md) for service ownership, security
boundaries, session isolation, and the internal request path.

## If something is wrong

Run the bounded health check first:

```bash
orichum doctor
```

Then inspect the part of the setup involved:

```bash
orichum config paths
orichum provider accounts
orichum stack list
orichum context list
orichum sessions
orichum leanctx stats
```

The [Troubleshooting guide](docs/troubleshooting.md) covers unavailable routes,
connection failures, GitHub identity, missing MCPs, LeanCTX activity,
historical session contracts, and installer port conflicts.

## Documentation

| Guide | Use it for |
|---|---|
| [Installation, setup, and configuration](docs/setup-and-configuration.md) | Complete guided journey from installation through first-run setup and ongoing project configuration |
| [Installation and upgrades](docs/installation.md) | Platforms, prerequisites, locations, ports, services, and upgrades |
| [Providers and accounts](docs/providers-and-accounts.md) | Login, credentials, account names, pools, and priorities |
| [Multi-account routing](docs/multi-account-usage.md) | Multiple accounts from the same or different providers |
| [Model stacks](docs/model-stacks.md) | Interactive model selection, roles, and provider locks |
| [Project contexts](docs/project-contexts.md) | Directory mappings, identities, account pools, and Jira bindings |
| [Sessions](docs/sessions.md) | Start, inspect, resume, fork, and concurrent sessions |
| [Status line](docs/status-line.md) | Active model, account, failover state, context, and quota metrics |
| [Routing and failover](docs/routing-and-failover.md) | Route selection, cooldowns, rollover, and handoff boundaries |
| [Subagents](docs/subagents.md) | Automatic delegation, specialist roles, and the sole-writer policy |
| [Plugins](docs/plugins.md) | Add, update, synchronize, inspect, and remove plugins |
| [MCP integrations](docs/mcp-integrations.md) | LeanCTX and project-bound Atlassian MCP configuration |
| [LeanCTX](docs/leanctx.md) | Compact source context, fallbacks, savings statistics, and live monitoring |
| [Memory and code intelligence](docs/memory-and-code-graph.md) | How LeanCTX combines live code context with durable project knowledge |
| [Configuration](docs/configuration.md) | Focused files, private state, and environment overrides |
| [Architecture](docs/architecture.md) | Components, request flow, ownership, and security boundaries |
| [Troubleshooting](docs/troubleshooting.md) | Symptoms, diagnostics, and recovery |
| [CLI reference](docs/cli-reference.md) | The complete command map |
| [Release readiness](docs/release-readiness.md) | End-to-end acceptance evidence, supported boundaries, and known notices |
| [Efficiency and performance](docs/efficiency-and-performance.md) | Measured savings, latency, cost, cache, and resource usage |

## Built with

### Runs on

- [Claude Code](https://code.claude.com/docs/en/overview) — the interactive
  coding host.

### Integrates

- [Claudex](https://claudex.space/en/) — translates Claude Code requests for
  the selected model.
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) — provides
  provider authentication and model access.
- [LeanCTX](https://github.com/yvgude/lean-ctx) — provides compact live file,
  tree, search, graph, callgraph, and impact context.
- [mcp-atlassian](https://github.com/sooperset/mcp-atlassian) — provides
  project-bound Jira tools from each project's private credentials.

Orichum is an independent project. It is not affiliated with or endorsed by
these upstream projects, and it integrates them without modifying their source
code.

## License

Orichum is licensed under the [Apache License 2.0](LICENSE). See
[NOTICE](NOTICE) for Orichum attribution and
[third-party notices](THIRD_PARTY_NOTICES.md) for the independent tools and
content it integrates.

## References

- [Claude Code LLM gateway configuration](https://code.claude.com/docs/en/llm-gateway)
- [Claude Code status-line configuration](https://code.claude.com/docs/en/statusline)
- [LeanCTX documentation](https://leanctx.com/docs/)
- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
- [Claudex](https://github.com/StringKe/claudex)
- [mcp-atlassian documentation](https://mcp-atlassian.soomiles.com/docs/)
- [Orichum architecture](docs/architecture.md)
