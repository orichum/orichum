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
A newly created logical session then freezes its selected primary route and at
most one compatible fallback.

If the launch path resolves a valid `.orichum/config.json`, a fresh logical
session uses its repository model mapping before freezing routes and its Jira
and GitHub account names for the physical launch. Later edits or deletion do
not alter the stored logical model route.

## Resume

```bash
orichum resume SESSION_ID
```

`SESSION_ID` may be the `oc-s-…` logical ID shown by `orichum sessions` or the
Claude session UUID printed when Claude Code exits. Orichum resolves either form
to the same frozen logical session.

Resume loads the stored Orichum context again, verifies its integrity, and
preserves the original model/account binding and Claude session identity. It
does not silently move to another family after configuration changes.

## Fork

Use a fork to change stack or model family while carrying only an explicit,
bounded handoff:

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
intent: Orichum resolves that named machine-local stack instead. Resume always
keeps the original frozen routes.
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
