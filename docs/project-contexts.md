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

A nested repository can keep all of its simple Orichum choices in one
`.orichum/config.json`. Discovery starts at the canonical launch directory,
walks upward to and including the matched context root, and uses the nearest
file. Files above the context root are ignored.

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

Both service values may be `null`, which explicitly disables that integration.
The file cannot contain URLs, usernames, tokens, credential paths, commands,
environment values, account pools, provider routes, or fallback policy.

Named Jira credentials remain private in
`~/.orichum/config/jira-profiles.json`. A selected alias must exist there or
the launch fails closed. The GitHub account must already be available through
`gh auth`; Orichum creates an isolated account-specific `GH_CONFIG_DIR`.

Legacy `.orichum/models.json` remains supported for model assignments only.
It cannot coexist with `config.json` in the same directory and does not replace
machine-local Jira or GitHub defaults.

`orichum context jira ROOT` remains the compatible machine-local default. Start
or resume a session after changing credentials or `.orichum/config.json` so a
fresh physical process receives the current binding.

See [Memory and code intelligence](memory-and-code-graph.md) for LeanCTX
project identity, worktrees, and shared durable knowledge.
