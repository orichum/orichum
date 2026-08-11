# Model stacks

A model stack assigns a controller model and ordered candidates for specialist
roles. It separates task roles from provider credentials, allowing the same
workflow to use GPT, Claude, Google, Kimi, or another declared family.

## Configure models for a project

```bash
cd ~/projects/my-app
orichum configure
```

Choose **Models**. The guided flow reads the owned live CLIProxyAPI catalogue
and offers a recommended setup, one model everywhere, models by work type,
per-role customization, or another saved profile. Model and provider IDs are
selected from numbered, searchable choices rather than typed.

The final preview names every concrete role and states that changes apply only
to new sessions. Live availability is checked again immediately before saving.

## Project-local JSON mapping

A repository can keep its controller and specialist assignments in
`.orichum/models.json` instead of changing them through the wizard:

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
  }
}
```

The file must contain exactly those five agent roles. Every value is a logical
model ID already declared by the machine's private `model-stacks.json`.
Providers, accounts, pools, credentials, fallback policy, candidate lists,
commands, and tools cannot be specified in the repository file.

Orichum searches from the canonical launch directory toward the matched
machine-local context root, includes that root, and uses the nearest file. It
never searches above the context boundary. An unsafe, malformed, oversized,
symlinked, or unknown-model mapping fails closed instead of falling back to a
parent file or machine stack.

The mapping is converted into a one-session in-memory stack. It is never
written into private `model-stacks.json` or `projects.json`, and persisted
named-account locks do not attach to its synthetic candidates. Machine-local
provider routes, context account pools, active accounts, and live-route checks
remain authoritative.

When this file exists, `orichum configure` shows its absolute path and keeps the
Models action read-only; edit the JSON file directly. Account setup and health
checks remain available. `orichum models resolve` from the project reports the
file as the effective source. A fresh session adopts edits; resume and a fork
without `--stack` keep the parent's frozen routes, while an explicit
`orichum fork --stack` uses the requested machine-local stack.

The advanced stack wizard remains available for ordered startup candidates and
named-account locks:

```bash
orichum stack available
orichum stack configure
```

For each role, the advanced wizard lets you choose a live model and either:

- select automatically within one provider; or
- lock the route to one named account.

The final review validates the routes again before saving. The wizard can then
assign the stack to the longest matching project context for the current
directory.

## Inspect and validate

```bash
orichum stack list
orichum stack show STACK
orichum models list
orichum models stacks
orichum models resolve
orichum models resolve STACK
orichum models validate
```

Candidates in a role are ordered startup choices. Runtime fallback is separate:
session creation freezes an exact primary route and at most one compatible
fallback.

Machine-local reusable stack definitions live in `model-stacks.json`, and
machine-local named account locks live privately in `stack-bindings.json`.
The optional repository `.orichum/models.json` contains only direct logical
model assignments. Editing either source does not mutate existing sessions;
start a fresh session to use the changed definition.

The standard roles are controller, repository explorer, repository verifier,
correctness critic, architecture advisor, and implementation worker. Runtime
policy decides whether specialists are needed; defining them does not cause
automatic fan-out on every task.

## Shipped balanced stack

| Role | Ordered default | Why |
|---|---|---|
| Controller | GPT-5.6 Sol | Primary coordinator and sole writer |
| Repository explorer | GPT-5.6 Terra | Efficient bounded reconnaissance |
| Repository verifier | GPT-5.6 Terra | Independent read-only verification |
| Correctness critic | Claude Sonnet 5 through Anthropic | Strong routine review without paying Opus cost |
| Architecture advisor | Claude Opus 5 through Anthropic, then Claude Opus 4.6 Thinking through Antigravity | Highest configured architecture model per provider |
| Implementation worker | GPT-5.6 Sol | Strong execution inside an explicit ownership boundary |

Ordered candidates are evaluated only while creating a session. For example,
the architecture advisor uses Anthropic Opus 5 when that route is live and uses
the declared Antigravity candidate only when the first candidate cannot be
bound. The selected route is then frozen with the session; Orichum does not
silently change the model of an existing session.

These defaults are ordinary entries in `model-stacks.json`. Edit that file or
run `orichum stack configure` to change them; no agent definition needs to be
rewritten.
