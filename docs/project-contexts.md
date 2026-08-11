# Project contexts

A project context maps a parent directory to its model stack, account pools,
GitHub identity, and optional Jira configuration.

The parent does not need to be a Git repository. Launches from nested
repositories inherit the configured containing context.

## Add a context

```bash
orichum context add ~/xebia \
  --github-account athevar-xebia
orichum context jira ~/xebia

orichum context add ~/personal --pool shared
orichum context add ~/work --model-stack balanced \
  --pool xebia --pool shared
```

Jira is optional. Adding a context validates the directory and saves the
mapping immediately—there is no repository mining or population step. LeanCTX
builds live source indexes, graphs, and project knowledge lazily as sessions
use them. Repeat `--pool` to set an ordered fallback list. Omit `--model-stack`
to inherit the configured default stack.

## Maintain contexts

```bash
orichum context list
orichum context validate
orichum context update ~/personal \
  --pool shared --github-account arvind9981
orichum context jira ~/personal --remove
orichum context update ~/personal --inherit-model-stack --no-github-account
orichum context remove ~/personal
orichum context remove ~/personal --yes
```

Repositories added below a configured parent inherit the mapping
automatically. No context refresh command or Git hook is required.

A nested repository can override only its controller and specialist logical
models with `.orichum/models.json`. Discovery starts at the canonical launch
directory, walks upward to and including the matched context root, and uses the
nearest file. Files above the context root are ignored. A worktree inside the
context uses its own nearer file; a worktree outside every configured context
remains unmapped.

This repository file does not create a second project context and cannot change
account pools, GitHub, Jira, providers, credentials, or fallback policy. Those
settings continue to come from the machine-local context.

`orichum context jira ROOT` writes the URL, username, and token directly into
the matching entry in private machine-local `projects.json`. There is no
separate Jira account registry. Every new physical session freezes whether
Jira is available and starts the MCP with the current project credentials.
Rerun the command to rotate credentials; an empty token keeps the existing
token. Start or resume a session afterward to create a fresh physical MCP
process.

When `githubAccount` is configured, Orichum creates an isolated
account-specific `GH_CONFIG_DIR` from an existing `gh auth` login. Concurrent
projects therefore do not change the machine-wide active GitHub account.

See [Memory and code intelligence](memory-and-code-graph.md) for LeanCTX
project identity, worktrees, and shared durable knowledge.
