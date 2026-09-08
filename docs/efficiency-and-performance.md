# Efficiency and performance

This report records controlled measurements from the 2026-07-27
release-candidate pass. Measurements are local observations, not marketing
estimates.

## Context safeguards

New physical launches, including logical-session resumes, apply these guards:

- Bash and LeanCTX shell text above 8,000 aggregate characters is replaced by
  a marked head/tail excerpt. The complete original response is saved first
  as a SHA-256-addressed JSON file in the private run directory's
  `context-artifacts/` (directory mode 0700, files 0600). Claude receives read
  access to that directory. Artifacts have the run directory's lifetime; they
  are not durable project memory. Existing cleanup of a run removes them.
- Exact file reads, small outputs, structured or mixed-media MCP responses,
  MCP errors, and interrupted/image Bash results remain unchanged. Exit-status
  fields are preserved. Storage failures retain the original output rather
  than discard evidence. This is a shell text budget, not a token limit on
  every tool or a guarantee that compaction will never be needed.
- Controller and implementation-worker instructions prefer compressed shell
  output and targeted raw verification. Compaction guidance targets a concise
  handoff preserving approvals, progress, pending work, and evidence paths.
- Two compaction attempts without a successful checkpoint exhaust the automatic
  retry budget for that native conversation. A third automatic attempt is
  blocked with recovery guidance. Manual `/compact` remains available; a new
  user prompt or successful compaction resets the budget. This guard observes
  native hook events, not retries internal to a single provider request.
- Native conversation IDs isolate compaction checkpoints and attempt counters
  for background forks sharing the same logical route.

The DirectAnthropic model path goes straight from Claude to Orichum's route
proxy, retaining route enforcement, LeanCTX, and the logical session header.
Claudex remains the launcher but its five-minute total request deadline no
longer cuts off healthy streams. Claude has a 30-minute request timeout; the
route proxy's five-minute socket-idle timeout remains in force. Disconnects
after output are surfaced without replaying project actions.

These changes do not rewrite existing transcripts or hot-patch running clients.

## Model-aware context windows

New sessions, resumes, and forks encode a per-model native `[1m]` hint for
the exact OpenAI routes `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, and
`gpt-6-astra`. Their documented model capacity is 1,050,000 tokens; the client
uses 1,000,000. Claude strips the hint before sending the model ID, so route
identity and fallback selection remain unchanged. The capacity mapping is
source-backed in `integrations/common/model_context.py`, not inferred from a
model name prefix or a user-assigned alias.

Each controller/worker binding uses the smallest supported window across its
primary and automatic fallback. Unknown models/providers retain the existing
conservative client behavior. A large controller does not enlarge a smaller
specialist. Inherited context/compaction overrides are removed from managed
launches. Orichum supplies a 1M **compaction ceiling**, which Claude clamps to
each model's own capacity; it is not a global model-window override. This
explicit policy enables threshold compaction for `[1m]` aliases instead of
waiting for a provider context error. The status line shows the native capacity
alongside usage, for example `context 41%/1,000k`.

The window is shared by instructions, conversation, tool results, and generated
output; it is not a one-million-token allowance for user messages alone. Native
output reserves and compaction headroom trigger summarization below that limit.

The model specification is not proof of account-specific entitlement. Native
CI verifies the client window, unchanged wire ID, output budgets, repeated
interactive compactions, and a before-output fallback against a local fixture.
It does not claim that a live provider accepted a million-token request or
that a synthetic handoff measures real summarization quality. Larger contexts
can also increase latency and cross provider long-context pricing thresholds.

The provider-free soak is reproducible with:

```bash
ORICHUM_NATIVE_CLAUDE="$(command -v claude)" \
  ORICHUM_CONTEXT_SOAK_TURNS=256 \
  python3 -m unittest tests.test_context_soak_native
```

It exercises the interactive REPL in a private temporary directory, uses
synthetic token counts to trigger full-window transitions, checks that numbered
fixture writes are not repeated, and verifies handoff/checkpoint continuity.

Local verification on 2026-09-09 with Claude Code 2.1.263 completed 256 tool
calls and 32 automatic compactions in approximately 85 seconds, including a
before-output fallback. Every numbered tool completed successfully exactly once,
and each summary retained the fixture handoff. A separate native contract seeded
an exhausted retry history and verified that `PreCompact` blocked compaction
without applying a summary. These are accelerated lifecycle tests, not evidence
of multi-hour stability, live provider capacity, or real summary quality.

## Source-context savings

Token counts use the `o200k_base` tokenizer for a consistent comparison. The
same source bytes were measured before and after LeanCTX processing. These are
dated fixture snapshots from commit
`71ea58280b94788d201c9f362b8582755ff19835`; current files may have changed.

### Large Orichum module

Fixture: `integrations/common/orichum_cli.py`, 2,007 lines at that commit.

| Read mode | Tokens | Reduction from raw |
|---|---:|---:|
| Native/raw | 14,177 | — |
| LeanCTX full | 14,177 | 0% |
| LeanCTX signatures | 1,437 | 89.9% |
| LeanCTX map | 1,558 | 89.0% |

### Small module

Fixture: `integrations/common/route_status.py`, 106 lines and 3,375 bytes at
that commit.

| Read mode | Tokens | Reduction from raw |
|---|---:|---:|
| Native/raw | 727 | — |
| LeanCTX full | 727 | 0% |
| LeanCTX signatures | 295 | 59.4% |
| LeanCTX map | 231 | 68.2% |
| LeanCTX anchored | 1,315 | -80.9% |

Anchored mode is an edit-safety mechanism, not a compression mode. Its hashes
cost tokens but allow `ctx_patch` to reject stale edits. Orichum therefore uses
map/signature/search modes for understanding and anchored mode only before a
patch.

## Tool-schema efficiency

The default `lean` profile keeps four task-execution LeanCTX tools resident in
eligible controller requests:

- LeanCTX read, search, tree, and shell tools.

The `full` profile retains the previous nine-tool resident set by adding
expansion, graph, impact, callgraph, and patch. All eleven bounded MCP tools
remain available in both profiles through provider-native tool search.

`ctx_shell` is the visible default for every finite, non-interactive command,
including unknown or custom CLIs. Orichum's private MCP uses LeanCTX's empty
allowlist override, eliminating rejected discovery calls and per-command
configuration while retaining dangerous-pattern blocking, project jailing,
secret redaction, and Claude Code approval. Native `Bash` is deferred and
loaded only for interactive, streaming, or long-running processes;
LeanCTX-rejected shell behavior; or one explicit fallback. This removes
speculative shell selection without maintaining a command inventory.

Overview and knowledge remain available but are deferred until the controller
needs task orientation or durable history. Native Bash and other native and
project-specific tools are marked for deferred loading, and Claude receives
the tool-search primitive. Atlassian schemas exist only in physical sessions
whose project context declares an account.
Keeping the small execution surface resident makes code routing deterministic
without paying for unused schemas on every turn.

Every Orichum-managed LeanCTX process disables LeanCTX rule-file injection.
Orichum already supplies controller and project steering, so this removes a
duplicate per-request instruction prefix while preserving the same tools,
project jail, shell policy, indexing, semantic search, and shared caches.

This request transform is enabled only for model protocols that Orichum has
verified with Claude Code's tool-search contract. Unknown Kimi, Gemini, or
future model routes are passed through unchanged instead of receiving
Anthropic-specific request fields that could break inference. Provider support
does not by itself imply tool-deferral protocol support.

The exact LeanCTX MCP is intentionally limited to eleven tools. Nine context,
code-intelligence, and knowledge tools are preapproved; patch and shell keep
normal approval. The universal gateway, agent coordination, composition,
autonomy, daemon, and provider paths remain disabled.

Specialists reuse the session's LeanCTX MCP. Read-only specialists receive only
the seven repository-context tools. The implementation worker also receives
anchored patching, observational shell, and its native write tools. Overview
and durable knowledge stay controller-owned so parallel agents do not repeat
the same orientation or mutate shared memory.

## Prompt-cache evidence

A live logical-session resume consumed 807 new input tokens and reused 11,776
cached input tokens. The original session had consumed 24,658 new input tokens.
These prompts were not identical, so this is evidence that resume preserves
provider cache continuity—not a claim of a universal 96.7% saving.

## Live latency and cost samples

| Flow | API duration | Cost | New input | Cached input | Output |
|---|---:|---:|---:|---:|---:|
| GPT controller + LeanCTX read | 12.52 s | $0.134653 | 24,658 | 11,776 | 219 |
| Resume same logical session | 4.40 s | $0.011648 | 807 | 11,776 | 69 |
| GPT/Terra explorer | 12.15 s | $0.055014 | 3,509 | 20,480 | 124 |
| Sonnet critic | 88.28 s | $0.214390 | 13,293 | 10,752 | 187 |
| Opus architect | 34.54 s | $0.207710 | 24,172 | 0 | 170 |
| Verifier + implementation worker | 46.37 s | $0.154055 | 6,187 | 43,008 | 484 |

These are single bounded samples on one network and account state. They show
relative workflow behavior, not guaranteed provider latency or price.
The specialist samples predate specialist LeanCTX enforcement and remain useful
as a baseline, not as the optimized target.

### Post-migration specialist and memory acceptance

The 2026-07-28 live acceptance exercised the enforced specialist tool contract
and the controller-owned overview and knowledge route:

| Flow | Source | Returned | Reduction |
|---|---:|---:|---:|
| Repository explorer bounded read | 2,699 | 161 | 94.0% |
| Verifier, critic, architect, and implementation worker | 10,796 | 100 | 99.1% |
| Controller overview plus read-only knowledge recall | — | — | Not emitted |

These values come from `orichum leanctx stats`. They measure only LeanCTX tool
payloads and should not be interpreted as whole-session billing savings.
Overview and knowledge completed successfully but currently emit zero source
and reduction counters, so Orichum reports them as commands without inventing a
savings percentage.

### Shared wire-proxy acceptance

The 2026-07-28 installed-chain test first sent a normal one-turn request. It
passed through unchanged, as expected: Orichum does not enable free-prose or
system-prompt rewriting, and LeanCTX preserves a fresh cache prefix.

A second request carried a synthetic, non-sensitive Bash result through the
real route proxy, shared LeanCTX proxy, CLIProxyAPI, and GPT model:

| Request | Source bytes | Forwarded bytes | Saved bytes | Estimated tokens saved | Reduction |
|---|---:|---:|---:|---:|---:|
| Structured tool-result fallback | 3,919 | 2,909 | 1,010 | 252 | 25.8% |

The model returned the expected response. Two concurrent Orichum sessions then
completed through the same resident LeanCTX process. These values demonstrate
the fallback behavior that the wire proxy adds; they are not a promise that
every request will compress. `orichum leanctx stats` is the live source of
truth for the installed machine.

## Local resource use

At idle after all sessions exited:

| Shared service | CPU | Resident memory |
|---|---:|---:|
| CLIProxyAPI | 0.0% | 21,120 KiB |
| LeanCTX wire proxy | 0.0% | 19,296 KiB |
| Orichum route proxy | 0.0% | 12,960 KiB |
| Total shared resident footprint | 0.0% | 53,376 KiB (about 52 MiB) |

Exactly one shared LeanCTX proxy and no per-session Claudex translator remained
after two concurrent one-shot sessions exited. LeanCTX MCP processes also end
with their physical session. One-shot live session
process trees peaked at roughly 355–380 MiB RSS in the sampled runs.

The final in-place install/upgrade took 65.03 seconds on an Apple Silicon
Mac with warm package caches.

## Conclusion

The efficient daily-driver path is:

1. defer optional schemas;
2. use LeanCTX map/signature/search for understanding;
3. use anchored reads only for edits;
4. use LeanCTX graph tools for relationships and impact;
5. use LeanCTX overview and knowledge for bounded durable context;
6. let the shared wire proxy compress eligible accumulated fallback context;
7. delegate only when independent specialist work justifies its latency and
   token cost.

This preserves the strong controller and full worker output while using one
context engine on both the tool and wire paths.
