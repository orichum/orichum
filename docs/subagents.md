# Subagents

Orichum uses a subagent-driven workflow without requiring manual workflow
commands. The controller routes adaptively from the current evidence, task
scope, uncertainty, consequence, and the value of an independent perspective.

## Policy

- Routing is evidence-driven: use the smallest suitable specialist whenever it
  can add distinct evidence or challenge the controller's current conclusion.
- Keep work inline only when the controller has sufficient evidence and no
  specialist can add non-duplicative value.
- Do not use model family, provider, keywords, file counts, or a predetermined
  agent count as a routing rule.
- Verification uses a separate verifier when a change needs independent review.
- Correctness and architecture specialists are reserved for relevant risk.
- Claude Code's built-in `Plan` and `Explore` requests are routed to audited
  Orichum roles instead of being allowed as generic agents.
- The controller remains the sole writer and synthesizes all findings.
- Generic agents and arbitrary workflows are denied.
- Ultra effort is not the default; controller effort is high.

The installed controller plugin contains audited agent definitions and saved
workflow scripts. A `PreToolUse` hook rejects undeclared agent types and
arbitrary workflow bodies or names. This prevents a model from bypassing the
declared roles while keeping specialist reasoning and tool access intact.

Every specialist reuses the session's project-jailed LeanCTX MCP:

- explorers, verifiers, critics, and architects receive only bounded read,
  search, tree, expansion, graph, impact, and callgraph tools;
- the implementation worker also receives anchored patching, `ctx_shell` for
  every finite command, native edits, and deferred Bash for interactive,
  streaming, long-running, rejected, or unsupported commands;
- project overview and durable knowledge remain controller-owned, avoiding
  repeated orientation calls and concurrent memory writes;
- raw native read/search tools are not exposed to specialists, so repository
  context does not silently bypass compression.

## Built-in agent aliases

The orchestration hook preserves the original task input while replacing the
built-in agent type:

- `Plan` becomes `orichum-controller:planning-advisor`;
- `Explore` becomes `orichum-controller:repository-explorer`.

The planning advisor is a bounded, read-only, non-delegating role for routine
implementation and operational planning. It has a dedicated planner route and
uses LeanCTX tools, returning validation, rollback, stop conditions, and
remaining uncertainty. Unknown generic agent types remain denied.

## Compaction continuity

After manual or automatic compaction, Orichum writes a private checkpoint in
the session run directory. It records the compact summary, repository HEAD and
dirty state, and only the type and description of successfully completed Agent
calls. Prompts and agent results are not copied into the checkpoint.

When Claude Code restarts the same session with `source=compact`, Orichum adds a
short continuity directive. If repository state is unchanged, completed
investigations must not be repeated. If it changed, only the changed boundary
should be revalidated. The full compact summary is not injected a second time.

This checkpoint is private, bounded, and transient session state. It is not
durable LeanCTX knowledge and must not be promoted to project memory.

Session materialization and resume verify this tool contract together with each
role's frozen model. A modified or outdated agent definition is rejected before
the session continues.

Runtime limits live in `runtime.json`:

```json
{
  "controller": {
    "effort": "high",
    "maxToolUseConcurrency": 3,
    "maxSubagentsPerSession": 24
  }
}
```

These values bound fan-out and concurrency; they do not truncate a worker's
response. Defining a specialist in a model stack makes it available whenever
adaptive routing finds that it can add non-duplicative value.
