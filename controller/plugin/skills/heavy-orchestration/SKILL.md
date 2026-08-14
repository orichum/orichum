---
name: heavy-orchestration
description: Adaptively route independent investigation, review, or high-impact cross-checking to an audited read-only Orichum Workflow when it can add distinct evidence. Do not use when the controller already has sufficient evidence and no independent workflow can add non-duplicative value.
when_to_use: Use when the controller's current evidence indicates that independent investigation or review would materially improve the result.
user-invocable: false
---

# Heavy orchestration router

The main model remains controller and writer. Select exactly one saved
read-only workflow without asking the user to choose:

- Investigation, competing hypotheses, or evidence gathering:
  call Workflow with scriptPath
  "${CLAUDE_PLUGIN_ROOT}/audited-workflows/investigate.js" and structured args
  {question, scope, highRisk}.
- Review, cross-checking, or consistency checking:
  call Workflow with scriptPath
  "${CLAUDE_PLUGIN_ROOT}/audited-workflows/review.js" and structured args
  {subject, scope, highRisk}.

Set highRisk true only for security, authentication, concurrency, migration,
irreversible architecture, or conflicting evidence with material impact.
Otherwise set it false.

Before invoking a workflow, the controller must collect live cloud or
remote-service evidence with its own authorized tools and pass a bounded
summary in the workflow subject/question and scope. Do not ask repository
agents to collect live evidence; they are intentionally restricted to supplied
material and repository reads.

Never call Workflow by inline script, name, external path, generated path, or
user-supplied path. Never launch both scripts for one task. Never place a
writer in a Workflow. After the result returns, the main model synthesizes it
and performs any authorized edits. Treat `status` as authoritative: disclose
`degraded` or `failed` results and their `missingAgents` instead of presenting
partial evidence as a complete workflow.
