# Troubleshooting

Start with:

```bash
orichum doctor
orichum config paths
orichum context list
orichum stack list
orichum provider accounts
```

## Installation paths look scattered or runtime is stale

`orichum config paths` should report `~/.orichum` as the home and data root,
with configuration and cache below it. `orichum doctor` also verifies that the
launcher and services use the content-addressed release selected by
`~/.orichum/runtime/current`, not the Git checkout.

If both an old XDG root and the matching path below `ORICHUM_HOME` exist, the
installer stops instead of merging ambiguous data. Preserve both directories,
decide which one is authoritative, and move the other aside before rerunning
the installer. For an intentional non-default layout, set `ORICHUM_HOME`
consistently for install and use `orichum config paths` to confirm it.

## Bound routes are not live

The selected stack references a provider/model route that CLIProxyAPI is not
currently advertising. Confirm provider login, account status, pool membership,
and live models:

```bash
orichum stack available
orichum stack show STACK
orichum provider accounts
orichum models resolve STACK
```

Create or update the stack with `orichum stack configure`. Existing logical
sessions keep their frozen routes.

## Inference gateway connection refused

This means the physical session could not reach its private Claudex translator
or one of the resident loopback services. Run `orichum doctor`, then inspect
service logs. Restart through a fresh `orichum` or `orichum resume SESSION_ID`
after the health check passes.

Linux and WSL logs:

```bash
journalctl --user -u orichum-cliproxy.service
journalctl --user -u orichum-leanctx-proxy.service
journalctl --user -u orichum-route-proxy.service
```

## API error while the gateway reports HTTP 200

An HTTP 200 records that the gateway accepted the response headers; it does not
prove that the complete streaming response reached Claude Code. Inspect the
route-proxy log for `route-retry`, `route-complete`, and `route-failed` events:

```bash
journalctl --user -u orichum-route-proxy.service
```

Correlate components with the opaque `requestId` field or the
`X-Orichum-Request-ID` response header. A failure with stage `before-output`
may use the session's frozen fallback. A failure with stage `after-output` is
reported without replay because output or a tool request may already have
reached the client. The events contain route identifiers and byte counts but no
request body, prompt, credential, or provider token.

## Wrong GitHub identity

Confirm the context and authenticated accounts:

```bash
orichum context list
gh auth status --hostname github.com
```

Set the expected identity with `orichum context update ROOT
--github-account ACCOUNT`. New physical sessions receive an isolated
`GH_CONFIG_DIR`; the global active account is not switched.

## Missing Jira tools

Atlassian is intentionally conditional. Check the project entry:

```bash
orichum context list
orichum doctor
```

If the context shows no Jira URL, run `orichum context jira ROOT`. If the URL
or username is wrong, rerun the same command; submit an empty token to retain
the existing token. Start a fresh physical session afterward; existing session
MCP files are immutable.

Orichum does not read or switch Docker MCP profiles. A Docker Toolkit profile
therefore cannot supply Jira tools to an Orichum session.

## Missing LeanCTX MCP

LeanCTX is included for every configured project, including a multi-repository
parent such as `~/xebia`. Run `orichum leanctx list`; the default view contains
only attached runs. Use `orichum leanctx list --all` to inspect incompatible
historical runs. If a required session says `ATTACHED no`, rerun `./install.sh`
and start a new physical session; existing session MCP files are immutable.
`orichum doctor` also verifies that the product-managed controller policy is
current and that LeanCTX advertises exactly Orichum's eleven allowed tools.
Orichum does not depend on a global LeanCTX setup or shell hook.

## `ctx_shell` rejects a troubleshooting command

New Orichum sessions disable LeanCTX's executable-name allowlist inside their
private MCP. Arbitrary finite CLI names therefore need no manual registration.
The user's global LeanCTX configuration is not changed.

LeanCTX can still reject an unconditional dangerous pattern, a path-jail
violation, file-writing shell syntax, or an unsupported execution mode. Use
the deferred native Bash lane once for that bounded fallback; use it directly
for interactive, streaming, or long-running commands. If an ordinary CLI name
is rejected in a newly created session, rerun `./install.sh`, start a fresh
physical session, and confirm `orichum doctor` passes.

## LeanCTX has no activity or graph results

Confirm that the current physical run is attached:

```bash
orichum leanctx list
orichum leanctx stats
orichum doctor
```

For a status-line problem, inspect the same logical session outside the Claude
interface:

```bash
orichum sessions
orichum status SESSION_ID
```

If `orichum status` works but the in-session line is absent, restart that
physical session after reinstalling. If both fail, `orichum doctor` reports
whether the isolated Claude settings, renderer, or route telemetry endpoint is
unavailable.

If `--all` shows `ATTACHED no`, reinstall and start a new physical session;
existing session MCP files are immutable. If the run is attached but has no
events, the model has not called a LeanCTX tool. Graph and impact indexes are
built lazily, so an unused session correctly reports zero activity.

`orichum leanctx watch` and `dashboard` display the selected session MCP, so
they can remain quiet while native tools or external MCPs are in use. Run
`orichum leanctx stats` to see both that session's MCP counters and the shared
wire-proxy counters. If the shared request count does not increase after a new
model turn, run `orichum doctor` and inspect the LeanCTX and route-proxy logs.

If a graph or impact call fails, verify that Orichum was launched from the
intended repository or configured parent. Start a new session from the narrower
repository root when a multi-repository parent produces too much scope.

## Prior project knowledge is missing

Confirm that the current run is attached and that Orichum was launched from
the intended repository:

```bash
orichum leanctx list
orichum doctor
```

LeanCTX scopes knowledge by project identity. Repositories with the same Git
remote share durable knowledge even when cloned elsewhere. Unrelated
repositories and configured parent directories remain separate.

## Installer port conflict

The installer reuses only verified Orichum-owned listeners. It does not replace
an unknown service. Interactive installation offers another port;
non-interactive installation selects the next available port and persists it.
