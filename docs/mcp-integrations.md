# MCP integrations

Orichum creates a private strict MCP configuration for each physical session.
Only services relevant to the resolved project are included.

| MCP | Loaded when | Purpose |
|---|---|---|
| LeanCTX | Managed binary and project root are valid | Compact source context, graphs, task overview, durable knowledge, patches, and observational shell output |
| Atlassian | Project context has direct Jira credentials or `.orichum/config.json` selects a private named profile | Project-specific Jira read and write tools |

The LeanCTX MCP is session-scoped. It is separate from Orichum's shared
LeanCTX wire proxy, which optimizes model requests and does not expose tools.

## Project-bound Jira

Add the project context, then run the interactive Jira configuration:

```bash
orichum context add ~/xebia --pool shared
orichum context jira ~/xebia

orichum context add ~/complion --pool shared
orichum context jira ~/complion
```

The command asks for a Jira URL, username, and API token and stores them
directly in the matching entry in private
`~/.orichum/config/projects.json`. Repositories below the parent inherit that
default; a context with `atlassian: null` loads no Atlassian process or tool
schema unless a repository selects a named profile.

For repository-owned selection, define credentials only in private
`~/.orichum/config/jira-profiles.json` and set the profile name in the same
`.orichum/config.json` that controls project models:

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

A missing alias, unavailable GitHub login, malformed file, or unsafe symlink
fails closed. `null` explicitly disables the corresponding integration. No
Jira URL, username, token, or GitHub credential is copied into the repository.

The installed [mcp-atlassian](https://github.com/sooperset/mcp-atlassian)
server exposes Jira reads and writes, including create, update, comment,
delete, and transition operations. Claude Code approval and Jira permissions
still apply.

Use `orichum context list` to inspect configured Jira URLs without showing
tokens. Re-run `orichum context jira ROOT` to update credentials; submit an
empty token to keep the existing token. Use `orichum context jira ROOT
--remove` to stop loading Jira for new sessions. Start or resume a session
after a change so a fresh physical MCP process loads the current credentials.

## Isolation and approvals

- The MCP file belongs to one verified physical session.
- LeanCTX is jailed to the resolved root.
- LeanCTX project data and model cache are shared across sessions;
  configuration, events, state, and temporary probe indexes remain
  session-private.
- The project Jira profile and GitHub identity selectors are frozen into the
  verified physical-session context.
- Concurrent sessions may use different Jira credentials and repositories
  without changing global state.

LeanCTX advertises exactly eleven tools. Read, search, tree, expansion, graph,
impact, callgraph, knowledge, and overview are preapproved. `ctx_patch` and
`ctx_shell` retain normal approval because they edit text or execute commands.
`ctx_shell` remains resident for finite commands and uses a session-local empty
allowlist override, so arbitrary CLI names do not require configuration.
LeanCTX's dangerous-pattern blocks and project jail remain active. Orichum
disables LeanCTX output secret detection in its private session configuration,
so troubleshooting output is not redacted. Native Bash is deferred for
interactive, streaming, long-running, rejected, or unsupported cases.
The universal `ctx_call` gateway and LeanCTX autonomy, daemon, proxy, provider,
and global-hook features remain disabled.

Atlassian operations retain normal approval. The API token is loaded by the
session process at startup from private machine-local configuration and is not
copied into `.orichum/config.json`, the session MCP file, or controller policy.

Orichum does not use Docker MCP Toolkit. Existing Docker profiles remain
external to Orichum and are neither read, changed, nor removed.
