# Changelog

All notable Orichum changes are recorded here.

## Unreleased

### Fixed

- Proxied sessions no longer force a 1,000,000-token context window or delayed
  token-based compaction, which could retain image attachments until their
  serialized request exceeded the 32 MiB proxy limit.

## 0.1.0-rc.19 - 2026-08-20

### Fixed

- Planning subagents now use their own configured model and account route instead
  of inheriting the controller route.

## 0.1.0-rc.18 - 2026-08-19

### Fixed

- CLI tests now isolate their default session environment so active Orichum
  sessions cannot change missing-session test behavior.

## 0.1.0-rc.17 - 2026-08-19

### Fixed

- GPT 5.6 Sol routes now remove unsupported `prompt_cache_retention` before
  forwarding requests, including the tool-optimization retry, while preserving
  the option for other models.
- GPT controller sessions now use the supported 1,000,000-token context window
  and compact at the standard 82% threshold.

## 0.1.0-rc.16 - 2026-08-14

### Changed

- Delegation now adapts to current task evidence and specialist fit rather than
  controller model, provider, fixed keywords, numeric thresholds, or preset
  fan-out.
- Audited investigation and review workflows now use the same adaptive routing
  rule.

## 0.1.0-rc.15 - 2026-08-14

### Changed

- LeanCTX session output, setup diagnostics, and raw configuration inspection
  now retain values needed for local troubleshooting, including database values,
  PII, credentials, URLs, and complete command output.
- Bootstrap now resolves and installs the latest published Orichum release
  rather than checking out `main`.
## 0.1.0-rc.14 - 2026-08-13

### Fixed

- Bootstrap-installed users now receive valid README reconciliation, upgrade,
  uninstall, and purge commands instead of relative checkout paths.
## 0.1.0-rc.13 - 2026-08-13

### Added

- A one-command bootstrap installer clones or updates Orichum, provisions
  supported host prerequisites, installs Claude Code, Codex CLI, uv, and jq,
  then starts the normal installer.

### Changed

- Installation documentation now presents the copy-paste bootstrap command as
  the default path while retaining direct checkout installation for manual use.
## 0.1.0-rc.12 - 2026-08-12

### Added

- `orichum configure` now guides creation, selection, and disabling of private
  named Jira profiles for projects using `.orichum/config.json`; API tokens use
  hidden input and remain outside the repository.

### Changed

- Normal installer output now shows the current operation throughout long
  reconciliation paths and reports the failing operation with its concise,
  actionable reason without requiring `--verbose`.
- `orichum setup` now creates an editable `.orichum/config.json` from the
  recommended stack without overwriting an existing repository configuration.

### Fixed

- Setup reuses its resolved control-plane configuration after creating the
  repository-local file and publishes new project configuration atomically, so
  completed setup and concurrent creators cannot expose partial JSON.

## 0.1.0-rc.11 - 2026-08-12

### Changed

- Project-owned model assignments, Jira profile name, and GitHub account name
  now live together in one `.orichum/config.json`; legacy model-only
  `.orichum/models.json` files remain supported.

## 0.1.0-rc.10 - 2026-08-12

### Added

- Projects can commit a strict `.orichum/models.json` that maps the controller
  and five runtime specialist roles to approved machine-local logical models
  without exposing providers, accounts, credentials, or fallback policy.
- Route requests now carry opaque correlation IDs and redacted lifecycle
  telemetry covering selected routes, response state, byte counts, duration,
  failure stage, and failure kind.

### Changed

- `orichum configure` now uses a compact project dashboard, progressive
  disclosure for model, account, and advanced operations, explicit review and
  apply behavior, and safe pending-change exit handling.
- LeanCTX keeps a smaller default resident tool-schema surface, defers optional
  context tools to provider-native search, disables duplicate rule injection,
  and reports more accurate rolling economics.
- Project model mappings are discovered nearest-first within the configured
  context, converted to ephemeral in-memory stacks, shown as authoritative in
  configure, and applied only to fresh session resolution.

### Fixed

- Streaming responses are prepared within a bounded prelude and may retry once
  on the frozen fallback only before client-visible output; incomplete streams
  after output are reported without replaying model or tool execution.
- SSE validation now handles LF, CRLF, bare-CR, and colonless `data` fields,
  detects premature EOF and advertised-length truncation, and reports exact
  forwarded byte counts.
- Searchable configure menus can no longer accept a hidden default selection
  after filtering.
- Native release acceptance now follows the simplified configure dashboard
  labels and exit position.

## 0.1.0-rc.9 - 2026-08-04

### Changed

- Orichum now installs the latest standard Claudex release from the maintained
  `alupao/claudex` fork while retaining asset checksum verification,
  transactional activation, and rollback.

### Fixed

- Shared upstream ownership attestation is cached for a bounded interval and
  refreshed once across concurrent requests, preventing verifier process
  amplification while remaining fail-closed and invalidating after upstream
  failures.
- Route-state errors now report ownership-verifier duration, and resumed
  sessions print the stable `orichum resume SESSION_ID` command instead of
  lower-level Claudex launch details.

## 0.1.0-rc.8 - 2026-08-03

### Added

- A bounded read-only planning advisor and private per-session compaction
  checkpoints preserve completed investigation metadata across context
  compaction without duplicating prompts or agent results.

### Changed

- Claude Code's built-in `Plan` and `Explore` agents are transparently routed
  to audited Orichum roles, while generic and isolated read-only agents remain
  denied.

## 0.1.0-rc.7 - 2026-08-03

### Added

- A complete installation, setup, and configuration guide covering local and
  SSH authentication, resumable onboarding, every guided configuration path,
  review and repair, practical examples, and recovery.

### Changed

- The README, installation guide, and configuration guide now link to the
  canonical end-to-end setup and configuration guide.

## 0.1.0-rc.6 - 2026-08-03

### Added

- `orichum configure` now provides one guided flow for additional provider
  accounts, explicit same-provider backups, live model selection, controller
  and specialist-role assignments, project stack selection, review, and local
  runtime repair.
- Width-aware numbered and searchable terminal choices, project completion,
  and native macOS/Linux acceptance coverage for ongoing configuration.

### Changed

- Provider authentication is prepared before preview without registering an
  account; reusable authentication is retained securely when configuration is
  cancelled.
- Ongoing account and model documentation now recommends the guided command,
  while low-level commands remain available for advanced placement, secrets,
  recovery, and automation.

### Fixed

- Failed multi-account application compensates every account created earlier
  in the confirmed draft.
- Backup validation covers every live project route, including ordered stack
  fallbacks, and excludes accounts the project does not currently use.
- Existing stack assignments are revalidated against the live project routes
  before persistence.

## 0.1.0-rc.5 - 2026-08-01

### Added

- A resumable `orichum setup` wizard now combines provider authentication,
  named-account registration, runtime reconciliation, project mapping, model
  stack configuration, and the final health check.

### Changed

- GPT controller sessions now use a 400,000-token context ceiling and compact
  at 60% utilization; non-GPT controllers retain their configured context and
  compaction settings.
- The mandatory pull-request and `main` contract now runs complete Python test
  discovery instead of a hand-selected subset.
- Native macOS ARM64 and Linux AMD64 acceptance now exercise guided setup twice
  to verify project onboarding and resumability without duplicate state.

### Fixed

- Schema-bound audited workflows preserve successful sibling results and
  return explicit degraded or failed status when a worker does not produce its
  required structured result.
- Provider setup reuses compatible unregistered authentication without
  exposing private credential filenames or account identity data.

## 0.1.0-rc.4 - 2026-07-29

### Added

- Complete parser-owned help for every public Orichum command path, including
  nested commands.
- Native Bash, zsh, and fish completion generated from the CLI grammar, with
  safe dynamic completion for local stacks, providers, accounts, plugins,
  marketplaces, contexts, sessions, and runs.
- Immutable LeanCTX `lean` and `full` residency profiles plus
  `orichum leanctx economics` rolling and all-time telemetry.
- Managed, SHA-pinned CPU ONNX Runtime provisioning for LeanCTX semantic
  search.

### Changed

- LeanCTX project sessions now reuse a persistent shared model cache while
  retaining private per-run configuration and state.
- LeanCTX indexing remains capped at two threads, uses a 12% soft per-process
  memory target, and lazily downloads the MiniLM semantic model when needed.
- Install, upgrade, rollback, and uninstall now reconcile owned shell
  completion definitions and profile blocks transactionally.

### Fixed

- Fresh installs, repairs, upgrades, and doctor checks now fail early when the
  managed LeanCTX ONNX Runtime is missing or unsafe.
- LeanCTX readiness probes now require a real dense semantic-search result and
  reuse the shared model cache without weakening project isolation.
- Fresh installs and explicit upgrades now pin LeanCTX 3.9.12 instead of
  floating to an unvalidated newer upstream release during the RC4 lifecycle.
- Explicit upgrades refuse to downgrade a newer recorded LeanCTX installation;
  fast repair preserves it until Orichum ships a compatible newer pin.
- LeanCTX economics reports unavailable ROI explicitly instead of presenting
  incomplete telemetry as savings.

## 0.1.0-rc.3 - 2026-07-28

### Added

- One shared, loopback-only LeanCTX wire proxy now compresses eligible model
  history and tool results after Orichum selects the session route.
- `orichum leanctx stats` now separates per-session MCP savings from shared
  wire-proxy request savings.
- `orichum context jira ROOT` stores Jira credentials directly on one private
  project context.
- Project-bound `mcp-atlassian` processes expose Jira reads and writes only in
  sessions whose project declares Jira credentials.
- `orichum sessions remove` and `orichum sessions clear` preview and remove
  inactive logical-session records without deleting Claude Code transcripts or
  LeanCTX project knowledge.

### Changed

- Model catalogue discovery bypasses LeanCTX and continues directly to
  CLIProxyAPI; inference traffic uses the shared optimizer.
- Concurrent sessions share the resident LeanCTX proxy while retaining private
  Claudex translators and project-jailed LeanCTX MCP state.
- Private LeanCTX MCP sessions use blocklist-only shell execution so arbitrary
  finite CLIs work without maintaining an executable-name allowlist; project
  jailing, secret redaction, dangerous-pattern blocking, and Claude Code
  approvals remain active.
- Project configuration now stores optional Jira credentials directly instead
  of a Docker MCP profile or separate Atlassian account registry. Installer
  reconciliation drops old profile and account-ID bindings without guessing
  credentials.

### Fixed

- Bound the LeanCTX proxy ownership verifier into the attested route-runtime
  digest so an altered verifier cannot be accepted as the installed runtime.
- Native release gates now validate the managed LeanCTX proxy port and
  diagnostic-only steps no longer report a second false failure.
- Route health checks no longer perform slow ownership attestations on the
  health endpoint; inference sockets remain fail-closed and attested.
- Runtime creation suppresses Python bytecode so the immutable installed
  release remains content-verifiable.

### Removed

- Docker MCP Toolkit profiles and gateways from Orichum session routing.
  Existing Docker profiles outside Orichum are untouched.

## 0.1.0-rc.2 - 2026-07-28

### Added

- Orichum now installs an allowlisted, content-addressed runtime under
  `~/.orichum/runtime/releases` and activates it through an atomic current
  pointer.
- Existing XDG-based Orichum state is migrated transactionally into the
  consolidated home and restored if installation fails.

### Changed

- Configuration, credentials, sessions, logs, caches, and LeanCTX knowledge
  now live under one configurable `ORICHUM_HOME`, which defaults to
  `~/.orichum`.
- Launchers and owned services bind to a verified physical runtime release;
  the Git checkout is used only as an installation and upgrade source.
- Architecture, installation, configuration, troubleshooting, and CLI
  documentation now describe the consolidated runtime and state layout.
- LeanCTX now owns live code context, repository graphs, task orientation, and
  durable project knowledge through one repo-aware store.
- Every built-in specialist reuses the session's jailed LeanCTX MCP under an
  exact role-specific tool contract.
- LeanCTX monitoring reports only the selected physical run and distinguishes
  MCP registration from real tool activity.
- Project contexts no longer require memory population, palace paths, or wing
  names.
- Deterministic shell routing uses `ctx_shell` for compressed observation and
  native `Bash` for mutations, authentication, and interactive processes.
- User documentation now reflects the consolidated LeanCTX architecture and
  complete Orichum command surface.

### Fixed

- Unknown bare launcher commands now fail closed instead of being forwarded to
  Claude Code as prompts.
- Route-proxy services now retain the selected Orichum data root, including
  relocated macOS, Linux, and CI installations.
- Native acceptance validates the private managed Python and the current
  route-proxy runtime rather than stale system or wrapper paths.
- Fast repeat acceptance now requires routing reuse instead of unnecessary
  repair.
- Provider-free installs no longer emit a routing-fingerprint traceback or
  report intentionally inactive route telemetry as a second failure while
  waiting for the first account login.
- Native acceptance isolates the consolidated home and validates only models
  provided by its disposable OpenAI and Anthropic accounts.
- Nested context and plugin help is delegated to the helper that owns the
  command, so it displays the real options instead of generic passthrough help.

### Removed

- The Mempalace runtime, MCP server, hooks, installer dependency, and project
  configuration fields.

## 0.1.0-rc.1 - 2026-07-27

First release candidate of the unified Orichum harness.

### Added

- Project-aware model stacks spanning GPT, Claude, Google, Kimi, and other
  configured CLIProxyAPI routes.
- Named provider accounts, priorities, account pools, and bounded same-model
  recovery.
- Immutable logical sessions with resume and explicit cross-stack forks.
- Deterministic LeanCTX source, graph, callgraph, and impact routing;
  project-bound Jira configurations; and isolated GitHub identities.
- Interactive provider and model-stack configuration.
- macOS ARM64, Linux AMD64, and WSL2-with-systemd acceptance contracts.

### Known release-candidate exclusions

- Real multi-account quota exhaustion and rollover.
- Kimi inference with real credentials.
