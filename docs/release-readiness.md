# Release readiness

This report records the latest committed native release gates, the carried-
forward consolidated-home acceptance baseline, and current deterministic
validation. It separates observed live evidence from deterministic and
isolated coverage.

## Verdict

The `0.1.0-rc.10` candidate passes native macOS ARM64, native Linux AMD64,
and deterministic regression coverage. The final runtime and native gates ran
against source commit `a4bdea805bb3ba6bfc5c1e02502bb03a405b186e`; the main
contract passed against the same commit. This release adds bounded streaming
recovery and request lifecycle telemetry, simplifies guided configuration,
introduces strict repository-local logical-model assignments, and reduces the
default LeanCTX schema and instruction overhead. The latest provider-backed
request evidence is carried forward from the `0.1.0-rc.4` baseline. The Linux
gate also exercises the WSL-compatible systemd-user contract in an isolated
Ubuntu container; native WSL execution remains a separate release-environment
check.

Every pull request and `main` push runs one fast Linux contract check. The
costlier native macOS ARM64 and Linux AMD64 acceptance workflows remain manual
release gates.

Two intentionally excluded cases are not release blockers:

- real quota exhaustion across multiple accounts;
- Kimi inference with real credentials.

Both were excluded at the user's request. Account selection, priority,
validation, and rollover behavior remain covered deterministically.

## Latest consolidated-home acceptance baseline

The following observed installation and service evidence was recorded on
2026-07-28 for the `0.1.0-rc.3` release baseline:

| Boundary | Observed result |
|---|---|
| macOS ARM64 live install | Upgrade, automatic doctor, and a provider-backed prompt completed; the prompt returned `ORICHUM_FINAL_OK` |
| macOS fast reconcile | A repeat install completed in 7 seconds, retained the same service processes, and kept one physical runtime release |
| Runtime isolation | The launcher and owned services resolved to the verified physical release while mutable state remained under `~/.orichum` |
| Linux/systemd | Fresh and repeat installs completed in an Ubuntu 24.04 systemd-user container; the repeat completed in 7 seconds with one runtime release and no traceback |
| Provider-free install | CLIProxyAPI remained active, the route proxy remained intentionally inactive, and the installer reported the bounded `pending-provider-login` state |
| Migration safety | Consolidated-home migration, failed-install rollback, and retry behavior passed the transaction contract |
| Local regression | The final `0.1.0-rc.10` source passed 638 Python tests and the smoke suite; the native Linux gate passed all eight shell acceptance suites plus the WSL-compatible systemd-user contract on 2026-08-12 |

## 0.1.0-rc.10 release gates

The final deterministic and native gates passed against source commit
`a4bdea805bb3ba6bfc5c1e02502bb03a405b186e`:

| Gate | Result |
|---|---|
| [Main Contract](https://github.com/orichum/orichum/actions/runs/31527650854) | Pass |
| [macOS ARM64 acceptance](https://github.com/orichum/orichum/actions/runs/31530080651) | Pass |
| [Linux AMD64 and WSL-compatible acceptance](https://github.com/orichum/orichum/actions/runs/31527660757) | Pass |

The first post-redesign native attempts exposed stale configure menu labels in
the acceptance scripts. PRs #96 and #97 corrected those test interactions;
the final runs above completed setup, installation, route activation, LeanCTX
verification, fast reconciliation, explicit upgrade, guided configuration,
uninstall, and platform-specific lifecycle coverage.

## 0.1.0-rc.9 release gates

The final deterministic gate passed against source commit
`59fffa466214f4cb861eb42c5f158a9360b023ad`. The native gates passed against
source commit `02e067da0739e1ca6956fab36109783a1c99bf18`; the only later
source change isolates provider-login tests from ambient SSH variables and
does not alter installed runtime behavior. Runtime behavior remains commit
`3b765fcd4ef8354ed94cac20580b4124da724d16`:

| Gate | Result |
|---|---|
| [Main Contract](https://github.com/orichum/orichum/actions/runs/30907073711) | Pass |
| [macOS ARM64 acceptance](https://github.com/orichum/orichum/actions/runs/30905449680) | Pass |
| [Linux AMD64 and WSL-compatible acceptance](https://github.com/orichum/orichum/actions/runs/30905451692) | Pass |

Orichum is licensed under Apache-2.0. Its root `LICENSE` and `NOTICE` files
declare the project terms, while `THIRD_PARTY_NOTICES.md` records the
independent licenses retained by integrated upstream tools.

## Provider-backed feature acceptance

The following feature-level checks were run against the committed release
baseline. The consolidated-home baseline above revalidated installation,
service ownership, health, and one provider-backed controller request.

| Capability | Evidence | Result |
|---|---|---|
| Install and upgrade | Ran `./install.sh` against the existing managed installation; services were reused and the automatic doctor completed | Pass |
| Runtime health | Private Python 3.14.x, CLIProxyAPI, shared LeanCTX proxy, route proxy, Claudex, Claude Code, and LeanCTX MCP passed local component readiness checks | Pass |
| OpenAI controller | GPT 5.6 Sol completed new and resumed logical sessions through Orichum | Pass |
| Anthropic agents | Sonnet 5 critic and Opus 4.8 architect completed bounded delegated work | Pass |
| Antigravity transport | Claude Opus 4.6 Thinking and Gemini 3 Flash returned live acceptance markers through the named Antigravity route | Pass |
| Sessions | New, resume, same-family fork, parent linkage, immutable route display, and concurrent physical state were exercised | Pass |
| Subagents | Explorer, verifier, critic, architect, and implementation-worker roles completed bounded tasks | Pass |
| Project routing | Xebia and Complion resolved independent Jira configurations, GitHub identities, and account pools | Pass |
| GitHub identity | Isolated `GH_CONFIG_DIR` instances resolved `athevar-xebia` and `arvind9981` without changing the user's active account | Pass |
| Atlassian isolation | Hermetic tests proved project-local URLs and tokens; session MCP state contains only the project root | Pass |
| LeanCTX | Exact eleven-tool jailed MCP exposed only the bounded source, graph, overview, and knowledge surface | Pass |
| LeanCTX shell | A fresh session ran `orichum --version` through `ctx_shell` without registering the executable; interpreter inline execution remained blocked | Pass |
| LeanCTX wire path | A real structured tool-result request completed through the installed shared proxy and reduced 3,919 bytes to 2,909 bytes; ordinary fresh prose remained unchanged | Pass |
| LeanCTX specialists | Explorer, verifier, critic, architect, and implementation worker each completed a live bounded read through the shared session MCP | Pass |
| LeanCTX memory route | The controller dynamically loaded deferred overview and knowledge tools, completed task orientation, and performed read-only project recall | Pass |
| Status line | Displayed Orichum, project, stack, active GPT account, route state, context, and quota values | Pass |
| Service lifecycle | One shared LeanCTX proxy served two concurrent sessions; shared services remained healthy and no per-session Claudex translators remained after exit | Pass |

The live provider tests were bounded and did not write to external Jira,
Atlassian, GitHub, or other project services.

The post-migration LeanCTX acceptance on 2026-07-28 measured 94.0% reduction
for a bounded explorer read and 99.1% across the remaining specialist roles.
The controller also completed one overview and one read-only knowledge call;
those tools do not emit source-compression counters. These are LeanCTX
tool-payload measurements, not whole-session provider-token savings.

## Deterministic and isolated acceptance

| Boundary | Coverage |
|---|---|
| Python behavior | Complete local `unittest` discovery, including routing, accounts, sessions, hooks, tool deferral, LeanCTX isolation, and status rendering |
| Shell behavior | All eight suites: smoke, plugin, installer, transaction, route, launcher, completion, and uninstall |
| Installer safety | Fresh install, idempotent upgrade, occupied-port selection, owned-service reuse, foreign-service preservation, and rollback |
| Uninstall | Default and purge behavior in isolated homes; external tools and unrelated services are preserved |
| Linux AMD64 | Native GitHub Actions acceptance plus a privileged Ubuntu systemd-user container |
| WSL2 contract | The Linux systemd-user path plus WSL1 rejection and WSL2 detection fixtures; native WSL execution is a separate release-environment check |
| macOS ARM64 | Native macOS 15 acceptance with launchd service lifecycle |
| Security | Private ownership/modes, no-follow reads, immutable session digests, project jails, strict MCP config, exact tool surfaces, and blocklist-only shell execution with dangerous-pattern enforcement |
