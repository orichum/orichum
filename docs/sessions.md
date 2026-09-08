# Sessions

Orichum distinguishes a logical session from the physical Claude Code process.
The logical session records the project, stack, model family, account route,
and Claude session identity needed for a consistent resume.

## Start and inspect

```bash
cd ~/work/project
orichum
orichum run -- -p "Summarize this repository"

orichum sessions
orichum sessions --limit 50
orichum sessions --all
orichum session routes SESSION_ID
orichum sessions routes SESSION_ID
```

The session list shows the newest 20 logical sessions by default. Use `--limit`
for a different bound or `--all` when the complete history is needed.

Use `--` after `orichum run` when forwarding Claude Code arguments. Orichum
rejects runtime options it owns, including model, session, workspace, MCP,
plugin, effort, tool-approval, and permission-mode settings.

Every launch re-resolves and validates the project context and live services.
A newly created logical session records its selected primary route and at
most one compatible fallback. Agent routes remain pinned; the controller is
refreshed from current configuration when the session resumes.

If the launch path resolves a valid `.orichum/config.json`, a fresh logical
session uses its repository model mapping before freezing routes and its Jira
and GitHub account names for the physical launch. Later edits take effect for
the controller on resume. Deleting the repository file restores the current
machine configuration as the source of the resumed controller.

## Resume

```bash
orichum resume SESSION_ID
```

`SESSION_ID` may be the `oc-s-…` logical ID shown by `orichum sessions` or the
Claude session UUID printed when Claude Code exits. Orichum resolves either form
to the same logical session and conversation.

Resume validates the workspace and resolves the controller from the current
project or machine configuration. Changing the controller model or family
preserves the logical session ID, Claude conversation UUID and transcript,
parent, LeanCTX profile, and existing agent bindings. No fork or handoff is
required. The launcher passes the new model with the original `--resume` UUID.

The new route must be configured, authenticated, and advertised by the live
catalogue. Orichum validates it and prepares the physical run before atomically
updating the saved controller. A failed validation leaves the saved session
intact. `orichum session routes SESSION_ID` shows the updated controller.

This happens when resuming; editing configuration does not switch a running
controller mid-turn. Agent model changes still require a new session or fork.

## Fork

Use a fork when you want a separate conversation or different agent bindings,
carrying only an explicit, bounded handoff:

```bash
orichum models stacks
orichum fork SESSION_ID \
  --stack TARGET_STACK \
  --handoff-file ./bounded-handoff.md
```

The parent remains resumable. The child does not receive hidden provider state
or the full parent transcript.

A fork without `--stack` inherits the parent's frozen routes and ignores the
current repository model file. Supplying `--stack` is explicit session-scoped
intent: Orichum resolves that named machine-local stack instead. Resume keeps
the agent bindings and refreshes the controller from current configuration.
Concurrent sessions use separate physical run directories, MCP files, plugin
copies, and Claudex translation ports. CLIProxyAPI, the LeanCTX wire proxy, and
the Orichum route proxy are shared, while each physical session owns its
Claudex translator.

## Remove logical sessions

Preview removal of one inactive leaf session:

```bash
orichum sessions remove SESSION_ID
```

Apply the preview:

```bash
orichum sessions remove SESSION_ID --yes
```

`SESSION_ID` may be an Orichum logical ID or its Claude session UUID. A parent
cannot be removed while it still has child sessions.

To clear every inactive logical session, preview and then apply:

```bash
orichum sessions clear
orichum sessions clear --yes
```

Clear preserves active sessions and any parents they still reference. Removing
a logical record removes its frozen Orichum route and therefore its
`orichum resume` entry. It does not delete Claude Code's underlying transcript,
LeanCTX project knowledge, or physical launch snapshots.

## Clean old physical runs

Logical sessions remain resumable, but each launch also creates a disposable
physical snapshot. Preview inactive snapshots older than seven days:

```bash
orichum sessions cleanup
```

Remove only the runs shown by that preview:

```bash
orichum sessions cleanup --yes
```

Use `--older-than DAYS` to change the minimum age. Cleanup never removes
logical session records and skips a run while its Claudex translator port is
live.
